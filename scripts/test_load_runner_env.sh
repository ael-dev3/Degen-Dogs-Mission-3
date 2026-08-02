#!/usr/bin/env bash
set -Eeuo pipefail
trap 'status=$?; printf "runner env regression failed at line %s (status %s)\n" "$LINENO" "$status" >&2; exit "$status"' ERR

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d)"
cleanup() {
  rm -rf -- "$TEST_DIR"
}
trap cleanup EXIT

ENV_FILE="${TEST_DIR}/runner.env"
SENTINEL="${TEST_DIR}/must-not-exist"
BACKTICK_SENTINEL="${TEST_DIR}/backtick-must-not-exist"
printf '%s\n' \
  'BASE_RPC_URLS=https://one.example,https://two.example' \
  'MISSION3_WATCHER_AUTO_PUSH=0' \
  'MISSION3_REFRESH_COMMAND="npm run refresh:current"' \
  "DEGEN_DOGS_LITERAL_COMMAND=\$(touch ${SENTINEL})" \
  "DEGEN_DOGS_LITERAL_BACKTICKS=\`touch ${BACKTICK_SENTINEL}\`" >"$ENV_FILE"
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
[[ "$DEGEN_DOGS_LITERAL_COMMAND" == "\$(touch ${SENTINEL})" ]]
[[ "$DEGEN_DOGS_LITERAL_BACKTICKS" == "\`touch ${BACKTICK_SENTINEL}\`" ]]
[[ ! -e "$SENTINEL" ]]
[[ ! -e "$BACKTICK_SENTINEL" ]]
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
# Only the two data workers may inherit the common provider/API configuration.
# The independent watchdog deliberately reloads workers through their installers
# and must never receive provider credentials in its own launchd environment.
for worker_installer in \
  "${ROOT}/scripts/install_hourly_refresh_launchd.sh" \
  "${ROOT}/scripts/install_auction_watcher_launchd.sh"; do
  grep -q 'DEGEN_DOGS_RUNNER_COMMON_ENV_ALLOWLIST' "$worker_installer"
done
if grep -q 'DEGEN_DOGS_RUNNER_COMMON_ENV_ALLOWLIST' \
  "${ROOT}/scripts/install_runner_health_launchd.sh"; then
  printf '%s\n' 'health watchdog inherited the common credential allowlist' >&2
  exit 1
fi
grep -q 'The watchdog must not inherit provider/API credentials' \
  "${ROOT}/scripts/install_runner_health_launchd.sh"

chmod 644 "$ENV_FILE"
if (
  unset BASE_RPC_URLS
  DEGEN_DOGS_ENV_FILE="$ENV_FILE"
  degen_dogs_load_runner_env "$ROOT"
) >/dev/null 2>&1; then
  printf '%s\n' 'insecure runner environment file was accepted' >&2
  exit 1
fi

MALFORMED_FILE="${TEST_DIR}/malformed.env"
printf '%s\n' 'BASE_RPC_URLS=https://one.example' 'this is not an assignment' >"$MALFORMED_FILE"
chmod 600 "$MALFORMED_FILE"
if (
  unset BASE_RPC_URLS
  DEGEN_DOGS_ENV_FILE="$MALFORMED_FILE"
  degen_dogs_load_runner_env "$ROOT"
) >/dev/null 2>&1; then
  printf '%s\n' 'malformed runner environment file was accepted' >&2
  exit 1
fi

DUPLICATE_FILE="${TEST_DIR}/duplicate.env"
printf '%s\n' 'BASE_RPC_URLS=https://one.example' 'BASE_RPC_URLS=https://two.example' >"$DUPLICATE_FILE"
chmod 600 "$DUPLICATE_FILE"
if (
  unset BASE_RPC_URLS
  DEGEN_DOGS_ENV_FILE="$DUPLICATE_FILE"
  degen_dogs_load_runner_env "$ROOT"
) >/dev/null 2>&1; then
  printf '%s\n' 'duplicate runner environment key was accepted' >&2
  exit 1
fi

INJECTION_FILE="${TEST_DIR}/environment-injection.env"
printf '%s\n' 'BASH_ENV=/tmp/attacker-controlled-shell-code' >"$INJECTION_FILE"
chmod 600 "$INJECTION_FILE"
if (
  DEGEN_DOGS_ENV_FILE="$INJECTION_FILE"
  degen_dogs_load_runner_env "$ROOT"
) >/dev/null 2>&1; then
  printf '%s\n' 'unsupported process-control environment key was accepted' >&2
  exit 1
fi

printf '%s\n' 'runner_env_tests=pass count=8'
