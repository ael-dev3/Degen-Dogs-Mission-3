#!/usr/bin/env bash
set -Eeuo pipefail

# Clean, shared entrypoint for the WSL2 systemd units. Secrets are deliberately
# loaded inside the process rather than embedded in a world-readable unit file.
umask 077

job="${1:-}"
case "$job" in
  watcher|publisher|hourly|health|preflight) ;;
  *)
    printf 'usage: %s {watcher|publisher|hourly|health|preflight}\n' "$0" >&2
    exit 64
    ;;
esac

repo_dir="${DEGEN_DOGS_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
[[ "$repo_dir" = /* && "$repo_dir" != /mnt/* ]] || {
  printf 'error: DEGEN_DOGS_REPO_DIR must be an absolute path on the WSL ext4 filesystem\n' >&2
  exit 78
}
repo_dir="$(cd "$repo_dir" && pwd -P)"
[[ "$repo_dir" != /mnt && "$repo_dir" != /mnt/* ]] || {
  printf 'error: resolved DEGEN_DOGS_REPO_DIR must stay on the WSL ext4 filesystem\n' >&2
  exit 78
}

runner_home="${HOME:?HOME must be set by the systemd unit}"
log_dir="${DEGEN_DOGS_LOG_DIR:-/var/log/degen-dogs}"
lock_dir="${DEGEN_DOGS_LOCK_DIR:-/var/cache/degen-dogs}"
env_file="${DEGEN_DOGS_ENV_FILE:-${repo_dir}/.env.local}"

export HOME="$runner_home"
export PATH="${repo_dir}/scripts/runtime-bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export NPM_CONFIG_CACHE="${lock_dir}/npm"
export DEGEN_DOGS_REPO_DIR="$repo_dir"
export DEGEN_DOGS_LOG_DIR="$log_dir"
export DEGEN_DOGS_LOCK_DIR="$lock_dir"
export DEGEN_DOGS_ENV_FILE="$env_file"
export DEGEN_DOGS_REFRESH_LOCK_PATH="${lock_dir}/refresh.lock"
export MISSION3_REFRESH_LOCK_PATH="$DEGEN_DOGS_REFRESH_LOCK_PATH"

cd "$repo_dir"

# shellcheck source=load_runner_env.sh
source "${repo_dir}/scripts/load_runner_env.sh"
degen_dogs_load_runner_env "$repo_dir"

# These are deployment policy, not operator-tunable configuration. Pin them
# after loading .env.local so no local value can redirect or suppress a push.
export DEGEN_DOGS_REMOTE=origin
export DEGEN_DOGS_BRANCH=main
export DEGEN_DOGS_SKIP_PUSH=0
export DEGEN_DOGS_SKIP_PULL=0
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export GIT_TERMINAL_PROMPT=0

validate_runtime_git_destination() {
  local origin_url push_urls expected_ssh_command
  [[ "$(git branch --show-current)" == "main" ]] || {
    printf 'error: publisher checkout is not on main\n' >&2
    return 1
  }
  origin_url="$(git remote get-url origin)"
  [[ "$origin_url" == 'git@github-degen-dogs:ael-dev3/Degen-Dogs-Mission-3.git' ]] || {
    printf 'error: publisher origin is not the pinned deploy-key destination\n' >&2
    return 1
  }
  [[ -z "$(git config --local --get-all remote.origin.pushurl || true)" ]] || {
    printf 'error: publisher origin has a separate pushurl\n' >&2
    return 1
  }
  push_urls="$(git remote get-url --push --all origin)"
  [[ "$push_urls" == "$origin_url" ]] || {
    printf 'error: publisher push destination differs from origin\n' >&2
    return 1
  }
  expected_ssh_command="ssh -F ${runner_home}/.ssh/degen_dogs_config"
  [[ "$(git config --local --get core.sshCommand)" == "$expected_ssh_command" ]] || {
    printf 'error: publisher SSH command is not pinned\n' >&2
    return 1
  }
  [[ "$(git config --local --get core.hooksPath)" == "/dev/null" ]] || {
    printf 'error: publisher Git hooks are not disabled\n' >&2
    return 1
  }
}

validate_runtime_git_destination

# Machine identity is public-safe and intentionally generic: do not use a
# hostname, user name, serial number, or other private machine identifier.
export DEGEN_DOGS_RUNNER_ID="${DEGEN_DOGS_RUNNER_ID:-windows-wsl}"

require_production_rpc_quorum() {
  python3 - <<'PY'
import os

for name in ("BASE_RPC_URLS", "BASE_LOG_RPC_URLS"):
    values = [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]
    if len(values) < 2:
        raise SystemExit(f"error: {name} must contain at least two independently operated production endpoints")

try:
    quorum = int(os.environ.get("BASE_RPC_QUORUM_SIZE", "2"))
except ValueError as exc:
    raise SystemExit("error: BASE_RPC_QUORUM_SIZE must be an integer") from exc
if quorum < 2:
    raise SystemExit("error: BASE_RPC_QUORUM_SIZE must be at least 2")
PY
}

case "$job" in
  watcher)
    require_production_rpc_quorum
    # Keep event publication fast. Archive reconciliation belongs to the
    # staggered hourly job and both paths still share the same publisher lock.
    export MISSION3_WATCHER_AUTO_PUSH=1
    export MISSION3_REFRESH_COMMAND='npm run refresh:publish'
    export MISSION3_WATCHER_REQUIRE_CLEAN_TREE=1
    export MISSION3_WATCHER_FORCE_REFRESH_AFTER_SECONDS=0
    export DEGEN_DOGS_FULL_REFRESH=0
    export DEGEN_DOGS_RUN_MISSION3_ARCHIVE=0
    export DEGEN_DOGS_REFRESH_TRIGGER=watcher
    exec python3 "${repo_dir}/scripts/watch_mission3_onchain_activity.py" --once
    ;;
  publisher)
    require_production_rpc_quorum
    # Re-pin every path captured before .env.local was loaded. The queue and
    # recovery journal may select only a validated generation/digest; neither
    # may select a command or filesystem path.
    export DEGEN_DOGS_REPO_DIR="$repo_dir"
    export DEGEN_DOGS_LOG_DIR="$log_dir"
    export DEGEN_DOGS_LOCK_DIR="$lock_dir"
    export DEGEN_DOGS_ENV_FILE="$env_file"
    export DEGEN_DOGS_REFRESH_LOCK_PATH="${lock_dir}/refresh.lock"
    export MISSION3_REFRESH_LOCK_PATH="$DEGEN_DOGS_REFRESH_LOCK_PATH"
    unset MISSION3_REFRESH_COMMAND
    exec python3 "${repo_dir}/scripts/drain_publication_queue.py"
    ;;
  hourly)
    require_production_rpc_quorum
    hourly_run_mission3_archive="${DEGEN_DOGS_RUN_MISSION3_ARCHIVE-1}"
    case "$hourly_run_mission3_archive" in
      0|1) ;;
      *)
        printf '%s\n' 'error: DEGEN_DOGS_RUN_MISSION3_ARCHIVE must be 0 or 1' >&2
        exit 78
        ;;
    esac
    export DEGEN_DOGS_FULL_REFRESH=0
    export DEGEN_DOGS_RUN_MISSION3_ARCHIVE="$hourly_run_mission3_archive"
    export DEGEN_DOGS_REFRESH_TRIGGER=hourly_refresh
    exec /bin/bash -p "${repo_dir}/scripts/refresh_and_publish.sh"
    ;;
  health)
    exec python3 "${repo_dir}/scripts/check_wsl_runner_health.py"
    ;;
  preflight)
    require_production_rpc_quorum
    export MISSION3_WATCHER_AUTO_PUSH=0
    export MISSION3_REFRESH_COMMAND='npm run refresh:current'
    export MISSION3_WATCHER_REQUIRE_CLEAN_TREE=1
    export MISSION3_WATCHER_LOG_PATH=-
    export MISSION3_WATCHER_STATE_PATH="${lock_dir}/watcher-preflight-state.json"
    export DEGEN_DOGS_SKIP_PUSH=1
    python3 "${repo_dir}/scripts/preflight_wsl_rpc.py"
    exec python3 "${repo_dir}/scripts/watch_mission3_onchain_activity.py" --once --dry-run
    ;;
esac
