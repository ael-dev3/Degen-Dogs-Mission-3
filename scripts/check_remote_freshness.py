#!/usr/bin/env python3
"""Compare raw-main and GitHub Pages refresh status for off-host monitoring."""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_RAW_URL = "https://raw.githubusercontent.com/ael-dev3/Degen-Dogs-Mission-3/main/generated/refresh_status.json"
DEFAULT_PAGES_URL = "https://ael-dev3.github.io/Degen-Dogs-Mission-3/generated/refresh_status.json"
STATUS_RESPONSE_LIMIT_BYTES = 2 * 1024 * 1024
STATUS_TARGET_CONTENT_TYPES = {
    DEFAULT_RAW_URL: "text/plain",
    DEFAULT_PAGES_URL: "application/json",
}
BLOCK_HASH_PATTERN = re.compile(r"0x[a-fA-F0-9]{64}")
QUORUM_AGREEMENT_PATTERN = re.compile(r"([1-9][0-9]*)/([1-9][0-9]*)")
REQUIRED_ONCHAIN_SCOPE = frozenset(
    {
        "snapshot_hash",
        "contract_code",
        "current_auction",
        "dog_total_supply",
        "recent_event_logs",
    }
)


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


def provider_names(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {part.strip().lower() for part in re.split(r"[,|]", value) if part.strip()}


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
    scope = {part.strip() for part in str(payload.get("onchain_verification_scope") or "").split(",") if part.strip()}
    missing_scope = sorted(REQUIRED_ONCHAIN_SCOPE - scope)
    if missing_scope:
        return f"onchain verification scope is incomplete (missing {','.join(missing_scope)})"
    if positive_int(payload.get("onchain_chain_id")) != 8453:
        return "chain id is not Base mainnet"
    if positive_int(payload.get("latest_generated_block")) <= 0:
        return "latest generated block is missing"
    block_hash = str(payload.get("snapshot_block_hash") or "")
    if not BLOCK_HASH_PATTERN.fullmatch(block_hash) or int(block_hash[2:], 16) == 0:
        return "snapshot block hash is invalid"
    quorum_size = positive_int(payload.get("rpc_quorum_size"))
    if quorum_size < 2:
        return "RPC quorum size is below 2"
    agreement_match = QUORUM_AGREEMENT_PATTERN.fullmatch(str(payload.get("rpc_quorum_agreement") or "").strip())
    if not agreement_match:
        return "RPC quorum agreement is invalid"
    agreeing, attempted = (int(value) for value in agreement_match.groups())
    if agreeing < quorum_size or attempted < agreeing:
        return "RPC quorum agreement is below the required quorum"
    providers = provider_names(payload.get("rpc_quorum_providers"))
    if len(providers) < agreeing:
        return "RPC quorum provider set does not support the reported agreement"
    log_providers = provider_names(payload.get("log_rpc_quorum_providers"))
    if len(log_providers) < quorum_size:
        return "log RPC provider set is below the required quorum"
    if utc_time(payload.get("last_successful_refresh_time_utc")) is None:
        return "last successful refresh time is invalid"
    return ""


def mismatched_fields(raw: dict[str, Any], pages: dict[str, Any]) -> list[str]:
    return sorted(
        key
        for key in raw.keys() | pages.keys()
        if key not in raw or key not in pages or raw[key] != pages[key]
    )


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
    payload_divergence_age_seconds: int | None = None
    payload_mismatch_fields: list[str] = []
    payload_relation = "unchecked"
    pages_needs_deploy = False
    if not raw_stale:
        if pages_problem:
            pages_stale = True
            payload_relation = "invalid_pages_payload"
            pages_needs_deploy = True
        else:
            assert isinstance(raw, dict) and isinstance(pages, dict) and raw_time and pages_time
            pages_lag_seconds = int(max(0, (raw_time - pages_time).total_seconds()))
            raw_block = positive_int(raw.get("latest_generated_block"))
            pages_block = positive_int(pages.get("latest_generated_block"))
            payload_mismatch_fields = mismatched_fields(raw, pages)
            if not payload_mismatch_fields:
                payload_relation = "matched"
            else:
                if pages_block < raw_block:
                    payload_relation = "raw_ahead"
                    pages_needs_deploy = True
                elif pages_block > raw_block:
                    payload_relation = "pages_ahead"
                else:
                    payload_relation = "same_block_mismatch"
                    pages_needs_deploy = raw_time > pages_time

                timestamp_gap = int(abs((raw_time - pages_time).total_seconds()))
                newer_payload_age = min(raw_age or 0, pages_age or 0)
                payload_divergence_age_seconds = max(timestamp_gap, newer_payload_age)
            if payload_mismatch_fields and payload_divergence_age_seconds > propagation_grace_seconds:
                pages_stale = True
                fields = ",".join(payload_mismatch_fields[:8])
                if len(payload_mismatch_fields) > 8:
                    fields += f",+{len(payload_mismatch_fields) - 8} more"
                pages_problem = (
                    f"Pages/raw payloads diverge ({payload_relation}); raw block {raw_block}, "
                    f"Pages block {pages_block}, evidence age {payload_divergence_age_seconds}s, "
                    f"mismatched fields: {fields}"
                )
            elif payload_mismatch_fields:
                pages_needs_deploy = False

    incident = raw_stale or pages_stale
    return {
        "incident": incident,
        "pages_needs_deploy": pages_stale and pages_needs_deploy and not raw_stale,
        "pages_problem": pages_problem if pages_stale else "",
        "pages_refresh_age_seconds": pages_age,
        "pages_timestamp_lag_seconds": pages_lag_seconds,
        "payload_divergence_age_seconds": payload_divergence_age_seconds,
        "payload_mismatch_fields": payload_mismatch_fields,
        "payload_relation": payload_relation,
        "raw_problem": raw_problem if raw_stale else "",
        "raw_refresh_age_seconds": raw_age,
        "raw_stale": raw_stale,
        "status": "unhealthy" if incident else "healthy",
    }


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201, ARG002
        return None


STATUS_OPENER = urllib.request.build_opener(_RejectRedirects())


def fetch_json(url: str, timeout: int, *, expected_url: str | None = None) -> Any:
    expected_url = expected_url or url
    expected_content_type = STATUS_TARGET_CONTENT_TYPES.get(expected_url)
    if expected_content_type is None or url != expected_url:
        raise RuntimeError("status endpoint is not an approved fixed target")
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or parts.query
    ):
        raise RuntimeError("status endpoint failed fixed-target validation")
    separator = "?"
    request_url = f"{url}{separator}monitor={int(datetime.now(timezone.utc).timestamp())}"
    request = urllib.request.Request(
        request_url,
        headers={"Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": "degen-dogs-freshness-monitor/1.0"},
    )
    with STATUS_OPENER.open(request, timeout=timeout) as response:  # noqa: S310 - exact fixed targets validated above
        if response.geturl() != request_url:
            raise RuntimeError("status endpoint changed origin or path")
        if response.getcode() != 200:
            raise RuntimeError("status endpoint returned a non-success status")
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != expected_content_type:
            raise RuntimeError("status endpoint returned an unexpected content type")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("status endpoint returned an invalid content length") from exc
            if declared_length < 0 or declared_length > STATUS_RESPONSE_LIMIT_BYTES:
                raise RuntimeError("status endpoint response exceeds the size limit")
        body = response.read(STATUS_RESPONSE_LIMIT_BYTES + 1)
        if len(body) > STATUS_RESPONSE_LIMIT_BYTES:
            raise RuntimeError("status endpoint response exceeds the size limit")
        try:
            text = body.decode("utf-8", errors="strict")
            return json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("status endpoint returned invalid UTF-8 JSON") from exc


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
        raw = fetch_json(args.raw_url, args.timeout_seconds, expected_url=DEFAULT_RAW_URL)
    except Exception as exc:  # noqa: BLE001
        raw_error = f"raw main fetch failed: {exc}"
    try:
        pages = fetch_json(args.pages_url, args.timeout_seconds, expected_url=DEFAULT_PAGES_URL)
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
