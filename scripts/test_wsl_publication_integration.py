#!/usr/bin/env python3
"""Deterministic ext4 proof for delayed Pages verification and queue draining."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
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

    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
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
    for forbidden in ("secret-path", "private-proof", "queue_digest", "expected_block_hash"):
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
    for forbidden in ("rpc.example", "alice", "C:\\Users", "secret-path", "private-proof"):
        assert forbidden not in rendered


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
        assert expected <= set(summary["problems"]), (name, summary)

    older_pending_newer_receipt = health.publication_health_summary({
        "last_generation": 8, "latest": None, "journal": None,
        "pending": pending(7, "2026-08-31T11:55:00Z"),
        "checkpoint": pushed(7, "2026-08-31T11:55:00Z"),
        "pages_verified": receipt(8, "2026-08-31T11:59:00Z"),
    }, now=now, publisher_lock_active=False)
    assert older_pending_newer_receipt["pages_verification_state"] == "pending"
    assert older_pending_newer_receipt["last_direct_data_compatible_static_block"] == 702
    assert "publication_proof_gap" not in older_pending_newer_receipt["problems"]


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
    test_queue_health_future_boundary_and_watcher_lock_rule()
    test_matching_pending_and_receipt_is_finalization_wait_not_verifier_failure()
    test_queue_health_state_machine_boundaries_and_durable_watermark()
    test_queue_health_timestamp_and_active_grace_boundaries()
    print("wsl_publication_integration=pass delay_seconds=120")
