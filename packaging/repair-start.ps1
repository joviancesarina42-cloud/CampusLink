param(
    [string]$PackageDirectory = $PSScriptRoot,
    [switch]$RepairOnly
)
$ErrorActionPreference = 'Stop'
try {
    $root = (Get-Item -LiteralPath $PackageDirectory).FullName
    $exe = Join-Path $root 'CampusLink.exe'
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw '找不到 CampusLink.exe。请完整解压发布包；如果被隔离，请先检查 Windows 安全中心的保护历史记录。'
    }

    # 只处理程序与依赖，不修改 data 中的账户、订阅、数据库或回滚点。
    $files = @(Get-ChildItem -LiteralPath $root -File)
    foreach ($name in @('_internal', '_desktop', 'vendor')) {
        $directory = Join-Path $root $name
        if (Test-Path -LiteralPath $directory -PathType Container) {
            $files += @(Get-ChildItem -LiteralPath $directory -Recurse -File)
        }
    }
    foreach ($file in $files) {
        Unblock-File -LiteralPath $file.FullName -ErrorAction Stop
        $blocked = @(Get-Item -LiteralPath $file.FullName -Stream * -ErrorAction Stop |
            Where-Object { $_.Stream -eq 'Zone.Identifier' })
        if ($blocked.Count) { throw ('解除下载标记失败：' + $file.FullName) }
    }
    Write-Host '程序文件的下载标记已解除。'
    if (-not $RepairOnly) {
        # 显式等待 Shell 接受启动请求，UAC 取消或启动失败必须报告给使用者。
        Start-Process -FilePath $exe -WorkingDirectory $root -Verb RunAs -ErrorAction Stop | Out-Null
        Write-Host '已提交启动请求；请在管理员权限窗口中选择“是”。'
    }
} catch {
    $message = $_.Exception.Message
    if ($_.Exception.NativeErrorCode -eq 1223) {
        $message = '已取消管理员权限请求，CampusLink 未启动。重新启动后选择“是”即可。'
    }
    [Console]::Error.WriteLine('CampusLink：' + $message)
    exit 1
}
