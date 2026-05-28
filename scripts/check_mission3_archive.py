#!/usr/bin/env python3
"""Health checks for the Degen Dogs Mission 3 archive outputs."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "archive" / "mission3"
DATA_DIR = ARCHIVE / "data"
DB_PATH = DATA_DIR / "mission3_archive.sqlite"
GENERATED_DIR = DATA_DIR / "generated"
PUBLIC_DIR = ROOT / "public" / "generated" / "mission3"
INDEXER_PATH = ROOT / "scripts" / "archive_mission3_index.py"

SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
]


def load_indexer():
    spec = importlib.util.spec_from_file_location("archive_mission3_index", INDEXER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load archive_mission3_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def csv_row_count(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as handle:
        return max(sum(1 for _ in handle) - 1, 0)


def json_row_count(path: Path) -> int:
    data = read_json(path)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return len(data)
    raise AssertionError(f"JSON output is not list/dict: {path}")


def scan_secrets(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append(str(path.relative_to(ROOT)))
                break
    return sorted(set(hits))


def check_db(errors: list[str]) -> None:
    if not DB_PATH.exists():
        errors.append(f"missing archive database: {DB_PATH.relative_to(ROOT)}")
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        counts = dict(conn.execute("SELECT metric, value FROM mission3_archive_metrics").fetchall())
        numeric_expectations = {
            "raw_logs": 1,
            "auctions_created": 1,
            "bids": 1,
        }
        for metric, minimum in numeric_expectations.items():
            try:
                value = int(str(counts.get(metric, "0")))
            except ValueError:
                errors.append(f"archive metric is not numeric: {metric}={counts.get(metric)!r}")
                continue
            if value < minimum:
                errors.append(f"archive metric too low: {metric}={value} < {minimum}")
        status = str(counts.get("status", ""))
        if status != "success":
            errors.append(f"archive state is not success: {status!r}")
        gaps = int(str(counts.get("unresolved_gaps", "0") or 0))
        if gaps:
            errors.append(f"archive has unresolved gaps: {gaps}")
        created_tokens = [row[0] for row in conn.execute("SELECT token_id FROM mission3_auction_created ORDER BY token_id")]
        if created_tokens:
            expected = set(range(int(created_tokens[0]), int(created_tokens[-1]) + 1))
            missing = sorted(expected.difference(int(token_id) for token_id in created_tokens))
            if missing:
                preview = ", ".join(str(item) for item in missing[:12])
                suffix = "..." if len(missing) > 12 else ""
                errors.append(f"auction-created token range has {len(missing)} missing token ids: {preview}{suffix}")
    except sqlite3.Error as exc:
        errors.append(f"database health query failed: {exc}")
    finally:
        conn.close()


def check_generated(errors: list[str], *, require_generated: bool) -> list[Path]:
    manifest_path = GENERATED_DIR / "manifest.json"
    checked_paths: list[Path] = []
    if not manifest_path.exists():
        if require_generated:
            errors.append(f"missing archive manifest: {manifest_path.relative_to(ROOT)}")
        return checked_paths

    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != 1 or manifest.get("mission") != 3:
        errors.append("archive manifest schema_version/mission mismatch")
    if not manifest.get("generated_at_utc"):
        errors.append("archive manifest missing generated_at_utc")

    checked_paths.append(manifest_path)
    for item in manifest.get("files", []):
        rel = item.get("path")
        if not rel:
            errors.append(f"manifest file entry missing path: {item}")
            continue
        path = ROOT / rel
        checked_paths.append(path)
        if not path.exists():
            errors.append(f"manifest-listed file missing: {rel}")
            continue
        expected_sha = item.get("sha256")
        if expected_sha and file_sha(path) != expected_sha:
            errors.append(f"sha mismatch for {rel}")
        expected_rows = item.get("rows")
        if isinstance(expected_rows, int):
            try:
                actual_rows = csv_row_count(path) if path.suffix == ".csv" else json_row_count(path)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"row-count check failed for {rel}: {exc}")
                continue
            if actual_rows != expected_rows:
                errors.append(f"row count mismatch for {rel}: manifest={expected_rows} actual={actual_rows}")

    counts = manifest.get("counts") or {}
    for key in ("raw_logs", "auctions_created", "latest_indexed_block", "status"):
        if key not in counts:
            errors.append(f"manifest counts missing {key}")
    return checked_paths


def check_public(errors: list[str]) -> list[Path]:
    expected = {
        "mission3_dog_search_index.json": GENERATED_DIR / "mission3_dog_search_index.json",
        "mission3_archive_metrics.json": GENERATED_DIR / "mission3_archive_metrics.json",
    }
    checked: list[Path] = []
    public_manifest_path = PUBLIC_DIR / "archive_manifest.json"
    checked.append(public_manifest_path)
    if not public_manifest_path.exists():
        errors.append(f"missing public archive file: {public_manifest_path.relative_to(ROOT)}")
    else:
        try:
            public_manifest = read_json(public_manifest_path)
            if public_manifest.get("schema_version") != 1 or public_manifest.get("mission") != 3 or public_manifest.get("public") is not True:
                errors.append("public archive manifest schema_version/mission/public mismatch")
            serialized = json.dumps(public_manifest, sort_keys=True)
            forbidden = ["archive/mission3/data", "mission3_archive.sqlite", "raw_logs_ndjson", "mission3_raw_logs.ndjson"]
            leaked = [item for item in forbidden if item in serialized]
            if leaked:
                errors.append("public archive manifest exposes internal archive paths: " + ", ".join(leaked))
            for item in public_manifest.get("files", []):
                rel = str(item.get("path", ""))
                if not rel.startswith("generated/mission3/"):
                    errors.append(f"public manifest file path is not public-relative: {rel}")
                    continue
                target = ROOT / "public" / rel
                if not target.exists():
                    errors.append(f"public manifest-listed file missing: public/{rel}")
                elif item.get("sha256") and file_sha(target) != item.get("sha256"):
                    errors.append(f"public manifest sha mismatch for public/{rel}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"public archive manifest check failed: {exc}")
    for name, source in expected.items():
        public_path = PUBLIC_DIR / name
        checked.append(public_path)
        if not source.exists():
            continue
        if not public_path.exists():
            errors.append(f"missing public archive file: {public_path.relative_to(ROOT)}")
            continue
        if file_sha(public_path) != file_sha(source):
            errors.append(f"public archive copy differs from generated source: {public_path.relative_to(ROOT)}")
    return checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Mission 3 archive health.")
    parser.add_argument("--rpc", action="store_true", help="Also verify live Base RPC chain and contract code.")
    parser.add_argument("--allow-missing-generated", action="store_true", help="Do not fail if generated archive outputs do not exist yet.")
    parser.add_argument("--skip-db", action="store_true", help="Skip SQLite DB checks.")
    args = parser.parse_args(argv)

    errors: list[str] = []
    indexer = load_indexer()
    try:
        indexer.verify_config(check_rpc=args.rpc)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"config verification failed: {exc}")

    if not args.skip_db:
        check_db(errors)
    checked = check_generated(errors, require_generated=not args.allow_missing_generated)
    checked.extend(check_public(errors))
    secret_hits = scan_secrets(checked)
    if secret_hits:
        errors.append("possible secret pattern in archive outputs: " + ", ".join(secret_hits))

    if errors:
        print("archive_health=failed")
        for error in errors:
            print(f"- {error}")
        return 1
    print("archive_health=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
