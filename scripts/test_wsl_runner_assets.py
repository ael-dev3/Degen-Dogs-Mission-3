#!/usr/bin/env python3
"""Static regression checks for the WSL2/systemd runner package."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def text(relative: str) -> str:
    path = ROOT / relative
    raw = path.read_bytes()
    assert b"\r\n" not in raw, f"{relative} must use LF line endings"
    return raw.decode("utf-8", errors="strict")


def powershell_literal_payload(source: str, variable: str) -> str:
    match = re.search(
        rf"(?ms)^\${re.escape(variable)}\s*=\s+@'\r?\n(?P<body>.*?)\r?\n'@\s*$",
        source,
    )
    assert match, f"missing literal PowerShell payload ${variable}"
    return match.group("body")


def run_bash(source: str, *, expected_returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["bash", "-s", "--"],
        input=source.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    assert result.returncode == expected_returncode, (
        f"bash returncode={result.returncode}, expected={expected_returncode}\n"
        f"stdout={result.stdout.decode('utf-8', errors='replace')}\n"
        f"stderr={result.stderr.decode('utf-8', errors='replace')}"
    )
    return result


def test() -> None:
    required = (
        ".gitattributes",
        "config/logrotate/degen-dogs-wsl.in",
        "config/systemd/degen-dogs-watcher.service.in",
        "config/systemd/degen-dogs-watcher.timer",
        "config/systemd/degen-dogs-hourly.service.in",
        "config/systemd/degen-dogs-hourly.timer",
        "config/systemd/degen-dogs-health.service.in",
        "config/systemd/degen-dogs-health.timer",
        "config/systemd/degen-dogs-runner.target",
        "config/wsl-runner.env.template",
        "docs/windows-wsl-runner.md",
        "scripts/check_wsl_runner_health.py",
        "scripts/install_wsl_runner.sh",
        "scripts/install_wsl_startup_task.ps1",
        "scripts/preflight_wsl_rpc.py",
        "scripts/run_wsl_runner_anchor.sh",
        "scripts/run_wsl_runner_job.sh",
    )
    for relative in required:
        assert (ROOT / relative).is_file(), relative
        text(relative)

    attributes = text(".gitattributes")
    assert "*.sh text eol=lf" in attributes
    assert "*.service text eol=lf" in attributes
    assert "*.ps1 text eol=lf" in attributes
    assert "scripts/runtime-bin/* text eol=lf" in attributes

    watcher_timer = text("config/systemd/degen-dogs-watcher.timer")
    assert "OnCalendar=*-*-* *:*:07,22,37,52" in watcher_timer
    assert "Persistent=false" in watcher_timer
    hourly_timer = text("config/systemd/degen-dogs-hourly.timer")
    assert "OnCalendar=*-*-* *:59:00" in hourly_timer
    assert "Persistent=true" in hourly_timer

    for relative in (
        "config/systemd/degen-dogs-watcher.service.in",
        "config/systemd/degen-dogs-hourly.service.in",
        "config/systemd/degen-dogs-health.service.in",
    ):
        service = text(relative)
        assert "User=@RUNNER_USER@" in service
        assert "ProtectSystem=strict" in service
        assert "ProtectHome=read-only" in service
        assert "CapabilityBoundingSet=" in service
        assert "Restart=on-failure" in service
        assert "@LOCK_DIR@" in service
        assert "ConditionPathExists=/run/degen-dogs/activation-enabled" in service

    launcher = text("scripts/run_wsl_runner_job.sh")
    assert "MISSION3_WATCHER_AUTO_PUSH=1" in launcher
    assert "DEGEN_DOGS_RUN_MISSION3_ARCHIVE=0" in launcher
    assert "DEGEN_DOGS_RUN_MISSION3_ARCHIVE=1" in launcher
    assert "preflight_wsl_rpc.py" in launcher
    assert "MISSION3_WATCHER_LOG_PATH=-" in launcher
    assert "DEGEN_DOGS_REFRESH_LOCK_PATH" in launcher
    assert 'export DEGEN_DOGS_REFRESH_LOCK_PATH="${lock_dir}/refresh.lock"' in launcher
    assert "export DEGEN_DOGS_REMOTE=origin" in launcher
    assert "export DEGEN_DOGS_BRANCH=main" in launcher
    assert "export DEGEN_DOGS_SKIP_PUSH=0" in launcher
    assert "export DEGEN_DOGS_SKIP_PULL=0" in launcher
    assert 'export DEGEN_DOGS_RUNNER_ID="${DEGEN_DOGS_RUNNER_ID:-windows-wsl}"' in launcher
    assert "remote.origin.pushurl" in launcher

    installer = text("scripts/install_wsl_runner.sh")
    assert "Usage: /usr/local/libexec/degen-dogs-wsl-installer" in installer
    assert "--repo-dir is required and must be supplied" in installer
    assert 'python3 - "$runtime_tree"' in installer
    assert "subprocess.check_output" not in installer
    assert 'filesystem_type="$(stat -f -c %T "$repo_dir")"' in installer
    assert "test_refresh_and_publish.sh" in installer
    assert "--expected-head" in installer and "--runtime-tree" in installer
    assert '"$asset_dir" != "$repo_dir"' in installer
    assert '"$(id -u "$runner_user")" != "0"' in installer
    assert "runner checkout parent must be a root-owned" in installer
    assert "/var/lib/degen-dogs/activation-armed" in installer
    assert "git -C \"$repo_dir\" push --dry-run" in installer
    assert 'current_branch="$(run_as_runner_runtime git -C "$repo_dir" branch --show-current)"' in installer
    assert 'remote_head="$(run_as_runner_runtime git -C "$repo_dir" rev-parse refs/remotes/origin/main)"' in installer
    assert 'PATH="${repo_dir}/scripts/runtime-bin:/usr/local/bin:/usr/bin:/bin"' in installer
    assert 'run_as_runner /usr/bin/python3 -m venv' in installer
    assert '"%" in value' in installer
    assert "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" in installer
    assert "validate_runner_git_destination" in installer
    skip_deploy_marker = installer.index('if [[ "$skip_deploy_key" != "1" ]]')
    assert installer.index("config --unset-all remote.origin.pushurl") > skip_deploy_marker
    deploy_key_block = installer.split('if [[ "$skip_deploy_key" != "1" ]]', 1)[1]
    assert deploy_key_block.index("validate_runner_git_destination") > deploy_key_block.index("fi\n")

    validator_marker = "# WSL_SSH_MATERIAL_VALIDATOR"
    validator_marker_offset = installer.index(validator_marker)
    validator_start = installer.rfind("<<'PY'\n", 0, validator_marker_offset)
    assert validator_start >= 0
    validator_start += len("<<'PY'\n")
    validator_end = installer.index("\nPY", validator_marker_offset)
    validator = installer[validator_start:validator_end]
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        known_hosts_path = temporary / "known_hosts"
        config_path = temporary / "config"
        key_path = temporary / "deploy_key"
        known_hosts = (
            "github.com ssh-ed25519 "
            "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
        )
        config = f"""Host github-degen-dogs
    HostName github.com
    User git
    IdentityFile {key_path}
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking yes
    UserKnownHostsFile {known_hosts_path}
    GlobalKnownHostsFile /dev/null
    ProxyCommand none
    ProxyJump none
"""
        known_hosts_path.write_text(known_hosts, encoding="ascii", newline="\n")
        config_path.write_text(config, encoding="ascii", newline="\n")

        def validate_ssh_material() -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    validator,
                    str(known_hosts_path),
                    str(config_path),
                    str(key_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        accepted = validate_ssh_material()
        assert accepted.returncode == 0, accepted.stderr.decode("utf-8", errors="replace")
        mutations = {
            "HostName": config.replace("HostName github.com", "HostName attacker.invalid"),
            "IdentityFile": config.replace(str(key_path), str(temporary / "attacker_key")),
            "StrictHostKeyChecking": config.replace("StrictHostKeyChecking yes", "StrictHostKeyChecking no"),
            "UserKnownHostsFile": config.replace(str(known_hosts_path), str(temporary / "other_hosts")),
            "ProxyCommand": config.replace("ProxyCommand none", "ProxyCommand ssh attacker.invalid -W %h:%p"),
            "ProxyJump": config.replace("ProxyJump none", "ProxyJump attacker.invalid"),
        }
        for field, mutation in mutations.items():
            config_path.write_text(mutation, encoding="ascii", newline="\n")
            rejected = validate_ssh_material()
            assert rejected.returncode != 0, f"unsafe {field} mutation was accepted"
        config_path.write_text(config, encoding="ascii", newline="\n")
        known_hosts_path.write_text(
            known_hosts.replace("github.com", "attacker.invalid"),
            encoding="ascii",
            newline="\n",
        )
        rejected = validate_ssh_material()
        assert rejected.returncode != 0, "non-canonical known_hosts destination was accepted"
        known_hosts_path.write_text(
            known_hosts + "attacker.invalid ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEvil\n",
            encoding="ascii",
            newline="\n",
        )
        rejected = validate_ssh_material()
        assert rejected.returncode != 0, "extra known_hosts entry was accepted"
    for line in installer.splitlines():
        if re.search(r'\bgit\s+-C\s+"\$repo_dir"', line):
            assert "runner_git" in line or "run_as_runner_runtime" in line, line
    assert "systemctl disable --now" in installer
    assert "--uninstall" in installer

    powershell = text("scripts/install_wsl_startup_task.ps1")
    assert not powershell.startswith("#Requires -RunAsAdministrator")
    assert "function Assert-WslRunnerInvocationPolicy" in powershell
    assert "function Get-WslRunnerGitPath" in powershell
    assert "$gitPath = Get-WslRunnerGitPath" in powershell
    assert "function Remove-WslRunnerTemporaryGitDirectory" in powershell
    assert "Remove-WslRunnerTemporaryGitDirectory `" in powershell
    policy_gate = powershell.rindex("Assert-WslRunnerInvocationPolicy `")
    source_attestation = powershell.index(
        "Assert-TrustedBootstrapSource -Commit $TrustedInstallerCommit"
    )
    assert policy_gate < source_attestation
    assert "function Invoke-VerifiedWslImport" in powershell
    assert powershell.count("Invoke-VerifiedWslImport") >= 2
    assert "https://releases.ubuntu.com/24.04.4/ubuntu-24.04.4-wsl-amd64.wsl" in powershell
    assert "9b2f7730dc68227dd04a9f3e5eab86ad85caf556b8606ad94f1f29ff5c4fd3f5" in powershell
    assert "Get-WslRunnerImportArguments" in powershell
    assert "Get-WslRunnerKnownLocalAppData" in powershell
    assert "New-WslRunnerImportAttempt" in powershell
    assert "Enter-WslRunnerDistroLock" in powershell
    assert "Enter-WslRunnerTaskLock" in powershell
    assert "Invoke-WslRunnerImportRollback" in powershell
    assert "$listArguments = @('--list', '--all', '--quiet')" in powershell
    assert "--proto-redir '=https'" in powershell
    assert "--retry-all-errors" in powershell
    assert "is not Ubuntu 24.04 AMD64" in powershell
    assert "New-ScheduledTaskAction" in powershell
    assert "-Disable `" in powershell
    assert "degen-dogs-wsl-anchor" in powershell
    assert "-AtStartup" in powershell and "-AtLogOn" in powershell
    assert "-RepetitionInterval (New-TimeSpan -Minutes 5)" in powershell
    assert "$AtLogOnOnly" in powershell
    assert "Get-WslRunnerTriggerKinds" in powershell
    assert powershell.count("-Trigger $selectedTriggers.ToArray()") == 3
    registration_blocks = powershell.split("Register-ScheduledTask `")[1:]
    assert len(registration_blocks) == 3
    for block in registration_blocks:
        assert "-Force" not in block.split("\n        }", 1)[0]
    assert powershell.count("-PrepareAction $isolateRegisteredTaskAction") == 2
    assert "-Trigger @($startupTrigger" not in powershell
    assert "Export-ScheduledTask" in powershell
    assert powershell.count("Assert-WslRunnerOwnedTaskDefinition") >= 6
    assert "function Assert-WslRunnerManagedTaskXml" in powershell
    assert "-AllowManagedPredecessor" in powershell
    assert "Assert-CurrentAccountCredential" in powershell
    assert 'merge --ff-only "$runtime_sha"' in powershell
    assert "refs/heads/main" in powershell
    assert "could not quiesce %s before runner upgrade" in powershell
    assert "systemctl is-active --quiet" in powershell
    assert "$UpgradeTrustedBundle" in powershell
    assert "TrustedInstallerCommit" in powershell
    trust_required = powershell.index("if (-not $TrustedInstallerCommit)")
    source_check = powershell.index(
        "Assert-TrustedBootstrapSource -Commit $TrustedInstallerCommit"
    )
    wsl_initialization = powershell.index("$wsl = Join-Path")
    assert trust_required < source_check < wsl_initialization
    assert "if ($TrustedInstallerCommit)" not in powershell[:wsl_initialization]
    assert "$installedTrustedCommit" in powershell
    assert "The installed frozen bundle does not match TrustedInstallerCommit" in powershell
    assert "A trusted bundle is already installed; use -UpgradeTrustedBundle" not in powershell
    assert "$trustedWrapperProvision" in powershell
    assert "wrapper bytes differ after trusted regeneration" in powershell
    assert "unsafe pre-existing privileged installer" in powershell
    task_name_pattern_match = re.search(
        r"\[ValidatePattern\('([^']+)'\)\]\s*\[string\]\$TaskName",
        powershell,
    )
    assert task_name_pattern_match
    task_name_pattern = task_name_pattern_match.group(1)
    assert re.fullmatch(task_name_pattern, "Degen Dogs WSL Runner")
    for unsafe_task_name in (
        "*",
        "Degen Dogs*",
        "\\Degen Dogs",
        "Degen/Dogs",
        " Degen Dogs",
        "Degen Dogs ",
        "Degen Dogs?",
        "Degen[Dogs]",
    ):
        assert not re.fullmatch(task_name_pattern, unsafe_task_name), unsafe_task_name
    assert "function Get-ExactScheduledTask" in powershell
    task_lookup = powershell.split("function Get-ExactScheduledTask", 1)[1].split(
        "function Assert-WslRunnerOwnedTaskDefinition", 1
    )[0]
    assert "-TaskPath '\\'" in task_lookup
    assert "-ErrorAction Stop" in task_lookup
    assert "SilentlyContinue" not in task_lookup
    assert "[StringComparison]::OrdinalIgnoreCase" in task_lookup
    assert "[StringComparison]::Ordinal" in task_lookup
    assert not re.search(
        r"(?m)^\s*(?:Disable|Stop|Unregister|Enable|Start)-ScheduledTask\s+-TaskName\s+\$TaskName",
        powershell,
    )
    source_guard = powershell[:wsl_initialization]
    assert "hash-object" in source_guard and "--no-filters" in source_guard
    assert "'merge-base'" in source_guard and "'--is-ancestor'" in source_guard
    assert "refs/remotes/origin/main" in source_guard
    assert "ROOT_ASSETS.sha256" in powershell
    assert 'git -c core.hooksPath=/dev/null --git-dir="$stage/repo.git" archive' in powershell
    assert "/run/degen-dogs/anchor-ready" in powershell
    assert "catch {\n        $activationError = $_" in powershell
    activation_rollback = powershell.rsplit("catch {\n        $activationError = $_", 1)[1]
    assert "$rollbackClean" in activation_rollback
    assert "Invoke-CurrentWslRunnerTaskIsolation -Remove $true" in activation_rollback
    assert "Windows task isolation was unproven" in activation_rollback
    assert "function Invoke-WslRunnerTaskIsolation" in powershell
    assert "Final ownership attestation failed" in powershell
    assert "OperationAttempts" in powershell
    assert "exact Windows task isolation could not be established" in activation_rollback
    assert "--terminate $DistroName" in activation_rollback
    assert "fallback termination failed" in activation_rollback
    assert "Activation failed and clean rollback could not be established" in activation_rollback
    activation_success = powershell.split("if ($Activate) {", 1)[1].split("catch {", 1)[0]
    assert activation_success.count("/run/degen-dogs/anchor-ready") >= 2
    assert "if ($currentTask.State -ne 'Running')" in activation_success
    assert "The final activation liveness proof failed" in activation_success
    assert "6F71F525282841EEDAF851B42F59B5F99B1BE0B4" in powershell
    key_download = powershell.index("nodesource-repo.gpg.key")
    key_verify = powershell.index("--with-colons", key_download)
    key_trust = powershell.index("--dearmor", key_verify)
    apt_source = powershell.index("nodesource.list", key_trust)
    assert key_download < key_verify < key_trust < apt_source
    powershell_lines = powershell.splitlines()
    for index, line in enumerate(powershell_lines):
        if "git -c core.hooksPath=/dev/null" in line and ("'$RepoDir'" in line or "'$RepoDir/.git'" in line):
            context = "\n".join(powershell_lines[max(0, index - 2) : index + 1])
            assert "runuser -u '$RunnerUser'" in context, context
    embedded_payloads = re.findall(
        r"(?ms)=\s+@(?P<quote>['\"])(?:\r?\n)(?P<body>.*?)(?:\r?\n)(?P=quote)@",
        powershell,
    )
    assert len(embedded_payloads) == 11
    for index, (quote, payload) in enumerate(embedded_payloads, start=1):
        if quote == '"':
            payload = re.sub(r"`(.)", r"\1", payload)
        result = subprocess.run(
            ["bash", "-n"],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode == 0, (
            f"embedded Bash payload {index}: {result.stderr.decode('utf-8', errors='replace')}"
        )

    anchor = text("scripts/run_wsl_runner_anchor.sh")
    assert "activation-armed" in anchor and "activation-enabled" in anchor
    assert "systemctl is-enabled --quiet" in anchor
    anchor_regression = anchor + r'''

test_root=$(mktemp -d)
state_dir="$test_root/state"
runtime_dir="$test_root/run"
armed_marker="${state_dir}/activation-armed"
active_marker="${runtime_dir}/activation-enabled"
ready_marker="${runtime_dir}/anchor-ready"

id() {
  if [[ "${1:-}" == "-u" ]]; then printf '0\n'; return 0; fi
  command id "$@"
}
install() {
  if [[ "${1:-}" == "-d" ]]; then
    mkdir -p -- "${@: -2}"
    return 0
  fi
  local source="${@: -2:1}"
  local target="${@: -1}"
  cp -- "$source" "$target"
  chmod 0644 "$target"
}
stat() {
  case "${2:-}" in
    %U) printf 'root\n' ;;
    %h) printf '1\n' ;;
    %a) printf '644\n' ;;
    *) command stat "$@" ;;
  esac
}
systemctl() {
  case "${1:-}" in
    is-enabled) return 0 ;;
    is-active) return 1 ;;
    start) return 42 ;;
    *) return 43 ;;
  esac
}
mkdir -p "$state_dir" "$runtime_dir"
printf 'armed=1\n' >"$armed_marker"
chmod 0644 "$armed_marker"
set +e
( set -Eeuo pipefail; anchor_main )
anchor_status=$?
set -e
test "$anchor_status" = 42
test ! -e "$ready_marker"
test ! -e "$active_marker"
printf 'anchor-failure-cleanup-checked\n'
rm -rf -- "$test_root"
'''
    anchor_failure = run_bash(anchor_regression)
    assert anchor_failure.stdout == b"anchor-failure-cleanup-checked\n"

    attestation = powershell_literal_payload(powershell, "trustedBundleAttestation")
    attestation_regression = r'''
set -Eeuo pipefail
attest() (
''' + attestation + r'''
)
test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT
bundle_root="$test_root/trusted-bundles"
trusted_commit=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
bundle_target="$bundle_root/$trusted_commit"
mkdir -p "$bundle_target"
chmod 0700 "$test_root" "$bundle_root"
printf 'trusted asset\n' >"$bundle_target/asset"
printf '%s\n' "$trusted_commit" >"$bundle_target/TRUSTED_COMMIT"
(cd "$bundle_target" && sha256sum asset >ROOT_ASSETS.sha256)
ln -s "$bundle_target" "$bundle_root/current"
actual=$(attest "$bundle_root" "$(id -un)")
test "$actual" = "$trusted_commit"
chmod 0777 "$bundle_root"
if attest "$bundle_root" "$(id -un)" >/dev/null 2>&1; then
  printf 'writable frozen-bundle root passed attestation\n' >&2
  exit 88
fi
chmod 0700 "$bundle_root"
chmod 0777 "$test_root"
if attest "$bundle_root" "$(id -un)" >/dev/null 2>&1; then
  printf 'writable frozen-bundle parent passed attestation\n' >&2
  exit 89
fi
chmod 0700 "$test_root"
printf 'tampered\n' >>"$bundle_target/asset"
if attest "$bundle_root" "$(id -un)" >/dev/null 2>&1; then
  printf 'tampered frozen bundle passed attestation\n' >&2
  exit 90
fi
printf 'trusted asset\n' >"$bundle_target/asset"
find() { return 66; }
if attest "$bundle_root" "$(id -un)" >/dev/null 2>&1; then
  printf 'failed metadata traversal passed attestation\n' >&2
  exit 91
fi
'''
    run_bash(attestation_regression)

    wrapper_provision = powershell_literal_payload(powershell, "trustedWrapperProvision")
    wrapper_regression = r'''
set -Eeuo pipefail
provision_wrapper() (
''' + wrapper_provision + r'''
)
test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT
bundle_root="$test_root/trusted-bundles"
trusted_commit=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
bundle_target="$bundle_root/$trusted_commit"
wrapper_root="$test_root/libexec"
mkdir -p "$bundle_target/scripts"
chmod 0700 "$test_root" "$bundle_root"
cat >"$bundle_target/scripts/install_wsl_runner.sh" <<'INSTALLER'
#!/usr/bin/env bash
printf 'trusted-wrapper-executed\n'
INSTALLER
chmod 0755 "$bundle_target/scripts/install_wsl_runner.sh"
printf '%s\n' "$trusted_commit" >"$bundle_target/TRUSTED_COMMIT"
(cd "$bundle_target" && sha256sum scripts/install_wsl_runner.sh >ROOT_ASSETS.sha256)
ln -s "$bundle_target" "$bundle_root/current"
provision_wrapper "$wrapper_root" "$bundle_root" "$(id -un)" "$(id -gn)"
test -f "$wrapper_root/degen-dogs-wsl-installer"
test ! -L "$wrapper_root/degen-dogs-wsl-installer"
test "$(stat -c %a "$wrapper_root/degen-dogs-wsl-installer")" = 755
test "$("$wrapper_root/degen-dogs-wsl-installer")" = trusted-wrapper-executed
first_wrapper_inode=$(stat -c %i "$wrapper_root/degen-dogs-wsl-installer")
provision_wrapper "$wrapper_root" "$bundle_root" "$(id -un)" "$(id -gn)"
second_wrapper_inode=$(stat -c %i "$wrapper_root/degen-dogs-wsl-installer")
test "$second_wrapper_inode" != "$first_wrapper_inode"
rm -f "$wrapper_root/degen-dogs-wsl-installer"
ln -s "$test_root/attacker" "$wrapper_root/degen-dogs-wsl-installer"
if provision_wrapper "$wrapper_root" "$bundle_root" "$(id -un)" "$(id -gn)" >/dev/null 2>&1; then
  printf 'unsafe wrapper symlink was regenerated through\n' >&2
  exit 92
fi
'''
    run_bash(wrapper_regression)

    rollback = powershell_literal_payload(powershell, "rollbackPublisher")
    expected_rollback_calls = """disable --now degen-dogs-runner.target
disable --now degen-dogs-watcher.timer
disable --now degen-dogs-hourly.timer
disable --now degen-dogs-health.timer
stop degen-dogs-watcher.service
stop degen-dogs-hourly.service
stop degen-dogs-health.service
show --property=ActiveState --value degen-dogs-runner.target
show --property=ActiveState --value degen-dogs-watcher.timer
show --property=ActiveState --value degen-dogs-hourly.timer
show --property=ActiveState --value degen-dogs-health.timer
show --property=ActiveState --value degen-dogs-watcher.service
show --property=ActiveState --value degen-dogs-hourly.service
show --property=ActiveState --value degen-dogs-health.service
"""

    def rollback_regression(mode: str, expected_returncode: int) -> None:
        harness = r'''
set -Eeuo pipefail
test_root=$(mktemp -d)
calls="$test_root/calls"
state_dir="$test_root/state"
runtime_dir="$test_root/run"
mkdir -p "$state_dir" "$runtime_dir"
touch "$state_dir/activation-armed" "$runtime_dir/activation-enabled" "$runtime_dir/anchor-ready"
set -- "$state_dir" "$runtime_dir"
cat >"$test_root/expected" <<'EXPECTED_CALLS'
''' + expected_rollback_calls + r'''EXPECTED_CALLS
systemctl() {
  printf '%s\n' "$*" >>"$calls"
  if [[ "''' + mode + r'''" == "failure" && "$*" == "stop degen-dogs-hourly.service" ]]; then
    return 9
  fi
  if [[ "${1:-}" == "show" ]]; then
    if [[ "''' + mode + r'''" == "failure" && "${*: -1}" == "degen-dogs-hourly.service" ]]; then
      printf 'active\n'
    else
      printf 'inactive\n'
    fi
  fi
}
rollback_command() {
''' + rollback + r'''
}
set +e
rollback_command "$@"
status=$?
set -e
test ! -e "$state_dir/activation-armed" || status=91
test ! -e "$runtime_dir/activation-enabled" || status=92
test ! -e "$runtime_dir/anchor-ready" || status=93
if ! cmp -s "$calls" "$test_root/expected"; then
  diff -u "$test_root/expected" "$calls" >&2 || true
  status=94
fi
printf 'rollback-cleanup-checked status=%s\n' "$status"
rm -rf -- "$test_root"
exit "$status"
'''
        result = run_bash(harness, expected_returncode=expected_returncode)
        assert b"rollback-cleanup-checked" in result.stdout

    rollback_regression("success", 0)
    rollback_regression("failure", 1)

    runner_docs = text("docs/windows-wsl-runner.md")
    assert "& $bootstrapScript -TrustedInstallerCommit $trustedCommit -Activate -Credential $credential" in runner_docs
    assert "& $bootstrapScript -TrustedInstallerCommit $trustedCommit -AtLogOnOnly" in runner_docs
    assert "& $bootstrapScript -TrustedInstallerCommit $trustedCommit -Activate -AtLogOnOnly" in runner_docs
    assert "& $bootstrapScript -TrustedInstallerCommit $trustedCommit -Uninstall" in runner_docs
    assert "& $bootstrapScript -TrustedInstallerCommit $trustedCommit -AtLogOnOnly -Uninstall" in runner_docs
    assert "cannot recover\nwhile the user is signed out" in runner_docs

    preflight = text("scripts/preflight_wsl_rpc.py")
    ast.parse(preflight)
    assert "RARITY_MUTATION_TOPICS" in preflight
    assert "builder.DEGEN_DOGS" in preflight
    assert '"eth_getLogs"' in preflight
    assert "builder.fetch_dog_total_supply(snapshot_tag)" in preflight
    assert "builder.fetch_token_uri_bindings(" in preflight
    assert "[current_token, total_supply]" in preflight
    assert "block_hash=expected_hash" in preflight
    assert "current_present_next_nonexistent" in preflight
    report_source = preflight.split("report = {", 1)[1]
    assert '"token_uri"' not in report_source
    health = text("scripts/check_wsl_runner_health.py")
    health_tree = ast.parse(health)
    assert "current_dog_token_id" in health
    assert "terminal_publication_problem(latest_terminal)" in health
    selected = [
        node
        for node in health_tree.body
        if isinstance(node, (ast.Assign, ast.FunctionDef))
        and (
            isinstance(node, ast.FunctionDef)
            and node.name == "terminal_publication_problem"
            or isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "PUBLISHED_RESULTS" for target in node.targets)
        )
    ]
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "check_wsl_runner_health.py", "exec"), namespace)
    publication_problem = namespace["terminal_publication_problem"]
    assert callable(publication_problem)
    assert publication_problem({"result": "success_pushed"}) == ""
    assert publication_problem({"result": "success_pushed_live_timeout"}) == ""
    assert publication_problem({"result": "success_superseded_by_peer"}) == ""
    assert publication_problem({"result": "success_no_diff"}) == ""
    assert "failed" in publication_problem({"result": "failed"})
    assert "not published" in publication_problem({"result": "success_generated"})
    assert "not published" in publication_problem({"result": "success_skip_push"})
    assert "missing" in publication_problem({})

    runner_env = text("config/wsl-runner.env.template")
    assert "MISSION3_LOG_QUORUM_MAX_BLOCKS=500" in runner_env

    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    scripts = package["scripts"]
    assert scripts["refresh:live-snapshot"] == "python3 scripts/build_live_snapshot_bundle.py"
    assert scripts["test:live-snapshot"] == "python3 scripts/test_build_live_snapshot_bundle.py"
    assert "test:pages-validation-runner" in scripts["test:dashboard"]
    assert scripts["test:wsl-runner-assets"] == "python3 scripts/test_wsl_runner_assets.py"
    assert scripts["test:wsl-windows-policy"] == "python3 scripts/test_wsl_runner_windows_policy.py"
    assert "test:wsl-windows-policy" in scripts["test:ops"]

    runner_env_loader = (ROOT / "scripts" / "load_runner_env.sh").read_text(encoding="utf-8")
    production_allowlist = runner_env_loader.split("DEGEN_DOGS_RUNNER_COMMON_ENV_ALLOWLIST='", 1)[1].split("'", 1)[0]
    assert production_allowlist.count("DEGEN_DOGS_RUNNER_ID") == 1
    assert "MISSION3_LOG_QUORUM_MAX_BLOCKS" in production_allowlist
    assert "DEGEN_DOGS_HEALTH_REFRESH_RETRY_BASE_SECONDS" in runner_env_loader
    assert "DEGEN_DOGS_HEALTH_REFRESH_RETRY_MAX_SECONDS" in runner_env_loader

    print(f"wsl_runner_asset_tests=pass count={len(required)}")


if __name__ == "__main__":
    test()
