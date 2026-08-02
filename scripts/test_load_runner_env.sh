#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d)"
cleanup() {
  rm -rf -- "$TEST_DIR"
}
trap cleanup EXIT

ENV_FILE="${TEST_DIR}/runner.env"
printf '%s\n' \
  'BASE_RPC_URLS=https://one.example,https://two.example' \
  'MISSION3_WATCHER_AUTO_PUSH=0' \
  'MISSION3_REFRESH_COMMAND="npm run refresh:current"' >"$ENV_FILE"
chmod 600 "$ENV_FILE"

# shellcheck source=load_runner_env.sh
source "${ROOT}/scripts/load_runner_env.sh"
DEGEN_DOGS_ENV_FILE="$ENV_FILE"
export MISSION3_WATCHER_AUTO_PUSH=1
export MISSION3_REFRESH_COMMAND="npm run refresh:publish"
degen_dogs_load_runner_env "$ROOT"
[[ "$BASE_RPC_URLS" == "https://one.example,https://two.example" ]]
python3 -c 'import os; assert os.environ["BASE_RPC_URLS"].endswith("two.example")'
[[ "$DEGEN_DOGS_ENV_FILE" == "$ENV_FILE" ]]
[[ "$MISSION3_WATCHER_AUTO_PUSH" == "1" ]]
[[ "$MISSION3_REFRESH_COMMAND" == "npm run refresh:publish" ]]
degen_dogs_export_runner_env_allowlist
for required in BASE_RPC_BATCH_LIMIT DOG_METADATA_WORKERS MISSION3_LOG_CACHE MISSION3_CURRENT_SURFACE_OVERLAP NEYNAR_API_KEY COINGECKO_API_KEY; do
  [[ " ${DEGEN_DOGS_RUNNER_COMMON_ENV_ALLOWLIST//$'\n'/ } " == *" ${required} "* ]]
done
for required in MISSION3_FROM_BLOCK MISSION3_ARCHIVE_DB; do
  [[ " ${DEGEN_DOGS_RUNNER_ARCHIVE_ENV_ALLOWLIST//$'\n'/ } " == *" ${required} "* ]]
done
for required in MISSION3_WATCHER_TELEMETRY_PATH MISSION3_WATCHER_AUTO_PUSH; do
  [[ " ${DEGEN_DOGS_RUNNER_WATCHER_ENV_ALLOWLIST//$'\n'/ } " == *" ${required} "* ]]
done
[[ " ${DEGEN_DOGS_RUNNER_HEALTH_ENV_ALLOWLIST//$'\n'/ } " == *" DEGEN_DOGS_HEALTH_GITHUB_ALERTS "* ]]
[[ "$(grep -l 'DEGEN_DOGS_RUNNER_COMMON_ENV_ALLOWLIST' \
  "${ROOT}/scripts/install_hourly_refresh_launchd.sh" \
  "${ROOT}/scripts/install_auction_watcher_launchd.sh" \
  "${ROOT}/scripts/install_runner_health_launchd.sh" | wc -l | tr -d ' ')" == "3" ]]

chmod 644 "$ENV_FILE"
if (
  unset BASE_RPC_URLS
  DEGEN_DOGS_ENV_FILE="$ENV_FILE"
  degen_dogs_load_runner_env "$ROOT"
) >/dev/null 2>&1; then
  printf '%s\n' 'insecure runner environment file was accepted' >&2
  exit 1
fi

printf '%s\n' 'runner_env_tests=pass count=4'
