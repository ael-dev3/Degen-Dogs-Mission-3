#!/bin/bash
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset BASH_ENV ENV NODE_OPTIONS PYTHONHOME PYTHONPATH

usage() {
  cat <<'EOF'
Usage: /usr/local/libexec/degen-dogs-wsl-installer [options]

Options:
  --repo-dir PATH       WSL ext4 runtime clone (required; bootstrap supplies it)
  --runner-user USER    Dedicated Linux service user (default: degendogs)
  --runner-id ID        Public-safe telemetry label (default: windows-wsl)
  --env-file PATH       Protected runner config (default: REPO/.env.local)
  --expected-head SHA   Exact trusted origin/main commit staged by bootstrap
  --runtime-tree PATH   Root-owned export of EXPECTED_HEAD used as manifest
  --skip-deploy-key     Keep existing credentials only if canonical and valid
  --skip-bootstrap      Reuse the exact valid core-suite/build receipt for activation
  --enable-now          Verify prerequisites and enable units behind activation gate
  --uninstall           Disable/remove services; preserve repo, secrets, logs, and keys
  --help                Show this help

Internal entrypoint: never execute this script from the runner-owned clone.
The elevated Windows bootstrap supplies the frozen trusted bundle, exact
runtime manifest, and startup-task transaction.
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
asset_dir="$(cd "${script_dir}/.." && pwd -P)"
repo_dir=""
runner_user="degendogs"
runner_id="windows-wsl"
env_file=""
expected_head=""
runtime_tree=""
skip_bootstrap=0
skip_deploy_key=0
enable_now=0
uninstall=0

while (( $# )); do
  case "$1" in
    --repo-dir)
      [[ $# -ge 2 ]] || { usage >&2; exit 64; }
      repo_dir="$2"
      shift 2
      ;;
    --runner-user)
      [[ $# -ge 2 ]] || { usage >&2; exit 64; }
      runner_user="$2"
      shift 2
      ;;
    --runner-id)
      [[ $# -ge 2 ]] || { usage >&2; exit 64; }
      runner_id="$2"
      shift 2
      ;;
    --env-file)
      [[ $# -ge 2 ]] || { usage >&2; exit 64; }
      env_file="$2"
      shift 2
      ;;
    --expected-head)
      [[ $# -ge 2 ]] || { usage >&2; exit 64; }
      expected_head="$2"
      shift 2
      ;;
    --runtime-tree)
      [[ $# -ge 2 ]] || { usage >&2; exit 64; }
      runtime_tree="$2"
      shift 2
      ;;
    --skip-bootstrap)
      skip_bootstrap=1
      shift
      ;;
    --skip-deploy-key)
      skip_deploy_key=1
      shift
      ;;
    --enable-now)
      enable_now=1
      shift
      ;;
    --uninstall)
      uninstall=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'error: unknown option: %s\n' "$1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

all_bootstrap_gates=(
  python-venv
  python-pip
  test-npm-ci
  suite:test_build_dashboard.py
  suite:test_build_live_snapshot_bundle.py
  suite:test_refresh_current_surface.py
  suite:test_runner_publication_state.py
  suite:test_publication_coverage_git_hardening.py
  suite:test_watch_mission3_auction.py
  suite:test_drain_publication_queue.py
  suite:test_verify_pages_deployment.py
  suite:test_refresh_and_publish.sh
  suite:test_refresh_telemetry.py
  suite:test_degen_dogs_runner_health.py
  suite:test_wsl_publication_integration.py
  dashboard-build
  bootstrap-runtime-tests
  production-npm-warm
  render:degen-dogs-watcher.service
  render:degen-dogs-hourly.service
  render:degen-dogs-health.service
  render:degen-dogs-publisher.service
  render:degen-dogs-publisher.path
  render:degen-dogs-pages-verifier.service
  render:degen-dogs-pages-verifier.path
  render:degen-dogs-watcher.timer
  render:degen-dogs-hourly.timer
  render:degen-dogs-health.timer
  render:degen-dogs-publisher.timer
  render:degen-dogs-pages-verifier.timer
  render:degen-dogs-runner.target
  render:logrotate
  systemd-analyze
  logrotate
  final-checkout
  final-git-surface
  final-python-runtime
)
required_bootstrap_gates=(
  bootstrap-runtime-tests
  production-npm-warm
  render:degen-dogs-watcher.service
  render:degen-dogs-hourly.service
  render:degen-dogs-health.service
  render:degen-dogs-publisher.service
  render:degen-dogs-publisher.path
  render:degen-dogs-pages-verifier.service
  render:degen-dogs-pages-verifier.path
  render:degen-dogs-watcher.timer
  render:degen-dogs-hourly.timer
  render:degen-dogs-health.timer
  render:degen-dogs-publisher.timer
  render:degen-dogs-pages-verifier.timer
  render:degen-dogs-runner.target
  render:logrotate
  systemd-analyze
  logrotate
  final-checkout
  final-git-surface
  final-python-runtime
)
declare -A completed_bootstrap_gates=()

run_required_gate() {
  local gate_name="$1"
  local expected_gate
  local allowed_gate=0
  local gate_status=0
  local tracked_gate=0
  shift
  for expected_gate in "${all_bootstrap_gates[@]}"; do
    if [[ "$gate_name" == "$expected_gate" ]]; then
      allowed_gate=1
      break
    fi
  done
  if [[ "$allowed_gate" != "1" ]]; then
    fail "bootstrap attempted an unrecognized release gate: ${gate_name}"
    return 1
  fi
  if [[ "${skip_bootstrap:-0}" != "1" ]]; then
    for expected_gate in "${required_bootstrap_gates[@]}"; do
      if [[ "$gate_name" == "$expected_gate" ]]; then
        tracked_gate=1
        break
      fi
    done
    if [[ "$tracked_gate" == "1" && -n "${completed_bootstrap_gates[$gate_name]+present}" ]]; then
      fail "bootstrap release gate ran more than once: ${gate_name}"
      return 1
    fi
  fi
  # Keep the callback in a simple-command context. Wrapping a shell function
  # in `if`, `!`, or `||` disables errexit throughout that function and can
  # let an early nested gate failure be masked by a later successful command.
  "$@"
  gate_status=$?
  if [[ "$gate_status" != "0" ]]; then
    return "$gate_status"
  fi
  if [[ "$tracked_gate" == "1" ]]; then
    completed_bootstrap_gates["$gate_name"]=1
  fi
}

assert_bootstrap_gate_completion() {
  local gate_name
  for gate_name in "${required_bootstrap_gates[@]}"; do
    if [[ -z "${completed_bootstrap_gates[$gate_name]+present}" ]]; then
      fail "bootstrap test receipt is missing required gate: ${gate_name}"
      return 1
    fi
  done
  if (( ${#completed_bootstrap_gates[@]} != ${#required_bootstrap_gates[@]} )); then
    fail "bootstrap test receipt gate accounting is inconsistent"
    return 1
  fi
}

[[ "$(id -u)" == "0" ]] || fail "run this installer as root with sudo"
grep -qi microsoft /proc/sys/kernel/osrelease || fail "this installer is for WSL2"
[[ "$(ps -p 1 -o comm=)" == "systemd" ]] || fail "enable systemd=true in /etc/wsl.conf, then run wsl --shutdown from Windows"

attest_system_tool() {
  local requested="$1"
  local resolved mode
  [[ "$requested" = /* && -e "$requested" ]] || fail "required absolute system tool is missing: ${requested}"
  resolved="$(/usr/bin/readlink -f -- "$requested")"
  [[ "$resolved" == /usr/bin/* || "$resolved" == /usr/sbin/* || \
    "$resolved" == /bin/* || "$resolved" == /sbin/* || \
    "$resolved" == /usr/lib/node_modules/npm/* ]] || \
    fail "system tool resolved outside the trusted system roots: ${requested}"
  [[ -f "$resolved" && ! -L "$resolved" && "$(/usr/bin/stat -c %U "$resolved")" == "root" ]] || \
    fail "system tool is not a root-owned regular file: ${requested}"
  mode="$(/usr/bin/stat -c %a "$resolved")"
  (( (8#$mode & 8#022) == 0 )) || fail "system tool is group/other writable: ${requested}"
  printf '%s\n' "$resolved"
}

system_python_bin="$(attest_system_tool /usr/bin/python3)"
git_bin="$(attest_system_tool /usr/bin/git)"
node_bin="$(attest_system_tool /usr/bin/node)"
npm_cli="$(attest_system_tool /usr/bin/npm)"
env_bin="$(attest_system_tool /usr/bin/env)"
runuser_bin="$(attest_system_tool /usr/sbin/runuser)"
bash_bin="$(attest_system_tool /bin/bash)"
ssh_bin="$(attest_system_tool /usr/bin/ssh)"
ssh_keygen_bin="$(attest_system_tool /usr/bin/ssh-keygen)"

unit_dir="/etc/systemd/system"
activation_unit_names=(
  degen-dogs-runner.target
  degen-dogs-watcher.timer
  degen-dogs-hourly.timer
  degen-dogs-health.timer
  degen-dogs-publisher.path
  degen-dogs-publisher.timer
  degen-dogs-pages-verifier.path
  degen-dogs-pages-verifier.timer
)
service_unit_names=(
  degen-dogs-watcher.service
  degen-dogs-hourly.service
  degen-dogs-health.service
  degen-dogs-publisher.service
  degen-dogs-pages-verifier.service
)
unit_names=(
  "${activation_unit_names[@]}"
  "${service_unit_names[@]}"
)
state_dir="/var/lib/degen-dogs"
npm_user_config="${state_dir}/npm-user.conf"
npm_global_config="${state_dir}/npm-global.conf"
tested_receipt_path="${state_dir}/bootstrap-test-receipt.json"
claimed_receipt_path="${state_dir}/bootstrap-test-receipt.claimed.json"
legacy_tested_sha_path="${state_dir}/tested-main.sha"
bootstrap_receipt_schema_version=2

if [[ "$uninstall" == "1" ]]; then
  rm -f -- /var/lib/degen-dogs/activation-armed /run/degen-dogs/activation-enabled /run/degen-dogs/anchor-ready \
    "$tested_receipt_path" "$claimed_receipt_path" "$legacy_tested_sha_path"
  systemctl disable --now "${activation_unit_names[@]}" >/dev/null 2>&1 || true
  systemctl stop "${service_unit_names[@]}" >/dev/null 2>&1 || true
  for old_unit in "${unit_names[@]}"; do
    systemctl is-active --quiet "$old_unit" && fail "could not stop ${old_unit} during uninstall"
  done
  for name in "${unit_names[@]}"; do
    rm -f -- "${unit_dir}/${name}"
  done
  rm -f -- \
    /etc/logrotate.d/degen-dogs-wsl \
    /usr/local/libexec/degen-dogs-wsl-anchor \
    /usr/local/libexec/degen-dogs-wsl-installer
  systemctl daemon-reload
  systemctl reset-failed >/dev/null 2>&1 || true
  printf 'WSL runner services removed; repo, .env.local, deploy key, logs, and caches were preserved\n'
  exit 0
fi

[[ "$expected_head" =~ ^[0-9a-f]{40}$ ]] || \
  fail "--expected-head with the exact trusted origin/main SHA-1 is required"
[[ "$enable_now" != "1" || "$skip_bootstrap" == "1" ]] || \
  fail "--enable-now requires the separate one-shot --skip-bootstrap receipt claim"
[[ -n "$repo_dir" ]] || \
  fail "--repo-dir is required and must be supplied by the elevated Windows bootstrap"
[[ "$runtime_tree" =~ ^/[A-Za-z0-9._/-]+$ && "$runtime_tree" != /mnt/* && "$runtime_tree" != *%* ]] || \
  fail "--runtime-tree must name a root-owned WSL ext4 export"
runtime_tree="$(readlink -f "$runtime_tree")"
[[ -d "$runtime_tree" && ! -L "$runtime_tree" && "$(stat -c %U "$runtime_tree")" == "root" ]] || \
  fail "runtime manifest tree must be a root-owned non-symlink directory"
runtime_tree_mode="$(stat -c %a "$runtime_tree")"
(( (8#$runtime_tree_mode & 8#022) == 0 && (8#$runtime_tree_mode & 8#005) == 8#005 )) || \
  fail "runtime manifest tree must be non-writable and runner-readable/traversable"
runtime_tree_parent="$(dirname "$runtime_tree")"
[[ -d "$runtime_tree_parent" && ! -L "$runtime_tree_parent" && \
  "$(stat -c %U "$runtime_tree_parent")" == root ]] || \
  fail "runtime manifest parent must be a root-owned real directory"
runtime_tree_parent_mode="$(stat -c %a "$runtime_tree_parent")"
(( (8#$runtime_tree_parent_mode & 8#022) == 0 && (8#$runtime_tree_parent_mode & 8#001) == 8#001 )) || \
  fail "runtime manifest parent must be non-writable and runner-traversable"
[[ "$asset_dir" =~ ^/[A-Za-z0-9._/-]+$ && "$asset_dir" != /mnt/* && "$asset_dir" != *%* ]] || \
  fail "trusted asset path must be a systemd-safe WSL ext4 path"
[[ -d "$asset_dir" && ! -L "$asset_dir" && "$(stat -c %U "$asset_dir")" == "root" ]] || \
  fail "installer assets must be exported into a root-owned non-symlink directory"
asset_mode="$(stat -c %a "$asset_dir")"
(( (8#$asset_mode & 8#022) == 0 )) || fail "trusted asset directory must not be group/other writable"
[[ "$asset_dir" != "$repo_dir" ]] || \
  fail "never execute the root installer from the runner-writable checkout; use the Windows trusted-stage bootstrap"
trusted_root_assets=(
  scripts/install_wsl_runner.sh
  scripts/run_wsl_runner_anchor.sh
  config/wsl-runner.env.template
  config/logrotate/degen-dogs-wsl.in
  config/systemd/degen-dogs-watcher.service.in
  config/systemd/degen-dogs-watcher.timer
  config/systemd/degen-dogs-hourly.service.in
  config/systemd/degen-dogs-hourly.timer
  config/systemd/degen-dogs-health.service.in
  config/systemd/degen-dogs-health.timer
  config/systemd/degen-dogs-runner.target
  config/systemd/degen-dogs-publisher.service.in
  config/systemd/degen-dogs-publisher.path.in
  config/systemd/degen-dogs-publisher.timer
  config/systemd/degen-dogs-pages-verifier.service.in
  config/systemd/degen-dogs-pages-verifier.path.in
  config/systemd/degen-dogs-pages-verifier.timer
  scripts/runner_publication_state.py
  scripts/publication_coverage.py
  scripts/drain_publication_queue.py
  scripts/verify_pages_deployment.py
)
for relative in "${trusted_root_assets[@]}"; do
  trusted_path="${asset_dir}/${relative}"
  [[ -f "$trusted_path" && ! -L "$trusted_path" && "$(stat -c %U "$trusted_path")" == "root" ]] || \
    fail "root-consumed asset is not a root-owned regular file: ${relative}"
  trusted_mode="$(stat -c %a "$trusted_path")"
  (( (8#$trusted_mode & 8#022) == 0 )) || \
    fail "root-consumed asset is group/other writable: ${relative}"
done
trusted_commit_path="${asset_dir}/TRUSTED_COMMIT"
[[ -f "$trusted_commit_path" && ! -L "$trusted_commit_path" && \
  "$(stat -c %U "$trusted_commit_path")" == "root" && \
  "$(stat -c %h "$trusted_commit_path")" == "1" ]] || \
  fail "trusted installer commit metadata is not a root-owned single-link regular file"
trusted_commit_mode="$(stat -c %a "$trusted_commit_path")"
(( (8#$trusted_commit_mode & 8#022) == 0 )) || \
  fail "trusted installer commit metadata is group/other writable"
[[ "$(stat -c %s "$trusted_commit_path")" == "41" ]] || \
  fail "trusted installer commit metadata has an invalid size"
trusted_installer_commit="$(<"$trusted_commit_path")"
[[ "$trusted_installer_commit" =~ ^[0-9a-f]{40}$ ]] || \
  fail "trusted installer commit metadata is malformed"

# Direct Linux upgrades must be as race-safe as the Windows bootstrap. Remove
# the two-phase activation gates first, then synchronously quiesce every old
# timer/worker before inspecting or replacing the runtime and unit files.
rm -f -- /var/lib/degen-dogs/activation-armed /run/degen-dogs/activation-enabled /run/degen-dogs/anchor-ready
systemctl disable --now "${activation_unit_names[@]}" >/dev/null 2>&1 || true
systemctl stop "${service_unit_names[@]}" >/dev/null 2>&1 || true
for old_unit in "${unit_names[@]}"; do
  systemctl is-active --quiet "$old_unit" && fail "could not quiesce ${old_unit} before installation"
done
if [[ "$skip_bootstrap" != "1" ]]; then
  rm -f -- "$tested_receipt_path" "$claimed_receipt_path" "$legacy_tested_sha_path"
fi

[[ "$runner_user" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || fail "invalid runner user"
[[ "$runner_id" =~ ^[a-z0-9][a-z0-9._-]{0,31}$ ]] || fail "runner ID must be a public-safe lowercase label"
[[ "$repo_dir" =~ ^/[A-Za-z0-9._/-]+$ && "$repo_dir" != /mnt/* && "$repo_dir" != *%* ]] || \
  fail "repo path must be an absolute whitespace-free path on the WSL ext4 filesystem"
[[ "$repo_dir" != */../* && "$repo_dir" != */.. ]] || fail "repo path must not contain a parent traversal"
repo_dir="$(readlink -f "$repo_dir")"
[[ -d "${repo_dir}/.git" && -f "${repo_dir}/package.json" ]] || fail "repo path is not a Degen Dogs Git clone"
filesystem_type="$(stat -f -c %T "$repo_dir")"
[[ "$filesystem_type" == "ext2/ext3" ]] || fail "repo must live on WSL ext4, not ${filesystem_type}"
repo_parent="$(dirname "$repo_dir")"
[[ -d "$repo_parent" && ! -L "$repo_parent" && "$(stat -c %U "$repo_parent")" == "root" ]] || \
  fail "runner checkout parent must be a root-owned non-symlink directory"
repo_parent_mode="$(stat -c %a "$repo_parent")"
(( (8#$repo_parent_mode & 8#022) == 0 )) || \
  fail "runner checkout parent must not be group/other writable"

"$system_python_bin" -I -B - "$runtime_tree" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

trusted = Path(sys.argv[1])
checked = {".py", ".sh", ".service", ".timer", ".target", ".in", ".template"}
bad: list[str] = []
for path in trusted.rglob("*"):
    if not path.is_file() or path.is_symlink():
        continue
    relative = path.relative_to(trusted)
    if (
        relative.suffix in checked or relative.as_posix().startswith("scripts/runtime-bin/")
    ) and b"\r\n" in path.read_bytes():
        bad.append(relative.as_posix())
if bad:
    raise SystemExit("CRLF is unsafe for the WSL runner commit: " + ", ".join(bad[:20]))
PY

for command in git node npm npx python3 runuser systemctl systemd-analyze lsof logrotate ssh ssh-keygen; do
  command -v "$command" >/dev/null 2>&1 || fail "required command is missing: ${command}"
done
node_major="$("$node_bin" -p 'process.versions.node.split(`.`)[0]')"
[[ "$node_major" == "22" ]] || fail "Node 22 is required (found $("$node_bin" --version))"

if ! id "$runner_user" >/dev/null 2>&1; then
  useradd --user-group --create-home --shell /bin/bash "$runner_user"
fi
[[ "$(id -u "$runner_user")" != "0" ]] || fail "runner user must never resolve to uid 0"
[[ "$(id -g "$runner_user")" != "0" ]] || fail "runner user primary group must never resolve to gid 0"
[[ "$(id -G "$runner_user")" == "$(id -g "$runner_user")" ]] || \
  fail "runner user must not belong to supplementary groups"
runner_group="$(id -gn "$runner_user")"
runner_home="$(getent passwd "$runner_user" | cut -d: -f6)"
[[ "$runner_home" =~ ^/[A-Za-z0-9._/-]+$ && "$runner_home" != *%* && -d "$runner_home" ]] || \
  fail "runner user has no systemd-safe home directory"

repo_owner="$(stat -c %U "$repo_dir")"
[[ "$repo_owner" == "$runner_user" ]] || fail "${repo_dir} must be owned by ${runner_user}; clone it as that user instead of recursively changing an existing tree"
install -d -o root -g root -m 0755 "$state_dir"
for npm_config in "$npm_user_config" "$npm_global_config"; do
  if [[ ! -e "$npm_config" ]]; then
    /usr/bin/install -o root -g root -m 0444 /dev/null "$npm_config"
  fi
  [[ -f "$npm_config" && ! -L "$npm_config" && "$(stat -c %U "$npm_config")" == root && \
    "$(stat -c %h "$npm_config")" == 1 && "$(stat -c %a "$npm_config")" == 444 && \
    ! -s "$npm_config" ]] || fail "trusted npm configuration is unsafe: $npm_config"
done

migrate_legacy_checkout_venv() {
  local legacy_path="${repo_dir}/.venv"
  local quarantine
  [[ -e "$legacy_path" || -L "$legacy_path" ]] || return 0
  quarantine="$(mktemp -d "${state_dir}/legacy-checkout-venv.XXXXXX")"
  /bin/chmod 0700 "$quarantine"
  /bin/mv -T -- "$legacy_path" "$quarantine/quarantined.venv"
  "$system_python_bin" -I -B - "$repo_dir" "$state_dir" "$quarantine" <<'PY'
import os
import stat
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve(strict=True)
state = Path(sys.argv[2]).resolve(strict=True)
quarantine = Path(sys.argv[3])
details = quarantine.lstat()
if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
    raise SystemExit("legacy runtime quarantine is not a real directory")
if details.st_uid != 0 or stat.S_IMODE(details.st_mode) != 0o700:
    raise SystemExit("legacy runtime quarantine ownership/mode is unsafe")
if quarantine.resolve(strict=True).parent != state:
    raise SystemExit("legacy runtime quarantine escaped the fixed state directory")
if (quarantine / "quarantined.venv").parent.resolve(strict=True) != quarantine.resolve(strict=True):
    raise SystemExit("legacy runtime quarantine target escaped")
for path in (repo, state, quarantine):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
  /bin/rm -rf -- "$quarantine"
  "$system_python_bin" -I -B - "$state_dir" <<'PY'
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

if [[ "$skip_bootstrap" != "1" ]]; then
  migrate_legacy_checkout_venv
fi

runner_git() {
  "$runuser_bin" -u "$runner_user" -- "$env_bin" -i \
    HOME="$runner_home" \
    PATH="/usr/bin:/bin" \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_SYSTEM=/dev/null \
    GIT_TERMINAL_PROMPT=0 \
    "$git_bin" -c core.hooksPath=/dev/null "$@"
}

actual_head="$(runner_git -C "$repo_dir" rev-parse --verify HEAD)"
[[ "$actual_head" == "$expected_head" ]] || \
  fail "runner checkout HEAD does not match the exact trusted origin/main commit"
expected_tree="$(runner_git -C "$repo_dir" rev-parse "${expected_head}^{tree}")"
index_tree="$(runner_git -C "$repo_dir" write-tree)"
[[ "$index_tree" == "$expected_tree" ]] || \
  fail "runner index differs from the exact trusted origin/main tree"

# Git index flags can hide worktree edits from status/diff. Compare every file
# exported from the independently fetched root-owned commit byte-for-byte and
# compare symlink targets/Git's owner-executable classification without trusting
# the runner's index.
validate_runtime_checkout_matches() {
"$system_python_bin" -I -B - "$runtime_tree" "$repo_dir" <<'PY'
from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

trusted = Path(sys.argv[1])
checkout = Path(sys.argv[2])


def digest(path: Path) -> bytes:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.digest()


for source in trusted.rglob("*"):
    relative = source.relative_to(trusted)
    target = checkout / relative
    source_details = source.lstat()
    try:
        target_details = target.lstat()
    except FileNotFoundError as exc:
        raise SystemExit(f"runner checkout is missing trusted path: {relative}") from exc
    if stat.S_ISLNK(source_details.st_mode):
        if not stat.S_ISLNK(target_details.st_mode) or os.readlink(source) != os.readlink(target):
            raise SystemExit(f"runner symlink differs from trusted commit: {relative}")
    elif stat.S_ISDIR(source_details.st_mode):
        if not stat.S_ISDIR(target_details.st_mode) or stat.S_ISLNK(target_details.st_mode):
            raise SystemExit(f"runner path has wrong directory type: {relative}")
    elif stat.S_ISREG(source_details.st_mode):
        if not stat.S_ISREG(target_details.st_mode) or stat.S_ISLNK(target_details.st_mode):
            raise SystemExit(f"runner path has wrong file type: {relative}")
        if digest(source) != digest(target):
            raise SystemExit(f"runner file differs from trusted commit: {relative}")
        if (source_details.st_mode & stat.S_IXUSR) != (target_details.st_mode & stat.S_IXUSR):
            raise SystemExit(f"runner executable mode differs from trusted commit: {relative}")
    else:
        raise SystemExit(f"trusted commit contains unsupported file type: {relative}")
PY
}

validate_trusted_checkout_surface() {
  "$runuser_bin" -u "$runner_user" -- "$env_bin" -i \
    HOME="$runner_home" PATH=/usr/bin:/bin PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
    "$system_python_bin" -I -B - "$repo_dir" "$git_bin" "$runner_home" <<'PY'
# WSL_TRUSTED_CHECKOUT_SURFACE_VALIDATOR
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
git = sys.argv[2]
runner_home = Path(sys.argv[3])

legacy_venv = repo / ".venv"
if os.path.lexists(legacy_venv):
    raise SystemExit("runner checkout must not contain the legacy ignored .venv")

runtime_bin = repo / "scripts" / "runtime-bin"
if not runtime_bin.is_dir() or runtime_bin.is_symlink():
    raise SystemExit("runner runtime-bin must be a real directory")
entries = sorted(path.name for path in runtime_bin.iterdir())
if entries != ["python3"]:
    raise SystemExit(f"runner runtime-bin entry set is not exact: {entries!r}")
shim = runtime_bin / "python3"
shim_details = shim.lstat()
if not stat.S_ISREG(shim_details.st_mode) or stat.S_ISLNK(shim_details.st_mode):
    raise SystemExit("tracked runtime-bin python3 shim is not a regular file")
cache_candidates = list((repo / "scripts").rglob("*")) + list(repo.glob("*.py[co]"))
cache_candidates += [repo / "__pycache__"]
for cache_path in cache_candidates:
    if os.path.lexists(cache_path) and (
        cache_path.name == "__pycache__" or cache_path.suffix in {".pyc", ".pyo"}
    ):
        raise SystemExit(f"runner checkout contains a forbidden Python cache: {cache_path}")

git_dir = repo / ".git"
if not git_dir.is_dir() or git_dir.is_symlink():
    raise SystemExit("runner .git must be a real directory")
for forbidden in (git_dir / "config.worktree", git_dir / "info" / "sparse-checkout"):
    if os.path.lexists(forbidden):
        raise SystemExit(f"runner Git hide mechanism is forbidden: {forbidden.name}")

for info_name in ("exclude", "attributes"):
    info_file = git_dir / "info" / info_name
    if not os.path.lexists(info_file):
        continue
    details = info_file.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or details.st_nlink != 1
        or details.st_size > 65_536
    ):
        raise SystemExit(f"runner .git/info/{info_name} is unsafe")
    for line in info_file.read_text(encoding="utf-8", errors="strict").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            raise SystemExit(f"runner .git/info/{info_name} contains an active rule")

clean_env = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": os.devnull,
    "PATH": "/usr/bin:/bin",
}
config = subprocess.run(
    [git, "-c", "core.hooksPath=/dev/null", "-C", str(repo), "config", "--local", "--no-includes", "--null", "--list"],
    env=clean_env,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
if config.returncode != 0:
    raise SystemExit(f"could not inspect runner local Git config: {config.stderr.decode(errors='replace').strip()}")
actual_config = {}
for raw_entry in config.stdout.split(b"\0"):
    if not raw_entry:
        continue
    raw_name, separator, raw_value = raw_entry.partition(b"\n")
    if not separator:
        raise SystemExit("runner local Git config encoding is malformed")
    name = raw_name.decode("utf-8", errors="strict").lower()
    value = raw_value.decode("utf-8", errors="strict")
    if name in actual_config:
        raise SystemExit(f"runner local Git config repeats a key: {name}")
    actual_config[name] = value
expected_config = {
    "branch.main.merge": "refs/heads/main",
    "branch.main.remote": "origin",
    "core.autocrlf": "false",
    "core.bare": "false",
    "core.filemode": "true",
    "core.hookspath": "/dev/null",
    "core.logallrefupdates": "true",
    "core.repositoryformatversion": "0",
    "core.safecrlf": "true",
    "core.sshcommand": f"ssh -F {runner_home}/.ssh/degen_dogs_config",
    "remote.origin.fetch": "+refs/heads/*:refs/remotes/origin/*",
    "remote.origin.url": "git@github-degen-dogs:ael-dev3/Degen-Dogs-Mission-3.git",
    "user.email": "degen-dogs-runner@users.noreply.github.com",
    "user.name": "Degen Dogs Windows Runner",
}
if actual_config != expected_config:
    extra = sorted(set(actual_config) - set(expected_config))
    missing = sorted(set(expected_config) - set(actual_config))
    changed = sorted(
        name for name in set(actual_config) & set(expected_config)
        if actual_config[name] != expected_config[name]
    )
    raise SystemExit(
        f"runner local Git config is not the exact managed policy: "
        f"extra={extra!r} missing={missing!r} changed={changed!r}"
    )

index = subprocess.run(
    [git, "-c", "core.hooksPath=/dev/null", "-C", str(repo), "ls-files", "-v"],
    env=clean_env,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    check=False,
)
if index.returncode != 0:
    raise SystemExit(f"could not inspect runner index flags: {index.stderr.strip()}")
for line in index.stdout.splitlines():
    if not line.startswith("H "):
        raise SystemExit(f"runner index contains a hidden/special path flag: {line[:80]}")
PY
}

cleanup_legacy_python_caches() {
  "$runuser_bin" -u "$runner_user" -- "$env_bin" -i PATH=/usr/bin:/bin \
    /usr/bin/find "${repo_dir}/scripts" -xdev -depth \
    \( -name '*.pyc' -o -name '*.pyo' -o -name __pycache__ \) \
    -delete
  "$runuser_bin" -u "$runner_user" -- "$env_bin" -i PATH=/usr/bin:/bin \
    /usr/bin/find "$repo_dir" -xdev -maxdepth 1 \
    \( -name '*.pyc' -o -name '*.pyo' -o -name __pycache__ \) \
    -delete
}

cleanup_legacy_python_caches
validate_runtime_checkout_matches
validate_trusted_checkout_surface

python_runtime_root="${state_dir}/python-runtimes"
python_runtime_key="${expected_head}-${trusted_installer_commit}-v${bootstrap_receipt_schema_version}"
python_runtime_dir="${python_runtime_root}/${python_runtime_key}"
python_runtime_link="${state_dir}/python-runtime"
trusted_python_bin="${python_runtime_dir}/bin/python3"

validate_bootstrap_receipt() {
  local receipt_path="$1"
  "$system_python_bin" -I -B - "$receipt_path" "$expected_head" \
    "$trusted_installer_commit" "$bootstrap_receipt_schema_version" 0 <<'PY'
# WSL_BOOTSTRAP_RECEIPT_VALIDATOR
from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected_runtime_commit = sys.argv[2]
expected_trusted_installer_commit = sys.argv[3]
expected_schema_version = int(sys.argv[4])
expected_uid = int(sys.argv[5])
maximum_size = 512

try:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
except OSError as exc:
    raise SystemExit(f"bootstrap test receipt is missing or unsafe: {exc}") from exc
try:
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode):
        raise SystemExit("bootstrap test receipt is not a regular file")
    if details.st_uid != expected_uid:
        raise SystemExit("bootstrap test receipt owner is not trusted")
    if stat.S_IMODE(details.st_mode) != 0o600:
        raise SystemExit("bootstrap test receipt must have mode 0600")
    if details.st_nlink != 1:
        raise SystemExit("bootstrap test receipt must have exactly one hard link")
    if details.st_size <= 0 or details.st_size > maximum_size:
        raise SystemExit("bootstrap test receipt has an invalid size")
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        descriptor = -1
        raw = handle.read(maximum_size + 1)
finally:
    if descriptor >= 0:
        os.close(descriptor)

try:
    record = json.loads(raw.decode("utf-8", errors="strict"))
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"bootstrap test receipt is malformed: {exc}") from exc
required_keys = {
    "runtime_commit",
    "schema_version",
    "trusted_installer_commit",
}
if not isinstance(record, dict) or set(record) != required_keys:
    raise SystemExit("bootstrap test receipt has an unexpected schema")
if type(record["schema_version"]) is not int or record["schema_version"] != expected_schema_version:
    raise SystemExit("bootstrap test receipt was minted under an old test schema")
commit_pattern = re.compile(r"[0-9a-f]{40}")
for field in ("runtime_commit", "trusted_installer_commit"):
    value = record[field]
    if not isinstance(value, str) or commit_pattern.fullmatch(value) is None:
        raise SystemExit(f"bootstrap test receipt has an invalid {field}")
if record["runtime_commit"] != expected_runtime_commit:
    raise SystemExit("bootstrap test receipt does not match the runtime commit")
if record["trusted_installer_commit"] != expected_trusted_installer_commit:
    raise SystemExit("bootstrap test receipt was minted by a different trusted installer")
canonical = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
if raw != canonical:
    raise SystemExit("bootstrap test receipt is not canonical JSON")
PY
}

write_bootstrap_receipt() {
  assert_bootstrap_gate_completion || return $?
  "$system_python_bin" -I -B - "$tested_receipt_path" "$expected_head" \
    "$trusted_installer_commit" "$bootstrap_receipt_schema_version" 0 <<'PY'
# WSL_BOOTSTRAP_RECEIPT_WRITER
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

target = Path(sys.argv[1])
runtime_commit = sys.argv[2]
trusted_installer_commit = sys.argv[3]
schema_version = int(sys.argv[4])
expected_uid = int(sys.argv[5])
if os.geteuid() != expected_uid:
    raise SystemExit("bootstrap test receipt writer is not running as the trusted owner")
record = {
    "runtime_commit": runtime_commit,
    "schema_version": schema_version,
    "trusted_installer_commit": trusted_installer_commit,
}
payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
if len(payload) > 512:
    raise SystemExit("bootstrap test receipt payload is unexpectedly large")

descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
temporary = Path(temporary_name)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        descriptor = -1
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    directory_descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
finally:
    if descriptor >= 0:
        os.close(descriptor)
    temporary.unlink(missing_ok=True)
PY
  validate_bootstrap_receipt "$tested_receipt_path"
}

fsync_state_directory() {
  "$system_python_bin" -I -B - "$state_dir" <<'PY'
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

claim_bootstrap_receipt() {
  # WSL_BOOTSTRAP_RECEIPT_CLAIM
  [[ ! -e "$claimed_receipt_path" && ! -L "$claimed_receipt_path" ]] || \
    fail "a prior activation already claimed the bootstrap receipt; rerun the full disabled bootstrap"
  validate_bootstrap_receipt "$tested_receipt_path" || \
    fail "no valid bootstrap test receipt exists for this runtime and trusted installer"
  /bin/mv -T -- "$tested_receipt_path" "$claimed_receipt_path"
  fsync_state_directory
  validate_bootstrap_receipt "$claimed_receipt_path" || \
    fail "claimed bootstrap test receipt failed revalidation"
}

consume_bootstrap_receipt() {
  validate_bootstrap_receipt "$claimed_receipt_path" || \
    fail "claimed bootstrap test receipt became invalid before activation completed"
  /bin/rm -f -- "$claimed_receipt_path"
  fsync_state_directory
}

if [[ "$skip_bootstrap" == "1" && "$enable_now" == "1" ]]; then
  claim_bootstrap_receipt
fi

env_file="${env_file:-${repo_dir}/.env.local}"
[[ "$env_file" =~ ^/[A-Za-z0-9._/-]+$ && "$env_file" != *%* ]] || \
  fail "env file must be an absolute systemd-safe path"
if [[ ! -e "$env_file" ]]; then
  install -o "$runner_user" -g "$runner_group" -m 0600 \
    "${asset_dir}/config/wsl-runner.env.template" "$env_file"
  printf 'created %s; fill the credentialed RPC endpoints before --enable-now\n' "$env_file"
fi
[[ -f "$env_file" && ! -L "$env_file" ]] || fail "env file must be a regular non-symlink file"
[[ "$(stat -c %U "$env_file")" == "$runner_user" ]] || fail "env file must be owned by ${runner_user}"
[[ "$(stat -c %h "$env_file")" == "1" ]] || fail "env file must have exactly one hard link"
[[ "$(stat -c %a "$env_file")" == "600" ]] || fail "env file must have mode 600"

log_dir="/var/log/degen-dogs"
lock_dir="/var/cache/degen-dogs"
install -d -o "$runner_user" -g "$runner_group" -m 0700 "$log_dir" "$lock_dir" "$lock_dir/npm"
runuser -u "$runner_user" -- mkdir -p "${repo_dir}/.local" "${repo_dir}/logs"
for local_dir in "${repo_dir}/.local" "${repo_dir}/logs"; do
  [[ -d "$local_dir" && ! -L "$local_dir" && "$(stat -c %U "$local_dir")" == "$runner_user" ]] || \
    fail "runner data directory must be an owned non-symlink directory: ${local_dir}"
  runuser -u "$runner_user" -- chmod 0700 "$local_dir"
done

run_as_runner() {
  "$runuser_bin" -u "$runner_user" -- "$env_bin" -i \
    HOME="$runner_home" \
    PATH="/usr/bin:/bin" \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_SYSTEM=/dev/null \
    GIT_TERMINAL_PROMPT=0 \
    NPM_CONFIG_CACHE="${lock_dir}/npm" \
    "$@"
}

run_as_runner_runtime() {
  validate_trusted_python_runtime "$python_runtime_dir"
  "$runuser_bin" -u "$runner_user" -- "$env_bin" -i \
    HOME="$runner_home" \
    PATH="${python_runtime_dir}/bin:/usr/bin:/bin" \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_SYSTEM=/dev/null \
    GIT_TERMINAL_PROMPT=0 \
    NPM_CONFIG_CACHE="${lock_dir}/npm" \
    "$@"
}

runner_git -C "$repo_dir" config user.name 'Degen Dogs Windows Runner'
runner_git -C "$repo_dir" config user.email 'degen-dogs-runner@users.noreply.github.com'
runner_git -C "$repo_dir" config core.hooksPath /dev/null
runner_git -C "$repo_dir" config core.autocrlf false
runner_git -C "$repo_dir" config core.safecrlf true
runner_git -C "$repo_dir" config core.filemode true

ssh_dir="${runner_home}/.ssh"
deploy_key="${ssh_dir}/degen_dogs_windows_ed25519"
ssh_config="${ssh_dir}/degen_dogs_config"
known_hosts="${ssh_dir}/degen_dogs_known_hosts"
deploy_public_key=""
if [[ "$skip_deploy_key" != "1" ]]; then
  runner_git -C "$repo_dir" config --unset-all remote.origin.pushurl >/dev/null 2>&1 || true
  if [[ -e "$ssh_dir" || -L "$ssh_dir" ]]; then
    [[ -d "$ssh_dir" && ! -L "$ssh_dir" && "$(stat -c %U "$ssh_dir")" == "$runner_user" ]] || \
      fail "runner SSH directory must be an owned non-symlink directory"
    runuser -u "$runner_user" -- chmod 0700 "$ssh_dir"
  else
    install -d -o "$runner_user" -g "$runner_group" -m 0700 "$ssh_dir"
  fi
  if [[ ! -e "$deploy_key" ]]; then
    run_as_runner "$ssh_keygen_bin" -q -t ed25519 -a 100 -N '' \
      -C "degen-dogs-${runner_id}" -f "$deploy_key"
  fi
  [[ -f "$deploy_key" && ! -L "$deploy_key" && "$(stat -c %U "$deploy_key")" == "$runner_user" && \
    "$(stat -c %h "$deploy_key")" == "1" && "$(stat -c %a "$deploy_key")" == "600" ]] || \
    fail "deploy private key is missing, linked, or not mode 600"

  run_as_runner "$system_python_bin" -I -B - "$known_hosts" "$ssh_config" "$deploy_key" <<'PY'
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

known_hosts, config, key = map(Path, sys.argv[1:])
# Published by GitHub Docs. Fingerprint:
# SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU
known_hosts_text = (
    "github.com ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
)
config_text = f"""Host github-degen-dogs
    HostName github.com
    User git
    IdentityFile {key}
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking yes
    UserKnownHostsFile {known_hosts}
    GlobalKnownHostsFile /dev/null
    ProxyCommand none
    ProxyJump none
"""

for target, text in ((known_hosts, known_hosts_text), (config, config_text)):
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
PY

  runner_git -C "$repo_dir" remote set-url origin \
    git@github-degen-dogs:ael-dev3/Degen-Dogs-Mission-3.git
  runner_git -C "$repo_dir" config core.sshCommand "ssh -F ${ssh_config}"
  deploy_public_key="$(run_as_runner "$ssh_keygen_bin" -y -f "$deploy_key")"
fi

validate_runner_git_destination() {
  local host_fingerprint origin_url push_urls expected_ssh_command
  [[ -d "$ssh_dir" && ! -L "$ssh_dir" && "$(stat -c %U "$ssh_dir")" == "$runner_user" && \
    "$(stat -c %a "$ssh_dir")" == "700" ]] || \
    fail "runner SSH directory is missing, linked, or not mode 700"
  for protected_path in "$deploy_key" "$ssh_config" "$known_hosts"; do
    [[ -f "$protected_path" && ! -L "$protected_path" && \
      "$(stat -c %U "$protected_path")" == "$runner_user" && \
      "$(stat -c %h "$protected_path")" == "1" && \
      "$(stat -c %a "$protected_path")" == "600" ]] || \
      fail "dedicated SSH material is missing, linked, or not mode 600: ${protected_path}"
  done
  run_as_runner "$system_python_bin" -I -B - "$known_hosts" "$ssh_config" "$deploy_key" <<'PY'
# WSL_SSH_MATERIAL_VALIDATOR
from __future__ import annotations

import sys
from pathlib import Path

known_hosts, config, key = map(Path, sys.argv[1:])
expected_known_hosts = (
    "github.com ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
).encode("ascii")
expected_config = f"""Host github-degen-dogs
    HostName github.com
    User git
    IdentityFile {key}
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking yes
    UserKnownHostsFile {known_hosts}
    GlobalKnownHostsFile /dev/null
    ProxyCommand none
    ProxyJump none
""".encode("ascii")

for label, path, expected in (
    ("known_hosts", known_hosts, expected_known_hosts),
    ("SSH config", config, expected_config),
):
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"could not read canonical {label}: {exc}") from exc
    if actual != expected:
        raise SystemExit(f"dedicated {label} does not match the canonical managed content")
PY
  run_as_runner "$ssh_keygen_bin" -y -f "$deploy_key" >/dev/null || \
    fail "dedicated deploy private key is invalid"
  host_fingerprint="$(run_as_runner "$ssh_keygen_bin" -lf "$known_hosts" -E sha256 | /usr/bin/awk '{print $2}')"
  [[ "$host_fingerprint" == "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" ]] || \
    fail "pinned GitHub host key fingerprint validation failed"
  origin_url="$(runner_git -C "$repo_dir" remote get-url origin)"
  [[ "$origin_url" == 'git@github-degen-dogs:ael-dev3/Degen-Dogs-Mission-3.git' ]] || \
    fail "origin is not the pinned Degen Dogs deploy-key destination"
  [[ -z "$(runner_git -C "$repo_dir" config --local --get-all remote.origin.pushurl || true)" ]] || \
    fail "origin must not define a separate pushurl"
  push_urls="$(runner_git -C "$repo_dir" remote get-url --push --all origin)"
  [[ "$push_urls" == "$origin_url" ]] || fail "runtime push destination differs from the validated origin"
  expected_ssh_command="ssh -F ${ssh_config}"
  [[ "$(runner_git -C "$repo_dir" config --local --get core.sshCommand)" == "$expected_ssh_command" ]] || \
    fail "origin is not pinned to the dedicated strict-host-key SSH configuration"
}

# --skip-deploy-key preserves existing credentials; it never skips validation
# of the dedicated destination, strict SSH config, or pinned GitHub host key.
validate_runner_git_destination

validate_trusted_python_runtime() {
  local candidate="$1"
  "$system_python_bin" -I -B - "$candidate" <<'PY'
# WSL_TRUSTED_PYTHON_RUNTIME_PROVISION
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
if not root.is_dir() or root.is_symlink():
    raise SystemExit("trusted Python runtime is not a real directory")
root_details = root.lstat()
if root_details.st_uid != 0 or stat.S_IMODE(root_details.st_mode) & 0o022:
    raise SystemExit("trusted Python runtime root ownership/mode is unsafe")
if stat.S_IMODE(root_details.st_mode) & 0o005 != 0o005:
    raise SystemExit("trusted Python runtime root is not runner-readable/traversable")
root_resolved = root.resolve(strict=True)
for path in root.rglob("*"):
    details = path.lstat()
    if details.st_uid != 0:
        raise SystemExit(f"trusted Python runtime has a non-root owner: {path}")
    if stat.S_ISLNK(details.st_mode):
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise SystemExit(f"trusted Python runtime symlink escapes its root: {path}") from exc
        continue
    if not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
        raise SystemExit(f"trusted Python runtime has an unsupported file type: {path}")
    if stat.S_IMODE(details.st_mode) & 0o022:
        raise SystemExit(f"trusted Python runtime is group/other writable: {path}")
    if stat.S_ISDIR(details.st_mode) and stat.S_IMODE(details.st_mode) & 0o005 != 0o005:
        raise SystemExit(f"trusted Python runtime directory is not runner-readable/traversable: {path}")
    if stat.S_ISREG(details.st_mode) and not stat.S_IMODE(details.st_mode) & 0o004:
        raise SystemExit(f"trusted Python runtime file is not runner-readable: {path}")
python = root / "bin" / "python3"
details = python.lstat()
if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
    raise SystemExit("trusted Python entrypoint is not a regular copied binary")
if details.st_uid != 0 or details.st_nlink != 1 or not os.access(python, os.X_OK):
    raise SystemExit("trusted Python entrypoint ownership/link/mode is unsafe")
PY
  "$runuser_bin" -u "$runner_user" -- "$env_bin" -i \
    HOME="$runner_home" PATH="${candidate}/bin:/usr/bin:/bin" \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
    "${candidate}/bin/python3" -I -B -c \
    "import Crypto; from Crypto.Hash import keccak; assert Crypto.__version__ == '3.23.0'; assert keccak.new(digest_bits=256, data=b'').hexdigest() == 'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470'"
}

provision_trusted_python_runtime() (
  set -Eeuo pipefail
  local package_user=nobody
  local package_uid package_gid runtime_stage dependency_stage purelib link_tmp
  package_uid="$(id -u "$package_user")"
  package_gid="$(id -g "$package_user")"
  [[ "$package_uid" != "0" && "$package_gid" != "0" && \
    "$(id -G "$package_user")" == "$package_gid" ]] || \
    fail "isolated dependency installer account is unavailable or over-privileged"
  install -d -o root -g root -m 0755 "$python_runtime_root"
  if [[ ! -e "$python_runtime_dir" ]]; then
    runtime_stage="$(mktemp -d "${python_runtime_root}/.runtime.XXXXXX")"
    dependency_stage="$(mktemp -d "${state_dir}/.python-dependencies.XXXXXX")"
    cleanup_python_runtime() {
      case "${runtime_stage:-}" in "${python_runtime_root}/.runtime."*) /bin/rm -rf -- "$runtime_stage" ;; esac
      case "${dependency_stage:-}" in "${state_dir}/.python-dependencies."*) /bin/rm -rf -- "$dependency_stage" ;; esac
    }
    trap cleanup_python_runtime EXIT
    run_required_gate python-venv "$system_python_bin" -I -B -m venv --without-pip --copies "$runtime_stage"
    purelib="$("${runtime_stage}/bin/python3" -I -B -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    [[ "$purelib" == "$runtime_stage"/* ]] || fail "trusted Python site-packages escaped its stage"
    /bin/chown "$package_uid:$package_gid" "$dependency_stage"
    /bin/chmod 0700 "$dependency_stage"
    run_required_gate python-pip "$runuser_bin" -u "$package_user" -- "$env_bin" -i \
      HOME=/nonexistent PATH=/usr/bin:/bin PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
      TMPDIR="$dependency_stage" \
      "$system_python_bin" -I -B -m pip install --disable-pip-version-check \
      --require-hashes --only-binary=:all: --no-deps --target "$dependency_stage/site" \
      -r "${runtime_tree}/requirements.txt"
    "$system_python_bin" -I -B - "$dependency_stage/site" "$package_uid" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected_uid = int(sys.argv[2])
resolved_root = root.resolve(strict=True)
for path in root.rglob("*"):
    details = path.lstat()
    if details.st_uid != expected_uid:
        raise SystemExit(f"dependency stage owner mismatch: {path}")
    if stat.S_ISLNK(details.st_mode):
        try:
            path.resolve(strict=True).relative_to(resolved_root)
        except ValueError as exc:
            raise SystemExit(f"dependency stage symlink escaped: {path}") from exc
    elif not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
        raise SystemExit(f"dependency stage file type is unsafe: {path}")
    elif stat.S_IMODE(details.st_mode) & 0o022:
        raise SystemExit(f"dependency stage is group/other writable: {path}")
PY
    /bin/cp -a -- "$dependency_stage/site/." "$purelib/"
    /bin/chown -R root:root "$runtime_stage"
    /bin/chmod -R u+rwX,go+rX,go-w "$runtime_stage"
    validate_trusted_python_runtime "$runtime_stage"
    /bin/mv -T -- "$runtime_stage" "$python_runtime_dir"
    runtime_stage=""
  fi
  validate_trusted_python_runtime "$python_runtime_dir"
  link_tmp="${state_dir}/.python-runtime.$$"
  /bin/rm -f -- "$link_tmp"
  /bin/ln -s -- "$python_runtime_dir" "$link_tmp"
  /bin/mv -Tf -- "$link_tmp" "$python_runtime_link"
  fsync_state_directory
  [[ "$(/usr/bin/readlink -f -- "$python_runtime_link")" == "$python_runtime_dir" ]] || \
    fail "trusted Python runtime pointer did not resolve to the exact version"
)

bootstrap_runtime_and_run_tests() (
  # WSL_BOOTSTRAP_CORE_START
  local package_user=nobody
  local package_uid package_gid source_stage npm_stage runner_tmp build_output
  package_uid="$(id -u "$package_user")"
  package_gid="$(id -g "$package_user")"
  provision_trusted_python_runtime
  source_stage="$(mktemp -d "${state_dir}/.bootstrap-source.XXXXXX")"
  npm_stage="$(mktemp -d "${state_dir}/.npm-dependencies.XXXXXX")"
  runner_tmp="$(mktemp -d "${state_dir}/.bootstrap-tmp.XXXXXX")"
  build_output="$(mktemp -d "${state_dir}/.bootstrap-build.XXXXXX")"
  cleanup_bootstrap_tests() {
    case "${source_stage:-}" in "${state_dir}/.bootstrap-source."*) /bin/rm -rf -- "$source_stage" ;; esac
    case "${npm_stage:-}" in "${state_dir}/.npm-dependencies."*) /bin/rm -rf -- "$npm_stage" ;; esac
    case "${runner_tmp:-}" in "${state_dir}/.bootstrap-tmp."*) /bin/rm -rf -- "$runner_tmp" ;; esac
    case "${build_output:-}" in "${state_dir}/.bootstrap-build."*) /bin/rm -rf -- "$build_output" ;; esac
  }
  trap cleanup_bootstrap_tests EXIT
  /bin/cp -a -- "${runtime_tree}/." "$source_stage/"
  [[ ! -e "${source_stage}/.git" && ! -L "${source_stage}/.git" ]] || \
    fail "trusted test source unexpectedly contains Git metadata"
  /bin/chown -R root:root "$source_stage"
  /bin/chmod -R a+rX,u+w,go-w "$source_stage"

  /bin/chown "$package_uid:$package_gid" "$npm_stage"
  /bin/chmod 0700 "$npm_stage"
  /usr/bin/install -o "$package_uid" -g "$package_gid" -m 0600 \
    "${runtime_tree}/package.json" "$npm_stage/package.json"
  /usr/bin/install -o "$package_uid" -g "$package_gid" -m 0600 \
    "${runtime_tree}/package-lock.json" "$npm_stage/package-lock.json"
  run_required_gate test-npm-ci "$runuser_bin" -u "$package_user" -- "$env_bin" -i \
    HOME="$npm_stage" PATH=/usr/bin:/bin NPM_CONFIG_CACHE="$npm_stage/cache" \
    "$node_bin" "$npm_cli" --prefix "$npm_stage" ci --ignore-scripts --no-audit --no-fund \
    --workspaces=false \
    --userconfig="$npm_user_config" --globalconfig="$npm_global_config" \
    --cache="$npm_stage/cache" \
    >/dev/null
  # WSL_TRUSTED_TEST_SOURCE_PROVISION
  "$system_python_bin" -I -B - "$npm_stage/node_modules" "$package_uid" <<'PY'
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected_uid = int(sys.argv[2])
resolved_root = root.resolve(strict=True)
for path in root.rglob("*"):
    details = path.lstat()
    if details.st_uid != expected_uid:
        raise SystemExit(f"npm dependency stage owner mismatch: {path}")
    if stat.S_ISLNK(details.st_mode):
        try:
            path.resolve(strict=True).relative_to(resolved_root)
        except ValueError as exc:
            raise SystemExit(f"npm dependency symlink escaped: {path}") from exc
    elif not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
        raise SystemExit(f"npm dependency file type is unsafe: {path}")
    elif stat.S_IMODE(details.st_mode) & 0o022:
        raise SystemExit(f"npm dependency is group/other writable: {path}")
PY
  /bin/chown -R root:root "$npm_stage/node_modules"
  /bin/chmod -R u+rwX,go+rX,go-w "$npm_stage/node_modules"
  /bin/mv -T -- "$npm_stage/node_modules" "$source_stage/node_modules"
  /bin/chown "$runner_user:$runner_group" "$runner_tmp" "$build_output"
  /bin/chmod 0700 "$runner_tmp" "$build_output"

  run_bootstrap_test() {
    "$runuser_bin" -u "$runner_user" -- "$env_bin" -i \
      HOME="$runner_home" PATH="${python_runtime_dir}/bin:/usr/bin:/bin" \
      TMPDIR="$runner_tmp" NPM_CONFIG_CACHE="$runner_tmp/npm" \
      DEGEN_DOGS_PYTHON_BIN="${python_runtime_link}/bin/python3" \
      PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
      GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 \
      "$@"
  }
  run_bootstrap_python() {
    local script_path="$1"
    shift
    run_required_gate "suite:${script_path##*/}" run_bootstrap_test "$trusted_python_bin" -I -B -c \
      'import os,runpy,sys; p=sys.argv.pop(1); sys.path.insert(0, os.path.dirname(p)); runpy.run_path(p, run_name="__main__")' \
      "$script_path" "$@"
  }
  run_bootstrap_python "${source_stage}/scripts/test_build_dashboard.py"
  run_bootstrap_python "${source_stage}/scripts/test_build_live_snapshot_bundle.py"
  run_bootstrap_python "${source_stage}/scripts/test_refresh_current_surface.py"
  run_bootstrap_python "${source_stage}/scripts/test_runner_publication_state.py"
  run_bootstrap_python "${source_stage}/scripts/test_publication_coverage_git_hardening.py"
  run_bootstrap_python "${source_stage}/scripts/test_watch_mission3_auction.py"
  run_bootstrap_python "${source_stage}/scripts/test_drain_publication_queue.py"
  run_bootstrap_python "${source_stage}/scripts/test_verify_pages_deployment.py"
  run_required_gate suite:test_refresh_and_publish.sh run_bootstrap_test "$bash_bin" "${source_stage}/scripts/test_refresh_and_publish.sh"
  run_bootstrap_python "${source_stage}/scripts/test_refresh_telemetry.py"
  run_bootstrap_python "${source_stage}/scripts/test_degen_dogs_runner_health.py"
  run_bootstrap_python "${source_stage}/scripts/test_wsl_publication_integration.py"
  run_required_gate dashboard-build run_bootstrap_test "$node_bin" "$npm_cli" --prefix "$source_stage" run build -- \
    --outDir "$build_output"
  # WSL_BOOTSTRAP_CORE_END
)

provision_runner_node_dependencies() {
  validate_runtime_checkout_matches
  validate_trusted_checkout_surface
  "$runuser_bin" -u "$runner_user" -- "$env_bin" -i \
    HOME="$runner_home" PATH=/usr/bin:/bin \
    NPM_CONFIG_CACHE="${lock_dir}/npm" \
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 \
    "$node_bin" "$npm_cli" --prefix "$repo_dir" ci --ignore-scripts \
    --no-audit --no-fund --workspaces=false \
    --userconfig="$npm_user_config" --globalconfig="$npm_global_config" \
    --cache="${lock_dir}/npm" >/dev/null
  validate_runtime_checkout_matches
  validate_trusted_checkout_surface
}

if [[ "$skip_bootstrap" != "1" ]]; then
  run_required_gate bootstrap-runtime-tests bootstrap_runtime_and_run_tests
  run_required_gate production-npm-warm provision_runner_node_dependencies
else
  validate_trusted_python_runtime "$python_runtime_dir"
  [[ "$(/usr/bin/readlink -f -- "$python_runtime_link")" == "$python_runtime_dir" ]] || \
    fail "trusted Python runtime pointer does not match this activation receipt"
fi

render_template() {
  local source_path="$1"
  local target_path="$2"
  "$system_python_bin" -I -B - "$source_path" "$target_path" \
    "$runner_user" "$runner_group" "$runner_home" "$repo_dir" "$log_dir" "$lock_dir" "$env_file" "$runner_id" <<'PY'
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
values = dict(
    zip(
        (
            "@RUNNER_USER@",
            "@RUNNER_GROUP@",
            "@RUNNER_HOME@",
            "@REPO_DIR@",
            "@LOG_DIR@",
            "@LOCK_DIR@",
            "@ENV_FILE@",
            "@RUNNER_ID@",
        ),
        sys.argv[3:],
        strict=True,
    )
)
text = source.read_text(encoding="utf-8")
for marker, value in values.items():
    if "%" in value or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise SystemExit(f"unsafe systemd metacharacter in template value for {marker}")
    text = text.replace(marker, value)
if "@RUNNER_" in text or "@REPO_DIR@" in text or "@LOG_DIR@" in text or "@LOCK_DIR@" in text or "@ENV_FILE@" in text:
    raise SystemExit(f"unresolved template marker in {source}")
target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
temporary = Path(temporary_name)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, target)
finally:
    temporary.unlink(missing_ok=True)
PY
}

unit_dir="/etc/systemd/system"
rendered_unit_names=(
  degen-dogs-watcher.service
  degen-dogs-hourly.service
  degen-dogs-health.service
  degen-dogs-publisher.service
  degen-dogs-publisher.path
  degen-dogs-pages-verifier.service
  degen-dogs-pages-verifier.path
)
copied_unit_names=(
  degen-dogs-watcher.timer
  degen-dogs-hourly.timer
  degen-dogs-health.timer
  degen-dogs-publisher.timer
  degen-dogs-pages-verifier.timer
  degen-dogs-runner.target
)
for name in "${rendered_unit_names[@]}"; do
  run_required_gate "render:${name}" render_template \
    "${asset_dir}/config/systemd/${name}.in" "${unit_dir}/${name}"
done
for name in "${copied_unit_names[@]}"; do
  run_required_gate "render:${name}" install -m 0644 \
    "${asset_dir}/config/systemd/${name}" "${unit_dir}/${name}"
done
run_required_gate render:logrotate render_template \
  "${asset_dir}/config/logrotate/degen-dogs-wsl.in" "/etc/logrotate.d/degen-dogs-wsl"
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "${asset_dir}/scripts/run_wsl_runner_anchor.sh" \
  /usr/local/libexec/degen-dogs-wsl-anchor

systemctl daemon-reload
verify_units=()
for name in "${unit_names[@]}"; do
  verify_units+=("${unit_dir}/${name}")
done
run_required_gate systemd-analyze systemd-analyze verify "${verify_units[@]}"
run_required_gate logrotate logrotate --debug /etc/logrotate.d/degen-dogs-wsl >/dev/null
if [[ "$skip_bootstrap" != "1" ]]; then
  run_required_gate final-checkout validate_runtime_checkout_matches
  run_required_gate final-git-surface validate_trusted_checkout_surface
  run_required_gate final-python-runtime validate_trusted_python_runtime "$python_runtime_dir"
  write_bootstrap_receipt
fi

if [[ "$enable_now" == "1" ]]; then
  validate_runtime_checkout_matches
  validate_trusted_checkout_surface
  validate_trusted_python_runtime "$python_runtime_dir"
  tracked_status="$(run_as_runner_runtime "$git_bin" -C "$repo_dir" status --porcelain --untracked-files=all)"
  [[ -z "$tracked_status" ]] || fail "non-ignored worktree changes must be committed or removed before enabling auto-push"
  validate_runner_git_destination
  [[ "$(run_as_runner_runtime "$git_bin" -C "$repo_dir" config --local --get core.hooksPath)" == "/dev/null" ]] || \
    fail "Git hooks must remain disabled for the publisher clone"
  [[ -n "$(run_as_runner_runtime "$git_bin" -C "$repo_dir" config --local --get user.name)" ]] || \
    fail "repo-local Git user.name is missing"
  [[ -n "$(run_as_runner_runtime "$git_bin" -C "$repo_dir" config --local --get user.email)" ]] || \
    fail "repo-local Git user.email is missing"
  current_branch="$(run_as_runner_runtime "$git_bin" -C "$repo_dir" branch --show-current)"
  [[ "$current_branch" == "main" ]] || fail "publisher clone must be on main, not ${current_branch:-detached HEAD}"
  run_as_runner_runtime "$git_bin" -C "$repo_dir" fetch origin \
    refs/heads/main:refs/remotes/origin/main
  local_head="$(run_as_runner_runtime "$git_bin" -C "$repo_dir" rev-parse HEAD)"
  remote_head="$(run_as_runner_runtime "$git_bin" -C "$repo_dir" rev-parse refs/remotes/origin/main)"
  [[ "$local_head" == "$remote_head" ]] || \
    fail "local main must exactly match origin/main before activation; fast-forward it and rerun"
  # Parse .env.local through the data-only loader, prove the configured
  # cross-provider quorum, and run one read-only watcher sample. The preflight
  # never writes watcher state or invokes a refresh/push command.
  run_as_runner_runtime "$env_bin" \
    DEGEN_DOGS_REPO_DIR="$runtime_tree" \
    DEGEN_DOGS_ENV_FILE="$env_file" \
    DEGEN_DOGS_LOG_DIR="$log_dir" \
    DEGEN_DOGS_LOCK_DIR="$lock_dir" \
    DEGEN_DOGS_PYTHON_BIN="${python_runtime_link}/bin/python3" \
    "$bash_bin" -p "${runtime_tree}/scripts/run_wsl_runner_job.sh" preflight
  validate_runtime_checkout_matches
  validate_trusted_checkout_surface

  ls_remote_main="$(run_as_runner_runtime "$git_bin" -C "$repo_dir" ls-remote --exit-code origin refs/heads/main)" || \
    fail "noninteractive GitHub authentication or network access is unavailable"
  [[ "${ls_remote_main%%[[:space:]]*}" == "$remote_head" ]] || \
    fail "origin/main advanced during activation; rerun the bootstrap against the new exact commit"
  run_as_runner_runtime "$git_bin" -C "$repo_dir" push --dry-run origin HEAD:refs/heads/main >/dev/null || \
    fail "GitHub write authentication, branch policy, or fast-forward state rejected a dry-run push"

  if ! systemctl enable "${activation_unit_names[@]}"; then
    systemctl disable --now "${activation_unit_names[@]}" >/dev/null 2>&1 || true
    systemctl stop "${service_unit_names[@]}" >/dev/null 2>&1 || true
    fail "systemd enable failed and all runner timers were disabled again"
  fi
  for name in degen-dogs-publisher.service degen-dogs-pages-verifier.service; do
    [[ "$(systemctl show --property=LoadState --value "$name")" == "loaded" ]] || \
      fail "triggered worker did not load after installation: ${name}"
    systemctl is-failed --quiet "$name" && fail "triggered worker is failed after installation: ${name}"
  done
  consume_bootstrap_receipt
  printf 'systemd units enabled behind the absent activation marker; the Windows bootstrap must verify its keepalive before publication can start\n'
else
  systemctl disable --now "${activation_unit_names[@]}" >/dev/null 2>&1 || true
  systemctl stop "${service_unit_names[@]}" >/dev/null 2>&1 || true
  printf 'units installed but disabled; rerun with --enable-now only after the race-safe publisher is on main and RPC/GitHub configuration is complete\n'
fi

if [[ -n "$deploy_public_key" ]]; then
  printf '\nAdd this PUBLIC key in GitHub repository Settings > Deploy keys with write access:\n%s\n\n' \
    "$deploy_public_key"
  printf 'The private key remains mode 600 inside the WSL runner account and was not displayed.\n'
fi
printf 'WSL runner assets installed for %s at %s\n' "$runner_user" "$repo_dir"
