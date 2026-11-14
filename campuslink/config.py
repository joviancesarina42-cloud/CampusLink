from __future__ import annotations

import copy
import ipaddress
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .common import DATA, ROOT, VENDOR, atomic_write, read_json, write_json, run
from .nodes import AI_GROUP, ANCHOR, build_nodes
from .entries import pin_entry, remap_selections, select_entry, v6_entry

WARP_PROXY = 'CL-WARP'
NATIVE = 'CL-原生IPv6'
CAMPUS_DIRECT = 'CL-内网直连'
DEFAULT_GROUP = 'CL-IPv6出口'
# Cloudflare's four standard IPv6 MASQUE endpoints. They are only used as
# pre-authorised candidates; the runtime endpoint is still verified over HTTPS.
STANDARD_V6_HOSTS = ['2606:4700:103::1', '2606:4700:103::2',
                     '2606:4700:104::1', '2606:4700:104::2']
STANDARD_V6_ENDPOINTS = ['[2606:4700:104::2]:4500', '[2606:4700:104::2]:4443',
                         '[2606:4700:103::2]:8443', '[2606:4700:104::1]:8095',
                         '[2606:4700:103::1]:500']
DEFAULTS = {
    'mode': 'standalone', 'api': 'http://127.0.0.1:9097', 'socks_port': 11081,
    'mixed_port': 17897, 'core_api_port': 17979,
    # Empty means "not imported yet" or "auto-discover from the local install".
    'source_config': '', 'kernel_path': '', 'warp_path': '', 'warp_account': '',
    'warp_enable': True,
    'airport_ipv6': '', 'airport_source_name': '', 'airport_source_host': '',
    'endpoints': list(STANDARD_V6_ENDPOINTS),
    'campus_domains': [], 'campus_cidrs': [], 'native_domains': [],
    # 默认就是一套可用的“视频走 WARP、UDP 逼回 TCP”配置。
    'warp_domains': ['bilibili.com', 'bilibili.tv', 'bilivideo.com', 'bilivideo.cn',
                     'hdslb.com', 'biliapi.net', 'biliapi.com', 'szbdyd.com'],
    'warp_processes': ['哔哩哔哩.exe', 'bilibili.exe'],
    'warp_cidrs': ['111.33.0.0/16', '111.31.0.0/16', '182.48.108.0/24'],
    'warp_udp': 'block',
    'native_cn': True, 'exceptions': [],
    'dns': ['[2400:3200::1]:53', '[2402:4e00::]:53'],
    'capture_interfaces': [], 'check_seconds': 25, 'failure_threshold': 3,
    'tun': True, 'auto_capture': True,
    'default_node': '', 'ai_node': '',
    'ai_domains': ['openai.com', 'chatgpt.com', 'chat.com', 'oaistatic.com', 'oaiusercontent.com',
        'anthropic.com', 'claude.ai', 'claude.com', 'gemini.google.com', 'aistudio.google.com',
        'generativelanguage.googleapis.com', 'perplexity.ai', 'cursor.com', 'cursor.sh',
        'deepseek.com', 'x.ai', 'grok.com', 'poe.com'],
    'ai_processes': ['ChatGPT.exe', 'codex.exe', 'Claude.exe'], 'ai_extra_rules': [],
}


def validate_settings(s):
    if s['mode'] not in ('attach', 'standalone'):
        raise ValueError('运行方式无效')
    if s['airport_ipv6'] and not is_global_v6(s['airport_ipv6']):
        raise ValueError('机场入口必须为 IPv6 字面量，不能填域名或 IPv4')
    api = urlsplit(s['api'])
    if api.scheme != 'http' or api.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('内核 API 必须为本机回环地址')
    ports = [int(s[k]) for k in ('socks_port', 'mixed_port', 'core_api_port')]
    if any(not 1024 <= x <= 65535 for x in ports) or len(set(ports)) != 3:
        raise ValueError('三个端口必须在 1024–65535 之间且互不相同')
    if not 10 <= int(s['check_seconds']) <= 600 or not 2 <= int(s['failure_threshold']) <= 10:
        raise ValueError('健康检查间隔为 10–600 秒，连续失败阈值为 2–10 次')
    if s['warp_udp'] not in ('block', 'airport'):
        raise ValueError('WARP 域名 UDP 策略无效')
    if not s['endpoints']:
        raise ValueError('至少配置一个 IPv6 WARP 端点')
    for endpoint in s['endpoints']:
        parse_endpoint(endpoint)
    if not s['dns']:
        raise ValueError('至少配置一个 IPv6 DNS')
    for dns in s['dns']:
        # Literal upstreams keep DNS bootstrap independent from the tunnels.
        parse_endpoint(dns)
    for network in s.get('campus_cidrs', []):
        try:
            if ipaddress.ip_network(network.strip(), strict=False).version != 6:
                raise ValueError
        except ValueError:
            raise ValueError(f'内网 IPv6 网段格式无效：{network}')
    for network in s.get('warp_cidrs', []):
        try:
            if ipaddress.ip_network(network.strip(), strict=False).version != 4:
                raise ValueError
        except ValueError:
            raise ValueError(f'WARP 域名对应的 IPv4 网段无效：{network}')
    for e in s['exceptions']:
        if e.get('kind') not in ('domain', 'process') or e.get('route') not in ('airport', 'ai', 'warp', 'ipv4', 'native', 'block'):
            raise ValueError('例外规则类型或路线无效')
        if not e.get('value') or any(x in e['value'] for x in ',()\r\n'):
            raise ValueError('例外规则内容含非法字符')
    names = (s['native_domains'] + s['campus_domains'] + s['warp_domains']
             + s['warp_processes'] + s.get('ai_domains', []) + s.get('ai_processes', []))
    for name in names:
        if not name or any(x in name for x in ',()\r\n'):
            raise ValueError('域名或进程名称无效')
    for rule in s.get('ai_extra_rules', []):
        parts = rule.split(',')
        if len(parts) != 2 or parts[0] not in ('DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD') or not parts[1] or any(c in parts[1] for c in '()\r\n'):
            raise ValueError('导入的 AI 规则格式无效')


def parse_endpoint(value):
    if not isinstance(value, str) or not value.startswith('[') or ']:' not in value:
        raise ValueError(f'应使用 [IPv6]:端口 格式：{value}')
    host, port = value[1:].rsplit(']:', 1)
    addr = ipaddress.ip_address(host)
    if addr.version != 6 or not addr.is_global or not 1 <= int(port) <= 65535:
        raise ValueError('端点必须是公网 IPv6 和有效端口')
    return str(addr), int(port)


class Settings:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.value = copy.deepcopy(DEFAULTS)
        self.value.update(read_json(DATA / 'settings.json', {}))

    def save(self, values):
        value = copy.deepcopy(DEFAULTS)
        value.update(values)
        validate_settings(value)
        write_json(DATA / 'settings.json', value)
        self.value = value


def first_file(candidates):
    for item in candidates:
        if item and Path(item).is_file():
            return str(Path(item))
    return ''


def config_dirs():
    """Places a Clash Verge / mihomo profile may live, in priority order."""
    dirs = []
    env = os.environ.get('CAMPUSLINK_CONFIG_DIR')
    if env:
        dirs.append(Path(env))
    appdata = os.environ.get('APPDATA')
    if appdata:
        dirs += [Path(appdata) / 'io.github.clash-verge-rev.clash-verge-rev',
                 Path(appdata) / 'clash-verge',
                 Path(appdata) / 'clash-verge-rev']
    return dirs


def discover(settings=None):
    """Locate the profile, kernel and WARP helper without assuming one install layout."""
    s = settings or {}
    dirs = config_dirs()
    local = os.environ.get('LOCALAPPDATA', '')
    kernel = first_file([
        s.get('kernel_path'), VENDOR / 'mihomo.exe', ROOT / 'mihomo.exe',
        os.environ.get('CAMPUSLINK_MIHOMO'), shutil.which('mihomo'),
        *[d / n for d in dirs for n in ('verge-mihomo.exe', 'verge-mihomo-alpha.exe', 'mihomo.exe')],
        Path(local) / 'Programs/clash-verge/verge-mihomo.exe' if local else '',
        Path(os.environ.get('ProgramFiles', '')) / 'Clash Verge/verge-mihomo.exe'
        if os.environ.get('ProgramFiles') else '',
    ])
    warp = first_file([s.get('warp_path'), VENDOR / 'warpscout.exe', ROOT / 'warpscout.exe',
                       os.environ.get('CAMPUSLINK_WARPSCOUT'), shutil.which('warpscout')])
    runtime = first_file([os.environ.get('CAMPUSLINK_RUNTIME'),
                          *[d / 'clash-verge.yaml' for d in dirs]])
    source = first_file([s.get('source_config'), os.environ.get('CAMPUSLINK_SOURCE'),
                         runtime, DATA / 'source.yaml'])
    # warpscout writes its account next to the binary by default.
    warp_dir = Path(warp).parent if warp else None
    account = first_file([s.get('warp_account'), DATA / 'warp-account.json',
                          VENDOR / 'warp-account.json', ROOT / 'warp-account.json',
                          VENDOR / 'warpscout-account.json', ROOT / 'warpscout-account.json',
                          warp_dir / 'warpscout-account.json' if warp_dir else ''])
    verge_dir = next((str(d) for d in dirs if d.is_dir()), str(dirs[0]) if dirs else '')
    return {'verge_dir': verge_dir, 'runtime': runtime, 'source': source,
            'kernel': kernel, 'warp': warp, 'account': account,
            'imported': (DATA / 'source.yaml').exists(),
            'checked': {'config_dirs': [str(d) for d in dirs],
                        'kernel': bool(kernel), 'warp': bool(warp), 'account': bool(account)}}


def _copy_if_missing(src, dest):
    if not src:
        return False
    source = Path(src)
    if not source.is_file() or source.resolve() == dest.resolve():
        return False
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    return True


def register_warp(settings, fresh=False, target=None):
    """Create a WARP account with warpscout. IPv6 pools first, plain as fallback."""
    d = discover(settings.value)
    if not d['warp']:
        raise RuntimeError('没有找到 warpscout：把它放到 vendor/warpscout.exe 或填写路径')
    target = Path(target) if target is not None else DATA / 'warp-account.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    binary = Path(d['warp'])
    base = [str(binary), 'register', '-a', str(target)] + (['-fresh'] if fresh else [])
    attempts = [base + ['-6'], list(base)]
    detail = ''
    for argv in attempts:
        try:
            result = run(argv, timeout=180, cwd=str(binary.parent))
        except Exception as error:
            detail = str(error)
            continue
        if target.is_file() and target.stat().st_size > 0:
            return str(target)
        detail = (result.stderr or result.stdout).decode('utf-8', 'replace')[-400:]
    raise RuntimeError('WARP 账户注册失败：' + (detail.strip() or '没有拿到账户文件'))


def import_local(settings: Settings):
    """Prepare local dependencies: kernel, optional airport profile, WARP account."""
    d = discover(settings.value)
    if not d['kernel']:
        raise RuntimeError('没有找到 mihomo 内核：点击“下载内核与 warpscout”，或放到 vendor/mihomo.exe')
    VENDOR.mkdir(exist_ok=True)
    _copy_if_missing(d['kernel'], VENDOR / 'mihomo.exe')
    if d['warp']:
        _copy_if_missing(d['warp'], VENDOR / 'warpscout.exe')
    # Rule data travels with the profile; GEOSITE rules need it at runtime.
    for folder in (VENDOR, Path(d['kernel']).parent, Path(d['verge_dir'] or '.')):
        for pattern in ('*.dat', '*.mmdb', 'wintun.dll'):
            for found in folder.glob(pattern):
                _copy_if_missing(found, VENDOR / found.name)

    value = copy.deepcopy(settings.value)
    notes = []
    airport = ''
    used_source = ''
    source_path = Path(d['source']) if d['source'] else None
    imported = None
    if source_path and source_path.is_file() and source_path.resolve() != (DATA / 'source.yaml').resolve():
        imported = source_path.read_text('utf-8-sig')
    elif (DATA / 'source.yaml').is_file():
        imported = (DATA / 'source.yaml').read_text('utf-8-sig')
    if imported:
        try:
            c = yaml.safe_load(imported)
        except yaml.YAMLError:
            c = None
        selected, address = select_entry(c or {}, value)
        if selected is not None:
            value['airport_source_name'] = selected['name']
            value['airport_source_host'] = selected['server']  # 原始域名，供命中校验
            value['airport_ipv6'] = address                     # 解析出的 IPv6 字面量，运行时用它
            remap_selections(c, value)
            build_config(c, value)
            if address != selected['server']:
                notes.append('机场入口按域名解析为 IPv6：' + address)
            airport = selected['name']
            if c.get('external-controller'):
                value['api'] = 'http://' + c['external-controller']
            provider = c.get('rule-providers', {}).get('AI', {})
            rules = []
            if provider.get('path'):
                cache = Path(provider['path'])
                if not cache.is_absolute():
                    cache = source_path.parent / cache if source_path else cache
                if cache.is_file() and provider.get('behavior') == 'classical':
                    payload = cache.read_text('utf-8-sig')
                    try:
                        parsed = yaml.safe_load(payload)
                        rules = parsed.get('payload', []) if isinstance(parsed, dict) else payload.splitlines()
                    except yaml.YAMLError:
                        rules = payload.splitlines()
                    atomic_write(DATA / 'ai-rules-source.txt', payload)
            value['ai_extra_rules'] = [r.strip() for r in rules if isinstance(r, str)
                and len(r.strip().split(',')) == 2
                and r.strip().split(',')[0] in ('DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD')]
            atomic_write(DATA / 'source.yaml', imported)
            used_source = str(source_path) if source_path else str(DATA / 'source.yaml')
        else:
            if value.get('airport_source_name'):
                raise RuntimeError('没有发现可用的公网 IPv6 机场入口；请检查入口域名的 AAAA 记录与本机 DNS，已保留原配置')
            value.update(airport_ipv6='', airport_source_name='', airport_source_host='')
            notes.append('机场配置里没有公网 IPv6 入口，按“仅 WARP”模式运行')
    else:
        value.update(airport_ipv6='', airport_source_name='', airport_source_host='')
        notes.append('没有机场配置，按“仅 WARP”模式运行')

    if value.get('warp_enable', True):
        if d['account']:
            _copy_if_missing(d['account'], DATA / 'warp-account.json')
        if not (DATA / 'warp-account.json').is_file():
            if not d['warp']:
                value['warp_enable'] = False
                notes.append('没有 warpscout，WARP 未启用')
            else:
                try:
                    register_warp(settings)
                except Exception as error:
                    value['warp_enable'] = False
                    notes.append(str(error))
    settings.save(value)
    return {'airport': airport, 'warp': bool(value['warp_enable']),
            'kernel': str(VENDOR / 'mihomo.exe'),
            'source': used_source, 'notes': notes}


def is_global_v6(value):
    try:
        ip = ipaddress.ip_address(value)
        return ip.version == 6 and ip.is_global
    except ValueError:
        return False


def build_config(source, settings, standalone=False):
    """Build from a copy; never publish a subscription which lost its pinned node."""
    s = settings
    validate_settings(s)
    if not s['airport_ipv6'] and not s['warp_enable']:
        raise ValueError('机场和 WARP 都没有启用：至少需要其中之一')
    c = copy.deepcopy(source) if source else {}
    managed, groups = [], []
    if s['airport_ipv6']:
        matches = [p for p in c.get('proxies', []) if p.get('name') == s['airport_source_name']
                   and p.get('server') == s['airport_source_host']]
        if len(matches) != 1:
            raise ValueError('来源节点未唯一命中：订阅可能换入口。保留上一份配置，请重新导入核实')
        airport = copy.deepcopy(matches[0])
        if airport.get('type') not in ('hysteria2', 'tuic', 'ss', 'vmess', 'vless', 'trojan', 'hysteria', 'wireguard'):
            raise ValueError('来源节点协议未验收，不能保证其 IPv6 出站')
        pin_entry(airport, s['airport_ipv6'])
        airport['name'] = ANCHOR
        airport.pop('dialer-proxy', None)
        # Every selectable IPv4/domain node is chained through the pinned IPv6 anchor.
        managed, groups = build_nodes(source, s, airport)
    c['proxies'] = list(managed) + [{'name': NATIVE, 'type': 'direct', 'ip-version': 'ipv6'}]
    if s['campus_domains']:
        c['proxies'].append({'name': CAMPUS_DIRECT, 'type': 'direct', 'ip-version': 'ipv6-prefer'})
    if s['warp_enable']:
        c['proxies'].append({'name': WARP_PROXY, 'type': 'socks5', 'server': '127.0.0.1',
                             'port': int(s['socks_port']), 'udp': False})
    if not groups:
        # No airport: WARP is the only tunnel, native IPv6 stays the fallback.
        members = ([WARP_PROXY] if s['warp_enable'] else []) + [NATIVE]
        groups = [{'name': DEFAULT_GROUP, 'type': 'select', 'proxies': list(members)},
                  {'name': AI_GROUP, 'type': 'select', 'proxies': list(members)}]
    c['proxy-groups'] = groups
    c.pop('proxy-providers', None)
    c['ipv6'] = True
    c['mode'] = 'rule'
    c['find-process-mode'] = 'always'
    c['profile'] = {'store-selected': False, 'store-fake-ip': True}
    dns = c.setdefault('dns', {})
    for key in ('nameserver-policy', 'fallback', 'fallback-filter', 'proxy-server-nameserver', 'direct-nameserver'):
        dns.pop(key, None)
    dns.update(enable=True, ipv6=True, nameserver=s['dns'], **{'default-nameserver': s['dns']})
    # Keep fake-ip exclusions, but remove inherited v4 resolver overrides.
    dns.setdefault('enhanced-mode', 'fake-ip')
    if s['campus_domains']:
        # Gateways and SSO hosts must resolve to real internal addresses, including IPv4-only ones.
        dns['fake-ip-filter'] = list(dict.fromkeys(dns.get('fake-ip-filter', [])
            + ['+.' + domain for domain in s['campus_domains']]))
    # Without an airport there is no UDP-capable IPv6 exit, so UDP falls back to REJECT
    # instead of the default group (a socks5 WARP proxy is TCP-only here).
    udp_exit = DEFAULT_GROUP if s['airport_ipv6'] else 'REJECT'
    rules = []
    # Exact outer endpoint exceptions precede any UDP rejection, including port 443.
    outer = {parse_endpoint(e)[0] for e in s['endpoints']} | set(STANDARD_V6_HOSTS)
    if s['airport_ipv6']:
        outer.add(s['airport_ipv6'])
    for host in sorted(outer):
        rules.append(f'IP-CIDR6,{host}/128,DIRECT,no-resolve')
    for subnet in ['127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16']:
        rules.append(f'IP-CIDR,{subnet},DIRECT,no-resolve')
    for subnet in ['::1/128', 'fc00::/7', 'fe80::/10'] + list(s.get('campus_cidrs', [])):
        rules.append(f'IP-CIDR6,{subnet},DIRECT,no-resolve')
    routes = {'airport': DEFAULT_GROUP, 'ai': AI_GROUP,
              'warp': WARP_PROXY if s['warp_enable'] else DEFAULT_GROUP,
              'ipv4': 'DIRECT', 'native': NATIVE, 'block': 'REJECT'}
    for e in s['exceptions']:
        cond = ('DOMAIN-SUFFIX' if e['kind'] == 'domain' else 'PROCESS-NAME') + ',' + e['value']
        if e['route'] == 'warp' and s['warp_enable']:
            rules.append(f'AND,(({cond}),(NETWORK,UDP)),REJECT')
        if e['route'] == 'native':
            rules.append(f'AND,(({cond}),(NETWORK,UDP)),{DEFAULT_GROUP}')
        rules.append(f'{cond},{routes[e["route"]]}')
    # Keep gateways and their authentication subdomains on the internal network.
    # Explicit user exceptions above still take precedence over this built-in route.
    for domain in s['campus_domains']:
        rules.append(f'DOMAIN-SUFFIX,{domain},{CAMPUS_DIRECT}')
    # AI routing must precede native IPv6 and domestic rules, including for IPv6 targets.
    rules.append(f'DOMAIN,one.one.one.one,{AI_GROUP}')  # dedicated HTTPS exit probe
    ai_rules = ['DOMAIN-SUFFIX,' + d for d in s.get('ai_domains', [])]
    ai_rules += ['PROCESS-NAME,' + p for p in s.get('ai_processes', [])]
    ai_rules += s.get('ai_extra_rules', [])
    for condition in dict.fromkeys(ai_rules):
        rules.append(condition + ',' + AI_GROUP)
    # IPv6 literal connections can remain native; fake IPv4 addresses do not match.
    rules.append(f'IP-CIDR6,2000::/3,{NATIVE},no-resolve')
    if s['warp_enable']:
        warp_conds = [('DOMAIN-SUFFIX,' + d, False) for d in s['warp_domains']]
        warp_conds += [('PROCESS-NAME,' + p, False) for p in s['warp_processes']]
        warp_conds += [('IP-CIDR,' + c, True) for c in s.get('warp_cidrs', [])]
        for cond, no_resolve in dict.fromkeys(warp_conds):
            dest = udp_exit if s['warp_udp'] == 'airport' else 'REJECT'
            rules.append(f'AND,(({cond}),(NETWORK,UDP)),{dest}')
            rules.append(f'{cond},{WARP_PROXY}' + (',no-resolve' if no_resolve else ''))
    for domain in s['native_domains']:
        # v1.18.6 Direct.ListenPacket does not enforce ip-version on UDP.
        rules.append(f'AND,((DOMAIN-SUFFIX,{domain}),(NETWORK,UDP)),{udp_exit}')
        rules.append(f'DOMAIN-SUFFIX,{domain},{NATIVE}')
    if s['native_cn']:
        rules.append(f'AND,((GEOSITE,cn),(NETWORK,UDP)),{udp_exit}')
        rules.append(f'GEOSITE,cn,{NATIVE}')
    rules.append(f'MATCH,{DEFAULT_GROUP}')
    c['rules'] = rules
    c['sniffer'] = {'enable': True, 'sniff': {'HTTP': {'ports': [80, '8080-8880']},
                     'TLS': {'ports': [443, 8443]}, 'QUIC': {'ports': [443, 8443]}},
                     'force-dns-mapping': True, 'parse-pure-ip': True}
    if standalone:
        c['external-controller'] = f'127.0.0.1:{s["core_api_port"]}'
        c['secret'] = ''
        c['mixed-port'] = int(s['mixed_port'])
        for key in ('port', 'socks-port', 'redir-port', 'tproxy-port', 'external-controller-pipe',
                    'external-ui', 'external-ui-url', 'listeners', 'tunnels', 'interface-name'):
            c.pop(key, None)
        c['allow-lan'] = False
        c['tun'] = {'enable': bool(s['tun']), 'device': 'CampusLink', 'stack': 'gvisor',
                    'auto-route': True, 'auto-detect-interface': True, 'strict-route': True,
                    'dns-hijack': ['any:53'], 'mtu': 1500}
        c['dns']['listen'] = '127.0.0.1:15353'
    # Rule providers are unused, avoid startup downloads and independent path dependencies.
    c.pop('rule-providers', None)
    for key in ('geox-url', 'geo-auto-update'):
        c.pop(key, None)
    assert all(p.get('type') == 'direct' or p.get('server') == '127.0.0.1'
               or is_global_v6(p.get('server', '')) or p.get('dialer-proxy') == ANCHOR for p in c['proxies'])
    return c


def render(c):
    return yaml.safe_dump(c, allow_unicode=True, sort_keys=False)


def validate_kernel(text, kernel=None):
    kernel = kernel or discover()['kernel']
    if not kernel:
        raise RuntimeError('没有找到 mihomo 内核，无法校验配置')
    path = DATA / 'candidate.yaml'
    atomic_write(path, text)
    result = run([kernel, '-t', '-d', VENDOR, '-f', path], timeout=45)
    atomic_write(path.parent / 'validation.log', result.stdout + result.stderr)
    if result.returncode:
        # Do not return raw node credentials in validation errors.
        raise RuntimeError('mihomo 配置校验失败，请查看 data/validation.log')
    return True
