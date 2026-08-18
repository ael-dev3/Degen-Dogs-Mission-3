#!/usr/bin/env python3
"""Read-only health probe for the WSL2/systemd dashboard publisher.

The macOS health agent intentionally understands launchd and repairs plists.
This smaller probe leaves service supervision to systemd and verifies the
signals that matter for a second publisher: timers, local locks/state, Git
cleanliness, generated freshness, remote freshness, and free disk space.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_remote_freshness as remote_freshness  # noqa: E402

TIMER_UNITS = (
    "degen-dogs-watcher.timer",
    "degen-dogs-hourly.timer",
    "degen-dogs-health.timer",
)
WORKER_UNITS = (
    "degen-dogs-watcher.service",
    "degen-dogs-hourly.service",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip().replace(" ", "T")
    if not text:
        return None
    if not text.endswith("Z") and "+" not in text[10:]:
        text += "Z"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc
    if value < minimum:
        raise SystemExit(f"{name} must be at least {minimum}")
    return value


def run(command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env={
                "HOME": os.environ.get("HOME", ""),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 127, "", type(exc).__name__)


def systemd_properties(unit: str) -> dict[str, str]:
    result = run(
        [
            "systemctl",
            "show",
            unit,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=UnitFileState",
            "--property=Result",
        ]
    )
    if result.returncode != 0:
        return {"error": (result.stderr or result.stdout or f"exit {result.returncode}").strip()[:300]}
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    return properties


def read_json(path: Path, *, max_bytes: int = 2 * 1024 * 1024) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > max_bytes:
            raise ValueError("not a bounded regular file")
        with os.fdopen(descriptor, encoding="utf-8", errors="strict") as handle:
            descriptor = -1
            payload = json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise ValueError("JSON root is not an object")
    return payload


def refresh_lock_active(path: Path) -> bool:
    try:
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise RuntimeError("shared refresh lock is not a private owned single-link regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def age_seconds(value: Any, now: datetime) -> int | None:
    parsed = parse_utc(value)
    return None if parsed is None else max(0, int((now - parsed).total_seconds()))


def main() -> int:
    now = utc_now()
    lock_dir = Path(os.environ.get("DEGEN_DOGS_LOCK_DIR", "/var/cache/degen-dogs")).expanduser()
    log_dir = Path(os.environ.get("DEGEN_DOGS_LOG_DIR", "/var/log/degen-dogs")).expanduser()
    state_path = Path(
        os.environ.get("MISSION3_WATCHER_STATE_PATH", str(ROOT / ".local" / "mission3_onchain_tracker_state.json"))
    ).expanduser()
    status_path = ROOT / "generated" / "refresh_status.json"
    lock_path = Path(
        os.environ.get("DEGEN_DOGS_REFRESH_LOCK_PATH", str(lock_dir / "refresh.lock"))
    ).expanduser()

    watcher_stale_seconds = env_int("DEGEN_DOGS_HEALTH_WATCHER_STALE_SECONDS", 180, minimum=30)
    pending_stale_seconds = env_int("DEGEN_DOGS_HEALTH_PENDING_STALE_SECONDS", 900, minimum=60)
    local_stale_seconds = env_int("DEGEN_DOGS_HEALTH_LIVE_STALE_SECONDS", 5400, minimum=300)
    min_free_bytes = env_int("DEGEN_DOGS_HEALTH_MIN_FREE_BYTES", 5 * 1024**3, minimum=1)
    min_free_percent = env_int("DEGEN_DOGS_HEALTH_MIN_FREE_PERCENT", 5, minimum=1)

    problems: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    timers: dict[str, Any] = {}
    for unit in TIMER_UNITS:
        properties = systemd_properties(unit)
        timers[unit] = properties
        if properties.get("error"):
            problems.append(f"{unit}: {properties['error']}")
        elif properties.get("LoadState") != "loaded" or properties.get("ActiveState") != "active":
            problems.append(
                f"{unit} is not active (load={properties.get('LoadState')} active={properties.get('ActiveState')})"
            )
        elif properties.get("UnitFileState") not in {"enabled", "enabled-runtime"}:
            problems.append(f"{unit} is not enabled ({properties.get('UnitFileState')})")
    checks["timers"] = timers

    workers: dict[str, Any] = {}
    for unit in WORKER_UNITS:
        properties = systemd_properties(unit)
        workers[unit] = properties
        if properties.get("error"):
            problems.append(f"{unit}: {properties['error']}")
        elif properties.get("ActiveState") == "failed" or properties.get("Result") not in {"", "success"}:
            problems.append(
                f"{unit} last result is unhealthy (active={properties.get('ActiveState')} result={properties.get('Result')})"
            )
    checks["workers"] = workers

    try:
        lock_active = refresh_lock_active(lock_path)
    except Exception as exc:  # noqa: BLE001 - only the exception class is reported
        lock_active = False
        problems.append(f"shared refresh lock validation failed ({type(exc).__name__})")
    checks["refresh_lock_active"] = lock_active

    try:
        watcher = read_json(state_path)
        watcher_age = age_seconds(watcher.get("last_checked_at_utc"), now)
        checks["watcher"] = {
            "age_seconds": watcher_age,
            "pending_refresh": bool(watcher.get("pending_refresh")),
            "consecutive_rpc_failures": int(watcher.get("consecutive_rpc_failures") or 0),
            "consecutive_refresh_failures": int(watcher.get("consecutive_refresh_failures") or 0),
            "last_refresh_status": str(watcher.get("last_refresh_status") or ""),
        }
        if watcher_age is None:
            problems.append("watcher state has no valid last_checked_at_utc")
        elif watcher_age > watcher_stale_seconds and not lock_active:
            problems.append(f"watcher state is stale ({watcher_age}s > {watcher_stale_seconds}s)")
        if checks["watcher"]["consecutive_rpc_failures"] >= 3:
            problems.append("watcher has at least three consecutive RPC failures")
        if checks["watcher"]["consecutive_refresh_failures"] >= 3:
            problems.append("watcher has at least three consecutive refresh failures")
        if watcher.get("pending_refresh"):
            pending_age = age_seconds(watcher.get("pending_refresh_since_utc"), now)
            checks["watcher"]["pending_age_seconds"] = pending_age
            if pending_age is None or pending_age > pending_stale_seconds:
                problems.append("watcher has a stale pending refresh")
    except FileNotFoundError:
        problems.append("watcher state file is missing")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"watcher state is unreadable ({type(exc).__name__})")

    try:
        local_status = read_json(status_path)
        local_problem = remote_freshness.status_problem(local_status)
        local_age = age_seconds(local_status.get("last_successful_refresh_time_utc"), now)
        checks["local_status"] = {
            "age_seconds": local_age,
            "latest_generated_block": local_status.get("latest_generated_block"),
            "current_dog_token_id": local_status.get("current_dog_token_id"),
            "problem": local_problem,
        }
        if local_problem:
            problems.append(f"local refresh status is invalid: {local_problem}")
        elif local_age is None or local_age > local_stale_seconds:
            problems.append(f"local refresh status is stale ({local_age}s; limit {local_stale_seconds}s)")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"local refresh status is unreadable ({type(exc).__name__})")

    raw: Any = None
    pages: Any = None
    raw_error = ""
    pages_error = ""
    timeout_seconds = env_int("DEGEN_DOGS_HEALTH_REMOTE_TIMEOUT_SECONDS", 15, minimum=2)
    try:
        raw = remote_freshness.fetch_json(
            remote_freshness.DEFAULT_RAW_URL,
            timeout_seconds,
            expected_url=remote_freshness.DEFAULT_RAW_URL,
        )
    except Exception as exc:  # noqa: BLE001
        raw_error = f"raw-main fetch failed ({type(exc).__name__})"
    try:
        pages = remote_freshness.fetch_json(
            remote_freshness.DEFAULT_PAGES_URL,
            timeout_seconds,
            expected_url=remote_freshness.DEFAULT_PAGES_URL,
        )
    except Exception as exc:  # noqa: BLE001
        pages_error = f"Pages fetch failed ({type(exc).__name__})"
    remote_report = remote_freshness.assess_freshness(
        raw,
        pages,
        now=now,
        max_raw_age_seconds=local_stale_seconds,
        propagation_grace_seconds=900,
        raw_fetch_error=raw_error,
        pages_fetch_error=pages_error,
    )
    checks["remote"] = remote_report
    if remote_report["incident"]:
        problems.append(
            "remote dashboard freshness is unhealthy: "
            + (remote_report.get("raw_problem") or remote_report.get("pages_problem") or "unknown")
        )

    git_branch = run(["git", "branch", "--show-current"])
    git_status = run(["git", "status", "--porcelain", "--untracked-files=no"])
    checks["git"] = {
        "branch": git_branch.stdout.strip() if git_branch.returncode == 0 else "",
        "tracked_dirty": bool(git_status.stdout.strip()) if git_status.returncode == 0 else None,
    }
    if git_branch.returncode != 0 or git_branch.stdout.strip() != os.environ.get("DEGEN_DOGS_BRANCH", "main"):
        problems.append("publisher clone is not on the configured branch")
    if git_status.returncode != 0:
        problems.append("publisher clone Git status failed")
    elif git_status.stdout.strip() and not lock_active:
        # Close the common probe/status race: a publisher can acquire the lock
        # and begin generation after the first probe but before Git status.
        try:
            lock_active = refresh_lock_active(lock_path)
            checks["refresh_lock_active"] = lock_active
        except Exception as exc:  # noqa: BLE001
            problems.append(f"shared refresh lock recheck failed ({type(exc).__name__})")
        if not lock_active:
            problems.append("publisher clone has tracked changes while no refresh owns the lock")

    filesystems: dict[str, Any] = {}
    seen_devices: set[int] = set()
    for label, path in (("repo", ROOT), ("log", log_dir), ("cache", lock_dir)):
        try:
            details = path.stat()
            if details.st_dev in seen_devices:
                continue
            seen_devices.add(details.st_dev)
            usage = shutil.disk_usage(path)
            free_percent = int(100 * usage.free / usage.total) if usage.total else 0
            filesystems[label] = {"free_bytes": usage.free, "free_percent": free_percent}
            if usage.free < min_free_bytes or free_percent < min_free_percent:
                problems.append(
                    f"{label} filesystem is low on space ({usage.free} bytes, {free_percent}% free)"
                )
        except OSError as exc:
            problems.append(f"{label} filesystem cannot be inspected ({type(exc).__name__})")
    checks["filesystems"] = filesystems

    # Log rotation is external, but surface unexpectedly large files before they
    # can exhaust the WSL VHD. This is warning-only because logrotate may be due.
    for path in (log_dir / "refresh.log", log_dir / "watch-onchain.log", ROOT / ".local" / "watcher_checks.jsonl"):
        try:
            if path.stat().st_size > 32 * 1024 * 1024:
                warnings.append(f"managed log exceeds 32 MiB: {path.name}")
        except FileNotFoundError:
            continue

    report = {
        "kind": "degen_dogs_wsl_runner_health",
        "checked_at_utc": utc_text(now),
        "runner_id": os.environ.get("DEGEN_DOGS_RUNNER_ID", "windows-wsl"),
        "status": "healthy" if not problems else "unhealthy",
        "problems": problems,
        "warnings": warnings,
        "checks": checks,
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
