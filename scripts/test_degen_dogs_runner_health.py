#!/usr/bin/env python3
"""Regression tests for the Mission 3 local runner health watchdog."""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import plistlib
import sys
import tempfile
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).with_name("degen_dogs_runner_health.py")
spec = importlib.util.spec_from_file_location("degen_dogs_runner_health", SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError(f"could not load {SCRIPT}")
health = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = health
spec.loader.exec_module(health)


def test_refresh_lock_detection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        lock_path = Path(directory) / "refresh.lock"
        setattr(health, "REFRESH_LOCK_PATH", lock_path)

        # A stale/unlocked lock file must not suppress a real dirty-worktree alert.
        lock_path.touch()
        assert health.refresh_is_active() is False

        fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert health.refresh_is_active() is True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        assert health.refresh_is_active() is False


def test_active_lock_metadata_requires_a_held_flock() -> None:
    with tempfile.TemporaryDirectory() as directory:
        lock_path = Path(directory) / "watcher.lock"
        lock_path.write_text(
            "kind=watcher\npid=123\nstarted_at_utc=2026-08-02T12:00:00Z\n",
            encoding="utf-8",
        )
        assert health.inspect_active_lock(lock_path) == (False, None)

        fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            active, started_ts = health.inspect_active_lock(lock_path)
            assert active is True
            assert started_ts == datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc).timestamp()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        assert health.inspect_active_lock(lock_path) == (False, None)


def test_fresh_active_attempt_requires_new_held_bounded_run() -> None:
    now = datetime(2026, 8, 2, 12, 10, tzinfo=timezone.utc).timestamp()
    started = now - 60
    completed = now - 120
    assert health.fresh_active_attempt(
        lock_held=True,
        started_ts=started,
        completed_ts=completed,
        now=now,
        grace_seconds=90,
    ) is True
    assert health.fresh_active_attempt(
        lock_held=False,
        started_ts=started,
        completed_ts=completed,
        now=now,
        grace_seconds=90,
    ) is False
    assert health.fresh_active_attempt(
        lock_held=True,
        started_ts=completed,
        completed_ts=completed,
        now=now,
        grace_seconds=90,
    ) is False
    assert health.fresh_active_attempt(
        lock_held=True,
        started_ts=now - 90,
        completed_ts=completed,
        now=now,
        grace_seconds=90,
    ) is False
    assert health.fresh_active_attempt(
        lock_held=True,
        started_ts=now + 1,
        completed_ts=completed,
        now=now,
        grace_seconds=90,
    ) is False


def test_active_watcher_filters_only_completion_lag_issues() -> None:
    issues = [
        "watcher state missing: /tmp/state.json",
        "watcher state has no valid last_checked_at_utc",
        "watcher state age=12m exceeds threshold=5m",
        "watcher has 3 consecutive RPC failures",
        "watcher has 4 consecutive refresh failures",
        "watcher pending refresh age=20m exceeds threshold=15m",
        "watcher state unreadable: JSONDecodeError",
    ]
    filtered = health.filter_watcher_issues_for_active_attempt(issues, True)
    assert filtered == issues[3:]
    assert health.filter_watcher_issues_for_active_attempt(issues, False) == issues


def test_launchd_cause_requires_an_explicit_launchd_fault() -> None:
    benign = health.derive_causes(
        issues=[
            "issue: refresh appears stale/failed, but launchd job is currently running; left it alone",
            "issue: watcher state is unhealthy, but the watcher job is currently running; left it alone",
        ],
        dirty_paths=[],
        log_details={},
        stale=False,
        failed_last=False,
        live_ok=True,
        launch_output="state = running",
        now=0,
    )
    assert "launchd_agent_unhealthy_or_drifted" not in benign

    drifted = health.derive_causes(
        issues=["issue: onchain auction watcher launchd plist drift: StartInterval"],
        dirty_paths=[],
        log_details={},
        stale=False,
        failed_last=False,
        live_ok=True,
        launch_output="state = not running",
        now=0,
    )
    assert "launchd_agent_unhealthy_or_drifted" in drifted


def test_expected_live_publish_lag_is_narrow() -> None:
    assert health.expected_live_publish_lag(
        "live refresh status block 100 trails local generated block 101"
    ) is True
    assert health.expected_live_publish_lag(
        "live refresh status current_bid_eth differs from local validated status at block 101"
    ) is True
    assert health.expected_live_publish_lag("live HTTP status 503") is False
    assert health.expected_live_publish_lag("live refresh status payload is invalid") is False


def valid_plist(spec: object) -> dict[str, object]:
    required_environment = dict(getattr(spec, "required_environment"))
    return {
        "Label": getattr(spec, "label"),
        "ProgramArguments": list(getattr(spec, "program_arguments")),
        "WorkingDirectory": str(health.REPO_DIR),
        "StartInterval": getattr(spec, "interval_seconds"),
        "RunAtLoad": True,
        "EnvironmentVariables": required_environment,
    }


def test_launchd_plist_validation_covers_hourly_and_watcher() -> None:
    original = {
        "HOME": health.HOME,
        "REPO_DIR": health.REPO_DIR,
        "REFRESH_SCRIPT": health.REFRESH_SCRIPT,
        "WATCHER_SCRIPT": health.WATCHER_SCRIPT,
        "HOURLY_INSTALL_SCRIPT": health.HOURLY_INSTALL_SCRIPT,
        "WATCHER_INSTALL_SCRIPT": health.WATCHER_INSTALL_SCRIPT,
    }
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        try:
            health.HOME = root / "home"
            health.REPO_DIR = root / "repo"
            health.REFRESH_SCRIPT = health.REPO_DIR / "scripts" / "refresh_and_publish.sh"
            health.WATCHER_SCRIPT = health.REPO_DIR / "scripts" / "watch_mission3_onchain_activity.py"
            health.HOURLY_INSTALL_SCRIPT = health.REPO_DIR / "scripts" / "install_hourly_refresh_launchd.sh"
            health.WATCHER_INSTALL_SCRIPT = health.REPO_DIR / "scripts" / "install_auction_watcher_launchd.sh"
            plist_dir = health.HOME / "Library" / "LaunchAgents"
            plist_dir.mkdir(parents=True)

            for service in health.launchd_specs():
                service.plist_path.write_bytes(plistlib.dumps(valid_plist(service)))
                issues: list[str] = []
                assert health.plist_needs_reinstall(issues, service) is False
                assert issues == []

                drifted = valid_plist(service)
                drifted["RunAtLoad"] = False
                service.plist_path.write_bytes(plistlib.dumps(drifted))
                issues = []
                assert health.plist_needs_reinstall(issues, service) is True
                assert "RunAtLoad" in issues[0]
        finally:
            for name, value in original.items():
                setattr(health, name, value)


def test_watcher_state_health() -> None:
    with tempfile.TemporaryDirectory() as directory:
        state_path = Path(directory) / "watcher-state.json"
        now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc).timestamp()
        state_path.write_text(
            json.dumps(
                {
                    "last_checked_at_utc": "2026-08-02T11:59:30Z",
                    "last_checked_block": 123,
                    "last_observed_block": 122,
                    "consecutive_rpc_failures": 0,
                    "consecutive_refresh_failures": 0,
                    "pending_refresh": False,
                    "last_refresh_status": "success",
                }
            ),
            encoding="utf-8",
        )
        issues, summary = health.inspect_watcher_state(now, state_path)
        assert issues == []
        assert summary["last_checked_age_seconds"] == 30

        state_path.write_text(
            json.dumps(
                {
                    "last_checked_at_utc": "2026-08-02T11:30:00Z",
                    "consecutive_rpc_failures": 3,
                    "consecutive_refresh_failures": 4,
                    "pending_refresh": True,
                    "pending_refresh_since_utc": "2026-08-02T11:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        issues, summary = health.inspect_watcher_state(now, state_path)
        assert any("state age" in issue for issue in issues)
        assert any("3 consecutive RPC failures" in issue for issue in issues)
        assert any("4 consecutive refresh failures" in issue for issue in issues)
        assert any("pending refresh age" in issue for issue in issues)
        assert summary["pending_refresh"] is True


def test_log_compaction_is_bounded_and_preserves_launchd_inode() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "launchd.out.log"
        path.write_text("".join(f"line-{index:03d}-{'x' * 24}\n" for index in range(100)), encoding="utf-8")
        inode_before = path.stat().st_ino

        rotated, before, after = health.compact_log_in_place(path, max_bytes=512, retain_bytes=320)

        content = path.read_text(encoding="utf-8")
        assert rotated is True
        assert before > 512
        assert after <= 512
        assert path.stat().st_ino == inode_before
        assert "log compacted in place" in content
        assert "line-099" in content
        assert "line-000" not in content


def test_managed_log_inventory_includes_all_high_growth_jsonl_files() -> None:
    paths = {item.path for item in health.managed_logs()}
    assert health.REPO_DIR / ".local" / "watcher_checks.jsonl" in paths
    assert health.REPO_DIR / ".local" / "refresh_runs.jsonl" in paths
    assert health.REPO_DIR / "logs" / "refresh-metrics.jsonl" in paths


def test_log_compaction_refuses_symlinks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target.log"
        link = root / "health.log"
        target.write_bytes(b"sensitive" * 100)
        link.symlink_to(target)
        try:
            health.compact_log_in_place(link, max_bytes=100, retain_bytes=20)
        except ValueError as exc:
            assert "non-regular" in str(exc)
        else:
            raise AssertionError("expected symlink log compaction to be refused")
        assert target.read_bytes() == b"sensitive" * 100


def test_jsonl_compaction_retains_complete_latest_rows() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "watcher_checks.jsonl"
        path.write_text(
            "".join(json.dumps({"id": index, "result": "no_refresh"}) + "\n" for index in range(100)),
            encoding="utf-8",
        )

        rotated, _before, after = health.compact_log_in_place(path, max_bytes=600, retain_bytes=420)
        rows = health.read_jsonl_tail(path, 100)

        assert rotated is True
        assert after <= 600
        assert rows
        assert rows[-1]["id"] == 99
        assert all(isinstance(row.get("id"), int) for row in rows)


def test_active_log_defers_until_emergency_cap() -> None:
    originals = {
        "managed_logs": health.managed_logs,
        "LOG_MAX_BYTES": health.LOG_MAX_BYTES,
        "LOG_RETAIN_BYTES": health.LOG_RETAIN_BYTES,
        "LOG_EMERGENCY_MAX_BYTES": health.LOG_EMERGENCY_MAX_BYTES,
        "DRY_RUN": health.DRY_RUN,
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "watcher.log"
        try:
            health.LOG_MAX_BYTES = 100
            health.LOG_RETAIN_BYTES = 30
            health.LOG_EMERGENCY_MAX_BYTES = 300
            health.DRY_RUN = False
            health.managed_logs = lambda: (
                health.ManagedLog(path, (health.WATCHER_LABEL,), "watcher test log"),
            )

            path.write_bytes(b"x" * 200)
            lines: list[str] = []
            assert health.rotate_managed_logs(lines, {health.WATCHER_LABEL}) is False
            assert path.stat().st_size == 200
            assert lines == []

            path.write_bytes(b"y" * 400)
            lines = []
            assert health.rotate_managed_logs(lines, {health.WATCHER_LABEL}) is False
            assert path.stat().st_size <= 100
            assert any("emergency compacted" in line for line in lines)
        finally:
            for name, value in originals.items():
                setattr(health, name, value)


def test_disk_free_thresholds() -> None:
    DiskUsage = namedtuple("DiskUsage", "total used free")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        healthy_issues, healthy_summary = health.inspect_disk_free(
            [path],
            min_free_bytes=200,
            min_free_percent=15,
            usage_fn=lambda _path: DiskUsage(1000, 500, 500),
        )
        assert healthy_issues == []
        assert healthy_summary[0]["free_percent"] == 50.0

        low_issues, low_summary = health.inspect_disk_free(
            [path],
            min_free_bytes=200,
            min_free_percent=15,
            usage_fn=lambda _path: DiskUsage(1000, 900, 100),
        )
        assert len(low_issues) == 1
        assert "runner disk free space low" in low_issues[0]
        assert low_summary[0]["free_bytes"] == 100


if __name__ == "__main__":
    test_refresh_lock_detection()
    test_active_lock_metadata_requires_a_held_flock()
    test_fresh_active_attempt_requires_new_held_bounded_run()
    test_active_watcher_filters_only_completion_lag_issues()
    test_launchd_cause_requires_an_explicit_launchd_fault()
    test_expected_live_publish_lag_is_narrow()
    test_launchd_plist_validation_covers_hourly_and_watcher()
    test_watcher_state_health()
    test_log_compaction_is_bounded_and_preserves_launchd_inode()
    test_managed_log_inventory_includes_all_high_growth_jsonl_files()
    test_log_compaction_refuses_symlinks()
    test_jsonl_compaction_retains_complete_latest_rows()
    test_active_log_defers_until_emergency_cap()
    test_disk_free_thresholds()
    print("degen dogs runner health tests passed")
