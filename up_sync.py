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

    def fetch_all(self) -> dict[str, list[dict[str, Any]]]:
        regions = self._entries(self._call("get_regions_by_region_ua", {"region_name": ""}))
        for region in regions:
            if region.get("REGION_ID"):
                region.setdefault("Ref", region["REGION_ID"])
        districts: list[dict[str, Any]] = []
        cities: list[dict[str, Any]] = []
        postoffices: list[dict[str, Any]] = []
        seen = {name: set() for name in ("districts", "cities", "postoffices")}
        for region in regions:
            region_id = region.get("REGION_ID") or region.get("region_id")
            if not region_id:
                continue
            region_districts = self._entries(self._call(
                "get_districts_by_region_id", {"region_id": region_id}
            ))
            for district in region_districts or [{}]:
                district_id = district.get("DISTRICT_ID") or district.get("district_id")
                if district:
                    district.setdefault("REGION_ID", region_id)
                    if district_id:
                        district.setdefault("Ref", district_id)
                    ref = _ref(district)
                    if ref not in seen["districts"]:
                        seen["districts"].add(ref)
                        districts.append(district)
                params = {"region_id": region_id}
                if district_id:
                    params["district_id"] = district_id
                for city in self._entries(self._call(
                    "get_city_by_region_id_and_district_id", params
                )):
                    city.setdefault("REGION_ID", region_id)
                    if district_id:
                        city.setdefault("DISTRICT_ID", district_id)
                    city_id = city.get("CITY_ID") or city.get("city_id")
                    if city_id:
                        city.setdefault("Ref", city_id)
                    ref = _ref(city)
                    if ref not in seen["cities"]:
                        seen["cities"].add(ref)
                        cities.append(city)
                    if city_id:
                        for office in self._entries(self._call(
                            "get_postoffices_by_city_id", {"city_id": city_id}
                        )):
                            office.setdefault("CITY_ID", city_id)
                            office_id = (office.get("POSTOFFICE_ID") or office.get("PO_ID")
                                          or office.get("ID") or office.get("id"))
                            if office_id:
                                office.setdefault("Ref", office_id)
                            ref = _ref(office)
                            if ref not in seen["postoffices"]:
                                seen["postoffices"].add(ref)
                                postoffices.append(office)
        return {"regions": regions, "districts": districts,
                "cities": cities, "postoffices": postoffices}


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
    try:
        counts: dict[str, int] = {}
        for resource, records in client.fetch_all().items():
            name = OUTPUT_NAMES.get(resource, resource)
            if records and not has_full_export(output_dir, name):
                write_chunks(output_dir, name, records, config["export_page_size"])
            changed = store.sync(f"ukrposhta_{resource}", records, run_date.isoformat())
            counts[resource] = len(changed)
            if changed:
                write_chunks(output_dir, name, changed, config["export_page_size"], day)
        return counts
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Ukrposhta reference data")
    parser.add_argument("--date", help="Date in YYYY-MM-DD format")
    parser.add_argument("--from-db", action="store_true",
                        help="Generate complete files from SQLite without API requests")
    args = parser.parse_args()
    try:
        config = config_from_env(not args.from_db)
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
