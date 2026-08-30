#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]{1,48}$')]
    [string]$DistroName = 'DegenDogsRunner',

    [ValidatePattern('^[A-Za-z_][A-Za-z0-9_-]{0,30}$')]
    [string]$RunnerUser = 'degendogs',

    [ValidatePattern('^/[A-Za-z0-9._/-]+$')]
    [string]$RepoDir = '/srv/degen-dogs/repo',

    [ValidatePattern('^[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,62}[A-Za-z0-9_-])?$')]
    [string]$TaskName = 'Degen Dogs WSL Runner',

    [string]$TrustedInstallerCommit = '',
    [switch]$UpgradeTrustedBundle,
    [switch]$Activate,
    [switch]$AtLogOnOnly,
    [switch]$Uninstall,
    [System.Management.Automation.PSCredential]$Credential
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($RepoDir -match '(^|/)\.\.(/|$)' -or $RepoDir.StartsWith('/mnt/')) {
    throw 'RepoDir must be a normalized path on the WSL ext4 filesystem.'
}
if ($RepoDir -ne '/srv/degen-dogs/repo') {
    throw 'RepoDir is fixed at /srv/degen-dogs/repo so its parent can remain root-owned and non-writable.'
}
if ($RunnerUser -eq 'root') {
    throw 'RunnerUser must be an unprivileged dedicated account, never root.'
}
if (-not $TrustedInstallerCommit) {
    throw 'Every privileged install, activation, or uninstall requires -TrustedInstallerCommit with the exact reviewed commit.'
}
if ($TrustedInstallerCommit -notmatch '^[0-9a-f]{40}$') {
    throw 'TrustedInstallerCommit must be an exact lowercase 40-character reviewed Git SHA-1.'
}
if ($UpgradeTrustedBundle -and -not $TrustedInstallerCommit) {
    throw '-UpgradeTrustedBundle requires -TrustedInstallerCommit with the exact reviewed commit.'
}
if ($Uninstall -and $UpgradeTrustedBundle) {
    throw '-Uninstall cannot be combined with -UpgradeTrustedBundle.'
}
if ($TaskName -match '[\x00-\x1f]') {
    throw 'TaskName contains a control character.'
}

function Invoke-CheckedGit {
    param(
        [Parameter(Mandatory)][string]$GitPath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$SingleLine
    )

    $output = @(& $GitPath @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $detail = ($output | ForEach-Object { $_.ToString() }) -join "`n"
        throw "Git source verification failed (exit=$LASTEXITCODE): $detail"
    }
    if ($SingleLine) {
        $lines = @(
            $output |
                ForEach-Object { $_.ToString().Trim() } |
                Where-Object { $_ }
        )
        if ($lines.Count -ne 1) {
            throw "Git source verification expected exactly one output line, found $($lines.Count)."
        }
        return $lines[0]
    }
    return $output
}

function Assert-TrustedBootstrapSource {
    param([Parameter(Mandatory)][string]$Commit)

    $gitCommand = Get-Command git.exe -CommandType Application -ErrorAction Stop
    $gitPath = $gitCommand.Source
    $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $stageName = 'degen-dogs-bootstrap-source-' + [Guid]::NewGuid().ToString('N')
    $stage = [IO.Path]::GetFullPath((Join-Path $temporaryRoot $stageName))
    if (-not $stage.StartsWith(
        (Join-Path $temporaryRoot 'degen-dogs-bootstrap-source-'),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Could not construct a bounded temporary source-verification directory.'
    }
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    try {
        Invoke-CheckedGit -GitPath $gitPath -Arguments @('init', '--bare', '--quiet', $stage) | Out-Null
        Invoke-CheckedGit -GitPath $gitPath -Arguments @(
            "--git-dir=$stage",
            'fetch',
            '--quiet',
            '--no-tags',
            '--force',
            'https://github.com/ael-dev3/Degen-Dogs-Mission-3.git',
            'refs/heads/main:refs/remotes/origin/main'
        ) | Out-Null

        $resolvedCommit = Invoke-CheckedGit `
            -GitPath $gitPath `
            -Arguments @("--git-dir=$stage", 'rev-parse', '--verify', "${Commit}^{commit}") `
            -SingleLine
        if (-not [String]::Equals($resolvedCommit, $Commit, [StringComparison]::Ordinal)) {
            throw 'TrustedInstallerCommit did not resolve to the exact requested commit object.'
        }
        Invoke-CheckedGit -GitPath $gitPath -Arguments @(
            "--git-dir=$stage",
            'merge-base',
            '--is-ancestor',
            $Commit,
            'refs/remotes/origin/main'
        ) | Out-Null

        $scriptObject = Invoke-CheckedGit `
            -GitPath $gitPath `
            -Arguments @(
                "--git-dir=$stage",
                'rev-parse',
                '--verify',
                "${Commit}:scripts/install_wsl_startup_task.ps1"
            ) `
            -SingleLine
        $scriptObjectType = Invoke-CheckedGit `
            -GitPath $gitPath `
            -Arguments @("--git-dir=$stage", 'cat-file', '-t', $scriptObject) `
            -SingleLine
        if (-not [String]::Equals($scriptObjectType, 'blob', [StringComparison]::Ordinal)) {
            throw 'The reviewed bootstrap path is not a Git blob.'
        }
        $localScriptObject = Invoke-CheckedGit `
            -GitPath $gitPath `
            -Arguments @('hash-object', '--no-filters', '--', $PSCommandPath) `
            -SingleLine
        if (-not [String]::Equals($localScriptObject, $scriptObject, [StringComparison]::Ordinal)) {
            throw 'The elevated bootstrap bytes do not match TrustedInstallerCommit.'
        }

        $scriptDirectory = Split-Path -Parent $PSCommandPath
        $localRootOutput = @(& $gitPath -C $scriptDirectory rev-parse --show-toplevel 2>$null)
        if ($LASTEXITCODE -eq 0 -and $localRootOutput.Count -eq 1) {
            $localRoot = [IO.Path]::GetFullPath($localRootOutput[0].ToString().Trim())
            $expectedScriptPath = [IO.Path]::GetFullPath(
                (Join-Path $localRoot 'scripts\install_wsl_startup_task.ps1')
            )
            $actualScriptPath = [IO.Path]::GetFullPath($PSCommandPath)
            if (-not [String]::Equals(
                $actualScriptPath,
                $expectedScriptPath,
                [StringComparison]::OrdinalIgnoreCase
            )) {
                throw 'The bootstrap must run from its tracked repository path or a verified archive.'
            }
            $localHead = Invoke-CheckedGit `
                -GitPath $gitPath `
                -Arguments @('-C', $localRoot, 'rev-parse', '--verify', 'HEAD') `
                -SingleLine
            if (-not [String]::Equals($localHead, $Commit, [StringComparison]::Ordinal)) {
                throw 'The local bootstrap checkout HEAD is not TrustedInstallerCommit.'
            }
            $trackedStatus = @(
                & $gitPath -C $localRoot status --porcelain=v1 --untracked-files=no 2>&1 |
                    ForEach-Object { $_.ToString() } |
                    Where-Object { $_ }
            )
            if ($LASTEXITCODE -ne 0) {
                throw 'Could not verify that the local bootstrap checkout is clean.'
            }
            if ($trackedStatus.Count -ne 0) {
                throw 'The local bootstrap checkout has tracked changes; use an exact detached checkout or verified archive.'
            }
        }
    }
    finally {
        if ([IO.Directory]::Exists($stage)) {
            [IO.Directory]::Delete($stage, $true)
        }
    }
}

# This detects accidental checkout/argument mismatches before host state is
# changed. It cannot make already-malicious local PowerShell trustworthy.
Assert-TrustedBootstrapSource -Commit $TrustedInstallerCommit

$wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
if (-not (Test-Path -LiteralPath $wsl)) {
    throw 'wsl.exe is unavailable. Enable Windows Subsystem for Linux first.'
}

function Get-WslDistros {
    $raw = (& $wsl --list --quiet 2>$null | Out-String) -replace "`0", ''
    return @($raw -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Invoke-WslRoot {
    param([Parameter(Mandatory)][string]$Script)
    & $wsl --distribution $DistroName --user root --exec /bin/bash -lc $Script
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE."
    }
}

function Invoke-WslRootSingleLine {
    param([Parameter(Mandatory)][string]$Script)

    $output = @(& $wsl --distribution $DistroName --user root --exec /bin/bash -lc $Script 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $detail = ($output | ForEach-Object { $_.ToString() }) -join "`n"
        throw "WSL attestation command failed with exit code $LASTEXITCODE`: $detail"
    }
    $lines = @(
        $output |
            ForEach-Object { $_.ToString().Trim() } |
            Where-Object { $_ }
    )
    if ($lines.Count -ne 1) {
        throw "WSL attestation expected exactly one output line, found $($lines.Count)."
    }
    return $lines[0]
}

function Assert-CurrentAccountCredential {
    param([Parameter(Mandatory)][System.Management.Automation.PSCredential]$Candidate)

    $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    try {
        $candidateAccount = [System.Security.Principal.NTAccount]::new($Candidate.UserName)
        $candidateSid = $candidateAccount.Translate([System.Security.Principal.SecurityIdentifier])
    }
    catch {
        $activationError = $_
        throw "Credential account '$($Candidate.UserName)' could not be resolved to a local Windows security identifier."
    }
    if ($candidateSid.Value -ne $currentSid.Value) {
        throw 'The scheduled-task credential must be for the current Windows account, because WSL distros are registered per user.'
    }
}

function Get-ExactScheduledTask {
    param([Parameter(Mandatory)][string]$Name)

    $escapedName = [WildcardPattern]::Escape($Name)
    $candidateTasks = @(
        Get-ScheduledTask `
            -TaskName $escapedName `
            -TaskPath '\' `
            -ErrorAction SilentlyContinue
    )
    $exactTasks = @(
        $candidateTasks | Where-Object {
            [String]::Equals($_.TaskName, $Name, [StringComparison]::Ordinal) -and
            [String]::Equals($_.TaskPath, '\', [StringComparison]::Ordinal)
        }
    )
    if ($candidateTasks.Count -ne $exactTasks.Count) {
        throw "Task Scheduler returned a non-exact root match for '$Name'."
    }
    if ($exactTasks.Count -gt 1) {
        throw "Multiple exact root Task Scheduler objects unexpectedly matched '$Name'."
    }
    if ($exactTasks.Count -eq 1) {
        return $exactTasks[0]
    }
    return $null
}

$trustedBundleAttestation = @'
set -Eeuo pipefail
bundle_root="${1:-/var/lib/degen-dogs/trusted-bundles}"
expected_owner="${2:-root}"
current_link="$bundle_root/current"
attestation_failed() { printf 'error: frozen bundle attestation failed: %s\n' "$1" >&2; exit 1; }
bundle_parent=$(dirname -- "$bundle_root")
test -d "$bundle_parent" && test ! -L "$bundle_parent" || attestation_failed 'unsafe bundle parent'
test "$(stat -c %U "$bundle_parent")" = "$expected_owner" || attestation_failed 'unsafe bundle-parent owner'
parent_mode=$(stat -c %a "$bundle_parent") || attestation_failed 'could not inspect bundle-parent permissions'
(( (8#$parent_mode & 0022) == 0 )) || attestation_failed 'bundle parent is group/world writable'
test -d "$bundle_root" && test ! -L "$bundle_root" || attestation_failed 'unsafe bundle root'
test "$(stat -c %U "$bundle_root")" = "$expected_owner" || attestation_failed 'unsafe bundle-root owner'
test "$(stat -c %a "$bundle_root")" = 700 || attestation_failed 'bundle root mode is not 0700'
test -L "$current_link" || attestation_failed 'current pointer is not a symbolic link'
test "$(stat -c %U "$current_link")" = "$expected_owner" || attestation_failed 'unsafe current-pointer owner'
bundle=$(readlink -f -- "$current_link") || attestation_failed 'could not resolve current pointer'
case "$bundle" in
  "$bundle_root"/*) ;;
  *) printf 'error: trusted bundle pointer escaped its root\n' >&2; exit 1 ;;
esac
trusted_commit=$(basename -- "$bundle")
[[ "$trusted_commit" =~ ^[0-9a-f]{40}$ ]] || attestation_failed 'invalid trusted commit name'
test "$bundle" = "$bundle_root/$trusted_commit" || attestation_failed 'non-canonical bundle target'
test -d "$bundle" && test ! -L "$bundle" || attestation_failed 'unsafe bundle target'
test "$(stat -c %U "$bundle")" = "$expected_owner" || attestation_failed 'unsafe bundle owner'
for metadata in TRUSTED_COMMIT ROOT_ASSETS.sha256; do
  test -f "$bundle/$metadata" && test ! -L "$bundle/$metadata" || attestation_failed "unsafe $metadata"
  test "$(stat -c %U "$bundle/$metadata")" = "$expected_owner" || attestation_failed "unsafe $metadata owner"
done
test "$(tr -d '\r\n' <"$bundle/TRUSTED_COMMIT")" = "$trusted_commit" || attestation_failed 'TRUSTED_COMMIT mismatch'
symlink_entry=''
symlink_entry=$(find "$bundle" -type l -print -quit) || attestation_failed 'could not inspect bundle links'
test -z "$symlink_entry" || attestation_failed 'bundle contains a symbolic link'
foreign_owner_entry=''
foreign_owner_entry=$(find "$bundle" ! -user "$expected_owner" -print -quit) || attestation_failed 'could not inspect bundle ownership'
test -z "$foreign_owner_entry" || attestation_failed 'bundle ownership is not trusted'
writable_entry=''
writable_entry=$(find "$bundle" -perm /022 -print -quit) || attestation_failed 'could not inspect bundle permissions'
test -z "$writable_entry" || attestation_failed 'bundle is group/world writable'
(cd "$bundle" && sha256sum --check --status ROOT_ASSETS.sha256) || attestation_failed 'asset digest mismatch'
printf '%s\n' "$trusted_commit"
'@

$distroAlreadyExists = (Get-WslDistros) -contains $DistroName
$trustedInstallerExists = $false
$installedTrustedCommit = ''
if ($distroAlreadyExists) {
    & $wsl --distribution $DistroName --user root --exec /usr/bin/test -x /usr/local/libexec/degen-dogs-wsl-installer
    $trustedInstallerExists = $LASTEXITCODE -eq 0
    & $wsl --distribution $DistroName --user root --exec /bin/bash -lc `
        'test -e /var/lib/degen-dogs/trusted-bundles/current || test -L /var/lib/degen-dogs/trusted-bundles/current'
    $trustedBundlePointerExists = $LASTEXITCODE -eq 0
    if ($trustedBundlePointerExists) {
        $installedTrustedCommit = Invoke-WslRootSingleLine -Script $trustedBundleAttestation
    }
    elseif ($trustedInstallerExists) {
        throw 'The privileged installer exists without an attestable frozen bundle pointer.'
    }
}
if ($installedTrustedCommit -and -not [String]::Equals(
    $installedTrustedCommit,
    $TrustedInstallerCommit,
    [StringComparison]::Ordinal
)) {
    if (-not $UpgradeTrustedBundle) {
        throw 'The installed frozen bundle does not match TrustedInstallerCommit; use the matching detached bootstrap or explicitly review and pass -UpgradeTrustedBundle.'
    }
}
$trustedBundleExists = [bool]($installedTrustedCommit -and $trustedInstallerExists)

if ($Uninstall) {
    $task = Get-ExactScheduledTask -Name $TaskName
    if ($task) {
        $task | Disable-ScheduledTask | Out-Null
        $task | Stop-ScheduledTask -ErrorAction SilentlyContinue
        $task | Unregister-ScheduledTask -Confirm:$false
    }
    if ((Get-WslDistros) -contains $DistroName) {
        $uninstallScript = @'
set -Eeuo pipefail
rm -f -- /var/lib/degen-dogs/activation-armed /run/degen-dogs/activation-enabled /run/degen-dogs/anchor-ready
systemctl disable --now degen-dogs-runner.target degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer >/dev/null 2>&1 || true
systemctl stop degen-dogs-watcher.service degen-dogs-hourly.service degen-dogs-health.service >/dev/null 2>&1 || true
for unit in degen-dogs-runner.target degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer degen-dogs-watcher.service degen-dogs-hourly.service degen-dogs-health.service; do
  if systemctl is-active --quiet "$unit"; then exit 1; fi
done
rm -f -- /etc/systemd/system/degen-dogs-{watcher,hourly,health}.{service,timer} /etc/systemd/system/degen-dogs-runner.target /etc/logrotate.d/degen-dogs-wsl /usr/local/libexec/degen-dogs-wsl-anchor /usr/local/libexec/degen-dogs-wsl-installer
systemctl daemon-reload
'@
        Invoke-WslRoot -Script $uninstallScript
        & $wsl --terminate $DistroName
    }
    Write-Host 'Startup task and WSL services removed. The distro, clone, keys, configuration, logs, and caches were preserved.'
    return
}

# Resolve all interactive input before disabling a healthy existing runner.
if ($Activate) {
    if ($AtLogOnOnly -and $Credential) {
        throw 'Do not supply -Credential with -AtLogOnOnly; the interactive fallback uses the current signed-in account.'
    }
    if (-not $Credential -and -not $AtLogOnOnly) {
        $Credential = Get-Credential `
            -UserName "$env:USERDOMAIN\$env:USERNAME" `
            -Message 'Windows password used only by Task Scheduler to run the WSL keepalive before login'
        if (-not $Credential) {
            throw 'Activation credential prompt was cancelled before any runner changes were made.'
        }
    }
    if ($Credential) {
        Assert-CurrentAccountCredential -Candidate $Credential
    }
}

# Stop any previous keepalive before changing WSL units. Otherwise its
# one-minute repair loop could restart timers while a new preflight is running.
$existingTask = Get-ExactScheduledTask -Name $TaskName
if ($existingTask) {
    $existingTask | Disable-ScheduledTask | Out-Null
    $existingTask | Stop-ScheduledTask -ErrorAction SilentlyContinue
}

if (-not $distroAlreadyExists) {
    Write-Host "Installing an isolated Ubuntu 24.04 WSL2 distro named $DistroName..."
    & $wsl --install Ubuntu-24.04 --name $DistroName --version 2 --no-launch
    if ($LASTEXITCODE -ne 0) {
        throw 'WSL distro installation failed. If Windows requested a reboot, reboot and rerun this script.'
    }
}
else {
    # Quiesce the old installation before apt, Git fast-forward, or unit-file
    # replacement. Stopping the Windows task alone does not stop Linux
    # processes that WSL/systemd already started.
$quiesce = @'
set -Eeuo pipefail
rm -f -- /var/lib/degen-dogs/activation-armed /run/degen-dogs/activation-enabled /run/degen-dogs/anchor-ready
for unit in degen-dogs-runner.target degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer; do
  systemctl disable --now "$unit" >/dev/null 2>&1 || true
done
for unit in degen-dogs-watcher.service degen-dogs-hourly.service degen-dogs-health.service; do
  systemctl stop "$unit" >/dev/null 2>&1 || true
done
for unit in degen-dogs-runner.target degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer degen-dogs-watcher.service degen-dogs-hourly.service degen-dogs-health.service; do
  if systemctl is-active --quiet "$unit"; then
    printf 'error: could not quiesce %s before runner upgrade\n' "$unit" >&2
    exit 1
  fi
done
'@
    try {
        Invoke-WslRoot -Script $quiesce
    }
    finally {
        # A hard distro boundary also removes any orphaned old anchor before a
        # clean systemd boot. The task is already disabled, so it cannot race
        # this restart.
        & $wsl --terminate $DistroName
        if ($LASTEXITCODE -ne 0) {
            throw "Could not terminate $DistroName after quiescing the old runner."
        }
    }
}

# Configure systemd before provisioning. Terminating only this distro avoids
# disrupting unrelated WSL workloads.
$wslConfig = @'
install -d -m 0755 /etc
tmp=$(mktemp)
printf '[boot]\nsystemd=true\n' > "$tmp"
install -o root -g root -m 0644 "$tmp" /etc/wsl.conf
rm -f "$tmp"
'@
Invoke-WslRoot -Script $wslConfig
& $wsl --terminate $DistroName
if ($LASTEXITCODE -ne 0) {
    throw "Could not restart $DistroName after enabling systemd."
}

$trustedBundleProvision = ''
if (-not $trustedBundleExists -or $UpgradeTrustedBundle) {
    $trustedBundleProvision = @'
(
  set -Eeuo pipefail
  umask 077
  trusted_commit='__TRUSTED_COMMIT__'
  bundle_root=/var/lib/degen-dogs/trusted-bundles
  bundle_target="$bundle_root/$trusted_commit"
  install -d -o root -g root -m 0700 /var/lib/degen-dogs "$bundle_root"
  stage=$(mktemp -d /var/lib/degen-dogs/.trusted-bundle.XXXXXX)
  cleanup() { case "$stage" in /var/lib/degen-dogs/.trusted-bundle.*) rm -rf -- "$stage" ;; esac; }
  trap cleanup EXIT
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0
  git -c core.hooksPath=/dev/null init --bare "$stage/repo.git"
  git -c core.hooksPath=/dev/null --git-dir="$stage/repo.git" fetch --no-tags \
    https://github.com/ael-dev3/Degen-Dogs-Mission-3.git \
    refs/heads/main:refs/remotes/origin/main
  git -c core.hooksPath=/dev/null --git-dir="$stage/repo.git" cat-file -e "${trusted_commit}^{commit}"
  git -c core.hooksPath=/dev/null --git-dir="$stage/repo.git" merge-base --is-ancestor \
    "$trusted_commit" refs/remotes/origin/main
  required=(
    scripts/install_wsl_runner.sh scripts/run_wsl_runner_anchor.sh
    config/wsl-runner.env.template config/logrotate/degen-dogs-wsl.in
    config/systemd/degen-dogs-watcher.service.in config/systemd/degen-dogs-watcher.timer
    config/systemd/degen-dogs-hourly.service.in config/systemd/degen-dogs-hourly.timer
    config/systemd/degen-dogs-health.service.in config/systemd/degen-dogs-health.timer
    config/systemd/degen-dogs-runner.target
  )
  mkdir "$stage/tree"
  git -c core.hooksPath=/dev/null --git-dir="$stage/repo.git" archive \
    "$trusted_commit" "${required[@]}" | tar -x -C "$stage/tree"
  for relative in "${required[@]}"; do
    test -f "$stage/tree/$relative" && test ! -L "$stage/tree/$relative"
  done
  printf '%s\n' "$trusted_commit" >"$stage/tree/TRUSTED_COMMIT"
  (cd "$stage/tree" && sha256sum "${required[@]}" >ROOT_ASSETS.sha256)
  chmod -R go-w "$stage/tree"
  if [ -e "$bundle_target" ]; then
    test -d "$bundle_target" && test ! -L "$bundle_target"
    test "$(tr -d '\r\n' <"$bundle_target/TRUSTED_COMMIT")" = "$trusted_commit"
    (cd "$bundle_target" && sha256sum --check --status ROOT_ASSETS.sha256)
  else
    mv "$stage/tree" "$bundle_target"
  fi
  link_tmp="${bundle_root}/.current.$$"
  ln -s "$bundle_target" "$link_tmp"
  mv -Tf "$link_tmp" "$bundle_root/current"
)
'@
    $trustedBundleProvision = $trustedBundleProvision.Replace('__TRUSTED_COMMIT__', $TrustedInstallerCommit)
}

$trustedWrapperProvision = @'
trusted_wrapper_provision() (
  set -Eeuo pipefail
  umask 077
  wrapper_root="${1:-/usr/local/libexec}"
  bundle_root="${2:-/var/lib/degen-dogs/trusted-bundles}"
  expected_owner="${3:-root}"
  expected_group="${4:-root}"
  wrapper_target="$wrapper_root/degen-dogs-wsl-installer"
  wrapper_failed() { printf 'error: privileged installer regeneration failed: %s\n' "$1" >&2; exit 1; }

  bundle=$(readlink -f -- "$bundle_root/current") || wrapper_failed 'could not resolve frozen bundle'
  trusted_commit=$(basename -- "$bundle")
  [[ "$trusted_commit" =~ ^[0-9a-f]{40}$ ]] || wrapper_failed 'invalid frozen-bundle commit'
  test "$bundle" = "$bundle_root/$trusted_commit" || wrapper_failed 'non-canonical frozen-bundle target'
  (cd "$bundle" && sha256sum --check --status ROOT_ASSETS.sha256) || wrapper_failed 'frozen-bundle digest mismatch'

  if [[ -e "$wrapper_root" || -L "$wrapper_root" ]]; then
    test -d "$wrapper_root" && test ! -L "$wrapper_root" || wrapper_failed 'unsafe privileged-installer parent'
  else
    install -d -m 0755 "$wrapper_root"
  fi
  test "$(stat -c %U "$wrapper_root")" = "$expected_owner" || wrapper_failed 'unsafe privileged-installer parent owner'
  test "$(stat -c %G "$wrapper_root")" = "$expected_group" || wrapper_failed 'unsafe privileged-installer parent group'
  wrapper_root_mode=$(stat -c %a "$wrapper_root") || wrapper_failed 'could not inspect privileged-installer parent mode'
  (( (8#$wrapper_root_mode & 0022) == 0 )) || wrapper_failed 'privileged-installer parent is group/world writable'

  if [[ -e "$wrapper_target" || -L "$wrapper_target" ]]; then
    if [[ ! -f "$wrapper_target" || -L "$wrapper_target" || \
      "$(stat -c %U "$wrapper_target")" != "$expected_owner" || \
      "$(stat -c %G "$wrapper_target")" != "$expected_group" || \
      "$(stat -c %a "$wrapper_target")" != "755" || \
      "$(stat -c %h "$wrapper_target")" != "1" ]]; then
      wrapper_failed 'unsafe pre-existing privileged installer'
    fi
  fi

  wrapper_tmp=$(mktemp "$wrapper_root/.degen-dogs-installer.XXXXXX")
  wrapper_expected=$(mktemp "$wrapper_root/.degen-dogs-expected.XXXXXX")
  cleanup_wrapper() {
    if [[ -n "$wrapper_tmp" ]]; then rm -f -- "$wrapper_tmp"; fi
    rm -f -- "$wrapper_expected"
  }
  trap cleanup_wrapper EXIT
  printf -v quoted_bundle_root '%q' "$bundle_root"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -Eeuo pipefail' \
    "bundle_root=$quoted_bundle_root" \
    'bundle=$(readlink -f -- "$bundle_root/current")' \
    'trusted_commit=$(basename -- "$bundle")' \
    '[[ "$trusted_commit" =~ ^[0-9a-f]{40}$ ]] || exit 78' \
    'test "$bundle" = "$bundle_root/$trusted_commit" || exit 78' \
    '(cd "$bundle" && sha256sum --check --status ROOT_ASSETS.sha256)' \
    'exec "$bundle/scripts/install_wsl_runner.sh" "$@"' >"$wrapper_tmp"
  chmod 0755 "$wrapper_tmp"
  test "$(stat -c %U "$wrapper_tmp")" = "$expected_owner" || wrapper_failed 'prepared wrapper owner mismatch'
  test "$(stat -c %G "$wrapper_tmp")" = "$expected_group" || wrapper_failed 'prepared wrapper group mismatch'
  test "$(stat -c %a "$wrapper_tmp")" = 755 || wrapper_failed 'prepared wrapper mode mismatch'
  test "$(stat -c %h "$wrapper_tmp")" = 1 || wrapper_failed 'prepared wrapper has multiple hard links'
  cp --preserve=mode -- "$wrapper_tmp" "$wrapper_expected"
  cmp -s "$wrapper_tmp" "$wrapper_expected" || wrapper_failed 'prepared wrapper byte copy mismatch'
  mv -Tf -- "$wrapper_tmp" "$wrapper_target"
  wrapper_tmp=''
  test -f "$wrapper_target" && test ! -L "$wrapper_target" || wrapper_failed 'regenerated wrapper is not a regular file'
  test "$(stat -c %U "$wrapper_target")" = "$expected_owner" || wrapper_failed 'regenerated wrapper owner mismatch'
  test "$(stat -c %G "$wrapper_target")" = "$expected_group" || wrapper_failed 'regenerated wrapper group mismatch'
  test "$(stat -c %a "$wrapper_target")" = 755 || wrapper_failed 'regenerated wrapper mode mismatch'
  test "$(stat -c %h "$wrapper_target")" = 1 || wrapper_failed 'regenerated wrapper has multiple hard links'
  cmp -s "$wrapper_expected" "$wrapper_target" || wrapper_failed 'wrapper bytes differ after trusted regeneration'
)
trusted_wrapper_provision "$@"
'@

# A fresh root-owned fetch supplies only a byte manifest and exact SHA for the
# unprivileged runtime checkout. Privileged assets always come from the frozen,
# operator-pinned bundle above.
$runtimeStage = @'
stage_runtime_and_install() (
  set -Eeuo pipefail
  umask 077
  runtime_stage=$(mktemp -d /run/degen-dogs-runtime.XXXXXX)
  cleanup() { case "$runtime_stage" in /run/degen-dogs-runtime.*) rm -rf -- "$runtime_stage" ;; esac; }
  trap cleanup EXIT
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0
  git -c core.hooksPath=/dev/null init --bare "$runtime_stage/repo.git"
  git -c core.hooksPath=/dev/null --git-dir="$runtime_stage/repo.git" fetch --no-tags \
    https://github.com/ael-dev3/Degen-Dogs-Mission-3.git \
    refs/heads/main:refs/heads/main
  runtime_sha=$(git -c core.hooksPath=/dev/null --git-dir="$runtime_stage/repo.git" rev-parse refs/heads/main)
  mkdir "$runtime_stage/tree"
  git -c core.hooksPath=/dev/null --git-dir="$runtime_stage/repo.git" archive "$runtime_sha" | \
    tar -x -C "$runtime_stage/tree"
  chmod -R go-w "$runtime_stage/tree"

  runner_home=$(getent passwd '__RUNNER_USER__' | cut -d: -f6)
  runner_git=(runuser -u '__RUNNER_USER__' -- env HOME="$runner_home" PATH=/usr/local/bin:/usr/bin:/bin \
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 \
    git -c core.hooksPath=/dev/null)
  test "$("${runner_git[@]}" -C '__REPO_DIR__' branch --show-current)" = main
  test -z "$("${runner_git[@]}" -C '__REPO_DIR__' status --porcelain)"
  origin_url=$("${runner_git[@]}" -C '__REPO_DIR__' remote get-url origin)
  case "$origin_url" in
    'https://github.com/ael-dev3/Degen-Dogs-Mission-3.git'|'git@github.com:ael-dev3/Degen-Dogs-Mission-3.git'|'git@github-degen-dogs:ael-dev3/Degen-Dogs-Mission-3.git') ;;
    *) printf 'error: runtime checkout has an unexpected origin\n' >&2; exit 1 ;;
  esac
  "${runner_git[@]}" -C '__REPO_DIR__' fetch --no-tags \
    https://github.com/ael-dev3/Degen-Dogs-Mission-3.git refs/heads/main
  test "$("${runner_git[@]}" -C '__REPO_DIR__' rev-parse FETCH_HEAD)" = "$runtime_sha"
  "${runner_git[@]}" -C '__REPO_DIR__' merge --ff-only "$runtime_sha"
  test "$("${runner_git[@]}" -C '__REPO_DIR__' rev-parse HEAD)" = "$runtime_sha"
  /usr/local/libexec/degen-dogs-wsl-installer \
    --repo-dir '__REPO_DIR__' --expected-head "$runtime_sha" \
    --runtime-tree "$runtime_stage/tree" "$@"
)
'@
$runtimeStage = $runtimeStage.Replace('__REPO_DIR__', $RepoDir)
$runtimeStage = $runtimeStage.Replace('__RUNNER_USER__', $RunnerUser)

$bootstrap = @"
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates coreutils curl git gnupg lsof logrotate openssh-client python3 python3-pip python3-venv tar

key_tmp=`$(mktemp)
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
  https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key --output "`$key_tmp"
nodesource_expected_fingerprint='6F71F525282841EEDAF851B42F59B5F99B1BE0B4'
nodesource_fingerprint=`$(gpg --batch --show-keys --with-colons "`$key_tmp" | awk -F: '`$1 == "fpr" { print `$10; exit }')
if [ "`$nodesource_fingerprint" != "`$nodesource_expected_fingerprint" ]; then
  rm -f "`$key_tmp"
  printf 'error: downloaded NodeSource signing key fingerprint mismatch\n' >&2
  exit 1
fi
gpg --batch --yes --dearmor --output /usr/share/keyrings/nodesource.gpg "`$key_tmp"
rm -f "`$key_tmp"
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main' \
  > /etc/apt/sources.list.d/nodesource.list
apt-get update
apt-get install -y --no-install-recommends nodejs
test "`$(node -p 'process.versions.node.split(`".`")[0]')" = 22

if ! id '$RunnerUser' >/dev/null 2>&1; then
  useradd --user-group --create-home --shell /bin/bash '$RunnerUser'
fi
test "`$(id -u '$RunnerUser')" != 0
test "`$(id -g '$RunnerUser')" != 0
test "`$(id -G '$RunnerUser')" = "`$(id -g '$RunnerUser')"
runner_group=`$(id -gn '$RunnerUser')
runner_home=`$(getent passwd '$RunnerUser' | cut -d: -f6)
if [ -e /srv/degen-dogs ]; then test -d /srv/degen-dogs && test ! -L /srv/degen-dogs; fi
install -d -o root -g root -m 0755 /srv/degen-dogs
if [ ! -d '$RepoDir/.git' ]; then
  install -d -o '$RunnerUser' -g "`$runner_group" -m 0755 '$RepoDir'
  runuser -u '$RunnerUser' -- env HOME="`$runner_home" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 \
    git -c core.hooksPath=/dev/null clone --origin origin \
    https://github.com/ael-dev3/Degen-Dogs-Mission-3.git '$RepoDir'
fi
test -d '$RepoDir' && test ! -L '$RepoDir'
test "`$(stat -c %U /srv/degen-dogs)" = root
test "`$(stat -c %a /srv/degen-dogs)" = 755
test "`$(stat -f -c %T '$RepoDir')" = 'ext2/ext3'
runuser -u '$RunnerUser' -- env HOME="`$runner_home" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null \
  git -c core.hooksPath=/dev/null -C '$RepoDir' config user.name 'Degen Dogs Windows Runner'
runuser -u '$RunnerUser' -- env HOME="`$runner_home" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null \
  git -c core.hooksPath=/dev/null -C '$RepoDir' config user.email 'degen-dogs-runner@users.noreply.github.com'

$trustedBundleProvision
$trustedWrapperProvision
$runtimeStage
stage_runtime_and_install
"@
Invoke-WslRoot -Script $bootstrap

$action = New-ScheduledTaskAction `
    -Execute $wsl `
    -Argument "--distribution $DistroName --user root --exec /usr/local/libexec/degen-dogs-wsl-anchor"
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -Disable `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$rollbackPublisher = @'
rollback_publisher() (
  set -Eeuo pipefail
  state_dir="${1:-/var/lib/degen-dogs}"
  runtime_dir="${2:-/run/degen-dogs}"
  rm -f -- "$state_dir/activation-armed" "$runtime_dir/activation-enabled" "$runtime_dir/anchor-ready"
  for marker in "$state_dir/activation-armed" "$runtime_dir/activation-enabled" "$runtime_dir/anchor-ready"; do
    if [[ -e "$marker" || -L "$marker" ]]; then
      printf 'error: activation rollback could not remove %s\n' "$marker" >&2
      return 1
    fi
  done
  rollback_failed=0
  enabled_units=(
    degen-dogs-runner.target
    degen-dogs-watcher.timer
    degen-dogs-hourly.timer
    degen-dogs-health.timer
  )
  service_units=(
    degen-dogs-watcher.service
    degen-dogs-hourly.service
    degen-dogs-health.service
  )
  all_units=("${enabled_units[@]}" "${service_units[@]}")
  for unit in "${enabled_units[@]}"; do
    if ! systemctl disable --now "$unit"; then
      printf 'error: activation rollback could not disable/stop %s\n' "$unit" >&2
      rollback_failed=1
    fi
  done
  for unit in "${service_units[@]}"; do
    if ! systemctl stop "$unit"; then
      printf 'error: activation rollback could not stop %s\n' "$unit" >&2
      rollback_failed=1
    fi
  done
  for unit in "${all_units[@]}"; do
    unit_state=''
    if ! unit_state=$(systemctl show --property=ActiveState --value "$unit"); then
      printf 'error: activation rollback could not inspect %s\n' "$unit" >&2
      rollback_failed=1
      continue
    fi
    if [[ "$unit_state" != "inactive" ]]; then
      printf 'error: activation rollback found %s in state %s\n' "$unit" "$unit_state" >&2
      rollback_failed=1
    fi
  done
  return "$rollback_failed"
)
rollback_publisher "$@"
'@

if ($Activate) {
    $registeredTask = $null
    if ($AtLogOnOnly) {
        $principal = New-ScheduledTaskPrincipal `
            -UserId "$env:USERDOMAIN\$env:USERNAME" `
            -LogonType Interactive `
            -RunLevel Limited
        $registeredTask = Register-ScheduledTask `
            -TaskName $TaskName `
            -TaskPath '\' `
            -Action $action `
            -Trigger @($startupTrigger, $logonTrigger, $watchdogTrigger) `
            -Settings $settings `
            -Principal $principal `
            -Description 'Keeps the Degen Dogs systemd publisher alive in WSL2; real jobs remain least-privilege Linux services.' `
            -Force
    }
    else {
        $plainPassword = $Credential.GetNetworkCredential().Password
        try {
            $registeredTask = Register-ScheduledTask `
                -TaskName $TaskName `
                -TaskPath '\' `
                -Action $action `
                -Trigger @($startupTrigger, $logonTrigger, $watchdogTrigger) `
                -Settings $settings `
                -User $Credential.UserName `
                -Password $plainPassword `
                -RunLevel Limited `
                -Description 'Keeps the Degen Dogs systemd publisher alive in WSL2; real jobs remain least-privilege Linux services.' `
                -Force
        }
        finally {
            $plainPassword = $null
        }
    }

    # Activation is intentionally last. It fails closed unless the checked-out
    # peer-aware publisher, RPC quorum, watcher dry-run, and Git write dry-run
    # all pass inside WSL.
    try {
        $activation = @"
set -Eeuo pipefail
$runtimeStage
stage_runtime_and_install --skip-bootstrap --enable-now
"@
        Invoke-WslRoot -Script $activation
        $registeredTask | Enable-ScheduledTask | Out-Null
        $registeredTask | Start-ScheduledTask
        $taskDeadline = (Get-Date).AddSeconds(30)
        do {
            $currentTask = Get-ExactScheduledTask -Name $TaskName
            if (-not $currentTask) {
                throw "The exact root WSL keepalive task '$TaskName' disappeared during activation."
            }
            $taskState = $currentTask.State
            if ($taskState -eq 'Running') {
                break
            }
            Start-Sleep -Seconds 1
        } while ((Get-Date) -lt $taskDeadline)
        if ($taskState -ne 'Running') {
            throw "The WSL keepalive task did not reach Running state (state=$taskState)."
        }
        $anchorReady = $false
        $anchorDeadline = (Get-Date).AddSeconds(30)
        do {
            & $wsl --distribution $DistroName --user root --exec /usr/bin/test -f /run/degen-dogs/anchor-ready
            if ($LASTEXITCODE -eq 0) {
                $anchorReady = $true
                break
            }
            Start-Sleep -Seconds 1
        } while ((Get-Date) -lt $anchorDeadline)
        if (-not $anchorReady) {
            throw 'The WSL keepalive task did not establish its bounded anchor-ready signal.'
        }
        $commitActivation = @'
set -Eeuo pipefail
for unit in degen-dogs-runner.target degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer; do
  systemctl is-enabled --quiet "$unit"
done
install -d -o root -g root -m 0755 /var/lib/degen-dogs /run/degen-dogs
armed_tmp=$(mktemp /var/lib/degen-dogs/.activation-armed.XXXXXX)
printf 'armed=1\n' >"$armed_tmp"
install -o root -g root -m 0644 "$armed_tmp" /var/lib/degen-dogs/activation-armed
rm -f -- "$armed_tmp"
active_tmp=$(mktemp /run/degen-dogs/.activation-enabled.XXXXXX)
printf 'active=1\n' >"$active_tmp"
install -o root -g root -m 0644 "$active_tmp" /run/degen-dogs/activation-enabled
rm -f -- "$active_tmp"
systemctl start degen-dogs-runner.target degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer
systemctl is-active --quiet degen-dogs-runner.target degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer
'@
        Invoke-WslRoot -Script $commitActivation
        $publisherReady = $false
        $publisherDeadline = (Get-Date).AddSeconds(30)
        do {
            $currentTask = Get-ExactScheduledTask -Name $TaskName
            if ($currentTask -and $currentTask.State -eq 'Running') {
                & $wsl --distribution $DistroName --user root --exec /bin/bash -lc `
                    'test -f /run/degen-dogs/anchor-ready && test -f /run/degen-dogs/activation-enabled && systemctl is-active --quiet degen-dogs-runner.target degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer'
                if ($LASTEXITCODE -eq 0) {
                    $publisherReady = $true
                    break
                }
            }
            Start-Sleep -Seconds 1
        } while ((Get-Date) -lt $publisherDeadline)
        if (-not $publisherReady) {
            throw 'The activation marker or publisher timers did not become healthy within 30 seconds.'
        }
        $currentTask = Get-ExactScheduledTask -Name $TaskName
        if (-not $currentTask) {
            throw "The exact root WSL keepalive task '$TaskName' disappeared after activation."
        }
        if ($currentTask.State -ne 'Running') {
            throw "The exact root WSL keepalive task stopped after activation (state=$($currentTask.State))."
        }
        & $wsl --distribution $DistroName --user root --exec /bin/bash -lc `
            'test -f /run/degen-dogs/anchor-ready && test -f /run/degen-dogs/activation-enabled && systemctl is-active --quiet degen-dogs-runner.target degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer'
        if ($LASTEXITCODE -ne 0) {
            throw 'The final activation liveness proof failed: the anchor, activation gate, or publisher units are no longer healthy.'
        }
        $currentTask | Get-ScheduledTaskInfo | Format-List LastRunTime,LastTaskResult,NextRunTime
    }
    catch {
        $activationError = $_
        $rollbackClean = $true
        $rollbackFailures = [Collections.Generic.List[string]]::new()
        try {
            $rollbackTask = Get-ExactScheduledTask -Name $TaskName
            if ($rollbackTask) {
                $rollbackTask | Disable-ScheduledTask | Out-Null
                $rollbackTask | Stop-ScheduledTask
            }
        }
        catch {
            $rollbackClean = $false
            $rollbackFailures.Add("Windows task disable/stop failed: $($_.Exception.Message)")
        }

        try {
            $taskRollbackDeadline = (Get-Date).AddSeconds(10)
            do {
                $verifiedRollbackTask = Get-ExactScheduledTask -Name $TaskName
                $taskStillEnabled = $false
                $taskStillRunning = $false
                if ($verifiedRollbackTask) {
                    $taskStillEnabled = [bool]$verifiedRollbackTask.Settings.Enabled
                    $taskStillRunning = $verifiedRollbackTask.State -eq 'Running'
                }
                if (-not $taskStillEnabled -and -not $taskStillRunning) {
                    break
                }
                Start-Sleep -Milliseconds 250
            } while ((Get-Date) -lt $taskRollbackDeadline)
            if ($taskStillEnabled) {
                $rollbackClean = $false
                $rollbackFailures.Add('rollback task remained enabled after disable.')
            }
            if ($taskStillRunning) {
                $rollbackClean = $false
                $rollbackFailures.Add('rollback task remained running after stop.')
            }
        }
        catch {
            $rollbackClean = $false
            $rollbackFailures.Add("Windows task rollback verification failed: $($_.Exception.Message)")
        }

        try {
            Invoke-WslRoot -Script $rollbackPublisher
        }
        catch {
            $rollbackClean = $false
            $rollbackFailures.Add("WSL publisher rollback or inactive-state verification failed: $($_.Exception.Message)")
        }

        if (-not $rollbackClean) {
            $taskBoundaryEstablished = $false
            # Retry the exact task boundary before terminating WSL. If the task
            # still cannot be proved disabled/stopped, remove only that exact
            # task so it cannot immediately recreate a persistent armed gate.
            try {
                $fallbackTask = Get-ExactScheduledTask -Name $TaskName
                if ($fallbackTask) {
                    try {
                        $fallbackTask | Disable-ScheduledTask | Out-Null
                        $fallbackTask | Stop-ScheduledTask
                    }
                    catch {
                        $rollbackFailures.Add("fallback task disable/stop failed: $($_.Exception.Message)")
                    }
                    $fallbackTask = Get-ExactScheduledTask -Name $TaskName
                    if ($fallbackTask -and (
                        [bool]$fallbackTask.Settings.Enabled -or
                        $fallbackTask.State -eq 'Running'
                    )) {
                        Write-Warning "The exact rollback task remained runnable; unregistering only '$TaskName' before WSL termination."
                        $fallbackTask | Unregister-ScheduledTask -Confirm:$false
                    }
                }
                $fallbackTask = Get-ExactScheduledTask -Name $TaskName
                if ($fallbackTask -and (
                    [bool]$fallbackTask.Settings.Enabled -or
                    $fallbackTask.State -eq 'Running'
                )) {
                    $rollbackFailures.Add('fallback task isolation failed: the exact task remains enabled or running.')
                }
                else {
                    $taskBoundaryEstablished = $true
                }
            }
            catch {
                $rollbackFailures.Add("fallback task isolation failed: $($_.Exception.Message)")
            }
            $preTerminationDetail = $rollbackFailures -join '; '
            Write-Warning "Activation rollback was not clean; terminating only '$DistroName' as the fail-closed WSL boundary. $preTerminationDetail"
            & $wsl --terminate $DistroName
            if ($LASTEXITCODE -ne 0) {
                $rollbackFailures.Add("fallback termination failed for '$DistroName' with exit code $LASTEXITCODE.")
            }
            else {
                if ($taskBoundaryEstablished) {
                    Write-Warning "Fallback termination stopped only the '$DistroName' distro; the disabled or removed exact task cannot recreate the runtime publication gate."
                }
                else {
                    Write-Warning "Fallback termination stopped '$DistroName', but exact Windows task isolation could not be established; manual Task Scheduler remediation is required before restart."
                }
            }
            $rollbackDetail = $rollbackFailures -join '; '
            $combinedMessage = "Activation failed and clean rollback could not be established. Original activation error: $($activationError.Exception.Message). Rollback: $rollbackDetail"
            throw [InvalidOperationException]::new($combinedMessage, $activationError.Exception)
        }
        throw $activationError
    }
}
else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited
    $registeredTask = Register-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath '\' `
        -Action $action `
        -Trigger @($startupTrigger, $logonTrigger, $watchdogTrigger) `
        -Settings $settings `
        -Principal $principal `
        -Description 'Disabled until the peer-aware publisher, RPC quorum, and GitHub deploy key pass preflight.' `
        -Force
    $registeredTask | Disable-ScheduledTask | Out-Null
    Write-Host "Bootstrap complete. The systemd units and Windows task are disabled."
    Write-Host "Add the displayed public deploy key to GitHub with write access, fill $RepoDir/.env.local, then rerun with -Activate."
}
