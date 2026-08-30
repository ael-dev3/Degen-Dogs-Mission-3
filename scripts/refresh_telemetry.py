#!/usr/bin/env python3
"""Structured telemetry and public refresh status helpers for Mission 3 runners."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
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
    re.compile(r"(?i)\b[A-Z]:\\[^\s\"']+"),
]
PUBLIC_URL_HOSTNAMES = frozenset(
    {
        "ael-dev3.github.io",
        "raw.githubusercontent.com",
        "base-rpc.publicnode.com",
        "mainnet.base.org",
        "base-mainnet.g.alchemy.com",
        "developer-access-mainnet.base.org",
    }
)
SUCCESS_RESULTS = {
    "success",
    "success_generated",
    "success_no_diff",
    "success_superseded_by_peer",
    "success_skip_push",
    "success_pushed",
    "success_pushed_live_timeout",
}
LIVE_STATUS_MAX_BYTES = 2 * 1024 * 1024
LIVE_ARTIFACT_MAX_BYTES = 32 * 1024 * 1024
RAW_STATUS_HOST = "raw.githubusercontent.com"
RAW_STATUS_PATH = re.compile(
    r"^/ael-dev3/Degen-Dogs-Mission-3/(?P<commit>[0-9a-f]{40})/public/generated/refresh_status\.json$"
)
LIVE_BUNDLE_FILENAME_PATTERN = (
    r"live_snapshot_[1-9][0-9]*_[0-9a-f]{64}_[0-9a-f]{64}\.json"
)
RAW_BUNDLE_PATH = re.compile(
    r"^/ael-dev3/Degen-Dogs-Mission-3/(?P<commit>[0-9a-f]{40})/"
    rf"public/generated/(?P<filename>{LIVE_BUNDLE_FILENAME_PATTERN})$"
)
PAGES_STATUS_HOST = "ael-dev3.github.io"
PAGES_STATUS_PATH = "/Degen-Dogs-Mission-3/generated/refresh_status.json"
PAGES_BUNDLE_PATH = re.compile(
    rf"^/Degen-Dogs-Mission-3/generated/(?P<filename>{LIVE_BUNDLE_FILENAME_PATTERN})$"
)
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
LIVE_STATUS_CACHE_BUST = re.compile(r"^cache_bust=[0-9]+$")
REFRESH_STATUS_INTEGER_FIELDS = frozenset(
    {
        "schema_version",
        "latest_generated_block",
        "current_dog_token_id",
        "onchain_chain_id",
        "snapshot_confirmations",
        "rpc_quorum_size",
        "dog_total_supply",
        "dog_id_ceiling",
        "dog_token_uri_present_count",
        "dog_token_uri_unavailable_count",
        "dog_base_existing_count",
        "dog_base_unclaimed_count",
        "dog_metadata_onchain_verified_count",
        "dog_metadata_unavailable_count",
        "dog_metadata_content_observed_count",
        "dog_rarity_universe_count",
        "dog_rarity_excluded_nonexistent_count",
        "dog_rarity_incomplete_metadata_count",
        "dog_rarity_attested_block",
        "dog_rarity_continuity_through_block",
        "dog_rarity_extension_mint_count",
        "live_snapshot_bundle_bytes",
        "live_snapshot_bundle_schema_version",
        "unified_dog_search_bytes",
    }
)


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow a live-verification redirect."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        return None


LIVE_STATUS_OPENER = urllib.request.build_opener(RejectRedirectHandler())


def immutable_raw_status_url(commit_sha: str) -> str:
    """Build the only raw GitHub artifact URL accepted by live verification."""
    if not isinstance(commit_sha, str) or not COMMIT_SHA.fullmatch(commit_sha):
        raise RuntimeError("live verification requires a canonical 40-hex pushed commit SHA")
    return (
        f"https://{RAW_STATUS_HOST}/ael-dev3/Degen-Dogs-Mission-3/"
        f"{commit_sha}/public/generated/refresh_status.json"
    )


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
    if value == SITE_URL:
        return SITE_URL
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except (TypeError, ValueError):
        return "<redacted-url>"
    hostname = (parts.hostname or "").lower().rstrip(".")
    if not hostname:
        return "<redacted-url>"
    if hostname in PUBLIC_URL_HOSTNAMES:
        host = hostname
    else:
        host_label = hashlib.sha256(hostname.encode("utf-8")).hexdigest()[:12]
        host = f"rpc-host-{host_label}"
    if port:
        host += f":{port}"
    path = ""
    if parts.path and parts.path != "/":
        path = "/<redacted-path>"
    elif parts.path == "/":
        path = "/"
    query = "redacted=1" if parts.query else ""
    return urllib.parse.urlunsplit(("https", host, path, query, ""))


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(redact_value(str(k))): redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    for url in re.findall(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+", text):
        text = text.replace(url, redact_url(url))
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}=<redacted>" if m.groups() else "<redacted-secret>", text)
    for pattern in PRIVATE_PATH_PATTERNS:
        text = pattern.sub("<redacted-path>", text)
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


def ensure_owned_directory_tree(directory: Path) -> None:
    missing: list[Path] = []
    cursor = directory
    while True:
        try:
            details = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor)
            if cursor.parent == cursor:
                raise RuntimeError(f"unable to find a trusted ancestor for private directory: {directory}")
            cursor = cursor.parent
            continue
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise RuntimeError(f"refusing unsafe private directory ancestor: {cursor}")
        if details.st_uid != os.getuid() or cursor.parent == cursor:
            break
        cursor = cursor.parent
    for item in reversed(missing):
        try:
            item.mkdir(mode=0o700)
        except FileExistsError:
            pass
        details = item.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
            raise RuntimeError(f"refusing unsafe private directory ancestor: {item}")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    ensure_owned_directory_tree(path.parent)
    path.parent.chmod(0o700)
    safe_row = redact_value(row)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        os.close(descriptor)
        raise RuntimeError(f"refusing telemetry path that is not an owned regular file: {path}")
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        # Cooperates with the health watchdog's inode-preserving JSONL
        # compaction so a 30-second watcher append cannot race retention.
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(safe_row, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()


def read_jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return []
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
        os.close(descriptor)
        raise RuntimeError(f"refusing unsafe telemetry file: {path}")
    rows: list[dict[str, Any]] = []
    with os.fdopen(descriptor, encoding="utf-8") as handle:
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
    ensure_owned_directory_tree(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
        "onchain_verification_status": str(metrics.get("onchain_verification_status") or ""),
        "onchain_verification_scope": str(metrics.get("onchain_verification_scope") or ""),
        "onchain_chain_id": int_or_none(metrics.get("onchain_chain_id")),
        "snapshot_block_hash": str(metrics.get("snapshot_block_hash") or ""),
        "snapshot_confirmations": int_or_none(metrics.get("snapshot_confirmations")),
        "rpc_quorum_size": int_or_none(metrics.get("rpc_quorum_size")),
        "rpc_quorum_agreement": str(metrics.get("rpc_quorum_agreement") or ""),
        "rpc_quorum_providers": str(metrics.get("rpc_quorum_providers") or ""),
        "log_rpc_quorum_providers": str(metrics.get("log_rpc_quorum_providers") or ""),
        "auction_house_code_sha256": str(metrics.get("auction_house_code_sha256") or ""),
        "dog_nft_code_sha256": str(metrics.get("dog_nft_code_sha256") or ""),
        "dog_total_supply": int_or_none(metrics.get("dog_total_supply")),
        "dog_id_ceiling": int_or_none(metrics.get("dog_id_ceiling")),
        "dog_token_uri_verification_status": str(metrics.get("dog_token_uri_verification_status") or ""),
        "dog_base_existence_verification_status": str(metrics.get("dog_base_existence_verification_status") or ""),
        "dog_token_uri_present_count": int_or_none(metrics.get("dog_token_uri_present_count")),
        "dog_token_uri_unavailable_count": int_or_none(metrics.get("dog_token_uri_unavailable_count")),
        "dog_base_existing_count": int_or_none(metrics.get("dog_base_existing_count")),
        "dog_base_unclaimed_count": int_or_none(metrics.get("dog_base_unclaimed_count")),
        "dog_base_existing_token_ids_sha256": str(metrics.get("dog_base_existing_token_ids_sha256") or ""),
        "dog_base_unclaimed_token_ids_sha256": str(metrics.get("dog_base_unclaimed_token_ids_sha256") or ""),
        "dog_metadata_verification_status": str(metrics.get("dog_metadata_verification_status") or ""),
        "dog_metadata_onchain_verified_count": int_or_none(metrics.get("dog_metadata_onchain_verified_count")),
        "dog_metadata_unavailable_count": int_or_none(metrics.get("dog_metadata_unavailable_count")),
        "dog_metadata_content_verification_status": str(metrics.get("dog_metadata_content_verification_status") or ""),
        "dog_metadata_content_observed_count": int_or_none(metrics.get("dog_metadata_content_observed_count")),
        "dog_rarity_verification_status": str(metrics.get("dog_rarity_verification_status") or ""),
        "dog_rarity_universe_count": int_or_none(metrics.get("dog_rarity_universe_count")),
        "dog_rarity_excluded_nonexistent_count": int_or_none(metrics.get("dog_rarity_excluded_nonexistent_count")),
        "dog_rarity_incomplete_metadata_count": int_or_none(metrics.get("dog_rarity_incomplete_metadata_count")),
        "dog_rarity_scope": str(metrics.get("dog_rarity_scope") or ""),
        "dog_rarity_attested_block": int_or_none(metrics.get("dog_rarity_attested_block")),
        "dog_rarity_attested_block_hash": str(metrics.get("dog_rarity_attested_block_hash") or ""),
        "dog_rarity_continuity_through_block": int_or_none(metrics.get("dog_rarity_continuity_through_block")),
        "dog_rarity_continuity_through_block_hash": str(metrics.get("dog_rarity_continuity_through_block_hash") or ""),
        "dog_rarity_continuity_verification_status": str(metrics.get("dog_rarity_continuity_verification_status") or ""),
        "dog_rarity_extension_mint_count": int_or_none(
            metrics.get("dog_rarity_extension_mint_count")
        ),
        "dog_rarity_extension_mint_token_ids": str(
            metrics.get("dog_rarity_extension_mint_token_ids") or ""
        ),
        "dog_rarity_extension_mint_token_ids_sha256": str(
            metrics.get("dog_rarity_extension_mint_token_ids_sha256") or ""
        ),
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
    observed = env_timestamp(env, "DEGEN_DOGS_OBSERVED_AT_UTC") or detected
    event_block_time = env_timestamp(env, "DEGEN_DOGS_EVENT_BLOCK_TIME_UTC")
    raw_commit_verified = str(env.get("DEGEN_DOGS_RAW_COMMIT_VERIFIED") or "").strip().lower()
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
        "observed_at_utc": observed,
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
        "event_to_observation_seconds": seconds_between(event_block_time, observed) if event_block_time and observed else None,
        "observation_to_push_seconds": seconds_between(observed, pushed) if observed and pushed else None,
        "block_to_push_seconds": seconds_between(event_block_time, pushed) if event_block_time and pushed else None,
        "queue_generation": int_or_none(env.get("DEGEN_DOGS_PUBLICATION_GENERATION")),
        "queue_digest": env.get("DEGEN_DOGS_PUBLICATION_DIGEST") or None,
        "queue_outcome": env.get("DEGEN_DOGS_QUEUE_OUTCOME") or None,
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
        "raw_commit_verified": raw_commit_verified in {"1", "true", "yes"} if raw_commit_verified else None,
        "live_verify_error": redact_value(env.get("DEGEN_DOGS_LIVE_VERIFY_ERROR") or "") or None,
        "error": redact_value(error or env.get("DEGEN_DOGS_REFRESH_ERROR") or "") or None,
    }
    row.update(generated_state(root))
    return redact_value({key: value for key, value in row.items() if value not in (None, [], "")})


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
    out = redact_value(dict(row))
    out.setdefault("schema_version", SCHEMA_VERSION)
    out.setdefault("kind", "watcher_check")
    if out.get("started_at_utc") and out.get("completed_at_utc") and out.get("duration_seconds") is None:
        out["duration_seconds"] = seconds_between(out["started_at_utc"], out["completed_at_utc"])
    if out.get("event_block_time_utc") and out.get("observation_created_at_utc") and out.get("event_to_observation_seconds") is None:
        out["event_to_observation_seconds"] = seconds_between(out["event_block_time_utc"], out["observation_created_at_utc"])
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
        "onchain_verification_status": state.get("onchain_verification_status"),
        "onchain_verification_scope": state.get("onchain_verification_scope"),
        "onchain_chain_id": state.get("onchain_chain_id"),
        "snapshot_block_hash": state.get("snapshot_block_hash"),
        "snapshot_confirmations": state.get("snapshot_confirmations"),
        "rpc_quorum_size": state.get("rpc_quorum_size"),
        "rpc_quorum_agreement": state.get("rpc_quorum_agreement"),
        "rpc_quorum_providers": state.get("rpc_quorum_providers"),
        "log_rpc_quorum_providers": state.get("log_rpc_quorum_providers"),
        "auction_house_code_sha256": state.get("auction_house_code_sha256"),
        "dog_nft_code_sha256": state.get("dog_nft_code_sha256"),
        "dog_total_supply": state.get("dog_total_supply"),
        "dog_id_ceiling": state.get("dog_id_ceiling"),
        "dog_token_uri_verification_status": state.get("dog_token_uri_verification_status"),
        "dog_base_existence_verification_status": state.get("dog_base_existence_verification_status"),
        "dog_token_uri_present_count": state.get("dog_token_uri_present_count"),
        "dog_token_uri_unavailable_count": state.get("dog_token_uri_unavailable_count"),
        "dog_base_existing_count": state.get("dog_base_existing_count"),
        "dog_base_unclaimed_count": state.get("dog_base_unclaimed_count"),
        "dog_base_existing_token_ids_sha256": state.get("dog_base_existing_token_ids_sha256"),
        "dog_base_unclaimed_token_ids_sha256": state.get("dog_base_unclaimed_token_ids_sha256"),
        "dog_metadata_verification_status": state.get("dog_metadata_verification_status"),
        "dog_metadata_onchain_verified_count": state.get("dog_metadata_onchain_verified_count"),
        "dog_metadata_unavailable_count": state.get("dog_metadata_unavailable_count"),
        "dog_metadata_content_verification_status": state.get("dog_metadata_content_verification_status"),
        "dog_metadata_content_observed_count": state.get("dog_metadata_content_observed_count"),
        "dog_rarity_verification_status": state.get("dog_rarity_verification_status"),
        "dog_rarity_universe_count": state.get("dog_rarity_universe_count"),
        "dog_rarity_excluded_nonexistent_count": state.get("dog_rarity_excluded_nonexistent_count"),
        "dog_rarity_incomplete_metadata_count": state.get("dog_rarity_incomplete_metadata_count"),
        "dog_rarity_scope": state.get("dog_rarity_scope"),
        "dog_rarity_attested_block": state.get("dog_rarity_attested_block"),
        "dog_rarity_attested_block_hash": state.get("dog_rarity_attested_block_hash"),
        "dog_rarity_continuity_through_block": state.get("dog_rarity_continuity_through_block"),
        "dog_rarity_continuity_through_block_hash": state.get("dog_rarity_continuity_through_block_hash"),
        "dog_rarity_continuity_verification_status": state.get("dog_rarity_continuity_verification_status"),
        "dog_rarity_extension_mint_count": state.get("dog_rarity_extension_mint_count"),
        "dog_rarity_extension_mint_token_ids": state.get("dog_rarity_extension_mint_token_ids"),
        "dog_rarity_extension_mint_token_ids_sha256": state.get(
            "dog_rarity_extension_mint_token_ids_sha256"
        ),
    }
    clean = redact_value({key: value for key, value in status.items() if value not in (None, "", [])})
    assert_public_safe(clean)
    return clean


def write_refresh_status(env: dict[str, str], root: Path = ROOT, *, prefer_current_env: bool = False) -> dict[str, Any]:
    previous_status = load_json(root / "generated" / "refresh_status.json", {})
    previous_bundle = (
        previous_status.get("live_snapshot_bundle")
        if isinstance(previous_status, dict)
        and isinstance(previous_status.get("live_snapshot_bundle"), str)
        else None
    )
    status = public_refresh_status(env, root=root, prefer_current_env=prefer_current_env)
    write_json(root / "generated" / "refresh_status.json", status)
    write_json(root / "public" / "generated" / "refresh_status.json", status)
    # Every public status rewrite must recompute the immutable pointer.  This
    # includes the publisher's final telemetry rewrite after the dashboard
    # build, preventing that final write from silently dropping attestation.
    from build_live_snapshot_bundle import build_live_snapshot_bundle

    return build_live_snapshot_bundle(root=root, previous_bundle=previous_bundle)


def validate_refresh_status(
    root: Path = ROOT,
    *,
    validate_live_snapshot: bool = True,
) -> dict[str, Any]:
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
    invalid_integer_types = sorted(
        key
        for key in REFRESH_STATUS_INTEGER_FIELDS.intersection(status)
        if type(status[key]) is not int
    )
    if invalid_integer_types:
        raise AssertionError(
            "refresh_status integer fields have invalid JSON types: "
            + ", ".join(invalid_integer_types)
        )
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
        "onchain_verification_status",
        "onchain_verification_scope",
        "onchain_chain_id",
        "snapshot_block_hash",
        "snapshot_confirmations",
        "rpc_quorum_size",
        "rpc_quorum_agreement",
        "rpc_quorum_providers",
        "log_rpc_quorum_providers",
        "auction_house_code_sha256",
        "dog_nft_code_sha256",
        "dog_total_supply",
        "dog_id_ceiling",
        "dog_token_uri_verification_status",
        "dog_base_existence_verification_status",
        "dog_token_uri_present_count",
        "dog_token_uri_unavailable_count",
        "dog_base_existing_count",
        "dog_base_unclaimed_count",
        "dog_base_existing_token_ids_sha256",
        "dog_base_unclaimed_token_ids_sha256",
        "dog_metadata_verification_status",
        "dog_metadata_onchain_verified_count",
        "dog_metadata_unavailable_count",
        "dog_metadata_content_verification_status",
        "dog_metadata_content_observed_count",
        "dog_rarity_verification_status",
        "dog_rarity_universe_count",
        "dog_rarity_excluded_nonexistent_count",
        "dog_rarity_incomplete_metadata_count",
        "dog_rarity_scope",
        "dog_rarity_attested_block",
        "dog_rarity_attested_block_hash",
        "dog_rarity_continuity_through_block",
        "dog_rarity_continuity_through_block_hash",
        "dog_rarity_continuity_verification_status",
        "live_snapshot_bundle",
        "live_snapshot_bundle_sha256",
        "live_snapshot_bundle_bytes",
        "live_snapshot_bundle_schema_version",
        "unified_dog_search_sha256",
        "unified_dog_search_bytes",
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
    if status.get("onchain_verification_status") != "current_snapshot_cross_provider_verified":
        raise AssertionError("refresh_status onchain_verification_status is not cross-provider verified")
    required_scope = {
        "snapshot_hash",
        "contract_code",
        "current_auction",
        "dog_total_supply",
        "dog_token_uri_bindings",
        "recent_event_logs",
    }
    actual_scope = {item.strip() for item in str(status.get("onchain_verification_scope") or "").split(",") if item.strip()}
    if not required_scope.issubset(actual_scope):
        raise AssertionError("refresh_status onchain_verification_scope is incomplete")
    if int_or_none(status.get("onchain_chain_id")) != 8453:
        raise AssertionError("refresh_status onchain_chain_id is not Base mainnet")
    if not re.fullmatch(r"0x[a-fA-F0-9]{64}", str(status.get("snapshot_block_hash") or "")):
        raise AssertionError("refresh_status snapshot_block_hash is invalid")
    if int_or_none(status.get("rpc_quorum_size")) is None or int(status["rpc_quorum_size"]) < 2:
        raise AssertionError("refresh_status rpc_quorum_size is invalid")
    quorum_size = int(status["rpc_quorum_size"])
    agreement = re.fullmatch(r"(\d+)/(\d+)", str(status.get("rpc_quorum_agreement") or ""))
    if not agreement or int(agreement.group(1)) < quorum_size or int(agreement.group(1)) > int(agreement.group(2)):
        raise AssertionError("refresh_status rpc_quorum_agreement is invalid")
    for key in ("rpc_quorum_providers", "log_rpc_quorum_providers"):
        providers = {item.strip() for item in re.split(r"[,|]", str(status.get(key) or "")) if item.strip()}
        if len(providers) < quorum_size:
            raise AssertionError(f"refresh_status {key} is below quorum")
    if int_or_none(status.get("snapshot_confirmations")) is None or int(status["snapshot_confirmations"]) < 1:
        raise AssertionError("refresh_status snapshot_confirmations must be at least one")
    for key in ("auction_house_code_sha256", "dog_nft_code_sha256"):
        if not re.fullmatch(r"[a-fA-F0-9]{64}", str(status.get(key) or "")):
            raise AssertionError(f"refresh_status {key} is invalid")
    full_token_uri_status = "hash_pinned_cross_provider_exact_outcome_quorum"
    continuity_token_uri_status = "baseline_hash_pinned_quorum_plus_cross_provider_rarity_event_continuity"
    full_existence_status = "hash_pinned_cross_provider_exists_token_uri_parity_quorum"
    continuity_existence_status = (
        "baseline_exists_token_uri_quorum_plus_cross_provider_rarity_event_continuity"
    )
    full_continuity_status = "full_snapshot_exists_token_uri_content_schema_attested"
    incremental_continuity_status = (
        "hash_pinned_cross_provider_no_existence_or_token_uri_mutation_events_since_attestation"
    )
    extended_continuity_status = (
        "hash_pinned_cross_provider_canonical_mint_extension_plus_no_other_rarity_mutations"
    )
    if status.get("dog_token_uri_verification_status") not in {
        full_token_uri_status,
        continuity_token_uri_status,
    }:
        raise AssertionError("refresh_status tokenURI outcomes are not hash-pinned and cross-provider verified")
    if status.get("dog_base_existence_verification_status") not in {
        full_existence_status,
        continuity_existence_status,
    }:
        raise AssertionError("refresh_status Base exists/tokenURI parity is not cross-provider verified")
    rarity_attested_block = int_or_none(status.get("dog_rarity_attested_block"))
    rarity_continuity_block = int_or_none(status.get("dog_rarity_continuity_through_block"))
    latest_generated_block = int_or_none(status.get("latest_generated_block"))
    if (
        rarity_attested_block is None
        or rarity_continuity_block is None
        or latest_generated_block is None
        or rarity_attested_block <= 0
        or rarity_attested_block > rarity_continuity_block
        or rarity_continuity_block != latest_generated_block
    ):
        raise AssertionError("refresh_status rarity attestation block range is invalid")
    for key in ("dog_rarity_attested_block_hash", "dog_rarity_continuity_through_block_hash"):
        if not re.fullmatch(r"0x[a-fA-F0-9]{64}", str(status.get(key) or "")):
            raise AssertionError(f"refresh_status {key} is invalid")
    if str(status.get("dog_rarity_continuity_through_block_hash")).lower() != str(
        status.get("snapshot_block_hash")
    ).lower():
        raise AssertionError("refresh_status rarity continuity hash differs from the snapshot")
    continuity_status = status.get("dog_rarity_continuity_verification_status")
    if continuity_status == full_continuity_status:
        if (
            status.get("dog_token_uri_verification_status") != full_token_uri_status
            or status.get("dog_base_existence_verification_status") != full_existence_status
            or rarity_attested_block != latest_generated_block
            or str(status.get("dog_rarity_attested_block_hash")).lower()
            != str(status.get("snapshot_block_hash")).lower()
        ):
            raise AssertionError("refresh_status full rarity attestation is internally inconsistent")
    elif continuity_status in {incremental_continuity_status, extended_continuity_status}:
        if (
            status.get("dog_token_uri_verification_status") != continuity_token_uri_status
            or status.get("dog_base_existence_verification_status") != continuity_existence_status
        ):
            raise AssertionError("refresh_status incremental rarity continuity is internally inconsistent")
        if continuity_status == extended_continuity_status:
            raw_ids = str(status.get("dog_rarity_extension_mint_token_ids") or "")
            extension_count = int_or_none(status.get("dog_rarity_extension_mint_count"))
            if extension_count is None or not re.fullmatch(
                r"(?:0|[1-9][0-9]*)(?:,(?:0|[1-9][0-9]*))*",
                raw_ids,
            ):
                raise AssertionError("refresh_status rarity mint-extension provenance is missing")
            try:
                extension_ids = tuple(int(value) for value in raw_ids.split(","))
            except ValueError as exc:
                raise AssertionError("refresh_status rarity mint-extension IDs are malformed") from exc
            expected_hash = hashlib.sha256(raw_ids.encode("ascii")).hexdigest()
            extension_total_supply = int_or_none(status.get("dog_total_supply"))
            if (
                not extension_ids
                or extension_total_supply is None
                or len(extension_ids) != extension_count
            ):
                raise AssertionError("refresh_status rarity mint-extension provenance is inconsistent")
            expected_extension_ids = tuple(
                range(
                    extension_total_supply - extension_count,
                    extension_total_supply,
                )
            )
            if (
                tuple(sorted(set(extension_ids))) != extension_ids
                or extension_ids != expected_extension_ids
                or status.get("dog_rarity_extension_mint_token_ids_sha256") != expected_hash
            ):
                raise AssertionError("refresh_status rarity mint-extension provenance is inconsistent")
    else:
        raise AssertionError("refresh_status rarity continuity verification status is unsupported")
    if continuity_status != extended_continuity_status and any(
        status.get(key) not in (None, "", [])
        for key in (
            "dog_rarity_extension_mint_count",
            "dog_rarity_extension_mint_token_ids",
            "dog_rarity_extension_mint_token_ids_sha256",
        )
    ):
        raise AssertionError("refresh_status rarity mint-extension provenance contradicts its status")
    dog_total_supply = int_or_none(status.get("dog_total_supply"))
    dog_id_ceiling = int_or_none(status.get("dog_id_ceiling"))
    token_uri_present = int_or_none(status.get("dog_token_uri_present_count"))
    token_uri_unavailable = int_or_none(status.get("dog_token_uri_unavailable_count"))
    base_existing = int_or_none(status.get("dog_base_existing_count"))
    base_unclaimed = int_or_none(status.get("dog_base_unclaimed_count"))
    metadata_verified = int_or_none(status.get("dog_metadata_onchain_verified_count"))
    metadata_unavailable = int_or_none(status.get("dog_metadata_unavailable_count"))
    metadata_content_observed = int_or_none(status.get("dog_metadata_content_observed_count"))
    rarity_universe = int_or_none(status.get("dog_rarity_universe_count"))
    rarity_excluded = int_or_none(status.get("dog_rarity_excluded_nonexistent_count"))
    rarity_incomplete = int_or_none(status.get("dog_rarity_incomplete_metadata_count"))
    if None in {
        dog_total_supply,
        dog_id_ceiling,
        token_uri_present,
        token_uri_unavailable,
        base_existing,
        base_unclaimed,
        metadata_verified,
        metadata_unavailable,
        metadata_content_observed,
        rarity_universe,
        rarity_excluded,
        rarity_incomplete,
    }:
        raise AssertionError("refresh_status tokenURI/metadata aggregate counts are invalid")
    assert dog_total_supply is not None
    assert dog_id_ceiling is not None
    assert token_uri_present is not None
    assert token_uri_unavailable is not None
    assert base_existing is not None
    assert base_unclaimed is not None
    assert metadata_verified is not None
    assert metadata_unavailable is not None
    assert metadata_content_observed is not None
    assert rarity_universe is not None
    assert rarity_excluded is not None
    assert rarity_incomplete is not None
    if min(
        dog_id_ceiling,
        token_uri_present,
        token_uri_unavailable,
        base_existing,
        base_unclaimed,
        metadata_verified,
        metadata_unavailable,
        metadata_content_observed,
        rarity_universe,
        rarity_excluded,
        rarity_incomplete,
    ) < 0:
        raise AssertionError("refresh_status tokenURI/metadata aggregate counts cannot be negative")
    if dog_id_ceiling != dog_total_supply:
        raise AssertionError("refresh_status Dog ID ceiling contradicts legacy dog_total_supply")
    if token_uri_present + token_uri_unavailable != dog_total_supply:
        raise AssertionError("refresh_status tokenURI aggregate counts do not equal Dog total supply")
    if base_existing != token_uri_present or base_unclaimed != token_uri_unavailable:
        raise AssertionError("refresh_status Base existence counts contradict tokenURI outcomes")
    for key in ("dog_base_existing_token_ids_sha256", "dog_base_unclaimed_token_ids_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(status.get(key) or "")):
            raise AssertionError(f"refresh_status {key} is invalid")
    if metadata_verified + metadata_unavailable != dog_total_supply:
        raise AssertionError("refresh_status metadata aggregate counts do not equal Dog total supply")
    if metadata_unavailable < token_uri_unavailable:
        raise AssertionError("refresh_status hides tokenURI-unavailable Dogs from metadata unavailability")
    if metadata_content_observed != metadata_verified:
        raise AssertionError("refresh_status observed metadata content count contradicts verified tokenURIs")
    if (
        status.get("dog_metadata_content_verification_status")
        != "verified_token_uri_offchain_content_hash_observed"
    ):
        raise AssertionError("refresh_status metadata content verification class is unsupported")
    expected_metadata_status = (
        "complete_onchain_token_uri_verified"
        if metadata_unavailable == 0
        else "partial_onchain_token_uri_unavailable"
        if metadata_unavailable == token_uri_unavailable
        else "incomplete_metadata_unavailable"
    )
    if status.get("dog_metadata_verification_status") != expected_metadata_status:
        raise AssertionError("refresh_status dog metadata aggregate status contradicts its counts")
    if rarity_universe != metadata_verified or rarity_excluded != token_uri_unavailable:
        raise AssertionError("refresh_status rarity coverage contradicts verified Base metadata")
    if rarity_incomplete != metadata_unavailable - token_uri_unavailable:
        raise AssertionError("refresh_status rarity incomplete count contradicts metadata outcomes")
    expected_rarity_status = (
        "complete_verified_existing_token_universe"
        if rarity_universe > 0 and rarity_incomplete == 0
        else "unavailable_no_verified_existing_tokens"
        if rarity_universe == 0
        else "incomplete_existing_token_metadata"
    )
    if status.get("dog_rarity_verification_status") != expected_rarity_status:
        raise AssertionError("refresh_status rarity status contradicts its counts")
    if status.get("dog_rarity_scope") != "base_existing":
        raise AssertionError("refresh_status rarity scope is not Base-existing")
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
    for key in (
        "onchain_verification_status",
        "onchain_verification_scope",
        "onchain_chain_id",
        "snapshot_block_hash",
        "snapshot_confirmations",
        "rpc_quorum_size",
        "rpc_quorum_agreement",
        "rpc_quorum_providers",
        "log_rpc_quorum_providers",
        "auction_house_code_sha256",
        "dog_nft_code_sha256",
        "dog_total_supply",
        "dog_id_ceiling",
        "dog_token_uri_verification_status",
        "dog_base_existence_verification_status",
        "dog_token_uri_present_count",
        "dog_token_uri_unavailable_count",
        "dog_base_existing_count",
        "dog_base_unclaimed_count",
        "dog_base_existing_token_ids_sha256",
        "dog_base_unclaimed_token_ids_sha256",
        "dog_metadata_verification_status",
        "dog_metadata_onchain_verified_count",
        "dog_metadata_unavailable_count",
        "dog_metadata_content_verification_status",
        "dog_metadata_content_observed_count",
        "dog_rarity_verification_status",
        "dog_rarity_universe_count",
        "dog_rarity_excluded_nonexistent_count",
        "dog_rarity_incomplete_metadata_count",
        "dog_rarity_scope",
        "dog_rarity_attested_block",
        "dog_rarity_attested_block_hash",
        "dog_rarity_continuity_through_block",
        "dog_rarity_continuity_through_block_hash",
        "dog_rarity_continuity_verification_status",
    ):
        if str(status.get(key, "")) != str(state.get(key, "")):
            raise AssertionError(f"refresh_status {key} differs from mission3_metrics")
    if continuity_status == extended_continuity_status:
        for key in (
            "dog_rarity_extension_mint_count",
            "dog_rarity_extension_mint_token_ids",
            "dog_rarity_extension_mint_token_ids_sha256",
        ):
            if str(status.get(key, "")) != str(state.get(key, "")):
                raise AssertionError(f"refresh_status {key} differs from mission3_metrics")
    if validate_live_snapshot:
        from build_live_snapshot_bundle import validate_live_snapshot_bundle

        validate_live_snapshot_bundle(root=root, status=status)
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
        reason = str(redact_value(str(exc))).splitlines()[0][:160]
        return {"label": label, "available": False, "reason": reason}
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


def fetch_live_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: int = 15,
) -> bytes:
    if type(max_bytes) is not int or max_bytes < 1 or max_bytes > LIVE_ARTIFACT_MAX_BYTES:
        raise RuntimeError("invalid live verification response-size limit")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid live verification URL") from exc
    host = parsed.hostname or ""
    raw_target = host == RAW_STATUS_HOST and RAW_STATUS_PATH.fullmatch(parsed.path)
    raw_bundle_target = host == RAW_STATUS_HOST and RAW_BUNDLE_PATH.fullmatch(parsed.path)
    pages_target = host == PAGES_STATUS_HOST and parsed.path == PAGES_STATUS_PATH
    pages_bundle_target = host == PAGES_STATUS_HOST and PAGES_BUNDLE_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc != host
        or parsed.fragment
        or (parsed.query and not LIVE_STATUS_CACHE_BUST.fullmatch(parsed.query))
        or not (raw_target or raw_bundle_target or pages_target or pages_bundle_target)
    ):
        raise RuntimeError("live verification URL is outside the fixed GitHub publication allowlist")
    expected_content_type = (
        "text/plain" if raw_target or raw_bundle_target else "application/json"
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "degen-dogs-refresh-verify/0.1"})
    try:
        response = LIVE_STATUS_OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"live verification HTTP {exc.code}") from None
    except Exception as exc:  # noqa: BLE001 - keep transport details out of public telemetry
        raise RuntimeError(f"live verification transport failed ({type(exc).__name__})") from None
    try:
        with response:
            if response.getcode() != 200:
                raise RuntimeError("live verification returned an unexpected HTTP status")
            if str(response.geturl()) != url:
                raise RuntimeError("live verification response URL changed unexpectedly")
            headers = getattr(response, "headers", None)
            content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() if headers else ""
            if content_type != expected_content_type:
                raise RuntimeError("live verification returned an unexpected content type")
            content_length = headers.get("Content-Length") if headers else None
            if content_length is not None:
                try:
                    parsed_length = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("live verification returned an invalid Content-Length") from exc
                if parsed_length < 0 or parsed_length > max_bytes:
                    raise RuntimeError("live verification response is too large")
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise RuntimeError("live verification response is too large")
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - keep response read details out of public telemetry
        raise RuntimeError(f"live verification response read failed ({type(exc).__name__})") from None
    return payload


def fetch_json(url: str, timeout: int = 15) -> Any:
    payload = fetch_live_bytes(
        url,
        max_bytes=LIVE_STATUS_MAX_BYTES,
        timeout=timeout,
    )
    try:
        return json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("live verification response is not valid UTF-8 JSON") from exc


def snapshot_mismatch(expected: Any, actual: Any) -> str:
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return f"type expected={type(expected).__name__} actual={type(actual).__name__}"

    def exactly_equal(left: Any, right: Any) -> bool:
        if type(left) is not type(right):
            return False
        if isinstance(left, dict):
            return left.keys() == right.keys() and all(exactly_equal(left[key], right[key]) for key in left)
        if isinstance(left, list):
            return len(left) == len(right) and all(exactly_equal(a, b) for a, b in zip(left, right, strict=True))
        return bool(left == right)

    keys = sorted({*expected, *actual})
    differing = [key for key in keys if key not in expected or key not in actual or not exactly_equal(expected[key], actual[key])]
    return "fields=" + ",".join(differing[:12]) if differing else ""


def live_bundle_url(status_url: str, filename: str) -> str:
    if not isinstance(filename, str) or not re.fullmatch(
        LIVE_BUNDLE_FILENAME_PATTERN,
        filename,
    ):
        raise RuntimeError("live verification status has an unsafe bundle filename")
    parsed = urllib.parse.urlsplit(status_url)
    parent = parsed.path.rsplit("/", 1)[0]
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, f"{parent}/{filename}", "", "")
    )


def fetch_verified_remote_snapshot(
    source: str,
    status_url: str,
    expected_status: dict[str, Any],
    expected_bundle: bytes,
) -> None:
    cache_bust = time.time_ns()
    status = fetch_json(f"{status_url}?cache_bust={cache_bust}")
    mismatch = snapshot_mismatch(expected_status, status)
    if mismatch:
        raise RuntimeError(f"{source} refresh_status mismatch {mismatch}")
    filename = expected_status.get("live_snapshot_bundle")
    expected_size = expected_status.get("live_snapshot_bundle_bytes")
    expected_sha256 = expected_status.get("live_snapshot_bundle_sha256")
    if (
        not isinstance(filename, str)
        or not re.fullmatch(LIVE_BUNDLE_FILENAME_PATTERN, filename)
        or type(expected_size) is not int
        or expected_size < 1
        or expected_size > LIVE_ARTIFACT_MAX_BYTES
        or not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        raise RuntimeError("local refresh_status live snapshot pointer is invalid")
    bundle_url = live_bundle_url(status_url, filename)
    payload = fetch_live_bytes(
        f"{bundle_url}?cache_bust={time.time_ns()}",
        max_bytes=expected_size,
    )
    if len(payload) != expected_size:
        raise RuntimeError(f"{source} live snapshot bundle size mismatch")
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RuntimeError(f"{source} live snapshot bundle SHA256 mismatch")
    if payload != expected_bundle:
        raise RuntimeError(f"{source} live snapshot bundle bytes mismatch")


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
    expected_status = load_json(root / "public" / "generated" / "refresh_status.json", {})
    if not isinstance(expected_status, dict) or not expected_status:
        expected_status = load_json(root / "generated" / "refresh_status.json", {})
    if not isinstance(expected_status, dict) or not expected_status:
        raise RuntimeError("local refresh_status.json is missing or invalid")
    from build_live_snapshot_bundle import validate_live_snapshot_bundle

    try:
        local_bundle = validate_live_snapshot_bundle(root=root, status=expected_status)
    except AssertionError as exc:
        raise RuntimeError(f"local live snapshot validation failed: {str(exc)[:260]}") from None
    bundle_path = (
        root
        / "public"
        / "generated"
        / str(local_bundle["filename"])
    )
    expected_bundle = bundle_path.read_bytes()
    raw_commit_url = immutable_raw_status_url(env.get("DEGEN_DOGS_COMMIT_SHA", ""))
    status_urls = [
        ("raw_commit", raw_commit_url),
        ("github_pages", urllib.parse.urljoin(base_url.rstrip("/") + "/", "generated/refresh_status.json")),
    ]
    verified_source = ""
    raw_commit_verified = False
    pages_bundle_verified = False
    poll_interval_seconds = min(5, max(1, int(interval_seconds)))
    while time.monotonic() <= deadline:
        pages_bundle_verified = False
        for source, status_url in status_urls:
            if source == "raw_commit" and raw_commit_verified:
                # The raw URL is immutable at the exact pushed commit. Once its
                # status and pointer-target bytes verify, cache that proof for
                # this invocation while only polling the mutable Pages edge.
                continue
            try:
                fetch_verified_remote_snapshot(
                    source,
                    status_url,
                    expected_status,
                    expected_bundle,
                )
                if source == "raw_commit":
                    raw_commit_verified = True
                elif source == "github_pages":
                    pages_bundle_verified = True
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, OSError, RuntimeError) as exc:
                last_error = f"{source}: {str(redact_value(str(exc)))[:260]}"
        # The immutable raw URL proves the exact pushed artifact landed; Pages
        # proves the user-facing deployment completed. Neither alone is live.
        if raw_commit_verified and pages_bundle_verified:
            result = "verified"
            verified_at = utc_now()
            verified_source = "github_pages"
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval_seconds, remaining))
    completed = verified_at or utc_now()
    push_completed = env.get("DEGEN_DOGS_PUSH_COMPLETED_AT_UTC")
    event_block_time = env.get("DEGEN_DOGS_EVENT_BLOCK_TIME_UTC")
    return {
        "live_verify_started_at_utc": started,
        "live_verified_at_utc": verified_at or None,
        "live_verify_completed_at_utc": completed,
        "live_verify_result": result,
        "live_verify_source": verified_source or None,
        "raw_commit_verified": raw_commit_verified,
        # Retain the old result key for existing private telemetry readers. It
        # now aliases immutable-commit verification; no mutable main URL is used.
        "raw_main_verified": raw_commit_verified,
        "live_snapshot_bundle": expected_status.get("live_snapshot_bundle"),
        "live_snapshot_bundle_verified": result == "verified",
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
        "raw_commit_verified": "DEGEN_DOGS_RAW_COMMIT_VERIFIED",
        "error": "DEGEN_DOGS_LIVE_VERIFY_ERROR",
    }
    lines = []
    for key, env_key in mapping.items():
        value = values.get(key)
        if value is None:
            continue
        escaped = str(value).replace("'", "'\\''")
        lines.append(f"export {env_key}='{escaped}'")
    payload = "\n".join(lines) + ("\n" if lines else "")
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        os.close(descriptor)
        raise RuntimeError(f"refusing unsafe live-verification environment file: {path}")
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


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
    live = sub.add_parser("verify-live", help="Poll the immutable pushed commit and GitHub Pages until both exactly match local refresh_status.json")
    live.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("DEGEN_DOGS_LIVE_VERIFY_TIMEOUT_SECONDS", "300")))
    live.add_argument("--interval-seconds", type=int, default=int(os.environ.get("DEGEN_DOGS_LIVE_VERIFY_INTERVAL_SECONDS", "5")))
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
