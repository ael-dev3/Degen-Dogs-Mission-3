#!/usr/bin/env python3
"""Deterministic ext4 proof for delayed Pages verification and queue draining."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> Any:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


state = load("runner_publication_state")
drainer = load("drain_publication_queue")
verifier = load("verify_pages_deployment")
health = load("check_wsl_runner_health")

EXPECTED_ACTIVATION_UNITS = (
    "degen-dogs-runner.target",
    "degen-dogs-watcher.timer",
    "degen-dogs-hourly.timer",
    "degen-dogs-health.timer",
    "degen-dogs-publisher.path",
    "degen-dogs-publisher.timer",
    "degen-dogs-pages-verifier.path",
    "degen-dogs-pages-verifier.timer",
)
EXPECTED_WORKER_UNITS = (
    "degen-dogs-watcher.service",
    "degen-dogs-hourly.service",
    "degen-dogs-publisher.service",
    "degen-dogs-pages-verifier.service",
)


class FakeLock:
    def __enter__(self) -> "FakeLock":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class HeldRefreshLock:
    def __init__(self, path: Path) -> None:
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.active = False

    def __enter__(self) -> int:
        self.active = True
        return self.fd

    def __exit__(self, *_args: object) -> None:
        self.active = False

    def close(self) -> None:
        os.close(self.fd)


class Process:
    def __init__(self, callback) -> None:  # noqa: ANN001
        self.callback = callback
        self.returncode: int | None = None
        self.pid = 7001

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        self.callback()
        self.returncode = 0
        return 0


class Clock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        self.monotonic_seconds = 0.0
        self.sleeps: list[float] = []
        self.on_first_sleep = None

    def utc_now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.on_first_sleep is not None:
            callback, self.on_first_sleep = self.on_first_sleep, None
            callback()
        self.monotonic_seconds += seconds
        self.wall += timedelta(seconds=seconds)


class Transport:
    def __init__(self, status: bytes, bundle: bytes, clock: Clock) -> None:
        self.status = status
        self.bundle = bundle
        self.clock = clock
        self.calls: list[str] = []

    def fetch(self, _url: str, resource: str, _cap: int, _timeout: float, *, absolute_deadline: float | None = None) -> bytes:
        del absolute_deadline
        self.calls.append(resource)
        if resource == "raw_status":
            return self.status
        if resource == "raw_bundle":
            return self.bundle
        if resource == "pages_status":
            return self.status if self.clock.monotonic_seconds >= 120 else b'{"stale":true}\n'
        if resource == "pages_bundle":
            return self.bundle
        raise AssertionError(f"unexpected verifier resource: {resource}")


class PortableState:
    PendingFinalizeResult = state.PendingFinalizeResult

    def read_pending_with_digest(self, lock_dir: Path) -> Any:
        return state.read_pending_with_digest(lock_dir, lock_context=FakeLock())

    def cas_write_pending(self, lock_dir: Path, generation: int, commit_sha: str, replacement: dict[str, Any]) -> bool:
        return state.cas_write_pending(lock_dir, generation, commit_sha, replacement, lock_context=FakeLock())

    def finalize_verified_pending(self, lock_dir: Path, captured: Any, verified_at: str) -> Any:
        return state.finalize_verified_pending(lock_dir, captured, verified_at, lock_context=FakeLock())


def observation(block: int, hash_char: str) -> dict[str, Any]:
    block_hash = "0x" + hash_char * 64
    return {
        "confirmed_block_number": block,
        "confirmed_block_hash": block_hash,
        "confirmed_block_time_utc": "2026-08-31T11:59:00Z",
        "token_id": "818",
        "amount_wei": "1",
        "start_time_unix": "1780000000",
        "end_time_unix": "1780003600",
        "bidder_wallet": "0x" + "1" * 40,
        "settled": False,
        "event_name": None,
        "event_tx_hash": None,
        "event_log_index": None,
        "event_block_number": None,
        "event_block_hash": None,
        "event_block_time_utc": None,
        "canonical_reorg_from_hash": None,
    }


def proof(generation: int, digest: str) -> tuple[bytes, bytes, dict[str, Any]]:
    block = 701
    block_hash = "0x" + "a" * 64
    bundle = (json.dumps({
        "auction_feed": [], "current_auction": [], "current_auction_bid_history": [],
        "kind": "degen_dogs_live_snapshot", "latest_generated_block": block,
        "mission3_metrics": [], "schema_version": 1, "snapshot_block_hash": block_hash,
    }, sort_keys=True, separators=(",", ":")) + "\n").encode()
    bundle_digest = hashlib.sha256(bundle).hexdigest()
    filename = f"live_snapshot_{block}_{block_hash[2:]}_{bundle_digest}.json"
    status = (json.dumps({
        "kind": "refresh_status", "latest_generated_block": block,
        "live_snapshot_bundle": filename, "live_snapshot_bundle_bytes": len(bundle),
        "live_snapshot_bundle_schema_version": 1, "live_snapshot_bundle_sha256": bundle_digest,
        "schema_version": 1, "snapshot_block_hash": block_hash,
    }, indent=2, sort_keys=True) + "\n").encode()
    return status, bundle, {
        "schema_version": 1, "generation": generation, "queue_digest": digest, "commit_sha": "a" * 40,
        "raw_status_path": "public/generated/refresh_status.json",
        "raw_bundle_path": f"public/generated/{filename}", "expected_bundle_sha256": bundle_digest,
        "expected_bundle_bytes": len(bundle), "expected_block_number": block,
        "expected_block_hash": block_hash, "push_completed_at_utc": "2026-08-31T12:00:00Z",
        "retry_deadline_utc": "2026-08-31T12:10:00Z", "retry_count": 0,
    }


def drain_once(root: Path, lock_dir: Path, launched: list[int]) -> None:
    owned = HeldRefreshLock(lock_dir / "refresh.lock")
    try:
        def launch(_argv: list[str], **kwargs: Any) -> Process:
            launched.append(int(kwargs["env"]["DEGEN_DOGS_PUBLICATION_GENERATION"]))
            return Process(lambda: None)
        assert drainer.drain_publication_queue(
            repo_dir=root / "repo", lock_dir=lock_dir, refresh_lock_path=lock_dir / "refresh.lock",
            base_env={"PATH": "/usr/bin:/bin"}, process_launcher=launch,
            lock_factory=lambda _path, **_kwargs: owned,
            finalize_handoff=lambda path, generation, digest: state.cas_clear_latest(path, generation, digest),
        ) == 0
    finally:
        owned.close()


def test_pages_delay_keeps_newer_observation_and_drains_it_next() -> None:
    """Catches a verifier delay blocking observation persistence or newest-first draining."""
    unavailable = health.publication_health_summary(
        {"latest": None, "pending": None, "checkpoint": None, "pages_verified": None, "journal": None, "last_generation": 0},
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
        publisher_lock_active=False,
    )
    assert unavailable["queue_mode"] is False
    assert unavailable["last_direct_data_compatible_static_block"] is None

    # The runner's path-security model deliberately rejects world-writable
    # ancestors such as /tmp.  Release gates provide a private 0700 TMPDIR;
    # honor it instead of bypassing it with a hard-coded parent.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "repo" / "scripts").mkdir(parents=True)
        lock_dir = root / "state"
        first = state.enqueue_latest_observation(
            lock_dir, observation(700, "a"), runner_id="windows-wsl", run_scope="current",
            created_at_utc="2026-08-31T12:00:00Z", lock_context=FakeLock(),
        )
        launched: list[int] = []
        drain_once(root, lock_dir, launched)
        assert launched == [first.generation]
        assert state.read_latest_with_digest(lock_dir) is None

        status, bundle, pending = proof(first.generation, first.digest)
        state.atomic_write_record(state.state_paths(lock_dir).pending, pending)
        clock = Clock()

        def enqueue_later() -> None:
            state.enqueue_latest_observation(
                lock_dir, observation(702, "b"), runner_id="windows-wsl", run_scope="current",
                created_at_utc="2026-08-31T12:00:05Z", lock_context=FakeLock(),
            )

        clock.on_first_sleep = enqueue_later
        result = verifier.run_once(
            lock_dir, root / "private-logs", state_api=PortableState(), transport=Transport(status, bundle, clock),
            telemetry_writer=lambda _directory, _row: None, utc_now=clock.utc_now,
            monotonic=clock.monotonic, sleep=clock.sleep,
            config=verifier.VerifierConfig(130, 5, 3),
        )
        assert (result.exit_code, result.result) == (0, "verified_cleared")
        assert clock.monotonic_seconds == 120
        newer = state.read_latest_with_digest(lock_dir)
        assert newer is not None and newer[0]["generation"] == first.generation + 1

        drain_once(root, lock_dir, launched)
        assert launched == [first.generation, first.generation + 1]


def test_public_queue_health_uses_receipt_not_observation_and_exposes_no_private_values() -> None:
    """Catches a public health summary inferring deployed state from queue data."""
    commit = "a" * 40
    snapshot = {
        "last_generation": 5,
        "latest": None,
        "pending": None,
        "journal": None,
        "checkpoint": {
            "outcome": "no_diff", "generation": 5, "queue_digest": "e" * 64,
            "commit_sha": None, "push_completed_at_utc": None,
            "publication_target": {
                "observation": {
                    "bidder_wallet": "private-target-wallet",
                    "provider_evidence": "private-target-provider",
                },
            },
            "coverage_proof": {
                "bundle_path": "private-proof-bundle-path",
                "quorum_attestation": {"providers": "private-proof-provider"},
            },
        },
        "pages_verified": {
            "generation": 4, "queue_digest": "d" * 64, "commit_sha": commit,
            "expected_block_number": 701, "expected_block_hash": "0x" + "b" * 64,
            "push_completed_at_utc": "2026-08-31T11:40:00Z",
            "pages_verified_at_utc": "2026-08-31T11:41:00Z",
            "raw_status_path": "private/secret-path", "proof_fingerprint": "private-proof",
        },
    }
    summary = health.publication_health_summary(
        snapshot, now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc), publisher_lock_active=False,
    )
    assert summary["queue_mode"] is True
    assert summary["handled_generation"] == 5
    assert summary["handled_pushed_generation"] is None
    assert summary["last_direct_data_compatible_static_block"] == 701
    assert summary["pages_verification_state"] == "verified"
    assert summary["problems"] == []
    rendered = json.dumps(summary, sort_keys=True)
    for forbidden in (
        "secret-path",
        "private-proof",
        "private-target",
        "queue_digest",
        "expected_block_hash",
        "coverage_proof",
        "publication_target",
    ):
        assert forbidden not in rendered
    report = health.public_health_report(
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
        problems=["private failure https://rpc.example/key"], warnings=["C:\\Users\\alice\\repo"],
        checks={
            "refresh_lock_active": True,
            "timers": {"timer": {"error": "host alice.example"}},
            "workers": {},
            "publication": summary,
            "remote": {"incident": True, "raw_problem": "https://rpc.example/key"},
            "git": {"tracked_dirty": False, "branch": "alice/private"},
            "filesystems": [{"path": "/private/alice", "error": None}],
        },
    )
    rendered = json.dumps(report, sort_keys=True)
    for forbidden in (
        "rpc.example",
        "alice",
        "C:\\Users",
        "secret-path",
        "private-proof",
        "private-target",
        "coverage_proof",
        "publication_target",
    ):
        assert forbidden not in rendered


def systemd_properties_fixture(
    *,
    load_state: str = "loaded",
    active_state: str = "active",
    sub_state: str = "waiting",
    unit_file_state: str = "enabled",
    result: str = "success",
) -> dict[str, str]:
    """Mirror the complete bounded ``systemctl show`` property response."""
    return {
        "LoadState": load_state,
        "ActiveState": active_state,
        "SubState": sub_state,
        "UnitFileState": unit_file_state,
        "Result": result,
    }


def inspect_systemd_with(rows: dict[str, dict[str, str]]) -> tuple[list[str], dict[str, Any], list[str]]:
    calls: list[str] = []
    original = health.systemd_properties
    try:
        def fake_properties(unit: str) -> dict[str, str]:
            calls.append(unit)
            return dict(rows[unit])

        health.systemd_properties = fake_properties
        problems, checks = health.inspect_systemd_inventory()
        return problems, checks, calls
    finally:
        health.systemd_properties = original


def public_systemd_report(
    problems: list[str],
    checks: dict[str, Any],
    *,
    publication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if publication is None:
        publication = health.publication_health_summary(
            {
                "last_generation": 0,
                "latest": None,
                "pending": None,
                "checkpoint": None,
                "pages_verified": None,
                "journal": None,
            },
            now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
            publisher_lock_active=False,
        )
    return health.public_health_report(
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
        problems=problems,
        warnings=[],
        checks={
            **checks,
            "publication": publication,
            "remote": {"incident": False},
            "git": {"tracked_dirty": False},
            "filesystems": [],
        },
    )


def test_systemd_health_inventory_checks_every_required_unit_exactly_once() -> None:
    """Catches any required queue activation or worker disappearing from health."""
    rows = {
        unit: systemd_properties_fixture(unit_file_state="enabled-runtime")
        for unit in EXPECTED_ACTIVATION_UNITS
    }
    rows.update({
        "degen-dogs-watcher.service": systemd_properties_fixture(
            active_state="inactive",
            sub_state="dead",
            unit_file_state="disabled",
            result="",
        ),
        "degen-dogs-hourly.service": systemd_properties_fixture(
            active_state="activating",
            sub_state="start",
            unit_file_state="disabled",
            result="",
        ),
        "degen-dogs-publisher.service": systemd_properties_fixture(
            active_state="inactive",
            sub_state="dead",
            unit_file_state="disabled",
            result="success",
        ),
        # The verifier wrapper maps its intentional exit 2 to systemd success.
        "degen-dogs-pages-verifier.service": systemd_properties_fixture(
            active_state="inactive",
            sub_state="dead",
            unit_file_state="disabled",
            result="success",
        ),
    })

    problems, checks, calls = inspect_systemd_with(rows)
    report = public_systemd_report(problems, checks)

    assert calls == list(EXPECTED_ACTIVATION_UNITS + EXPECTED_WORKER_UNITS)
    assert len(calls) == len(set(calls))
    assert "degen-dogs-health.service" not in calls
    assert problems == []
    assert report["status"] == "healthy"
    assert report["checks"]["activation_units_healthy"] is True
    assert report["checks"]["timers_healthy"] is True
    assert report["checks"]["workers_healthy"] is True
    assert checks["workers"]["degen-dogs-watcher.service"]["Result"] == ""
    assert checks["workers"]["degen-dogs-hourly.service"]["ActiveState"] == "activating"
    assert checks["workers"]["degen-dogs-pages-verifier.service"]["Result"] == "success"


def test_systemd_health_rejects_each_activation_unit_state_failure() -> None:
    """Catches inactive, disabled, unloaded, or failed persistent triggers being accepted."""
    failures = {
        "inactive": {"active_state": "inactive"},
        "disabled": {"unit_file_state": "disabled"},
        "unloaded": {"load_state": "not-found"},
        "failed": {"active_state": "failed", "sub_state": "failed", "result": "exit-code"},
    }
    for unit in EXPECTED_ACTIVATION_UNITS:
        for name, overrides in failures.items():
            rows = {
                item: systemd_properties_fixture()
                for item in EXPECTED_ACTIVATION_UNITS + EXPECTED_WORKER_UNITS
            }
            rows[unit] = systemd_properties_fixture(**overrides)

            problems, checks, _calls = inspect_systemd_with(rows)
            report = public_systemd_report(problems, checks)

            assert any(unit in problem for problem in problems), (unit, name, problems)
            assert report["status"] == "unhealthy", (unit, name, report)
            assert report["checks"]["activation_units_healthy"] is False, (unit, name, report)
            assert report["checks"]["timers_healthy"] is False, (unit, name, report)
            assert report["checks"]["workers_healthy"] is True, (unit, name, report)


def test_systemd_health_rejects_each_worker_load_or_result_failure() -> None:
    """Catches a missing or failed oneshot worker being hidden by idle state."""
    failures = {
        "unloaded": {"load_state": "not-found", "active_state": "inactive", "sub_state": "dead"},
        "failed": {"active_state": "failed", "sub_state": "failed", "result": "exit-code"},
        "bad_result": {"active_state": "inactive", "sub_state": "dead", "result": "exit-code"},
    }
    for unit in EXPECTED_WORKER_UNITS:
        for name, overrides in failures.items():
            rows = {
                item: systemd_properties_fixture()
                for item in EXPECTED_ACTIVATION_UNITS + EXPECTED_WORKER_UNITS
            }
            rows[unit] = systemd_properties_fixture(**overrides)

            problems, checks, _calls = inspect_systemd_with(rows)
            report = public_systemd_report(problems, checks)

            assert any(unit in problem for problem in problems), (unit, name, problems)
            assert report["status"] == "unhealthy", (unit, name, report)
            assert report["checks"]["activation_units_healthy"] is True, (unit, name, report)
            assert report["checks"]["workers_healthy"] is False, (unit, name, report)


def test_systemd_activation_failure_is_independent_of_healthy_queue_records() -> None:
    """Catches a healthy queue state masking a disabled publication trigger."""
    rows = {
        unit: systemd_properties_fixture()
        for unit in EXPECTED_ACTIVATION_UNITS + EXPECTED_WORKER_UNITS
    }
    rows["degen-dogs-publisher.path"] = systemd_properties_fixture(unit_file_state="disabled")

    problems, checks, _calls = inspect_systemd_with(rows)
    publication = health.publication_health_summary(
        {
            "last_generation": 5,
            "latest": None,
            "pending": None,
            "checkpoint": {
                "outcome": "no_diff",
                "generation": 5,
                "queue_digest": "e" * 64,
                "commit_sha": None,
                "push_completed_at_utc": None,
            },
            "pages_verified": None,
            "journal": None,
        },
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
        publisher_lock_active=False,
        configured_queue_mode=True,
    )
    report = public_systemd_report(problems, checks, publication=publication)

    assert publication["handled_generation"] == 5
    assert publication["problems"] == []
    assert report["checks"]["publication"]["problems"] == []
    assert report["checks"]["publication"]["queue_mode"] is True
    assert report["checks"]["activation_units_healthy"] is False
    assert report["status"] == "unhealthy"


def test_main_returns_unhealthy_when_systemd_inventory_reports_failure() -> None:
    """Catches main rendering unit drift but omitting it from the exit status."""
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    activation = {
        unit: systemd_properties_fixture()
        for unit in EXPECTED_ACTIVATION_UNITS
    }
    activation["degen-dogs-publisher.path"] = systemd_properties_fixture(
        unit_file_state="disabled"
    )
    workers = {
        unit: systemd_properties_fixture(active_state="inactive", sub_state="dead", result="")
        for unit in EXPECTED_WORKER_UNITS
    }
    systemd_problem = "degen-dogs-publisher.path is not enabled (disabled)"
    originals = {
        "ROOT": health.ROOT,
        "utc_now": health.utc_now,
        "inspect_systemd_inventory": health.inspect_systemd_inventory,
        "refresh_lock_active": health.refresh_lock_active,
        "read_json": health.read_json,
        "run": health.run,
        "disk_usage": health.shutil.disk_usage,
        "status_problem": health.remote_freshness.status_problem,
        "fetch_json": health.remote_freshness.fetch_json,
        "assess_freshness": health.remote_freshness.assess_freshness,
        "latest_refresh_row": health.refresh_telemetry.latest_refresh_row,
    }
    environment_names = (
        "DEGEN_DOGS_LOCK_DIR",
        "DEGEN_DOGS_LOG_DIR",
        "MISSION3_WATCHER_STATE_PATH",
        "DEGEN_DOGS_REFRESH_LOCK_PATH",
        "MISSION3_PUBLICATION_MODE",
        "DEGEN_DOGS_BRANCH",
    )
    original_environment = {name: os.environ.get(name) for name in environment_names}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        log_dir = root / "logs"
        lock_dir = root / "state"
        (root / "generated").mkdir()
        log_dir.mkdir()
        lock_dir.mkdir()
        try:
            health.ROOT = root
            health.utc_now = lambda: now
            health.inspect_systemd_inventory = lambda: (
                [systemd_problem],
                {"activation_units": activation, "workers": workers},
            )
            health.refresh_lock_active = lambda _path: False
            health.read_json = lambda path: (
                {
                    "last_checked_at_utc": "2026-08-31T12:00:00Z",
                    "pending_refresh": False,
                    "consecutive_rpc_failures": 0,
                    "consecutive_refresh_failures": 0,
                    "last_refresh_status": "not_needed",
                }
                if path.name == "watcher.json"
                else {
                    "last_successful_refresh_time_utc": "2026-08-31T12:00:00Z",
                    "latest_generated_block": 700,
                    "current_dog_token_id": 10,
                }
            )
            health.remote_freshness.status_problem = lambda _status: ""
            health.refresh_telemetry.latest_refresh_row = lambda _env, root: {
                "result": "success_no_diff",
                "completed_at_utc": "2026-08-31T12:00:00Z",
            }
            health.remote_freshness.fetch_json = lambda *_args, **_kwargs: {}
            health.remote_freshness.assess_freshness = lambda *_args, **_kwargs: {
                "incident": False,
            }

            def fake_run(command: list[str], *, timeout: int = 20) -> Any:  # noqa: ARG001
                output = "main\n" if command == ["git", "branch", "--show-current"] else ""
                return health.subprocess.CompletedProcess(command, 0, output, "")

            health.run = fake_run
            health.shutil.disk_usage = lambda _path: type(
                "DiskUsage",
                (),
                {
                    "total": 100 * 1024**3,
                    "used": 10 * 1024**3,
                    "free": 90 * 1024**3,
                },
            )()
            os.environ.update({
                "DEGEN_DOGS_LOCK_DIR": str(lock_dir),
                "DEGEN_DOGS_LOG_DIR": str(log_dir),
                "MISSION3_WATCHER_STATE_PATH": str(root / "watcher.json"),
                "DEGEN_DOGS_REFRESH_LOCK_PATH": str(lock_dir / "refresh.lock"),
                "MISSION3_PUBLICATION_MODE": "inline",
                "DEGEN_DOGS_BRANCH": "main",
            })

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = health.main()
            report = json.loads(stdout.getvalue())

            assert exit_code == 1
            assert report["status"] == "unhealthy"
            assert report["problem_count"] == 1
            assert report["checks"]["activation_units_healthy"] is False
        finally:
            health.ROOT = originals["ROOT"]
            health.utc_now = originals["utc_now"]
            health.inspect_systemd_inventory = originals["inspect_systemd_inventory"]
            health.refresh_lock_active = originals["refresh_lock_active"]
            health.read_json = originals["read_json"]
            health.run = originals["run"]
            health.shutil.disk_usage = originals["disk_usage"]
            health.remote_freshness.status_problem = originals["status_problem"]
            health.remote_freshness.fetch_json = originals["fetch_json"]
            health.remote_freshness.assess_freshness = originals["assess_freshness"]
            health.refresh_telemetry.latest_refresh_row = originals["latest_refresh_row"]
            for name, value in original_environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def test_queue_health_future_boundary_and_watcher_lock_rule() -> None:
    """Catches queue mode inheriting the legacy publisher-lock watcher exemption."""
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    def snapshot(created_at: str) -> dict[str, Any]:
        return {
            "last_generation": 1,
            "latest": {"record": {"generation": 1, "created_at_utc": created_at}},
            "pending": None, "checkpoint": None, "pages_verified": None, "journal": None,
        }

    accepted = health.publication_health_summary(
        snapshot("2026-08-31T12:00:30Z"), now=now, publisher_lock_active=False,
    )
    assert "publication_future_clock_skew" not in accepted["problems"]
    rejected = health.publication_health_summary(
        snapshot("2026-08-31T12:00:31Z"), now=now, publisher_lock_active=False,
    )
    assert "publication_future_clock_skew" in rejected["problems"]
    assert health.watcher_stale_requires_failure(181, 180, publisher_lock_active=True, publication_mode="queue") is True
    assert health.watcher_stale_requires_failure(181, 180, publisher_lock_active=True, publication_mode="inline") is False


def test_matching_pending_and_receipt_is_finalization_wait_not_verifier_failure() -> None:
    """Catches health reading the snapshot wrapper instead of its validated pending record."""
    commit = "a" * 40
    digest = "d" * 64
    snapshot = {
        "last_generation": 7, "latest": None, "journal": None,
        "pending": {"record": {
            "generation": 7, "queue_digest": digest, "commit_sha": commit,
            "push_completed_at_utc": "2026-08-31T11:55:00Z",
        }},
        "checkpoint": {"outcome": "pushed", "generation": 7, "queue_digest": digest,
                       "commit_sha": commit, "push_completed_at_utc": "2026-08-31T11:55:00Z"},
        "pages_verified": {
            "generation": 7, "queue_digest": digest, "commit_sha": commit, "expected_block_number": 702,
            "push_completed_at_utc": "2026-08-31T11:55:00Z",
            "pages_verified_at_utc": "2026-08-31T11:59:00Z",
        },
    }
    summary = health.publication_health_summary(
        snapshot, now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc), publisher_lock_active=False,
    )
    assert summary["pages_verification_state"] == "pages_verified_finalization_pending"
    assert summary["unresolved_verification_generation"] == 7
    assert "pages_verification_unresolved" not in summary["problems"]
    assert "publication_state_integrity_failure" not in summary["problems"]


def test_queue_health_state_machine_boundaries_and_durable_watermark() -> None:
    """Catches incomplete queue proofs hidden by a stale receipt or latest record."""
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    commit = "a" * 40

    def pending(generation: int, pushed_at: str, digest: str = "d" * 64) -> dict[str, Any]:
        return {"record": {
            "generation": generation, "queue_digest": digest, "commit_sha": commit,
            "push_completed_at_utc": pushed_at,
        }}

    def pushed(generation: int, pushed_at: str, digest: str = "d" * 64) -> dict[str, Any]:
        return {"outcome": "pushed", "generation": generation, "queue_digest": digest,
                "commit_sha": commit, "push_completed_at_utc": pushed_at}

    def receipt(generation: int, verified_at: str, digest: str = "d" * 64) -> dict[str, Any]:
        return {"generation": generation, "queue_digest": digest, "commit_sha": commit,
                "expected_block_number": 702, "push_completed_at_utc": "2026-08-31T11:44:00Z",
                "pages_verified_at_utc": verified_at}

    cases = [
        # A receipt for an older generation never makes a newer pending proof healthy forever.
        ("mismatched_receipt_at_900", {
            "last_generation": 8, "latest": None, "journal": None,
            "pending": pending(8, "2026-08-31T11:45:00Z"),
            "checkpoint": pushed(8, "2026-08-31T11:45:00Z"),
            "pages_verified": receipt(7, "2026-08-31T11:46:00Z"),
        }, set()),
        ("mismatched_receipt_at_901", {
            "last_generation": 8, "latest": None, "journal": None,
            "pending": pending(8, "2026-08-31T11:44:59Z"),
            "checkpoint": pushed(8, "2026-08-31T11:44:59Z"),
            "pages_verified": receipt(7, "2026-08-31T11:46:00Z"),
        }, {"pages_verification_unresolved"}),
        # Durable watermark is authoritative; an old latest record or unrelated pending cannot hide loss.
        ("watermark_latest_lost", {
            "last_generation": 5,
            "latest": {"record": {"generation": 4, "created_at_utc": "2026-08-31T11:59:00Z"}},
            "pending": None, "journal": None,
            "checkpoint": {"outcome": "no_diff", "generation": 3}, "pages_verified": None,
        }, {"publication_latest_record_lost"}),
        ("watermark_pending_cannot_hide_loss", {
            "last_generation": 9,
            "latest": {"record": {"generation": 8, "created_at_utc": "2026-08-31T11:59:00Z"}},
            "pending": pending(7, "2026-08-31T11:55:00Z"), "journal": None,
            "checkpoint": pushed(7, "2026-08-31T11:55:00Z"), "pages_verified": None,
        }, {"publication_latest_record_lost"}),
        # A pushed durable checkpoint must have a same-identity pending or receipt proof.
        ("pushed_checkpoint_missing_proof", {
            "last_generation": 4, "latest": None, "pending": None, "journal": None,
            "checkpoint": pushed(4, "2026-08-31T11:55:00Z"), "pages_verified": None,
        }, {"publication_proof_gap"}),
        # A newer terminal no_diff checkpoint is allowed to coexist with an older durable receipt.
        ("newer_no_diff_over_receipt", {
            "last_generation": 5, "latest": None, "pending": None, "journal": None,
            "checkpoint": {"outcome": "no_diff", "generation": 5},
            "pages_verified": receipt(4, "2026-08-31T11:46:00Z"),
        }, set()),
    ]
    for name, snapshot, expected in cases:
        summary = health.publication_health_summary(snapshot, now=now, publisher_lock_active=False)
        assert summary["latest_observed_generation"] == snapshot["last_generation"], name
        assert set(summary["problems"]) == expected, (name, summary)

    older_pending_newer_receipt = health.publication_health_summary({
        "last_generation": 8, "latest": None, "journal": None,
        "pending": pending(7, "2026-08-31T11:55:00Z"),
        "checkpoint": pushed(7, "2026-08-31T11:55:00Z"),
        "pages_verified": receipt(8, "2026-08-31T11:59:00Z"),
    }, now=now, publisher_lock_active=False)
    assert older_pending_newer_receipt["pages_verification_state"] == "pending"
    assert older_pending_newer_receipt["last_direct_data_compatible_static_block"] == 702
    assert "publication_proof_gap" not in older_pending_newer_receipt["problems"]

    for age, expected in ((180, set()), (181, {"pages_verified_finalization_stale"})):
        verified_at = (now - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
        finalization = health.publication_health_summary({
            "last_generation": 7, "latest": None, "journal": None,
            "pending": pending(7, "2026-08-31T11:55:00Z"),
            "checkpoint": pushed(7, "2026-08-31T11:55:00Z"),
            "pages_verified": {
                "generation": 7, "queue_digest": "d" * 64, "commit_sha": commit,
                "expected_block_number": 702, "push_completed_at_utc": "2026-08-31T11:55:00Z",
                "pages_verified_at_utc": verified_at,
            },
        }, now=now, publisher_lock_active=False)
        assert finalization["pages_verification_state"] == "pages_verified_finalization_pending"
        assert set(finalization["problems"]) == expected, (age, finalization)


def test_queue_health_timestamp_and_active_grace_boundaries() -> None:
    """Catches future/casual state accepted around the exact health grace boundaries."""
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    commit = "a" * 40
    digest = "d" * 64

    def active_snapshot(created_at: str, journal_at: str, checkpoint_at: str) -> dict[str, Any]:
        return {
            "last_generation": 2,
            "latest": {"record": {"generation": 2, "created_at_utc": created_at}, "record_digest": digest},
            "journal": {"publication_generation": 2, "queue_digest": digest, "created_at_utc": journal_at},
            "pending": {"record": {"generation": 2, "queue_digest": digest, "commit_sha": commit,
                                     "push_completed_at_utc": checkpoint_at}},
            "checkpoint": {"outcome": "pushed", "generation": 2, "queue_digest": digest,
                           "commit_sha": commit, "push_completed_at_utc": checkpoint_at},
            "pages_verified": None,
        }

    accepted = health.publication_health_summary(
        active_snapshot("2026-08-31T11:55:00Z", "2026-08-31T11:56:00Z", "2026-08-31T11:57:00Z"),
        now=now, publisher_lock_active=True,
    )
    assert "publication_timestamp_reversal" not in accepted["problems"]
    assert "publication_future_clock_skew" not in health.publication_health_summary(
        active_snapshot("2026-08-31T11:55:00Z", "2026-08-31T12:00:30Z", "2026-08-31T11:57:00Z"),
        now=now, publisher_lock_active=True,
    )["problems"]
    assert "publication_future_clock_skew" not in health.publication_health_summary(
        active_snapshot("2026-08-31T11:55:00Z", "2026-08-31T11:56:00Z", "2026-08-31T12:00:30Z"),
        now=now, publisher_lock_active=True,
    )["problems"]
    future = health.publication_health_summary(
        active_snapshot("2026-08-31T11:55:00Z", "2026-08-31T12:00:31Z", "2026-08-31T11:57:00Z"),
        now=now, publisher_lock_active=True,
    )
    assert "publication_future_clock_skew" in future["problems"]
    checkpoint_future = health.publication_health_summary(
        active_snapshot("2026-08-31T11:55:00Z", "2026-08-31T11:56:00Z", "2026-08-31T12:00:31Z"),
        now=now, publisher_lock_active=True,
    )
    assert "publication_future_clock_skew" in checkpoint_future["problems"]
    reversed_summary = health.publication_health_summary(
        active_snapshot("2026-08-31T11:57:00Z", "2026-08-31T11:56:00Z", "2026-08-31T11:55:00Z"),
        now=now, publisher_lock_active=True,
    )
    assert "publication_timestamp_reversal" in reversed_summary["problems"]

    for age, stale in ((180, False), (181, True), (300, False), (301, True)):
        at = (now - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
        snapshot = {
            "last_generation": 1,
            "latest": {"record": {"generation": 1, "created_at_utc": at}, "record_digest": digest},
            "journal": {"publication_generation": 1, "queue_digest": digest, "created_at_utc": at},
            "pending": None, "checkpoint": None, "pages_verified": None,
        }
        summary = health.publication_health_summary(snapshot, now=now, publisher_lock_active=age >= 300)
        assert ("publication_queue_stale" in summary["problems"]) is stale, (age, summary)

    invalid = health.publication_health_summary(
        {"last_generation": "bad", "latest": None, "pending": None, "checkpoint": None,
         "pages_verified": None, "journal": None},
        now=now, publisher_lock_active=False, configured_queue_mode=True,
    )
    assert invalid["queue_mode"] is True
    assert invalid["problems"] == ["publication_state_integrity_failure"]


if __name__ == "__main__":
    test_pages_delay_keeps_newer_observation_and_drains_it_next()
    test_public_queue_health_uses_receipt_not_observation_and_exposes_no_private_values()
    test_systemd_health_inventory_checks_every_required_unit_exactly_once()
    test_systemd_health_rejects_each_activation_unit_state_failure()
    test_systemd_health_rejects_each_worker_load_or_result_failure()
    test_systemd_activation_failure_is_independent_of_healthy_queue_records()
    test_main_returns_unhealthy_when_systemd_inventory_reports_failure()
    test_queue_health_future_boundary_and_watcher_lock_rule()
    test_matching_pending_and_receipt_is_finalization_wait_not_verifier_failure()
    test_queue_health_state_machine_boundaries_and_durable_watermark()
    test_queue_health_timestamp_and_active_grace_boundaries()
    print("wsl_publication_integration=pass delay_seconds=120")
