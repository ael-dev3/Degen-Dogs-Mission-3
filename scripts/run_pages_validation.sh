#!/usr/bin/env bash
set -uo pipefail

# Run every GitHub Pages validation gate, while overlapping only checks that
# operate on independent fixtures or read the generated production artifacts.
# Each group keeps a private log and every exit status is collected before the
# workflow fails, so one early regression cannot hide failures in later gates.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 70
cd "$ROOT" || exit 70

LOG_PARENT="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
if [[ ! -d "$LOG_PARENT" ]]; then
  printf 'pages validation log parent is unavailable: %s\n' "$LOG_PARENT" >&2
  exit 70
fi

LOG_DIR="$(mktemp -d "${LOG_PARENT%/}/degen-dogs-pages-checks.XXXXXX")" || {
  printf 'could not create the pages validation log directory\n' >&2
  exit 70
}

cleanup() {
  local leaf
  leaf="$(basename -- "$LOG_DIR")" || return
  if [[ -d "$LOG_DIR" && "$leaf" == degen-dogs-pages-checks.* ]]; then
    rm -rf -- "$LOG_DIR"
  fi
}
trap cleanup EXIT

run_check() {
  local check="$1"
  case "$check" in
    python-bytecode)
      python3 -m py_compile \
        scripts/build_dashboard.py \
        scripts/build_live_snapshot_bundle.py \
        scripts/refresh_current_surface.py \
        scripts/watch_mission3_auction.py \
        scripts/validate_dashboard_consistency.py \
        scripts/degen_dogs_runner_health.py \
        scripts/check_remote_freshness.py
      ;;
    npm:*)
      npm run "${check#npm:}"
      ;;
    *)
      printf 'unknown pages validation check: %s\n' "$check" >&2
      return 64
      ;;
  esac
}

run_group() {
  local group="$1"
  local check code
  local failed=0
  local checks=()

  case "$group" in
    publisher)
      checks=(
        npm:test:runner-publish
      )
      ;;
    data)
      checks=(
        npm:test:data-cache
        npm:test:watcher
        npm:test:refresh-current
      )
      ;;
    runner)
      checks=(
        npm:test:runner-health
        npm:test:runner-health-env
        npm:test:runner-env
        npm:test:runner-permissions
        npm:test:wsl-runner-assets
      )
      ;;
    validation)
      checks=(
        python-bytecode
        npm:test:unified
        npm:test:archive-prices
        npm:test:archive-usd
        npm:test:historical-checker
        npm:test:validator
        npm:test:refresh-telemetry
        npm:test:live-snapshot
        npm:test:rpc-redaction
        npm:test:freshness
        npm:archive:prices:validate
        npm:check:historical-dogs
        npm:validate:dashboard
        npm:check:dashboard-ui
        npm:test:pages-validation-runner
      )
      ;;
    *)
      printf 'unknown pages validation group: %s\n' "$group" >&2
      return 64
      ;;
  esac

  for check in "${checks[@]}"; do
    printf '\n--- %s ---\n' "$check"
    if run_check "$check"; then
      printf '[PASS] %s\n' "$check"
    else
      code=$?
      failed=1
      printf '[FAIL] %s (exit %d)\n' "$check" "$code" >&2
    fi
  done

  return "$failed"
}

GROUP_IDS=(publisher data runner validation)
GROUP_NAMES=("Publisher safety" "Data and watcher tests" "Runner tests" "Artifact validation")
PIDS=()
LOGS=()
STATUSES=()

for index in "${!GROUP_IDS[@]}"; do
  log_path="$LOG_DIR/group-${index}.log"
  LOGS+=("$log_path")
  run_group "${GROUP_IDS[$index]}" >"$log_path" 2>&1 &
  PIDS+=("$!")
done

overall_status=0
failed_groups=0
for index in "${!PIDS[@]}"; do
  if wait "${PIDS[$index]}"; then
    STATUSES+=(0)
  else
    code=$?
    STATUSES+=("$code")
    overall_status=1
    failed_groups=$((failed_groups + 1))
  fi
done

for index in "${!GROUP_IDS[@]}"; do
  printf '::group::Pages checks: %s\n' "${GROUP_NAMES[$index]}"
  if ! sed -n '1,$p' "${LOGS[$index]}"; then
    printf 'could not read captured log: %s\n' "${LOGS[$index]}" >&2
    overall_status=1
  fi
  if [[ "${STATUSES[$index]}" == "0" ]]; then
    printf '[PASS] %s\n' "${GROUP_NAMES[$index]}"
  else
    printf '[FAIL] %s (group exit %s)\n' "${GROUP_NAMES[$index]}" "${STATUSES[$index]}" >&2
  fi
  printf '::endgroup::\n'
done

if [[ "$overall_status" != "0" ]]; then
  printf 'pages validation failed: %d of %d groups failed\n' \
    "$failed_groups" "${#GROUP_IDS[@]}" >&2
  exit 1
fi

printf 'pages_validation=pass groups=%d\n' "${#GROUP_IDS[@]}"
