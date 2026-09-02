#!/usr/bin/env python3
"""Drain the WSL latest-wins publication queue under the refresh lock.

The Bash publisher remains the only recovery and Git mutation engine.  This
process owns the fixed refresh lock, passes that exact open file description to
the fixed Bash entrypoint, and performs only the authenticated Task 4
finalization transition after a successful child exit.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import errno
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from runner_publication_state import (
    finalize_pushed_handoff,
    read_deferred_recovery_journal,
    read_latest_with_digest,
)


DEFAULT_RUNTIME_BUDGET_SECONDS = 240.0
DEFAULT_LOCK_WAIT_SECONDS = 0.5
DEFAULT_LOCK_POLL_SECONDS = 0.05
DEFAULT_CLEANUP_GRACE_SECONDS = 10.0
DEFAULT_TERMINATION_GRACE_SECONDS = 3.0
DEFAULT_FOLLOWUP_RESERVE_SECONDS = 20.0
DEFAULT_KILL_POLL_SECONDS = 0.02
EXIT_FAILURE = 1
EXIT_CONFIG = 78
EXIT_TERMINATED = 128 + 15
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*\Z")
_MAX_REFRESH_STATUS_BYTES = 64 * 1024


class ConfigurationError(RuntimeError):
    """The fixed WSL runner paths or budget are invalid."""


class SecurePathError(RuntimeError):
    """The refresh lock failed descriptor-pinned path validation."""


class TerminationRequested(BaseException):
    """Raised by the narrow SIGTERM handler so the child group is reaped."""


@dataclasses.dataclass(frozen=True)
class PublisherResult:
    kind: str
    returncode: int | None


def _path_security_module() -> Any:
    # runner_path_security intentionally requires POSIX descriptor operations
    # and evaluates os.getuid() at import time.  Keep this lazy so the portable
    # state/logic tests can run under native Windows Python.
    import runner_path_security

    return runner_path_security


def inspect_existing_private_file(
    path: os.PathLike[str] | str,
    *,
    require_private_mode: bool = True,
) -> os.stat_result | None:
    security = _path_security_module()
    try:
        return security.inspect_existing_private_file(
            path,
            require_private_mode=require_private_mode,
        )
    except security.SecurePathError as exc:
        raise SecurePathError(str(exc)) from exc


def open_private_lock(path: os.PathLike[str] | str) -> int:
    security = _path_security_module()
    try:
        return security.open_private_lock(path)
    except security.SecurePathError as exc:
        raise SecurePathError(str(exc)) from exc


def open_existing_private_file(
    path: os.PathLike[str] | str,
    *,
    writable: bool = False,
) -> int:
    security = _path_security_module()
    try:
        return security.open_existing_private_file(path, writable=writable)
    except security.SecurePathError as exc:
        raise SecurePathError(str(exc)) from exc


def _is_drvfs_path(path: Path) -> bool:
    text = os.fspath(path)
    return text == "/mnt" or text.startswith("/mnt/")


def validate_runtime_paths(
    repo_dir: os.PathLike[str] | str,
    lock_dir: os.PathLike[str] | str,
    refresh_lock_path: os.PathLike[str] | str,
) -> tuple[Path, Path, Path]:
    """Validate fixed native paths without resolving the lock-file target."""

    raw_repo = os.fspath(repo_dir)
    raw_lock_root = os.fspath(lock_dir)
    raw_refresh_lock = os.fspath(refresh_lock_path)
    if not all(os.path.isabs(value) for value in (raw_repo, raw_lock_root, raw_refresh_lock)):
        raise ConfigurationError("runner paths must be absolute")
    repo = Path(os.path.abspath(raw_repo))
    lock_root = Path(os.path.abspath(raw_lock_root))
    refresh_lock = Path(os.path.abspath(raw_refresh_lock))
    if refresh_lock != lock_root / "refresh.lock":
        raise ConfigurationError("refresh lock must be the fixed file below the lock directory")
    if os.name != "posix":
        return repo, lock_root, refresh_lock
    # Resolve directory components only to detect a canonical checkout or state
    # directory redirected onto DrvFS.  The refresh.lock path itself is never
    # resolved or followed; owned_refresh_lock opens it with descriptor-relative
    # O_NOFOLLOW operations.
    canonical_repo = Path(os.path.realpath(repo))
    canonical_lock_root = Path(os.path.realpath(lock_root))
    if any(
        _is_drvfs_path(path)
        for path in (repo, canonical_repo, lock_root, canonical_lock_root, refresh_lock)
    ):
        raise ConfigurationError("runner paths must stay on the WSL native filesystem")
    return canonical_repo, lock_root, refresh_lock


def _validate_lock_details(
    details: os.stat_result,
    *,
    label: str,
    expected: os.stat_result | None = None,
) -> None:
    if not stat.S_ISREG(details.st_mode):
        raise SecurePathError(f"{label} is not a regular file")
    if details.st_uid != os.getuid():
        raise SecurePathError(f"{label} is not owned by the runner user")
    if details.st_nlink != 1:
        raise SecurePathError(f"{label} has an unexpected link count")
    if stat.S_IMODE(details.st_mode) != 0o600:
        raise SecurePathError(f"{label} mode is not exactly 0600")
    if expected is not None:
        identity = (
            details.st_dev,
            details.st_ino,
            details.st_uid,
            stat.S_IFMT(details.st_mode),
            details.st_nlink,
            stat.S_IMODE(details.st_mode),
        )
        expected_identity = (
            expected.st_dev,
            expected.st_ino,
            expected.st_uid,
            stat.S_IFMT(expected.st_mode),
            expected.st_nlink,
            stat.S_IMODE(expected.st_mode),
        )
        if identity != expected_identity:
            raise SecurePathError("refresh lock identity changed across descriptor opens")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise SecurePathError("refresh lock metadata write made no progress")
        offset += written


@contextlib.contextmanager
def owned_refresh_lock(
    path: os.PathLike[str] | str,
    *,
    wait_seconds: float,
    poll_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    started_at_utc: str,
) -> Iterator[int | None]:
    """Yield the exact owned refresh-lock FD, or ``None`` on benign contention."""

    if os.name != "posix":
        raise ConfigurationError("the queued publisher requires POSIX flock support")
    if wait_seconds < 0 or poll_seconds <= 0:
        raise ConfigurationError("lock wait values are invalid")
    import fcntl

    lock_path = Path(path)
    # open_private_lock deliberately hardens an existing file.  Inspect first so
    # an already-broad or otherwise unsafe lock is rejected without mutation.
    before = inspect_existing_private_file(lock_path, require_private_mode=True)
    descriptor: int | None = None
    try:
        descriptor = open_private_lock(lock_path)
        primary = os.fstat(descriptor)
        _validate_lock_details(primary, label="refresh lock")
        if before is not None:
            _validate_lock_details(primary, label="refresh lock", expected=before)

        deadline = monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                remaining = deadline - monotonic()
                if remaining <= 0:
                    yield None
                    return
                sleep(min(poll_seconds, remaining))

        # Reopen only after ownership and authenticate every relevant metadata
        # field.  The same OFD must re-flock successfully, while a separately
        # reopened OFD must remain blocked.
        reopened = open_existing_private_file(lock_path, writable=True)
        try:
            reopened_details = os.fstat(reopened)
            _validate_lock_details(reopened_details, label="reopened refresh lock", expected=primary)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fcntl.flock(reopened, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
            else:
                raise SecurePathError("separately reopened refresh lock was not blocked")
        finally:
            os.close(reopened)

        metadata = (
            f"publisher_pid={os.getpid()}\n"
            f"publisher_started_at_utc={started_at_utc}\n"
        ).encode("ascii")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        _write_all(descriptor, metadata)
        os.fsync(descriptor)
        os.set_inheritable(descriptor, False)
        yield descriptor
    finally:
        if descriptor is not None:
            try:
                os.set_inheritable(descriptor, False)
            except OSError:
                pass
            # Release by close only.  Explicit unlock would also release the
            # shared OFD lock from an unexpected surviving inherited duplicate.
            os.close(descriptor)


_DYNAMIC_EXACT = {
    "DEGEN_DOGS_RUN_ID",
    "DEGEN_DOGS_REFRESH_RUN_ID",
    "DEGEN_DOGS_REFRESH_RESULT",
    "DEGEN_DOGS_REFRESH_ERROR",
    "DEGEN_DOGS_COMMIT_SHA",
    "DEGEN_DOGS_CHANGED_FILES",
    "DEGEN_DOGS_RAW_COMMIT_URL",
    "DEGEN_DOGS_RAW_COMMIT_VERIFIED",
    "DEGEN_DOGS_REFRESH_REASON",
    "DEGEN_DOGS_REFRESH_REASONS",
    "DEGEN_DOGS_RUN_SCOPE",
    "DEGEN_DOGS_LOCK_HELD",
    "DEGEN_DOGS_LOCK_FD",
    "DEGEN_DOGS_PUBLICATION_GENERATION",
    "DEGEN_DOGS_PUBLICATION_DIGEST",
    "DEGEN_DOGS_SUPERSESSION_RETRY_COUNT",
}
_DYNAMIC_PREFIXES = (
    "DEGEN_DOGS_EVENT_",
    "DEGEN_DOGS_OBSERVED_",
    "DEGEN_DOGS_DETECTED_",
    "DEGEN_DOGS_CONFIRMED_",
    "DEGEN_DOGS_BLOCK_TO_",
    "DEGEN_DOGS_PUBLICATION_",
    "DEGEN_DOGS_QUEUE_",
    "DEGEN_DOGS_PUSH_TO_",
    "DEGEN_DOGS_RAW_",
)


def _is_dynamic_child_field(name: str) -> bool:
    if name == "MISSION3_REFRESH_COMMAND" or name in _DYNAMIC_EXACT:
        return True
    if any(name.startswith(prefix) for prefix in _DYNAMIC_PREFIXES):
        return True
    if name.startswith("DEGEN_DOGS_") and (
        name.endswith("_AT_UTC")
        or name.endswith("_DURATION_SECONDS")
        or name.startswith("DEGEN_DOGS_LIVE_VERIFY_")
    ):
        return True
    return False


def _canonical_target_token(publication_target: Mapping[str, Any]) -> int:
    """Return the selected queue token, rejecting anything that is not canonical."""

    observation = publication_target.get("observation")
    if not isinstance(observation, Mapping):
        raise RuntimeError("publication target observation is invalid")
    token = observation.get("token_id")
    if not isinstance(token, str) or not _CANONICAL_POSITIVE_DECIMAL.fullmatch(token):
        raise RuntimeError("publication target token is invalid")
    return int(token)


def incremental_archive_required(
    repo_dir: Path,
    publication_target: Mapping[str, Any],
) -> bool:
    """Fail closed unless the fixed committed baseline matches the selected token."""

    target_token = _canonical_target_token(publication_target)
    try:
        with (repo_dir / "generated" / "refresh_status.json").open("rb") as status_file:
            payload = status_file.read(_MAX_REFRESH_STATUS_BYTES + 1)
        if len(payload) > _MAX_REFRESH_STATUS_BYTES:
            return True
        status = json.loads(payload)
        if not isinstance(status, Mapping):
            return True
        baseline = status.get("current_dog_token_id")
        if isinstance(baseline, bool) or not isinstance(baseline, int) or baseline < 1:
            return True
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return True
    return target_token != baseline


def sanitized_publisher_environment(
    base_env: Mapping[str, str],
    *,
    repo_dir: Path,
    lock_dir: Path,
    refresh_lock_path: Path,
    descriptor: int,
    generation: int,
    digest: str,
    publication_target: Mapping[str, Any] | None = None,
    force_archive: bool = False,
) -> dict[str, str]:
    archive_required = force_archive
    if publication_target is None:
        # Isolated fixed-entrypoint tests do not select a queue target.  Their
        # only safe environment is the conservative archive path.
        archive_required = True
    else:
        archive_required = incremental_archive_required(repo_dir, publication_target) or archive_required
    environment = {
        str(key): str(value)
        for key, value in base_env.items()
        if not _is_dynamic_child_field(str(key))
    }
    environment.update(
        {
            "DEGEN_DOGS_REPO_DIR": str(repo_dir),
            "DEGEN_DOGS_LOCK_HELD": "1",
            "DEGEN_DOGS_LOCK_FD": str(descriptor),
            "DEGEN_DOGS_REFRESH_LOCK_PATH": str(refresh_lock_path),
            "DEGEN_DOGS_LOCK_DIR": str(lock_dir),
            "DEGEN_DOGS_DEFER_PAGES_VERIFICATION": "1",
            "DEGEN_DOGS_PUBLICATION_GENERATION": str(generation),
            "DEGEN_DOGS_PUBLICATION_DIGEST": digest,
            "DEGEN_DOGS_FULL_REFRESH": "0",
            "DEGEN_DOGS_RUN_MISSION3_ARCHIVE": "1" if archive_required else "0",
            "DEGEN_DOGS_SKIP_PUSH": "0",
            "DEGEN_DOGS_SKIP_PULL": "0",
            "DEGEN_DOGS_SUPERSESSION_RETRY_COUNT": "0",
            "DEGEN_DOGS_REFRESH_TRIGGER": "publication_queue",
        }
    )
    return environment


def _process_group_exists(process_group: int) -> bool:
    kill_group = getattr(os, "killpg")
    try:
        kill_group(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise RuntimeError("cannot prove publisher process group termination") from exc
    return True


def terminate_publisher_process_group(
    process: Any,
    *,
    termination_grace_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    kill_poll_seconds: float,
) -> None:
    """Terminate and reap every process in the publisher's new process group."""

    kill_group = getattr(os, "killpg")
    term_signal = getattr(signal, "SIGTERM")
    kill_signal = getattr(signal, "SIGKILL")
    process_group = process.pid
    try:
        kill_group(process_group, term_signal)
    except ProcessLookupError:
        pass

    grace_deadline = monotonic() + termination_grace_seconds
    while _process_group_exists(process_group):
        remaining = grace_deadline - monotonic()
        if remaining <= 0:
            break
        if process.returncode is None:
            try:
                process.wait(timeout=min(kill_poll_seconds, remaining))
            except subprocess.TimeoutExpired:
                pass
        else:
            sleep(min(kill_poll_seconds, remaining))

    if _process_group_exists(process_group):
        try:
            kill_group(process_group, kill_signal)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        process.wait()

    # A leader can be reaped before a TERM-ignoring grandchild.  Keep the lock
    # owner in this function until the complete fixed group is gone.
    while _process_group_exists(process_group):
        try:
            kill_group(process_group, kill_signal)
        except ProcessLookupError:
            break
        sleep(kill_poll_seconds)


def _terminate_until_complete(
    terminator: Callable[..., None],
    process: Any,
    *,
    termination_grace_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    kill_poll_seconds: float,
) -> None:
    """Do not let a repeated parent SIGTERM interrupt group cleanup."""

    while True:
        try:
            terminator(
                process,
                termination_grace_seconds=termination_grace_seconds,
                monotonic=monotonic,
                sleep=sleep,
                kill_poll_seconds=kill_poll_seconds,
            )
            return
        except TerminationRequested:
            continue


def run_publisher(
    *,
    repo_dir: Path,
    lock_dir: Path,
    refresh_lock_path: Path,
    descriptor: int,
    generation: int,
    digest: str,
    publication_target: Mapping[str, Any] | None = None,
    force_archive: bool = False,
    base_env: Mapping[str, str],
    timeout_seconds: float,
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    kill_poll_seconds: float = DEFAULT_KILL_POLL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    process_launcher: Callable[..., Any] | None = None,
    terminate_process_group: Callable[..., None] | None = None,
) -> PublisherResult:
    """Launch only the fixed Bash publisher with the exact inherited lock FD."""

    if timeout_seconds <= 0:
        return PublisherResult("timeout", None)
    launcher = subprocess.Popen if process_launcher is None else process_launcher
    terminator = (
        terminate_publisher_process_group
        if terminate_process_group is None
        else terminate_process_group
    )
    argv = ["/bin/bash", "-p", str(repo_dir / "scripts" / "refresh_and_publish.sh")]
    environment = sanitized_publisher_environment(
        base_env,
        repo_dir=repo_dir,
        lock_dir=lock_dir,
        refresh_lock_path=refresh_lock_path,
        descriptor=descriptor,
        generation=generation,
        digest=digest,
        publication_target=publication_target,
        force_archive=force_archive,
    )
    child_deadline = monotonic() + timeout_seconds
    process: Any | None = None
    termination_during_launch = False
    previous_term_handler: Any | None = None

    def defer_termination(_signum: int, _frame: Any) -> None:
        nonlocal termination_during_launch
        termination_during_launch = True

    try:
        if os.name == "posix":
            try:
                previous_term_handler = signal.getsignal(signal.SIGTERM)
                signal.signal(signal.SIGTERM, defer_termination)
            except ValueError:
                # Only the main thread may alter signal handlers. Production
                # invokes the drainer in the main thread; injected unit workers
                # still retain the outer termination cleanup path.
                previous_term_handler = None
        try:
            os.set_inheritable(descriptor, True)
            process = launcher(
                argv,
                cwd=str(repo_dir),
                env=environment,
                shell=False,
                pass_fds=(descriptor,),
                start_new_session=True,
            )
        finally:
            try:
                os.set_inheritable(descriptor, False)
            finally:
                if previous_term_handler is not None:
                    signal.signal(signal.SIGTERM, previous_term_handler)
        if termination_during_launch:
            _terminate_until_complete(
                terminator,
                process,
                termination_grace_seconds=termination_grace_seconds,
                monotonic=monotonic,
                sleep=sleep,
                kill_poll_seconds=kill_poll_seconds,
            )
            return PublisherResult("terminated", process.returncode)
        remaining = max(0.0, child_deadline - monotonic())
        return PublisherResult("completed", process.wait(timeout=remaining))
    except subprocess.TimeoutExpired:
        _terminate_until_complete(
            terminator,
            process,
            termination_grace_seconds=termination_grace_seconds,
            monotonic=monotonic,
            sleep=sleep,
            kill_poll_seconds=kill_poll_seconds,
        )
        return PublisherResult("timeout", process.returncode)
    except TerminationRequested:
        if process is None:
            raise
        _terminate_until_complete(
            terminator,
            process,
            termination_grace_seconds=termination_grace_seconds,
            monotonic=monotonic,
            sleep=sleep,
            kill_poll_seconds=kill_poll_seconds,
        )
        return PublisherResult("terminated", process.returncode)
    except BaseException:
        if process is not None:
            _terminate_until_complete(
                terminator,
                process,
                termination_grace_seconds=termination_grace_seconds,
                monotonic=monotonic,
                sleep=sleep,
                kill_poll_seconds=kill_poll_seconds,
            )
        raise


def _validated_identity(generation: Any, digest: Any) -> tuple[int, str]:
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise RuntimeError("publication generation is invalid")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise RuntimeError("publication digest is invalid")
    return generation, digest


def _journal_identity(record: Mapping[str, Any]) -> tuple[int, str]:
    return _validated_identity(
        record.get("publication_generation"),
        record.get("queue_digest"),
    )


def _latest_identity(
    value: tuple[Mapping[str, Any], str],
) -> tuple[int, str]:
    return _validated_identity(value[0].get("generation"), value[1])


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def drain_publication_queue(
    *,
    repo_dir: os.PathLike[str] | str,
    lock_dir: os.PathLike[str] | str,
    refresh_lock_path: os.PathLike[str] | str,
    base_env: Mapping[str, str] | None = None,
    runtime_budget_seconds: float = DEFAULT_RUNTIME_BUDGET_SECONDS,
    lock_wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
    lock_poll_seconds: float = DEFAULT_LOCK_POLL_SECONDS,
    cleanup_grace_seconds: float = DEFAULT_CLEANUP_GRACE_SECONDS,
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    followup_reserve_seconds: float = DEFAULT_FOLLOWUP_RESERVE_SECONDS,
    kill_poll_seconds: float = DEFAULT_KILL_POLL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    utc_now: Callable[[], str] = _utc_now,
    process_launcher: Callable[..., Any] | None = None,
    terminate_process_group: Callable[..., None] | None = None,
    lock_factory: Callable[..., Any] | None = None,
    read_journal: Callable[[Path], Mapping[str, Any] | None] | None = None,
    read_latest: Callable[[Path], tuple[Mapping[str, Any], str] | None] | None = None,
    finalize_handoff: Callable[[Path, int, str], bool] | None = None,
) -> int:
    """Drain retained authenticated work and then the newest queued identity."""

    if any(
        value <= 0
        for value in (
            runtime_budget_seconds,
            lock_poll_seconds,
            cleanup_grace_seconds,
            termination_grace_seconds,
            followup_reserve_seconds,
            kill_poll_seconds,
        )
    ) or lock_wait_seconds < 0:
        return EXIT_CONFIG
    try:
        repo, lock_root, refresh_lock = validate_runtime_paths(
            repo_dir,
            lock_dir,
            refresh_lock_path,
        )
    except (OSError, ConfigurationError) as exc:
        print(f"error: invalid queued publisher configuration: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    environment = dict(os.environ if base_env is None else base_env)
    acquire = owned_refresh_lock if lock_factory is None else lock_factory
    journal_reader = (
        read_deferred_recovery_journal if read_journal is None else read_journal
    )
    latest_reader = read_latest_with_digest if read_latest is None else read_latest
    finalizer = finalize_pushed_handoff if finalize_handoff is None else finalize_handoff
    deadline = monotonic() + runtime_budget_seconds

    try:
        with acquire(
            refresh_lock,
            wait_seconds=lock_wait_seconds,
            poll_seconds=lock_poll_seconds,
            monotonic=monotonic,
            sleep=sleep,
            started_at_utc=utc_now(),
        ) as descriptor:
            if descriptor is None:
                return 0

            while True:
                recovery = journal_reader(lock_root)
                if recovery is not None:
                    generation, digest = _journal_identity(recovery)
                    publication_target = recovery.get("publication_target")
                    if not isinstance(publication_target, Mapping):
                        raise RuntimeError("recovery publication target is invalid")
                    force_archive = recovery.get("handoff_phase") == "generating" or recovery.get(
                        "run_scope"
                    ) in {"archive", "archive_full"}
                else:
                    latest = latest_reader(lock_root)
                    if latest is None:
                        return 0
                    generation, digest = _latest_identity(latest)
                    publication_target = latest[0]
                    force_archive = False

                child_budget = deadline - monotonic() - cleanup_grace_seconds
                if child_budget <= 0:
                    return 0
                outcome = run_publisher(
                    repo_dir=repo,
                    lock_dir=lock_root,
                    refresh_lock_path=refresh_lock,
                    descriptor=descriptor,
                    generation=generation,
                    digest=digest,
                    publication_target=publication_target,
                    force_archive=force_archive,
                    base_env=environment,
                    timeout_seconds=child_budget,
                    termination_grace_seconds=termination_grace_seconds,
                    kill_poll_seconds=kill_poll_seconds,
                    monotonic=monotonic,
                    sleep=sleep,
                    process_launcher=process_launcher,
                    terminate_process_group=terminate_process_group,
                )

                if outcome.kind == "terminated":
                    return EXIT_TERMINATED
                if outcome.kind == "timeout":
                    return EXIT_FAILURE
                if outcome.returncode is None or outcome.returncode < 0:
                    return EXIT_FAILURE
                if outcome.returncode == 0:
                    # The child outcome is never inferred from stdout, env, or a
                    # queue field.  One public API authenticates pushed,
                    # no-diff, and peer-superseded durable handoffs.
                    if finalizer(lock_root, generation, digest) is not True:
                        return EXIT_FAILURE
                    newer = latest_reader(lock_root)
                    if newer is None:
                        return 0
                    newer_generation, _newer_digest = _latest_identity(newer)
                    if newer_generation <= generation:
                        return EXIT_FAILURE
                    if deadline - monotonic() < followup_reserve_seconds:
                        return 0
                    continue

                # The sole failure retry is Bash's pre-journal N -> N+1 race.
                # A timeout, parent signal, or negative signal return never
                # reaches this branch.
                if journal_reader(lock_root) is not None:
                    return EXIT_FAILURE
                newer = latest_reader(lock_root)
                if newer is None:
                    return EXIT_FAILURE
                newer_generation, _newer_digest = _latest_identity(newer)
                if newer_generation <= generation:
                    return EXIT_FAILURE
                if deadline - monotonic() < followup_reserve_seconds:
                    return EXIT_FAILURE
    except TerminationRequested:
        return EXIT_TERMINATED
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: queued publisher failed closed: {exc}", file=sys.stderr)
        return EXIT_FAILURE


def _termination_handler(_signum: int, _frame: Any) -> None:
    raise TerminationRequested()


def main() -> int:
    repo_dir = os.environ.get("DEGEN_DOGS_REPO_DIR", "")
    lock_dir = os.environ.get("DEGEN_DOGS_LOCK_DIR", "")
    refresh_lock_path = os.environ.get("DEGEN_DOGS_REFRESH_LOCK_PATH", "")
    if not repo_dir or not lock_dir or not refresh_lock_path:
        print("error: fixed WSL queued publisher paths are missing", file=sys.stderr)
        return EXIT_CONFIG
    if os.name != "posix":
        print("error: queued publishing is supported only inside WSL", file=sys.stderr)
        return EXIT_CONFIG
    previous = signal.signal(signal.SIGTERM, _termination_handler)
    try:
        return drain_publication_queue(
            repo_dir=repo_dir,
            lock_dir=lock_dir,
            refresh_lock_path=refresh_lock_path,
        )
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
