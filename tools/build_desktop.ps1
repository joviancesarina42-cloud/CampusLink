param([string]$Destination = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$version = '1.0.4314.35'
$package = Join-Path $root ('data\webview2.{0}.nupkg' -f $version)
$expanded = Join-Path $root ('data\webview2-sdk-{0}' -f $version)
if (-not $Destination) { $Destination = Join-Path $root '_desktop' }
New-Item -ItemType Directory -Force -Path (Join-Path $root 'data'),$destination | Out-Null
if (-not (Test-Path -LiteralPath $package)) {
    & curl.exe --fail --location --max-time 90 --output $package ('https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/{0}/microsoft.web.webview2.{0}.nupkg' -f $version)
    if ($LASTEXITCODE -ne 0) { throw 'WebView2 SDK download failed' }
}
if (-not (Test-Path -LiteralPath $expanded)) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($package,$expanded)
}
foreach ($name in 'Microsoft.Web.WebView2.Core.dll','Microsoft.Web.WebView2.WinForms.dll') {
    Copy-Item -LiteralPath (Join-Path $expanded ('lib\net462\' + $name)) -Destination $destination -Force
}
Copy-Item -LiteralPath (Join-Path $expanded 'runtimes\win-x64\native\WebView2Loader.dll') -Destination $destination -Force
Copy-Item -LiteralPath (Join-Path $expanded 'LICENSE.txt') -Destination (Join-Path $destination 'WebView2-LICENSE.txt') -Force
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$arguments = @('/nologo','/target:winexe','/platform:x64','/optimize+',
    ('/out:' + (Join-Path $destination 'CampusLink.Desktop.exe')),
    ('/win32manifest:' + (Join-Path $root 'desktop\app.manifest')),
    '/reference:System.dll','/reference:System.Core.dll','/reference:System.Drawing.dll',
    '/reference:System.Windows.Forms.dll','/reference:System.Net.Http.dll','/reference:System.Web.Extensions.dll',
    ('/reference:' + (Join-Path $destination 'Microsoft.Web.WebView2.Core.dll')),
    ('/reference:' + (Join-Path $destination 'Microsoft.Web.WebView2.WinForms.dll')),
    (Join-Path $root 'desktop\CampusLink.Desktop.cs'))
& $compiler @arguments
if ($LASTEXITCODE -ne 0) { throw 'Desktop host compilation failed' }
