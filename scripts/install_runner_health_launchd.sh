#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Install the independent launchd watchdog for both Mission 3 refresh services.

PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

USER_HOME="${HOME:-$(python3 - <<'PY'
import os
import pwd
print(pwd.getpwuid(os.getuid()).pw_dir)
PY
)}"
export HOME="$USER_HOME"

SCRIPT_REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=load_runner_env.sh
source "${SCRIPT_REPO_DIR}/scripts/load_runner_env.sh"
degen_dogs_load_runner_env "$SCRIPT_REPO_DIR"
degen_dogs_warn_public_rpc_fallback
degen_dogs_export_runner_env_allowlist
# shellcheck source=runner_permissions.sh
source "${SCRIPT_REPO_DIR}/scripts/runner_permissions.sh"
REPO_DIR="${DEGEN_DOGS_REPO_DIR:-$SCRIPT_REPO_DIR}"
RUNNER_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
RUNNER_VENV_ERROR=""
if [[ -e "${REPO_DIR}/.venv" || -L "${REPO_DIR}/.venv" ]]; then
  if [[ "$REPO_DIR" != *:* && -x "${REPO_DIR}/.venv/bin/python3" ]] && \
    [[ -x "${REPO_DIR}/scripts/runtime-bin/python3" ]] && \
    PYTHONNOUSERSITE=1 "${REPO_DIR}/.venv/bin/python3" -I -c \
      'import Crypto; from Crypto.Hash import keccak; assert Crypto.__version__ == "3.23.0"; assert keccak.new(digest_bits=256, data=b"").hexdigest() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"' \
      >/dev/null 2>&1; then
  RUNNER_PATH="${REPO_DIR}/scripts/runtime-bin:${RUNNER_PATH}"
  else
    RUNNER_VENV_ERROR="repo Python virtualenv is present but failed the pinned Keccak runtime check"
  fi
fi
PATH="$RUNNER_PATH"
export PATH
LABEL="${DEGEN_DOGS_HEALTH_LAUNCHD_LABEL:-com.ael.degendogs.mission3.health}"
INTERVAL_SECONDS="${DEGEN_DOGS_HEALTH_INTERVAL_SECONDS:-300}"
REFRESH_INTERVAL_SECONDS="${DEGEN_DOGS_REFRESH_INTERVAL_SECONDS:-3600}"
WATCHER_INTERVAL_SECONDS="${MISSION3_WATCHER_INTERVAL_SECONDS:-15}"
WATCHER_AUTO_PUSH="${MISSION3_WATCHER_AUTO_PUSH:-0}"
HOURLY_FULL_REFRESH="${DEGEN_DOGS_FULL_REFRESH:-0}"
HOURLY_RUN_MISSION3_ARCHIVE="${DEGEN_DOGS_RUN_MISSION3_ARCHIVE:-1}"
PLIST_DIR="${USER_HOME}/Library/LaunchAgents"
LOG_DIR="$(degen_dogs_resolve_runner_path "$REPO_DIR" "${DEGEN_DOGS_LOG_DIR:-${USER_HOME}/Library/Logs/degen-dogs-mission3}")"
LOCK_DIR="$(degen_dogs_resolve_runner_path "$REPO_DIR" "${DEGEN_DOGS_LOCK_DIR:-${USER_HOME}/Library/Caches/degen-dogs-mission3}")"
REFRESH_LOCK_PATH="$(degen_dogs_resolve_runner_path "$REPO_DIR" "${DEGEN_DOGS_REFRESH_LOCK_PATH:-${MISSION3_REFRESH_LOCK_PATH:-${LOCK_DIR}/refresh.lock}}")"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
SCRIPT_PATH="${REPO_DIR}/scripts/degen_dogs_runner_health.py"
ALERT_STATE_PATH="$(degen_dogs_resolve_runner_path "$REPO_DIR" "${DEGEN_DOGS_HEALTH_ALERT_STATE_PATH:-${LOCK_DIR}/critical-alert-state.json}")"
ALLOW_RUNNING_RESTART="${DEGEN_DOGS_INSTALL_ALLOW_RUNNING_RESTART:-0}"

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

[[ -z "$RUNNER_VENV_ERROR" ]] || fail "$RUNNER_VENV_ERROR"
[[ "$LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "invalid launchd label: ${LABEL}"
for value in "$INTERVAL_SECONDS" "$REFRESH_INTERVAL_SECONDS" "$WATCHER_INTERVAL_SECONDS"; do
  [[ "$value" =~ ^[0-9]+$ ]] || fail "health and runner intervals must be integer seconds"
done
if (( INTERVAL_SECONDS < 60 )); then
  fail "health interval too small; refusing to schedule under 60 seconds"
fi
if (( REFRESH_INTERVAL_SECONDS < 300 )); then
  fail "refresh interval too small; refusing to supervise an interval under 300 seconds"
fi
if (( WATCHER_INTERVAL_SECONDS < 15 )); then
  fail "watcher interval too small; refusing to supervise an interval under 15 seconds"
fi
[[ "$WATCHER_AUTO_PUSH" == "0" || "$WATCHER_AUTO_PUSH" == "1" ]] || fail "MISSION3_WATCHER_AUTO_PUSH must be 0 or 1"
[[ "$HOURLY_FULL_REFRESH" == "0" || "$HOURLY_FULL_REFRESH" == "1" ]] || fail "DEGEN_DOGS_FULL_REFRESH must be 0 or 1"
[[ "$HOURLY_RUN_MISSION3_ARCHIVE" == "0" || "$HOURLY_RUN_MISSION3_ARCHIVE" == "1" ]] || fail "DEGEN_DOGS_RUN_MISSION3_ARCHIVE must be 0 or 1"
[[ "$ALLOW_RUNNING_RESTART" == "0" || "$ALLOW_RUNNING_RESTART" == "1" ]] || fail "DEGEN_DOGS_INSTALL_ALLOW_RUNNING_RESTART must be 0 or 1"
[[ "$REPO_DIR" = /* ]] || fail "repo dir must be absolute: ${REPO_DIR}"
[[ -f "$SCRIPT_PATH" ]] || fail "health script missing: ${SCRIPT_PATH}"

degen_dogs_acquire_installer_lock "$REFRESH_LOCK_PATH" "$0" "$@"

uid="$(id -u)"
target="gui/${uid}/${LABEL}"
existing_job="$(launchctl print "$target" 2>/dev/null || true)"
if [[ "$ALLOW_RUNNING_RESTART" != "1" ]] && {
  [[ "$existing_job" == *"state = running"* ]] ||
    [[ "$existing_job" =~ active[[:space:]]count[[:space:]]=[[:space:]][1-9] ]]
}; then
  fail "refusing to replace running launchd job ${LABEL}; retry when idle or set DEGEN_DOGS_INSTALL_ALLOW_RUNNING_RESTART=1 for an intentional restart"
fi

degen_dogs_private_dir "$PLIST_DIR"
degen_dogs_private_dir "$(dirname "$ALERT_STATE_PATH")"
degen_dogs_private_dir "$LOG_DIR"
degen_dogs_private_dir "$LOCK_DIR"
degen_dogs_private_dir "${REPO_DIR}/.local"
degen_dogs_private_dir "${REPO_DIR}/logs"
for private_log in \
  "${LOG_DIR}/refresh.log" \
  "${LOG_DIR}/launchd.out.log" \
  "${LOG_DIR}/launchd.err.log" \
  "${LOG_DIR}/watch-onchain.log" \
  "${LOG_DIR}/watcher.launchd.out.log" \
  "${LOG_DIR}/watcher.launchd.err.log" \
  "${LOG_DIR}/health.launchd.out.log" \
  "${LOG_DIR}/health.launchd.err.log"; do
  degen_dogs_private_file "$private_log"
done
degen_dogs_private_file "$REFRESH_LOCK_PATH"
degen_dogs_private_file "$ALERT_STATE_PATH" 0
degen_dogs_private_file "$PLIST_PATH" 0
for private_state in \
  "${REPO_DIR}/.local/mission3_onchain_tracker.lock" \
  "${REPO_DIR}/.local/mission3_onchain_tracker_state.json" \
  "${REPO_DIR}/.local/refresh_runs.jsonl" \
  "${REPO_DIR}/.local/watcher_checks.jsonl" \
  "${REPO_DIR}/logs/refresh-metrics.jsonl" \
  "${REPO_DIR}/logs/watch-onchain.log"; do
  degen_dogs_private_file "$private_state" 0
done
if [[ ! -x "$SCRIPT_PATH" ]]; then
  chmod +x "$SCRIPT_PATH"
fi

PLIST_CANDIDATE_PATH="$(degen_dogs_private_temp_file "${PLIST_PATH}.candidate")"
cleanup_plist_candidate() {
  if [[ -n "${PLIST_CANDIDATE_PATH:-}" ]]; then
    degen_dogs_unlink_private_file "$PLIST_CANDIDATE_PATH"
  fi
}
trap cleanup_plist_candidate EXIT

MISSION3_REFRESH_COMMAND="${MISSION3_REFRESH_COMMAND:-}"
if [[ -z "$MISSION3_REFRESH_COMMAND" && "$WATCHER_AUTO_PUSH" == "1" ]]; then
  MISSION3_REFRESH_COMMAND="npm run refresh:publish"
fi
if [[ -z "$MISSION3_REFRESH_COMMAND" ]]; then
  MISSION3_REFRESH_COMMAND="npm run refresh:current"
fi

PLIST_PATH="$PLIST_PATH" \
PLIST_CANDIDATE_PATH="$PLIST_CANDIDATE_PATH" \
LABEL="$LABEL" \
SCRIPT_PATH="$SCRIPT_PATH" \
REPO_DIR="$REPO_DIR" \
RUNNER_PATH="$RUNNER_PATH" \
LOG_DIR="$LOG_DIR" \
LOCK_DIR="$LOCK_DIR" \
REFRESH_LOCK_PATH="$REFRESH_LOCK_PATH" \
INTERVAL_SECONDS="$INTERVAL_SECONDS" \
REFRESH_INTERVAL_SECONDS="$REFRESH_INTERVAL_SECONDS" \
WATCHER_INTERVAL_SECONDS="$WATCHER_INTERVAL_SECONDS" \
WATCHER_AUTO_PUSH="$WATCHER_AUTO_PUSH" \
HOURLY_FULL_REFRESH="$HOURLY_FULL_REFRESH" \
HOURLY_RUN_MISSION3_ARCHIVE="$HOURLY_RUN_MISSION3_ARCHIVE" \
MISSION3_REFRESH_COMMAND="$MISSION3_REFRESH_COMMAND" \
python3 - <<'PY'
from __future__ import annotations

import os
import plistlib
import stat
from pathlib import Path

env = {
    "HOME": os.environ["HOME"],
    "PATH": os.environ["RUNNER_PATH"],
    "GIT_TERMINAL_PROMPT": "0",
    "DEGEN_DOGS_REPO_DIR": os.environ["REPO_DIR"],
    "DEGEN_DOGS_LOG_DIR": os.environ["LOG_DIR"],
    "DEGEN_DOGS_LOCK_DIR": os.environ["LOCK_DIR"],
    "DEGEN_DOGS_REFRESH_LOCK_PATH": os.environ["REFRESH_LOCK_PATH"],
    "DEGEN_DOGS_REFRESH_INTERVAL_SECONDS": os.environ["REFRESH_INTERVAL_SECONDS"],
    "DEGEN_DOGS_FULL_REFRESH": os.environ["HOURLY_FULL_REFRESH"],
    "DEGEN_DOGS_RUN_MISSION3_ARCHIVE": os.environ["HOURLY_RUN_MISSION3_ARCHIVE"],
    "DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH": os.environ.get("DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH", "1"),
    "MISSION3_WATCHER_INTERVAL_SECONDS": os.environ["WATCHER_INTERVAL_SECONDS"],
    "MISSION3_WATCHER_AUTO_PUSH": os.environ["WATCHER_AUTO_PUSH"],
    "MISSION3_REFRESH_COMMAND": os.environ["MISSION3_REFRESH_COMMAND"],
    "MISSION3_REFRESH_LOCK_PATH": os.environ["REFRESH_LOCK_PATH"],
}
# The watchdog must not inherit provider/API credentials. Worker installers
# securely reload the protected data-only env file when a repair is needed.
safe_optional_keys = """
DEGEN_DOGS_ENV_FILE
DEGEN_DOGS_REFRESH_TELEMETRY_PATH
DEGEN_DOGS_REFRESH_METRICS_PATH
MISSION3_WATCHER_TELEMETRY_PATH
MISSION3_WATCHER_STATE_PATH
MISSION3_WATCHER_LOCK_PATH
MISSION3_WATCHER_LOG_PATH
DEGEN_DOGS_HEALTH_GITHUB_REPO
DEGEN_DOGS_HEALTH_GITHUB_ALERTS
DEGEN_DOGS_HEALTH_DISCORD_MENTION
DEGEN_DOGS_HEALTH_ALERT_STATE_PATH
DEGEN_DOGS_HEALTH_CRITICAL_STALE_SECONDS
DEGEN_DOGS_HEALTH_REPEAT_ALERT_SECONDS
DEGEN_DOGS_HEALTH_WATCHER_STALE_SECONDS
DEGEN_DOGS_HEALTH_PENDING_STALE_SECONDS
DEGEN_DOGS_HEALTH_LIVE_STALE_SECONDS
DEGEN_DOGS_HEALTH_LOG_MAX_BYTES
DEGEN_DOGS_HEALTH_LOG_RETAIN_BYTES
DEGEN_DOGS_HEALTH_LOG_EMERGENCY_MAX_BYTES
DEGEN_DOGS_HEALTH_MIN_FREE_BYTES
DEGEN_DOGS_HEALTH_MIN_FREE_PERCENT
""".split()
for key in safe_optional_keys:
    value = os.environ.get(key)
    if value:
        env[key] = value

plist = {
    "Label": os.environ["LABEL"],
    "ProgramArguments": ["/usr/bin/env", "python3", os.environ["SCRIPT_PATH"]],
    "WorkingDirectory": os.environ["REPO_DIR"],
    "StartInterval": int(os.environ["INTERVAL_SECONDS"]),
    "RunAtLoad": True,
    "ProcessType": "Background",
    "ThrottleInterval": 30,
    "Umask": 0o077,
    "StandardOutPath": f"{os.environ['LOG_DIR']}/health.launchd.out.log",
    "StandardErrorPath": f"{os.environ['LOG_DIR']}/health.launchd.err.log",
    "EnvironmentVariables": env,
}
path = Path(os.environ["PLIST_CANDIDATE_PATH"])
payload = plistlib.dumps(plist, sort_keys=False)
flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(path, flags)
try:
    details = os.fstat(fd)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        raise SystemExit("unsafe health launchd candidate")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
except Exception:
    try:
        os.close(fd)
    except OSError:
        pass
    raise
PY

plutil -lint "$PLIST_CANDIDATE_PATH"
degen_dogs_private_file "$PLIST_CANDIDATE_PATH" 0
degen_dogs_install_launchd_transaction "$PLIST_CANDIDATE_PATH" "$PLIST_PATH" "$LABEL" "$uid"
PLIST_CANDIDATE_PATH=""
trap - EXIT

if [[ "${DEGEN_DOGS_KICKSTART:-0}" == "1" ]]; then
  launchctl kickstart -k "gui/${uid}/${LABEL}"
fi

launchctl print "gui/${uid}/${LABEL}" >/dev/null

echo "installed ${LABEL}"
echo "plist: ${PLIST_PATH}"
echo "interval_seconds: ${INTERVAL_SECONDS}"
echo "monitors: com.ael.degendogs.mission3.refresh, com.ael.degendogs.mission3.watch-auction"
echo "logs: ${LOG_DIR}/health.launchd.out.log and ${LOG_DIR}/health.launchd.err.log"
