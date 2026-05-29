#!/usr/bin/env python3
"""
Fetch official SQL text for Dune queries using Dune's Read Query API.

Usage:
  Export your Dune API key in the local shell, then run:
  python scripts/fetch_official_dune_sql.py query_ids.json

The script reads query_ids.json, skips entries with null query_id, and writes
queries/query_<id>.official.sql for each query Dune returns.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_URL = "https://api.dune.com/api/v1/query/{query_id}"


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") or "query"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "query_ids.json"
    output_dir = root / "queries"
    output_dir.mkdir(parents=True, exist_ok=True)

    api_key = os.environ.get("DUNE_API_KEY")
    if not api_key:
        print("Missing DUNE_API_KEY environment variable.", file=sys.stderr)
        return 2

    entries = json.loads(config_path.read_text())
    for entry in entries:
        query_id = entry.get("query_id")
        if not query_id:
            print(f"Skipping {entry.get('name', '<unnamed>')}: no query_id set")
            continue

        request = Request(
            API_URL.format(query_id=query_id),
            headers={"X-DUNE-API-KEY": api_key},
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            print(f"Query {query_id}: HTTP {exc.code} {exc.reason}", file=sys.stderr)
            continue
        except URLError as exc:
            print(f"Query {query_id}: URL error {exc.reason}", file=sys.stderr)
            continue

        name = payload.get("name") or entry.get("name") or f"query_{query_id}"
        sql = payload.get("query_sql")
        if not sql:
            print(f"Query {query_id}: response did not include query_sql", file=sys.stderr)
            continue

        filename = f"query_{query_id}_{slugify(name)}.official.sql"
        path = output_dir / filename
        header = (
            f"-- Official SQL fetched from Dune Read Query API\n"
            f"-- Query id: {query_id}\n"
            f"-- Name: {name}\n"
            f"-- Owner: {payload.get('owner', '<unknown>')}\n\n"
        )
        path.write_text(header + sql.rstrip() + "\n")
        print(f"Wrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
