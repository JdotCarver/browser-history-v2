# tools/perf/watch_launches.ps1
<#
.SYNOPSIS
    Record every launch of the plugin's run.py while you use Flow Launcher.

.DESCRIPTION
    The plugin's own trace can only start once Python is running. This watches
    from outside, so it also sees launches that die before writing anything and
    lets you count processes per keypress independently of the plugin's view.

    Run it in a PowerShell window, use Flow Launcher normally for the duration,
    then send the CSV it writes. Nothing but process metadata is recorded: no
    window titles, no query text.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\perf\watch_launches.ps1 -Seconds 90
#>
param(
    [int]$Seconds = 90,
    [int]$PollMilliseconds = 100,
    [string]$Out = "$env:TEMP\bh-launches.csv"
)

$ErrorActionPreference = 'Stop'
$seen = @{}
$rows = New-Object System.Collections.Generic.List[object]

Write-Host "Watching for plugin launches for $Seconds seconds. Use Flow Launcher now." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds($Seconds)

while ((Get-Date) -lt $deadline) {
    $processes = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
    foreach ($process in $processes) {
        if ($seen.ContainsKey($process.ProcessId)) { continue }
        if (-not $process.CommandLine -or $process.CommandLine -notmatch 'run\.py') { continue }

        $seen[$process.ProcessId] = $true

        # The method is in the JSON payload Flow passes as the only argument.
        # It arrives quoted and escaped differently depending on how the process
        # was started, so match the value, not the punctuation around it.
        $method = 'unknown'
        if ($process.CommandLine -match 'method.{1,6}(query|context_menu)') { $method = $Matches[1] }

        $parentName = ''
        try {
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)" -ErrorAction SilentlyContinue
            if ($parent) { $parentName = $parent.Name }
        } catch { }

        $rows.Add([pscustomobject]@{
            LaunchedAt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss.fff')
            Pid        = $process.ProcessId
            ParentPid  = $process.ParentProcessId
            ParentName = $parentName
            Method     = $method
        })
        Write-Host ("  {0}  pid {1}  {2}  parent {3}" -f $rows[-1].LaunchedAt, $process.ProcessId, $method, $parentName)
    }
    Start-Sleep -Milliseconds $PollMilliseconds
}

$rows | Export-Csv -Path $Out -NoTypeInformation -Encoding UTF8
Write-Host ""
Write-Host "$($rows.Count) launches recorded to $Out" -ForegroundColor Green

if ($rows.Count -gt 1) {
    $gaps = for ($i = 1; $i -lt $rows.Count; $i++) {
        ([datetime]$rows[$i].LaunchedAt - [datetime]$rows[$i - 1].LaunchedAt).TotalMilliseconds
    }
    $close = @($gaps | Where-Object { $_ -lt 400 })
    Write-Host ("gaps under 400 ms: {0} of {1}  (a launch pair that close is one press, two processes)" -f $close.Count, $gaps.Count)
}
