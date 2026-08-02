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
import hashlib
import json
import os
import queue
import random
import re
import signal
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
CHAIN_ID = 8453
DEFAULT_RPC_URLS = [
    "https://base-rpc.publicnode.com",
    "https://mainnet.base.org",
    "https://base-mainnet.g.alchemy.com/public",
    "https://developer-access-mainnet.base.org",
]
DEFAULT_LOG_RPC_URLS = DEFAULT_RPC_URLS
RPC_QUORUM_DEADLINE_SECONDS = max(
    5.0,
    min(float(os.environ.get("BASE_RPC_QUORUM_DEADLINE_SECONDS", "35")), 120.0),
)
RPC_HEAD_PROBE_DEADLINE_SECONDS = max(
    2.0,
    min(float(os.environ.get("BASE_RPC_HEAD_PROBE_DEADLINE_SECONDS", "12")), 60.0),
)
RPC_HEAD_PROBE_GRACE_SECONDS = max(
    0.0,
    min(float(os.environ.get("BASE_RPC_HEAD_PROBE_GRACE_SECONDS", "0.35")), 3.0),
)
RPC_SLOW_COOLDOWN_SECONDS = max(
    1.0,
    min(float(os.environ.get("BASE_RPC_SLOW_COOLDOWN_SECONDS", "60")), 600.0),
)
RPC_MAX_HEAD_SPREAD_BLOCKS = max(
    1,
    min(int(os.environ.get("BASE_RPC_MAX_HEAD_SPREAD_BLOCKS", "20")), 10_000),
)
RPC_MAX_BLOCK_AGE_SECONDS = max(
    30,
    min(int(os.environ.get("BASE_RPC_MAX_BLOCK_AGE_SECONDS", "600")), 86_400),
)
RPC_MAX_RESPONSE_BYTES = max(
    1024 * 1024,
    min(int(os.environ.get("BASE_RPC_MAX_RESPONSE_BYTES", str(32 * 1024 * 1024))), 64 * 1024 * 1024),
)
RPC_SLOW_UNTIL: dict[tuple[str, str], float] = {}

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
DEFAULT_LOCAL_REFRESH_COMMAND = "npm run refresh:current"
DEFAULT_PUBLISH_REFRESH_COMMAND = "npm run refresh:publish"
SUPPORTED_REFRESH_COMMANDS: dict[str, tuple[str, ...]] = {
    DEFAULT_LOCAL_REFRESH_COMMAND: ("npm", "run", "refresh:current"),
    DEFAULT_PUBLISH_REFRESH_COMMAND: ("npm", "run", "refresh:publish"),
}
PUBLIC_RPC_HOSTNAMES = frozenset(
    urllib.parse.urlsplit(url).hostname or ""
    for url in (*DEFAULT_RPC_URLS, *DEFAULT_LOG_RPC_URLS)
)

try:
    import refresh_telemetry
except Exception:  # pragma: no cover - watcher still works without telemetry helper on ad-hoc copies.
    refresh_telemetry = None  # type: ignore[assignment]


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
    quorum_size: int
    confirmations: int


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


def rpc_provider_key(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or url).lower().strip(".")
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in ("quicknode.pro", "quiknode.pro")):
        return "quicknode"
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in ("alchemy.com", "alchemyapi.io", "blastapi.io")):
        return "alchemy"
    for suffix in (
        "base.org",
        "publicnode.com",
        "ankr.com",
        "drpc.org",
        "infura.io",
        "blastapi.io",
    ):
        if host == suffix or host.endswith(f".{suffix}"):
            return suffix
    return f"rpc-host-{hashlib.sha256(host.encode('utf-8')).hexdigest()[:12]}"


def independent_rpc_urls(urls: list[str]) -> list[str]:
    unique: list[str] = []
    operators: set[str] = set()
    for raw in urls:
        url = str(raw or "").strip()
        if not url:
            continue
        operator = rpc_provider_key(url)
        if operator in operators:
            continue
        operators.add(operator)
        unique.append(url)
    return unique


def configured_rpc_urls() -> list[str]:
    candidates: list[str] = []
    environment = dict(os.environ)
    explicit = any(environment.get(name, "").strip() for name in ("BASE_RPC_URL", "BASE_RPC_URLS", "BASE_LOG_RPC_URLS"))
    if environment.get("BASE_RPC_URL"):
        candidates.append(environment["BASE_RPC_URL"].strip())
    for name in ("BASE_RPC_URLS", "BASE_LOG_RPC_URLS"):
        candidates.extend(item.strip() for item in environment.get(name, "").split(",") if item.strip())
    if env_bool(environment, "BASE_INCLUDE_PUBLIC_FALLBACKS", not explicit):
        candidates.extend(DEFAULT_RPC_URLS)
        candidates.extend(DEFAULT_LOG_RPC_URLS)
    return candidates


def same_operator_rpc_urls(primary_url: str) -> list[str]:
    operator = rpc_provider_key(primary_url)
    candidates = [primary_url, *configured_rpc_urls()]
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen or rpc_provider_key(candidate) != operator:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def responsive_rpc_urls(urls: list[str], required: int, scope: str) -> list[str]:
    now = time.monotonic()
    responsive = [url for url in urls if RPC_SLOW_UNTIL.get((scope, url), 0.0) <= now]
    return responsive if len(responsive) >= required else list(urls)


def mark_rpc_pending_slow(urls: list[str], scope: str) -> None:
    until = time.monotonic() + RPC_SLOW_COOLDOWN_SECONDS
    for url in urls:
        RPC_SLOW_UNTIL[(scope, url)] = until


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
    explicit_rpc = any(env.get(name, "").strip() for name in ("BASE_RPC_URL", "BASE_RPC_URLS", "BASE_LOG_RPC_URLS"))
    include_public_fallbacks = env_bool(env, "BASE_INCLUDE_PUBLIC_FALLBACKS", not explicit_rpc)
    rpc_candidates: list[str] = []
    if env.get("BASE_RPC_URL"):
        rpc_candidates.append(env["BASE_RPC_URL"].strip())
    rpc_candidates.extend(parse_url_list(env, "BASE_RPC_URLS", []))
    if not explicit_rpc or include_public_fallbacks:
        rpc_candidates.extend(DEFAULT_RPC_URLS)
    rpc_urls = independent_rpc_urls(rpc_candidates)
    if len(rpc_urls) < 2:
        raise SystemExit("configure at least two independent Base RPC providers or enable BASE_INCLUDE_PUBLIC_FALLBACKS")
    log_candidates = parse_url_list(env, "BASE_LOG_RPC_URLS", rpc_urls)
    log_rpc_urls = independent_rpc_urls([*log_candidates, *rpc_urls])

    auto_push = env_bool(env, "MISSION3_WATCHER_AUTO_PUSH", False)
    refresh_command = env.get("MISSION3_REFRESH_COMMAND", "")
    if refresh_command == "":
        refresh_command = DEFAULT_PUBLISH_REFRESH_COMMAND if auto_push else DEFAULT_LOCAL_REFRESH_COMMAND
    resolve_refresh_command_argv(refresh_command, auto_push=auto_push)

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
        interval_seconds=env_int(env, "MISSION3_WATCHER_INTERVAL_SECONDS", 15, minimum=15),
        cooldown_seconds=env_int(env, "MISSION3_WATCHER_COOLDOWN_SECONDS", 30, minimum=0),
        bid_cooldown_seconds=env_int(env, "MISSION3_WATCHER_BID_COOLDOWN_SECONDS", 15, minimum=0),
        force_after_seconds=env_int(env, "MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS", 0, minimum=0),
        lookback_blocks=env_int_any(env, ["MISSION3_WATCHER_LOOKBACK_BLOCKS", "MISSION3_WATCHER_LOG_WINDOW_BLOCKS"], 100, minimum=1, maximum=10000),
        safety_overlap_blocks=env_int_any(env, ["MISSION3_WATCHER_SAFETY_OVERLAP_BLOCKS", "MISSION3_WATCHER_LOG_SAFETY_OVERLAP_BLOCKS"], 50, minimum=0, maximum=500),
        log_chunk=env_int(env, "MISSION3_WATCHER_LOG_CHUNK", 50, minimum=1, maximum=10000),
        refresh_command=refresh_command,
        auto_push=auto_push,
        require_clean_tree=env_bool(env, "MISSION3_WATCHER_REQUIRE_CLEAN_TREE", auto_push),
        timeout_seconds=env_int(env, "MISSION3_WATCHER_REFRESH_TIMEOUT_SECONDS", 1800, minimum=60),
        quorum_size=env_int(env, "BASE_RPC_QUORUM_SIZE", 2, minimum=2, maximum=3),
        confirmations=env_int(env, "BASE_SNAPSHOT_CONFIRMATIONS", 1, minimum=1, maximum=64),
    )


def redact_url(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except (TypeError, ValueError):
        return "<redacted-url>"
    hostname = (parts.hostname or "").lower().rstrip(".")
    if not hostname:
        return "<redacted-url>"
    if hostname in PUBLIC_RPC_HOSTNAMES:
        host = hostname
    else:
        host = f"rpc-host-{hashlib.sha256(hostname.encode('utf-8')).hexdigest()[:12]}"
    if port:
        host += f":{port}"
    path = ""
    if parts.path and parts.path != "/":
        path = "/<redacted-path>"
    elif parts.path == "/":
        path = "/"
    query = "redacted=1" if parts.query else ""
    return urllib.parse.urlunsplit(("https", host, path, query, ""))


def redact_rpc_text(value: Any) -> str:
    text = str(value)
    for url in re.findall(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+", text):
        text = text.replace(url, redact_url(url))
    return text


def redact_command(command: str) -> str:
    # Mask common inline secret assignments while preserving enough context for logs.
    pattern = re.compile(r"\b([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Za-z0-9_]*)=([^\s]+)", re.I)
    return pattern.sub(r"\1=<redacted>", command)


def log(config: Config | None, message: str) -> None:
    line = f"[{utc_now()}] {message}"
    print(line)
    if config and config.log_path:
        ensure_private_directory(config.log_path.parent)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(config.log_path, flags, 0o600)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            os.close(descriptor)
            raise RuntimeError(f"watcher log is not an owned regular file: {config.log_path}")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(line + "\n")
            handle.flush()


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects before a provider key or JSON-RPC body can be forwarded."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201, ARG002
        return None


RPC_OPENER = urllib.request.build_opener(NoRedirectHandler())


def open_rpc_request(request: urllib.request.Request, timeout: int):
    return RPC_OPENER.open(request, timeout=timeout)


def validate_rpc_url(url: str) -> None:
    if not isinstance(url, str) or not url or any(character.isspace() for character in url):
        raise RuntimeError("RPC endpoint URL is invalid")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise RuntimeError("RPC endpoint URL is invalid") from exc
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise RuntimeError("RPC endpoint must use HTTPS on port 443 without userinfo or a fragment")


def post_json(url: str, payload: Any, *, timeout: int = 30) -> Any:
    validate_rpc_url(url)
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
        response = open_rpc_request(req, timeout)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"RPC HTTP {exc.code}") from None
    except Exception as exc:  # noqa: BLE001 - provider exceptions must not expose credential-bearing URLs
        raise RuntimeError(f"RPC transport failed ({type(exc).__name__})") from None
    try:
        with response:
            if response.getcode() != 200:
                raise RuntimeError("RPC response returned an unexpected HTTP status")
            if str(response.geturl()) != url:
                raise RuntimeError("RPC response URL changed unexpectedly")
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise RuntimeError("RPC response has a non-JSON Content-Type")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("RPC response has an invalid Content-Length") from exc
                if declared_length < 0 or declared_length > RPC_MAX_RESPONSE_BYTES:
                    raise RuntimeError("RPC response exceeds the configured byte limit")
            raw = response.read(RPC_MAX_RESPONSE_BYTES + 1)
            if len(raw) > RPC_MAX_RESPONSE_BYTES:
                raise RuntimeError("RPC response exceeds the configured byte limit")
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - keep provider-controlled details out of logs/state
        raise RuntimeError(f"RPC response read failed ({type(exc).__name__})") from None
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError("RPC response is not valid UTF-8") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("RPC response is not valid JSON") from exc


def rpc_call(method: str, params: list[Any], *, urls: list[str], timeout: int = 30) -> tuple[Any, str]:
    errors: list[str] = []
    for url in urls:
        try:
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            data = post_json(url, payload, timeout=timeout)
            if not isinstance(data, dict) or data.get("jsonrpc") != "2.0" or type(data.get("id")) is not int or data.get("id") != 1:
                raise RuntimeError("invalid JSON-RPC response envelope")
            has_result = "result" in data
            has_error = "error" in data
            if has_result == has_error:
                raise RuntimeError("invalid JSON-RPC response envelope")
            if has_error:
                error = data.get("error")
                if (
                    not isinstance(error, dict)
                    or type(error.get("code")) is not int
                    or not isinstance(error.get("message"), str)
                ):
                    raise RuntimeError("invalid JSON-RPC error envelope")
                raise RuntimeError(f"JSON-RPC error code={error['code']}")
            return data["result"], url
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{redact_url(url)}: {redact_rpc_text(exc)}")
    raise RuntimeError("; ".join(errors))


def rpc_call_with_retry(method: str, params: list[Any], *, url: str, timeout: int = 30) -> tuple[Any, str]:
    operator_urls = same_operator_rpc_urls(url)
    attempts = max(3, len(operator_urls))
    for attempt in range(attempts):
        candidate = operator_urls[attempt % len(operator_urls)]
        try:
            return rpc_call(method, params, urls=[candidate], timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            permanent = any(code in message for code in ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404", "-32600", "-32601", "-32602"))
            if permanent and attempt + 1 >= len(operator_urls):
                raise
            if attempt == attempts - 1:
                raise
            time.sleep(random.uniform(0, min(2.0, 0.25 * (2**attempt))))
    raise RuntimeError(f"unreachable retry state for {method}")


def canonical_rpc_result(method: str, value: Any) -> str:
    if method == "eth_getLogs" and isinstance(value, list):
        normalized = sorted(
            (
                {
                    "address": str(item.get("address") or "").lower(),
                    "blockHash": str(item.get("blockHash") or "").lower(),
                    "blockNumber": str(item.get("blockNumber") or "").lower(),
                    "data": str(item.get("data") or "").lower(),
                    "logIndex": str(item.get("logIndex") or "").lower(),
                    "removed": bool(item.get("removed", False)),
                    "topics": [str(topic).lower() for topic in item.get("topics") or []],
                    "transactionHash": str(item.get("transactionHash") or "").lower(),
                }
                for item in value
                if isinstance(item, dict)
            ),
            key=lambda item: (
                item["blockHash"],
                item["transactionHash"],
                int(item["logIndex"] or "0x0", 16),
            ),
        )
        return json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    if isinstance(value, str):
        return value.lower()
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def rpc_quorum_call(
    method: str,
    params: list[Any],
    *,
    urls: list[str],
    required: int,
    timeout: int = 30,
) -> tuple[Any, list[str]]:
    urls = responsive_rpc_urls(urls, required, method)
    if len(urls) < required:
        raise RuntimeError(f"{method} requires {required} independent RPC providers; configured={len(urls)}")
    grouped: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    errors: list[str] = []
    responses: queue.Queue[tuple[int, str, Any, Exception | None]] = queue.Queue()

    def worker(index: int, url: str) -> None:
        try:
            value, _used = rpc_call_with_retry(method, params, url=url, timeout=timeout)
            responses.put((index, url, value, None))
        except Exception as exc:  # noqa: BLE001
            responses.put((index, url, None, exc))

    pending_indexes = set(range(len(urls)))
    for index, url in enumerate(urls):
        threading.Thread(
            target=worker,
            args=(index, url),
            name=f"rpc-quorum-{method}-{index}",
            daemon=True,
        ).start()

    deadline = time.monotonic() + RPC_QUORUM_DEADLINE_SECONDS
    while pending_indexes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            index, url, value, error = responses.get(timeout=remaining)
        except queue.Empty:
            break
        if index not in pending_indexes:
            continue
        pending_indexes.remove(index)
        if error is None:
            grouped[canonical_rpc_result(method, value)].append((url, value))
        else:
            errors.append(f"{redact_url(url)}: {redact_rpc_text(error)}")
        pending = len(pending_indexes)
        ordered = sorted(grouped.values(), key=len, reverse=True)
        winner = ordered[0] if ordered else []
        runner_up_votes = len(ordered[1]) if len(ordered) > 1 else 0
        if len(winner) >= required and len(winner) > runner_up_votes + pending:
            mark_rpc_pending_slow([urls[item] for item in pending_indexes], method)
            return winner[0][1], [winner_url for winner_url, _value in winner]

    if pending_indexes:
        pending_urls = [urls[item] for item in pending_indexes]
        mark_rpc_pending_slow(pending_urls, method)
        errors.append(
            "deadline exceeded: "
            + ", ".join(redact_url(url) for url in pending_urls[:3])
        )

    votes = sorted((len(group) for group in grouped.values()), reverse=True)
    error_detail = f" errors={'; '.join(errors[:3])}" if errors else ""
    raise RuntimeError(f"{method} quorum disagreement required={required} votes={votes}{error_detail}")


def collect_rpc_probes(
    urls: list[str],
    *,
    required: int,
    probe: Any,
    label: str,
) -> tuple[list[Any], list[str]]:
    scope = f"probe:{label}"
    active_urls = responsive_rpc_urls(urls, required, scope)
    responses: queue.Queue[tuple[int, str, Any, Exception | None]] = queue.Queue()

    def worker(index: int, url: str) -> None:
        try:
            responses.put((index, url, probe(url), None))
        except Exception as exc:  # noqa: BLE001
            responses.put((index, url, None, exc))

    pending_indexes = set(range(len(active_urls)))
    results: list[Any] = []
    errors: list[str] = []
    for index, url in enumerate(active_urls):
        threading.Thread(
            target=worker,
            args=(index, url),
            name=f"rpc-probe-{label}-{index}",
            daemon=True,
        ).start()

    hard_deadline = time.monotonic() + RPC_HEAD_PROBE_DEADLINE_SECONDS
    quorum_deadline: float | None = None
    while pending_indexes:
        now = time.monotonic()
        if len(results) >= required and quorum_deadline is None:
            quorum_deadline = now + RPC_HEAD_PROBE_GRACE_SECONDS
        deadline = min(hard_deadline, quorum_deadline) if quorum_deadline is not None else hard_deadline
        remaining = deadline - now
        if remaining <= 0:
            break
        try:
            index, url, value, error = responses.get(timeout=remaining)
        except queue.Empty:
            break
        if index not in pending_indexes:
            continue
        pending_indexes.remove(index)
        if error is None:
            results.append(value)
        else:
            errors.append(f"{redact_url(url)}: {redact_rpc_text(error)}")

    if pending_indexes:
        pending_urls = [active_urls[item] for item in pending_indexes]
        mark_rpc_pending_slow(pending_urls, scope)
        errors.append(
            f"{label} probe deadline exceeded: "
            + ", ".join(redact_url(url) for url in pending_urls[:3])
        )
    return results, errors


def verified_snapshot_head(config: Config) -> tuple[int, dict[str, Any], list[str]]:
    def endpoint_head(url: str) -> tuple[str, int]:
        chain_hex, _ = rpc_call_with_retry("eth_chainId", [], url=url, timeout=20)
        chain_id = int(str(chain_hex), 16)
        if chain_id != CHAIN_ID:
            raise RuntimeError(f"wrong chain id {chain_id}; expected {CHAIN_ID}")
        block_hex, _ = rpc_call_with_retry("eth_blockNumber", [], url=url, timeout=20)
        return url, int(str(block_hex), 16)

    heads, errors = collect_rpc_probes(
        config.rpc_urls,
        required=config.quorum_size,
        probe=endpoint_head,
        label="head",
    )
    if len(heads) < config.quorum_size:
        raise RuntimeError(
            f"Base RPC head quorum unavailable healthy={len(heads)} required={config.quorum_size}; "
            + "; ".join(errors[:3])
        )
    ordered_pairs = sorted(heads, key=lambda item: item[1], reverse=True)
    head_cluster: list[tuple[str, int]] = []
    for _anchor_url, anchor_head in ordered_pairs:
        candidate = [
            (url, head)
            for url, head in ordered_pairs
            if anchor_head - RPC_MAX_HEAD_SPREAD_BLOCKS <= head <= anchor_head
        ]
        if len(candidate) >= config.quorum_size:
            head_cluster = candidate
            break
    if len(head_cluster) < config.quorum_size:
        detail = ", ".join(f"{redact_url(url)}={head}" for url, head in ordered_pairs)
        raise RuntimeError(
            f"Base RPC heads cannot form a recent quorum within {RPC_MAX_HEAD_SPREAD_BLOCKS} blocks: {detail}"
        )
    quorum_head = sorted((head for _url, head in head_cluster), reverse=True)[config.quorum_size - 1]
    block_number = max(0, quorum_head - config.confirmations)
    eligible = [url for url, head in head_cluster if head >= block_number]
    block, agreeing_urls = rpc_quorum_call(
        "eth_getBlockByNumber",
        [hex(block_number), False],
        urls=eligible,
        required=config.quorum_size,
        timeout=30,
    )
    if not isinstance(block, dict) or not block.get("hash"):
        raise RuntimeError(f"verified Base block {block_number} missing hash")
    try:
        observed_number = int(str(block.get("number") or ""), 16)
        block_timestamp = int(str(block.get("timestamp") or ""), 16)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"verified Base block {block_number} has malformed number/timestamp") from exc
    if observed_number != block_number:
        raise RuntimeError(
            f"verified Base block number mismatch expected={block_number} observed={observed_number}"
        )
    age = time.time() - block_timestamp
    if age < -60 or age > RPC_MAX_BLOCK_AGE_SECONDS:
        raise RuntimeError(
            f"verified Base block {block_number} is outside the freshness window: age_seconds={age:.0f}"
        )
    return block_number, block, agreeing_urls


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
    recent_from_block = max(default_from_block, latest_block - lookback_blocks + 1)
    if not state:
        return recent_from_block
    try:
        last_checked = int(state.get("last_checked_block") or state.get("last_seen_block") or 0)
    except (TypeError, ValueError):
        last_checked = 0
    if last_checked > latest_block:
        # A provider can briefly report a lower head, and a canonical reorg can
        # genuinely move it backwards. Re-scan the full configured window so a
        # regressed head cannot collapse log coverage to only the latest block.
        return recent_from_block
    if last_checked > 0:
        return max(default_from_block, min(latest_block, last_checked + 1 - safety_overlap_blocks))
    return recent_from_block


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
        result, _urls = rpc_quorum_call(
            "eth_getLogs",
            [log_filter(AUCTION_HOUSE, WATCHED_TOPICS, start, end)],
            urls=config.log_rpc_urls,
            required=config.quorum_size,
            timeout=45,
        )
        if not isinstance(result, list):
            raise RuntimeError(f"unexpected eth_getLogs response: {result!r}")
        logs.extend(item for item in result if isinstance(item, dict) and not bool(item.get("removed", False)))
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
    latest_block, block_data, agreeing_urls = verified_snapshot_head(config)
    code, code_urls = rpc_quorum_call(
        "eth_getCode",
        [AUCTION_HOUSE, hex(latest_block)],
        urls=agreeing_urls,
        required=config.quorum_size,
        timeout=30,
    )
    if not isinstance(code, str) or code in {"", "0x", "0x0"}:
        raise RuntimeError(f"auction house has no code at verified Base block {latest_block}")
    raw, call_urls = rpc_quorum_call(
        "eth_call",
        [{"to": AUCTION_HOUSE, "data": SELECTOR_AUCTION}, hex(latest_block)],
        urls=[url for url in agreeing_urls if url in code_urls],
        required=config.quorum_size,
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
    expected_block_hash = str(block_data.get("hash") or "").lower()

    def verify_log_url(url: str) -> str:
        chain_hex, _ = rpc_call_with_retry("eth_chainId", [], url=url, timeout=15)
        if int(str(chain_hex), 16) != CHAIN_ID:
            raise RuntimeError(f"wrong chain id from {redact_url(url)}")
        candidate, _ = rpc_call_with_retry(
            "eth_getBlockByNumber",
            [hex(latest_block), False],
            url=url,
            timeout=20,
        )
        observed_hash = str((candidate or {}).get("hash") or "").lower() if isinstance(candidate, dict) else ""
        if observed_hash != expected_block_hash:
            raise RuntimeError(
                f"snapshot hash mismatch expected={expected_block_hash} observed={observed_hash or 'missing'}"
            )
        sample_from = max(
            0,
            latest_block - max(config.lookback_blocks, config.safety_overlap_blocks, config.log_chunk) + 1,
        )
        sample, _ = rpc_call_with_retry(
            "eth_getLogs",
            [log_filter(AUCTION_HOUSE, WATCHED_TOPICS, sample_from, latest_block)],
            url=url,
            timeout=30,
        )
        if not isinstance(sample, list):
            raise RuntimeError("eth_getLogs capability check did not return a list")
        return url

    log_candidates = independent_rpc_urls([*config.log_rpc_urls, *call_urls])
    verified_log_urls, log_errors = collect_rpc_probes(
        log_candidates,
        required=config.quorum_size,
        probe=verify_log_url,
        label="log-capability",
    )
    if len(verified_log_urls) < config.quorum_size:
        raise RuntimeError(
            f"Base RPC log quorum unavailable healthy={len(verified_log_urls)} required={config.quorum_size}; "
            + "; ".join(log_errors[:3])
        )
    logs = fetch_logs(config._replace(log_rpc_urls=verified_log_urls), from_block, latest_block)
    rechecked_block, _recheck_urls = rpc_quorum_call(
        "eth_getBlockByNumber",
        [hex(latest_block), False],
        urls=call_urls,
        required=config.quorum_size,
        timeout=30,
    )
    rechecked_hash = str((rechecked_block or {}).get("hash") or "").lower() if isinstance(rechecked_block, dict) else ""
    if rechecked_hash != expected_block_hash:
        raise RuntimeError(
            f"verified Base block {latest_block} reorganized during watcher scan: "
            f"expected={expected_block_hash} observed={rechecked_hash or 'missing'}"
        )
    providers = sorted({rpc_provider_key(url) for url in call_urls})
    log_providers = sorted({rpc_provider_key(url) for url in verified_log_urls})
    snapshot = {
        **auction,
        "checked_from_block": from_block,
        "checked_to_block": latest_block,
        "checked_log_count": len(logs),
        "created_log": latest_log_for_topic(logs, TOPIC_AUCTION_CREATED),
        "bid_log": latest_log_for_topic(logs, TOPIC_AUCTION_BID),
        "extended_log": latest_log_for_topic(logs, TOPIC_AUCTION_EXTENDED),
        "settled_log": latest_log_for_topic(logs, TOPIC_AUCTION_SETTLED),
        "snapshot_block_hash": str(block_data.get("hash") or "").lower(),
        "onchain_verification_status": "current_snapshot_cross_provider_verified",
        "onchain_verification_scope": "snapshot_hash,contract_code,current_auction,recent_event_logs",
        "rpc_quorum_size": config.quorum_size,
        "rpc_quorum_providers": providers,
        "log_rpc_quorum_providers": log_providers,
        "rpc_urls": [redact_url(url) for url in call_urls],
    }
    return snapshot


def load_state(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SystemExit(f"unable to securely open watcher state at {path}: {exc}") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise SystemExit(f"unsafe watcher state at {path}: expected an owned regular file")
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise SystemExit(f"unsafe watcher state permissions at {path}: expected mode 600")
        if details.st_size > 2_097_152:
            raise SystemExit(f"watcher state is unexpectedly large at {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            data = json.load(handle)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid watcher state JSON at {path}: {exc}") from exc
    finally:
        os.close(descriptor)
    if not isinstance(data, dict):
        raise SystemExit(f"invalid watcher state at {path}: expected object")
    return data


def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise RuntimeError(f"runner directory is not an owned directory: {path}")
    path.chmod(0o700)


def save_state(path: Path, state: dict[str, Any]) -> None:
    ensure_private_directory(path.parent)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
        path.chmod(0o600)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


class RefreshAlreadyRunning(RuntimeError):
    pass


def acquire_file_lock(path: Path, *, label: str) -> Any | None:
    ensure_private_directory(path.parent)
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        os.close(descriptor)
        raise RuntimeError(f"runner lock is not an owned regular file: {path}")
    os.fchmod(descriptor, 0o600)
    handle = os.fdopen(descriptor, "a+", encoding="utf-8")
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
    if int(snapshot.get("start_time_unix") or 0) != int(state.get("last_seen_start_time_unix") or 0):
        reasons.append("auction_start_time_changed")
    if int(snapshot.get("end_time_unix") or 0) != int(state.get("last_seen_end_time_unix") or 0):
        reasons.append("auction_end_time_changed")

    if state.get("pending_refresh"):
        pending_age = seconds_since(state.get("pending_refresh_since_utc"), now_utc)
        last_refresh_age = seconds_since(state.get("last_refresh_at_utc"), now_utc)
        pending_reasons = _state_reasons(state.get("pending_refresh_reasons")) or ["pending_refresh_after_cooldown"]
        pending_cooldown = cooldown_for_reasons(pending_reasons, cooldown_seconds=cooldown_seconds, bid_cooldown_seconds=bid_cooldown_seconds)
        if not pending_backoff_active(state, now_utc) and (
            last_refresh_age is None or last_refresh_age >= pending_cooldown or pending_age is None or pending_age >= pending_cooldown
        ):
            retry_reasons = ["pending_refresh_after_cooldown"]
            for reason in reasons:
                if reason not in retry_reasons:
                    retry_reasons.append(reason)
            return RefreshDecision(True, retry_reasons)

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


def latest_activity_event(snapshot: dict[str, Any]) -> dict[str, Any]:
    events = []
    for key in ("created_log", "bid_log", "extended_log", "settled_log"):
        item = get_snapshot_log(snapshot, key)
        if item:
            events.append(item)
    if not events:
        return {}
    return max(events, key=lambda item: (int(item.get("block_number") or 0), int(item.get("log_index") or 0)))


def telemetry_base_row(started_at_utc: str, completed_at_utc: str, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = snapshot or {}
    event = latest_activity_event(snapshot) if snapshot else {}
    row: dict[str, Any] = {
        "started_at_utc": started_at_utc,
        "completed_at_utc": completed_at_utc,
        "duration_seconds": seconds_since(started_at_utc, completed_at_utc),
        "checked_from_block": snapshot.get("checked_from_block"),
        "checked_to_block": snapshot.get("checked_to_block") or snapshot.get("latest_block"),
        "latest_block": snapshot.get("latest_block"),
        "token_id": snapshot.get("token_id"),
        "high_bidder": normalize_address(snapshot.get("high_bidder")),
        "amount_wei": str(snapshot.get("amount_wei") or ""),
        "settled": bool(snapshot.get("settled")) if snapshot else None,
        "checked_log_count": snapshot.get("checked_log_count"),
        "event_name": event.get("event_name"),
        "event_block_number": event.get("block_number"),
        "event_tx_hash": event.get("tx_hash"),
        "event_log_index": event.get("log_index"),
    }
    return {key: value for key, value in row.items() if value not in (None, "")}


def record_watcher_telemetry(config: Config, row: dict[str, Any]) -> None:
    if refresh_telemetry is None:
        return
    try:
        refresh_telemetry.record_watcher_check(row, root=ROOT)
    except Exception as exc:  # noqa: BLE001
        log(config, f"warning: unable to record watcher telemetry: {exc}")


PENDING_REFRESH_IDENTITY_KEYS = [
    "pending_token_id",
    "pending_high_bidder",
    "pending_bidder",
    "pending_amount_wei",
    "pending_settled",
    "pending_start_time_unix",
    "pending_end_time_unix",
    "pending_bid_log_id",
    "pending_bid_tx",
    "pending_bid_log_index",
    "pending_bid_token_id",
    "pending_event_name",
    "pending_event_block_number",
    "pending_event_tx_hash",
    "pending_event_log_index",
    "pending_observed_block",
]


def _clear_pending_refresh_fields(state: dict[str, Any]) -> None:
    for key in ["pending_refresh", "pending_refresh_since_utc", "pending_refresh_reasons", *PENDING_REFRESH_IDENTITY_KEYS]:
        state.pop(key, None)


def _apply_pending_identity(state: dict[str, Any], snapshot: dict[str, Any], *, now_utc: str, reasons: list[str]) -> None:
    bid_log = get_snapshot_log(snapshot, "bid_log")
    extended_log = get_snapshot_log(snapshot, "extended_log")
    event = latest_activity_event(snapshot)
    state["pending_refresh"] = True
    state.setdefault("pending_refresh_since_utc", now_utc)
    state["pending_refresh_reasons"] = reasons
    state["pending_token_id"] = int(snapshot.get("token_id") or 0)
    state["pending_high_bidder"] = normalize_address(snapshot.get("high_bidder"))
    state["pending_bidder"] = normalize_address(bid_log.get("bidder")) or normalize_address(snapshot.get("high_bidder"))
    state["pending_amount_wei"] = str(snapshot.get("amount_wei") or bid_log.get("amount_wei") or "0")
    state["pending_settled"] = bool(snapshot.get("settled"))
    state["pending_start_time_unix"] = int(snapshot.get("start_time_unix") or 0)
    state["pending_end_time_unix"] = int(snapshot.get("end_time_unix") or extended_log.get("end_time_unix") or 0)
    state["pending_bid_log_id"] = bid_log.get("id", "") or state.get("pending_bid_log_id", "")
    state["pending_bid_tx"] = bid_log.get("tx_hash", "") or state.get("pending_bid_tx", "")
    state["pending_bid_log_index"] = int(bid_log.get("log_index") or state.get("pending_bid_log_index") or 0)
    state["pending_bid_token_id"] = int(bid_log.get("token_id") or snapshot.get("token_id") or state.get("pending_bid_token_id") or 0)
    if event:
        state["pending_event_name"] = event.get("event_name", "")
        state["pending_event_block_number"] = event.get("block_number", "")
        state["pending_event_tx_hash"] = event.get("tx_hash", "")
        state["pending_event_log_index"] = event.get("log_index", "")
    state["pending_observed_block"] = latest_activity_block(snapshot)


def state_from_snapshot(
    snapshot: dict[str, Any],
    *,
    now_utc: str,
    previous_state: dict[str, Any],
    decision: RefreshDecision | None = None,
    acknowledge: bool | None = None,
) -> dict[str, Any]:
    state = dict(previous_state)
    created_log = get_snapshot_log(snapshot, "created_log")
    bid_log = get_snapshot_log(snapshot, "bid_log")
    extended_log = get_snapshot_log(snapshot, "extended_log")
    settled_log = get_snapshot_log(snapshot, "settled_log")
    checked_to_block = int(snapshot.get("checked_to_block") or snapshot.get("latest_block") or 0)
    observed_block = latest_activity_block(snapshot)

    if acknowledge is None:
        acknowledge = not bool(decision and (decision.should_refresh or decision.pending_refresh))

    state.update(
        {
            "schema_version": SCHEMA_VERSION,
            "updated_at_utc": now_utc,
            "chain_id": CHAIN_ID,
            "auction_house": AUCTION_HOUSE,
            "onchain_verification_status": snapshot.get("onchain_verification_status", ""),
            "onchain_verification_scope": snapshot.get("onchain_verification_scope", ""),
            "last_verified_block_hash": snapshot.get("snapshot_block_hash", ""),
            "last_rpc_quorum_size": int(snapshot.get("rpc_quorum_size") or 0),
            "last_rpc_quorum_providers": snapshot.get("rpc_quorum_providers") or [],
            "last_log_rpc_quorum_providers": snapshot.get("log_rpc_quorum_providers") or [],
            "last_checked_at_utc": now_utc,
            "last_checked_block": checked_to_block,
            "last_checked_from_block": int(snapshot.get("checked_from_block") or 0),
            "last_observed_block": observed_block,
            "last_observed_token_id": int(snapshot.get("token_id") or 0),
            "last_observed_high_bidder": normalize_address(snapshot.get("high_bidder")),
            "last_observed_bidder": normalize_address(bid_log.get("bidder")) or normalize_address(snapshot.get("high_bidder")),
            "last_observed_amount_wei": str(snapshot.get("amount_wei") or bid_log.get("amount_wei") or "0"),
            "last_observed_settled": bool(snapshot.get("settled")),
            "last_observed_start_time_unix": int(snapshot.get("start_time_unix") or 0),
            "last_observed_end_time_unix": int(snapshot.get("end_time_unix") or extended_log.get("end_time_unix") or 0),
            "last_observed_auction_created_log_id": created_log.get("id", "") or state.get("last_observed_auction_created_log_id", ""),
            "last_observed_auction_created_tx": created_log.get("tx_hash", "") or state.get("last_observed_auction_created_tx", ""),
            "last_observed_auction_settled_log_id": settled_log.get("id", "") or state.get("last_observed_auction_settled_log_id", ""),
            "last_observed_auction_settled_tx": settled_log.get("tx_hash", "") or state.get("last_observed_auction_settled_tx", ""),
            "last_observed_auction_extended_log_id": extended_log.get("id", "") or state.get("last_observed_auction_extended_log_id", ""),
            "last_observed_auction_extended_tx": extended_log.get("tx_hash", "") or state.get("last_observed_auction_extended_tx", ""),
            "last_observed_bid_log_id": bid_log.get("id", "") or state.get("last_observed_bid_log_id", ""),
            "last_observed_bid_tx": bid_log.get("tx_hash", "") or state.get("last_observed_bid_tx", ""),
            "last_observed_bid_log_index": int(bid_log.get("log_index") or state.get("last_observed_bid_log_index") or 0),
            "last_observed_bid_token_id": int(bid_log.get("token_id") or snapshot.get("token_id") or state.get("last_observed_bid_token_id") or 0),
            "last_rpc_url": ",".join(snapshot.get("rpc_urls") or []),
            "last_log_count": int(snapshot.get("checked_log_count") or 0),
            "last_error": None,
        }
    )

    if acknowledge:
        state.update(
            {
                "last_seen_block": observed_block,
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
            }
        )

    if decision and (decision.pending_refresh or decision.should_refresh):
        _apply_pending_identity(state, snapshot, now_utc=now_utc, reasons=decision.reasons)
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
            "last_error": redact_rpc_text(error)[:500],
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
        _clear_pending_refresh_fields(state)
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


def resolve_refresh_command_argv(command: str, *, auto_push: bool) -> tuple[str, ...]:
    argv = SUPPORTED_REFRESH_COMMANDS.get(command)
    if argv is None:
        supported = ", ".join(repr(item) for item in SUPPORTED_REFRESH_COMMANDS)
        raise SystemExit(
            "MISSION3_REFRESH_COMMAND must exactly match a supported command "
            f"({supported}); shell syntax, paths, extra arguments, and whitespace variants are forbidden"
        )
    if command == DEFAULT_PUBLISH_REFRESH_COMMAND and not auto_push:
        raise SystemExit(
            "npm run refresh:publish requires MISSION3_WATCHER_AUTO_PUSH=1"
        )
    return argv


def validate_refresh_command(config: Config) -> tuple[str, ...]:
    return resolve_refresh_command_argv(config.refresh_command, auto_push=config.auto_push)


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


def run_refresh(config: Config, reasons: list[str], *, dry_run: bool, event: dict[str, Any] | None = None) -> tuple[str, int]:
    refresh_argv = validate_refresh_command(config)
    refresh_lock = None
    if not dry_run:
        refresh_lock = acquire_refresh_lock(config)
        if config.refresh_lock_path and refresh_lock is None:
            raise RefreshAlreadyRunning(f"another refresh is already running at {config.refresh_lock_path}")

    try:
        # Inspect the worktree only after owning the shared publisher lock. The
        # hourly publisher legitimately dirties generated files while it runs;
        # lock contention must be treated as a healthy deferral, not corruption.
        if config.require_clean_tree:
            tracked = git_status_tracked().strip()
            if tracked:
                raise RuntimeError("tracked working tree changes exist; refusing guarded refresh:\n" + tracked)
        else:
            tracked = git_status_tracked().strip()
            if tracked:
                log(config, "warning: tracked working tree changes exist before local refresh")

        command_for_log = " ".join(refresh_argv)
        if dry_run:
            log(config, f"dry-run: would run refresh command: {command_for_log}; reasons={','.join(reasons)}")
            return "dry_run", 0

        child_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "DEGEN_DOGS_REFRESH_TRIGGER": "watcher",
            "DEGEN_DOGS_REFRESH_REASONS": json.dumps(reasons),
            "DEGEN_DOGS_DETECTED_AT_UTC": utc_now(),
        }
        event = event or {}
        if event:
            child_env["DEGEN_DOGS_EVENT_NAME"] = str(event.get("event_name") or "")
            child_env["DEGEN_DOGS_EVENT_BLOCK_NUMBER"] = str(event.get("block_number") or "")
            child_env["DEGEN_DOGS_EVENT_TX_HASH"] = str(event.get("tx_hash") or "")
            child_env["DEGEN_DOGS_EVENT_LOG_INDEX"] = str(event.get("log_index") or "")
        if refresh_lock and config.refresh_lock_path:
            # The parent watcher holds the same lock used by refresh_and_publish.sh.
            # Passing DEGEN_DOGS_LOCK_HELD lets that script run without trying to
            # reacquire the lock it already owns through this process.
            child_env["DEGEN_DOGS_LOCK_HELD"] = "1"
            child_env["DEGEN_DOGS_LOCK_DIR"] = str(config.refresh_lock_path.parent)
            child_env["DEGEN_DOGS_REFRESH_LOCK_PATH"] = str(config.refresh_lock_path)
            child_env["DEGEN_DOGS_LOCK_FD"] = str(refresh_lock.fileno())
        log(config, f"running refresh command: {command_for_log}; reasons={','.join(reasons)}")
        pass_fds = (refresh_lock.fileno(),) if refresh_lock else ()
        process = subprocess.Popen(
            list(refresh_argv),
            cwd=ROOT,
            text=True,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=pass_fds,
            shell=False,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=config.timeout_seconds)
        except subprocess.TimeoutExpired:
            # Terminate the entire refresh process group before releasing the
            # inherited publisher lock; otherwise grandchildren can outlive the
            # watcher and mutate after another refresh starts.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(list(refresh_argv), config.timeout_seconds, output=stdout, stderr=stderr)
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
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
    check_started = utc_now()
    now = check_started
    state = load_state(config.state_path)
    try:
        snapshot = fetch_snapshot(config, state)
    except Exception as exc:  # noqa: BLE001
        if not dry_run:
            record_rpc_error(config.state_path, state, exc, now)
        completed = utc_now()
        record_watcher_telemetry(
            config,
            {
                **telemetry_base_row(check_started, completed),
                "result": "failed",
                "error": redact_rpc_text(exc)[:500],
                "dry_run": dry_run,
                "force_refresh": force_refresh,
            },
        )
        log(config, f"rpc_error: {redact_rpc_text(exc)}")
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
    activity_event = latest_activity_event(snapshot)

    if decision.should_refresh:
        try:
            status, exit_code = run_refresh(config, decision.reasons, dry_run=dry_run, event=activity_event)
        except RefreshAlreadyRunning as exc:
            new_state = mark_pending_refresh(new_state, reasons=decision.reasons, now_utc=utc_now(), status="deferred_refresh_lock")
            if not dry_run:
                save_state(config.state_path, new_state)
            completed = utc_now()
            record_watcher_telemetry(
                config,
                {
                    **telemetry_base_row(check_started, completed, snapshot),
                    "result": "deferred_refresh_lock",
                    "reasons": decision.reasons,
                    "pending_refresh": True,
                    "refresh_status": "deferred_refresh_lock",
                    "error": str(exc)[:500],
                    "dry_run": dry_run,
                    "force_refresh": force_refresh,
                },
            )
            log(config, f"refresh_lock_skip pending=1: {exc}; {summary}")
            return 0
        except Exception as exc:  # noqa: BLE001
            new_state = record_refresh_result(new_state, status="failure", reasons=decision.reasons, now_utc=utc_now(), exit_code=1)
            new_state["last_refresh_error"] = str(exc)[:500]
            if not dry_run:
                save_state(config.state_path, new_state)
            completed = utc_now()
            record_watcher_telemetry(
                config,
                {
                    **telemetry_base_row(check_started, completed, snapshot),
                    "result": "refresh_failed",
                    "reasons": decision.reasons,
                    "pending_refresh": True,
                    "refresh_status": "failure",
                    "refresh_exit_code": 1,
                    "error": str(exc)[:500],
                    "dry_run": dry_run,
                    "force_refresh": force_refresh,
                },
            )
            log(config, f"refresh_error: {exc}; {summary}")
            return 2
        new_state = state_from_snapshot(snapshot, now_utc=utc_now(), previous_state=new_state, acknowledge=status in {"success", "dry_run"})
        new_state = record_refresh_result(new_state, status=status, reasons=decision.reasons, now_utc=utc_now(), exit_code=exit_code)
        if not dry_run:
            save_state(config.state_path, new_state)
        completed = utc_now()
        record_watcher_telemetry(
            config,
            {
                **telemetry_base_row(check_started, completed, snapshot),
                "result": "refresh_failed" if status == "failure" else f"refresh_{status}",
                "reasons": decision.reasons,
                "pending_refresh": bool(new_state.get("pending_refresh")),
                "refresh_status": status,
                "refresh_exit_code": exit_code,
                "dry_run": dry_run,
                "force_refresh": force_refresh,
            },
        )
        if status == "failure":
            log(config, f"refresh_failed exit_code={exit_code}; {summary}")
            return 2
        log(config, f"refresh_{status}; {summary}")
        return 0

    if not dry_run:
        save_state(config.state_path, new_state)
    completed = utc_now()
    record_watcher_telemetry(
        config,
        {
            **telemetry_base_row(check_started, completed, snapshot),
            "result": "cooldown_skip" if decision.cooldown_skip else "no_refresh",
            "reasons": decision.reasons,
            "pending_refresh": bool(new_state.get("pending_refresh")),
            "dry_run": dry_run,
            "force_refresh": force_refresh,
        },
    )
    if decision.cooldown_skip:
        log(config, f"cooldown_skip pending=1; {summary}")
    else:
        log(config, f"no_refresh; {summary}")
    return 0


def run_once(config: Config, *, dry_run: bool = False, force_refresh: bool = False) -> int:
    lock_handle = acquire_run_lock(config)
    if config.lock_path and lock_handle is None:
        now = utc_now()
        record_watcher_telemetry(config, {"started_at_utc": now, "completed_at_utc": now, "duration_seconds": 0, "result": "lock_skip"})
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
    parser.add_argument(
        "--refresh-command",
        help="Select exactly 'npm run refresh:current' or 'npm run refresh:publish' for this run.",
    )
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
