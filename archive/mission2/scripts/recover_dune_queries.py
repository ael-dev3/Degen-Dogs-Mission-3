#!/usr/bin/env python3
"""Recover known Mission 2 Dune queries through the official Dune API.

This script does not discover unknown query IDs. Add IDs to
archive/mission2/dune/query_ids.json first, then run with DUNE_API_KEY set.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
ARCHIVE = ROOT / "archive" / "mission2"
DUNE = ARCHIVE / "dune"
QUERY_IDS = DUNE / "query_ids.json"
QUERIES_DIR = DUNE / "queries"
RESULTS_DIR = DUNE / "results"
CONSTANTS_PATH = DUNE / "hardcoded_constants.json"
API = "https://api.dune.com/api/v1"

ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
TOPIC_RE = re.compile(r"0x[a-fA-F0-9]{64}")
QUERY_ID_RE = re.compile(r"^[0-9]+$")
TABLE_RE = re.compile(r"\b(?:degen|dex|tokens|nft|superfluid)\.[A-Za-z_][A-Za-z0-9_]*\b", re.I)
BLOCK_RE = re.compile(r"\b(?:block_number|evt_block_number|block)\s*(?:>=|>|=|between)\s*([0-9]{4,})", re.I)
DOG_RE = re.compile(r"\b(?:dog_id|dogid|token_id|tokenid)\s*(?:=|in|between|>=|<=|>|<)\s*([0-9][0-9,\s]*)", re.I)
DATE_RE = re.compile(r"\b20[0-9]{2}-[0-9]{2}-[0-9]{2}\b")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return value or "query"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def dune_get(path: str, api_key: str) -> Any:
    req = urllib.request.Request(
        f"{API}{path}",
        headers={"X-Dune-API-Key": api_key, "User-Agent": "mission2-dune-recovery/0.1"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def query_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for entry in data.get("queries", []):
        if isinstance(entry, dict):
            qid = entry.get("query_id") or entry.get("id") or entry.get("queryId")
            title = entry.get("title") or entry.get("name") or f"query_{qid}"
        else:
            qid = entry
            title = f"query_{qid}"
        if qid in (None, ""):
            continue
        qid_text = str(qid).strip()
        if not QUERY_ID_RE.match(qid_text):
            raise SystemExit(f"Unsafe/non-numeric Dune query ID in query_ids.json: {qid_text!r}")
        out.append({"query_id": qid_text, "title": str(title), "slug": slugify(str(title))})
    return out


def extract_constants(sql: str) -> dict[str, list[str]]:
    return {
        "contracts": sorted(set(m.group(0).lower() for m in ADDRESS_RE.finditer(sql))),
        "event_topics": sorted(set(m.group(0).lower() for m in TOPIC_RE.finditer(sql))),
        "table_names": sorted(set(m.group(0) for m in TABLE_RE.finditer(sql))),
        "block_ranges": sorted(set(m.group(0) for m in BLOCK_RE.finditer(sql))),
        "dog_id_filters": sorted(set(m.group(0) for m in DOG_RE.finditer(sql))),
        "date_ranges": sorted(set(m.group(0) for m in DATE_RE.finditer(sql))),
    }


def main() -> int:
    api_key = os.environ.get("DUNE_API_KEY")
    data = load_json(QUERY_IDS)
    entries = query_entries(data)
    if not entries:
        print("No Mission 2 Dune query IDs recorded yet. Open Dune UI, find the `Degen Dogs Mission 2` dashboard by `ael_dev`, record query IDs in archive/mission2/dune/query_ids.json, then rerun.")
        return 0
    if not api_key:
        raise SystemExit("DUNE_API_KEY is required to fetch official Dune query SQL.")

    QUERIES_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    constants = {
        "generated_at": utc_now(),
        "status": "recovered_from_recorded_query_ids",
        "dashboard_title": data.get("dashboard_title"),
        "owner": data.get("owner"),
        "contracts": [],
        "event_topics": [],
        "table_names": [],
        "block_ranges": [],
        "dog_id_filters": [],
        "wallet_lists": [],
        "token_addresses": [],
        "superfluid_addresses": [],
        "date_ranges": [],
        "assumptions": [],
        "queries": [],
    }
    merged = {k: set() for k in ["contracts", "event_topics", "table_names", "block_ranges", "dog_id_filters", "date_ranges"]}
    updated_queries = []

    for entry in entries:
        qid = entry["query_id"]
        title = entry["title"]
        slug = entry["slug"]
        meta = dune_get(f"/query/{urllib.parse.quote(qid)}", api_key)
        query = meta.get("query") if isinstance(meta.get("query"), dict) else meta
        sql = query.get("query_sql") or query.get("sql") or query.get("sql_query") or meta.get("query_sql") or meta.get("sql")
        official_title = query.get("name") or query.get("title") or title
        slug = slugify(official_title)
        sql_path = QUERIES_DIR / f"{qid}_{slug}.sql"
        meta_path = QUERIES_DIR / f"{qid}_{slug}.metadata.json"
        if not sql:
            raise RuntimeError(f"Dune query {qid} response did not include SQL text")
        sql_path.write_text(sql.rstrip() + "\n", encoding="utf-8")
        write_json(meta_path, meta)

        result_path = None
        try:
            result = dune_get(f"/query/{urllib.parse.quote(qid)}/results", api_key)
            result_path = RESULTS_DIR / f"{qid}_{slug}.json"
            write_json(result_path, result)
        except urllib.error.HTTPError as exc:
            if exc.code not in (402, 403, 404, 429):
                raise
            result_path = None
        found = extract_constants(sql)
        for key, values in found.items():
            merged[key].update(values)
        constants["queries"].append({
            "query_id": qid,
            "title": official_title,
            "sql_path": str(sql_path.relative_to(ROOT)),
            "metadata_path": str(meta_path.relative_to(ROOT)),
            "result_path": str(result_path.relative_to(ROOT)) if result_path else None,
            "extracted": found,
        })
        updated_queries.append({
            **entry,
            "title": official_title,
            "sql_path": str(sql_path.relative_to(ROOT)),
            "metadata_path": str(meta_path.relative_to(ROOT)),
            "result_path": str(result_path.relative_to(ROOT)) if result_path else None,
            "status": "recovered",
        })
        time.sleep(0.2)

    for key, values in merged.items():
        constants[key] = sorted(values)
    write_json(CONSTANTS_PATH, constants)
    data["queries"] = updated_queries
    data["status"] = "recorded query IDs recovered through Dune API"
    write_json(QUERY_IDS, data)
    print(json.dumps({"recovered_queries": len(updated_queries), "constants_path": str(CONSTANTS_PATH.relative_to(ROOT))}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
