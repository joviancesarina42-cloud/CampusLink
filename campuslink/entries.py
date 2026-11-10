"""Resolve and pin subscription entries while retaining node selections."""
import ipaddress
import socket

from .nodes import METADATA_PREFIXES, V6_PROTOCOLS


def v6_entry(server, preferred=''):
    server = str(server or '').strip()
    if not server:
        return ''
    try:
        address = ipaddress.ip_address(server)
    except ValueError:
        address = None
    if address is not None:
        return str(address) if address.version == 6 and not address.ipv4_mapped and address.is_global else ''
    try:
        answers = socket.getaddrinfo(server, None, socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return ''
    addresses = []
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer[4][0])
        except ValueError:
            continue
        if address.version == 6 and not address.ipv4_mapped and address.is_global:
            addresses.append(str(address))
    return preferred if preferred in addresses else next(iter(addresses), '')


def node_base_name(name):
    return name.removesuffix(' [v6]')


def select_entry(source, settings):
    """Prefer the previous node, including a removed/added legacy [v6] suffix."""
    previous = settings.get('airport_source_name', '')
    candidates = [p for p in source.get('proxies', []) if isinstance(p, dict)
                  and p.get('type') in V6_PROTOCOLS and p.get('name')
                  and not p['name'].startswith(METADATA_PREFIXES)]

    def priority(proxy):
        if previous and proxy['name'] == previous:
            return 0
        if previous and node_base_name(proxy['name']) == node_base_name(previous):
            return 1
        if proxy.get('server') in (settings.get('airport_source_host'), settings.get('airport_ipv6')):
            return 2
        return 3

    resolved = {}
    for proxy in sorted(candidates, key=priority):
        host = proxy.get('server', '')
        if host not in resolved:
            resolved[host] = v6_entry(host, settings.get('airport_ipv6', ''))
        if resolved[host]:
            return proxy, resolved[host]
    return None, ''


def remap_selections(source, settings):
    """Only migrate unique legacy suffix changes; keep missing choices explicit."""
    names = [p['name'] for p in source.get('proxies', [])
             if isinstance(p, dict) and p.get('name') and p.get('type') in V6_PROTOCOLS]
    for key in ('default_node', 'ai_node'):
        selected = settings.get(key, '')
        if not selected or selected in names:
            continue
        matches = [name for name in names if node_base_name(name) == node_base_name(selected)]
        if len(matches) == 1:
            settings[key] = matches[0]


def pin_entry(proxy, address):
    """Keep the original TLS hostname when replacing a domain with an IP."""
    host = proxy['server']
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if proxy['type'] in ('trojan', 'hysteria', 'hysteria2', 'tuic'):
            if not proxy.get('sni') and not proxy.get('disable-sni'):
                proxy['sni'] = host
        elif proxy['type'] in ('vmess', 'vless') and proxy.get('tls'):
            if not proxy.get('servername'):
                proxy['servername'] = host
    proxy['server'] = address
