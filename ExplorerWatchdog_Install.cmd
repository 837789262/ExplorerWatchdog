@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_explorer_watchdog_startup.ps1" -Action Install -IntervalSec 10
if errorlevel 1 (
  echo Install failed.
  pause
  exit /b 1
)

call "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ExplorerWatchdog.cmd"

echo Install ok.
pause
