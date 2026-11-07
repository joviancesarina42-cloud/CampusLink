"""Read-only physical capture using the existing Npcap driver, no injected packets."""
from __future__ import annotations

import ctypes as C
import ipaddress
import os
import socket
import struct
import threading
import time
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from .common import powershell


@lru_cache(maxsize=4096)
def remote_address(raw):
    address = ipaddress.ip_address(raw)
    return str(address), 'public' if address.is_global else 'lan'


def decode_frame(packet: bytes, mac: bytes):
    if len(packet) < 14:
        return None
    direction = 'up' if packet[6:12] == mac else 'down' if packet[:6] == mac else None
    if direction is None:
        return None  # exclude unrelated multicast / broadcast frames
    protocol = struct.unpack_from('!H', packet, 12)[0]
    offset = 14
    while protocol in (0x8100, 0x88a8):
        if len(packet) < offset + 4:
            return None
        protocol = struct.unpack_from('!H', packet, offset + 2)[0]
        offset += 4
    if protocol == 0x0800 and len(packet) >= offset + 20:
        family = 4
        length = struct.unpack_from('!H', packet, offset + 2)[0]
        remote_raw = packet[offset + 16:offset + 20] if direction == 'up' else packet[offset + 12:offset + 16]
        transport = packet[offset + 9]
        l4 = offset + (packet[offset] & 15) * 4
        # Do not interpret non-initial IPv4 fragments as port headers.
        fragment = struct.unpack_from('!H', packet, offset + 6)[0] & 0x1fff
    elif protocol == 0x86dd and len(packet) >= offset + 40:
        family = 6
        length = struct.unpack_from('!H', packet, offset + 4)[0] + 40
        remote_raw = packet[offset + 24:offset + 40] if direction == 'up' else packet[offset + 8:offset + 24]
        transport = packet[offset + 6]
        l4 = offset + 40
        fragment = 0
        for _ in range(8):
            if transport not in (0, 43, 44, 51, 60) or len(packet) < l4 + 8:
                break
            next_header = packet[l4]
            if transport == 44:
                fragment = struct.unpack_from('!H', packet, l4 + 2)[0] >> 3
                size = 8
            elif transport == 51:
                size = (packet[l4 + 1] + 2) * 4
            else:
                size = (packet[l4 + 1] + 1) * 8
            l4 += size
            transport = next_header
    else:
        return None
    remote, scope = remote_address(remote_raw)
    port = 0
    if not fragment and transport in (6, 17) and len(packet) >= l4 + 4:
        port = struct.unpack_from('!H', packet, l4 + (2 if direction == 'up' else 0))[0]
    return {'family': family, 'direction': direction, 'remote': remote, 'port': port,
            'protocol': {6: 'TCP', 17: 'UDP', 1: 'ICMP', 58: 'ICMPv6'}.get(transport, str(transport)),
            'bytes': length, 'scope': scope}


class TimeVal(C.Structure):
    _fields_ = [('tv_sec', C.c_long), ('tv_usec', C.c_long)]


class PcapHeader(C.Structure):
    _fields_ = [('ts', TimeVal), ('caplen', C.c_uint32), ('len', C.c_uint32)]


class PcapStat(C.Structure):
    _fields_ = [('received', C.c_uint), ('dropped', C.c_uint), ('interface_dropped', C.c_uint), ('captured', C.c_uint)]


def load_pcap():
    if os.name != 'nt':
        raise RuntimeError('物理流量监控需要 Windows 和 Npcap')
    directory = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'System32/Npcap'
    if not (directory / 'wpcap.dll').exists():
        raise RuntimeError('没有找到 Npcap，请安装 Npcap 后启用物理流量监控')
    cookie = os.add_dll_directory(str(directory))
    p = C.CDLL(str(directory / 'wpcap.dll'))
    p._dll_cookie = cookie
    p.pcap_open_live.argtypes = [C.c_char_p, C.c_int, C.c_int, C.c_int, C.c_char_p]
    p.pcap_open_live.restype = C.c_void_p
    p.pcap_next_ex.argtypes = [C.c_void_p, C.POINTER(C.POINTER(PcapHeader)), C.POINTER(C.POINTER(C.c_ubyte))]
    p.pcap_next_ex.restype = C.c_int
    p.pcap_datalink.argtypes = [C.c_void_p]
    p.pcap_datalink.restype = C.c_int
    p.pcap_stats.argtypes = [C.c_void_p, C.POINTER(PcapStat)]
    p.pcap_stats.restype = C.c_int
    p.pcap_close.argtypes = [C.c_void_p]
    p.pcap_geterr.argtypes = [C.c_void_p]
    p.pcap_geterr.restype = C.c_char_p
    p.pcap_setnonblock.argtypes = [C.c_void_p, C.c_int, C.c_char_p]
    p.pcap_setnonblock.restype = C.c_int
    if hasattr(p, 'pcap_setbuff'):
        p.pcap_setbuff.argtypes = [C.c_void_p, C.c_int]
    return p


def physical_adapters():
    # An empty pipeline emits no text in Windows PowerShell 5.1. Passing the
    # array explicitly guarantees [] while adapters are disconnected.
    value = powershell("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        "ConvertTo-Json -Compress -InputObject @(Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | "
        "Select-Object Name,InterfaceGuid,MacAddress,InterfaceDescription,ifIndex)")
    return value if isinstance(value, list) else [value] if value else []


class Capture:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.threads = []
        self.pending = defaultdict(lambda: [0, 0])
        self.status = {'running': False, 'error': '', 'interfaces': [], 'dropped': 0,
                       'started': None, 'last_packet': None}
        self.generation = 0

    def start(self, selected=None):
        self.stop()
        adapters = physical_adapters()
        if selected:
            adapters = [a for a in adapters if a['Name'] in selected]
        if not adapters:
            raise RuntimeError('没有找到已连接的物理网卡；请检查 WLAN/以太网连接')
        pcap = load_pcap()
        self.stop_event = threading.Event()
        self.status.update(running=True, error='', interfaces=adapters, dropped=0, started=time.time())
        self.generation += 1
        handles = []
        try:
            for adapter in adapters:
                guid = adapter['InterfaceGuid'].strip('{}')
                device = ('\\Device\\NPF_{' + guid + '}').encode()
                err = C.create_string_buffer(512)
                handle = pcap.pcap_open_live(device, 128, 0, 250, err)
                if not handle:
                    raise RuntimeError('无法打开网卡，请以管理员身份启动。' + err.value.decode('utf-8', 'replace'))
                handles.append((handle, adapter))
                if pcap.pcap_datalink(handle) != 1:
                    raise RuntimeError('网卡不是 Ethernet 捕获格式，未计入统计')
                if hasattr(pcap, 'pcap_setbuff'):
                    pcap.pcap_setbuff(handle, 2 * 1024 * 1024)
                if pcap.pcap_setnonblock(handle, 1, err) != 0:
                    raise RuntimeError('无法设置非阻塞抓包：' + err.value.decode('utf-8', 'replace'))
        except Exception:
            for handle, _ in handles:
                pcap.pcap_close(handle)
            self.status['running'] = False
            raise
        for handle, adapter in handles:
            thread = threading.Thread(target=self._loop, args=(pcap, handle, adapter, self.stop_event), daemon=True)
            self.threads.append(thread)
            thread.start()

    def _loop(self, pcap, handle, adapter, stop):
        mac = bytes.fromhex(adapter['MacAddress'].replace('-', '').replace(':', ''))
        header, packet = C.POINTER(PcapHeader)(), C.POINTER(C.c_ubyte)()
        last_stats = 0
        last_drop = 0
        batch = defaultdict(lambda: [0, 0])
        batch_count = 0
        last_publish = time.monotonic()
        last_packet = None

        def publish():
            nonlocal batch_count, last_publish
            if batch:
                with self.lock:
                    for key, amount in batch.items():
                        self.pending[key][0] += amount[0]
                        self.pending[key][1] += amount[1]
                    self.status['last_packet'] = last_packet
                batch.clear()
            batch_count = 0
            last_publish = time.monotonic()

        try:
            while not stop.is_set():
                result = pcap.pcap_next_ex(handle, C.byref(header), C.byref(packet))
                if result < 0:
                    raise RuntimeError(pcap.pcap_geterr(handle).decode('utf-8', 'replace'))
                if result == 1:
                    decoded = decode_frame(C.string_at(packet, header.contents.caplen), mac)
                    if decoded:
                        k = (adapter['Name'], decoded['family'], decoded['scope'], decoded['remote'],
                             decoded['port'], decoded['protocol'])
                        batch[k][0 if decoded['direction'] == 'up' else 1] += decoded['bytes']
                        batch_count += 1
                        last_packet = time.time()
                now = time.monotonic()
                if batch_count >= 128 or now - last_publish >= .1:
                    publish()
                if result == 0:
                    stop.wait(.05)
                if now - last_stats > 2:
                    stats = PcapStat()
                    if pcap.pcap_stats(handle, C.byref(stats)) == 0:
                        dropped = stats.dropped + stats.interface_dropped
                        with self.lock:
                            self.status['dropped'] += max(0, dropped - last_drop)
                        last_drop = dropped
                    last_stats = time.monotonic()
        except Exception as e:
            self.status.update(error=str(e), running=False)
        finally:
            publish()
            pcap.pcap_close(handle)

    def drain(self):
        with self.lock:
            result = dict(self.pending)
            self.pending.clear()
        return result

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=2)
        # A handle is closed by its own reading thread, never from a second thread.
        self.threads = [t for t in self.threads if t.is_alive()]
        if self.threads:
            raise RuntimeError('网卡读取尚未退出，请稍后再试')
        self.status['running'] = False
