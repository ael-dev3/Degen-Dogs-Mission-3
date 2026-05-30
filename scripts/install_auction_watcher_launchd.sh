#!/usr/bin/env bash
set -Eeuo pipefail

# Install the event-aware Mission 3 auction watcher launchd job on macOS.
# The watcher is separate from the hourly refresh fallback and shares the same
# refresh lock so event-triggered and hourly refreshes cannot overlap.

PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

USER_HOME="${HOME:-$(python3 - <<'PY'
import os
import pwd
print(pwd.getpwuid(os.getuid()).pw_dir)
PY
)}"
export HOME="$USER_HOME"

REPO_DIR="${DEGEN_DOGS_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LABEL="${DEGEN_DOGS_WATCHER_LAUNCHD_LABEL:-com.ael.degendogs.mission3.watch-auction}"
INTERVAL_SECONDS="${MISSION3_WATCHER_INTERVAL_SECONDS:-60}"
PLIST_DIR="${USER_HOME}/Library/LaunchAgents"
LOG_DIR="${DEGEN_DOGS_LOG_DIR:-${USER_HOME}/Library/Logs/degen-dogs-mission3}"
LOCK_DIR="${DEGEN_DOGS_LOCK_DIR:-${USER_HOME}/Library/Caches/degen-dogs-mission3}"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
SCRIPT_PATH="${REPO_DIR}/scripts/watch_mission3_onchain_activity.py"

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

[[ "$LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "invalid launchd label: ${LABEL}"
[[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || fail "interval must be an integer number of seconds"
if (( INTERVAL_SECONDS < 30 )); then
  fail "interval too small; refusing to schedule under 30 seconds"
fi
[[ "$REPO_DIR" = /* ]] || fail "repo dir must be absolute: ${REPO_DIR}"
[[ -f "$SCRIPT_PATH" ]] || fail "watcher script missing: ${SCRIPT_PATH}"

mkdir -p "$PLIST_DIR" "$LOG_DIR" "$LOCK_DIR"
chmod 700 "$LOCK_DIR" || true

if [[ ! -x "$SCRIPT_PATH" ]]; then
  chmod +x "$SCRIPT_PATH"
fi

# Safe default: the installed watcher runs local refresh only. To allow publish,
# install with:
#   MISSION3_WATCHER_AUTO_PUSH=1 MISSION3_REFRESH_COMMAND="npm run refresh:publish" npm run watch:install
MISSION3_WATCHER_AUTO_PUSH="${MISSION3_WATCHER_AUTO_PUSH:-0}"
MISSION3_REFRESH_COMMAND="${MISSION3_REFRESH_COMMAND:-}"
if [[ -z "$MISSION3_REFRESH_COMMAND" && "$MISSION3_WATCHER_AUTO_PUSH" == "1" ]]; then
  MISSION3_REFRESH_COMMAND="npm run refresh:publish"
fi
if [[ -z "$MISSION3_REFRESH_COMMAND" ]]; then
  MISSION3_REFRESH_COMMAND="npm run data && npm run build"
fi
export MISSION3_WATCHER_AUTO_PUSH MISSION3_REFRESH_COMMAND

PLIST_PATH="$PLIST_PATH" \
LABEL="$LABEL" \
SCRIPT_PATH="$SCRIPT_PATH" \
REPO_DIR="$REPO_DIR" \
LOG_DIR="$LOG_DIR" \
LOCK_DIR="$LOCK_DIR" \
INTERVAL_SECONDS="$INTERVAL_SECONDS" \
python3 - <<'PY'
from __future__ import annotations

import os
import plistlib
from pathlib import Path

pass_through = [
    "BASE_RPC_URL",
    "BASE_RPC_URLS",
    "BASE_LOG_RPC_URLS",
    "BASE_FROM_BLOCK",
    "DEGEN_DOGS_REPO_DIR",
    "DEGEN_DOGS_LOG_DIR",
    "DEGEN_DOGS_LOCK_DIR",
    "DEGEN_DOGS_REMOTE",
    "DEGEN_DOGS_BRANCH",
    "DEGEN_DOGS_SKIP_PUSH",
    "DEGEN_DOGS_SKIP_PULL",
    "MISSION3_WATCHER_INTERVAL_SECONDS",
    "MISSION3_WATCHER_COOLDOWN_SECONDS",
    "MISSION3_WATCHER_BID_COOLDOWN_SECONDS",
    "MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS",
    "MISSION3_WATCHER_LOOKBACK_BLOCKS",
    "MISSION3_WATCHER_SAFETY_OVERLAP_BLOCKS",
    "MISSION3_WATCHER_LOG_CHUNK",
    "MISSION3_WATCHER_STATE_PATH",
    "MISSION3_WATCHER_LOCK_PATH",
    "MISSION3_WATCHER_LOG_PATH",
    "MISSION3_REFRESH_LOCK_PATH",
    "MISSION3_REFRESH_COMMAND",
    "MISSION3_WATCHER_AUTO_PUSH",
    "MISSION3_WATCHER_REQUIRE_CLEAN_TREE",
    "MISSION3_WATCHER_REFRESH_TIMEOUT_SECONDS",
]

env = {
    "HOME": os.environ["HOME"],
    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "GIT_TERMINAL_PROMPT": "0",
    "DEGEN_DOGS_REPO_DIR": os.environ["REPO_DIR"],
    "DEGEN_DOGS_LOG_DIR": os.environ["LOG_DIR"],
    "DEGEN_DOGS_LOCK_DIR": os.environ["LOCK_DIR"],
    "MISSION3_WATCHER_INTERVAL_SECONDS": os.environ["INTERVAL_SECONDS"],
    "MISSION3_REFRESH_LOCK_PATH": f"{os.environ['LOCK_DIR']}/refresh.lock",
}
for key in pass_through:
    value = os.environ.get(key)
    if value:
        env[key] = value

plist = {
    "Label": os.environ["LABEL"],
    "ProgramArguments": ["/usr/bin/env", "python3", os.environ["SCRIPT_PATH"], "--once"],
    "WorkingDirectory": os.environ["REPO_DIR"],
    "StartInterval": int(os.environ["INTERVAL_SECONDS"]),
    "RunAtLoad": False,
    "StandardOutPath": f"{os.environ['LOG_DIR']}/watcher.launchd.out.log",
    "StandardErrorPath": f"{os.environ['LOG_DIR']}/watcher.launchd.err.log",
    "EnvironmentVariables": env,
}
path = Path(os.environ["PLIST_PATH"])
path.write_bytes(plistlib.dumps(plist, sort_keys=False))
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
echo "logs: ${LOG_DIR}/watch-onchain.log and ${LOG_DIR}/watcher.launchd.*.log"
echo "state: ${MISSION3_WATCHER_STATE_PATH:-${REPO_DIR}/.local/mission3_onchain_tracker_state.json}"
echo "refresh_lock: ${MISSION3_REFRESH_LOCK_PATH:-${LOCK_DIR}/refresh.lock}"
echo "auto_push: ${MISSION3_WATCHER_AUTO_PUSH}"
echo "refresh_command: ${MISSION3_REFRESH_COMMAND}"
if [[ "$MISSION3_WATCHER_AUTO_PUSH" != "1" ]]; then
  echo "note: auto-push is disabled; set MISSION3_WATCHER_AUTO_PUSH=1 and MISSION3_REFRESH_COMMAND='npm run refresh:publish' to publish event-triggered refreshes."
fi
