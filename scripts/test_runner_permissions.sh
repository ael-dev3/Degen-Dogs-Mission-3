#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=runner_permissions.sh
source "${ROOT}/scripts/runner_permissions.sh"

PRIVATE_TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/degen-dogs-permissions.XXXXXX")"
HOLDER_PID=""
cleanup() {
  if [[ -n "$HOLDER_PID" ]]; then
    kill "$HOLDER_PID" >/dev/null 2>&1 || true
    wait "$HOLDER_PID" >/dev/null 2>&1 || true
  fi
  rm -rf "$PRIVATE_TEST_ROOT"
}
trap cleanup EXIT

file_mode() {
  local mode
  if mode="$(stat -f '%Lp' "$1" 2>/dev/null)"; then
    printf '%s\n' "$mode"
  else
    # GNU stat can emit filesystem details to stdout before rejecting BSD's
    # `-f FORMAT` syntax, so never combine the two probes with a bare `||`.
    stat -c '%a' "$1"
  fi
}

private_dir="${PRIVATE_TEST_ROOT}/private"
mkdir "$private_dir"
chmod 755 "$private_dir"
degen_dogs_private_dir "$private_dir"
[[ "$(file_mode "$private_dir")" == "700" ]]

private_file="${private_dir}/runner.log"
printf 'keep-me\n' >"$private_file"
chmod 644 "$private_file"
degen_dogs_private_file "$private_file"
[[ "$(file_mode "$private_file")" == "600" ]]
[[ "$(<"$private_file")" == "keep-me" ]]

new_file="${private_dir}/new.jsonl"
degen_dogs_private_file "$new_file"
[[ -f "$new_file" ]]
[[ "$(file_mode "$new_file")" == "600" ]]

absent_file="${private_dir}/absent.json"
degen_dogs_private_file "$absent_file" 0
[[ ! -e "$absent_file" ]]

target="${PRIVATE_TEST_ROOT}/target"
printf 'sensitive\n' >"$target"
chmod 644 "$target"
link="${private_dir}/linked.log"
ln -s "$target" "$link"
if degen_dogs_private_file "$link" 0 2>/dev/null; then
  echo "expected symlink private file to be rejected" >&2
  exit 1
fi
[[ "$(file_mode "$target")" == "644" ]]

# A link in any ancestor must be rejected before mkdir/chmod/open can affect its
# target. Exercise both an existing file and a not-yet-created file so the
# validation covers hardening and creation paths.
nested_dir_parent="${PRIVATE_TEST_ROOT}/nested-dir-parent"
nested_dir_target="${PRIVATE_TEST_ROOT}/nested-dir-target"
mkdir "$nested_dir_parent" "$nested_dir_target"
chmod 755 "$nested_dir_target"
ln -s "$nested_dir_target" "${nested_dir_parent}/redirect"
if degen_dogs_private_dir "${nested_dir_parent}/redirect/created-private" 2>/dev/null; then
  echo "expected nested symlink private directory ancestor to be rejected" >&2
  exit 1
fi
[[ ! -e "${nested_dir_target}/created-private" ]]
[[ "$(file_mode "$nested_dir_target")" == "755" ]]

nested_file_parent="${PRIVATE_TEST_ROOT}/nested-file-parent"
nested_file_target="${PRIVATE_TEST_ROOT}/nested-file-target"
mkdir "$nested_file_parent" "$nested_file_target"
printf 'ancestor-target-content\n' >"${nested_file_target}/existing.log"
chmod 644 "${nested_file_target}/existing.log"
ln -s "$nested_file_target" "${nested_file_parent}/redirect"
if degen_dogs_private_file "${nested_file_parent}/redirect/existing.log" 2>/dev/null; then
  echo "expected nested symlink existing private file ancestor to be rejected" >&2
  exit 1
fi
if degen_dogs_private_file "${nested_file_parent}/redirect/new.log" 2>/dev/null; then
  echo "expected nested symlink new private file ancestor to be rejected" >&2
  exit 1
fi
[[ "$(<"${nested_file_target}/existing.log")" == "ancestor-target-content" ]]
[[ "$(file_mode "${nested_file_target}/existing.log")" == "644" ]]
[[ ! -e "${nested_file_target}/new.log" ]]

[[ "$(degen_dogs_resolve_runner_path /srv/degen .local/state.json)" == "/srv/degen/.local/state.json" ]]
[[ "$(degen_dogs_resolve_runner_path /srv/degen /var/tmp/state.json)" == "/var/tmp/state.json" ]]
[[ "$(degen_dogs_resolve_runner_path /srv/degen '~/state.json')" == "${HOME}/state.json" ]]

for installer in \
  install_hourly_refresh_launchd.sh \
  install_auction_watcher_launchd.sh \
  install_runner_health_launchd.sh; do
  script="${ROOT}/scripts/${installer}"
  bash -n "$script"
  grep -q '^umask 077$' "$script"
  grep -q '"Umask": 0o077' "$script"
  grep -q 'runner_permissions.sh' "$script"
  grep -q 'DEGEN_DOGS_INSTALL_ALLOW_RUNNING_RESTART' "$script"
  lock_line="$(grep -n 'degen_dogs_acquire_installer_lock' "$script" | head -1 | cut -d: -f1)"
  running_line="$(grep -n '^existing_job=' "$script" | head -1 | cut -d: -f1)"
  lint_line="$(grep -n 'plutil -lint "\$PLIST_CANDIDATE_PATH"' "$script" | head -1 | cut -d: -f1)"
  transaction_line="$(grep -n '^degen_dogs_install_launchd_transaction ' "$script" | head -1 | cut -d: -f1)"
  [[ "$lock_line" -lt "$running_line" ]]
  [[ "$running_line" -lt "$lint_line" ]]
  [[ "$lint_line" -lt "$transaction_line" ]]
  if grep -q '^launchctl \(bootout\|bootstrap\|enable\) ' "$script"; then
    echo "installer bypasses the shared transactional launchd helper: ${installer}" >&2
    exit 1
  fi
  if grep -q '^mkdir -p ' "$script"; then
    echo "installer uses recursive path creation outside the secure descriptor walker: ${installer}" >&2
    exit 1
  fi
done

grep -Fq 'FULL_REFRESH="${DEGEN_DOGS_FULL_REFRESH:-0}"' "${ROOT}/scripts/install_hourly_refresh_launchd.sh"
grep -Fq 'RUN_MISSION3_ARCHIVE="${DEGEN_DOGS_RUN_MISSION3_ARCHIVE:-1}"' "${ROOT}/scripts/install_hourly_refresh_launchd.sh"
grep -Fq 'plist["EnvironmentVariables"]["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"] = os.environ["RUN_MISSION3_ARCHIVE"]' "${ROOT}/scripts/install_hourly_refresh_launchd.sh"
grep -Fq 'env["DEGEN_DOGS_FULL_REFRESH"] = "0"' "${ROOT}/scripts/install_auction_watcher_launchd.sh"
grep -Fq 'env["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"] = "0"' "${ROOT}/scripts/install_auction_watcher_launchd.sh"
grep -Fq 'degen_dogs_validate_watcher_refresh_command "$MISSION3_REFRESH_COMMAND" "$MISSION3_WATCHER_AUTO_PUSH"' "${ROOT}/scripts/install_auction_watcher_launchd.sh"
grep -Fq 'MISSION3_WATCHER_REQUIRE_CLEAN_TREE must be 1 when auto-push is enabled' "${ROOT}/scripts/install_auction_watcher_launchd.sh"
grep -Fq 'MISSION3_WATCHER_REFRESH_TIMEOUT_SECONDS must be at least 60' "${ROOT}/scripts/install_auction_watcher_launchd.sh"
grep -Fq 'degen_dogs_validate_watcher_refresh_command "$MISSION3_REFRESH_COMMAND" "$WATCHER_AUTO_PUSH"' "${ROOT}/scripts/install_runner_health_launchd.sh"
grep -Fq 'MISSION3_WATCHER_REQUIRE_CLEAN_TREE must be 1 when auto-push is enabled' "${ROOT}/scripts/install_runner_health_launchd.sh"
grep -Fq '"MISSION3_WATCHER_REQUIRE_CLEAN_TREE": os.environ["WATCHER_REQUIRE_CLEAN_TREE"]' "${ROOT}/scripts/install_runner_health_launchd.sh"
grep -Fq '"MISSION3_WATCHER_REFRESH_TIMEOUT_SECONDS": os.environ["WATCHER_REFRESH_TIMEOUT_SECONDS"]' "${ROOT}/scripts/install_runner_health_launchd.sh"
grep -Fq '"DEGEN_DOGS_FULL_REFRESH": os.environ["HOURLY_FULL_REFRESH"]' "${ROOT}/scripts/install_runner_health_launchd.sh"
grep -Fq '"DEGEN_DOGS_RUN_MISSION3_ARCHIVE": os.environ["HOURLY_RUN_MISSION3_ARCHIVE"]' "${ROOT}/scripts/install_runner_health_launchd.sh"
grep -Fq 'MISSION3_WATCHER_REQUIRE_CLEAN_TREE=1' "${ROOT}/.env.example"

if grep -q 'DEGEN_DOGS_RUNNER_COMMON_ENV_ALLOWLIST' "${ROOT}/scripts/install_runner_health_launchd.sh"; then
  echo "health installer still imports the credential-bearing common environment allowlist" >&2
  exit 1
fi
for secret_key in BASE_RPC_URL BASE_RPC_URLS BASE_LOG_RPC_URLS NEYNAR_API_KEY COINGECKO_API_KEY DUNE_API_KEY; do
  if grep -q "$secret_key" "${ROOT}/scripts/install_runner_health_launchd.sh"; then
    echo "health installer leaks credential-bearing ${secret_key} into the watchdog plist" >&2
    exit 1
  fi
done

installer_fixture="${PRIVATE_TEST_ROOT}/installer-fixture.sh"
installer_lock="${PRIVATE_TEST_ROOT}/installer-locks/refresh.lock"
installer_marker="${PRIVATE_TEST_ROOT}/installer-ran"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -Eeuo pipefail' \
  'source "$1"' \
  'lock_path="$2"' \
  'marker="$3"' \
  'degen_dogs_acquire_installer_lock "$lock_path" "$0" "$@"' \
  'printf "ran\\n" >"$marker"' \
  'degen_dogs_release_installer_lock' >"$installer_fixture"
chmod +x "$installer_fixture"

lock_attack_parent="${PRIVATE_TEST_ROOT}/lock-attack-parent"
lock_attack_target="${PRIVATE_TEST_ROOT}/lock-attack-target"
lock_attack_marker="${PRIVATE_TEST_ROOT}/lock-attack-ran"
mkdir "$lock_attack_parent" "$lock_attack_target"
chmod 755 "$lock_attack_target"
ln -s "$lock_attack_target" "${lock_attack_parent}/redirect"
if "$installer_fixture" "${ROOT}/scripts/runner_permissions.sh" \
  "${lock_attack_parent}/redirect/locks/refresh.lock" "$lock_attack_marker" 2>/dev/null; then
  echo "expected nested symlink installer lock ancestor to be rejected" >&2
  exit 1
fi
[[ ! -e "$lock_attack_marker" ]]
[[ ! -e "${lock_attack_target}/locks" ]]
[[ "$(file_mode "$lock_attack_target")" == "755" ]]

"$installer_fixture" "${ROOT}/scripts/runner_permissions.sh" "$installer_lock" "$installer_marker"
[[ "$(<"$installer_marker")" == "ran" ]]

rm -f "$installer_marker"
installer_ready="${PRIVATE_TEST_ROOT}/installer-holder-ready"
python3 - "$installer_lock" "$installer_ready" <<'PY' &
import fcntl
import os
import sys
import time
from pathlib import Path

fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
Path(sys.argv[2]).write_text("ready\n", encoding="utf-8")
time.sleep(30)
PY
HOLDER_PID=$!
for _ in {1..100}; do
  [[ -f "$installer_ready" ]] && break
  sleep 0.01
done
[[ -f "$installer_ready" ]]
if "$installer_fixture" "${ROOT}/scripts/runner_permissions.sh" "$installer_lock" "$installer_marker" 2>/dev/null; then
  echo "installer bypassed a contended shared refresh lock" >&2
  exit 1
fi
[[ ! -e "$installer_marker" ]]
kill "$HOLDER_PID" >/dev/null 2>&1 || true
wait "$HOLDER_PID" >/dev/null 2>&1 || true
HOLDER_PID=""

# Candidate bootstrap, enable, and print failures must all restore both the
# protected prior plist and its loaded job before the shared lock is released.
mock_bin="${PRIVATE_TEST_ROOT}/mock-bin"
mkdir "$mock_bin"
mock_launchctl_log="${PRIVATE_TEST_ROOT}/mock-launchctl.log"
mock_job_state="${PRIVATE_TEST_ROOT}/mock-job-state"
mock_enabled_state="${PRIVATE_TEST_ROOT}/mock-enabled-state"
cat >"${mock_bin}/launchctl" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
operation="${1:?operation required}"
shift
printf '%s %s\n' "$operation" "$*" >>"${MOCK_LAUNCHCTL_LOG:?}"
python3 - "${DEGEN_DOGS_INSTALL_LOCK_PATH:?}" "${DEGEN_DOGS_INSTALL_LOCK_FD:?}" <<'PY'
import fcntl
import os
import sys

try:
    os.close(int(sys.argv[2]))
except OSError:
    pass
descriptor = os.open(sys.argv[1], os.O_RDWR)
try:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(0)
    raise SystemExit("transaction launchctl call ran without the shared lock")
finally:
    os.close(descriptor)
PY
case "$operation" in
  bootout)
    rm -f -- "${MOCK_JOB_STATE:?}"
    ;;
  bootstrap)
    [[ -f "${MOCK_ENABLED_STATE:?}" ]] || exit 47
    plist_path="${!#}"
    definition="$(<"$plist_path")"
    if [[ "$definition" == candidate-definition* && "${MOCK_FAIL_OPERATION:-}" == "bootstrap" ]]; then
      exit 42
    fi
    printf '%s\n' "$definition" >"${MOCK_JOB_STATE:?}"
    ;;
  enable)
    enable_attempts="$(grep -c '^enable ' "${MOCK_LAUNCHCTL_LOG:?}" || true)"
    if [[ "$enable_attempts" == "1" && "${MOCK_FAIL_OPERATION:-}" == "enable" ]]; then
      exit 43
    fi
    : >"${MOCK_ENABLED_STATE:?}"
    ;;
  print)
    [[ -f "${MOCK_JOB_STATE:?}" ]] || exit 44
    definition="$(<"${MOCK_JOB_STATE:?}")"
    if [[ "$definition" == candidate-definition* && "${MOCK_FAIL_OPERATION:-}" == "print" ]]; then
      exit 45
    fi
    printf 'state = loaded\n'
    ;;
  *)
    exit 46
    ;;
esac
SH
chmod +x "${mock_bin}/launchctl"

transaction_fixture="${PRIVATE_TEST_ROOT}/transaction-fixture.sh"
cat >"$transaction_fixture" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
helper="$1"
lock_path="$2"
candidate_path="$3"
plist_path="$4"
label="$5"
uid="$6"
source "$helper"
degen_dogs_acquire_installer_lock "$lock_path" "$0" "$@"
degen_dogs_install_launchd_transaction "$candidate_path" "$plist_path" "$label" "$uid"
SH
chmod +x "$transaction_fixture"

transaction_dir="${PRIVATE_TEST_ROOT}/launch-agents"
mkdir "$transaction_dir"
chmod 700 "$transaction_dir"
transaction_plist="${transaction_dir}/com.example.runner.plist"
transaction_lock="${PRIVATE_TEST_ROOT}/transaction-locks/refresh.lock"
export MOCK_LAUNCHCTL_LOG="$mock_launchctl_log" MOCK_JOB_STATE="$mock_job_state" MOCK_ENABLED_STATE="$mock_enabled_state"

for failure in bootstrap enable print; do
  candidate="${transaction_dir}/candidate-${failure}.plist"
  printf 'prior-definition\n' >"$transaction_plist"
  printf 'candidate-definition provider-secret-do-not-log\n' >"$candidate"
  chmod 600 "$transaction_plist" "$candidate"
  printf 'prior-definition\n' >"$mock_job_state"
  rm -f "$mock_enabled_state"
  : >"$mock_launchctl_log"
  export MOCK_FAIL_OPERATION="$failure"
  set +e
  transaction_output="$(PATH="${mock_bin}:$PATH" "$transaction_fixture" \
    "${ROOT}/scripts/runner_permissions.sh" "$transaction_lock" "$candidate" \
    "$transaction_plist" "com.example.runner" "$(id -u)" 2>&1)"
  transaction_status=$?
  set -e
  if [[ "$transaction_status" == "0" ]]; then
    echo "expected mocked candidate ${failure} failure" >&2
    exit 1
  fi
  [[ "$(<"$transaction_plist")" == "prior-definition" ]]
  [[ "$(<"$mock_job_state")" == "prior-definition" ]]
  [[ ! -e "$candidate" ]]
  [[ "$transaction_output" == *"restored and reloaded prior job"* ]]
  if [[ "$transaction_output" == *"provider-secret-do-not-log"* ]]; then
    echo "transaction rollback leaked candidate provider data" >&2
    exit 1
  fi
  expected_bootstraps=2
  [[ "$failure" != "enable" ]] || expected_bootstraps=1
  [[ "$(grep -c '^bootstrap ' "$mock_launchctl_log")" == "$expected_bootstraps" ]]
  [[ "$(grep -c '^enable ' "$mock_launchctl_log")" -ge "1" ]]
  [[ "$(grep -c '^print ' "$mock_launchctl_log")" -ge "1" ]]
  first_enable_line="$(grep -n '^enable ' "$mock_launchctl_log" | head -1 | cut -d: -f1)"
  first_bootstrap_line="$(grep -n '^bootstrap ' "$mock_launchctl_log" | head -1 | cut -d: -f1)"
  [[ "$first_enable_line" -lt "$first_bootstrap_line" ]]
  if compgen -G "${transaction_plist}.previous.*" >/dev/null; then
    echo "transaction left a prior-plist backup behind" >&2
    exit 1
  fi
done
unset MOCK_FAIL_OPERATION

candidate="${transaction_dir}/candidate-success.plist"
printf 'prior-definition\n' >"$transaction_plist"
printf 'candidate-definition\n' >"$candidate"
chmod 600 "$transaction_plist" "$candidate"
printf 'prior-definition\n' >"$mock_job_state"
rm -f "$mock_enabled_state"
: >"$mock_launchctl_log"
PATH="${mock_bin}:$PATH" "$transaction_fixture" \
  "${ROOT}/scripts/runner_permissions.sh" "$transaction_lock" "$candidate" \
  "$transaction_plist" "com.example.runner" "$(id -u)"
[[ "$(<"$transaction_plist")" == "candidate-definition" ]]
[[ "$(<"$mock_job_state")" == "candidate-definition" ]]
[[ ! -e "$candidate" ]]
[[ -f "$mock_enabled_state" ]]
python3 - "$transaction_lock" <<'PY'
import fcntl
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fcntl.flock(descriptor, fcntl.LOCK_UN)
finally:
    os.close(descriptor)
PY

unset MOCK_LAUNCHCTL_LOG MOCK_JOB_STATE MOCK_ENABLED_STATE

bash -n "${ROOT}/scripts/refresh_archive_and_publish.sh"
grep -q '^umask 077$' "${ROOT}/scripts/refresh_archive_and_publish.sh"

echo "runner permission tests passed"
