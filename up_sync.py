"""Daily Ukrposhta reference synchronizer."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sync_common import (
    ReferenceStore,
    _ref,
    export_from_database,
    git_commit_and_push,
    has_full_export,
    write_chunks,
)

OUTPUT_NAMES = {"cities": "city"}
RESOURCES = ("regions", "districts", "cities", "postoffices")


class UkrPoshtaClient:
    def __init__(self, token: str, endpoint: str, timeout: int = 60,
                 request_delay: float = 0.3, max_retries: int = 5,
                 backoff_base: float = 2.0):
        self.token = token
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.request_delay = max(0.0, request_delay)
        self.max_retries = max(0, max_retries)
        self.backoff_base = max(0.1, backoff_base)
        self._last_request = 0.0

    def _call(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.endpoint}/{path.lstrip('/')}"
        if params:
            url += "?" + urlencode(params)
        for attempt in range(self.max_retries + 1):
            wait = self.request_delay - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            request = Request(url, headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            })
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                if isinstance(body, dict) and body.get("error"):
                    raise RuntimeError(str(body["error"]))
                return body
            except (HTTPError, RuntimeError) as error:
                status = getattr(error, "code", 0)
                text = str(error).lower()
                retryable = status in (429, 502, 503, 504) or any(
                    marker in text for marker in ("too many", "rate limit", "throttl")
                )
                if not retryable or attempt >= self.max_retries:
                    raise RuntimeError(f"Ukrposhta {path}: {error}") from error
                time.sleep(self.backoff_base ** attempt)
        raise RuntimeError(f"Ukrposhta {path}: retry limit exceeded")

    @staticmethod
    def _entries(body: Any) -> list[dict[str, Any]]:
        value = body
        if isinstance(value, dict):
            value = value.get("Entries", value.get("entries", value))
        if isinstance(value, dict):
            value = value.get("Entry", value.get("entry", []))
        if isinstance(value, dict):
            value = [value]
        return [item for item in (value or []) if isinstance(item, dict)]

    def fetch_regions(self) -> list[dict[str, Any]]:
        regions = self._entries(self._call("get_regions_by_region_ua", {"region_name": ""}))
        for region in regions:
            if region.get("REGION_ID"):
                region.setdefault("Ref", region["REGION_ID"])
        return regions

    def fetch_districts(self, region_id: str) -> list[dict[str, Any]]:
        try:
            return self._entries(self._call(
                "get_districts_by_region_id_and_district_ua", {"region_id": region_id}
            ))
        except Exception:
            try:
                return self._entries(self._call(
                    "get_districts_by_region_id", {"region_id": region_id}
                ))
            except Exception:
                return []

    def fetch_cities(self, region_id: str, district_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"region_id": region_id}
        if district_id:
            params["district_id"] = district_id
        try:
            return self._entries(self._call(
                "get_city_by_region_id_and_district_id_and_city_ua", params
            ))
        except Exception:
            try:
                return self._entries(self._call(
                    "get_city_by_region_id_and_district_id", params
                ))
            except Exception:
                return []

    def fetch_postoffices(self, city_id: str) -> list[dict[str, Any]]:
        try:
            return self._entries(self._call(
                "get_postoffices_by_city_id", {"city_id": city_id}
            ))
        except Exception:
            return []


def fill_districts_from_data(
    districts: list[dict[str, Any]],
    cities: list[dict[str, Any]],
    postoffices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Populate/supplement districts list using district attributes found in cities and postoffices."""
    result: dict[str, dict[str, Any]] = {}
    for d in districts:
        ref = _ref(d)
        result[ref] = d

    for item in (*cities, *postoffices):
        district_id = item.get("DISTRICT_ID") or item.get("district_id")
        district_name = (
            item.get("DISTRICT_UA") or item.get("district_ua")
            or item.get("DISTRICT_NAME") or item.get("district_name")
            or item.get("DISTRICT_RU") or item.get("district_ru")
            or item.get("DISTRICT_EN") or item.get("district_en")
        )
        if not district_id and not district_name:
            continue

        ref = str(district_id) if district_id else f"{item.get('REGION_ID', '')}_{district_name}"
        if ref not in result:
            district_entry: dict[str, Any] = {
                "Ref": ref,
            }
            if district_id:
                district_entry["DISTRICT_ID"] = district_id
            if item.get("REGION_ID"):
                district_entry["REGION_ID"] = item["REGION_ID"]
            if district_name:
                district_entry["DISTRICT_UA"] = district_name
            for key in ("DISTRICT_RU", "DISTRICT_EN", "DISTRICT_KOATUU", "DISTRICT_KATOTTG"):
                val = item.get(key) or item.get(key.lower())
                if val:
                    district_entry[key] = val
            result[ref] = district_entry

    return list(result.values())


def config_from_env(require_token: bool = True) -> dict[str, Any]:
    # Load dotenv through the same dependency/fallback as the main script.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        env = Path(".env")
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    token = os.getenv("UKRPOSHTA_API_TOKEN") or os.getenv("UKRPOSHTA_TOKEN")
    if require_token and not token:
        raise ValueError("UKRPOSHTA_API_TOKEN is not set in .env")
    return {
        "api_token": token or "",
        "endpoint": os.getenv("UKRPOSHTA_API_URL", "https://www.ukrposhta.ua/address-classifier-ws"),
        "database": os.getenv("UP_DATABASE", "data/delivery.sqlite3"),
        "output_dir": os.getenv("UP_OUTPUT_DIR", "data/up"),
        "timeout": int(os.getenv("NP_API_TIMEOUT", "60")),
        "request_delay": float(os.getenv("NP_REQUEST_DELAY", "0.3")),
        "max_retries": int(os.getenv("NP_MAX_RETRIES", "5")),
        "backoff_base": float(os.getenv("NP_BACKOFF_BASE", "2")),
        "export_page_size": int(os.getenv("NP_EXPORT_PAGE_SIZE", "5000")),
        "output_names": OUTPUT_NAMES,
    }


def sync(config: dict[str, Any], run_date: date | None = None) -> dict[str, int]:
    run_date = run_date or date.today()
    day = run_date.strftime("%y%m%d")
    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    client = UkrPoshtaClient(config["api_token"], config["endpoint"], config["timeout"],
                             config["request_delay"], config["max_retries"], config["backoff_base"])
    store = ReferenceStore(config["database"])

    STATE_PREFIX = "up_sync_"
    KEY_DATE = f"{STATE_PREFIX}date"
    KEY_PROCESSED_REGIONS = f"{STATE_PREFIX}processed_regions"
    KEY_PROCESSED_CITIES = f"{STATE_PREFIX}processed_cities"
    KEY_ACTIVE_REFS = f"{STATE_PREFIX}active_refs"
    KEY_CHANGES = f"{STATE_PREFIX}changes"

    try:
        current_sync_date = store.get_state(KEY_DATE)
        if current_sync_date != run_date.isoformat():
            # Starting a new sync session for this date: clear any leftover state from previous dates
            store.clear_state(STATE_PREFIX)
            store.set_state(KEY_DATE, run_date.isoformat())
            processed_regions: set[str] = set()
            processed_cities: set[str] = set()
            active_refs: dict[str, list[str]] = {r: [] for r in RESOURCES}
            counts: dict[str, int] = {r: 0 for r in RESOURCES}
        else:
            # Resuming an interrupted sync for the same date
            processed_regions = set(store.get_state(KEY_PROCESSED_REGIONS, []))
            processed_cities = set(store.get_state(KEY_PROCESSED_CITIES, []))
            active_refs = store.get_state(KEY_ACTIVE_REFS, {r: [] for r in RESOURCES})
            counts = store.get_state(KEY_CHANGES, {r: 0 for r in RESOURCES})
            print(f"Resuming Ukrposhta sync for {run_date.isoformat()}: "
                  f"{len(processed_regions)} region(s) already processed.")

        # 1. Fetch regions
        regions = client.fetch_regions()
        for r in regions:
            r_id = r.get("REGION_ID")
            if r_id:
                r.setdefault("Ref", r_id)
        changed_regions = store.upsert_records("ukrposhta_regions", regions, run_date.isoformat())
        counts["regions"] += len(changed_regions)
        active_refs["regions"] = [_ref(r) for r in regions]
        store.set_state(KEY_ACTIVE_REFS, active_refs)
        store.set_state(KEY_CHANGES, counts)

        # 2. Iterate regions and districts
        for region in regions:
            region_id = str(region.get("REGION_ID") or region.get("region_id"))
            if not region_id or region_id in processed_regions:
                continue

            region_name = region.get("REGION_UA", region_id)
            print(f"Syncing region: {region_name} (ID: {region_id})...")

            # Fetch districts for this region
            region_districts = client.fetch_districts(region_id)
            for district in region_districts:
                d_id = district.get("DISTRICT_ID") or district.get("district_id")
                district.setdefault("REGION_ID", region_id)
                if d_id:
                    district.setdefault("Ref", d_id)
                ref = _ref(district)
                if ref not in active_refs["districts"]:
                    active_refs["districts"].append(ref)

            changed_districts = store.upsert_records("ukrposhta_districts", region_districts, run_date.isoformat())
            counts["districts"] += len(changed_districts)

            district_ids = [d.get("DISTRICT_ID") or d.get("district_id") for d in region_districts]
            district_targets = [d_id for d_id in district_ids if d_id] or [None]

            region_cities: list[dict[str, Any]] = []
            for d_id in district_targets:
                cities = client.fetch_cities(region_id, d_id)
                for city in cities:
                    city.setdefault("REGION_ID", region_id)
                    if d_id:
                        city.setdefault("DISTRICT_ID", d_id)
                    city_id = city.get("CITY_ID") or city.get("city_id")
                    if city_id:
                        city.setdefault("Ref", city_id)
                    ref = _ref(city)
                    if ref not in active_refs["cities"]:
                        active_refs["cities"].append(ref)
                    region_cities.append(city)

            changed_cities = store.upsert_records("ukrposhta_cities", region_cities, run_date.isoformat())
            counts["cities"] += len(changed_cities)

            # Derive and upsert any additional districts found from city attributes
            extra_districts = fill_districts_from_data([], region_cities, [])
            if extra_districts:
                for ed in extra_districts:
                    ref = _ref(ed)
                    if ref not in active_refs["districts"]:
                        active_refs["districts"].append(ref)
                changed_extra = store.upsert_records("ukrposhta_districts", extra_districts, run_date.isoformat())
                counts["districts"] += len(changed_extra)

            # Fetch postoffices for cities in this region
            region_offices: list[dict[str, Any]] = []
            for city in region_cities:
                city_id = str(city.get("CITY_ID") or city.get("city_id") or "")
                if not city_id or city_id in processed_cities:
                    continue
                offices = client.fetch_postoffices(city_id)
                for office in offices:
                    office.setdefault("CITY_ID", city_id)
                    office.setdefault("REGION_ID", region_id)
                    if city.get("DISTRICT_ID"):
                        office.setdefault("DISTRICT_ID", city["DISTRICT_ID"])
                    office_id = (office.get("POSTOFFICE_ID") or office.get("PO_ID")
                                  or office.get("ID") or office.get("id"))
                    if office_id:
                        office.setdefault("Ref", office_id)
                    ref = _ref(office)
                    if ref not in active_refs["postoffices"]:
                        active_refs["postoffices"].append(ref)
                    region_offices.append(office)

                processed_cities.add(city_id)

            if region_offices:
                changed_offices = store.upsert_records("ukrposhta_postoffices", region_offices, run_date.isoformat())
                counts["postoffices"] += len(changed_offices)

            # Checkpoint after each completed region
            processed_regions.add(region_id)
            store.set_state(KEY_PROCESSED_REGIONS, list(processed_regions))
            store.set_state(KEY_PROCESSED_CITIES, list(processed_cities))
            store.set_state(KEY_ACTIVE_REFS, active_refs)
            store.set_state(KEY_CHANGES, counts)

        # 3. All regions completed successfully: handle deletions
        for resource in RESOURCES:
            table_name = f"ukrposhta_{resource}"
            del_changed = store.mark_deleted(table_name, active_refs[resource])
            counts[resource] += len(del_changed)

        # 4. Export JSON files (full baseline and daily deltas)
        for resource in RESOURCES:
            table_name = f"ukrposhta_{resource}"
            name = OUTPUT_NAMES.get(resource, resource)
            all_records = store.all(table_name)
            if all_records and not has_full_export(output_dir, name):
                write_chunks(output_dir, name, all_records, config["export_page_size"])

            # Daily delta export if there were changes today
            if counts[resource] > 0:
                daily_records = [
                    json.loads(p) for (p,) in store.connection.execute(
                        "SELECT payload FROM references_data WHERE resource = ? AND updated_at = ? ORDER BY ref",
                        (table_name, run_date.isoformat())
                    )
                ]
                if daily_records:
                    write_chunks(output_dir, name, daily_records, config["export_page_size"], day)

        # Sync finished cleanly: clear checkpoint state
        store.clear_state(STATE_PREFIX)
        return counts
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Ukrposhta reference data")
    parser.add_argument("--date", help="Date in YYYY-MM-DD format")
    parser.add_argument("--from-db", action="store_true",
                        help="Generate complete files from SQLite without API requests")
    parser.add_argument("--reset", action="store_true",
                        help="Reset any saved checkpoint and restart sync from scratch")
    args = parser.parse_args()
    try:
        config = config_from_env(not args.from_db)
        if args.reset:
            store = ReferenceStore(config["database"])
            store.clear_state("up_sync_")
            store.close()
            print("Reset Ukrposhta checkpoint state.")
        counts = (export_from_database(config, RESOURCES, "ukrposhta_") if args.from_db
                  else sync(config, date.fromisoformat(args.date) if args.date else None))
        git_commit_and_push(config["output_dir"], commit_message=f"Update Ukrposhta data: {date.today().isoformat()}")
    except Exception as error:
        print(f"sync failed: {error}", file=sys.stderr)
        return 1
    print("; ".join(f"{name}: {count}" for name, count in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
