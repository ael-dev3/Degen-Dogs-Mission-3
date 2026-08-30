#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]{1,48}$')]
    [string]$DistroName = 'DegenDogsRunner',

    [ValidatePattern('^[A-Za-z_][A-Za-z0-9_-]{0,30}$')]
    [string]$RunnerUser = 'degendogs',

    [ValidatePattern('^/[A-Za-z0-9._/-]+$')]
    [string]$RepoDir = '/srv/degen-dogs/repo',

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
if ($TrustedInstallerCommit -and $TrustedInstallerCommit -notmatch '^[0-9a-f]{40}$') {
    throw 'TrustedInstallerCommit must be an exact lowercase 40-character reviewed Git SHA-1.'
}
if ($UpgradeTrustedBundle -and -not $TrustedInstallerCommit) {
    throw '-UpgradeTrustedBundle requires -TrustedInstallerCommit with the exact reviewed commit.'
}
if ($TaskName -match '[\x00-\x1f]') {
    throw 'TaskName contains a control character.'
}

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

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
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

$distroAlreadyExists = (Get-WslDistros) -contains $DistroName
$trustedBundleExists = $false
if ($distroAlreadyExists) {
    & $wsl --distribution $DistroName --user root --exec /usr/bin/test -x /usr/local/libexec/degen-dogs-wsl-installer
    $trustedBundleExists = $LASTEXITCODE -eq 0
}
if (-not $trustedBundleExists -and -not $TrustedInstallerCommit) {
    throw 'First install requires -TrustedInstallerCommit with an exact operator-reviewed commit.'
}
if ($trustedBundleExists -and $TrustedInstallerCommit -and -not $UpgradeTrustedBundle) {
    throw 'A trusted bundle is already installed; use -UpgradeTrustedBundle for an explicit privileged-asset update.'
}

# Stop any previous keepalive before changing WSL units. Otherwise its
# one-minute repair loop could restart timers while a new preflight is running.
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
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
  install -d -o root -g root -m 0755 /usr/local/libexec
  wrapper_tmp=$(mktemp /usr/local/libexec/.degen-dogs-installer.XXXXXX)
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -Eeuo pipefail' \
    'bundle=$(readlink -f /var/lib/degen-dogs/trusted-bundles/current)' \
    '[[ "$bundle" =~ ^/var/lib/degen-dogs/trusted-bundles/[0-9a-f]{40}$ ]] || exit 78' \
    '(cd "$bundle" && sha256sum --check --status ROOT_ASSETS.sha256)' \
    'exec "$bundle/scripts/install_wsl_runner.sh" "$@"' >"$wrapper_tmp"
  install -o root -g root -m 0755 "$wrapper_tmp" /usr/local/libexec/degen-dogs-wsl-installer
  rm -f -- "$wrapper_tmp"
)
'@
    $trustedBundleProvision = $trustedBundleProvision.Replace('__TRUSTED_COMMIT__', $TrustedInstallerCommit)
}

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

if ($Activate) {
    if ($AtLogOnOnly) {
        $principal = New-ScheduledTaskPrincipal `
            -UserId "$env:USERDOMAIN\$env:USERNAME" `
            -LogonType Interactive `
            -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger @($startupTrigger, $logonTrigger, $watchdogTrigger) `
            -Settings $settings `
            -Principal $principal `
            -Description 'Keeps the Degen Dogs systemd publisher alive in WSL2; real jobs remain least-privilege Linux services.' `
            -Force | Out-Null
    }
    else {
        $plainPassword = $Credential.GetNetworkCredential().Password
        try {
            Register-ScheduledTask `
                -TaskName $TaskName `
                -Action $action `
                -Trigger @($startupTrigger, $logonTrigger, $watchdogTrigger) `
                -Settings $settings `
                -User $Credential.UserName `
                -Password $plainPassword `
                -RunLevel Limited `
                -Description 'Keeps the Degen Dogs systemd publisher alive in WSL2; real jobs remain least-privilege Linux services.' `
                -Force | Out-Null
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
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        $taskDeadline = (Get-Date).AddSeconds(30)
        do {
            $taskState = (Get-ScheduledTask -TaskName $TaskName).State
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
            & $wsl --distribution $DistroName --user root --exec /bin/bash -lc `
                'test -f /run/degen-dogs/activation-enabled && systemctl is-active --quiet degen-dogs-runner.target degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer'
            if ($LASTEXITCODE -eq 0) {
                $publisherReady = $true
                break
            }
            Start-Sleep -Seconds 1
        } while ((Get-Date) -lt $publisherDeadline)
        if (-not $publisherReady) {
            throw 'The activation marker or publisher timers did not become healthy within 30 seconds.'
        }
        Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime,LastTaskResult,NextRunTime
    }
    catch {
        $activationError = $_
        try {
            Invoke-WslRoot -Script 'rm -f -- /var/lib/degen-dogs/activation-armed /run/degen-dogs/activation-enabled /run/degen-dogs/anchor-ready'
        }
        catch {
            Write-Warning 'Activation rollback could not remove WSL markers; disabling the Windows keepalive next.'
        }
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        try {
            Invoke-WslRoot -Script 'systemctl disable --now degen-dogs-runner.target degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer >/dev/null 2>&1 || true'
            Invoke-WslRoot -Script 'systemctl stop degen-dogs-watcher.service degen-dogs-hourly.service degen-dogs-health.service >/dev/null 2>&1 || true'
        }
        catch {
            Write-Warning 'Activation rollback could not reach WSL; the Windows keepalive remains disabled.'
        }
        throw $activationError
    }
}
else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger @($startupTrigger, $logonTrigger, $watchdogTrigger) `
        -Settings $settings `
        -Principal $principal `
        -Description 'Disabled until the peer-aware publisher, RPC quorum, and GitHub deploy key pass preflight.' `
        -Force | Out-Null
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Host "Bootstrap complete. The systemd units and Windows task are disabled."
    Write-Host "Add the displayed public deploy key to GitHub with write access, fill $RepoDir/.env.local, then rerun with -Activate."
}
