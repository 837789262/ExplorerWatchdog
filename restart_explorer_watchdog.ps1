param(
    [int]$IntervalSec = 10
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$installScript = Join-Path $scriptRoot "install_explorer_watchdog_startup.ps1"

if (-not (Test-Path $installScript)) {
    throw "Installer not found: $installScript"
}

# Stop existing watchdog processes (python/pythonw) in current user context
$procs = Get-CimInstance Win32_Process |
    Where-Object {
        ($_.Name -ieq "pythonw.exe" -or $_.Name -ieq "python.exe") -and
        ($_.CommandLine -match "explorer_watchdog\.py" -or $_.CommandLine -match "ExplorerWatchdog")
    }

foreach ($p in $procs) {
    try {
        Invoke-CimMethod -InputObject $p -MethodName Terminate | Out-Null
    } catch {
    }
}

powershell -ExecutionPolicy Bypass -File $installScript -Action Install -IntervalSec $IntervalSec | Out-Host

$startupCmd = Join-Path ([Environment]::GetFolderPath("Startup")) "ExplorerWatchdog.cmd"
if (Test-Path $startupCmd) {
    & $startupCmd | Out-Null
}

Write-Host "Watchdog restarted."

