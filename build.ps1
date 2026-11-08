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
        if ($attempt -ge 2) { throw 'PyInstaller 构建失败' }
        Start-Sleep -Seconds 2
    } while ($true)
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
