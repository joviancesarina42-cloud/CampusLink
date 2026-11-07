"""Node catalog and selectors, with a fixed IPv6 first hop for non-v6 nodes."""
import copy
import ipaddress

DEFAULT_GROUP = 'CL-IPv6出口'
AI_GROUP = 'CL-AI服务'
ANCHOR = 'CL-机场IPv6'
CHAIN_PROTOCOLS = {'ss', 'vmess', 'vless', 'trojan'}
V6_PROTOCOLS = CHAIN_PROTOCOLS | {'hysteria2', 'hysteria', 'tuic', 'wireguard'}
METADATA_PREFIXES = ('剩余流量', '套餐到期', '账号邮箱', '官网地址', '流量重置', '到期时间')


def global_v6(host):
    try:
        ip = ipaddress.ip_address(host)
        return ip.version == 6 and ip.is_global
    except ValueError:
        return False


def catalog(source, settings):
    nodes = []
    seen = set()
    anchor_id = settings['airport_source_name']
    for proxy in source.get('proxies', []):
        name = proxy.get('name', '')
        protocol = proxy.get('type', '')
        if not name or name.startswith(METADATA_PREFIXES) or protocol not in V6_PROTOCOLS:
            continue
        if name in seen:
            raise ValueError('订阅中节点名称重复，无法安全选择：' + name)
        seen.add(name)
        direct = name == anchor_id or global_v6(proxy.get('server', ''))
        if not direct and protocol not in CHAIN_PROTOCOLS:
            continue
        nodes.append({'id': name, 'name': name, 'protocol': protocol,
            'proxy': ANCHOR if name == anchor_id else 'CL-节点/' + name,
            'path': 'direct6' if direct else 'via6',
            'gateway': '' if direct else anchor_id,
            'detail': 'IPv6 直连入口' if direct else '经 IPv6 首跳中转'})
    return nodes


def choices(settings):
    anchor_id = settings['airport_source_name']
    return {'default': settings.get('default_node') or anchor_id,
            'ai': settings.get('ai_node') or anchor_id}


def build_nodes(source, settings, anchor):
    nodes = catalog(source, settings)
    by_id = {n['id']: n for n in nodes}
    selection = choices(settings)
    if settings['airport_source_name'] not in by_id:
        raise ValueError('IPv6 首跳节点已不存在，请重新导入核实')
    for role, identifier in selection.items():
        if identifier not in by_id:
            raise ValueError(f'{role} 所选节点已不存在或不支持 IPv6 首跳中转：{identifier}')
    sources = {p['name']: p for p in source['proxies']}
    proxies = [anchor]
    for node in nodes:
        if node['proxy'] == ANCHOR:
            continue
        proxy = copy.deepcopy(sources[node['id']])
        proxy['name'] = node['proxy']
        proxy.pop('dialer-proxy', None)
        if node['path'] == 'via6':
            proxy['dialer-proxy'] = ANCHOR
            proxy.pop('interface-name', None)
        proxies.append(proxy)
    all_names = [n['proxy'] for n in nodes]
    groups = []
    for role, group in [('default', DEFAULT_GROUP), ('ai', AI_GROUP)]:
        selected = by_id[selection[role]]['proxy']
        groups.append({'name': group, 'type': 'select',
                       'proxies': [selected] + [p for p in all_names if p != selected]})
    return proxies, groups
