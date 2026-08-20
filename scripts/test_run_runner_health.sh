#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
LAUNCHER_REPO="${TEST_DIR}/launcher"
CONFIGURED_REPO="${TEST_DIR}/configured-repo"
mkdir -p \
  "${LAUNCHER_REPO}/scripts" \
  "${CONFIGURED_REPO}/scripts/runtime-bin" \
  "${CONFIGURED_REPO}/.venv/bin"
cp "${ROOT}/scripts/run_runner_health.sh" "${LAUNCHER_REPO}/scripts/run_runner_health.sh"
cp "${ROOT}/scripts/load_runner_env.sh" "${LAUNCHER_REPO}/scripts/load_runner_env.sh"
cp "${ROOT}/scripts/runner_permissions.sh" "${LAUNCHER_REPO}/scripts/runner_permissions.sh"
: > "${CONFIGURED_REPO}/scripts/degen_dogs_runner_health.py"

cat > "${CONFIGURED_REPO}/scripts/runtime-bin/python3" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "${MISSION3_WATCHER_AUTO_PUSH:-}" == "1" ]]
[[ "${MISSION3_WATCHER_REQUIRE_CLEAN_TREE:-}" == "1" ]]
[[ "${MISSION3_WATCHER_REFRESH_TIMEOUT_SECONDS:-}" == "120" ]]
[[ "${MISSION3_REFRESH_COMMAND:-}" == "npm run refresh:publish" ]]
[[ "${DEGEN_DOGS_HEALTH_DRY_RUN:-}" == "1" ]]
[[ "${DEGEN_DOGS_HEALTH_GITHUB_ALERTS:-}" == "0" ]]
[[ "${DEGEN_DOGS_HEALTH_REFRESH_RETRY_STATE_PATH:-}" == "relative-retry-state.json" ]]
[[ "${DEGEN_DOGS_HEALTH_REFRESH_RETRY_BASE_SECONDS:-}" == "600" ]]
[[ "${DEGEN_DOGS_HEALTH_REFRESH_RETRY_MAX_SECONDS:-}" == "2400" ]]
[[ "${DEGEN_DOGS_REPO_DIR:-}" == */configured-repo ]]
[[ "${DEGEN_DOGS_ENV_FILE:-}" == */launcher/.env.local ]]
[[ "${DEGEN_DOGS_LOG_DIR:-}" == */configured-repo/relative-logs ]]
[[ "${DEGEN_DOGS_LOCK_DIR:-}" == */configured-repo/relative-locks ]]
[[ "${DEGEN_DOGS_REFRESH_LOCK_PATH:-}" == */configured-repo/degen-lock/refresh.lock ]]
[[ "${MISSION3_REFRESH_LOCK_PATH:-}" == "${DEGEN_DOGS_REFRESH_LOCK_PATH}" ]]
[[ "$PWD" == "${DEGEN_DOGS_REPO_DIR}" ]]
[[ "${1:-}" == */configured-repo/scripts/degen_dogs_runner_health.py ]]
[[ "${2:-}" == "--probe-argument" ]]
for forbidden in \
  BASE_RPC_URL BASE_RPC_URLS BASE_LOG_RPC_URLS NEYNAR_API_KEY COINGECKO_API_KEY \
  DUNE_API_KEY WOOF_USD_PRICE SUP_USD_PRICE DEGEN_DOGS_REMOTE GH_TOKEN \
  GITHUB_TOKEN GIT_ASKPASS SSH_AUTH_SOCK PYTHONPATH PYTHONHOME NODE_OPTIONS \
  RUNNER_HEALTH_TEST_MARKER; do
  [[ -z "$(declare -p "$forbidden" 2>/dev/null || true)" ]]
done
printf '%s\n' 'manual health environment probe passed'
SH
cat > "${CONFIGURED_REPO}/.venv/bin/python3" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
[[ "${1:-}" == "-I" ]]
[[ "${2:-}" == "-c" ]]
for forbidden in \
  BASE_RPC_URL BASE_RPC_URLS BASE_LOG_RPC_URLS NEYNAR_API_KEY COINGECKO_API_KEY \
  DUNE_API_KEY WOOF_USD_PRICE SUP_USD_PRICE DEGEN_DOGS_REMOTE GH_TOKEN \
  GITHUB_TOKEN GIT_ASKPASS SSH_AUTH_SOCK PYTHONPATH PYTHONHOME NODE_OPTIONS \
  RUNNER_HEALTH_TEST_MARKER; do
  [[ -z "$(declare -p "$forbidden" 2>/dev/null || true)" ]]
done
SH
chmod 700 \
  "${CONFIGURED_REPO}/.venv/bin/python3" \
  "${CONFIGURED_REPO}/scripts/runtime-bin/python3" \
  "${LAUNCHER_REPO}/scripts/run_runner_health.sh"

cat > "${LAUNCHER_REPO}/.env.local" <<ENV
DEGEN_DOGS_REPO_DIR=${CONFIGURED_REPO}
DEGEN_DOGS_LOG_DIR=relative-logs
DEGEN_DOGS_LOCK_DIR=relative-locks
DEGEN_DOGS_REFRESH_LOCK_PATH=degen-lock/refresh.lock
MISSION3_REFRESH_LOCK_PATH=mission-lock/ignored.lock
BASE_RPC_URL=https://rpc-secret.example/key
BASE_RPC_URLS=https://rpc-secret.example/key,https://second-secret.example/key
BASE_LOG_RPC_URLS=https://logs-secret.example/key
NEYNAR_API_KEY=neynar-secret
COINGECKO_API_KEY=coingecko-secret
DUNE_API_KEY=dune-secret
WOOF_USD_PRICE=secret-price
SUP_USD_PRICE=secret-price
DEGEN_DOGS_REMOTE=https://secret-user:secret-pass@example.invalid/repo.git
MISSION3_WATCHER_AUTO_PUSH=1
MISSION3_WATCHER_REQUIRE_CLEAN_TREE=1
MISSION3_WATCHER_REFRESH_TIMEOUT_SECONDS=120
MISSION3_REFRESH_COMMAND=npm run refresh:publish
DEGEN_DOGS_HEALTH_GITHUB_ALERTS=0
DEGEN_DOGS_HEALTH_REFRESH_RETRY_STATE_PATH=relative-retry-state.json
DEGEN_DOGS_HEALTH_REFRESH_RETRY_BASE_SECONDS=600
DEGEN_DOGS_HEALTH_REFRESH_RETRY_MAX_SECONDS=2400
ENV
chmod 600 "${LAUNCHER_REPO}/.env.local"

mkdir -p "${TEST_DIR}/python-injection"
cat > "${TEST_DIR}/python-injection/sitecustomize.py" <<'PY'
import os
from pathlib import Path
Path(os.environ["RUNNER_HEALTH_TEST_MARKER"]).touch()
PY

mkdir -p "${TEST_DIR}/hostile-bin"
cat > "${TEST_DIR}/hostile-bin/bash" <<'SH'
#!/bin/sh
: > "${RUNNER_HEALTH_HOSTILE_PATH_MARKER}"
exit 97
SH
chmod 700 "${TEST_DIR}/hostile-bin/bash"
cat > "${TEST_DIR}/hostile-bash-env.sh" <<'SH'
: > "${RUNNER_HEALTH_BASH_ENV_MARKER}"
trap 'if [[ -n "${BASE_RPC_URLS:-}" ]]; then printf "%s\n" "$BASE_RPC_URLS" >>"$RUNNER_HEALTH_BASH_ENV_MARKER"; fi' DEBUG
SH

grep -Fq '"runner:health": "DEGEN_DOGS_HEALTH_CLEAN_BOOTSTRAP=0 /bin/bash -p scripts/run_runner_health.sh"' \
  "${ROOT}/package.json"
grep -Fq '"runner:health:dry": "DEGEN_DOGS_HEALTH_CLEAN_BOOTSTRAP=0 DEGEN_DOGS_HEALTH_DRY_RUN=1 DEGEN_DOGS_HEALTH_ALERT_DRY_RUN=1 /bin/bash -p scripts/run_runner_health.sh"' \
  "${ROOT}/package.json"

PROBE_STDERR="${TEST_DIR}/probe.stderr"
probe_output="$(
  /usr/bin/env \
    BASH_ENV="${TEST_DIR}/hostile-bash-env.sh" \
    SHELLOPTS=xtrace \
    PATH="${TEST_DIR}/hostile-bin:/usr/bin:/bin" \
    DEGEN_DOGS_HEALTH_CLEAN_BOOTSTRAP=0 \
    DEGEN_DOGS_HEALTH_DRY_RUN=1 \
    GH_TOKEN=github-secret \
    GITHUB_TOKEN=github-secret \
    GIT_ASKPASS="${TEST_DIR}/askpass" \
    SSH_AUTH_SOCK="${TEST_DIR}/ssh-agent" \
    PYTHONPATH="${TEST_DIR}/python-injection" \
    PYTHONHOME="${TEST_DIR}/python-home" \
    NODE_OPTIONS='--require=/secret/module.js' \
    RUNNER_HEALTH_TEST_MARKER="${TEST_DIR}/sitecustomize-executed" \
    RUNNER_HEALTH_BASH_ENV_MARKER="${TEST_DIR}/bash-env-executed" \
    RUNNER_HEALTH_HOSTILE_PATH_MARKER="${TEST_DIR}/hostile-bash-executed" \
    /bin/bash -p "${LAUNCHER_REPO}/scripts/run_runner_health.sh" --probe-argument \
    2>"$PROBE_STDERR"
)"
[[ "$probe_output" == "manual health environment probe passed" ]]
[[ ! -e "${TEST_DIR}/sitecustomize-executed" ]]
[[ ! -e "${TEST_DIR}/bash-env-executed" ]]
[[ ! -e "${TEST_DIR}/hostile-bash-executed" ]]
for secret in rpc-secret neynar-secret coingecko-secret dune-secret secret-user secret-pass; do
  ! grep -Fq "$secret" "$PROBE_STDERR"
done

cat > "${LAUNCHER_REPO}/.env.local" <<ENV
DEGEN_DOGS_REPO_DIR=${CONFIGURED_REPO}
MISSION3_WATCHER_AUTO_PUSH=1
MISSION3_WATCHER_REQUIRE_CLEAN_TREE=0
MISSION3_REFRESH_COMMAND=npm run refresh:publish
ENV
chmod 600 "${LAUNCHER_REPO}/.env.local"
if DEGEN_DOGS_HEALTH_CLEAN_BOOTSTRAP=0 /bin/bash -p \
  "${LAUNCHER_REPO}/scripts/run_runner_health.sh" >/dev/null 2>&1; then
  printf '%s\n' 'auto-push accepted a disabled clean-tree safety gate' >&2
  exit 1
fi

cat > "${LAUNCHER_REPO}/.env.local" <<ENV
DEGEN_DOGS_REPO_DIR=${CONFIGURED_REPO}
MISSION3_WATCHER_AUTO_PUSH=1
MISSION3_REFRESH_COMMAND=npm run refresh:publish; touch ${TEST_DIR}/executed
ENV
chmod 600 "${LAUNCHER_REPO}/.env.local"
if DEGEN_DOGS_HEALTH_CLEAN_BOOTSTRAP=0 /bin/bash -p \
  "${LAUNCHER_REPO}/scripts/run_runner_health.sh" >/dev/null 2>&1; then
  printf '%s\n' 'unsafe refresh command unexpectedly passed validation' >&2
  exit 1
fi
[[ ! -e "${TEST_DIR}/executed" ]]

printf '%s\n' 'manual runner health environment tests passed'
