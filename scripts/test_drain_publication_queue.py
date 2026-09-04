#!/usr/bin/env python3
"""Behavioral tests for the single-flight WSL publication queue drainer."""
from __future__ import annotations

import ast
import contextlib
import errno
import importlib.util
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator


ROOT = Path(__file__).resolve().parents[1]
DIGEST_1 = "1" * 64
DIGEST_2 = "2" * 64
DIGEST_3 = "3" * 64


def load_module() -> Any:
    path = ROOT / "scripts" / "drain_publication_queue.py"
    spec = importlib.util.spec_from_file_location("drain_publication_queue", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


drainer = load_module()


def test_runtime_budget_parser_accepts_default_and_boundaries() -> None:
    """Catches changing the documented default or excluding either operator boundary."""

    assert drainer.parse_runtime_budget_seconds(None) == 900.0
    assert drainer.parse_runtime_budget_seconds("300") == 300.0
    assert drainer.parse_runtime_budget_seconds("2700") == 2700.0


def test_runtime_budget_parser_rejects_noncanonical_and_out_of_range_values() -> None:
    """Catches permissive parsing, value disclosure, or huge-integer parser escapes."""

    rejected = (
        "",
        "0",
        "0299",
        "299",
        "2701",
        "+300",
        "300.0",
        " 300",
        "300 ",
        "3e2",
        "\u0663\u0660\u0660",
        "9" * 5_000,
    )
    for value in rejected:
        try:
            drainer.parse_runtime_budget_seconds(value)
        except drainer.ConfigurationError as exc:
            assert str(exc) == drainer.RUNTIME_BUDGET_CONFIGURATION_ERROR
        else:
            raise AssertionError(f"invalid runtime budget was accepted at case index {rejected.index(value)}")


def test_main_forwards_validated_runtime_budget_and_rejects_invalid_configuration() -> None:
    """Catches bypassing entry-point validation or leaking invalid values to the child path."""

    if os.name != "posix":
        return
    original_environment = os.environ.copy()
    original_drain = drainer.drain_publication_queue
    original_sigterm_handler = signal.getsignal(signal.SIGTERM)
    calls: list[dict[str, Any]] = []

    def fake_drain(**kwargs: Any) -> int:
        calls.append(dict(kwargs))
        return 0

    try:
        drainer.drain_publication_queue = fake_drain
        os.environ.update(
            {
                "DEGEN_DOGS_REPO_DIR": "/srv/degen-dogs/repo",
                "DEGEN_DOGS_LOCK_DIR": "/var/cache/degen-dogs",
                "DEGEN_DOGS_REFRESH_LOCK_PATH": "/var/cache/degen-dogs/refresh.lock",
            }
        )
        os.environ.pop("DEGEN_DOGS_QUEUE_RUNTIME_BUDGET_SECONDS", None)

        assert drainer.main() == 0
        assert calls[-1]["runtime_budget_seconds"] == 900.0
        assert signal.getsignal(signal.SIGTERM) == original_sigterm_handler

        os.environ["DEGEN_DOGS_QUEUE_RUNTIME_BUDGET_SECONDS"] = "2700"
        assert drainer.main() == 0
        assert calls[-1]["runtime_budget_seconds"] == 2700.0
        assert signal.getsignal(signal.SIGTERM) == original_sigterm_handler

        os.environ["DEGEN_DOGS_QUEUE_RUNTIME_BUDGET_SECONDS"] = "299"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            assert drainer.main() == drainer.EXIT_CONFIG
        assert len(calls) == 2
        assert stderr.getvalue() == (
            f"error: invalid queued publisher configuration: "
            f"{drainer.RUNTIME_BUDGET_CONFIGURATION_ERROR}\n"
        )
        assert signal.getsignal(signal.SIGTERM) == original_sigterm_handler
    finally:
        drainer.drain_publication_queue = original_drain
        os.environ.clear()
        os.environ.update(original_environment)
        signal.signal(signal.SIGTERM, original_sigterm_handler)


def private_temporary_directory() -> tempfile.TemporaryDirectory[str]:
    """Place real secure-path fixtures below an owner-controlled POSIX ancestor."""
    parent = Path.home() if os.name == "posix" else None
    return tempfile.TemporaryDirectory(dir=parent)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds


class FakeOwnedLock:
    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy
        self.active = False
        self.entered = 0
        self.exited = 0
        self.inheritable_at_exit: bool | None = None
        self.temporary = tempfile.TemporaryDirectory()
        self.fd = os.open(Path(self.temporary.name) / "refresh.lock", os.O_RDWR | os.O_CREAT, 0o600)

    def __enter__(self) -> int | None:
        self.entered += 1
        if self.busy:
            return None
        self.active = True
        return self.fd

    def __exit__(self, *_args: object) -> None:
        self.inheritable_at_exit = os.get_inheritable(self.fd)
        self.active = False
        self.exited += 1

    def close(self) -> None:
        os.close(self.fd)
        self.temporary.cleanup()


class FakeProcess:
    _next_pid = 7000

    def __init__(self, outcome: int | BaseException, after_wait: Callable[[], None] | None = None) -> None:
        self.outcome = outcome
        self.after_wait = after_wait
        self.wait_timeouts: list[float | None] = []
        self.returncode: int | None = None
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.after_wait is not None:
            callback, self.after_wait = self.after_wait, None
            callback()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        self.returncode = self.outcome
        return self.outcome


class FakeLauncher:
    def __init__(self, outcomes: list[tuple[int | BaseException, Callable[[], None] | None]]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        for descriptor in kwargs.get("pass_fds", ()):
            assert os.get_inheritable(descriptor), "publisher FD was not inheritable at Popen"
        self.calls.append((list(argv), dict(kwargs)))
        if not self.outcomes:
            raise AssertionError("publisher launched more times than expected")
        outcome, callback = self.outcomes.pop(0)
        process = FakeProcess(outcome, callback)
        self.processes.append(process)
        return process


class QueueHarness:
    def __init__(self, owned_lock: FakeOwnedLock) -> None:
        self.owned_lock = owned_lock
        self.journal: dict[str, Any] | None = None
        self.latest: tuple[dict[str, Any], str] | None = None
        self.read_journal_calls = 0
        self.read_latest_calls = 0
        self.finalize_calls: list[tuple[Path, int, str]] = []
        self.finalize_result = True
        self.finalize_error: BaseException | None = None
        self.on_finalize: Callable[[int, str], None] | None = None
        self.latest_read_forbidden_until_finalize = False
        self.events: list[str] = []

    def read_journal(self, _lock_dir: Path) -> dict[str, Any] | None:
        assert self.owned_lock.active, "journal was read without the refresh lock"
        self.read_journal_calls += 1
        self.events.append("read_journal")
        return None if self.journal is None else dict(self.journal)

    def read_latest(self, _lock_dir: Path) -> tuple[dict[str, Any], str] | None:
        assert self.owned_lock.active, "latest was read without the refresh lock"
        assert not self.latest_read_forbidden_until_finalize, "latest was read before journal recovery/finalization"
        self.read_latest_calls += 1
        self.events.append("read_latest")
        if self.latest is None:
            return None
        return dict(self.latest[0]), self.latest[1]

    def finalize(self, lock_dir: Path, generation: int, digest: str) -> bool:
        assert self.owned_lock.active, "finalization ran after refresh-lock release"
        assert not os.get_inheritable(self.owned_lock.fd), "refresh fd remained inheritable during finalization"
        self.finalize_calls.append((lock_dir, generation, digest))
        self.events.append("finalize")
        if self.finalize_error is not None:
            raise self.finalize_error
        if not self.finalize_result:
            return False
        self.latest_read_forbidden_until_finalize = False
        if self.on_finalize is not None:
            self.on_finalize(generation, digest)
        self.journal = None
        if self.latest is not None and self.latest[0]["generation"] == generation and self.latest[1] == digest:
            self.latest = None
        return True


def target(
    generation: int,
    digest: str,
    *,
    token_id: str = "818",
    command: str = "ignored",
) -> tuple[dict[str, Any], str]:
    return {
        "schema_version": 1,
        "generation": generation,
        "created_at_utc": "2026-08-30T12:34:56Z",
        "runner_id": "windows-wsl",
        "run_scope": "current",
        "observation": {
            "confirmed_block_number": 100 + generation,
            "token_id": token_id,
            "queue_selected_command": command,
        },
    }, digest


def write_committed_refresh_status(repo: Path, baseline: object) -> None:
    """Create the literal queue-publisher baseline the drainer is allowed to read."""

    status = repo / "generated" / "refresh_status.json"
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(
        json.dumps({"current_dog_token_id": baseline}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Queue Fixture"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "queue-fixture@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", "generated/refresh_status.json"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture status"], check=True)


def journal(generation: int, digest: str, *, phase: str = "generating", outcome: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "publication_generation": generation,
        "queue_digest": digest,
        "handoff_phase": phase,
        "terminal_outcome": outcome,
        "publication_target": target(generation, digest)[0],
        "repo_realpath": "/attacker/ignored",
        "publish_paths": ["attacker/ignored"],
    }


class FakeStateLock:
    """Portable setup lock; the drainer's real finalizer deliberately does not receive it."""

    def __enter__(self) -> "FakeStateLock":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def real_observation(block: int, hash_character: str) -> dict[str, object]:
    block_hash = "0x" + hash_character * 64
    return {
        "confirmed_block_number": block,
        "confirmed_block_hash": block_hash,
        "confirmed_block_time_utc": "2026-08-30T12:34:00Z",
        "token_id": "818",
        "amount_wei": "5500000000000000",
        "start_time_unix": "1780000000",
        "end_time_unix": "1780003600",
        "bidder_wallet": "0x" + "1" * 40,
        "settled": False,
        "event_name": "AuctionBid",
        "event_tx_hash": "0x" + "b" * 64,
        "event_log_index": 0,
        "event_block_number": block,
        "event_block_hash": block_hash,
        "event_block_time_utc": "2026-08-30T12:34:00Z",
        "canonical_reorg_from_hash": None,
    }


def real_handoff_records(
    generation: int,
    digest: str,
    *,
    outcome: str,
    publication_target: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    commit = "a" * 40
    terminal = outcome != "pushed"
    observation = publication_target["observation"]
    assert isinstance(observation, dict)
    source_kind = {
        "pushed": "generated_commit",
        "no_diff": "baseline_no_diff",
        "peer_superseded": "peer_commit",
    }[outcome]
    source_commit = "d" * 40 if outcome == "no_diff" else commit
    proof = {
        "schema_version": 1,
        "source_kind": source_kind,
        "source_commit_sha": source_commit,
        "status_path": "public/generated/refresh_status.json",
        "status_sha256": "c" * 64,
        "bundle_path": (
            f"public/generated/live_snapshot_{observation['confirmed_block_number']}_"
            f"{str(observation['confirmed_block_hash'])[2:]}_{'b' * 64}.json"
        ),
        "bundle_sha256": "b" * 64,
        "bundle_bytes": 1234,
        "block_number": observation["confirmed_block_number"],
        "block_hash": observation["confirmed_block_hash"],
        "auction": {
            key: observation[key]
            for key in (
                "token_id",
                "amount_wei",
                "start_time_unix",
                "end_time_unix",
                "bidder_wallet",
                "settled",
            )
        },
        "canonical_reorg_from_hash": None,
        "quorum_attestation": {
            "onchain_chain_id": 8453,
            "onchain_verification_status": "current_snapshot_cross_provider_verified",
            "onchain_verification_scope": (
                "snapshot_hash,contract_code,current_auction,recent_event_logs"
            ),
            "rpc_quorum_size": 2,
            "rpc_quorum_agreement": "2/2",
            "rpc_quorum_providers": "base.org,publicnode.com",
            "snapshot_confirmations": 1,
        },
    }
    recovery = {
        "schema_version": 1,
        "repo_realpath": str(ROOT),
        "branch": "main",
        "baseline_head": "d" * 40,
        "run_id": "task4-real-state-test",
        "runner_id": "windows-wsl",
        "run_scope": "current",
        "created_at_utc": "2026-08-30T12:34:56Z",
        "publish_paths": ["generated", "public"],
        "publication_target": publication_target,
        "alignment_runner_commit": None,
        "alignment_remote_head": None,
        "alignment_result": None,
        "publication_generation": generation,
        "queue_digest": digest,
        "coverage_proof": proof if terminal else None,
        "terminal_outcome": outcome if terminal else None,
        "handoff_phase": "terminal" if terminal else "generating",
        "remote_commit": None if outcome == "no_diff" or not terminal else commit,
        "raw_status_path": None,
        "raw_bundle_path": None,
        "expected_bundle_sha256": None,
        "expected_bundle_bytes": None,
        "expected_block_number": None,
        "expected_block_hash": None,
        "push_completed_at_utc": None,
        "retry_deadline_utc": None,
        "retry_count": None,
    }
    pending = {
        "schema_version": 1,
        "generation": generation,
        "queue_digest": digest,
        "commit_sha": commit,
        "raw_status_path": "public/generated/refresh_status.json",
        "raw_bundle_path": proof["bundle_path"],
        "expected_bundle_sha256": "b" * 64,
        "expected_bundle_bytes": 1234,
        "expected_block_number": observation["confirmed_block_number"],
        "expected_block_hash": observation["confirmed_block_hash"],
        "push_completed_at_utc": "2026-08-30T12:35:00Z",
        "retry_deadline_utc": "2026-08-30T12:45:00Z",
        "retry_count": 0,
    }
    checkpoint = {
        "schema_version": 1,
        "outcome": outcome,
        "generation": generation,
        "queue_digest": digest,
        "commit_sha": None if outcome == "no_diff" else commit,
        "push_completed_at_utc": "2026-08-30T12:35:00Z" if outcome == "pushed" else None,
        "publication_target": publication_target,
        "coverage_proof": proof,
    }
    return recovery, pending, checkpoint


def stage_real_handoff(state: Any, lock_dir: Path, queued: Any, outcome: str) -> None:
    recovery, pending, checkpoint = real_handoff_records(
        queued.generation,
        queued.digest,
        outcome=outcome,
        publication_target=queued.record,
    )
    generating = dict(recovery)
    generating.update(
        {
            "terminal_outcome": None,
            "handoff_phase": "generating",
            "remote_commit": None,
            "coverage_proof": None,
        }
    )
    state.create_deferred_recovery_journal(lock_dir, generating, lock_context=FakeStateLock())
    if outcome == "pushed":
        armed = state.arm_deferred_pushed_handoff(
            lock_dir,
            queued.generation,
            queued.digest,
            "a" * 40,
            checkpoint["coverage_proof"],
            lock_context=FakeStateLock(),
        )
        state.prepare_pushed_handoff(
            lock_dir,
            armed,
            pending,
            checkpoint,
            lock_context=FakeStateLock(),
        )
    else:
        state.record_terminal_outcome(
            lock_dir,
            recovery,
            checkpoint,
            lock_context=FakeStateLock(),
        )


def enqueue_real_observation(state: Any, lock_dir: Path, block: int, hash_character: str) -> Any:
    return state.enqueue_latest_observation(
        lock_dir,
        real_observation(block, hash_character),
        runner_id="windows-wsl",
        run_scope="current",
        created_at_utc="2026-08-30T12:34:56Z",
        lock_context=FakeStateLock(),
    )


def drain_real_state_once(repo_dir: Path, lock_dir: Path, owned: FakeOwnedLock) -> int:
    return drainer.drain_publication_queue(
        repo_dir=repo_dir,
        lock_dir=lock_dir,
        refresh_lock_path=lock_dir / "refresh.lock",
        base_env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        process_launcher=FakeLauncher([(0, None)]),
        lock_factory=lambda _path, **_kwargs: owned,
    )


@contextlib.contextmanager
def fake_lock_factory(lock: FakeOwnedLock, expected_path: Path, **expected_limits: float) -> Iterator[int | None]:
    def factory(path: Path, **kwargs: Any) -> FakeOwnedLock:
        assert path == expected_path
        for key, value in expected_limits.items():
            assert kwargs[key] == value
        return lock

    yield factory  # type: ignore[misc]


def run_drainer(
    temporary: Path,
    queue: QueueHarness,
    launcher: FakeLauncher,
    clock: FakeClock,
    owned_lock: FakeOwnedLock,
    **overrides: Any,
) -> int:
    repo_dir = temporary / "repo"
    lock_dir = temporary / "state"
    lock_path = lock_dir / "refresh.lock"
    (repo_dir / "scripts").mkdir(parents=True, exist_ok=True)
    base_env = {
        "PATH": "/trusted/bin",
        "BASE_RPC_URLS": "https://one.invalid,https://two.invalid",
        "MISSION3_REFRESH_COMMAND": "/attacker/queue-selected-command",
        "DEGEN_DOGS_RUN_ID": "old-run",
        "DEGEN_DOGS_REFRESH_RUN_ID": "real-old-run-id",
        "DEGEN_DOGS_REFRESH_RESULT": "success_pushed",
        "DEGEN_DOGS_REFRESH_ERROR": "old-error",
        "DEGEN_DOGS_COMMIT_SHA": "a" * 40,
        "DEGEN_DOGS_REFRESH_QUEUED_AT_UTC": "2020-01-01T00:00:00Z",
        "DEGEN_DOGS_REFRESH_STARTED_AT_UTC": "2020-01-01T00:00:00Z",
        "DEGEN_DOGS_DATA_STARTED_AT_UTC": "2020-01-01T00:00:00Z",
        "DEGEN_DOGS_BUILD_COMPLETED_AT_UTC": "2020-01-01T00:00:00Z",
        "DEGEN_DOGS_VALIDATION_STARTED_AT_UTC": "2020-01-01T00:00:00Z",
        "DEGEN_DOGS_GIT_STATUS_COMPLETED_AT_UTC": "2020-01-01T00:00:00Z",
        "DEGEN_DOGS_PUSH_STARTED_AT_UTC": "2020-01-01T00:00:00Z",
        "DEGEN_DOGS_PUSH_COMPLETED_AT_UTC": "2020-01-01T00:00:01Z",
        "DEGEN_DOGS_CHANGED_FILES": "attacker/path",
        "DEGEN_DOGS_LIVE_VERIFIED_AT_UTC": "2020-01-01T00:00:02Z",
        "DEGEN_DOGS_RAW_COMMIT_URL": "https://attacker.invalid",
        "DEGEN_DOGS_RAW_COMMIT_VERIFIED": "1",
        "DEGEN_DOGS_RUN_SCOPE": "archive",
        "DEGEN_DOGS_PUBLICATION_OUTCOME": "old-pushed",
        "DEGEN_DOGS_QUEUE_GENERATION": "88",
        "DEGEN_DOGS_QUEUE_OUTCOME": "old-peer",
        "DEGEN_DOGS_QUEUE_RUNTIME_BUDGET_SECONDS": "2700",
        "DEGEN_DOGS_PUSH_TO_LIVE_SECONDS": "12.5",
        "DEGEN_DOGS_REFRESH_REASONS": "old-reason",
        "DEGEN_DOGS_EVENT_NAME": "old-event",
        "DEGEN_DOGS_EVENT_BLOCK_NUMBER": "1",
        "DEGEN_DOGS_LOCK_FD": "999",
        "DEGEN_DOGS_PUBLICATION_GENERATION": "999",
        "DEGEN_DOGS_PUBLICATION_DIGEST": "f" * 64,
    }
    options: dict[str, Any] = {
        "repo_dir": repo_dir,
        "lock_dir": lock_dir,
        "refresh_lock_path": lock_path,
        "base_env": base_env,
        "runtime_budget_seconds": 100.0,
        "lock_wait_seconds": 0.5,
        "lock_poll_seconds": 0.05,
        "cleanup_grace_seconds": 5.0,
        "termination_grace_seconds": 2.0,
        "followup_reserve_seconds": 10.0,
        "monotonic": clock.monotonic,
        "sleep": clock.sleep,
        "process_launcher": launcher,
        "lock_factory": lambda path, **kwargs: owned_lock,
        "read_journal": queue.read_journal,
        "read_latest": queue.read_latest,
        "finalize_handoff": queue.finalize,
    }
    options.update(overrides)
    with contextlib.redirect_stderr(io.StringIO()):
        return drainer.drain_publication_queue(**options)


def test_fixed_publisher_argv_exact_fd_and_sanitized_environment() -> None:
    """Catches queue-controlled argv/path use, inherited outcome leakage, or FD drift."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            queue = QueueHarness(owned)
            write_committed_refresh_status(temporary / "repo", 818)
            queue.latest = target(1, DIGEST_1, command="touch /tmp/owned")
            launcher = FakeLauncher([(0, None)])
            result = run_drainer(temporary, queue, launcher, FakeClock(), owned)
            assert result == 0
            assert len(launcher.calls) == 1
            argv, kwargs = launcher.calls[0]
            assert argv == ["/bin/bash", "-p", str(temporary / "repo" / "scripts" / "refresh_and_publish.sh")]
            assert kwargs["shell"] is False
            assert kwargs["cwd"] == str(temporary / "repo")
            assert kwargs["pass_fds"] == (owned.fd,)
            assert kwargs["start_new_session"] is True
            env = kwargs["env"]
            expected = {
                "DEGEN_DOGS_LOCK_HELD": "1",
                "DEGEN_DOGS_LOCK_FD": str(owned.fd),
                "DEGEN_DOGS_REFRESH_LOCK_PATH": str(temporary / "state" / "refresh.lock"),
                "DEGEN_DOGS_LOCK_DIR": str(temporary / "state"),
                "DEGEN_DOGS_DEFER_PAGES_VERIFICATION": "1",
                "DEGEN_DOGS_PUBLICATION_GENERATION": "1",
                "DEGEN_DOGS_PUBLICATION_DIGEST": DIGEST_1,
                "DEGEN_DOGS_FULL_REFRESH": "0",
                "DEGEN_DOGS_RUN_MISSION3_ARCHIVE": "0",
                "DEGEN_DOGS_SKIP_PUSH": "0",
                "DEGEN_DOGS_SKIP_PULL": "0",
                "DEGEN_DOGS_SUPERSESSION_RETRY_COUNT": "0",
            }
            for key, value in expected.items():
                assert env[key] == value
            assert env["BASE_RPC_URLS"].startswith("https://one.invalid")
            for forbidden in (
                "MISSION3_REFRESH_COMMAND",
                "DEGEN_DOGS_RUN_ID",
                "DEGEN_DOGS_REFRESH_RUN_ID",
                "DEGEN_DOGS_REFRESH_RESULT",
                "DEGEN_DOGS_REFRESH_ERROR",
                "DEGEN_DOGS_COMMIT_SHA",
                "DEGEN_DOGS_REFRESH_QUEUED_AT_UTC",
                "DEGEN_DOGS_REFRESH_STARTED_AT_UTC",
                "DEGEN_DOGS_DATA_STARTED_AT_UTC",
                "DEGEN_DOGS_BUILD_COMPLETED_AT_UTC",
                "DEGEN_DOGS_VALIDATION_STARTED_AT_UTC",
                "DEGEN_DOGS_GIT_STATUS_COMPLETED_AT_UTC",
                "DEGEN_DOGS_PUSH_STARTED_AT_UTC",
                "DEGEN_DOGS_PUSH_COMPLETED_AT_UTC",
                "DEGEN_DOGS_CHANGED_FILES",
                "DEGEN_DOGS_LIVE_VERIFIED_AT_UTC",
                "DEGEN_DOGS_RAW_COMMIT_URL",
                "DEGEN_DOGS_RAW_COMMIT_VERIFIED",
                "DEGEN_DOGS_RUN_SCOPE",
                "DEGEN_DOGS_PUBLICATION_OUTCOME",
                "DEGEN_DOGS_QUEUE_GENERATION",
                "DEGEN_DOGS_QUEUE_OUTCOME",
                "DEGEN_DOGS_QUEUE_RUNTIME_BUDGET_SECONDS",
                "DEGEN_DOGS_PUSH_TO_LIVE_SECONDS",
                "DEGEN_DOGS_REFRESH_REASONS",
                "DEGEN_DOGS_EVENT_NAME",
                "DEGEN_DOGS_EVENT_BLOCK_NUMBER",
            ):
                assert forbidden not in env
            assert queue.finalize_calls == [(temporary / "state", 1, DIGEST_1)]
            assert owned.inheritable_at_exit is False
            assert launcher.processes[0].wait_timeouts == [95.0]
        finally:
            owned.close()


def test_matching_current_target_keeps_bounded_refresh() -> None:
    """Catches promoting archive work when the authenticated target matches the committed baseline."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            write_committed_refresh_status(temporary / "repo", 818)
            queue = QueueHarness(owned)
            queue.latest = target(1, DIGEST_1, token_id="818")
            launcher = FakeLauncher([(0, None)])
            assert run_drainer(temporary, queue, launcher, FakeClock(), owned) == 0
            env = launcher.calls[0][1]["env"]
            assert env["DEGEN_DOGS_FULL_REFRESH"] == "0"
            assert env["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"] == "0"
        finally:
            owned.close()


def test_newer_target_promotes_archive_refresh() -> None:
    """Catches publishing a newly selected auction without incremental archive indexing."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            write_committed_refresh_status(temporary / "repo", 818)
            queue = QueueHarness(owned)
            queue.latest = target(1, DIGEST_1, token_id="819")
            launcher = FakeLauncher([(0, None)])
            assert run_drainer(temporary, queue, launcher, FakeClock(), owned) == 0
            env = launcher.calls[0][1]["env"]
            assert env["DEGEN_DOGS_FULL_REFRESH"] == "0"
            assert env["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"] == "1"
        finally:
            owned.close()


def test_missing_baseline_promotes_archive_refresh() -> None:
    """Catches treating an absent committed status as safe for a bounded-only publication."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            queue = QueueHarness(owned)
            queue.latest = target(1, DIGEST_1, token_id="818")
            launcher = FakeLauncher([(0, None)])
            assert run_drainer(temporary, queue, launcher, FakeClock(), owned) == 0
            assert launcher.calls[0][1]["env"]["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"] == "1"
        finally:
            owned.close()


def test_invalid_status_and_baselines_promote_archive_refresh() -> None:
    """Catches accepting corrupt, Boolean, or negative baseline values as a safe current auction."""
    cases: tuple[tuple[str, object], ...] = (
        ("invalid-json", "{not json"),
        ("boolean-baseline", True),
        ("negative-baseline", -1),
    )
    for name, baseline in cases:
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            owned = FakeOwnedLock()
            try:
                status = temporary / "repo" / "generated" / "refresh_status.json"
                status.parent.mkdir(parents=True)
                if name == "invalid-json":
                    status.write_text(str(baseline), encoding="utf-8")
                else:
                    status.write_text(
                        json.dumps({"current_dog_token_id": baseline}) + "\n",
                        encoding="utf-8",
                    )
                queue = QueueHarness(owned)
                queue.latest = target(1, DIGEST_1, token_id="818")
                launcher = FakeLauncher([(0, None)])
                assert run_drainer(temporary, queue, launcher, FakeClock(), owned) == 0, name
                assert launcher.calls[0][1]["env"]["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"] == "1", name
            finally:
                owned.close()


def test_oversized_baseline_integer_promotes_archive_refresh() -> None:
    """Catches aborting when a bounded status exceeds Python's JSON integer conversion limit."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            status = temporary / "repo" / "generated" / "refresh_status.json"
            status.parent.mkdir(parents=True)
            status.write_text(
                '{"current_dog_token_id":' + "9" * 5_000 + "}\n",
                encoding="ascii",
            )
            queue = QueueHarness(owned)
            queue.latest = target(1, DIGEST_1, token_id="818")
            launcher = FakeLauncher([(0, None)])
            assert run_drainer(temporary, queue, launcher, FakeClock(), owned) == 0
            assert launcher.calls[0][1]["env"]["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"] == "1"
        finally:
            owned.close()


def test_invalid_authenticated_target_fails_closed_before_launch() -> None:
    """Catches interpreting a malformed authenticated target as a bounded current publication."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            write_committed_refresh_status(temporary / "repo", 818)
            queue = QueueHarness(owned)
            queue.latest = target(1, DIGEST_1, token_id="0819")
            launcher = FakeLauncher([])
            assert run_drainer(temporary, queue, launcher, FakeClock(), owned) != 0
            assert not launcher.calls
        finally:
            owned.close()


def test_generating_recovery_promotes_archive_and_stays_bound_to_its_journal_target() -> None:
    """Catches a generating journal selecting newer latest data or dropping its conservative archive scope."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            write_committed_refresh_status(temporary / "repo", 818)
            queue = QueueHarness(owned)
            recovery = journal(1, DIGEST_1, phase="generating")
            recovery["publication_target"] = target(1, DIGEST_1, token_id="819")[0]
            queue.journal = recovery
            queue.latest = target(2, DIGEST_2, token_id="818")
            queue.latest_read_forbidden_until_finalize = True
            launcher = FakeLauncher([(0, None), (0, None)])
            assert run_drainer(temporary, queue, launcher, FakeClock(), owned) == 0
            first = launcher.calls[0][1]["env"]
            assert first["DEGEN_DOGS_PUBLICATION_GENERATION"] == "1"
            assert first["DEGEN_DOGS_PUBLICATION_DIGEST"] == DIGEST_1
            assert first["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"] == "1"
            assert launcher.calls[0][0] == [
                "/bin/bash",
                "-p",
                str(temporary / "repo" / "scripts" / "refresh_and_publish.sh"),
            ]
            assert queue.events[:3] == ["read_journal", "finalize", "read_latest"]
        finally:
            owned.close()


def test_journal_recovers_before_latest_and_arrivals_coalesce_to_newest() -> None:
    """Catches consulting N+1 before journal N or publishing obsolete intermediate N+1."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            queue = QueueHarness(owned)
            queue.journal = journal(1, DIGEST_1, phase="raw_proven", outcome="pushed")
            queue.latest = target(2, DIGEST_2)
            queue.latest_read_forbidden_until_finalize = True

            def arrive_n2() -> None:
                queue.latest = target(3, DIGEST_3)

            launcher = FakeLauncher([(0, arrive_n2), (0, None)])
            assert run_drainer(temporary, queue, launcher, FakeClock(), owned) == 0
            identities = [
                (call[1]["env"]["DEGEN_DOGS_PUBLICATION_GENERATION"], call[1]["env"]["DEGEN_DOGS_PUBLICATION_DIGEST"])
                for call in launcher.calls
            ]
            assert identities == [("1", DIGEST_1), ("3", DIGEST_3)]
            assert [call[1:] for call in queue.finalize_calls] == [(1, DIGEST_1), (3, DIGEST_3)]
            assert queue.events[:3] == ["read_journal", "finalize", "read_latest"]
        finally:
            owned.close()


def test_arrival_during_finalization_is_selected_only_after_exact_captured_finalize() -> None:
    """Catches reselecting N+2 before authenticating and finalizing captured N."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            queue = QueueHarness(owned)
            queue.journal = journal(1, DIGEST_1, phase="terminal", outcome="no_diff")
            queue.latest = target(1, DIGEST_1)

            def arrive_during_finalize(generation: int, _digest: str) -> None:
                if generation == 1:
                    queue.latest = target(3, DIGEST_3)

            queue.on_finalize = arrive_during_finalize
            launcher = FakeLauncher([(0, None), (0, None)])
            assert run_drainer(temporary, queue, launcher, FakeClock(), owned) == 0
            assert [call[1:] for call in queue.finalize_calls] == [(1, DIGEST_1), (3, DIGEST_3)]
            assert [call[1]["env"]["DEGEN_DOGS_PUBLICATION_GENERATION"] for call in launcher.calls] == ["1", "3"]
        finally:
            owned.close()


def test_positive_prevalidation_race_retries_only_strictly_newer_identity() -> None:
    """Catches losing N+1 when Bash rejects stale N before creating its journal."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            queue = QueueHarness(owned)
            queue.latest = target(1, DIGEST_1)

            def replace_latest() -> None:
                queue.latest = target(2, DIGEST_2)

            launcher = FakeLauncher([(75, replace_latest), (0, None)])
            assert run_drainer(temporary, queue, launcher, FakeClock(), owned) == 0
            assert [call[1]["env"]["DEGEN_DOGS_PUBLICATION_GENERATION"] for call in launcher.calls] == ["1", "2"]
            assert [call[1:] for call in queue.finalize_calls] == [(2, DIGEST_2)]
        finally:
            owned.close()


def test_failure_retry_rejects_signal_timeout_same_absent_older_or_journal() -> None:
    """Catches treating ambiguous/signal failures as the benign stale-generation race."""
    cases: tuple[tuple[str, int | BaseException, Callable[[QueueHarness], None]], ...] = (
        ("negative-signal", -signal.SIGTERM, lambda queue: setattr(queue, "latest", target(2, DIGEST_2))),
        (
            "timeout",
            subprocess.TimeoutExpired(["publisher"], 1),
            lambda queue: setattr(queue, "latest", target(2, DIGEST_2)),
        ),
        ("same-generation-conflict", 1, lambda queue: setattr(queue, "latest", target(1, DIGEST_2))),
        ("same-generation-unchanged", 1, lambda queue: setattr(queue, "latest", target(1, DIGEST_1))),
        ("absent", 1, lambda queue: setattr(queue, "latest", None)),
        ("older", 1, lambda queue: setattr(queue, "latest", target(0, DIGEST_2))),
        (
            "journal-created",
            1,
            lambda queue: (
                setattr(queue, "latest", target(2, DIGEST_2)),
                setattr(queue, "journal", journal(1, DIGEST_1)),
            ),
        ),
    )
    for name, outcome, mutate in cases:
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            owned = FakeOwnedLock()
            try:
                queue = QueueHarness(owned)
                queue.latest = target(1, DIGEST_1)
                launcher = FakeLauncher([(outcome, lambda q=queue, m=mutate: m(q))])
                terminated: list[int] = []

                def terminate(process: FakeProcess, **_kwargs: Any) -> None:
                    assert owned.active
                    terminated.append(process.pid)

                result = run_drainer(
                    temporary,
                    queue,
                    launcher,
                    FakeClock(),
                    owned,
                    terminate_process_group=terminate,
                )
                assert result != 0, name
                assert len(launcher.calls) == 1, name
                assert not queue.finalize_calls, name
                assert bool(terminated) == (name == "timeout"), name
            finally:
                owned.close()


def test_parent_termination_and_timeout_kill_group_before_lock_release_and_never_finalize() -> None:
    """Catches early lock release or finalization after a timed-out/signalled child later exits zero."""
    for outcome in (
        subprocess.TimeoutExpired(["publisher"], 5),
        None,
    ):
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            owned = FakeOwnedLock()
            try:
                queue = QueueHarness(owned)
                queue.latest = target(1, DIGEST_1)
                child_outcome: BaseException = outcome or drainer.TerminationRequested()
                launcher = FakeLauncher([(child_outcome, None)])
                terminated: list[tuple[int, bool]] = []

                def terminate(process: FakeProcess, **_kwargs: Any) -> None:
                    terminated.append((process.pid, owned.active))
                    process.returncode = 0

                result = run_drainer(
                    temporary,
                    queue,
                    launcher,
                    FakeClock(),
                    owned,
                    terminate_process_group=terminate,
                )
                assert result != 0
                assert terminated == [(launcher.processes[0].pid, True)]
                assert not queue.finalize_calls
                assert owned.exited == 1
            finally:
                owned.close()


def test_finalize_false_exception_and_spawn_failure_fail_closed_with_cloexec_restored() -> None:
    """Catches manual cleanup fallback or leaking the lock FD across failed launches."""
    for mode in ("false", "raise", "spawn"):
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            owned = FakeOwnedLock()
            try:
                queue = QueueHarness(owned)
                queue.journal = journal(1, DIGEST_1, phase="terminal", outcome="no_diff")
                queue.latest = target(1, DIGEST_1)
                launcher = FakeLauncher([(0, None)])
                if mode == "false":
                    queue.finalize_result = False
                elif mode == "raise":
                    queue.finalize_error = RuntimeError("injected finalization failure")
                else:
                    def fail_spawn(_argv: list[str], **_kwargs: Any) -> FakeProcess:
                        raise OSError("injected spawn failure")

                    launcher = fail_spawn  # type: ignore[assignment]
                assert run_drainer(temporary, queue, launcher, FakeClock(), owned) != 0
                assert queue.journal is not None
                assert queue.latest is not None
                assert owned.inheritable_at_exit is False
            finally:
                owned.close()


def test_finalization_failure_replays_fixed_journal_before_latest_on_next_run() -> None:
    """Catches skipping retained durable handoff state after a finalizer crash."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            queue = QueueHarness(owned)
            queue.journal = journal(1, DIGEST_1, phase="terminal", outcome="peer_superseded")
            queue.latest = target(2, DIGEST_2)
            queue.finalize_error = RuntimeError("crash before exact finalization")
            assert run_drainer(temporary, queue, FakeLauncher([(0, None)]), FakeClock(), owned) != 0
            assert queue.journal is not None
            queue.finalize_error = None
            queue.latest_read_forbidden_until_finalize = True
            assert run_drainer(temporary, queue, FakeLauncher([(0, None), (0, None)]), FakeClock(), owned) == 0
            assert [call[1:] for call in queue.finalize_calls] == [
                (1, DIGEST_1),
                (1, DIGEST_1),
                (2, DIGEST_2),
            ]
        finally:
            owned.close()


def test_busy_lock_is_benign_and_reads_or_launches_nothing() -> None:
    """Catches work/state mutation after another publisher owns refresh.lock."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock(busy=True)
        try:
            queue = QueueHarness(owned)
            queue.latest = target(1, DIGEST_1)
            launcher = FakeLauncher([])
            assert run_drainer(temporary, queue, launcher, FakeClock(), owned) == 0
            assert not launcher.calls
            assert queue.read_journal_calls == 0
            assert queue.read_latest_calls == 0
            assert not queue.finalize_calls
        finally:
            owned.close()


def test_malformed_journal_fails_before_latest_processing() -> None:
    """Catches bypassing a malformed/legacy fixed journal to publish newer latest state."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            queue = QueueHarness(owned)
            queue.latest = target(2, DIGEST_2)

            def malformed(_lock_dir: Path) -> None:
                raise ValueError("legacy journal")

            launcher = FakeLauncher([])
            assert run_drainer(temporary, queue, launcher, FakeClock(), owned, read_journal=malformed) != 0
            assert queue.read_latest_calls == 0
            assert not launcher.calls
        finally:
            owned.close()


def test_deadline_retains_newer_latest_instead_of_starting_without_reserve() -> None:
    """Catches beginning a new generation without time to terminate it safely."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            queue = QueueHarness(owned)
            queue.latest = target(1, DIGEST_1)
            clock = FakeClock()

            def arrive_and_consume_budget() -> None:
                queue.latest = target(2, DIGEST_2)
                clock.now += 91.0

            launcher = FakeLauncher([(0, arrive_and_consume_budget)])
            assert run_drainer(temporary, queue, launcher, clock, owned) == 0
            assert len(launcher.calls) == 1
            assert queue.latest is not None and queue.latest[0]["generation"] == 2
        finally:
            owned.close()


def test_failed_stale_attempt_without_followup_budget_remains_failure() -> None:
    """Catches reporting success when no generation was handled before the deadline."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        owned = FakeOwnedLock()
        try:
            queue = QueueHarness(owned)
            queue.latest = target(1, DIGEST_1)
            clock = FakeClock()

            def replace_and_consume_budget() -> None:
                queue.latest = target(2, DIGEST_2)
                clock.now += 91.0

            launcher = FakeLauncher([(75, replace_and_consume_budget)])
            assert run_drainer(temporary, queue, launcher, clock, owned) != 0
            assert len(launcher.calls) == 1
            assert queue.latest is not None and queue.latest[0]["generation"] == 2
        finally:
            owned.close()


def test_all_terminal_outcomes_use_only_exact_finalization_api() -> None:
    """Catches separate no-diff/peer cleanup paths that bypass authenticated finalization."""
    for outcome, phase in (("pushed", "raw_proven"), ("no_diff", "terminal"), ("peer_superseded", "terminal")):
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            owned = FakeOwnedLock()
            try:
                queue = QueueHarness(owned)
                queue.journal = journal(1, DIGEST_1, phase=phase, outcome=outcome)
                queue.latest = target(1, DIGEST_1)
                launcher = FakeLauncher([(0, None)])
                assert run_drainer(temporary, queue, launcher, FakeClock(), owned) == 0
                assert queue.finalize_calls == [(temporary / "state", 1, DIGEST_1)]
            finally:
                owned.close()


def test_real_public_state_finalization_preserves_only_pushed_pending_for_task5() -> None:
    """Proves the default public finalizer's pushed and terminal pending ownership boundary."""
    if os.name != "posix":
        return
    state = sys.modules["runner_publication_state"]
    with private_temporary_directory() as raw:
        temporary = Path(raw)

        for index, outcome in enumerate(("no_diff", "peer_superseded"), start=1):
            lock_dir = temporary / f"empty-{outcome}"
            repo_dir = temporary / f"repo-{outcome}"
            (repo_dir / "scripts").mkdir(parents=True)
            queued = enqueue_real_observation(state, lock_dir, 100 + index, str(index))
            stage_real_handoff(state, lock_dir, queued, outcome)
            owned = FakeOwnedLock()
            try:
                assert drain_real_state_once(repo_dir, lock_dir, owned) == 0
                paths = state.state_paths(lock_dir)
                assert not paths.pending.exists(), f"{outcome} created a pending record"
                assert not paths.latest.exists() and not paths.journal.exists()
            finally:
                owned.close()

        lock_dir = temporary / "retained-pushed-pending"
        repo_dir = temporary / "repo-retained"
        (repo_dir / "scripts").mkdir(parents=True)
        pushed = enqueue_real_observation(state, lock_dir, 200, "c")
        stage_real_handoff(state, lock_dir, pushed, "pushed")
        owned = FakeOwnedLock()
        try:
            assert drain_real_state_once(repo_dir, lock_dir, owned) == 0
            paths = state.state_paths(lock_dir)
            retained_pending = paths.pending.read_bytes()
            assert json.loads(retained_pending)["generation"] == pushed.generation

            for block, hash_character, outcome in (
                (201, "d", "no_diff"),
                (202, "e", "peer_superseded"),
            ):
                queued = enqueue_real_observation(state, lock_dir, block, hash_character)
                stage_real_handoff(state, lock_dir, queued, outcome)
                assert drain_real_state_once(repo_dir, lock_dir, owned) == 0
                assert paths.pending.read_bytes() == retained_pending
                assert json.loads(paths.pending.read_bytes())["generation"] == pushed.generation
                assert not paths.latest.exists() and not paths.journal.exists()
        finally:
            owned.close()


def test_source_has_only_public_state_boundary_and_no_git_pages_or_direct_cleanup() -> None:
    """Catches accidental expansion across the Task 4/Task 5 ownership boundary."""
    source_path = ROOT / "scripts" / "drain_publication_queue.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_state_names: set[str] = set()
    imported_modules: set[str] = set()
    called_names: set[str] = set()
    called_attributes: set[str] = set()
    string_values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "runner_publication_state":
            imported_state_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called_attributes.add(node.func.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            string_values.add(node.value)
    assert imported_state_names == {
        "finalize_pushed_handoff",
        "read_deferred_recovery_journal",
        "read_latest_with_digest",
    }
    assert "runner_publication_state" not in imported_modules
    assert not ({"cas_clear_latest", "cas_clear_pending", "recover_deferred_handoff", "unlink", "remove"} & called_names)
    assert not ({"unlink", "remove"} & called_attributes)
    assert not any("github.io" in value or "raw.githubusercontent" in value for value in string_values)
    assert not any(value == "git" or value.startswith("git ") for value in string_values)
    assert not any(isinstance(node, ast.Attribute) and node.attr == "LOCK_UN" for node in ast.walk(tree))


def test_runtime_paths_reject_drvfs_and_refresh_path_mismatch() -> None:
    """Catches resolving the mutable checkout or private state onto Windows DrvFS."""
    if os.name != "posix":
        return
    invalid = (
        (Path("/mnt/c/repo"), Path("/srv/state"), Path("/srv/state/refresh.lock")),
        (Path("/srv/repo"), Path("/mnt/d/state"), Path("/mnt/d/state/refresh.lock")),
        (Path("/srv/repo"), Path("/srv/state"), Path("/srv/other/refresh.lock")),
    )
    for repo_dir, lock_dir, lock_path in invalid:
        try:
            drainer.validate_runtime_paths(repo_dir, lock_dir, lock_path)
        except drainer.ConfigurationError:
            pass
        else:
            raise AssertionError(f"unsafe runtime paths were accepted: {repo_dir}, {lock_dir}, {lock_path}")

    original_cwd = Path.cwd()
    try:
        os.chdir("/tmp")
        try:
            drainer.validate_runtime_paths(
                Path("relative/repo"),
                Path("/srv/state"),
                Path("/srv/state/refresh.lock"),
            )
        except drainer.ConfigurationError:
            pass
        else:
            raise AssertionError("relative repository path was accepted")
    finally:
        os.chdir(original_cwd)

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        linked_repo = root / "repo-link"
        linked_repo.symlink_to("/mnt/c", target_is_directory=True)
        lock_dir = root / "state"
        try:
            drainer.validate_runtime_paths(linked_repo, lock_dir, lock_dir / "refresh.lock")
        except drainer.ConfigurationError:
            pass
        else:
            raise AssertionError("canonical repo symlink onto DrvFS was accepted")


def test_existing_broad_lock_fails_before_mutation() -> None:
    """Catches open_private_lock silently normalizing an already-unsafe lock."""
    if os.name != "posix":
        return
    with private_temporary_directory() as raw:
        root = Path(raw)
        root.chmod(0o700)
        lock_path = root / "refresh.lock"
        lock_path.write_text("sentinel", encoding="utf-8")
        lock_path.chmod(0o644)
        try:
            with drainer.owned_refresh_lock(
                lock_path,
                wait_seconds=0.0,
                poll_seconds=0.01,
                monotonic=lambda: 0.0,
                sleep=lambda _seconds: None,
                started_at_utc="2026-08-30T12:34:56Z",
            ):
                raise AssertionError("broad lock was accepted")
        except drainer.SecurePathError:
            pass
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o644
        assert lock_path.read_text(encoding="utf-8") == "sentinel"


def test_real_lock_same_description_relocks_separate_open_blocks_and_metadata_is_durable() -> None:
    """Catches self-deadlock, wrong inherited OFD, or metadata written before ownership."""
    if os.name != "posix":
        return
    import fcntl

    with private_temporary_directory() as raw:
        root = Path(raw)
        root.chmod(0o700)
        lock_path = root / "refresh.lock"
        with drainer.owned_refresh_lock(
            lock_path,
            wait_seconds=0.0,
            poll_seconds=0.01,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
            started_at_utc="2026-08-30T12:34:56Z",
        ) as descriptor:
            assert descriptor is not None
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            reopened = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                try:
                    fcntl.flock(reopened, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    assert exc.errno in {errno.EACCES, errno.EAGAIN}
                else:
                    raise AssertionError("separately reopened descriptor acquired the held lock")
            finally:
                os.close(reopened)
            metadata = lock_path.read_text(encoding="ascii")
            assert metadata.startswith("publisher_pid=")
            assert metadata.endswith("publisher_started_at_utc=2026-08-30T12:34:56Z\n")
            assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_lock_metadata_retries_short_writes_and_fsyncs_owned_descriptor() -> None:
    """Catches partial PID metadata or a lock owner record that was never made durable."""
    if os.name != "posix":
        return
    with private_temporary_directory() as raw:
        root = Path(raw)
        root.chmod(0o700)
        lock_path = root / "refresh.lock"
        original_write = drainer.os.write
        original_fsync = drainer.os.fsync
        writes: list[int] = []
        fsyncs: list[int] = []

        def short_write(descriptor: int, data: bytes) -> int:
            count = max(1, len(data) // 2)
            writes.append(count)
            return original_write(descriptor, data[:count])

        def tracked_fsync(descriptor: int) -> None:
            fsyncs.append(descriptor)
            original_fsync(descriptor)

        drainer.os.write = short_write
        drainer.os.fsync = tracked_fsync
        try:
            with drainer.owned_refresh_lock(
                lock_path,
                wait_seconds=0.0,
                poll_seconds=0.01,
                monotonic=lambda: 0.0,
                sleep=lambda _seconds: None,
                started_at_utc="2026-08-30T12:34:56Z",
            ) as descriptor:
                assert descriptor is not None
                assert len(writes) > 1
                assert descriptor in fsyncs
                assert lock_path.read_text(encoding="ascii").endswith(
                    "publisher_started_at_utc=2026-08-30T12:34:56Z\n"
                )
        finally:
            drainer.os.write = original_write
            drainer.os.fsync = original_fsync


def test_real_busy_lock_retains_existing_bytes_and_is_a_noop() -> None:
    """Catches truncating lock metadata while merely probing a busy publisher."""
    if os.name != "posix":
        return
    import fcntl

    with private_temporary_directory() as raw:
        root = Path(raw)
        root.chmod(0o700)
        lock_path = root / "refresh.lock"
        lock_path.write_text("existing-owner\n", encoding="ascii")
        lock_path.chmod(0o600)
        holder = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            clock = FakeClock()
            with drainer.owned_refresh_lock(
                lock_path,
                wait_seconds=0.1,
                poll_seconds=0.05,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                started_at_utc="2026-08-30T12:34:56Z",
            ) as descriptor:
                assert descriptor is None
            assert lock_path.read_text(encoding="ascii") == "existing-owner\n"
            assert 0.099999 <= clock.now - 100.0 <= 0.100001
        finally:
            os.close(holder)


def test_real_lock_rejects_symlink_hardlink_wrong_type_and_replaced_inode() -> None:
    """Catches accepting a lock path whose protected identity can be redirected."""
    if os.name != "posix":
        return
    for kind in ("symlink", "hardlink", "fifo"):
        with private_temporary_directory() as raw:
            root = Path(raw)
            root.chmod(0o700)
            lock_path = root / "refresh.lock"
            target_path = root / "target"
            target_path.write_text("x", encoding="ascii")
            target_path.chmod(0o600)
            if kind == "symlink":
                lock_path.symlink_to(target_path)
            elif kind == "hardlink":
                os.link(target_path, lock_path)
            else:
                os.mkfifo(lock_path, 0o600)
            try:
                with drainer.owned_refresh_lock(
                    lock_path,
                    wait_seconds=0.0,
                    poll_seconds=0.01,
                    monotonic=lambda: 0.0,
                    sleep=lambda _seconds: None,
                    started_at_utc="2026-08-30T12:34:56Z",
                ):
                    raise AssertionError(f"{kind} lock was accepted")
            except (drainer.SecurePathError, OSError):
                pass

    with private_temporary_directory() as raw:
        root = Path(raw)
        root.chmod(0o700)
        lock_path = root / "refresh.lock"
        other_path = root / "other.lock"
        lock_path.write_text("one", encoding="ascii")
        other_path.write_text("two", encoding="ascii")
        lock_path.chmod(0o600)
        other_path.chmod(0o600)
        original_reopen = drainer.open_existing_private_file

        def reopen_other(_path: Path, *, writable: bool = False) -> int:
            return os.open(other_path, os.O_RDWR if writable else os.O_RDONLY)

        drainer.open_existing_private_file = reopen_other
        try:
            try:
                with drainer.owned_refresh_lock(
                    lock_path,
                    wait_seconds=0.0,
                    poll_seconds=0.01,
                    monotonic=lambda: 0.0,
                    sleep=lambda _seconds: None,
                    started_at_utc="2026-08-30T12:34:56Z",
                ):
                    raise AssertionError("replaced inode was accepted")
            except drainer.SecurePathError:
                pass
        finally:
            drainer.open_existing_private_file = original_reopen


def test_real_lock_rejects_wrong_owner_metadata() -> None:
    """Catches accepting a lock owned by an identity other than the runner user."""
    if os.name != "posix" or os.getuid() == 0:
        return
    import runner_path_security

    with private_temporary_directory() as raw:
        root = Path(raw)
        root.chmod(0o700)
        lock_path = root / "refresh.lock"
        lock_path.write_text("owner", encoding="ascii")
        lock_path.chmod(0o600)
        original_uid = runner_path_security._CURRENT_UID
        runner_path_security._CURRENT_UID = os.getuid() + 1
        try:
            try:
                with drainer.owned_refresh_lock(
                    lock_path,
                    wait_seconds=0.0,
                    poll_seconds=0.01,
                    monotonic=lambda: 0.0,
                    sleep=lambda _seconds: None,
                    started_at_utc="2026-08-30T12:34:56Z",
                ):
                    raise AssertionError("wrong-owner metadata was accepted")
            except drainer.SecurePathError:
                pass
        finally:
            runner_path_security._CURRENT_UID = original_uid


def test_fixed_child_inherits_same_locked_description() -> None:
    """Catches a pass_fds descriptor that is not the exact owned refresh lock."""
    if os.name != "posix":
        return
    with private_temporary_directory() as raw:
        temporary = Path(raw)
        repo = temporary / "repo"
        scripts = repo / "scripts"
        scripts.mkdir(parents=True)
        publisher = scripts / "refresh_and_publish.sh"
        publisher.write_text(
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            "python3 - \"$DEGEN_DOGS_LOCK_FD\" \"$LOCK_CHECK_PATH\" <<'PY'\n"
            "import fcntl, os, sys\n"
            "fd = int(sys.argv[1])\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "with open(sys.argv[2], 'w', encoding='ascii') as handle:\n"
            "    handle.write(f'{os.fstat(fd).st_dev}:{os.fstat(fd).st_ino}')\n"
            "PY\n",
            encoding="utf-8",
        )
        publisher.chmod(0o755)
        lock_dir = temporary / "state"
        lock_dir.mkdir(mode=0o700)
        lock_path = lock_dir / "refresh.lock"
        check_path = temporary / "lock-check"
        with drainer.owned_refresh_lock(
            lock_path,
            wait_seconds=0.0,
            poll_seconds=0.01,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
            started_at_utc="2026-08-30T12:34:56Z",
        ) as descriptor:
            assert descriptor is not None
            result = drainer.run_publisher(
                repo_dir=repo,
                lock_dir=lock_dir,
                refresh_lock_path=lock_path,
                descriptor=descriptor,
                generation=1,
                digest=DIGEST_1,
                base_env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LOCK_CHECK_PATH": str(check_path)},
                timeout_seconds=5.0,
            )
            assert result.kind == "completed" and result.returncode == 0
            details = os.fstat(descriptor)
            assert check_path.read_text(encoding="ascii") == f"{details.st_dev}:{details.st_ino}"
            assert not os.get_inheritable(descriptor)


def test_timeout_kills_term_ignoring_grandchild_before_return() -> None:
    """Catches releasing refresh.lock while a descendant can still mutate the checkout."""
    if os.name != "posix":
        return
    with private_temporary_directory() as raw:
        temporary = Path(raw)
        repo = temporary / "repo"
        scripts = repo / "scripts"
        scripts.mkdir(parents=True)
        publisher = scripts / "refresh_and_publish.sh"
        pid_path = temporary / "grandchild.pid"
        publisher.write_text(
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            "exec python3 - \"$GRANDCHILD_PID_PATH\" <<'PY'\n"
            "import os, signal, subprocess, sys, time\n"
            "code = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'\n"
            "child = subprocess.Popen([sys.executable, '-c', code])\n"
            "with open(sys.argv[1], 'w', encoding='ascii') as handle:\n"
            "    handle.write(str(child.pid))\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True: time.sleep(1)\n"
            "PY\n",
            encoding="utf-8",
        )
        publisher.chmod(0o755)
        lock_dir = temporary / "state"
        lock_dir.mkdir(mode=0o700)
        lock_path = lock_dir / "refresh.lock"
        with drainer.owned_refresh_lock(
            lock_path,
            wait_seconds=0.0,
            poll_seconds=0.01,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
            started_at_utc="2026-08-30T12:34:56Z",
        ) as descriptor:
            assert descriptor is not None
            result = drainer.run_publisher(
                repo_dir=repo,
                lock_dir=lock_dir,
                refresh_lock_path=lock_path,
                descriptor=descriptor,
                generation=1,
                digest=DIGEST_1,
                base_env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "GRANDCHILD_PID_PATH": str(pid_path),
                },
                timeout_seconds=0.5,
                termination_grace_seconds=0.1,
                kill_poll_seconds=0.01,
            )
            assert result.kind == "timeout"
            grandchild = int(pid_path.read_text(encoding="ascii"))
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("TERM-ignoring grandchild survived timeout cleanup")


def test_termination_grace_applies_to_whole_group_after_leader_exits() -> None:
    """Catches immediately SIGKILLing a grandchild that is still inside its TERM cleanup grace."""
    if os.name != "posix":
        return
    with private_temporary_directory() as raw:
        temporary = Path(raw)
        repo = temporary / "repo"
        scripts = repo / "scripts"
        scripts.mkdir(parents=True)
        publisher = scripts / "refresh_and_publish.sh"
        ready_path = temporary / "grandchild.ready"
        cleaned_path = temporary / "grandchild.cleaned"
        publisher.write_text(
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            "exec python3 - \"$GRANDCHILD_READY_PATH\" \"$GRANDCHILD_CLEANED_PATH\" <<'PY'\n"
            "import os, signal, subprocess, sys, time\n"
            "code = '''import os,signal,sys,time\n"
            "ready,cleaned=sys.argv[1:3]\n"
            "def finish(_signum,_frame):\n"
            "    time.sleep(0.2)\n"
            "    open(cleaned,'w',encoding='ascii').write('clean')\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM,finish)\n"
            "open(ready,'w',encoding='ascii').write('ready')\n"
            "while True: time.sleep(1)\n"
            "'''\n"
            "child = subprocess.Popen([sys.executable, '-c', code, sys.argv[1], sys.argv[2]])\n"
            "while not os.path.exists(sys.argv[1]): time.sleep(0.01)\n"
            "while True: time.sleep(1)\n"
            "PY\n",
            encoding="utf-8",
        )
        publisher.chmod(0o755)
        lock_dir = temporary / "state"
        lock_dir.mkdir(mode=0o700)
        lock_path = lock_dir / "refresh.lock"
        with drainer.owned_refresh_lock(
            lock_path,
            wait_seconds=0.0,
            poll_seconds=0.01,
            monotonic=time.monotonic,
            sleep=time.sleep,
            started_at_utc="2026-08-30T12:34:56Z",
        ) as descriptor:
            assert descriptor is not None
            result = drainer.run_publisher(
                repo_dir=repo,
                lock_dir=lock_dir,
                refresh_lock_path=lock_path,
                descriptor=descriptor,
                generation=1,
                digest=DIGEST_1,
                base_env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "GRANDCHILD_READY_PATH": str(ready_path),
                    "GRANDCHILD_CLEANED_PATH": str(cleaned_path),
                },
                timeout_seconds=0.5,
                termination_grace_seconds=1.0,
                kill_poll_seconds=0.01,
            )
            assert result.kind == "timeout"
            assert cleaned_path.read_text(encoding="ascii") == "clean"


def test_launcher_publisher_case_executes_only_fixed_drainer_and_repins_paths() -> None:
    """Catches a launcher path/command selected from .env or queue state."""
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        repo = temporary / "repo"
        scripts = repo / "scripts"
        runtime_bin = scripts / "runtime-bin"
        runtime_bin.mkdir(parents=True)
        launcher_source = (ROOT / "scripts" / "run_wsl_runner_job.sh").read_text(encoding="utf-8")
        trusted_start = launcher_source.index("# WSL_TRUSTED_PYTHON_START")
        trusted_end = launcher_source.index("# WSL_TRUSTED_PYTHON_END") + len(
            "# WSL_TRUSTED_PYTHON_END"
        )
        launcher_source = (
            launcher_source[:trusted_start]
            + 'python_bin="${DEGEN_DOGS_TEST_PYTHON_BIN:?}"\n'
            + launcher_source[trusted_end:]
        )
        (scripts / "run_wsl_runner_job.sh").write_text(
            launcher_source, encoding="utf-8"
        )
        shutil.copy2(ROOT / "scripts" / "load_runner_env.sh", scripts / "load_runner_env.sh")
        capture = temporary / "capture.json"
        (scripts / "drain_publication_queue.py").write_text(
            "import json, os, sys\n"
            "with open(os.environ['CAPTURE_PATH'], 'w', encoding='utf-8') as handle:\n"
            "    json.dump({'argv': sys.argv, 'repo': os.environ['DEGEN_DOGS_REPO_DIR'], "
            "'lock': os.environ['DEGEN_DOGS_LOCK_DIR'], 'refresh': os.environ['DEGEN_DOGS_REFRESH_LOCK_PATH'], "
            "'mission': os.environ.get('MISSION3_REFRESH_COMMAND')}, handle)\n",
            encoding="utf-8",
        )
        (runtime_bin / "python3").write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
        (runtime_bin / "python3").chmod(0o755)
        env_file = repo / ".env.local"
        env_file.write_text(
            "MISSION3_REFRESH_COMMAND='touch /tmp/not-allowed'\n"
            "DEGEN_DOGS_REFRESH_LOCK_PATH=/tmp/not-allowed.lock\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        runner_home = temporary / "home"
        runner_home.mkdir()
        subprocess.run(["/usr/bin/git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repo), "config", "user.name", "Degen Dogs Windows Runner"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repo), "config", "user.email", "degen-dogs-runner@users.noreply.github.com"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repo), "config", "core.hooksPath", "/dev/null"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repo), "config", "core.autocrlf", "false"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repo), "config", "core.safecrlf", "true"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repo), "config", "core.sshCommand", f"ssh -F {runner_home}/.ssh/degen_dogs_config"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repo), "remote", "add", "origin", "git@github-degen-dogs:ael-dev3/Degen-Dogs-Mission-3.git"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repo), "config", "branch.main.remote", "origin"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(repo), "config", "branch.main.merge", "refs/heads/main"], check=True)
        lock_dir = temporary / "state"
        environment = dict(os.environ)
        environment.update(
            {
                "HOME": str(runner_home),
                "CAPTURE_PATH": str(capture),
                "DEGEN_DOGS_REPO_DIR": str(repo),
                "DEGEN_DOGS_LOCK_DIR": str(lock_dir),
                "DEGEN_DOGS_LOG_DIR": str(temporary / "logs"),
                "DEGEN_DOGS_ENV_FILE": str(env_file),
                "BASE_RPC_URLS": "https://one.invalid,https://two.invalid",
                "BASE_LOG_RPC_URLS": "https://one.invalid,https://two.invalid",
                "BASE_RPC_QUORUM_SIZE": "2",
                "DEGEN_DOGS_TEST_PYTHON_BIN": sys.executable,
            }
        )
        completed = subprocess.run(
            ["/bin/bash", "-p", str(scripts / "run_wsl_runner_job.sh"), "publisher"],
            cwd=repo,
            env=environment,
            check=False,
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(capture.read_text(encoding="utf-8"))
        assert payload == {
            "argv": [str(scripts / "drain_publication_queue.py")],
            "repo": str(repo),
            "lock": str(lock_dir),
            "refresh": str(lock_dir / "refresh.lock"),
            "mission": None,
        }
        capture.unlink()
        insufficient = dict(environment)
        insufficient["BASE_RPC_URLS"] = "https://only-one.invalid"
        rejected = subprocess.run(
            ["/bin/bash", "-p", str(scripts / "run_wsl_runner_job.sh"), "publisher"],
            cwd=repo,
            env=insufficient,
            check=False,
            text=True,
            capture_output=True,
        )
        assert rejected.returncode != 0
        assert not capture.exists(), "insufficient quorum reached the publisher drainer"


def test_main_installs_delivers_and_restores_sigterm_handler() -> None:
    """Catches default SIGTERM killing the parent before child-group cleanup can run."""
    if os.name != "posix":
        return
    original_drain = drainer.drain_publication_queue
    original_handler = signal.getsignal(signal.SIGTERM)
    original_env = dict(os.environ)

    def deliver(**_kwargs: Any) -> int:
        assert signal.getsignal(signal.SIGTERM) is drainer._termination_handler
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM handler did not interrupt the drainer")

    drainer.drain_publication_queue = deliver
    os.environ.update(
        {
            "DEGEN_DOGS_REPO_DIR": "/srv/test-repo",
            "DEGEN_DOGS_LOCK_DIR": "/srv/test-state",
            "DEGEN_DOGS_REFRESH_LOCK_PATH": "/srv/test-state/refresh.lock",
        }
    )
    try:
        try:
            drainer.main()
        except drainer.TerminationRequested:
            pass
        else:
            raise AssertionError("delivered SIGTERM did not raise TerminationRequested")
        assert signal.getsignal(signal.SIGTERM) == original_handler
    finally:
        drainer.drain_publication_queue = original_drain
        os.environ.clear()
        os.environ.update(original_env)


def test_sigterm_during_popen_is_deferred_until_child_group_is_known() -> None:
    """Catches losing a just-spawned child when SIGTERM interrupts Popen return."""
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        repo = temporary / "repo"
        (repo / "scripts").mkdir(parents=True)
        lock_dir = temporary / "state"
        lock_dir.mkdir()
        lock_path = lock_dir / "refresh.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        process = FakeProcess(0)
        terminated: list[int] = []
        previous = signal.signal(signal.SIGTERM, drainer._termination_handler)

        def signal_during_launch(_argv: list[str], **_kwargs: Any) -> FakeProcess:
            os.kill(os.getpid(), signal.SIGTERM)
            return process

        def terminate(child: FakeProcess, **_kwargs: Any) -> None:
            terminated.append(child.pid)
            child.returncode = 0

        try:
            result = drainer.run_publisher(
                repo_dir=repo,
                lock_dir=lock_dir,
                refresh_lock_path=lock_path,
                descriptor=descriptor,
                generation=1,
                digest=DIGEST_1,
                base_env={"PATH": "/usr/bin:/bin"},
                timeout_seconds=5.0,
                process_launcher=signal_during_launch,
                terminate_process_group=terminate,
            )
            assert result.kind == "terminated"
            assert terminated == [process.pid]
            assert not os.get_inheritable(descriptor)
        finally:
            signal.signal(signal.SIGTERM, previous)
            os.close(descriptor)


def test_sigterm_handler_is_restored_when_inheritable_setup_fails() -> None:
    """Catches leaving the temporary swallowing handler installed after a pre-spawn error."""
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        repo = temporary / "repo"
        (repo / "scripts").mkdir(parents=True)
        lock_dir = temporary / "state"
        lock_dir.mkdir()
        lock_path = lock_dir / "refresh.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        original_set_inheritable = drainer.os.set_inheritable
        previous = signal.signal(signal.SIGTERM, drainer._termination_handler)

        def fail_before_spawn(target: int, inheritable: bool) -> None:
            if target == descriptor and inheritable:
                raise OSError("injected inheritable setup failure")
            original_set_inheritable(target, inheritable)

        drainer.os.set_inheritable = fail_before_spawn
        try:
            try:
                drainer.run_publisher(
                    repo_dir=repo,
                    lock_dir=lock_dir,
                    refresh_lock_path=lock_path,
                    descriptor=descriptor,
                    generation=1,
                    digest=DIGEST_1,
                    base_env={"PATH": "/usr/bin:/bin"},
                    timeout_seconds=5.0,
                    process_launcher=FakeLauncher([]),
                )
            except OSError:
                pass
            else:
                raise AssertionError("injected inheritability error was swallowed")
            assert signal.getsignal(signal.SIGTERM) is drainer._termination_handler
        finally:
            drainer.os.set_inheritable = original_set_inheritable
            signal.signal(signal.SIGTERM, previous)
            os.close(descriptor)


def test_child_wait_budget_deducts_spawn_latency() -> None:
    """Catches extending the absolute child deadline by the Popen handshake duration."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        repo = temporary / "repo"
        (repo / "scripts").mkdir(parents=True)
        lock_dir = temporary / "state"
        lock_dir.mkdir()
        lock_path = lock_dir / "refresh.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        process = FakeProcess(0)
        clock = FakeClock()

        def delayed_spawn(_argv: list[str], **_kwargs: Any) -> FakeProcess:
            clock.now += 4.0
            return process

        try:
            result = drainer.run_publisher(
                repo_dir=repo,
                lock_dir=lock_dir,
                refresh_lock_path=lock_path,
                descriptor=descriptor,
                generation=1,
                digest=DIGEST_1,
                base_env={"PATH": "/usr/bin:/bin"},
                timeout_seconds=10.0,
                monotonic=clock.monotonic,
                process_launcher=delayed_spawn,
            )
            assert result.kind == "completed" and result.returncode == 0
            assert process.wait_timeouts == [6.0]
        finally:
            os.close(descriptor)


def test_repeated_sigterm_during_teardown_resumes_until_group_cleanup_completes() -> None:
    """Catches a second SIGTERM releasing the refresh lock before descendants are gone."""
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        repo = temporary / "repo"
        (repo / "scripts").mkdir(parents=True)
        lock_dir = temporary / "state"
        lock_dir.mkdir()
        lock_path = lock_dir / "refresh.lock"
        owned = FakeOwnedLock()
        process = FakeProcess(subprocess.TimeoutExpired(["publisher"], 1.0))
        calls: list[bool] = []

        def launcher(_argv: list[str], **_kwargs: Any) -> FakeProcess:
            return process

        def interrupted_once(child: FakeProcess, **_kwargs: Any) -> None:
            calls.append(owned.active)
            if len(calls) == 1:
                raise drainer.TerminationRequested()
            child.returncode = -getattr(signal, "SIGKILL", 9)

        try:
            with owned as descriptor:
                assert descriptor is not None
                result = drainer.run_publisher(
                    repo_dir=repo,
                    lock_dir=lock_dir,
                    refresh_lock_path=lock_path,
                    descriptor=descriptor,
                    generation=1,
                    digest=DIGEST_1,
                    base_env={"PATH": "/usr/bin:/bin"},
                    timeout_seconds=5.0,
                    process_launcher=launcher,
                    terminate_process_group=interrupted_once,
                )
                assert result.kind == "timeout"
                assert calls == [True, True]
                assert owned.active
        finally:
            owned.close()


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"drain_publication_queue_tests=pass count={len(tests)}")
