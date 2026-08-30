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

function Test-CurrentProcessIsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-WslRunnerInvocationPolicy {
    param(
        [Parameter(Mandatory)][bool]$IsAdministrator,
        [Parameter(Mandatory)][bool]$AtLogOnOnly,
        [Parameter(Mandatory)][bool]$HasCredential
    )

    if (-not $IsAdministrator -and -not $AtLogOnOnly) {
        throw 'Run this installer from an elevated PowerShell, or select -AtLogOnOnly for the current-user fallback.'
    }
    if ($AtLogOnOnly -and $HasCredential) {
        throw 'Do not supply -Credential with -AtLogOnOnly; the interactive fallback uses the current signed-in account.'
    }
}

function Get-WslRunnerImageSpec {
    return [PSCustomObject]@{
        Uri = 'https://releases.ubuntu.com/24.04.4/ubuntu-24.04.4-wsl-amd64.wsl'
        Sha256 = '9b2f7730dc68227dd04a9f3e5eab86ad85caf556b8606ad94f1f29ff5c4fd3f5'
    }
}

function Get-WslRunnerOwnershipMarkerValue {
    param(
        [Parameter(Mandatory)]
        [ValidatePattern('^[0-9a-f]{32}$')]
        [string]$AttemptId
    )

    return "degen-dogs-windows-runner-v1:$AttemptId"
}

function Get-WslRunnerTriggerKinds {
    param([Parameter(Mandatory)][bool]$AtLogOnOnly)

    if ($AtLogOnOnly) {
        return @('Logon', 'Watchdog')
    }
    return @('Startup', 'Logon', 'Watchdog')
}

function Get-WslRunnerKnownLocalAppData {
    $knownPath = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::LocalApplicationData
    )
    if (-not $knownPath) {
        throw 'The Windows LocalApplicationData known folder is unavailable.'
    }
    return [IO.Path]::GetFullPath($knownPath)
}

function Assert-WslRunnerDirectoryBoundary {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Candidate,
        [scriptblock]$GetItemAction
    )

    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $candidatePath = [IO.Path]::GetFullPath($Candidate).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $boundedPrefix = $rootPath + [IO.Path]::DirectorySeparatorChar
    if (-not [String]::Equals($candidatePath, $rootPath, [StringComparison]::OrdinalIgnoreCase) -and
        -not $candidatePath.StartsWith($boundedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The candidate directory escaped the Windows known-folder boundary.'
    }
    if (-not $GetItemAction) {
        $GetItemAction = {
            param($path)
            if (Test-Path -LiteralPath $path) {
                return Get-Item -LiteralPath $path -Force
            }
            return $null
        }
    }

    $pathsToInspect = [Collections.Generic.List[string]]::new()
    $pathsToInspect.Add($rootPath)
    if (-not [String]::Equals($candidatePath, $rootPath, [StringComparison]::OrdinalIgnoreCase)) {
        $relative = $candidatePath.Substring($boundedPrefix.Length)
        $cursor = $rootPath
        foreach ($segment in @($relative -split '[\\/]')) {
            if (-not $segment) { continue }
            $cursor = Join-Path $cursor $segment
            $pathsToInspect.Add([IO.Path]::GetFullPath($cursor))
        }
    }
    foreach ($path in $pathsToInspect) {
        $item = & $GetItemAction $path
        if ($null -eq $item) {
            if ([String]::Equals($path, $rootPath, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'The Windows LocalApplicationData known folder does not exist.'
            }
            continue
        }
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "The WSL install boundary contains a non-directory or reparse point: $path"
        }
    }
    return $candidatePath
}

function Initialize-WslRunnerInstallBase {
    param([Parameter(Mandatory)][string]$KnownLocalAppData)

    $knownRoot = Assert-WslRunnerDirectoryBoundary `
        -Root $KnownLocalAppData `
        -Candidate $KnownLocalAppData
    $degenDogsRoot = [IO.Path]::GetFullPath((Join-Path $knownRoot 'DegenDogs'))
    [IO.Directory]::CreateDirectory($degenDogsRoot) | Out-Null
    Assert-WslRunnerDirectoryBoundary -Root $knownRoot -Candidate $degenDogsRoot | Out-Null
    $installBase = [IO.Path]::GetFullPath((Join-Path $degenDogsRoot 'WSL'))
    [IO.Directory]::CreateDirectory($installBase) | Out-Null
    Assert-WslRunnerDirectoryBoundary -Root $knownRoot -Candidate $installBase | Out-Null
    return [PSCustomObject]@{
        KnownLocalAppData = $knownRoot
        Base = $installBase
    }
}

function Enter-WslRunnerDistroLock {
    param(
        [Parameter(Mandatory)][string]$KnownLocalAppData,
        [Parameter(Mandatory)][string]$InstallBase,
        [Parameter(Mandatory)][string]$UserSid,
        [Parameter(Mandatory)][string]$DistroName,
        [ValidateSet('distro', 'task')][string]$LockNamespace = 'distro'
    )

    $basePath = [IO.Path]::GetFullPath($InstallBase)
    Assert-WslRunnerDirectoryBoundary `
        -Root $KnownLocalAppData `
        -Candidate $basePath |
        Out-Null
    $baseItem = Get-Item -LiteralPath $basePath -Force
    if (-not $baseItem.PSIsContainer -or ($baseItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'The WSL install base is not an ordinary directory.'
    }
    $lockDirectory = [IO.Path]::GetFullPath((Join-Path $basePath '.locks'))
    [IO.Directory]::CreateDirectory($lockDirectory) | Out-Null
    Assert-WslRunnerDirectoryBoundary `
        -Root $KnownLocalAppData `
        -Candidate $lockDirectory |
        Out-Null
    $lockDirectoryItem = Get-Item -LiteralPath $lockDirectory -Force
    if (-not $lockDirectoryItem.PSIsContainer -or
        ($lockDirectoryItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'The WSL runner lock directory is not an ordinary directory.'
    }
    $hashAlgorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $canonicalDistroName = $DistroName.ToUpperInvariant()
        $lockIdentity = "$UserSid`0$LockNamespace`0$canonicalDistroName"
        $lockDigest = ([BitConverter]::ToString(
            $hashAlgorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($lockIdentity))
        )).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $hashAlgorithm.Dispose()
    }
    $lockPath = Join-Path $lockDirectory "$lockDigest.lock"
    try {
        $stream = [IO.File]::Open(
            $lockPath,
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    }
    catch {
        throw "Another Degen Dogs installer or uninstaller owns the current-user $LockNamespace lock: $($_.Exception.Message)"
    }
    return [PSCustomObject]@{
        Path = $lockPath
        Stream = $stream
    }
}

function Enter-WslRunnerTaskLock {
    param(
        [Parameter(Mandatory)][string]$KnownLocalAppData,
        [Parameter(Mandatory)][string]$InstallBase,
        [Parameter(Mandatory)][string]$UserSid,
        [Parameter(Mandatory)][string]$TaskName
    )

    return Enter-WslRunnerDistroLock `
        -KnownLocalAppData $KnownLocalAppData `
        -InstallBase $InstallBase `
        -UserSid $UserSid `
        -DistroName $TaskName `
        -LockNamespace task
}

function Exit-WslRunnerDistroLock {
    param([Parameter(Mandatory)][object]$Lock)

    if ($Lock.PSObject.Properties['Stream'] -and $Lock.Stream) {
        $Lock.Stream.Dispose()
    }
}

function New-WslRunnerImportAttempt {
    param(
        [Parameter(Mandatory)][string]$KnownLocalAppData,
        [Parameter(Mandatory)][string]$DistroName
    )

    $basePlan = Initialize-WslRunnerInstallBase -KnownLocalAppData $KnownLocalAppData
    $attemptId = [Guid]::NewGuid().ToString('N')
    $location = [IO.Path]::GetFullPath((Join-Path $basePlan.Base "$DistroName-$attemptId"))
    [IO.Directory]::CreateDirectory($location) | Out-Null
    Assert-WslRunnerDirectoryBoundary `
        -Root $basePlan.KnownLocalAppData `
        -Candidate $location |
        Out-Null
    $receiptPath = Join-Path $location '.degen-dogs-import-attempt'
    $receiptText = "degen-dogs-wsl-import-v1:$attemptId"
    $receiptBytes = [Text.Encoding]::UTF8.GetBytes($receiptText)
    $receiptStream = [IO.File]::Open(
        $receiptPath,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $receiptStream.Write($receiptBytes, 0, $receiptBytes.Length)
        $receiptStream.Flush($true)
    }
    finally {
        $receiptStream.Dispose()
    }
    return [PSCustomObject]@{
        Id = $attemptId
        DistroName = $DistroName
        KnownLocalAppData = $basePlan.KnownLocalAppData
        Base = $basePlan.Base
        Location = $location
        ReceiptPath = $receiptPath
        ImportCommandSucceeded = $false
    }
}

function Test-WslRunnerImportReceipt {
    param([Parameter(Mandatory)][object]$Attempt)

    try {
        if ($Attempt.Id -notmatch '^[0-9a-f]{32}$') { return $false }
        $expectedLocation = [IO.Path]::GetFullPath(
            (Join-Path $Attempt.Base "$($Attempt.DistroName)-$($Attempt.Id)")
        )
        if (-not [String]::Equals(
            $expectedLocation,
            [IO.Path]::GetFullPath($Attempt.Location),
            [StringComparison]::OrdinalIgnoreCase
        )) { return $false }
        Assert-WslRunnerDirectoryBoundary `
            -Root $Attempt.KnownLocalAppData `
            -Candidate $expectedLocation |
            Out-Null
        $expectedReceipt = [IO.Path]::GetFullPath(
            (Join-Path $expectedLocation '.degen-dogs-import-attempt')
        )
        if (-not [String]::Equals(
            $expectedReceipt,
            [IO.Path]::GetFullPath($Attempt.ReceiptPath),
            [StringComparison]::OrdinalIgnoreCase
        )) { return $false }
        if (-not (Test-Path -LiteralPath $expectedReceipt -PathType Leaf)) { return $false }
        $receiptItem = Get-Item -LiteralPath $expectedReceipt -Force
        if ($receiptItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
        $expectedText = "degen-dogs-wsl-import-v1:$($Attempt.Id)"
        return [String]::Equals(
            [IO.File]::ReadAllText($expectedReceipt, [Text.Encoding]::UTF8),
            $expectedText,
            [StringComparison]::Ordinal
        )
    }
    catch {
        return $false
    }
}

function Get-WslRunnerImportAttemptFromRegistration {
    param(
        [Parameter(Mandatory)][object]$Registration,
        [Parameter(Mandatory)][string]$KnownLocalAppData,
        [Parameter(Mandatory)][string]$DistroName
    )

    try {
        $registeredName = [string]$Registration.Name
        $registeredVersion = [int]$Registration.Version
        $registeredBasePath = [string]$Registration.BasePath
    }
    catch {
        throw 'The existing WSL registration has an invalid shape.'
    }
    if (-not [String]::Equals($registeredName, $DistroName, [StringComparison]::Ordinal) -or
        $registeredVersion -ne 2 -or -not $registeredBasePath) {
        throw 'The existing WSL registration does not match the exact distro name and WSL2 version.'
    }
    if ($registeredBasePath.StartsWith('\\?\', [StringComparison]::Ordinal)) {
        $registeredBasePath = $registeredBasePath.Substring(4)
    }
    $knownRoot = [IO.Path]::GetFullPath($KnownLocalAppData)
    $installBase = [IO.Path]::GetFullPath(
        (Join-Path (Join-Path $knownRoot 'DegenDogs') 'WSL')
    )
    $location = [IO.Path]::GetFullPath($registeredBasePath)
    Assert-WslRunnerDirectoryBoundary -Root $knownRoot -Candidate $installBase | Out-Null
    Assert-WslRunnerDirectoryBoundary -Root $knownRoot -Candidate $location | Out-Null
    if (-not (Test-Path -LiteralPath $location -PathType Container)) {
        throw 'The existing WSL registration install directory does not exist.'
    }
    $leaf = Split-Path -Leaf $location
    $leafPattern = '^' + [Regex]::Escape($DistroName) + '-([0-9a-f]{32})$'
    if ($leaf -notmatch $leafPattern) {
        throw 'The existing WSL registration path has no attempt-specific location token.'
    }
    $attemptId = $Matches[1].ToLowerInvariant()
    $attempt = [PSCustomObject]@{
        Id = $attemptId
        DistroName = $DistroName
        KnownLocalAppData = $knownRoot
        Base = $installBase
        Location = $location
        ReceiptPath = Join-Path $location '.degen-dogs-import-attempt'
        ImportCommandSucceeded = $true
    }
    if (-not (Test-WslRunnerImportReceipt -Attempt $attempt)) {
        throw 'The existing WSL registration has no matching ordinary host attempt receipt.'
    }
    return $attempt
}

function Get-WslRunnerImportArguments {
    param(
        [Parameter(Mandatory)][string]$DistroName,
        [Parameter(Mandatory)][string]$InstallLocation,
        [Parameter(Mandatory)][string]$ImagePath
    )

    return @('--import', $DistroName, $InstallLocation, $ImagePath, '--version', '2')
}

function New-WslRunnerTaskPlan {
    param(
        [Parameter(Mandatory)][bool]$AtLogOnOnly,
        [Parameter(Mandatory)][bool]$Activate,
        [Parameter(Mandatory)][string]$UserSid,
        [Parameter(Mandatory)][string]$WslPath,
        [Parameter(Mandatory)][string]$DistroName
    )

    $logonType = if ($AtLogOnOnly -or -not $Activate) { 'Interactive' } else { 'Password' }
    return [PSCustomObject]@{
        TriggerKinds = @(Get-WslRunnerTriggerKinds -AtLogOnOnly $AtLogOnOnly)
        UserId = $UserSid
        LogonType = $logonType
        RunLevel = 'Limited'
        Executable = $WslPath
        Arguments = "--distribution $DistroName --user root --exec /usr/local/libexec/degen-dogs-wsl-anchor"
        InitiallyEnabled = $false
    }
}

function Resolve-WslRunnerUserSid {
    param([Parameter(Mandatory)][string]$UserId)

    if ($UserId -match '^S-1-[0-9-]+$') {
        return $UserId
    }
    try {
        $account = [Security.Principal.NTAccount]::new($UserId)
        return $account.Translate([Security.Principal.SecurityIdentifier]).Value
    }
    catch {
        throw "Scheduled-task user '$UserId' could not be resolved to a Windows SID."
    }
}

function Test-WslDistroRegistrationMatches {
    param(
        [Parameter(Mandatory)][object]$Registration,
        [Parameter(Mandatory)][string]$DistroName,
        [Parameter(Mandatory)][string]$InstallLocation
    )

    try {
        $registeredName = [string]$Registration.Name
        $registeredVersion = [int]$Registration.Version
        $registeredBasePath = [string]$Registration.BasePath
    }
    catch {
        return $false
    }
    if (-not [String]::Equals($registeredName, $DistroName, [StringComparison]::Ordinal)) {
        return $false
    }
    if ($registeredVersion -ne 2 -or -not $registeredBasePath) {
        return $false
    }
    if (-not (Test-WslDistroRegistrationPathMatches `
        -Registration $Registration `
        -DistroName $DistroName `
        -InstallLocation $InstallLocation)) {
        return $false
    }
    return $registeredVersion -eq 2
}

function Test-WslDistroRegistrationPathMatches {
    param(
        [Parameter(Mandatory)][object]$Registration,
        [Parameter(Mandatory)][string]$DistroName,
        [Parameter(Mandatory)][string]$InstallLocation
    )

    try {
        $registeredName = [string]$Registration.Name
        $registeredBasePath = [string]$Registration.BasePath
    }
    catch {
        return $false
    }
    if (-not [String]::Equals($registeredName, $DistroName, [StringComparison]::Ordinal) -or -not $registeredBasePath) {
        return $false
    }
    if ($registeredBasePath.StartsWith('\\?\', [StringComparison]::Ordinal)) {
        $registeredBasePath = $registeredBasePath.Substring(4)
    }
    $registeredPath = [IO.Path]::GetFullPath($registeredBasePath).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $expectedPath = [IO.Path]::GetFullPath($InstallLocation).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    return [String]::Equals($registeredPath, $expectedPath, [StringComparison]::OrdinalIgnoreCase)
}

function Remove-BoundedWslImportDirectory {
    param([Parameter(Mandatory)][object]$Attempt)

    if (-not (Test-WslRunnerImportReceipt -Attempt $Attempt)) {
        throw 'Refusing to remove a WSL import without the exact attempt receipt.'
    }
    $basePath = [IO.Path]::GetFullPath($Attempt.Base)
    $locationPath = [IO.Path]::GetFullPath($Attempt.Location)
    Assert-WslRunnerDirectoryBoundary `
        -Root $Attempt.KnownLocalAppData `
        -Candidate $basePath |
        Out-Null
    Assert-WslRunnerDirectoryBoundary `
        -Root $Attempt.KnownLocalAppData `
        -Candidate $locationPath |
        Out-Null
    if (-not (Test-Path -LiteralPath $locationPath)) {
        return
    }
    $locationItem = Get-Item -LiteralPath $locationPath -Force
    if (-not $locationItem.PSIsContainer -or (
        $locationItem.Attributes -band [IO.FileAttributes]::ReparsePoint
    )) {
        throw 'Refusing to remove a partial WSL import that is not an ordinary directory.'
    }
    $reparseEntry = @(
        Get-ChildItem -LiteralPath $locationPath -Force -Recurse |
            Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
            Select-Object -First 1
    )
    if ($reparseEntry.Count -ne 0) {
        throw 'Refusing to recursively remove a partial WSL import containing a reparse point.'
    }
    Remove-Item -LiteralPath $locationPath -Recurse -Force
}

function Invoke-WslRunnerImportRollback {
    param(
        [Parameter(Mandatory)][object]$Attempt,
        [Parameter(Mandatory)][scriptblock]$GetInventoryAction,
        [Parameter(Mandatory)][scriptblock]$GetRegistrationAction,
        [Parameter(Mandatory)][scriptblock]$UnregisterAction,
        [Parameter(Mandatory)][scriptblock]$RemoveAction
    )

    if (-not (Test-WslRunnerImportReceipt -Attempt $Attempt)) {
        throw 'The failed import has no exact host attempt receipt; preserving all state.'
    }
    $inventory = @(& $GetInventoryAction)
    if ($inventory -contains $Attempt.DistroName) {
        if (-not [bool]$Attempt.ImportCommandSucceeded) {
            throw 'The import command did not prove success but the distro name is registered; preserving all state.'
        }
        $registration = & $GetRegistrationAction $Attempt.DistroName
        if ($null -eq $registration -or -not (Test-WslDistroRegistrationMatches `
            -Registration $registration `
            -DistroName $Attempt.DistroName `
            -InstallLocation $Attempt.Location)) {
            throw 'The registered distro does not match the exact failed import attempt; preserving all state.'
        }
        if (-not (Test-WslRunnerImportReceipt -Attempt $Attempt)) {
            throw 'The failed import attempt receipt changed before unregister; preserving all state.'
        }
        & $UnregisterAction $Attempt.DistroName
        $postUnregisterInventory = @(& $GetInventoryAction)
        if ($postUnregisterInventory -contains $Attempt.DistroName) {
            throw 'The distro remains registered after rollback; preserving its install directory.'
        }
    }
    if (-not (Test-WslRunnerImportReceipt -Attempt $Attempt)) {
        throw 'The failed import attempt receipt changed before directory cleanup; preserving it.'
    }
    & $RemoveAction $Attempt
}

function Invoke-WslRunnerTaskIsolation {
    param(
        [Parameter(Mandatory)][bool]$Remove,
        [Parameter(Mandatory)][scriptblock]$ResolveExactTaskAction,
        [Parameter(Mandatory)][scriptblock]$AssertOwnedTaskAction,
        [Parameter(Mandatory)][scriptblock]$DisableAction,
        [Parameter(Mandatory)][scriptblock]$StopAction,
        [Parameter(Mandatory)][scriptblock]$UnregisterAction
    )

    $errors = [Collections.Generic.List[string]]::new()
    $operationAttempts = [Collections.Generic.List[string]]::new()
    $unsafeOrForeign = $false
    $operations = [Collections.Generic.List[object]]::new()
    $operations.Add([PSCustomObject]@{ Name = 'Disable'; Action = $DisableAction })
    $operations.Add([PSCustomObject]@{ Name = 'Stop'; Action = $StopAction })
    if ($Remove) {
        $operations.Add([PSCustomObject]@{ Name = 'Unregister'; Action = $UnregisterAction })
    }

    foreach ($operation in $operations) {
        try {
            $task = & $ResolveExactTaskAction
        }
        catch {
            $errors.Add("$($operation.Name) resolve failed: $($_.Exception.Message)")
            continue
        }
        if ($null -eq $task) {
            break
        }
        try {
            & $AssertOwnedTaskAction $task
        }
        catch {
            $unsafeOrForeign = $true
            $errors.Add("$($operation.Name) ownership attestation failed: $($_.Exception.Message)")
            continue
        }
        $operationAttempts.Add($operation.Name)
        try {
            & $operation.Action $task
        }
        catch {
            $errors.Add("$($operation.Name) failed: $($_.Exception.Message)")
        }
    }

    $boundaryEstablished = $false
    $endState = 'Unproven'
    try {
        $finalTask = & $ResolveExactTaskAction
        if ($null -eq $finalTask) {
            $boundaryEstablished = $true
            $endState = 'Absent'
        }
        else {
            try {
                & $AssertOwnedTaskAction $finalTask
                $finalEnabled = [bool]$finalTask.Settings.Enabled
                $finalState = [string]$finalTask.State
                if (-not $finalEnabled -and $finalState -in @('Ready', 'Disabled')) {
                    $boundaryEstablished = $true
                    $endState = 'DisabledStopped'
                }
                else {
                    $errors.Add("Final managed task remained enabled or runnable (enabled=$finalEnabled state=$finalState).")
                }
            }
            catch {
                $unsafeOrForeign = $true
                $errors.Add("Final ownership attestation failed: $($_.Exception.Message)")
            }
        }
    }
    catch {
        $errors.Add("Final task resolution failed: $($_.Exception.Message)")
    }
    return [PSCustomObject]@{
        BoundaryEstablished = $boundaryEstablished
        EndState = $endState
        OperationAttempts = @($operationAttempts)
        Errors = @($errors)
        UnsafeOrForeign = $unsafeOrForeign
    }
}

function Invoke-WslRunnerTaskRegistrationTransaction {
    param(
        [Parameter(Mandatory)][scriptblock]$PrepareAction,
        [Parameter(Mandatory)][scriptblock]$RegisterAction,
        [Parameter(Mandatory)][scriptblock]$ResolveExactTaskAction,
        [Parameter(Mandatory)][scriptblock]$AttestAction,
        [Parameter(Mandatory)][scriptblock]$IsolationAction
    )

    $preparation = & $PrepareAction
    if ($null -eq $preparation -or -not [bool]$preparation.BoundaryEstablished) {
        $detail = if ($preparation -and $preparation.PSObject.Properties['Errors']) {
            @($preparation.Errors) -join '; '
        }
        else {
            'no preparation evidence was returned'
        }
        throw "The pre-registration task boundary was not established: $detail"
    }
    try {
        $registeredTask = & $RegisterAction
        if ($null -eq $registeredTask) {
            throw 'Task Scheduler registration returned no task object.'
        }
        $resolvedTask = & $ResolveExactTaskAction
        if ($null -eq $resolvedTask) {
            throw 'The registered task could not be resolved by its exact root name.'
        }
        & $AttestAction $resolvedTask
        return $resolvedTask
    }
    catch {
        $registrationError = $_
        try {
            $isolation = & $IsolationAction
        }
        catch {
            throw [InvalidOperationException]::new(
                "Scheduled-task registration failed and isolation threw: $($_.Exception.Message)",
                $registrationError.Exception
            )
        }
        if ($null -eq $isolation -or -not [bool]$isolation.BoundaryEstablished) {
            $detail = if ($isolation -and $isolation.PSObject.Properties['Errors']) {
                @($isolation.Errors) -join '; '
            }
            else {
                'no isolation evidence was returned'
            }
            throw [InvalidOperationException]::new(
                "Scheduled-task registration failed and a safe task boundary was not established: $detail",
                $registrationError.Exception
            )
        }
        throw $registrationError
    }
}

function Invoke-VerifiedWslImport {
    param(
        [Parameter(Mandatory)][uri]$ImageUri,
        [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedSha256,
        [Parameter(Mandatory)][string]$TemporaryRoot,
        [Parameter(Mandatory)][string]$DistroName,
        [Parameter(Mandatory)][string]$InstallLocation,
        [Parameter(Mandatory)][scriptblock]$DownloadAction,
        [Parameter(Mandatory)][scriptblock]$ImportAction,
        [Parameter(Mandatory)][scriptblock]$RollbackAction
    )

    if (-not [String]::Equals($ImageUri.Scheme, 'https', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The WSL image URI must use HTTPS.'
    }
    $temporaryRootPath = [IO.Path]::GetFullPath($TemporaryRoot)
    if (-not [IO.Directory]::Exists($temporaryRootPath)) {
        throw 'The WSL image temporary directory does not exist.'
    }
    $imagePrefix = Join-Path $temporaryRootPath 'degen-dogs-ubuntu-24.04.4-'
    $imagePath = [IO.Path]::GetFullPath(
        ($imagePrefix + [Guid]::NewGuid().ToString('N') + '.wsl')
    )
    if (-not $imagePath.StartsWith($imagePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Could not construct a bounded WSL image temporary path.'
    }

    try {
        & $DownloadAction $ImageUri.AbsoluteUri $imagePath
        if (-not (Test-Path -LiteralPath $imagePath -PathType Leaf)) {
            throw 'The WSL image download did not create a regular file.'
        }
        $imageItem = Get-Item -LiteralPath $imagePath -Force
        if ($imageItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'The downloaded WSL image is a reparse point.'
        }
        $imageStream = [IO.File]::OpenRead($imagePath)
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try {
            $actualSha256 = (
                [BitConverter]::ToString($sha256.ComputeHash($imageStream))
            ).Replace('-', '').ToLowerInvariant()
        }
        finally {
            $sha256.Dispose()
            $imageStream.Dispose()
        }
        if (-not [String]::Equals($actualSha256, $ExpectedSha256, [StringComparison]::Ordinal)) {
            throw 'The downloaded WSL image SHA-256 does not match the reviewed digest.'
        }

        try {
            & $ImportAction $DistroName $InstallLocation $imagePath
        }
        catch {
            $importError = $_
            try {
                & $RollbackAction $DistroName $InstallLocation
            }
            catch {
                throw [InvalidOperationException]::new(
                    "WSL import failed and rollback also failed: $($_.Exception.Message)",
                    $importError.Exception
                )
            }
            throw $importError
        }
    }
    finally {
        if ([IO.File]::Exists($imagePath)) {
            $verifiedParent = [IO.Path]::GetFullPath((Split-Path -Parent $imagePath))
            if (-not [String]::Equals(
                $verifiedParent,
                $temporaryRootPath.TrimEnd([IO.Path]::DirectorySeparatorChar),
                [StringComparison]::OrdinalIgnoreCase
            )) {
                throw 'Refusing to remove a WSL image outside the bounded temporary directory.'
            }
            Remove-Item -LiteralPath $imagePath -Force
        }
    }
}

function Assert-WslRunnerScheduledTaskXml {
    param(
        [Parameter(Mandatory)][string]$XmlText,
        [Parameter(Mandatory)][string]$ExpectedUserSid,
        [Parameter(Mandatory)][ValidateSet('InteractiveToken', 'Password')][string]$ExpectedLogonType,
        [Parameter(Mandatory)][string]$ExpectedWslPath,
        [Parameter(Mandatory)][string]$ExpectedArguments,
        [Parameter(Mandatory)][bool]$AtLogOnOnly,
        [Parameter(Mandatory)][bool]$ExpectedEnabled
    )

    [xml]$taskXml = $XmlText
    $principals = @($taskXml.SelectNodes("/*[local-name()='Task']/*[local-name()='Principals']/*[local-name()='Principal']"))
    if ($principals.Count -ne 1) {
        throw "Expected one scheduled-task principal, found $($principals.Count)."
    }
    $principal = $principals[0]
    $principalUser = $principal.SelectSingleNode("./*[local-name()='UserId']")
    $logonType = $principal.SelectSingleNode("./*[local-name()='LogonType']")
    $runLevel = $principal.SelectSingleNode("./*[local-name()='RunLevel']")
    if (-not $principalUser -or -not [String]::Equals(
        (Resolve-WslRunnerUserSid -UserId $principalUser.InnerText),
        $ExpectedUserSid,
        [StringComparison]::Ordinal
    )) {
        throw 'The scheduled-task principal is not the exact current-user SID.'
    }
    if (-not $logonType -or $logonType.InnerText -ne $ExpectedLogonType) {
        throw "The scheduled-task logon type is not $ExpectedLogonType."
    }
    if ($runLevel -and $runLevel.InnerText -ne 'LeastPrivilege') {
        throw 'The scheduled-task run level is not LeastPrivilege.'
    }

    $actions = @($taskXml.SelectNodes("/*[local-name()='Task']/*[local-name()='Actions']/*"))
    if ($actions.Count -ne 1 -or $actions[0].LocalName -ne 'Exec') {
        throw 'The scheduled task must contain exactly one Exec action.'
    }
    $command = $actions[0].SelectSingleNode("./*[local-name()='Command']")
    $arguments = $actions[0].SelectSingleNode("./*[local-name()='Arguments']")
    if (-not $command -or -not [String]::Equals($command.InnerText, $ExpectedWslPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The scheduled-task executable is not the exact System32 wsl.exe path.'
    }
    if (-not $arguments -or -not [String]::Equals($arguments.InnerText, $ExpectedArguments, [StringComparison]::Ordinal)) {
        throw 'The scheduled-task arguments do not match the exact runner anchor invocation.'
    }

    $triggers = @($taskXml.SelectNodes("/*[local-name()='Task']/*[local-name()='Triggers']/*"))
    $expectedKinds = if ($AtLogOnOnly) {
        @('LogonTrigger', 'TimeTrigger')
    }
    else {
        @('BootTrigger', 'LogonTrigger', 'TimeTrigger')
    }
    $actualKinds = @($triggers | ForEach-Object { $_.LocalName } | Sort-Object)
    $sortedExpectedKinds = @($expectedKinds | Sort-Object)
    if (($actualKinds -join ',') -ne ($sortedExpectedKinds -join ',')) {
        throw "The scheduled-task trigger set is unsafe: $($actualKinds -join ',')."
    }
    foreach ($trigger in $triggers) {
        $triggerEnabled = $trigger.SelectSingleNode("./*[local-name()='Enabled']")
        # Task Scheduler omits this node when it uses the schema default (true).
        if ($triggerEnabled -and $triggerEnabled.InnerText -ne 'true') {
            throw "The scheduled-task $($trigger.LocalName) is not enabled."
        }
    }
    $logonTriggers = @($triggers | Where-Object { $_.LocalName -eq 'LogonTrigger' })
    $logonUser = $logonTriggers[0].SelectSingleNode("./*[local-name()='UserId']")
    if (-not $logonUser -or -not [String]::Equals(
        (Resolve-WslRunnerUserSid -UserId $logonUser.InnerText),
        $ExpectedUserSid,
        [StringComparison]::Ordinal
    )) {
        throw 'The logon trigger is not restricted to the exact current-user SID.'
    }
    $timeTriggers = @($triggers | Where-Object { $_.LocalName -eq 'TimeTrigger' })
    $interval = $timeTriggers[0].SelectSingleNode("./*[local-name()='Repetition']/*[local-name()='Interval']")
    $duration = $timeTriggers[0].SelectSingleNode("./*[local-name()='Repetition']/*[local-name()='Duration']")
    if (-not $interval -or $interval.InnerText -ne 'PT5M') {
        throw 'The scheduled-task watchdog interval is not five minutes.'
    }
    if (-not $duration -or $duration.InnerText -ne 'P3650D') {
        throw 'The scheduled-task watchdog duration is not 3650 days.'
    }

    $settings = @($taskXml.SelectNodes("/*[local-name()='Task']/*[local-name()='Settings']"))
    if ($settings.Count -ne 1) {
        throw "Expected one scheduled-task Settings element, found $($settings.Count)."
    }
    $multipleInstances = $settings[0].SelectSingleNode("./*[local-name()='MultipleInstancesPolicy']")
    $startWhenAvailable = $settings[0].SelectSingleNode("./*[local-name()='StartWhenAvailable']")
    $disallowBatteryStart = $settings[0].SelectSingleNode("./*[local-name()='DisallowStartIfOnBatteries']")
    $stopOnBattery = $settings[0].SelectSingleNode("./*[local-name()='StopIfGoingOnBatteries']")
    $wakeToRun = $settings[0].SelectSingleNode("./*[local-name()='WakeToRun']")
    $restartCount = $settings[0].SelectSingleNode("./*[local-name()='RestartOnFailure']/*[local-name()='Count']")
    $restartInterval = $settings[0].SelectSingleNode("./*[local-name()='RestartOnFailure']/*[local-name()='Interval']")
    $executionLimit = $settings[0].SelectSingleNode("./*[local-name()='ExecutionTimeLimit']")
    $enabled = $settings[0].SelectSingleNode("./*[local-name()='Enabled']")
    if (-not $multipleInstances -or $multipleInstances.InnerText -ne 'IgnoreNew') {
        throw 'The scheduled-task multiple-instance policy is not IgnoreNew.'
    }
    if (-not $startWhenAvailable -or $startWhenAvailable.InnerText -ne 'true') {
        throw 'The scheduled task does not enable StartWhenAvailable.'
    }
    if (-not $disallowBatteryStart -or $disallowBatteryStart.InnerText -ne 'false') {
        throw 'The scheduled task is not allowed to start on battery power.'
    }
    if (-not $stopOnBattery -or $stopOnBattery.InnerText -ne 'false') {
        throw 'The scheduled task may stop when switching to battery power.'
    }
    if (-not $wakeToRun -or $wakeToRun.InnerText -ne 'true') {
        throw 'The scheduled task does not enable WakeToRun.'
    }
    if (-not $restartCount -or $restartCount.InnerText -ne '999' -or
        -not $restartInterval -or $restartInterval.InnerText -ne 'PT1M') {
        throw 'The scheduled-task restart policy is not 999 attempts at one-minute intervals.'
    }
    if (-not $executionLimit -or $executionLimit.InnerText -notin @('PT0S', 'P0D')) {
        throw 'The scheduled task does not have an unlimited execution time.'
    }
    $expectedEnabledText = if ($ExpectedEnabled) { 'true' } else { 'false' }
    if (-not $enabled -or $enabled.InnerText -ne $expectedEnabledText) {
        throw "The scheduled-task enabled state is not $expectedEnabledText."
    }
}

function Assert-WslRunnerManagedTaskXml {
    param(
        [Parameter(Mandatory)][string]$XmlText,
        [Parameter(Mandatory)][string]$ExpectedUserSid,
        [Parameter(Mandatory)][string]$ExpectedWslPath,
        [Parameter(Mandatory)][string]$ExpectedArguments,
        [Parameter(Mandatory)][bool]$ExpectedEnabled
    )

    # These are the only task schemas this installer can leave behind:
    # current-user fallback, elevated bootstrap, and elevated activation.
    $managedSchemas = @(
        [PSCustomObject]@{ LogonType = 'InteractiveToken'; AtLogOnOnly = $true },
        [PSCustomObject]@{ LogonType = 'InteractiveToken'; AtLogOnOnly = $false },
        [PSCustomObject]@{ LogonType = 'Password'; AtLogOnOnly = $false }
    )
    $failures = [Collections.Generic.List[string]]::new()
    foreach ($schema in $managedSchemas) {
        try {
            Assert-WslRunnerScheduledTaskXml `
                -XmlText $XmlText `
                -ExpectedUserSid $ExpectedUserSid `
                -ExpectedLogonType $schema.LogonType `
                -ExpectedWslPath $ExpectedWslPath `
                -ExpectedArguments $ExpectedArguments `
                -AtLogOnOnly ([bool]$schema.AtLogOnOnly) `
                -ExpectedEnabled $ExpectedEnabled
            return
        }
        catch {
            $failures.Add($_.Exception.Message)
        }
    }
    throw "The scheduled task does not match any exact managed predecessor schema: $($failures -join ' | ')"
}

$isAdministrator = Test-CurrentProcessIsAdministrator
Assert-WslRunnerInvocationPolicy `
    -IsAdministrator $isAdministrator `
    -AtLogOnOnly ([bool]$AtLogOnOnly) `
    -HasCredential ($null -ne $Credential)

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

function Get-WslRunnerGitPath {
    param([scriptblock]$ResolveAction)

    $commands = if ($ResolveAction) {
        @(& $ResolveAction)
    }
    else {
        @(Get-Command git.exe -CommandType Application -All -ErrorAction Stop)
    }
    if ($commands.Count -eq 0) {
        throw 'git.exe is unavailable.'
    }
    $source = [string]$commands[0].Source
    if ([String]::IsNullOrWhiteSpace($source)) {
        throw 'The selected git.exe command has no executable source path.'
    }
    return [IO.Path]::GetFullPath($source)
}

function Remove-WslRunnerTemporaryGitDirectory {
    param(
        [Parameter(Mandatory)][string]$TemporaryRoot,
        [Parameter(Mandatory)][string]$Stage
    )

    $temporaryRootPath = [IO.Path]::GetFullPath($TemporaryRoot).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $stagePath = [IO.Path]::GetFullPath($Stage).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $stageParent = [IO.Path]::GetFullPath((Split-Path -Parent $stagePath)).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $stageLeaf = Split-Path -Leaf $stagePath
    if (-not [String]::Equals(
        $stageParent,
        $temporaryRootPath,
        [StringComparison]::OrdinalIgnoreCase
    ) -or $stageLeaf -cnotmatch '^degen-dogs-bootstrap-source-[0-9a-f]{32}$') {
        throw 'Refusing to remove a Git source stage outside the exact bounded temporary path.'
    }
    if (-not [IO.Directory]::Exists($stagePath)) {
        return
    }
    $stageItem = Get-Item -LiteralPath $stagePath -Force
    if (-not $stageItem.PSIsContainer -or
        ($stageItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Refusing to remove a Git source stage that is not an ordinary directory.'
    }
    $entries = @(Get-ChildItem -LiteralPath $stagePath -Force -Recurse -ErrorAction Stop)
    if (@($entries | Where-Object {
        $_.Attributes -band [IO.FileAttributes]::ReparsePoint
    }).Count -ne 0) {
        throw 'Refusing to remove a Git source stage containing a reparse point.'
    }
    foreach ($entry in @($entries) + @($stageItem)) {
        if ($entry.Attributes -band [IO.FileAttributes]::ReadOnly) {
            $attributes = [IO.FileAttributes]([int]$entry.Attributes -band
                (-bnot [int][IO.FileAttributes]::ReadOnly))
            [IO.File]::SetAttributes($entry.FullName, $attributes)
        }
    }
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        if (-not [IO.Directory]::Exists($stagePath)) {
            return
        }
        try {
            [IO.Directory]::Delete($stagePath, $true)
            return
        }
        catch {
            if ($attempt -eq 3) { throw }
            Start-Sleep -Milliseconds (100 * $attempt)
        }
    }
}

function Assert-TrustedBootstrapSource {
    param([Parameter(Mandatory)][string]$Commit)

    $gitPath = Get-WslRunnerGitPath
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
        Remove-WslRunnerTemporaryGitDirectory `
            -TemporaryRoot $temporaryRoot `
            -Stage $stage
    }
}

# This detects accidental checkout/argument mismatches before host state is
# changed. It cannot make already-malicious local PowerShell trustworthy.
Assert-TrustedBootstrapSource -Commit $TrustedInstallerCommit

$wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
if (-not (Test-Path -LiteralPath $wsl)) {
    throw 'wsl.exe is unavailable. Enable Windows Subsystem for Linux first.'
}
$currentUserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$taskPlan = New-WslRunnerTaskPlan `
    -AtLogOnOnly ([bool]$AtLogOnOnly) `
    -Activate ([bool]$Activate) `
    -UserSid $currentUserSid `
    -WslPath $wsl `
    -DistroName $DistroName

function Get-WslDistros {
    param([scriptblock]$ListAction)

    $listArguments = @('--list', '--all', '--quiet')
    if ($ListAction) {
        $result = & $ListAction $listArguments
    }
    else {
        $output = @(& $wsl @listArguments 2>&1)
        $result = [PSCustomObject]@{
            ExitCode = $LASTEXITCODE
            Output = $output
        }
    }
    if ($null -eq $result -or
        $null -eq $result.PSObject.Properties['ExitCode'] -or
        $null -eq $result.PSObject.Properties['Output']) {
        throw 'The WSL inventory action returned an invalid result.'
    }
    $exitCode = [int]$result.ExitCode
    $outputLines = @($result.Output | ForEach-Object { $_.ToString() })
    if ($exitCode -ne 0) {
        $detail = ($outputLines | Where-Object { $_ }) -join "`n"
        throw "Could not enumerate WSL distributions (exit=$exitCode): $detail"
    }
    $raw = ($outputLines -join "`n") -replace "`0", ''
    return @($raw -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Get-WslDistroRegistration {
    param([Parameter(Mandatory)][string]$Name)

    $lxssRoot = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
    if (-not (Test-Path -LiteralPath $lxssRoot)) {
        return $null
    }
    $matches = @(
        Get-ChildItem -LiteralPath $lxssRoot -ErrorAction Stop |
            ForEach-Object {
                $properties = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction Stop
                if ([String]::Equals(
                    [string]$properties.DistributionName,
                    $Name,
                    [StringComparison]::Ordinal
                )) {
                    [PSCustomObject]@{
                        Name = [string]$properties.DistributionName
                        BasePath = [string]$properties.BasePath
                        Version = [int]$properties.Version
                        RegistryPath = $_.PSPath
                    }
                }
            }
    )
    if ($matches.Count -gt 1) {
        throw "Multiple WSL registrations unexpectedly use the exact name '$Name'."
    }
    if ($matches.Count -eq 1) {
        return $matches[0]
    }
    return $null
}

function Assert-WslRunnerDistroIdentity {
    param([Parameter(Mandatory)][string]$Name)

    $verboseListing = ((& $wsl --list --verbose 2>&1 | Out-String) -replace "`0", '')
    if ($LASTEXITCODE -ne 0 -or $verboseListing -notmatch (
        '(?m)^\s*\*?\s*' + [Regex]::Escape($Name) + '\s+\S+\s+2\s*$'
    )) {
        throw "Distro '$Name' was not verified as WSL version 2."
    }
    & $wsl `
        --distribution $Name `
        --user root `
        --exec /bin/sh -c `
        '. /etc/os-release && test "$ID" = ubuntu && test "$VERSION_ID" = 24.04 && test "$(dpkg --print-architecture)" = amd64'
    if ($LASTEXITCODE -ne 0) {
        throw "Distro '$Name' is not Ubuntu 24.04 AMD64."
    }
}

function Test-WslRunnerOwnershipMarker {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{32}$')][string]$AttemptId
    )

    $expectedMarker = Get-WslRunnerOwnershipMarkerValue -AttemptId $AttemptId
    & $wsl `
        --distribution $Name `
        --user root `
        --exec /bin/sh -c `
        'marker=/var/lib/degen-dogs/windows-runner-owned; test -f "$marker" && test ! -L "$marker" && test "$(stat -c %U "$marker")" = root && test "$(stat -c %G "$marker")" = root && test "$(stat -c %a "$marker")" = 600 && test "$(stat -c %h "$marker")" = 1 && test "$(tr -d "\r\n" <"$marker")" = "$1"' `
        sh $expectedMarker `
        *> $null
    return $LASTEXITCODE -eq 0
}

function Set-WslRunnerOwnershipMarker {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{32}$')][string]$AttemptId
    )

    $markerValue = Get-WslRunnerOwnershipMarkerValue -AttemptId $AttemptId
    & $wsl `
        --distribution $Name `
        --user root `
        --exec /bin/sh -c `
        'set -eu; install -d -o root -g root -m 0755 /var/lib/degen-dogs; tmp=$(mktemp /var/lib/degen-dogs/.windows-runner-owned.XXXXXX); printf "%s\n" "$1" >"$tmp"; install -o root -g root -m 0600 "$tmp" /var/lib/degen-dogs/windows-runner-owned; rm -f -- "$tmp"' `
        sh $markerValue
    if ($LASTEXITCODE -ne 0 -or -not (Test-WslRunnerOwnershipMarker -Name $Name -AttemptId $AttemptId)) {
        throw "Could not establish the root-owned ownership marker in distro '$Name'."
    }
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
    param(
        [Parameter(Mandatory)][string]$Name,
        [scriptblock]$QueryAction
    )

    if ($QueryAction) {
        $candidateTasks = @(& $QueryAction)
    }
    else {
        # Enumerating the exact root folder distinguishes an empty result from
        # provider, RPC, or access failures, all of which must stop the caller.
        $candidateTasks = @(
            Get-ScheduledTask `
                -TaskPath '\' `
                -ErrorAction Stop
        )
    }
    $nameCollisions = @(
        $candidateTasks | Where-Object {
            [String]::Equals($_.TaskName, $Name, [StringComparison]::OrdinalIgnoreCase)
        }
    )
    foreach ($collision in $nameCollisions) {
        if (-not [String]::Equals($collision.TaskName, $Name, [StringComparison]::Ordinal) -or
            -not [String]::Equals($collision.TaskPath, '\', [StringComparison]::Ordinal)) {
            throw "Task Scheduler returned a non-exact name or path collision for '$Name'."
        }
    }
    if ($nameCollisions.Count -gt 1) {
        throw "Multiple exact root Task Scheduler objects unexpectedly matched '$Name'."
    }
    if ($nameCollisions.Count -eq 1) {
        return $nameCollisions[0]
    }
    return $null
}

function Assert-WslRunnerOwnedTaskDefinition {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][bool]$ExpectedEnabled,
        [switch]$AllowManagedPredecessor
    )

    $exactTask = Get-ExactScheduledTask -Name $Name
    if (-not $exactTask) {
        throw "The exact root WSL keepalive task '$Name' does not exist."
    }
    $taskXml = Export-ScheduledTask `
        -TaskName $Name `
        -TaskPath '\' `
        -ErrorAction Stop
    if ($AllowManagedPredecessor) {
        Assert-WslRunnerManagedTaskXml `
            -XmlText $taskXml `
            -ExpectedUserSid $currentUserSid `
            -ExpectedWslPath $taskPlan.Executable `
            -ExpectedArguments $taskPlan.Arguments `
            -ExpectedEnabled $ExpectedEnabled
    }
    else {
        $expectedLogonType = if ($taskPlan.LogonType -eq 'Password') {
            'Password'
        }
        else {
            'InteractiveToken'
        }
        Assert-WslRunnerScheduledTaskXml `
            -XmlText $taskXml `
            -ExpectedUserSid $currentUserSid `
            -ExpectedLogonType $expectedLogonType `
            -ExpectedWslPath $taskPlan.Executable `
            -ExpectedArguments $taskPlan.Arguments `
            -AtLogOnOnly ([bool]$AtLogOnOnly) `
            -ExpectedEnabled $ExpectedEnabled
    }
    return $exactTask
}

function Assert-WslRunnerAtLogOnTaskDefinition {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][bool]$ExpectedEnabled
    )

    if (-not $AtLogOnOnly) {
        throw 'AtLogOn task attestation was requested outside current-user mode.'
    }
    return Assert-WslRunnerOwnedTaskDefinition `
        -Name $Name `
        -ExpectedEnabled $ExpectedEnabled
}

function Invoke-CurrentWslRunnerTaskIsolation {
    param([Parameter(Mandatory)][bool]$Remove)

    return Invoke-WslRunnerTaskIsolation `
        -Remove $Remove `
        -ResolveExactTaskAction { Get-ExactScheduledTask -Name $TaskName } `
        -AssertOwnedTaskAction {
            param($task)
            Assert-WslRunnerOwnedTaskDefinition `
                -Name $TaskName `
                -ExpectedEnabled ([bool]$task.Settings.Enabled) `
                -AllowManagedPredecessor |
                Out-Null
        } `
        -DisableAction { param($task) $task | Disable-ScheduledTask | Out-Null } `
        -StopAction { param($task) $task | Stop-ScheduledTask -ErrorAction Stop } `
        -UnregisterAction { param($task) $task | Unregister-ScheduledTask -Confirm:$false }
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

$perUserInstallBasePlan = $null
$perUserImportAttempt = $null
$runnerDistroLock = $null
$runnerTaskLock = $null
if ($AtLogOnOnly) {
    if (-not [String]::Equals(
        $env:PROCESSOR_ARCHITECTURE,
        'AMD64',
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'The reviewed per-user WSL image supports AMD64 hosts only.'
    }
    & $wsl --status *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'The per-user fallback requires an already-operational WSL2 runtime.'
    }
}
$knownLocalAppData = Get-WslRunnerKnownLocalAppData
$lockBasePlan = Initialize-WslRunnerInstallBase `
    -KnownLocalAppData $knownLocalAppData
$runnerTaskLock = Enter-WslRunnerTaskLock `
    -KnownLocalAppData $lockBasePlan.KnownLocalAppData `
    -InstallBase $lockBasePlan.Base `
    -UserSid $currentUserSid `
    -TaskName $TaskName

try {
$runnerDistroLock = Enter-WslRunnerDistroLock `
    -KnownLocalAppData $lockBasePlan.KnownLocalAppData `
    -InstallBase $lockBasePlan.Base `
    -UserSid $currentUserSid `
    -DistroName $DistroName
if ($AtLogOnOnly) {
    $perUserInstallBasePlan = $lockBasePlan
}
$distroInventory = @(Get-WslDistros)
$distroAlreadyExists = $distroInventory -contains $DistroName
$trustedInstallerExists = $false
$installedTrustedCommit = ''
$perUserOwnershipMarkerExists = $false
if ($distroAlreadyExists) {
    if ($AtLogOnOnly) {
        $registration = Get-WslDistroRegistration -Name $DistroName
        if (-not $registration) {
            throw "Existing distro '$DistroName' has no inspectable current-user WSL registration."
        }
        $perUserImportAttempt = Get-WslRunnerImportAttemptFromRegistration `
            -Registration $registration `
            -KnownLocalAppData $perUserInstallBasePlan.KnownLocalAppData `
            -DistroName $DistroName
        Assert-WslRunnerDistroIdentity -Name $DistroName
        $perUserOwnershipMarkerExists = Test-WslRunnerOwnershipMarker `
            -Name $DistroName `
            -AttemptId $perUserImportAttempt.Id
    }
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
if ($AtLogOnOnly -and $distroAlreadyExists -and -not (
    $perUserOwnershipMarkerExists -or $trustedBundleExists
)) {
    throw "Existing distro '$DistroName' has no verified Degen Dogs ownership marker or frozen trusted bundle."
}
if ($AtLogOnOnly -and $distroAlreadyExists -and -not $perUserOwnershipMarkerExists) {
    Set-WslRunnerOwnershipMarker `
        -Name $DistroName `
        -AttemptId $perUserImportAttempt.Id
    $perUserOwnershipMarkerExists = $true
}

if ($Uninstall) {
    $task = Get-ExactScheduledTask -Name $TaskName
    if ($task) {
        $taskIsolation = Invoke-CurrentWslRunnerTaskIsolation -Remove $true
        if (-not $taskIsolation.BoundaryEstablished) {
            throw "The existing task could not be safely isolated for uninstall: $(@($taskIsolation.Errors) -join '; ')"
        }
    }
    if ($distroAlreadyExists) {
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
    $existingTaskIsolation = Invoke-CurrentWslRunnerTaskIsolation -Remove $false
    if (-not $existingTaskIsolation.BoundaryEstablished) {
        throw "The existing task could not be safely disabled and stopped: $(@($existingTaskIsolation.Errors) -join '; ')"
    }
}

if (-not $distroAlreadyExists) {
    Write-Host "Installing an isolated Ubuntu 24.04 WSL2 distro named $DistroName..."
    if ($AtLogOnOnly) {
        $perUserImportAttempt = New-WslRunnerImportAttempt `
            -KnownLocalAppData $perUserInstallBasePlan.KnownLocalAppData `
            -DistroName $DistroName
        $locationPlan = $perUserImportAttempt

        $curlPath = Join-Path $env:SystemRoot 'System32\curl.exe'
        if (-not (Test-Path -LiteralPath $curlPath -PathType Leaf)) {
            throw 'The System32 curl.exe required for the pinned WSL image download is unavailable.'
        }
        $imageSpec = Get-WslRunnerImageSpec
        $downloadImage = {
            param($uri, $destination)
            $downloadOutput = @(
                & $curlPath `
                    --proto '=https' `
                    --proto-redir '=https' `
                    --tlsv1.2 `
                    --fail `
                    --silent `
                    --show-error `
                    --location `
                    --retry 3 `
                    --retry-all-errors `
                    --connect-timeout 20 `
                    --output $destination `
                    $uri 2>&1
            )
            if ($LASTEXITCODE -ne 0) {
                $detail = ($downloadOutput | ForEach-Object { $_.ToString() }) -join "`n"
                throw "The pinned Ubuntu WSL image download failed (exit=$LASTEXITCODE): $detail"
            }
        }
        $importDistro = {
            param($name, $location, $imagePath)
            $importArguments = @(Get-WslRunnerImportArguments `
                -DistroName $name `
                -InstallLocation $location `
                -ImagePath $imagePath)
            $importOutput = @(& $wsl @importArguments 2>&1)
            if ($LASTEXITCODE -ne 0) {
                $detail = ($importOutput | ForEach-Object { $_.ToString() }) -join "`n"
                throw "The verified WSL distro import failed (exit=$LASTEXITCODE): $detail"
            }
            $perUserImportAttempt.ImportCommandSucceeded = $true
            $postImportInventory = @(Get-WslDistros)
            if (-not ($postImportInventory -contains $name)) {
                throw 'The verified WSL import returned success without registering the exact distro.'
            }
            $registration = Get-WslDistroRegistration -Name $name
            if (-not $registration -or -not (Test-WslDistroRegistrationMatches `
                -Registration $registration `
                -DistroName $name `
                -InstallLocation $location)) {
                throw 'The imported distro registration does not own the exact bounded WSL2 location.'
            }
            Assert-WslRunnerDistroIdentity -Name $name
            Set-WslRunnerOwnershipMarker `
                -Name $name `
                -AttemptId $perUserImportAttempt.Id
        }
        $rollbackImport = {
            param($name, $location)
            $unregisterAttempt = {
                param($registeredName)
                & $wsl --unregister $registeredName
                if ($LASTEXITCODE -ne 0) {
                    throw "Could not unregister the exact failed import '$registeredName'."
                }
            }
            Invoke-WslRunnerImportRollback `
                -Attempt $perUserImportAttempt `
                -GetInventoryAction { @(Get-WslDistros) } `
                -GetRegistrationAction { param($registeredName) Get-WslDistroRegistration -Name $registeredName } `
                -UnregisterAction $unregisterAttempt `
                -RemoveAction { param($attempt) Remove-BoundedWslImportDirectory -Attempt $attempt }
        }
        Invoke-VerifiedWslImport `
            -ImageUri $imageSpec.Uri `
            -ExpectedSha256 $imageSpec.Sha256 `
            -TemporaryRoot ([IO.Path]::GetTempPath()) `
            -DistroName $DistroName `
            -InstallLocation $locationPlan.Location `
            -DownloadAction $downloadImage `
            -ImportAction $importDistro `
            -RollbackAction $rollbackImport
        $distroAlreadyExists = $true
    }
    else {
        & $wsl --install Ubuntu-24.04 --name $DistroName --version 2 --no-launch
        if ($LASTEXITCODE -ne 0) {
            throw 'WSL distro installation failed. If Windows requested a reboot, reboot and rerun this script.'
        }
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
    -Execute $taskPlan.Executable `
    -Argument $taskPlan.Arguments
$selectedTriggers = [Collections.Generic.List[object]]::new()
foreach ($triggerKind in $taskPlan.TriggerKinds) {
    switch ($triggerKind) {
        'Startup' {
            $selectedTriggers.Add((New-ScheduledTaskTrigger -AtStartup))
        }
        'Logon' {
            $selectedTriggers.Add((New-ScheduledTaskTrigger -AtLogOn -User $currentUserSid))
        }
        'Watchdog' {
            $selectedTriggers.Add((New-ScheduledTaskTrigger `
                -Once `
                -At (Get-Date).AddMinutes(2) `
                -RepetitionInterval (New-TimeSpan -Minutes 5) `
                -RepetitionDuration (New-TimeSpan -Days 3650)))
        }
        default {
            throw "Unexpected scheduled-task trigger kind '$triggerKind'."
        }
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
    $plainPassword = $null
    if ($AtLogOnOnly) {
        $principal = New-ScheduledTaskPrincipal `
            -UserId $taskPlan.UserId `
            -LogonType Interactive `
            -RunLevel Limited
        $registerTaskAction = {
            Register-ScheduledTask `
                -TaskName $TaskName `
                -TaskPath '\' `
                -Action $action `
                -Trigger $selectedTriggers.ToArray() `
                -Settings $settings `
                -Principal $principal `
                -Description 'Keeps the Degen Dogs systemd publisher alive in WSL2; real jobs remain least-privilege Linux services.'
        }
    }
    else {
        $plainPassword = $Credential.GetNetworkCredential().Password
        $registerTaskAction = {
            Register-ScheduledTask `
                -TaskName $TaskName `
                -TaskPath '\' `
                -Action $action `
                -Trigger $selectedTriggers.ToArray() `
                -Settings $settings `
                -User $Credential.UserName `
                -Password $plainPassword `
                -RunLevel Limited `
                -Description 'Keeps the Degen Dogs systemd publisher alive in WSL2; real jobs remain least-privilege Linux services.'
        }
    }
    $resolveRegisteredTaskAction = {
        Get-ExactScheduledTask -Name $TaskName
    }
    $attestTaskAction = {
        param($task)
        Assert-WslRunnerOwnedTaskDefinition `
            -Name $TaskName `
            -ExpectedEnabled $false |
            Out-Null
    }
    $isolateRegisteredTaskAction = {
        Invoke-CurrentWslRunnerTaskIsolation -Remove $true
    }
    # Registration and activation share one rollback boundary.
    try {
        try {
            $registeredTask = Invoke-WslRunnerTaskRegistrationTransaction `
                -PrepareAction $isolateRegisteredTaskAction `
                -RegisterAction $registerTaskAction `
                -ResolveExactTaskAction $resolveRegisteredTaskAction `
                -AttestAction $attestTaskAction `
                -IsolationAction $isolateRegisteredTaskAction
        }
        finally {
            $plainPassword = $null
        }

        # Activation is intentionally last. It fails closed unless the checked-out
        # peer-aware publisher, RPC quorum, watcher dry-run, and Git write dry-run
        # all pass inside WSL.
        $activation = @"
set -Eeuo pipefail
$runtimeStage
stage_runtime_and_install --skip-bootstrap --enable-now
"@
        Invoke-WslRoot -Script $activation
        $registeredTask | Enable-ScheduledTask | Out-Null
        $registeredTask = Assert-WslRunnerOwnedTaskDefinition `
            -Name $TaskName `
            -ExpectedEnabled $true
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
        $currentTask = Assert-WslRunnerOwnedTaskDefinition `
            -Name $TaskName `
            -ExpectedEnabled $true
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
            $taskIsolation = Invoke-CurrentWslRunnerTaskIsolation -Remove $true
        }
        catch {
            $taskIsolation = $null
            $rollbackClean = $false
            $rollbackFailures.Add("Windows task isolation threw: $($_.Exception.Message)")
        }
        if ($taskIsolation -and -not $taskIsolation.BoundaryEstablished) {
            $rollbackClean = $false
            $rollbackFailures.Add("Windows task isolation was unproven: $(@($taskIsolation.Errors) -join '; ')")
        }

        try {
            Invoke-WslRoot -Script $rollbackPublisher
        }
        catch {
            $rollbackClean = $false
            $rollbackFailures.Add("WSL publisher rollback or inactive-state verification failed: $($_.Exception.Message)")
        }

        if (-not $rollbackClean) {
            $preTerminationDetail = $rollbackFailures -join '; '
            Write-Warning "Activation rollback was not clean; terminating only '$DistroName' as the fail-closed WSL boundary. $preTerminationDetail"
            & $wsl --terminate $DistroName
            if ($LASTEXITCODE -ne 0) {
                $rollbackFailures.Add("fallback termination failed for '$DistroName' with exit code $LASTEXITCODE.")
            }
            else {
                if ($taskIsolation -and $taskIsolation.BoundaryEstablished) {
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
        -UserId $taskPlan.UserId `
        -LogonType Interactive `
        -RunLevel Limited
    $registerTaskAction = {
        Register-ScheduledTask `
            -TaskName $TaskName `
            -TaskPath '\' `
            -Action $action `
            -Trigger $selectedTriggers.ToArray() `
            -Settings $settings `
            -Principal $principal `
            -Description 'Disabled until the peer-aware publisher, RPC quorum, and GitHub deploy key pass preflight.'
    }
    $attestTaskAction = {
        param($task)
        Assert-WslRunnerOwnedTaskDefinition `
            -Name $TaskName `
            -ExpectedEnabled $false |
            Out-Null
    }
    $resolveRegisteredTaskAction = {
        Get-ExactScheduledTask -Name $TaskName
    }
    $isolateRegisteredTaskAction = {
        Invoke-CurrentWslRunnerTaskIsolation -Remove $true
    }
    $registeredTask = Invoke-WslRunnerTaskRegistrationTransaction `
        -PrepareAction $isolateRegisteredTaskAction `
        -RegisterAction $registerTaskAction `
        -ResolveExactTaskAction $resolveRegisteredTaskAction `
        -AttestAction $attestTaskAction `
        -IsolationAction $isolateRegisteredTaskAction
    Write-Host "Bootstrap complete. The systemd units and Windows task are disabled."
    Write-Host "Add the displayed public deploy key to GitHub with write access, fill $RepoDir/.env.local, then rerun with -Activate."
}
}
finally {
    if ($runnerDistroLock) {
        Exit-WslRunnerDistroLock -Lock $runnerDistroLock
    }
    if ($runnerTaskLock) {
        Exit-WslRunnerDistroLock -Lock $runnerTaskLock
    }
}
