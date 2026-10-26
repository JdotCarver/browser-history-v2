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

    $Output = & gh api @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "GitHub API access failed. Check that your LOCAL gh login can administer $Repository."
    }

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

if ($SelfTest) {
    $SampleJsonLines = @(
        '[',
        '  {"os":"win","architecture":"x64","filename":"runner.zip"},',
        '  {"os":"linux","architecture":"x64","filename":"runner.tar.gz"}',
        ']'
    )
    $Downloads = (Join-NativeOutputLines -Lines $SampleJsonLines) | ConvertFrom-Json
    $WindowsDownload = $Downloads |
        Where-Object { $_.os -eq "win" -and $_.architecture -eq "x64" } |
        Select-Object -First 1
    if ($null -eq $WindowsDownload -or $WindowsDownload.filename -ne "runner.zip") {
        throw "Multi-line JSON self-test failed."
    }
    Write-Host "PowerShell multi-line JSON self-test passed."
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

        Write-Step "Resolving the runner package approved for this repository"
        $DownloadsJson = Invoke-GitHubApi -Arguments @("repos/$Repository/actions/runners/downloads")
        $Downloads = $DownloadsJson | ConvertFrom-Json
        # The runner-download API uses "win" for Windows packages.
        $RunnerDownload = $Downloads |
            Where-Object { $_.os -eq "win" -and $_.architecture -eq $Architecture } |
            Select-Object -First 1
        if ($null -eq $RunnerDownload) {
            $AvailableTargets = $Downloads |
                ForEach-Object { "$($_.os)/$($_.architecture)" }
            throw "No Windows/$Architecture package was returned. Available targets: $AvailableTargets"
        }

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
        $RegistrationToken = (Invoke-GitHubApi -Arguments @(
            "--method", "POST",
            "repos/$Repository/actions/runners/registration-token",
            "--jq", ".token"
        )).Trim()
        if ([string]::IsNullOrWhiteSpace($RegistrationToken)) {
            throw "GitHub returned an empty registration token."
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
