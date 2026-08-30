#!/usr/bin/env bash

# Shared, source-only loader for launchd installer configuration. It exports
# KEY=value entries from a protected local file without printing their values.

degen_dogs_load_runner_env() {
  local default_repo_dir="${1:?default repository directory is required}"
  local export_allowlist="*"
  if (( $# >= 2 )); then
    export_allowlist="$2"
  fi
  local env_file="${DEGEN_DOGS_ENV_FILE:-${default_repo_dir}/.env.local}"
  if [[ ! -e "$env_file" ]]; then
    return 0
  fi
  local parsed_records
  if ! parsed_records="$(PYTHONNOUSERSITE=1 python3 -I - "$env_file" "$export_allowlist" <<'PY'
from __future__ import annotations

import os
import re
import shlex
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
raw_export_allowlist = sys.argv[2]
export_allowlist = None if raw_export_allowlist == "*" else set(raw_export_allowlist.split())
flags = os.O_RDONLY
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
try:
    descriptor = os.open(path, flags)
except OSError as exc:
    raise SystemExit(f"error: unable to securely open runner environment file {path}: {exc}") from exc

details = os.fstat(descriptor)
if not stat.S_ISREG(details.st_mode):
    os.close(descriptor)
    raise SystemExit(f"error: runner environment path is not a regular file: {path}")
if details.st_uid != os.getuid():
    os.close(descriptor)
    raise SystemExit(f"error: runner environment file is not owned by the current user: {path}")
if stat.S_IMODE(details.st_mode) & 0o077:
    os.close(descriptor)
    raise SystemExit(f"error: runner environment file must be mode 600 (chmod 600 {path})")
if details.st_size > 131_072:
    os.close(descriptor)
    raise SystemExit(f"error: runner environment file is unexpectedly large: {path}")

with os.fdopen(descriptor, "r", encoding="utf-8", errors="strict") as handle:
    lines = handle.read().splitlines()

key_pattern = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
allowed_key_pattern = re.compile(
    r"(?:BASE_|MISSION[123]_|DEGEN_DOGS_|DOG_METADATA_|HISTORICAL_)"
    r"[A-Za-z0-9_]*\Z"
    r"|(?:NEYNAR_API_KEY|COINGECKO_API_KEY|DUNE_API_KEY|WOOF_USD_PRICE|SUP_USD_PRICE)\Z"
)
seen: set[str] = set()
if export_allowlist is not None:
    invalid_exports = sorted(key for key in export_allowlist if not key_pattern.fullmatch(key))
    if invalid_exports:
        raise SystemExit("error: invalid runner environment export allowlist")
for line_number, original in enumerate(lines, 1):
    line = original.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("export "):
        line = line[7:].lstrip()
    if "=" not in line:
        raise SystemExit(f"error: invalid runner environment entry on line {line_number}: expected KEY=value")
    key, raw_value = line.split("=", 1)
    key = key.strip()
    if not key_pattern.fullmatch(key):
        raise SystemExit(f"error: invalid runner environment key on line {line_number}: {key!r}")
    if not allowed_key_pattern.fullmatch(key):
        raise SystemExit(f"error: unsupported runner environment key on line {line_number}: {key}")
    if key in seen:
        raise SystemExit(f"error: duplicate runner environment key on line {line_number}: {key}")
    seen.add(key)

    raw_value = raw_value.strip()
    if raw_value.startswith(("'", '"')):
        try:
            values = shlex.split(raw_value, comments=False, posix=True)
        except ValueError as exc:
            raise SystemExit(f"error: invalid quoted value on line {line_number}: {exc}") from exc
        if len(values) != 1:
            raise SystemExit(f"error: quoted runner environment value on line {line_number} must be one value")
        value = values[0]
    else:
        # Unquoted text is data, never shell syntax. In particular, command
        # substitutions and backticks remain literal strings.
        value = raw_value
    if "\x00" in value:
        raise SystemExit(f"error: NUL byte in runner environment value on line {line_number}")
    if export_allowlist is not None and key not in export_allowlist:
        continue
    encoded = "".join(f"\\x{byte:02x}" for byte in value.encode("utf-8"))
    print(f"{key}\t{encoded}")
PY
  )"
  then
    return 1
  fi

  # Preserve explicitly exported process settings so conventional precedence
  # remains: installer/launch environment > .env.local > script defaults.
  # Decode inert byte escapes without source/eval so the config file can never
  # execute shell code and secrets never need to be copied into a temp file.
  local key encoded_value value declaration
  while IFS=$'\t' read -r key encoded_value; do
    [[ -n "$key" ]] || continue
    declaration="$(declare -p "$key" 2>/dev/null || true)"
    if [[ "$declaration" == "declare -x "* ]]; then
      continue
    fi
    printf -v value '%b' "$encoded_value"
    printf -v "$key" '%s' "$value"
    export "$key"
  done <<<"$parsed_records"
  DEGEN_DOGS_ENV_FILE="$(cd "$(dirname "$env_file")" && pwd)/$(basename "$env_file")"
  export DEGEN_DOGS_ENV_FILE
}

degen_dogs_warn_public_rpc_fallback() {
  if [[ -z "${BASE_RPC_URLS:-}" || -z "${BASE_LOG_RPC_URLS:-}" ]]; then
    printf '%s\n' \
      'warning: no explicit BASE_RPC_URLS and BASE_LOG_RPC_URLS quorum is configured; public RPC fallbacks are best-effort and have no production SLA.' >&2
  fi
}

degen_dogs_validate_watcher_refresh_command() {
  local refresh_command="${1-}"
  local auto_push="${2-0}"
  case "$refresh_command" in
    'npm run refresh:current')
      return 0
      ;;
    'npm run refresh:publish')
      if [[ "$auto_push" != "1" ]]; then
        printf '%s\n' \
          'error: npm run refresh:publish requires MISSION3_WATCHER_AUTO_PUSH=1' >&2
        return 1
      fi
      return 0
      ;;
    *)
      printf '%s\n' \
        "error: MISSION3_REFRESH_COMMAND must exactly match 'npm run refresh:current' or 'npm run refresh:publish'; shell syntax, paths, extra arguments, and whitespace variants are forbidden" >&2
      return 1
      ;;
  esac
}

degen_dogs_export_runner_env_allowlist() {
  # Single source of truth for protected configuration that launchd jobs may
  # inherit. Installers copy only non-empty values into mode-600 plists and
  # select the least-privilege groups needed by each job.
  DEGEN_DOGS_RUNNER_COMMON_ENV_ALLOWLIST='
BASE_RPC_URL
BASE_RPC_URLS
BASE_LOG_RPC_URLS
BASE_INCLUDE_PUBLIC_FALLBACKS
BASE_RPC_QUORUM_SIZE
BASE_SNAPSHOT_CONFIRMATIONS
BASE_RPC_QUORUM_DEADLINE_SECONDS
BASE_RPC_HEAD_PROBE_DEADLINE_SECONDS
BASE_RPC_HEAD_PROBE_GRACE_SECONDS
BASE_RPC_SLOW_COOLDOWN_SECONDS
BASE_RPC_MAX_HEAD_SPREAD_BLOCKS
BASE_RPC_MAX_BLOCK_AGE_SECONDS
BASE_FROM_BLOCK
BASE_LOG_CHUNK
BASE_LOG_WORKERS
BASE_RPC_ATTEMPTS
BASE_RPC_BATCH_LIMIT
BASE_RPC_MAX_RESPONSE_BYTES
BASE_LOG_RPC_TIMEOUT
BASE_BLOCK_TIME_RPC_TIMEOUT
DOG_METADATA_WORKERS
DOG_METADATA_FETCH_TIMEOUT
DOG_METADATA_FALLBACK_TIMEOUT
DOG_METADATA_ITEM_TIMEOUT
DOG_METADATA_SEQUENTIAL_THRESHOLD
DOG_METADATA_ALLOWED_HOSTS
DOG_METADATA_MAX_RESPONSE_BYTES
DOG_METADATA_CACHE_MAX_AGE_SECONDS
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
DEGEN_DOGS_RUNNER_ID
DEGEN_DOGS_REFRESH_LOCK_PATH
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
MISSION3_ARCHIVE_OVERLAP_BLOCKS
MISSION3_ARCHIVE_MAX_AGE_SECONDS
MISSION3_ARCHIVE_MAX_HEAD_LAG_BLOCKS
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
DEGEN_DOGS_HEALTH_REFRESH_RETRY_STATE_PATH
DEGEN_DOGS_HEALTH_REFRESH_RETRY_BASE_SECONDS
DEGEN_DOGS_HEALTH_REFRESH_RETRY_MAX_SECONDS
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
