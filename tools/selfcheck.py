"""Read-only environment check: tells you what is missing before you start.

    python tools/selfcheck.py
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campuslink import __version__
from campuslink.common import CREATE_NO_WINDOW, DATA, ROOT, VENDOR, is_admin, run
from campuslink.config import Settings, discover


def pcap_present():
    if os.name != 'nt':
        return False
    folder = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'System32/Npcap'
    return (folder / 'wpcap.dll').is_file()


def kernel_version(path):
    try:
        result = run([path, '-v'], timeout=15)
        text = (result.stdout + result.stderr).decode('utf-8', 'replace').strip()
        return text.splitlines()[0] if text else '已找到，未返回版本'
    except Exception as error:
        return f'无法执行：{error}'


def main():
    parser = argparse.ArgumentParser(description='CampusLink 环境自检')
    parser.add_argument('--json', action='store_true', help='以 JSON 输出')
    args = parser.parse_args()
    settings = Settings()
    found = discover(settings.value)
    items = []

    def add(name, ok, detail):
        items.append({'item': name, 'ok': bool(ok), 'detail': detail})

    add('系统', os.name == 'nt', 'Windows' if os.name == 'nt' else '仅支持 Windows')
    add('Python', sys.version_info >= (3, 10), sys.version.split()[0])
    add('64 位解释器', ctypes.sizeof(ctypes.c_void_p) == 8, '64 位' if ctypes.sizeof(ctypes.c_void_p) == 8 else '32 位')
    add('管理员权限', is_admin(), '已提升' if is_admin() else '普通权限：独立 TUN 模式需要管理员')
    add('物理监控驱动', pcap_present(), '已安装 Npcap' if pcap_present() else '未安装 Npcap：只影响物理流量监控')
    host = ROOT / '_desktop' / 'CampusLink.Desktop.exe'
    add('原生窗口', host.is_file(), str(host) if host.is_file() else '未编译：界面会退回默认浏览器')
    add('mihomo 内核', bool(found['kernel']),
        kernel_version(found['kernel']) if found['kernel'] else '未找到：放到 vendor/mihomo.exe 或填写内核路径')
    add('warpscout', bool(found['warp']),
        found['warp'] or '未找到：无法启用 WARP，只能走原生 IPv6')
    add('mihomo 配置', bool(found['source']),
        found['source'] or '未找到：放入 data/source.yaml 或填写配置来源')
    add('WARP 账户', bool(found['account']),
        found['account'] or '还没有账户：初始化时会自动注册')
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        probe = DATA / '.selfcheck'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink()
        writable = True
    except Exception:
        writable = False
    add('data 目录可写', writable, str(DATA))
    add('固定 IPv6 首跳', bool(settings.value['airport_ipv6']),
        settings.value['airport_ipv6'] or '尚未导入：启动后点击“重新导入本机配置”')

    if args.json:
        print(json.dumps({'version': __version__, 'root': str(ROOT), 'items': items}, ensure_ascii=False, indent=2))
    else:
        print(f'CampusLink {__version__}  自检  {ROOT}')
        for item in items:
            print(f"  {'[ok]' if item['ok'] else '[--]'} {item['item']:<14} {item['detail']}")
    return 0 if all(item['ok'] for item in items if item['item'] in ('系统', 'mihomo 内核')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
