#!/usr/bin/env bash

# Shared, source-only loader for launchd installer configuration. It exports
# KEY=value entries from a protected local file without printing their values.

degen_dogs_load_runner_env() {
  local default_repo_dir="${1:?default repository directory is required}"
  local env_file="${DEGEN_DOGS_ENV_FILE:-${default_repo_dir}/.env.local}"
  if [[ ! -e "$env_file" ]]; then
    return 0
  fi
  if ! python3 - "$env_file" <<'PY'
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
if not path.is_file():
    raise SystemExit(f"error: runner environment path is not a regular file: {path}")
details = path.stat()
if details.st_uid != os.getuid():
    raise SystemExit(f"error: runner environment file is not owned by the current user: {path}")
if stat.S_IMODE(details.st_mode) & 0o077:
    raise SystemExit(f"error: runner environment file must be mode 600 (chmod 600 {path})")
PY
  then
    return 1
  fi
  # Preserve explicitly exported process settings so conventional precedence
  # remains: installer/launch environment > .env.local > script defaults.
  local prior_exports
  prior_exports="$(export -p)"
  local restore_allexport=0
  if [[ $- != *a* ]]; then
    restore_allexport=1
    set -a
  fi
  # shellcheck disable=SC1090
  source "$env_file"
  # Bash emits safely quoted `declare -x` statements from `export -p`. Reapply
  # them as exports so this function does not turn them into function locals.
  eval "${prior_exports//declare -x/export}"
  if (( restore_allexport == 1 )); then
    set +a
  fi
  DEGEN_DOGS_ENV_FILE="$(cd "$(dirname "$env_file")" && pwd)/$(basename "$env_file")"
  export DEGEN_DOGS_ENV_FILE
}

degen_dogs_warn_public_rpc_fallback() {
  if [[ -z "${BASE_RPC_URLS:-}" || -z "${BASE_LOG_RPC_URLS:-}" ]]; then
    printf '%s\n' \
      'warning: no explicit BASE_RPC_URLS and BASE_LOG_RPC_URLS quorum is configured; public RPC fallbacks are best-effort and have no production SLA.' >&2
  fi
}

degen_dogs_export_runner_env_allowlist() {
  # Single source of truth for protected configuration that launchd jobs may
  # inherit. Installers copy only non-empty values into mode-600 plists and
  # select the least-privilege groups needed by each job.
  DEGEN_DOGS_RUNNER_COMMON_ENV_ALLOWLIST='
BASE_RPC_URL
BASE_RPC_URLS
BASE_LOG_RPC_URLS
BASE_RPC_QUORUM_SIZE
BASE_SNAPSHOT_CONFIRMATIONS
BASE_RPC_QUORUM_DEADLINE_SECONDS
BASE_RPC_HEAD_PROBE_DEADLINE_SECONDS
BASE_RPC_HEAD_PROBE_GRACE_SECONDS
BASE_RPC_SLOW_COOLDOWN_SECONDS
BASE_FROM_BLOCK
BASE_LOG_CHUNK
BASE_LOG_WORKERS
BASE_RPC_ATTEMPTS
BASE_RPC_BATCH_LIMIT
BASE_LOG_RPC_TIMEOUT
BASE_BLOCK_TIME_RPC_TIMEOUT
DOG_METADATA_WORKERS
DOG_METADATA_FETCH_TIMEOUT
DOG_METADATA_FALLBACK_TIMEOUT
DOG_METADATA_ITEM_TIMEOUT
DOG_METADATA_SEQUENTIAL_THRESHOLD
MISSION3_LOG_CACHE
MISSION3_LOG_CACHE_OVERLAP_BLOCKS
MISSION3_LOG_QUORUM_MAX_BLOCKS
MISSION3_LOG_QUORUM_WINDOW_BLOCKS
MISSION3_CURRENT_SURFACE_OVERLAP
MISSION3_SETTLEMENT_RECON_MARGIN_BLOCKS
MISSION3_BALANCE_CACHE
NEYNAR_API_KEY
WOOF_USD_PRICE
SUP_USD_PRICE
COINGECKO_API_KEY
HISTORICAL_PRICES_PREFER_COINGECKO
DEGEN_DOGS_ENV_FILE
DEGEN_DOGS_REMOTE
DEGEN_DOGS_BRANCH
DEGEN_DOGS_COMMIT_PREFIX
DEGEN_DOGS_SKIP_PUSH
DEGEN_DOGS_SKIP_PULL
DEGEN_DOGS_RUN_MISSION3_ARCHIVE
DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH
DEGEN_DOGS_LIVE_VERIFY_TIMEOUT_SECONDS
DEGEN_DOGS_LIVE_VERIFY_INTERVAL_SECONDS
DEGEN_DOGS_LIVE_VERIFY_BASE_URL
DEGEN_DOGS_GIT_RETRY_ATTEMPTS
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS
DEGEN_DOGS_REFRESH_TELEMETRY_PATH
DEGEN_DOGS_REFRESH_METRICS_PATH
'
  DEGEN_DOGS_RUNNER_ARCHIVE_ENV_ALLOWLIST='
MISSION3_FROM_BLOCK
MISSION3_TO_BLOCK
MISSION3_ARCHIVE_DB
MISSION3_OUTPUT_DIR
MISSION3_LOG_CHUNK
MISSION3_LOG_WORKERS
'
  DEGEN_DOGS_RUNNER_WATCHER_ENV_ALLOWLIST='
MISSION3_WATCHER_TELEMETRY_PATH
MISSION3_WATCHER_INTERVAL_SECONDS
MISSION3_WATCHER_COOLDOWN_SECONDS
MISSION3_WATCHER_BID_COOLDOWN_SECONDS
MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS
MISSION3_WATCHER_LOOKBACK_BLOCKS
MISSION3_WATCHER_LOG_WINDOW_BLOCKS
MISSION3_WATCHER_SAFETY_OVERLAP_BLOCKS
MISSION3_WATCHER_LOG_SAFETY_OVERLAP_BLOCKS
MISSION3_WATCHER_LOG_CHUNK
MISSION3_WATCHER_STATE_PATH
MISSION3_WATCHER_LOCK_PATH
MISSION3_WATCHER_LOG_PATH
MISSION3_REFRESH_COMMAND
MISSION3_WATCHER_AUTO_PUSH
MISSION3_WATCHER_REQUIRE_CLEAN_TREE
MISSION3_WATCHER_REFRESH_TIMEOUT_SECONDS
'
  DEGEN_DOGS_RUNNER_HEALTH_ENV_ALLOWLIST='
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
'
  export \
    DEGEN_DOGS_RUNNER_COMMON_ENV_ALLOWLIST \
    DEGEN_DOGS_RUNNER_ARCHIVE_ENV_ALLOWLIST \
    DEGEN_DOGS_RUNNER_WATCHER_ENV_ALLOWLIST \
    DEGEN_DOGS_RUNNER_HEALTH_ENV_ALLOWLIST
}
