#!/usr/bin/env python3
"""Mission 3 Base archive indexer.

Fetches Degen Dogs Mission 3 auction-house logs from Base, stores raw logs and
decoded events in an append-only SQLite archive, then exports generated CSV/JSON
files for long-term preservation and future dashboard integration.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import email.utils
import hashlib
import itertools
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Iterable

getcontext().prec = 80

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "archive" / "mission3"
CONFIG_DIR = ARCHIVE / "config"
SQL_DIR = ARCHIVE / "sql"
DATA_DIR = ARCHIVE / "data"
DEFAULT_DB = DATA_DIR / "mission3_archive.sqlite"
DEFAULT_OUTPUT_DIR = DATA_DIR / "generated"
DEFAULT_RAW_DIR = DATA_DIR / "raw"
PUBLIC_OUTPUT_DIR = ROOT / "public" / "generated" / "mission3"

DEFAULT_RPC_URLS = [
    "https://mainnet.base.org",
    "https://developer-access-mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.drpc.org",
    "https://base.gateway.tenderly.co",
    "https://base.lava.build",
]
DEFAULT_LOG_RPC_URLS = [
    "https://mainnet.base.org",
    "https://base.gateway.tenderly.co",
    "https://base.lava.build",
]
DEFAULT_RPC_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
STATE_ID = "mission3"
SELECTOR_AUCTION = "0x7d9f6db5"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
OPENSEA_ITEM_BASE = "https://opensea.io/item/base"

CSV_EXPORTS = [
    "mission3_auction_created",
    "mission3_auction_bids",
    "mission3_auction_extended",
    "mission3_auction_settled",
    "mission3_auction_winners",
    "mission3_recent_bids",
    "mission3_bidder_leaderboard",
    "mission3_auction_timeline",
    "mission3_daily_activity",
]
JSON_LIST_EXPORTS = [
    "mission3_dog_search_index",
]
JSON_OBJECT_EXPORTS = [
    "mission3_archive_metrics",
]


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so credentials and JSON-RPC bodies never change origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None


RPC_OPENER = urllib.request.build_opener(NoRedirectHandler())
MAX_RETRY_AFTER_SECONDS = 300.0
MAX_RETRY_AFTER_HEADER_CHARS = 128
_HTTP_WEEKDAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_HTTP_WEEKDAY_LONG = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_HTTP_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_HTTP_TIME = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
_RETRY_AFTER_HTTP_DATE_PATTERNS = (
    re.compile(rf"{_HTTP_WEEKDAY}, (?:0[1-9]|[12][0-9]|3[01]) {_HTTP_MONTH} [0-9]{{4}} {_HTTP_TIME} GMT"),
    re.compile(rf"{_HTTP_WEEKDAY_LONG}, (?:0[1-9]|[12][0-9]|3[01])-{_HTTP_MONTH}-[0-9]{{2}} {_HTTP_TIME} GMT"),
    re.compile(rf"{_HTTP_WEEKDAY} {_HTTP_MONTH} (?: [1-9]|0[1-9]|[12][0-9]|3[01]) {_HTTP_TIME} [0-9]{{4}}"),
)


class RpcRateLimited(RuntimeError):
    """Secret-safe HTTP 429 signal with an optional bounded retry hint."""

    def __init__(self, retry_after_seconds: float | None) -> None:
        super().__init__("HTTP 429")
        self.retry_after_seconds = retry_after_seconds


class RpcLogRangeLimit(RuntimeError):
    """An explicit eth_getLogs range/response-size limit, safe to retry smaller."""


def is_explicit_log_range_error(code: int, message: str) -> bool:
    if code >= 0:
        return False
    normalized = message.casefold()
    return any(marker in normalized for marker in (
        "range limit", "range is too large", "range too large", "block range exceeds",
        "maximum range", "maximum block range", "max range", "max block range",
        "too many results", "response size",
        "query returned more than", "please limit the query",
    ))


def open_rpc_request(request: urllib.request.Request, timeout: int):
    return RPC_OPENER.open(request, timeout=timeout)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_from_unix(value: int | str | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def parse_url_list(env_name: str, default_urls: list[str]) -> list[str]:
    if env_name == "BASE_RPC_URL" and os.environ.get("BASE_RPC_URL"):
        return [os.environ["BASE_RPC_URL"]]
    raw = os.environ.get(env_name)
    if not raw:
        return list(default_urls)
    urls = [item.strip() for item in raw.split(",") if item.strip()]
    return urls or list(default_urls)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise RuntimeError(f"{name} must be a boolean value")


def dedupe_urls(urls: Iterable[str]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        url = str(raw or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        selected.append(url)
    return selected


def rpc_urls() -> list[str]:
    single = os.environ.get("BASE_RPC_URL", "").strip()
    listed = os.environ.get("BASE_RPC_URLS", "").strip()
    explicit = bool(single or listed)
    urls = [single] if single else ([item.strip() for item in listed.split(",") if item.strip()] if listed else [])
    if not explicit or env_bool("BASE_INCLUDE_PUBLIC_FALLBACKS", False):
        urls.extend(DEFAULT_RPC_URLS)
    return dedupe_urls(urls)


def log_rpc_urls() -> list[str]:
    single = os.environ.get("BASE_RPC_URL", "").strip()
    listed = os.environ.get("BASE_LOG_RPC_URLS", "").strip()
    explicit = bool(single or listed)
    urls = [single] if single else ([item.strip() for item in listed.split(",") if item.strip()] if listed else [])
    if not explicit or env_bool("BASE_INCLUDE_PUBLIC_FALLBACKS", False):
        urls.extend(DEFAULT_LOG_RPC_URLS)
    return dedupe_urls(urls)


def rpc_provider_key(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or url).lower().strip(".")
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in ("quicknode.pro", "quiknode.pro")):
        return "quicknode"
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in ("alchemy.com", "alchemyapi.io")):
        return "alchemy"
    for suffix in (
        "base.org",
        "publicnode.com",
        "ankr.com",
        "blastapi.io",
        "drpc.org",
        "infura.io",
        "1rpc.io",
        "tenderly.co",
        "lava.build",
    ):
        if host == suffix or host.endswith(f".{suffix}"):
            return suffix
    return host


def independent_rpc_urls(urls: list[str]) -> list[str]:
    selected: list[str] = []
    operators: set[str] = set()
    for raw in urls:
        url = str(raw or "").strip()
        if not url:
            continue
        operator = rpc_provider_key(url)
        if operator in operators:
            continue
        operators.add(operator)
        selected.append(url)
    return selected


def redact_url(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except (TypeError, ValueError):
        return "<redacted-url>"
    hostname = (parts.hostname or "").lower().rstrip(".")
    if not hostname:
        return "<redacted-url>"
    # Hash every host rather than maintaining a vendor list: custom providers
    # can place credentials in subdomains just as easily as in paths/queries.
    host_label = hashlib.sha256(hostname.encode("utf-8")).hexdigest()[:12]
    netloc = f"rpc-host-{host_label}"
    if port:
        netloc += f":{port}"
    path = "/<redacted-path>" if parts.path and parts.path != "/" else ("/" if parts.path == "/" else "")
    query = "redacted=1" if parts.query else ""
    return urllib.parse.urlunsplit(("https", netloc, path, query, ""))


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


def parse_retry_after(value: Any, *, now: datetime | None = None) -> float | None:
    if not isinstance(value, str) or len(value) > MAX_RETRY_AFTER_HEADER_CHARS:
        return None
    raw = value.strip(" \t")
    if not raw:
        return None
    if raw.isascii() and raw.isdigit():
        return float(min(int(raw), int(MAX_RETRY_AFTER_SECONDS)))
    date_format = next(
        (index for index, pattern in enumerate(_RETRY_AFTER_HTTP_DATE_PATTERNS) if pattern.fullmatch(raw)),
        None,
    )
    if date_format is None:
        return None
    try:
        retry_at = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        if date_format != 2:
            return None
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return None
    delay = (retry_at.astimezone(timezone.utc) - current.astimezone(timezone.utc)).total_seconds()
    return min(max(delay, 0.0), MAX_RETRY_AFTER_SECONDS)


def post_json(url: str, payload: Any, *, timeout: int = 60) -> Any:
    validate_rpc_url(url)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "degen-dogs-mission3-archive/0.1",
        },
        method="POST",
    )
    max_bytes = max(
        1024 * 1024,
        min(int(os.environ.get("BASE_RPC_MAX_RESPONSE_BYTES", str(DEFAULT_RPC_MAX_RESPONSE_BYTES))), 128 * 1024 * 1024),
    )
    is_log_request = isinstance(payload, dict) and payload.get("method") == "eth_getLogs"
    rate_limit_error: RpcRateLimited | None = None
    range_limit_error: RpcLogRangeLimit | None = None
    try:
        response = open_rpc_request(req, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            try:
                retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            except Exception:  # noqa: BLE001 - provider-controlled headers are never diagnostic output
                retry_after = None
            rate_limit_error = RpcRateLimited(retry_after)
        elif exc.code == 413 and is_log_request:
            range_limit_error = RpcLogRangeLimit("eth_getLogs provider range/response limit")
        else:
            raise RuntimeError(f"HTTP {exc.code}") from None
    except Exception as exc:  # noqa: BLE001 - never expose credential-bearing URLs in transport errors
        raise RuntimeError(f"RPC transport failed ({type(exc).__name__})") from None
    if rate_limit_error is not None:
        raise rate_limit_error
    if range_limit_error is not None:
        raise range_limit_error
    try:
        with response:
            status = response.getcode() if hasattr(response, "getcode") else getattr(response, "status", None)
            if status != 200:
                raise RuntimeError("RPC response returned unexpected HTTP status")
            final_url = str(response.geturl())
            if final_url != url:
                raise RuntimeError("RPC response URL changed unexpectedly")
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise RuntimeError("RPC response has a non-JSON Content-Type")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    parsed_length = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("RPC response has an invalid Content-Length") from exc
                if parsed_length < 0:
                    raise RuntimeError("RPC response has an invalid Content-Length")
                if parsed_length > max_bytes:
                    if is_log_request:
                        raise RpcLogRangeLimit("eth_getLogs provider range/response limit")
                    raise RuntimeError(f"RPC response exceeds {max_bytes} byte limit")
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                if is_log_request:
                    raise RpcLogRangeLimit("eth_getLogs provider range/response limit")
                raise RuntimeError(f"RPC response exceeds {max_bytes} byte limit")
            text = raw.decode("utf-8", errors="strict")
    except RuntimeError:
        raise
    except UnicodeDecodeError as exc:
        raise RuntimeError("RPC response is not valid UTF-8") from exc
    except Exception as exc:  # noqa: BLE001 - keep provider-controlled read details out of logs
        raise RuntimeError(f"RPC response read failed ({type(exc).__name__})") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("RPC response is not valid JSON") from exc


def rpc_call(method: str, params: list[Any], *, urls: list[str] | None = None, timeout: int = 60) -> tuple[Any, str]:
    active_urls = urls or rpc_urls()
    errors: list[str] = []
    for url in active_urls:
        try:
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            data = post_json(url, payload, timeout=timeout)
            if (
                not isinstance(data, dict)
                or data.get("jsonrpc") != "2.0"
                or type(data.get("id")) is not int
                or data.get("id") != 1
            ):
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
                if method == "eth_getLogs" and is_explicit_log_range_error(error["code"], error["message"]):
                    raise RpcLogRangeLimit("eth_getLogs provider range/response limit")
                raise RuntimeError(f"JSON-RPC error code={error['code']}")
            return data["result"], url
        except RpcLogRangeLimit as exc:
            if len(active_urls) == 1:
                raise
            errors.append(f"{redact_url(url)}: {exc}")
        except RpcRateLimited as exc:
            if len(active_urls) == 1:
                raise
            errors.append(f"{redact_url(url)}: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{redact_url(url)}: {exc}")
    raise RuntimeError(f"RPC {method} failed: {'; '.join(errors)}")


def validated_rpc_batch_items(data: Any, call_count: int) -> dict[int, dict[str, Any]]:
    if not isinstance(data, list):
        raise RuntimeError("invalid JSON-RPC batch response envelope")
    by_id: dict[int, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise RuntimeError("JSON-RPC batch response contains a non-object item")
        request_id = item.get("id")
        if type(request_id) is not int or request_id < 0 or request_id >= call_count:
            raise RuntimeError("JSON-RPC batch response contains an invalid id")
        if request_id in by_id:
            raise RuntimeError("JSON-RPC batch response contains a duplicate id")
        if item.get("jsonrpc") != "2.0":
            raise RuntimeError("invalid JSON-RPC batch response envelope")
        has_result = "result" in item
        has_error = "error" in item
        if has_result == has_error:
            raise RuntimeError("invalid JSON-RPC batch response envelope")
        if has_error:
            error = item.get("error")
            if (
                not isinstance(error, dict)
                or type(error.get("code")) is not int
                or not isinstance(error.get("message"), str)
            ):
                raise RuntimeError("invalid JSON-RPC batch error envelope")
        by_id[request_id] = item
    if set(by_id) != set(range(call_count)):
        raise RuntimeError("JSON-RPC batch response has incomplete ids")
    return by_id


def rpc_batch(calls: list[tuple[str, list[Any]]], *, urls: list[str] | None = None, timeout: int = 120) -> list[Any]:
    if not calls:
        return []
    active_urls = urls or rpc_urls()
    payload = [
        {"jsonrpc": "2.0", "id": idx, "method": method, "params": params}
        for idx, (method, params) in enumerate(calls)
    ]
    errors: list[str] = []
    for url in active_urls:
        try:
            data = post_json(url, payload, timeout=timeout)
            by_id = validated_rpc_batch_items(data, len(calls))
            results: list[Any] = []
            for idx, (method, params) in enumerate(calls):
                item = by_id.get(idx)
                if not item or "error" in item:
                    result, _ = rpc_call(method, params, urls=active_urls, timeout=timeout)
                    results.append(result)
                else:
                    results.append(item.get("result"))
            return results
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{redact_url(url)}: {exc}")
    raise RuntimeError(f"RPC batch failed: {'; '.join(errors)}")


def canonical_hex_quantity(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) <= 2:
        raise RuntimeError(f"invalid {field} quantity")
    try:
        number = int(value, 16)
    except ValueError as exc:
        raise RuntimeError(f"invalid {field} quantity") from exc
    if number < 0:
        raise RuntimeError(f"invalid {field} quantity")
    return hex(number)


def canonical_hash(value: Any, field: str) -> str:
    normalized = str(value or "").lower()
    if len(normalized) != 66 or not normalized.startswith("0x"):
        raise RuntimeError(f"invalid {field} hash")
    try:
        int(normalized[2:], 16)
    except ValueError as exc:
        raise RuntimeError(f"invalid {field} hash") from exc
    return normalized


def canonical_address(value: Any, field: str = "address") -> str:
    normalized = str(value or "").lower()
    if len(normalized) != 42 or not normalized.startswith("0x"):
        raise RuntimeError(f"invalid {field}")
    try:
        int(normalized[2:], 16)
    except ValueError as exc:
        raise RuntimeError(f"invalid {field}") from exc
    return normalized


def canonical_data(value: Any) -> str:
    normalized = str(value or "").lower()
    if not normalized.startswith("0x") or len(normalized) % 2:
        raise RuntimeError("invalid log data")
    try:
        int(normalized[2:] or "0", 16)
    except ValueError as exc:
        raise RuntimeError("invalid log data") from exc
    return normalized


def canonical_block(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid block response: {value!r}")
    return {
        "number": canonical_hex_quantity(value.get("number"), "block number"),
        "hash": canonical_hash(value.get("hash"), "block"),
        "parentHash": canonical_hash(value.get("parentHash"), "parent block"),
        "timestamp": canonical_hex_quantity(value.get("timestamp"), "block timestamp"),
    }


def canonical_logs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RuntimeError(f"invalid eth_getLogs response: {value!r}")
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError("eth_getLogs response contains a non-object entry")
        topics = item.get("topics")
        if not isinstance(topics, list) or not topics:
            raise RuntimeError("eth_getLogs response contains invalid topics")
        removed = item.get("removed", False)
        if not isinstance(removed, bool):
            raise RuntimeError("eth_getLogs response contains invalid removed flag")
        rows.append({
            "address": canonical_address(item.get("address")),
            "blockHash": canonical_hash(item.get("blockHash"), "log block"),
            "blockNumber": canonical_hex_quantity(item.get("blockNumber"), "log block number"),
            "data": canonical_data(item.get("data")),
            "logIndex": canonical_hex_quantity(item.get("logIndex"), "log index"),
            "removed": removed,
            "topics": [canonical_hash(topic, "log topic") for topic in topics],
            "transactionHash": canonical_hash(item.get("transactionHash"), "transaction"),
            "transactionIndex": canonical_hex_quantity(item.get("transactionIndex"), "transaction index"),
        })
    rows.sort(key=lambda row: (hex_int(row["blockNumber"]), row["transactionHash"], hex_int(row["logIndex"])))
    return rows


def rpc_consensus(
    method: str,
    params: list[Any],
    *,
    urls: list[str] | None = None,
    timeout: int = 60,
    normalizer: Any | None = None,
) -> tuple[Any, list[str]]:
    active_urls = independent_rpc_urls(rpc_urls() if urls is None else urls)
    required = max(2, min(int(os.environ.get("BASE_RPC_QUORUM_SIZE", "2")), 3))
    if len(active_urls) < required:
        raise RuntimeError(f"RPC {method} requires {required} independent providers, found {len(active_urls)}")
    normalize = normalizer or (lambda value: value)
    votes: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    errors: list[str] = []
    range_limit_errors = 0
    deadline_seconds = max(
        1.0,
        min(float(os.environ.get("BASE_RPC_QUORUM_DEADLINE_SECONDS", "35")), float(timeout)),
    )
    deadline = time.monotonic() + deadline_seconds
    attempts = max(1, min(int(os.environ.get("BASE_RPC_ATTEMPTS", "2")), 3))

    def call(url: str) -> tuple[str, Any]:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                result, _ = rpc_call(method, params, urls=[url], timeout=max(1.0, min(float(timeout), remaining)))
                return url, result
            except RpcLogRangeLimit:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                remaining = deadline - time.monotonic()
                if attempt + 1 >= attempts:
                    break
                retry_hint = exc.retry_after_seconds if isinstance(exc, RpcRateLimited) else None
                delay = max(0.25 * (2**attempt), retry_hint or 0.0)
                if delay + 0.05 > remaining:
                    break
                time.sleep(delay)
        raise RuntimeError(str(last_error or "unknown RPC failure"))

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(active_urls))
    pending = {pool.submit(call, url): url for url in active_urls}
    try:
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _not_done = concurrent.futures.wait(
                pending,
                timeout=remaining,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                break
            for future in done:
                url = pending.pop(future)
                try:
                    used_url, result = future.result()
                    normalized = normalize(result)
                    vote_key = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
                    votes[vote_key].append((used_url, result))
                except RpcLogRangeLimit as exc:
                    range_limit_errors += 1
                    errors.append(f"{redact_url(url)}: {exc}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{redact_url(url)}: {exc}")

            ranked = sorted(votes.values(), key=len, reverse=True)
            top = len(ranked[0]) if ranked else 0
            second = len(ranked[1]) if len(ranked) > 1 else 0
            # Return early only when no outstanding provider could tie or
            # overtake the current winner. This preserves unique quorum while
            # preventing a straggler from holding an already-final answer.
            if top >= required and second + len(pending) < top:
                winner = ranked[0]
                return winner[0][1], [url for url, _result in winner]
    finally:
        unresolved_count = len(pending)
        for future in pending:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

    ranked = sorted(votes.values(), key=len, reverse=True)
    winner = ranked[0] if ranked else []
    runner_up_count = len(ranked[1]) if len(ranked) > 1 else 0
    if len(winner) < required or runner_up_count + unresolved_count >= len(winner):
        top_votes = len(winner)
        if method == "eth_getLogs" and range_limit_errors > 0 and top_votes + range_limit_errors >= required:
            raise RpcLogRangeLimit("eth_getLogs range was rejected by the independent RPC quorum")
        counts = sorted((len(group) for group in votes.values()), reverse=True)
        tie = "; ambiguous_or_incomplete_top_vote=1"
        raise RuntimeError(
            f"RPC {method} failed independent quorum {required}; vote_counts={counts}{tie}; "
            f"errors={'; '.join(errors)}"
        )
    return winner[0][1], [url for url, _result in winner]


def keccak256_text(text: str) -> str:
    payload = text.encode("utf-8")
    try:
        from Crypto.Hash import keccak  # type: ignore

        k = keccak.new(digest_bits=256)
        k.update(payload)
        return "0x" + k.hexdigest()
    except Exception:
        pass
    openssl = shutil.which("openssl")
    if openssl:
        try:
            completed = subprocess.run(
                [openssl, "dgst", "-keccak-256"],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            digest = completed.stdout.decode("ascii", errors="strict").strip().rsplit("=", 1)[-1].strip()
            if len(digest) == 64 and all(character in "0123456789abcdefABCDEF" for character in digest):
                return "0x" + digest.lower()
        except Exception:
            pass
    try:
        from eth_hash.auto import keccak  # type: ignore

        return "0x" + keccak(payload).hex()
    except Exception as exc:
        raise RuntimeError("Cannot compute Ethereum Keccak-256 topics. Install pycryptodome or eth-hash.") from exc


def word(data_hex: str, index: int) -> int:
    data = data_hex[2:] if data_hex.startswith("0x") else data_hex
    start = index * 64
    chunk = data[start : start + 64]
    if len(chunk) != 64:
        raise ValueError(f"missing ABI word {index} in data {data_hex}")
    return int(chunk, 16)


def word_address(data_hex: str, index: int) -> str:
    return "0x" + f"{word(data_hex, index):064x}"[-40:]


def topic_uint(topic: str | None) -> int:
    if not topic:
        raise ValueError("missing indexed uint topic")
    return int(topic, 16)


def hex_int(value: str | None, default: int = 0) -> int:
    if value is None:
        return default
    return int(value, 16)


def wei_to_eth_string(amount_raw: int | str) -> str:
    amount = Decimal(str(amount_raw)) / Decimal(10**18)
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def load_configs() -> dict[str, Any]:
    return {
        "chain": load_json(CONFIG_DIR / "mission3_chain.verified.json"),
        "contracts": load_json(CONFIG_DIR / "mission3_contracts.verified.json"),
        "blocks": load_json(CONFIG_DIR / "mission3_blocks.verified.json"),
        "events": load_json(CONFIG_DIR / "mission3_events.verified.json"),
    }


def event_topics(events_config: dict[str, Any]) -> dict[str, str]:
    topics: dict[str, str] = {}
    for event in events_config["events"]:
        computed = keccak256_text(event["signature"])
        expected = str(event["topic0"]).lower()
        if computed.lower() != expected:
            raise RuntimeError(f"topic mismatch for {event['name']}: computed {computed}, config {expected}")
        topics[event["name"]] = expected
    return topics


def verify_config(*, check_rpc: bool = True) -> dict[str, Any]:
    configs = load_configs()
    chain_id = int(configs["chain"]["chain"]["chain_id"])
    if chain_id != 8453:
        raise RuntimeError(f"unexpected chain id in config: {chain_id}")
    topics = event_topics(configs["events"])

    db = sqlite3.connect(":memory:")
    db.executescript((SQL_DIR / "schema.sql").read_text(encoding="utf-8"))
    db.executescript((SQL_DIR / "marts.sql").read_text(encoding="utf-8"))
    db.close()

    rpc_report: dict[str, Any] = {"checked": False}
    if check_rpc:
        chain_hex, agreeing_urls = rpc_consensus(
            "eth_chainId",
            [],
            urls=rpc_urls(),
            normalizer=lambda value: int(str(value), 16),
        )
        live_chain_id = int(chain_hex, 16)
        if live_chain_id != chain_id:
            raise RuntimeError(f"RPC chain mismatch: config={chain_id} rpc={live_chain_id}")
        contract_report: dict[str, int] = {}
        for name, item in configs["contracts"]["contracts"].items():
            code, _ = rpc_consensus(
                "eth_getCode",
                [item["address"], "latest"],
                urls=rpc_urls(),
                normalizer=lambda value: str(value or "").lower(),
            )
            code_bytes = max((len(code or "0x") - 2) // 2, 0)
            if code_bytes <= 0:
                raise RuntimeError(f"contract has no code: {name} {item['address']}")
            contract_report[name] = code_bytes
        rpc_report = {
            "checked": True,
            "chain_id": live_chain_id,
            "rpc_quorum": [redact_url(url) for url in agreeing_urls],
            "contract_code_bytes": contract_report,
        }

    return {"status": "ok", "topics": topics, "rpc": rpc_report}


def block_ranges(start: int, end: int, size: int) -> Iterable[tuple[int, int]]:
    cursor = start
    while cursor <= end:
        hi = min(cursor + size - 1, end)
        yield cursor, hi
        cursor = hi + 1


def log_filter(address: str, topics0: list[str], start: int, end: int) -> dict[str, Any]:
    return {
        "address": address,
        "fromBlock": hex(start),
        "toBlock": hex(end),
        "topics": [topics0],
    }


def fetch_log_range(address: str, topics0: list[str], start: int, end: int, urls: list[str]) -> tuple[tuple[int, int], list[dict[str, Any]], str]:
    try:
        raw_logs, agreeing_urls = rpc_consensus(
            "eth_getLogs",
            [log_filter(address, topics0, start, end)],
            urls=urls,
            timeout=120,
            normalizer=canonical_logs,
        )
    except RpcLogRangeLimit:
        if start >= end:
            raise
        midpoint = (start + end) // 2
        _left_bounds, left_logs, left_source = fetch_log_range(address, topics0, start, midpoint, urls)
        _right_bounds, right_logs, right_source = fetch_log_range(address, topics0, midpoint + 1, end, urls)
        logs = left_logs + right_logs
        logs.sort(key=lambda row: (hex_int(row["blockNumber"]), row["transactionHash"], hex_int(row["logIndex"])))
        return (start, end), logs, ";".join((left_source, right_source))
    logs = canonical_logs(raw_logs)
    expected_address = canonical_address(address)
    expected_topics = {canonical_hash(topic, "configured event topic") for topic in topics0}
    for log in logs:
        block_number = hex_int(log["blockNumber"])
        if log["address"] != expected_address:
            raise RuntimeError(f"eth_getLogs returned an unexpected contract address for range {start}-{end}")
        if not start <= block_number <= end:
            raise RuntimeError(f"eth_getLogs returned block {block_number} outside requested range {start}-{end}")
        if log["topics"][0] not in expected_topics:
            raise RuntimeError(f"eth_getLogs returned an unexpected event topic for range {start}-{end}")
    logs = [log for log in logs if not log["removed"]]
    redacted = "quorum:" + ",".join(redact_url(url) for url in agreeing_urls)
    for log in logs:
        log["__source_rpc"] = redacted
    return (start, end), logs, redacted


def fetch_logs(
    address: str,
    topics0: list[str],
    from_block: int,
    to_block: int,
    *,
    chunk_size: int,
    workers: int,
    urls: list[str] | None = None,
) -> list[dict[str, Any]]:
    if from_block > to_block:
        return []
    ranges = list(block_ranges(from_block, to_block, chunk_size))
    active_urls = urls if urls is not None else log_rpc_urls()
    logs: list[dict[str, Any]] = []
    completed = 0
    print(f"fetching {len(ranges)} log chunks from {from_block} to {to_block} (chunk={chunk_size}, workers={workers})", file=sys.stderr)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    range_iterator = iter(ranges)
    future_map: dict[concurrent.futures.Future[Any], tuple[int, int]] = {}

    def submit_next() -> bool:
        try:
            lo, hi = next(range_iterator)
        except StopIteration:
            return False
        future_map[pool.submit(fetch_log_range, address, topics0, lo, hi, active_urls)] = (lo, hi)
        return True

    for _ in range(min(workers, len(ranges))):
        submit_next()
    try:
        while future_map:
            done, _pending = concurrent.futures.wait(
                future_map,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                future_map.pop(future)
                bounds, rows, source = future.result()
                completed += 1
                logs.extend(rows)
                if completed == 1 or completed == len(ranges) or completed % 25 == 0:
                    print(f"  log chunks {completed}/{len(ranges)} latest={bounds[0]}-{bounds[1]} rows={len(rows)} rpc={source}", file=sys.stderr)
                submit_next()
    except Exception:
        for future in future_map:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    logs.sort(key=lambda item: (hex_int(item.get("blockNumber")), hex_int(item.get("logIndex"))))
    return logs


def fetch_canonical_blocks(blocks: Iterable[int], *, urls: list[str] | None = None) -> dict[int, dict[str, str]]:
    ordered = sorted(set(int(block) for block in blocks))
    out: dict[int, dict[str, str]] = {}
    if not ordered:
        return out

    def fetch(block: int) -> tuple[int, dict[str, str]]:
        result, _agreeing_urls = rpc_consensus(
            "eth_getBlockByNumber",
            [hex(block), False],
            urls=rpc_urls() if urls is None else urls,
            timeout=120,
            normalizer=canonical_block,
        )
        canonical = canonical_block(result)
        if int(canonical["number"], 16) != block:
            raise RuntimeError(f"block response number mismatch: requested={block} got={canonical['number']}")
        if not utc_from_unix(int(canonical["timestamp"], 16)):
            raise RuntimeError(f"block {block} has no valid timestamp")
        return block, canonical

    worker_count = min(8, len(ordered))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
    block_iterator = iter(ordered)
    future_map: dict[concurrent.futures.Future[Any], int] = {}

    def submit_next() -> bool:
        try:
            block = next(block_iterator)
        except StopIteration:
            return False
        future_map[pool.submit(fetch, block)] = block
        return True

    for _ in range(worker_count):
        submit_next()
    try:
        while future_map:
            done, _pending = concurrent.futures.wait(
                future_map,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                future_map.pop(future)
                block, canonical = future.result()
                out[block] = canonical
                submit_next()
    except Exception:
        for future in future_map:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    return out


def fetch_block_times(blocks: Iterable[int]) -> dict[int, str]:
    return {
        block: utc_from_unix(int(canonical["timestamp"], 16)) or ""
        for block, canonical in fetch_canonical_blocks(blocks).items()
    }


def absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def path_lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def open_directory_path(path: Path, *, create: bool) -> int:
    """Open/create an absolute directory path without following mutable symlinks."""

    path = absolute_path(path)
    nofollow_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    follow_flags = nofollow_flags & ~getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open("/", nofollow_flags)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, nofollow_flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise RuntimeError(f"required directory does not exist: {path}") from None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    # A concurrent creator must still pass the no-follow open.
                    pass
                try:
                    child = os.open(component, nofollow_flags, dir_fd=descriptor)
                except OSError as exc:
                    raise RuntimeError(f"could not securely create directory path: {path}") from exc
            except OSError as exc:
                try:
                    link_state = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                    parent_state = os.fstat(descriptor)
                except OSError:
                    raise RuntimeError(f"could not securely traverse directory path: {path}") from exc
                # macOS exposes immutable root-owned aliases such as /var ->
                # /private/var. They cannot be replaced by an unprivileged
                # process; user-owned or writable-parent symlinks are rejected.
                trusted_system_alias = (
                    stat.S_ISLNK(link_state.st_mode)
                    and link_state.st_uid == 0
                    and parent_state.st_uid == 0
                    and not (parent_state.st_mode & 0o022)
                )
                if not trusted_system_alias:
                    raise RuntimeError(f"refusing symlink ancestor in publication path: {path}") from exc
                try:
                    child = os.open(component, follow_flags, dir_fd=descriptor)
                except OSError as follow_error:
                    raise RuntimeError(f"could not traverse trusted system directory alias: {path}") from follow_error
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def secure_directory(path: Path, *, create: bool = False, private: bool = False) -> None:
    path = absolute_path(path)
    descriptor = open_directory_path(path, create=create)
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISDIR(current.st_mode):
            raise RuntimeError(f"publication path is not a real directory: {path}")
        if current.st_uid != os.getuid():
            raise RuntimeError(f"publication directory is not owned by the current user: {path}")
        if current.st_mode & 0o022:
            raise RuntimeError(f"publication directory is group/world writable: {path}")
        if private:
            os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def secure_regular_file(path: Path, *, private: bool = False, sync: bool = False) -> None:
    path = absolute_path(path)
    before = path_lstat(path)
    if before is None:
        raise RuntimeError(f"required publication file does not exist: {path}")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"publication path is not a regular file: {path}")
    if before.st_uid != os.getuid():
        raise RuntimeError(f"publication file is not owned by the current user: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError(f"publication file changed during validation: {path}")
        if private:
            os.fchmod(descriptor, 0o600)
        if sync:
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    secure_directory(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute_path(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def harden_private_directory_tree(path: Path) -> None:
    path = absolute_path(path)
    candidates = [path, *path.rglob("*")]
    for candidate in candidates:
        current = path_lstat(candidate)
        if current is None or stat.S_ISLNK(current.st_mode) or current.st_uid != os.getuid():
            raise RuntimeError(f"unsafe path in private publication directory: {candidate}")
        if stat.S_ISDIR(current.st_mode):
            secure_directory(candidate, private=True)
        elif stat.S_ISREG(current.st_mode):
            secure_regular_file(candidate, private=True)
        else:
            raise RuntimeError(f"unsupported file type in private publication directory: {candidate}")


def validate_publication_target(path: Path, *, directory: bool, private: bool = False) -> None:
    path = absolute_path(path)
    secure_directory(path.parent, create=True)
    existing = path_lstat(path)
    if existing is None:
        return
    if stat.S_ISLNK(existing.st_mode):
        raise RuntimeError(f"refusing symlinked publication target: {path}")
    if existing.st_uid != os.getuid():
        raise RuntimeError(f"publication target is not owned by the current user: {path}")
    if directory:
        if not stat.S_ISDIR(existing.st_mode):
            raise RuntimeError(f"publication target is not a directory: {path}")
        secure_directory(path, private=private)
        if private:
            harden_private_directory_tree(path)
    else:
        if not stat.S_ISREG(existing.st_mode):
            raise RuntimeError(f"publication target is not a regular file: {path}")
        secure_regular_file(path, private=private)


def remove_owned_path(path: Path) -> None:
    path = absolute_path(path)
    existing = path_lstat(path)
    if existing is None:
        return
    if stat.S_ISLNK(existing.st_mode) or existing.st_uid != os.getuid():
        raise RuntimeError(f"refusing to remove unsafe publication path: {path}")
    if stat.S_ISDIR(existing.st_mode):
        shutil.rmtree(path)
    elif stat.S_ISREG(existing.st_mode):
        path.unlink()
    else:
        raise RuntimeError(f"refusing to remove unsupported publication path: {path}")


def init_db(path: Path, *, full_refresh: bool) -> sqlite3.Connection:
    path = absolute_path(path)
    validate_publication_target(path, directory=False, private=True)
    if full_refresh and path.exists():
        raise RuntimeError("refusing to destructively replace an existing archive database")
    conn = sqlite3.connect(path)
    path.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.executescript((SQL_DIR / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    return conn


def create_full_refresh_db_path(destination: Path) -> Path:
    destination = absolute_path(destination)
    validate_publication_target(destination, directory=False, private=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{destination.name}.refresh-",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    path = Path(raw_path)
    path.chmod(0o600)
    return path


def create_staged_database_path(destination: Path, *, seed_existing: bool) -> Path:
    destination = absolute_path(destination)
    staged = create_full_refresh_db_path(destination)
    if not seed_existing or path_lstat(destination) is None:
        return staged
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        secure_regular_file(destination, private=True)
        quoted = urllib.parse.quote(str(destination), safe="/")
        source = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True)
        target = sqlite3.connect(staged)
        source.backup(target)
        target.commit()
        target.close()
        target = None
        source.close()
        source = None
        secure_regular_file(staged, private=True, sync=True)
        return staged
    except Exception:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        if path_lstat(staged) is not None:
            remove_owned_path(staged)
        raise


def validate_database_for_replacement(conn: sqlite3.Connection) -> None:
    integrity = conn.execute("PRAGMA quick_check").fetchone()
    if not integrity or str(integrity[0]).lower() != "ok":
        raise RuntimeError(f"archive database quick_check failed: {integrity!r}")
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise RuntimeError(f"archive database foreign-key check failed: {foreign_keys[:3]!r}")
    state = conn.execute(
        "SELECT status FROM mission3_index_state WHERE id = ?",
        (STATE_ID,),
    ).fetchone()
    if not state or state[0] != "success":
        raise RuntimeError("replacement archive database does not have successful index state")
    unresolved = conn.execute(
        "SELECT COUNT(*) FROM mission3_index_gaps WHERE status != 'resolved'"
    ).fetchone()
    if not unresolved or int(unresolved[0]) != 0:
        raise RuntimeError("replacement archive database contains unresolved index gaps")
    # Force materialization of the core publication views before replacing the
    # last-known-good database.
    conn.execute("SELECT COUNT(*) FROM mission3_archive_metrics").fetchone()
    conn.execute("SELECT COUNT(*) FROM mission3_auction_timeline").fetchone()


def atomic_replace_database(source: Path, destination: Path) -> None:
    atomic_publish([
        PublicationEntry(source, destination, directory=False, private=True),
    ])


def record_state(
    conn: sqlite3.Connection,
    *,
    chain_id: int,
    auction_house: str,
    from_block: int,
    latest_indexed_block: int | None,
    latest_indexed_block_time_utc: str | None,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO mission3_index_state (
          id, chain_id, auction_house, from_block, latest_indexed_block,
          latest_indexed_block_time_utc, latest_run_at_utc, status, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            STATE_ID,
            chain_id,
            auction_house.lower(),
            from_block,
            latest_indexed_block,
            latest_indexed_block_time_utc,
            utc_now(),
            status,
            error,
        ),
    )
    conn.commit()


def get_latest_indexed_block(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT latest_indexed_block FROM mission3_index_state WHERE id = ?",
        (STATE_ID,),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0])


def record_gap(conn: sqlite3.Connection, start: int, end: int, reason: str, status: str = "open") -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO mission3_index_gaps
        (from_block, to_block, reason, status, created_at_utc, resolved_at_utc)
        VALUES (?, ?, ?, ?, COALESCE((SELECT created_at_utc FROM mission3_index_gaps WHERE from_block=? AND to_block=? AND reason=?), ?), NULL)
        """,
        (start, end, reason[:500], status, start, end, reason[:500], utc_now()),
    )
    conn.commit()


def resolve_covered_gaps(conn: sqlite3.Connection, start: int, end: int) -> None:
    if start > end:
        return
    conn.execute(
        """
        UPDATE mission3_index_gaps
        SET status = 'resolved', resolved_at_utc = ?
        WHERE status = 'open' AND from_block >= ? AND to_block <= ?
        """,
        (utc_now(), start, end),
    )
    conn.commit()


def insert_raw_logs(
    conn: sqlite3.Connection,
    logs: list[dict[str, Any]],
    chain_id: int,
    fetched_at: str,
    *,
    commit: bool = True,
) -> None:
    rows: list[tuple[Any, ...]] = []
    for log in logs:
        topics = [str(topic).lower() for topic in log.get("topics", [])]
        padded = topics + [None] * (4 - len(topics))
        rows.append(
            (
                chain_id,
                str(log.get("address") or "").lower(),
                hex_int(log.get("blockNumber")),
                str(log.get("blockHash") or "").lower(),
                str(log.get("transactionHash") or "").lower(),
                hex_int(log.get("transactionIndex")),
                hex_int(log.get("logIndex")),
                int(bool(log.get("removed", False))),
                padded[0],
                padded[1],
                padded[2],
                padded[3],
                str(log.get("data") or "0x").lower(),
                fetched_at,
                str(log.get("__source_rpc") or "<unknown>"),
            )
        )
    conn.executemany(
        """
        INSERT OR REPLACE INTO mission3_raw_logs (
          chain_id, address, block_number, block_hash, transaction_hash,
          transaction_index, log_index, removed, topic0, topic1, topic2, topic3,
          data, fetched_at_utc, source_rpc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    if commit:
        conn.commit()


def decode_and_insert(
    conn: sqlite3.Connection,
    logs: list[dict[str, Any]],
    topics_by_name: dict[str, str],
    *,
    commit: bool = True,
    canonical_rpc_urls: list[str] | None = None,
) -> dict[str, int]:
    canonical_blocks = fetch_canonical_blocks(
        (hex_int(log.get("blockNumber")) for log in logs),
        urls=canonical_rpc_urls,
    )
    block_times = {
        block: utc_from_unix(int(canonical["timestamp"], 16)) or ""
        for block, canonical in canonical_blocks.items()
    }
    created: list[tuple[Any, ...]] = []
    bids: list[tuple[Any, ...]] = []
    extended: list[tuple[Any, ...]] = []
    settled: list[tuple[Any, ...]] = []
    topic_to_name = {topic.lower(): name for name, topic in topics_by_name.items()}

    for log in logs:
        topics = [str(topic).lower() for topic in log.get("topics", [])]
        if not topics:
            continue
        name = topic_to_name.get(topics[0])
        if not name:
            continue
        block_number = hex_int(log.get("blockNumber"))
        canonical = canonical_blocks.get(block_number)
        if canonical is None or str(log.get("blockHash") or "").lower() != canonical["hash"]:
            raise RuntimeError(
                f"log block hash disagrees with canonical block quorum at block {block_number}"
            )
        tx_hash = str(log.get("transactionHash") or "").lower()
        log_index = hex_int(log.get("logIndex"))
        block_time = block_times.get(block_number)
        data = str(log.get("data") or "0x")

        if name == "AuctionCreated":
            created.append((topic_uint(topics[1] if len(topics) > 1 else None), word(data, 0), word(data, 1), block_number, tx_hash, log_index, block_time))
        elif name == "AuctionBid":
            amount = word(data, 1)
            bids.append((topic_uint(topics[1] if len(topics) > 1 else None), word_address(data, 0), str(amount), wei_to_eth_string(amount), int(bool(word(data, 2))), block_number, tx_hash, log_index, block_time))
        elif name == "AuctionExtended":
            extended.append((topic_uint(topics[1] if len(topics) > 1 else None), word(data, 0), block_number, tx_hash, log_index, block_time))
        elif name == "AuctionSettled":
            amount = word(data, 1)
            settled.append((topic_uint(topics[1] if len(topics) > 1 else None), word_address(data, 0), str(amount), wei_to_eth_string(amount), block_number, tx_hash, log_index, block_time))

    conn.executemany(
        """
        INSERT OR REPLACE INTO mission3_auction_created
        (token_id, start_time, end_time, block_number, transaction_hash, log_index, block_time_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        created,
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO mission3_auction_bids
        (token_id, bidder, amount_raw, amount_eth, extended, block_number, transaction_hash, log_index, block_time_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        bids,
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO mission3_auction_extended
        (token_id, end_time, block_number, transaction_hash, log_index, block_time_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        extended,
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO mission3_auction_settled
        (token_id, winner, amount_raw, amount_eth, block_number, transaction_hash, log_index, block_time_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        settled,
    )
    if commit:
        conn.commit()
    return {
        "created": len(created),
        "bids": len(bids),
        "extended": len(extended),
        "settled": len(settled),
    }


def fetch_current_auction(
    conn: sqlite3.Connection,
    auction_house: str,
    snapshot: dict[str, str],
    *,
    urls: list[str] | None = None,
) -> None:
    latest_block = int(snapshot["number"], 16)
    raw, agreeing_urls = rpc_consensus(
        "eth_call",
        [
            {"to": auction_house, "data": SELECTOR_AUCTION},
            {"blockHash": snapshot["hash"], "requireCanonical": True},
        ],
        urls=rpc_urls() if urls is None else urls,
        normalizer=lambda value: str(value or "").lower(),
    )
    if not utc_from_unix(int(snapshot["timestamp"], 16)):
        raise RuntimeError(f"missing canonical timestamp for archive snapshot block {latest_block}")
    token_id = word(raw, 0)
    amount_raw = word(raw, 1)
    start_time = word(raw, 2)
    end_time = word(raw, 3)
    highest_bidder = word_address(raw, 4)
    settled = int(word(raw, 5))
    conn.execute(
        """
        INSERT OR REPLACE INTO mission3_current_auction_snapshots
        (snapshot_at_utc, latest_block, token_id, start_time, end_time, highest_bidder, amount_raw, amount_eth, settled, source, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            latest_block,
            token_id,
            start_time,
            end_time,
            highest_bidder.lower(),
            str(amount_raw),
            wei_to_eth_string(amount_raw),
            settled,
            "quorum:" + ",".join(redact_url(url) for url in agreeing_urls),
            "cross_provider_verified_contract_call",
        ),
    )
    conn.commit()


def purge_indexed_range(conn: sqlite3.Connection, chain_id: int, from_block: int, to_block: int) -> None:
    """Remove a re-fetched overlap so orphaned logs/events cannot survive."""
    conn.execute(
        "DELETE FROM mission3_raw_logs WHERE chain_id = ? AND block_number BETWEEN ? AND ?",
        (chain_id, from_block, to_block),
    )
    for table in (
        "mission3_auction_created",
        "mission3_auction_bids",
        "mission3_auction_extended",
        "mission3_auction_settled",
    ):
        conn.execute(f'DELETE FROM "{table}" WHERE block_number BETWEEN ? AND ?', (from_block, to_block))
    conn.execute(
        "DELETE FROM mission3_current_auction_snapshots WHERE latest_block BETWEEN ? AND ?",
        (from_block, to_block),
    )


def incremental_reorg_window(
    configured_from_block: int,
    previous_latest_indexed: int | None,
    target_block: int,
    overlap_blocks: int,
) -> tuple[int, int]:
    if previous_latest_indexed is None:
        return configured_from_block, target_block
    anchor = min(previous_latest_indexed, target_block)
    from_block = max(configured_from_block, anchor - overlap_blocks + 1)
    # When the safe head regresses, delete the old tail as well as replacing
    # the configured overlap so no future/orphaned rows survive publication.
    purge_to_block = max(previous_latest_indexed, target_block)
    return from_block, purge_to_block


def configured_log_chunk_size() -> int:
    """Return the bounded archive log range requested by the operator."""
    return max(1, min(int(os.environ.get("MISSION3_LOG_CHUNK", "2000")), 10_000))


def apply_marts(conn: sqlite3.Connection) -> None:
    conn.executescript((SQL_DIR / "marts.sql").read_text(encoding="utf-8"))
    conn.commit()


def table_rows(conn: sqlite3.Connection, table: str, *, limit: int | None = None) -> tuple[list[str], list[tuple[Any, ...]]]:
    sql = f'SELECT * FROM "{table}"'
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    cols = [item[0] for item in cur.description]
    rows = [tuple(row) for row in cur.fetchall()]
    return cols, rows


def rows_to_dicts(cols: list[str], rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [dict(zip(cols, row)) for row in rows]


def write_csv_file(path: Path, cols: list[str], rows: list[tuple[Any, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(cols)
        writer.writerows(rows)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_reference(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def write_raw_ndjson(conn: sqlite3.Connection, raw_dir: Path) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / "mission3_raw_logs.ndjson"
    cur = conn.execute("SELECT * FROM mission3_raw_logs ORDER BY block_number, log_index")
    cols = [item[0] for item in cur.description]
    with path.open("w", encoding="utf-8") as handle:
        for row in cur.fetchall():
            handle.write(json.dumps(dict(zip(cols, tuple(row))), sort_keys=True) + "\n")
    return path


class PublicationEntry:
    def __init__(self, source: Path, target: Path, *, directory: bool, private: bool) -> None:
        self.source = absolute_path(source)
        self.target = absolute_path(target)
        self.directory = directory
        self.private = private


class PublicationStage:
    def __init__(self, manifest: dict[str, Any], entries: list[PublicationEntry]) -> None:
        self.manifest = manifest
        self.entries = entries

    def cleanup(self) -> None:
        for entry in reversed(self.entries):
            if path_lstat(entry.source) is not None:
                remove_owned_path(entry.source)


def raw_output_target(output_dir: Path) -> Path:
    output_dir = absolute_path(output_dir)
    if output_dir == absolute_path(DEFAULT_OUTPUT_DIR):
        return absolute_path(DEFAULT_RAW_DIR)
    return output_dir.parent / "raw"


def validate_publication_layout(
    output_dir: Path,
    raw_dir: Path,
    db_path: Path,
    *,
    write_public: bool,
) -> None:
    output_dir = absolute_path(output_dir)
    raw_dir = absolute_path(raw_dir)
    db_path = absolute_path(db_path)
    directory_targets = [output_dir, raw_dir]
    if write_public:
        directory_targets.append(absolute_path(PUBLIC_OUTPUT_DIR))
    for index, left in enumerate(directory_targets):
        for right in directory_targets[index + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise RuntimeError(f"publication directories overlap: {left} and {right}")
    for directory in directory_targets:
        if directory == db_path or directory in db_path.parents:
            raise RuntimeError(f"archive database cannot be inside a published directory: {db_path}")
    validate_publication_target(output_dir, directory=True)
    validate_publication_target(raw_dir, directory=True, private=True)
    if write_public:
        validate_publication_target(absolute_path(PUBLIC_OUTPUT_DIR), directory=True)
    validate_publication_target(db_path, directory=False, private=True)


def create_staging_directory(target: Path) -> Path:
    target = absolute_path(target)
    secure_directory(target.parent, create=True)
    raw_path = tempfile.mkdtemp(prefix=f".{target.name}.publish-", dir=target.parent)
    stage = Path(raw_path)
    stage.chmod(0o700)
    secure_directory(stage, private=True)
    return stage


def sync_publication_tree(root: Path, *, private: bool) -> None:
    root = absolute_path(root)
    paths = [root, *root.rglob("*")]
    directories: list[Path] = []
    for path in paths:
        current = path_lstat(path)
        if current is None or stat.S_ISLNK(current.st_mode) or current.st_uid != os.getuid():
            raise RuntimeError(f"unsafe path in staged publication: {path}")
        if stat.S_ISDIR(current.st_mode):
            path.chmod(0o700 if private else 0o755)
            directories.append(path)
        elif stat.S_ISREG(current.st_mode):
            path.chmod(0o600 if private else 0o644)
            secure_regular_file(path, private=private, sync=True)
        else:
            raise RuntimeError(f"unsupported file type in staged publication: {path}")
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        fsync_directory(directory)


def unused_backup_path(target: Path) -> Path:
    for _ in range(100):
        candidate = target.parent / f".{target.name}.backup-{secrets.token_hex(8)}"
        if path_lstat(candidate) is None:
            return candidate
    raise RuntimeError(f"could not allocate a publication backup path for {target}")


def atomic_publish(entries: list[PublicationEntry]) -> None:
    if not entries:
        return
    targets: set[Path] = set()
    states: list[dict[str, Any]] = []
    for entry in entries:
        if entry.target in targets:
            raise RuntimeError(f"duplicate publication target: {entry.target}")
        targets.add(entry.target)
        if entry.source.parent != entry.target.parent:
            raise RuntimeError("staged publication must be on the target filesystem and in its parent directory")
        validate_publication_target(entry.target, directory=entry.directory, private=entry.private)
        if entry.directory:
            secure_directory(entry.source, private=entry.private)
        else:
            secure_regular_file(entry.source, private=entry.private, sync=True)
        states.append({
            "entry": entry,
            "backup": unused_backup_path(entry.target),
            "had_original": path_lstat(entry.target) is not None,
            "old_moved": False,
            "new_installed": False,
        })

    try:
        for state in states:
            entry = state["entry"]
            if state["had_original"]:
                os.replace(entry.target, state["backup"])
                state["old_moved"] = True
                fsync_directory(entry.target.parent)
            os.replace(entry.source, entry.target)
            state["new_installed"] = True
            fsync_directory(entry.target.parent)
    except Exception as publish_error:
        rollback_errors: list[str] = []
        for state in reversed(states):
            entry = state["entry"]
            try:
                if state["new_installed"]:
                    if path_lstat(entry.source) is not None:
                        raise RuntimeError(f"rollback source unexpectedly exists: {entry.source}")
                    validate_publication_target(entry.target, directory=entry.directory, private=entry.private)
                    os.replace(entry.target, entry.source)
                    state["new_installed"] = False
                if state["old_moved"]:
                    if path_lstat(entry.target) is not None:
                        raise RuntimeError(f"rollback target unexpectedly exists: {entry.target}")
                    os.replace(state["backup"], entry.target)
                    state["old_moved"] = False
                fsync_directory(entry.target.parent)
            except Exception as rollback_error:  # noqa: BLE001
                rollback_errors.append(f"{entry.target}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                "publication failed and rollback was incomplete: " + "; ".join(rollback_errors)
            ) from publish_error
        raise

    cleanup_errors: list[str] = []
    for state in states:
        backup = state["backup"]
        if state["old_moved"] and path_lstat(backup) is not None:
            try:
                remove_owned_path(backup)
                fsync_directory(state["entry"].target.parent)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(f"{backup}: {exc}")
    if cleanup_errors:
        print("warning: committed publication left recoverable backups: " + "; ".join(cleanup_errors), file=sys.stderr)


def render_outputs(
    conn: sqlite3.Connection,
    output_dir: Path,
    raw_dir: Path,
    *,
    logical_output_dir: Path,
    logical_raw_dir: Path,
    db_path: Path,
    write_public: bool,
    public_output_dir: Path | None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = write_raw_ndjson(conn, raw_dir)
    files: list[dict[str, Any]] = []

    for table in CSV_EXPORTS:
        cols, rows = table_rows(conn, table)
        csv_path = output_dir / f"{table}.csv"
        json_path = output_dir / f"{table}.json"
        write_csv_file(csv_path, cols, rows)
        write_json(json_path, rows_to_dicts(cols, rows))
        files.append({"name": table, "type": "csv", "path": artifact_reference(logical_output_dir / csv_path.name), "rows": len(rows), "sha256": sha256_file(csv_path)})
        files.append({"name": table, "type": "json", "path": artifact_reference(logical_output_dir / json_path.name), "rows": len(rows), "sha256": sha256_file(json_path)})

    for table in JSON_LIST_EXPORTS:
        cols, rows = table_rows(conn, table)
        records = rows_to_dicts(cols, rows)
        for record in records:
            if isinstance(record.get("sources"), str):
                record["sources"] = [item for item in str(record["sources"]).split(",") if item]
            if record.get("settled") in (0, 1):
                record["settled"] = bool(record["settled"])
        json_path = output_dir / f"{table}.json"
        write_json(json_path, records)
        files.append({"name": table, "type": "json", "path": artifact_reference(logical_output_dir / json_path.name), "rows": len(records), "sha256": sha256_file(json_path)})

    for table in JSON_OBJECT_EXPORTS:
        cols, rows = table_rows(conn, table)
        metrics = {str(row[0]): row[1] for row in rows}
        metrics["generated_at_utc"] = utc_now()
        json_path = output_dir / f"{table}.json"
        write_json(json_path, metrics)
        files.append({"name": table, "type": "json", "path": artifact_reference(logical_output_dir / json_path.name), "rows": len(metrics), "sha256": sha256_file(json_path)})

    state = conn.execute("SELECT * FROM mission3_index_state WHERE id = ?", (STATE_ID,)).fetchone()
    counts = {row["metric"]: row["value"] for row in conn.execute("SELECT metric, value FROM mission3_archive_metrics")}
    manifest = {
        "schema_version": 1,
        "mission": 3,
        "generated_at_utc": utc_now(),
        "database": artifact_reference(db_path),
        "raw_logs_ndjson": artifact_reference(logical_raw_dir / raw_path.name),
        "raw_logs_sha256": sha256_file(raw_path),
        "index_state": dict(state) if state else None,
        "counts": counts,
        "files": files,
    }
    manifest_path = output_dir / "manifest.json"
    write_json(manifest_path, manifest)

    if write_public:
        if public_output_dir is None:
            raise RuntimeError("public output staging directory was not supplied")
        public_output_dir.mkdir(parents=True, exist_ok=True)
        public_files = {
            "mission3_dog_search_index.json": output_dir / "mission3_dog_search_index.json",
            "mission3_archive_metrics.json": output_dir / "mission3_archive_metrics.json",
        }
        public_manifest_files: list[dict[str, Any]] = []
        file_meta_by_path = {item["path"]: item for item in files}
        for target_name, source_path in public_files.items():
            target_path = public_output_dir / target_name
            shutil.copyfile(source_path, target_path)
            source_rel = artifact_reference(logical_output_dir / source_path.name)
            source_meta = file_meta_by_path.get(source_rel, {})
            public_manifest_files.append({
                "name": source_meta.get("name", target_path.stem),
                "type": "json",
                "path": f"generated/mission3/{target_name}",
                "rows": source_meta.get("rows"),
                "sha256": sha256_file(target_path),
            })
        public_manifest = {
            "schema_version": 1,
            "mission": 3,
            "public": True,
            "generated_at_utc": manifest["generated_at_utc"],
            "index_state": manifest["index_state"],
            "counts": counts,
            "files": public_manifest_files,
        }
        write_json(public_output_dir / "archive_manifest.json", public_manifest)

    return manifest


def validate_staged_outputs(stage: PublicationStage, *, write_public: bool) -> None:
    entries_by_target = {entry.target: entry for entry in stage.entries}
    output_entry = next(entry for entry in stage.entries if not entry.private)
    output_manifest_path = output_entry.source / "manifest.json"
    persisted_manifest = load_json(output_manifest_path)
    if persisted_manifest != stage.manifest:
        raise RuntimeError("staged archive manifest does not match the rendered publication")
    expected_output_names = {".gitkeep", "manifest.json"}
    for item in stage.manifest.get("files", []):
        filename = Path(str(item.get("path") or "")).name
        artifact = output_entry.source / filename
        if not filename or not artifact.is_file() or sha256_file(artifact) != item.get("sha256"):
            raise RuntimeError(f"staged artifact hash mismatch: {filename or '<missing>'}")
        expected_output_names.add(filename)
    actual_output_names = {item.name for item in output_entry.source.iterdir()}
    if actual_output_names != expected_output_names:
        raise RuntimeError("staged generated artifact set is incomplete or contains unexpected files")

    raw_entry = next(entry for entry in stage.entries if entry.private)
    raw_path = raw_entry.source / "mission3_raw_logs.ndjson"
    if sha256_file(raw_path) != stage.manifest.get("raw_logs_sha256"):
        raise RuntimeError("staged raw log hash does not match the archive manifest")
    if {item.name for item in raw_entry.source.iterdir()} != {".gitkeep", raw_path.name}:
        raise RuntimeError("staged raw publication contains unexpected files")

    if write_public:
        public_entry = entries_by_target.get(absolute_path(PUBLIC_OUTPUT_DIR))
        if public_entry is None:
            raise RuntimeError("staged public publication is missing")
        public_manifest = load_json(public_entry.source / "archive_manifest.json")
        if public_manifest.get("generated_at_utc") != stage.manifest.get("generated_at_utc"):
            raise RuntimeError("public and archive manifests describe different generations")
        expected_public_names = {"archive_manifest.json"}
        for item in public_manifest.get("files", []):
            filename = Path(str(item.get("path") or "")).name
            public_artifact = public_entry.source / filename
            generated_artifact = output_entry.source / filename
            expected_hash = item.get("sha256")
            if (
                not filename
                or not public_artifact.is_file()
                or not generated_artifact.is_file()
                or sha256_file(public_artifact) != expected_hash
                or sha256_file(generated_artifact) != expected_hash
            ):
                raise RuntimeError(f"public artifact is incoherent with generated output: {filename or '<missing>'}")
            expected_public_names.add(filename)
        if {item.name for item in public_entry.source.iterdir()} != expected_public_names:
            raise RuntimeError("staged public artifact set is incomplete or contains unexpected files")


def stage_outputs(
    conn: sqlite3.Connection,
    output_dir: Path,
    *,
    db_path: Path,
    write_public: bool,
) -> PublicationStage:
    output_target = absolute_path(output_dir)
    raw_target = raw_output_target(output_target)
    db_target = absolute_path(db_path)
    validate_publication_layout(output_target, raw_target, db_target, write_public=write_public)
    entries: list[PublicationEntry] = []
    try:
        output_stage = create_staging_directory(output_target)
        entries.append(PublicationEntry(output_stage, output_target, directory=True, private=False))
        raw_stage = create_staging_directory(raw_target)
        entries.append(PublicationEntry(raw_stage, raw_target, directory=True, private=True))
        public_stage: Path | None = None
        if write_public:
            public_target = absolute_path(PUBLIC_OUTPUT_DIR)
            public_stage = create_staging_directory(public_target)
            entries.append(PublicationEntry(public_stage, public_target, directory=True, private=False))

        (output_stage / ".gitkeep").write_text("", encoding="utf-8")
        (raw_stage / ".gitkeep").write_text("", encoding="utf-8")
        manifest = render_outputs(
            conn,
            output_stage,
            raw_stage,
            logical_output_dir=output_target,
            logical_raw_dir=raw_target,
            db_path=db_target,
            write_public=write_public,
            public_output_dir=public_stage,
        )
        sync_publication_tree(output_stage, private=False)
        sync_publication_tree(raw_stage, private=True)
        if public_stage is not None:
            sync_publication_tree(public_stage, private=False)
        stage = PublicationStage(manifest, entries)
        validate_staged_outputs(stage, write_public=write_public)
        return stage
    except Exception:
        for entry in reversed(entries):
            if path_lstat(entry.source) is not None:
                remove_owned_path(entry.source)
        raise


def export_outputs(conn: sqlite3.Connection, output_dir: Path, *, db_path: Path, write_public: bool) -> dict[str, Any]:
    stage = stage_outputs(conn, output_dir, db_path=db_path, write_public=write_public)
    try:
        atomic_publish([stage.entries[1], stage.entries[0], *stage.entries[2:]])
        return stage.manifest
    finally:
        stage.cleanup()


def verified_block_snapshot(
    block_number: int,
    *,
    urls: list[str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    block, agreeing_urls = rpc_consensus(
        "eth_getBlockByNumber",
        [hex(block_number), False],
        urls=rpc_urls() if urls is None else urls,
        timeout=60,
        normalizer=canonical_block,
    )
    canonical = canonical_block(block)
    if int(canonical["number"], 16) != block_number:
        raise RuntimeError(
            f"canonical block number mismatch: requested={block_number} got={canonical['number']}"
        )
    return canonical, agreeing_urls


def assert_snapshot_unchanged(snapshot: dict[str, str], *, urls: list[str]) -> None:
    block_number = int(snapshot["number"], 16)
    current, _agreeing_urls = verified_block_snapshot(block_number, urls=urls)
    if current != snapshot:
        raise RuntimeError(
            f"canonical archive snapshot changed during collection at block {block_number}: "
            f"expected_hash={snapshot['hash']} current_hash={current['hash']}"
        )


def verified_safe_snapshot() -> tuple[dict[str, str], list[str]]:
    urls = independent_rpc_urls(rpc_urls())
    required = max(2, min(int(os.environ.get("BASE_RPC_QUORUM_SIZE", "2")), 3))
    max_spread = max(1, min(int(os.environ.get("BASE_RPC_MAX_HEAD_SPREAD_BLOCKS", "20")), 500))
    confirmations = max(1, min(int(os.environ.get("BASE_SNAPSHOT_CONFIRMATIONS", "1")), 64))
    max_block_age = max(30, min(int(os.environ.get("BASE_RPC_MAX_BLOCK_AGE_SECONDS", "600")), 3600))
    if len(urls) < required:
        raise RuntimeError(f"safe archive head requires {required} independent providers, found {len(urls)}")

    heads: list[tuple[str, int]] = []
    errors: list[str] = []
    probe_deadline_seconds = max(
        1.0,
        min(float(os.environ.get("BASE_RPC_HEAD_PROBE_DEADLINE_SECONDS", "12")), 60.0),
    )
    grace_seconds = max(
        0.0,
        min(float(os.environ.get("BASE_RPC_HEAD_PROBE_GRACE_SECONDS", "0.35")), 3.0),
    )
    deadline = time.monotonic() + probe_deadline_seconds

    def fetch(url: str) -> tuple[str, int]:
        remaining = max(1.0, deadline - time.monotonic())
        value, _ = rpc_call("eth_blockNumber", [], urls=[url], timeout=min(30.0, remaining))
        return url, int(value, 16)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(urls))
    pending = {pool.submit(fetch, url): url for url in urls}
    cluster_ready_at: float | None = None
    try:
        while pending:
            now = time.monotonic()
            stop_at = deadline
            if cluster_ready_at is not None:
                stop_at = min(stop_at, cluster_ready_at + grace_seconds)
            remaining = stop_at - now
            if remaining <= 0:
                break
            done, _not_done = concurrent.futures.wait(
                pending,
                timeout=remaining,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                break
            for future in done:
                url = pending.pop(future)
                try:
                    heads.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{redact_url(url)}: {exc}")
            has_cluster = any(
                max(head for _url, head in combination) - min(head for _url, head in combination) <= max_spread
                for combination in itertools.combinations(heads, required)
            )
            if has_cluster and cluster_ready_at is None:
                cluster_ready_at = time.monotonic()
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

    viable = [
        combination
        for combination in itertools.combinations(heads, required)
        if max(head for _url, head in combination) - min(head for _url, head in combination) <= max_spread
    ]
    if not viable:
        reported = sorted((redact_url(url), head) for url, head in heads)
        raise RuntimeError(
            f"independent RPC heads have no {required}-provider cluster within {max_spread} blocks: "
            f"heads={reported}; errors={'; '.join(errors)}"
        )
    candidate_errors: list[str] = []
    ordered_clusters = sorted(
        viable,
        key=lambda group: (min(head for _url, head in group), sum(head for _url, head in group)),
        reverse=True,
    )
    for cluster in ordered_clusters:
        safe_block = min(head for _url, head in cluster) - confirmations
        if safe_block <= 0:
            continue
        cluster_urls = [url for url, _head in cluster]
        try:
            canonical, agreeing_urls = verified_block_snapshot(safe_block, urls=cluster_urls)
            block_time = datetime.fromtimestamp(int(canonical["timestamp"], 16), timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - block_time).total_seconds()
            if age_seconds < -60 or age_seconds > max_block_age:
                raise RuntimeError(
                    f"safe archive head timestamp is stale or future-dated: age_seconds={int(age_seconds)}"
                )
            return canonical, agreeing_urls
        except Exception as exc:  # noqa: BLE001
            candidate_errors.append(
                f"cluster={[redact_url(url) for url in cluster_urls]}: {exc}"
            )
    raise RuntimeError(
        "no independent head cluster agreed on a recent canonical safe block: "
        + "; ".join(candidate_errors)
    )


def verified_safe_head() -> int:
    snapshot, _agreeing_urls = verified_safe_snapshot()
    return int(snapshot["number"], 16)


def resolve_to_block(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    if value.lower() == "latest":
        return verified_safe_head()
    return int(value)


def latest_block_time(block_number: int) -> str | None:
    return fetch_block_times([block_number]).get(block_number)


def run_index(args: argparse.Namespace) -> dict[str, Any]:
    # Fail closed on wrong-chain or missing contract RPCs before opening or
    # mutating the archive database.
    verify_config(check_rpc=True)
    configs = load_configs()
    topics_by_name = event_topics(configs["events"])
    topics0 = list(topics_by_name.values())
    chain_id = int(configs["chain"]["chain"]["chain_id"])
    auction_house = str(configs["contracts"]["contracts"]["auction_house"]["address"])
    configured_from_block = int(configs["blocks"]["indexing"]["verified_from_block"])
    from_block_base = int(args.from_block or os.environ.get("MISSION3_FROM_BLOCK") or configured_from_block)
    requested_to_block = args.to_block or os.environ.get("MISSION3_TO_BLOCK")
    live_target = requested_to_block is None or str(requested_to_block).lower() == "latest"
    if live_target:
        target_snapshot, _target_rpc_urls = verified_safe_snapshot()
        to_block = int(target_snapshot["number"], 16)
    else:
        to_block = int(str(requested_to_block))
        target_snapshot, _target_rpc_urls = verified_block_snapshot(to_block)
    if to_block < from_block_base:
        raise RuntimeError(
            f"archive target block {to_block} is earlier than configured start block {from_block_base}"
        )

    # Bind the log-provider set to the same canonical target hash before any
    # numeric-range eth_getLogs calls. Rechecking both provider sets after the
    # scan makes empty ranges as well as returned event hashes fork-consistent.
    active_state_urls = independent_rpc_urls(rpc_urls())
    active_log_urls = independent_rpc_urls(log_rpc_urls())
    state_target_snapshot, _state_target_urls = verified_block_snapshot(to_block, urls=active_state_urls)
    if state_target_snapshot != target_snapshot:
        raise RuntimeError(
            f"safe-head and state RPC quorums disagree on archive target block {to_block}: "
            f"safe_hash={target_snapshot['hash']} state_hash={state_target_snapshot['hash']}"
        )
    log_target_snapshot, _verified_log_urls = verified_block_snapshot(to_block, urls=active_log_urls)
    if log_target_snapshot != target_snapshot:
        raise RuntimeError(
            f"state and log RPC quorums disagree on archive target block {to_block}: "
            f"state_hash={target_snapshot['hash']} log_hash={log_target_snapshot['hash']}"
        )

    db_path = Path(args.db_path or os.environ.get("MISSION3_ARCHIVE_DB") or DEFAULT_DB).expanduser()
    output_dir = Path(args.output_dir or os.environ.get("MISSION3_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR).expanduser()
    full_refresh = bool(args.full_refresh)
    # Every run works on a private SQLite snapshot. The previous successful DB
    # stays untouched until all canonical checks and artifact exports succeed.
    working_db_path = create_staged_database_path(db_path, seed_existing=not full_refresh)
    conn: sqlite3.Connection | None = None
    publication_stage: PublicationStage | None = None
    try:
        conn = init_db(working_db_path, full_refresh=False)
    except Exception:
        if path_lstat(working_db_path) is not None:
            remove_owned_path(working_db_path)
        raise
    try:
        previous_latest_indexed = get_latest_indexed_block(conn)

        if args.incremental and not full_refresh and not args.from_block and not os.environ.get("MISSION3_FROM_BLOCK"):
            overlap = max(1, min(int(os.environ.get("MISSION3_ARCHIVE_OVERLAP_BLOCKS", "100")), 10_000))
            from_block, purge_to_block = incremental_reorg_window(
                from_block_base,
                previous_latest_indexed,
                to_block,
                overlap,
            )
        else:
            from_block = from_block_base
            purge_to_block = max(to_block, previous_latest_indexed or to_block)

        if previous_latest_indexed is not None and previous_latest_indexed > to_block and not live_target:
            raise RuntimeError(
                f"refusing to move archive state backwards from {previous_latest_indexed} to explicit target {to_block}; "
                "use --full-refresh for an intentional historical rebuild"
            )
        # Base recommends sub-2,000-block public queries, so 2,000 remains the
        # conservative default. Credentialed/archive-capable providers can
        # safely opt into the documented 10,000-block ceiling to reduce a full
        # rebuild from thousands of quorum round trips to hundreds.
        chunk_size = configured_log_chunk_size()
        workers = max(1, min(int(os.environ.get("MISSION3_LOG_WORKERS", "4")), 16))
        record_state(
            conn,
            chain_id=chain_id,
            auction_house=auction_house,
            from_block=from_block_base,
            latest_indexed_block=previous_latest_indexed,
            latest_indexed_block_time_utc=None,
            status="running",
        )
    except Exception:
        conn.close()
        conn = None
        if path_lstat(working_db_path) is not None:
            remove_owned_path(working_db_path)
        raise

    try:
        fetched_at = utc_now()
        logs = fetch_logs(
            auction_house,
            topics0,
            from_block,
            to_block,
            chunk_size=chunk_size,
            workers=workers,
            urls=active_log_urls,
        )
        with conn:
            purge_indexed_range(conn, chain_id, from_block, purge_to_block)
            insert_raw_logs(conn, logs, chain_id, fetched_at, commit=False)
            decoded_counts = decode_and_insert(
                conn,
                logs,
                topics_by_name,
                commit=False,
                canonical_rpc_urls=active_state_urls,
            )
        print(f"decoded current run: {decoded_counts}", file=sys.stderr)

        fetch_current_auction(conn, auction_house, target_snapshot, urls=active_state_urls)
        assert_snapshot_unchanged(target_snapshot, urls=active_state_urls)
        assert_snapshot_unchanged(target_snapshot, urls=active_log_urls)
        latest_time = utc_from_unix(int(target_snapshot["timestamp"], 16))
        if not latest_time:
            raise RuntimeError(f"archive target block {to_block} has no valid canonical timestamp")
        record_state(
            conn,
            chain_id=chain_id,
            auction_house=auction_house,
            from_block=from_block_base,
            latest_indexed_block=to_block,
            latest_indexed_block_time_utc=latest_time,
            status="success",
        )
        resolve_covered_gaps(conn, from_block, to_block)
        apply_marts(conn)
        if full_refresh:
            validate_database_for_replacement(conn)
        publication_stage = stage_outputs(
            conn,
            output_dir,
            db_path=db_path,
            write_public=bool(args.write_public),
        )
        manifest = publication_stage.manifest
        conn.commit()
        conn.close()
        conn = None
        database_entry = PublicationEntry(working_db_path, db_path, directory=False, private=True)
        # Raw data and the DB are installed before generated/public manifests;
        # any failure rolls every completed replacement back in reverse order.
        atomic_publish([
            publication_stage.entries[1],
            database_entry,
            publication_stage.entries[0],
            *publication_stage.entries[2:],
        ])
        print(json.dumps({"status": "success", "from_block": from_block, "to_block": to_block, "run_logs": len(logs), "counts": manifest["counts"]}, indent=2, sort_keys=True))
        return manifest
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)
        if conn is not None:
            record_gap(conn, from_block, to_block, reason, status="open")
            record_state(
                conn,
                chain_id=chain_id,
                auction_house=auction_house,
                from_block=from_block_base,
                latest_indexed_block=previous_latest_indexed,
                latest_indexed_block_time_utc=None,
                status="error",
                error=reason[:1000],
            )
        raise
    finally:
        if conn is not None:
            conn.close()
        if publication_stage is not None:
            publication_stage.cleanup()
        if path_lstat(working_db_path) is not None:
            remove_owned_path(working_db_path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Index Degen Dogs Mission 3 Base auction logs into a local archive.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-only", action="store_true", help="Validate configs, schema, event topics, chain id, and contract code, then exit.")
    mode.add_argument("--full-refresh", action="store_true", help="Rebuild the archive DB from the verified start block or supplied --from-block.")
    mode.add_argument("--incremental", action="store_true", help="Index from latest_indexed_block + 1 when possible. This is the default mode.")
    parser.add_argument("--from-block", type=int, help="Override the Mission 3 archive start block.")
    parser.add_argument("--to-block", help="Override the ending block; integer or 'latest'.")
    parser.add_argument("--db-path", help="Archive SQLite path. Defaults to archive/mission3/data/mission3_archive.sqlite.")
    parser.add_argument("--output-dir", help="Generated output directory. Defaults to archive/mission3/data/generated.")
    parser.add_argument("--write-public", action="store_true", help="Copy small future-ready JSON files to public/generated/mission3/.")
    parser.add_argument("--skip-rpc-check", action="store_true", help="For --verify-only, skip live RPC checks and validate local files only.")
    args = parser.parse_args(argv)
    if not args.verify_only and not args.full_refresh and not args.incremental:
        args.incremental = True
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.verify_only:
        report = verify_config(check_rpc=not args.skip_rpc_check)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    run_index(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
