#!/usr/bin/env python3
"""Compare raw-main and GitHub Pages refresh status for off-host monitoring."""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_RAW_URL = "https://raw.githubusercontent.com/ael-dev3/Degen-Dogs-Mission-3/main/generated/refresh_status.json"
DEFAULT_PAGES_URL = "https://ael-dev3.github.io/Degen-Dogs-Mission-3/generated/refresh_status.json"


def utc_time(value: Any) -> datetime | None:
    text = str(value or "").strip().replace(" ", "T")
    if not text:
        return None
    if not text.endswith("Z") and "+" not in text[10:]:
        text += "Z"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def positive_int(value: Any) -> int:
    try:
        parsed = int(str(value or 0))
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def status_problem(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "payload is not an object"
    if payload.get("kind") != "refresh_status":
        return "kind is not refresh_status"
    if str(payload.get("last_refresh_result") or "").lower() not in {
        "success_generated",
        "success_current_surface",
        "success_pushed",
    }:
        return "last refresh result is not successful"
    if payload.get("onchain_verification_status") != "current_snapshot_cross_provider_verified":
        return "onchain verification is not cross-provider verified"
    if "current_auction" not in str(payload.get("onchain_verification_scope") or "").split(","):
        return "onchain verification scope is incomplete"
    if positive_int(payload.get("onchain_chain_id")) != 8453:
        return "chain id is not Base mainnet"
    if positive_int(payload.get("latest_generated_block")) <= 0:
        return "latest generated block is missing"
    if not str(payload.get("snapshot_block_hash") or "").startswith("0x"):
        return "snapshot block hash is missing"
    if utc_time(payload.get("last_successful_refresh_time_utc")) is None:
        return "last successful refresh time is invalid"
    return ""


def assess_freshness(
    raw: Any,
    pages: Any,
    *,
    now: datetime,
    max_raw_age_seconds: int,
    propagation_grace_seconds: int,
    raw_fetch_error: str = "",
    pages_fetch_error: str = "",
) -> dict[str, Any]:
    raw_problem = raw_fetch_error or status_problem(raw)
    pages_problem = pages_fetch_error or status_problem(pages)
    raw_time = utc_time(raw.get("last_successful_refresh_time_utc")) if isinstance(raw, dict) else None
    pages_time = utc_time(pages.get("last_successful_refresh_time_utc")) if isinstance(pages, dict) else None
    raw_age = int(max(0, (now - raw_time).total_seconds())) if raw_time else None
    pages_age = int(max(0, (now - pages_time).total_seconds())) if pages_time else None
    if raw_age is not None and raw_age > max_raw_age_seconds:
        raw_problem = f"raw main is {raw_age}s old (limit {max_raw_age_seconds}s)"

    raw_stale = bool(raw_problem)
    pages_stale = False
    pages_lag_seconds: int | None = None
    if not raw_stale:
        if pages_problem:
            pages_stale = True
        else:
            assert isinstance(raw, dict) and isinstance(pages, dict) and raw_time and pages_time
            pages_lag_seconds = int(max(0, (raw_time - pages_time).total_seconds()))
            raw_block = positive_int(raw.get("latest_generated_block"))
            pages_block = positive_int(pages.get("latest_generated_block"))
            if pages_block != raw_block and pages_lag_seconds > propagation_grace_seconds:
                pages_stale = True
                pages_problem = (
                    f"Pages block {pages_block} differs from raw block {raw_block}; "
                    f"data timestamp lag is {pages_lag_seconds}s"
                )

    incident = raw_stale or pages_stale
    return {
        "incident": incident,
        "pages_needs_deploy": pages_stale and not raw_stale,
        "pages_problem": pages_problem if pages_stale else "",
        "pages_refresh_age_seconds": pages_age,
        "pages_timestamp_lag_seconds": pages_lag_seconds,
        "raw_problem": raw_problem if raw_stale else "",
        "raw_refresh_age_seconds": raw_age,
        "raw_stale": raw_stale,
        "status": "unhealthy" if incident else "healthy",
    }


def fetch_json(url: str, timeout: int) -> Any:
    separator = "&" if urllib.parse.urlsplit(url).query else "?"
    request = urllib.request.Request(
        f"{url}{separator}monitor={int(datetime.now(timezone.utc).timestamp())}",
        headers={"Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": "degen-dogs-freshness-monitor/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed/operator-provided URL
        return json.loads(response.read().decode("utf-8"))


def write_github_output(path: Path, report: dict[str, Any]) -> None:
    lines = [
        f"incident={'true' if report['incident'] else 'false'}",
        f"pages_needs_deploy={'true' if report['pages_needs_deploy'] else 'false'}",
        f"raw_stale={'true' if report['raw_stale'] else 'false'}",
        f"report={json.dumps(report, sort_keys=True, separators=(',', ':'))}",
    ]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-url", default=DEFAULT_RAW_URL)
    parser.add_argument("--pages-url", default=DEFAULT_PAGES_URL)
    parser.add_argument("--max-raw-age-seconds", type=int, default=5400)
    parser.add_argument("--propagation-grace-seconds", type=int, default=900)
    parser.add_argument("--timeout-seconds", type=int, default=15)
    parser.add_argument("--github-output", type=Path, default=None)
    args = parser.parse_args()

    raw: Any = None
    pages: Any = None
    raw_error = ""
    pages_error = ""
    try:
        raw = fetch_json(args.raw_url, args.timeout_seconds)
    except Exception as exc:  # noqa: BLE001
        raw_error = f"raw main fetch failed: {exc}"
    try:
        pages = fetch_json(args.pages_url, args.timeout_seconds)
    except Exception as exc:  # noqa: BLE001
        pages_error = f"Pages fetch failed: {exc}"
    report = assess_freshness(
        raw,
        pages,
        now=datetime.now(timezone.utc),
        max_raw_age_seconds=max(60, args.max_raw_age_seconds),
        propagation_grace_seconds=max(0, args.propagation_grace_seconds),
        raw_fetch_error=raw_error,
        pages_fetch_error=pages_error,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    output_path = args.github_output or (Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None)
    if output_path:
        write_github_output(output_path, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
