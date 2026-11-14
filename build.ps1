param([switch]$NoDesktop)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Push-Location $root
try {
    $running = @(Get-Process CampusLink, CampusLink.Desktop -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    })
    if ($running) { throw '本目录下的 CampusLink 正在运行：请先从托盘退出，再重新构建。' }
    if (-not $NoDesktop) {
        & (Join-Path $root 'tools\build_desktop.ps1') -Destination (Join-Path $root 'dist\desktop-build')
        if ($LASTEXITCODE -ne 0) { throw '原生窗口编译失败' }
    }
    # 杀软扫描刚生成的 exe 会短暂占用文件，失败时重试一次。
    $attempt = 0
    do {
        $attempt++
        python -m PyInstaller --noconfirm --clean --onedir --windowed --uac-admin --name CampusLink `
            --add-data 'web;web' --hidden-import yaml --collect-all yaml app.py
        if ($LASTEXITCODE -eq 0) { break }
        if ($attempt -ge 3) { throw 'PyInstaller 构建失败' }
        Start-Sleep -Seconds 5
    } while ($true)
    $desktop = Join-Path $root 'dist\desktop-build'
    if (Test-Path -LiteralPath $desktop) {
        $package = Join-Path $root 'dist\CampusLink\_desktop'
        New-Item -ItemType Directory -Force -Path $package | Out-Null
        Get-ChildItem -LiteralPath $desktop -File | Copy-Item -Destination $package -Force
    }
    # CMD 自身也可能被 SmartScreen 拦截；说明中提供从系统终端执行的修复入口。
    $extra = Join-Path $root 'packaging'
    $target = Join-Path $root 'dist\CampusLink'
    if (Test-Path -LiteralPath $extra) {
        Get-ChildItem -LiteralPath $extra -File | Copy-Item -Destination $target -Force
    }
    foreach ($name in @('启动 CampusLink.cmd', 'repair-start.ps1', '使用说明.txt')) {
        if (-not (Test-Path -LiteralPath (Join-Path $target $name) -PathType Leaf)) {
            throw ('发布包缺少启动文件：' + $name)
        }
    }
    $lines = foreach ($name in @('CampusLink.exe', '启动 CampusLink.cmd', 'repair-start.ps1', '使用说明.txt')) {
        $path = Join-Path $target $name
        if (Test-Path -LiteralPath $path) {
            '{0}  {1}' -f (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower(), $name
        }
    }
    if ($lines) { $lines | Set-Content -LiteralPath (Join-Path $target 'SHA256SUMS.txt') -Encoding utf8 }
    Write-Host '构建完成：dist\CampusLink\CampusLink.exe'
    Write-Host '运行前把 data\ 与 vendor\ 一并带上；这两项目录不进入版本库。'
} finally {
    Pop-Location
}
