#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("check_remote_freshness.py")


def load_module():
    spec = importlib.util.spec_from_file_location("check_remote_freshness", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def status(block: int, timestamp: str) -> dict:
    return {
        "kind": "refresh_status",
        "last_refresh_result": "success_generated",
        "last_successful_refresh_time_utc": timestamp,
        "latest_generated_block": block,
        "onchain_chain_id": "8453",
        "onchain_verification_status": "current_snapshot_cross_provider_verified",
        "onchain_verification_scope": "snapshot_hash,contract_code,current_auction,dog_total_supply,recent_event_logs",
        "snapshot_block_hash": "0x" + "a" * 64,
    }


def test_healthy_equal_status() -> None:
    monitor = load_module()
    now = datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc)
    report = monitor.assess_freshness(
        status(100, "2026-08-02T12:30:00Z"),
        status(100, "2026-08-02T12:30:00Z"),
        now=now,
        max_raw_age_seconds=5400,
        propagation_grace_seconds=900,
    )
    assert report["status"] == "healthy"
    assert report["incident"] is False


def test_stale_raw_opens_incident_without_pages_redeploy() -> None:
    monitor = load_module()
    now = datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc)
    report = monitor.assess_freshness(
        status(100, "2026-08-02T10:00:00Z"),
        status(100, "2026-08-02T10:00:00Z"),
        now=now,
        max_raw_age_seconds=5400,
        propagation_grace_seconds=900,
    )
    assert report["raw_stale"] is True
    assert report["pages_needs_deploy"] is False


def test_pages_lag_redeploys_only_after_grace() -> None:
    monitor = load_module()
    now = datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc)
    raw = status(101, "2026-08-02T12:50:00Z")
    pages = status(100, "2026-08-02T12:30:00Z")
    report = monitor.assess_freshness(
        raw,
        pages,
        now=now,
        max_raw_age_seconds=5400,
        propagation_grace_seconds=900,
    )
    assert report["pages_needs_deploy"] is True
    assert report["incident"] is True


def test_invalid_onchain_verification_is_stale() -> None:
    monitor = load_module()
    now = datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc)
    raw = status(101, "2026-08-02T12:50:00Z")
    raw["onchain_verification_status"] = "single_provider"
    report = monitor.assess_freshness(
        raw,
        status(101, "2026-08-02T12:50:00Z"),
        now=now,
        max_raw_age_seconds=5400,
        propagation_grace_seconds=900,
    )
    assert report["raw_stale"] is True
    assert "cross-provider" in report["raw_problem"]


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"remote_freshness_tests=pass count={len(tests)}")
