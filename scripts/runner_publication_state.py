#!/usr/bin/env python3
"""Private, durable state primitives for the WSL latest-wins publisher.

This module deliberately contains no publishing, networking, or subprocess
logic.  It owns only authenticated records below ``LOCK_DIR/publication`` and
the exact compare-and-swap transitions consumed by the watcher and drainer.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import errno
import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any, Callable, Iterator


SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 64 * 1024
_HEX_64 = re.compile(r"0x[0-9a-f]{64}\Z")
_SHA_40 = re.compile(r"[0-9a-f]{40}\Z")
_ADDRESS = re.compile(r"0x[0-9a-f]{40}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_UTC_Z = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


class StateValidationError(RuntimeError):
    """A private state artifact is malformed, unsafe, or internally inconsistent."""


@dataclasses.dataclass(frozen=True)
class StatePaths:
    root: Path
    publication: Path
    latest: Path
    pending: Path
    checkpoint: Path
    sequence: Path
    journal: Path
    lock: Path


@dataclasses.dataclass(frozen=True)
class EnqueueResult:
    action: str
    generation: int
    digest: str
    record: dict[str, Any]


def state_paths(lock_dir: os.PathLike[str] | str) -> StatePaths:
    # ``resolve()`` would silently follow an attacker-controlled lock-dir link.
    root = Path(os.path.abspath(os.fspath(lock_dir)))
    publication = root / "publication"
    return StatePaths(
        root=root,
        publication=publication,
        latest=publication / "latest.json",
        pending=publication / "pending.json",
        checkpoint=publication / "pushed.json",
        sequence=publication / "generation.json",
        journal=root / "publisher-recovery.json",
        lock=publication / "state.lock",
    )


def _owner_uid() -> int | None:
    return os.getuid() if hasattr(os, "getuid") else None


def _requires_posix_metadata() -> bool:
    """Windows Python cannot faithfully report POSIX mode/owner metadata.

    The production entrypoint is WSL-only; this branch exists solely so the
    schema/transition tests remain runnable with native Windows Python.
    """
    return os.name == "posix"


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise StateValidationError(f"private state directory is not a real directory: {path}")
    owner = _owner_uid()
    if _requires_posix_metadata() and owner is not None and details.st_uid != owner:
        raise StateValidationError(f"private state directory is owned by another user: {path}")
    if _requires_posix_metadata() and stat.S_IMODE(details.st_mode) != 0o700:
        os.chmod(path, 0o700)


def _validate_private_file(path: Path, *, max_bytes: int = MAX_RECORD_BYTES) -> os.stat_result:
    try:
        details = path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise StateValidationError(f"private state record is not a regular file: {path}")
    owner = _owner_uid()
    if _requires_posix_metadata() and owner is not None and details.st_uid != owner:
        raise StateValidationError(f"private state record is owned by another user: {path}")
    if _requires_posix_metadata() and stat.S_IMODE(details.st_mode) != 0o600:
        raise StateValidationError(f"private state record mode is not 0600: {path}")
    if _requires_posix_metadata() and details.st_nlink != 1:
        raise StateValidationError(f"private state record has unexpected link count: {path}")
    if details.st_size > max_bytes:
        raise StateValidationError(f"private state record exceeds size limit: {path}")
    return details


def _canonical_bytes(record: dict[str, Any]) -> bytes:
    try:
        data = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise StateValidationError("state record cannot be represented as canonical JSON") from exc
    if len(data) > MAX_RECORD_BYTES:
        raise StateValidationError("state record exceeds size limit")
    return data


def _digest(record: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(record)).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    before = _validate_private_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise StateValidationError(f"private state record changed during open: {path}")
        raw = os.read(descriptor, MAX_RECORD_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_RECORD_BYTES:
        raise StateValidationError(f"private state record exceeds size limit: {path}")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateValidationError(f"private state record is not valid JSON: {path}") from exc
    if not isinstance(decoded, dict):
        raise StateValidationError(f"private state record is not a JSON object: {path}")
    return decoded


def atomic_write_record(path: os.PathLike[str] | str, record: dict[str, Any]) -> None:
    """Durably replace one fixed private record (file fsync then parent fsync)."""
    target = Path(path)
    _ensure_private_directory(target.parent)
    data = _canonical_bytes(record)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(16)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise StateValidationError("atomic state record write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, target)
        _validate_private_file(target)
        if _requires_posix_metadata():
            installed = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(installed)
            finally:
                os.close(installed)
        if _requires_posix_metadata():
            parent = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _unlink_record(path: Path) -> bool:
    try:
        _validate_private_file(path)
    except FileNotFoundError:
        return False
    path.unlink()
    if _requires_posix_metadata():
        parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    return True


@contextlib.contextmanager
def production_lock(path: os.PathLike[str] | str, *, nonblocking: bool = False) -> Iterator[None]:
    """Acquire a real POSIX flock on the one fixed private state lock file."""
    if os.name != "posix":
        raise StateValidationError("production publication locking requires POSIX fcntl")
    import fcntl

    lock_path = Path(path)
    _ensure_private_directory(lock_path.parent)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        _validate_private_file(lock_path)
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(descriptor, flags)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise StateValidationError("publication state lock is already held") from exc
            raise
        yield
    finally:
        os.close(descriptor)


def _lock(paths: StatePaths, lock_context: Any | None) -> Any:
    _ensure_private_directory(paths.root)
    _ensure_private_directory(paths.publication)
    return production_lock(paths.lock) if lock_context is None else lock_context


def _require_exact_keys(value: dict[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise StateValidationError(f"{label} has an invalid JSON shape")


def _utc(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _UTC_Z.fullmatch(value):
        raise StateValidationError(f"{label} must be a UTC Z timestamp")
    try:
        dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise StateValidationError(f"{label} is not a valid UTC timestamp") from exc
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StateValidationError(f"{label} is invalid")
    return value


def _decimal(value: Any, label: str, *, positive: bool = False) -> str:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value) or (positive and value == "0"):
        raise StateValidationError(f"{label} is not canonical decimal")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX_64.fullmatch(value):
        raise StateValidationError(f"{label} is not a canonical block hash")
    return value


def validate_observation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StateValidationError("observation must be an object")
    keys = {
        "confirmed_block_number", "confirmed_block_hash", "confirmed_block_time_utc",
        "token_id", "amount_wei", "start_time_unix", "end_time_unix", "bidder_wallet",
        "settled", "event_name", "event_tx_hash", "event_log_index", "event_block_number",
        "event_block_hash", "event_block_time_utc", "canonical_reorg_from_hash",
    }
    _require_exact_keys(value, keys, "observation")
    _integer(value["confirmed_block_number"], "confirmed_block_number", minimum=1)
    _hash(value["confirmed_block_hash"], "confirmed_block_hash")
    _utc(value["confirmed_block_time_utc"], "confirmed_block_time_utc")
    _decimal(value["token_id"], "token_id", positive=True)
    _decimal(value["amount_wei"], "amount_wei")
    _decimal(value["start_time_unix"], "start_time_unix", positive=True)
    _decimal(value["end_time_unix"], "end_time_unix", positive=True)
    if not isinstance(value["bidder_wallet"], str) or not _ADDRESS.fullmatch(value["bidder_wallet"]):
        raise StateValidationError("bidder_wallet is invalid")
    if not isinstance(value["settled"], bool):
        raise StateValidationError("settled is invalid")
    event_values = [value[name] for name in ("event_name", "event_tx_hash", "event_log_index", "event_block_number", "event_block_hash", "event_block_time_utc")]
    if any(item is None for item in event_values):
        if any(item is not None for item in event_values):
            raise StateValidationError("state-only observation must set every event field to null")
    else:
        if not isinstance(value["event_name"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value["event_name"]):
            raise StateValidationError("event_name is invalid")
        _hash(value["event_tx_hash"], "event_tx_hash")
        _integer(value["event_log_index"], "event_log_index")
        _integer(value["event_block_number"], "event_block_number", minimum=1)
        _hash(value["event_block_hash"], "event_block_hash")
        _utc(value["event_block_time_utc"], "event_block_time_utc")
    reorg = value["canonical_reorg_from_hash"]
    if reorg is not None:
        _hash(reorg, "canonical_reorg_from_hash")
    return value


def latest_record(generation: int, runner_id: str, run_scope: str, created_at_utc: str, observation: dict[str, Any]) -> dict[str, Any]:
    _integer(generation, "generation", minimum=1)
    if not isinstance(runner_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", runner_id):
        raise StateValidationError("runner_id is invalid")
    if run_scope != "current":
        raise StateValidationError("run_scope must be current for the Windows queue")
    _utc(created_at_utc, "created_at_utc")
    validate_observation(observation)
    return {"schema_version": SCHEMA_VERSION, "generation": generation, "created_at_utc": created_at_utc, "runner_id": runner_id, "run_scope": run_scope, "observation": observation}


def validate_latest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StateValidationError("latest record must be an object")
    _require_exact_keys(value, {"schema_version", "generation", "created_at_utc", "runner_id", "run_scope", "observation"}, "latest record")
    if value["schema_version"] != SCHEMA_VERSION:
        raise StateValidationError("latest record schema version is invalid")
    return latest_record(value["generation"], value["runner_id"], value["run_scope"], value["created_at_utc"], value["observation"])


def read_latest_with_digest(lock_dir: os.PathLike[str] | str) -> tuple[dict[str, Any], str] | None:
    paths = state_paths(lock_dir)
    _ensure_private_directory(paths.root)
    _ensure_private_directory(paths.publication)
    path = paths.latest
    try:
        record = _read_json(path)
    except FileNotFoundError:
        return None
    return validate_latest(record), _digest(record)


def _read_generation_watermark(path: Path) -> int:
    try:
        record = _read_json(path)
    except FileNotFoundError:
        return 0
    _require_exact_keys(record, {"schema_version", "last_generation"}, "generation watermark")
    if record["schema_version"] != SCHEMA_VERSION:
        raise StateValidationError("generation watermark schema version is invalid")
    return _integer(record["last_generation"], "generation watermark", minimum=0)


def _advance_generation(paths: StatePaths, current_generation: int) -> int:
    """Durably reserve the next monotonic generation before replacing latest."""
    generation = max(_read_generation_watermark(paths.sequence), current_generation) + 1
    atomic_write_record(paths.sequence, {"schema_version": SCHEMA_VERSION, "last_generation": generation})
    return generation


def enqueue_latest_observation(
    lock_dir: os.PathLike[str] | str,
    observation: dict[str, Any],
    *,
    runner_id: str,
    run_scope: str,
    created_at_utc: str,
    canonical_reorg_quorum: bool = False,
    lock_context: Any | None = None,
) -> EnqueueResult:
    """Persist one quorum-validated observation using latest-wins chain ordering."""
    paths = state_paths(lock_dir)
    validate_observation(observation)
    with _lock(paths, lock_context):
        current = read_latest_with_digest(lock_dir)
        if current is None:
            record = latest_record(_advance_generation(paths, 0), runner_id, run_scope, created_at_utc, observation)
            digest = _digest(record)
            atomic_write_record(paths.latest, record)
            return EnqueueResult("enqueued", record["generation"], digest, record)
        old, old_digest = current
        old_observation = old["observation"]
        if observation == old_observation:
            return EnqueueResult("coalesced", old["generation"], old_digest, old)
        old_block = old_observation["confirmed_block_number"]
        new_block = observation["confirmed_block_number"]
        if new_block < old_block:
            return EnqueueResult("stale", old["generation"], old_digest, old)
        if new_block == old_block:
            if observation["confirmed_block_hash"] == old_observation["confirmed_block_hash"]:
                raise StateValidationError("same-height observation changed without a canonical block change")
            if not canonical_reorg_quorum or observation["canonical_reorg_from_hash"] != old_observation["confirmed_block_hash"]:
                raise StateValidationError("same-height block hash requires an explicit quorum canonical reorg transition")
            action = "reorg_replaced"
        else:
            action = "replaced"
        record = latest_record(_advance_generation(paths, old["generation"]), runner_id, run_scope, created_at_utc, observation)
        digest = _digest(record)
        atomic_write_record(paths.latest, record)
        return EnqueueResult(action, record["generation"], digest, record)


def cas_clear_latest(lock_dir: os.PathLike[str] | str, generation: int, digest: str, *, lock_context: Any | None = None) -> bool:
    paths = state_paths(lock_dir)
    with _lock(paths, lock_context):
        current = read_latest_with_digest(lock_dir)
        if current is None or current[0]["generation"] != generation or current[1] != digest:
            return False
        return _unlink_record(paths.latest)


def cas_write_pending(
    lock_dir: os.PathLike[str] | str,
    expected_generation: int,
    expected_commit_sha: str,
    replacement: dict[str, Any],
    *,
    lock_context: Any | None = None,
) -> bool:
    """Advance retry state only if the authenticated immutable proof still matches."""
    paths = state_paths(lock_dir)
    replacement = _validate_pending(replacement)
    with _lock(paths, lock_context):
        try:
            current = _validate_pending(_read_json(paths.pending))
        except FileNotFoundError:
            return False
        if current["generation"] != expected_generation or current["commit_sha"] != expected_commit_sha:
            return False
        if not _same_pending_immutable(current, replacement):
            raise StateValidationError("pending replacement changes immutable publication proof")
        if (
            replacement["retry_count"] < current["retry_count"]
            or replacement["retry_deadline_utc"] < current["retry_deadline_utc"]
        ):
            raise StateValidationError("pending replacement regresses retry state")
        atomic_write_record(paths.pending, replacement)
        return True


def cas_clear_pending(
    lock_dir: os.PathLike[str] | str,
    generation: int,
    commit_sha: str,
    *,
    lock_context: Any | None = None,
) -> bool:
    """Clear pending only for the exact generation and immutable commit it names."""
    paths = state_paths(lock_dir)
    with _lock(paths, lock_context):
        try:
            current = _validate_pending(_read_json(paths.pending))
        except FileNotFoundError:
            return False
        if current["generation"] != generation or current["commit_sha"] != commit_sha:
            return False
        return _unlink_record(paths.pending)


_JOURNAL_BASE_KEYS = {
    "schema_version", "repo_realpath", "branch", "baseline_head", "run_id", "runner_id",
    "run_scope", "created_at_utc", "publish_paths",
}
_JOURNAL_ALIGNMENT_KEYS = {"alignment_runner_commit", "alignment_remote_head", "alignment_result"}
_JOURNAL_PROOF_KEYS = {
    "raw_status_path", "raw_bundle_path", "expected_bundle_sha256", "expected_bundle_bytes",
    "expected_block_number", "expected_block_hash", "push_completed_at_utc", "retry_deadline_utc",
    "retry_count",
}
_JOURNAL_DEFERRED_KEYS = _JOURNAL_BASE_KEYS | _JOURNAL_ALIGNMENT_KEYS | _JOURNAL_PROOF_KEYS | {
    "publication_generation", "queue_digest", "terminal_outcome", "handoff_phase", "remote_commit",
}
_PENDING_KEYS = {
    "schema_version", "generation", "queue_digest", "commit_sha", "raw_status_path", "raw_bundle_path",
    "expected_bundle_sha256", "expected_bundle_bytes", "expected_block_number", "expected_block_hash",
    "push_completed_at_utc", "retry_deadline_utc", "retry_count",
}
_PENDING_MUTABLE_KEYS = {"retry_deadline_utc", "retry_count"}
_PENDING_IMMUTABLE_KEYS = _PENDING_KEYS - _PENDING_MUTABLE_KEYS


def _validate_journal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise StateValidationError("recovery journal schema is invalid")
    _require_exact_keys(value, _JOURNAL_DEFERRED_KEYS, "recovery journal")
    if not isinstance(value["repo_realpath"], str) or not os.path.isabs(value["repo_realpath"]):
        raise StateValidationError("journal repository path is invalid")
    if not isinstance(value["branch"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", value["branch"]):
        raise StateValidationError("journal branch is invalid")
    if not isinstance(value["baseline_head"], str) or not _SHA_40.fullmatch(value["baseline_head"]):
        raise StateValidationError("journal baseline head is invalid")
    if not isinstance(value["run_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value["run_id"]):
        raise StateValidationError("journal run ID is invalid")
    if not isinstance(value["runner_id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value["runner_id"]):
        raise StateValidationError("journal runner ID is invalid")
    if value["run_scope"] not in {"current", "full", "archive", "archive_full"}:
        raise StateValidationError("journal run scope is invalid")
    _utc(value["created_at_utc"], "journal creation time")
    if not isinstance(value["publish_paths"], list) or not value["publish_paths"] or len(value["publish_paths"]) > 32:
        raise StateValidationError("journal publish paths are invalid")
    for path in value["publish_paths"]:
        if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", path):
            raise StateValidationError("journal publish path is invalid")
    _integer(value["publication_generation"], "journal generation", minimum=1)
    if not isinstance(value["queue_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["queue_digest"]):
        raise StateValidationError("journal queue digest is invalid")
    alignment = (
        value["alignment_runner_commit"],
        value["alignment_remote_head"],
        value["alignment_result"],
    )
    if alignment != (None, None, None):
        if (
            not isinstance(alignment[0], str)
            or not _SHA_40.fullmatch(alignment[0])
            or not isinstance(alignment[1], str)
            or not _SHA_40.fullmatch(alignment[1])
            or alignment[2] not in {"peer_supersedes", "regenerate"}
        ):
            raise StateValidationError("journal alignment state is invalid")
    if value["terminal_outcome"] not in {None, "pushed", "no_diff", "peer_superseded"}:
        raise StateValidationError("journal terminal outcome is invalid")
    if value["handoff_phase"] not in {"generating", "push_ready", "raw_proven", "terminal"}:
        raise StateValidationError("journal handoff phase is invalid")
    if value["remote_commit"] is not None and (
        not isinstance(value["remote_commit"], str) or not _SHA_40.fullmatch(value["remote_commit"])
    ):
        raise StateValidationError("journal remote commit is invalid")
    proof_values = tuple(value[key] for key in _JOURNAL_PROOF_KEYS)
    proof_is_empty = all(item is None for item in proof_values)
    phase = value["handoff_phase"]
    outcome = value["terminal_outcome"]
    remote_commit = value["remote_commit"]
    if phase == "generating":
        if outcome is not None or remote_commit is not None or not proof_is_empty:
            raise StateValidationError("generating journal contains terminal handoff evidence")
    elif phase == "push_ready":
        if outcome != "pushed" or remote_commit is None or not proof_is_empty:
            raise StateValidationError("push-ready journal is incomplete or claims raw proof")
    elif phase == "raw_proven":
        if outcome != "pushed" or remote_commit is None or proof_is_empty:
            raise StateValidationError("raw-proven journal lacks exact immutable proof")
        pending = {
            "schema_version": SCHEMA_VERSION,
            "generation": value["publication_generation"],
            "queue_digest": value["queue_digest"],
            "commit_sha": remote_commit,
            **{key: value[key] for key in _JOURNAL_PROOF_KEYS},
        }
        _validate_pending(pending)
    else:
        if outcome not in {"no_diff", "peer_superseded"} or not proof_is_empty:
            raise StateValidationError("terminal journal outcome is incomplete or contains push proof")
        if outcome == "no_diff" and remote_commit is not None:
            raise StateValidationError("no-diff journal must not invent a remote commit")
        if outcome == "peer_superseded" and remote_commit is None:
            raise StateValidationError("peer-superseded journal lacks the peer commit")
    return value


def _validate_pending(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StateValidationError("pending record must be an object")
    _require_exact_keys(value, _PENDING_KEYS, "pending record")
    if value["schema_version"] != SCHEMA_VERSION:
        raise StateValidationError("pending record schema version is invalid")
    _integer(value["generation"], "pending generation", minimum=1)
    if not isinstance(value["queue_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["queue_digest"]):
        raise StateValidationError("pending queue digest is invalid")
    if not isinstance(value["commit_sha"], str) or not _SHA_40.fullmatch(value["commit_sha"]):
        raise StateValidationError("pending commit is invalid")
    for key in ("raw_status_path", "raw_bundle_path"):
        if not isinstance(value[key], str) or not re.fullmatch(r"(?:public/generated/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value[key]):
            raise StateValidationError(f"pending {key} is invalid")
    if not isinstance(value["expected_bundle_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["expected_bundle_sha256"]):
        raise StateValidationError("pending bundle digest is invalid")
    _integer(value["expected_bundle_bytes"], "pending bundle bytes")
    _integer(value["expected_block_number"], "pending block number", minimum=1)
    _hash(value["expected_block_hash"], "pending block hash")
    _utc(value["push_completed_at_utc"], "pending push completion")
    _utc(value["retry_deadline_utc"], "pending retry deadline")
    _integer(value["retry_count"], "pending retry count")
    return value


def _same_pending_immutable(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left[key] == right[key] for key in _PENDING_IMMUTABLE_KEYS)


def _validate_checkpoint(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StateValidationError("checkpoint record must be an object")
    _require_exact_keys(value, {"schema_version", "outcome", "generation", "queue_digest", "commit_sha", "push_completed_at_utc"}, "checkpoint record")
    if value["schema_version"] != SCHEMA_VERSION or value["outcome"] not in {"pushed", "no_diff", "peer_superseded"}:
        raise StateValidationError("checkpoint outcome is invalid")
    _integer(value["generation"], "checkpoint generation", minimum=1)
    if not isinstance(value["queue_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["queue_digest"]):
        raise StateValidationError("checkpoint queue digest is invalid")
    if value["commit_sha"] is not None and (not isinstance(value["commit_sha"], str) or not _SHA_40.fullmatch(value["commit_sha"])):
        raise StateValidationError("checkpoint commit is invalid")
    if value["outcome"] == "pushed":
        if value["commit_sha"] is None or value["push_completed_at_utc"] is None:
            raise StateValidationError("pushed checkpoint lacks immutable push proof")
    elif value["outcome"] == "no_diff":
        if value["push_completed_at_utc"] is not None:
            raise StateValidationError("no-diff checkpoint must not invent a push completion time")
    else:
        if value["commit_sha"] is None or value["push_completed_at_utc"] is not None:
            raise StateValidationError("peer-superseded checkpoint requires peer commit and no local push time")
    if value["push_completed_at_utc"] is not None:
        _utc(value["push_completed_at_utc"], "checkpoint push completion")
    return value


def _same_identity(journal: dict[str, Any], record: dict[str, Any], label: str) -> None:
    if journal["publication_generation"] != record["generation"] or journal["queue_digest"] != record["queue_digest"]:
        raise StateValidationError(f"{label} identity differs from recovery journal")
    if record.get("commit_sha") not in {None, journal["remote_commit"]}:
        raise StateValidationError(f"{label} immutable commit differs from recovery journal")


def _same_journal_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = _JOURNAL_BASE_KEYS | {"publication_generation", "queue_digest"}
    return all(left[key] == right[key] for key in keys)


def read_deferred_recovery_journal(lock_dir: os.PathLike[str] | str) -> dict[str, Any] | None:
    """Read only the one fixed deferred journal below ``lock_dir``."""
    path = state_paths(lock_dir).journal
    try:
        return _validate_journal(_read_json(path))
    except FileNotFoundError:
        return None


def create_deferred_recovery_journal(
    lock_dir: os.PathLike[str] | str,
    journal: dict[str, Any],
    *,
    lock_context: Any | None = None,
) -> None:
    """Create the authenticated pre-generation journal at its fixed path."""
    paths = state_paths(lock_dir)
    journal = _validate_journal(journal)
    if journal["handoff_phase"] != "generating":
        raise StateValidationError("new deferred recovery journal must begin in generating phase")
    with _lock(paths, lock_context):
        try:
            _read_json(paths.journal)
        except FileNotFoundError:
            atomic_write_record(paths.journal, journal)
            return
        raise StateValidationError("deferred recovery journal already exists")


def arm_deferred_pushed_handoff(
    lock_dir: os.PathLike[str] | str,
    generation: int,
    digest: str,
    commit_sha: str,
    *,
    lock_context: Any | None = None,
) -> dict[str, Any]:
    """Bind the pre-push journal to the one local commit before the CAS push."""
    _integer(generation, "publication generation", minimum=1)
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise StateValidationError("publication digest is invalid")
    if not isinstance(commit_sha, str) or not _SHA_40.fullmatch(commit_sha):
        raise StateValidationError("publisher commit is invalid")
    paths = state_paths(lock_dir)
    with _lock(paths, lock_context):
        journal = _validate_journal(_read_json(paths.journal))
        if (
            journal["handoff_phase"] != "generating"
            or journal["publication_generation"] != generation
            or journal["queue_digest"] != digest
            or journal["alignment_remote_head"] is not None
        ):
            raise StateValidationError("recovery journal cannot be armed for this pushed handoff")
        armed = dict(journal)
        armed["terminal_outcome"] = "pushed"
        armed["handoff_phase"] = "push_ready"
        armed["remote_commit"] = commit_sha
        _validate_journal(armed)
        atomic_write_record(paths.journal, armed)
        return armed


def update_deferred_alignment(
    lock_dir: os.PathLike[str] | str,
    generation: int,
    digest: str,
    runner_commit: str,
    remote_head: str,
    alignment_result: str,
    *,
    lock_context: Any | None = None,
) -> dict[str, Any]:
    """Persist one crash-safe remote-alignment target without changing paths."""
    paths = state_paths(lock_dir)
    with _lock(paths, lock_context):
        journal = _validate_journal(_read_json(paths.journal))
        if journal["handoff_phase"] in {"push_ready", "raw_proven"}:
            raise StateValidationError("alignment cannot erase a pushed handoff phase")
        if journal["publication_generation"] != generation or journal["queue_digest"] != digest:
            raise StateValidationError("alignment identity differs from queued publication")
        if not _SHA_40.fullmatch(runner_commit) or not _SHA_40.fullmatch(remote_head):
            raise StateValidationError("alignment commits are invalid")
        if alignment_result not in {"peer_supersedes", "regenerate"}:
            raise StateValidationError("alignment result is invalid")
        updated = dict(journal)
        updated["alignment_runner_commit"] = runner_commit
        updated["alignment_remote_head"] = remote_head
        updated["alignment_result"] = alignment_result
        for key in _JOURNAL_PROOF_KEYS:
            updated[key] = None
        if alignment_result == "peer_supersedes":
            updated["terminal_outcome"] = "peer_superseded"
            updated["handoff_phase"] = "terminal"
            updated["remote_commit"] = remote_head
        else:
            updated["terminal_outcome"] = None
            updated["handoff_phase"] = "generating"
            updated["remote_commit"] = None
        _validate_journal(updated)
        atomic_write_record(paths.journal, updated)
        return updated


def _raw_proven_journal(journal: dict[str, Any], pending: dict[str, Any]) -> dict[str, Any]:
    proven = dict(journal)
    proven["handoff_phase"] = "raw_proven"
    for key in _JOURNAL_PROOF_KEYS:
        proven[key] = pending[key]
    return _validate_journal(proven)


def _pending_from_journal(journal: dict[str, Any]) -> dict[str, Any]:
    if journal["handoff_phase"] != "raw_proven" or journal["terminal_outcome"] != "pushed":
        raise StateValidationError("journal does not contain durable raw proof")
    return _validate_pending({
        "schema_version": SCHEMA_VERSION,
        "generation": journal["publication_generation"],
        "queue_digest": journal["queue_digest"],
        "commit_sha": journal["remote_commit"],
        **{key: journal[key] for key in _JOURNAL_PROOF_KEYS},
    })


def _checkpoint_from_journal(journal: dict[str, Any]) -> dict[str, Any]:
    outcome = journal["terminal_outcome"]
    if outcome not in {"pushed", "no_diff", "peer_superseded"}:
        raise StateValidationError("journal does not identify a terminal checkpoint")
    return _validate_checkpoint({
        "schema_version": SCHEMA_VERSION,
        "outcome": outcome,
        "generation": journal["publication_generation"],
        "queue_digest": journal["queue_digest"],
        "commit_sha": journal["remote_commit"],
        "push_completed_at_utc": journal["push_completed_at_utc"] if outcome == "pushed" else None,
    })


def _install_generation_record(
    path: Path,
    record: dict[str, Any],
    validator: Callable[[Any], dict[str, Any]],
    label: str,
    *,
    preserve_pending_retry: bool = False,
) -> None:
    try:
        existing = validator(_read_json(path))
    except FileNotFoundError:
        atomic_write_record(path, record)
        return
    existing_generation = existing["generation"]
    record_generation = record["generation"]
    if existing_generation < record_generation:
        atomic_write_record(path, record)
        return
    if existing_generation > record_generation:
        raise StateValidationError(f"existing {label} is newer than authenticated handoff")
    if not preserve_pending_retry:
        if _canonical_bytes(existing) != _canonical_bytes(record):
            raise StateValidationError(f"existing {label} conflicts with authenticated handoff")
        return
    if not _same_pending_immutable(existing, record):
        raise StateValidationError(f"existing {label} immutable proof conflicts with authenticated handoff")
    existing_dominates = (
        existing["retry_count"] >= record["retry_count"]
        and existing["retry_deadline_utc"] >= record["retry_deadline_utc"]
    )
    record_dominates = (
        record["retry_count"] >= existing["retry_count"]
        and record["retry_deadline_utc"] >= existing["retry_deadline_utc"]
    )
    if existing_dominates:
        return
    if record_dominates:
        atomic_write_record(path, record)
        return
    raise StateValidationError(f"existing {label} retry state is not monotonic")


def _prepare_pushed_handoff_locked(
    paths: StatePaths,
    journal: dict[str, Any],
    pending: dict[str, Any],
    checkpoint: dict[str, Any],
) -> None:
    persisted = _validate_journal(_read_json(paths.journal))
    if journal["handoff_phase"] == "push_ready":
        if _canonical_bytes(persisted) != _canonical_bytes(journal):
            raise StateValidationError("recovery journal changed before pushed handoff preparation")
        proven = _raw_proven_journal(journal, pending)
        atomic_write_record(paths.journal, proven)
    elif journal["handoff_phase"] == "raw_proven":
        proven = _raw_proven_journal(journal, pending)
        if _canonical_bytes(persisted) != _canonical_bytes(proven):
            raise StateValidationError("durable raw proof differs from reconstructed handoff")
    else:
        raise StateValidationError("pushed handoff journal is not push-ready or raw-proven")
    _install_generation_record(
        paths.pending,
        pending,
        _validate_pending,
        "pending record",
        preserve_pending_retry=True,
    )
    _install_generation_record(paths.checkpoint, checkpoint, _validate_checkpoint, "checkpoint record")


def prepare_pushed_handoff(lock_dir: os.PathLike[str] | str, journal: dict[str, Any], pending: dict[str, Any], checkpoint: dict[str, Any], *, lock_context: Any | None = None) -> None:
    """Persist raw proof in journal, then pending, then checkpoint; never acknowledge latest."""
    paths = state_paths(lock_dir)
    journal = _validate_journal(journal)
    pending = _validate_pending(pending)
    checkpoint = _validate_checkpoint(checkpoint)
    if journal["terminal_outcome"] != "pushed" or checkpoint["outcome"] != "pushed":
        raise StateValidationError("prepare_pushed_handoff only accepts pushed outcomes")
    _same_identity(journal, pending, "pending record")
    _same_identity(journal, checkpoint, "checkpoint record")
    if journal["handoff_phase"] not in {"push_ready", "raw_proven"}:
        raise StateValidationError("pushed handoff journal is in the wrong phase")
    with _lock(paths, lock_context):
        _prepare_pushed_handoff_locked(paths, journal, pending, checkpoint)


def record_terminal_outcome(lock_dir: os.PathLike[str] | str, journal: dict[str, Any], checkpoint: dict[str, Any], *, lock_context: Any | None = None) -> None:
    paths = state_paths(lock_dir)
    journal = _validate_journal(journal)
    checkpoint = _validate_checkpoint(checkpoint)
    if (
        journal["handoff_phase"] != "terminal"
        or journal["terminal_outcome"] == "pushed"
        or checkpoint["outcome"] != journal["terminal_outcome"]
    ):
        raise StateValidationError("terminal outcome checkpoint disagrees with recovery journal")
    _same_identity(journal, checkpoint, "checkpoint record")
    with _lock(paths, lock_context):
        persisted_journal = _validate_journal(_read_json(paths.journal))
        if _canonical_bytes(persisted_journal) != _canonical_bytes(journal):
            if (
                persisted_journal["handoff_phase"] != "generating"
                or not _same_journal_identity(persisted_journal, journal)
                or any(persisted_journal[key] != journal[key] for key in _JOURNAL_ALIGNMENT_KEYS)
            ):
                raise StateValidationError("recovery journal changed before terminal outcome recording")
            atomic_write_record(paths.journal, journal)
        _install_generation_record(paths.checkpoint, checkpoint, _validate_checkpoint, "checkpoint record")


def finalize_pushed_handoff(lock_dir: os.PathLike[str] | str, generation: int, digest: str, *, lock_context: Any | None = None) -> bool:
    """Authenticate all handoff state, CAS-clear only the handled generation, unlink journal."""
    paths = state_paths(lock_dir)
    with _lock(paths, lock_context):
        journal = _validate_journal(_read_json(paths.journal))
        checkpoint = _validate_checkpoint(_read_json(paths.checkpoint))
        _same_identity(journal, checkpoint, "checkpoint record")
        if checkpoint["outcome"] != journal["terminal_outcome"]:
            raise StateValidationError("checkpoint outcome differs from recovery journal")
        if journal["publication_generation"] != generation or journal["queue_digest"] != digest:
            raise StateValidationError("finalization generation/digest differs from recovery journal")
        if journal["terminal_outcome"] == "pushed":
            pending = _validate_pending(_read_json(paths.pending))
            _same_identity(journal, pending, "pending record")
            if pending["commit_sha"] != checkpoint["commit_sha"]:
                raise StateValidationError("pending commit differs from checkpoint")
        current = read_latest_with_digest(lock_dir)
        if current is None:
            _unlink_record(paths.journal)
            return True
        current_generation, current_digest = current[0]["generation"], current[1]
        if current_generation == generation:
            if current_digest != digest:
                raise StateValidationError("same-generation latest record differs from finalization digest")
            _unlink_record(paths.latest)
            _unlink_record(paths.journal)
            return True
        if current_generation > generation:
            _unlink_record(paths.journal)
            return True
        raise StateValidationError("latest generation predates the finalization generation")


def recover_deferred_handoff(lock_dir: os.PathLike[str] | str, immutable_commit_confirmed: Callable[[str], bool], *, pending: dict[str, Any] | None = None, checkpoint: dict[str, Any] | None = None, lock_context: Any | None = None) -> bool:
    """Recreate missing deferred records only after independent immutable proof."""
    paths = state_paths(lock_dir)
    with _lock(paths, lock_context):
        try:
            journal = _validate_journal(_read_json(paths.journal))
        except FileNotFoundError:
            return False
        if journal["terminal_outcome"] == "pushed":
            if not immutable_commit_confirmed(journal["remote_commit"]):
                return False
            if journal["handoff_phase"] == "raw_proven":
                reconstructed_pending = _pending_from_journal(journal)
                reconstructed_checkpoint = _checkpoint_from_journal(journal)
                if pending is not None and _canonical_bytes(_validate_pending(pending)) != _canonical_bytes(reconstructed_pending):
                    raise StateValidationError("supplied pending record differs from durable raw proof")
                if checkpoint is not None and _canonical_bytes(_validate_checkpoint(checkpoint)) != _canonical_bytes(reconstructed_checkpoint):
                    raise StateValidationError("supplied checkpoint differs from durable raw proof")
                pending, checkpoint = reconstructed_pending, reconstructed_checkpoint
            elif pending is None or checkpoint is None:
                raise StateValidationError("push-ready recovery needs freshly re-proven pending/checkpoint evidence")
            else:
                pending = _validate_pending(pending)
                checkpoint = _validate_checkpoint(checkpoint)
            _same_identity(journal, pending, "pending record")
            _same_identity(journal, checkpoint, "checkpoint record")
            if checkpoint["outcome"] != journal["terminal_outcome"]:
                raise StateValidationError("checkpoint outcome differs from recovery journal")
            _prepare_pushed_handoff_locked(paths, journal, pending, checkpoint)
        else:
            if journal["handoff_phase"] != "terminal":
                return False
            if journal["terminal_outcome"] == "peer_superseded" and not immutable_commit_confirmed(journal["remote_commit"]):
                return False
            reconstructed_checkpoint = _checkpoint_from_journal(journal)
            if checkpoint is not None and _canonical_bytes(_validate_checkpoint(checkpoint)) != _canonical_bytes(reconstructed_checkpoint):
                raise StateValidationError("supplied checkpoint differs from terminal journal")
            checkpoint = reconstructed_checkpoint
            _same_identity(journal, checkpoint, "checkpoint record")
            _install_generation_record(paths.checkpoint, checkpoint, _validate_checkpoint, "checkpoint record")
        return True
