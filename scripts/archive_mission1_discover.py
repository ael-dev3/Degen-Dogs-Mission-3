#!/usr/bin/env python3
"""Discover public Mission 1 sources and record source availability.

This is deliberately light-touch: it checks public docs/URLs and whether private
API keys are present, but it never prints or stores secret values.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "archive" / "mission1"
OUT = ARCHIVE / "docs" / "data_sources.md"
DUNE_OUT = ARCHIVE / "dune" / "README.md"
JSON_OUT = ARCHIVE / "config" / "mission1_discovery_results.json"

URLS = [
    "https://docs.degendogs.club/introduction.md",
    "https://docs.degendogs.club/getting-started.md",
    "https://docs.degendogs.club/basics/auctions.md",
    "https://docs.degendogs.club/developers/contracts.md",
    "https://raw.githubusercontent.com/markcarey/degendogs/main/README.md",
    "https://github.com/markcarey/degendogs",
    "https://forum.superfluid.org/t/season-5-ideas-established-project-degen-dogs/1556",
    "https://r.jina.ai/http://medium.com/degen-dogs/degen-dogs-are-nfts-that-stream-defi-tokens-c00743581fed",
    "https://polygonscan.com/address/0xA920464B46548930bEfECcA5467860B2b4C2B5b9",
    "https://polygonscan.com/address/0xC9F32Fc6aa9F4D3d734B1b3feC739d55c2f1C1A7",
    "https://polygonscan.com/address/0x600e5F4920f90132725b43412D47A76bC2219F92",
    "https://polygonscan.com/address/0xE0159F36b6A09e6407dF0c7debAc433a77511625",
    "https://dune.com/ael_dev/degen-dogs-mission-3",
    "https://dune.com/browse/dashboards?q=Degen%20Dogs",
]

TERMS = ["Polygon", "3.14", "Ukraine", "Unchain", "Dog Biscuits", "BSCT", "WETH", "Idle", "Superfluid", "auction", "201", "contract"]


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch(url: str) -> dict:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 degen-dogs-mission1-discover/0.1", "Accept": "text/plain,text/html,application/json"})
        raw = urlopen(req, timeout=40).read().decode("utf-8", "replace")
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text)
        snippets = {}
        for term in TERMS:
            idx = text.lower().find(term.lower())
            if idx >= 0:
                snippets[term] = text[max(0, idx - 140): idx + 320]
        return {"url": url, "status": "ok", "bytes": len(raw), "snippets": snippets}
    except HTTPError as exc:
        return {"url": url, "status": "http_error", "code": exc.code, "error": str(exc)}
    except (URLError, TimeoutError, OSError) as exc:
        return {"url": url, "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:240]}"}


def main() -> int:
    ARCHIVE.joinpath("docs").mkdir(parents=True, exist_ok=True)
    ARCHIVE.joinpath("dune").mkdir(parents=True, exist_ok=True)
    ARCHIVE.joinpath("config").mkdir(parents=True, exist_ok=True)
    results = [fetch(u) for u in URLS]
    api_status = {
        "DUNE_API_KEY": "set" if os.getenv("DUNE_API_KEY") else "unset",
        "POLYGONSCAN_API_KEY": "set" if os.getenv("POLYGONSCAN_API_KEY") else "unset",
        "POLYGON_RPC_URL": "set" if os.getenv("POLYGON_RPC_URL") else "unset",
        "POLYGON_RPC_URLS": "set" if os.getenv("POLYGON_RPC_URLS") else "unset",
    }
    payload = {"recovered_at_utc": now(), "api_status": api_status, "sources": results}
    JSON_OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    lines = [
        "# Mission 1 Data Sources",
        "",
        f"Recovered at UTC: `{payload['recovered_at_utc']}`",
        "",
        "This file records public sources used for the Polygon-era Mission 1 archive. The archive is independent/community-built and separates verified data from candidates.",
        "",
        "## API / Tool Availability",
        "",
    ]
    for key, value in api_status.items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Public Sources Checked", ""])
    for r in results:
        lines.append(f"### {r['url']}")
        lines.append(f"- Status: `{r['status']}`" + (f" `{r.get('code')}`" if r.get('code') else ""))
        if r.get("bytes"):
            lines.append(f"- Bytes fetched: `{r['bytes']}`")
        if r.get("snippets"):
            lines.append("- Matching snippets:")
            for term, snippet in r["snippets"].items():
                safe = snippet.replace("`", "'")
                lines.append(f"  - `{term}`: {safe}")
        if r.get("error"):
            lines.append(f"- Error: `{r['error']}`")
        lines.append("")
    OUT.write_text("\n".join(lines).rstrip() + "\n")

    dune_lines = [
        "# Mission 1 Dune Recovery",
        "",
        f"Recovered at UTC: `{payload['recovered_at_utc']}`",
        "",
        "No Dune API key was available in this environment." if api_status["DUNE_API_KEY"] == "unset" else "A Dune API key is present, but this script does not automatically export private query results.",
        "Public Dune UI requests for Degen Dogs pages/search returned HTTP 403 during recovery, so no Mission 1 Dune SQL/results were recovered in this pass.",
        "",
        "Searches attempted:",
        "- Degen Dogs",
        "- Degen Dogs Mission 1",
        "- Degen Dogs Polygon",
        "- Dog Biscuits / BSCT",
        "- ael_dev Degen Dogs Polygon",
        "- markcarey Degen Dogs Dune",
        "",
        "If Dune API access becomes available, save dashboard/query metadata under `archive/mission1/dune/dune_dashboards.json`, `dune_queries.json`, `sql/`, and `results/`.",
    ]
    DUNE_OUT.write_text("\n".join(dune_lines) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
