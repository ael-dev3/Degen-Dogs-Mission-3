#!/usr/bin/env python3
"""Private, durable state primitives for the WSL latest-wins publisher.

This module deliberately contains no publishing, networking, or subprocess
logic.  It owns only authenticated records below ``LOCK_DIR/publication`` and
the exact compare-and-swap transitions consumed by the watcher and drainer.
"""
from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import datetime as dt
import errno
import enum
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
    pages_verified: Path
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


@dataclasses.dataclass(frozen=True)
class PendingSnapshot:
    """One validated pending record and its captured mutable/immutable digests."""

    record: dict[str, Any]
    record_digest: str
    proof_fingerprint: str


class PendingFinalizeResult(enum.Enum):
    CLEARED = "cleared"
    BLOCKED_MATCHING_JOURNAL = "blocked_matching_journal"
    SUPERSEDED_OR_ABSENT = "superseded_or_absent"


@dataclasses.dataclass(frozen=True)
class _PinnedStateIO:
    root_path: Path
    publication_path: Path
    root_fd: int
    publication_fd: int


_ACTIVE_STATE_IO: contextvars.ContextVar[_PinnedStateIO | None] = contextvars.ContextVar(
    "runner_publication_state_active_io",
    default=None,
)


def _runner_path_security_module() -> Any:
    """Load the fixed sibling helper even in stdin/importlib fixture contexts."""
    try:
        import runner_path_security

        return runner_path_security
    except ModuleNotFoundError as exc:
        if exc.name != "runner_path_security":
            raise
    import importlib.util
    import sys

    module_name = "_degen_dogs_runner_path_security"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    helper_path = Path(__file__).resolve().with_name("runner_path_security.py")
    specification = importlib.util.spec_from_file_location(module_name, helper_path)
    if specification is None or specification.loader is None:
        raise StateValidationError("cannot load the fixed runner path-security helper")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _publication_coverage_module() -> Any:
    """Load the fixed sibling proof helper in importlib-driven test contexts."""
    try:
        import publication_coverage

        return publication_coverage
    except ModuleNotFoundError as exc:
        if exc.name != "publication_coverage":
            raise
    import importlib.util
    import sys

    module_name = "_degen_dogs_publication_coverage"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    helper_path = Path(__file__).resolve().with_name("publication_coverage.py")
    specification = importlib.util.spec_from_file_location(module_name, helper_path)
    if specification is None or specification.loader is None:
        raise StateValidationError("cannot load the fixed publication coverage helper")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def state_paths(lock_dir: os.PathLike[str] | str) -> StatePaths:
    # ``resolve()`` would silently follow an attacker-controlled lock-dir link.
    root = Path(os.path.abspath(os.fspath(lock_dir)))
    publication = root / "publication"
    return StatePaths(
        root=root,
        publication=publication,
        latest=publication / "latest.json",
        pending=publication / "pending.json",
        pages_verified=publication / "pages-verified.json",
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
    if _requires_posix_metadata():
        normalized = Path(os.path.abspath(os.fspath(path)))
        active = _ACTIVE_STATE_IO.get()
        if active is not None:
            if normalized == active.root_path:
                _validate_private_directory_descriptor(active.root_fd, normalized)
                return
            if normalized == active.publication_path:
                _validate_private_directory_descriptor(active.publication_fd, normalized)
                return
            raise StateValidationError(
                f"state transaction attempted an unrelated directory: {normalized}"
            )
        try:
            runner_path_security = _runner_path_security_module()

            descriptor = runner_path_security.open_secure_directory(
                normalized,
                create=True,
                private=True,
            )
            try:
                details = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        except Exception as exc:
            raise StateValidationError(f"private state directory is unsafe: {normalized}") from exc
        if not stat.S_ISDIR(details.st_mode):
            raise StateValidationError(f"private state directory is not a real directory: {normalized}")
        return
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise StateValidationError(f"private state directory is not a real directory: {path}")
    owner = _owner_uid()
    if _requires_posix_metadata() and owner is not None and details.st_uid != owner:
        raise StateValidationError(f"private state directory is owned by another user: {path}")
    if _requires_posix_metadata() and stat.S_IMODE(details.st_mode) != 0o700:
        os.chmod(path, 0o700)


def _validate_private_directory_descriptor(descriptor: int, path: Path) -> os.stat_result:
    details = os.fstat(descriptor)
    owner = _owner_uid()
    if (
        not stat.S_ISDIR(details.st_mode)
        or owner is None
        or details.st_uid != owner
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise StateValidationError(f"private state directory metadata is unsafe: {path}")
    return details


def _open_private_directory(path: Path, *, create: bool) -> int:
    """Open one absolute private directory without following any ancestor link."""
    try:
        runner_path_security = _runner_path_security_module()

        descriptor = runner_path_security.open_secure_directory(
            path,
            create=create,
            private=True,
        )
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise StateValidationError(f"private state directory is unsafe: {path}") from exc
    try:
        _validate_private_directory_descriptor(descriptor, path)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _validate_private_descriptor(
    descriptor: int,
    path: Path,
    *,
    max_bytes: int = MAX_RECORD_BYTES,
) -> os.stat_result:
    details = os.fstat(descriptor)
    owner = _owner_uid()
    if not stat.S_ISREG(details.st_mode):
        raise StateValidationError(f"private state record is not a regular file: {path}")
    if owner is None or details.st_uid != owner:
        raise StateValidationError(f"private state record is owned by another user: {path}")
    if stat.S_IMODE(details.st_mode) != 0o600:
        raise StateValidationError(f"private state record mode is not 0600: {path}")
    if details.st_nlink != 1:
        raise StateValidationError(f"private state record has unexpected link count: {path}")
    if details.st_size > max_bytes:
        raise StateValidationError(f"private state record exceeds size limit: {path}")
    return details


def _validate_named_identity(
    parent_descriptor: int,
    path: Path,
    opened: os.stat_result,
) -> None:
    try:
        named = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise StateValidationError(f"private state record identity changed: {path}") from exc
    if (
        stat.S_ISLNK(named.st_mode)
        or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise StateValidationError(f"private state record identity changed: {path}")


@contextlib.contextmanager
def _private_parent(path: Path, *, create: bool) -> Iterator[int]:
    """Borrow the transaction-pinned parent, or securely pin it for one operation."""
    target = Path(os.path.abspath(os.fspath(path)))
    active = _ACTIVE_STATE_IO.get()
    if active is not None:
        if target.parent == active.publication_path:
            yield active.publication_fd
            return
        if target.parent == active.root_path:
            yield active.root_fd
            return
        raise StateValidationError(f"state transaction attempted an unrelated path: {target}")
    descriptor = _open_private_directory(target.parent, create=create)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


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
    path = Path(os.path.abspath(os.fspath(path)))
    if not _requires_posix_metadata():
        before = _validate_private_file(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise StateValidationError(f"private state record changed during open: {path}")
            raw = os.read(descriptor, MAX_RECORD_BYTES + 1)
        finally:
            os.close(descriptor)
    else:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            with _private_parent(path, create=False) as parent_descriptor:
                descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
                try:
                    opened = _validate_private_descriptor(descriptor, path)
                    chunks: list[bytes] = []
                    total = 0
                    while total <= MAX_RECORD_BYTES:
                        chunk = os.read(
                            descriptor,
                            min(64 * 1024, MAX_RECORD_BYTES + 1 - total),
                        )
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                    after = _validate_private_descriptor(descriptor, path)
                    raw = b"".join(chunks)
                    if (
                        (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
                        or opened.st_size != after.st_size
                        or opened.st_mtime_ns != after.st_mtime_ns
                        or opened.st_ctime_ns != after.st_ctime_ns
                        or len(raw) != after.st_size
                    ):
                        raise StateValidationError(f"private state record changed during read: {path}")
                    _validate_named_identity(parent_descriptor, path, after)
                finally:
                    os.close(descriptor)
        except FileNotFoundError:
            raise
        except StateValidationError:
            raise
        except OSError as exc:
            raise StateValidationError(f"cannot securely read private state record: {path}") from exc
    if len(raw) > MAX_RECORD_BYTES:
        raise StateValidationError(f"private state record exceeds size limit: {path}")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate state key")
            result[key] = value
        return result

    def nonfinite(_value: str) -> Any:
        raise ValueError("non-finite state number")

    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise StateValidationError(f"private state record is not valid JSON: {path}") from exc
    if not isinstance(decoded, dict):
        raise StateValidationError(f"private state record is not a JSON object: {path}")
    return decoded


def atomic_write_record(path: os.PathLike[str] | str, record: dict[str, Any]) -> None:
    """Durably replace one fixed private record (file fsync then parent fsync)."""
    target = Path(os.path.abspath(os.fspath(path)))
    _ensure_private_directory(target.parent)
    data = _canonical_bytes(record)
    if not _requires_posix_metadata():
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
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return

    temporary_name = f".{target.name}.{secrets.token_hex(16)}.tmp"
    descriptor: int | None = None
    installed_descriptor: int | None = None
    temporary_identity: tuple[int, int] | None = None
    installed = False
    try:
        with _private_parent(target, create=True) as parent_descriptor:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            descriptor = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
            os.fchmod(descriptor, 0o600)
            created_details = _validate_private_descriptor(descriptor, target)
            temporary_identity = (created_details.st_dev, created_details.st_ino)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, data[offset:])
                if written <= 0:
                    raise StateValidationError("atomic state record write made no progress")
                offset += written
            os.fsync(descriptor)
            completed = _validate_private_descriptor(descriptor, target)
            if (
                (completed.st_dev, completed.st_ino) != temporary_identity
                or completed.st_size != len(data)
            ):
                raise StateValidationError("atomic state record changed during write")

            existing_descriptor: int | None = None
            try:
                existing_descriptor = os.open(
                    target.name,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=parent_descriptor,
                )
            except FileNotFoundError:
                existing_descriptor = None
            if existing_descriptor is not None:
                try:
                    existing = _validate_private_descriptor(existing_descriptor, target)
                    _validate_named_identity(parent_descriptor, target, existing)
                finally:
                    os.close(existing_descriptor)

            os.replace(
                temporary_name,
                target.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            installed = True
            installed_descriptor = os.open(
                target.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_descriptor,
            )
            installed_details = _validate_private_descriptor(installed_descriptor, target)
            if (installed_details.st_dev, installed_details.st_ino) != temporary_identity:
                raise StateValidationError("installed state record has unexpected identity")
            _validate_named_identity(parent_descriptor, target, installed_details)
            os.fsync(installed_descriptor)
            after_file_sync = _validate_private_descriptor(installed_descriptor, target)
            _validate_named_identity(parent_descriptor, target, after_file_sync)
            os.fsync(parent_descriptor)
            after_parent_sync = _validate_private_descriptor(installed_descriptor, target)
            _validate_named_identity(parent_descriptor, target, after_parent_sync)
    except StateValidationError:
        raise
    except OSError as exc:
        raise StateValidationError(f"cannot securely replace private state record: {target}") from exc
    finally:
        if installed_descriptor is not None:
            os.close(installed_descriptor)
        if descriptor is not None:
            os.close(descriptor)
        if not installed and temporary_identity is not None:
            try:
                with _private_parent(target, create=False) as parent_descriptor:
                    named = os.stat(
                        temporary_name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    if (named.st_dev, named.st_ino) == temporary_identity:
                        os.unlink(temporary_name, dir_fd=parent_descriptor)
            except (FileNotFoundError, StateValidationError, OSError):
                pass


def _unlink_record(path: Path) -> bool:
    path = Path(os.path.abspath(os.fspath(path)))
    if not _requires_posix_metadata():
        try:
            _validate_private_file(path)
        except FileNotFoundError:
            return False
        path.unlink()
        return True
    descriptor: int | None = None
    try:
        with _private_parent(path, create=False) as parent_descriptor:
            try:
                descriptor = os.open(
                    path.name,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=parent_descriptor,
                )
            except FileNotFoundError:
                return False
            opened = _validate_private_descriptor(descriptor, path)
            _validate_named_identity(parent_descriptor, path, opened)
            os.unlink(path.name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
            return True
    except FileNotFoundError:
        return False
    except StateValidationError:
        raise
    except OSError as exc:
        raise StateValidationError(f"cannot securely unlink private state record: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _validate_pinned_state_identity(pinned: _PinnedStateIO) -> None:
    root_details = _validate_private_directory_descriptor(pinned.root_fd, pinned.root_path)
    publication_details = _validate_private_directory_descriptor(
        pinned.publication_fd,
        pinned.publication_path,
    )
    try:
        named_publication = os.stat(
            pinned.publication_path.name,
            dir_fd=pinned.root_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise StateValidationError("publication directory identity changed") from exc
    if stat.S_ISLNK(named_publication.st_mode) or not _same_inode(
        named_publication,
        publication_details,
    ):
        raise StateValidationError("publication directory identity changed")

    reopened_root = _open_private_directory(pinned.root_path, create=False)
    try:
        reopened_details = _validate_private_directory_descriptor(
            reopened_root,
            pinned.root_path,
        )
        if not _same_inode(root_details, reopened_details):
            raise StateValidationError("state root directory identity changed")
    finally:
        os.close(reopened_root)


@contextlib.contextmanager
def _pin_state_io(paths: StatePaths) -> Iterator[_PinnedStateIO]:
    root_descriptor = _open_private_directory(paths.root, create=True)
    publication_descriptor: int | None = None
    try:
        publication_descriptor = _open_private_directory(paths.publication, create=True)
        pinned = _PinnedStateIO(
            root_path=paths.root,
            publication_path=paths.publication,
            root_fd=root_descriptor,
            publication_fd=publication_descriptor,
        )
        _validate_pinned_state_identity(pinned)
        yield pinned
    finally:
        if publication_descriptor is not None:
            os.close(publication_descriptor)
        os.close(root_descriptor)


@contextlib.contextmanager
def _acquire_pinned_lock(
    pinned: _PinnedStateIO,
    lock_path: Path,
    *,
    nonblocking: bool,
) -> Iterator[None]:
    import fcntl

    if lock_path.parent != pinned.publication_path:
        raise StateValidationError("publication lock is outside the pinned state directory")
    descriptor: int | None = None
    flags = (
        os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        try:
            descriptor = os.open(
                lock_path.name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=pinned.publication_fd,
            )
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.fsync(pinned.publication_fd)
        except FileExistsError:
            descriptor = os.open(
                lock_path.name,
                flags,
                dir_fd=pinned.publication_fd,
            )
        opened = _validate_private_descriptor(descriptor, lock_path)
        _validate_named_identity(pinned.publication_fd, lock_path, opened)
        lock_flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(descriptor, lock_flags)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise StateValidationError("publication state lock is already held") from exc
            raise
        after_lock = _validate_private_descriptor(descriptor, lock_path)
        _validate_named_identity(pinned.publication_fd, lock_path, after_lock)
        _validate_pinned_state_identity(pinned)
        yield
    except StateValidationError:
        raise
    except OSError as exc:
        raise StateValidationError("cannot securely acquire publication state lock") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextlib.contextmanager
def production_lock(path: os.PathLike[str] | str, *, nonblocking: bool = False) -> Iterator[None]:
    """Acquire a real POSIX flock with state paths pinned for its full lifetime."""
    if os.name != "posix":
        raise StateValidationError("production publication locking requires POSIX fcntl")
    lock_path = Path(os.path.abspath(os.fspath(path)))
    paths = state_paths(lock_path.parent.parent)
    if lock_path != paths.lock:
        raise StateValidationError("publication state lock path is invalid")
    with _pin_state_io(paths) as pinned:
        token = _ACTIVE_STATE_IO.set(pinned)
        try:
            with _acquire_pinned_lock(pinned, lock_path, nonblocking=nonblocking):
                yield
                _validate_pinned_state_identity(pinned)
        finally:
            _ACTIVE_STATE_IO.reset(token)


@contextlib.contextmanager
def _lock(paths: StatePaths, lock_context: Any | None) -> Iterator[None]:
    if not _requires_posix_metadata():
        _ensure_private_directory(paths.root)
        _ensure_private_directory(paths.publication)
        context = production_lock(paths.lock) if lock_context is None else lock_context
        with context:
            yield
        return

    with _pin_state_io(paths) as pinned:
        token = _ACTIVE_STATE_IO.set(pinned)
        try:
            context = (
                _acquire_pinned_lock(pinned, paths.lock, nonblocking=False)
                if lock_context is None
                else lock_context
            )
            with context:
                _validate_pinned_state_identity(pinned)
                yield
                _validate_pinned_state_identity(pinned)
        finally:
            _ACTIVE_STATE_IO.reset(token)


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


def _utc_datetime(value: Any, label: str) -> dt.datetime:
    canonical = _utc(value, label)
    return dt.datetime.strptime(canonical, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc,
    )


def _format_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )


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
        if reorg == value["confirmed_block_hash"]:
            raise StateValidationError(
                "canonical_reorg_from_hash cannot equal the confirmed block hash"
            )
    return value


def validate_coverage_proof_for_target(
    proof: Any,
    publication_target: Any,
) -> dict[str, Any]:
    """Validate one strict proof and require its snapshot to cover the target."""
    target = validate_latest(publication_target)
    coverage = _publication_coverage_module()
    try:
        validated = coverage.validate_coverage_proof(proof)
        if not coverage.coverage_proof_covers_observation(
            validated,
            target["observation"],
        ):
            raise coverage.CoverageValidationError(
                "publication snapshot does not cover the selected observation"
            )
    except coverage.CoverageValidationError as exc:
        raise StateValidationError(f"publication coverage proof is invalid: {exc}") from exc
    return validated


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
    captured_snapshot: PendingSnapshot | None = None,
    lock_context: Any | None = None,
) -> bool:
    """Legacy boolean clear, requiring a full captured immutable proof."""
    if captured_snapshot is None:
        return False
    captured = _validate_pending_snapshot(captured_snapshot)
    if captured["generation"] != generation or captured["commit_sha"] != commit_sha:
        raise StateValidationError("pending clear arguments differ from captured proof")
    paths = state_paths(lock_dir)
    with _lock(paths, lock_context):
        try:
            current = _validate_pending(_read_json(paths.pending))
        except FileNotFoundError:
            return False
        if current["generation"] != generation or current["commit_sha"] != commit_sha:
            return False
        if not _same_pending_immutable(current, captured):
            raise StateValidationError("pending immutable proof changed before legacy clear")
        if (
            current["retry_count"] < captured["retry_count"]
            or current["retry_deadline_utc"] < captured["retry_deadline_utc"]
        ):
            raise StateValidationError("pending retry state regressed before legacy clear")
        try:
            journal = _validate_journal(_read_json(paths.journal))
        except FileNotFoundError:
            journal = None
        if journal is not None and journal["publication_generation"] == current["generation"]:
            if (
                journal["queue_digest"] != current["queue_digest"]
                or journal["remote_commit"] != current["commit_sha"]
            ):
                raise StateValidationError("same-generation journal conflicts with pending clear")
            return False
        return _unlink_record(paths.pending)


_JOURNAL_BASE_KEYS = {
    "schema_version", "repo_realpath", "branch", "baseline_head", "run_id", "runner_id",
    "run_scope", "created_at_utc", "publish_paths", "publication_target",
}
_JOURNAL_ALIGNMENT_KEYS = {"alignment_runner_commit", "alignment_remote_head", "alignment_result"}
_JOURNAL_PROOF_KEYS = {
    "raw_status_path", "raw_bundle_path", "expected_bundle_sha256", "expected_bundle_bytes",
    "expected_block_number", "expected_block_hash", "push_completed_at_utc", "retry_deadline_utc",
    "retry_count",
}
_JOURNAL_DEFERRED_KEYS = _JOURNAL_BASE_KEYS | _JOURNAL_ALIGNMENT_KEYS | _JOURNAL_PROOF_KEYS | {
    "publication_generation", "queue_digest", "coverage_proof", "terminal_outcome",
    "handoff_phase", "remote_commit",
}
_CHECKPOINT_KEYS = {
    "schema_version", "outcome", "generation", "queue_digest", "commit_sha",
    "push_completed_at_utc", "publication_target", "coverage_proof",
}
_PENDING_KEYS = {
    "schema_version", "generation", "queue_digest", "commit_sha", "raw_status_path", "raw_bundle_path",
    "expected_bundle_sha256", "expected_bundle_bytes", "expected_block_number", "expected_block_hash",
    "push_completed_at_utc", "retry_deadline_utc", "retry_count",
}
_PENDING_MUTABLE_KEYS = {"retry_deadline_utc", "retry_count"}
_PENDING_IMMUTABLE_KEYS = _PENDING_KEYS - _PENDING_MUTABLE_KEYS
_PAGES_VERIFIED_KEYS = _PENDING_IMMUTABLE_KEYS | {
    "pending_proof_fingerprint",
    "pages_verified_at_utc",
}


def _validate_journal(
    value: Any,
    *,
    allow_unbound_publication_target: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise StateValidationError("recovery journal schema is invalid")
    expected_keys = _JOURNAL_DEFERRED_KEYS
    if allow_unbound_publication_target and "publication_target" not in value:
        expected_keys = expected_keys - {"publication_target"}
    _require_exact_keys(value, expected_keys, "recovery journal")
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
    target: dict[str, Any] | None = None
    if "publication_target" in value:
        target = validate_latest(value["publication_target"])
        if target["generation"] != value["publication_generation"]:
            raise StateValidationError("journal publication target generation differs from queue identity")
        if _digest(target) != value["queue_digest"]:
            raise StateValidationError("journal publication target digest differs from queue identity")
    elif not allow_unbound_publication_target:
        raise StateValidationError("recovery journal lacks its publication target")
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
    coverage_proof = value["coverage_proof"]
    if phase == "generating":
        if (
            outcome is not None
            or remote_commit is not None
            or not proof_is_empty
            or coverage_proof is not None
        ):
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
    if phase != "generating":
        if target is None or coverage_proof is None:
            raise StateValidationError("terminal journal lacks publication coverage evidence")
        validated_proof = validate_coverage_proof_for_target(coverage_proof, target)
        source_kind = validated_proof["source_kind"]
        source_commit = validated_proof["source_commit_sha"]
        if outcome == "pushed" and (
            source_kind != "generated_commit" or source_commit != remote_commit
        ):
            raise StateValidationError("pushed journal coverage source differs from publisher commit")
        if outcome == "no_diff" and (
            source_kind != "baseline_no_diff" or source_commit != value["baseline_head"]
        ):
            raise StateValidationError("no-diff journal coverage source differs from baseline")
        if outcome == "peer_superseded" and (
            source_kind != "peer_commit" or source_commit != remote_commit
        ):
            raise StateValidationError("peer journal coverage source differs from peer commit")
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
    pushed_at = _utc_datetime(value["push_completed_at_utc"], "pending push completion")
    retry_at = _utc_datetime(value["retry_deadline_utc"], "pending retry deadline")
    retry_count = _integer(value["retry_count"], "pending retry count")
    if retry_count == 0:
        if retry_at != pushed_at + dt.timedelta(minutes=10):
            raise StateValidationError("initial pending retry deadline is not push plus ten minutes")
    elif retry_at < pushed_at:
        raise StateValidationError("pending retry deadline predates push completion")
    return value


def _same_pending_immutable(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left[key] == right[key] for key in _PENDING_IMMUTABLE_KEYS)


def _pending_proof_fingerprint(record: dict[str, Any]) -> str:
    validated = _validate_pending(record)
    proof = {key: validated[key] for key in _PENDING_IMMUTABLE_KEYS}
    return hashlib.sha256(_canonical_bytes(proof)).hexdigest()


def _validate_pending_snapshot(snapshot: PendingSnapshot) -> dict[str, Any]:
    if not isinstance(snapshot, PendingSnapshot):
        raise StateValidationError("pending snapshot type is invalid")
    record = _validate_pending(dict(snapshot.record))
    if snapshot.record_digest != _digest(record):
        raise StateValidationError("pending snapshot record digest is invalid")
    if snapshot.proof_fingerprint != _pending_proof_fingerprint(record):
        raise StateValidationError("pending snapshot proof fingerprint is invalid")
    return record


def read_pending_with_digest(
    lock_dir: os.PathLike[str] | str,
    *,
    lock_context: Any | None = None,
) -> PendingSnapshot | None:
    """Capture one validated pending record while holding only state.lock."""
    paths = state_paths(lock_dir)
    with _lock(paths, lock_context):
        try:
            record = _validate_pending(_read_json(paths.pending))
        except FileNotFoundError:
            return None
        captured = dict(record)
        return PendingSnapshot(
            record=captured,
            record_digest=_digest(captured),
            proof_fingerprint=_pending_proof_fingerprint(captured),
        )


def pages_verified_receipt(
    snapshot: PendingSnapshot,
    pages_verified_at_utc: str,
) -> dict[str, Any]:
    """Build the fixed durable health receipt for one immutable pending proof."""
    record = _validate_pending_snapshot(snapshot)
    verified_at = _utc(pages_verified_at_utc, "Pages verification time")
    receipt = {key: record[key] for key in _PENDING_IMMUTABLE_KEYS}
    receipt["pending_proof_fingerprint"] = snapshot.proof_fingerprint
    receipt["pages_verified_at_utc"] = verified_at
    return _validate_pages_verified(receipt)


def _validate_pages_verified(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StateValidationError("Pages verification receipt must be an object")
    _require_exact_keys(value, _PAGES_VERIFIED_KEYS, "Pages verification receipt")
    pending = {key: value[key] for key in _PENDING_IMMUTABLE_KEYS}
    pushed_at = _utc_datetime(value["push_completed_at_utc"], "pending push completion")
    pending["retry_deadline_utc"] = _format_utc(pushed_at + dt.timedelta(minutes=10))
    pending["retry_count"] = 0
    _validate_pending(pending)
    expected_fingerprint = _pending_proof_fingerprint(pending)
    if value["pending_proof_fingerprint"] != expected_fingerprint:
        raise StateValidationError("Pages verification receipt proof fingerprint is invalid")
    verified_at = _utc(value["pages_verified_at_utc"], "Pages verification time")
    if verified_at < value["push_completed_at_utc"]:
        raise StateValidationError("Pages verification receipt predates push completion")
    return value


def read_pages_verified_receipt(
    lock_dir: os.PathLike[str] | str,
    *,
    lock_context: Any | None = None,
) -> dict[str, Any] | None:
    """Read the protected monotonic Pages verification health receipt."""
    paths = state_paths(lock_dir)
    with _lock(paths, lock_context):
        try:
            return dict(_validate_pages_verified(_read_json(paths.pages_verified)))
        except FileNotFoundError:
            return None


def read_publication_health_snapshot(
    lock_dir: os.PathLike[str] | str,
    *,
    lock_context: Any | None = None,
) -> dict[str, Any]:
    """Read all fixed publication-health records under one state-lock view."""
    paths = state_paths(lock_dir)

    def optional(path: Path, validator: Callable[[Any], dict[str, Any]]) -> dict[str, Any] | None:
        try:
            return dict(validator(_read_json(path)))
        except FileNotFoundError:
            return None

    with _lock(paths, lock_context):
        latest_record_value = optional(paths.latest, validate_latest)
        latest = None
        if latest_record_value is not None:
            latest = {
                "record": latest_record_value,
                "record_digest": _digest(latest_record_value),
            }

        pending_record = optional(paths.pending, _validate_pending)
        pending = None
        if pending_record is not None:
            pending = {
                "record": pending_record,
                "record_digest": _digest(pending_record),
                "proof_fingerprint": _pending_proof_fingerprint(pending_record),
            }
        checkpoint = optional(paths.checkpoint, _validate_checkpoint)
        verified = optional(paths.pages_verified, _validate_pages_verified)
        journal = optional(paths.journal, _validate_journal)
        last_generation = _read_generation_watermark(paths.sequence)

        generations = [
            record["generation"]
            for record in (
                latest_record_value,
                pending_record,
                checkpoint,
                verified,
            )
            if record is not None
        ]
        if journal is not None:
            generations.append(journal["publication_generation"])
        if generations and max(generations) > last_generation:
            raise StateValidationError("publication generation watermark predates durable state")

        if latest is not None and pending_record is not None:
            if latest_record_value["generation"] == pending_record["generation"] and (
                latest["record_digest"] != pending_record["queue_digest"]
            ):
                raise StateValidationError("same-generation latest and pending identities conflict")
        if pending_record is not None and checkpoint is not None:
            if pending_record["generation"] == checkpoint["generation"] and (
                checkpoint["outcome"] != "pushed"
                or checkpoint["queue_digest"] != pending_record["queue_digest"]
                or checkpoint["commit_sha"] != pending_record["commit_sha"]
                or checkpoint["push_completed_at_utc"] != pending_record["push_completed_at_utc"]
            ):
                raise StateValidationError("same-generation pending and checkpoint identities conflict")
        if pending_record is not None and verified is not None:
            if pending_record["generation"] == verified["generation"] and not _receipt_matches_pending(
                verified, pending_record
            ):
                raise StateValidationError("same-generation pending and Pages receipt identities conflict")
        if checkpoint is not None and verified is not None:
            if checkpoint["generation"] == verified["generation"] and (
                checkpoint["outcome"] != "pushed"
                or checkpoint["queue_digest"] != verified["queue_digest"]
                or checkpoint["commit_sha"] != verified["commit_sha"]
                or checkpoint["push_completed_at_utc"] != verified["push_completed_at_utc"]
            ):
                raise StateValidationError("same-generation checkpoint and Pages receipt identities conflict")
        if journal is not None and pending_record is not None:
            if journal["publication_generation"] == pending_record["generation"]:
                if journal["handoff_phase"] != "raw_proven" or journal["terminal_outcome"] != "pushed":
                    raise StateValidationError("same-generation journal cannot authenticate pending state")
                _authenticate_pending_from_journal(journal, pending_record)
        if journal is not None and checkpoint is not None:
            if journal["publication_generation"] == checkpoint["generation"]:
                _authenticate_checkpoint_from_journal(journal, checkpoint)

        return {
            "latest": latest,
            "pending": pending,
            "checkpoint": checkpoint,
            "pages_verified": verified,
            "journal": journal,
            "last_generation": last_generation,
        }


def _validate_checkpoint(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StateValidationError("checkpoint record must be an object")
    _require_exact_keys(value, _CHECKPOINT_KEYS, "checkpoint record")
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
    target = validate_latest(value["publication_target"])
    if target["generation"] != value["generation"] or _digest(target) != value["queue_digest"]:
        raise StateValidationError("checkpoint publication target differs from queue identity")
    proof = validate_coverage_proof_for_target(value["coverage_proof"], target)
    source_kind = proof["source_kind"]
    source_commit = proof["source_commit_sha"]
    if value["outcome"] == "pushed" and (
        source_kind != "generated_commit" or source_commit != value["commit_sha"]
    ):
        raise StateValidationError("pushed checkpoint coverage source differs from commit")
    if value["outcome"] == "no_diff" and source_kind != "baseline_no_diff":
        raise StateValidationError("no-diff checkpoint coverage source is not the baseline")
    if value["outcome"] == "peer_superseded" and (
        source_kind != "peer_commit" or source_commit != value["commit_sha"]
    ):
        raise StateValidationError("peer checkpoint coverage source differs from commit")
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
    journal_template = _validate_journal(
        journal,
        allow_unbound_publication_target=True,
    )
    if journal_template["handoff_phase"] != "generating":
        raise StateValidationError("new deferred recovery journal must begin in generating phase")
    with _lock(paths, lock_context):
        try:
            _read_json(paths.journal)
        except FileNotFoundError:
            selected = read_latest_with_digest(lock_dir)
            if selected is None:
                raise StateValidationError("deferred recovery journal has no selected latest record")
            selected_record, selected_digest = selected
            if (
                selected_record["generation"] != journal_template["publication_generation"]
                or selected_digest != journal_template["queue_digest"]
            ):
                raise StateValidationError(
                    "deferred recovery journal identity differs from the selected latest record"
                )
            bound = dict(journal_template)
            supplied_target = bound.get("publication_target")
            if supplied_target is not None and _canonical_bytes(supplied_target) != _canonical_bytes(selected_record):
                raise StateValidationError(
                    "deferred recovery journal supplied a conflicting publication target"
                )
            bound["publication_target"] = selected_record
            bound = _validate_journal(bound)
            atomic_write_record(paths.journal, bound)
            return
        raise StateValidationError("deferred recovery journal already exists")


def arm_deferred_pushed_handoff(
    lock_dir: os.PathLike[str] | str,
    generation: int,
    digest: str,
    commit_sha: str,
    coverage_proof: dict[str, Any],
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
        armed["coverage_proof"] = validate_coverage_proof_for_target(
            coverage_proof,
            journal["publication_target"],
        )
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
    coverage_proof: dict[str, Any] | None = None,
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
        updated = _aligned_journal(
            journal,
            runner_commit,
            remote_head,
            alignment_result,
            coverage_proof,
        )
        atomic_write_record(paths.journal, updated)
        return updated


def record_deferred_push_rejected_alignment(
    lock_dir: os.PathLike[str] | str,
    generation: int,
    digest: str,
    runner_commit: str,
    remote_head: str,
    alignment_result: str,
    coverage_proof: dict[str, Any] | None = None,
    *,
    lock_context: Any | None = None,
) -> dict[str, Any]:
    """Transition exact push-ready evidence after a freshly classified rejected CAS."""
    paths = state_paths(lock_dir)
    with _lock(paths, lock_context):
        journal = _validate_journal(_read_json(paths.journal))
        if (
            journal["handoff_phase"] != "push_ready"
            or journal["publication_generation"] != generation
            or journal["queue_digest"] != digest
            or journal["remote_commit"] != runner_commit
        ):
            raise StateValidationError("rejected push does not match exact push-ready journal identity")
        if remote_head in {runner_commit, journal["baseline_head"]}:
            raise StateValidationError("rejected push remote is not a distinct sibling target")
        updated = _aligned_journal(
            journal,
            runner_commit,
            remote_head,
            alignment_result,
            coverage_proof,
        )
        atomic_write_record(paths.journal, updated)
        return updated


def _aligned_journal(
    journal: dict[str, Any],
    runner_commit: str,
    remote_head: str,
    alignment_result: str,
    coverage_proof: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(runner_commit, str) or not _SHA_40.fullmatch(runner_commit):
        raise StateValidationError("alignment runner commit is invalid")
    if not isinstance(remote_head, str) or not _SHA_40.fullmatch(remote_head):
        raise StateValidationError("alignment remote commit is invalid")
    if alignment_result not in {"peer_supersedes", "regenerate"}:
        raise StateValidationError("alignment result is invalid")
    updated = dict(journal)
    updated["alignment_runner_commit"] = runner_commit
    updated["alignment_remote_head"] = remote_head
    updated["alignment_result"] = alignment_result
    for key in _JOURNAL_PROOF_KEYS:
        updated[key] = None
    if alignment_result == "peer_supersedes":
        if coverage_proof is None:
            raise StateValidationError("peer supersession lacks publication coverage proof")
        updated["terminal_outcome"] = "peer_superseded"
        updated["handoff_phase"] = "terminal"
        updated["remote_commit"] = remote_head
        updated["coverage_proof"] = validate_coverage_proof_for_target(
            coverage_proof,
            updated["publication_target"],
        )
    else:
        if coverage_proof is not None:
            raise StateValidationError("regeneration alignment must not retain a coverage proof")
        updated["terminal_outcome"] = None
        updated["handoff_phase"] = "generating"
        updated["remote_commit"] = None
        updated["coverage_proof"] = None
    return _validate_journal(updated)


def _raw_proven_journal(journal: dict[str, Any], pending: dict[str, Any]) -> dict[str, Any]:
    _authenticate_pending_coverage(journal, pending)
    proven = dict(journal)
    proven["handoff_phase"] = "raw_proven"
    for key in _JOURNAL_PROOF_KEYS:
        proven[key] = pending[key]
    return _validate_journal(proven)


def _authenticate_pending_coverage(
    journal: dict[str, Any],
    pending: dict[str, Any],
) -> None:
    """Bind verifier handoff metadata to the exact causal coverage proof."""
    proof = validate_coverage_proof_for_target(
        journal["coverage_proof"],
        journal["publication_target"],
    )
    expected = {
        "commit_sha": proof["source_commit_sha"],
        "raw_status_path": proof["status_path"],
        "raw_bundle_path": proof["bundle_path"],
        "expected_bundle_sha256": proof["bundle_sha256"],
        "expected_bundle_bytes": proof["bundle_bytes"],
        "expected_block_number": proof["block_number"],
        "expected_block_hash": proof["block_hash"],
    }
    if any(pending[key] != value for key, value in expected.items()):
        raise StateValidationError("pending metadata differs from publication coverage proof")


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
        "publication_target": journal["publication_target"],
        "coverage_proof": journal["coverage_proof"],
    })


def _authenticate_pending_from_journal(journal: dict[str, Any], pending: dict[str, Any]) -> None:
    _authenticate_pending_coverage(journal, pending)
    expected = _pending_from_journal(journal)
    if not _same_pending_immutable(expected, pending):
        raise StateValidationError("pending immutable proof differs from raw-proven journal")
    if (
        pending["retry_count"] < expected["retry_count"]
        or pending["retry_deadline_utc"] < expected["retry_deadline_utc"]
    ):
        raise StateValidationError("pending retry state predates raw-proven journal")


def _authenticate_checkpoint_from_journal(journal: dict[str, Any], checkpoint: dict[str, Any]) -> None:
    expected = _checkpoint_from_journal(journal)
    if _canonical_bytes(checkpoint) != _canonical_bytes(expected):
        raise StateValidationError("checkpoint differs from authenticated recovery journal")


def _receipt_matches_pending(receipt: dict[str, Any], pending: dict[str, Any]) -> bool:
    return all(receipt[key] == pending[key] for key in _PENDING_IMMUTABLE_KEYS)


def finalize_verified_pending(
    lock_dir: os.PathLike[str] | str,
    captured: PendingSnapshot,
    pages_verified_at_utc: str,
    *,
    lock_context: Any | None = None,
) -> PendingFinalizeResult:
    """Install a durable receipt, then clear only the captured immutable proof.

    Retry-only progress may advance while HTTP is in flight.  Every immutable
    field stays authenticated against the captured snapshot, and a matching
    Task 4 recovery journal blocks rather than races the verifier.
    """
    captured_record = _validate_pending_snapshot(captured)
    receipt_to_install = pages_verified_receipt(captured, pages_verified_at_utc)
    paths = state_paths(lock_dir)
    with _lock(paths, lock_context):
        try:
            current = _validate_pending(_read_json(paths.pending))
        except FileNotFoundError:
            return PendingFinalizeResult.SUPERSEDED_OR_ABSENT

        # A fixed recovery journal is part of the same protected state view.
        # Validate it even when pending was superseded; supersession must not
        # turn malformed durable state into a benign result.
        try:
            journal = _validate_journal(_read_json(paths.journal))
        except FileNotFoundError:
            journal = None

        captured_generation = captured_record["generation"]
        current_generation = current["generation"]
        matching_journal = False
        if journal is not None:
            journal_generation = journal["publication_generation"]
            if journal_generation < current_generation:
                raise StateValidationError("recovery journal generation predates pending verification")
            if journal_generation == current_generation:
                if (
                    journal["queue_digest"] != current["queue_digest"]
                    or journal["remote_commit"] != current["commit_sha"]
                    or journal["handoff_phase"] != "raw_proven"
                    or journal["terminal_outcome"] != "pushed"
                ):
                    raise StateValidationError("same-generation recovery journal conflicts with pending verification")
                _authenticate_pending_from_journal(journal, current)
                matching_journal = True
        if current_generation > captured_generation:
            return PendingFinalizeResult.SUPERSEDED_OR_ABSENT
        if current_generation < captured_generation:
            raise StateValidationError("pending generation regressed during Pages verification")
        if not _same_pending_immutable(current, captured_record):
            raise StateValidationError("pending immutable proof changed during Pages verification")
        if (
            current["retry_count"] < captured_record["retry_count"]
            or current["retry_deadline_utc"] < captured_record["retry_deadline_utc"]
        ):
            raise StateValidationError("pending retry state regressed during Pages verification")

        if matching_journal:
            return PendingFinalizeResult.BLOCKED_MATCHING_JOURNAL

        try:
            existing_receipt = _validate_pages_verified(_read_json(paths.pages_verified))
        except FileNotFoundError:
            existing_receipt = None
        if existing_receipt is not None:
            existing_generation = existing_receipt["generation"]
            if existing_generation == current_generation:
                if not _receipt_matches_pending(existing_receipt, current):
                    raise StateValidationError("equal-generation Pages receipt conflicts with pending proof")
            elif existing_generation < current_generation:
                atomic_write_record(paths.pages_verified, receipt_to_install)
            else:
                # A newer verified receipt is monotonic authority; never regress it.
                pass
        else:
            atomic_write_record(paths.pages_verified, receipt_to_install)

        if not _unlink_record(paths.pending):
            raise StateValidationError("pending record vanished before authenticated unlink")
        return PendingFinalizeResult.CLEARED


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
    elif journal["handoff_phase"] == "raw_proven":
        proven = _raw_proven_journal(journal, pending)
        if _canonical_bytes(persisted) != _canonical_bytes(proven):
            raise StateValidationError("durable raw proof differs from reconstructed handoff")
    else:
        raise StateValidationError("pushed handoff journal is not push-ready or raw-proven")
    _authenticate_checkpoint_from_journal(proven, checkpoint)
    if journal["handoff_phase"] == "push_ready":
        atomic_write_record(paths.journal, proven)
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
    _authenticate_checkpoint_from_journal(journal, checkpoint)
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
        if journal["publication_generation"] != generation or journal["queue_digest"] != digest:
            raise StateValidationError("finalization generation/digest differs from recovery journal")
        if journal["terminal_outcome"] == "pushed":
            if journal["handoff_phase"] != "raw_proven":
                raise StateValidationError("pushed finalization requires durable raw proof")
            pending = _validate_pending(_read_json(paths.pending))
            _authenticate_pending_from_journal(journal, pending)
        elif journal["handoff_phase"] != "terminal":
            raise StateValidationError("terminal finalization journal is in the wrong phase")
        _authenticate_checkpoint_from_journal(journal, checkpoint)
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
