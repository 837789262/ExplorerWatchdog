@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_explorer_watchdog_startup.ps1" -Action Uninstall
if errorlevel 1 (
  echo Uninstall failed.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | ?{ ($_.Name -ieq 'pythonw.exe' -or $_.Name -ieq 'python.exe') -and ($_.CommandLine -match 'ExplorerWatchdog\\explorer_watchdog\.py') } | % { try { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null } catch {} }"

echo Uninstall ok.
pause
