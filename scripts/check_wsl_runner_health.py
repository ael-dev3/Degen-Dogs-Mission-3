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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_remote_freshness as remote_freshness  # noqa: E402
import refresh_telemetry  # noqa: E402
import runner_publication_state  # noqa: E402

TIMER_UNITS = (
    "degen-dogs-watcher.timer",
    "degen-dogs-hourly.timer",
    "degen-dogs-health.timer",
)
WORKER_UNITS = (
    "degen-dogs-watcher.service",
    "degen-dogs-hourly.service",
)
PUBLISHED_RESULTS = {
    "success_no_diff",
    "success_superseded_by_peer",
    "success_pushed",
    "success_pushed_live_timeout",
}
QUEUE_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def terminal_publication_problem(row: dict[str, object]) -> str:
    result = str(row.get("result") or "")
    if not result:
        return "latest terminal publication telemetry is missing"
    if result in PUBLISHED_RESULTS:
        return ""
    if result == "failed" or result.endswith("failed"):
        return f"latest terminal publication failed ({result})"
    return f"latest terminal publication was not published ({result})"


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


STRICT_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


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


def public_commit_sha(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if len(text) == 40 and all(character in "0123456789abcdef" for character in text) else None


def queue_digest(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if QUEUE_DIGEST.fullmatch(text) else None


def public_timestamp_and_age(value: Any, now: datetime) -> tuple[str | None, int | None, bool]:
    """Return a canonical UTC value, non-negative age, and >30s future-skew flag."""
    if not isinstance(value, str) or not STRICT_UTC.fullmatch(value):
        return None, None, False
    parsed = parse_utc(value)
    if parsed is None or utc_text(parsed) != value:
        return None, None, False
    text = utc_text(parsed)
    delta = (now - parsed).total_seconds()
    return text, max(0, int(delta)), delta < -30


def watcher_stale_requires_failure(
    watcher_age_seconds: int | None,
    stale_seconds: int,
    *,
    publisher_lock_active: bool,
    publication_mode: str,
) -> bool:
    """Keep the old inline lock exemption, but never apply it to queue mode."""
    if watcher_age_seconds is None or watcher_age_seconds <= stale_seconds:
        return False
    return publication_mode == "queue" or not publisher_lock_active


def publication_health_summary(
    snapshot: dict[str, Any],
    *,
    now: datetime,
    publisher_lock_active: bool,
    provider_failure_count: int = 0,
    configured_queue_mode: bool = False,
) -> dict[str, Any]:
    """Project one protected state-lock snapshot to a strict public allowlist.

    This deliberately never copies a state record, digest, proof fingerprint,
    path, observation payload, provider value, or journal value into health
    output.  State validation belongs to ``read_publication_health_snapshot``;
    this layer adds queue causality and age policy only.
    """
    empty = {
        "queue_mode": bool(configured_queue_mode),
        "latest_observed_generation": None,
        "latest_observed_at_utc": None,
        "handled_generation": None,
        "handled_pushed_generation": None,
        "handled_pushed_commit_sha": None,
        "queue_lag": 0,
        "queue_age_seconds": None,
        "unresolved_verification_generation": None,
        "unresolved_verification_commit_sha": None,
        "unresolved_verification_age_seconds": None,
        "pages_verification_state": "unavailable",
        "last_direct_data_compatible_static_block": None,
        "provider_failure_count": max(0, int(provider_failure_count)),
        "problems": [],
    }
    problems: list[str] = []
    try:
        watermark = snapshot.get("last_generation", 0)
        if not isinstance(watermark, int) or isinstance(watermark, bool) or watermark < 0:
            raise ValueError("generation watermark")
        latest = snapshot.get("latest")
        pending = snapshot.get("pending")
        checkpoint = snapshot.get("checkpoint")
        receipt = snapshot.get("pages_verified")
        journal = snapshot.get("journal")
        records = (latest, pending, checkpoint, receipt, journal)
        if any(record is not None and not isinstance(record, dict) for record in records):
            raise ValueError("record shape")
    except Exception:
        empty["problems"] = ["publication_state_integrity_failure"]
        return empty

    queue_mode = bool(configured_queue_mode or watermark or any(record is not None for record in records))
    empty["queue_mode"] = queue_mode
    if not queue_mode:
        return empty

    latest_record = latest.get("record") if latest else None
    latest_digest = queue_digest(latest.get("record_digest")) if latest else None
    if latest is not None and not isinstance(latest_record, dict):
        problems.append("publication_state_integrity_failure")
        latest_record = None
    # The watermark is durable.  The latest file is an ephemeral handoff record
    # and is only evidence when it is exactly the durable latest generation.
    empty["latest_observed_generation"] = watermark or None
    latest_generation = None
    latest_created_at: str | None = None
    if latest_record is not None:
        latest_generation = latest_record.get("generation")
        if not isinstance(latest_generation, int) or isinstance(latest_generation, bool) or latest_generation < 1:
            problems.append("publication_state_integrity_failure")
            latest_generation = None
        latest_created_at, queue_age, future = public_timestamp_and_age(latest_record.get("created_at_utc"), now)
        if latest_created_at is None:
            problems.append("publication_state_integrity_failure")
        elif latest_generation == watermark:
            empty["latest_observed_at_utc"] = latest_created_at
            empty["queue_age_seconds"] = queue_age
        if future:
            problems.append("publication_future_clock_skew")

    handled_generation = None
    checkpoint_generation = None
    checkpoint_outcome = None
    checkpoint_push_at: str | None = None
    checkpoint_commit = None
    checkpoint_digest = None
    if checkpoint is not None:
        generation = checkpoint.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            problems.append("publication_state_integrity_failure")
        else:
            handled_generation = generation
            checkpoint_generation = generation
            empty["handled_generation"] = generation
        checkpoint_outcome = checkpoint.get("outcome")
        if checkpoint_outcome not in {"pushed", "no_diff", "peer_superseded"}:
            problems.append("publication_state_integrity_failure")
        if checkpoint_outcome == "pushed":
            empty["handled_pushed_generation"] = handled_generation
            checkpoint_commit = public_commit_sha(checkpoint.get("commit_sha"))
            checkpoint_digest = queue_digest(checkpoint.get("queue_digest"))
            checkpoint_push_at, _checkpoint_age, checkpoint_future = public_timestamp_and_age(
                checkpoint.get("push_completed_at_utc"), now
            )
            if checkpoint_commit is None or checkpoint_digest is None or checkpoint_push_at is None:
                problems.append("publication_state_integrity_failure")
            else:
                empty["handled_pushed_commit_sha"] = checkpoint_commit
            if checkpoint_future:
                problems.append("publication_future_clock_skew")

    effective_handled = handled_generation or 0
    empty["queue_lag"] = max(0, watermark - effective_handled)
    if watermark < effective_handled:
        problems.append("publication_state_integrity_failure")
    if watermark > effective_handled and (latest_generation != watermark or latest_created_at is None):
        problems.append("publication_latest_record_lost")

    pending_generation = None
    pending_commit = None
    pending_push_at: str | None = None
    pending_digest = None
    pending_record = pending.get("record") if isinstance(pending, dict) else None
    if pending is not None and not isinstance(pending_record, dict):
        problems.append("publication_state_integrity_failure")
    if pending_record is not None:
        pending_generation = pending_record.get("generation")
        pending_commit = public_commit_sha(pending_record.get("commit_sha"))
        pending_digest = queue_digest(pending_record.get("queue_digest"))
        pending_push_at, pending_age, pending_future = public_timestamp_and_age(pending_record.get("push_completed_at_utc"), now)
        if not isinstance(pending_generation, int) or isinstance(pending_generation, bool) or pending_generation < 1 or pending_commit is None or pending_digest is None or pending_push_at is None:
            problems.append("publication_state_integrity_failure")
        else:
            empty["unresolved_verification_generation"] = pending_generation
            empty["unresolved_verification_commit_sha"] = pending_commit
            empty["unresolved_verification_age_seconds"] = pending_age
        if pending_future:
            problems.append("publication_future_clock_skew")
        if (
            checkpoint_outcome != "pushed"
            or checkpoint_generation != pending_generation
            or checkpoint_commit != pending_commit
            or checkpoint_digest != pending_digest
            or checkpoint_push_at != pending_push_at
        ):
            problems.append("publication_proof_gap")

    receipt_generation = None
    receipt_commit = None
    receipt_push_at: str | None = None
    receipt_verified_at: str | None = None
    receipt_digest = None
    finalization_verified_age: int | None = None
    if receipt is not None:
        receipt_generation = receipt.get("generation")
        receipt_commit = public_commit_sha(receipt.get("commit_sha"))
        receipt_digest = queue_digest(receipt.get("queue_digest"))
        block = receipt.get("expected_block_number")
        receipt_push_at, _receipt_push_age, receipt_push_future = public_timestamp_and_age(
            receipt.get("push_completed_at_utc"), now
        )
        receipt_verified_at, verified_age, receipt_future = public_timestamp_and_age(receipt.get("pages_verified_at_utc"), now)
        if not isinstance(receipt_generation, int) or isinstance(receipt_generation, bool) or receipt_generation < 1 or receipt_commit is None or receipt_digest is None or not isinstance(block, int) or isinstance(block, bool) or block < 1 or receipt_push_at is None or receipt_verified_at is None:
            problems.append("publication_state_integrity_failure")
        else:
            # Controller ruling: only this durable receipt proves a static block
            # reached Pages; never infer it from a queued observation or pending.
            empty["last_direct_data_compatible_static_block"] = block
        if receipt_future or receipt_push_future:
            problems.append("publication_future_clock_skew")
        if parse_utc(receipt_verified_at) is not None and parse_utc(receipt_push_at) is not None and parse_utc(receipt_verified_at) < parse_utc(receipt_push_at):
            problems.append("publication_timestamp_reversal")
        if pending_record is not None and (
            receipt_generation == pending_generation and receipt_commit == pending_commit
            and receipt_digest == pending_digest and receipt_push_at == pending_push_at
        ):
            empty["pages_verification_state"] = "pages_verified_finalization_pending"
            finalization_verified_age = verified_age
        elif pending_record is not None:
            empty["pages_verification_state"] = "pending"
        else:
            empty["pages_verification_state"] = "verified"
    elif pending_record is not None:
        empty["pages_verification_state"] = "pending"
        empty["pages_verification_state"] = "pending"
    else:
        empty["pages_verification_state"] = "none"

    # Every outstanding pending must age out even when an older/nonmatching
    # receipt exists. A matching receipt is finalization rather than verifier work.
    receipt_matches_pending = bool(
        pending_record is not None and receipt is not None
        and receipt_generation == pending_generation and receipt_commit == pending_commit
        and receipt_digest == pending_digest and receipt_push_at == pending_push_at
    )
    unresolved_age = empty["unresolved_verification_age_seconds"]
    if pending_record is not None and not receipt_matches_pending and isinstance(unresolved_age, int) and unresolved_age > 900:
        problems.append("pages_verification_unresolved")

    # A durable pushed checkpoint cannot lose both the current pending proof and
    # its exact receipt.  A later no_diff checkpoint over an older receipt is
    # intentionally allowed because it did not replace deployed static data.
    checkpoint_matches_pending = bool(
        checkpoint_outcome == "pushed" and checkpoint_generation == pending_generation
        and checkpoint_commit == pending_commit and checkpoint_digest == pending_digest
        and checkpoint_push_at == pending_push_at
    )
    checkpoint_matches_receipt = bool(
        checkpoint_outcome == "pushed" and checkpoint_generation == receipt_generation
        and checkpoint_commit == receipt_commit and checkpoint_digest == receipt_digest
        and checkpoint_push_at == receipt_push_at
    )
    if checkpoint_outcome == "pushed" and not checkpoint_matches_pending and not checkpoint_matches_receipt:
        problems.append("publication_proof_gap")

    journal_at: str | None = None
    journal_generation: int | None = None
    journal_matches_latest = False
    if journal is not None:
        journal_at, _journal_age, journal_future = public_timestamp_and_age(journal.get("created_at_utc"), now)
        journal_generation = journal.get("publication_generation")
        journal_matches_latest = bool(
            latest_record is not None and journal_generation == latest_generation
            and latest_digest is not None and queue_digest(journal.get("queue_digest")) == latest_digest
        )
        if journal_at is None or not isinstance(journal_generation, int) or isinstance(journal_generation, bool) or journal_generation < 1:
            problems.append("publication_state_integrity_failure")
        if journal_future:
            problems.append("publication_future_clock_skew")

    # Applicable causal chain is observation -> journal -> push -> verification.
    journal_causal_with_latest = bool(journal_matches_latest and journal_at is not None)
    if latest_generation is not None and latest_created_at is not None and journal_causal_with_latest:
        if parse_utc(journal_at) < parse_utc(latest_created_at):
            problems.append("publication_timestamp_reversal")
            journal_causal_with_latest = False
    for generation, push_at in ((pending_generation, pending_push_at), (checkpoint_generation, checkpoint_push_at), (receipt_generation, receipt_push_at)):
        if generation is not None and push_at is not None and latest_generation == generation and latest_created_at is not None:
            if parse_utc(push_at) < parse_utc(latest_created_at):
                problems.append("publication_timestamp_reversal")
        if generation is not None and push_at is not None and journal_generation == generation and journal_at is not None:
            if parse_utc(push_at) < parse_utc(journal_at):
                problems.append("publication_timestamp_reversal")

    active_publication = bool(publisher_lock_active and journal_causal_with_latest)
    if isinstance(finalization_verified_age, int) and finalization_verified_age > 180 and not active_publication:
        problems.append("pages_verified_finalization_stale")
    queue_age = empty["queue_age_seconds"]
    if empty["queue_lag"] > 0 and isinstance(queue_age, int):
        queue_limit = 300 if active_publication else 180
        if queue_age > queue_limit:
            problems.append("publication_queue_stale")

    empty["problems"] = sorted(set(problems))
    return empty


def public_health_report(
    *,
    now: datetime,
    problems: list[str],
    warnings: list[str],
    checks: dict[str, Any],
) -> dict[str, Any]:
    """Emit only public derived enums/counts/booleans/times/integers/SHAs."""
    watcher = checks.get("watcher") if isinstance(checks.get("watcher"), dict) else {}
    local_status = checks.get("local_status") if isinstance(checks.get("local_status"), dict) else {}
    terminal = checks.get("terminal_publication") if isinstance(checks.get("terminal_publication"), dict) else {}
    publication = checks.get("publication") if isinstance(checks.get("publication"), dict) else publication_health_summary(
        {"last_generation": 0, "latest": None, "pending": None, "checkpoint": None, "pages_verified": None, "journal": None},
        now=now,
        publisher_lock_active=False,
    )
    timers = checks.get("timers") if isinstance(checks.get("timers"), dict) else {}
    workers = checks.get("workers") if isinstance(checks.get("workers"), dict) else {}
    timer_healthy = all(
        isinstance(row, dict)
        and not row.get("error")
        and row.get("LoadState") == "loaded"
        and row.get("ActiveState") == "active"
        and row.get("UnitFileState") in {"enabled", "enabled-runtime"}
        for row in timers.values()
    )
    workers_healthy = all(
        isinstance(row, dict)
        and not row.get("error")
        and row.get("ActiveState") != "failed"
        and row.get("Result") in {"", "success"}
        for row in workers.values()
    )
    terminal_result = str(terminal.get("result") or "")
    if terminal_result not in PUBLISHED_RESULTS | {"failed", "unavailable"}:
        terminal_result = "unavailable"
    completed, _, _ = public_timestamp_and_age(terminal.get("completed_at_utc"), now)
    local_block = local_status.get("latest_generated_block")
    if not isinstance(local_block, int) or isinstance(local_block, bool) or local_block < 0:
        local_block = None
    remote = checks.get("remote") if isinstance(checks.get("remote"), dict) else {}
    git = checks.get("git") if isinstance(checks.get("git"), dict) else {}
    filesystems = checks.get("filesystems") if isinstance(checks.get("filesystems"), list) else []
    return {
        "kind": "degen_dogs_wsl_runner_health",
        "checked_at_utc": utc_text(now),
        "status": "healthy" if not problems else "unhealthy",
        "problem_count": len(problems),
        "warning_count": len(warnings),
        "checks": {
            "timers_healthy": timer_healthy,
            "workers_healthy": workers_healthy,
            "refresh_lock_active": bool(checks.get("refresh_lock_active")),
            "watcher_age_seconds": watcher.get("age_seconds") if isinstance(watcher.get("age_seconds"), int) else None,
            "watcher_pending_refresh": bool(watcher.get("pending_refresh")),
            "watcher_consecutive_rpc_failures": max(0, int(watcher.get("consecutive_rpc_failures") or 0)),
            "watcher_consecutive_refresh_failures": max(0, int(watcher.get("consecutive_refresh_failures") or 0)),
            "local_status_age_seconds": local_status.get("age_seconds") if isinstance(local_status.get("age_seconds"), int) else None,
            "local_latest_generated_block": local_block,
            "terminal_publication_result": terminal_result,
            "terminal_publication_completed_at_utc": completed,
            "remote_healthy": not bool(remote.get("incident")),
            "git_clean": git.get("tracked_dirty") is False,
            "filesystems_healthy": all(isinstance(row, dict) and row.get("error") is None for row in filesystems),
            "publication": publication,
        },
    }


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
    publication_mode = os.environ.get("MISSION3_PUBLICATION_MODE", "inline").strip()
    if publication_mode not in {"inline", "queue"}:
        publication_mode = "inline"
        # Do not probe/create queue state for an unrecognized legacy config.

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

    publication_snapshot: dict[str, Any] | None = None
    if publication_mode == "queue":
        try:
            publication_snapshot = runner_publication_state.read_publication_health_snapshot(lock_dir)
        except Exception as exc:  # noqa: BLE001 - detailed state failures remain private
            problems.append(f"publication queue state is unreadable ({type(exc).__name__})")
            publication = publication_health_summary(
                {"last_generation": "invalid"},
                now=now,
                publisher_lock_active=lock_active,
                configured_queue_mode=True,
            )
            checks["publication"] = publication
            problems.extend(f"publication health failure: {code}" for code in publication["problems"])

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
        elif watcher_stale_requires_failure(
            watcher_age,
            watcher_stale_seconds,
            publisher_lock_active=lock_active,
            publication_mode=publication_mode,
        ):
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

    try:
        latest_terminal = refresh_telemetry.latest_refresh_row(dict(os.environ), root=ROOT)
        terminal_problem = terminal_publication_problem(latest_terminal)
        checks["terminal_publication"] = {
            "completed_at_utc": latest_terminal.get("completed_at_utc"),
            "result": latest_terminal.get("result"),
            "problem": terminal_problem,
        }
        if terminal_problem:
            problems.append(terminal_problem)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"terminal publication telemetry is unreadable ({type(exc).__name__})")

    if publication_snapshot is not None:
        try:
            telemetry_summary = refresh_telemetry.metrics_summary(dict(os.environ), root=ROOT)
            provider_failure_count = int(telemetry_summary.get("provider_failure_count_24h") or 0)
        except Exception:
            provider_failure_count = 0
            problems.append("publication provider telemetry is unreadable")
        publication = publication_health_summary(
            publication_snapshot,
            now=now,
            publisher_lock_active=lock_active,
            provider_failure_count=max(0, provider_failure_count),
            configured_queue_mode=True,
        )
        checks["publication"] = publication
        problems.extend(f"publication health failure: {code}" for code in publication["problems"])

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
    monitored_logs = [log_dir / "refresh.log", log_dir / "watch-onchain.log", ROOT / ".local" / "watcher_checks.jsonl"]
    if publication_mode == "queue":
        monitored_logs.append(log_dir / "pages-verifier.jsonl")
    for path in monitored_logs:
        try:
            if path.stat().st_size > 32 * 1024 * 1024:
                warnings.append(f"managed log exceeds 32 MiB: {path.name}")
        except FileNotFoundError:
            continue

    report = public_health_report(now=now, problems=problems, warnings=warnings, checks=checks)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
