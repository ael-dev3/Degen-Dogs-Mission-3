#!/usr/bin/env python3
"""Self-healing watchdog for the Degen Dogs Mission 3 private Mac mini runner.

The watchdog verifies both launchd services, watcher state, refresh history, the
local worktree, and the deployed status sidecar. Healthy/no-op runs stay silent.
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Iterable

from runner_path_security import (
    SecurePathError,
    create_private_temp,
    ensure_private_directory as secure_private_directory,
    ensure_private_file as secure_private_file,
    open_existing_private_file,
    open_private_lock,
    replace_private_file,
    unlink_private_file,
)

os.umask(0o077)

HOME = Path(os.environ.get("DEGEN_DOGS_HEALTH_HOME", str(Path.home())))
REPO_DIR = Path(os.environ.get("DEGEN_DOGS_REPO_DIR", "/Users/marko/projects/Degen-Dogs-Mission-3"))
HOURLY_LABEL = "com.ael.degendogs.mission3.refresh"
WATCHER_LABEL = "com.ael.degendogs.mission3.watch-auction"
LOG_DIR = Path(
    os.environ.get("DEGEN_DOGS_LOG_DIR", str(HOME / "Library" / "Logs" / "degen-dogs-mission3"))
).expanduser()
CACHE_DIR = Path(
    os.environ.get("DEGEN_DOGS_LOCK_DIR", str(HOME / "Library" / "Caches" / "degen-dogs-mission3"))
).expanduser()
REFRESH_LOCK_PATH = Path(
    os.environ.get("MISSION3_REFRESH_LOCK_PATH", str(CACHE_DIR / "refresh.lock"))
).expanduser()
WATCHER_LOCK_PATH = Path(
    os.environ.get("MISSION3_WATCHER_LOCK_PATH", str(REPO_DIR / ".local" / "mission3_onchain_tracker.lock"))
).expanduser()
REFRESH_LOG = LOG_DIR / "refresh.log"
REFRESH_SCRIPT = REPO_DIR / "scripts" / "refresh_and_publish.sh"
WATCHER_SCRIPT = REPO_DIR / "scripts" / "watch_mission3_onchain_activity.py"
HOURLY_INSTALL_SCRIPT = REPO_DIR / "scripts" / "install_hourly_refresh_launchd.sh"
WATCHER_INSTALL_SCRIPT = REPO_DIR / "scripts" / "install_auction_watcher_launchd.sh"
WATCHER_STATE_PATH = Path(
    os.environ.get("MISSION3_WATCHER_STATE_PATH", str(REPO_DIR / ".local" / "mission3_onchain_tracker_state.json"))
).expanduser()
LIVE_URL = "https://ael-dev3.github.io/Degen-Dogs-Mission-3/"
LIVE_STATUS_URL = LIVE_URL + "generated/refresh_status.json"
LIVE_HTML_MAX_BYTES = 250_000
LIVE_STATUS_MAX_BYTES = 128_000
LIVE_TARGETS = {
    LIVE_URL: ("text/html", LIVE_HTML_MAX_BYTES),
    LIVE_STATUS_URL: ("application/json", LIVE_STATUS_MAX_BYTES),
}
GITHUB_REPO = os.environ.get("DEGEN_DOGS_HEALTH_GITHUB_REPO", "ael-dev3/Degen-Dogs-Mission-3")
RUNNER_ISSUE_TITLE = "Local runner critical health alert"
RUNNER_ISSUE_MARKER = "<!-- degen-dogs-runner-health-incident:v1 -->"
TRUSTED_STATE_KEY = "_degen_dogs_health_state_trusted"
DISCORD_MENTION = os.environ.get("DEGEN_DOGS_HEALTH_DISCORD_MENTION", "@Ael")
ALERT_STATE_PATH = Path(
    os.environ.get("DEGEN_DOGS_HEALTH_ALERT_STATE_PATH", str(CACHE_DIR / "critical-alert-state.json"))
).expanduser()
EXPECTED_INTERVAL_SECONDS = int(os.environ.get("DEGEN_DOGS_REFRESH_INTERVAL_SECONDS", "3600"))
WATCHER_INTERVAL_SECONDS = int(os.environ.get("MISSION3_WATCHER_INTERVAL_SECONDS", "15"))
WATCHER_AUTO_PUSH = os.environ.get("MISSION3_WATCHER_AUTO_PUSH", "0")
LIVE_VERIFY_AFTER_PUSH = os.environ.get("DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH", "1")
HOURLY_FULL_REFRESH = "1" if os.environ.get("DEGEN_DOGS_FULL_REFRESH", "0") == "1" else "0"
HOURLY_RUN_MISSION3_ARCHIVE = "0" if os.environ.get("DEGEN_DOGS_RUN_MISSION3_ARCHIVE", "1") == "0" else "1"
WATCHER_REFRESH_COMMAND = os.environ.get("MISSION3_REFRESH_COMMAND") or (
    "npm run refresh:publish" if WATCHER_AUTO_PUSH == "1" else "npm run refresh:current"
)
WATCHER_STALE_SECONDS = int(
    os.environ.get("DEGEN_DOGS_HEALTH_WATCHER_STALE_SECONDS", str(max(300, WATCHER_INTERVAL_SECONDS * 5)))
)
PENDING_STALE_SECONDS = int(os.environ.get("DEGEN_DOGS_HEALTH_PENDING_STALE_SECONDS", "900"))
LIVE_STALE_SECONDS = int(os.environ.get("DEGEN_DOGS_HEALTH_LIVE_STALE_SECONDS", str(3 * 3600)))
STALE_SUCCESS_SECONDS = max(2 * EXPECTED_INTERVAL_SECONDS, 2 * 3600)
CRITICAL_STALE_SECONDS = int(os.environ.get("DEGEN_DOGS_HEALTH_CRITICAL_STALE_SECONDS", str(2 * 3600)))
REPEAT_ALERT_SECONDS = int(os.environ.get("DEGEN_DOGS_HEALTH_REPEAT_ALERT_SECONDS", str(6 * 3600)))
BASE_PATH_VALUE = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PYTHON_RUNTIME_PROBE = (
    "import Crypto; from Crypto.Hash import keccak; "
    "assert Crypto.__version__ == '3.23.0'; "
    "assert keccak.new(digest_bits=256, data=b'').hexdigest() == "
    "'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470'"
)


def runner_python_ready(repo_dir: Path | None = None) -> bool:
    root = REPO_DIR if repo_dir is None else repo_dir
    root_text = str(root)
    python_path = root / ".venv" / "bin" / "python3"
    if ":" in root_text or not python_path.is_file() or not os.access(python_path, os.X_OK):
        return False
    try:
        completed = subprocess.run(
            [str(python_path), "-I", "-c", PYTHON_RUNTIME_PROBE],
            env={"PATH": BASE_PATH_VALUE, "PYTHONNOUSERSITE": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def runner_python_shim_ready(repo_dir: Path | None = None) -> bool:
    root = REPO_DIR if repo_dir is None else repo_dir
    shim = root / "scripts" / "runtime-bin" / "python3"
    return shim.is_file() and os.access(shim, os.X_OK)


def runner_path_value(repo_dir: Path | None = None) -> str:
    """Prefer the hash-locked repo virtualenv without accepting PATH separators."""
    root = REPO_DIR if repo_dir is None else repo_dir
    root_text = str(root)
    if runner_python_ready(root) and runner_python_shim_ready(root):
        return f"{root_text}/scripts/runtime-bin:{BASE_PATH_VALUE}"
    return BASE_PATH_VALUE


PATH_VALUE = runner_path_value()
DRY_RUN = os.environ.get("DEGEN_DOGS_HEALTH_DRY_RUN") == "1"
ALERT_DRY_RUN = DRY_RUN or os.environ.get("DEGEN_DOGS_HEALTH_ALERT_DRY_RUN") == "1"
GITHUB_ALERTS_ENABLED = os.environ.get("DEGEN_DOGS_HEALTH_GITHUB_ALERTS", "1") != "0"
LOG_MAX_BYTES = max(65_536, int(os.environ.get("DEGEN_DOGS_HEALTH_LOG_MAX_BYTES", str(8 * 1024 * 1024))))
LOG_RETAIN_BYTES = max(
    16_384,
    min(int(os.environ.get("DEGEN_DOGS_HEALTH_LOG_RETAIN_BYTES", str(2 * 1024 * 1024))), LOG_MAX_BYTES // 2),
)
LOG_EMERGENCY_MAX_BYTES = max(
    LOG_MAX_BYTES,
    int(os.environ.get("DEGEN_DOGS_HEALTH_LOG_EMERGENCY_MAX_BYTES", str(4 * LOG_MAX_BYTES))),
)
MIN_FREE_BYTES = max(0, int(os.environ.get("DEGEN_DOGS_HEALTH_MIN_FREE_BYTES", str(5 * 1024 * 1024 * 1024))))
MIN_FREE_PERCENT = max(0.0, float(os.environ.get("DEGEN_DOGS_HEALTH_MIN_FREE_PERCENT", "5")))
REFRESH_ACTIVE_GRACE_SECONDS = min(
    45 * 60,
    max(60, int(os.environ.get("DEGEN_DOGS_HEALTH_REFRESH_ACTIVE_GRACE_SECONDS", str(45 * 60)))),
)
WATCHER_ACTIVE_GRACE_SECONDS = max(
    30,
    int(os.environ.get("DEGEN_DOGS_HEALTH_WATCHER_ACTIVE_GRACE_SECONDS", "90")),
)

SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
]


@dataclass
class Result:
    code: int
    out: str
    err: str


@dataclass(frozen=True)
class LaunchdSpec:
    label: str
    plist_path: Path
    installer: Path
    program_arguments: tuple[str, ...]
    interval_seconds: int
    name: str
    standard_out_path: Path
    standard_error_path: Path
    throttle_interval: int
    required_environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ManagedLog:
    path: Path
    services: tuple[str, ...]
    name: str


def launchd_specs() -> tuple[LaunchdSpec, LaunchdSpec]:
    plist_dir = HOME / "Library" / "LaunchAgents"
    common_env = (
        ("HOME", str(HOME)),
        ("PATH", runner_path_value()),
        ("GIT_TERMINAL_PROMPT", "0"),
        ("DEGEN_DOGS_REPO_DIR", str(REPO_DIR)),
        ("DEGEN_DOGS_LOG_DIR", str(LOG_DIR)),
        ("DEGEN_DOGS_LOCK_DIR", str(CACHE_DIR)),
        ("DEGEN_DOGS_REFRESH_LOCK_PATH", str(REFRESH_LOCK_PATH)),
        ("DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH", LIVE_VERIFY_AFTER_PUSH),
    )
    return (
        LaunchdSpec(
            label=HOURLY_LABEL,
            plist_path=plist_dir / f"{HOURLY_LABEL}.plist",
            installer=HOURLY_INSTALL_SCRIPT,
            program_arguments=(str(REFRESH_SCRIPT),),
            interval_seconds=EXPECTED_INTERVAL_SECONDS,
            name="hourly reconcile refresh",
            standard_out_path=LOG_DIR / "launchd.out.log",
            standard_error_path=LOG_DIR / "launchd.err.log",
            throttle_interval=10,
            required_environment=(
                *common_env,
                ("DEGEN_DOGS_FULL_REFRESH", HOURLY_FULL_REFRESH),
                ("DEGEN_DOGS_RUN_MISSION3_ARCHIVE", HOURLY_RUN_MISSION3_ARCHIVE),
            ),
        ),
        LaunchdSpec(
            label=WATCHER_LABEL,
            plist_path=plist_dir / f"{WATCHER_LABEL}.plist",
            installer=WATCHER_INSTALL_SCRIPT,
            program_arguments=("/usr/bin/env", "python3", str(WATCHER_SCRIPT), "--once"),
            interval_seconds=WATCHER_INTERVAL_SECONDS,
            name="onchain auction watcher",
            standard_out_path=LOG_DIR / "watcher.launchd.out.log",
            standard_error_path=LOG_DIR / "watcher.launchd.err.log",
            throttle_interval=10,
            required_environment=(
                *common_env,
                ("DEGEN_DOGS_FULL_REFRESH", "0"),
                ("DEGEN_DOGS_RUN_MISSION3_ARCHIVE", "0"),
                ("MISSION3_REFRESH_LOCK_PATH", str(REFRESH_LOCK_PATH)),
                ("MISSION3_WATCHER_AUTO_PUSH", WATCHER_AUTO_PUSH),
                ("MISSION3_REFRESH_COMMAND", WATCHER_REFRESH_COMMAND),
            ),
        ),
    )


def _optional_local_path(value: str | None, default: Path) -> Path | None:
    if value is None or not value.strip():
        return default
    if value.strip() == "-":
        return None
    path = Path(value.strip()).expanduser()
    return path if path.is_absolute() else REPO_DIR / path


def managed_logs() -> tuple[ManagedLog, ...]:
    """Logs compacted in place so launchd keeps writing to the same inode."""
    both_workers = (HOURLY_LABEL, WATCHER_LABEL)
    watcher_log = _optional_local_path(os.environ.get("MISSION3_WATCHER_LOG_PATH"), LOG_DIR / "watch-onchain.log")
    refresh_runs = _optional_local_path(
        os.environ.get("DEGEN_DOGS_REFRESH_TELEMETRY_PATH"), REPO_DIR / ".local" / "refresh_runs.jsonl"
    )
    refresh_metrics = _optional_local_path(
        os.environ.get("DEGEN_DOGS_REFRESH_METRICS_PATH"), REPO_DIR / "logs" / "refresh-metrics.jsonl"
    )
    values = [
        ManagedLog(LOG_DIR / "refresh.log", both_workers, "refresh log"),
        ManagedLog(LOG_DIR / "launchd.out.log", (HOURLY_LABEL,), "hourly launchd stdout"),
        ManagedLog(LOG_DIR / "launchd.err.log", (HOURLY_LABEL,), "hourly launchd stderr"),
        ManagedLog(LOG_DIR / "watcher.launchd.out.log", (WATCHER_LABEL,), "watcher launchd stdout"),
        ManagedLog(LOG_DIR / "watcher.launchd.err.log", (WATCHER_LABEL,), "watcher launchd stderr"),
        ManagedLog(LOG_DIR / "health.launchd.out.log", (), "health launchd stdout"),
        ManagedLog(LOG_DIR / "health.launchd.err.log", (), "health launchd stderr"),
        ManagedLog(REPO_DIR / "logs" / "watch-onchain.log", (WATCHER_LABEL,), "repository watcher activity log"),
        ManagedLog(REPO_DIR / ".local" / "watcher_checks.jsonl", (WATCHER_LABEL,), "watcher telemetry"),
    ]
    if watcher_log is not None:
        values.append(ManagedLog(watcher_log, (WATCHER_LABEL,), "watcher activity log"))
    if refresh_runs is not None:
        values.append(ManagedLog(refresh_runs, both_workers, "refresh telemetry"))
    if refresh_metrics is not None:
        values.append(ManagedLog(refresh_metrics, both_workers, "refresh metrics"))
    unique_logs: dict[Path, ManagedLog] = {}
    for item in values:
        previous = unique_logs.get(item.path)
        if previous is None:
            unique_logs[item.path] = item
            continue
        services = tuple(dict.fromkeys((*previous.services, *item.services)))
        unique_logs[item.path] = ManagedLog(item.path, services, f"{previous.name}/{item.name}")
    return tuple(unique_logs.values())


def _absolute_runner_path(path: Path) -> Path:
    return path if path.is_absolute() else REPO_DIR / path


def private_runner_directories() -> tuple[Path, ...]:
    paths = (
        _absolute_runner_path(LOG_DIR),
        _absolute_runner_path(CACHE_DIR),
        REPO_DIR / ".local",
        REPO_DIR / "logs",
    )
    return tuple(dict.fromkeys(paths))


def private_runner_files() -> tuple[Path, ...]:
    plist_dir = HOME / "Library" / "LaunchAgents"
    paths = [
        *(_absolute_runner_path(item.path) for item in managed_logs()),
        _absolute_runner_path(WATCHER_STATE_PATH),
        _absolute_runner_path(WATCHER_LOCK_PATH),
        _absolute_runner_path(REFRESH_LOCK_PATH),
        _absolute_runner_path(ALERT_STATE_PATH),
        plist_dir / f"{HOURLY_LABEL}.plist",
        plist_dir / f"{WATCHER_LABEL}.plist",
        plist_dir / "com.ael.degendogs.mission3.health.plist",
    ]
    return tuple(dict.fromkeys(paths))


def harden_private_directory(path: Path) -> bool:
    """Create or repair a directory without following any path component."""
    try:
        return secure_private_directory(path)
    except SecurePathError as exc:
        raise PermissionError(f"runner private directory is unsafe: {path}: {exc}") from exc


def harden_private_file(path: Path) -> bool:
    """Repair an existing owned regular artifact in place; missing files stay absent."""
    try:
        return secure_private_file(path, create=False)
    except SecurePathError as exc:
        raise PermissionError(f"runner private file is unsafe: {path}: {exc}") from exc


def harden_runner_permissions(lines: list[str]) -> bool:
    """Keep private runner state unreadable to other local accounts."""
    had_error = False
    for path in private_runner_directories():
        if DRY_RUN:
            try:
                details = path.lstat()
                unsafe = not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode) or details.st_uid != os.getuid()
                if unsafe:
                    raise PermissionError(f"runner private directory is unsafe: {path}")
                if stat.S_IMODE(details.st_mode) != 0o700:
                    append_fix(lines, f"DRY-RUN would set runner directory {path} to mode 0700")
            except FileNotFoundError:
                append_fix(lines, f"DRY-RUN would create runner directory {path} with mode 0700")
            except (OSError, PermissionError) as exc:
                append_issue(lines, f"runner permission hardening failed for directory {path}: {type(exc).__name__}: {exc}")
                had_error = True
            continue
        try:
            if harden_private_directory(path):
                append_fix(lines, f"set runner directory {path} to mode 0700")
        except (OSError, PermissionError) as exc:
            append_issue(lines, f"runner permission hardening failed for directory {path}: {type(exc).__name__}: {exc}")
            had_error = True

    for path in private_runner_files():
        if DRY_RUN:
            try:
                details = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode) or details.st_uid != os.getuid():
                append_issue(lines, f"runner permission hardening refused unsafe file: {path}")
                had_error = True
            elif stat.S_IMODE(details.st_mode) != 0o600:
                append_fix(lines, f"DRY-RUN would set runner file {path} to mode 0600")
            continue
        try:
            if harden_private_file(path):
                append_fix(lines, f"set runner file {path} to mode 0600")
        except (OSError, PermissionError) as exc:
            append_issue(lines, f"runner permission hardening failed for file {path}: {type(exc).__name__}: {exc}")
            had_error = True
    return had_error


def _reset_matching_standard_stream_offsets(file_stat: os.stat_result) -> None:
    for fd in (1, 2):
        try:
            stream_stat = os.fstat(fd)
            if (stream_stat.st_dev, stream_stat.st_ino) == (file_stat.st_dev, file_stat.st_ino):
                os.lseek(fd, 0, os.SEEK_END)
        except OSError:
            continue


def compact_log_in_place(path: Path, *, max_bytes: int, retain_bytes: int) -> tuple[bool, int, int]:
    """Tail-compact an oversized regular log while preserving its inode.

    Preserving the inode is important for launchd StandardOutPath/StandardErrorPath:
    renaming an active file can leave launchd writing forever to an unlinked inode.
    """
    if max_bytes < 1 or retain_bytes < 0:
        raise ValueError("log bounds must be non-negative and max_bytes must be positive")
    try:
        descriptor = open_existing_private_file(path, writable=True)
    except FileNotFoundError:
        return False, 0, 0
    except (OSError, SecurePathError) as exc:
        raise ValueError(f"refusing to compact unsafe/non-regular log: {path}: {exc}") from exc
    with os.fdopen(descriptor, "r+b", buffering=0) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        opened = os.fstat(handle.fileno())
        before = opened.st_size
        if before <= max_bytes:
            return False, before, before
        header = f"[{iso_now()}] log compacted in place; prior_bytes={before}\n".encode("utf-8")[:max_bytes]
        budget = max(0, min(retain_bytes, max_bytes - len(header)))
        start = max(0, before - budget)
        handle.seek(start)
        tail = handle.read(budget)
        if start > 0 and tail:
            newline = tail.find(b"\n")
            tail = tail[newline + 1 :] if newline >= 0 else b""
        payload = header + tail
        handle.seek(0)
        # Truncate before the bounded write: after mutation begins, even a crash
        # cannot leave the old oversized allocation behind.
        handle.truncate(0)
        handle.write(payload)
        handle.truncate(len(payload))
        handle.flush()
        os.fsync(handle.fileno())
        after_stat = os.fstat(handle.fileno())
        _reset_matching_standard_stream_offsets(after_stat)
        return True, before, after_stat.st_size


def rotate_managed_logs(lines: list[str], active_services: set[str]) -> bool:
    """Compact oversized logs; defer active logs unless they cross the emergency cap."""
    had_error = False
    for item in managed_logs():
        try:
            size = item.path.stat().st_size
        except FileNotFoundError:
            continue
        except OSError as exc:
            append_issue(lines, f"log rotation could not stat {item.name}: {type(exc).__name__}")
            had_error = True
            continue
        if size <= LOG_MAX_BYTES:
            continue
        active = any(service in active_services for service in item.services)
        if active and size <= LOG_EMERGENCY_MAX_BYTES:
            continue
        if DRY_RUN:
            append_fix(lines, f"DRY-RUN would compact {item.name} from {size} bytes")
            continue
        try:
            rotated, before, after = compact_log_in_place(
                item.path,
                max_bytes=LOG_MAX_BYTES,
                retain_bytes=LOG_RETAIN_BYTES,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            append_issue(lines, f"log rotation failed for {item.name}: {type(exc).__name__}: {exc}")
            had_error = True
            continue
        if rotated:
            qualifier = " emergency" if active else ""
            append_fix(lines, f"{qualifier} compacted {item.name} in place from {before} to {after} bytes")
    return had_error


def _nearest_existing_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def inspect_disk_free(
    paths: Iterable[Path] | None = None,
    *,
    min_free_bytes: int = MIN_FREE_BYTES,
    min_free_percent: float = MIN_FREE_PERCENT,
    usage_fn: Any = shutil.disk_usage,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Check each distinct filesystem backing runner state and logs."""
    issues: list[str] = []
    summary: list[dict[str, Any]] = []
    seen_devices: set[int] = set()
    for requested in paths or (REPO_DIR, LOG_DIR, CACHE_DIR):
        anchor = _nearest_existing_path(requested)
        try:
            device = anchor.stat().st_dev
            if device in seen_devices:
                continue
            seen_devices.add(device)
            usage = usage_fn(anchor)
        except OSError as exc:
            issues.append(f"disk free-space check failed for {requested}: {type(exc).__name__}")
            continue
        total = max(0, int(usage.total))
        free = max(0, int(usage.free))
        free_percent = (100.0 * free / total) if total else 0.0
        row = {
            "path": str(anchor),
            "total_bytes": total,
            "free_bytes": free,
            "free_percent": round(free_percent, 2),
        }
        summary.append(row)
        if free < min_free_bytes or free_percent < min_free_percent:
            issues.append(
                f"runner disk free space low at {anchor}: {free / (1024 ** 3):.2f} GiB "
                f"({free_percent:.1f}%) available; require at least "
                f"{min_free_bytes / (1024 ** 3):.2f} GiB and {min_free_percent:.1f}%"
            )
    return issues, summary


def env() -> dict[str, str]:
    data = os.environ.copy()
    data.update({
        "HOME": str(HOME),
        "PATH": runner_path_value(),
        "GIT_TERMINAL_PROMPT": "0",
    })
    return data


def sanitize(text: str, limit: int = 1200) -> str:
    cleaned = text or ""
    cleaned = cleaned.replace(str(REPO_DIR), "<repo>").replace(str(HOME), "<home>")
    cleaned = re.sub(r"https?://[^\s\"'<>]+", "<url>", cleaned)
    for pattern in SECRET_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    cleaned = cleaned.replace("\r", "")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…"
    return cleaned.strip()


def run(cmd: list[str], *, cwd: Path | None = REPO_DIR, timeout: int = 60, check: bool = False) -> Result:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    result = Result(proc.returncode, sanitize(proc.stdout), sanitize(proc.stderr))
    if check and result.code != 0:
        raise RuntimeError(f"command failed ({result.code}): {' '.join(cmd)}\n{result.out}\n{result.err}")
    return result


def run_raw(cmd: list[str], *, cwd: Path | None = REPO_DIR, timeout: int = 60) -> Result:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    return Result(proc.returncode, proc.stdout, proc.stderr)


def launch_domain() -> str:
    return f"gui/{os.getuid()}"


def launch_target(label: str = HOURLY_LABEL) -> str:
    return f"{launch_domain()}/{label}"


def parse_log_ts(value: str) -> float | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def iso_from_ts(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def age_minutes(now: float, ts: float | None) -> int | None:
    if ts is None:
        return None
    return max(0, int((now - ts) / 60))


def parse_refresh_log_details() -> dict[str, Any]:
    details: dict[str, Any] = {
        "last_success_ts": None,
        "last_finished_ts": None,
        "last_finished_status": None,
        "last_started_ts": None,
        "last_error": None,
        "recent_signals": [],
    }
    if not REFRESH_LOG.exists():
        return details
    # Keep parsing bounded even if the log grows large.
    data = REFRESH_LOG.read_bytes()[-768_000:].decode("utf-8", errors="replace")
    signal_needles = (
        "tracked working tree changes exist",
        "refusing to refresh",
        "no backend is currently healthy",
        "http error 503",
        "http error 429",
        "timeout",
        "traceback",
        "runtimeerror",
        "error:",
        "finished status=1",
    )
    recent_signals: list[str] = []
    for line in data.splitlines():
        start = re.match(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\] starting hourly refresh", line)
        if start:
            details["last_started_ts"] = parse_log_ts(start.group(1))
        finished = re.match(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\] finished status=(\d+)", line)
        if finished:
            ts = parse_log_ts(finished.group(1))
            status = int(finished.group(2))
            details["last_finished_ts"] = ts
            details["last_finished_status"] = status
            if status == 0:
                details["last_success_ts"] = ts
                details["last_error"] = None
                recent_signals = []
        lower = line.lower()
        if any(needle in lower for needle in signal_needles):
            clean = sanitize(line, 360)
            if clean:
                recent_signals.append(clean)
            if "error:" in lower or "traceback" in lower or "runtimeerror" in lower:
                details["last_error"] = clean
    if details["last_finished_status"] == 0:
        details["last_error"] = None
    details["recent_signals"] = recent_signals[-12:]
    return details


def parse_refresh_log() -> tuple[float | None, int | None, str | None]:
    details = parse_refresh_log_details()
    return details.get("last_success_ts"), details.get("last_finished_status"), details.get("last_error")


def load_metrics() -> dict[str, str]:
    path = REPO_DIR / "generated" / "mission3_metrics.csv"
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {row.get("metric", ""): row.get("value", "") for row in csv.DictReader(handle)}


def load_local_refresh_status() -> dict[str, Any]:
    path = REPO_DIR / "generated" / "refresh_status.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class NoLiveRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201, ARG002
        return None


LIVE_OPENER = urllib.request.build_opener(NoLiveRedirectHandler())


def fetch_fixed_live_text(url: str, *, cache_buster: int) -> str:
    target_policy = LIVE_TARGETS.get(url)
    if target_policy is None:
        raise RuntimeError("live endpoint is not an approved fixed target")
    expected_content_type, max_bytes = target_policy
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except (TypeError, ValueError) as exc:
        raise RuntimeError("live endpoint failed fixed-target validation") from exc
    if (
        parts.scheme != "https"
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise RuntimeError("live endpoint failed fixed-target validation")
    request_url = f"{url}?runner_health={cache_buster}"
    request = urllib.request.Request(
        request_url,
        headers={
            "Accept": expected_content_type,
            "Cache-Control": "no-cache",
            "User-Agent": "DegenDogs-runner-health/3.0",
        },
    )
    try:
        response = LIVE_OPENER.open(request, timeout=25)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"live endpoint HTTP {exc.code}") from None
    except Exception as exc:  # noqa: BLE001 - never expose provider-controlled URL/reason text
        raise RuntimeError(f"live endpoint transport failed ({type(exc).__name__})") from None
    try:
        with response:
            if response.getcode() != 200:
                raise RuntimeError("live endpoint returned an unexpected HTTP status")
            if str(response.geturl()) != request_url:
                raise RuntimeError("live endpoint response URL changed unexpectedly")
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type != expected_content_type:
                raise RuntimeError("live endpoint returned an unexpected content type")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("live endpoint returned an invalid content length") from exc
                if declared_length < 0 or declared_length > max_bytes:
                    raise RuntimeError("live endpoint response exceeds the size limit")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise RuntimeError("live endpoint response exceeds the size limit")
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - suppress provider-controlled read details
        raise RuntimeError(f"live endpoint response read failed ({type(exc).__name__})") from None
    try:
        return body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError("live endpoint response is not valid UTF-8") from exc


def live_site_ok(expected_status: dict[str, Any] | None = None) -> tuple[bool, str]:
    cache_buster = int(time.time())
    try:
        body = fetch_fixed_live_text(LIVE_URL, cache_buster=cache_buster)
    except RuntimeError as exc:
        return False, f"live HTTP check failed: {exc}"
    if "auction_feed" not in body or LIVE_URL.rstrip("/") not in body:
        return False, "live HTML missing expected auction_feed/site_url markers"
    try:
        status_data = json.loads(fetch_fixed_live_text(LIVE_STATUS_URL, cache_buster=cache_buster))
    except json.JSONDecodeError:
        return False, "live refresh status check failed: invalid JSON"
    except RuntimeError as exc:
        return False, f"live refresh status check failed: {exc}"
    if not isinstance(status_data, dict) or status_data.get("kind") != "refresh_status":
        return False, "live refresh status payload is invalid"
    if status_data.get("site_url") != LIVE_URL:
        return False, "live refresh status site_url is invalid"
    try:
        latest_block = int(status_data.get("latest_generated_block") or 0)
    except (TypeError, ValueError):
        latest_block = 0
    if latest_block <= 0:
        return False, "live refresh status latest_generated_block is invalid"
    if expected_status:
        try:
            expected_block = int(expected_status.get("latest_generated_block") or 0)
        except (TypeError, ValueError):
            expected_block = 0
        if expected_block > 0 and latest_block < expected_block:
            return False, f"live refresh status block {latest_block} trails local generated block {expected_block}"
        if expected_block > 0 and latest_block == expected_block:
            for key in (
                "current_dog_token_id",
                "current_bid_eth",
                "current_high_bidder_wallet",
                "current_auction_status",
                "current_auction_end_time_utc",
            ):
                if str(status_data.get(key) or "") != str(expected_status.get(key) or ""):
                    return False, f"live refresh status {key} differs from local validated status at block {latest_block}"
    refreshed_at = parse_iso_timestamp(status_data.get("last_successful_refresh_time_utc"))
    if refreshed_at is None:
        return False, "live refresh status timestamp is invalid"
    live_age = max(0, int(time.time() - refreshed_at))
    if live_age > LIVE_STALE_SECONDS:
        return False, f"live refresh status age={live_age // 60}m exceeds threshold={LIVE_STALE_SECONDS // 60}m"
    return True, f"live site/status ok at block {latest_block}"


def read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = path.read_bytes()[-768_000:].decode("utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in data.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows[-limit:]


def compact_refresh_history(limit: int = 8) -> list[str]:
    rows = read_jsonl_tail(REPO_DIR / ".local" / "refresh_runs.jsonl", limit)
    out: list[str] = []
    for row in rows:
        ts = row.get("completed_at_utc") or row.get("started_at_utc") or row.get("time_utc") or "unknown-time"
        result = row.get("result") or "unknown"
        duration = row.get("duration_seconds")
        error = row.get("error") or row.get("reason") or ""
        detail = f"{ts}: {result}"
        if duration is not None:
            detail += f" ({duration}s)"
        if error:
            detail += f"; {sanitize(str(error), 180)}"
        out.append(detail)
    return out


def compact_watcher_history(limit: int = 6) -> list[str]:
    rows = read_jsonl_tail(REPO_DIR / ".local" / "watcher_checks.jsonl", limit)
    out: list[str] = []
    for row in rows:
        ts = row.get("completed_at_utc") or row.get("started_at_utc") or row.get("time_utc") or "unknown-time"
        result = row.get("result") or "unknown"
        reasons = row.get("reasons") or row.get("reason") or ""
        duration = row.get("duration_seconds")
        detail = f"{ts}: {result}"
        if duration is not None:
            detail += f" ({duration}s)"
        if reasons:
            detail += f"; reasons={sanitize(str(reasons), 160)}"
        out.append(detail)
    return out


def unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def derive_causes(
    *,
    issues: list[str],
    dirty_paths: list[str],
    log_details: dict[str, Any],
    stale: bool,
    failed_last: bool,
    live_ok: bool,
    launch_output: str,
    now: float,
) -> list[str]:
    combined = "\n".join([*issues, *[str(item) for item in log_details.get("recent_signals", [])]]).lower()
    causes: list[str] = []
    if dirty_paths or "tracked working tree changes exist" in combined or "refusing to overwrite" in combined:
        causes.append("dirty_worktree_preflight_block")
    if "no backend is currently healthy" in combined or "http error 503" in combined:
        causes.append("base_rpc_backend_unhealthy")
    if "timeout" in combined:
        causes.append("rpc_timeout_or_hung_refresh")
    if stale:
        causes.append("no_successful_refresh_over_threshold")
    if failed_last:
        causes.append("latest_refresh_failed")
    if launchd_fault_present(issues):
        causes.append("launchd_agent_unhealthy_or_drifted")
    if "watcher" in combined:
        causes.append("onchain_watcher_unhealthy_or_stale")
    if "disk free" in combined or "disk free-space" in combined or "free space" in combined:
        causes.append("runner_disk_space_low_or_unreadable")
    if "log rotation" in combined:
        causes.append("runner_log_rotation_failed")
    if "runner permission hardening" in combined:
        causes.append("runner_private_artifact_permissions_unsafe")
    if "python virtualenv" in combined or "keccak runtime" in combined:
        causes.append("runner_python_runtime_invalid")
    if not live_ok:
        causes.append("live_site_or_refresh_status_failure")
    last_started = log_details.get("last_started_ts")
    last_finished = log_details.get("last_finished_ts")
    if service_is_running(launch_output) and last_started and (not last_finished or last_finished < last_started):
        runtime_minutes = age_minutes(now, last_started) or 0
        if runtime_minutes >= 45:
            causes.append("refresh_process_running_too_long")
    if not causes and issues:
        causes.append("health_watchdog_detected_issue")
    return unique(causes)


def alert_fingerprint(snapshot: dict[str, Any]) -> str:
    payload = {
        "causes": snapshot.get("causes") or [],
        "dirty_paths": sorted(snapshot.get("dirty_paths") or []),
        "last_finished_status": snapshot.get("last_finished_status"),
        "live_ok": snapshot.get("live_ok"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def load_alert_state() -> dict[str, Any]:
    try:
        fd = open_existing_private_file(ALERT_STATE_PATH)
        try:
            file_stat = os.fstat(fd)
            if file_stat.st_size > 1_048_576:
                return {}
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                data = json.load(handle)
        finally:
            if fd >= 0:
                os.close(fd)
    except (FileNotFoundError, OSError, SecurePathError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    data[TRUSTED_STATE_KEY] = True
    return data


def save_alert_state(state: dict[str, Any]) -> None:
    if ALERT_DRY_RUN:
        return
    try:
        secure_private_directory(ALERT_STATE_PATH.parent)
    except SecurePathError as exc:
        raise PermissionError(
            f"refusing to write alert state in unprotected directory: {ALERT_STATE_PATH.parent}: {exc}"
        ) from exc
    persisted = {key: value for key, value in state.items() if key != TRUSTED_STATE_KEY}
    payload = (json.dumps(persisted, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = Path(create_private_temp(ALERT_STATE_PATH.parent / f".{ALERT_STATE_PATH.name}"))
    fd = open_existing_private_file(temporary, writable=True)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_private_file(temporary, ALERT_STATE_PATH)
    finally:
        if fd >= 0:
            os.close(fd)
        unlink_private_file(temporary, missing_ok=True)


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_timestamp(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def inspect_watcher_state(now: float | None = None, path: Path | None = None) -> tuple[list[str], dict[str, Any]]:
    """Return actionable watcher-state issues and a sanitized operational summary."""
    now = time.time() if now is None else now
    path = WATCHER_STATE_PATH if path is None else path
    try:
        descriptor = open_existing_private_file(path)
    except FileNotFoundError:
        return [f"watcher state missing: {path}"], {"state_path": str(path), "present": False}
    except (OSError, SecurePathError) as exc:
        return [f"watcher state unsafe/unreadable: {type(exc).__name__}"], {"state_path": str(path), "present": True}
    try:
        details = os.fstat(descriptor)
        if details.st_size > 2_097_152:
            return ["watcher state is not a protected owned regular file"], {"state_path": str(path), "present": True}
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            state = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"watcher state unreadable: {type(exc).__name__}"], {"state_path": str(path), "present": True}
    finally:
        os.close(descriptor)
    if not isinstance(state, dict):
        return ["watcher state is not a JSON object"], {"state_path": str(path), "present": True}

    issues: list[str] = []
    checked_ts = parse_iso_timestamp(state.get("last_checked_at_utc") or state.get("updated_at_utc"))
    checked_age_seconds = None if checked_ts is None else max(0, int(now - checked_ts))
    if checked_ts is None:
        issues.append("watcher state has no valid last_checked_at_utc")
    elif checked_age_seconds is not None and checked_age_seconds > WATCHER_STALE_SECONDS:
        issues.append(
            f"watcher state age={checked_age_seconds // 60}m exceeds threshold={WATCHER_STALE_SECONDS // 60}m"
        )

    def failure_count(key: str) -> int:
        try:
            return max(0, int(state.get(key) or 0))
        except (TypeError, ValueError):
            issues.append(f"watcher state {key} is invalid")
            return 0

    rpc_failures = failure_count("consecutive_rpc_failures")
    refresh_failures = failure_count("consecutive_refresh_failures")
    if rpc_failures >= 3:
        issues.append(f"watcher has {rpc_failures} consecutive RPC failures")
    if refresh_failures >= 3:
        issues.append(f"watcher has {refresh_failures} consecutive refresh failures")

    pending = state.get("pending_refresh") is True
    pending_ts = parse_iso_timestamp(state.get("pending_refresh_since_utc")) if pending else None
    pending_age_seconds = None if pending_ts is None else max(0, int(now - pending_ts))
    if pending and pending_ts is None:
        issues.append("watcher pending refresh has no valid pending_refresh_since_utc")
    elif pending_age_seconds is not None and pending_age_seconds > PENDING_STALE_SECONDS:
        issues.append(
            f"watcher pending refresh age={pending_age_seconds // 60}m exceeds threshold={PENDING_STALE_SECONDS // 60}m"
        )

    summary = {
        "state_path": str(path),
        "present": True,
        "last_checked_at_utc": state.get("last_checked_at_utc"),
        "last_checked_age_seconds": checked_age_seconds,
        "last_checked_block": state.get("last_checked_block"),
        "last_observed_block": state.get("last_observed_block"),
        "consecutive_rpc_failures": rpc_failures,
        "consecutive_refresh_failures": refresh_failures,
        "pending_refresh": pending,
        "pending_refresh_since_utc": state.get("pending_refresh_since_utc"),
        "pending_refresh_age_seconds": pending_age_seconds,
        "last_refresh_status": state.get("last_refresh_status"),
    }
    return issues, summary


def run_gh(args: list[str], *, body: str | None = None, timeout: int = 45) -> Result:
    if body is None:
        return run_raw(["gh", *args], cwd=None, timeout=timeout)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".md") as handle:
        handle.write(body)
        body_path = handle.name
    try:
        return run_raw(["gh", *args, "--body-file", body_path], cwd=None, timeout=timeout)
    finally:
        try:
            Path(body_path).unlink()
        except OSError:
            pass


def github_actor_login() -> str | None:
    """Return the authenticated account that would own a watchdog-created issue."""
    result = run_gh(["api", "user", "--jq", ".login"], timeout=20)
    if result.code != 0:
        return None
    login = result.out.strip()
    if not login or len(login) > 39 or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", login) is None:
        return None
    return login


def canonical_issue_url(number: int) -> str:
    return f"https://github.com/{GITHUB_REPO}/issues/{number}"


def trusted_state_issue_number(state: dict[str, Any]) -> int | None:
    number = state.get("issue_number")
    if state.get(TRUSTED_STATE_KEY) is not True or not isinstance(number, int) or isinstance(number, bool):
        return None
    return number if number > 0 else None


def find_open_runner_issue() -> tuple[int | None, str | None]:
    actor = github_actor_login()
    if actor is None:
        return None, None
    result = run_gh(
        [
            "issue",
            "list",
            "--repo",
            GITHUB_REPO,
            "--state",
            "open",
            "--author",
            actor,
            "--search",
            f"{RUNNER_ISSUE_TITLE} in:title",
            "--json",
            "number,url,title,body,author",
            "--limit",
            "100",
        ],
        timeout=30,
    )
    if result.code != 0:
        return None, None
    try:
        issues = json.loads(result.out or "[]")
    except json.JSONDecodeError:
        return None, None
    if not isinstance(issues, list):
        return None, None
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        title = str(issue.get("title") or "")
        body = str(issue.get("body") or "")
        author = issue.get("author")
        author_login = str(author.get("login") or "") if isinstance(author, dict) else ""
        number = issue.get("number")
        if (
            title == RUNNER_ISSUE_TITLE
            and RUNNER_ISSUE_MARKER in body
            and author_login.casefold() == actor.casefold()
            and isinstance(number, int)
            and not isinstance(number, bool)
            and number > 0
        ):
            return number, canonical_issue_url(number)
    return None, None


def update_github_issue(snapshot: dict[str, Any], body: str, state: dict[str, Any]) -> tuple[str, int | None, str | None]:
    if not GITHUB_ALERTS_ENABLED:
        return "GitHub alert update skipped: disabled", None, None
    if ALERT_DRY_RUN:
        number = trusted_state_issue_number(state)
        url = canonical_issue_url(number) if number is not None else None
        return "DRY-RUN would create/update GitHub issue", number, url
    auth = run_gh(["auth", "status"], timeout=20)
    if auth.code != 0:
        return f"GitHub alert update failed: gh auth status failed: {sanitize(auth.out or auth.err)}", None, None

    number = trusted_state_issue_number(state)
    url = canonical_issue_url(number) if number is not None else None
    if not number:
        number, url = find_open_runner_issue()
    if number:
        comment = run_gh(["issue", "comment", str(number), "--repo", GITHUB_REPO], body=body, timeout=45)
        if comment.code == 0:
            url = canonical_issue_url(number)
            return f"GitHub issue updated: {url}", number, url
        return f"GitHub alert update failed: {sanitize(comment.out or comment.err)}", number, url

    create = run_gh(
        ["issue", "create", "--repo", GITHUB_REPO, "--title", RUNNER_ISSUE_TITLE],
        body=body,
        timeout=45,
    )
    if create.code != 0:
        return f"GitHub alert update failed: {sanitize(create.out or create.err)}", None, None
    created_url = create.out.strip().splitlines()[-1] if create.out.strip() else ""
    match = re.search(r"/issues/(\d+)", created_url)
    created_number = int(match.group(1)) if match else None
    return f"GitHub issue created: {created_url or 'unknown-url'}", created_number, created_url or None


def close_github_issue(state: dict[str, Any], body: str) -> str | None:
    number = trusted_state_issue_number(state)
    if not number or not GITHUB_ALERTS_ENABLED:
        return None
    if ALERT_DRY_RUN:
        return f"DRY-RUN would close GitHub issue #{number}"
    comment = run_gh(["issue", "comment", str(number), "--repo", GITHUB_REPO], body=body, timeout=45)
    close = run_gh(["issue", "close", str(number), "--repo", GITHUB_REPO, "--reason", "completed"], timeout=45)
    if close.code == 0:
        if comment.code != 0:
            return (
                f"GitHub issue closed: {canonical_issue_url(number)} "
                f"(recovery comment failed: {sanitize(comment.out or comment.err)})"
            )
        return f"GitHub issue closed: {canonical_issue_url(number)}"
    return f"GitHub recovery update failed: {sanitize(comment.out or comment.err or close.out or close.err)}"


def build_incident_body(snapshot: dict[str, Any]) -> str:
    lines = [
        RUNNER_ISSUE_MARKER,
        "## Critical local runner health alert",
        "",
        "The private Mac mini runner watchdog detected a critical refresh failure. Values below are sanitized before being posted to GitHub.",
        "",
        "### Summary",
        f"- Detected at UTC: `{snapshot.get('detected_at_utc')}`",
        f"- Cause classification: `{', '.join(snapshot.get('causes') or ['unknown'])}`",
        f"- Last successful refresh: `{snapshot.get('last_success_at_utc') or 'none'}`",
        f"- Last success age: `{snapshot.get('last_success_age_minutes')}` minutes",
        f"- Latest finished status: `{snapshot.get('last_finished_status')}`",
        f"- Latest refresh start: `{snapshot.get('last_started_at_utc') or 'unknown'}`",
        f"- Live site check: `{'ok' if snapshot.get('live_ok') else 'failed'}`",
        "",
        "### Blocking dirty paths",
    ]
    dirty_paths = snapshot.get("dirty_paths") or []
    lines.extend(f"- `{sanitize(str(path), 220)}`" for path in dirty_paths[:20])
    if not dirty_paths:
        lines.append("- none detected")
    lines.extend(["", "### Runner disk space"])
    disk_rows = snapshot.get("disk") or []
    for row in disk_rows:
        lines.append(
            f"- `{sanitize(str(row.get('path') or ''), 160)}`: "
            f"{int(row.get('free_bytes') or 0) / (1024 ** 3):.2f} GiB free "
            f"({float(row.get('free_percent') or 0):.1f}%)"
        )
    if not disk_rows:
        lines.append("- disk summary unavailable")
    lines.extend(["", "### Health watchdog findings"])
    findings = snapshot.get("issues") or []
    lines.extend(f"- {sanitize(str(item), 260)}" for item in findings[:20])
    if not findings:
        lines.append("- none")
    lines.extend(["", "### Recent failure signals from refresh.log"])
    signals = snapshot.get("recent_signals") or []
    lines.extend(f"- `{sanitize(str(item), 320)}`" for item in signals[-12:])
    if not signals:
        lines.append("- none")
    lines.extend(["", "### Recent refresh history"])
    history = snapshot.get("refresh_history") or []
    lines.extend(f"- `{sanitize(str(item), 300)}`" for item in history)
    if not history:
        lines.append("- no private refresh telemetry rows found")
    lines.extend(["", "### Recent watcher history"])
    watcher = snapshot.get("watcher_history") or []
    lines.extend(f"- `{sanitize(str(item), 260)}`" for item in watcher)
    if not watcher:
        lines.append("- no watcher telemetry rows found")
    lines.extend(["", "### Operator note", "This issue is created/updated automatically by `~/.hermes/scripts/degen_dogs_runner_health.py`. The watchdog dedupes repeated alerts by failure fingerprint and comments again only when the cause changes or the repeat window elapses."])
    return "\n".join(lines) + "\n"


def build_discord_alert(snapshot: dict[str, Any], github_message: str, issue_url: str | None) -> str:
    causes = ", ".join(snapshot.get("causes") or ["unknown"])
    lines = [
        f"{DISCORD_MENTION} Degen Dogs local runner critical alert",
        f"- Cause: {causes}",
        f"- Last success: {snapshot.get('last_success_at_utc') or 'none'} ({snapshot.get('last_success_age_minutes')}m ago)",
        f"- Latest status: {snapshot.get('last_finished_status')}",
    ]
    dirty_paths = snapshot.get("dirty_paths") or []
    if dirty_paths:
        lines.append("- Blocking paths: " + ", ".join(sanitize(str(path), 120) for path in dirty_paths[:4]))
    if issue_url:
        lines.append(f"- GitHub: {issue_url}")
    lines.append(f"- GitHub update: {github_message}")
    return "\n".join(lines)


def handle_critical_alert(snapshot: dict[str, Any]) -> str | None:
    state = load_alert_state()
    was_active = state.get("active") is True
    if not was_active:
        # A recovered/closed issue should remain historical; a new incident gets a fresh
        # open issue unless one is already open in GitHub.
        state.pop("issue_number", None)
        state.pop("issue_url", None)
    fingerprint = alert_fingerprint(snapshot)
    now = time.time()
    previous_notified = parse_iso_timestamp(state.get("last_notified_at_utc"))
    same_active = state.get("active") is True and state.get("fingerprint") == fingerprint
    due = not same_active or previous_notified is None or (now - previous_notified) >= REPEAT_ALERT_SECONDS
    state.update({
        "active": True,
        "fingerprint": fingerprint,
        "last_seen_at_utc": iso_now(),
        "last_snapshot": snapshot,
    })
    if not due:
        save_alert_state(state)
        return None
    body = build_incident_body(snapshot)
    github_message, issue_number, issue_url = update_github_issue(snapshot, body, state)
    if issue_number:
        state["issue_number"] = issue_number
    if issue_url:
        state["issue_url"] = issue_url
    state["last_notified_at_utc"] = iso_now()
    state["github_update"] = github_message
    save_alert_state(state)
    return build_discord_alert(snapshot, github_message, issue_url or state.get("issue_url"))


def handle_recovery_alert(snapshot: dict[str, Any]) -> str | None:
    state = load_alert_state()
    if state.get("active") is not True:
        return None
    body = (
        "## Local runner recovered\n\n"
        f"Recovered at UTC: `{snapshot.get('detected_at_utc')}`\n\n"
        f"Last successful refresh: `{snapshot.get('last_success_at_utc')}`\n\n"
        f"Live site check: `{'ok' if snapshot.get('live_ok') else 'failed'}`\n"
    )
    github_message = close_github_issue(state, body)
    if github_message and github_message.startswith("GitHub recovery update failed:"):
        # Keep the incident active so the next healthy watchdog pass retries the
        # external close instead of orphaning an open uptime issue forever.
        state.update({
            "active": True,
            "last_recovery_attempt_at_utc": iso_now(),
            "recovery_snapshot": snapshot,
            "github_recovery_update": github_message,
        })
        save_alert_state(state)
        return f"Degen Dogs local runner recovered; GitHub closure will retry\n- {github_message}"
    state.update({
        "active": False,
        "recovered_at_utc": iso_now(),
        "recovery_snapshot": snapshot,
        "github_recovery_update": github_message,
    })
    save_alert_state(state)
    if github_message:
        return f"Degen Dogs local runner recovered\n- {github_message}"
    return "Degen Dogs local runner recovered"


def append_issue(lines: list[str], message: str) -> None:
    lines.append(f"issue: {message}")


def append_fix(lines: list[str], message: str) -> None:
    lines.append(f"fixed: {message}")


def tracked_dirty_paths(status_output: str) -> list[str]:
    dirty_paths: list[str] = []
    for line in status_output.splitlines():
        if len(line) >= 3 and line[2] == " ":
            dirty_paths.append(line[3:].strip())
        elif len(line) >= 2 and line[1] == " ":
            # sanitize() strips leading whitespace from the whole stdout, so the
            # first porcelain line can lose its index-0 status column.
            dirty_paths.append(line[2:].strip())
    return dirty_paths


VOLATILE_PRICE_FIELDS = {"fetched_at_utc", "updated_at_utc"}
PRICE_TIMESTAMP_ONLY_PATHS = {
    "archive/prices/data/generated/historical_prices_daily.csv",
    "archive/prices/data/generated/historical_prices_daily.json",
    "archive/prices/data/generated/price_manifest.json",
}


def git_show_head(rel_path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"HEAD:{rel_path}"],
        cwd=str(REPO_DIR),
        env=env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def csv_equal_excluding_volatile_timestamps(old_text: str, new_text: str) -> bool:
    old_rows = list(csv.DictReader(StringIO(old_text)))
    new_rows = list(csv.DictReader(StringIO(new_text)))
    if len(old_rows) != len(new_rows):
        return False
    if not old_rows and not new_rows:
        return True
    old_fields = [field for field in (old_rows[0].keys() if old_rows else []) if field not in VOLATILE_PRICE_FIELDS]
    new_fields = [field for field in (new_rows[0].keys() if new_rows else []) if field not in VOLATILE_PRICE_FIELDS]
    if old_fields != new_fields:
        return False
    return all({field: row.get(field, "") for field in old_fields} == {field: other.get(field, "") for field in old_fields} for row, other in zip(old_rows, new_rows))


def strip_volatile_price_timestamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: strip_volatile_price_timestamps(item) for key, item in value.items() if key not in VOLATILE_PRICE_FIELDS}
    if isinstance(value, list):
        return [strip_volatile_price_timestamps(item) for item in value]
    return value


def generated_price_change_is_timestamp_only(rel_path: str) -> bool:
    old_text = git_show_head(rel_path)
    path = REPO_DIR / rel_path
    if old_text is None or not path.exists():
        return False
    new_text = path.read_text(encoding="utf-8")
    if rel_path.endswith(".csv"):
        return csv_equal_excluding_volatile_timestamps(old_text, new_text)
    if rel_path.endswith(".json"):
        try:
            return strip_volatile_price_timestamps(json.loads(old_text)) == strip_volatile_price_timestamps(json.loads(new_text))
        except json.JSONDecodeError:
            return False
    return False


def clean_timestamp_only_price_changes(lines: list[str], dirty_paths: list[str]) -> bool:
    """Clear harmless generated price-cache timestamp churn before kickstarting refresh.

    The full refresh rewrites fetched_at/updated_at fields in generated price files. If a
    manual/data run is interrupted before commit, those timestamp-only diffs block every
    guarded launchd refresh. Only auto-reset the narrow generated price files when all
    semantic fields match HEAD.
    """
    if not dirty_paths:
        return True
    unique_paths = sorted(set(dirty_paths))
    if any(path not in PRICE_TIMESTAMP_ONLY_PATHS for path in unique_paths):
        return False
    if DRY_RUN:
        if not all(generated_price_change_is_timestamp_only(path) for path in unique_paths):
            return False
        append_fix(lines, "DRY-RUN would reset timestamp-only generated price-cache changes")
        return True
    lock_fd, reason = acquire_refresh_mutation_lock()
    if lock_fd is None:
        message = f"deferred timestamp-only generated price-cache cleanup: {reason or 'refresh lock unavailable'}"
        if reason == "refresh lock is active":
            append_fix(lines, message)
        else:
            append_issue(lines, message)
        return False
    try:
        # Re-read and compare every candidate only after owning the mutation
        # lock. Otherwise a manual or scheduled generator could make a semantic
        # change between this check and checkout and have valid work erased.
        if not all(generated_price_change_is_timestamp_only(path) for path in unique_paths):
            return False
        result = maybe_run(
            lines,
            "reset timestamp-only generated price-cache changes blocking launchd refresh",
            ["git", "checkout", "--", *unique_paths],
            timeout=30,
        )
        return result.code == 0
    finally:
        release_refresh_mutation_lock(lock_fd)


def maybe_run(lines: list[str], description: str, cmd: list[str], *, cwd: Path | None = REPO_DIR, timeout: int = 90) -> Result:
    if DRY_RUN:
        append_fix(lines, f"DRY-RUN would {description}")
        return Result(0, "", "")
    result = run(cmd, cwd=cwd, timeout=timeout)
    if result.code == 0:
        append_fix(lines, description)
    else:
        append_issue(lines, f"failed to {description}: exit {result.code}; {result.out or result.err}")
    return result


def plist_needs_reinstall(issues: list[str], spec: LaunchdSpec | None = None) -> bool:
    spec = launchd_specs()[0] if spec is None else spec
    if not spec.plist_path.exists():
        issues.append(f"{spec.name} launchd plist missing")
        return True
    try:
        data = plistlib.loads(spec.plist_path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        issues.append(f"{spec.name} launchd plist unreadable: {type(exc).__name__}")
        return True

    actual_environment = data.get("EnvironmentVariables") or {}
    if not isinstance(actual_environment, dict):
        issues.append(f"{spec.name} launchd plist drift: EnvironmentVariables")
        return True
    checks = {
        "Label": data.get("Label"),
        "ProgramArguments": tuple(data.get("ProgramArguments") or []),
        "WorkingDirectory": data.get("WorkingDirectory"),
        "StartInterval": data.get("StartInterval"),
        "RunAtLoad": data.get("RunAtLoad"),
        "ProcessType": data.get("ProcessType"),
        "ThrottleInterval": data.get("ThrottleInterval"),
        "Umask": data.get("Umask"),
        "StandardOutPath": data.get("StandardOutPath"),
        "StandardErrorPath": data.get("StandardErrorPath"),
    }
    expected = {
        "Label": spec.label,
        "ProgramArguments": spec.program_arguments,
        "WorkingDirectory": str(REPO_DIR),
        "StartInterval": spec.interval_seconds,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "ThrottleInterval": spec.throttle_interval,
        "Umask": 0o077,
        "StandardOutPath": str(spec.standard_out_path),
        "StandardErrorPath": str(spec.standard_error_path),
    }
    drift = [name for name, actual in checks.items() if actual != expected[name]]
    drift.extend(
        f"EnvironmentVariables.{key}"
        for key, value in spec.required_environment
        if actual_environment.get(key) != value
    )
    high_risk_environment = {
        "BASH_ENV",
        "ENV",
        "PYTHONHOME",
        "PYTHONPATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
    }
    unexpected_high_risk = sorted(
        key
        for key in actual_environment
        if key in high_risk_environment or key.startswith("DYLD_")
    )
    drift.extend(f"EnvironmentVariables.{key}" for key in unexpected_high_risk)
    if drift:
        issues.append(f"{spec.name} launchd plist drift: " + ", ".join(drift))
        return True
    return False


def launchctl_print(label: str = HOURLY_LABEL) -> Result:
    return run(["launchctl", "print", launch_target(label)], cwd=None, timeout=20)


def inspect_active_lock(path: Path) -> tuple[bool, float | None]:
    """Return whether a lock is held and its trustworthy in-file start time.

    A leftover file is not an active attempt. Only flock contention counts as active;
    metadata is then read through the already-open descriptor to avoid a path swap.
    """
    try:
        fd = open_existing_private_file(path, writable=True)
    except (FileNotFoundError, OSError, SecurePathError):
        return False, None
    acquired = False
    active = False
    payload = ""
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            active = True
        if active:
            os.lseek(fd, 0, os.SEEK_SET)
            payload = os.read(fd, 4096).decode("utf-8", errors="replace")
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
    if not active:
        return False, None
    started_at = None
    for line in payload.splitlines():
        if line.startswith("started_at_utc="):
            started_at = parse_iso_timestamp(line.partition("=")[2].strip())
            break
    return True, started_at


def refresh_is_active() -> bool:
    """Return whether the shared refresh lock is held by another process.

    The refresh intentionally makes tracked generated files dirty while it builds. The
    watchdog must not classify that expected, in-progress state as a pre-existing dirty
    worktree that blocks the next refresh. An unlocked/stale lock file is not enough to
    suppress an alert; only an active flock counts.
    """
    return inspect_active_lock(REFRESH_LOCK_PATH)[0]


def acquire_refresh_mutation_lock() -> tuple[int | None, str | None]:
    """Try to reserve the shared refresh lock before a watchdog mutation.

    The returned descriptor must remain open until the protected mutation finishes.
    A non-empty reason with no descriptor is deliberately fail-closed.
    """
    fd: int | None = None
    try:
        fd = open_private_lock(REFRESH_LOCK_PATH)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            fd = None
            return None, "refresh lock is active"
        return fd, None
    except (OSError, SecurePathError) as exc:
        if fd is not None:
            os.close(fd)
        return None, f"refresh lock could not be acquired: {type(exc).__name__}: {exc}"


def release_refresh_mutation_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def fresh_active_attempt(
    *,
    lock_held: bool,
    started_ts: float | None,
    completed_ts: float | None,
    now: float,
    grace_seconds: int,
) -> bool:
    """Identify a newer, bounded in-flight attempt without trusting launchd state alone."""
    if not lock_held or started_ts is None:
        return False
    if completed_ts is not None and started_ts <= completed_ts:
        return False
    age = now - started_ts
    return 0 <= age < grace_seconds


def watcher_completion_lag_issue(issue: str) -> bool:
    """Return true only for state signals a newly started watcher can repair."""
    lowered = issue.lower()
    return (
        lowered.startswith("watcher state missing:")
        or lowered == "watcher state has no valid last_checked_at_utc"
        or lowered.startswith("watcher state age=")
    )


def filter_watcher_issues_for_active_attempt(issues: list[str], active_attempt: bool) -> list[str]:
    if not active_attempt:
        return list(issues)
    return [issue for issue in issues if not watcher_completion_lag_issue(issue)]


def expected_live_publish_lag(message: str) -> bool:
    """Recognize only local-ahead parity lag while an active publisher deploys."""
    lowered = message.lower()
    return (
        lowered.startswith("live refresh status block ") and " trails local generated block " in lowered
    ) or (
        lowered.startswith("live refresh status ") and " differs from local validated status at block " in lowered
    )


def launchd_fault_present(issues: Iterable[str]) -> bool:
    markers = (
        "launchd plist missing",
        "launchd plist unreadable",
        "launchd plist drift",
        "launchctl cannot see",
        "could not inspect disabled launchd",
        "failed to reinstall launchd",
        "failed to enable launchd",
        "deferred launchd",
    )
    return any(any(marker in issue.lower() for marker in markers) for issue in issues)


def service_is_running(print_output: str) -> bool:
    return "state = running" in print_output or re.search(r"active count = [1-9]", print_output) is not None


def maybe_run_with_refresh_guard(
    lines: list[str],
    description: str,
    cmd: list[str],
    *,
    cwd: Path | None = REPO_DIR,
    timeout: int = 90,
    require_idle_label: str | None = None,
    release_before_run: bool = False,
) -> Result:
    """Run a repair only after a fresh shared-lock and optional service-state check."""
    if DRY_RUN:
        return maybe_run(lines, description, cmd, cwd=cwd, timeout=timeout)
    lock_fd, reason = acquire_refresh_mutation_lock()
    if lock_fd is None:
        message = f"deferred {description}: {reason or 'refresh lock unavailable'}"
        if reason == "refresh lock is active":
            append_fix(lines, message)
        else:
            append_issue(lines, message)
        return Result(75, "", reason or "refresh lock unavailable")
    try:
        if require_idle_label is not None:
            current = launchctl_print(require_idle_label)
            current_output = current.out + "\n" + current.err
            if service_is_running(current_output):
                append_fix(lines, f"deferred {description}: {require_idle_label} is currently running")
                return Result(75, "", "launchd service is running")
        if release_before_run:
            # Kickstarts need the worker to acquire the lock; installers now
            # acquire and recheck the same protected lock themselves.
            release_refresh_mutation_lock(lock_fd)
            lock_fd = None
        return maybe_run(lines, description, cmd, cwd=cwd, timeout=timeout)
    finally:
        release_refresh_mutation_lock(lock_fd)


def ensure_launchd_service(lines: list[str], spec: LaunchdSpec, *, allow_repair: bool = True) -> str:
    reinstall_reasons: list[str] = []
    if plist_needs_reinstall(reinstall_reasons, spec):
        append_issue(lines, "; ".join(reinstall_reasons))
        if allow_repair:
            maybe_run_with_refresh_guard(
                lines,
                f"reinstall launchd {spec.name} agent",
                ["bash", str(spec.installer)],
                timeout=120,
                require_idle_label=spec.label,
                release_before_run=True,
            )
        else:
            append_issue(lines, f"deferred launchd {spec.name} repair while runner disk space is low")

    printed = launchctl_print(spec.label)
    if printed.code != 0:
        append_issue(lines, f"launchctl cannot see {spec.label}: {printed.out or printed.err}")
        if allow_repair:
            maybe_run_with_refresh_guard(
                lines,
                f"reinstall launchd {spec.name} agent after launchctl miss",
                ["bash", str(spec.installer)],
                timeout=120,
                require_idle_label=spec.label,
                release_before_run=True,
            )
            printed = launchctl_print(spec.label)

    # Enabling is idempotent and cheap; do it if print-disabled says the label is disabled.
    disabled = run(["launchctl", "print-disabled", launch_domain()], cwd=None, timeout=20)
    if disabled.code == 0:
        label_quoted = f'"{spec.label}" => true'
        label_plain = f"{spec.label} => true"
        if label_quoted in disabled.out or label_plain in disabled.out:
            if allow_repair:
                maybe_run_with_refresh_guard(
                    lines,
                    f"enable launchd {spec.name} agent",
                    ["launchctl", "enable", launch_target(spec.label)],
                    cwd=None,
                    timeout=20,
                )
            else:
                append_issue(lines, f"deferred enabling launchd {spec.name} agent while runner disk space is low")
    elif disabled.err:
        append_issue(lines, f"could not inspect disabled launchd {spec.name} job: {disabled.err}")

    return printed.out + "\n" + printed.err


def ensure_launchd(lines: list[str], *, allow_repair: bool = True) -> dict[str, str]:
    return {
        spec.label: ensure_launchd_service(lines, spec, allow_repair=allow_repair)
        for spec in launchd_specs()
    }


def emit_startup_failure(lines: list[str], causes: list[str]) -> None:
    now = time.time()
    log_details = parse_refresh_log_details()
    ok, _live_msg = live_site_ok()
    last_success_ts = log_details.get("last_success_ts")
    snapshot: dict[str, Any] = {
        "detected_at_utc": iso_now(),
        "issues": [line for line in lines if line.startswith("issue:")],
        "all_actions": lines,
        "causes": causes,
        "dirty_paths": [],
        "last_success_at_utc": iso_from_ts(last_success_ts),
        "last_success_age_minutes": age_minutes(now, last_success_ts),
        "last_finished_at_utc": iso_from_ts(log_details.get("last_finished_ts")),
        "last_finished_status": log_details.get("last_finished_status"),
        "last_started_at_utc": iso_from_ts(log_details.get("last_started_ts")),
        "recent_signals": log_details.get("recent_signals", []),
        "refresh_history": compact_refresh_history(),
        "watcher_history": compact_watcher_history(),
        "live_ok": ok,
    }
    alert_message = handle_critical_alert(snapshot)
    if alert_message:
        print(alert_message)


def main() -> int:
    lines: list[str] = []

    if not REPO_DIR.exists():
        append_issue(lines, f"repo missing: {REPO_DIR}")
        emit_startup_failure(lines, ["runner_repo_missing"])
        return 1

    git_tree = run(["git", "rev-parse", "--is-inside-work-tree"], timeout=20)
    if git_tree.code != 0 or git_tree.out.strip() != "true":
        append_issue(lines, f"not a git worktree: {REPO_DIR}")
        emit_startup_failure(lines, ["runner_repo_not_git_worktree"])
        return 1

    required_scripts = (REFRESH_SCRIPT, WATCHER_SCRIPT, HOURLY_INSTALL_SCRIPT, WATCHER_INSTALL_SCRIPT)
    for path in required_scripts:
        if not path.exists():
            append_issue(lines, f"required script missing: {path}")
    if any(not path.exists() for path in required_scripts):
        emit_startup_failure(lines, ["required_runner_script_missing"])
        return 1

    for path in required_scripts:
        if not os.access(path, os.X_OK):
            maybe_run(lines, f"make {path.name} executable", ["chmod", "+x", str(path)], cwd=None, timeout=20)

    runner_venv_path = REPO_DIR / ".venv"
    runner_runtime_failed = (runner_venv_path.exists() or runner_venv_path.is_symlink()) and (
        not runner_python_ready() or not runner_python_shim_ready()
    )
    if runner_runtime_failed:
        append_issue(lines, "repo Python virtualenv failed the pinned Crypto 3.23.0 Keccak runtime check")

    permission_hardening_failed = harden_runner_permissions(lines)
    refresh_in_progress = refresh_is_active()
    initial_launch_outputs = {
        HOURLY_LABEL: launchctl_print(HOURLY_LABEL),
        WATCHER_LABEL: launchctl_print(WATCHER_LABEL),
    }
    active_services = {
        label
        for label, result in initial_launch_outputs.items()
        if service_is_running(result.out + "\n" + result.err)
    }
    if refresh_in_progress:
        # A manual or watcher-triggered publisher owns the same files even if its
        # launchd label cannot be identified from the refresh lock alone.
        active_services.update({HOURLY_LABEL, WATCHER_LABEL})
    rotation_failed = rotate_managed_logs(lines, active_services)
    disk_issues, disk_summary = inspect_disk_free()
    for issue in disk_issues:
        append_issue(lines, issue)
    disk_low = bool(disk_issues)

    dirty_blocking = False
    branch = run(["git", "branch", "--show-current"], timeout=20)
    status = run(["git", "status", "--porcelain", "--untracked-files=no"], timeout=30)
    dirty_paths: list[str] = tracked_dirty_paths(status.out) if status.code == 0 else []
    if branch.code == 0 and branch.out.strip() != "main":
        if status.code == 0 and not status.out.strip():
            maybe_run_with_refresh_guard(
                lines,
                "switch runner repo back to main",
                ["git", "switch", "main"],
                timeout=60,
            )
        else:
            dirty_blocking = True
            append_issue(lines, f"runner repo on {branch.out.strip() or 'unknown'} with tracked changes; not switching")
    if status.code == 0 and status.out.strip():
        if refresh_is_active() and branch.code == 0 and branch.out.strip() == "main":
            # The refresh lock is acquired before the generator writes tracked output.
            # Those changes are expected until the active refresh commits or exits.
            dirty_paths = []
        elif clean_timestamp_only_price_changes(lines, dirty_paths):
            status = run(["git", "status", "--porcelain", "--untracked-files=no"], timeout=30)
            dirty_paths = tracked_dirty_paths(status.out) if status.code == 0 else dirty_paths
            if status.code == 0 and status.out.strip():
                dirty_blocking = True
                append_issue(lines, "tracked worktree changes remain after timestamp-only price cleanup; hourly refresh will refuse to overwrite")
        else:
            # The refresh can acquire the lock between the status read and guarded
            # checkout attempt. Recheck instead of converting that expected race into
            # a false dirty-worktree incident.
            status = run(["git", "status", "--porcelain", "--untracked-files=no"], timeout=30)
            dirty_paths = tracked_dirty_paths(status.out) if status.code == 0 else dirty_paths
            if refresh_is_active() and branch.code == 0 and branch.out.strip() == "main":
                dirty_paths = []
            elif status.code == 0 and not status.out.strip():
                dirty_paths = []
            else:
                dirty_blocking = True
                append_issue(lines, "tracked worktree changes present; hourly refresh will refuse to overwrite")
    elif status.code != 0:
        dirty_blocking = True
        append_issue(lines, f"could not inspect git status: {status.out or status.err}")

    launch_outputs = ensure_launchd(lines, allow_repair=not disk_low)
    print_output = launch_outputs.get(HOURLY_LABEL, "")
    watcher_output = launch_outputs.get(WATCHER_LABEL, "")

    log_details = parse_refresh_log_details()
    last_success_ts = log_details.get("last_success_ts")
    last_finished_status = log_details.get("last_finished_status")
    last_error = log_details.get("last_error")
    now = time.time()
    refresh_attempt_active = fresh_active_attempt(
        lock_held=refresh_is_active(),
        started_ts=log_details.get("last_started_ts"),
        completed_ts=log_details.get("last_finished_ts"),
        now=now,
        grace_seconds=REFRESH_ACTIVE_GRACE_SECONDS,
    )

    all_watcher_issues, watcher_state = inspect_watcher_state(now)
    watcher_lock_held, watcher_started_ts = inspect_active_lock(WATCHER_LOCK_PATH)
    watcher_checked_ts = parse_iso_timestamp(watcher_state.get("last_checked_at_utc"))
    watcher_attempt_active = fresh_active_attempt(
        lock_held=watcher_lock_held,
        started_ts=watcher_started_ts,
        completed_ts=watcher_checked_ts,
        now=now,
        grace_seconds=WATCHER_ACTIVE_GRACE_SECONDS,
    ) or (watcher_lock_held and refresh_attempt_active)
    watcher_issues = filter_watcher_issues_for_active_attempt(all_watcher_issues, watcher_attempt_active)
    suppressed_watcher_issues = [issue for issue in all_watcher_issues if issue not in watcher_issues]
    if suppressed_watcher_issues:
        append_fix(
            lines,
            f"deferred {len(suppressed_watcher_issues)} watcher completion-lag check(s) while a newer locked run is active",
        )
    for issue in watcher_issues:
        append_issue(lines, issue)
    if watcher_issues:
        if disk_low:
            append_issue(lines, "watcher repair kickstart deferred while runner disk space is low")
        elif service_is_running(watcher_output):
            append_issue(lines, "watcher state is unhealthy, but the watcher job is currently running; left it alone")
        else:
            maybe_run_with_refresh_guard(
                lines,
                "kickstart onchain auction watcher after unhealthy state",
                ["launchctl", "kickstart", launch_target(WATCHER_LABEL)],
                cwd=None,
                timeout=30,
                require_idle_label=WATCHER_LABEL,
                release_before_run=True,
            )

    stale = last_success_ts is None or (now - last_success_ts) > STALE_SUCCESS_SECONDS
    critical_stale = last_success_ts is None or (now - last_success_ts) > CRITICAL_STALE_SECONDS
    failed_last = last_finished_status is not None and last_finished_status != 0
    effective_critical_stale = critical_stale and not refresh_attempt_active
    effective_failed_last = failed_last and not refresh_attempt_active
    if effective_critical_stale:
        if last_success_ts is None:
            append_issue(lines, "no successful refresh found; critical local-runner alert threshold crossed")
        else:
            append_issue(lines, f"last successful refresh age={int((now - last_success_ts) / 60)}m exceeds critical threshold={int(CRITICAL_STALE_SECONDS / 60)}m")
    if effective_failed_last:
        append_issue(lines, f"latest refresh finished with nonzero status={last_finished_status}")
    if last_error and not refresh_attempt_active:
        append_issue(lines, f"latest refresh log error: {sanitize(str(last_error), 300)}")

    if stale or failed_last:
        if refresh_attempt_active:
            append_fix(lines, "deferred prior refresh staleness/failure while a newer locked refresh is active")
        elif disk_low:
            append_issue(lines, "hourly refresh kickstart deferred while runner disk space is low")
        elif dirty_blocking:
            append_issue(lines, "refresh appears stale/failed, but tracked worktree changes still block safe kickstart")
        elif service_is_running(print_output):
            append_issue(lines, "refresh appears stale/failed, but launchd job is currently running; left it alone")
        else:
            reason = "no successful refresh found" if last_success_ts is None else f"last successful refresh age={int((now - last_success_ts) / 60)}m"
            if failed_last:
                reason += f", last status={last_finished_status}"
            maybe_run_with_refresh_guard(
                lines,
                f"kickstart hourly refresh agent ({reason})",
                ["launchctl", "kickstart", launch_target(HOURLY_LABEL)],
                cwd=None,
                timeout=30,
                require_idle_label=HOURLY_LABEL,
                release_before_run=True,
            )

    metrics = load_metrics()
    ok, live_msg = live_site_ok(load_local_refresh_status())
    live_publish_pending = not ok and refresh_attempt_active and expected_live_publish_lag(live_msg)
    effective_live_ok = ok or live_publish_pending
    if live_publish_pending:
        append_fix(lines, "deferred live parity check while the newer locked snapshot is publishing")
    elif not ok:
        append_issue(lines, live_msg)
    latest_block = metrics.get("latest_block")
    current_dog = metrics.get("current_auction_token_id")

    issue_lines = [line for line in lines if line.startswith("issue:")]
    causes = derive_causes(
        issues=issue_lines,
        dirty_paths=dirty_paths if dirty_blocking else [],
        log_details=log_details,
        stale=effective_critical_stale,
        failed_last=effective_failed_last,
        live_ok=effective_live_ok,
        launch_output=print_output,
        now=now,
    )
    snapshot: dict[str, Any] = {
        "detected_at_utc": iso_now(),
        "issues": issue_lines,
        "all_actions": lines,
        "causes": causes,
        "dirty_paths": dirty_paths if dirty_blocking else [],
        "last_success_at_utc": iso_from_ts(last_success_ts),
        "last_success_age_minutes": age_minutes(now, last_success_ts),
        "last_finished_at_utc": iso_from_ts(log_details.get("last_finished_ts")),
        "last_finished_status": last_finished_status,
        "last_started_at_utc": iso_from_ts(log_details.get("last_started_ts")),
        "recent_signals": log_details.get("recent_signals", []),
        "refresh_history": compact_refresh_history(),
        "watcher_history": compact_watcher_history(),
        "watcher_state": watcher_state,
        "disk": disk_summary,
        "live_ok": effective_live_ok,
        "live_actual_ok": ok,
        "live_publish_pending": live_publish_pending,
        "latest_block": latest_block,
        "current_dog": current_dog,
    }
    critical = bool(issue_lines) and (
        effective_critical_stale
        or effective_failed_last
        or dirty_blocking
        or not effective_live_ok
        or bool(watcher_issues)
        or bool(disk_issues)
        or rotation_failed
        or permission_hardening_failed
        or runner_runtime_failed
        or launchd_fault_present(issue_lines)
        or any("required script missing" in line.lower() for line in issue_lines)
    )

    if critical:
        alert_message = handle_critical_alert(snapshot)
        if alert_message:
            print(alert_message)
        return 1

    recovery_pending = (
        refresh_attempt_active and (critical_stale or failed_last or live_publish_pending)
    ) or (watcher_attempt_active and bool(suppressed_watcher_issues))
    if not recovery_pending:
        alert_message = handle_recovery_alert(snapshot)
        if alert_message:
            print(alert_message)
            return 0

    if lines:
        suffix = []
        if latest_block:
            suffix.append(f"local block {latest_block}")
        if current_dog:
            suffix.append(f"current dog {current_dog}")
        if ok:
            suffix.append("live HTTP 200")
        extra = f"\n- status: {', '.join(suffix)}" if suffix else ""
        dry = " [dry-run]" if DRY_RUN else ""
        print(f"Degen Dogs local runner health{dry}:\n- " + "\n- ".join(lines) + extra)
    return 0


def handle_cli_args(argv: list[str]) -> int | None:
    """Handle informational/invalid CLI arguments before any health side effects."""
    if not argv:
        return None
    if argv in (["-h"], ["--help"]):
        print("usage: degen_dogs_runner_health.py")
        print("Run without arguments to inspect and, when configured, repair/alert on runner health.")
        return 0
    print(f"error: unsupported argument: {argv[0]}", file=sys.stderr)
    return 2


def cli_main(argv: list[str]) -> int:
    cli_result = handle_cli_args(argv)
    return main() if cli_result is None else cli_result


if __name__ == "__main__":
    try:
        raise SystemExit(cli_main(sys.argv[1:]))
    except subprocess.TimeoutExpired as exc:
        print(f"Degen Dogs local runner health fatal: timeout running {exc.cmd}")
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 - let cron alert on script defects.
        print(f"Degen Dogs local runner health fatal: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
