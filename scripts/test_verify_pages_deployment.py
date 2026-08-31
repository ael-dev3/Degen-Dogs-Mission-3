#!/usr/bin/env python3
"""Behavioral tests for detached immutable GitHub Pages verification."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import http.server
import importlib.util
import io
import json
import os
import socketserver
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str) -> Any:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


state = load_module("runner_publication_state")
verifier = load_module("verify_pages_deployment")


class FakeLock:
    def __enter__(self) -> "FakeLock":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class PortableState:
    """Use the real state machine with injected locks on native Windows."""

    PendingFinalizeResult = state.PendingFinalizeResult
    StateValidationError = state.StateValidationError

    def read_pending_with_digest(self, lock_dir: Path) -> Any:
        return state.read_pending_with_digest(lock_dir, lock_context=FakeLock())

    def cas_write_pending(
        self,
        lock_dir: Path,
        generation: int,
        commit_sha: str,
        replacement: dict[str, Any],
    ) -> bool:
        return state.cas_write_pending(
            lock_dir,
            generation,
            commit_sha,
            replacement,
            lock_context=FakeLock(),
        )

    def finalize_verified_pending(
        self,
        lock_dir: Path,
        captured: Any,
        verified_at: str,
    ) -> Any:
        return state.finalize_verified_pending(
            lock_dir,
            captured,
            verified_at,
            lock_context=FakeLock(),
        )


class FakeClock:
    def __init__(self, start: dt.datetime | None = None) -> None:
        self.wall = start or dt.datetime(2026, 8, 30, 12, 35, tzinfo=dt.timezone.utc)
        self.mono = 100.0
        self.sleeps: list[float] = []

    def utc_now(self) -> dt.datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.mono += seconds
        self.wall += dt.timedelta(seconds=seconds)


class ScriptedTransport:
    def __init__(
        self,
        responses: dict[str, list[Any]],
        hook: Callable[[str, int], None] | None = None,
    ) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[tuple[str, str, int, float]] = []
        self.hook = hook

    def fetch(
        self,
        url: str,
        resource: str,
        max_bytes: int,
        timeout: float,
        *,
        absolute_deadline: float | None = None,
    ) -> bytes:
        self.calls.append((url, resource, max_bytes, timeout))
        if self.hook is not None:
            self.hook(resource, len(self.calls))
        queue = self.responses.get(resource, [])
        if not queue:
            raise AssertionError(f"unexpected request for {resource}")
        value = queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.chmod(path, 0o600)


def proof(block: int = 123, block_hash: str = "0x" + "a" * 64) -> tuple[bytes, bytes, dict[str, Any]]:
    bundle_value = {
        "auction_feed": [],
        "current_auction": [],
        "current_auction_bid_history": [],
        "kind": "degen_dogs_live_snapshot",
        "latest_generated_block": block,
        "mission3_metrics": [],
        "schema_version": 1,
        "snapshot_block_hash": block_hash,
    }
    bundle = (
        json.dumps(bundle_value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(bundle).hexdigest()
    filename = f"live_snapshot_{block}_{block_hash[2:]}_{digest}.json"
    status_value = {
        "kind": "refresh_status",
        "latest_generated_block": block,
        "live_snapshot_bundle": filename,
        "live_snapshot_bundle_bytes": len(bundle),
        "live_snapshot_bundle_schema_version": 1,
        "live_snapshot_bundle_sha256": digest,
        "schema_version": 1,
        "snapshot_block_hash": block_hash,
    }
    status = (json.dumps(status_value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    pending = {
        "schema_version": 1,
        "generation": 7,
        "queue_digest": "e" * 64,
        "commit_sha": "a" * 40,
        "raw_status_path": "public/generated/refresh_status.json",
        "raw_bundle_path": f"public/generated/{filename}",
        "expected_bundle_sha256": digest,
        "expected_bundle_bytes": len(bundle),
        "expected_block_number": block,
        "expected_block_hash": block_hash,
        "push_completed_at_utc": "2026-08-30T12:35:00Z",
        "retry_deadline_utc": "2026-08-30T12:45:00Z",
        "retry_count": 0,
    }
    return status, bundle, pending


def install_pending(root: Path, pending: dict[str, Any]) -> None:
    state.atomic_write_record(state.state_paths(root).pending, pending)


def success_transport(status: bytes, bundle: bytes, **overrides: list[Any]) -> ScriptedTransport:
    responses: dict[str, list[Any]] = {
        "raw_status": [status],
        "raw_bundle": [bundle],
        "pages_status": [status],
        "pages_bundle": [bundle],
    }
    responses.update(overrides)
    return ScriptedTransport(responses)


def run_once(
    root: Path,
    transport: Any,
    *,
    clock: FakeClock | None = None,
    telemetry: list[dict[str, Any]] | None = None,
    state_api: Any | None = None,
    budget: float = 20.0,
    interval: float = 5.0,
) -> Any:
    active_clock = clock or FakeClock()
    rows = telemetry if telemetry is not None else []
    return verifier.run_once(
        root,
        root / "logs",
        state_api=state_api or PortableState(),
        transport=transport,
        telemetry_writer=lambda _log_dir, row: rows.append(dict(row)),
        utc_now=active_clock.utc_now,
        monotonic=active_clock.monotonic,
        sleep=active_clock.sleep,
        config=verifier.VerifierConfig(
            invocation_budget_seconds=budget,
            pages_poll_interval_seconds=interval,
            request_timeout_seconds=3.0,
        ),
    )


def assert_hard(result: Any, expected_code: str | None = None) -> None:
    assert result.exit_code == 1, result
    if expected_code is not None:
        assert result.error_code == expected_code, result


def test_exact_immutable_proof_clears_pending_and_installs_receipt() -> None:
    status, bundle, pending = proof()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)
        rows: list[dict[str, Any]] = []
        transport = success_transport(status, bundle)
        result = run_once(root, transport, telemetry=rows)
        assert (result.exit_code, result.result, result.error_code) == (0, "verified_cleared", None)
        assert [call[1] for call in transport.calls] == [
            "raw_status", "raw_bundle", "pages_status", "pages_bundle"
        ]
        assert transport.calls[0][0] == (
            "https://raw.githubusercontent.com/ael-dev3/Degen-Dogs-Mission-3/"
            + pending["commit_sha"]
            + "/public/generated/refresh_status.json"
        )
        assert transport.calls[1][0].endswith("/" + pending["raw_bundle_path"])
        assert transport.calls[2][0] == (
            "https://ael-dev3.github.io/Degen-Dogs-Mission-3/generated/refresh_status.json"
        )
        assert transport.calls[3][0].endswith("/generated/" + Path(pending["raw_bundle_path"]).name)
        assert not state.state_paths(root).pending.exists()
        receipt = state.read_pages_verified_receipt(root, lock_context=FakeLock())
        assert receipt is not None and receipt["generation"] == pending["generation"]
        assert len(rows) == 1
        assert rows[0]["result"] == "proof_verified"
        assert rows[0]["raw_verified"] is True and rows[0]["pages_verified"] is True
        assert set(rows[0]) == {
            "schema_version", "timestamp_utc", "result", "error_code", "generation",
            "commit_sha", "expected_block_number", "retry_count", "duration_seconds",
            "raw_verified", "pages_verified",
        }


def test_raw_bytes_latch_once_and_pages_bundle_waits_for_exact_status() -> None:
    status, bundle, pending = proof()
    stale = status.replace(b'"latest_generated_block": 123', b'"latest_generated_block": 122')
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)
        transport = success_transport(
            status,
            bundle,
            pages_status=[stale, stale, status],
            pages_bundle=[bundle],
        )
        result = run_once(root, transport)
        assert result.result == "verified_cleared"
        kinds = [call[1] for call in transport.calls]
        assert kinds.count("raw_status") == 1 and kinds.count("raw_bundle") == 1
        assert kinds == ["raw_status", "raw_bundle", "pages_status", "pages_status", "pages_status", "pages_bundle"]


def test_semantically_equal_but_byte_different_pages_never_verifies() -> None:
    status, bundle, pending = proof()
    status_value = json.loads(status)
    semantic_status = (json.dumps(status_value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    bundle_value = json.loads(bundle)
    semantic_bundle = json.dumps(bundle_value, sort_keys=True, separators=(", ", ": ")).encode() + b"\n"
    for mismatch_target, pages_status, pages_bundle in (
        ("status", semantic_status, bundle),
        ("bundle", status, semantic_bundle),
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_pending(root, pending)
            transport = success_transport(
                status,
                bundle,
                pages_status=[pages_status] * 10,
                pages_bundle=[pages_bundle] * 10,
            )
            result = run_once(root, transport, budget=6.0, interval=2.0)
            assert (result.exit_code, result.result) == (2, "unresolved_retry_scheduled")
            assert state.state_paths(root).pending.exists()
            if mismatch_target == "status":
                assert all(call[1] != "pages_bundle" for call in transport.calls)


def test_pending_paths_filename_components_and_size_are_exact() -> None:
    status, bundle, pending = proof()
    cases: tuple[tuple[str, Any], ...] = (
        ("raw_status_path", "generated/refresh_status.json"),
        ("raw_status_path", "public/generated/other.json"),
        ("raw_bundle_path", "public/generated/../secret.json"),
        ("raw_bundle_path", "public/generated/live_snapshot_123_" + "a" * 64 + "_" + "b" * 64 + ".json"),
        ("expected_bundle_sha256", "b" * 64),
        ("expected_bundle_bytes", 0),
        ("expected_bundle_bytes", 32 * 1024 * 1024 + 1),
        ("expected_block_number", 124),
        ("expected_block_hash", "0x" + "b" * 64),
    )
    for key, wrong in cases:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = dict(pending)
            candidate[key] = wrong
            install_pending(root, candidate)
            transport = success_transport(status, bundle)
            result = run_once(root, transport)
            assert_hard(result, "pending_proof_invalid")
            assert transport.calls == [], f"unsafe {key} reached the network"


def test_raw_status_binds_every_proof_field_and_rejects_bool_integers() -> None:
    status, bundle, pending = proof()
    base = json.loads(status)
    mutations: tuple[tuple[str, Any], ...] = (
        ("kind", "other"),
        ("live_snapshot_bundle", "other.json"),
        ("live_snapshot_bundle_sha256", "b" * 64),
        ("live_snapshot_bundle_bytes", len(bundle) + 1),
        ("live_snapshot_bundle_schema_version", 2),
        ("latest_generated_block", 124),
        ("snapshot_block_hash", "0x" + "b" * 64),
        ("schema_version", 2),
        ("latest_generated_block", True),
        ("live_snapshot_bundle_bytes", True),
    )
    overflow_status = (
        json.dumps(base, sort_keys=True, separators=(",", ":"))[:-1]
        + ',"extra":{"overflow":1e9999}}'
    ).encode()
    malformed_values = [
        b"\xff",
        b"{",
        b'{"kind":"refresh_status","kind":"refresh_status"}',
        b'{"x":NaN}',
        overflow_status,
    ]
    for label, raw in [
        *((key, (json.dumps({**base, key: wrong}) + "\n").encode()) for key, wrong in mutations),
        *(("malformed", value) for value in malformed_values),
    ]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_pending(root, pending)
            transport = success_transport(raw, bundle)
            result = run_once(root, transport)
            assert_hard(result)
            assert [call[1] for call in transport.calls] == ["raw_status"], label


def test_raw_bundle_digest_canonical_json_and_fields_are_all_enforced() -> None:
    status, bundle, pending = proof()
    base = json.loads(bundle)
    candidates: list[tuple[str, bytes, dict[str, Any]]] = []
    for key, wrong in (
        ("kind", "other"),
        ("schema_version", 2),
        ("latest_generated_block", 124),
        ("snapshot_block_hash", "0x" + "b" * 64),
        ("latest_generated_block", True),
        ("schema_version", True),
    ):
        raw = (json.dumps({**base, key: wrong}, sort_keys=True, separators=(",", ":")) + "\n").encode()
        adjusted = dict(pending)
        adjusted["expected_bundle_bytes"] = len(raw)
        adjusted["expected_bundle_sha256"] = hashlib.sha256(raw).hexdigest()
        adjusted["raw_bundle_path"] = (
            f"public/generated/live_snapshot_{adjusted['expected_block_number']}_"
            f"{adjusted['expected_block_hash'][2:]}_{adjusted['expected_bundle_sha256']}.json"
        )
        status_value = json.loads(status)
        status_value["live_snapshot_bundle"] = Path(adjusted["raw_bundle_path"]).name
        status_value["live_snapshot_bundle_bytes"] = len(raw)
        status_value["live_snapshot_bundle_sha256"] = adjusted["expected_bundle_sha256"]
        candidates.append((key, raw, adjusted | {"_status": (json.dumps(status_value) + "\n").encode()}))

    for label, raw in (
        ("malformed_utf8", b"\xff"),
        ("malformed_json", b"{"),
        ("duplicate", b'{"kind":"degen_dogs_live_snapshot","kind":"degen_dogs_live_snapshot"}\n'),
        ("nan", b'{"kind":"degen_dogs_live_snapshot","value":NaN}\n'),
        ("overflow", bundle.replace(b'"auction_feed":[]', b'"auction_feed":[1e9999]')),
        ("noncanonical", json.dumps(base, indent=2, sort_keys=True).encode() + b"\n"),
    ):
        adjusted = dict(pending)
        adjusted["expected_bundle_bytes"] = len(raw)
        adjusted["expected_bundle_sha256"] = hashlib.sha256(raw).hexdigest()
        adjusted["raw_bundle_path"] = (
            f"public/generated/live_snapshot_{adjusted['expected_block_number']}_"
            f"{adjusted['expected_block_hash'][2:]}_{adjusted['expected_bundle_sha256']}.json"
        )
        status_value = json.loads(status)
        status_value["live_snapshot_bundle"] = Path(adjusted["raw_bundle_path"]).name
        status_value["live_snapshot_bundle_bytes"] = len(raw)
        status_value["live_snapshot_bundle_sha256"] = adjusted["expected_bundle_sha256"]
        candidates.append((label, raw, adjusted | {"_status": (json.dumps(status_value) + "\n").encode()}))

    for label, raw, mixed in candidates:
        adjusted = dict(mixed)
        raw_status = adjusted.pop("_status")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_pending(root, adjusted)
            result = run_once(root, success_transport(raw_status, raw))
            assert_hard(result)
            assert state.state_paths(root).pending.exists(), label

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)
        wrong = bytearray(bundle)
        wrong[-2] ^= 1
        result = run_once(root, success_transport(status, bytes(wrong)))
        assert_hard(result, "raw_bundle_sha256_mismatch")


def test_raw_bundle_requires_the_exact_production_field_set() -> None:
    status, bundle, pending = proof()
    base = json.loads(bundle)
    candidates = []
    missing = dict(base)
    missing.pop("auction_feed")
    candidates.append(("missing", missing))
    extra = dict(base)
    extra["unexpected"] = []
    candidates.append(("extra", extra))
    for label, value in candidates:
        raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        digest = hashlib.sha256(raw).hexdigest()
        adjusted = dict(pending)
        adjusted["expected_bundle_bytes"] = len(raw)
        adjusted["expected_bundle_sha256"] = digest
        adjusted["raw_bundle_path"] = (
            f"public/generated/live_snapshot_{adjusted['expected_block_number']}_"
            f"{adjusted['expected_block_hash'][2:]}_{digest}.json"
        )
        status_value = json.loads(status)
        status_value["live_snapshot_bundle"] = Path(adjusted["raw_bundle_path"]).name
        status_value["live_snapshot_bundle_bytes"] = len(raw)
        status_value["live_snapshot_bundle_sha256"] = digest
        raw_status = (json.dumps(status_value, sort_keys=True) + "\n").encode()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_pending(root, adjusted)
            result = run_once(root, success_transport(raw_status, raw))
            assert_hard(result, "raw_bundle_mismatch")
            assert state.state_paths(root).pending.exists(), label


def replace_with_newer(root: Path, pending: dict[str, Any]) -> dict[str, Any]:
    newer = dict(pending)
    newer["generation"] += 1
    newer["queue_digest"] = "f" * 64
    newer["commit_sha"] = "b" * 40
    install_pending(root, newer)
    return newer


def test_newer_pending_at_every_network_or_finalization_boundary_is_never_touched() -> None:
    status, bundle, pending = proof()
    for boundary_call in (1, 2, 3, 4):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_pending(root, pending)
            newer_holder: list[dict[str, Any]] = []

            def hook(_resource: str, call_number: int) -> None:
                if call_number == boundary_call:
                    newer_holder.append(replace_with_newer(root, pending))

            transport = success_transport(status, bundle)
            transport.hook = hook
            rows: list[dict[str, Any]] = []
            result = run_once(root, transport, telemetry=rows)
            assert (result.exit_code, result.result) == (0, "abandoned_newer_pending")
            assert state.read_pending_with_digest(root, lock_context=FakeLock()).record == newer_holder[0]

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)
        newer = []

        def telemetry_hook(_log_dir: Path, row: dict[str, Any]) -> None:
            if row["result"] == "proof_verified":
                newer.append(replace_with_newer(root, pending))
            else:
                assert row["result"] == "abandoned_newer_pending"

        clock = FakeClock()
        result = verifier.run_once(
            root,
            root / "logs",
            state_api=PortableState(),
            transport=success_transport(status, bundle),
            telemetry_writer=telemetry_hook,
            utc_now=clock.utc_now,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            config=verifier.VerifierConfig(20.0, 5.0, 3.0),
        )
        assert result.result == "abandoned_newer_pending"
        assert state.read_pending_with_digest(root, lock_context=FakeLock()).record == newer[0]


def test_same_generation_conflict_lower_or_malformed_checkpoint_fails_closed() -> None:
    status, bundle, pending = proof()
    for mutation in ("same_commit", "same_proof", "lower", "malformed"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_pending(root, pending)

            def hook(_resource: str, call_number: int) -> None:
                if call_number != 1:
                    return
                changed = dict(pending)
                if mutation == "same_commit":
                    changed["commit_sha"] = "c" * 40
                elif mutation == "same_proof":
                    changed["expected_bundle_sha256"] = "c" * 64
                elif mutation == "lower":
                    changed["generation"] -= 1
                else:
                    changed["unexpected"] = True
                private_json(state.state_paths(root).pending, changed)

            transport = success_transport(status, bundle)
            transport.hook = hook
            result = run_once(root, transport)
            assert_hard(result, "pending_state_conflict")
            assert state.state_paths(root).pending.exists(), mutation


def test_retry_controller_is_immediate_then_not_before_and_never_deletes() -> None:
    status, bundle, pending = proof()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)
        transport = ScriptedTransport({"raw_status": [verifier.FetchError("transport_timeout")] * 20})
        result = run_once(root, transport, budget=6.0, interval=2.0)
        assert (result.exit_code, result.result) == (2, "unresolved_retry_scheduled")
        retried = state.read_pending_with_digest(root, lock_context=FakeLock()).record
        assert retried["retry_count"] == 1
        assert retried["retry_deadline_utc"] == pending["retry_deadline_utc"], "future initial deadline was not preserved"

        no_network = ScriptedTransport({})
        result = run_once(root, no_network)
        assert (result.exit_code, result.result) == (0, "retry_not_due")
        assert no_network.calls == []
        assert state.state_paths(root).pending.exists()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        due = dict(pending)
        due["retry_count"] = 4
        due["retry_deadline_utc"] = "2026-08-30T12:35:00Z"
        install_pending(root, due)
        transport = ScriptedTransport({"raw_status": [verifier.FetchError("transport_failure")] * 20})
        result = run_once(root, transport, budget=1.0, interval=1.0)
        assert result.result == "unresolved_retry_scheduled"
        advanced = state.read_pending_with_digest(root, lock_context=FakeLock()).record
        assert advanced["retry_count"] == 5
        assert advanced["retry_deadline_utc"] == "2026-08-30T12:50:01Z", "backoff was not capped at 15 minutes"
        assert state.state_paths(root).pending.exists(), "retry deadline was treated as delete authority"


def test_initial_expired_deadline_makes_no_request_and_advances_retry() -> None:
    _status, _bundle, pending = proof()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)
        transport = ScriptedTransport({})
        clock = FakeClock(dt.datetime(2026, 8, 30, 12, 46, tzinfo=dt.timezone.utc))
        result = run_once(root, transport, clock=clock)
        assert (result.exit_code, result.result) == (2, "unresolved_retry_scheduled")
        assert transport.calls == []
        retry = state.read_pending_with_digest(root, lock_context=FakeLock()).record
        assert retry["retry_count"] == 1
        assert retry["retry_deadline_utc"] == "2026-08-30T12:47:00Z"


def test_retry_deadline_cannot_starve_verification_beyond_capped_backoff() -> None:
    _status, _bundle, pending = proof()
    pending["retry_count"] = 1
    pending["retry_deadline_utc"] = "2126-08-30T12:45:00Z"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)
        transport = ScriptedTransport({})
        result = run_once(root, transport)
        assert_hard(result, "pending_state_conflict")
        assert transport.calls == []
        assert state.state_paths(root).pending.exists()


def test_future_push_timestamp_fails_before_network() -> None:
    _status, _bundle, pending = proof()
    pending["push_completed_at_utc"] = "2026-08-30T13:00:00Z"
    pending["retry_deadline_utc"] = "2026-08-30T13:10:00Z"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)
        transport = ScriptedTransport({})
        result = run_once(root, transport)
        assert_hard(result, "pending_state_conflict")
        assert transport.calls == []
        assert state.state_paths(root).pending.exists()


def test_idle_invocation_does_not_construct_remote_transport() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        original_transport = verifier.FixedHttpTransport

        def forbidden_transport() -> Any:
            raise AssertionError("idle verifier constructed remote TLS transport")

        verifier.FixedHttpTransport = forbidden_transport
        clock = FakeClock()
        try:
            result = verifier.run_once(
                root,
                root / "logs",
                state_api=PortableState(),
                transport=None,
                telemetry_writer=lambda _path, _row: None,
                utc_now=clock.utc_now,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                config=verifier.VerifierConfig(1.0, 1.0, 1.0),
            )
        finally:
            verifier.FixedHttpTransport = original_transport
        assert (result.exit_code, result.result) == (0, "idle")


def test_concurrent_retry_only_advance_is_preserved_without_hard_failure() -> None:
    _status, _bundle, pending = proof()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)

        class RetryRaceState(PortableState):
            def __init__(self) -> None:
                self.raced = False

            def cas_write_pending(
                self,
                lock_dir: Path,
                generation: int,
                commit_sha: str,
                replacement: dict[str, Any],
            ) -> bool:
                if not self.raced:
                    self.raced = True
                    current = state.read_pending_with_digest(lock_dir, lock_context=FakeLock()).record
                    concurrent = dict(current)
                    concurrent["retry_count"] = current["retry_count"] + 2
                    concurrent["retry_deadline_utc"] = "2026-08-30T13:00:00Z"
                    assert state.cas_write_pending(
                        lock_dir,
                        generation,
                        commit_sha,
                        concurrent,
                        lock_context=FakeLock(),
                    )
                return super().cas_write_pending(lock_dir, generation, commit_sha, replacement)

        transport = ScriptedTransport({"raw_status": [verifier.FetchError("transport_failure")] * 20})
        result = run_once(
            root,
            transport,
            state_api=RetryRaceState(),
            budget=1.0,
            interval=1.0,
        )
        assert (result.exit_code, result.result) == (2, "unresolved_retry_scheduled")
        durable = state.read_pending_with_digest(root, lock_context=FakeLock()).record
        assert durable["retry_count"] == 2
        assert durable["retry_deadline_utc"] == "2026-08-30T13:00:00Z"

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)
        newer_holder: list[dict[str, Any]] = []

        class NewerRaceState(PortableState):
            def cas_write_pending(
                self,
                lock_dir: Path,
                generation: int,
                commit_sha: str,
                replacement: dict[str, Any],
            ) -> bool:
                newer_holder.append(replace_with_newer(lock_dir, pending))
                return super().cas_write_pending(lock_dir, generation, commit_sha, replacement)

        transport = ScriptedTransport({"raw_status": [verifier.FetchError("transport_failure")] * 20})
        result = run_once(
            root,
            transport,
            state_api=NewerRaceState(),
            budget=1.0,
            interval=1.0,
        )
        assert (result.exit_code, result.result) == (0, "abandoned_newer_pending")
        assert state.read_pending_with_digest(root, lock_context=FakeLock()).record == newer_holder[0]


def test_raw_transport_retries_but_raw_semantic_failure_is_immediate() -> None:
    status, bundle, pending = proof()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)
        transport = success_transport(
            status,
            bundle,
            raw_status=[verifier.FetchError("transport_failure"), status],
            raw_bundle=[verifier.FetchError("transport_timeout"), bundle],
        )
        result = run_once(root, transport, budget=20.0, interval=1.0)
        assert result.result == "verified_cleared"
        assert [call[1] for call in transport.calls].count("raw_status") == 2
        assert [call[1] for call in transport.calls].count("raw_bundle") == 2

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)
        malformed = status.replace(b'"kind": "refresh_status"', b'"kind": "evil"')
        transport = success_transport(malformed, bundle, raw_status=[malformed, status])
        result = run_once(root, transport)
        assert_hard(result, "raw_status_mismatch")
        assert [call[1] for call in transport.calls] == ["raw_status"]


def test_absolute_deadline_caps_each_request_and_prevents_a_final_overrun_request() -> None:
    status, bundle, pending = proof()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)
        clock = FakeClock()

        class AdvancingTransport:
            def __init__(self) -> None:
                self.calls: list[tuple[str, float]] = []

            def fetch(
                self,
                _url: str,
                resource: str,
                _cap: int,
                timeout: float,
                *,
                absolute_deadline: float | None = None,
            ) -> bytes:
                assert absolute_deadline == 105.0
                self.calls.append((resource, timeout))
                if resource == "raw_status":
                    clock.sleep(2.5)
                    return status
                if resource == "raw_bundle":
                    assert 0 < timeout <= 2.5
                    clock.sleep(timeout)
                    return bundle
                raise AssertionError("request began after the absolute invocation deadline")

        transport = AdvancingTransport()
        result = run_once(root, transport, clock=clock, budget=5.0, interval=1.0)
        assert result.result == "unresolved_retry_scheduled"
        assert [name for name, _timeout in transport.calls] == ["raw_status", "raw_bundle"]
        assert transport.calls[0][1] == 3.0
        assert transport.calls[1][1] == 2.5
        assert clock.monotonic() == 105.0


def test_fixed_http_transport_recomputes_timeout_against_caller_absolute_deadline() -> None:
    class GapClock:
        def __init__(self) -> None:
            self.now = 100.0

        def monotonic(self) -> float:
            return self.now

    clock = GapClock()

    def delayed_cache_bust() -> int:
        clock.now = 104.8
        return 1

    opener = MemoryOpener(lambda request: MemoryResponse(b"{}", request.full_url))
    client = verifier.FixedHttpTransport(
        opener=opener,
        cache_bust_factory=delayed_cache_bust,
        monotonic=clock.monotonic,
    )
    original_deadline = verifier._absolute_request_deadline
    verifier._absolute_request_deadline = lambda _seconds: contextlib.nullcontext()
    try:
        assert client.fetch(
            fixed_pages_url(),
            "pages_status",
            20,
            5.0,
            absolute_deadline=105.0,
        ) == b"{}"
    finally:
        verifier._absolute_request_deadline = original_deadline
    assert len(opener.requests) == 1
    assert 0 < opener.requests[0][1] <= 0.21


def test_matching_task4_journal_blocks_then_next_attempt_clears() -> None:
    status, bundle, strict_pending = proof()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        queued = state.enqueue_latest_observation(
            root,
            {
                "confirmed_block_number": 123,
                "confirmed_block_hash": "0x" + "a" * 64,
                "confirmed_block_time_utc": "2026-08-30T12:34:00Z",
                "token_id": "818", "amount_wei": "1", "start_time_unix": "1", "end_time_unix": "2",
                "bidder_wallet": "0x" + "1" * 40, "settled": False,
                "event_name": None, "event_tx_hash": None, "event_log_index": None,
                "event_block_number": None, "event_block_hash": None, "event_block_time_utc": None,
                "canonical_reorg_from_hash": None,
            },
            runner_id="windows-wsl", run_scope="current", created_at_utc="2026-08-30T12:34:56Z",
            lock_context=FakeLock(),
        )
        pending = dict(strict_pending)
        pending["generation"] = queued.generation
        pending["queue_digest"] = queued.digest
        commit = pending["commit_sha"]
        journal = {
            "schema_version": 1, "repo_realpath": str(ROOT), "branch": "main", "baseline_head": "d" * 40,
            "run_id": "task5-test", "runner_id": "windows-wsl", "run_scope": "current",
            "created_at_utc": "2026-08-30T12:34:56Z", "publish_paths": ["generated", "public"],
            "alignment_runner_commit": None, "alignment_remote_head": None, "alignment_result": None,
            "publication_generation": queued.generation, "queue_digest": queued.digest,
            "terminal_outcome": "pushed", "handoff_phase": "push_ready", "remote_commit": commit,
            "raw_status_path": None, "raw_bundle_path": None, "expected_bundle_sha256": None,
            "expected_bundle_bytes": None, "expected_block_number": None, "expected_block_hash": None,
            "push_completed_at_utc": None, "retry_deadline_utc": None, "retry_count": None,
        }
        checkpoint = {
            "schema_version": 1, "outcome": "pushed", "generation": queued.generation,
            "queue_digest": queued.digest, "commit_sha": commit,
            "push_completed_at_utc": pending["push_completed_at_utc"],
        }
        paths = state.state_paths(root)
        state.atomic_write_record(paths.journal, journal)
        state.prepare_pushed_handoff(root, journal, pending, checkpoint, lock_context=FakeLock())
        first = run_once(root, success_transport(status, bundle))
        assert (first.exit_code, first.result) == (2, "verified_waiting_for_journal")
        assert paths.pending.exists()
        assert state.finalize_pushed_handoff(root, queued.generation, queued.digest, lock_context=FakeLock())
        second = run_once(root, success_transport(status, bundle))
        assert (second.exit_code, second.result) == (0, "verified_cleared")
        assert not paths.pending.exists()
        third_transport = ScriptedTransport({})
        third = run_once(root, third_transport)
        assert (third.exit_code, third.result) == (0, "idle")
        assert third_transport.calls == []


class MemoryHeaders:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class MemoryResponse:
    def __init__(
        self,
        body: bytes,
        url: str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunk: int | None = None,
    ) -> None:
        self.body = io.BytesIO(body)
        self.url = url
        self.status = status
        self.headers = MemoryHeaders(headers or {"Content-Type": "application/json", "Content-Length": str(len(body))})
        self.chunk = chunk
        self.reads = 0

    def read(self, size: int = -1) -> bytes:
        if self.chunk is not None and self.reads > 0:
            return b""
        self.reads += 1
        return self.body.read(min(size, self.chunk) if self.chunk is not None and size >= 0 else size)

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> "MemoryResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class MemoryOpener:
    def __init__(self, factory: Callable[[urllib.request.Request], MemoryResponse]) -> None:
        self.factory = factory
        self.requests: list[tuple[urllib.request.Request, float]] = []

    def open(self, request: urllib.request.Request, timeout: float) -> MemoryResponse:
        self.requests.append((request, timeout))
        return self.factory(request)


def fixed_pages_url() -> str:
    return "https://ael-dev3.github.io/Degen-Dogs-Mission-3/generated/refresh_status.json"


def test_http_client_uses_unique_cache_bust_fixed_headers_and_exact_final_url() -> None:
    seen_urls: list[str] = []

    def response(request: urllib.request.Request) -> MemoryResponse:
        seen_urls.append(request.full_url)
        return MemoryResponse(b"{}", request.full_url)

    opener = MemoryOpener(response)
    busts = iter((101, 102))
    client = verifier.FixedHttpTransport(opener=opener, cache_bust_factory=lambda: next(busts))
    assert client.fetch(fixed_pages_url(), "pages_status", 20, 2.5) == b"{}"
    assert client.fetch(fixed_pages_url(), "pages_status", 20, 2.5) == b"{}"
    assert seen_urls[0].endswith("?cache_bust=101") and seen_urls[1].endswith("?cache_bust=102")
    assert seen_urls[0] != seen_urls[1]
    headers = {key.lower(): value for key, value in opener.requests[0][0].header_items()}
    assert headers["accept"] == "application/json"
    assert headers["accept-encoding"] == "identity"
    assert headers["cache-control"] == "no-cache, no-store, max-age=0"
    assert "degen-dogs-pages-verifier" in headers["user-agent"].lower()

    redirecting = MemoryOpener(lambda request: MemoryResponse(b"{}", request.full_url + "&redirected=1"))
    client = verifier.FixedHttpTransport(opener=redirecting, cache_bust_factory=lambda: 1)
    try:
        client.fetch(fixed_pages_url(), "pages_status", 20, 1.0)
    except verifier.FetchError as exc:
        assert exc.code == "redirect_or_final_url"
    else:
        raise AssertionError("changed final URL was accepted")


def test_http_url_policy_rejects_every_origin_path_and_query_escape() -> None:
    valid = fixed_pages_url() + "?cache_bust=123"
    verifier.validate_remote_url(valid, "pages_status")
    invalid = (
        valid.replace("https://", "http://"),
        valid.replace("ael-dev3.github.io", "evil.example"),
        valid.replace("ael-dev3.github.io", "ael-dev3.github.io."),
        valid.replace("https://", "https://user:pass@"),
        valid.replace(".github.io/", ".github.io:443/"),
        valid.replace("/generated/", "/other/"),
        valid.replace("/generated/", "/generated/../"),
        valid.replace("/generated/", "/generated/%2f"),
        valid + "&extra=1",
        valid + "#fragment",
        "https://raw.githubusercontent.com/ael-dev3/Other/" + "a" * 40 + "/public/generated/refresh_status.json?cache_bust=1",
        "https://raw.githubusercontent.com/ael-dev3/Degen-Dogs-Mission-3/main/public/generated/refresh_status.json?cache_bust=1",
    )
    for url in invalid:
        try:
            verifier.validate_remote_url(url, "pages_status" if "github.io" in url else "raw_status")
        except verifier.FetchError as exc:
            assert exc.code == "url_policy"
        else:
            raise AssertionError(f"unsafe URL was accepted: {url}")


def test_http_response_policy_rejects_status_mime_encoding_length_and_overflow() -> None:
    url = fixed_pages_url()
    cases = (
        ("http_status", 204, {"Content-Type": "application/json", "Content-Length": "2"}, b"{}", None),
        ("http_status", 206, {"Content-Type": "application/json", "Content-Length": "2"}, b"{}", None),
        ("http_status", 302, {"Content-Type": "application/json", "Content-Length": "2"}, b"{}", None),
        ("http_status", 404, {"Content-Type": "application/json", "Content-Length": "2"}, b"{}", None),
        ("http_status", 429, {"Content-Type": "application/json", "Content-Length": "2"}, b"{}", None),
        ("http_status", 500, {"Content-Type": "application/json", "Content-Length": "2"}, b"{}", None),
        ("content_type", 200, {"Content-Type": "text/plain", "Content-Length": "2"}, b"{}", None),
        ("content_encoding", 200, {"Content-Type": "application/json", "Content-Encoding": "gzip", "Content-Length": "2"}, b"{}", None),
        ("content_length", 200, {"Content-Type": "application/json", "Content-Length": "-1"}, b"{}", None),
        ("content_length", 200, {"Content-Type": "application/json", "Content-Length": "999"}, b"{}", None),
        ("truncated_body", 200, {"Content-Type": "application/json", "Content-Length": "3"}, b"{}", None),
        ("response_oversize", 200, {"Content-Type": "application/json"}, b"123456", None),
        ("truncated_body", 200, {"Content-Type": "application/json", "Content-Length": "2"}, b"{}", 1),
    )
    for code, status_code, headers, body, chunk in cases:
        opener = MemoryOpener(
            lambda request, s=status_code, h=headers, b=body, c=chunk: MemoryResponse(
                b, request.full_url, status=s, headers=h, chunk=c
            )
        )
        client = verifier.FixedHttpTransport(opener=opener, cache_bust_factory=lambda: 1)
        try:
            client.fetch(url, "pages_status", 5, 1.0)
        except verifier.FetchError as exc:
            assert exc.code == code, (code, exc.code)
        else:
            raise AssertionError(f"unsafe response was accepted for {code}")


def test_http_transport_timeout_tls_and_drop_are_enumerated_without_details() -> None:
    url = fixed_pages_url()
    failures = (
        (TimeoutError("SECRET timeout detail"), "transport_timeout"),
        (verifier.ssl.SSLError("SECRET tls detail"), "tls_failure"),
        (verifier.urllib.error.URLError(verifier.ssl.SSLError("SECRET nested tls")), "tls_failure"),
        (RuntimeError("SECRET dropped response"), "transport_failure"),
    )
    for failure, expected in failures:
        class RaisingOpener:
            def open(self, _request: urllib.request.Request, timeout: float) -> Any:
                assert timeout <= 1.0
                raise failure

        client = verifier.FixedHttpTransport(opener=RaisingOpener(), cache_bust_factory=lambda: 1)
        try:
            client.fetch(url, "pages_status", 20, 1.0)
        except verifier.FetchError as exc:
            assert exc.code == expected
            assert "SECRET" not in str(exc)
        else:
            raise AssertionError(f"transport failure was accepted: {expected}")


def test_slow_drip_cannot_extend_one_request_past_its_absolute_deadline() -> None:
    class SlowDripHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"{" + (b" " * 17) + b"}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for byte in body:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    verifier.time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), SlowDripHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            client = verifier.FixedHttpTransport(
                opener=RewritingOpener(origin),
                cache_bust_factory=lambda: 1,
            )
            started = verifier.time.monotonic()
            try:
                client.fetch(fixed_pages_url(), "pages_status", 100, 0.1)
            except verifier.FetchError as exc:
                elapsed = verifier.time.monotonic() - started
                assert exc.code == "transport_timeout"
                assert elapsed < 0.75, f"absolute 0.1s request ran for {elapsed:.3f}s"
            else:
                raise AssertionError("slow-drip body bypassed the absolute request deadline")
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_production_tls_context_ignores_environment_trust_and_keylog_paths() -> None:
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        keylog = root / "attacker-keylog"
        fake_ca = root / "attacker-ca.pem"
        fake_ca.write_text("not a certificate", encoding="utf-8")
        saved = {name: os.environ.get(name) for name in ("SSLKEYLOGFILE", "SSL_CERT_FILE", "SSL_CERT_DIR")}
        os.environ["SSLKEYLOGFILE"] = str(keylog)
        os.environ["SSL_CERT_FILE"] = str(fake_ca)
        os.environ["SSL_CERT_DIR"] = str(root)
        try:
            context = verifier.build_production_tls_context()
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        assert context.verify_mode == verifier.ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert context.keylog_filename is None
        assert not keylog.exists(), "hostile SSLKEYLOGFILE caused a verifier-side write"


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, tuple[str, bytes]] = {}

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        content_type, body = self.routes[path]
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return None


class FinalUrlResponse:
    def __init__(self, response: Any, final_url: str) -> None:
        self.response = response
        self.final_url = final_url
        self.status = response.status
        self.headers = response.headers

    def read(self, size: int = -1) -> bytes:
        return self.response.read(size)

    def read1(self, size: int = -1) -> bytes:
        reader = getattr(self.response, "read1", self.response.read)
        return reader(size)

    def geturl(self) -> str:
        return self.final_url

    def __enter__(self) -> "FinalUrlResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        self.response.close()


class RewritingOpener:
    def __init__(self, local_origin: str) -> None:
        self.local_origin = local_origin

    def open(self, request: urllib.request.Request, timeout: float) -> FinalUrlResponse:
        parsed = urllib.parse.urlsplit(request.full_url)
        local = self.local_origin + parsed.path + ("?" + parsed.query if parsed.query else "")
        response = urllib.request.urlopen(local, timeout=timeout)
        return FinalUrlResponse(response, request.full_url)


def test_real_local_http_fixture_can_only_be_reached_through_injected_opener() -> None:
    status, bundle, pending = proof()
    raw_status_path = "/ael-dev3/Degen-Dogs-Mission-3/" + pending["commit_sha"] + "/" + pending["raw_status_path"]
    raw_bundle_path = "/ael-dev3/Degen-Dogs-Mission-3/" + pending["commit_sha"] + "/" + pending["raw_bundle_path"]
    pages_status_path = "/Degen-Dogs-Mission-3/generated/refresh_status.json"
    pages_bundle_path = "/Degen-Dogs-Mission-3/generated/" + Path(pending["raw_bundle_path"]).name
    FixtureHandler.routes = {
        raw_status_path: ("text/plain", status),
        raw_bundle_path: ("text/plain", bundle),
        pages_status_path: ("application/json", status),
        pages_bundle_path: ("application/json", bundle),
    }
    with socketserver.TCPServer(("127.0.0.1", 0), FixtureHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            local_origin = f"http://127.0.0.1:{server.server_address[1]}"
            clock = FakeClock()
            transport = verifier.FixedHttpTransport(
                opener=RewritingOpener(local_origin),
                cache_bust_factory=iter(range(1, 20)).__next__,
                monotonic=clock.monotonic,
            )
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                install_pending(root, pending)
                result = run_once(root, transport, clock=clock)
                assert result.result == "verified_cleared"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_private_telemetry_is_fixed_protected_and_secret_free() -> None:
    if os.name != "posix":
        return
    row = {
        "schema_version": 1,
        "timestamp_utc": "2026-08-30T12:35:00Z",
        "result": "unresolved",
        "error_code": "transport_failure",
        "generation": 7,
        "commit_sha": "a" * 40,
        "expected_block_number": 123,
        "retry_count": 1,
        "duration_seconds": 1.25,
        "raw_verified": False,
        "pages_verified": False,
    }
    with tempfile.TemporaryDirectory() as temporary:
        log_dir = Path(temporary) / "logs"
        log_dir.mkdir(mode=0o700)
        fsynced_types: list[int] = []
        original_fsync = verifier.os.fsync

        def tracked_fsync(descriptor: int) -> None:
            fsynced_types.append(stat.S_IFMT(verifier.os.fstat(descriptor).st_mode))
            original_fsync(descriptor)

        verifier.os.fsync = tracked_fsync
        try:
            verifier.append_private_telemetry(log_dir, row)
        finally:
            verifier.os.fsync = original_fsync
        path = log_dir / "pages-verifier.jsonl"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_nlink == 1
        assert stat.S_IFREG in fsynced_types and stat.S_IFDIR in fsynced_types, (
            "new telemetry row did not fsync both file and containing directory"
        )
        assert json.loads(path.read_text(encoding="utf-8")) == row
        target = log_dir / "target"
        target.write_text("secret", encoding="utf-8")
        path.unlink()
        path.symlink_to(target)
        try:
            verifier.append_private_telemetry(log_dir, row)
        except verifier.TelemetryError:
            pass
        else:
            raise AssertionError("symlinked telemetry path was accepted")


def telemetry_row() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "timestamp_utc": "2026-08-30T12:35:00Z",
        "result": "unresolved",
        "error_code": "transport_failure",
        "generation": 7,
        "commit_sha": "a" * 40,
        "expected_block_number": 123,
        "retry_count": 1,
        "duration_seconds": 1.25,
        "raw_verified": False,
        "pages_verified": False,
    }


def expect_telemetry_error(action: Callable[[], None]) -> None:
    try:
        action()
    except verifier.TelemetryError:
        return
    raise AssertionError("unsafe telemetry operation was accepted")


def test_private_telemetry_rejects_symlinked_directory_ancestor() -> None:
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / "target"
        target.mkdir(mode=0o700)
        linked = root / "linked"
        linked.symlink_to(target, target_is_directory=True)
        expect_telemetry_error(
            lambda: verifier.append_private_telemetry(linked, telemetry_row())
        )
        assert not (target / verifier.TELEMETRY_FILENAME).exists()


def test_private_telemetry_fifo_and_held_lock_fail_without_blocking() -> None:
    if os.name != "posix":
        return
    import fcntl

    with tempfile.TemporaryDirectory() as temporary:
        log_dir = Path(temporary) / "logs"
        log_dir.mkdir(mode=0o700)
        path = log_dir / verifier.TELEMETRY_FILENAME
        os.mkfifo(path, mode=0o600)
        outcome: list[BaseException | None] = []

        def append_fifo() -> None:
            try:
                verifier.append_private_telemetry(log_dir, telemetry_row())
            except BaseException as exc:
                outcome.append(exc)
            else:
                outcome.append(None)

        fifo_thread = threading.Thread(target=append_fifo, daemon=True)
        fifo_thread.start()
        fifo_thread.join(timeout=0.25)
        completed_without_reader = not fifo_thread.is_alive()
        if fifo_thread.is_alive():
            reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                fifo_thread.join(timeout=2)
            finally:
                os.close(reader)
        assert completed_without_reader, "telemetry FIFO open blocked waiting for a reader"
        assert len(outcome) == 1 and isinstance(outcome[0], verifier.TelemetryError)

        path.unlink()
        path.write_bytes(b"")
        path.chmod(0o600)
        held = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        outcome.clear()
        lock_thread = threading.Thread(target=append_fifo, daemon=True)
        lock_thread.start()
        lock_thread.join(timeout=0.25)
        completed_while_locked = not lock_thread.is_alive()
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
        lock_thread.join(timeout=2)
        assert completed_while_locked, "telemetry append blocked on a held advisory lock"
        assert len(outcome) == 1 and isinstance(outcome[0], verifier.TelemetryError)


def test_private_telemetry_detects_identity_races_through_final_fsync() -> None:
    if os.name != "posix":
        return
    for mutation_stage in ("file_fsync", "directory_fsync"):
        with tempfile.TemporaryDirectory() as temporary:
            log_dir = Path(temporary) / "logs"
            log_dir.mkdir(mode=0o700)
            path = log_dir / verifier.TELEMETRY_FILENAME
            raced = log_dir / "raced"
            original_fsync = verifier.os.fsync
            mutation_done = False

            def racing_fsync(descriptor: int) -> None:
                nonlocal mutation_done
                details = verifier.os.fstat(descriptor)
                original_fsync(descriptor)
                is_target_stage = (
                    mutation_stage == "file_fsync" and stat.S_ISREG(details.st_mode)
                ) or (
                    mutation_stage == "directory_fsync" and stat.S_ISDIR(details.st_mode)
                )
                if is_target_stage and not mutation_done:
                    mutation_done = True
                    if mutation_stage == "file_fsync":
                        os.link(path, raced)
                    else:
                        path.rename(raced)
                        path.write_bytes(b"attacker")
                        path.chmod(0o600)

            verifier.os.fsync = racing_fsync
            try:
                expect_telemetry_error(
                    lambda: verifier.append_private_telemetry(log_dir, telemetry_row())
                )
            finally:
                verifier.os.fsync = original_fsync
            assert mutation_done, f"{mutation_stage} race hook did not execute"


def test_private_telemetry_detects_prewrite_link_and_path_replacement_races() -> None:
    if os.name != "posix":
        return
    for mutation in ("hardlink", "replace"):
        with tempfile.TemporaryDirectory() as temporary:
            log_dir = Path(temporary) / "logs"
            log_dir.mkdir(mode=0o700)
            path = log_dir / verifier.TELEMETRY_FILENAME
            path.write_bytes(b"")
            path.chmod(0o600)
            raced = log_dir / "raced"
            original_validate = verifier._validate_log_identity
            validation_count = 0

            def racing_validate(directory_fd: int, descriptor: int) -> os.stat_result:
                nonlocal validation_count
                validation_count += 1
                details = original_validate(directory_fd, descriptor)
                if validation_count == 1:
                    if mutation == "hardlink":
                        os.link(path, raced)
                    else:
                        path.rename(raced)
                        path.write_bytes(b"attacker")
                        path.chmod(0o600)
                return details

            verifier._validate_log_identity = racing_validate
            try:
                expect_telemetry_error(
                    lambda: verifier.append_private_telemetry(log_dir, telemetry_row())
                )
            finally:
                verifier._validate_log_identity = original_validate
            assert validation_count >= 2


def test_posix_absolute_deadline_rejects_worker_thread_and_preserves_earlier_alarm() -> None:
    if os.name != "posix":
        return
    import signal

    opened: list[bool] = []

    class UnexpectedOpener:
        def open(self, *_args: Any, **_kwargs: Any) -> Any:
            opened.append(True)
            raise AssertionError("worker thread reached network opener")

    transport = verifier.FixedHttpTransport(opener=UnexpectedOpener())
    outcome: list[BaseException | None] = []

    def worker() -> None:
        try:
            transport.fetch(
                "https://ael-dev3.github.io/Degen-Dogs-Mission-3/generated/refresh_status.json",
                "pages_status",
                1024,
                1.0,
            )
        except BaseException as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert not opened
    assert (
        len(outcome) == 1
        and isinstance(outcome[0], verifier.FetchError)
        and outcome[0].code == "transport_timeout"
    )

    prior_handler = signal.getsignal(signal.SIGALRM)
    prior_timer = signal.getitimer(signal.ITIMER_REAL)
    prior_events: list[float] = []
    started = time.monotonic()
    try:
        signal.signal(signal.SIGALRM, lambda _signum, _frame: prior_events.append(time.monotonic()))
        signal.setitimer(signal.ITIMER_REAL, 0.08)
        try:
            with verifier._absolute_request_deadline(1.0):
                pass
        except verifier._RequestDeadlineExpired:
            pass
        else:
            raise AssertionError("request deadline accepted an already-armed process alarm")
        assert time.monotonic() - started < 0.05
        time.sleep(0.12)
        assert prior_events, "request deadline consumed the caller's earlier alarm"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, prior_handler)
        if prior_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, prior_timer[0], prior_timer[1])


def test_production_directories_are_pinned_and_never_open_the_checkout() -> None:
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repo = root / "repo"
        lock_dir = root / "lock"
        log_dir = root / "log"
        for directory, mode in ((repo, 0o755), (lock_dir, 0o700), (log_dir, 0o700)):
            directory.mkdir(mode=mode)
            directory.chmod(mode)
        names = ("DEGEN_DOGS_REPO_DIR", "DEGEN_DOGS_LOCK_DIR", "DEGEN_DOGS_LOG_DIR")
        saved = {name: os.environ.get(name) for name in names}
        saved_constants = (
            verifier.PRODUCTION_REPO_DIR,
            verifier.PRODUCTION_LOCK_DIR,
            verifier.PRODUCTION_LOG_DIR,
        )
        try:
            verifier.PRODUCTION_REPO_DIR = repo
            verifier.PRODUCTION_LOCK_DIR = lock_dir
            verifier.PRODUCTION_LOG_DIR = log_dir
            os.environ.update({
                # The detached verifier must not consult or open this checkout
                # value.  It is deliberately invalid and nonexistent.
                "DEGEN_DOGS_REPO_DIR": str(root / "missing-checkout"),
                "DEGEN_DOGS_LOCK_DIR": str(lock_dir),
                "DEGEN_DOGS_LOG_DIR": str(log_dir),
            })
            assert verifier._production_directories() == (lock_dir, log_dir)

            arbitrary = root / "arbitrary"
            arbitrary.mkdir(mode=0o700)
            os.environ["DEGEN_DOGS_LOG_DIR"] = str(arbitrary)
            try:
                verifier._production_directories()
            except ValueError:
                pass
            else:
                raise AssertionError("arbitrary environment-selected telemetry directory was accepted")

            checkout_log = repo / "logs"
            checkout_log.mkdir(mode=0o700)
            verifier.PRODUCTION_LOG_DIR = checkout_log
            os.environ["DEGEN_DOGS_LOG_DIR"] = str(checkout_log)
            try:
                verifier._production_directories()
            except ValueError:
                pass
            else:
                raise AssertionError("repository-contained telemetry directory was accepted")

            outside = root / "outside"
            outside.mkdir(mode=0o700)
            linked = root / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            verifier.PRODUCTION_LOG_DIR = linked
            os.environ["DEGEN_DOGS_LOG_DIR"] = str(linked)
            try:
                verifier._production_directories()
            except ValueError:
                pass
            else:
                raise AssertionError("symlinked production directory ancestor was accepted")
        finally:
            (
                verifier.PRODUCTION_REPO_DIR,
                verifier.PRODUCTION_LOCK_DIR,
                verifier.PRODUCTION_LOG_DIR,
            ) = saved_constants
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def test_secret_bearing_failures_never_reach_summary_or_telemetry() -> None:
    _status, _bundle, pending = proof()
    secret = "SECRET_TOKEN_user@example.test/private/path"

    class LeakingTransport:
        def fetch(self, *_args: Any, **_kwargs: Any) -> bytes:
            raise RuntimeError(secret)

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        install_pending(root, pending)
        rows: list[dict[str, Any]] = []
        result = run_once(root, LeakingTransport(), telemetry=rows, budget=1.0, interval=1.0)
        serialized = json.dumps({"result": result.__dict__, "rows": rows}, sort_keys=True)
        assert secret not in serialized
        assert "example.test" not in serialized and "private/path" not in serialized
        assert result.error_code in {None, "transport_failure"}


def test_telemetry_durability_failure_retains_pending_before_retry_or_clear() -> None:
    status, bundle, pending = proof()
    for transport in (
        success_transport(status, bundle),
        ScriptedTransport({"raw_status": [verifier.FetchError("transport_failure")] * 20}),
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_pending(root, pending)
            clock = FakeClock()
            result = verifier.run_once(
                root,
                root / "logs",
                state_api=PortableState(),
                transport=transport,
                telemetry_writer=lambda _path, _row: (_ for _ in ()).throw(OSError("SECRET write failure")),
                utc_now=clock.utc_now,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                config=verifier.VerifierConfig(1.0, 1.0, 1.0),
            )
            assert (result.exit_code, result.error_code) == (1, "telemetry_write_failed")
            durable = state.read_pending_with_digest(root, lock_context=FakeLock()).record
            assert durable["retry_count"] == 0
            assert not state.state_paths(root).pages_verified.exists()


def test_source_has_no_process_checkout_or_dynamic_authority_surface() -> None:
    import ast

    path = ROOT / "scripts" / "verify_pages_deployment.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not ({"subprocess", "fcntl"} & imported), "forbidden process/POSIX module imported at module load"
    lowered = source.lower()
    for forbidden in ("git ", "npm", "npx", "node ", "refresh_and_publish", "build_live_snapshot_bundle"):
        assert forbidden not in lowered
    parser_options = verifier.cli_option_strings()
    for forbidden in ("--url", "--host", "--repo", "--commit", "--path", "--command", "--output"):
        assert forbidden not in parser_options


def test_production_cli_fails_closed_off_posix_without_network() -> None:
    if os.name == "posix":
        return
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = verifier.main([])
    assert exit_code == verifier.CONFIG_EXIT
    assert "http" not in stdout.getvalue().lower() + stderr.getvalue().lower()


def test_cli_parse_errors_never_echo_secret_bearing_arguments() -> None:
    secret = "SECRET_TOKEN_user@example.test/private/path"
    for arguments in (("--budget-seconds", secret), (secret,)):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = verifier.main(list(arguments))
        combined = stdout.getvalue() + stderr.getvalue()
        assert exit_code == verifier.CONFIG_EXIT
        assert secret not in combined and "example.test" not in combined and "private/path" not in combined
        assert stderr.getvalue() == ""
        summary = json.loads(stdout.getvalue())
        assert summary == {
            "schema_version": 1,
            "result": "configuration_error",
            "error_code": "configuration_invalid",
        }


def test_missing_production_ca_is_distinct_configuration_failure() -> None:
    if os.name != "posix":
        return
    _status, _bundle, pending = proof()
    pending["retry_count"] = 1
    pending["retry_deadline_utc"] = "2026-08-31T00:00:00Z"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repo = root / "repo"
        lock_dir = root / "lock"
        log_dir = root / "log"
        repo.mkdir(mode=0o755)
        lock_dir.mkdir(mode=0o700)
        log_dir.mkdir(mode=0o700)
        install_pending(lock_dir, pending)
        names = ("DEGEN_DOGS_REPO_DIR", "DEGEN_DOGS_LOCK_DIR", "DEGEN_DOGS_LOG_DIR")
        saved_environment = {name: os.environ.get(name) for name in names}
        saved_constants = (
            verifier.PRODUCTION_REPO_DIR,
            verifier.PRODUCTION_LOCK_DIR,
            verifier.PRODUCTION_LOG_DIR,
            verifier.SYSTEM_CA_BUNDLE,
        )
        try:
            verifier.PRODUCTION_REPO_DIR = repo
            verifier.PRODUCTION_LOCK_DIR = lock_dir
            verifier.PRODUCTION_LOG_DIR = log_dir
            verifier.SYSTEM_CA_BUNDLE = root / "missing-ca.pem"
            os.environ.update({
                "DEGEN_DOGS_REPO_DIR": str(repo),
                "DEGEN_DOGS_LOCK_DIR": str(lock_dir),
                "DEGEN_DOGS_LOG_DIR": str(log_dir),
            })
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = verifier.main(["--budget-seconds", "1"])
            assert exit_code == verifier.CONFIG_EXIT
            assert stderr.getvalue() == ""
            assert json.loads(stdout.getvalue()) == {
                "schema_version": 1,
                "result": "configuration_error",
                "error_code": "configuration_invalid",
            }
            assert state.state_paths(lock_dir).pending.exists()
        finally:
            (
                verifier.PRODUCTION_REPO_DIR,
                verifier.PRODUCTION_LOCK_DIR,
                verifier.PRODUCTION_LOG_DIR,
                verifier.SYSTEM_CA_BUNDLE,
            ) = saved_constants
            for name, value in saved_environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"verify_pages_deployment_tests=pass count={len(tests)}")
