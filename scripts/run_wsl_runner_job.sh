#!/usr/bin/env bash
set -Eeuo pipefail
export PATH=/usr/bin:/bin

scrub_untrusted_process_environment() {
  local variable_name
  unset BASH_ENV ENV NODE_OPTIONS PYTHONHOME PYTHONPATH SSH_ASKPASS SSH_ASKPASS_REQUIRE
  while IFS= read -r variable_name; do
    [[ "$variable_name" == GIT_* ]] && unset "$variable_name"
  done < <(compgen -e)
  return 0
}

scrub_untrusted_process_environment

# Clean, shared entrypoint for the WSL2 systemd units. Secrets are deliberately
# loaded inside the process rather than embedded in a world-readable unit file.
umask 077

job="${1:-}"
case "$job" in
  watcher|publisher|verifier|hourly|health|preflight) ;;
  *)
    printf 'usage: %s {watcher|publisher|verifier|hourly|health|preflight}\n' "$0" >&2
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
# WSL_TRUSTED_PYTHON_START
python_bin="${DEGEN_DOGS_PYTHON_BIN:-/var/lib/degen-dogs/python-runtime/bin/python3}"
[[ "$python_bin" == "/var/lib/degen-dogs/python-runtime/bin/python3" ]] || {
  printf 'error: DEGEN_DOGS_PYTHON_BIN must use the fixed trusted WSL runtime pointer\n' >&2
  exit 78
}
runtime_link=/var/lib/degen-dogs/python-runtime
for trusted_parent in /var/lib/degen-dogs /var/lib/degen-dogs/python-runtimes; do
  [[ -d "$trusted_parent" && ! -L "$trusted_parent" && \
    "$(/usr/bin/stat -c %U "$trusted_parent")" == root ]] || {
    printf 'error: trusted WSL Python runtime parent is unsafe\n' >&2
    exit 78
  }
  trusted_parent_mode="$(/usr/bin/stat -c %a "$trusted_parent")"
  (( (8#$trusted_parent_mode & 8#022) == 0 && (8#$trusted_parent_mode & 8#005) == 8#005 )) || {
    printf 'error: trusted WSL Python runtime parent mode is unsafe\n' >&2
    exit 78
  }
done
[[ -L "$runtime_link" && "$(/usr/bin/stat -c %U "$runtime_link")" == root && \
  "$(/usr/bin/stat -c %h "$runtime_link")" == 1 ]] || {
  printf 'error: trusted WSL Python runtime pointer is unsafe\n' >&2
  exit 78
}
python_resolved="$(/usr/bin/readlink -f -- "$python_bin")"
[[ "$python_resolved" =~ ^/var/lib/degen-dogs/python-runtimes/[0-9a-f]{40}-[0-9a-f]{40}-v[0-9]+/bin/python3$ && \
  -f "$python_resolved" && ! -L "$python_resolved" && \
  "$(/usr/bin/stat -c %U "$python_resolved")" == "root" && \
  "$(/usr/bin/stat -c %h "$python_resolved")" == "1" ]] || {
  printf 'error: trusted WSL Python runtime is missing or unsafe\n' >&2
  exit 78
}
python_mode="$(/usr/bin/stat -c %a "$python_resolved")"
(( (8#$python_mode & 8#022) == 0 )) || {
  printf 'error: trusted WSL Python runtime is group/other writable\n' >&2
  exit 78
}
# WSL_TRUSTED_PYTHON_END

export HOME="$runner_home"
export PATH="$(/usr/bin/dirname -- "$python_bin"):/usr/bin:/bin"
export DEGEN_DOGS_PYTHON_BIN="$python_bin"
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
scrub_untrusted_process_environment
export DEGEN_DOGS_PYTHON_BIN="$python_bin"

exec_python_entrypoint() {
  local script_path="$1"
  shift
  exec "$python_bin" -I -B -c \
    'import os,runpy,sys; p=sys.argv.pop(1); sys.path.insert(0, os.path.dirname(p)); runpy.run_path(p, run_name="__main__")' \
    "$script_path" "$@"
}

# These are deployment policy, not operator-tunable configuration. Pin them
# after loading .env.local so no local value can redirect or suppress a push.
export DEGEN_DOGS_REMOTE=origin
export DEGEN_DOGS_BRANCH=main
export DEGEN_DOGS_SKIP_PUSH=0
export DEGEN_DOGS_SKIP_PULL=0
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export GIT_TERMINAL_PROMPT=0
export GIT_NO_REPLACE_OBJECTS=1
export GIT_OPTIONAL_LOCKS=0

validate_runtime_git_destination() {
  local origin_url push_urls expected_ssh_command
  [[ "$(/usr/bin/git branch --show-current)" == "main" ]] || {
    printf 'error: publisher checkout is not on main\n' >&2
    return 1
  }
  origin_url="$(/usr/bin/git remote get-url origin)"
  [[ "$origin_url" == 'git@github-degen-dogs:ael-dev3/Degen-Dogs-Mission-3.git' ]] || {
    printf 'error: publisher origin is not the pinned deploy-key destination\n' >&2
    return 1
  }
  [[ -z "$(/usr/bin/git config --local --get-all remote.origin.pushurl || true)" ]] || {
    printf 'error: publisher origin has a separate pushurl\n' >&2
    return 1
  }
  push_urls="$(/usr/bin/git remote get-url --push --all origin)"
  [[ "$push_urls" == "$origin_url" ]] || {
    printf 'error: publisher push destination differs from origin\n' >&2
    return 1
  }
  expected_ssh_command="ssh -F ${runner_home}/.ssh/degen_dogs_config"
  [[ "$(/usr/bin/git config --local --get core.sshCommand)" == "$expected_ssh_command" ]] || {
    printf 'error: publisher SSH command is not pinned\n' >&2
    return 1
  }
  [[ "$(/usr/bin/git config --local --get core.hooksPath)" == "/dev/null" ]] || {
    printf 'error: publisher Git hooks are not disabled\n' >&2
    return 1
  }
}

validate_checkout_tool_surface() {
  local entries
  [[ ! -e "${repo_dir}/.venv" && ! -L "${repo_dir}/.venv" ]] || {
    printf 'error: legacy checkout Python runtime is forbidden\n' >&2
    return 1
  }
  entries="$(/usr/bin/find "${repo_dir}/scripts/runtime-bin" -mindepth 1 -maxdepth 1 -printf '%f\n' | /usr/bin/sort)"
  [[ "$entries" == "python3" ]] || {
    printf 'error: checkout runtime-bin contains an untrusted extra entry\n' >&2
    return 1
  }
  if /usr/bin/find "${repo_dir}/scripts" -xdev \
    \( -name '*.pyc' -o -name '*.pyo' -o -name __pycache__ \) \
    -print -quit | /usr/bin/grep -q .; then
    printf 'error: checkout contains a forbidden Python bytecode cache\n' >&2
    return 1
  fi
  if /usr/bin/find "$repo_dir" -xdev -maxdepth 1 \
    \( -name '*.pyc' -o -name '*.pyo' -o -name __pycache__ \) \
    -print -quit | /usr/bin/grep -q .; then
    printf 'error: checkout root contains a forbidden Python bytecode cache\n' >&2
    return 1
  fi
}

validate_checkout_tool_surface
if [[ "$job" != "verifier" && "$job" != "preflight" ]]; then
  validate_runtime_git_destination
fi

# Machine identity is public-safe and intentionally generic: do not use a
# hostname, user name, serial number, or other private machine identifier.
export DEGEN_DOGS_RUNNER_ID="${DEGEN_DOGS_RUNNER_ID:-windows-wsl}"

require_production_rpc_quorum() {
  "$python_bin" -I -B - <<'PY'
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

scrub_fixed_worker_authority() {
  local name
  while IFS= read -r name; do
    case "$name" in
      DEGEN_DOGS_*_OUTCOME|DEGEN_DOGS_*_RESULT|DEGEN_DOGS_*_ERROR|\
      DEGEN_DOGS_*_COMMAND|DEGEN_DOGS_*_URL|DEGEN_DOGS_*_URLS|DEGEN_DOGS_*_PATH|\
      MISSION3_*_OUTCOME|MISSION3_*_RESULT|MISSION3_*_ERROR|\
      MISSION3_*_COMMAND|MISSION3_*_URL|MISSION3_*_URLS|MISSION3_*_PATH|\
      DEGEN_DOGS_REPO_DIR|DEGEN_DOGS_LOG_DIR|DEGEN_DOGS_LOCK_DIR|DEGEN_DOGS_ENV_FILE)
        unset "$name"
        ;;
    esac
  done < <(compgen -e)

  export DEGEN_DOGS_REPO_DIR="$repo_dir"
  export DEGEN_DOGS_LOG_DIR="$log_dir"
  export DEGEN_DOGS_LOCK_DIR="$lock_dir"
  export DEGEN_DOGS_ENV_FILE="$env_file"
  export DEGEN_DOGS_REFRESH_LOCK_PATH="${lock_dir}/refresh.lock"
  export MISSION3_REFRESH_LOCK_PATH="$DEGEN_DOGS_REFRESH_LOCK_PATH"
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
    export MISSION3_WATCHER_PUBLICATION_MODE=queue
    exec_python_entrypoint "${repo_dir}/scripts/watch_mission3_onchain_activity.py" --once
    ;;
  publisher)
    require_production_rpc_quorum
    # Re-pin every path captured before .env.local was loaded. The queue and
    # recovery journal may select only a validated generation/digest; neither
    # may select a command or filesystem path.
    scrub_fixed_worker_authority
    exec_python_entrypoint "${repo_dir}/scripts/drain_publication_queue.py"
    ;;
  verifier)
    # The detached verifier receives no command, URL, outcome, or filesystem
    # authority from the environment and never validates mutable Git state.
    scrub_fixed_worker_authority
    exec_python_entrypoint "${repo_dir}/scripts/verify_pages_deployment.py"
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
    export MISSION3_PUBLICATION_MODE=queue
    exec_python_entrypoint "${repo_dir}/scripts/check_wsl_runner_health.py"
    ;;
  preflight)
    require_production_rpc_quorum
    export MISSION3_WATCHER_AUTO_PUSH=0
    export MISSION3_REFRESH_COMMAND='npm run refresh:current'
    export MISSION3_WATCHER_REQUIRE_CLEAN_TREE=1
    export MISSION3_WATCHER_LOG_PATH=-
    export MISSION3_WATCHER_LOCK_PATH="${lock_dir}/watcher-preflight.lock"
    export MISSION3_WATCHER_STATE_PATH="${lock_dir}/watcher-preflight-state.json"
    export DEGEN_DOGS_SKIP_PUSH=1
    "$python_bin" -I -B -c \
      'import os,runpy,sys; p=sys.argv.pop(1); sys.path.insert(0, os.path.dirname(p)); runpy.run_path(p, run_name="__main__")' \
      "${repo_dir}/scripts/preflight_wsl_rpc.py"
    exec_python_entrypoint "${repo_dir}/scripts/watch_mission3_onchain_activity.py" --once --dry-run
    ;;
esac
