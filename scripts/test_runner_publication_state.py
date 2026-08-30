#!/usr/bin/env python3
"""Behavioral tests for the private WSL publication queue state machine."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_module() -> Any:
    path = ROOT / "scripts" / "runner_publication_state.py"
    spec = importlib.util.spec_from_file_location("runner_publication_state", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


state = load_module()


class FakeLock:
    """Portable lock double: production fcntl behavior has its own WSL test."""

    entered = 0

    def __enter__(self) -> "FakeLock":
        self.entered += 1
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def observation(
    *,
    block: int = 100,
    block_hash: str = "0x" + "a" * 64,
    reorg_from: str | None = None,
) -> dict[str, object]:
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
        "canonical_reorg_from_hash": reorg_from,
    }


def enqueue(root: Path, value: dict[str, object], **kwargs: object) -> Any:
    return state.enqueue_latest_observation(
        root,
        value,
        runner_id="windows-wsl",
        run_scope="current",
        created_at_utc="2026-08-30T12:34:56Z",
        lock_context=FakeLock(),
        **kwargs,
    )


def private_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.chmod(path, 0o600)


def expect_invalid(operation: Any, message: str) -> None:
    try:
        operation()
    except state.StateValidationError:
        return
    raise AssertionError(message)


def test_protected_record_rejects_unsafe_or_malformed_latest() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        result = enqueue(root, observation())
        assert result.action == "enqueued"
        latest = state.state_paths(root).latest
        if os.name == "posix":
            assert stat.S_IMODE(latest.stat().st_mode) == 0o600
            assert latest.stat().st_nlink == 1

        malformed_cases: tuple[tuple[str, Any], ...] = (
            ("regular", lambda: (latest.unlink(), latest.mkdir())),
            ("size", lambda: latest.write_bytes(b"x" * (state.MAX_RECORD_BYTES + 1))),
            ("json", lambda: latest.write_text("{not-json", encoding="utf-8")),
            ("shape", lambda: latest.write_text('{"schema_version":1}', encoding="utf-8")),
        )
        if os.name == "posix":
            malformed_cases += (
                ("mode", lambda: os.chmod(latest, 0o644)),
                ("link", lambda: os.link(latest, latest.with_name("latest-link.json"))),
            )
        for name, corrupt in malformed_cases:
            if latest.exists():
                if latest.is_dir():
                    latest.rmdir()
                else:
                    latest.unlink()
            for sibling in latest.parent.glob("latest-link.json"):
                sibling.unlink()
            enqueue(root, observation())
            corrupt()
            expect_invalid(lambda: state.read_latest_with_digest(root), f"unsafe {name} record was accepted")
        if os.name == "posix" and latest.exists():
            latest.unlink()
        if os.name == "posix":
            enqueue(root, observation())
            original_owner = state._owner_uid
            state._owner_uid = lambda: os.getuid() + 1
            try:
                expect_invalid(lambda: state.read_latest_with_digest(root), "wrong-owner record was accepted")
            finally:
                state._owner_uid = original_owner


def test_state_root_must_not_be_a_symlink() -> None:
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / "target"
        target.mkdir()
        linked = root / "linked"
        linked.symlink_to(target, target_is_directory=True)
        expect_invalid(lambda: enqueue(linked, observation()), "symlinked lock root was accepted")


def test_atomic_writer_fsyncs_file_and_parent_and_rejects_corrupt_temp() -> None:
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = state.state_paths(root).latest
        fsync_calls: list[int] = []
        original_fsync = state.os.fsync
        state.os.fsync = lambda descriptor: fsync_calls.append(descriptor)
        try:
            state.atomic_write_record(path, state.latest_record(1, "windows-wsl", "current", "2026-08-30T12:34:56Z", observation()))
        finally:
            state.os.fsync = original_fsync
        assert len(fsync_calls) >= 2, "atomic write did not fsync both file and parent"
        temp = path.with_name(".latest.json.corrupt.tmp")
        temp.write_text("corrupt", encoding="utf-8")
        os.chmod(temp, 0o600)
        assert state.read_latest_with_digest(root)[0]["generation"] == 1
        assert temp.exists(), "reader must not trust or consume a corrupt temporary file"


def test_atomic_writer_retries_short_writes_until_the_full_record_is_durable() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = state.state_paths(root).latest
        record = state.latest_record(1, "windows-wsl", "current", "2026-08-30T12:34:56Z", observation())
        original_write = state.os.write
        writes = 0

        def one_byte_at_a_time(descriptor: int, data: bytes) -> int:
            nonlocal writes
            writes += 1
            return original_write(descriptor, data[:1])

        state.os.write = one_byte_at_a_time
        try:
            state.atomic_write_record(path, record)
        finally:
            state.os.write = original_write
        assert writes > 1
        assert state.read_latest_with_digest(root)[0] == record


def test_atomic_writer_rejects_zero_progress_without_installing_a_partial_target() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = state.state_paths(root).latest
        record = state.latest_record(1, "windows-wsl", "current", "2026-08-30T12:34:56Z", observation())
        original_write = state.os.write
        state.os.write = lambda _descriptor, _data: 0
        try:
            expect_invalid(
                lambda: state.atomic_write_record(path, record),
                "atomic writer accepted a zero-progress write",
            )
        finally:
            state.os.write = original_write
        assert not path.exists(), "zero-progress write installed a partial target"
        assert not list(path.parent.glob(f".{path.name}.*.tmp")), "zero-progress write leaked a temporary file"


def test_enqueue_is_latest_wins_but_chain_correct() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        first = enqueue(root, observation())
        identical = enqueue(root, observation())
        higher = enqueue(root, observation(block=101, block_hash="0x" + "c" * 64))
        lower = enqueue(root, observation(block=99, block_hash="0x" + "d" * 64))
        assert first.generation == 1
        assert identical.action == "coalesced" and identical.generation == 1
        assert higher.action == "replaced" and higher.generation == 2
        assert lower.action == "stale" and lower.generation == 2
        latest, _digest = state.read_latest_with_digest(root)
        assert latest["observation"]["confirmed_block_number"] == 101


def test_equal_height_hash_requires_explicit_quorum_reorg_transition() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        original = observation()
        enqueue(root, original)
        changed_hash = "0x" + "c" * 64
        expect_invalid(
            lambda: enqueue(root, observation(block_hash=changed_hash)),
            "equal-height hash replacement without a reorg proof was accepted",
        )
        accepted = enqueue(
            root,
            observation(block_hash=changed_hash, reorg_from=original["confirmed_block_hash"]),
            canonical_reorg_quorum=True,
        )
        assert accepted.action == "reorg_replaced" and accepted.generation == 2


def test_generation_digest_compare_and_swap_does_not_clear_newer_latest() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        first = enqueue(root, observation())
        assert state.cas_clear_latest(root, first.generation, first.digest, lock_context=FakeLock())
        assert state.read_latest_with_digest(root) is None
        second = enqueue(root, observation(block=101, block_hash="0x" + "c" * 64))
        assert second.generation == 2
        enqueue(root, observation(block=102, block_hash="0x" + "d" * 64))
        assert not state.cas_clear_latest(root, second.generation, second.digest, lock_context=FakeLock())
        latest, _digest = state.read_latest_with_digest(root)
        assert latest["generation"] == 3


def handoff_records(generation: int, digest: str, *, outcome: str = "pushed") -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    commit = "a" * 40
    terminal = outcome != "pushed"
    journal = {
        "schema_version": 1,
        "repo_realpath": str(ROOT),
        "branch": "main",
        "baseline_head": "d" * 40,
        "run_id": "queued-publication-test",
        "runner_id": "windows-wsl",
        "run_scope": "current",
        "created_at_utc": "2026-08-30T12:34:56Z",
        "publish_paths": ["generated", "public"],
        "alignment_runner_commit": None,
        "alignment_remote_head": None,
        "alignment_result": None,
        "publication_generation": generation,
        "queue_digest": digest,
        "terminal_outcome": outcome,
        "handoff_phase": "terminal" if terminal else "push_ready",
        "remote_commit": None if outcome == "no_diff" else commit,
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
        "raw_bundle_path": "public/generated/bundle.json",
        "expected_bundle_sha256": "b" * 64,
        "expected_bundle_bytes": 2,
        "expected_block_number": 100,
        "expected_block_hash": "0x" + "a" * 64,
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
    }
    return journal, pending, checkpoint


def generating_journal(generation: int, digest: str) -> dict[str, object]:
    journal, _pending, _checkpoint = handoff_records(generation, digest)
    journal["terminal_outcome"] = None
    journal["handoff_phase"] = "generating"
    journal["remote_commit"] = None
    return journal


def prepare_and_finalize_generation(root: Path, queued: Any, outcome: str) -> None:
    """Test-only full handoff used to exercise retained cross-generation state."""
    state.create_deferred_recovery_journal(
        root,
        generating_journal(queued.generation, queued.digest),
        lock_context=FakeLock(),
    )
    journal, pending, checkpoint = handoff_records(queued.generation, queued.digest, outcome=outcome)
    if outcome == "pushed":
        journal = state.arm_deferred_pushed_handoff(
            root,
            queued.generation,
            queued.digest,
            "a" * 40,
            lock_context=FakeLock(),
        )
        state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
    else:
        state.record_terminal_outcome(root, journal, checkpoint, lock_context=FakeLock())
    assert state.finalize_pushed_handoff(
        root,
        queued.generation,
        queued.digest,
        lock_context=FakeLock(),
    )


def test_deferred_journal_creation_and_push_arm_use_only_fixed_authenticated_state() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal = generating_journal(queued.generation, queued.digest)
        state.create_deferred_recovery_journal(root, journal, lock_context=FakeLock())
        paths = state.state_paths(root)
        assert paths.journal.exists()
        armed = state.arm_deferred_pushed_handoff(
            root,
            queued.generation,
            queued.digest,
            "a" * 40,
            lock_context=FakeLock(),
        )
        assert armed["handoff_phase"] == "push_ready"
        assert armed["terminal_outcome"] == "pushed"
        assert armed["remote_commit"] == "a" * 40
        assert state.read_deferred_recovery_journal(root) == armed


def test_pushed_handoff_is_recoverable_after_push_raw_proof_pending_and_checkpoint_boundaries() -> None:
    # Missing this test permits a retrying latest generation with no durable
    # checkpoint after any one of the ordered publisher fsync boundaries.
    for crash_before in ("pending", "checkpoint"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queued = enqueue(root, observation())
            generating = generating_journal(queued.generation, queued.digest)
            state.create_deferred_recovery_journal(root, generating, lock_context=FakeLock())
            journal = state.arm_deferred_pushed_handoff(
                root,
                queued.generation,
                queued.digest,
                "a" * 40,
                lock_context=FakeLock(),
            )
            _unused, pending, checkpoint = handoff_records(queued.generation, queued.digest)
            paths = state.state_paths(root)
            target = getattr(paths, crash_before)
            original_write = state.atomic_write_record

            def crash_at_target(path: Path, record: dict[str, object]) -> None:
                if Path(path) == target:
                    raise RuntimeError(f"simulated crash before {crash_before} fsync")
                original_write(path, record)

            state.atomic_write_record = crash_at_target
            try:
                try:
                    state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
                except RuntimeError as exc:
                    assert "simulated crash" in str(exc)
                else:
                    raise AssertionError(f"{crash_before} crash injection did not interrupt preparation")
            finally:
                state.atomic_write_record = original_write

            durable_journal = state.read_deferred_recovery_journal(root)
            assert durable_journal is not None and durable_journal["handoff_phase"] == "raw_proven"
            assert paths.journal.exists() and state.read_latest_with_digest(root) is not None
            assert paths.pending.exists() is (crash_before == "checkpoint")
            assert not paths.checkpoint.exists()
            assert state.recover_deferred_handoff(
                root,
                lambda commit: commit == "a" * 40,
                lock_context=FakeLock(),
            )
            assert paths.pending.exists() and paths.checkpoint.exists() and paths.journal.exists()

    # A crash immediately after the remote push but before the raw-proof
    # journal transition must re-prove and reconstruct from fresh exact bytes.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        state.create_deferred_recovery_journal(
            root,
            generating_journal(queued.generation, queued.digest),
            lock_context=FakeLock(),
        )
        state.arm_deferred_pushed_handoff(
            root,
            queued.generation,
            queued.digest,
            "a" * 40,
            lock_context=FakeLock(),
        )
        _journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        assert state.recover_deferred_handoff(
            root,
            lambda commit: commit == "a" * 40,
            pending=pending,
            checkpoint=checkpoint,
            lock_context=FakeLock(),
        )
        paths = state.state_paths(root)
        assert paths.pending.exists() and paths.checkpoint.exists() and paths.journal.exists()


def test_terminal_outcomes_checkpoint_before_finalization_and_no_diff_invents_no_commit() -> None:
    for outcome in ("no_diff", "peer_superseded"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queued = enqueue(root, observation())
            state.create_deferred_recovery_journal(
                root,
                generating_journal(queued.generation, queued.digest),
                lock_context=FakeLock(),
            )
            journal, _pending, checkpoint = handoff_records(queued.generation, queued.digest, outcome=outcome)
            state.record_terminal_outcome(root, journal, checkpoint, lock_context=FakeLock())
            paths = state.state_paths(root)
            assert paths.checkpoint.exists() and paths.journal.exists()
            assert state.read_latest_with_digest(root) is not None
            if outcome == "no_diff":
                assert journal["remote_commit"] is None and checkpoint["commit_sha"] is None


def test_retained_handoff_records_advance_across_every_sequential_outcome_pair() -> None:
    # Removing generation-aware replacement makes pending.json/pushed.json
    # one-shot records because successful finalization intentionally retains
    # them for verifier and health consumers.
    outcome_pairs = (
        ("pushed", "pushed"),
        ("pushed", "no_diff"),
        ("pushed", "peer_superseded"),
        ("no_diff", "pushed"),
        ("no_diff", "no_diff"),
        ("no_diff", "peer_superseded"),
        ("peer_superseded", "pushed"),
        ("peer_superseded", "no_diff"),
        ("peer_superseded", "peer_superseded"),
    )
    for first_outcome, second_outcome in outcome_pairs:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = enqueue(root, observation())
            prepare_and_finalize_generation(root, first, first_outcome)
            second = enqueue(root, observation(block=101, block_hash="0x" + "c" * 64))
            assert second.generation == first.generation + 1
            prepare_and_finalize_generation(root, second, second_outcome)

            paths = state.state_paths(root)
            checkpoint = state._validate_checkpoint(state._read_json(paths.checkpoint))
            assert checkpoint["generation"] == second.generation
            assert checkpoint["outcome"] == second_outcome
            if second_outcome == "pushed":
                pending = state._validate_pending(state._read_json(paths.pending))
                assert pending["generation"] == second.generation

    # Generation-aware replacement must still fail closed when an existing
    # record is the same generation with different proof, or is newer.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)

        conflicting = dict(pending)
        conflicting["expected_bundle_sha256"] = "c" * 64
        state.atomic_write_record(paths.pending, conflicting)
        expect_invalid(
            lambda: state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock()),
            "same-generation immutable pending conflict was replaced",
        )

    for target_name in ("pending", "checkpoint"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queued = enqueue(root, observation())
            journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
            paths = state.state_paths(root)
            state.atomic_write_record(paths.journal, journal)
            newer = dict(pending if target_name == "pending" else checkpoint)
            newer["generation"] = queued.generation + 1
            state.atomic_write_record(getattr(paths, target_name), newer)
            expect_invalid(
                lambda: state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock()),
                f"newer existing {target_name} record was replaced by an older handoff",
            )


def test_finalization_crashes_leave_queue_and_journal_in_a_retryable_order() -> None:
    for crash_before in ("latest", "journal"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queued = enqueue(root, observation())
            journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
            paths = state.state_paths(root)
            state.atomic_write_record(paths.journal, journal)
            state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
            original_unlink = state._unlink_record
            target = getattr(paths, crash_before)

            def crash_at_target(path: Path) -> bool:
                if path == target:
                    raise RuntimeError(f"simulated crash before {crash_before} unlink")
                return original_unlink(path)

            state._unlink_record = crash_at_target
            try:
                try:
                    state.finalize_pushed_handoff(root, queued.generation, queued.digest, lock_context=FakeLock())
                except RuntimeError as exc:
                    assert "simulated crash" in str(exc)
                else:
                    raise AssertionError(f"{crash_before} crash injection did not interrupt finalization")
            finally:
                state._unlink_record = original_unlink

            assert paths.journal.exists(), "journal was cleared before finalization completed"
            if crash_before == "latest":
                assert paths.latest.exists(), "queue was cleared before its CAS boundary"
            else:
                assert not paths.latest.exists(), "journal unlink was attempted before queue acknowledgement"
            assert state.finalize_pushed_handoff(
                root,
                queued.generation,
                queued.digest,
                lock_context=FakeLock(),
            )
            assert not paths.latest.exists() and not paths.journal.exists()


def test_handoff_rejects_every_journal_pending_checkpoint_identity_mismatch() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)
        state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
        for name, path, key, wrong in (
            ("journal", paths.journal, "publication_generation", 99),
            ("journal", paths.journal, "queue_digest", "f" * 64),
            ("pending", paths.pending, "generation", 99),
            ("pending", paths.pending, "queue_digest", "f" * 64),
            ("pending", paths.pending, "commit_sha", "c" * 40),
            ("checkpoint", paths.checkpoint, "generation", 99),
            ("checkpoint", paths.checkpoint, "queue_digest", "f" * 64),
            ("checkpoint", paths.checkpoint, "commit_sha", "c" * 40),
        ):
            value = json.loads(path.read_text(encoding="utf-8"))
            value[key] = wrong
            private_json(path, value)
            expect_invalid(
                lambda: state.finalize_pushed_handoff(root, queued.generation, queued.digest, lock_context=FakeLock()),
                f"{name} identity mismatch was accepted",
            )
            paths.pending.unlink(missing_ok=True)
            paths.checkpoint.unlink(missing_ok=True)
            state.atomic_write_record(paths.journal, journal)
            state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())


def test_finalize_clears_exact_generation_then_authenticated_journal_only() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        state.atomic_write_record(state.state_paths(root).journal, journal)
        state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
        newer = enqueue(root, observation(block=101, block_hash="0x" + "c" * 64))
        assert state.finalize_pushed_handoff(root, queued.generation, queued.digest, lock_context=FakeLock())
        latest, _digest = state.read_latest_with_digest(root)
        assert latest["generation"] == newer.generation
        paths = state.state_paths(root)
        assert not paths.journal.exists()
        assert paths.pending.exists() and paths.checkpoint.exists()


def test_terminal_no_diff_outcome_is_durable_and_acknowledged() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, _pending, checkpoint = handoff_records(queued.generation, queued.digest, outcome="no_diff")
        state.atomic_write_record(state.state_paths(root).journal, journal)
        state.record_terminal_outcome(root, journal, checkpoint, lock_context=FakeLock())
        assert state.finalize_pushed_handoff(root, queued.generation, queued.digest, lock_context=FakeLock())
        assert state.read_latest_with_digest(root) is None
        assert state.state_paths(root).checkpoint.exists()


def test_terminal_outcome_requires_no_diff_without_push_time_and_peer_with_commit_only() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, _pending, no_diff = handoff_records(queued.generation, queued.digest, outcome="no_diff")
        no_diff["push_completed_at_utc"] = "2026-08-30T12:35:00Z"
        state.atomic_write_record(state.state_paths(root).journal, journal)
        expect_invalid(
            lambda: state.record_terminal_outcome(root, journal, no_diff, lock_context=FakeLock()),
            "no-diff terminal outcome accepted an invented push time",
        )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, _pending, peer = handoff_records(queued.generation, queued.digest, outcome="peer_superseded")
        peer["commit_sha"] = None
        state.atomic_write_record(state.state_paths(root).journal, journal)
        expect_invalid(
            lambda: state.record_terminal_outcome(root, journal, peer, lock_context=FakeLock()),
            "peer-superseded terminal outcome accepted no immutable peer commit",
        )

        peer["commit_sha"] = "a" * 40
        state.record_terminal_outcome(root, journal, peer, lock_context=FakeLock())


def test_journal_rejects_unrecognized_fields() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, _pending, checkpoint = handoff_records(queued.generation, queued.digest, outcome="no_diff")
        journal["untrusted_path"] = "/tmp/attacker-controlled"
        state.atomic_write_record(state.state_paths(root).journal, journal)
        expect_invalid(
            lambda: state.record_terminal_outcome(root, journal, checkpoint, lock_context=FakeLock()),
            "journal accepted an arbitrary unrecognized field",
        )


def test_pending_compare_and_swap_requires_the_exact_generation_and_commit() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)
        state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
        replacement = dict(pending)
        replacement["retry_count"] = 1
        assert not state.cas_write_pending(root, 99, "a" * 40, replacement, lock_context=FakeLock())
        assert state.cas_write_pending(root, queued.generation, "a" * 40, replacement, lock_context=FakeLock())
        assert not state.cas_clear_pending(root, queued.generation, "c" * 40, lock_context=FakeLock())
        assert not state.cas_clear_pending(root, queued.generation, "a" * 40, lock_context=FakeLock())
        assert state.finalize_pushed_handoff(
            root,
            queued.generation,
            queued.digest,
            lock_context=FakeLock(),
        )
        assert state.cas_clear_pending(root, queued.generation, "a" * 40, lock_context=FakeLock())


def test_pending_clear_waits_for_authenticated_journal_finalization_and_fails_on_conflict() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)
        state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())

        assert not state.cas_clear_pending(
            root,
            queued.generation,
            "a" * 40,
            lock_context=FakeLock(),
        ), "verifier cleared pending before Task 4 finalized its matching journal"
        assert paths.pending.exists() and paths.journal.exists()

        durable_journal = state._validate_journal(state._read_json(paths.journal))
        conflicting_journal = dict(durable_journal)
        conflicting_journal["queue_digest"] = "f" * 64
        state.atomic_write_record(paths.journal, conflicting_journal)
        expect_invalid(
            lambda: state.cas_clear_pending(
                root,
                queued.generation,
                "a" * 40,
                lock_context=FakeLock(),
            ),
            "same-generation conflicting journal/pending identity did not fail closed",
        )

        state.atomic_write_record(paths.journal, durable_journal)
        assert state.finalize_pushed_handoff(
            root,
            queued.generation,
            queued.digest,
            lock_context=FakeLock(),
        )
        assert not paths.journal.exists() and paths.pending.exists()
        assert state.cas_clear_pending(
            root,
            queued.generation,
            "a" * 40,
            lock_context=FakeLock(),
        )


def test_pushed_finalization_authenticates_raw_proof_checkpoint_and_retry_progress() -> None:
    # A push-ready journal alone cannot authenticate fabricated pending and
    # checkpoint files, even when their coarse generation/commit identity fits.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)
        state.atomic_write_record(paths.pending, pending)
        state.atomic_write_record(paths.checkpoint, checkpoint)
        expect_invalid(
            lambda: state.finalize_pushed_handoff(
                root,
                queued.generation,
                queued.digest,
                lock_context=FakeLock(),
            ),
            "push-ready journal finalized fabricated handoff records",
        )
        assert paths.journal.exists() and paths.latest.exists()

    for target, key, wrong in (
        ("pending", "expected_bundle_sha256", "c" * 64),
        ("pending", "retry_deadline_utc", "2026-08-30T12:40:00Z"),
        ("checkpoint", "push_completed_at_utc", "2026-08-30T12:36:00Z"),
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queued = enqueue(root, observation())
            journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
            paths = state.state_paths(root)
            state.atomic_write_record(paths.journal, journal)
            state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
            path = getattr(paths, target)
            value = state._read_json(path)
            value[key] = wrong
            state.atomic_write_record(path, value)
            expect_invalid(
                lambda: state.finalize_pushed_handoff(
                    root,
                    queued.generation,
                    queued.digest,
                    lock_context=FakeLock(),
                ),
                f"finalization accepted divergent {target} field {key}",
            )
            assert paths.journal.exists() and paths.latest.exists()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)
        state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
        advanced = dict(pending)
        advanced["retry_count"] = 4
        advanced["retry_deadline_utc"] = "2026-08-30T13:15:00Z"
        assert state.cas_write_pending(
            root,
            queued.generation,
            "a" * 40,
            advanced,
            lock_context=FakeLock(),
        )
        assert state.finalize_pushed_handoff(
            root,
            queued.generation,
            queued.digest,
            lock_context=FakeLock(),
        )
        assert not paths.journal.exists() and paths.pending.exists()


def test_prepare_pushed_handoff_rejects_checkpoint_not_derived_from_raw_proof() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)
        checkpoint["push_completed_at_utc"] = "2026-08-30T12:36:00Z"
        expect_invalid(
            lambda: state.prepare_pushed_handoff(
                root,
                journal,
                pending,
                checkpoint,
                lock_context=FakeLock(),
            ),
            "prepare accepted checkpoint push time divergent from proven pending",
        )
        persisted = state._validate_journal(state._read_json(paths.journal))
        assert persisted["handoff_phase"] == "push_ready"
        assert not paths.pending.exists() and not paths.checkpoint.exists()


def test_terminal_finalization_requires_the_exact_reconstructed_checkpoint() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, _pending, checkpoint = handoff_records(
            queued.generation,
            queued.digest,
            outcome="no_diff",
        )
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)
        state.record_terminal_outcome(root, journal, checkpoint, lock_context=FakeLock())
        fabricated = dict(checkpoint)
        fabricated["commit_sha"] = "c" * 40
        state.atomic_write_record(paths.checkpoint, fabricated)
        expect_invalid(
            lambda: state.finalize_pushed_handoff(
                root,
                queued.generation,
                queued.digest,
                lock_context=FakeLock(),
            ),
            "terminal finalization accepted a checkpoint not reconstructed from its journal",
        )
        assert paths.journal.exists() and paths.latest.exists()


def test_pending_compare_and_swap_preserves_immutable_proof_and_monotonic_retry_state() -> None:
    immutable_mutations: tuple[tuple[str, object], ...] = (
        ("generation", 2),
        ("queue_digest", "f" * 64),
        ("commit_sha", "c" * 40),
        ("raw_status_path", "public/generated/other-status.json"),
        ("raw_bundle_path", "public/generated/other-bundle.json"),
        ("expected_bundle_sha256", "c" * 64),
        ("expected_bundle_bytes", 3),
        ("expected_block_number", 101),
        ("expected_block_hash", "0x" + "c" * 64),
        ("push_completed_at_utc", "2026-08-30T12:36:00Z"),
    )
    for key, wrong in immutable_mutations:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queued = enqueue(root, observation())
            journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
            state.atomic_write_record(state.state_paths(root).journal, journal)
            state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
            replacement = dict(pending)
            replacement[key] = wrong
            expect_invalid(
                lambda: state.cas_write_pending(
                    root,
                    queued.generation,
                    "a" * 40,
                    replacement,
                    lock_context=FakeLock(),
                ),
                f"pending CAS changed immutable field {key}",
            )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)
        state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
        advanced = dict(pending)
        advanced["retry_count"] = 2
        advanced["retry_deadline_utc"] = "2026-08-30T12:55:00Z"
        assert state.cas_write_pending(
            root,
            queued.generation,
            "a" * 40,
            advanced,
            lock_context=FakeLock(),
        )
        for key, regressed in (
            ("retry_count", 1),
            ("retry_deadline_utc", "2026-08-30T12:50:00Z"),
        ):
            replacement = dict(advanced)
            replacement[key] = regressed
            expect_invalid(
                lambda replacement=replacement: state.cas_write_pending(
                    root,
                    queued.generation,
                    "a" * 40,
                    replacement,
                    lock_context=FakeLock(),
                ),
                f"pending CAS regressed {key}",
            )


def test_recovery_after_pending_retry_preserves_retry_progress_and_recreates_checkpoint() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)
        state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
        retried = dict(pending)
        retried["retry_count"] = 3
        retried["retry_deadline_utc"] = "2026-08-30T13:00:00Z"
        assert state.cas_write_pending(
            root,
            queued.generation,
            "a" * 40,
            retried,
            lock_context=FakeLock(),
        )
        paths.checkpoint.unlink()

        assert state.recover_deferred_handoff(
            root,
            lambda commit: commit == "a" * 40,
            lock_context=FakeLock(),
        )
        assert state._validate_pending(state._read_json(paths.pending)) == retried
        assert state._validate_checkpoint(state._read_json(paths.checkpoint)) == checkpoint


def test_alignment_cannot_erase_a_pushed_handoff_phase() -> None:
    for phase in ("push_ready", "raw_proven"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queued = enqueue(root, observation())
            state.create_deferred_recovery_journal(
                root,
                generating_journal(queued.generation, queued.digest),
                lock_context=FakeLock(),
            )
            journal = state.arm_deferred_pushed_handoff(
                root,
                queued.generation,
                queued.digest,
                "a" * 40,
                lock_context=FakeLock(),
            )
            if phase == "raw_proven":
                _unused, pending, checkpoint = handoff_records(queued.generation, queued.digest)
                state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
            expect_invalid(
                lambda: state.update_deferred_alignment(
                    root,
                    queued.generation,
                    queued.digest,
                    "a" * 40,
                    "c" * 40,
                    "regenerate",
                    lock_context=FakeLock(),
                ),
                f"alignment erased {phase} pushed handoff state",
            )


def test_rejected_push_transition_is_narrowly_limited_to_exact_push_ready_identity() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        state.create_deferred_recovery_journal(
            root,
            generating_journal(queued.generation, queued.digest),
            lock_context=FakeLock(),
        )
        armed = state.arm_deferred_pushed_handoff(
            root,
            queued.generation,
            queued.digest,
            "a" * 40,
            lock_context=FakeLock(),
        )
        for generation, digest, runner_commit in (
            (queued.generation + 1, queued.digest, "a" * 40),
            (queued.generation, "f" * 64, "a" * 40),
            (queued.generation, queued.digest, "d" * 40),
        ):
            expect_invalid(
                lambda generation=generation, digest=digest, runner_commit=runner_commit: (
                    state.record_deferred_push_rejected_alignment(
                        root,
                        generation,
                        digest,
                        runner_commit,
                        "c" * 40,
                        "regenerate",
                        lock_context=FakeLock(),
                    )
                ),
                "rejected-push transition accepted non-exact push-ready identity",
            )
            assert state.read_deferred_recovery_journal(root) == armed

        updated = state.record_deferred_push_rejected_alignment(
            root,
            queued.generation,
            queued.digest,
            "a" * 40,
            "c" * 40,
            "peer_supersedes",
            lock_context=FakeLock(),
        )
        assert updated["handoff_phase"] == "terminal"
        assert updated["terminal_outcome"] == "peer_superseded"
        assert updated["alignment_runner_commit"] == "a" * 40
        assert updated["alignment_remote_head"] == "c" * 40

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        state.create_deferred_recovery_journal(
            root,
            generating_journal(queued.generation, queued.digest),
            lock_context=FakeLock(),
        )
        state.arm_deferred_pushed_handoff(
            root,
            queued.generation,
            queued.digest,
            "a" * 40,
            lock_context=FakeLock(),
        )
        updated = state.record_deferred_push_rejected_alignment(
            root,
            queued.generation,
            queued.digest,
            "a" * 40,
            "c" * 40,
            "regenerate",
            lock_context=FakeLock(),
        )
        assert updated["handoff_phase"] == "generating"
        assert updated["terminal_outcome"] is None
        assert updated["remote_commit"] is None
        assert updated["alignment_result"] == "regenerate"

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        state.create_deferred_recovery_journal(
            root,
            generating_journal(queued.generation, queued.digest),
            lock_context=FakeLock(),
        )
        armed = state.arm_deferred_pushed_handoff(
            root,
            queued.generation,
            queued.digest,
            "a" * 40,
            lock_context=FakeLock(),
        )
        _journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        state.prepare_pushed_handoff(root, armed, pending, checkpoint, lock_context=FakeLock())
        expect_invalid(
            lambda: state.record_deferred_push_rejected_alignment(
                root,
                queued.generation,
                queued.digest,
                "a" * 40,
                "c" * 40,
                "regenerate",
                lock_context=FakeLock(),
            ),
            "rejected-push transition erased durable raw proof",
        )


def test_recovery_requires_independent_immutable_commit_confirmation() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)
        assert not state.recover_deferred_handoff(root, lambda _commit: False, lock_context=FakeLock())
        assert not paths.pending.exists() and not paths.checkpoint.exists()
        assert state.recover_deferred_handoff(
            root,
            lambda commit: commit == "a" * 40,
            pending=pending,
            checkpoint=checkpoint,
            lock_context=FakeLock(),
        )
        assert paths.pending.exists() and paths.checkpoint.exists()


def test_recovery_rejects_existing_untrusted_or_conflicting_handoff_records() -> None:
    if os.name == "posix":
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queued = enqueue(root, observation())
            journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
            paths = state.state_paths(root)
            state.atomic_write_record(paths.journal, journal)
            target = root / "attacker-pending.json"
            private_json(target, pending)
            paths.pending.symlink_to(target)
            expect_invalid(
                lambda: state.recover_deferred_handoff(root, lambda _commit: True, pending=pending, checkpoint=checkpoint, lock_context=FakeLock()),
                "recovery accepted a symlinked pending record",
            )

    for name in ("pending", "checkpoint"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queued = enqueue(root, observation())
            journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
            paths = state.state_paths(root)
            state.atomic_write_record(paths.journal, journal)
            conflicting = dict(pending if name == "pending" else checkpoint)
            conflicting["generation"] = queued.generation + 1
            state.atomic_write_record(getattr(paths, name), conflicting)
            expect_invalid(
                lambda: state.recover_deferred_handoff(root, lambda _commit: True, pending=pending, checkpoint=checkpoint, lock_context=FakeLock()),
                f"recovery accepted a conflicting existing {name} record",
            )


def test_finalization_rejects_outcome_or_same_generation_digest_mismatch_without_unlinking_journal() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)
        state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
        checkpoint["outcome"] = "no_diff"
        checkpoint["push_completed_at_utc"] = None
        state.atomic_write_record(paths.checkpoint, checkpoint)
        expect_invalid(
            lambda: state.finalize_pushed_handoff(root, queued.generation, queued.digest, lock_context=FakeLock()),
            "finalization accepted a checkpoint outcome different from the journal",
        )
        assert paths.journal.exists()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = enqueue(root, observation())
        journal, pending, checkpoint = handoff_records(queued.generation, queued.digest)
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)
        state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
        conflicting_latest = dict(queued.record)
        conflicting_latest["created_at_utc"] = "2026-08-30T12:35:56Z"
        state.atomic_write_record(paths.latest, conflicting_latest)
        expect_invalid(
            lambda: state.finalize_pushed_handoff(root, queued.generation, queued.digest, lock_context=FakeLock()),
            "finalization cleared a same-generation record with a different digest",
        )
        assert paths.journal.exists()


def test_wsl_fcntl_lock_is_exclusive_and_inode_stable() -> None:
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        lock_path = state.state_paths(root).lock
        with state.production_lock(lock_path):
            first_inode = lock_path.stat().st_ino
            expect_invalid(
                lambda: state.production_lock(lock_path, nonblocking=True).__enter__(),
                "second fcntl lock acquired while production lock was held",
            )
            assert lock_path.stat().st_ino == first_inode


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"runner_publication_state_tests=pass count={len(tests)}")
