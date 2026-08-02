#!/usr/bin/env bash
set -Eeuo pipefail

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
REPO_DIR="${DEGEN_DOGS_REPO_DIR:-$SCRIPT_REPO_DIR}"
LABEL="${DEGEN_DOGS_LAUNCHD_LABEL:-com.ael.degendogs.mission3.refresh}"
INTERVAL_SECONDS="${DEGEN_DOGS_REFRESH_INTERVAL_SECONDS:-3600}"
PLIST_DIR="${USER_HOME}/Library/LaunchAgents"
LOG_DIR="${DEGEN_DOGS_LOG_DIR:-${USER_HOME}/Library/Logs/degen-dogs-mission3}"
LOCK_DIR="${DEGEN_DOGS_LOCK_DIR:-${USER_HOME}/Library/Caches/degen-dogs-mission3}"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
SCRIPT_PATH="${REPO_DIR}/scripts/refresh_and_publish.sh"
FULL_REFRESH="${DEGEN_DOGS_FULL_REFRESH:-1}"
LIVE_VERIFY_AFTER_PUSH="${DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH:-1}"
GIT_RETRY_ATTEMPTS="${DEGEN_DOGS_GIT_RETRY_ATTEMPTS:-4}"
GIT_RETRY_BASE_SECONDS="${DEGEN_DOGS_GIT_RETRY_BASE_SECONDS:-2}"
GIT_RETRY_MAX_SECONDS="${DEGEN_DOGS_GIT_RETRY_MAX_SECONDS:-30}"
GIT_RETRY_JITTER_SECONDS="${DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS:-3}"
LIVE_VERIFY_TIMEOUT_SECONDS="${DEGEN_DOGS_LIVE_VERIFY_TIMEOUT_SECONDS:-300}"
LIVE_VERIFY_INTERVAL_SECONDS="${DEGEN_DOGS_LIVE_VERIFY_INTERVAL_SECONDS:-10}"

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

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
[[ "$LIVE_VERIFY_AFTER_PUSH" == "0" || "$LIVE_VERIFY_AFTER_PUSH" == "1" ]] || fail "DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH must be 0 or 1"
[[ "$REPO_DIR" = /* ]] || fail "repo dir must be absolute: ${REPO_DIR}"
[[ -f "$SCRIPT_PATH" ]] || fail "refresh script missing: ${SCRIPT_PATH}"

mkdir -p "$PLIST_DIR" "$LOG_DIR" "$LOCK_DIR"
chmod 700 "$LOCK_DIR" || true

if [[ ! -x "$SCRIPT_PATH" ]]; then
  chmod +x "$SCRIPT_PATH"
fi

PLIST_PATH="$PLIST_PATH" \
LABEL="$LABEL" \
SCRIPT_PATH="$SCRIPT_PATH" \
REPO_DIR="$REPO_DIR" \
LOG_DIR="$LOG_DIR" \
LOCK_DIR="$LOCK_DIR" \
INTERVAL_SECONDS="$INTERVAL_SECONDS" \
FULL_REFRESH="$FULL_REFRESH" \
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
import tempfile
from pathlib import Path

plist = {
    "Label": os.environ["LABEL"],
    "ProgramArguments": [os.environ["SCRIPT_PATH"]],
    "WorkingDirectory": os.environ["REPO_DIR"],
    "StartInterval": int(os.environ["INTERVAL_SECONDS"]),
    "RunAtLoad": True,
    "ProcessType": "Background",
    "ThrottleInterval": 10,
    "StandardOutPath": f"{os.environ['LOG_DIR']}/launchd.out.log",
    "StandardErrorPath": f"{os.environ['LOG_DIR']}/launchd.err.log",
    "EnvironmentVariables": {
        "HOME": os.environ["HOME"],
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "GIT_TERMINAL_PROMPT": "0",
        "DEGEN_DOGS_REPO_DIR": os.environ["REPO_DIR"],
        "DEGEN_DOGS_LOG_DIR": os.environ["LOG_DIR"],
        "DEGEN_DOGS_LOCK_DIR": os.environ["LOCK_DIR"],
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
path = Path(os.environ["PLIST_PATH"])
payload = plistlib.dumps(plist, sort_keys=False)
fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY

plutil -lint "$PLIST_PATH"

uid="$(id -u)"
launchctl bootout "gui/${uid}" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/${uid}" "$PLIST_PATH"
launchctl enable "gui/${uid}/${LABEL}"

if [[ "${DEGEN_DOGS_KICKSTART:-0}" == "1" ]]; then
  launchctl kickstart -k "gui/${uid}/${LABEL}"
fi

launchctl print "gui/${uid}/${LABEL}" >/dev/null

echo "installed ${LABEL}"
echo "plist: ${PLIST_PATH}"
echo "interval_seconds: ${INTERVAL_SECONDS}"
echo "full_refresh: ${FULL_REFRESH}"
echo "live_verify_after_push: ${LIVE_VERIFY_AFTER_PUSH}"
echo "logs: ${LOG_DIR}/refresh.log"
echo "lock: ${LOCK_DIR}/refresh.lock"
