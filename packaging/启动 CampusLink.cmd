@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
if not exist "CampusLink.exe" (
  echo.
  echo 找不到 CampusLink.exe。请把本文件放在解压后的目录里再运行。
  echo.
  pause
  exit /b 1
)
echo 正在解除本目录下所有文件的“来自 Internet”标记…
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0' -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue"
echo 启动 CampusLink…
start "" "CampusLink.exe"
exit /b 0
