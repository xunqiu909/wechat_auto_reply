@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
  echo.
  echo Install failed. Please check the message above.
  pause
  exit /b 1
)
echo.
echo Install finished.
pause
