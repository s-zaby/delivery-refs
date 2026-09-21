"""Daily synchronisation of Nova Poshta reference data.

Run with: ``python np_sync.py``.  Configuration is read from ``.env``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sync_common import (
    ReferenceStore,
    export_from_database,
    git_commit_and_push,
    has_full_export,
    write_chunks,
)

try:
    from dotenv import load_dotenv
except ImportError:  # makes the script usable with only the Python standard library
    def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
        file = Path(path)
        if not file.exists():
            return
        for line in file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


RESOURCES = {
    "regions": "getAreas",
    # Nova Poshta's current public API has no getDistricts method. Districts
    # are derived from Region/RegionsDescription in the settlements response.
    "districts": None,
    "cities": "getCities",
    "settlements": "getSettlements",
    "warehouses": "getWarehouses",
}
OUTPUT_NAMES = {"cities": "city"}
EXPORT_RESOURCES = tuple(RESOURCES)


class NovaPoshtaClient:
    def __init__(
        self,
        api_key: str,
        endpoint: str,
        timeout: int = 60,
        request_delay: float = 0.3,
        max_retries: int = 5,
        backoff_base: float = 2.0,
    ):
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout
        self.request_delay = max(0.0, request_delay)
        self.max_retries = max(0, max_retries)
        self.backoff_base = max(0.1, backoff_base)
        self._last_request = 0.0

    def _throttle(self) -> None:
        wait = self.request_delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    @staticmethod
    def _is_rate_limited(errors: Any) -> bool:
        text = " ".join(map(str, errors or [])).lower()
        return any(marker in text for marker in ("too many", "rate limit", "429", "throttl"))

    def call(
        self,
        method: str,
        properties: dict[str, Any] | None = None,
        model_name: str = "Address",
    ) -> list[dict[str, Any]]:
        payload = {
            "apiKey": self.api_key,
            "modelName": model_name,
            "calledMethod": method,
            "methodProperties": properties or {},
        }
        for attempt in range(self.max_retries + 1):
            self._throttle()
            request = Request(
                self.endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                if error.code not in (429, 502, 503, 504) or attempt >= self.max_retries:
                    raise RuntimeError(f"{method}: HTTP {error.code}") from error
                time.sleep(self.backoff_base ** attempt)
                continue

            if not body.get("success"):
                errors = body.get("errors") or body.get("warnings") or ["Unknown Nova Poshta API error"]
                if self._is_rate_limited(errors) and attempt < self.max_retries:
                    time.sleep(self.backoff_base ** attempt)
                    continue
                raise RuntimeError(f"{method}: {'; '.join(map(str, errors))}")
            data = body.get("data", [])
            if not isinstance(data, list):
                raise RuntimeError(f"{method}: API returned non-list data")
            return [item for item in data if isinstance(item, dict)]
        raise RuntimeError(f"{method}: retry limit exceeded")

    def fetch_all(
        self, method: str, paginated: bool = True, model_name: str = "Address"
    ) -> list[dict[str, Any]]:
        # Areas and districts do not accept Page/Limit in the NP API.  The
        # larger methods do, so paginate those to avoid silently losing data.
        if not paginated:
            return self.call(method, model_name=model_name)
        limit = int(os.getenv("NP_API_PAGE_SIZE", "500"))
        page = 1
        result: list[dict[str, Any]] = []
        while True:
            batch = self.call(
                method, {"Page": str(page), "Limit": str(limit)}, model_name=model_name
            )
            result.extend(batch)
            if len(batch) < limit:
                return result
            page += 1


def districts_from_settlements(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    districts: dict[str, dict[str, Any]] = {}
    for settlement in records:
        ref = settlement.get("Region")
        if ref:
            districts.setdefault(str(ref), {
                "Ref": ref,
                "Description": settlement.get("RegionsDescription", ""),
                "DescriptionRu": settlement.get("RegionsDescriptionRu", ""),
                "DescriptionTranslit": settlement.get("RegionsDescriptionTranslit", ""),
                "Area": settlement.get("Area"),
                "AreaDescription": settlement.get("AreaDescription"),
            })
    return list(districts.values())


def sync(config: dict[str, Any], run_date: date | None = None) -> dict[str, int]:
    run_date = run_date or date.today()
    day = run_date.strftime("%y%m%d")
    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    store = ReferenceStore(config["database"])
    client = NovaPoshtaClient(
        config["api_key"],
        config["endpoint"],
        config["timeout"],
        config["request_delay"],
        config["max_retries"],
        config["backoff_base"],
    )
    export_page_size = int(config["export_page_size"])
    counts: dict[str, int] = {}
    fetched: dict[str, list[dict[str, Any]]] = {}
    try:
        for resource, method in RESOURCES.items():
            if resource == "districts":
                # Reuse the settlements response; this avoids an unsupported
                # getDistricts request and avoids downloading it twice.
                settlements = fetched.get("settlements")
                if settlements is None:
                    settlements = client.fetch_all("getSettlements", True)
                    fetched["settlements"] = settlements
                records = districts_from_settlements(settlements)
            else:
                records = fetched.get(resource)
                if records is None:
                    paginated = resource in {"cities", "settlements", "warehouses"}
                    records = client.fetch_all(method, paginated)
                    fetched[resource] = records
            # Keep an immutable full snapshot as the baseline. Daily files
            # contain deltas only and must not be used as a complete directory.
            output_name = OUTPUT_NAMES.get(resource, resource)
            if records and not has_full_export(output_dir, output_name):
                write_chunks(output_dir, output_name, records, export_page_size)

            changed = store.sync(resource, records, run_date.isoformat())
            counts[resource] = len(changed)
            if changed:
                write_chunks(output_dir, output_name, changed, export_page_size, day)
    finally:
        store.close()
    return counts


def config_from_env(require_api_key: bool = True) -> dict[str, Any]:
    load_dotenv()
    api_key = os.getenv("NOVA_POSHTA_API_KEY") or os.getenv("NP_API_KEY")
    if require_api_key and not api_key:
        raise ValueError("NOVA_POSHTA_API_KEY is not set in .env")
    return {
        "api_key": api_key or "",
        "endpoint": os.getenv("NOVA_POSHTA_API_URL", "https://api.novaposhta.ua/v2.0/json/"),
        "database": os.getenv("NP_DATABASE", "data/delivery.sqlite3"),
        "output_dir": os.getenv("NP_OUTPUT_DIR", "data/np"),
        "timeout": int(os.getenv("NP_API_TIMEOUT", "60")),
        # One request every 300 ms plus exponential retry backoff protects the
        # API when a large paginated reference is being downloaded.
        "request_delay": float(os.getenv("NP_REQUEST_DELAY", "0.3")),
        "max_retries": int(os.getenv("NP_MAX_RETRIES", "5")),
        "backoff_base": float(os.getenv("NP_BACKOFF_BASE", "2")),
        "export_page_size": int(os.getenv("NP_EXPORT_PAGE_SIZE", "5000")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Nova Poshta reference data")
    parser.add_argument("--date", help="Date in YYYY-MM-DD format (for a repeatable run)")
    parser.add_argument(
        "--from-db", action="store_true",
        help="Generate complete paginated reference files from SQLite without API requests",
    )
    args = parser.parse_args()
    try:
        config = config_from_env(require_api_key=not args.from_db)
        if args.from_db:
            counts = export_from_database(
                {**config, "output_names": OUTPUT_NAMES}, RESOURCES
            )
        else:
            run_date = date.fromisoformat(args.date) if args.date else None
            counts = sync(config, run_date)
        git_commit_and_push(config["output_dir"], commit_message=f"Update Nova Poshta data: {date.today().isoformat()}")
    except Exception as error:
        print(f"sync failed: {error}", file=sys.stderr)
        return 1
    print("; ".join(f"{name}: {count}" for name, count in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
