param(
    [ValidateSet("Install", "Uninstall")]
    [Alias("Action")]
    [string]$Mode = "Install",

    [int]$IntervalSec = 10,

    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

$destDir = Join-Path $env:LOCALAPPDATA "ExplorerWatchdog"
$destScript = Join-Path $destDir "explorer_watchdog.py"
$logFile = Join-Path $destDir "watchdog.log"
$launcherCmd = Join-Path $destDir "run_watchdog.cmd"
$startupDir = [Environment]::GetFolderPath("Startup")
$startupLink = Join-Path $startupDir "ExplorerWatchdog.cmd"

function Stop-ExistingWatchdog {
    # 结束当前用户会话里已存在的 watchdog，避免脚本文件被占用导致复制失败
    try {
        $procs = Get-CimInstance Win32_Process |
            Where-Object {
                ($_.Name -ieq "pythonw.exe" -or $_.Name -ieq "python.exe") -and
                ($_.CommandLine -match "ExplorerWatchdog\\explorer_watchdog\.py" -or $_.CommandLine -match "explorer_watchdog\.py")
            }

        foreach ($p in $procs) {
            try {
                Invoke-CimMethod -InputObject $p -MethodName Terminate | Out-Null
            } catch {
            }
        }
    } catch {
    }
}

if ($Mode -eq "Uninstall") {
    if (Test-Path $startupLink) {
        Remove-Item -Path $startupLink -Force
    }
    Stop-ExistingWatchdog
    Write-Host "Startup auto-launch removed."
    exit 0
}

New-Item -ItemType Directory -Path $destDir -Force | Out-Null

$sourceScript = Join-Path $PSScriptRoot "explorer_watchdog.py"
if (-not (Test-Path $sourceScript)) {
    throw "Source script not found: $sourceScript"
}
Stop-ExistingWatchdog

$copied = $false
for ($i = 0; $i -lt 5; $i++) {
    try {
        Copy-Item -Path $sourceScript -Destination $destScript -Force
        $copied = $true
        break
    } catch {
        Start-Sleep -Milliseconds 300
    }
}
if (-not $copied) {
    throw "Copy script failed: $destScript"
}

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $defaultPythonW = "D:\ProgramData\miniconda3\envs\chenwei_310\pythonw.exe"
    $defaultPython = "D:\ProgramData\miniconda3\envs\chenwei_310\python.exe"
    if (Test-Path $defaultPythonW) {
        $PythonExe = $defaultPythonW
    } elseif (Test-Path $defaultPython) {
        $PythonExe = $defaultPython
    } else {
        throw "Default Python not found: $defaultPython"
    }
}

if (-not (Test-Path $PythonExe)) {
    throw "PythonExe not found: $PythonExe"
}

$launcherContent = @(
    "@echo off",
    "cd /d `"$destDir`"",
    "start `"`" `"$PythonExe`" `"$destScript`" --tray --interval $IntervalSec --log-file `"$logFile`""
) -join "`r`n"

[System.IO.File]::WriteAllText($launcherCmd, $launcherContent, [System.Text.Encoding]::ASCII)
[System.IO.File]::WriteAllText($startupLink, $launcherContent, [System.Text.Encoding]::ASCII)

Write-Host "Startup auto-launch installed."
Write-Host "Launcher path: $launcherCmd"
Write-Host "Startup entry: $startupLink"
Write-Host "Log path: $logFile"
