#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

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
  --skip-bootstrap      Do not create the venv, npm install, build, or smoke tests
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

[[ "$(id -u)" == "0" ]] || fail "run this installer as root with sudo"
grep -qi microsoft /proc/sys/kernel/osrelease || fail "this installer is for WSL2"
[[ "$(ps -p 1 -o comm=)" == "systemd" ]] || fail "enable systemd=true in /etc/wsl.conf, then run wsl --shutdown from Windows"

unit_dir="/etc/systemd/system"
unit_names=(
  degen-dogs-watcher.service
  degen-dogs-watcher.timer
  degen-dogs-hourly.service
  degen-dogs-hourly.timer
  degen-dogs-health.service
  degen-dogs-health.timer
  degen-dogs-runner.target
)

if [[ "$uninstall" == "1" ]]; then
  rm -f -- /var/lib/degen-dogs/activation-armed /run/degen-dogs/activation-enabled /run/degen-dogs/anchor-ready
  systemctl disable --now \
    degen-dogs-runner.target \
    degen-dogs-watcher.timer \
    degen-dogs-hourly.timer \
    degen-dogs-health.timer >/dev/null 2>&1 || true
  systemctl stop \
    degen-dogs-watcher.service \
    degen-dogs-hourly.service \
    degen-dogs-health.service >/dev/null 2>&1 || true
  for old_unit in \
    degen-dogs-runner.target \
    degen-dogs-watcher.timer \
    degen-dogs-hourly.timer \
    degen-dogs-health.timer \
    degen-dogs-watcher.service \
    degen-dogs-hourly.service \
    degen-dogs-health.service; do
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
[[ -n "$repo_dir" ]] || \
  fail "--repo-dir is required and must be supplied by the elevated Windows bootstrap"
[[ "$runtime_tree" =~ ^/[A-Za-z0-9._/-]+$ && "$runtime_tree" != /mnt/* && "$runtime_tree" != *%* ]] || \
  fail "--runtime-tree must name a root-owned WSL ext4 export"
runtime_tree="$(readlink -f "$runtime_tree")"
[[ -d "$runtime_tree" && ! -L "$runtime_tree" && "$(stat -c %U "$runtime_tree")" == "root" ]] || \
  fail "runtime manifest tree must be a root-owned non-symlink directory"
runtime_tree_mode="$(stat -c %a "$runtime_tree")"
(( (8#$runtime_tree_mode & 8#022) == 0 )) || \
  fail "runtime manifest tree must not be group/other writable"
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
)
for relative in "${trusted_root_assets[@]}"; do
  trusted_path="${asset_dir}/${relative}"
  [[ -f "$trusted_path" && ! -L "$trusted_path" && "$(stat -c %U "$trusted_path")" == "root" ]] || \
    fail "root-consumed asset is not a root-owned regular file: ${relative}"
  trusted_mode="$(stat -c %a "$trusted_path")"
  (( (8#$trusted_mode & 8#022) == 0 )) || \
    fail "root-consumed asset is group/other writable: ${relative}"
done

# Direct Linux upgrades must be as race-safe as the Windows bootstrap. Remove
# the two-phase activation gates first, then synchronously quiesce every old
# timer/worker before inspecting or replacing the runtime and unit files.
rm -f -- /var/lib/degen-dogs/activation-armed /run/degen-dogs/activation-enabled /run/degen-dogs/anchor-ready
systemctl disable --now \
  degen-dogs-runner.target \
  degen-dogs-watcher.timer \
  degen-dogs-hourly.timer \
  degen-dogs-health.timer >/dev/null 2>&1 || true
systemctl stop \
  degen-dogs-watcher.service \
  degen-dogs-hourly.service \
  degen-dogs-health.service >/dev/null 2>&1 || true
for old_unit in \
  degen-dogs-runner.target \
  degen-dogs-watcher.timer \
  degen-dogs-hourly.timer \
  degen-dogs-health.timer \
  degen-dogs-watcher.service \
  degen-dogs-hourly.service \
  degen-dogs-health.service; do
  systemctl is-active --quiet "$old_unit" && fail "could not quiesce ${old_unit} before installation"
done

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

python3 - "$runtime_tree" <<'PY'
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
node_major="$(node -p 'process.versions.node.split(`.`)[0]')"
[[ "$node_major" == "22" ]] || fail "Node 22 is required (found $(node --version))"

# systemd uses a narrow, deterministic PATH and never loads nvm startup files.
# Link an existing Node 22 toolchain into /usr/local/bin without replacing any
# administrator-managed entry already there.
for command in node npm npx; do
  command_path="$(command -v "$command")"
  if [[ ! -e "/usr/local/bin/${command}" ]]; then
    ln -s "$command_path" "/usr/local/bin/${command}"
  fi
done

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

runner_git() {
  runuser -u "$runner_user" -- env \
    HOME="$runner_home" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_SYSTEM=/dev/null \
    GIT_TERMINAL_PROMPT=0 \
    git -c core.hooksPath=/dev/null "$@"
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
# compare symlink targets/executable bits without trusting the runner's index.
/usr/bin/python3 - "$runtime_tree" "$repo_dir" <<'PY'
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
        if (source_details.st_mode & 0o111) != (target_details.st_mode & 0o111):
            raise SystemExit(f"runner executable mode differs from trusted commit: {relative}")
    else:
        raise SystemExit(f"trusted commit contains unsupported file type: {relative}")
PY

state_dir="/var/lib/degen-dogs"
tested_sha_path="${state_dir}/tested-main.sha"
install -d -o root -g root -m 0755 "$state_dir"
if [[ "$skip_bootstrap" == "1" && "$enable_now" == "1" ]]; then
  [[ -f "$tested_sha_path" && ! -L "$tested_sha_path" && "$(stat -c %U "$tested_sha_path")" == "root" ]] || \
    fail "no root-owned bootstrap test receipt exists for this checkout"
  [[ "$(tr -d '\r\n' <"$tested_sha_path")" == "$expected_head" ]] || \
    fail "origin/main advanced after dependency/build tests; rerun the full Windows bootstrap"
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
  runuser -u "$runner_user" -- env \
    HOME="$runner_home" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_SYSTEM=/dev/null \
    GIT_TERMINAL_PROMPT=0 \
    NPM_CONFIG_CACHE="${lock_dir}/npm" \
    "$@"
}

# Do not expose scripts/runtime-bin until the venv exists: its python3 wrapper
# intentionally fails closed when the pinned interpreter is unavailable. All
# post-bootstrap tests and activation checks use this helper so npm subprocesses
# and Python entrypoints resolve the repository's pinned runtime.
run_as_runner_runtime() {
  [[ -x "${repo_dir}/.venv/bin/python3" ]] || \
    fail "pinned Python runtime is missing; rerun without --skip-bootstrap"
  runuser -u "$runner_user" -- env \
    HOME="$runner_home" \
    PATH="${repo_dir}/scripts/runtime-bin:/usr/local/bin:/usr/bin:/bin" \
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
    run_as_runner ssh-keygen -q -t ed25519 -a 100 -N '' \
      -C "degen-dogs-${runner_id}" -f "$deploy_key"
  fi
  [[ -f "$deploy_key" && ! -L "$deploy_key" && "$(stat -c %U "$deploy_key")" == "$runner_user" && \
    "$(stat -c %h "$deploy_key")" == "1" && "$(stat -c %a "$deploy_key")" == "600" ]] || \
    fail "deploy private key is missing, linked, or not mode 600"

  run_as_runner python3 - "$known_hosts" "$ssh_config" "$deploy_key" <<'PY'
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
  deploy_public_key="$(run_as_runner ssh-keygen -y -f "$deploy_key")"
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
  run_as_runner python3 - "$known_hosts" "$ssh_config" "$deploy_key" <<'PY'
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
  run_as_runner ssh-keygen -y -f "$deploy_key" >/dev/null || \
    fail "dedicated deploy private key is invalid"
  host_fingerprint="$(run_as_runner ssh-keygen -lf "$known_hosts" -E sha256 | awk '{print $2}')"
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

if [[ "$skip_bootstrap" != "1" ]]; then
  if [[ ! -x "${repo_dir}/.venv/bin/python3" ]]; then
    run_as_runner /usr/bin/python3 -m venv "${repo_dir}/.venv"
  fi
  run_as_runner "${repo_dir}/.venv/bin/python3" -m pip install \
    --require-hashes --only-binary=:all: -r "${repo_dir}/requirements.txt"
  run_as_runner_runtime npm --prefix "$repo_dir" ci --ignore-scripts
  run_as_runner_runtime npm --prefix "$repo_dir" run test:watcher
  run_as_runner_runtime npm --prefix "$repo_dir" run build
  receipt_tmp="$(mktemp "${state_dir}/.tested-main.sha.XXXXXX")"
  printf '%s\n' "$expected_head" >"$receipt_tmp"
  install -o root -g root -m 0644 "$receipt_tmp" "$tested_sha_path"
  rm -f -- "$receipt_tmp"
fi

render_template() {
  local source_path="$1"
  local target_path="$2"
  python3 - "$source_path" "$target_path" \
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
for name in degen-dogs-watcher.service degen-dogs-hourly.service degen-dogs-health.service; do
  render_template "${asset_dir}/config/systemd/${name}.in" "${unit_dir}/${name}"
done
for name in degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer degen-dogs-runner.target; do
  install -m 0644 "${asset_dir}/config/systemd/${name}" "${unit_dir}/${name}"
done
render_template "${asset_dir}/config/logrotate/degen-dogs-wsl.in" "/etc/logrotate.d/degen-dogs-wsl"
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "${asset_dir}/scripts/run_wsl_runner_anchor.sh" \
  /usr/local/libexec/degen-dogs-wsl-anchor

systemctl daemon-reload
systemd-analyze verify \
  "${unit_dir}/degen-dogs-watcher.service" \
  "${unit_dir}/degen-dogs-watcher.timer" \
  "${unit_dir}/degen-dogs-hourly.service" \
  "${unit_dir}/degen-dogs-hourly.timer" \
  "${unit_dir}/degen-dogs-health.service" \
  "${unit_dir}/degen-dogs-health.timer" \
  "${unit_dir}/degen-dogs-runner.target"
logrotate --debug /etc/logrotate.d/degen-dogs-wsl >/dev/null

if [[ "$enable_now" == "1" ]]; then
  tracked_status="$(run_as_runner_runtime git -C "$repo_dir" status --porcelain --untracked-files=all)"
  [[ -z "$tracked_status" ]] || fail "non-ignored worktree changes must be committed or removed before enabling auto-push"
  validate_runner_git_destination
  [[ "$(run_as_runner_runtime git -C "$repo_dir" config --local --get core.hooksPath)" == "/dev/null" ]] || \
    fail "Git hooks must remain disabled for the publisher clone"
  [[ -n "$(run_as_runner_runtime git -C "$repo_dir" config --local --get user.name)" ]] || \
    fail "repo-local Git user.name is missing"
  [[ -n "$(run_as_runner_runtime git -C "$repo_dir" config --local --get user.email)" ]] || \
    fail "repo-local Git user.email is missing"
  current_branch="$(run_as_runner_runtime git -C "$repo_dir" branch --show-current)"
  [[ "$current_branch" == "main" ]] || fail "publisher clone must be on main, not ${current_branch:-detached HEAD}"
  run_as_runner_runtime git -C "$repo_dir" fetch origin \
    refs/heads/main:refs/remotes/origin/main
  local_head="$(run_as_runner_runtime git -C "$repo_dir" rev-parse HEAD)"
  remote_head="$(run_as_runner_runtime git -C "$repo_dir" rev-parse refs/remotes/origin/main)"
  [[ "$local_head" == "$remote_head" ]] || \
    fail "local main must exactly match origin/main before activation; fast-forward it and rerun"
  run_as_runner_runtime /bin/bash "${repo_dir}/scripts/test_refresh_and_publish.sh" || \
    fail "publisher collision/recovery regression failed on the exact activation commit"

  # Parse .env.local through the data-only loader, prove the configured
  # cross-provider quorum, and run one read-only watcher sample. The preflight
  # never writes watcher state or invokes a refresh/push command.
  run_as_runner_runtime env \
    DEGEN_DOGS_REPO_DIR="$repo_dir" \
    DEGEN_DOGS_ENV_FILE="$env_file" \
    DEGEN_DOGS_LOG_DIR="$log_dir" \
    DEGEN_DOGS_LOCK_DIR="$lock_dir" \
    /bin/bash -p "${repo_dir}/scripts/run_wsl_runner_job.sh" preflight

  ls_remote_main="$(run_as_runner_runtime git -C "$repo_dir" ls-remote --exit-code origin refs/heads/main)" || \
    fail "noninteractive GitHub authentication or network access is unavailable"
  [[ "${ls_remote_main%%[[:space:]]*}" == "$remote_head" ]] || \
    fail "origin/main advanced during activation; rerun the bootstrap against the new exact commit"
  run_as_runner_runtime git -C "$repo_dir" push --dry-run origin HEAD:refs/heads/main >/dev/null || \
    fail "GitHub write authentication, branch policy, or fast-forward state rejected a dry-run push"

  if ! systemctl enable \
    degen-dogs-watcher.timer \
    degen-dogs-hourly.timer \
    degen-dogs-health.timer \
    degen-dogs-runner.target; then
    systemctl disable --now \
      degen-dogs-runner.target \
      degen-dogs-watcher.timer \
      degen-dogs-hourly.timer \
      degen-dogs-health.timer >/dev/null 2>&1 || true
    systemctl stop \
      degen-dogs-watcher.service \
      degen-dogs-hourly.service \
      degen-dogs-health.service >/dev/null 2>&1 || true
    fail "systemd enable failed and all runner timers were disabled again"
  fi
  printf 'systemd units enabled behind the absent activation marker; the Windows bootstrap must verify its keepalive before publication can start\n'
else
  systemctl disable --now \
    degen-dogs-runner.target \
    degen-dogs-watcher.timer \
    degen-dogs-hourly.timer \
    degen-dogs-health.timer >/dev/null 2>&1 || true
  systemctl stop \
    degen-dogs-watcher.service \
    degen-dogs-hourly.service \
    degen-dogs-health.service >/dev/null 2>&1 || true
  printf 'units installed but disabled; rerun with --enable-now only after the race-safe publisher is on main and RPC/GitHub configuration is complete\n'
fi

if [[ -n "$deploy_public_key" ]]; then
  printf '\nAdd this PUBLIC key in GitHub repository Settings > Deploy keys with write access:\n%s\n\n' \
    "$deploy_public_key"
  printf 'The private key remains mode 600 inside the WSL runner account and was not displayed.\n'
fi
printf 'WSL runner assets installed for %s at %s\n' "$runner_user" "$repo_dir"
