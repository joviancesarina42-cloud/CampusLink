@echo off
setlocal DisableDelayedExpansion
set "CAMPUSLINK_HOME=%~dp0"
if not exist "%CAMPUSLINK_HOME%repair-start.ps1" (
  echo Missing repair-start.ps1. Extract the complete CampusLink package first.
  pause
  exit /b 1
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "& (Join-Path $env:CAMPUSLINK_HOME 'repair-start.ps1')"
if errorlevel 1 (
  echo.
  echo CampusLink did not start. Read the error above or see the instructions file.
  pause
  exit /b 1
)
exit /b 0
