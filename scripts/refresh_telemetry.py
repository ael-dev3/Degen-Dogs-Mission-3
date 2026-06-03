#!/usr/bin/env python3
"""Structured telemetry and public refresh status helpers for Mission 3 runners."""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
SITE_URL = "https://ael-dev3.github.io/Degen-Dogs-Mission-3/"
LOCAL_REFRESH_RUNS = ROOT / ".local" / "refresh_runs.jsonl"
LOCAL_WATCHER_CHECKS = ROOT / ".local" / "watcher_checks.jsonl"
LOG_REFRESH_METRICS = ROOT / "logs" / "refresh-metrics.jsonl"
STATUS_PATH = ROOT / "generated" / "refresh_status.json"
PUBLIC_STATUS_PATH = ROOT / "public" / "generated" / "refresh_status.json"
WATCHER_STATE_PATH = ROOT / ".local" / "mission3_onchain_tracker_state.json"

SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)=([^\s&]+)"),
]
PRIVATE_PATH_PATTERNS = [
    re.compile(r"/Users/[^\s\"']+"),
    re.compile(r"/var/folders/[^\s\"']+"),
    re.compile(r"/tmp/[^\s\"']+"),
]
SENSITIVE_RPC_DOMAINS = (
    "alchemy.com",
    "infura.io",
    "quicknode.pro",
    "quiknode.pro",
    "ankr.com",
    "blastapi.io",
    "drpc.org",
    "nodereal.io",
    "chainstack.com",
    "thirdweb.com",
    "getblock.io",
)
SUCCESS_RESULTS = {
    "success",
    "success_generated",
    "success_no_diff",
    "success_skip_push",
    "success_pushed",
    "success_pushed_live_timeout",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(value: Any) -> str | None:
    parsed = parse_utc(value)
    if not parsed:
        return None
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def seconds_between(start: Any, end: Any) -> float | None:
    start_dt = parse_utc(start)
    end_dt = parse_utc(end)
    if not start_dt or not end_dt:
        return None
    return round(max(0.0, (end_dt - start_dt).total_seconds()), 3)


def number_or_none(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number.is_integer():
        return int(number)
    return number


def int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def redact_url(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
    except Exception:
        return "<redacted-url>"
    if not parts.scheme or not parts.netloc:
        return value
    hostname = parts.hostname or ""
    lower_host = hostname.lower()
    if any(lower_host.endswith(domain) for domain in SENSITIVE_RPC_DOMAINS):
        domain = next(domain for domain in SENSITIVE_RPC_DOMAINS if lower_host.endswith(domain))
        host = f"***.{domain}"
    else:
        host = hostname
    if parts.port:
        host += f":{parts.port}"
    if parts.username or parts.password:
        host = "***@" + host
    path = ""
    if parts.path and parts.path != "/":
        path = "/<redacted-path>"
    elif parts.path == "/":
        path = "/"
    query = "redacted=1" if parts.query else ""
    return urllib.parse.urlunsplit((parts.scheme, host, path, query, ""))


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    for url in re.findall(r"https?://[^\s\"'<>]+", text):
        text = text.replace(url, redact_url(url))
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}=<redacted>" if m.groups() else "<redacted-secret>", text)
    return text


def assert_public_safe(data: Any) -> None:
    text = json.dumps(data, sort_keys=True, ensure_ascii=False)
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise AssertionError("public refresh status contains a secret-looking value")
    for pattern in PRIVATE_PATH_PATTERNS:
        if pattern.search(text):
            raise AssertionError("public refresh status contains a private local path")
    lowered = text.lower()
    forbidden = ["base_rpc_url", "base_rpc_urls", "private key", "degen_dogs_log_dir", "mission3_watcher_state_path"]
    for token in forbidden:
        if token in lowered:
            raise AssertionError(f"public refresh status contains forbidden token: {token}")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_row = redact_value(row)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe_row, sort_keys=True, separators=(",", ":")) + "\n")


def read_jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        lines = handle.readlines()
    if limit is not None:
        lines = lines[-limit:]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def first_dict(path: Path) -> dict[str, Any]:
    data = load_json(path, [])
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    return {}


def metric_lookup(root: Path = ROOT) -> dict[str, str]:
    rows = load_json(root / "generated" / "mission3_metrics.json", [])
    metrics: dict[str, str] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("metric") is not None:
                metrics[str(row.get("metric"))] = "" if row.get("value") is None else str(row.get("value"))
    return metrics


def generated_state(root: Path = ROOT) -> dict[str, Any]:
    current = first_dict(root / "generated" / "current_auction.json")
    metrics = metric_lookup(root)
    latest_block = int_or_none(metrics.get("latest_block") or current.get("latest_block"))
    latest_time = iso_utc(metrics.get("latest_block_time_utc") or current.get("latest_block_time_utc"))
    token_id = int_or_none(metrics.get("current_auction_token_id") or current.get("token_id"))
    return {
        "latest_generated_block": latest_block,
        "latest_generated_block_time_utc": latest_time,
        "generated_current_token_id": token_id,
        "generated_current_bid_eth": str(metrics.get("current_bid_eth") or current.get("current_bid_eth") or ""),
        "generated_current_high_bidder": str(metrics.get("current_bidder") or current.get("bidder") or ""),
        "generated_current_high_bidder_wallet": str(metrics.get("current_bidder_wallet") or current.get("bidder_wallet") or ""),
        "generated_current_end_time_utc": iso_utc(metrics.get("current_auction_end_utc") or current.get("end_time_utc")),
        "generated_current_status": str(metrics.get("current_auction_status") or current.get("auction_state") or ""),
    }


def parse_list_env(value: str) -> list[str]:
    text = (value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [item.strip() for item in text.split(",") if item.strip()]
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item).strip()]
    return [str(parsed)] if str(parsed).strip() else []


def env_timestamp(env: dict[str, str], name: str) -> str | None:
    return iso_utc(env.get(name))


def env_duration(env: dict[str, str], start_name: str, end_name: str) -> float | None:
    return seconds_between(env.get(start_name), env.get(end_name))


def build_refresh_row(env: dict[str, str], *, result: str, error: str | None = None, root: Path = ROOT) -> dict[str, Any]:
    completed = utc_now()
    run_id = env.get("DEGEN_DOGS_REFRESH_RUN_ID") or f"refresh-{int(time.time())}-{os.getpid()}"
    started = env_timestamp(env, "DEGEN_DOGS_REFRESH_STARTED_AT_UTC") or env_timestamp(env, "DEGEN_DOGS_STARTED_AT_UTC") or completed
    queued = env_timestamp(env, "DEGEN_DOGS_REFRESH_QUEUED_AT_UTC") or started
    lock_acquired = env_timestamp(env, "DEGEN_DOGS_LOCK_ACQUIRED_AT_UTC") or env_timestamp(env, "DEGEN_DOGS_REFRESH_LOCK_ACQUIRED_AT_UTC")
    pushed = env_timestamp(env, "DEGEN_DOGS_PUSH_COMPLETED_AT_UTC")
    detected = env_timestamp(env, "DEGEN_DOGS_DETECTED_AT_UTC")
    event_block_time = env_timestamp(env, "DEGEN_DOGS_EVENT_BLOCK_TIME_UTC")
    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "kind": "refresh_publish",
        "trigger": env.get("DEGEN_DOGS_REFRESH_TRIGGER") or "manual",
        "reasons": parse_list_env(env.get("DEGEN_DOGS_REFRESH_REASONS", "")),
        "event_name": env.get("DEGEN_DOGS_EVENT_NAME") or None,
        "event_block_number": int_or_none(env.get("DEGEN_DOGS_EVENT_BLOCK_NUMBER")),
        "event_block_time_utc": event_block_time,
        "event_tx_hash": env.get("DEGEN_DOGS_EVENT_TX_HASH") or None,
        "event_log_index": int_or_none(env.get("DEGEN_DOGS_EVENT_LOG_INDEX")),
        "detected_at_utc": detected,
        "queued_at_utc": queued,
        "lock_acquired_at_utc": lock_acquired,
        "lock_wait_seconds": seconds_between(queued, lock_acquired) if lock_acquired else None,
        "started_at_utc": started,
        "completed_at_utc": completed,
        "duration_seconds": seconds_between(started, completed),
        "git_pull_started_at_utc": env_timestamp(env, "DEGEN_DOGS_GIT_PULL_STARTED_AT_UTC"),
        "git_pull_completed_at_utc": env_timestamp(env, "DEGEN_DOGS_GIT_PULL_COMPLETED_AT_UTC"),
        "git_pull_duration_seconds": env_duration(env, "DEGEN_DOGS_GIT_PULL_STARTED_AT_UTC", "DEGEN_DOGS_GIT_PULL_COMPLETED_AT_UTC"),
        "data_started_at_utc": env_timestamp(env, "DEGEN_DOGS_DATA_STARTED_AT_UTC"),
        "data_completed_at_utc": env_timestamp(env, "DEGEN_DOGS_DATA_COMPLETED_AT_UTC"),
        "data_duration_seconds": env_duration(env, "DEGEN_DOGS_DATA_STARTED_AT_UTC", "DEGEN_DOGS_DATA_COMPLETED_AT_UTC"),
        "validation_started_at_utc": env_timestamp(env, "DEGEN_DOGS_VALIDATION_STARTED_AT_UTC"),
        "validation_completed_at_utc": env_timestamp(env, "DEGEN_DOGS_VALIDATION_COMPLETED_AT_UTC"),
        "validation_duration_seconds": env_duration(env, "DEGEN_DOGS_VALIDATION_STARTED_AT_UTC", "DEGEN_DOGS_VALIDATION_COMPLETED_AT_UTC"),
        "build_started_at_utc": env_timestamp(env, "DEGEN_DOGS_BUILD_STARTED_AT_UTC"),
        "build_completed_at_utc": env_timestamp(env, "DEGEN_DOGS_BUILD_COMPLETED_AT_UTC"),
        "build_duration_seconds": env_duration(env, "DEGEN_DOGS_BUILD_STARTED_AT_UTC", "DEGEN_DOGS_BUILD_COMPLETED_AT_UTC"),
        "git_status_started_at_utc": env_timestamp(env, "DEGEN_DOGS_GIT_STATUS_STARTED_AT_UTC"),
        "git_status_completed_at_utc": env_timestamp(env, "DEGEN_DOGS_GIT_STATUS_COMPLETED_AT_UTC"),
        "changed_files": parse_list_env(env.get("DEGEN_DOGS_CHANGED_FILES", "")),
        "commit_started_at_utc": env_timestamp(env, "DEGEN_DOGS_COMMIT_STARTED_AT_UTC"),
        "commit_completed_at_utc": env_timestamp(env, "DEGEN_DOGS_COMMIT_COMPLETED_AT_UTC"),
        "commit_duration_seconds": env_duration(env, "DEGEN_DOGS_COMMIT_STARTED_AT_UTC", "DEGEN_DOGS_COMMIT_COMPLETED_AT_UTC"),
        "push_started_at_utc": env_timestamp(env, "DEGEN_DOGS_PUSH_STARTED_AT_UTC"),
        "push_completed_at_utc": pushed,
        "push_duration_seconds": env_duration(env, "DEGEN_DOGS_PUSH_STARTED_AT_UTC", "DEGEN_DOGS_PUSH_COMPLETED_AT_UTC"),
        "upload_completed_at_utc": pushed,
        "detect_to_push_seconds": seconds_between(detected, pushed) if detected and pushed else None,
        "block_to_push_seconds": seconds_between(event_block_time, pushed) if event_block_time and pushed else None,
        "result": result,
        "commit_sha": env.get("DEGEN_DOGS_COMMIT_SHA") or None,
        "branch": env.get("DEGEN_DOGS_BRANCH") or None,
        "remote": env.get("DEGEN_DOGS_REMOTE") or None,
        "skip_push": str(env.get("DEGEN_DOGS_SKIP_PUSH", "0")).strip() == "1",
        "live_verify_started_at_utc": env_timestamp(env, "DEGEN_DOGS_LIVE_VERIFY_STARTED_AT_UTC"),
        "live_verified_at_utc": env_timestamp(env, "DEGEN_DOGS_LIVE_VERIFIED_AT_UTC"),
        "push_to_live_seconds": number_or_none(env.get("DEGEN_DOGS_PUSH_TO_LIVE_SECONDS")),
        "block_to_live_seconds": number_or_none(env.get("DEGEN_DOGS_BLOCK_TO_LIVE_SECONDS")),
        "live_verify_result": env.get("DEGEN_DOGS_LIVE_VERIFY_RESULT") or None,
        "error": redact_value(error or env.get("DEGEN_DOGS_REFRESH_ERROR") or "") or None,
    }
    row.update(generated_state(root))
    return {key: value for key, value in row.items() if value not in (None, [], "")}


def refresh_paths_from_env(env: dict[str, str], root: Path = ROOT) -> tuple[Path, Path]:
    local = Path(env.get("DEGEN_DOGS_REFRESH_TELEMETRY_PATH") or root / ".local" / "refresh_runs.jsonl").expanduser()
    metrics = Path(env.get("DEGEN_DOGS_REFRESH_METRICS_PATH") or root / "logs" / "refresh-metrics.jsonl").expanduser()
    if not local.is_absolute():
        local = root / local
    if not metrics.is_absolute():
        metrics = root / metrics
    return local, metrics


def record_refresh(env: dict[str, str], *, result: str, error: str | None = None, root: Path = ROOT) -> dict[str, Any]:
    row = build_refresh_row(env, result=result, error=error, root=root)
    local, metrics = refresh_paths_from_env(env, root=root)
    append_jsonl(local, row)
    append_jsonl(metrics, row)
    return row


def watcher_path_from_env(env: dict[str, str], root: Path = ROOT) -> Path:
    path = Path(env.get("MISSION3_WATCHER_TELEMETRY_PATH") or root / ".local" / "watcher_checks.jsonl").expanduser()
    return path if path.is_absolute() else root / path


def record_watcher_check(row: dict[str, Any], env: dict[str, str] | None = None, *, root: Path = ROOT) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    out = dict(row)
    out.setdefault("schema_version", SCHEMA_VERSION)
    out.setdefault("kind", "watcher_check")
    if out.get("started_at_utc") and out.get("completed_at_utc") and out.get("duration_seconds") is None:
        out["duration_seconds"] = seconds_between(out["started_at_utc"], out["completed_at_utc"])
    append_jsonl(watcher_path_from_env(env, root=root), out)
    return out


def latest_refresh_row(env: dict[str, str], root: Path = ROOT) -> dict[str, Any]:
    local, metrics = refresh_paths_from_env(env, root=root)
    rows = [*read_jsonl(metrics, limit=500), *read_jsonl(local, limit=500)]
    rows = [row for row in rows if row.get("kind") == "refresh_publish"]
    rows.sort(key=lambda row: str(row.get("completed_at_utc") or row.get("started_at_utc") or ""))
    return rows[-1] if rows else {}


def latest_successful_refresh_row(env: dict[str, str], root: Path = ROOT) -> dict[str, Any]:
    local, metrics = refresh_paths_from_env(env, root=root)
    rows = [*read_jsonl(metrics, limit=1000), *read_jsonl(local, limit=1000)]
    successes = [row for row in rows if row.get("kind") == "refresh_publish" and str(row.get("result")) in SUCCESS_RESULTS]
    successes.sort(key=lambda row: str(row.get("completed_at_utc") or row.get("started_at_utc") or ""))
    return successes[-1] if successes else {}


def public_refresh_status(env: dict[str, str], root: Path = ROOT, *, prefer_current_env: bool = False) -> dict[str, Any]:
    """Build the public-safe generation freshness sidecar.

    Private JSONL telemetry keeps publish outcomes such as skip-push, pushed,
    failed, and live-timeout. The public sidecar intentionally reports only the
    latest generated snapshot and its generation result, so it never exposes
    runner paths, commit SHAs, push timing, or operator-only publish state.
    """
    _ = prefer_current_env  # Kept for CLI/backward compatibility; public status is always generation-only.
    state = generated_state(root)
    generation_row = build_refresh_row(env, result="success_generated", root=root)
    reasons = generation_row.get("reasons") if isinstance(generation_row.get("reasons"), list) else []
    status = {
        "schema_version": SCHEMA_VERSION,
        "kind": "refresh_status",
        "site_url": SITE_URL,
        "last_successful_refresh_time_utc": generation_row.get("data_completed_at_utc") or generation_row.get("completed_at_utc") or generation_row.get("started_at_utc") or state.get("latest_generated_block_time_utc"),
        "latest_generated_block": state.get("latest_generated_block"),
        "latest_generated_block_time_utc": state.get("latest_generated_block_time_utc"),
        "trigger": generation_row.get("trigger") or env.get("DEGEN_DOGS_REFRESH_TRIGGER") or "data",
        "refresh_reason": ",".join(str(item) for item in reasons) if reasons else (env.get("DEGEN_DOGS_REFRESH_REASONS") or "data_generation"),
        "current_dog_token_id": state.get("generated_current_token_id"),
        "current_bid_eth": state.get("generated_current_bid_eth"),
        "current_high_bidder": state.get("generated_current_high_bidder"),
        "current_high_bidder_wallet": state.get("generated_current_high_bidder_wallet"),
        "current_auction_status": state.get("generated_current_status"),
        "current_auction_end_time_utc": state.get("generated_current_end_time_utc"),
        "last_refresh_result": "success_generated",
    }
    clean = {key: value for key, value in status.items() if value not in (None, "", [])}
    assert_public_safe(clean)
    return clean


def write_refresh_status(env: dict[str, str], root: Path = ROOT, *, prefer_current_env: bool = False) -> dict[str, Any]:
    status = public_refresh_status(env, root=root, prefer_current_env=prefer_current_env)
    write_json(root / "generated" / "refresh_status.json", status)
    write_json(root / "public" / "generated" / "refresh_status.json", status)
    return status


def validate_refresh_status(root: Path = ROOT) -> dict[str, Any]:
    generated = root / "generated" / "refresh_status.json"
    public = root / "public" / "generated" / "refresh_status.json"
    if not generated.exists():
        raise AssertionError("generated/refresh_status.json missing")
    if not public.exists():
        raise AssertionError("public/generated/refresh_status.json missing")
    status = load_json(generated, {})
    if not isinstance(status, dict):
        raise AssertionError("generated/refresh_status.json is not an object")
    if public.read_bytes() != generated.read_bytes():
        raise AssertionError("public/generated/refresh_status.json differs from generated/refresh_status.json")
    assert_public_safe(status)
    required = {
        "schema_version",
        "kind",
        "site_url",
        "last_successful_refresh_time_utc",
        "latest_generated_block",
        "latest_generated_block_time_utc",
        "trigger",
        "refresh_reason",
        "current_dog_token_id",
        "current_bid_eth",
        "current_high_bidder",
        "current_high_bidder_wallet",
        "current_auction_status",
        "current_auction_end_time_utc",
        "last_refresh_result",
    }
    missing = sorted(key for key in required if status.get(key) in (None, "", []))
    if missing:
        raise AssertionError("refresh_status missing required fields: " + ", ".join(missing))
    if int_or_none(status.get("schema_version")) != SCHEMA_VERSION:
        raise AssertionError("refresh_status schema_version is unsupported")
    if status.get("kind") != "refresh_status":
        raise AssertionError("refresh_status kind is invalid")
    if status.get("site_url") != SITE_URL:
        raise AssertionError("refresh_status site_url is invalid")
    if str(status.get("last_refresh_result")) != "success_generated":
        raise AssertionError("refresh_status last_refresh_result is not a public generation result")
    if not iso_utc(status.get("last_successful_refresh_time_utc")):
        raise AssertionError("refresh_status last_successful_refresh_time_utc is invalid")
    state = generated_state(root)
    if int_or_none(status.get("latest_generated_block")) != state.get("latest_generated_block"):
        raise AssertionError("refresh_status latest_generated_block differs from mission3_metrics/current_auction")
    if iso_utc(status.get("latest_generated_block_time_utc")) != state.get("latest_generated_block_time_utc"):
        raise AssertionError("refresh_status latest_generated_block_time_utc differs from mission3_metrics/current_auction")
    if int_or_none(status.get("current_dog_token_id")) != state.get("generated_current_token_id"):
        raise AssertionError("refresh_status current_dog_token_id differs from current_auction")
    if str(status.get("current_bid_eth", "")) != str(state.get("generated_current_bid_eth", "")):
        raise AssertionError("refresh_status current_bid_eth differs from mission3_metrics/current_auction")
    if str(status.get("current_high_bidder", "")) != str(state.get("generated_current_high_bidder", "")):
        raise AssertionError("refresh_status current_high_bidder differs from mission3_metrics/current_auction")
    if str(status.get("current_high_bidder_wallet", "")).lower() != str(state.get("generated_current_high_bidder_wallet", "")).lower():
        raise AssertionError("refresh_status current_high_bidder_wallet differs from mission3_metrics/current_auction")
    if str(status.get("current_auction_status", "")).lower() != str(state.get("generated_current_status", "")).lower():
        raise AssertionError("refresh_status current_auction_status differs from mission3_metrics/current_auction")
    if iso_utc(status.get("current_auction_end_time_utc")) != state.get("generated_current_end_time_utc"):
        raise AssertionError("refresh_status current_auction_end_time_utc differs from mission3_metrics/current_auction")
    return status


def percentile(values: list[float], pct: float) -> float | None:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None
    if len(clean) == 1:
        return round(clean[0], 3)
    rank = (len(clean) - 1) * pct
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return round(clean[int(rank)], 3)
    weight = rank - lower
    return round(clean[lower] * (1 - weight) + clean[upper] * weight, 3)


def recent_24h(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = []
    for row in rows:
        ts = parse_utc(row.get("completed_at_utc") or row.get("started_at_utc"))
        if ts and ts >= cutoff:
            recent.append(row)
    return recent


def detect_launchd(label: str) -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"label": label, "available": False, "reason": "not macOS"}
    target = f"gui/{os.getuid()}/{label}"
    try:
        output = subprocess.check_output(["launchctl", "print", target], text=True, stderr=subprocess.STDOUT, timeout=5)
    except Exception as exc:  # noqa: BLE001
        return {"label": label, "available": False, "reason": str(exc).splitlines()[0][:160]}
    interval_match = re.search(r"StartInterval\s*=>\s*(\d+)", output)
    state_match = re.search(r"state\s*=\s*([^\n]+)", output)
    return {
        "label": label,
        "available": True,
        "expected_interval_seconds": int(interval_match.group(1)) if interval_match else None,
        "state": state_match.group(1).strip() if state_match else None,
    }


def metrics_summary(env: dict[str, str], root: Path = ROOT) -> dict[str, Any]:
    local_refresh, metrics_refresh = refresh_paths_from_env(env, root=root)
    watcher_path = watcher_path_from_env(env, root=root)
    refresh_rows = read_jsonl(metrics_refresh, limit=5000) + read_jsonl(local_refresh, limit=5000)
    watcher_rows = read_jsonl(watcher_path, limit=5000)
    # Dedupe refresh rows by run_id/result/completed_at because local and logs mirror each other.
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in refresh_rows:
        key = (str(row.get("run_id", "")), str(row.get("result", "")), str(row.get("completed_at_utc", "")))
        deduped[key] = row
    refresh_rows = list(deduped.values())
    refresh_rows.sort(key=lambda row: str(row.get("completed_at_utc") or row.get("started_at_utc") or ""))
    watcher_rows.sort(key=lambda row: str(row.get("completed_at_utc") or row.get("started_at_utc") or ""))
    refresh_24 = recent_24h(refresh_rows)
    watcher_24 = recent_24h(watcher_rows)
    refresh_durations = []
    for row in refresh_24:
        duration = number_or_none(row.get("duration_seconds"))
        if duration is not None:
            refresh_durations.append(float(duration))
    watcher_durations = []
    for row in watcher_24:
        duration = number_or_none(row.get("duration_seconds"))
        if duration is not None:
            watcher_durations.append(float(duration))
    state = load_json(root / ".local" / "mission3_onchain_tracker_state.json", {})
    generated = generated_state(root)
    last_refresh = refresh_rows[-1] if refresh_rows else {}
    last_success = next((row for row in reversed(refresh_rows) if str(row.get("result")) in SUCCESS_RESULTS), {})
    last_watcher = watcher_rows[-1] if watcher_rows else {}
    summary = {
        "last_watcher_check_time_utc": last_watcher.get("completed_at_utc") or last_watcher.get("started_at_utc"),
        "last_watcher_result": last_watcher.get("result"),
        "last_successful_refresh_time_utc": last_success.get("completed_at_utc") or last_success.get("started_at_utc"),
        "last_refresh_result": last_refresh.get("result"),
        "last_detected_event": last_watcher.get("event_name") or last_refresh.get("event_name"),
        "last_refresh_reason": ",".join(last_refresh.get("reasons", [])) if isinstance(last_refresh.get("reasons"), list) else state.get("last_refresh_reason"),
        "last_generated_block": generated.get("latest_generated_block"),
        "last_generated_block_time_utc": generated.get("latest_generated_block_time_utc"),
        "last_pushed_commit": last_success.get("commit_sha"),
        "last_detection_lag_seconds": last_watcher.get("detection_lag_seconds"),
        "last_detect_to_push_seconds": last_success.get("detect_to_push_seconds"),
        "last_push_to_live_seconds": last_success.get("push_to_live_seconds"),
        "watcher_check_average_seconds_24h": round(sum(watcher_durations) / len(watcher_durations), 3) if watcher_durations else None,
        "watcher_check_p95_seconds_24h": percentile(watcher_durations, 0.95),
        "refresh_average_seconds_24h": round(sum(refresh_durations) / len(refresh_durations), 3) if refresh_durations else None,
        "refresh_p95_seconds_24h": percentile(refresh_durations, 0.95),
        "failed_watcher_checks_24h": sum(1 for row in watcher_24 if row.get("result") == "failed" or str(row.get("result", "")).endswith("failed")),
        "failed_refreshes_24h": sum(1 for row in refresh_24 if str(row.get("result", "")).endswith("failed") or row.get("result") == "failed"),
        "pending_refresh": bool(state.get("pending_refresh")),
        "pending_refresh_reasons": state.get("pending_refresh_reasons"),
        "next_allowed_refresh_after_utc": state.get("next_allowed_refresh_after_utc"),
        "launchd": [
            detect_launchd("com.ael.degendogs.mission3.refresh"),
            detect_launchd("com.ael.degendogs.mission3.watch-auction"),
        ],
        "telemetry_paths": {
            "watcher_checks": str(watcher_path),
            "refresh_runs": str(local_refresh),
            "refresh_metrics": str(metrics_refresh),
            "public_status": "generated/refresh_status.json",
        },
    }
    return summary


def print_summary(summary: dict[str, Any]) -> None:
    print(json.dumps(summary, indent=2, sort_keys=True))


def fetch_json(url: str, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "degen-dogs-refresh-verify/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def verify_live(env: dict[str, str], root: Path = ROOT, *, timeout_seconds: int, interval_seconds: int, base_url: str) -> dict[str, Any]:
    started = utc_now()
    state = generated_state(root)
    expected_block = state.get("latest_generated_block")
    expected_token = state.get("generated_current_token_id")
    expected_bid = str(state.get("generated_current_bid_eth") or "")
    deadline = time.monotonic() + timeout_seconds
    result = "timeout"
    last_error = ""
    verified_at = ""
    public_current_url = urllib.parse.urljoin(base_url.rstrip("/") + "/", "generated/current_auction.json")
    while time.monotonic() <= deadline:
        url = f"{public_current_url}?cache_bust={int(time.time())}"
        try:
            data = fetch_json(url)
            row = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
            live_block = int_or_none(row.get("latest_block"))
            live_token = int_or_none(row.get("token_id"))
            live_bid = str(row.get("current_bid_eth") or "")
            if live_block == expected_block and live_token == expected_token and live_bid == expected_bid:
                result = "verified"
                verified_at = utc_now()
                break
            last_error = f"live data mismatch block={live_block} token={live_token} bid={live_bid}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = str(exc)[:300]
        time.sleep(max(1, interval_seconds))
    completed = verified_at or utc_now()
    push_completed = env.get("DEGEN_DOGS_PUSH_COMPLETED_AT_UTC")
    event_block_time = env.get("DEGEN_DOGS_EVENT_BLOCK_TIME_UTC")
    return {
        "live_verify_started_at_utc": started,
        "live_verified_at_utc": verified_at or None,
        "live_verify_completed_at_utc": completed,
        "live_verify_result": result,
        "push_to_live_seconds": seconds_between(push_completed, completed) if push_completed else None,
        "block_to_live_seconds": seconds_between(event_block_time, completed) if event_block_time else None,
        "latest_generated_block": expected_block,
        "current_dog_token_id": expected_token,
        "error": last_error if result != "verified" else None,
    }


def write_env_file(path: Path, values: dict[str, Any]) -> None:
    mapping = {
        "live_verify_started_at_utc": "DEGEN_DOGS_LIVE_VERIFY_STARTED_AT_UTC",
        "live_verified_at_utc": "DEGEN_DOGS_LIVE_VERIFIED_AT_UTC",
        "live_verify_result": "DEGEN_DOGS_LIVE_VERIFY_RESULT",
        "push_to_live_seconds": "DEGEN_DOGS_PUSH_TO_LIVE_SECONDS",
        "block_to_live_seconds": "DEGEN_DOGS_BLOCK_TO_LIVE_SECONDS",
    }
    lines = []
    for key, env_key in mapping.items():
        value = values.get(key)
        if value is None:
            continue
        escaped = str(value).replace("'", "'\\''")
        lines.append(f"export {env_key}='{escaped}'")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mission 3 refresh telemetry helper")
    parser.add_argument("--root", default=str(ROOT), help="Repo root override for tests")
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record-refresh", help="Append a refresh/publish telemetry row from environment variables")
    record.add_argument("--result", required=True)
    record.add_argument("--error", default="")
    status = sub.add_parser("write-status", help="Write sanitized generated/refresh_status.json and public copy")
    status.add_argument("--prefer-current-env", action="store_true")
    sub.add_parser("validate-status", help="Validate generated refresh_status.json")
    sub.add_parser("metrics-summary", help="Print operator refresh/watch metrics summary")
    live = sub.add_parser("verify-live", help="Poll GitHub Pages generated/current_auction.json until it matches local generated data")
    live.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("DEGEN_DOGS_LIVE_VERIFY_TIMEOUT_SECONDS", "300")))
    live.add_argument("--interval-seconds", type=int, default=int(os.environ.get("DEGEN_DOGS_LIVE_VERIFY_INTERVAL_SECONDS", "10")))
    live.add_argument("--base-url", default=os.environ.get("DEGEN_DOGS_LIVE_VERIFY_BASE_URL", SITE_URL))
    live.add_argument("--env-file", help="Write shell exports for refresh_and_publish.sh")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    env = dict(os.environ)
    if args.command == "record-refresh":
        row = record_refresh(env, result=args.result, error=args.error, root=root)
        print(json.dumps(row, indent=2, sort_keys=True))
        return 0
    if args.command == "write-status":
        status = write_refresh_status(env, root=root, prefer_current_env=args.prefer_current_env)
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    if args.command == "validate-status":
        status = validate_refresh_status(root=root)
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    if args.command == "metrics-summary":
        print_summary(metrics_summary(env, root=root))
        return 0
    if args.command == "verify-live":
        result = verify_live(env, root=root, timeout_seconds=args.timeout_seconds, interval_seconds=args.interval_seconds, base_url=args.base_url)
        if args.env_file:
            write_env_file(Path(args.env_file), result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("live_verify_result") == "verified" else 2
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
