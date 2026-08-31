#!/usr/bin/env python3
"""Behavioral regression tests for the Windows WSL runner policy helpers."""

from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_wsl_startup_task.ps1"

ACTIVATION_UNITS = (
    "degen-dogs-runner.target",
    "degen-dogs-watcher.timer",
    "degen-dogs-hourly.timer",
    "degen-dogs-health.timer",
    "degen-dogs-publisher.path",
    "degen-dogs-publisher.timer",
    "degen-dogs-pages-verifier.path",
    "degen-dogs-pages-verifier.timer",
)
SERVICE_UNITS = (
    "degen-dogs-watcher.service",
    "degen-dogs-hourly.service",
    "degen-dogs-health.service",
    "degen-dogs-publisher.service",
    "degen-dogs-pages-verifier.service",
)
NEW_ASSETS = (
    "config/systemd/degen-dogs-publisher.service.in",
    "config/systemd/degen-dogs-publisher.path.in",
    "config/systemd/degen-dogs-publisher.timer",
    "config/systemd/degen-dogs-pages-verifier.service.in",
    "config/systemd/degen-dogs-pages-verifier.path.in",
    "config/systemd/degen-dogs-pages-verifier.timer",
    "scripts/runner_publication_state.py",
    "scripts/drain_publication_queue.py",
    "scripts/verify_pages_deployment.py",
)


def powershell() -> str:
    candidates = ("pwsh", "powershell") if os.name != "nt" else ("powershell.exe", "pwsh.exe")
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise AssertionError("PowerShell is required for Windows runner policy tests")


def ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def literal_payload(source: str, variable: str) -> str:
    match = re.search(
        rf"(?ms)^[ \t]*\${re.escape(variable)}\s*=\s+@'\r?\n(?P<body>.*?)\r?\n[ \t]*'@\s*$",
        source,
    )
    assert match, f"missing literal PowerShell payload ${variable}"
    return match.group("body")


def bash_array(source: str, variable: str) -> tuple[str, ...]:
    match = re.search(
        rf"(?ms)^[ \t]*{re.escape(variable)}=\(\s*(?P<body>.*?)^[ \t]*\)",
        source,
    )
    assert match, f"missing Bash array {variable}"
    return tuple(shlex.split(match.group("body"), comments=True, posix=True))


def activation_liveness_probes(source: str) -> tuple[str, ...]:
    return tuple(
        re.findall(
            r"(?m)^[ \t]*'(?P<probe>test -f /run/degen-dogs/anchor-ready.*?)'[ \t]*$",
            source,
        )
    )


def run_policy_harness(body: str) -> subprocess.CompletedProcess[str]:
    installer = ps_literal(str(INSTALLER))
    harness = rf"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
$tokens = $null
$parseErrors = $null
$installerAst = [System.Management.Automation.Language.Parser]::ParseFile(
    {installer},
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) {{
    throw "installer parse failed: $($parseErrors[0].Message)"
}}
$requiredFunctions = @(
    'Assert-WslRunnerInvocationPolicy',
    'Assert-WslRunnerDirectoryBoundary',
    'Enter-WslRunnerDistroLock',
    'Enter-WslRunnerTaskLock',
    'Exit-WslRunnerDistroLock',
    'Get-WslRunnerImportArguments',
    'Get-WslRunnerImportAttemptFromRegistration',
    'Get-WslRunnerGitPath',
    'Get-WslRunnerImageSpec',
    'Get-WslRunnerKnownLocalAppData',
    'Get-WslRunnerOwnershipMarkerValue',
    'Get-WslRunnerTaskResultReport',
    'Get-WslRunnerTaskRunningInstanceCount',
    'Get-WslRunnerTriggerKinds',
    'Get-WslDistros',
    'Get-ExactScheduledTask',
    'Initialize-WslRunnerInstallBase',
    'Invoke-VerifiedWslImport',
    'Invoke-WslRunnerImportRollback',
    'Invoke-WslRunnerTaskIsolation',
    'Invoke-WslRunnerTaskRegistrationTransaction',
    'New-WslRunnerImportAttempt',
    'New-WslRunnerTaskPlan',
    'Remove-BoundedWslImportDirectory',
    'Remove-WslRunnerTemporaryGitDirectory',
    'Resolve-WslRunnerUserSid',
    'Test-WslRunnerImportReceipt',
    'Test-WslDistroRegistrationMatches',
    'Test-WslDistroRegistrationPathMatches',
    'Assert-WslRunnerScheduledTaskXml',
    'Assert-WslRunnerManagedTaskXml'
)
foreach ($requiredFunction in $requiredFunctions) {{
    $definition = $installerAst.FindAll({{
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            [String]::Equals($node.Name, $requiredFunction, [StringComparison]::Ordinal)
    }}, $true)
    if ($definition.Count -ne 1) {{
        throw "expected exactly one production function named $requiredFunction, found $($definition.Count)"
    }}
    Invoke-Expression $definition[0].Extent.Text
}}
{body}
"""
    encoded = base64.b64encode(harness.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        [powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )


def assert_harness(body: str, marker: str) -> None:
    result = run_policy_harness(body)
    assert result.returncode == 0, (
        f"PowerShell policy harness failed with {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert marker in result.stdout, result.stdout


def test_invocation_policy_and_trigger_selection() -> None:
    assert_harness(
        r"""
Assert-WslRunnerInvocationPolicy -IsAdministrator $false -AtLogOnOnly $true -HasCredential $false
Assert-WslRunnerInvocationPolicy -IsAdministrator $true -AtLogOnOnly $false -HasCredential $false
$rejected = $false
try {
    Assert-WslRunnerInvocationPolicy -IsAdministrator $false -AtLogOnOnly $false -HasCredential $false
}
catch {
    $rejected = $true
}
if (-not $rejected) { throw 'non-admin unattended mode was accepted' }
$credentialRejected = $false
try {
    Assert-WslRunnerInvocationPolicy -IsAdministrator $false -AtLogOnOnly $true -HasCredential $true
}
catch {
    $credentialRejected = $true
}
if (-not $credentialRejected) { throw 'AtLogOnOnly accepted a credential' }

$perUserKinds = @(Get-WslRunnerTriggerKinds -AtLogOnOnly $true)
$unattendedKinds = @(Get-WslRunnerTriggerKinds -AtLogOnOnly $false)
if (($perUserKinds -join ',') -ne 'Logon,Watchdog') {
    throw "unexpected per-user triggers: $($perUserKinds -join ',')"
}
if (($unattendedKinds -join ',') -ne 'Startup,Logon,Watchdog') {
    throw "unexpected unattended triggers: $($unattendedKinds -join ',')"
}

$image = Get-WslRunnerImageSpec
if ($image.Uri -ne 'https://releases.ubuntu.com/24.04.4/ubuntu-24.04.4-wsl-amd64.wsl') {
    throw "unexpected Ubuntu image URI: $($image.Uri)"
}
if ($image.Sha256 -ne '9b2f7730dc68227dd04a9f3e5eab86ad85caf556b8606ad94f1f29ff5c4fd3f5') {
    throw "unexpected Ubuntu image digest: $($image.Sha256)"
}
Write-Output 'invocation-policy-checked'
""",
        "invocation-policy-checked",
    )


def test_git_command_resolution_is_deterministic() -> None:
    assert_harness(
        r"""
$firstPath = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) 'git-first.exe'))
$secondPath = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) 'git-second.exe'))
$resolved = Get-WslRunnerGitPath -ResolveAction {
    @(
        [PSCustomObject]@{ Source = $firstPath },
        [PSCustomObject]@{ Source = $secondPath }
    )
}
if (-not [String]::Equals($resolved, $firstPath, [StringComparison]::Ordinal)) {
    throw "Git resolution did not select the first PATH candidate: $resolved"
}

foreach ($badResolver in @(
    { @() },
    { @([PSCustomObject]@{ Source = '' }) }
)) {
    $rejected = $false
    try {
        Get-WslRunnerGitPath -ResolveAction $badResolver | Out-Null
    }
    catch {
        $rejected = $true
    }
    if (-not $rejected) { throw 'invalid Git command resolution was accepted' }
}
Write-Output 'git-command-resolution-checked'
""",
        "git-command-resolution-checked",
    )


def test_temporary_git_cleanup_is_bounded_and_handles_read_only_pack_files() -> None:
    assert_harness(
        r"""
$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$stage = Join-Path $temporaryRoot ('degen-dogs-bootstrap-source-' + [Guid]::NewGuid().ToString('N'))
$foreign = Join-Path $temporaryRoot ('foreign-bootstrap-source-' + [Guid]::NewGuid().ToString('N'))
try {
    $packDirectory = Join-Path $stage 'objects\pack'
    [IO.Directory]::CreateDirectory($packDirectory) | Out-Null
    $packFile = Join-Path $packDirectory 'pack-test.idx'
    [IO.File]::WriteAllText($packFile, 'read-only pack index')
    [IO.File]::SetAttributes($packFile, [IO.FileAttributes]::ReadOnly)
    Remove-WslRunnerTemporaryGitDirectory `
        -TemporaryRoot $temporaryRoot `
        -Stage $stage
    if ([IO.Directory]::Exists($stage)) {
        throw 'bounded Git stage with a read-only pack file was not removed'
    }

    [IO.Directory]::CreateDirectory($foreign) | Out-Null
    [IO.File]::WriteAllText((Join-Path $foreign 'keep.txt'), 'preserve')
    $foreignRejected = $false
    try {
        Remove-WslRunnerTemporaryGitDirectory `
            -TemporaryRoot $temporaryRoot `
            -Stage $foreign
    }
    catch {
        $foreignRejected = $true
    }
    if (-not $foreignRejected -or -not [IO.File]::Exists((Join-Path $foreign 'keep.txt'))) {
        throw 'temporary Git cleanup crossed its exact stage-name boundary'
    }
}
finally {
    if ([IO.Directory]::Exists($stage)) {
        Get-ChildItem -LiteralPath $stage -Force -Recurse | ForEach-Object { $_.IsReadOnly = $false }
        [IO.Directory]::Delete($stage, $true)
    }
    if ([IO.Directory]::Exists($foreign)) { [IO.Directory]::Delete($foreign, $true) }
}
Write-Output 'temporary-git-cleanup-checked'
""",
        "temporary-git-cleanup-checked",
    )


def test_task_plan_and_import_command_are_mode_complete() -> None:
    assert_harness(
        r"""
$sid = 'S-1-5-21-111-222-333-1001'
$wslPath = 'C:\WINDOWS\System32\wsl.exe'
$expectedArguments = '--distribution DegenDogsRunner --user root --exec /usr/local/libexec/degen-dogs-wsl-anchor'
foreach ($activate in @($false, $true)) {
    $plan = New-WslRunnerTaskPlan `
        -AtLogOnOnly $true `
        -Activate $activate `
        -UserSid $sid `
        -WslPath $wslPath `
        -DistroName 'DegenDogsRunner'
    if (($plan.TriggerKinds -join ',') -ne 'Logon,Watchdog') {
        throw "AtLogOnOnly plan contained unsafe triggers: $($plan.TriggerKinds -join ',')"
    }
    if ($plan.UserId -ne $sid -or $plan.LogonType -ne 'Interactive' -or $plan.RunLevel -ne 'Limited') {
        throw 'AtLogOnOnly plan did not retain the least-privilege current-user principal'
    }
    if ($plan.Executable -ne $wslPath -or $plan.Arguments -ne $expectedArguments) {
        throw 'AtLogOnOnly plan changed the exact WSL anchor action'
    }
    if ($plan.InitiallyEnabled) { throw 'task plan was not initially disabled' }
}

$unattended = New-WslRunnerTaskPlan `
    -AtLogOnOnly $false `
    -Activate $true `
    -UserSid $sid `
    -WslPath $wslPath `
    -DistroName 'DegenDogsRunner'
if (($unattended.TriggerKinds -join ',') -ne 'Startup,Logon,Watchdog') {
    throw 'unattended mode lost its boot trigger'
}
if ($unattended.LogonType -ne 'Password') {
    throw 'credential activation did not retain Password logon semantics'
}

$imagePath = Join-Path ([IO.Path]::GetTempPath()) 'verified-image.wsl'
$expectedLocation = Join-Path ([IO.Path]::GetTempPath()) 'DegenDogsRunner-attempt'
$importArguments = @(Get-WslRunnerImportArguments `
    -DistroName 'DegenDogsRunner' `
    -InstallLocation $expectedLocation `
    -ImagePath $imagePath)
$expectedImportArguments = @('--import', 'DegenDogsRunner', $expectedLocation, $imagePath, '--version', '2')
if (($importArguments -join "`n") -ne ($expectedImportArguments -join "`n")) {
    throw "unsafe WSL import arguments: $($importArguments -join ',')"
}
if ($importArguments -contains '--install') { throw 'per-user import planned wsl --install' }
Write-Output 'task-plan-import-command-checked'
""",
        "task-plan-import-command-checked",
    )


def test_task_result_reporting_requires_complete_healthy_suppression_state() -> None:
    assert_harness(
        r"""
$suppressedResult = [long]2147946720
$healthy = Get-WslRunnerTaskResultReport `
    -LastTaskResult $suppressedResult `
    -TaskEnabled $true `
    -TaskState 'Running' `
    -MultipleInstancesIgnoreNewAttested $true `
    -RunningInstanceCount 1 `
    -LinuxLivenessPassed $true
if (-not $healthy.HealthySuppression -or
    $healthy.Display -ne 'watchdog launch suppressed (healthy)') {
    throw 'complete healthy IgnoreNew state was not classified as a healthy suppression'
}
$signedHealthy = Get-WslRunnerTaskResultReport `
    -LastTaskResult ([long]-2147020576) `
    -TaskEnabled $true `
    -TaskState 'Running' `
    -MultipleInstancesIgnoreNewAttested $true `
    -RunningInstanceCount 1 `
    -LinuxLivenessPassed $true
if (-not $signedHealthy.HealthySuppression -or
    $signedHealthy.ResultCode -ne [uint32]2147946720) {
    throw 'signed Task Scheduler result did not normalize to 0x800710E0'
}

$unsafeStates = @(
    [PSCustomObject]@{ Name = 'different result'; Result = 1; Enabled = $true; State = 'Running'; IgnoreNew = $true; Instances = 1; Linux = $true },
    [PSCustomObject]@{ Name = 'disabled task'; Result = $suppressedResult; Enabled = $false; State = 'Running'; IgnoreNew = $true; Instances = 1; Linux = $true },
    [PSCustomObject]@{ Name = 'non-running task'; Result = $suppressedResult; Enabled = $true; State = 'Ready'; IgnoreNew = $true; Instances = 1; Linux = $true },
    [PSCustomObject]@{ Name = 'unattested policy'; Result = $suppressedResult; Enabled = $true; State = 'Running'; IgnoreNew = $false; Instances = 1; Linux = $true },
    [PSCustomObject]@{ Name = 'zero instances'; Result = $suppressedResult; Enabled = $true; State = 'Running'; IgnoreNew = $true; Instances = 0; Linux = $true },
    [PSCustomObject]@{ Name = 'multiple instances'; Result = $suppressedResult; Enabled = $true; State = 'Running'; IgnoreNew = $true; Instances = 2; Linux = $true },
    [PSCustomObject]@{ Name = 'failed Linux liveness'; Result = $suppressedResult; Enabled = $true; State = 'Running'; IgnoreNew = $true; Instances = 1; Linux = $false }
)
foreach ($state in $unsafeStates) {
    $report = Get-WslRunnerTaskResultReport `
        -LastTaskResult ([long]$state.Result) `
        -TaskEnabled ([bool]$state.Enabled) `
        -TaskState ([string]$state.State) `
        -MultipleInstancesIgnoreNewAttested ([bool]$state.IgnoreNew) `
        -RunningInstanceCount ([int]$state.Instances) `
        -LinuxLivenessPassed ([bool]$state.Linux)
    if ($report.HealthySuppression -or
        $report.Display -eq 'watchdog launch suppressed (healthy)') {
        throw "unsafe state suppressed the raw task result: $($state.Name)"
    }
    $expectedRaw = '0x{0:X8}' -f ([uint32]$state.Result)
    if ($report.Display -ne $expectedRaw) {
        throw "unsafe state did not preserve raw result $expectedRaw`: $($state.Name)"
    }
}

$instanceCount = Get-WslRunnerTaskRunningInstanceCount `
    -Name 'Degen Dogs WSL Runner' `
    -QueryAction { param($name) if ($name -ne 'Degen Dogs WSL Runner') { throw 'wrong task' }; 1 }
if ($instanceCount -ne 1) { throw 'validated instance query did not return one' }
foreach ($invalidCount in @(-1, 'unknown')) {
    $rejected = $false
    try {
        Get-WslRunnerTaskRunningInstanceCount `
            -Name 'Degen Dogs WSL Runner' `
            -QueryAction { param($name) $invalidCount } |
            Out-Null
    }
    catch {
        $rejected = $true
    }
    if (-not $rejected) { throw "invalid running-instance count was accepted: $invalidCount" }
}
Write-Output 'task-result-reporting-checked'
""",
        "task-result-reporting-checked",
    )


def test_verified_import_orders_hash_before_import_and_cleans_up() -> None:
    assert_harness(
        r"""
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ('degen-dogs-policy-test-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
try {
    $script:events = [Collections.Generic.List[string]]::new()
    $trustedBytes = [Text.Encoding]::UTF8.GetBytes("trusted-image`n")
    $downloadTrusted = {
        param($uri, $destination)
        $script:events.Add('download')
        [IO.File]::WriteAllBytes($destination, $trustedBytes)
    }
    $importRecorder = {
        param($name, $location, $imagePath)
        if (-not (Test-Path -LiteralPath $imagePath -PathType Leaf)) {
            throw 'verified image was removed before import'
        }
        $script:events.Add("import:$name")
    }
    $rollbackRecorder = {
        param($name, $location)
        $script:events.Add("rollback:$name")
    }
    Invoke-VerifiedWslImport `
        -ImageUri 'https://example.invalid/trusted.wsl' `
        -ExpectedSha256 '1f6209943cbabda1544bc111f805bf9c8b06c24d7c237a4f2ad2e5d3e134e6e9' `
        -TemporaryRoot $temporaryRoot `
        -DistroName 'PolicyTest' `
        -InstallLocation (Join-Path $temporaryRoot 'install') `
        -DownloadAction $downloadTrusted `
        -ImportAction $importRecorder `
        -RollbackAction $rollbackRecorder
    if (($script:events -join ',') -ne 'download,import:PolicyTest') {
        throw "unexpected successful import events: $($script:events -join ',')"
    }
    if (@(Get-ChildItem -LiteralPath $temporaryRoot -Force).Count -ne 0) {
        throw 'verified image temporary file was not removed after success'
    }

    $script:events.Clear()
    $downloadTampered = {
        param($uri, $destination)
        $script:events.Add('download')
        [IO.File]::WriteAllBytes($destination, [Text.Encoding]::UTF8.GetBytes("tampered`n"))
    }
    $hashRejected = $false
    try {
        Invoke-VerifiedWslImport `
            -ImageUri 'https://example.invalid/tampered.wsl' `
            -ExpectedSha256 '1f6209943cbabda1544bc111f805bf9c8b06c24d7c237a4f2ad2e5d3e134e6e9' `
            -TemporaryRoot $temporaryRoot `
            -DistroName 'PolicyTest' `
            -InstallLocation (Join-Path $temporaryRoot 'install') `
            -DownloadAction $downloadTampered `
            -ImportAction $importRecorder `
            -RollbackAction $rollbackRecorder
    }
    catch {
        $hashRejected = $true
    }
    if (-not $hashRejected) { throw 'tampered image digest was accepted' }
    if (($script:events -join ',') -ne 'download') {
        throw "import or rollback ran before a valid hash: $($script:events -join ',')"
    }
    if (@(Get-ChildItem -LiteralPath $temporaryRoot -Force).Count -ne 0) {
        throw 'tampered image temporary file was not removed'
    }

    $script:events.Clear()
    $importFailure = {
        param($name, $location, $imagePath)
        $script:events.Add("import:$name")
        throw 'synthetic import failure'
    }
    $failed = $false
    try {
        Invoke-VerifiedWslImport `
            -ImageUri 'https://example.invalid/trusted.wsl' `
            -ExpectedSha256 '1f6209943cbabda1544bc111f805bf9c8b06c24d7c237a4f2ad2e5d3e134e6e9' `
            -TemporaryRoot $temporaryRoot `
            -DistroName 'PolicyTest' `
            -InstallLocation (Join-Path $temporaryRoot 'install') `
            -DownloadAction $downloadTrusted `
            -ImportAction $importFailure `
            -RollbackAction $rollbackRecorder
    }
    catch {
        $failed = $true
    }
    if (-not $failed) { throw 'synthetic import failure was swallowed' }
    if (($script:events -join ',') -ne 'download,import:PolicyTest,rollback:PolicyTest') {
        throw "unexpected failed import events: $($script:events -join ',')"
    }
    if (@(Get-ChildItem -LiteralPath $temporaryRoot -Force).Count -ne 0) {
        throw 'verified image temporary file was not removed after import failure'
    }
}
finally {
    if ([IO.Directory]::Exists($temporaryRoot)) {
        [IO.Directory]::Delete($temporaryRoot, $true)
    }
}
Write-Output 'verified-import-checked'
""",
        "verified-import-checked",
    )


def test_wsl_inventory_fails_closed() -> None:
    assert_harness(
        r"""
$script:inventoryArguments = $null
$argumentProbe = @(Get-WslDistros -ListAction {
    param($arguments)
    $script:inventoryArguments = @($arguments)
    [PSCustomObject]@{ ExitCode = 0; Output = @() }
})
if (($script:inventoryArguments -join ',') -ne '--list,--all,--quiet') {
    throw "WSL inventory omitted transitional registrations: $($script:inventoryArguments -join ',')"
}

$failed = $false
try {
    Get-WslDistros -ListAction {
        [PSCustomObject]@{ ExitCode = 23; Output = @('synthetic list failure') }
    }
}
catch {
    $failed = $true
}
if (-not $failed) { throw 'nonzero WSL inventory was treated as an empty list' }

$inventory = @(Get-WslDistros -ListAction {
    [PSCustomObject]@{
        ExitCode = 0
        Output = @("Ubuntu-24.04`0", '', '  DegenDogsRunner  ')
    }
})
if (($inventory -join ',') -ne 'Ubuntu-24.04,DegenDogsRunner') {
    throw "successful WSL inventory was parsed incorrectly: $($inventory -join ',')"
}
Write-Output 'wsl-inventory-fail-closed-checked'
""",
        "wsl-inventory-fail-closed-checked",
    )


def test_import_claim_is_physical_unique_and_exclusive() -> None:
    assert_harness(
        r"""
$root = Join-Path ([IO.Path]::GetTempPath()) ('degen-dogs-claim-test-' + [Guid]::NewGuid().ToString('N'))
$knownLocal = Join-Path $root 'known-local'
[IO.Directory]::CreateDirectory($knownLocal) | Out-Null
$originalLocalAppData = $env:LOCALAPPDATA
$firstLock = $null
$secondLock = $null
$mixedCaseLock = $null
$taskLock = $null
$taskAliasLock = $null
$attempt = $null
try {
    $env:LOCALAPPDATA = (Join-Path $root 'untrusted-env-value')
    $basePlan = Initialize-WslRunnerInstallBase -KnownLocalAppData $knownLocal
    $expectedBase = [IO.Path]::GetFullPath((Join-Path (Join-Path $knownLocal 'DegenDogs') 'WSL'))
    if ($basePlan.Base -ne $expectedBase) {
        throw 'install base followed LOCALAPPDATA instead of the supplied known folder'
    }

    $firstLock = Enter-WslRunnerDistroLock `
        -KnownLocalAppData $knownLocal `
        -InstallBase $basePlan.Base `
        -UserSid 'S-1-5-21-111-222-333-1001' `
        -DistroName 'DegenDogsRunner'
    $lockRejected = $false
    try {
        $secondLock = Enter-WslRunnerDistroLock `
            -KnownLocalAppData $knownLocal `
            -InstallBase $basePlan.Base `
            -UserSid 'S-1-5-21-111-222-333-1001' `
            -DistroName 'DegenDogsRunner'
    }
    catch {
        $lockRejected = $true
    }
    if (-not $lockRejected) { throw 'same-user same-distro concurrent lock was accepted' }
    $mixedCaseRejected = $false
    try {
        $mixedCaseLock = Enter-WslRunnerDistroLock `
            -KnownLocalAppData $knownLocal `
            -InstallBase $basePlan.Base `
            -UserSid 'S-1-5-21-111-222-333-1001' `
            -DistroName 'degendogsrunner'
    }
    catch {
        $mixedCaseRejected = $true
    }
    if (-not $mixedCaseRejected) { throw 'mixed-case alias acquired a second distro lock' }
    Exit-WslRunnerDistroLock -Lock $firstLock
    $firstLock = $null
    $secondLock = Enter-WslRunnerDistroLock `
        -KnownLocalAppData $knownLocal `
        -InstallBase $basePlan.Base `
        -UserSid 'S-1-5-21-111-222-333-1001' `
        -DistroName 'DegenDogsRunner'
    Exit-WslRunnerDistroLock -Lock $secondLock
    $secondLock = $null

    $taskLock = Enter-WslRunnerTaskLock `
        -KnownLocalAppData $knownLocal `
        -InstallBase $basePlan.Base `
        -UserSid 'S-1-5-21-111-222-333-1001' `
        -TaskName 'Degen Dogs WSL Runner'
    $taskLockRejected = $false
    try {
        $taskAliasLock = Enter-WslRunnerTaskLock `
            -KnownLocalAppData $knownLocal `
            -InstallBase $basePlan.Base `
            -UserSid 'S-1-5-21-111-222-333-1001' `
            -TaskName 'degen dogs wsl runner'
    }
    catch {
        $taskLockRejected = $true
    }
    if (-not $taskLockRejected) { throw 'same-user logical task acquired a second lock' }
    Exit-WslRunnerDistroLock -Lock $taskLock
    $taskLock = $null

    $attempt = New-WslRunnerImportAttempt `
        -KnownLocalAppData $knownLocal `
        -DistroName 'DegenDogsRunner'
    if (-not $attempt.Location.StartsWith($expectedBase + [IO.Path]::DirectorySeparatorChar)) {
        throw 'attempt location escaped the known-folder install base'
    }
    if ((Split-Path -Leaf $attempt.Location) -ne "DegenDogsRunner-$($attempt.Id)") {
        throw 'attempt token was not bound into its unique location'
    }
    if (-not (Test-WslRunnerImportReceipt -Attempt $attempt)) {
        throw 'fresh attempt receipt could not prove ownership'
    }
    $markerValue = Get-WslRunnerOwnershipMarkerValue -AttemptId $attempt.Id
    if ($markerValue -ne "degen-dogs-windows-runner-v1:$($attempt.Id)") {
        throw 'Linux ownership marker was not bound to the host attempt token'
    }
    $registration = [PSCustomObject]@{
        Name = 'DegenDogsRunner'
        BasePath = $attempt.Location
        Version = 2
    }
    $recoveredAttempt = Get-WslRunnerImportAttemptFromRegistration `
        -Registration $registration `
        -KnownLocalAppData $knownLocal `
        -DistroName 'DegenDogsRunner'
    if ($recoveredAttempt.Id -ne $attempt.Id -or
        -not (Test-WslRunnerImportReceipt -Attempt $recoveredAttempt)) {
        throw 'existing registration did not recover its exact host attempt receipt'
    }
    $foreignRegistrationRejected = $false
    try {
        Get-WslRunnerImportAttemptFromRegistration `
            -Registration ([PSCustomObject]@{ Name = 'DegenDogsRunner'; BasePath = $root; Version = 2 }) `
            -KnownLocalAppData $knownLocal `
            -DistroName 'DegenDogsRunner' | Out-Null
    }
    catch {
        $foreignRegistrationRejected = $true
    }
    if (-not $foreignRegistrationRejected) {
        throw 'foreign existing registration was accepted without an attempt receipt'
    }

    $fakeItems = {
        param($path)
        if ((Split-Path -Leaf $path) -eq 'DegenDogs') {
            return [PSCustomObject]@{
                PSIsContainer = $true
                Attributes = [IO.FileAttributes]::Directory -bor [IO.FileAttributes]::ReparsePoint
            }
        }
        return [PSCustomObject]@{
            PSIsContainer = $true
            Attributes = [IO.FileAttributes]::Directory
        }
    }
    $reparseRejected = $false
    try {
        Assert-WslRunnerDirectoryBoundary `
            -Root $knownLocal `
            -Candidate $expectedBase `
            -GetItemAction $fakeItems | Out-Null
    }
    catch {
        $reparseRejected = $true
    }
    if (-not $reparseRejected) { throw 'reparse-point ancestor was accepted' }
}
finally {
    if ($firstLock) { Exit-WslRunnerDistroLock -Lock $firstLock }
    if ($secondLock) { Exit-WslRunnerDistroLock -Lock $secondLock }
    if ($mixedCaseLock) { Exit-WslRunnerDistroLock -Lock $mixedCaseLock }
    if ($taskLock) { Exit-WslRunnerDistroLock -Lock $taskLock }
    if ($taskAliasLock) { Exit-WslRunnerDistroLock -Lock $taskAliasLock }
    if ($attempt -and (Test-WslRunnerImportReceipt -Attempt $attempt)) {
        Remove-BoundedWslImportDirectory -Attempt $attempt
    }
    $env:LOCALAPPDATA = $originalLocalAppData
    if ([IO.Directory]::Exists($root)) { [IO.Directory]::Delete($root, $true) }
}
Write-Output 'import-claim-lock-boundary-checked'
""",
        "import-claim-lock-boundary-checked",
    )


def test_import_rollback_requires_exact_attempt_provenance() -> None:
    assert_harness(
        r"""
$knownLocal = Join-Path ([IO.Path]::GetTempPath()) ('degen-dogs-rollback-test-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($knownLocal) | Out-Null
$attemptA = New-WslRunnerImportAttempt -KnownLocalAppData $knownLocal -DistroName 'DegenDogsRunner'
$attemptB = New-WslRunnerImportAttempt -KnownLocalAppData $knownLocal -DistroName 'DegenDogsRunner'
try {
    $attemptA.ImportCommandSucceeded = $true
    $registrationB = [PSCustomObject]@{
        Name = 'DegenDogsRunner'
        BasePath = $attemptB.Location
        Version = 2
    }
    $script:events = [Collections.Generic.List[string]]::new()
    $differentAttemptRejected = $false
    try {
        Invoke-WslRunnerImportRollback `
            -Attempt $attemptA `
            -GetInventoryAction { @('DegenDogsRunner') } `
            -GetRegistrationAction { param($name) $registrationB } `
            -UnregisterAction { param($name) $script:events.Add("unregister:$name") } `
            -RemoveAction { param($attempt) $script:events.Add("remove:$($attempt.Id)") }
    }
    catch {
        $differentAttemptRejected = $true
    }
    if (-not $differentAttemptRejected) { throw 'rollback accepted another attempt registration' }
    if ($script:events.Count -ne 0) {
        throw "rollback mutated another attempt: $($script:events -join ',')"
    }

    $attemptA.ImportCommandSucceeded = $false
    $failedImportRejected = $false
    try {
        Invoke-WslRunnerImportRollback `
            -Attempt $attemptA `
            -GetInventoryAction { @('DegenDogsRunner') } `
            -GetRegistrationAction { param($name) $registrationB } `
            -UnregisterAction { param($name) $script:events.Add("unregister:$name") } `
            -RemoveAction { param($attempt) $script:events.Add("remove:$($attempt.Id)") }
    }
    catch {
        $failedImportRejected = $true
    }
    if (-not $failedImportRejected -or $script:events.Count -ne 0) {
        throw 'failed import rollback did not preserve an uncertain registered distro'
    }

    $attemptA.ImportCommandSucceeded = $true
    $registrationA = [PSCustomObject]@{
        Name = 'DegenDogsRunner'
        BasePath = $attemptA.Location
        Version = 2
    }
    $script:present = $true
    Invoke-WslRunnerImportRollback `
        -Attempt $attemptA `
        -GetInventoryAction { if ($script:present) { @('DegenDogsRunner') } else { @() } } `
        -GetRegistrationAction { param($name) $registrationA } `
        -UnregisterAction { param($name) $script:events.Add("unregister:$name"); $script:present = $false } `
        -RemoveAction { param($attempt) $script:events.Add("remove:$($attempt.Id)"); Remove-BoundedWslImportDirectory -Attempt $attempt }
    $expectedEvents = "unregister:DegenDogsRunner,remove:$($attemptA.Id)"
    if (($script:events -join ',') -ne $expectedEvents) {
        throw "exact attempt rollback events were wrong: $($script:events -join ',')"
    }
    if ([IO.Directory]::Exists($attemptA.Location)) {
        throw 'exact attempt directory survived proven rollback'
    }

    $attemptC = New-WslRunnerImportAttempt -KnownLocalAppData $knownLocal -DistroName 'DegenDogsRunner'
    $attemptC.ImportCommandSucceeded = $true
    $registrationC = [PSCustomObject]@{ Name = 'DegenDogsRunner'; BasePath = $attemptC.Location; Version = 2 }
    $postCheckRejected = $false
    try {
        Invoke-WslRunnerImportRollback `
            -Attempt $attemptC `
            -GetInventoryAction { @('DegenDogsRunner') } `
            -GetRegistrationAction { param($name) $registrationC } `
            -UnregisterAction { param($name) } `
            -RemoveAction { param($attempt) Remove-BoundedWslImportDirectory -Attempt $attempt }
    }
    catch {
        $postCheckRejected = $true
    }
    if (-not $postCheckRejected -or -not [IO.Directory]::Exists($attemptC.Location)) {
        throw 'rollback removed files without proving post-unregister absence'
    }
    Remove-BoundedWslImportDirectory -Attempt $attemptC
}
finally {
    foreach ($attempt in @($attemptA, $attemptB)) {
        if ($attempt -and (Test-WslRunnerImportReceipt -Attempt $attempt)) {
            Remove-BoundedWslImportDirectory -Attempt $attempt
        }
    }
    if ([IO.Directory]::Exists($knownLocal)) { [IO.Directory]::Delete($knownLocal, $true) }
}
Write-Output 'import-rollback-provenance-checked'
""",
        "import-rollback-provenance-checked",
    )


def test_registration_ownership_and_bounded_partial_cleanup() -> None:
    assert_harness(
        r"""
$knownLocal = Join-Path ([IO.Path]::GetTempPath()) ('degen-dogs-registration-test-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($knownLocal) | Out-Null
$attempt = New-WslRunnerImportAttempt -KnownLocalAppData $knownLocal -DistroName 'DegenDogsRunner'
$base = $attempt.Base
$location = $attempt.Location
$foreign = Join-Path ([IO.Path]::GetTempPath()) ('degen-dogs-foreign-test-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($foreign) | Out-Null
try {
    [IO.File]::WriteAllText((Join-Path $location 'partial.vhdx'), 'partial')
    [IO.File]::WriteAllText((Join-Path $foreign 'keep.txt'), 'foreign')
    $registration = [PSCustomObject]@{
        Name = 'DegenDogsRunner'
        BasePath = $location
        Version = 2
    }
    if (-not (Test-WslDistroRegistrationMatches `
        -Registration $registration `
        -DistroName 'DegenDogsRunner' `
        -InstallLocation $location)) {
        throw 'exact registration ownership was rejected'
    }
    foreach ($mutation in @(
        [PSCustomObject]@{ Name = 'Foreign'; BasePath = $location; Version = 2 },
        [PSCustomObject]@{ Name = 'DegenDogsRunner'; BasePath = $foreign; Version = 2 },
        [PSCustomObject]@{ Name = 'DegenDogsRunner'; BasePath = $location; Version = 1 }
    )) {
        if (Test-WslDistroRegistrationMatches `
            -Registration $mutation `
            -DistroName 'DegenDogsRunner' `
            -InstallLocation $location) {
            throw 'foreign WSL registration was accepted as owned'
        }
    }

    $outsideRejected = $false
    try {
        $foreignAttempt = [PSCustomObject]@{
            Id = $attempt.Id
            DistroName = $attempt.DistroName
            KnownLocalAppData = $attempt.KnownLocalAppData
            Base = $attempt.Base
            Location = $foreign
            ReceiptPath = $attempt.ReceiptPath
        }
        Remove-BoundedWslImportDirectory -Attempt $foreignAttempt
    }
    catch {
        $outsideRejected = $true
    }
    if (-not $outsideRejected -or -not [IO.File]::Exists((Join-Path $foreign 'keep.txt'))) {
        throw 'out-of-bound partial cleanup touched a foreign directory'
    }
    Remove-BoundedWslImportDirectory -Attempt $attempt
    if ([IO.Directory]::Exists($location)) {
        throw 'bounded partial import directory was not removed'
    }
}
finally {
    if ([IO.Directory]::Exists($location)) { [IO.Directory]::Delete($location, $true) }
    if ([IO.Directory]::Exists($knownLocal)) { [IO.Directory]::Delete($knownLocal, $true) }
    if ([IO.Directory]::Exists($foreign)) { [IO.Directory]::Delete($foreign, $true) }
}
Write-Output 'registration-ownership-cleanup-checked'
""",
        "registration-ownership-cleanup-checked",
    )


def test_task_registration_transaction_cleans_failed_attestation() -> None:
    assert_harness(
        r"""
$script:events = [Collections.Generic.List[string]]::new()
$registered = Invoke-WslRunnerTaskRegistrationTransaction `
    -PrepareAction { $script:events.Add('prepare'); [PSCustomObject]@{ BoundaryEstablished = $true } } `
    -RegisterAction { $script:events.Add('register'); return 'task-object' } `
    -ResolveExactTaskAction { $script:events.Add('resolve'); return 'resolved-task' } `
    -AttestAction { param($task) $script:events.Add("attest:$task") } `
    -IsolationAction { $script:events.Add('isolate'); [PSCustomObject]@{ BoundaryEstablished = $true } }
if ($registered -ne 'resolved-task' -or ($script:events -join ',') -ne 'prepare,register,resolve,attest:resolved-task') {
    throw "successful task transaction was wrong: $($script:events -join ',')"
}

$script:events.Clear()
$failed = $false
try {
    Invoke-WslRunnerTaskRegistrationTransaction `
        -PrepareAction { $script:events.Add('prepare'); [PSCustomObject]@{ BoundaryEstablished = $true } } `
        -RegisterAction { $script:events.Add('register'); return 'task-object' } `
        -ResolveExactTaskAction { $script:events.Add('resolve'); return 'resolved-task' } `
        -AttestAction { param($task) $script:events.Add("attest:$task"); throw 'unsafe XML' } `
        -IsolationAction { $script:events.Add('isolate'); [PSCustomObject]@{ BoundaryEstablished = $true } }
}
catch {
    $failed = $true
}
if (-not $failed) { throw 'failed task attestation was swallowed' }
if (($script:events -join ',') -ne 'prepare,register,resolve,attest:resolved-task,isolate') {
    throw "failed task transaction did not clean up: $($script:events -join ',')"
}

$script:events.Clear()
$nullFailed = $false
try {
    Invoke-WslRunnerTaskRegistrationTransaction `
        -PrepareAction { $script:events.Add('prepare'); [PSCustomObject]@{ BoundaryEstablished = $true } } `
        -RegisterAction { $script:events.Add('register'); return $null } `
        -ResolveExactTaskAction { $script:events.Add('resolve'); return 'resolved-task' } `
        -AttestAction { param($task) $script:events.Add("attest:$task") } `
        -IsolationAction { $script:events.Add('isolate'); [PSCustomObject]@{ BoundaryEstablished = $true } }
}
catch {
    $nullFailed = $true
}
if (-not $nullFailed -or ($script:events -join ',') -ne 'prepare,register,isolate') {
    throw "null registration was not reconciled through isolation: $($script:events -join ',')"
}

$script:events.Clear()
$foreignPredecessorRejected = $false
try {
    Invoke-WslRunnerTaskRegistrationTransaction `
        -PrepareAction {
            $script:events.Add('prepare:foreign')
            [PSCustomObject]@{
                BoundaryEstablished = $false
                Errors = @('ownership attestation failed: foreign task')
            }
        } `
        -RegisterAction { $script:events.Add('register:unsafe-overwrite') } `
        -ResolveExactTaskAction { $script:events.Add('resolve') } `
        -AttestAction { param($task) $script:events.Add('attest') } `
        -IsolationAction { $script:events.Add('isolate') }
}
catch {
    $foreignPredecessorRejected = $true
}
if (-not $foreignPredecessorRejected -or
    ($script:events -join ',') -ne 'prepare:foreign') {
    throw "foreign predecessor reached registration: $($script:events -join ',')"
}
Write-Output 'task-registration-transaction-checked'
""",
        "task-registration-transaction-checked",
    )


def test_scheduled_task_lookup_fails_closed() -> None:
    assert_harness(
        r"""
$queryFailureRejected = $false
try {
    Get-ExactScheduledTask -Name 'Degen Dogs WSL Runner' -QueryAction {
        throw 'synthetic Task Scheduler RPC failure'
    }
}
catch {
    $queryFailureRejected = $true
}
if (-not $queryFailureRejected) {
    throw 'Task Scheduler query failure was treated as task absence'
}

$absent = Get-ExactScheduledTask -Name 'Degen Dogs WSL Runner' -QueryAction {
    @([PSCustomObject]@{ TaskName = 'Unrelated Task'; TaskPath = '\' })
}
if ($null -ne $absent) { throw 'unrelated root task matched the runner task' }

$expected = [PSCustomObject]@{
    TaskName = 'Degen Dogs WSL Runner'
    TaskPath = '\'
    Marker = 'exact'
}
$resolved = Get-ExactScheduledTask -Name 'Degen Dogs WSL Runner' -QueryAction {
    @(
        [PSCustomObject]@{ TaskName = 'Unrelated Task'; TaskPath = '\' },
        $expected
    )
}
if ($resolved.Marker -ne 'exact') { throw 'exact root task was not resolved' }

foreach ($collision in @(
    [PSCustomObject]@{ TaskName = 'degen dogs wsl runner'; TaskPath = '\' },
    [PSCustomObject]@{ TaskName = 'Degen Dogs WSL Runner'; TaskPath = '\Foreign\' }
)) {
    $collisionRejected = $false
    try {
        Get-ExactScheduledTask -Name 'Degen Dogs WSL Runner' -QueryAction { @($collision) }
    }
    catch {
        $collisionRejected = $true
    }
    if (-not $collisionRejected) { throw 'non-exact task-name/path collision was treated as absence' }
}
Write-Output 'scheduled-task-lookup-fail-closed-checked'
""",
        "scheduled-task-lookup-fail-closed-checked",
    )


def test_task_isolation_does_not_short_circuit_or_touch_foreign_tasks() -> None:
    assert_harness(
        r"""
function Invoke-SyntheticIsolationCase {
    param(
        [bool]$InitiallyPresent,
        [bool]$InitiallyEnabled,
        [string]$InitiallyState,
        [bool]$Owned,
        [string]$FailOperation
    )
    $script:syntheticPresent = $InitiallyPresent
    $script:syntheticEnabled = $InitiallyEnabled
    $script:syntheticState = $InitiallyState
    $script:syntheticOwned = $Owned
    $script:syntheticEvents = [Collections.Generic.List[string]]::new()
    $resolve = {
        if (-not $script:syntheticPresent) { return $null }
        return [PSCustomObject]@{
            Settings = [PSCustomObject]@{ Enabled = $script:syntheticEnabled }
            State = $script:syntheticState
            Owned = $script:syntheticOwned
        }
    }
    $attest = {
        param($task)
        if (-not $task.Owned) { throw 'foreign task schema' }
    }
    $disable = {
        param($task)
        $script:syntheticEvents.Add('Disable')
        if ($FailOperation -eq 'Disable') { throw 'disable failed' }
        $script:syntheticEnabled = $false
    }
    $stop = {
        param($task)
        $script:syntheticEvents.Add('Stop')
        if ($FailOperation -eq 'Stop') { throw 'stop failed' }
        $script:syntheticState = 'Ready'
    }
    $unregister = {
        param($task)
        $script:syntheticEvents.Add('Unregister')
        if ($FailOperation -eq 'Unregister') { throw 'unregister failed' }
        $script:syntheticPresent = $false
    }
    $result = Invoke-WslRunnerTaskIsolation `
        -Remove $true `
        -ResolveExactTaskAction $resolve `
        -AssertOwnedTaskAction $attest `
        -DisableAction $disable `
        -StopAction $stop `
        -UnregisterAction $unregister
    return [PSCustomObject]@{
        Result = $result
        Events = @($script:syntheticEvents)
    }
}

$disableFailure = Invoke-SyntheticIsolationCase $true $true 'Running' $true 'Disable'
if (($disableFailure.Events -join ',') -ne 'Disable,Stop,Unregister') {
    throw "disable failure short-circuited isolation: $($disableFailure.Events -join ',')"
}
if (-not $disableFailure.Result.BoundaryEstablished -or $disableFailure.Result.EndState -ne 'Absent') {
    throw 'absence after disable failure was not accepted as an isolated boundary'
}
if ($disableFailure.Result.Errors.Count -ne 1) { throw 'disable failure evidence was lost' }

$unregisterFailure = Invoke-SyntheticIsolationCase $true $true 'Running' $true 'Unregister'
if (($unregisterFailure.Events -join ',') -ne 'Disable,Stop,Unregister') {
    throw 'unregister failure skipped an isolation operation'
}
if (-not $unregisterFailure.Result.BoundaryEstablished -or
    $unregisterFailure.Result.EndState -ne 'DisabledStopped') {
    throw 'managed disabled/stopped task was not accepted after unregister failure'
}
if ($unregisterFailure.Result.Errors.Count -ne 1) { throw 'unregister failure evidence was lost' }

$foreign = Invoke-SyntheticIsolationCase $true $true 'Running' $false ''
if ($foreign.Events.Count -ne 0) {
    throw "foreign task was mutated: $($foreign.Events -join ',')"
}
if ($foreign.Result.BoundaryEstablished -or -not $foreign.Result.UnsafeOrForeign) {
    throw 'foreign enabled/running task was reported as safely isolated'
}

$absent = Invoke-SyntheticIsolationCase $false $false 'Disabled' $false ''
if (-not $absent.Result.BoundaryEstablished -or $absent.Result.EndState -ne 'Absent' -or
    $absent.Events.Count -ne 0) {
    throw 'initially absent task did not establish a zero-mutation boundary'
}
Write-Output 'task-isolation-checked'
""",
        "task-isolation-checked",
    )


def test_task_xml_attestation_rejects_privilege_and_trigger_mutations() -> None:
    assert_harness(
        r"""
$sid = 'S-1-5-21-111-222-333-1001'
$wslPath = 'C:\WINDOWS\System32\wsl.exe'
$arguments = '--distribution DegenDogsRunner --user root --exec /usr/local/libexec/degen-dogs-wsl-anchor'
$valid = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals><Principal id="Author"><UserId>$sid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>$sid</UserId></LogonTrigger>
    <TimeTrigger><Repetition><Interval>PT5M</Interval><Duration>P3650D</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>2026-08-30T12:02:00</StartBoundary><Enabled>true</Enabled></TimeTrigger>
  </Triggers>
  <Settings><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><RestartOnFailure><Count>999</Count><Interval>PT1M</Interval></RestartOnFailure><StartWhenAvailable>true</StartWhenAvailable><WakeToRun>true</WakeToRun><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Enabled>false</Enabled></Settings>
  <Actions Context="Author"><Exec><Command>$wslPath</Command><Arguments>$arguments</Arguments></Exec></Actions>
</Task>
"@
Assert-WslRunnerScheduledTaskXml -XmlText $valid -ExpectedUserSid $sid -ExpectedLogonType 'InteractiveToken' -ExpectedWslPath $wslPath -ExpectedArguments $arguments -AtLogOnOnly $true -ExpectedEnabled $false

# Task Scheduler canonicalizes an enabled task by omitting Settings/Enabled,
# because the task-schema default is true. The attestor must accept that exact
# representation when enabled while continuing to require an explicit false
# node for the pre-activation state.
$enabledByDefault = $valid.Replace('<Enabled>false</Enabled></Settings>', '</Settings>')
Assert-WslRunnerScheduledTaskXml -XmlText $enabledByDefault -ExpectedUserSid $sid -ExpectedLogonType 'InteractiveToken' -ExpectedWslPath $wslPath -ExpectedArguments $arguments -AtLogOnOnly $true -ExpectedEnabled $true
Assert-WslRunnerManagedTaskXml -XmlText $enabledByDefault -ExpectedUserSid $sid -ExpectedWslPath $wslPath -ExpectedArguments $arguments -ExpectedEnabled $true

foreach ($unsafeEnabledExpectation in @(
    [PSCustomObject]@{ Xml = $enabledByDefault; Expected = $false },
    [PSCustomObject]@{ Xml = $valid; Expected = $true }
)) {
    $unsafeEnabledAccepted = $false
    try {
        Assert-WslRunnerScheduledTaskXml -XmlText $unsafeEnabledExpectation.Xml -ExpectedUserSid $sid -ExpectedLogonType 'InteractiveToken' -ExpectedWslPath $wslPath -ExpectedArguments $arguments -AtLogOnOnly $true -ExpectedEnabled ([bool]$unsafeEnabledExpectation.Expected)
        $unsafeEnabledAccepted = $true
    }
    catch {}
    if ($unsafeEnabledAccepted) { throw 'unsafe task enabled-state representation was accepted' }
}

$mutations = @(
    $valid.Replace('</Triggers>', '<BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>'),
    $valid.Replace('<LogonTrigger><Enabled>true</Enabled>', '<LogonTrigger><Enabled>false</Enabled>'),
    $valid.Replace('<Enabled>true</Enabled></TimeTrigger>', '<Enabled>false</Enabled></TimeTrigger>'),
    $valid.Replace('LeastPrivilege', 'HighestAvailable'),
    $valid.Replace('<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>', '<DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>'),
    $valid.Replace('<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>', '<StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>'),
    $valid.Replace('<RestartOnFailure><Count>999</Count><Interval>PT1M</Interval></RestartOnFailure>', '<RestartOnFailure><Count>1</Count><Interval>PT10M</Interval></RestartOnFailure>'),
    $valid.Replace('<WakeToRun>true</WakeToRun>', '<WakeToRun>false</WakeToRun>'),
    $valid.Replace('--distribution DegenDogsRunner', '--distribution ForeignDistro'),
    $valid.Replace('<Enabled>false</Enabled></Settings>', '<Enabled>true</Enabled></Settings>')
)
foreach ($mutation in $mutations) {
    $rejected = $false
    try {
        Assert-WslRunnerScheduledTaskXml -XmlText $mutation -ExpectedUserSid $sid -ExpectedLogonType 'InteractiveToken' -ExpectedWslPath $wslPath -ExpectedArguments $arguments -AtLogOnOnly $true -ExpectedEnabled $false
    }
    catch {
        $rejected = $true
    }
    if (-not $rejected) { throw 'unsafe task XML mutation was accepted' }
}

$unattended = $valid.Replace('InteractiveToken', 'Password').Replace(
    '<LogonTrigger><Enabled>true</Enabled><UserId>',
    '<BootTrigger><Enabled>true</Enabled></BootTrigger><LogonTrigger><Enabled>true</Enabled><UserId>'
)
Assert-WslRunnerScheduledTaskXml -XmlText $unattended -ExpectedUserSid $sid -ExpectedLogonType 'Password' -ExpectedWslPath $wslPath -ExpectedArguments $arguments -AtLogOnOnly $false -ExpectedEnabled $false

# Isolation must recognize every exact schema this installer can have created,
# independently of the replacement invocation's desired task plan.
$elevatedBootstrap = $valid.Replace(
    '<LogonTrigger><Enabled>true</Enabled><UserId>',
    '<BootTrigger><Enabled>true</Enabled></BootTrigger><LogonTrigger><Enabled>true</Enabled><UserId>'
)
foreach ($managed in @($valid, $elevatedBootstrap, $unattended)) {
    Assert-WslRunnerManagedTaskXml `
        -XmlText $managed `
        -ExpectedUserSid $sid `
        -ExpectedWslPath $wslPath `
        -ExpectedArguments $arguments `
        -ExpectedEnabled $false
}
$unsafePasswordPerUser = $valid.Replace('InteractiveToken', 'Password')
$unsafePredecessorAccepted = $false
try {
    Assert-WslRunnerManagedTaskXml `
        -XmlText $unsafePasswordPerUser `
        -ExpectedUserSid $sid `
        -ExpectedWslPath $wslPath `
        -ExpectedArguments $arguments `
        -ExpectedEnabled $false
    $unsafePredecessorAccepted = $true
}
catch {}
if ($unsafePredecessorAccepted) {
    throw 'an impossible Password/no-boot predecessor schema was accepted as managed'
}
Write-Output 'task-xml-attestation-checked'
""",
        "task-xml-attestation-checked",
    )


def test_real_windows_task_scheduler_round_trip() -> None:
    if os.name != "nt":
        return
    assert_harness(
        r"""
$taskName = 'Degen Dogs WSL Policy Test ' + [Guid]::NewGuid().ToString('N')
$registered = $null
try {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $wslPath = Join-Path $env:SystemRoot 'System32\wsl.exe'
    $plan = New-WslRunnerTaskPlan `
        -AtLogOnOnly $true `
        -Activate $false `
        -UserSid $sid `
        -WslPath $wslPath `
        -DistroName 'DegenDogsRunner'
    $action = New-ScheduledTaskAction -Execute $plan.Executable -Argument $plan.Arguments
    $triggers = [Collections.Generic.List[object]]::new()
    foreach ($kind in $plan.TriggerKinds) {
        switch ($kind) {
            'Logon' { $triggers.Add((New-ScheduledTaskTrigger -AtLogOn -User $sid)) }
            'Watchdog' {
                $triggers.Add((New-ScheduledTaskTrigger `
                    -Once `
                    -At (Get-Date).AddMinutes(2) `
                    -RepetitionInterval (New-TimeSpan -Minutes 5) `
                    -RepetitionDuration (New-TimeSpan -Days 3650)))
            }
            default { throw "unsafe per-user trigger kind: $kind" }
        }
    }
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
    $principal = New-ScheduledTaskPrincipal `
        -UserId $sid `
        -LogonType Interactive `
        -RunLevel Limited
    $registered = Register-ScheduledTask `
        -TaskName $taskName `
        -TaskPath '\' `
        -Action $action `
        -Trigger $triggers.ToArray() `
        -Settings $settings `
        -Principal $principal `
        -Description 'Disposable Degen Dogs per-user task policy verification.'
    $resolvedByProductionLookup = Get-ExactScheduledTask -Name $taskName
    if ($null -eq $resolvedByProductionLookup) {
        throw 'production exact-task lookup did not find the disposable root task'
    }
    $xml = Export-ScheduledTask -TaskName $taskName -TaskPath '\'
    Assert-WslRunnerScheduledTaskXml `
        -XmlText $xml `
        -ExpectedUserSid $sid `
        -ExpectedLogonType 'InteractiveToken' `
        -ExpectedWslPath $plan.Executable `
        -ExpectedArguments $plan.Arguments `
        -AtLogOnOnly $true `
        -ExpectedEnabled $false
    $registered | Enable-ScheduledTask | Out-Null
    $enabledTask = Get-ExactScheduledTask -Name $taskName
    if ($null -eq $enabledTask -or -not [bool]$enabledTask.Settings.Enabled) {
        throw 'disposable task did not become enabled'
    }
    $enabledXml = Export-ScheduledTask -TaskName $taskName -TaskPath '\'
    Assert-WslRunnerScheduledTaskXml `
        -XmlText $enabledXml `
        -ExpectedUserSid $sid `
        -ExpectedLogonType 'InteractiveToken' `
        -ExpectedWslPath $plan.Executable `
        -ExpectedArguments $plan.Arguments `
        -AtLogOnOnly $true `
        -ExpectedEnabled $true
    $isolation = Invoke-WslRunnerTaskIsolation `
        -Remove $true `
        -ResolveExactTaskAction {
            Get-ExactScheduledTask -Name $taskName
        } `
        -AssertOwnedTaskAction {
            param($task)
            $currentXml = Export-ScheduledTask -TaskName $taskName -TaskPath '\'
            Assert-WslRunnerScheduledTaskXml `
                -XmlText $currentXml `
                -ExpectedUserSid $sid `
                -ExpectedLogonType 'InteractiveToken' `
                -ExpectedWslPath $plan.Executable `
                -ExpectedArguments $plan.Arguments `
                -AtLogOnOnly $true `
                -ExpectedEnabled ([bool]$task.Settings.Enabled)
        } `
        -DisableAction { param($task) $task | Disable-ScheduledTask | Out-Null } `
        -StopAction { param($task) $task | Stop-ScheduledTask -ErrorAction Stop } `
        -UnregisterAction { param($task) $task | Unregister-ScheduledTask -Confirm:$false }
    if (-not $isolation.BoundaryEstablished -or $isolation.EndState -ne 'Absent') {
        throw "production isolation did not remove the disposable task: $($isolation.Errors -join '; ')"
    }
    $registered = $null
}
finally {
    $task = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
    if ($task) {
        $task | Unregister-ScheduledTask -Confirm:$false
    }
}
if (Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue) {
    throw 'disposable task cleanup failed'
}
Write-Output 'windows-task-round-trip-checked'
""",
        "windows-task-round-trip-checked",
    )


def test_embedded_linux_lifecycle_inventories_are_symmetric() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    trusted = literal_payload(source, "trustedBundleProvision")
    trusted_required = bash_array(trusted, "required")
    for relative in NEW_ASSETS:
        assert trusted_required.count(relative) == 1, relative

    runtime = literal_payload(source, "runtimeStage")
    assert bash_array(runtime, "runtime_required_assets") == NEW_ASSETS

    for payload_name in ("uninstallScript", "quiesce", "rollbackPublisher"):
        payload = literal_payload(source, payload_name)
        assert bash_array(payload, "activation_units") == ACTIVATION_UNITS
        assert bash_array(payload, "service_units") == SERVICE_UNITS

    activation = literal_payload(source, "commitActivation")
    assert bash_array(activation, "activation_units") == ACTIVATION_UNITS
    assert bash_array(activation, "triggered_services") == SERVICE_UNITS[-2:]
    for service in SERVICE_UNITS[-2:]:
        assert f'systemctl show --property=LoadState --value "$unit"' in activation
        assert f'systemctl is-failed --quiet "$unit"' in activation
        assert source.count(service) >= 6

    liveness = source.split("$publisherReady = $false", 1)[1].split("$taskInfo =", 1)[0]
    for unit in ACTIVATION_UNITS:
        assert liveness.count(unit) >= 2, unit
    for service in SERVICE_UNITS[-2:]:
        assert liveness.count(service) >= 2, service

    probes = activation_liveness_probes(source)
    assert len(probes) == 2
    for probe_number, probe in enumerate(probes, start=1):
        harness = r'''
set -Eeuo pipefail
test_root=$(mktemp -d)
trap 'command rm -rf -- "$test_root"' EXIT
cat >"$test_root/probe.sh" <<'ACTIVATION_PROBE'
''' + probe + r'''
ACTIVATION_PROBE
test() {
  if [[ "$#" == 2 && "$1" == -f ]]; then return 0; fi
  builtin test "$@"
}
systemctl() {
  case "${1:-}" in
    is-active)
      shift
      [[ "${1:-}" == --quiet ]] && shift
      for queried_unit in "$@"; do
        [[ "$queried_unit" != "$inactive_unit" ]] && return 0
      done
      return 3
      ;;
    show) printf 'loaded\n' ;;
    is-failed) return 1 ;;
    *) return 97 ;;
  esac
}
export -f test systemctl
run_probe() {
  inactive_unit="$1"
  expected_status="$2"
  export inactive_unit
  set +e
  bash -Eeuo pipefail "$test_root/probe.sh"
  status=$?
  set -e
  if [[ "$status" != "$expected_status" ]]; then
    printf 'probe ''' + str(probe_number) + r''' expected status %s with inactive unit %s, got %s\n' \
      "$expected_status" "${inactive_unit:-none}" "$status" >&2
    exit 96
  fi
}
run_probe '' 0
for inactive_unit in ''' + " ".join(ACTIVATION_UNITS) + r'''; do
  run_probe "$inactive_unit" 1
done
'''
        result = subprocess.run(
            ["bash", "-s", "--"],
            input=harness.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"activation liveness probe {probe_number} accepted an inactive unit\n"
            f"stdout={result.stdout.decode('utf-8', errors='replace')}\n"
            f"stderr={result.stderr.decode('utf-8', errors='replace')}"
        )


def main() -> None:
    test_invocation_policy_and_trigger_selection()
    test_git_command_resolution_is_deterministic()
    test_temporary_git_cleanup_is_bounded_and_handles_read_only_pack_files()
    test_task_plan_and_import_command_are_mode_complete()
    test_task_result_reporting_requires_complete_healthy_suppression_state()
    test_verified_import_orders_hash_before_import_and_cleans_up()
    test_wsl_inventory_fails_closed()
    test_import_claim_is_physical_unique_and_exclusive()
    test_import_rollback_requires_exact_attempt_provenance()
    test_registration_ownership_and_bounded_partial_cleanup()
    test_task_registration_transaction_cleans_failed_attestation()
    test_scheduled_task_lookup_fails_closed()
    test_task_isolation_does_not_short_circuit_or_touch_foreign_tasks()
    test_task_xml_attestation_rejects_privilege_and_trigger_mutations()
    test_real_windows_task_scheduler_round_trip()
    test_embedded_linux_lifecycle_inventories_are_symmetric()
    print("wsl_runner_windows_policy_tests=pass count=16")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"wsl_runner_windows_policy_tests=fail error={exc}", file=sys.stderr)
        raise
