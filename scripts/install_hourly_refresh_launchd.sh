#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Install the hourly launchd job for the Degen Dogs Mission 3 private refresh runner.

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
LABEL="${DEGEN_DOGS_LAUNCHD_LABEL:-com.ael.degendogs.mission3.refresh}"
INTERVAL_SECONDS="${DEGEN_DOGS_REFRESH_INTERVAL_SECONDS:-3600}"
PLIST_DIR="${USER_HOME}/Library/LaunchAgents"
LOG_DIR="$(degen_dogs_resolve_runner_path "$REPO_DIR" "${DEGEN_DOGS_LOG_DIR:-${USER_HOME}/Library/Logs/degen-dogs-mission3}")"
LOCK_DIR="$(degen_dogs_resolve_runner_path "$REPO_DIR" "${DEGEN_DOGS_LOCK_DIR:-${USER_HOME}/Library/Caches/degen-dogs-mission3}")"
REFRESH_LOCK_PATH="$(degen_dogs_resolve_runner_path "$REPO_DIR" "${DEGEN_DOGS_REFRESH_LOCK_PATH:-${MISSION3_REFRESH_LOCK_PATH:-${LOCK_DIR}/refresh.lock}}")"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
SCRIPT_PATH="${REPO_DIR}/scripts/refresh_and_publish.sh"
REFRESH_TELEMETRY_PATH="$(degen_dogs_resolve_runner_path "$REPO_DIR" "${DEGEN_DOGS_REFRESH_TELEMETRY_PATH:-.local/refresh_runs.jsonl}")"
REFRESH_METRICS_PATH="$(degen_dogs_resolve_runner_path "$REPO_DIR" "${DEGEN_DOGS_REFRESH_METRICS_PATH:-logs/refresh-metrics.jsonl}")"
FULL_REFRESH="${DEGEN_DOGS_FULL_REFRESH:-0}"
RUN_MISSION3_ARCHIVE="${DEGEN_DOGS_RUN_MISSION3_ARCHIVE:-1}"
LIVE_VERIFY_AFTER_PUSH="${DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH:-1}"
GIT_RETRY_ATTEMPTS="${DEGEN_DOGS_GIT_RETRY_ATTEMPTS:-4}"
GIT_RETRY_BASE_SECONDS="${DEGEN_DOGS_GIT_RETRY_BASE_SECONDS:-2}"
GIT_RETRY_MAX_SECONDS="${DEGEN_DOGS_GIT_RETRY_MAX_SECONDS:-30}"
GIT_RETRY_JITTER_SECONDS="${DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS:-3}"
LIVE_VERIFY_TIMEOUT_SECONDS="${DEGEN_DOGS_LIVE_VERIFY_TIMEOUT_SECONDS:-300}"
LIVE_VERIFY_INTERVAL_SECONDS="${DEGEN_DOGS_LIVE_VERIFY_INTERVAL_SECONDS:-10}"
ALLOW_RUNNING_RESTART="${DEGEN_DOGS_INSTALL_ALLOW_RUNNING_RESTART:-0}"

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

[[ -z "$RUNNER_VENV_ERROR" ]] || fail "$RUNNER_VENV_ERROR"
[[ "$LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "invalid launchd label: ${LABEL}"
[[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || fail "interval must be an integer number of seconds"
if (( INTERVAL_SECONDS < 300 )); then
  fail "interval too small; refusing to schedule under 300 seconds"
fi
for value in "$GIT_RETRY_ATTEMPTS" "$GIT_RETRY_BASE_SECONDS" "$GIT_RETRY_MAX_SECONDS" "$GIT_RETRY_JITTER_SECONDS" "$LIVE_VERIFY_TIMEOUT_SECONDS" "$LIVE_VERIFY_INTERVAL_SECONDS"; do
  [[ "$value" =~ ^[0-9]+$ ]] || fail "retry and live-verification settings must be non-negative integers"
done
if (( GIT_RETRY_ATTEMPTS < 1 || GIT_RETRY_ATTEMPTS > 10 )); then
  fail "DEGEN_DOGS_GIT_RETRY_ATTEMPTS must be between 1 and 10"
fi
if (( GIT_RETRY_MAX_SECONDS < GIT_RETRY_BASE_SECONDS )); then
  fail "DEGEN_DOGS_GIT_RETRY_MAX_SECONDS must be >= DEGEN_DOGS_GIT_RETRY_BASE_SECONDS"
fi
if (( LIVE_VERIFY_TIMEOUT_SECONDS < 1 || LIVE_VERIFY_INTERVAL_SECONDS < 1 )); then
  fail "live-verification timeout and interval must be at least one second"
fi
[[ "$FULL_REFRESH" == "0" || "$FULL_REFRESH" == "1" ]] || fail "DEGEN_DOGS_FULL_REFRESH must be 0 or 1"
[[ "$RUN_MISSION3_ARCHIVE" == "0" || "$RUN_MISSION3_ARCHIVE" == "1" ]] || fail "DEGEN_DOGS_RUN_MISSION3_ARCHIVE must be 0 or 1"
[[ "$LIVE_VERIFY_AFTER_PUSH" == "0" || "$LIVE_VERIFY_AFTER_PUSH" == "1" ]] || fail "DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH must be 0 or 1"
[[ "$ALLOW_RUNNING_RESTART" == "0" || "$ALLOW_RUNNING_RESTART" == "1" ]] || fail "DEGEN_DOGS_INSTALL_ALLOW_RUNNING_RESTART must be 0 or 1"
[[ "$REPO_DIR" = /* && "$REPO_DIR" != *:* && "$REPO_DIR" != *$'\n'* ]] || fail "repo dir must be absolute without a PATH separator or newline: ${REPO_DIR}"
[[ -f "$SCRIPT_PATH" ]] || fail "refresh script missing: ${SCRIPT_PATH}"
if [[ "$RUN_MISSION3_ARCHIVE" == "1" ]]; then
  python3 - <<'PY' || fail "Mission 3 archive requires the hash-locked Python runtime; create .venv and install requirements.txt"
from Crypto.Hash import keccak

digest = keccak.new(digest_bits=256, data=b"").hexdigest()
if digest != "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470":
    raise SystemExit("unexpected Ethereum Keccak-256 implementation")
PY
fi

# Serialize installer state checks and launchd replacement with every publisher.
# The helper securely re-execs this script while retaining the protected flock.
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
degen_dogs_private_dir "$(dirname "$REFRESH_TELEMETRY_PATH")"
degen_dogs_private_dir "$(dirname "$REFRESH_METRICS_PATH")"
degen_dogs_private_dir "$LOG_DIR"
degen_dogs_private_dir "$LOCK_DIR"
degen_dogs_private_dir "${REPO_DIR}/.local"
degen_dogs_private_dir "${REPO_DIR}/logs"
degen_dogs_private_file "${LOG_DIR}/refresh.log"
degen_dogs_private_file "${LOG_DIR}/launchd.out.log"
degen_dogs_private_file "${LOG_DIR}/launchd.err.log"
degen_dogs_private_file "$REFRESH_LOCK_PATH"
degen_dogs_private_file "$REFRESH_TELEMETRY_PATH"
degen_dogs_private_file "$REFRESH_METRICS_PATH"
degen_dogs_private_file "$PLIST_PATH" 0

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
FULL_REFRESH="$FULL_REFRESH" \
RUN_MISSION3_ARCHIVE="$RUN_MISSION3_ARCHIVE" \
LIVE_VERIFY_AFTER_PUSH="$LIVE_VERIFY_AFTER_PUSH" \
GIT_RETRY_ATTEMPTS="$GIT_RETRY_ATTEMPTS" \
GIT_RETRY_BASE_SECONDS="$GIT_RETRY_BASE_SECONDS" \
GIT_RETRY_MAX_SECONDS="$GIT_RETRY_MAX_SECONDS" \
GIT_RETRY_JITTER_SECONDS="$GIT_RETRY_JITTER_SECONDS" \
LIVE_VERIFY_TIMEOUT_SECONDS="$LIVE_VERIFY_TIMEOUT_SECONDS" \
LIVE_VERIFY_INTERVAL_SECONDS="$LIVE_VERIFY_INTERVAL_SECONDS" \
python3 - <<'PY'
from __future__ import annotations

import os
import plistlib
import stat
from pathlib import Path

plist = {
    "Label": os.environ["LABEL"],
    "ProgramArguments": [os.environ["SCRIPT_PATH"]],
    "WorkingDirectory": os.environ["REPO_DIR"],
    "StartInterval": int(os.environ["INTERVAL_SECONDS"]),
    "RunAtLoad": True,
    "ProcessType": "Background",
    "ThrottleInterval": 10,
    "Umask": 0o077,
    "StandardOutPath": f"{os.environ['LOG_DIR']}/launchd.out.log",
    "StandardErrorPath": f"{os.environ['LOG_DIR']}/launchd.err.log",
    "EnvironmentVariables": {
        "HOME": os.environ["HOME"],
        "PATH": os.environ["RUNNER_PATH"],
        "GIT_TERMINAL_PROMPT": "0",
        "DEGEN_DOGS_REPO_DIR": os.environ["REPO_DIR"],
        "DEGEN_DOGS_LOG_DIR": os.environ["LOG_DIR"],
        "DEGEN_DOGS_LOCK_DIR": os.environ["LOCK_DIR"],
        "DEGEN_DOGS_REFRESH_LOCK_PATH": os.environ["REFRESH_LOCK_PATH"],
        "DEGEN_DOGS_FULL_REFRESH": os.environ["FULL_REFRESH"],
        "DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH": os.environ["LIVE_VERIFY_AFTER_PUSH"],
        "DEGEN_DOGS_GIT_RETRY_ATTEMPTS": os.environ["GIT_RETRY_ATTEMPTS"],
        "DEGEN_DOGS_GIT_RETRY_BASE_SECONDS": os.environ["GIT_RETRY_BASE_SECONDS"],
        "DEGEN_DOGS_GIT_RETRY_MAX_SECONDS": os.environ["GIT_RETRY_MAX_SECONDS"],
        "DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS": os.environ["GIT_RETRY_JITTER_SECONDS"],
        "DEGEN_DOGS_LIVE_VERIFY_TIMEOUT_SECONDS": os.environ["LIVE_VERIFY_TIMEOUT_SECONDS"],
        "DEGEN_DOGS_LIVE_VERIFY_INTERVAL_SECONDS": os.environ["LIVE_VERIFY_INTERVAL_SECONDS"],
    },
}
allowlist = os.environ["DEGEN_DOGS_RUNNER_COMMON_ENV_ALLOWLIST"] + os.environ["DEGEN_DOGS_RUNNER_ARCHIVE_ENV_ALLOWLIST"]
for key in allowlist.split():
    value = os.environ.get(key)
    if value:
        plist["EnvironmentVariables"][key] = value
# Fixed worker policy is applied after the configurable allowlist so an env file
# cannot accidentally turn the hourly job back into an unconditional full build
# or disable the archive-maintenance path entirely.
plist["EnvironmentVariables"]["DEGEN_DOGS_FULL_REFRESH"] = os.environ["FULL_REFRESH"]
plist["EnvironmentVariables"]["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"] = os.environ["RUN_MISSION3_ARCHIVE"]
path = Path(os.environ["PLIST_CANDIDATE_PATH"])
payload = plistlib.dumps(plist, sort_keys=False)
flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(path, flags)
try:
    details = os.fstat(fd)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        raise SystemExit("unsafe hourly launchd candidate")
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
echo "full_refresh: ${FULL_REFRESH}"
echo "run_mission3_archive: ${RUN_MISSION3_ARCHIVE}"
echo "live_verify_after_push: ${LIVE_VERIFY_AFTER_PUSH}"
echo "logs: ${LOG_DIR}/refresh.log"
echo "lock: ${REFRESH_LOCK_PATH}"
