# Start-WindowsRunnerSession.ps1
# Open a foreground GitHub Actions runner for a whole work session, with no service.
#requires -Version 7.0

[CmdletBinding()]
param(
    [string]$Repository,
    [string]$Architecture,
    [string]$RunnerRoot = "C:\actions-runners",
    [string]$Labels = "arena-session",
    [switch]$SelfTest
)

$ErrorActionPreference = "Stop"
$OriginalLocation = Get-Location
$SessionMutex = $null
$MutexHeld = $false
$SessionOpened = $false
$RegistrationReused = $false

function Write-Step {
    param([string]$Message)
    Write-Host "`n=== $Message ===" -ForegroundColor Cyan
}

function Invoke-NativeCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath exited with code $LASTEXITCODE."
    }
}

function Join-NativeOutputLines {
    param([string[]]$Lines)

    # Native commands emit one string per line. JSON must be reconstructed before
    # ConvertFrom-Json, otherwise PowerShell pipes each line into it separately.
    return ($Lines -join "`n")
}

function Invoke-GitHubApi {
    param([string[]]$Arguments)

    $CombinedOutput = @(& gh api @Arguments 2>&1)
    $NativeExitCode = $LASTEXITCODE
    if ($NativeExitCode -ne 0) {
        $Detail = ($CombinedOutput | ForEach-Object { $_.ToString() }) -join "`n"
        throw "GitHub API request failed (gh api $($Arguments -join ' ')).`n$Detail"
    }

    # Keep stdout clean if gh writes a warning to stderr during a successful call.
    $Output = @($CombinedOutput | Where-Object { $_ -is [string] })
    return Join-NativeOutputLines -Lines $Output
}

function Get-RepositoryFromOrigin {
    param([string]$RepositoryRoot)

    $Remote = & git -C $RepositoryRoot remote get-url origin 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Remote)) {
        throw "Could not read origin. Pass -Repository owner/repo explicitly."
    }

    $Remote = $Remote.Trim()
    if ($Remote -match '^https?://(?:[^@/]+@)?github\.com/(?<Path>.+)$') {
        $RepositoryPath = $Matches.Path
    }
    elseif ($Remote -match '^git@github\.com:(?<Path>.+)$') {
        $RepositoryPath = $Matches.Path
    }
    else {
        throw "Only github.com origins are supported. Pass -Repository owner/repo explicitly."
    }

    $RepositoryPath = ($RepositoryPath -replace '\.git$', '').Trim('/')
    if ($RepositoryPath -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
        throw "Could not derive owner/repo from origin: $Remote"
    }
    return $RepositoryPath
}

function Get-RunnerPackage {
    param(
        [object]$Release,
        [string]$Architecture
    )

    # Release assets are global; the repository download catalog can be empty
    # until a new repository has initialized Actions with its first run.
    $ExpectedName = "^actions-runner-win-$Architecture-\d+\.\d+\.\d+\.zip$"
    $Asset = $Release.assets |
        Where-Object { $_.name -match $ExpectedName } |
        Select-Object -First 1
    if ($null -eq $Asset) {
        $AvailableAssets = $Release.assets | ForEach-Object { $_.name }
        throw "Latest actions/runner release $($Release.tag_name) has no Windows/$Architecture ZIP. Assets: $($AvailableAssets -join ', ')"
    }
    if ([string]::IsNullOrWhiteSpace($Asset.browser_download_url)) {
        throw "Runner asset $($Asset.name) has no download URL."
    }

    return [pscustomobject]@{
        filename = $Asset.name
        download_url = $Asset.browser_download_url
        version = $Release.tag_name.TrimStart("v")
    }
}

function Confirm-GitHubActionsEnabled {
    param([string]$Repository)

    try {
        $Permissions = (Invoke-GitHubApi -Arguments @(
            "repos/$Repository/actions/permissions"
        )) | ConvertFrom-Json
    }
    catch {
        # Registration may still work with a token whose preflight access differs.
        Write-Host "Actions permissions could not be preflighted; registration will still be attempted."
        Write-Verbose ($_.Exception.Message)
        return
    }

    if ($Permissions.enabled -ne $false) {
        return
    }

    $SettingsUrl = "https://github.com/$Repository/settings/actions"
    $Confirmation = Read-Host "GitHub Actions is disabled for $Repository. Enable it now? [y/N]"
    if ([string]::IsNullOrWhiteSpace($Confirmation) -or
        $Confirmation.Trim() -notmatch "^(y|yes)$") {
        throw "Actions remains disabled. Enable it at $SettingsUrl and retry."
    }

    Invoke-GitHubApi -Arguments @(
        "--method", "PUT",
        "repos/$Repository/actions/permissions",
        "-F", "enabled=true"
    ) | Out-Null
    Write-Host "GitHub Actions is now enabled for $Repository."
}

function Get-ActionsSetupGuidance {
    param([string]$Repository)

    # These reads are best-effort: preserve the registration error even when the
    # same credentials cannot read one of the diagnostic endpoints.
    $RepositoryInfo = $null
    $Permissions = $null
    $Workflows = $null
    $Runs = $null
    try {
        $RepositoryInfo = (Invoke-GitHubApi -Arguments @("repos/$Repository")) | ConvertFrom-Json
    }
    catch {}
    try {
        $Permissions = (Invoke-GitHubApi -Arguments @(
            "repos/$Repository/actions/permissions"
        )) | ConvertFrom-Json
    }
    catch {}
    try {
        $Workflows = (Invoke-GitHubApi -Arguments @(
            "repos/$Repository/actions/workflows"
        )) | ConvertFrom-Json
    }
    catch {}
    try {
        $Runs = (Invoke-GitHubApi -Arguments @(
            "repos/$Repository/actions/runs?per_page=1"
        )) | ConvertFrom-Json
    }
    catch {}

    $ActionsUrl = "https://github.com/$Repository/actions"
    if ($null -ne $Permissions -and $Permissions.enabled -eq $false) {
        return "Actions is disabled. Enable it at https://github.com/$Repository/settings/actions and retry."
    }
    if ($null -ne $RepositoryInfo -and
        $RepositoryInfo.fork -eq $true -and
        $null -ne $Workflows -and
        $Workflows.total_count -eq 0) {
        return "This fork has no registered workflows. If workflow files were inherited, enable them at $ActionsUrl (separate from the general Actions permission); otherwise inspect the default branch."
    }
    if ($null -ne $Workflows -and $Workflows.total_count -eq 0) {
        return "No workflows are registered on the default branch. Inspect the Actions setup at $ActionsUrl."
    }
    if ($null -ne $Runs -and $Runs.total_count -eq 0) {
        return "Actions reports no workflow runs. For a new repository, one existing GitHub-hosted workflow run can finish backend initialization. Start it from $ActionsUrl, then retry registration."
    }
    return "Check that your local gh login can administer $Repository and inspect its Actions setup at $ActionsUrl."
}

if ($SelfTest) {
    $SampleJsonLines = @(
        '{',
        '  "tag_name": "v2.338.1",',
        '  "assets": [',
        '    {"name":"actions-runner-linux-x64-2.338.1.tar.gz","browser_download_url":"https://example.invalid/linux"},',
        '    {"name":"actions-runner-win-arm64-2.338.1.zip","browser_download_url":"https://example.invalid/arm64"},',
        '    {"name":"actions-runner-win-x64-2.338.1.zip","browser_download_url":"https://example.invalid/x64"}',
        '  ]',
        '}'
    )
    $Release = (Join-NativeOutputLines -Lines $SampleJsonLines) | ConvertFrom-Json
    $WindowsDownload = Get-RunnerPackage -Release $Release -Architecture "x64"
    if ($WindowsDownload.filename -ne "actions-runner-win-x64-2.338.1.zip" -or
        $WindowsDownload.download_url -ne "https://example.invalid/x64" -or
        $WindowsDownload.version -ne "2.338.1") {
        throw "Runner package self-test failed."
    }
    $ArmDownload = Get-RunnerPackage -Release $Release -Architecture "arm64"
    if ($ArmDownload.filename -ne "actions-runner-win-arm64-2.338.1.zip" -or
        $ArmDownload.download_url -ne "https://example.invalid/arm64") {
        throw "ARM64 runner package self-test failed."
    }
    Write-Host "PowerShell multi-line JSON and runner-package self-test passed."
    return
}

try {
    Write-Step "Checking this runner session"
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "Git is required. Install it and try again."
    }

    $RepositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    if ([string]::IsNullOrWhiteSpace($Repository)) {
        $Repository = Get-RepositoryFromOrigin -RepositoryRoot $RepositoryRoot
    }
    if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
        throw "Repository must use the owner/repo form."
    }
    if ([string]::IsNullOrWhiteSpace($Labels)) {
        throw "At least one custom label is required; keep the default for the supplied workflow."
    }

    if ([string]::IsNullOrWhiteSpace($Architecture)) {
        if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") {
            $Architecture = "arm64"
        }
        else {
            $Architecture = "x64"
        }
    }
    if ($Architecture -notin @("x64", "arm64")) {
        throw "Architecture must be x64 or arm64."
    }

    $RepositoryKey = $Repository -replace '[^A-Za-z0-9_.-]', '-'
    if ($RepositoryKey.Length -gt 80) {
        $RepositoryKey = $RepositoryKey.Substring(0, 80)
    }
    $RunnerRoot = [System.IO.Path]::GetFullPath($RunnerRoot)
    $InstallDirectory = Join-Path $RunnerRoot "$RepositoryKey-session"
    $RunnerSettingsPath = Join-Path $InstallDirectory ".runner"
    $RunnerCredentialsPath = Join-Path $InstallDirectory ".credentials"
    $RunnerName = "$env:COMPUTERNAME-session"

    # A second run.cmd with the same registration would create confusing sessions.
    $SessionMutex = [System.Threading.Mutex]::new(
        $false,
        "Local\GitHubActionsRunner-$RepositoryKey-session"
    )
    $MutexHeld = $SessionMutex.WaitOne(0)
    if (-not $MutexHeld) {
        throw "A runner session for $Repository is already open in this Windows session."
    }

    Write-Host "Repository: $Repository"
    Write-Host "Directory:  $InstallDirectory"
    Write-Host "Lifetime:   foreground work session; no Windows service"

    if (Test-Path $RunnerSettingsPath) {
        $RegistrationReused = $true
        if (-not (Test-Path $RunnerCredentialsPath)) {
            throw "The registration is incomplete. Remove $InstallDirectory and the matching runner in GitHub Settings, then retry."
        }
        Write-Step "Reusing the existing registration"
        Write-Host "Will keep original labels and architecture."
    }
    else {
        Write-Step "Preparing first-time registration"
        if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
            throw "GitHub CLI is required for first-time registration. Install gh and try again."
        }

        & gh auth status | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Run 'gh auth login' locally as a repository administrator. Never paste tokens into Arena."
        }

        if ((Test-Path $RunnerCredentialsPath) -and -not (Test-Path $RunnerSettingsPath)) {
            throw "Credentials exist without runner settings. Remove $InstallDirectory after inspection, then retry."
        }

        Confirm-GitHubActionsEnabled -Repository $Repository

        Write-Step "Resolving the latest official runner package"
        $Release = (Invoke-GitHubApi -Arguments @(
            "repos/actions/runner/releases/latest"
        )) | ConvertFrom-Json
        $RunnerDownload = Get-RunnerPackage -Release $Release -Architecture $Architecture

        $CacheDirectory = Join-Path $RunnerRoot "_downloads"
        New-Item -ItemType Directory -Path $CacheDirectory -Force | Out-Null
        $PackageName = [System.IO.Path]::GetFileName($RunnerDownload.filename)
        $PackagePath = Join-Path $CacheDirectory $PackageName

        if (-not (Test-Path $PackagePath)) {
            Write-Step "Downloading $PackageName"
            $PartialPackagePath = "$PackagePath.download"
            try {
                Invoke-WebRequest `
                    -Uri $RunnerDownload.download_url `
                    -OutFile $PartialPackagePath
                Move-Item -Path $PartialPackagePath -Destination $PackagePath
            }
            finally {
                if (Test-Path $PartialPackagePath) {
                    Remove-Item $PartialPackagePath -Force -ErrorAction SilentlyContinue
                }
            }
        }
        else {
            Write-Host "Using cached package: $PackagePath"
        }

        $ConfigPath = Join-Path $InstallDirectory "config.cmd"
        if (-not (Test-Path $ConfigPath)) {
            if ((Test-Path $InstallDirectory) -and
                (Get-ChildItem $InstallDirectory -Force | Select-Object -First 1)) {
                throw "$InstallDirectory is not empty and does not contain a runner. Inspect it or choose another RunnerRoot."
            }

            Write-Step "Extracting the persistent session runner"
            New-Item -ItemType Directory -Path $InstallDirectory -Force | Out-Null
            Expand-Archive -Path $PackagePath -DestinationPath $InstallDirectory -Force
        }

        Set-Location $InstallDirectory
        Write-Step "Registering $RunnerName"
        try {
            $RegistrationToken = (Invoke-GitHubApi -Arguments @(
                "--method", "POST",
                "repos/$Repository/actions/runners/registration-token",
                "--jq", ".token"
            )).Trim()
            if ([string]::IsNullOrWhiteSpace($RegistrationToken)) {
                throw "GitHub returned an empty registration token."
            }
        }
        catch {
            $RegistrationError = $_.Exception.Message
            $SetupGuidance = Get-ActionsSetupGuidance -Repository $Repository
            throw "Could not obtain a runner registration token. $SetupGuidance`nUnderlying error: $RegistrationError"
        }

        $ConfigureArguments = @(
            "--url", "https://github.com/$Repository",
            "--token", $RegistrationToken,
            "--unattended",
            "--name", $RunnerName,
            "--labels", $Labels,
            "--work", "_work"
        )
        Invoke-NativeCommand -FilePath $ConfigPath -Arguments $ConfigureArguments
        $RegistrationToken = $null
    }

    Set-Location $InstallDirectory
    Write-Step "Opening for business"
    Write-Host "Runner: $RunnerName"
    if ($RegistrationReused) {
        Write-Host "Labels: existing registration"
    }
    else {
        Write-Host "Labels: $Labels plus automatic labels"
    }
    Write-Host "In Arena: bash tools/github_actions_runner/request_run.sh local-windows"
    Write-Host "Keep this console open between jobs. Press Ctrl+C to close the session."

    $SessionOpened = $true
    & (Join-Path $InstallDirectory "run.cmd")
    $RunExitCode = $LASTEXITCODE
    if ($RunExitCode -ne 0) {
        throw "The runner exited with code $RunExitCode."
    }
}
finally {
    Set-Location $OriginalLocation
    if ($SessionOpened) {
        Write-Step "Business is now Closed"
        Write-Host "`nRunner session ended. Jobs can no longer execute."
        Write-Host "The registration is retained for the next session."
    }
    if ($MutexHeld -and $null -ne $SessionMutex) {
        $SessionMutex.ReleaseMutex()
    }
    if ($null -ne $SessionMutex) {
        $SessionMutex.Dispose()
    }
}
