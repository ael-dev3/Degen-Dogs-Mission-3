#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "refresh_telemetry.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("refresh_telemetry", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def assert_raises_contains(fn: Any, needle: str) -> None:
    try:
        fn()
    except AssertionError as exc:
        assert needle in str(exc), str(exc)
        return
    raise AssertionError(f"expected AssertionError containing {needle!r}")


def iso(offset_seconds: int = 0) -> str:
    return (datetime(2026, 6, 2, 20, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def write_fixture(root: Path) -> None:
    metrics = {
        "latest_block": "46822740",
        "latest_block_time_utc": "2026-06-02 21:13:47",
        "current_auction_token_id": "732",
        "current_bid_eth": "0.01",
        "current_bidder": "@thec1",
        "current_bidder_wallet": "0xd29c790466675153a50df7860b9efdb689a21cde",
        "current_auction_status": "live",
        "current_auction_end_utc": "2026-06-03 19:37:21",
    }
    rows = [{"metric": key, "value": value} for key, value in metrics.items()]
    write_json(root / "generated" / "mission3_metrics.json", rows)
    write_json(root / "public" / "generated" / "mission3_metrics.json", rows)
    (root / "generated").mkdir(parents=True, exist_ok=True)
    (root / "generated" / "mission3_metrics.csv").write_text(
        "metric,value\n" + "".join(f"{key},{value}\n" for key, value in metrics.items()),
        encoding="utf-8",
    )
    current = {
        "token_id": 732,
        "current_bid_eth": 0.01,
        "bidder": "@thec1",
        "bidder_wallet": "0xd29c790466675153a50df7860b9efdb689a21cde",
        "auction_state": "live",
        "end_time_utc": "2026-06-03 19:37:21",
        "latest_block": 46822740,
        "latest_block_time_utc": "2026-06-02 21:13:47",
    }
    write_json(root / "generated" / "current_auction.json", [current])
    write_json(root / "public" / "generated" / "current_auction.json", [current])


def base_env(root: Path) -> dict[str, str]:
    return {
        "DEGEN_DOGS_REFRESH_TELEMETRY_PATH": str(root / ".local" / "refresh_runs.jsonl"),
        "DEGEN_DOGS_REFRESH_METRICS_PATH": str(root / "logs" / "refresh-metrics.jsonl"),
        "MISSION3_WATCHER_TELEMETRY_PATH": str(root / ".local" / "watcher_checks.jsonl"),
        "DEGEN_DOGS_REFRESH_RUN_ID": "unit-run-1",
        "DEGEN_DOGS_REFRESH_TRIGGER": "watcher",
        "DEGEN_DOGS_REFRESH_REASONS": json.dumps(["auction_bid", "highest_bid_amount_changed"]),
        "DEGEN_DOGS_REFRESH_QUEUED_AT_UTC": iso(0),
        "DEGEN_DOGS_LOCK_ACQUIRED_AT_UTC": iso(2),
        "DEGEN_DOGS_REFRESH_STARTED_AT_UTC": iso(3),
        "DEGEN_DOGS_DETECTED_AT_UTC": iso(-5),
        "DEGEN_DOGS_EVENT_NAME": "AuctionBid",
        "DEGEN_DOGS_EVENT_BLOCK_NUMBER": "46822730",
        "DEGEN_DOGS_EVENT_TX_HASH": "0xabc",
        "DEGEN_DOGS_EVENT_LOG_INDEX": "4",
        "DEGEN_DOGS_DATA_STARTED_AT_UTC": iso(4),
        "DEGEN_DOGS_DATA_COMPLETED_AT_UTC": iso(10),
        "DEGEN_DOGS_BUILD_STARTED_AT_UTC": iso(11),
        "DEGEN_DOGS_BUILD_COMPLETED_AT_UTC": iso(13),
        "DEGEN_DOGS_PUSH_STARTED_AT_UTC": iso(14),
        "DEGEN_DOGS_PUSH_COMPLETED_AT_UTC": iso(17),
        "DEGEN_DOGS_COMMIT_SHA": "abcdef1234567890",
    }


def test_record_refresh_redacts_secrets_and_writes_public_status() -> None:
    telemetry = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        env = base_env(root)
        row = telemetry.record_refresh(env, result="success_pushed", error="api_key=sk-secretsecretsecretsecretsecret and url=https://rpc.quicknode.pro/abc?token=secret", root=root)
        text = json.dumps(row)
        assert "sk-secret" not in text
        assert "quicknode.pro/abc" not in text
        assert row["result"] == "success_pushed"
        assert row["lock_wait_seconds"] == 2
        assert row["push_duration_seconds"] == 3
        assert row["detect_to_push_seconds"] == 22

        status = telemetry.write_refresh_status(env, root=root)
        assert status["kind"] == "refresh_status"
        assert status["latest_generated_block"] == 46822740
        assert status["current_dog_token_id"] == 732
        assert status["current_high_bidder"] == "@thec1"
        assert "/Users/" not in json.dumps(status)
        validated = telemetry.validate_refresh_status(root=root)
        assert validated == status


def test_refresh_outcome_rows_cover_no_diff_failure_and_live_timeout() -> None:
    telemetry = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        env = base_env(root)
        no_diff = telemetry.build_refresh_row(env, result="success_no_diff", root=root)
        assert no_diff["result"] == "success_no_diff"
        assert no_diff["reasons"] == ["auction_bid", "highest_bid_amount_changed"]

        failed = telemetry.build_refresh_row(env, result="failed", error="password=hunter2", root=root)
        assert failed["result"] == "failed"
        assert "hunter2" not in json.dumps(failed)

        timeout_env = dict(env)
        timeout_env.update(
            {
                "DEGEN_DOGS_LIVE_VERIFY_STARTED_AT_UTC": iso(18),
                "DEGEN_DOGS_LIVE_VERIFY_RESULT": "timeout",
                "DEGEN_DOGS_PUSH_TO_LIVE_SECONDS": "300",
                "DEGEN_DOGS_BLOCK_TO_LIVE_SECONDS": "420",
            }
        )
        timeout = telemetry.build_refresh_row(timeout_env, result="success_pushed_live_timeout", root=root)
        assert timeout["result"] == "success_pushed_live_timeout"
        assert timeout["live_verify_result"] == "timeout"
        assert timeout["push_to_live_seconds"] == 300
        telemetry.record_refresh(timeout_env, result="success_pushed_live_timeout", root=root)
        status = telemetry.write_refresh_status(env, root=root)
        assert status["last_refresh_result"] == "success_generated"
        assert "last_pushed_commit" not in status
        assert "last_push_duration_seconds" not in status
        assert telemetry.validate_refresh_status(root=root) == status


def test_refresh_status_validation_rejects_stale_required_fields() -> None:
    telemetry = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        env = base_env(root)
        status = telemetry.write_refresh_status(env, root=root, prefer_current_env=True)

        broken = dict(status)
        broken["current_high_bidder_wallet"] = "0x0000000000000000000000000000000000000001"
        write_json(root / "generated" / "refresh_status.json", broken)
        write_json(root / "public" / "generated" / "refresh_status.json", broken)
        assert_raises_contains(lambda: telemetry.validate_refresh_status(root=root), "current_high_bidder_wallet")

        broken = dict(status)
        broken["last_refresh_result"] = "failed"
        write_json(root / "generated" / "refresh_status.json", broken)
        write_json(root / "public" / "generated" / "refresh_status.json", broken)
        assert_raises_contains(lambda: telemetry.validate_refresh_status(root=root), "last_refresh_result")

        broken = dict(status)
        broken["last_refresh_result"] = "success_pushed_live_timeout"
        write_json(root / "generated" / "refresh_status.json", broken)
        write_json(root / "public" / "generated" / "refresh_status.json", broken)
        assert_raises_contains(lambda: telemetry.validate_refresh_status(root=root), "last_refresh_result")

        broken = dict(status)
        broken.pop("refresh_reason")
        write_json(root / "generated" / "refresh_status.json", broken)
        write_json(root / "public" / "generated" / "refresh_status.json", broken)
        assert_raises_contains(lambda: telemetry.validate_refresh_status(root=root), "missing required fields")


def test_metrics_summary_includes_pending_metadata_and_speed_percentiles() -> None:
    telemetry = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        env = base_env(root)
        telemetry.record_refresh(env, result="success_pushed", root=root)
        watcher_row = {
            "schema_version": 1,
            "kind": "watcher_check",
            "started_at_utc": iso(20),
            "completed_at_utc": iso(21),
            "duration_seconds": 1,
            "result": "cooldown_skip",
            "reasons": ["auction_bid"],
            "pending_refresh": True,
        }
        telemetry.record_watcher_check(watcher_row, env=env, root=root)
        write_json(
            root / ".local" / "mission3_onchain_tracker_state.json",
            {
                "pending_refresh": True,
                "pending_refresh_reasons": ["auction_bid"],
                "next_allowed_refresh_after_utc": iso(300),
            },
        )
        summary = telemetry.metrics_summary(env, root=root)
        assert summary["pending_refresh"] is True
        assert summary["pending_refresh_reasons"] == ["auction_bid"]
        assert summary["watcher_check_average_seconds_24h"] == 1
        assert summary["refresh_p95_seconds_24h"] is not None
        assert summary["last_refresh_result"] == "success_pushed"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"refresh_telemetry_tests=pass count={len(tests)}")
