#!/usr/bin/env bash
set -Eeuo pipefail

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
REPO_DIR="${DEGEN_DOGS_REPO_DIR:-$SCRIPT_REPO_DIR}"
LABEL="${DEGEN_DOGS_HEALTH_LAUNCHD_LABEL:-com.ael.degendogs.mission3.health}"
INTERVAL_SECONDS="${DEGEN_DOGS_HEALTH_INTERVAL_SECONDS:-300}"
REFRESH_INTERVAL_SECONDS="${DEGEN_DOGS_REFRESH_INTERVAL_SECONDS:-3600}"
WATCHER_INTERVAL_SECONDS="${MISSION3_WATCHER_INTERVAL_SECONDS:-30}"
WATCHER_AUTO_PUSH="${MISSION3_WATCHER_AUTO_PUSH:-0}"
PLIST_DIR="${USER_HOME}/Library/LaunchAgents"
LOG_DIR="${DEGEN_DOGS_LOG_DIR:-${USER_HOME}/Library/Logs/degen-dogs-mission3}"
LOCK_DIR="${DEGEN_DOGS_LOCK_DIR:-${USER_HOME}/Library/Caches/degen-dogs-mission3}"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
SCRIPT_PATH="${REPO_DIR}/scripts/degen_dogs_runner_health.py"

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

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
if (( WATCHER_INTERVAL_SECONDS < 30 )); then
  fail "watcher interval too small; refusing to supervise an interval under 30 seconds"
fi
[[ "$WATCHER_AUTO_PUSH" == "0" || "$WATCHER_AUTO_PUSH" == "1" ]] || fail "MISSION3_WATCHER_AUTO_PUSH must be 0 or 1"
[[ "$REPO_DIR" = /* ]] || fail "repo dir must be absolute: ${REPO_DIR}"
[[ -f "$SCRIPT_PATH" ]] || fail "health script missing: ${SCRIPT_PATH}"

mkdir -p "$PLIST_DIR" "$LOG_DIR" "$LOCK_DIR"
chmod 700 "$LOCK_DIR" || true
if [[ ! -x "$SCRIPT_PATH" ]]; then
  chmod +x "$SCRIPT_PATH"
fi

MISSION3_REFRESH_COMMAND="${MISSION3_REFRESH_COMMAND:-}"
if [[ -z "$MISSION3_REFRESH_COMMAND" && "$WATCHER_AUTO_PUSH" == "1" ]]; then
  MISSION3_REFRESH_COMMAND="npm run refresh:publish"
fi
if [[ -z "$MISSION3_REFRESH_COMMAND" ]]; then
  MISSION3_REFRESH_COMMAND="npm run refresh:current"
fi

PLIST_PATH="$PLIST_PATH" \
LABEL="$LABEL" \
SCRIPT_PATH="$SCRIPT_PATH" \
REPO_DIR="$REPO_DIR" \
LOG_DIR="$LOG_DIR" \
LOCK_DIR="$LOCK_DIR" \
INTERVAL_SECONDS="$INTERVAL_SECONDS" \
REFRESH_INTERVAL_SECONDS="$REFRESH_INTERVAL_SECONDS" \
WATCHER_INTERVAL_SECONDS="$WATCHER_INTERVAL_SECONDS" \
WATCHER_AUTO_PUSH="$WATCHER_AUTO_PUSH" \
MISSION3_REFRESH_COMMAND="$MISSION3_REFRESH_COMMAND" \
python3 - <<'PY'
from __future__ import annotations

import os
import plistlib
import tempfile
from pathlib import Path

env = {
    "HOME": os.environ["HOME"],
    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "GIT_TERMINAL_PROMPT": "0",
    "DEGEN_DOGS_REPO_DIR": os.environ["REPO_DIR"],
    "DEGEN_DOGS_LOG_DIR": os.environ["LOG_DIR"],
    "DEGEN_DOGS_LOCK_DIR": os.environ["LOCK_DIR"],
    "DEGEN_DOGS_REFRESH_INTERVAL_SECONDS": os.environ["REFRESH_INTERVAL_SECONDS"],
    "MISSION3_WATCHER_INTERVAL_SECONDS": os.environ["WATCHER_INTERVAL_SECONDS"],
    "MISSION3_WATCHER_AUTO_PUSH": os.environ["WATCHER_AUTO_PUSH"],
    "MISSION3_REFRESH_COMMAND": os.environ["MISSION3_REFRESH_COMMAND"],
    "MISSION3_REFRESH_LOCK_PATH": f"{os.environ['LOCK_DIR']}/refresh.lock",
}
allowlist = (
    os.environ["DEGEN_DOGS_RUNNER_COMMON_ENV_ALLOWLIST"]
    + os.environ["DEGEN_DOGS_RUNNER_ARCHIVE_ENV_ALLOWLIST"]
    + os.environ["DEGEN_DOGS_RUNNER_WATCHER_ENV_ALLOWLIST"]
    + os.environ["DEGEN_DOGS_RUNNER_HEALTH_ENV_ALLOWLIST"]
)
for key in allowlist.split():
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
    "StandardOutPath": f"{os.environ['LOG_DIR']}/health.launchd.out.log",
    "StandardErrorPath": f"{os.environ['LOG_DIR']}/health.launchd.err.log",
    "EnvironmentVariables": env,
}
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
echo "monitors: com.ael.degendogs.mission3.refresh, com.ael.degendogs.mission3.watch-auction"
echo "logs: ${LOG_DIR}/health.launchd.out.log and ${LOG_DIR}/health.launchd.err.log"
