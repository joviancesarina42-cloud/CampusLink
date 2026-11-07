param([switch]$NoDesktop)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Push-Location $root
try {
    $running = Get-Process CampusLink, CampusLink.Desktop -ErrorAction SilentlyContinue
    if ($running) { throw 'CampusLink 正在运行：请先从托盘退出，再重新构建。' }
    if (-not $NoDesktop) {
        & (Join-Path $root 'tools\build_desktop.ps1') -Destination (Join-Path $root 'dist\desktop-build')
        if ($LASTEXITCODE -ne 0) { throw '原生窗口编译失败' }
    }
    python -m PyInstaller --noconfirm --clean --onedir --windowed --uac-admin --name CampusLink `
        --add-data 'web;web' --hidden-import yaml --collect-all yaml app.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败' }
    $desktop = Join-Path $root 'dist\desktop-build'
    if (Test-Path -LiteralPath $desktop) {
        $package = Join-Path $root 'dist\CampusLink\_desktop'
        New-Item -ItemType Directory -Force -Path $package | Out-Null
        Get-ChildItem -LiteralPath $desktop -File | Copy-Item -Destination $package -Force
    }
    Write-Host '构建完成：dist\CampusLink\CampusLink.exe'
    Write-Host '运行前把 data\ 与 vendor\ 一并带上；这两项目录不进入版本库。'
} finally {
    Pop-Location
}
