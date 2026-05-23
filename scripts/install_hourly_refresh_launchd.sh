#!/usr/bin/env bash
set -Eeuo pipefail

# Install the hourly launchd job for the Degen Dogs Mission 3 private refresh runner.

PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

REPO_DIR="${DEGEN_DOGS_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LABEL="${DEGEN_DOGS_LAUNCHD_LABEL:-com.ael.degendogs.mission3.refresh}"
INTERVAL_SECONDS="${DEGEN_DOGS_REFRESH_INTERVAL_SECONDS:-3600}"
PLIST_DIR="${HOME}/Library/LaunchAgents"
LOG_DIR="${HOME}/Library/Logs/degen-dogs-mission3"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
SCRIPT_PATH="${REPO_DIR}/scripts/refresh_and_publish.sh"

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

[[ "$LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "invalid launchd label: ${LABEL}"
[[ "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || fail "interval must be an integer number of seconds"
if (( INTERVAL_SECONDS < 300 )); then
  fail "interval too small; refusing to schedule under 300 seconds"
fi
[[ "$REPO_DIR" = /* ]] || fail "repo dir must be absolute: ${REPO_DIR}"
[[ -f "$SCRIPT_PATH" ]] || fail "refresh script missing: ${SCRIPT_PATH}"

mkdir -p "$PLIST_DIR" "$LOG_DIR"

if [[ ! -x "$SCRIPT_PATH" ]]; then
  chmod +x "$SCRIPT_PATH"
fi

PLIST_PATH="$PLIST_PATH" LABEL="$LABEL" SCRIPT_PATH="$SCRIPT_PATH" REPO_DIR="$REPO_DIR" LOG_DIR="$LOG_DIR" INTERVAL_SECONDS="$INTERVAL_SECONDS" python3 - <<'PY'
from __future__ import annotations

import os
import plistlib
from pathlib import Path

plist = {
    "Label": os.environ["LABEL"],
    "ProgramArguments": [os.environ["SCRIPT_PATH"]],
    "WorkingDirectory": os.environ["REPO_DIR"],
    "StartInterval": int(os.environ["INTERVAL_SECONDS"]),
    "RunAtLoad": False,
    "StandardOutPath": f"{os.environ['LOG_DIR']}/launchd.out.log",
    "StandardErrorPath": f"{os.environ['LOG_DIR']}/launchd.err.log",
    "EnvironmentVariables": {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "DEGEN_DOGS_REPO_DIR": os.environ["REPO_DIR"],
    },
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
echo "logs: ${LOG_DIR}/refresh.log"
