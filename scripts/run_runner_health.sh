#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Manual health checks must use the same protected runner policy as the
# launchd watchdog without inheriting RPC endpoints or API credentials.

BASE_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PATH="$BASE_PATH"
export PATH

USER_HOME="${HOME:-$(PYTHONNOUSERSITE=1 python3 -I - <<'PY'
import os
import pwd
print(pwd.getpwuid(os.getuid()).pw_dir)
PY
)}"
export HOME="$USER_HOME"

SCRIPT_REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$SCRIPT_REPO_DIR" == *:* || "$SCRIPT_REPO_DIR" == *$'\n'* ]]; then
  printf '%s\n' 'error: repository path contains a PATH separator or newline' >&2
  exit 1
fi

# shellcheck source=load_runner_env.sh
source "${SCRIPT_REPO_DIR}/scripts/load_runner_env.sh"
degen_dogs_load_runner_env "$SCRIPT_REPO_DIR"
# shellcheck source=runner_permissions.sh
source "${SCRIPT_REPO_DIR}/scripts/runner_permissions.sh"

REPO_DIR="${DEGEN_DOGS_REPO_DIR:-$SCRIPT_REPO_DIR}"
if [[ "$REPO_DIR" != /* || "$REPO_DIR" == *:* || "$REPO_DIR" == *$'\n'* ]]; then
  printf '%s\n' 'error: configured repository path must be absolute without a PATH separator or newline' >&2
  exit 1
fi
HEALTH_SCRIPT="${REPO_DIR}/scripts/degen_dogs_runner_health.py"
if [[ ! -f "$HEALTH_SCRIPT" ]]; then
  printf 'error: health script missing: %s\n' "$HEALTH_SCRIPT" >&2
  exit 1
fi

runner_path="$BASE_PATH"
if [[ -e "${REPO_DIR}/.venv" || -L "${REPO_DIR}/.venv" ]]; then
  if [[ -x "${REPO_DIR}/.venv/bin/python3" ]] && \
    [[ -x "${REPO_DIR}/scripts/runtime-bin/python3" ]] && \
    (
      exec -c /usr/bin/env \
        "HOME=${USER_HOME}" \
        "PATH=${BASE_PATH}" \
        "PYTHONNOUSERSITE=1" \
        "${REPO_DIR}/.venv/bin/python3" -I -c \
        'import Crypto; from Crypto.Hash import keccak; assert Crypto.__version__ == "3.23.0"; assert keccak.new(digest_bits=256, data=b"").hexdigest() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"'
    ) >/dev/null 2>&1; then
    runner_path="${REPO_DIR}/scripts/runtime-bin:${BASE_PATH}"
  else
    printf '%s\n' 'error: repo Python virtualenv is present but failed the pinned Keccak runtime check' >&2
    exit 1
  fi
fi

LOG_DIR="$(degen_dogs_resolve_runner_path "$REPO_DIR" "${DEGEN_DOGS_LOG_DIR:-${USER_HOME}/Library/Logs/degen-dogs-mission3}")"
LOCK_DIR="$(degen_dogs_resolve_runner_path "$REPO_DIR" "${DEGEN_DOGS_LOCK_DIR:-${USER_HOME}/Library/Caches/degen-dogs-mission3}")"
REFRESH_LOCK_PATH="$(
  degen_dogs_resolve_runner_path \
    "$REPO_DIR" \
    "${DEGEN_DOGS_REFRESH_LOCK_PATH:-${MISSION3_REFRESH_LOCK_PATH:-${LOCK_DIR}/refresh.lock}}"
)"

watcher_auto_push="${MISSION3_WATCHER_AUTO_PUSH:-0}"
refresh_command="${MISSION3_REFRESH_COMMAND:-}"
if [[ -z "$refresh_command" && "$watcher_auto_push" == "1" ]]; then
  refresh_command="npm run refresh:publish"
fi
if [[ -z "$refresh_command" ]]; then
  refresh_command="npm run refresh:current"
fi
if [[ "$watcher_auto_push" != "0" && "$watcher_auto_push" != "1" ]]; then
  printf '%s\n' 'error: MISSION3_WATCHER_AUTO_PUSH must be 0 or 1' >&2
  exit 1
fi
degen_dogs_validate_watcher_refresh_command "$refresh_command" "$watcher_auto_push"

health_env=(
  "HOME=${USER_HOME}"
  "PATH=${runner_path}"
  "PYTHONNOUSERSITE=1"
  "GIT_TERMINAL_PROMPT=0"
  "DEGEN_DOGS_REPO_DIR=${REPO_DIR}"
  "DEGEN_DOGS_LOG_DIR=${LOG_DIR}"
  "DEGEN_DOGS_LOCK_DIR=${LOCK_DIR}"
  "DEGEN_DOGS_REFRESH_LOCK_PATH=${REFRESH_LOCK_PATH}"
  "MISSION3_REFRESH_LOCK_PATH=${REFRESH_LOCK_PATH}"
  "MISSION3_WATCHER_AUTO_PUSH=${watcher_auto_push}"
  "MISSION3_REFRESH_COMMAND=${refresh_command}"
)

# This is intentionally narrower than the worker installer allowlist. The
# health process can repair workers, inspect local state, and notify operators;
# it never needs an RPC URL, API key, token, or price override.
safe_health_keys=(
  DEGEN_DOGS_ENV_FILE
  DEGEN_DOGS_FULL_REFRESH
  DEGEN_DOGS_HEALTH_ALERT_DRY_RUN
  DEGEN_DOGS_HEALTH_ALERT_STATE_PATH
  DEGEN_DOGS_HEALTH_CRITICAL_STALE_SECONDS
  DEGEN_DOGS_HEALTH_DISCORD_MENTION
  DEGEN_DOGS_HEALTH_DRY_RUN
  DEGEN_DOGS_HEALTH_GITHUB_ALERTS
  DEGEN_DOGS_HEALTH_GITHUB_REPO
  DEGEN_DOGS_HEALTH_HOME
  DEGEN_DOGS_HEALTH_LIVE_STALE_SECONDS
  DEGEN_DOGS_HEALTH_LOG_EMERGENCY_MAX_BYTES
  DEGEN_DOGS_HEALTH_LOG_MAX_BYTES
  DEGEN_DOGS_HEALTH_LOG_RETAIN_BYTES
  DEGEN_DOGS_HEALTH_MIN_FREE_BYTES
  DEGEN_DOGS_HEALTH_MIN_FREE_PERCENT
  DEGEN_DOGS_HEALTH_PENDING_STALE_SECONDS
  DEGEN_DOGS_HEALTH_REFRESH_ACTIVE_GRACE_SECONDS
  DEGEN_DOGS_HEALTH_REPEAT_ALERT_SECONDS
  DEGEN_DOGS_HEALTH_WATCHER_ACTIVE_GRACE_SECONDS
  DEGEN_DOGS_HEALTH_WATCHER_STALE_SECONDS
  DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH
  DEGEN_DOGS_REFRESH_INTERVAL_SECONDS
  DEGEN_DOGS_REFRESH_METRICS_PATH
  DEGEN_DOGS_REFRESH_TELEMETRY_PATH
  DEGEN_DOGS_RUN_MISSION3_ARCHIVE
  MISSION3_WATCHER_INTERVAL_SECONDS
  MISSION3_WATCHER_LOCK_PATH
  MISSION3_WATCHER_LOG_PATH
  MISSION3_WATCHER_STATE_PATH
  MISSION3_WATCHER_TELEMETRY_PATH
)
for key in "${safe_health_keys[@]}"; do
  declaration="$(declare -p "$key" 2>/dev/null || true)"
  if [[ -n "$declaration" && -n "${!key}" ]]; then
    health_env+=("${key}=${!key}")
  fi
done

# Bash's exec -c clears the inherited environment in the kernel transition;
# /usr/bin/env itself therefore never receives the credentials loaded above.
cd "$REPO_DIR"
exec -c /usr/bin/env "${health_env[@]}" \
  python3 "$HEALTH_SCRIPT" "$@"
