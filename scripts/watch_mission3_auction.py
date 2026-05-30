#!/usr/bin/env python3
"""Event-aware Degen Dogs Mission 3 auction refresh watcher.

The watcher is intentionally local-runner oriented: it performs a cheap Base RPC
state/log check, compares it with a local untracked state file, and only launches
the heavier dashboard refresh when current auction activity changed.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[1]
CHAIN_ID = 8453
DEFAULT_RPC_URLS = [
    "https://base-rpc.publicnode.com",
    "https://mainnet.base.org",
    "https://developer-access-mainnet.base.org",
]
DEFAULT_LOG_RPC_URLS = ["https://mainnet.base.org"]

MISSION3_CONTRACTS_CONFIG = ROOT / "archive" / "mission3" / "config" / "mission3_contracts.verified.json"
MISSION3_EVENTS_CONFIG = ROOT / "archive" / "mission3" / "config" / "mission3_events.verified.json"
FALLBACK_AUCTION_HOUSE = "0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA"
FALLBACK_TOPIC_BY_EVENT = {
    "AuctionCreated": "0xd6eddd1118d71820909c1197aa966dbc15ed6f508554252169cc3d5ccac756ca",
    "AuctionBid": "0x1159164c56f277e6fc99c11731bd380e0347deb969b75523398734c252706ea3",
    "AuctionExtended": "0x6e912a3a9105bdd2af817ba5adc14e6c127c1035b5b648faa29ca0d58ab8ff4e",
    "AuctionSettled": "0xc9f72b276a388619c6d185d146697036241880c36654b1a3ffdad07c24038d99",
}
SELECTOR_AUCTION = "0x7d9f6db5"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
SCHEMA_VERSION = 1


def _load_verified_auction_house() -> str:
    try:
        data = json.loads(MISSION3_CONTRACTS_CONFIG.read_text(encoding="utf-8"))
        if int(data.get("chain_id")) != CHAIN_ID or data.get("confidence") != "verified":
            raise ValueError("contract config is not verified for Base mainnet")
        address = str(data["contracts"]["auction_house"]["address"])
        if not re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
            raise ValueError("auction_house address is invalid")
        return address
    except Exception:
        return FALLBACK_AUCTION_HOUSE


def _load_verified_event_topics() -> dict[str, str]:
    try:
        data = json.loads(MISSION3_EVENTS_CONFIG.read_text(encoding="utf-8"))
        if int(data.get("chain_id")) != CHAIN_ID or data.get("confidence") != "verified":
            raise ValueError("event config is not verified for Base mainnet")
        topics = {event["name"]: str(event["topic0"]).lower() for event in data.get("events", [])}
        missing = set(FALLBACK_TOPIC_BY_EVENT) - set(topics)
        if missing:
            raise ValueError(f"missing event topics: {sorted(missing)}")
        return {name: topics[name] for name in FALLBACK_TOPIC_BY_EVENT}
    except Exception:
        return dict(FALLBACK_TOPIC_BY_EVENT)


AUCTION_HOUSE = _load_verified_auction_house()
TOPIC_BY_EVENT = _load_verified_event_topics()
TOPIC_AUCTION_CREATED = TOPIC_BY_EVENT["AuctionCreated"]
TOPIC_AUCTION_BID = TOPIC_BY_EVENT["AuctionBid"]
TOPIC_AUCTION_EXTENDED = TOPIC_BY_EVENT["AuctionExtended"]
TOPIC_AUCTION_SETTLED = TOPIC_BY_EVENT["AuctionSettled"]
TOPIC_EVENT_NAMES = {topic.lower(): name for name, topic in TOPIC_BY_EVENT.items()}
WATCHED_EVENT_NAMES = ["AuctionCreated", "AuctionBid", "AuctionExtended", "AuctionSettled"]
WATCHED_TOPICS = [TOPIC_BY_EVENT[name] for name in WATCHED_EVENT_NAMES]

DEFAULT_STATE_PATH = ROOT / ".local" / "mission3_onchain_tracker_state.json"
DEFAULT_LOG_PATH = ROOT / "logs" / "watch-onchain.log"
DEFAULT_LOCAL_REFRESH_COMMAND = "npm run data && npm run build"
DEFAULT_PUBLISH_REFRESH_COMMAND = "npm run refresh:publish"


class Config(NamedTuple):
    rpc_urls: list[str]
    log_rpc_urls: list[str]
    state_path: Path
    lock_path: Path | None
    log_path: Path | None
    refresh_lock_path: Path | None
    interval_seconds: int
    cooldown_seconds: int
    bid_cooldown_seconds: int
    force_after_seconds: int
    lookback_blocks: int
    safety_overlap_blocks: int
    log_chunk: int
    refresh_command: str
    auto_push: bool
    require_clean_tree: bool
    timeout_seconds: int


class RefreshDecision(NamedTuple):
    should_refresh: bool
    reasons: list[str]
    cooldown_skip: bool = False
    pending_refresh: bool = False
    bypassed_cooldown: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def seconds_since(value: Any, now_utc: str) -> int | None:
    start = parse_utc(value)
    now = parse_utc(now_utc)
    if not start or not now:
        return None
    return max(0, int((now - start).total_seconds()))


def unix_from_utc(value: Any) -> int:
    parsed = parse_utc(value)
    return int(parsed.timestamp()) if parsed else 0


def env_bool(env: dict[str, str], name: str, default: bool = False) -> bool:
    raw = str(env.get(name, "")).strip().lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on"}


def env_int(env: dict[str, str], name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    raw = str(env.get(name, "")).strip()
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc
    value = max(minimum, value)
    if maximum is not None:
        value = min(value, maximum)
    return value


def env_int_any(
    env: dict[str, str],
    names: list[str],
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    for name in names:
        if str(env.get(name, "")).strip():
            return env_int(env, name, default, minimum=minimum, maximum=maximum)
    value = max(minimum, default)
    if maximum is not None:
        value = min(value, maximum)
    return value


def parse_url_list(env: dict[str, str], name: str, default_urls: list[str]) -> list[str]:
    raw = env.get(name, "")
    if not raw:
        return list(default_urls)
    urls = [item.strip() for item in raw.split(",") if item.strip()]
    return urls or list(default_urls)


def default_refresh_lock_path(env: dict[str, str]) -> Path:
    lock_dir_raw = env.get("DEGEN_DOGS_LOCK_DIR", "").strip()
    if lock_dir_raw:
        lock_dir = Path(lock_dir_raw).expanduser()
    else:
        lock_dir = Path.home() / "Library" / "Caches" / "degen-dogs-mission3"
    return lock_dir / "refresh.lock"


def default_log_path(env: dict[str, str]) -> Path:
    log_dir_raw = env.get("DEGEN_DOGS_LOG_DIR", "").strip()
    if log_dir_raw:
        return Path(log_dir_raw).expanduser() / "watch-onchain.log"
    return DEFAULT_LOG_PATH


def optional_path_from_env(env: dict[str, str], name: str, default: Path | None) -> Path | None:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        path = default
    elif raw.strip() == "-":
        return None
    else:
        path = Path(raw.strip()).expanduser()
    if path is None:
        return None
    if not path.is_absolute():
        path = ROOT / path
    return path


def config_from_env(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    if env.get("BASE_RPC_URL"):
        rpc_urls = [env["BASE_RPC_URL"].strip()]
        log_rpc_urls = [env["BASE_RPC_URL"].strip()]
    else:
        rpc_urls = parse_url_list(env, "BASE_RPC_URLS", DEFAULT_RPC_URLS)
        log_rpc_urls = parse_url_list(env, "BASE_LOG_RPC_URLS", DEFAULT_LOG_RPC_URLS)

    auto_push = env_bool(env, "MISSION3_WATCHER_AUTO_PUSH", False)
    refresh_command = env.get("MISSION3_REFRESH_COMMAND", "").strip()
    if not refresh_command:
        refresh_command = DEFAULT_PUBLISH_REFRESH_COMMAND if auto_push else DEFAULT_LOCAL_REFRESH_COMMAND

    state_path = optional_path_from_env(env, "MISSION3_WATCHER_STATE_PATH", DEFAULT_STATE_PATH)
    if state_path is None:
        raise SystemExit("MISSION3_WATCHER_STATE_PATH cannot be disabled")

    lock_path = optional_path_from_env(env, "MISSION3_WATCHER_LOCK_PATH", ROOT / ".local" / "mission3_onchain_tracker.lock")
    log_path = optional_path_from_env(env, "MISSION3_WATCHER_LOG_PATH", default_log_path(env))
    refresh_lock_path = optional_path_from_env(env, "MISSION3_REFRESH_LOCK_PATH", default_refresh_lock_path(env))

    return Config(
        rpc_urls=rpc_urls,
        log_rpc_urls=log_rpc_urls,
        state_path=state_path,
        lock_path=lock_path,
        log_path=log_path,
        refresh_lock_path=refresh_lock_path,
        interval_seconds=env_int(env, "MISSION3_WATCHER_INTERVAL_SECONDS", 60, minimum=30),
        cooldown_seconds=env_int(env, "MISSION3_WATCHER_COOLDOWN_SECONDS", 180, minimum=0),
        bid_cooldown_seconds=env_int(env, "MISSION3_WATCHER_BID_COOLDOWN_SECONDS", 60, minimum=0),
        force_after_seconds=env_int(env, "MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS", 3600, minimum=0),
        lookback_blocks=env_int_any(env, ["MISSION3_WATCHER_LOOKBACK_BLOCKS", "MISSION3_WATCHER_LOG_WINDOW_BLOCKS"], 2000, minimum=1, maximum=10000),
        safety_overlap_blocks=env_int_any(env, ["MISSION3_WATCHER_SAFETY_OVERLAP_BLOCKS", "MISSION3_WATCHER_LOG_SAFETY_OVERLAP_BLOCKS"], 50, minimum=0, maximum=500),
        log_chunk=env_int(env, "MISSION3_WATCHER_LOG_CHUNK", 2000, minimum=1, maximum=10000),
        refresh_command=refresh_command,
        auto_push=auto_push,
        require_clean_tree=env_bool(env, "MISSION3_WATCHER_REQUIRE_CLEAN_TREE", auto_push),
        timeout_seconds=env_int(env, "MISSION3_WATCHER_REFRESH_TIMEOUT_SECONDS", 1800, minimum=60),
    )


def redact_url(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
    except Exception:
        return "<redacted-url>"
    hostname = parts.hostname or ""
    lower_host = hostname.lower()
    sensitive_domains = (
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
    if any(lower_host.endswith(domain) for domain in sensitive_domains):
        domain = next(domain for domain in sensitive_domains if lower_host.endswith(domain))
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


def redact_command(command: str) -> str:
    # Mask common inline secret assignments while preserving enough context for logs.
    pattern = re.compile(r"\b([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Za-z0-9_]*)=([^\s]+)", re.I)
    return pattern.sub(r"\1=<redacted>", command)


def log(config: Config | None, message: str) -> None:
    line = f"[{utc_now()}] {message}"
    print(line)
    if config and config.log_path:
        config.log_path.parent.mkdir(parents=True, exist_ok=True)
        with config.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def post_json(url: str, payload: Any, *, timeout: int = 30) -> Any:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "degen-dogs-mission3-watcher/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc


def rpc_call(method: str, params: list[Any], *, urls: list[str], timeout: int = 30) -> tuple[Any, str]:
    errors: list[str] = []
    for url in urls:
        try:
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            data = post_json(url, payload, timeout=timeout)
            if "error" in data:
                raise RuntimeError(json.dumps(data["error"], sort_keys=True))
            return data.get("result"), url
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{redact_url(url)}: {exc}")
    raise RuntimeError("; ".join(errors))


def word(data: str, idx: int) -> int:
    clean = data[2:] if data.startswith("0x") else data
    return int(clean[idx * 64 : (idx + 1) * 64] or "0", 16)


def word_address(data: str, idx: int) -> str:
    return "0x" + f"{word(data, idx):064x}"[-40:]


def normalize_address(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip().lower()
    return text if text.startswith("0x") and len(text) == 42 else ""


def decode_auction_result(raw: str, *, latest_block: int) -> dict[str, Any]:
    if not raw or raw == "0x":
        raise RuntimeError("auction() returned empty result")
    return {
        "token_id": word(raw, 0),
        "amount_wei": str(word(raw, 1)),
        "start_time_unix": word(raw, 2),
        "end_time_unix": word(raw, 3),
        "high_bidder": normalize_address(word_address(raw, 4)),
        "settled": bool(word(raw, 5)),
        "latest_block": latest_block,
    }


def choose_log_from_block(
    state: dict[str, Any],
    *,
    latest_block: int,
    default_from_block: int,
    lookback_blocks: int,
    safety_overlap_blocks: int,
) -> int:
    if not state:
        return max(default_from_block, latest_block - lookback_blocks + 1)
    try:
        last_checked = int(state.get("last_checked_block") or state.get("last_seen_block") or 0)
    except (TypeError, ValueError):
        last_checked = 0
    if last_checked > 0:
        return max(default_from_block, min(latest_block, last_checked + 1 - safety_overlap_blocks))
    return max(default_from_block, latest_block - lookback_blocks + 1)


def log_filter(address: str, topics: list[str], from_block: int, to_block: int) -> dict[str, Any]:
    return {
        "address": address,
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
        "topics": [topics],
    }


def fetch_logs(config: Config, from_block: int, to_block: int) -> list[dict[str, Any]]:
    if from_block > to_block:
        return []
    logs: list[dict[str, Any]] = []
    start = from_block
    while start <= to_block:
        end = min(to_block, start + config.log_chunk - 1)
        result, _url = rpc_call(
            "eth_getLogs",
            [log_filter(AUCTION_HOUSE, WATCHED_TOPICS, start, end)],
            urls=config.log_rpc_urls,
            timeout=45,
        )
        if not isinstance(result, list):
            raise RuntimeError(f"unexpected eth_getLogs response: {result!r}")
        logs.extend(result)
        start = end + 1
    logs.sort(key=lambda item: (int(str(item.get("blockNumber", "0x0")), 16), int(str(item.get("logIndex", "0x0")), 16)))
    return logs


def log_identity(item: dict[str, Any] | None) -> str:
    if not item:
        return ""
    block = int(str(item.get("blockNumber", "0x0")), 16)
    tx_hash = str(item.get("transactionHash") or "")
    log_index = int(str(item.get("logIndex", "0x0")), 16)
    return f"{block}:{tx_hash}:{log_index}"


def topic_uint(item: dict[str, Any], idx: int) -> int | None:
    topics = item.get("topics") or []
    if len(topics) <= idx:
        return None
    try:
        return int(str(topics[idx]), 16)
    except (TypeError, ValueError):
        return None


def safe_data_word(item: dict[str, Any], idx: int) -> int | None:
    try:
        return word(str(item.get("data") or "0x"), idx)
    except (TypeError, ValueError):
        return None


def safe_data_address(item: dict[str, Any], idx: int) -> str:
    try:
        return normalize_address(word_address(str(item.get("data") or "0x"), idx))
    except (TypeError, ValueError):
        return ""


def compact_event_log(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    topic0 = str((item.get("topics") or [""])[0]).lower()
    event_name = TOPIC_EVENT_NAMES.get(topic0, "unknown")
    result: dict[str, Any] = {
        "id": log_identity(item),
        "block_number": int(str(item.get("blockNumber", "0x0")), 16),
        "tx_hash": str(item.get("transactionHash") or ""),
        "log_index": int(str(item.get("logIndex", "0x0")), 16),
        "topic0": topic0,
        "event_name": event_name,
    }
    token_id = topic_uint(item, 1)
    if token_id is not None:
        result["token_id"] = token_id
    if event_name == "AuctionCreated":
        start_time = safe_data_word(item, 0)
        end_time = safe_data_word(item, 1)
        if start_time is not None:
            result["start_time_unix"] = start_time
        if end_time is not None:
            result["end_time_unix"] = end_time
    elif event_name == "AuctionBid":
        bidder = safe_data_address(item, 0)
        amount = safe_data_word(item, 1)
        extended = safe_data_word(item, 2)
        if bidder:
            result["bidder"] = bidder
        if amount is not None:
            result["amount_wei"] = str(amount)
        if extended is not None:
            result["extended"] = bool(extended)
    elif event_name == "AuctionExtended":
        end_time = safe_data_word(item, 0)
        if end_time is not None:
            result["end_time_unix"] = end_time
    elif event_name == "AuctionSettled":
        winner = safe_data_address(item, 0)
        amount = safe_data_word(item, 1)
        if winner:
            result["winner"] = winner
        if amount is not None:
            result["amount_wei"] = str(amount)
    return result


def latest_log_for_topic(logs: list[dict[str, Any]], topic: str) -> dict[str, Any] | None:
    topic_lc = topic.lower()
    matches = [item for item in logs if (item.get("topics") or [""])[0].lower() == topic_lc]
    return compact_event_log(matches[-1]) if matches else None


def fetch_snapshot(config: Config, state: dict[str, Any]) -> dict[str, Any]:
    block_hex, block_url = rpc_call("eth_blockNumber", [], urls=config.rpc_urls, timeout=30)
    latest_block = int(block_hex, 16)
    raw, call_url = rpc_call(
        "eth_call",
        [{"to": AUCTION_HOUSE, "data": SELECTOR_AUCTION}, hex(latest_block)],
        urls=config.rpc_urls,
        timeout=30,
    )
    auction = decode_auction_result(str(raw), latest_block=latest_block)
    default_from_block = env_int(dict(os.environ), "BASE_FROM_BLOCK", 40500000, minimum=0)
    from_block = choose_log_from_block(
        state,
        latest_block=latest_block,
        default_from_block=default_from_block,
        lookback_blocks=config.lookback_blocks,
        safety_overlap_blocks=config.safety_overlap_blocks,
    )
    logs = fetch_logs(config, from_block, latest_block)
    snapshot = {
        **auction,
        "checked_from_block": from_block,
        "checked_to_block": latest_block,
        "checked_log_count": len(logs),
        "created_log": latest_log_for_topic(logs, TOPIC_AUCTION_CREATED),
        "bid_log": latest_log_for_topic(logs, TOPIC_AUCTION_BID),
        "extended_log": latest_log_for_topic(logs, TOPIC_AUCTION_EXTENDED),
        "settled_log": latest_log_for_topic(logs, TOPIC_AUCTION_SETTLED),
        "rpc_url": redact_url(call_url),
        "block_rpc_url": redact_url(block_url),
    }
    return snapshot


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid watcher state JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"invalid watcher state at {path}: expected object")
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


class RefreshAlreadyRunning(RuntimeError):
    pass


def acquire_file_lock(path: Path, *, label: str) -> Any | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"kind={label}\npid={os.getpid()}\nstarted_at_utc={utc_now()}\n")
    handle.flush()
    return handle


def acquire_run_lock(config: Config) -> Any | None:
    if not config.lock_path:
        return None
    return acquire_file_lock(config.lock_path, label="watcher")


def release_run_lock(handle: Any | None) -> None:
    if not handle:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _same_log_id(snapshot: dict[str, Any], state: dict[str, Any], snapshot_key: str, state_key: str) -> bool:
    item = snapshot.get(snapshot_key) or {}
    item_id = item.get("id") if isinstance(item, dict) else ""
    return not item_id or item_id == state.get(state_key, "")


def pending_backoff_active(state: dict[str, Any], now_utc: str) -> bool:
    next_allowed = parse_utc(state.get("next_allowed_refresh_after_utc"))
    now = parse_utc(now_utc)
    return bool(next_allowed and now and now < next_allowed)


BID_REFRESH_REASONS = {"auction_bid", "highest_bidder_changed", "highest_bid_amount_changed"}
MAJOR_REFRESH_REASONS = {"auction_created", "auction_settled", "auction_settled_state_changed", "current_auction_token_changed"}


def cooldown_for_reasons(reasons: list[str], *, cooldown_seconds: int, bid_cooldown_seconds: int | None = None) -> int:
    if bid_cooldown_seconds is None:
        bid_cooldown_seconds = cooldown_seconds
    reason_set = set(reasons)
    if reason_set and reason_set <= BID_REFRESH_REASONS:
        return max(0, bid_cooldown_seconds)
    if reason_set & BID_REFRESH_REASONS:
        return max(0, min(cooldown_seconds, bid_cooldown_seconds))
    return max(0, cooldown_seconds)


def _state_reasons(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def decide_refresh(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    now_utc: str,
    cooldown_seconds: int,
    force_after_seconds: int,
    bid_cooldown_seconds: int | None = None,
) -> RefreshDecision:
    if not state or state.get("last_seen_token_id") in {None, ""}:
        return RefreshDecision(False, ["initialize_state"])

    reasons: list[str] = []
    if not _same_log_id(snapshot, state, "created_log", "last_seen_auction_created_log_id"):
        reasons.append("auction_created")
    if not _same_log_id(snapshot, state, "bid_log", "last_seen_bid_log_id"):
        reasons.append("auction_bid")
    if not _same_log_id(snapshot, state, "extended_log", "last_seen_auction_extended_log_id"):
        reasons.append("auction_extended")
    if not _same_log_id(snapshot, state, "settled_log", "last_seen_auction_settled_log_id"):
        reasons.append("auction_settled")

    if int(snapshot.get("token_id") or 0) != int(state.get("last_seen_token_id") or 0):
        reasons.append("current_auction_token_changed")
    if normalize_address(snapshot.get("high_bidder")) != normalize_address(state.get("last_seen_high_bidder")):
        reasons.append("highest_bidder_changed")
    if str(snapshot.get("amount_wei") or "") != str(state.get("last_seen_amount_wei") or ""):
        reasons.append("highest_bid_amount_changed")
    if bool(snapshot.get("settled")) != bool(state.get("last_seen_settled")):
        reasons.append("auction_settled_state_changed")
    if int(snapshot.get("end_time_unix") or 0) != int(state.get("last_seen_end_time_unix") or 0):
        reasons.append("auction_end_time_changed")

    if state.get("pending_refresh") and not reasons:
        pending_age = seconds_since(state.get("pending_refresh_since_utc"), now_utc)
        last_refresh_age = seconds_since(state.get("last_refresh_at_utc"), now_utc)
        pending_reasons = _state_reasons(state.get("pending_refresh_reasons")) or ["pending_refresh_after_cooldown"]
        pending_cooldown = cooldown_for_reasons(pending_reasons, cooldown_seconds=cooldown_seconds, bid_cooldown_seconds=bid_cooldown_seconds)
        if not pending_backoff_active(state, now_utc) and (
            last_refresh_age is None or last_refresh_age >= pending_cooldown or pending_age is None or pending_age >= pending_cooldown
        ):
            return RefreshDecision(True, ["pending_refresh_after_cooldown"])

    if not reasons and force_after_seconds > 0:
        last_refresh_age = seconds_since(state.get("last_refresh_at_utc"), now_utc)
        if last_refresh_age is None or last_refresh_age >= force_after_seconds:
            reasons.append("force_refresh_after_interval")

    if not reasons:
        return RefreshDecision(False, [])

    if pending_backoff_active(state, now_utc):
        return RefreshDecision(False, reasons, cooldown_skip=True, pending_refresh=True)

    bypassed = any(reason in MAJOR_REFRESH_REASONS for reason in reasons)
    last_refresh_age = seconds_since(state.get("last_refresh_at_utc"), now_utc)
    active_cooldown = cooldown_for_reasons(reasons, cooldown_seconds=cooldown_seconds, bid_cooldown_seconds=bid_cooldown_seconds)
    if active_cooldown > 0 and last_refresh_age is not None and last_refresh_age < active_cooldown and not bypassed:
        return RefreshDecision(False, reasons, cooldown_skip=True, pending_refresh=True)

    return RefreshDecision(True, reasons, bypassed_cooldown=bypassed and last_refresh_age is not None and last_refresh_age < active_cooldown)


def get_snapshot_log(snapshot: dict[str, Any], key: str) -> dict[str, Any]:
    value = snapshot.get(key)
    return value if isinstance(value, dict) else {}


def latest_activity_block(snapshot: dict[str, Any]) -> int:
    blocks = []
    for key in ("created_log", "bid_log", "extended_log", "settled_log"):
        item = get_snapshot_log(snapshot, key)
        if item.get("block_number") is not None:
            try:
                blocks.append(int(item["block_number"]))
            except (TypeError, ValueError):
                pass
    if blocks:
        return max(blocks)
    return int(snapshot.get("latest_block") or snapshot.get("checked_to_block") or 0)


def state_from_snapshot(
    snapshot: dict[str, Any],
    *,
    now_utc: str,
    previous_state: dict[str, Any],
    decision: RefreshDecision | None = None,
) -> dict[str, Any]:
    state = dict(previous_state)
    created_log = get_snapshot_log(snapshot, "created_log")
    bid_log = get_snapshot_log(snapshot, "bid_log")
    extended_log = get_snapshot_log(snapshot, "extended_log")
    settled_log = get_snapshot_log(snapshot, "settled_log")
    checked_to_block = int(snapshot.get("checked_to_block") or snapshot.get("latest_block") or 0)
    state.update(
        {
            "schema_version": SCHEMA_VERSION,
            "updated_at_utc": now_utc,
            "chain_id": CHAIN_ID,
            "auction_house": AUCTION_HOUSE,
            "last_checked_at_utc": now_utc,
            "last_checked_block": checked_to_block,
            "last_seen_block": latest_activity_block(snapshot),
            "last_checked_from_block": int(snapshot.get("checked_from_block") or 0),
            "last_seen_token_id": int(snapshot.get("token_id") or 0),
            "last_seen_high_bidder": normalize_address(snapshot.get("high_bidder")),
            "last_seen_bidder": normalize_address(bid_log.get("bidder")) or normalize_address(snapshot.get("high_bidder")),
            "last_seen_amount_wei": str(snapshot.get("amount_wei") or bid_log.get("amount_wei") or "0"),
            "last_seen_settled": bool(snapshot.get("settled")),
            "last_seen_start_time_unix": int(snapshot.get("start_time_unix") or 0),
            "last_seen_end_time_unix": int(snapshot.get("end_time_unix") or extended_log.get("end_time_unix") or 0),
            "last_seen_auction_created_log_id": created_log.get("id", "") or state.get("last_seen_auction_created_log_id", ""),
            "last_seen_auction_created_tx": created_log.get("tx_hash", "") or state.get("last_seen_auction_created_tx", ""),
            "last_seen_created_tx": created_log.get("tx_hash", "") or state.get("last_seen_created_tx", ""),
            "last_seen_auction_settled_log_id": settled_log.get("id", "") or state.get("last_seen_auction_settled_log_id", ""),
            "last_seen_auction_settled_tx": settled_log.get("tx_hash", "") or state.get("last_seen_auction_settled_tx", ""),
            "last_seen_settled_tx": settled_log.get("tx_hash", "") or state.get("last_seen_settled_tx", ""),
            "last_seen_auction_extended_log_id": extended_log.get("id", "") or state.get("last_seen_auction_extended_log_id", ""),
            "last_seen_auction_extended_tx": extended_log.get("tx_hash", "") or state.get("last_seen_auction_extended_tx", ""),
            "last_seen_extended_tx": extended_log.get("tx_hash", "") or state.get("last_seen_extended_tx", ""),
            "last_seen_bid_log_id": bid_log.get("id", "") or state.get("last_seen_bid_log_id", ""),
            "last_seen_bid_tx": bid_log.get("tx_hash", "") or state.get("last_seen_bid_tx", ""),
            "last_seen_bid_log_index": int(bid_log.get("log_index") or state.get("last_seen_bid_log_index") or 0),
            "last_seen_bid_token_id": int(bid_log.get("token_id") or snapshot.get("token_id") or state.get("last_seen_bid_token_id") or 0),
            "last_rpc_url": snapshot.get("rpc_url", ""),
            "last_log_count": int(snapshot.get("checked_log_count") or 0),
            "last_error": None,
        }
    )
    if decision and decision.pending_refresh:
        state["pending_refresh"] = True
        state.setdefault("pending_refresh_since_utc", now_utc)
        state["pending_refresh_reasons"] = decision.reasons
    return state


def wei_from_eth_text(value: Any) -> str:
    try:
        eth = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return "0"
    if eth < 0:
        return "0"
    return str(int(eth * Decimal(10**18)))


def state_from_generated_dashboard(snapshot: dict[str, Any], *, now_utc: str, root: Path = ROOT) -> dict[str, Any]:
    """Build an initial watcher baseline from the committed dashboard snapshot.

    This lets a newly installed watcher notice if the cached dashboard is already
    stale without treating old recent logs as new activity.
    """
    path = root / "generated" / "current_auction.csv"
    if not path.exists():
        return {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return {}
    if not rows:
        return {}
    row = rows[0]
    created_log = snapshot.get("created_log") if isinstance(snapshot.get("created_log"), dict) else {}
    settled_log = snapshot.get("settled_log") if isinstance(snapshot.get("settled_log"), dict) else {}
    extended_log = snapshot.get("extended_log") if isinstance(snapshot.get("extended_log"), dict) else {}
    bid_log = snapshot.get("bid_log") if isinstance(snapshot.get("bid_log"), dict) else {}
    block_time = parse_utc(row.get("latest_block_time_utc") or now_utc)
    checked_block = int(row.get("latest_block") or snapshot.get("checked_to_block") or snapshot.get("latest_block") or 0)
    bid_tx = bid_log.get("tx_hash", "") if isinstance(bid_log, dict) else ""
    settled_tx = settled_log.get("tx_hash", "") if isinstance(settled_log, dict) else ""
    created_tx = created_log.get("tx_hash", "") if isinstance(created_log, dict) else ""
    extended_tx = extended_log.get("tx_hash", "") if isinstance(extended_log, dict) else ""
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at_utc": now_utc,
        "chain_id": CHAIN_ID,
        "auction_house": AUCTION_HOUSE,
        "last_checked_at_utc": now_utc,
        "last_checked_block": checked_block,
        "last_seen_block": checked_block,
        "last_checked_from_block": int(snapshot.get("checked_from_block") or 0),
        "last_seen_token_id": int(row.get("token_id") or 0),
        "last_seen_high_bidder": normalize_address(row.get("bidder_wallet")),
        "last_seen_bidder": normalize_address((bid_log.get("bidder") if isinstance(bid_log, dict) else "") or row.get("bidder_wallet")),
        "last_seen_amount_wei": wei_from_eth_text(row.get("current_bid_eth") or 0),
        "last_seen_settled": str(row.get("settled") or "").strip().lower() in {"1", "true", "yes"},
        "last_seen_start_time_unix": unix_from_utc(row.get("start_time_utc")),
        "last_seen_end_time_unix": unix_from_utc(row.get("end_time_utc")),
        "last_refresh_at_utc": (block_time or parse_utc(now_utc) or datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "last_refresh_reason": "generated_dashboard_baseline",
        "last_refresh_status": "success",
        "last_seen_auction_created_log_id": created_log.get("id", "") if isinstance(created_log, dict) else "",
        "last_seen_auction_created_tx": created_tx,
        "last_seen_created_tx": created_tx,
        "last_seen_auction_settled_log_id": settled_log.get("id", "") if isinstance(settled_log, dict) else "",
        "last_seen_auction_settled_tx": settled_tx,
        "last_seen_settled_tx": settled_tx,
        "last_seen_auction_extended_log_id": extended_log.get("id", "") if isinstance(extended_log, dict) else "",
        "last_seen_auction_extended_tx": extended_tx,
        "last_seen_extended_tx": extended_tx,
        "last_seen_bid_log_id": bid_log.get("id", "") if isinstance(bid_log, dict) else "",
        "last_seen_bid_tx": bid_tx,
        "last_seen_bid_log_index": int(bid_log.get("log_index") or 0) if isinstance(bid_log, dict) else 0,
        "last_seen_bid_token_id": int(bid_log.get("token_id") or row.get("token_id") or 0) if isinstance(bid_log, dict) else int(row.get("token_id") or 0),
        "last_error": None,
    }


def record_rpc_error(path: Path, state: dict[str, Any], error: Exception, now_utc: str) -> None:
    state = dict(state)
    failures = int(state.get("consecutive_rpc_failures") or 0) + 1
    state.update(
        {
            "schema_version": SCHEMA_VERSION,
            "updated_at_utc": now_utc,
            "last_checked_at_utc": now_utc,
            "last_error_at_utc": now_utc,
            "last_error": str(error)[:500],
            "consecutive_rpc_failures": failures,
        }
    )
    save_state(path, state)


def record_refresh_result(state: dict[str, Any], *, status: str, reasons: list[str], now_utc: str, exit_code: int = 0) -> dict[str, Any]:
    state = dict(state)
    state["last_refresh_at_utc"] = now_utc
    state["last_refresh_reason"] = ",".join(reasons)
    state["last_refresh_status"] = status
    state["last_refresh_exit_code"] = exit_code
    if status == "success":
        state["consecutive_refresh_failures"] = 0
        state.pop("next_allowed_refresh_after_utc", None)
        state.pop("pending_refresh", None)
        state.pop("pending_refresh_since_utc", None)
        state.pop("pending_refresh_reasons", None)
    else:
        failures = int(state.get("consecutive_refresh_failures") or 0) + 1
        state["consecutive_refresh_failures"] = failures
        delay = min(3600, max(300, 300 * (2 ** (failures - 1))))
        next_allowed = datetime.now(timezone.utc).replace(microsecond=0).timestamp() + delay
        state["next_allowed_refresh_after_utc"] = datetime.fromtimestamp(next_allowed, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        state["pending_refresh"] = True
        state.setdefault("pending_refresh_since_utc", now_utc)
        state["pending_refresh_reasons"] = reasons
    return state


def command_implies_push(command: str) -> bool:
    lowered = command.lower()
    publish_markers = [
        "git push",
        "refresh:publish",
        "refresh:archive",
        "refresh_and_publish",
        "refresh_archive_and_publish",
    ]
    return any(marker in lowered for marker in publish_markers)


def validate_refresh_command(config: Config) -> None:
    if command_implies_push(config.refresh_command) and not config.auto_push:
        raise SystemExit("refresh command appears to auto-push; set MISSION3_WATCHER_AUTO_PUSH=1 to allow auto-push")


def git_status_tracked() -> str:
    return subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True)


def acquire_refresh_lock(config: Config) -> Any | None:
    if not config.refresh_lock_path:
        return None
    return acquire_file_lock(config.refresh_lock_path, label="refresh")


def mark_pending_refresh(state: dict[str, Any], *, reasons: list[str], now_utc: str, status: str) -> dict[str, Any]:
    state = dict(state)
    state["last_refresh_status"] = status
    state["pending_refresh"] = True
    state.setdefault("pending_refresh_since_utc", now_utc)
    state["pending_refresh_reasons"] = reasons
    return state


def run_refresh(config: Config, reasons: list[str], *, dry_run: bool) -> tuple[str, int]:
    validate_refresh_command(config)
    if config.require_clean_tree:
        tracked = git_status_tracked().strip()
        if tracked:
            raise RuntimeError("tracked working tree changes exist; refusing guarded refresh:\n" + tracked)
    else:
        tracked = git_status_tracked().strip()
        if tracked:
            log(config, "warning: tracked working tree changes exist before local refresh")

    command_for_log = redact_command(config.refresh_command)
    if dry_run:
        log(config, f"dry-run: would run refresh command: {command_for_log}; reasons={','.join(reasons)}")
        return "dry_run", 0

    refresh_lock = acquire_refresh_lock(config)
    if config.refresh_lock_path and refresh_lock is None:
        raise RefreshAlreadyRunning(f"another refresh is already running at {config.refresh_lock_path}")

    try:
        child_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if refresh_lock and config.refresh_lock_path:
            # The parent watcher holds the same lock used by refresh_and_publish.sh.
            # Passing DEGEN_DOGS_LOCK_HELD lets that script run without trying to
            # reacquire the lock it already owns through this process.
            child_env["DEGEN_DOGS_LOCK_HELD"] = "1"
            child_env["DEGEN_DOGS_LOCK_DIR"] = str(config.refresh_lock_path.parent)
        log(config, f"running refresh command: {command_for_log}; reasons={','.join(reasons)}")
        result = subprocess.run(
            ["/bin/bash", "-lc", config.refresh_command],
            cwd=ROOT,
            text=True,
            timeout=config.timeout_seconds,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    finally:
        release_run_lock(refresh_lock)
    stdout_tail = "\n".join((result.stdout or "").splitlines()[-20:])
    stderr_tail = "\n".join((result.stderr or "").splitlines()[-20:])
    if stdout_tail:
        log(config, "refresh_stdout_tail: " + redact_command(stdout_tail))
    if stderr_tail:
        log(config, "refresh_stderr_tail: " + redact_command(stderr_tail))
    if result.returncode != 0:
        return "failure", result.returncode
    return "success", 0


def run_once_locked(config: Config, *, dry_run: bool = False, force_refresh: bool = False) -> int:
    now = utc_now()
    state = load_state(config.state_path)
    try:
        snapshot = fetch_snapshot(config, state)
    except Exception as exc:  # noqa: BLE001
        if not dry_run:
            record_rpc_error(config.state_path, state, exc, now)
        log(config, f"rpc_error: {exc}")
        return 1

    if not state or state.get("last_seen_token_id") in {None, ""}:
        state = state_from_generated_dashboard(snapshot, now_utc=now) or state

    decision = decide_refresh(
        state,
        snapshot,
        now_utc=now,
        cooldown_seconds=config.cooldown_seconds,
        force_after_seconds=config.force_after_seconds,
        bid_cooldown_seconds=config.bid_cooldown_seconds,
    )
    if force_refresh and not decision.should_refresh:
        decision = RefreshDecision(True, ["force_refresh"])
    new_state = state_from_snapshot(snapshot, now_utc=now, previous_state=state, decision=decision)
    new_state["consecutive_rpc_failures"] = 0
    summary = (
        f"block={snapshot.get('latest_block')} token={snapshot.get('token_id')} "
        f"bidder={snapshot.get('high_bidder')} amount_wei={snapshot.get('amount_wei')} "
        f"logs={snapshot.get('checked_log_count')} reasons={','.join(decision.reasons) or 'none'}"
    )

    if decision.should_refresh:
        try:
            status, exit_code = run_refresh(config, decision.reasons, dry_run=dry_run)
        except RefreshAlreadyRunning as exc:
            new_state = mark_pending_refresh(new_state, reasons=decision.reasons, now_utc=utc_now(), status="deferred_refresh_lock")
            if not dry_run:
                save_state(config.state_path, new_state)
            log(config, f"refresh_lock_skip pending=1: {exc}; {summary}")
            return 0
        except Exception as exc:  # noqa: BLE001
            new_state = record_refresh_result(new_state, status="failure", reasons=decision.reasons, now_utc=utc_now(), exit_code=1)
            new_state["last_refresh_error"] = str(exc)[:500]
            if not dry_run:
                save_state(config.state_path, new_state)
            log(config, f"refresh_error: {exc}; {summary}")
            return 2
        new_state = record_refresh_result(new_state, status=status, reasons=decision.reasons, now_utc=utc_now(), exit_code=exit_code)
        if not dry_run:
            save_state(config.state_path, new_state)
        if status == "failure":
            log(config, f"refresh_failed exit_code={exit_code}; {summary}")
            return 2
        log(config, f"refresh_{status}; {summary}")
        return 0

    if not dry_run:
        save_state(config.state_path, new_state)
    if decision.cooldown_skip:
        log(config, f"cooldown_skip pending=1; {summary}")
    else:
        log(config, f"no_refresh; {summary}")
    return 0


def run_once(config: Config, *, dry_run: bool = False, force_refresh: bool = False) -> int:
    lock_handle = acquire_run_lock(config)
    if config.lock_path and lock_handle is None:
        log(config, f"lock_skip: another watcher run is active at {config.lock_path}")
        return 0
    try:
        return run_once_locked(config, dry_run=dry_run, force_refresh=force_refresh)
    finally:
        release_run_lock(lock_handle)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch Mission 3 auction state and trigger local dashboard refreshes on meaningful changes.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run one check then exit (default).")
    mode.add_argument("--loop", action="store_true", help="Run continuously, sleeping between checks.")
    parser.add_argument("--dry-run", action="store_true", help="Detect changes and log the intended refresh without running it or writing watcher state.")
    parser.add_argument("--force-refresh", action="store_true", help="Run the configured refresh command even if this check only initializes or sees no new signal.")
    parser.add_argument("--state-path", help="Override MISSION3_WATCHER_STATE_PATH for this run.")
    parser.add_argument("--refresh-command", help="Override MISSION3_REFRESH_COMMAND for this run.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = dict(os.environ)
    if args.state_path:
        env["MISSION3_WATCHER_STATE_PATH"] = args.state_path
    if args.refresh_command:
        env["MISSION3_REFRESH_COMMAND"] = args.refresh_command
    config = config_from_env(env)
    validate_refresh_command(config)

    if not args.loop:
        return run_once(config, dry_run=args.dry_run, force_refresh=args.force_refresh)

    while True:
        run_once(config, dry_run=args.dry_run, force_refresh=args.force_refresh)
        time.sleep(config.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
