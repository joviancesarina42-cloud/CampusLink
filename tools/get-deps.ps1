param([string]$Destination = '')
# 下载 CampusLink 需要的两个可执行文件到 vendor\：
#   mihomo   内核
#   warpscout WARP 账户注册、端点扫描与 SOCKS
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = Split-Path $PSScriptRoot -Parent
if (-not $Destination) { $Destination = Join-Path $root 'vendor' }
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('campuslink-deps-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$headers = @{ 'User-Agent' = 'CampusLink'; 'Accept' = 'application/vnd.github+json' }

function Get-Release($repo) {
    try {
        Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers $headers -TimeoutSec 30
    } catch {
        throw "无法访问 GitHub API（$repo）：$($_.Exception.Message)"
    }
}

function Save-Asset($asset, $zip) {
    Write-Host ('下载 ' + $asset.name)
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -Headers $headers -TimeoutSec 300
}

function Expand-Once($zip, $folder) {
    Expand-Archive -Path $zip -DestinationPath $folder -Force
    Get-ChildItem -LiteralPath $folder -Recurse -File
}

try {
    Write-Host '查询 mihomo 最新版本…'
    $release = Get-Release 'MetaCubeX/mihomo'
    $asset = $release.assets | Where-Object { $_.name -eq ('mihomo-windows-amd64-' + $release.tag_name + '.zip') } | Select-Object -First 1
    if (-not $asset) { throw ('没有找到 mihomo-windows-amd64-' + $release.tag_name + '.zip') }
    $zip = Join-Path $tmp 'mihomo.zip'
    Save-Asset $asset $zip
    $exe = Expand-Once $zip (Join-Path $tmp 'mihomo') | Where-Object Extension -eq '.exe' | Select-Object -First 1
    if (-not $exe) { throw 'mihomo 压缩包里没有可执行文件' }
    Copy-Item -LiteralPath $exe.FullName -Destination (Join-Path $Destination 'mihomo.exe') -Force
    Write-Host ('mihomo ' + $release.tag_name + ' → vendor\mihomo.exe')

    Write-Host '查询 warpscout 最新版本…'
    $release = Get-Release 'vernette/warpscout'
    $asset = $release.assets | Where-Object { $_.name -eq ('warpscout_' + $release.tag_name.TrimStart('v') + '_windows_amd64.zip') } | Select-Object -First 1
    if (-not $asset) { throw ('没有找到 warpscout ' + $release.tag_name + ' 的 Windows 包') }
    $zip = Join-Path $tmp 'warpscout.zip'
    Save-Asset $asset $zip
    $exe = Expand-Once $zip (Join-Path $tmp 'warpscout') | Where-Object Extension -eq '.exe' | Select-Object -First 1
    if (-not $exe) { throw 'warpscout 压缩包里没有可执行文件' }
    Copy-Item -LiteralPath $exe.FullName -Destination (Join-Path $Destination 'warpscout.exe') -Force
    Write-Host ('warpscout ' + $release.tag_name + ' → vendor\warpscout.exe')

    Write-Host '完成。接着运行 python tools/selfcheck.py 检查还缺什么。'
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
