#!/usr/bin/python3
"""Immutable root-owned health attempt, lease, and incident recorder for WSL."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
LEASE_MAX_AGE_SECONDS = 480
CANDIDATE_MAX_AGE_SECONDS = 120
CANDIDATE_MAX_BYTES = 2048
ATTEMPT_MAX_BYTES = 1024
INSTALL_MAX_BYTES = 1024
STATE_MAX_BYTES = 8192

HEX32 = re.compile(r"[0-9a-f]{32}")
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
BOOT_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
UTC_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

# Shared with the unprivileged health probe. Values are public enums only.
CANDIDATE_FAILURE_CODES = frozenset(
    {
        "disk_space_low",
        "filesystem_unavailable",
        "git_unhealthy",
        "health_probe_failure",
        "local_status_invalid",
        "local_status_stale",
        "publication_queue_stale",
        "publication_state_invalid",
        "refresh_lock_invalid",
        "remote_dashboard_unhealthy",
        "systemd_activation_unhealthy",
        "systemd_worker_unhealthy",
        "terminal_publication_unhealthy",
        "watcher_pending_stale",
        "watcher_refresh_failures",
        "watcher_rpc_failures",
        "watcher_state_invalid",
        "watcher_state_missing",
        "watcher_stale",
    }
)
RECORDER_FAILURE_CODES = frozenset(
    {
        *CANDIDATE_FAILURE_CODES,
        "attempt_invalid",
        "attempt_missing",
        "attempt_mismatch",
        "boot_mismatch",
        "candidate_invalid",
        "candidate_invocation_mismatch",
        "candidate_missing",
        "candidate_timestamp_invalid",
        "install_mismatch",
        "service_failed",
    }
)
AUDIT_MIRROR_FAILURE_CODES = frozenset(
    {
        "activation_marker_unhealthy",
        "anchor_absent",
        "anchor_unreachable",
        "audit_unavailable",
        "lease_invalid",
        "lease_stale",
        "systemd_activation_unhealthy",
        "systemd_worker_unhealthy",
        "task_definition_unsafe",
        "task_instance_unsafe",
        "wsl_unreachable",
    }
)

PRODUCTION_STATE_DIR = Path("/var/lib/degen-dogs/health")
PRODUCTION_RUNTIME_DIR = Path("/run/degen-dogs/health")
PRODUCTION_CANDIDATE_PATH = Path("/var/cache/degen-dogs/health-report.json")


class StateError(RuntimeError):
    """A fixed-path health record failed validation."""


@dataclass(frozen=True)
class StateLayout:
    state_dir: Path
    runtime_dir: Path
    candidate_path: Path
    state_uid: int = 0
    state_gid: int = 0
    runner_uid: int | None = None
    runner_gid: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_dir", Path(self.state_dir))
        object.__setattr__(self, "runtime_dir", Path(self.runtime_dir))
        object.__setattr__(self, "candidate_path", Path(self.candidate_path))
        if self.runner_uid is None:
            object.__setattr__(self, "runner_uid", self.state_uid)


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise StateError("time must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or UTC_PATTERN.fullmatch(value) is None:
        raise StateError("timestamp is not strict UTC")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise StateError("timestamp is invalid") from exc
    if _utc_text(parsed) != value:
        raise StateError("timestamp is not canonical")
    return parsed


def _require_pattern(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise StateError(f"invalid {label}")
    return value


def _validate_boot_id(value: Any) -> str:
    return _require_pattern(value, BOOT_ID_PATTERN, "boot id")


def _validate_install(value: Any) -> dict[str, Any]:
    keys = {
        "install_epoch",
        "runner_gid",
        "runner_uid",
        "runtime_commit",
        "schema_version",
        "trusted_installer_commit",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise StateError("install identity schema is invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise StateError("install identity version is invalid")
    _require_pattern(value["install_epoch"], HEX32, "install epoch")
    for field in ("runner_uid", "runner_gid"):
        if type(value[field]) is not int or not (1 <= value[field] <= 2**31 - 1):
            raise StateError(f"invalid {field}")
    _require_pattern(value["runtime_commit"], HEX40, "runtime commit")
    _require_pattern(value["trusted_installer_commit"], HEX40, "trusted installer commit")
    return dict(value)


def _validate_attempt(value: Any) -> dict[str, Any]:
    keys = {
        "attempt_token",
        "boot_id",
        "install_epoch",
        "invocation_id",
        "schema_version",
        "started_at_utc",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise StateError("attempt schema is invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise StateError("attempt version is invalid")
    _require_pattern(value["attempt_token"], HEX64, "attempt token")
    _validate_boot_id(value["boot_id"])
    _require_pattern(value["install_epoch"], HEX32, "install epoch")
    _require_pattern(value["invocation_id"], HEX32, "invocation id")
    _parse_utc(value["started_at_utc"])
    return dict(value)


def _optional_nonnegative_int(value: Any, label: str, *, positive: bool = False) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < (1 if positive else 0):
        raise StateError(f"invalid {label}")
    return value


def _validate_codes(value: Any, allowed: frozenset[str], *, require_nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(code, str) for code in value):
        raise StateError("failure code list is invalid")
    if value != sorted(set(value)) or any(code not in allowed for code in value):
        raise StateError("failure code list is not canonical")
    if require_nonempty and not value:
        raise StateError("unhealthy status requires a failure code")
    return list(value)


def _validate_candidate(value: Any) -> dict[str, Any]:
    keys = {
        "attempt_token",
        "checked_at_utc",
        "failure_codes",
        "invocation_id",
        "latest_generated_block",
        "publication_generation",
        "runner_head",
        "schema_version",
        "status",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise StateError("candidate schema is invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise StateError("candidate version is invalid")
    _require_pattern(value["attempt_token"], HEX64, "attempt token")
    _require_pattern(value["invocation_id"], HEX32, "invocation id")
    _require_pattern(value["runner_head"], HEX40, "runner head")
    _parse_utc(value["checked_at_utc"])
    _optional_nonnegative_int(value["latest_generated_block"], "generated block")
    _optional_nonnegative_int(value["publication_generation"], "publication generation", positive=True)
    if value["status"] not in {"healthy", "unhealthy"}:
        raise StateError("candidate status is invalid")
    codes = _validate_codes(
        value["failure_codes"],
        CANDIDATE_FAILURE_CODES,
        require_nonempty=value["status"] == "unhealthy",
    )
    if value["status"] == "healthy" and codes:
        raise StateError("healthy candidate has failure codes")
    return dict(value)


def _validate_health_incident(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    keys = {
        "boot_id",
        "consecutive_failures",
        "failure_codes",
        "first_failure_at_utc",
        "install_epoch",
        "last_failure_at_utc",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise StateError("health incident schema is invalid")
    _validate_boot_id(value["boot_id"])
    _require_pattern(value["install_epoch"], HEX32, "install epoch")
    if type(value["consecutive_failures"]) is not int or value["consecutive_failures"] < 1:
        raise StateError("health incident count is invalid")
    _validate_codes(value["failure_codes"], RECORDER_FAILURE_CODES, require_nonempty=True)
    first = _parse_utc(value["first_failure_at_utc"])
    last = _parse_utc(value["last_failure_at_utc"])
    if last < first:
        raise StateError("health incident timestamps are reversed")
    return dict(value)


def _validate_audit_mirror(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"checked_at_utc", "failure_codes", "status"}:
        raise StateError("audit mirror schema is invalid")
    _parse_utc(value["checked_at_utc"])
    if value["status"] not in {"healthy", "unhealthy"}:
        raise StateError("audit mirror status is invalid")
    codes = _validate_codes(
        value["failure_codes"],
        AUDIT_MIRROR_FAILURE_CODES,
        require_nonempty=value["status"] == "unhealthy",
    )
    if value["status"] == "healthy" and codes:
        raise StateError("healthy audit mirror has failure codes")
    return dict(value)


def _validate_recovery(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    keys = {
        "boot_id",
        "consecutive_failures",
        "failure_codes",
        "first_failure_at_utc",
        "install_epoch",
        "last_failure_at_utc",
        "recovered_at_utc",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise StateError("recovery schema is invalid")
    health = _validate_health_incident({key: value[key] for key in keys - {"recovered_at_utc"}})
    assert health is not None
    recovered = _parse_utc(value["recovered_at_utc"])
    if recovered < _parse_utc(value["last_failure_at_utc"]):
        raise StateError("recovery precedes failure")
    return dict(value)


def _validate_incident(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "audit_mirror",
        "health",
        "last_recovery",
        "schema_version",
    }:
        raise StateError("incident schema is invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise StateError("incident version is invalid")
    _validate_health_incident(value["health"])
    _validate_audit_mirror(value["audit_mirror"])
    _validate_recovery(value["last_recovery"])
    return dict(value)


def _validate_last_good(value: Any) -> dict[str, Any]:
    keys = {
        "boot_id",
        "completed_at_boot_seconds",
        "completed_at_utc",
        "install_epoch",
        "invocation_id",
        "latest_generated_block",
        "publication_generation",
        "runner_head",
        "runtime_commit",
        "schema_version",
        "trusted_installer_commit",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise StateError("lease schema is invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise StateError("lease version is invalid")
    _validate_boot_id(value["boot_id"])
    _require_pattern(value["install_epoch"], HEX32, "install epoch")
    _require_pattern(value["invocation_id"], HEX32, "invocation id")
    _require_pattern(value["runner_head"], HEX40, "runner head")
    _require_pattern(value["runtime_commit"], HEX40, "runtime commit")
    _require_pattern(value["trusted_installer_commit"], HEX40, "trusted installer commit")
    _parse_utc(value["completed_at_utc"])
    boot_seconds = value["completed_at_boot_seconds"]
    if type(boot_seconds) not in {int, float} or not (0 <= boot_seconds <= 10**10):
        raise StateError("lease monotonic completion is invalid")
    _optional_nonnegative_int(value["latest_generated_block"], "generated block")
    _optional_nonnegative_int(value["publication_generation"], "publication generation", positive=True)
    return dict(value)


def _empty_incident() -> dict[str, Any]:
    return {
        "audit_mirror": None,
        "health": None,
        "last_recovery": None,
        "schema_version": SCHEMA_VERSION,
    }


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"incident", "last_good", "schema_version"}:
        raise StateError("health state schema is invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise StateError("health state version is invalid")
    _validate_incident(value["incident"])
    if value["last_good"] is not None:
        _validate_last_good(value["last_good"])
    return dict(value)


def _open_absolute_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise StateError("directory path is not absolute and normalized")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    exact_mode: int | None = None,
) -> int:
    descriptor = _open_absolute_directory(path)
    details = os.fstat(descriptor)
    mode = stat.S_IMODE(details.st_mode)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != expected_uid
        or details.st_gid != expected_gid
        or (exact_mode is not None and mode != exact_mode)
        or (exact_mode is None and mode & 0o022)
    ):
        os.close(descriptor)
        raise StateError("directory identity is unsafe")
    return descriptor


def _read_json_at(
    directory_fd: int,
    name: str,
    *,
    expected_uid: int,
    expected_gid: int,
    mode: int,
    maximum_size: int,
    validator,
) -> dict[str, Any]:
    flags = (
        os.O_RDONLY
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != expected_uid
            or details.st_gid != expected_gid
            or stat.S_IMODE(details.st_mode) != mode
            or details.st_nlink != 1
            or details.st_size <= 0
            or details.st_size > maximum_size
        ):
            raise StateError("record identity is unsafe")
        chunks: list[bytes] = []
        remaining = maximum_size + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > maximum_size:
        raise StateError("record is oversized")
    try:
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("record JSON is invalid") from exc
    validated = validator(value)
    if raw != _canonical_bytes(validated):
        raise StateError("record JSON is not canonical")
    return validated


def _atomic_write_at(
    directory_fd: int,
    name: str,
    value: dict[str, Any],
    *,
    owner_uid: int,
    owner_gid: int,
    mode: int = 0o600,
    maximum_size: int = STATE_MAX_BYTES,
) -> None:
    payload = _canonical_bytes(value)
    if not payload or len(payload) > maximum_size:
        raise StateError("record payload is outside its size bound")
    temporary = f".{name}.{secrets.token_hex(12)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, mode, dir_fd=directory_fd)
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, owner_uid, owner_gid)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short state write")
            offset += written
        os.fsync(descriptor)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != owner_uid
            or details.st_gid != owner_gid
            or stat.S_IMODE(details.st_mode) != mode
            or details.st_nlink != 1
        ):
            raise StateError("temporary record identity is unsafe")
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        installed = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            details = os.fstat(installed)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != owner_uid
                or details.st_gid != owner_gid
                or stat.S_IMODE(details.st_mode) != mode
                or details.st_nlink != 1
                or details.st_size != len(payload)
            ):
                raise StateError("installed record identity is unsafe")
        finally:
            os.close(installed)
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


@contextmanager
def _locked_state(layout: StateLayout) -> Iterator[int]:
    state_fd = _open_directory(
        layout.state_dir,
        expected_uid=layout.state_uid,
        expected_gid=layout.state_gid,
        exact_mode=0o700,
    )
    lock_fd = -1
    try:
        flags = (
            os.O_RDWR
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        created = False
        try:
            lock_fd = os.open("state.lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=state_fd)
            created = True
        except FileExistsError:
            lock_fd = os.open("state.lock", flags, dir_fd=state_fd)
        if created:
            details = os.fstat(lock_fd)
            if details.st_uid != layout.state_uid or details.st_gid != layout.state_gid:
                os.fchown(lock_fd, layout.state_uid, layout.state_gid)
            os.fchmod(lock_fd, 0o600)
        details = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != layout.state_uid
            or details.st_gid != layout.state_gid
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
            or details.st_size != 0
        ):
            raise StateError("state lock identity is unsafe")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        named = os.stat("state.lock", dir_fd=state_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(named.st_mode)
            or named.st_dev != details.st_dev
            or named.st_ino != details.st_ino
        ):
            raise StateError("state lock name changed during acquisition")
        yield state_fd
    finally:
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        os.close(state_fd)


def _read_install(state_fd: int, layout: StateLayout) -> dict[str, Any]:
    return _read_json_at(
        state_fd,
        "install.json",
        expected_uid=layout.state_uid,
        expected_gid=layout.state_gid,
        mode=0o600,
        maximum_size=INSTALL_MAX_BYTES,
        validator=_validate_install,
    )


def _read_optional_state(state_fd: int, layout: StateLayout) -> dict[str, Any]:
    try:
        return _read_json_at(
            state_fd,
            "state.json",
            expected_uid=layout.state_uid,
            expected_gid=layout.state_gid,
            mode=0o600,
            maximum_size=STATE_MAX_BYTES,
            validator=_validate_state,
        )
    except FileNotFoundError:
        return {
            "incident": _empty_incident(),
            "last_good": None,
            "schema_version": SCHEMA_VERSION,
        }


def _remove_legacy_split_state(state_fd: int) -> None:
    """Remove obsolete fixed-name records; state.json is the sole authority."""
    removed = False
    for name in ("last-good.json", "incident.json"):
        try:
            os.unlink(name, dir_fd=state_fd)
            removed = True
        except FileNotFoundError:
            pass
        except IsADirectoryError as exc:
            raise StateError("legacy split-state entry is unsafe") from exc
    if removed:
        os.fsync(state_fd)


def _open_candidate_parent(layout: StateLayout, expected_uid: int) -> int:
    return _open_directory(
        layout.candidate_path.parent,
        expected_uid=expected_uid,
        expected_gid=layout.runner_gid,
        exact_mode=0o700,
    )


def _ensure_runtime_directory(layout: StateLayout) -> int:
    parent_fd = _open_directory(
        layout.runtime_dir.parent,
        expected_uid=layout.state_uid,
        expected_gid=layout.state_gid,
        exact_mode=None,
    )
    runtime_fd = -1
    created = False
    try:
        try:
            os.mkdir(layout.runtime_dir.name, 0o750, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        runtime_fd = os.open(layout.runtime_dir.name, flags, dir_fd=parent_fd)
        details = os.fstat(runtime_fd)
        if not stat.S_ISDIR(details.st_mode):
            raise StateError("runtime directory identity is unsafe")
        if created:
            if details.st_uid != layout.state_uid:
                raise StateError("new runtime directory owner is unsafe")
            if details.st_uid != layout.state_uid or details.st_gid != layout.runner_gid:
                os.fchown(runtime_fd, layout.state_uid, layout.runner_gid)
            if stat.S_IMODE(details.st_mode) != 0o750:
                os.fchmod(runtime_fd, 0o750)
            os.fsync(runtime_fd)
            os.fsync(parent_fd)
            details = os.fstat(runtime_fd)
        if (
            details.st_uid != layout.state_uid
            or details.st_gid != layout.runner_gid
            or stat.S_IMODE(details.st_mode) != 0o750
        ):
            raise StateError("runtime directory identity is unsafe")
        result = runtime_fd
        runtime_fd = -1
        return result
    finally:
        if runtime_fd >= 0:
            os.close(runtime_fd)
        os.close(parent_fd)


def prepare_runtime(layout: StateLayout) -> dict[str, Any]:
    """Create/attest the volatile root:runner attempt directory after each WSL boot."""
    descriptor = _ensure_runtime_directory(layout)
    os.close(descriptor)
    return {"runtime_ready": True, "schema_version": SCHEMA_VERSION}


def write_install_identity(layout: StateLayout, install: dict[str, Any]) -> dict[str, Any]:
    """Durably install one exact installer/runtime identity."""
    validated = _validate_install(install)
    if validated["runner_uid"] != layout.runner_uid or validated["runner_gid"] != layout.runner_gid:
        raise StateError("install runner identity does not match the state layout")
    with _locked_state(layout) as state_fd:
        _remove_legacy_split_state(state_fd)
        _atomic_write_at(
            state_fd,
            "install.json",
            validated,
            owner_uid=layout.state_uid,
            owner_gid=layout.state_gid,
            maximum_size=INSTALL_MAX_BYTES,
        )
    return validated


def begin_health(
    layout: StateLayout,
    *,
    invocation_id: str,
    install: dict[str, Any],
    boot_id: str,
    now: datetime,
) -> dict[str, Any]:
    """Remove stale runner evidence and create a root-bound health attempt."""
    invocation_id = _require_pattern(invocation_id, HEX32, "invocation id")
    boot_id = _validate_boot_id(boot_id)
    install = _validate_install(install)
    if install["runner_uid"] != layout.runner_uid or install["runner_gid"] != layout.runner_gid:
        raise StateError("attempt runner identity does not match the install")
    started_at = _utc_text(now)
    with _locked_state(layout) as state_fd:
        active_install = _read_install(state_fd, layout)
        if active_install != install:
            raise StateError("active install identity changed")
        assert layout.runner_uid is not None
        candidate_fd = _open_candidate_parent(layout, layout.runner_uid)
        try:
            try:
                os.unlink(layout.candidate_path.name, dir_fd=candidate_fd)
            except FileNotFoundError:
                pass
            os.fsync(candidate_fd)
        finally:
            os.close(candidate_fd)
        runtime_fd = _ensure_runtime_directory(layout)
        try:
            attempt = {
                "attempt_token": secrets.token_hex(32),
                "boot_id": boot_id,
                "install_epoch": install["install_epoch"],
                "invocation_id": invocation_id,
                "schema_version": SCHEMA_VERSION,
                "started_at_utc": started_at,
            }
            _atomic_write_at(
                runtime_fd,
                "attempt.json",
                attempt,
                owner_uid=layout.state_uid,
                owner_gid=layout.runner_gid,
                mode=0o640,
                maximum_size=ATTEMPT_MAX_BYTES,
            )
            return attempt
        finally:
            os.close(runtime_fd)


def _consume_runtime_record(runtime_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=runtime_fd)
        os.fsync(runtime_fd)
    except FileNotFoundError:
        pass


def record_health(
    layout: StateLayout,
    *,
    service_result: str,
    exit_code: str,
    exit_status: str,
    now: datetime,
    boot_id: str,
    uptime_seconds: float,
    expected_uid: int,
) -> dict[str, Any]:
    """Consume one health result and advance the lease only on full agreement."""
    boot_id = _validate_boot_id(boot_id)
    now_text = _utc_text(now)
    if type(uptime_seconds) not in {int, float} or not (0 <= uptime_seconds <= 10**10):
        raise StateError("invalid boot-monotonic time")
    if expected_uid != layout.runner_uid:
        raise StateError("candidate owner does not match the install identity")
    failure_codes: set[str] = set()
    service_success = service_result == "success" and exit_code == "exited" and exit_status == "0"
    if not service_success:
        failure_codes.add("service_failed")

    with _locked_state(layout) as state_fd:
        install = _read_install(state_fd, layout)
        health_state = _read_optional_state(state_fd, layout)
        incident = health_state["incident"]
        event_time = _parse_utc(now_text)
        prior_health_boundary = _validate_health_incident(incident["health"])
        if prior_health_boundary is not None:
            event_time = max(event_time, _parse_utc(prior_health_boundary["last_failure_at_utc"]))
        prior_recovery_boundary = _validate_recovery(incident["last_recovery"])
        if prior_recovery_boundary is not None:
            event_time = max(event_time, _parse_utc(prior_recovery_boundary["recovered_at_utc"]))
        event_time_text = _utc_text(event_time)
        runtime_fd = _ensure_runtime_directory(layout)
        candidate_parent_fd = -1
        attempt: dict[str, Any] | None = None
        candidate: dict[str, Any] | None = None
        try:
            try:
                candidate_parent_fd = _open_candidate_parent(layout, expected_uid)
            except (OSError, StateError):
                failure_codes.add("candidate_invalid")
            try:
                attempt = _read_json_at(
                    runtime_fd,
                    "attempt.json",
                    expected_uid=layout.state_uid,
                    expected_gid=layout.runner_gid,
                    mode=0o640,
                    maximum_size=ATTEMPT_MAX_BYTES,
                    validator=_validate_attempt,
                )
            except FileNotFoundError:
                attempt = None
            except (OSError, StateError):
                failure_codes.add("attempt_invalid")

            if candidate_parent_fd >= 0:
                try:
                    candidate = _read_json_at(
                        candidate_parent_fd,
                        layout.candidate_path.name,
                        expected_uid=expected_uid,
                        expected_gid=layout.runner_gid,
                        mode=0o600,
                        maximum_size=CANDIDATE_MAX_BYTES,
                        validator=_validate_candidate,
                    )
                except FileNotFoundError:
                    failure_codes.add("candidate_missing")
                except (OSError, StateError):
                    failure_codes.add("candidate_invalid")

            if attempt is None and "attempt_invalid" not in failure_codes and candidate is not None:
                failure_codes.add("attempt_missing")
            if attempt is not None:
                if attempt["install_epoch"] != install["install_epoch"]:
                    failure_codes.add("install_mismatch")
                if attempt["boot_id"] != boot_id:
                    failure_codes.add("boot_mismatch")
            if candidate is not None:
                failure_codes.update(candidate["failure_codes"])
                if attempt is not None:
                    if candidate["attempt_token"] != attempt["attempt_token"]:
                        failure_codes.add("attempt_mismatch")
                    if candidate["invocation_id"] != attempt["invocation_id"]:
                        failure_codes.add("candidate_invocation_mismatch")
                    checked = _parse_utc(candidate["checked_at_utc"])
                    started = _parse_utc(attempt["started_at_utc"])
                    age = (now.astimezone(timezone.utc) - checked).total_seconds()
                    if checked < started or age < 0 or age > CANDIDATE_MAX_AGE_SECONDS:
                        failure_codes.add("candidate_timestamp_invalid")
                if candidate["status"] != "healthy":
                    if not candidate["failure_codes"]:
                        failure_codes.add("candidate_invalid")

            lease_advanced = bool(service_success and attempt is not None and candidate is not None and not failure_codes)
            if lease_advanced:
                assert attempt is not None and candidate is not None
                last_good = {
                    "boot_id": boot_id,
                    "completed_at_boot_seconds": round(float(uptime_seconds), 3),
                    "completed_at_utc": now_text,
                    "install_epoch": install["install_epoch"],
                    "invocation_id": attempt["invocation_id"],
                    "latest_generated_block": candidate["latest_generated_block"],
                    "publication_generation": candidate["publication_generation"],
                    "runner_head": candidate["runner_head"],
                    "runtime_commit": install["runtime_commit"],
                    "schema_version": SCHEMA_VERSION,
                    "trusted_installer_commit": install["trusted_installer_commit"],
                }
                _validate_last_good(last_good)
                health_state["last_good"] = last_good
                prior_health = _validate_health_incident(incident["health"])
                if prior_health is not None:
                    incident["last_recovery"] = {
                        **prior_health,
                        "recovered_at_utc": event_time_text,
                    }
                incident["health"] = None
            else:
                if not failure_codes:
                    failure_codes.add("candidate_invalid")
                codes = sorted(failure_codes)
                prior_health = _validate_health_incident(incident["health"])
                same_incident = bool(
                    prior_health is not None
                    and prior_health["install_epoch"] == install["install_epoch"]
                    and prior_health["boot_id"] == boot_id
                )
                incident["health"] = {
                    "boot_id": boot_id,
                    "consecutive_failures": (prior_health["consecutive_failures"] + 1) if same_incident else 1,
                    "failure_codes": codes,
                    "first_failure_at_utc": (
                        prior_health["first_failure_at_utc"] if same_incident else event_time_text
                    ),
                    "install_epoch": install["install_epoch"],
                    "last_failure_at_utc": event_time_text,
                }
            _validate_incident(incident)
            health_state["incident"] = incident
            _validate_state(health_state)
            _atomic_write_at(
                state_fd,
                "state.json",
                health_state,
                owner_uid=layout.state_uid,
                owner_gid=layout.state_gid,
            )
            _consume_runtime_record(runtime_fd, "attempt.json")
            if candidate_parent_fd >= 0:
                try:
                    os.unlink(layout.candidate_path.name, dir_fd=candidate_parent_fd)
                    os.fsync(candidate_parent_fd)
                except FileNotFoundError:
                    pass
            return {
                "failure_codes": [] if lease_advanced else sorted(failure_codes),
                "lease_advanced": lease_advanced,
                "schema_version": SCHEMA_VERSION,
            }
        finally:
            if candidate_parent_fd >= 0:
                os.close(candidate_parent_fd)
            os.close(runtime_fd)


def _safe_incident_snapshot(incident: dict[str, Any]) -> dict[str, Any]:
    return {
        "audit_mirror": incident["audit_mirror"],
        "health": incident["health"],
        "last_recovery": incident["last_recovery"],
    }


def snapshot(layout: StateLayout, *, boot_id: str, uptime_seconds: float) -> dict[str, Any]:
    """Return a bounded snapshot whose lease age uses WSL boot-monotonic time."""
    boot_id = _validate_boot_id(boot_id)
    if type(uptime_seconds) not in {int, float} or not (0 <= uptime_seconds <= 10**10):
        raise StateError("invalid boot-monotonic time")
    base: dict[str, Any] = {
        "incident": {"audit_mirror": None, "health": None, "last_recovery": None},
        "install_epoch": None,
        "latest_generated_block": None,
        "lease_age_seconds": None,
        "lease_max_age_seconds": LEASE_MAX_AGE_SECONDS,
        "lease_reason": "missing",
        "lease_valid": False,
        "publication_generation": None,
        "runner_head": None,
        "runtime_commit": None,
        "schema_version": SCHEMA_VERSION,
        "trusted_installer_commit": None,
    }
    try:
        with _locked_state(layout) as state_fd:
            install = _read_install(state_fd, layout)
            health_state = _read_optional_state(state_fd, layout)
            incident = health_state["incident"]
            base["incident"] = _safe_incident_snapshot(incident)
            base["install_epoch"] = install["install_epoch"]
            base["runtime_commit"] = install["runtime_commit"]
            base["trusted_installer_commit"] = install["trusted_installer_commit"]
            lease = health_state["last_good"]
            if lease is None:
                return base
            base["latest_generated_block"] = lease["latest_generated_block"]
            base["publication_generation"] = lease["publication_generation"]
            base["runner_head"] = lease["runner_head"]
            if (
                lease["install_epoch"] != install["install_epoch"]
                or lease["runtime_commit"] != install["runtime_commit"]
                or lease["trusted_installer_commit"] != install["trusted_installer_commit"]
            ):
                base["lease_reason"] = "install_mismatch"
                return base
            if lease["boot_id"] != boot_id:
                base["lease_reason"] = "boot_mismatch"
                return base
            age = float(uptime_seconds) - float(lease["completed_at_boot_seconds"])
            if age < 0:
                base["lease_reason"] = "monotonic_regression"
                return base
            base["lease_age_seconds"] = int(age)
            if age > LEASE_MAX_AGE_SECONDS:
                base["lease_reason"] = "stale"
                return base
            base["lease_reason"] = "valid"
            base["lease_valid"] = True
            return base
    except (OSError, StateError):
        base["lease_reason"] = "state_invalid"
        return base


def record_audit_mirror(layout: StateLayout, mirror: dict[str, Any]) -> dict[str, Any]:
    """Update only the non-authoritative Linux mirror of Windows audit state."""
    validated = _validate_audit_mirror(mirror)
    assert validated is not None
    with _locked_state(layout) as state_fd:
        _read_install(state_fd, layout)
        health_state = _read_optional_state(state_fd, layout)
        incident = health_state["incident"]
        incident["audit_mirror"] = validated
        _validate_incident(incident)
        health_state["incident"] = incident
        _validate_state(health_state)
        _atomic_write_at(
            state_fd,
            "state.json",
            health_state,
            owner_uid=layout.state_uid,
            owner_gid=layout.state_gid,
        )
    return validated


def clear_audit_mirror(layout: StateLayout) -> dict[str, Any]:
    with _locked_state(layout) as state_fd:
        _read_install(state_fd, layout)
        health_state = _read_optional_state(state_fd, layout)
        incident = health_state["incident"]
        incident["audit_mirror"] = None
        _validate_incident(incident)
        health_state["incident"] = incident
        _validate_state(health_state)
        _atomic_write_at(
            state_fd,
            "state.json",
            health_state,
            owner_uid=layout.state_uid,
            owner_gid=layout.state_gid,
        )
    return {"audit_mirror_cleared": True, "schema_version": SCHEMA_VERSION}


def _read_boot_id() -> str:
    descriptor = os.open(
        "/proc/sys/kernel/random/boot_id",
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        raw = os.read(descriptor, 128)
    finally:
        os.close(descriptor)
    return _validate_boot_id(raw.decode("ascii", errors="strict").strip())


def _boottime_seconds() -> float:
    if not hasattr(time, "CLOCK_BOOTTIME"):
        raise StateError("CLOCK_BOOTTIME is unavailable")
    return float(time.clock_gettime(time.CLOCK_BOOTTIME))


def _production_layout() -> StateLayout:
    root_layout = StateLayout(
        state_dir=PRODUCTION_STATE_DIR,
        runtime_dir=PRODUCTION_RUNTIME_DIR,
        candidate_path=PRODUCTION_CANDIDATE_PATH,
        state_uid=0,
        state_gid=0,
        runner_uid=0,
        runner_gid=0,
    )
    with _locked_state(root_layout) as state_fd:
        install = _read_install(state_fd, root_layout)
    return StateLayout(
        state_dir=PRODUCTION_STATE_DIR,
        runtime_dir=PRODUCTION_RUNTIME_DIR,
        candidate_path=PRODUCTION_CANDIDATE_PATH,
        state_uid=0,
        state_gid=0,
        runner_uid=install["runner_uid"],
        runner_gid=install["runner_gid"],
    )


def _ensure_production_state_dir() -> None:
    parent = PRODUCTION_STATE_DIR.parent
    parent_fd = _open_directory(parent, expected_uid=0, expected_gid=0, exact_mode=None)
    try:
        try:
            os.mkdir(PRODUCTION_STATE_DIR.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        os.chown(PRODUCTION_STATE_DIR.name, 0, 0, dir_fd=parent_fd, follow_symlinks=False)
        os.chmod(PRODUCTION_STATE_DIR.name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    descriptor = _open_directory(PRODUCTION_STATE_DIR, expected_uid=0, expected_gid=0, exact_mode=0o700)
    os.close(descriptor)


def _write_stdout(value: dict[str, Any]) -> None:
    payload = _canonical_bytes(value)
    if len(payload) > STATE_MAX_BYTES:
        raise StateError("command output is oversized")
    sys.stdout.buffer.write(payload)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    os.umask(0o077)
    if os.geteuid() != 0:
        raise SystemExit("health state helper must run as root")
    if not arguments:
        raise SystemExit(
            "usage: degen-dogs-wsl-health-state "
            "{install-identity|prepare-runtime|begin-health|record-health|snapshot|record-audit-mirror|clear-audit-mirror}"
        )
    mode, *rest = arguments
    if mode == "install-identity":
        if len(rest) != 5:
            raise SystemExit(
                "install-identity requires INSTALL_EPOCH RUNTIME_COMMIT TRUSTED_COMMIT RUNNER_UID RUNNER_GID"
            )
        if re.fullmatch(r"[1-9][0-9]{0,9}", rest[3]) is None or re.fullmatch(r"[1-9][0-9]{0,9}", rest[4]) is None:
            raise SystemExit("install-identity runner UID/GID must be canonical positive decimal integers")
        runner_uid = int(rest[3])
        runner_gid = int(rest[4])
        _ensure_production_state_dir()
        layout = StateLayout(
            state_dir=PRODUCTION_STATE_DIR,
            runtime_dir=PRODUCTION_RUNTIME_DIR,
            candidate_path=PRODUCTION_CANDIDATE_PATH,
            state_uid=0,
            state_gid=0,
            runner_uid=runner_uid,
            runner_gid=runner_gid,
        )
        record_value = {
            "install_epoch": rest[0],
            "runner_gid": runner_gid,
            "runner_uid": runner_uid,
            "runtime_commit": rest[1],
            "schema_version": SCHEMA_VERSION,
            "trusted_installer_commit": rest[2],
        }
        _write_stdout(write_install_identity(layout, record_value))
        return 0
    if rest:
        raise SystemExit(f"{mode} accepts no arguments")
    layout = _production_layout()
    if mode == "prepare-runtime":
        _write_stdout(prepare_runtime(layout))
        return 0
    if mode == "begin-health":
        invocation_id = os.environ.get("INVOCATION_ID", "")
        with _locked_state(layout) as state_fd:
            install = _read_install(state_fd, layout)
        _write_stdout(
            begin_health(
                layout,
                invocation_id=invocation_id,
                install=install,
                boot_id=_read_boot_id(),
                now=datetime.now(timezone.utc),
            )
        )
        return 0
    if mode == "record-health":
        assert layout.runner_uid is not None
        _write_stdout(
            record_health(
                layout,
                service_result=os.environ.get("SERVICE_RESULT", ""),
                exit_code=os.environ.get("EXIT_CODE", ""),
                exit_status=os.environ.get("EXIT_STATUS", ""),
                now=datetime.now(timezone.utc),
                boot_id=_read_boot_id(),
                uptime_seconds=_boottime_seconds(),
                expected_uid=layout.runner_uid,
            )
        )
        return 0
    if mode == "snapshot":
        _write_stdout(snapshot(layout, boot_id=_read_boot_id(), uptime_seconds=_boottime_seconds()))
        return 0
    if mode == "record-audit-mirror":
        raw = sys.stdin.buffer.read(STATE_MAX_BYTES + 1)
        if len(raw) > STATE_MAX_BYTES:
            raise StateError("audit mirror input is oversized")
        try:
            mirror = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateError("audit mirror input is invalid") from exc
        if raw != _canonical_bytes(mirror):
            raise StateError("audit mirror input is not canonical")
        _write_stdout(record_audit_mirror(layout, mirror))
        return 0
    if mode == "clear-audit-mirror":
        _write_stdout(clear_audit_mirror(layout))
        return 0
    raise SystemExit(f"unsupported mode: {mode}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, StateError) as exc:
        print(f"health-state error: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
