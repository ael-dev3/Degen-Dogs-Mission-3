#!/usr/bin/env python3
"""Fetch a cached read-only Farcaster channel snapshot for the dashboard.

Direct/open Farcaster infrastructure is preferred. Neynar is intentionally kept
as an opt-in last resort because the generated JSON is served publicly.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "generated"
PUBLIC_GENERATED = ROOT / "public" / "generated"
OUTPUT_FILENAME = "farcaster_degendogs_channel.json"
DEFAULT_CHANNEL_ID = "degendogs"
DEFAULT_CHANNEL_URL = "https://farcaster.xyz/~/channel/degendogs"
DEFAULT_PARENT_URL = "https://warpcast.com/~/channel/degendogs"
DEFAULT_HYPERSNAP_BASE_URL = "https://haatz.quilibrium.com"
NEYNAR_CHANNEL_FEED_URL = "https://api.neynar.com/v2/farcaster/feed/channels/"
SOURCE_PRIORITY = ["hypersnap", "snapchain", "neynar"]
FARCASTER_EPOCH = datetime(2021, 1, 1, tzinfo=timezone.utc)
USER_AGENT = "degen-dogs-mission3-dashboard/1.0"

JsonRequester = Callable[..., dict[str, Any]]


SECRET_PATTERNS = [
    re.compile(r"(?i)\bNEYNAR_API_KEY\s*[=:]\s*[^\s,;]+"),
    re.compile(r"(?i)\bx-api-key\s*[=:]\s*[^\s,;]+"),
    re.compile(r"(?i)\bapi[_-]?key\s*[=:]\s*[^\s,;]+"),
    re.compile(r"(?i)\btoken\s*[=:]\s*[^\s,;]+"),
    re.compile(r"(?i)\bsecret\s*[=:]\s*[^\s,;]+"),
]
URL_PATTERN = re.compile(r"https?://[^\s'\")<>]+")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def env_text(env: Mapping[str, str], key: str, default: str = "") -> str:
    if key in env:
        return str(env.get(key) or "").strip()
    return default


def env_flag(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw_default = "1" if default else "0"
    raw = env_text(env, key, raw_default).lower()
    return raw in {"1", "true", "yes", "on"}


def env_int(env: Mapping[str, str], key: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(env_text(env, key, str(default)))
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def strip_trailing_slash(url: str) -> str:
    return url.rstrip("/")


def is_hypersnap_feed_endpoint(url: str) -> bool:
    try:
        path = urllib.parse.urlsplit(str(url or "")).path
    except ValueError:
        return False
    return "/v2/farcaster/feed/channels" in path


def public_safe_url(url: str) -> str:
    """Return a public provenance URL with credentials/query/fragment removed."""
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return "[redacted-url]"
    if not parsed.scheme or not parsed.netloc:
        return redact_query_tokens(text)
    host = parsed.hostname or parsed.netloc.rsplit("@", 1)[-1]
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def redact_query_tokens(text: str) -> str:
    return re.sub(
        r"(?i)([?&](?:api[_-]?key|key|token|secret|signature|sig|auth|access[_-]?token|client[_-]?secret)=)[^\s&#]+",
        r"\1[redacted]",
        text,
    )


def redact_urls(text: str) -> str:
    return URL_PATTERN.sub(lambda match: public_safe_url(match.group(0)), text)


def redact_secrets(value: object) -> str:
    text = str(value)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    text = redact_query_tokens(text)
    text = re.sub(r"(https?://)[^/@\s]+@", r"\1[redacted]@", text)
    text = redact_urls(text)
    return text.replace(os.environ.get("NEYNAR_API_KEY", "\0"), "[redacted]") if os.environ.get("NEYNAR_API_KEY") else text


def add_error(errors: list[dict[str, str]], source: str, exc: object) -> None:
    errors.append({"source": source, "error": redact_secrets(exc)[:300]})


def summarize_errors(errors: list[dict[str, str]]) -> str:
    if not errors:
        return "No direct Farcaster source was configured or available."
    return "; ".join(f"{item.get('source', 'unknown')}: {item.get('error', '')}" for item in errors)


def normalize_hash(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text.startswith("0x") else f"0x{text}"


def nullable_hash(value: Any) -> str | None:
    normalized = normalize_hash(value)
    return normalized or None


def int_value(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_timestamp(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        return iso_utc(FARCASTER_EPOCH + timedelta(seconds=int(value)))
    text = str(value).strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text.replace(".000Z", "Z")
    return iso_utc(parsed)


def normalize_embeds(embeds: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(embeds, list):
        return out
    for embed in embeds[:8]:
        if isinstance(embed, dict):
            item: dict[str, Any] = {}
            if embed.get("url"):
                item["url"] = str(embed.get("url"))
            if embed.get("cast_id"):
                item["cast_id"] = embed.get("cast_id")
            if item:
                out.append(item)
        elif embed:
            out.append({"url": str(embed)})
    return out


def cast_url(username: str, cast_hash: str) -> str:
    if username:
        return f"https://farcaster.xyz/{urllib.parse.quote(username)}/{cast_hash}"
    if cast_hash:
        return f"https://farcaster.xyz/~/conversations/{cast_hash}"
    return DEFAULT_CHANNEL_URL


def dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_hypersnap_cast(cast: dict[str, Any]) -> dict[str, Any]:
    author = dict_value(cast.get("author"))
    cast_hash = normalize_hash(cast.get("hash"))
    parent_hash = nullable_hash(cast.get("parent_hash"))
    thread_hash = nullable_hash(cast.get("thread_hash") or cast.get("root_parent_hash")) or parent_hash or cast_hash
    author_fid = int_value(author.get("fid") or cast.get("author_fid"))
    username = str(author.get("username") or cast.get("author_username") or "").strip().lstrip("@")
    display_name = str(author.get("display_name") or cast.get("author_display_name") or username or "").strip()
    reactions = dict_value(cast.get("reactions"))
    replies = dict_value(cast.get("replies"))
    return {
        "hash": cast_hash,
        "thread_hash": thread_hash,
        "parent_hash": parent_hash,
        "author_fid": author_fid,
        "author_username": username or (f"fid:{author_fid}" if author_fid else ""),
        "author_display_name": display_name or (f"FID {author_fid}" if author_fid else "Unknown"),
        "author_pfp_url": str(author.get("pfp_url") or cast.get("author_pfp_url") or ""),
        "text": str(cast.get("text") or ""),
        "timestamp": normalize_timestamp(cast.get("timestamp")),
        "url": str(cast.get("url") or cast_url(username, cast_hash)),
        "replies_count": int_value(cast.get("replies_count") if cast.get("replies_count") is not None else replies.get("count")),
        "likes_count": int_value(cast.get("likes_count") if cast.get("likes_count") is not None else reactions.get("likes_count")),
        "recasts_count": int_value(cast.get("recasts_count") if cast.get("recasts_count") is not None else reactions.get("recasts_count")),
        "embeds": normalize_embeds(cast.get("embeds")),
    }


def normalize_snapchain_message(message: dict[str, Any]) -> dict[str, Any]:
    data = dict_value(message.get("data"))
    body = dict_value(data.get("castAddBody"))
    fid = int_value(data.get("fid"))
    cast_hash = normalize_hash(message.get("hash"))
    parent_cast = dict_value(body.get("parentCastId"))
    parent_hash = nullable_hash(parent_cast.get("hash"))
    return {
        "hash": cast_hash,
        "thread_hash": parent_hash or cast_hash,
        "parent_hash": parent_hash,
        "author_fid": fid,
        "author_username": f"fid:{fid}" if fid else "",
        "author_display_name": f"FID {fid}" if fid else "Unknown",
        "author_pfp_url": "",
        "text": str(body.get("text") or ""),
        "timestamp": normalize_timestamp(data.get("timestamp")),
        "url": cast_url("", cast_hash),
        "replies_count": 0,
        "likes_count": 0,
        "recasts_count": 0,
        "embeds": normalize_embeds(body.get("embeds") or body.get("embedsDeprecated")),
    }


def normalize_casts(casts: list[dict[str, Any]], *, limit: int, source: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for cast in casts:
        try:
            row = normalize_snapchain_message(cast) if source == "snapchain" else normalize_hypersnap_cast(cast)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: skipped malformed Farcaster cast from {source}: {redact_secrets(exc)}", file=sys.stderr)
            continue
        if row.get("hash") and row.get("text"):
            normalized.append(row)
        if len(normalized) >= limit:
            break
    return normalized


def request_json(url: str, *, headers: dict[str, str] | None = None, timeout_seconds: int = 15) -> dict[str, Any]:
    req_headers = {"accept": "application/json", "user-agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise RuntimeError("Farcaster endpoint returned non-object JSON")
    return data


def snapshot_base(channel_id: str, now: datetime) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "channel_id": channel_id,
        "channel_url": f"https://farcaster.xyz/~/channel/{channel_id}",
        "source_priority": SOURCE_PRIORITY,
        "updated_at_utc": iso_utc(now),
        "status": "ok",
        "error": None,
        "casts": [],
    }


def with_source(snapshot: dict[str, Any], *, source: str, endpoint: str, casts: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot.update({"source": source, "source_endpoint": public_safe_url(endpoint), "status": "ok", "error": None, "casts": casts})
    return snapshot


def fallback_empty_snapshot(channel_id: str, now: datetime, errors: list[dict[str, str]], *, status: str = "error") -> dict[str, Any]:
    snapshot = snapshot_base(channel_id, now)
    snapshot.update({
        "source": "none",
        "status": status,
        "error": "All Farcaster channel feed sources failed or were unavailable." if status == "error" else "Farcaster feed disabled by FARCASTER_FEED_ENABLED=0.",
        "casts": [],
    })
    if errors:
        snapshot["source_errors"] = errors
    return snapshot


def hypersnap_feed_url(base_or_endpoint: str, channel_id: str, limit: int) -> str:
    cleaned = strip_trailing_slash(base_or_endpoint)
    query = urllib.parse.urlencode({"channel_ids": channel_id, "limit": limit})
    if "/v2/farcaster/feed/channels" in cleaned:
        parsed = urllib.parse.urlsplit(cleaned)
        query_params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        query_params.update({"channel_ids": channel_id, "limit": str(limit)})
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query_params), parsed.fragment))
    return f"{cleaned}/v2/farcaster/feed/channels?{query}"


def snapchain_feed_url(base_url: str, channel_id: str, limit: int) -> str:
    parent_url = f"https://warpcast.com/~/channel/{channel_id}"
    query = urllib.parse.urlencode({"url": parent_url, "pageSize": limit, "reverse": "true"})
    return f"{strip_trailing_slash(base_url)}/v1/castsByParent?{query}"


def neynar_feed_url(channel_id: str, limit: int) -> str:
    query = urllib.parse.urlencode({"channel_ids": channel_id, "limit": limit})
    return f"{NEYNAR_CHANNEL_FEED_URL}?{query}"


def fetch_from_hypersnap(channel_id: str, limit: int, endpoint: str, requester: JsonRequester, timeout_seconds: int) -> tuple[list[dict[str, Any]], str]:
    url = hypersnap_feed_url(endpoint, channel_id, limit)
    data = requester(url, timeout_seconds=timeout_seconds)
    casts = data.get("casts")
    if not isinstance(casts, list):
        raise RuntimeError("Hypersnap response missing casts[]")
    return normalize_casts([cast for cast in casts if isinstance(cast, dict)], limit=limit, source="hypersnap"), url


def fetch_from_snapchain(channel_id: str, limit: int, endpoint: str, requester: JsonRequester, timeout_seconds: int) -> tuple[list[dict[str, Any]], str]:
    url = snapchain_feed_url(endpoint, channel_id, limit)
    data = requester(url, timeout_seconds=timeout_seconds)
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise RuntimeError("Snapchain response missing messages[]")
    return normalize_casts([message for message in messages if isinstance(message, dict)], limit=limit, source="snapchain"), url


def fetch_from_neynar(channel_id: str, limit: int, api_key: str, requester: JsonRequester, timeout_seconds: int) -> tuple[list[dict[str, Any]], str]:
    url = neynar_feed_url(channel_id, limit)
    data = requester(url, headers={"x-api-key": api_key}, timeout_seconds=timeout_seconds)
    casts = data.get("casts")
    if not isinstance(casts, list):
        raise RuntimeError("Neynar response missing casts[]")
    return normalize_casts([cast for cast in casts if isinstance(cast, dict)], limit=limit, source="neynar"), url


def build_channel_snapshot(
    *,
    env: Mapping[str, str] | None = None,
    request_json: JsonRequester = request_json,
    now: datetime | None = None,
) -> dict[str, Any]:
    env = os.environ if env is None else env
    now = utc_now() if now is None else now
    channel_id = env_text(env, "FARCASTER_CHANNEL_ID", DEFAULT_CHANNEL_ID) or DEFAULT_CHANNEL_ID
    limit = env_int(env, "FARCASTER_CHANNEL_LIMIT", 30, minimum=1, maximum=50)
    timeout_seconds = env_int(env, "FARCASTER_DIRECT_TIMEOUT_SECONDS", 15, minimum=3, maximum=60)
    errors: list[dict[str, str]] = []

    if not env_flag(env, "FARCASTER_FEED_ENABLED", True):
        return fallback_empty_snapshot(channel_id, now, errors, status="disabled")

    explicit_read_api = env_text(env, "HYPERSNAP_READ_API_URL", "")
    configured_hypersnap_base = env_text(env, "HYPERSNAP_BASE_URL", DEFAULT_HYPERSNAP_BASE_URL)
    hypersnap_base = explicit_read_api or configured_hypersnap_base
    if hypersnap_base:
        try:
            casts, endpoint = fetch_from_hypersnap(channel_id, limit, hypersnap_base, request_json, timeout_seconds)
            return with_source(snapshot_base(channel_id, now), source="hypersnap", endpoint=endpoint, casts=casts)
        except Exception as exc:  # noqa: BLE001
            add_error(errors, "hypersnap", exc)
            print(f"warning: Hypersnap Farcaster feed failed: {redact_secrets(exc)}", file=sys.stderr)

    if explicit_read_api:
        snapchain_default = configured_hypersnap_base if is_hypersnap_feed_endpoint(explicit_read_api) else explicit_read_api
    else:
        snapchain_default = hypersnap_base
    snapchain_base = env_text(env, "SNAPCHAIN_RPC_URL", snapchain_default)
    if snapchain_base:
        try:
            casts, endpoint = fetch_from_snapchain(channel_id, limit, snapchain_base, request_json, timeout_seconds)
            return with_source(snapshot_base(channel_id, now), source="snapchain", endpoint=endpoint, casts=casts)
        except Exception as exc:  # noqa: BLE001
            add_error(errors, "snapchain", exc)
            print(f"warning: Snapchain Farcaster feed failed: {redact_secrets(exc)}", file=sys.stderr)

    api_key = env_text(env, "NEYNAR_API_KEY", "")
    if env_flag(env, "NEYNAR_FALLBACK_ENABLED", False) and api_key:
        try:
            casts, endpoint = fetch_from_neynar(channel_id, limit, api_key, request_json, timeout_seconds)
            snapshot = with_source(snapshot_base(channel_id, now), source="neynar", endpoint=endpoint, casts=casts)
            snapshot["fallback_used"] = True
            snapshot["fallback_reason"] = summarize_errors(errors)
            return snapshot
        except Exception as exc:  # noqa: BLE001
            add_error(errors, "neynar", exc)
            print(f"warning: Neynar Farcaster fallback failed: {redact_secrets(exc)}", file=sys.stderr)

    return fallback_empty_snapshot(channel_id, now, errors)


def write_snapshot(snapshot: dict[str, Any]) -> None:
    GENERATED.mkdir(exist_ok=True)
    PUBLIC_GENERATED.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    for directory in [GENERATED, PUBLIC_GENERATED]:
        (directory / OUTPUT_FILENAME).write_text(payload, encoding="utf-8")


def main() -> int:
    started = time.time()
    snapshot = build_channel_snapshot()
    write_snapshot(snapshot)
    elapsed = time.time() - started
    print(json.dumps({
        "file": f"generated/{OUTPUT_FILENAME}",
        "source": snapshot.get("source"),
        "status": snapshot.get("status"),
        "casts": len(snapshot.get("casts") or []),
        "elapsed_seconds": round(elapsed, 2),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
