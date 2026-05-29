#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "fetch_farcaster_channel.py"
FIXED_NOW = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)


def load_module():
    spec = importlib.util.spec_from_file_location("fetch_farcaster_channel", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_normalize_hypersnap_cast_keeps_display_fields_and_stats():
    channel = load_module()
    cast = {
        "hash": "abc123",
        "parent_hash": None,
        "author": {
            "fid": 123,
            "username": "alice.eth",
            "display_name": "Alice",
            "pfp_url": "https://example.com/alice.png",
        },
        "text": "gm dogs",
        "timestamp": "2026-05-29T10:00:00.000Z",
        "replies": {"count": 2},
        "reactions": {"likes_count": 7, "recasts_count": 3},
        "embeds": [{"url": "https://example.com"}],
    }

    normalized = channel.normalize_hypersnap_cast(cast)

    assert normalized == {
        "hash": "0xabc123",
        "thread_hash": "0xabc123",
        "parent_hash": None,
        "author_fid": 123,
        "author_username": "alice.eth",
        "author_display_name": "Alice",
        "author_pfp_url": "https://example.com/alice.png",
        "text": "gm dogs",
        "timestamp": "2026-05-29T10:00:00Z",
        "url": "https://farcaster.xyz/alice.eth/0xabc123",
        "replies_count": 2,
        "likes_count": 7,
        "recasts_count": 3,
        "embeds": [{"url": "https://example.com"}],
    }


def test_normalize_snapchain_message_uses_farcaster_epoch_and_fid_fallback():
    channel = load_module()
    message = {
        "hash": "0xdef456",
        "data": {
            "fid": 456,
            "timestamp": 170595942,
            "castAddBody": {
                "parentUrl": "https://warpcast.com/~/channel/degendogs",
                "text": "direct node cast",
                "embeds": [{"url": "https://degendogs.club/auction"}],
            },
        },
    }

    normalized = channel.normalize_snapchain_message(message)

    assert normalized["hash"] == "0xdef456"
    assert normalized["thread_hash"] == "0xdef456"
    assert normalized["author_fid"] == 456
    assert normalized["author_username"] == "fid:456"
    assert normalized["author_display_name"] == "FID 456"
    assert normalized["timestamp"] == "2026-05-29T11:45:42Z"
    assert normalized["url"] == "https://farcaster.xyz/~/conversations/0xdef456"
    assert normalized["replies_count"] == 0
    assert normalized["likes_count"] == 0
    assert normalized["recasts_count"] == 0


def test_source_selection_keeps_neynar_disabled_unless_explicitly_enabled():
    channel = load_module()
    calls: list[str] = []

    def fake_request_json(url: str, *, headers=None, timeout_seconds=None):
        calls.append(url)
        raise RuntimeError("network unavailable and NEYNAR_API_KEY=super-secret")

    snapshot = channel.build_channel_snapshot(
        env={
            "FARCASTER_FEED_ENABLED": "1",
            "HYPERSNAP_BASE_URL": "",
            "HYPERSNAP_READ_API_URL": "",
            "SNAPCHAIN_RPC_URL": "",
            "NEYNAR_API_KEY": "super-secret",
            "NEYNAR_FALLBACK_ENABLED": "0",
        },
        request_json=fake_request_json,
        now=FIXED_NOW,
    )

    assert calls == []
    assert snapshot["source"] == "none"
    assert snapshot["status"] == "error"
    assert snapshot["casts"] == []
    assert "super-secret" not in str(snapshot)
    assert "NEYNAR_API_KEY" not in str(snapshot)


def test_neynar_fallback_is_marked_when_direct_sources_fail_and_fallback_enabled():
    channel = load_module()
    calls: list[str] = []

    def fake_request_json(url: str, *, headers=None, timeout_seconds=None):
        calls.append(url)
        if "api.neynar.com" in url:
            return {
                "casts": [
                    {
                        "hash": "0xbeef",
                        "author": {"fid": 99, "username": "fallback", "display_name": "Fallback", "pfp_url": ""},
                        "text": "fallback cast",
                        "timestamp": "2026-05-29T11:00:00Z",
                        "reactions": {"likes_count": 1, "recasts_count": 0},
                        "replies": {"count": 0},
                    }
                ]
            }
        raise RuntimeError("direct source unavailable")

    snapshot = channel.build_channel_snapshot(
        env={
            "FARCASTER_FEED_ENABLED": "1",
            "HYPERSNAP_BASE_URL": "https://bad-hypersnap.example",
            "HYPERSNAP_READ_API_URL": "",
            "SNAPCHAIN_RPC_URL": "https://bad-snapchain.example",
            "NEYNAR_API_KEY": "test-key",
            "NEYNAR_FALLBACK_ENABLED": "1",
            "FARCASTER_CHANNEL_LIMIT": "5",
        },
        request_json=fake_request_json,
        now=FIXED_NOW,
    )

    assert any("bad-hypersnap.example" in url for url in calls)
    assert any("bad-snapchain.example" in url for url in calls)
    assert any("api.neynar.com" in url for url in calls)
    assert snapshot["source"] == "neynar"
    assert snapshot["fallback_used"] is True
    assert "hypersnap" in snapshot["fallback_reason"]
    assert snapshot["status"] == "ok"
    assert snapshot["casts"][0]["hash"] == "0xbeef"
    assert "test-key" not in str(snapshot)


def test_full_hypersnap_endpoint_failure_uses_bare_snapchain_default():
    channel = load_module()
    calls: list[str] = []

    def fake_request_json(url: str, *, headers=None, timeout_seconds=None):
        calls.append(url)
        if "private.example" in url:
            raise RuntimeError(f"private read endpoint failed: {url}")
        if url.startswith(f"{channel.DEFAULT_HYPERSNAP_BASE_URL}/v1/castsByParent?"):
            return {
                "messages": [
                    {
                        "hash": "0xdef456",
                        "data": {
                            "fid": 456,
                            "timestamp": 170595942,
                            "castAddBody": {"text": "snapchain fallback cast", "embeds": []},
                        },
                    }
                ]
            }
        raise RuntimeError(f"unexpected url: {url}")

    snapshot = channel.build_channel_snapshot(
        env={
            "FARCASTER_FEED_ENABLED": "1",
            "HYPERSNAP_READ_API_URL": "https://user:pass@private.example/v2/farcaster/feed/channels?token=secret-token",
            "NEYNAR_FALLBACK_ENABLED": "0",
        },
        request_json=fake_request_json,
        now=FIXED_NOW,
    )

    assert snapshot["source"] == "snapchain"
    assert calls[0] == "https://user:pass@private.example/v2/farcaster/feed/channels?token=secret-token&channel_ids=degendogs&limit=30"
    assert calls[1].startswith(f"{channel.DEFAULT_HYPERSNAP_BASE_URL}/v1/castsByParent?")
    assert "/v2/farcaster/feed/channels" not in calls[1]
    assert "secret-token" not in str(snapshot)
    assert "user:pass" not in str(snapshot)


def test_public_snapshot_redacts_endpoint_and_error_urls():
    channel = load_module()

    success_calls: list[str] = []

    def successful_request(url: str, *, headers=None, timeout_seconds=None):
        success_calls.append(url)
        return {
            "casts": [
                {
                    "hash": "0xcafe",
                    "author": {"fid": 77, "username": "safe", "display_name": "Safe", "pfp_url": ""},
                    "text": "safe cast",
                    "timestamp": "2026-05-29T11:00:00Z",
                }
            ]
        }

    snapshot = channel.build_channel_snapshot(
        env={
            "FARCASTER_FEED_ENABLED": "1",
            "HYPERSNAP_READ_API_URL": "https://user:pass@private.example/v2/farcaster/feed/channels?token=secret-token&channel_ids=degendogs",
            "SNAPCHAIN_RPC_URL": "",
            "NEYNAR_FALLBACK_ENABLED": "0",
        },
        request_json=successful_request,
        now=FIXED_NOW,
    )

    assert snapshot["source"] == "hypersnap"
    assert success_calls == [
        "https://user:pass@private.example/v2/farcaster/feed/channels?token=secret-token&channel_ids=degendogs&limit=30"
    ]
    assert snapshot["source_endpoint"] == "https://private.example/v2/farcaster/feed/channels"
    assert "secret-token" not in str(snapshot)
    assert "user:pass" not in str(snapshot)
    assert "token=" not in snapshot["source_endpoint"]

    def failing_request(url: str, *, headers=None, timeout_seconds=None):
        raise RuntimeError(f"fetch failed for {url} with x-api-key=abc123 token=secret-token")

    failed = channel.build_channel_snapshot(
        env={
            "FARCASTER_FEED_ENABLED": "1",
            "HYPERSNAP_READ_API_URL": "https://user:pass@private.example/v2/farcaster/feed/channels?token=secret-token",
            "SNAPCHAIN_RPC_URL": "",
            "NEYNAR_FALLBACK_ENABLED": "0",
        },
        request_json=failing_request,
        now=FIXED_NOW,
    )
    rendered = str(failed)
    assert failed["source"] == "none"
    assert "secret-token" not in rendered
    assert "abc123" not in rendered
    assert "user:pass" not in rendered
    assert "token=" not in rendered


def main() -> int:
    for test in [
        test_normalize_hypersnap_cast_keeps_display_fields_and_stats,
        test_normalize_snapchain_message_uses_farcaster_epoch_and_fid_fallback,
        test_source_selection_keeps_neynar_disabled_unless_explicitly_enabled,
        test_neynar_fallback_is_marked_when_direct_sources_fail_and_fallback_enabled,
        test_full_hypersnap_endpoint_failure_uses_bare_snapchain_default,
        test_public_snapshot_redacts_endpoint_and_error_urls,
    ]:
        test()
    print("farcaster_channel_feed_tests=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
