"""Shared storage and JSON export helpers for postal providers."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ref(record: dict[str, Any]) -> str:
    for key in (
        "Ref", "ref", "SiteKey", "Number", "ID", "id", "POSTOFFICE_ID",
        "PO_ID", "CITY_ID", "DISTRICT_ID", "REGION_ID", "POST_CODE",
    ):
        if record.get(key) not in (None, ""):
            return str(record[key])
    return hashlib.sha256(_stable_json(record).encode()).hexdigest()


class ReferenceStore:
    def __init__(self, path: str | Path):
        database = Path(path)
        database.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS references_data (
                resource TEXT NOT NULL, ref TEXT NOT NULL, payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (resource, ref)
            )"""
        )
        self.connection.commit()

    def sync(self, resource: str, records: Iterable[dict[str, Any]], changed_on: str) -> list[dict[str, Any]]:
        incoming = {_ref(record): record for record in records}
        existing = {
            ref: (payload, payload_hash)
            for ref, payload, payload_hash in self.connection.execute(
                "SELECT ref, payload, payload_hash FROM references_data WHERE resource = ?", (resource,)
            )
        }
        if existing and not incoming:
            raise RuntimeError(f"{resource}: empty API response; local data was not changed")
        changed: list[dict[str, Any]] = []
        for ref, record in incoming.items():
            payload = _stable_json(record)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            if existing.get(ref, (None, None))[1] != digest:
                changed.append(record)
            self.connection.execute(
                """INSERT INTO references_data(resource, ref, payload, payload_hash, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(resource, ref) DO UPDATE SET payload=excluded.payload,
                   payload_hash=excluded.payload_hash, updated_at=excluded.updated_at""",
                (resource, ref, payload, digest, changed_on),
            )
        for ref in set(existing) - set(incoming):
            self.connection.execute("DELETE FROM references_data WHERE resource = ? AND ref = ?", (resource, ref))
            changed.append({"Ref": ref, "_deleted": True})
        self.connection.commit()
        return changed

    def all(self, resource: str) -> list[dict[str, Any]]:
        return [json.loads(payload) for (payload,) in self.connection.execute(
            "SELECT payload FROM references_data WHERE resource = ? ORDER BY ref", (resource,)
        )]

    def close(self) -> None:
        self.connection.close()


def write_chunks(output_dir: Path, name: str, records: list[dict[str, Any]], page_size: int, day: str | None = None) -> None:
    if page_size < 1:
        raise ValueError("export page size must be positive")
    prefix = f"{name}_{day}_" if day else f"{name}_"
    pattern = re.compile(rf"^{re.escape(prefix)}\d+\.json$")
    if day:
        for old_file in output_dir.iterdir():
            if old_file.is_file() and pattern.match(old_file.name):
                old_file.unlink()
    for number, start in enumerate(range(0, len(records), page_size), 1):
        (output_dir / f"{prefix}{number}.json").write_text(
            json.dumps(records[start:start + page_size], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def has_full_export(output_dir: Path, name: str) -> bool:
    return (output_dir / f"{name}_1.json").exists()


def export_from_database(config: dict[str, Any], resources: Iterable[str], prefix: str = "") -> dict[str, int]:
    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    store = ReferenceStore(config["database"])
    try:
        counts: dict[str, int] = {}
        for resource in resources:
            name = config.get("output_names", {}).get(resource, resource)
            records = store.all(f"{prefix}{resource}")
            if records:
                write_chunks(output_dir, name, records, config["export_page_size"])
            counts[resource] = len(records)
        return counts
    finally:
        store.close()


def git_commit_and_push(target_dir: str | Path, commit_message: str | None = None) -> bool:
    """Stage, commit and push json files in target_dir if there are any changes."""
    target = Path(target_dir)
    msg = commit_message or f"Update reference data: {target.name}"

    try:
        # Check if inside a git work tree
        subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], check=True, capture_output=True)

        # Stage added/modified json files in target_dir and removed files
        target_pattern = f"{target}/*.json"
        subprocess.run(["git", "add", "--", target_pattern], check=False, capture_output=True)
        # Also stage any deleted json files matching pattern
        subprocess.run(["git", "add", "-u", "--", target_pattern], check=False, capture_output=True)

        # Check if anything is staged for commit
        status = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--", target_pattern],
            capture_output=True, text=True, check=True
        )
        staged_files = [line.strip() for line in status.stdout.splitlines() if line.strip()]
        if not staged_files:
            print(f"Git: no changes to commit in {target}")
            return False

        # Commit
        subprocess.run(["git", "commit", "-m", msg], check=True)
        print(f"Git: committed {len(staged_files)} file(s)")

        # Push if remote exists
        remotes = subprocess.run(["git", "remote"], capture_output=True, text=True, check=True).stdout.strip()
        if not remotes:
            print("Git: remote repository is not configured, skipping git push")
            return False

        subprocess.run(["git", "push"], check=True)
        print("Git: pushed changes to remote")
        return True
    except subprocess.CalledProcessError as err:
        stderr_msg = err.stderr.decode("utf-8") if isinstance(err.stderr, bytes) else str(err.stderr or "")
        print(f"Git warning/error: {err} {stderr_msg}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("Git warning: git command not found", file=sys.stderr)
        return False

