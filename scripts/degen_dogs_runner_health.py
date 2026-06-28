#!/usr/bin/env python3
"""Silent watchdog for the Degen Dogs Mission 3 private Mac mini runner.

Runs from Hermes cron. Emits stdout only when it repairs drift or finds an issue
that needs attention. Healthy/no-op runs stay silent.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Iterable

HOME = Path(os.environ.get("DEGEN_DOGS_HEALTH_HOME", str(Path.home())))
REPO_DIR = Path(os.environ.get("DEGEN_DOGS_REPO_DIR", "/Users/marko/projects/Degen-Dogs-Mission-3"))
LABEL = "com.ael.degendogs.mission3.refresh"
PLIST_PATH = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = HOME / "Library" / "Logs" / "degen-dogs-mission3"
CACHE_DIR = HOME / "Library" / "Caches" / "degen-dogs-mission3"
REFRESH_LOG = LOG_DIR / "refresh.log"
REFRESH_SCRIPT = REPO_DIR / "scripts" / "refresh_and_publish.sh"
INSTALL_SCRIPT = REPO_DIR / "scripts" / "install_hourly_refresh_launchd.sh"
LIVE_URL = "https://ael-dev3.github.io/Degen-Dogs-Mission-3/"
GITHUB_REPO = os.environ.get("DEGEN_DOGS_HEALTH_GITHUB_REPO", "ael-dev3/Degen-Dogs-Mission-3")
DISCORD_MENTION = os.environ.get("DEGEN_DOGS_HEALTH_DISCORD_MENTION", "@Ael")
ALERT_STATE_PATH = Path(
    os.environ.get("DEGEN_DOGS_HEALTH_ALERT_STATE_PATH", str(CACHE_DIR / "critical-alert-state.json"))
).expanduser()
EXPECTED_INTERVAL_SECONDS = 3600
STALE_SUCCESS_SECONDS = 4 * 3600
CRITICAL_STALE_SECONDS = int(os.environ.get("DEGEN_DOGS_HEALTH_CRITICAL_STALE_SECONDS", str(2 * 3600)))
REPEAT_ALERT_SECONDS = int(os.environ.get("DEGEN_DOGS_HEALTH_REPEAT_ALERT_SECONDS", str(6 * 3600)))
PATH_VALUE = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DRY_RUN = os.environ.get("DEGEN_DOGS_HEALTH_DRY_RUN") == "1"
ALERT_DRY_RUN = DRY_RUN or os.environ.get("DEGEN_DOGS_HEALTH_ALERT_DRY_RUN") == "1"
GITHUB_ALERTS_ENABLED = os.environ.get("DEGEN_DOGS_HEALTH_GITHUB_ALERTS", "1") != "0"

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


def env() -> dict[str, str]:
    data = os.environ.copy()
    data.update({
        "HOME": str(HOME),
        "PATH": PATH_VALUE,
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


def launch_target() -> str:
    return f"{launch_domain()}/{LABEL}"


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


def live_site_ok() -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            LIVE_URL + f"?runner_health={int(time.time())}",
            headers={"User-Agent": "Hermes-DegenDogs-runner-health/1.0"},
        )
        with urllib.request.urlopen(req, timeout=25) as response:
            status = getattr(response, "status", 0)
            body = response.read(250_000).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - watchdog should report compactly, not crash.
        return False, f"live HTTP check failed: {type(exc).__name__}: {exc}"
    if status != 200:
        return False, f"live HTTP status {status}"
    if "auction_feed" not in body or LIVE_URL.rstrip("/") not in body:
        return False, "live HTML missing expected auction_feed/site_url markers"
    return True, "live site ok"


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
    if any("launchd" in item.lower() for item in issues) or "could not inspect disabled launchd" in combined:
        causes.append("launchd_agent_unhealthy_or_drifted")
    if not live_ok:
        causes.append("live_site_marker_or_http_failure")
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
        data = json.loads(ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_alert_state(state: dict[str, Any]) -> None:
    if ALERT_DRY_RUN:
        return
    ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALERT_STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


def find_open_runner_issue() -> tuple[int | None, str | None]:
    result = run_gh(
        [
            "issue",
            "list",
            "--repo",
            GITHUB_REPO,
            "--state",
            "open",
            "--search",
            "Local runner critical health alert in:title",
            "--json",
            "number,url,title",
            "--limit",
            "20",
        ],
        timeout=30,
    )
    if result.code != 0:
        return None, None
    try:
        issues = json.loads(result.out or "[]")
    except json.JSONDecodeError:
        return None, None
    for issue in issues:
        title = str(issue.get("title") or "")
        if "Local runner critical health alert" in title:
            number = issue.get("number")
            if isinstance(number, int):
                return number, str(issue.get("url") or f"https://github.com/{GITHUB_REPO}/issues/{number}")
    return None, None


def update_github_issue(snapshot: dict[str, Any], body: str, state: dict[str, Any]) -> tuple[str, int | None, str | None]:
    if not GITHUB_ALERTS_ENABLED:
        return "GitHub alert update skipped: disabled", None, None
    if ALERT_DRY_RUN:
        number = state.get("issue_number") if isinstance(state.get("issue_number"), int) else None
        url = state.get("issue_url") if isinstance(state.get("issue_url"), str) else None
        return "DRY-RUN would create/update GitHub issue", number, url
    auth = run_gh(["auth", "status"], timeout=20)
    if auth.code != 0:
        return f"GitHub alert update failed: gh auth status failed: {sanitize(auth.out or auth.err)}", None, None

    number = state.get("issue_number") if isinstance(state.get("issue_number"), int) else None
    url = state.get("issue_url") if isinstance(state.get("issue_url"), str) else None
    if not number:
        number, url = find_open_runner_issue()
    if number:
        comment = run_gh(["issue", "comment", str(number), "--repo", GITHUB_REPO], body=body, timeout=45)
        if comment.code == 0:
            url = url or f"https://github.com/{GITHUB_REPO}/issues/{number}"
            return f"GitHub issue updated: {url}", number, url
        return f"GitHub alert update failed: {sanitize(comment.out or comment.err)}", number, url

    title = "Local runner critical health alert"
    create = run_gh(["issue", "create", "--repo", GITHUB_REPO, "--title", title], body=body, timeout=45)
    if create.code != 0:
        return f"GitHub alert update failed: {sanitize(create.out or create.err)}", None, None
    created_url = create.out.strip().splitlines()[-1] if create.out.strip() else ""
    match = re.search(r"/issues/(\d+)", created_url)
    created_number = int(match.group(1)) if match else None
    return f"GitHub issue created: {created_url or 'unknown-url'}", created_number, created_url or None


def close_github_issue(state: dict[str, Any], body: str) -> str | None:
    number = state.get("issue_number") if isinstance(state.get("issue_number"), int) else None
    if not number or not GITHUB_ALERTS_ENABLED:
        return None
    if ALERT_DRY_RUN:
        return f"DRY-RUN would close GitHub issue #{number}"
    comment = run_gh(["issue", "comment", str(number), "--repo", GITHUB_REPO], body=body, timeout=45)
    close = run_gh(["issue", "close", str(number), "--repo", GITHUB_REPO, "--reason", "completed"], timeout=45)
    if comment.code == 0 and close.code == 0:
        return f"GitHub issue closed: https://github.com/{GITHUB_REPO}/issues/{number}"
    return f"GitHub recovery update failed: {sanitize(comment.out or comment.err or close.out or close.err)}"


def build_incident_body(snapshot: dict[str, Any]) -> str:
    lines = [
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
    if not all(generated_price_change_is_timestamp_only(path) for path in unique_paths):
        return False
    if DRY_RUN:
        append_fix(lines, "DRY-RUN would reset timestamp-only generated price-cache changes")
        return True
    result = run(["git", "checkout", "--", *unique_paths], timeout=30)
    if result.code == 0:
        append_fix(lines, "reset timestamp-only generated price-cache changes blocking launchd refresh")
        return True
    append_issue(lines, f"failed to reset timestamp-only generated price-cache changes: {result.out or result.err}")
    return False


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


def plist_needs_reinstall(issues: list[str]) -> bool:
    if not PLIST_PATH.exists():
        issues.append("launchd plist missing")
        return True
    try:
        data = plistlib.loads(PLIST_PATH.read_bytes())
    except Exception as exc:  # noqa: BLE001
        issues.append(f"launchd plist unreadable: {type(exc).__name__}")
        return True

    expected_program = str(REFRESH_SCRIPT)
    checks = {
        "ProgramArguments[0]": (data.get("ProgramArguments") or [None])[0],
        "WorkingDirectory": data.get("WorkingDirectory"),
        "StartInterval": data.get("StartInterval"),
        "EnvironmentVariables.HOME": (data.get("EnvironmentVariables") or {}).get("HOME"),
        "EnvironmentVariables.DEGEN_DOGS_REPO_DIR": (data.get("EnvironmentVariables") or {}).get("DEGEN_DOGS_REPO_DIR"),
    }
    expected = {
        "ProgramArguments[0]": expected_program,
        "WorkingDirectory": str(REPO_DIR),
        "StartInterval": EXPECTED_INTERVAL_SECONDS,
        "EnvironmentVariables.HOME": str(HOME),
        "EnvironmentVariables.DEGEN_DOGS_REPO_DIR": str(REPO_DIR),
    }
    drift = [name for name, actual in checks.items() if actual != expected[name]]
    if drift:
        issues.append("launchd plist drift: " + ", ".join(drift))
        return True
    return False


def launchctl_print() -> Result:
    return run(["launchctl", "print", launch_target()], cwd=None, timeout=20)


def service_is_running(print_output: str) -> bool:
    return "state = running" in print_output or re.search(r"active count = [1-9]", print_output) is not None


def ensure_launchd(lines: list[str]) -> str:
    reinstall_reasons: list[str] = []
    if plist_needs_reinstall(reinstall_reasons):
        append_issue(lines, "; ".join(reinstall_reasons))
        maybe_run(lines, "reinstall launchd hourly refresh agent", ["npm", "run", "refresh:install"], timeout=120)

    printed = launchctl_print()
    if printed.code != 0:
        append_issue(lines, f"launchctl cannot see {LABEL}: {printed.out or printed.err}")
        maybe_run(lines, "reinstall launchd hourly refresh agent after launchctl miss", ["npm", "run", "refresh:install"], timeout=120)
        printed = launchctl_print()

    # Enabling is idempotent and cheap; do it if print-disabled says the label is disabled.
    disabled = run(["launchctl", "print-disabled", launch_domain()], cwd=None, timeout=20)
    if disabled.code == 0:
        label_quoted = f'"{LABEL}" => true'
        label_plain = f"{LABEL} => true"
        if label_quoted in disabled.out or label_plain in disabled.out:
            maybe_run(lines, "enable launchd hourly refresh agent", ["launchctl", "enable", launch_target()], cwd=None, timeout=20)
    elif disabled.err:
        append_issue(lines, f"could not inspect disabled launchd jobs: {disabled.err}")

    return printed.out + "\n" + printed.err


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
        return 0

    git_tree = run(["git", "rev-parse", "--is-inside-work-tree"], timeout=20)
    if git_tree.code != 0 or git_tree.out.strip() != "true":
        append_issue(lines, f"not a git worktree: {REPO_DIR}")
        emit_startup_failure(lines, ["runner_repo_not_git_worktree"])
        return 0

    for path in (REFRESH_SCRIPT, INSTALL_SCRIPT):
        if not path.exists():
            append_issue(lines, f"required script missing: {path}")
    if not REFRESH_SCRIPT.exists() or not INSTALL_SCRIPT.exists():
        emit_startup_failure(lines, ["required_runner_script_missing"])
        return 0

    if not os.access(REFRESH_SCRIPT, os.X_OK):
        maybe_run(lines, "make refresh script executable", ["chmod", "+x", str(REFRESH_SCRIPT)], cwd=None, timeout=20)
    if not os.access(INSTALL_SCRIPT, os.X_OK):
        maybe_run(lines, "make install script executable", ["chmod", "+x", str(INSTALL_SCRIPT)], cwd=None, timeout=20)

    dirty_blocking = False
    branch = run(["git", "branch", "--show-current"], timeout=20)
    status = run(["git", "status", "--porcelain", "--untracked-files=no"], timeout=30)
    dirty_paths: list[str] = tracked_dirty_paths(status.out) if status.code == 0 else []
    if branch.code == 0 and branch.out.strip() != "main":
        if status.code == 0 and not status.out.strip():
            maybe_run(lines, "switch runner repo back to main", ["git", "switch", "main"], timeout=60)
        else:
            dirty_blocking = True
            append_issue(lines, f"runner repo on {branch.out.strip() or 'unknown'} with tracked changes; not switching")
    if status.code == 0 and status.out.strip():
        if clean_timestamp_only_price_changes(lines, dirty_paths):
            status = run(["git", "status", "--porcelain", "--untracked-files=no"], timeout=30)
            dirty_paths = tracked_dirty_paths(status.out) if status.code == 0 else dirty_paths
            if status.code == 0 and status.out.strip():
                dirty_blocking = True
                append_issue(lines, "tracked worktree changes remain after timestamp-only price cleanup; hourly refresh will refuse to overwrite")
        else:
            dirty_blocking = True
            append_issue(lines, "tracked worktree changes present; hourly refresh will refuse to overwrite")
    elif status.code != 0:
        dirty_blocking = True
        append_issue(lines, f"could not inspect git status: {status.out or status.err}")

    print_output = ensure_launchd(lines)

    log_details = parse_refresh_log_details()
    last_success_ts = log_details.get("last_success_ts")
    last_finished_status = log_details.get("last_finished_status")
    last_error = log_details.get("last_error")
    now = time.time()
    stale = last_success_ts is None or (now - last_success_ts) > STALE_SUCCESS_SECONDS
    critical_stale = last_success_ts is None or (now - last_success_ts) > CRITICAL_STALE_SECONDS
    failed_last = last_finished_status is not None and last_finished_status != 0
    if critical_stale:
        if last_success_ts is None:
            append_issue(lines, "no successful refresh found; critical local-runner alert threshold crossed")
        else:
            append_issue(lines, f"last successful refresh age={int((now - last_success_ts) / 60)}m exceeds critical threshold={int(CRITICAL_STALE_SECONDS / 60)}m")
    if failed_last:
        append_issue(lines, f"latest refresh finished with nonzero status={last_finished_status}")
    if last_error:
        append_issue(lines, f"latest refresh log error: {sanitize(str(last_error), 300)}")

    if stale or failed_last:
        if dirty_blocking:
            append_issue(lines, "refresh appears stale/failed, but tracked worktree changes still block safe kickstart")
        elif service_is_running(print_output):
            append_issue(lines, "refresh appears stale/failed, but launchd job is currently running; left it alone")
        else:
            reason = "no successful refresh found" if last_success_ts is None else f"last successful refresh age={int((now - last_success_ts) / 60)}m"
            if failed_last:
                reason += f", last status={last_finished_status}"
            maybe_run(lines, f"kickstart hourly refresh agent ({reason})", ["launchctl", "kickstart", "-k", launch_target()], cwd=None, timeout=30)

    ok, live_msg = live_site_ok()
    if not ok:
        append_issue(lines, live_msg)
    metrics = load_metrics()
    latest_block = metrics.get("latest_block")
    current_dog = metrics.get("current_auction_token_id")

    issue_lines = [line for line in lines if line.startswith("issue:")]
    causes = derive_causes(
        issues=issue_lines,
        dirty_paths=dirty_paths if dirty_blocking else [],
        log_details=log_details,
        stale=critical_stale,
        failed_last=failed_last,
        live_ok=ok,
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
        "live_ok": ok,
        "latest_block": latest_block,
        "current_dog": current_dog,
    }
    critical = bool(issue_lines) and (
        critical_stale
        or failed_last
        or dirty_blocking
        or not ok
        or any("launchd" in line.lower() for line in issue_lines)
        or any("required script missing" in line.lower() for line in issue_lines)
    )

    if critical:
        alert_message = handle_critical_alert(snapshot)
        if alert_message:
            print(alert_message)
        return 0

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


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired as exc:
        print(f"Degen Dogs local runner health fatal: timeout running {exc.cmd}")
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 - let cron alert on script defects.
        print(f"Degen Dogs local runner health fatal: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
