#!/usr/bin/env python3
"""Regenerate and validate deterministic Mission 2 archive artifacts.

The network fetch happens in archive_mission2_index.py. This build step uses the
local SQLite archive to refresh derived CSV/JSON exports, rewrite manifests with
current hashes, and fail if required archive files are missing or inconsistent.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "archive" / "mission2" / "data" / "sqlite" / "mission2.sqlite"
SQLITE_ALIAS = ROOT / "archive" / "mission2" / "data" / "mission2_archive.sqlite"
GENERATED = ROOT / "archive" / "mission2" / "data" / "generated"
RAW = ROOT / "archive" / "mission2" / "data" / "raw"
INDEXER_PATH = ROOT / "scripts" / "archive_mission2_index.py"


def load_indexer() -> Any:
    spec = importlib.util.spec_from_file_location("archive_mission2_index", INDEXER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to import {INDEXER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def latest_run(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT run_id, run_timestamp_utc, chain_id, rpc_url, from_block, to_block, "
        "auction_house_address, config_confidence, raw_log_path, warning "
        "FROM mission2_index_runs ORDER BY run_timestamp_utc DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise RuntimeError("mission2_index_runs is empty; run archive:mission2:index first")
    return {
        "run_id": row[0],
        "run_timestamp_utc": row[1],
        "chain_id": row[2],
        "rpc_used": row[3],
        "from_block": row[4],
        "to_block": row[5],
        "auction_house_address": row[6],
        "config_confidence": row[7],
        "raw_log_path": row[8],
        "warning": row[9],
    }


def validate_manifest_hashes(indexer: Any, manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for item in manifest.get("generated_files", []):
        for path_key, hash_key in (("csv_path", "csv_sha256"), ("json_path", "json_sha256")):
            rel = item.get(path_key)
            expected = item.get(hash_key)
            if not rel:
                continue
            path = ROOT / rel
            if not path.exists():
                errors.append(f"missing manifest file: {rel}")
                continue
            actual = indexer.sha256_file(path)
            if expected != actual:
                errors.append(f"sha256 mismatch for {rel}: manifest={expected} actual={actual}")
    for path_key in ("raw_log_file", "sqlite_file", "sqlite_archive_alias"):
        item = manifest.get(path_key)
        if not item:
            errors.append(f"manifest missing {path_key}")
            continue
        rel = item.get("path")
        expected = item.get("sha256")
        path = ROOT / rel
        if not path.exists():
            errors.append(f"missing {path_key}: {rel}")
            continue
        actual = indexer.sha256_file(path)
        if expected != actual:
            errors.append(f"sha256 mismatch for {rel}: manifest={expected} actual={actual}")
    sqlite_hash = manifest.get("sqlite_file", {}).get("sha256")
    alias_hash = manifest.get("sqlite_archive_alias", {}).get("sha256")
    if sqlite_hash != alias_hash:
        errors.append("sqlite_archive_alias must be byte-identical to sqlite_file")
    return errors


def main() -> int:
    indexer = load_indexer()
    required = [
        DB,
        SQLITE_ALIAS,
        RAW / "mission2_auction_logs.meta.json",
        RAW / "mission2_rpc_failures.json",
        RAW / "mission2_index_gaps.csv",
        GENERATED / "reconciliation_summary.json",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.exists()]
    if missing:
        print(json.dumps({"status": "missing_required_files", "missing": missing}, indent=2))
        return 1

    conn = sqlite3.connect(DB)
    try:
        exports = indexer.export_outputs(conn)
        counts = indexer.row_counts(conn, [
            "mission2_raw_logs",
            "mission2_auction_created",
            "mission2_auction_bids",
            "mission2_auction_extended",
            "mission2_auction_settled",
            "mission2_parameter_updates",
            "mission2_woof_vault_allocations",
            "mission2_index_gaps",
        ])
        run = latest_run(conn)
    finally:
        conn.close()

    raw_log_path = ROOT / run["raw_log_path"]
    manifest = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "run_timestamp_utc": run["run_timestamp_utc"],
        "chain_id": run["chain_id"],
        "network": "Degen Chain",
        "rpc_used": run["rpc_used"],
        "from_block": run["from_block"],
        "to_block": run["to_block"],
        "auction_house_address": run["auction_house_address"],
        "config_confidence": run["config_confidence"],
        "dune_reconciliation_status": "not_recovered",
        "warnings": [
            run["warning"],
            "Dune query IDs, official SQL, and official Dune result exports remain unrecovered.",
        ],
        "event_topics": indexer.event_topics(indexer.load_json(indexer.CONFIG / "mission2_event_abis.json")),
        "row_counts": counts,
        "source_files_used": [
            "archive/mission2/config/mission2_chain.verified.json",
            "archive/mission2/config/mission2_contracts.verified.json",
            "archive/mission2/config/mission2_blocks.verified.json",
            "archive/mission2/config/mission2_event_abis.json",
            "archive/mission2/config/woof_vault_allocations.json",
            "archive/mission2/sql/schema.sql",
            "archive/mission2/sql/marts.sql",
        ],
        "raw_log_file": {
            "path": str(raw_log_path.relative_to(ROOT)),
            "sha256": indexer.sha256_file(raw_log_path),
            "rows": counts["mission2_raw_logs"],
        },
        "sqlite_file": {
            "path": str(DB.relative_to(ROOT)),
            "sha256": indexer.sha256_file(DB),
        },
        "sqlite_archive_alias": {
            "path": str(SQLITE_ALIAS.relative_to(ROOT)),
            "sha256": indexer.sha256_file(SQLITE_ALIAS),
        },
        "generated_files": exports,
        "known_gaps": [],
    }
    indexer.write_json(GENERATED / "mission2_archive_manifest.json", manifest)
    indexer.write_json(GENERATED / "manifest.json", manifest)

    errors = validate_manifest_hashes(indexer, manifest)
    if errors:
        print(json.dumps({"status": "manifest_validation_failed", "errors": errors}, indent=2))
        return 1
    print(json.dumps({"status": "ok", "confidence": manifest["config_confidence"], "row_counts": counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
