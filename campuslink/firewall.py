"""Reversible, per-program IPv4 UDP guard outside the TUN routing layer."""
from __future__ import annotations

import base64
import json
import ntpath
import uuid

from .common import powershell

PREFIX = 'CampusLink-Bili-IPv4UDP-'
IPV4_ALL = '0.0.0.0-255.255.255.255'

# All properties changed on existing rules are captured, including their filters.
READ_RULE = r'''
function Read-Rule($r) {
    $a = $r | Get-NetFirewallAddressFilter -ErrorAction Stop
    $p = $r | Get-NetFirewallApplicationFilter -ErrorAction Stop
    $u = $r | Get-NetFirewallPortFilter -ErrorAction Stop
    $i = $r | Get-NetFirewallInterfaceFilter -ErrorAction Stop
    $t = $r | Get-NetFirewallInterfaceTypeFilter -ErrorAction Stop
    [pscustomobject]@{
        Name=$r.Name; Enabled=[string]$r.Enabled; Action=[string]$r.Action
        Profile=[string]$r.Profile; Direction=[string]$r.Direction
        Program=$p.Program; Protocol=[string]$u.Protocol
        LocalPort=@($u.LocalPort); RemotePort=@($u.RemotePort)
        LocalAddress=@($a.LocalAddress); RemoteAddress=@($a.RemoteAddress)
        InterfaceAlias=@($i.InterfaceAlias); InterfaceType=[string]$t.InterfaceType
    }
}
'''

DISCOVER = r'''
$rules = @(Get-NetFirewallRule -PolicyStore PersistentStore -ErrorAction Stop |
    Where-Object { $_.Name -like 'CampusLink-Bili-IPv4UDP-*' } |
    ForEach-Object { Read-Rule $_ })
$programs = @(
    Get-Process -ErrorAction Stop | Where-Object { $_.ProcessName -in @('bilibili', '哔哩哔哩') } |
        ForEach-Object { if ($_.Path) { $_.Path } }
    $roots = @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)})
    foreach ($root in $roots) {
        if ($root) {
            foreach ($sub in @('bilibili', 'Programs\bilibili')) {
                foreach ($exe in @('哔哩哔哩.exe', 'bilibili.exe')) {
                    $path = Join-Path (Join-Path $root $sub) $exe
                    if (Test-Path -LiteralPath $path -PathType Leaf) { $path }
                }
            }
        }
    }
    $keys = @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*')
    Get-ItemProperty -Path $keys -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match 'bilibili|哔哩哔哩' -and $_.InstallLocation } |
        ForEach-Object {
            foreach ($exe in @('哔哩哔哩.exe', 'bilibili.exe')) {
                $path = Join-Path $_.InstallLocation $exe
                if (Test-Path -LiteralPath $path -PathType Leaf) { $path }
            }
        }
)
[pscustomobject]@{rules=$rules; programs=@($programs | Sort-Object -Unique)} |
    ConvertTo-Json -Depth 8 -Compress
'''


def _command(script, payload=None):
    prelude = ("$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
               "[Console]::OutputEncoding=[Text.Encoding]::UTF8; ")
    if payload is not None:
        encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode('utf-8')).decode('ascii')
        prelude += f"$data = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')) | ConvertFrom-Json; "
    return powershell(prelude + READ_RULE + 'try {\n' + script +
                      '\n} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }', timeout=60)


def valid_program(path):
    return (isinstance(path, str) and ntpath.isabs(path) and bool(ntpath.splitdrive(path)[0])
            and path.lower().endswith('.exe') and not any(c in path for c in '*?\r\n\x00'))


def prepare(settings):
    """Read only. The caller must durably journal this plan before applying it."""
    info = _command(DISCOVER)
    rules = info['rules']
    paths = settings.get('bili_ipv4_udp_programs') or (
        [r['Program'] for r in rules if r['Protocol'] in ('UDP', '17')]
        + info['programs'])
    programs = {ntpath.normcase(ntpath.normpath(p)): ntpath.normpath(p)
                for p in paths if valid_program(p)}
    if not programs:
        raise RuntimeError('未找到 B 站程序，请在策略与配置中填写完整 exe 路径，或关闭 B 站 IPv4 UDP 防泄露')
    entries = []
    covered = set()
    for rule in rules:
        key = ntpath.normcase(ntpath.normpath(rule['Program']))
        if key in programs and rule['Protocol'] in ('UDP', '17') and rule['Direction'] in ('Inbound', 'Outbound'):
            entries.append({'name': rule['Name'], 'program': programs[key],
                            'direction': rule['Direction'], 'before': rule})
            covered.add((key, rule['Direction']))
    for key, path in programs.items():
        for direction in ('Outbound', 'Inbound'):
            if (key, direction) not in covered:
                entries.append({'name': PREFIX + uuid.uuid4().hex + '-' + direction,
                                'program': path, 'direction': direction, 'before': None})
    return {'entries': entries}


def apply(plan):
    _command(r'''
$profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
if ($profiles.Count -ne 3 -or @($profiles | Where-Object { -not $_.Enabled -or $_.AllowLocalFirewallRules -eq 'False' }).Count) {
    throw 'Windows 防火墙未启用或禁止本地规则，无法启用 B 站防泄露'
}
foreach ($entry in $data.entries) {
    $ruleArgs = @{
        PolicyStore='PersistentStore'; Name=$entry.name; Enabled='True'; Action='Block'
        Profile='Any'; InterfaceAlias='Any'; InterfaceType='Any'
        LocalAddress='Any'; RemoteAddress='0.0.0.0-255.255.255.255'
        LocalPort='Any'; RemotePort='Any'; Protocol='UDP'; Program=$entry.program
        Direction=$entry.direction; ErrorAction='Stop'
    }
    if ($null -ne $entry.before) {
        Set-NetFirewallRule @ruleArgs | Out-Null
    } else {
        New-NetFirewallRule @ruleArgs -DisplayName $entry.name -Group 'CampusLink Bili IPv4 UDP guard' | Out-Null
    }
    $r = Read-Rule (Get-NetFirewallRule -PolicyStore ActiveStore -Name $entry.name -ErrorAction Stop)
    if ($r.Enabled -ne 'True' -or $r.Action -ne 'Block' -or $r.Program -ne $entry.program -or
        $r.Direction -ne $entry.direction -or $r.Protocol -notin @('UDP','17') -or $r.Profile -ne 'Any' -or
        ($r.InterfaceAlias -join ',') -ne 'Any' -or $r.InterfaceType -ne 'Any' -or
        ($r.RemoteAddress -join ',') -ne '0.0.0.0-255.255.255.255' -or
        ($r.LocalAddress -join ',') -ne 'Any' -or ($r.LocalPort -join ',') -ne 'Any' -or
        ($r.RemotePort -join ',') -ne 'Any') { throw ('B 站防火墙规则生效校验失败：' + $entry.name) }
}
@{ok=$true} | ConvertTo-Json -Compress
''', plan)


def restore(plan):
    """Idempotent across partial application, app crashes and repeated recovery."""
    _command(r'''
$errors = @()
foreach ($entry in $data.entries) {
    try {
        if ($null -eq $entry.before) {
            $rule = @(Get-NetFirewallRule -PolicyStore PersistentStore -ErrorAction Stop |
                Where-Object { $_.Name -eq $entry.name })
            $rule | Remove-NetFirewallRule -ErrorAction Stop
            if (Get-NetFirewallRule -PolicyStore PersistentStore -ErrorAction Stop |
                Where-Object { $_.Name -eq $entry.name }) { throw '临时规则删除校验失败' }
        } else {
            $ruleArgs = @{PolicyStore='PersistentStore'; Name=$entry.name; ErrorAction='Stop'}
            foreach ($key in @('Enabled','Action','Profile','InterfaceAlias','InterfaceType',
                'LocalAddress','RemoteAddress','LocalPort','RemotePort','Protocol','Program','Direction')) {
                $ruleArgs[$key] = $entry.before.$key
            }
            Set-NetFirewallRule @ruleArgs | Out-Null
            $actual = Read-Rule (Get-NetFirewallRule -PolicyStore PersistentStore -Name $entry.name -ErrorAction Stop)
            foreach ($key in $ruleArgs.Keys) {
                if ($key -in @('PolicyStore','ErrorAction')) { continue }
                if ((@($actual.$key | Sort-Object) -join '|') -ne (@($entry.before.$key | Sort-Object) -join '|')) {
                    throw ('原规则恢复校验失败：' + $key)
                }
            }
        }
    } catch { $errors += ($entry.name + ': ' + $_.Exception.Message) }
}
if ($errors.Count) { throw ($errors -join '; ') }
@{ok=$true} | ConvertTo-Json -Compress
''', plan)
