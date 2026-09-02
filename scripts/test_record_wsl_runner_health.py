#!/usr/bin/env python3
"""POSIX regression tests for the immutable WSL health-state recorder."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if os.name == "posix":
    import pwd
else:
    pwd = None


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 2, 18, 0, 0, tzinfo=timezone.utc)
BOOT_ID = "11111111-2222-3333-4444-555555555555"
INVOCATION_ID = "1" * 32
INSTALL_EPOCH = "2" * 32
RUNTIME_COMMIT = "a" * 40
TRUSTED_COMMIT = "b" * 40


def load_recorder():
    path = ROOT / "scripts" / "record_wsl_runner_health.py"
    spec = importlib.util.spec_from_file_location("record_wsl_runner_health", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def canonical(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def install_identity(*, runner_uid: int | None = None, runner_gid: int | None = None) -> dict[str, object]:
    return {
        "install_epoch": INSTALL_EPOCH,
        "runner_gid": os.getgid() if runner_gid is None else runner_gid,
        "runner_uid": os.getuid() if runner_uid is None else runner_uid,
        "runtime_commit": RUNTIME_COMMIT,
        "schema_version": 1,
        "trusted_installer_commit": TRUSTED_COMMIT,
    }


def make_layout(health, root: Path):
    state = root / "state"
    cache = root / "cache"
    runtime = root / "run" / "health"
    state.mkdir(mode=0o700)
    cache.mkdir(mode=0o700)
    runtime.parent.mkdir(mode=0o700)
    runner_uid = os.getuid()
    runner_gid = os.getgid()
    if os.geteuid() == 0:
        assert pwd is not None
        runner = pwd.getpwnam("nobody")
        runner_uid = runner.pw_uid
        runner_gid = runner.pw_gid
        os.chown(cache, runner_uid, runner_gid)
    identity_path = state / "install.json"
    identity_path.write_bytes(canonical(install_identity(runner_uid=runner_uid, runner_gid=runner_gid)))
    identity_path.chmod(0o600)
    return health.StateLayout(
        state_dir=state,
        runtime_dir=runtime,
        candidate_path=cache / "health-report.json",
        state_uid=os.getuid(),
        state_gid=os.getgid(),
        runner_uid=runner_uid,
        runner_gid=runner_gid,
    )


def write_candidate(
    layout,
    attempt: dict[str, object],
    *,
    invocation_id: str = INVOCATION_ID,
    attempt_token: str | None = None,
    checked_at: datetime = NOW,
    status: str = "healthy",
    failure_codes: list[str] | None = None,
    latest_generated_block: int | None = 50_789_720,
    publication_generation: int | None = 5,
    runner_head: str = RUNTIME_COMMIT,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "attempt_token": attempt_token or str(attempt["attempt_token"]),
        "checked_at_utc": checked_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "failure_codes": [] if failure_codes is None else failure_codes,
        "invocation_id": invocation_id,
        "latest_generated_block": latest_generated_block,
        "publication_generation": publication_generation,
        "runner_head": runner_head,
        "schema_version": 1,
        "status": status,
    }
    if extra:
        record.update(extra)
    layout.candidate_path.write_bytes(canonical(record))
    layout.candidate_path.chmod(0o600)
    if os.geteuid() == 0:
        os.chown(layout.candidate_path, layout.runner_uid, layout.runner_gid)
    return record


def begin(health, layout, invocation_id: str = INVOCATION_ID) -> dict[str, object]:
    return health.begin_health(
        layout,
        invocation_id=invocation_id,
        install=install_identity(runner_uid=layout.runner_uid, runner_gid=layout.runner_gid),
        boot_id=BOOT_ID,
        now=NOW,
    )


def record(health, layout, *, service_result: str = "success", exit_code: str = "exited", exit_status: str = "0"):
    return health.record_health(
        layout,
        service_result=service_result,
        exit_code=exit_code,
        exit_status=exit_status,
        now=NOW,
        boot_id=BOOT_ID,
        uptime_seconds=900.0,
        expected_uid=layout.runner_uid,
    )


def assert_private_regular(path: Path, *, owner: int) -> None:
    details = path.lstat()
    assert stat.S_ISREG(details.st_mode)
    assert details.st_uid == owner
    assert details.st_gid == os.getgid()
    assert stat.S_IMODE(details.st_mode) == 0o600
    assert details.st_nlink == 1


def read_health_state(layout) -> dict[str, object]:
    raw = (layout.state_dir / "state.json").read_bytes()
    value = json.loads(raw.decode("utf-8"))
    assert raw == canonical(value)
    return value


def test_success_requires_candidate_and_systemd_conjunction(health) -> None:
    """Catches a service result or a candidate independently advancing the lease."""
    with tempfile.TemporaryDirectory() as raw:
        layout = make_layout(health, Path(raw))
        attempt = begin(health, layout)
        write_candidate(layout, attempt)
        result = record(health, layout)
        assert result["lease_advanced"] is True
        snap = health.snapshot(layout, boot_id=BOOT_ID, uptime_seconds=901.0)
        assert snap["lease_valid"] is True
        assert snap["lease_age_seconds"] == 1
        assert snap["runner_head"] == RUNTIME_COMMIT
        assert snap["latest_generated_block"] == 50_789_720
        assert snap["publication_generation"] == 5
        assert_private_regular(layout.state_dir / "state.json", owner=os.getuid())
        assert not layout.candidate_path.exists()
        assert not (layout.runtime_dir / "attempt.json").exists()

        old = read_health_state(layout)["last_good"]
        attempt = begin(health, layout, "3" * 32)
        write_candidate(layout, attempt, invocation_id="3" * 32)
        failed = record(health, layout, service_result="timeout", exit_code="killed", exit_status="9")
        assert failed["lease_advanced"] is False
        assert read_health_state(layout)["last_good"] == old


def test_prepare_runtime_creates_root_runner_boundary(health) -> None:
    """Catches a WSL reboot leaving the systemd health namespace path absent or runner-owned."""
    with tempfile.TemporaryDirectory() as raw:
        layout = make_layout(health, Path(raw))
        assert not layout.runtime_dir.exists()
        previous_umask = os.umask(0o077)
        try:
            health.prepare_runtime(layout)
        finally:
            os.umask(previous_umask)
        details = layout.runtime_dir.lstat()
        assert stat.S_ISDIR(details.st_mode)
        assert details.st_uid == os.getuid()
        assert details.st_gid == layout.runner_gid
        assert stat.S_IMODE(details.st_mode) == 0o750


def test_install_identity_removes_legacy_split_state(health) -> None:
    """Catches an upgrade leaving obsolete split records that operators could mistake for authority."""
    with tempfile.TemporaryDirectory() as raw:
        layout = make_layout(health, Path(raw))
        legacy_paths = (
            layout.state_dir / "last-good.json",
            layout.state_dir / "incident.json",
        )
        for path in legacy_paths:
            path.write_bytes(canonical({"obsolete": True}))
            path.chmod(0o600)
        health.write_install_identity(
            layout,
            install_identity(runner_uid=layout.runner_uid, runner_gid=layout.runner_gid),
        )
        assert all(not path.exists() for path in legacy_paths)


def test_existing_wrong_mode_state_lock_is_rejected_without_repair(health) -> None:
    """Catches a reader silently normalizing a pre-existing unsafe lock inode."""
    with tempfile.TemporaryDirectory() as raw:
        layout = make_layout(health, Path(raw))
        lock_path = layout.state_dir / "state.lock"
        lock_path.touch(mode=0o600)
        lock_path.chmod(0o640)
        result = health.snapshot(layout, boot_id=BOOT_ID, uptime_seconds=900.0)
        assert result["lease_reason"] == "state_invalid"
        assert stat.S_IMODE(lock_path.lstat().st_mode) == 0o640


def test_missing_mismatched_replayed_and_unhealthy_candidates_never_advance(health) -> None:
    """Catches missing/replayed/mismatched probe evidence being accepted as success."""
    cases = (
        "missing",
        "wrong_invocation",
        "wrong_token",
        "future",
        "old",
        "unhealthy",
    )
    for case in cases:
        with tempfile.TemporaryDirectory() as raw:
            layout = make_layout(health, Path(raw))
            attempt = begin(health, layout)
            if case != "missing":
                kwargs = {}
                if case == "wrong_invocation":
                    kwargs["invocation_id"] = "4" * 32
                elif case == "wrong_token":
                    kwargs["attempt_token"] = "5" * 64
                elif case == "future":
                    kwargs["checked_at"] = NOW + timedelta(seconds=1)
                elif case == "old":
                    kwargs["checked_at"] = NOW - timedelta(seconds=121)
                elif case == "unhealthy":
                    kwargs.update(status="unhealthy", failure_codes=["remote_dashboard_unhealthy"])
                write_candidate(layout, attempt, **kwargs)
            result = record(health, layout)
            assert result["lease_advanced"] is False, case
            assert read_health_state(layout)["last_good"] is None, case
            assert health.snapshot(layout, boot_id=BOOT_ID, uptime_seconds=901.0)["lease_valid"] is False

            replay = record(health, layout)
            assert replay["lease_advanced"] is False
            assert replay["failure_codes"] == ["candidate_missing"]


def test_candidate_schema_canonicality_bounds_and_file_identity_are_fail_closed(health) -> None:
    """Catches untrusted candidate bytes, links, permissions, and shapes reaching durable state."""
    mutations = (
        "extra_key",
        "bad_schema",
        "bad_sha",
        "bad_block",
        "bad_generation",
        "bad_code",
        "duplicate_codes",
        "noncanonical",
        "oversized",
        "wrong_mode",
        "hardlink",
        "symlink",
        "fifo",
    )
    for mutation in mutations:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            layout = make_layout(health, root)
            attempt = begin(health, layout)
            record_value = write_candidate(layout, attempt)
            if mutation == "extra_key":
                record_value["unexpected"] = True
            elif mutation == "bad_schema":
                record_value["schema_version"] = 2
            elif mutation == "bad_sha":
                record_value["runner_head"] = "A" * 40
            elif mutation == "bad_block":
                record_value["latest_generated_block"] = True
            elif mutation == "bad_generation":
                record_value["publication_generation"] = -1
            elif mutation == "bad_code":
                record_value["failure_codes"] = ["/secret/provider/url"]
            elif mutation == "duplicate_codes":
                record_value["failure_codes"] = ["git_unhealthy", "git_unhealthy"]
            if mutation in {
                "extra_key", "bad_schema", "bad_sha", "bad_block", "bad_generation", "bad_code", "duplicate_codes"
            }:
                layout.candidate_path.write_bytes(canonical(record_value))
                layout.candidate_path.chmod(0o600)
            elif mutation == "noncanonical":
                layout.candidate_path.write_text(json.dumps(record_value, indent=2), encoding="utf-8")
                layout.candidate_path.chmod(0o600)
            elif mutation == "oversized":
                layout.candidate_path.write_bytes(b"{" + b" " * (health.CANDIDATE_MAX_BYTES + 1))
                layout.candidate_path.chmod(0o600)
            elif mutation == "wrong_mode":
                layout.candidate_path.chmod(0o640)
            elif mutation == "hardlink":
                os.link(layout.candidate_path, root / "candidate-link")
            elif mutation == "symlink":
                target = root / "candidate-target"
                layout.candidate_path.replace(target)
                layout.candidate_path.symlink_to(target)
            elif mutation == "fifo":
                layout.candidate_path.unlink()
                os.mkfifo(layout.candidate_path, mode=0o600)
            started = time.monotonic()
            previous_alarm_handler = None
            if mutation == "fifo":
                previous_alarm_handler = signal.signal(
                    signal.SIGALRM,
                    lambda *_args: (_ for _ in ()).throw(TimeoutError("fifo open blocked")),
                )
                signal.alarm(1)
            try:
                result = record(health, layout)
            finally:
                signal.alarm(0)
                if previous_alarm_handler is not None:
                    signal.signal(signal.SIGALRM, previous_alarm_handler)
            if mutation == "fifo":
                assert time.monotonic() - started < 0.5, "candidate FIFO blocked before fstat rejection"
            assert result["lease_advanced"] is False, mutation
            assert read_health_state(layout)["last_good"] is None, mutation


def test_install_boot_and_monotonic_binding_invalidates_lease(health) -> None:
    """Catches an old install, old WSL boot, or future monotonic completion looking fresh."""
    with tempfile.TemporaryDirectory() as raw:
        layout = make_layout(health, Path(raw))
        attempt = begin(health, layout)
        write_candidate(layout, attempt)
        assert record(health, layout)["lease_advanced"] is True
        assert health.snapshot(layout, boot_id="99999999-2222-3333-4444-555555555555", uptime_seconds=901.0)["lease_reason"] == "boot_mismatch"
        assert health.snapshot(layout, boot_id=BOOT_ID, uptime_seconds=899.0)["lease_reason"] == "monotonic_regression"
        assert health.snapshot(layout, boot_id=BOOT_ID, uptime_seconds=1381.0)["lease_reason"] == "stale"
        assert health.snapshot(layout, boot_id=BOOT_ID, uptime_seconds=1380.0)["lease_valid"] is True

        changed = install_identity(runner_uid=layout.runner_uid, runner_gid=layout.runner_gid)
        changed["install_epoch"] = "9" * 32
        (layout.state_dir / "install.json").write_bytes(canonical(changed))
        (layout.state_dir / "install.json").chmod(0o600)
        assert health.snapshot(layout, boot_id=BOOT_ID, uptime_seconds=901.0)["lease_reason"] == "install_mismatch"


def test_four_failures_preserve_first_failure_and_success_records_recovery(health) -> None:
    """Catches incident evidence being reset/lost or audit evidence being erased on recovery."""
    with tempfile.TemporaryDirectory() as raw:
        layout = make_layout(health, Path(raw))
        for index in range(4):
            current = NOW + timedelta(seconds=index * 10)
            attempt = health.begin_health(
                layout,
                invocation_id=f"{index + 1:x}" * 32,
                install=install_identity(runner_uid=layout.runner_uid, runner_gid=layout.runner_gid),
                boot_id=BOOT_ID,
                now=current,
            )
            result = health.record_health(
                layout,
                service_result="timeout",
                exit_code="killed",
                exit_status="9",
                now=current,
                boot_id=BOOT_ID,
                uptime_seconds=900.0 + index * 10,
                expected_uid=layout.runner_uid,
            )
            assert result["lease_advanced"] is False
        incident = read_health_state(layout)["incident"]
        assert incident["health"]["consecutive_failures"] == 4
        assert incident["health"]["first_failure_at_utc"] == "2026-09-02T18:00:00Z"
        assert incident["health"]["last_failure_at_utc"] == "2026-09-02T18:00:30Z"

        audit = {
            "checked_at_utc": "2026-09-02T18:00:31Z",
            "failure_codes": ["anchor_unreachable"],
            "status": "unhealthy",
        }
        health.record_audit_mirror(layout, audit)
        recovered_at = NOW + timedelta(seconds=40)
        attempt = health.begin_health(
            layout,
            invocation_id="f" * 32,
            install=install_identity(runner_uid=layout.runner_uid, runner_gid=layout.runner_gid),
            boot_id=BOOT_ID,
            now=recovered_at,
        )
        write_candidate(layout, attempt, invocation_id="f" * 32, checked_at=recovered_at)
        result = health.record_health(
            layout,
            service_result="success",
            exit_code="exited",
            exit_status="0",
            now=recovered_at,
            boot_id=BOOT_ID,
            uptime_seconds=940.0,
            expected_uid=layout.runner_uid,
        )
        assert result["lease_advanced"] is True
        incident = read_health_state(layout)["incident"]
        assert incident["health"] is None
        assert incident["audit_mirror"] == audit
        assert incident["last_recovery"]["consecutive_failures"] == 4
        assert incident["last_recovery"]["recovered_at_utc"] == "2026-09-02T18:00:40Z"


def test_backward_wall_clock_clamps_incident_and_recovery_timestamps(health) -> None:
    """Catches backward WSL wall time blocking durable incident or recovery progress."""
    with tempfile.TemporaryDirectory() as raw:
        layout = make_layout(health, Path(raw))
        begin(health, layout)
        first = record(
            health,
            layout,
            service_result="timeout",
            exit_code="killed",
            exit_status="9",
        )
        assert first["lease_advanced"] is False

        backward = NOW - timedelta(hours=2)
        health.begin_health(
            layout,
            invocation_id="3" * 32,
            install=install_identity(runner_uid=layout.runner_uid, runner_gid=layout.runner_gid),
            boot_id=BOOT_ID,
            now=backward,
        )
        second = health.record_health(
            layout,
            service_result="timeout",
            exit_code="killed",
            exit_status="9",
            now=backward,
            boot_id=BOOT_ID,
            uptime_seconds=910.0,
            expected_uid=layout.runner_uid,
        )
        assert second["lease_advanced"] is False
        incident = read_health_state(layout)["incident"]
        assert incident["health"]["consecutive_failures"] == 2
        assert incident["health"]["first_failure_at_utc"] == "2026-09-02T18:00:00Z"
        assert incident["health"]["last_failure_at_utc"] == "2026-09-02T18:00:00Z"

        recovered_wall_time = backward + timedelta(seconds=10)
        attempt = health.begin_health(
            layout,
            invocation_id="4" * 32,
            install=install_identity(runner_uid=layout.runner_uid, runner_gid=layout.runner_gid),
            boot_id=BOOT_ID,
            now=recovered_wall_time,
        )
        write_candidate(
            layout,
            attempt,
            invocation_id="4" * 32,
            checked_at=recovered_wall_time,
        )
        recovered = health.record_health(
            layout,
            service_result="success",
            exit_code="exited",
            exit_status="0",
            now=recovered_wall_time,
            boot_id=BOOT_ID,
            uptime_seconds=920.0,
            expected_uid=layout.runner_uid,
        )
        assert recovered["lease_advanced"] is True
        incident = read_health_state(layout)["incident"]
        assert incident["health"] is None
        assert incident["last_recovery"]["recovered_at_utc"] == "2026-09-02T18:00:00Z"

        after_recovery_wall_time = backward + timedelta(seconds=20)
        health.begin_health(
            layout,
            invocation_id="5" * 32,
            install=install_identity(runner_uid=layout.runner_uid, runner_gid=layout.runner_gid),
            boot_id=BOOT_ID,
            now=after_recovery_wall_time,
        )
        failed_again = health.record_health(
            layout,
            service_result="timeout",
            exit_code="killed",
            exit_status="9",
            now=after_recovery_wall_time,
            boot_id=BOOT_ID,
            uptime_seconds=930.0,
            expected_uid=layout.runner_uid,
        )
        assert failed_again["lease_advanced"] is False
        incident = read_health_state(layout)["incident"]
        assert incident["health"]["first_failure_at_utc"] == "2026-09-02T18:00:00Z"
        assert incident["health"]["last_failure_at_utc"] == "2026-09-02T18:00:00Z"


def test_atomic_replace_failure_preserves_prior_lease(health) -> None:
    """Catches a failed durable replacement partially advancing or deleting the prior lease."""
    with tempfile.TemporaryDirectory() as raw:
        layout = make_layout(health, Path(raw))
        attempt = begin(health, layout)
        write_candidate(layout, attempt)
        assert record(health, layout)["lease_advanced"] is True
        before = (layout.state_dir / "state.json").read_bytes()

        later = NOW + timedelta(seconds=10)
        attempt = health.begin_health(
            layout,
            invocation_id="e" * 32,
            install=install_identity(runner_uid=layout.runner_uid, runner_gid=layout.runner_gid),
            boot_id=BOOT_ID,
            now=later,
        )
        write_candidate(layout, attempt, invocation_id="e" * 32, checked_at=later)
        original_replace = health.os.replace

        def fail_state(source, destination, *args, **kwargs):  # noqa: ANN001
            if destination == "state.json" or str(destination).endswith("state.json"):
                raise OSError("injected replacement failure")
            return original_replace(source, destination, *args, **kwargs)

        health.os.replace = fail_state
        try:
            try:
                health.record_health(
                    layout,
                    service_result="success",
                    exit_code="exited",
                    exit_status="0",
                    now=later,
                    boot_id=BOOT_ID,
                    uptime_seconds=910.0,
                    expected_uid=layout.runner_uid,
                )
            except OSError as exc:
                assert "injected" in str(exc)
            else:
                raise AssertionError("atomic replacement failure was swallowed")
        finally:
            health.os.replace = original_replace
        assert (layout.state_dir / "state.json").read_bytes() == before


def test_incident_commit_failure_preserves_prior_lease_and_incident(health) -> None:
    """Catches a later incident write failure exposing an otherwise uncommitted lease."""
    with tempfile.TemporaryDirectory() as raw:
        layout = make_layout(health, Path(raw))
        attempt = begin(health, layout)
        write_candidate(layout, attempt)
        assert record(health, layout)["lease_advanced"] is True

        failed_at = NOW + timedelta(seconds=10)
        attempt = health.begin_health(
            layout,
            invocation_id="d" * 32,
            install=install_identity(runner_uid=layout.runner_uid, runner_gid=layout.runner_gid),
            boot_id=BOOT_ID,
            now=failed_at,
        )
        write_candidate(layout, attempt, invocation_id="d" * 32, checked_at=failed_at)
        failed = health.record_health(
            layout,
            service_result="timeout",
            exit_code="killed",
            exit_status="9",
            now=failed_at,
            boot_id=BOOT_ID,
            uptime_seconds=910.0,
            expected_uid=layout.runner_uid,
        )
        assert failed["lease_advanced"] is False
        prior_state = (layout.state_dir / "state.json").read_bytes()

        recovered_at = NOW + timedelta(seconds=20)
        attempt = health.begin_health(
            layout,
            invocation_id="e" * 32,
            install=install_identity(runner_uid=layout.runner_uid, runner_gid=layout.runner_gid),
            boot_id=BOOT_ID,
            now=recovered_at,
        )
        write_candidate(layout, attempt, invocation_id="e" * 32, checked_at=recovered_at)
        original_replace = health.os.replace

        def fail_authoritative_state(source, destination, *args, **kwargs):  # noqa: ANN001
            if destination == "state.json" or str(destination).endswith("state.json"):
                raise OSError("injected authoritative state commit failure")
            return original_replace(source, destination, *args, **kwargs)

        health.os.replace = fail_authoritative_state
        try:
            try:
                health.record_health(
                    layout,
                    service_result="success",
                    exit_code="exited",
                    exit_status="0",
                    now=recovered_at,
                    boot_id=BOOT_ID,
                    uptime_seconds=920.0,
                    expected_uid=layout.runner_uid,
                )
            except OSError as exc:
                assert "injected" in str(exc)
            else:
                raise AssertionError("authoritative state commit failure was swallowed")
        finally:
            health.os.replace = original_replace
        assert (layout.state_dir / "state.json").read_bytes() == prior_state


def test_missing_candidate_parent_records_failure_without_advancing(health) -> None:
    """Catches runner cache corruption preventing the root incident from being committed."""
    with tempfile.TemporaryDirectory() as raw:
        layout = make_layout(health, Path(raw))
        begin(health, layout)
        layout.candidate_path.parent.rmdir()
        result = record(health, layout)
        assert result["lease_advanced"] is False
        assert result["failure_codes"] == ["candidate_invalid"]
        health_state = read_health_state(layout)
        assert health_state["last_good"] is None
        assert health_state["incident"]["health"]["failure_codes"] == ["candidate_invalid"]


def test_production_root_layout_uses_install_identity_without_runner_cache(health) -> None:
    """Catches snapshot/runtime preparation depending on the runner-owned cache directory."""
    if os.geteuid() != 0:
        return
    assert pwd is not None
    runner = pwd.getpwnam("nobody")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        state = root / "state"
        runtime = root / "run" / "health"
        state.mkdir(mode=0o700)
        runtime.parent.mkdir(mode=0o700)
        install = {
            **install_identity(),
            "runner_gid": runner.pw_gid,
            "runner_uid": runner.pw_uid,
        }
        identity_path = state / "install.json"
        identity_path.write_bytes(canonical(install))
        identity_path.chmod(0o600)
        previous = (
            health.PRODUCTION_STATE_DIR,
            health.PRODUCTION_RUNTIME_DIR,
            health.PRODUCTION_CANDIDATE_PATH,
        )
        health.PRODUCTION_STATE_DIR = state
        health.PRODUCTION_RUNTIME_DIR = runtime
        health.PRODUCTION_CANDIDATE_PATH = root / "missing-cache" / "health-report.json"
        try:
            layout = health._production_layout()
            assert layout.runner_uid == runner.pw_uid
            assert layout.runner_gid == runner.pw_gid
            assert health.prepare_runtime(layout)["runtime_ready"] is True
            assert health.snapshot(layout, boot_id=BOOT_ID, uptime_seconds=900.0)["lease_reason"] == "missing"
        finally:
            (
                health.PRODUCTION_STATE_DIR,
                health.PRODUCTION_RUNTIME_DIR,
                health.PRODUCTION_CANDIDATE_PATH,
            ) = previous


def test_concurrent_mirror_writers_leave_one_canonical_record(health) -> None:
    """Catches missing flock serialization corrupting or linking the durable incident record."""
    with tempfile.TemporaryDirectory() as raw:
        layout = make_layout(health, Path(raw))
        children: list[int] = []
        for index in range(6):
            child = os.fork()
            if child == 0:
                try:
                    health.record_audit_mirror(
                        layout,
                        {
                            "checked_at_utc": f"2026-09-02T18:00:{index:02d}Z",
                            "failure_codes": [],
                            "status": "healthy",
                        },
                    )
                except BaseException:
                    os._exit(1)
                os._exit(0)
            children.append(child)
        for child in children:
            _pid, status = os.waitpid(child, 0)
            assert os.waitstatus_to_exitcode(status) == 0
        raw_state = (layout.state_dir / "state.json").read_bytes()
        health_state = json.loads(raw_state.decode("utf-8"))
        assert raw_state == canonical(health_state)
        incident = health_state["incident"]
        assert incident["audit_mirror"]["status"] == "healthy"
        assert_private_regular(layout.state_dir / "state.json", owner=os.getuid())


def test_root_recorder_rejects_wrong_runner_owner(health) -> None:
    """Catches a root- or foreign-owned candidate satisfying the runner evidence boundary."""
    if os.geteuid() != 0:
        return
    assert pwd is not None
    runner = pwd.getpwnam("nobody")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        state = root / "state"
        cache = root / "cache"
        runtime = root / "run" / "health"
        state.mkdir(mode=0o700)
        cache.mkdir(mode=0o700)
        os.chown(cache, runner.pw_uid, runner.pw_gid)
        runtime.parent.mkdir(mode=0o700)
        identity_path = state / "install.json"
        identity_path.write_bytes(
            canonical(install_identity(runner_uid=runner.pw_uid, runner_gid=runner.pw_gid))
        )
        identity_path.chmod(0o600)
        layout = health.StateLayout(
            state_dir=state,
            runtime_dir=runtime,
            candidate_path=cache / "health-report.json",
            state_uid=0,
            state_gid=0,
            runner_uid=runner.pw_uid,
            runner_gid=runner.pw_gid,
        )
        attempt = begin(health, layout)
        write_candidate(layout, attempt)
        os.chown(layout.candidate_path, runner.pw_uid, runner.pw_gid)
        assert health.record_health(
            layout,
            service_result="success",
            exit_code="exited",
            exit_status="0",
            now=NOW,
            boot_id=BOOT_ID,
            uptime_seconds=900.0,
            expected_uid=runner.pw_uid,
        )["lease_advanced"] is True
        prior = read_health_state(layout)["last_good"]

        later = NOW + timedelta(seconds=10)
        attempt = health.begin_health(
            layout,
            invocation_id="e" * 32,
            install=install_identity(runner_uid=layout.runner_uid, runner_gid=layout.runner_gid),
            boot_id=BOOT_ID,
            now=later,
        )
        write_candidate(layout, attempt, invocation_id="e" * 32, checked_at=later)
        # The root-created candidate deliberately retains uid/gid 0 here.
        os.chown(layout.candidate_path, 0, 0)
        result = health.record_health(
            layout,
            service_result="success",
            exit_code="exited",
            exit_status="0",
            now=later,
            boot_id=BOOT_ID,
            uptime_seconds=910.0,
            expected_uid=runner.pw_uid,
        )
        assert result["lease_advanced"] is False
        assert result["failure_codes"] == ["candidate_invalid"]
        assert read_health_state(layout)["last_good"] == prior


def main() -> int:
    if os.name != "posix":
        print("record_wsl_runner_health=skip reason=requires_posix_file_identity")
        return 0
    health = load_recorder()
    test_success_requires_candidate_and_systemd_conjunction(health)
    test_prepare_runtime_creates_root_runner_boundary(health)
    test_install_identity_removes_legacy_split_state(health)
    test_existing_wrong_mode_state_lock_is_rejected_without_repair(health)
    test_missing_mismatched_replayed_and_unhealthy_candidates_never_advance(health)
    test_candidate_schema_canonicality_bounds_and_file_identity_are_fail_closed(health)
    test_install_boot_and_monotonic_binding_invalidates_lease(health)
    test_four_failures_preserve_first_failure_and_success_records_recovery(health)
    test_backward_wall_clock_clamps_incident_and_recovery_timestamps(health)
    test_atomic_replace_failure_preserves_prior_lease(health)
    test_incident_commit_failure_preserves_prior_lease_and_incident(health)
    test_missing_candidate_parent_records_failure_without_advancing(health)
    test_production_root_layout_uses_install_identity_without_runner_cache(health)
    test_concurrent_mirror_writers_leave_one_canonical_record(health)
    test_root_recorder_rejects_wrong_runner_owner(health)
    print("record_wsl_runner_health=pass cases=15")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
