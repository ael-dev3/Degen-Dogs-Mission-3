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
DEFAULT_RPC_URLS = [
    "https://base-rpc.publicnode.com",
    "https://mainnet.base.org",
    "https://developer-access-mainnet.base.org",
]
DEFAULT_LOG_RPC_URLS = ["https://mainnet.base.org"]

AUCTION_HOUSE = "0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA"
SELECTOR_AUCTION = "0x7d9f6db5"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

TOPIC_AUCTION_CREATED = "0xd6eddd1118d71820909c1197aa966dbc15ed6f508554252169cc3d5ccac756ca"
TOPIC_AUCTION_BID = "0x1159164c56f277e6fc99c11731bd380e0347deb969b75523398734c252706ea3"
TOPIC_AUCTION_SETTLED = "0xc9f72b276a388619c6d185d146697036241880c36654b1a3ffdad07c24038d99"
WATCHED_TOPICS = [TOPIC_AUCTION_CREATED, TOPIC_AUCTION_BID, TOPIC_AUCTION_SETTLED]

DEFAULT_STATE_PATH = ROOT / ".local" / "mission3_watcher_state.json"
DEFAULT_LOG_PATH = ROOT / "logs" / "watch-auction.log"
DEFAULT_LOCAL_REFRESH_COMMAND = "npm run refresh:local"
DEFAULT_PUBLISH_REFRESH_COMMAND = "npm run refresh:publish"
SCHEMA_VERSION = 1


class Config(NamedTuple):
    rpc_urls: list[str]
    log_rpc_urls: list[str]
    state_path: Path
    lock_path: Path | None
    log_path: Path | None
    interval_seconds: int
    cooldown_seconds: int
    force_after_seconds: int
    log_window_blocks: int
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


def parse_url_list(env: dict[str, str], name: str, default_urls: list[str]) -> list[str]:
    raw = env.get(name, "")
    if not raw:
        return list(default_urls)
    urls = [item.strip() for item in raw.split(",") if item.strip()]
    return urls or list(default_urls)


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

    state_path = Path(env.get("MISSION3_WATCHER_STATE_PATH", str(DEFAULT_STATE_PATH))).expanduser()
    if not state_path.is_absolute():
        state_path = ROOT / state_path

    lock_path_raw = env.get("MISSION3_WATCHER_LOCK_PATH", str(ROOT / ".local" / "mission3_watcher.lock")).strip()
    lock_path: Path | None
    if lock_path_raw in {"", "-"}:
        lock_path = None
    else:
        lock_path = Path(lock_path_raw).expanduser()
        if not lock_path.is_absolute():
            lock_path = ROOT / lock_path

    log_path_raw = env.get("MISSION3_WATCHER_LOG_PATH", str(DEFAULT_LOG_PATH)).strip()
    log_path: Path | None
    if log_path_raw in {"", "-"}:
        log_path = None
    else:
        log_path = Path(log_path_raw).expanduser()
        if not log_path.is_absolute():
            log_path = ROOT / log_path

    return Config(
        rpc_urls=rpc_urls,
        log_rpc_urls=log_rpc_urls,
        state_path=state_path,
        lock_path=lock_path,
        log_path=log_path,
        interval_seconds=env_int(env, "MISSION3_WATCHER_INTERVAL_SECONDS", 300, minimum=30),
        cooldown_seconds=env_int(env, "MISSION3_WATCHER_COOLDOWN_SECONDS", 300, minimum=0),
        force_after_seconds=env_int(env, "MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS", 0, minimum=0),
        log_window_blocks=env_int(env, "MISSION3_WATCHER_LOG_WINDOW_BLOCKS", 2000, minimum=1, maximum=10000),
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
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    if parts.username or parts.password:
        netloc = "***@" + netloc
    query = "redacted=1" if parts.query else ""
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, query, ""))


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


def choose_log_from_block(state: dict[str, Any], *, latest_block: int, default_from_block: int, window_blocks: int) -> int:
    safety_start = max(default_from_block, latest_block - window_blocks + 1)
    try:
        last_seen = int(state.get("last_seen_block") or 0)
    except (TypeError, ValueError):
        last_seen = 0
    if last_seen > 0 and last_seen >= safety_start:
        return min(latest_block, last_seen + 1)
    return safety_start


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


def compact_event_log(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    return {
        "id": log_identity(item),
        "block_number": int(str(item.get("blockNumber", "0x0")), 16),
        "tx_hash": str(item.get("transactionHash") or ""),
        "log_index": int(str(item.get("logIndex", "0x0")), 16),
        "topic0": str((item.get("topics") or [""])[0]).lower(),
    }


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
        window_blocks=config.log_window_blocks,
    )
    logs = fetch_logs(config, from_block, latest_block)
    snapshot = {
        **auction,
        "checked_from_block": from_block,
        "checked_to_block": latest_block,
        "checked_log_count": len(logs),
        "created_log": latest_log_for_topic(logs, TOPIC_AUCTION_CREATED),
        "bid_log": latest_log_for_topic(logs, TOPIC_AUCTION_BID),
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


def acquire_run_lock(config: Config) -> Any | None:
    if not config.lock_path:
        return None
    config.lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = config.lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\nstarted_at_utc={utc_now()}\n")
    handle.flush()
    return handle


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


def decide_refresh(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    now_utc: str,
    cooldown_seconds: int,
    force_after_seconds: int,
) -> RefreshDecision:
    if not state or state.get("last_seen_token_id") in {None, ""}:
        return RefreshDecision(False, ["initialize_state"])

    reasons: list[str] = []
    if not _same_log_id(snapshot, state, "created_log", "last_seen_auction_created_log_id"):
        reasons.append("auction_created")
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

    if state.get("pending_refresh") and not reasons:
        pending_age = seconds_since(state.get("pending_refresh_since_utc"), now_utc)
        last_refresh_age = seconds_since(state.get("last_refresh_at_utc"), now_utc)
        if not pending_backoff_active(state, now_utc) and (
            last_refresh_age is None or last_refresh_age >= cooldown_seconds or pending_age is None or pending_age >= cooldown_seconds
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

    major_reasons = {"auction_created", "auction_settled", "auction_settled_state_changed", "current_auction_token_changed"}
    bypassed = any(reason in major_reasons for reason in reasons)
    last_refresh_age = seconds_since(state.get("last_refresh_at_utc"), now_utc)
    if cooldown_seconds > 0 and last_refresh_age is not None and last_refresh_age < cooldown_seconds and not bypassed:
        return RefreshDecision(False, reasons, cooldown_skip=True, pending_refresh=True)

    return RefreshDecision(True, reasons, bypassed_cooldown=bypassed and last_refresh_age is not None and last_refresh_age < cooldown_seconds)


def state_from_snapshot(
    snapshot: dict[str, Any],
    *,
    now_utc: str,
    previous_state: dict[str, Any],
    decision: RefreshDecision | None = None,
) -> dict[str, Any]:
    state = dict(previous_state)
    state.update(
        {
            "schema_version": SCHEMA_VERSION,
            "updated_at_utc": now_utc,
            "last_checked_at_utc": now_utc,
            "last_seen_block": int(snapshot.get("latest_block") or 0),
            "last_checked_from_block": int(snapshot.get("checked_from_block") or 0),
            "last_seen_token_id": int(snapshot.get("token_id") or 0),
            "last_seen_high_bidder": normalize_address(snapshot.get("high_bidder")),
            "last_seen_amount_wei": str(snapshot.get("amount_wei") or "0"),
            "last_seen_settled": bool(snapshot.get("settled")),
            "last_seen_start_time_unix": int(snapshot.get("start_time_unix") or 0),
            "last_seen_end_time_unix": int(snapshot.get("end_time_unix") or 0),
            "last_seen_auction_created_log_id": ((snapshot.get("created_log") or {}).get("id") if isinstance(snapshot.get("created_log"), dict) else "") or state.get("last_seen_auction_created_log_id", ""),
            "last_seen_auction_created_tx": ((snapshot.get("created_log") or {}).get("tx_hash") if isinstance(snapshot.get("created_log"), dict) else "") or state.get("last_seen_auction_created_tx", ""),
            "last_seen_auction_settled_log_id": ((snapshot.get("settled_log") or {}).get("id") if isinstance(snapshot.get("settled_log"), dict) else "") or state.get("last_seen_auction_settled_log_id", ""),
            "last_seen_auction_settled_tx": ((snapshot.get("settled_log") or {}).get("tx_hash") if isinstance(snapshot.get("settled_log"), dict) else "") or state.get("last_seen_auction_settled_tx", ""),
            "last_seen_bid_log_id": ((snapshot.get("bid_log") or {}).get("id") if isinstance(snapshot.get("bid_log"), dict) else "") or state.get("last_seen_bid_log_id", ""),
            "last_seen_bid_tx": ((snapshot.get("bid_log") or {}).get("tx_hash") if isinstance(snapshot.get("bid_log"), dict) else "") or state.get("last_seen_bid_tx", ""),
            "last_rpc_url": snapshot.get("rpc_url", ""),
            "last_log_count": int(snapshot.get("checked_log_count") or 0),
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
    bid_log = snapshot.get("bid_log") if isinstance(snapshot.get("bid_log"), dict) else {}
    block_time = parse_utc(row.get("latest_block_time_utc") or now_utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at_utc": now_utc,
        "last_checked_at_utc": now_utc,
        "last_seen_block": int(row.get("latest_block") or 0),
        "last_seen_token_id": int(row.get("token_id") or 0),
        "last_seen_high_bidder": normalize_address(row.get("bidder_wallet")),
        "last_seen_amount_wei": wei_from_eth_text(row.get("current_bid_eth") or 0),
        "last_seen_settled": str(row.get("settled") or "").strip().lower() in {"1", "true", "yes"},
        "last_refresh_at_utc": (block_time or parse_utc(now_utc) or datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "last_refresh_reason": "generated_dashboard_baseline",
        "last_refresh_status": "success",
        "last_seen_auction_created_log_id": created_log.get("id", "") if isinstance(created_log, dict) else "",
        "last_seen_auction_created_tx": created_log.get("tx_hash", "") if isinstance(created_log, dict) else "",
        "last_seen_auction_settled_log_id": settled_log.get("id", "") if isinstance(settled_log, dict) else "",
        "last_seen_auction_settled_tx": settled_log.get("tx_hash", "") if isinstance(settled_log, dict) else "",
        "last_seen_bid_log_id": bid_log.get("id", "") if isinstance(bid_log, dict) else "",
        "last_seen_bid_tx": bid_log.get("tx_hash", "") if isinstance(bid_log, dict) else "",
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

    log(config, f"running refresh command: {command_for_log}; reasons={','.join(reasons)}")
    result = subprocess.run(
        ["/bin/bash", "-lc", config.refresh_command],
        cwd=ROOT,
        text=True,
        timeout=config.timeout_seconds,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        return "failure", result.returncode
    return "success", 0


def run_once_locked(config: Config, *, dry_run: bool = False) -> int:
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
    )
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


def run_once(config: Config, *, dry_run: bool = False) -> int:
    lock_handle = acquire_run_lock(config)
    if config.lock_path and lock_handle is None:
        log(config, f"lock_skip: another watcher run is active at {config.lock_path}")
        return 0
    try:
        return run_once_locked(config, dry_run=dry_run)
    finally:
        release_run_lock(lock_handle)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch Mission 3 auction state and trigger local dashboard refreshes on meaningful changes.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run one check then exit (default).")
    mode.add_argument("--loop", action="store_true", help="Run continuously, sleeping between checks.")
    parser.add_argument("--dry-run", action="store_true", help="Detect changes and log the intended refresh without running it or writing watcher state.")
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
        return run_once(config, dry_run=args.dry_run)

    while True:
        run_once(config, dry_run=args.dry_run)
        time.sleep(config.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
