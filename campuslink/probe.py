"""Bounded SOCKS5 + verified TLS/HTTP probes; no ICMP liveness inference."""
import http.client
import ipaddress
import socket
import ssl
import struct
import time


def read_exact(sock, count):
    chunks = bytearray()
    while len(chunks) < count:
        part = sock.recv(count - len(chunks))
        if not part:
            raise ConnectionError('SOCKS 回复提前结束')
        chunks.extend(part)
    return bytes(chunks)


def socks_connect(port, host, remote_port=443, timeout=9):
    sock = socket.create_connection(('127.0.0.1', int(port)), timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(b'\x05\x01\x00')
        if read_exact(sock, 2) != b'\x05\x00':
            raise ConnectionError('SOCKS5 无认证协商失败')
        encoded = host.encode('idna')
        sock.sendall(b'\x05\x01\x00\x03' + bytes([len(encoded)]) + encoded + struct.pack('!H', remote_port))
        version, rep, reserved, atyp = read_exact(sock, 4)
        if version != 5 or rep != 0 or reserved != 0:
            raise ConnectionError(f'SOCKS5 CONNECT 失败 REP={rep}')
        sizes = {1: 4, 4: 16}
        length = read_exact(sock, 1)[0] if atyp == 3 else sizes.get(atyp)
        if length is None:
            raise ConnectionError('SOCKS5 地址类型无效')
        read_exact(sock, length + 2)
        return sock
    except Exception:
        sock.close()
        raise


def check_trace(port, require_warp=False, host='www.cloudflare.com'):
    start = time.monotonic()
    sock = None
    try:
        sock = socks_connect(port, host)
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        sock = tls
        tls.sendall(('GET /cdn-cgi/trace HTTP/1.1\r\nHost: ' + host + '\r\nConnection: close\r\n\r\n').encode('ascii'))
        response = http.client.HTTPResponse(tls)
        response.begin()
        body = response.read(16385)
        if response.status != 200 or len(body) > 16384:
            raise ValueError(f'探针 HTTP 状态不是 200：{response.status}')
        fields = dict(line.split('=', 1) for line in body.decode('utf-8').splitlines() if '=' in line)
        ipaddress.ip_address(fields.get('ip', ''))
        if not fields.get('colo') or not fields.get('h'):
            raise ValueError('探针正文缺少 Cloudflare trace 标记')
        if require_warp and fields.get('warp') not in ('on', 'plus'):
            raise ValueError('探针未确认 warp=on/plus')
        return {'ok': True, 'ms': round((time.monotonic() - start) * 1000),
                'ip': fields['ip'], 'colo': fields['colo'], 'loc': fields.get('loc', ''), 'warp': fields.get('warp')}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'ms': round((time.monotonic() - start) * 1000)}
    finally:
        if sock:
            sock.close()
