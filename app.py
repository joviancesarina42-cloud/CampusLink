"""CampusLink launcher. Runtime dependencies: Python + PyYAML; UI is offline."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import mimetypes
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from campuslink import __version__
from campuslink.common import ASSETS, DATA, write_json
from campuslink.manager import Manager
from campuslink.desktop import Desktop, EXIT_MESSAGE, SHOW_MESSAGE, window_message


class LocalHTTPServer(ThreadingHTTPServer):
    # SO_REUSEADDR on Windows can admit a second listener on the same port.
    # Each backend must own its port exclusively before it creates a Manager.
    allow_reuse_address = False

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def backend_serves(port, timeout=0.8):
    """True only when a live backend answers on the port; a leftover window host does not."""
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/api/token', timeout=timeout):
            return True
    except Exception:
        return False


def serve(port=18686, headless=False, auto_import=True, auto_capture=True):
    if os.name != 'nt':
        raise SystemExit('CampusLink 只支持 Windows：它依赖 WFP/TUN、Npcap 和 Win32 网络状态接口')
    if not headless:
        if backend_serves(port):
            # 已有实例：把它的窗口带回前台，然后退出本次启动。
            window_message(SHOW_MESSAGE)
            return
        # 端口没人应答，但窗口可能还在：上一次异常退出把窗口宿主留了下来。
        # 直接用它只会得到一块点不动的界面，先请它退位，再启动自己的实例。
        window_message(EXIT_MESSAGE)
        time.sleep(0.4)
    token = secrets.token_urlsafe(32)
    holder = {}
    assets = {}
    for filename in ('index.html', 'app.js', 'style.css'):
        content = (ASSETS / 'web' / filename).read_bytes()
        assets[filename] = (content, '"' + hashlib.sha256(content).hexdigest() + '"')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value, content_type='application/json; charset=utf-8', extra=None):
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False).encode()
            elif isinstance(value, str):
                value = value.encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(value)))
            if not extra or 'Cache-Control' not in extra:
                self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            if extra:
                for k,v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(value)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def valid_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}')

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {'error': '只允许本机访问'})
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            try:
                manager = holder['manager']
                if parts.path == '/api/state':
                    return self.reply(200, manager.snapshot(query.get('day', [None])[0],
                        query.get('view', ['full'])[0], query.get('plane', ['live'])[0]))
                if parts.path == '/api/token':
                    return self.reply(200, {'token': token})
                if parts.path == '/api/export':
                    day = query.get('day', [time.strftime('%Y-%m-%d')])[0]
                    # Stream bounded chunks rather than materializing the day's rows.
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/csv; charset=utf-8')
                    self.send_header('Content-Disposition', 'attachment; filename="campuslink-deltas.csv"')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.close_connection = True
                    output = io.StringIO(newline='')
                    writer = csv.writer(output)
                    writer.writerow(['time','day','session_id','plane','category','process','host','remote','interface','protocol','upload_delta_bytes','download_delta_bytes'])
                    try:
                        self.wfile.write(b'\xef\xbb\xbf')
                        for row in manager.store.iter_export(day):
                            writer.writerow(["'" + x if isinstance(x,str) and x[:1] in ('=','+','-','@') else x for x in row])
                            if output.tell() >= 65536:
                                self.wfile.write(output.getvalue().encode())
                                output.seek(0)
                                output.truncate(0)
                        self.wfile.write(output.getvalue().encode())
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass
                    return
                if parts.path in ('/', '/index.html', '/app.js', '/style.css'):
                    filename = 'index.html' if parts.path == '/' else parts.path[1:]
                    content, etag = assets[filename]
                    extra = {'ETag': etag, 'Cache-Control': 'no-cache'}
                    if self.headers.get('If-None-Match') == etag:
                        return self.reply(304, b'', extra=extra)
                    return self.reply(200, content, mimetypes.guess_type(filename)[0] or 'text/plain', extra)
                return self.reply(404, {'error': '页面不存在'})
            except Exception as e:
                self.reply(500, {'error': str(e)})

        def do_POST(self):
            if not self.valid_host() or self.headers.get('X-CampusLink') != token:
                return self.reply(403, {'error': '本机会话验证失败，请刷新界面'})
            origin = self.headers.get('Origin')
            if origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
                return self.reply(403, {'error': '请求来源无效'})
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 <= size <= 131072:
                    return self.reply(413, {'error': '配置内容过大'})
                body = json.loads(self.rfile.read(size) or b'{}')
                name = urlsplit(self.path).path.removeprefix('/api/')
                manager = holder['manager']
                if name == 'show':
                    value = holder['desktop'].show() if holder.get('desktop') else {'message': '当前为无界面运行模式'}
                    return self.reply(200, value)
                actions = {'start': manager.start, 'stop': manager.rollback,
                    'deps-cancel': lambda: {'message': '下载已停止，可重新下载'},
                    'recover': manager.rollback, 'preview': manager.preview,
                    'import': manager.import_config, 'save': lambda: manager.save_settings(body),
                    'nodes-select': lambda: manager.select_nodes(body), 'nodes-probe': manager.probe_nodes,
                    'warp-register': manager.register_warp_account,
                    'deps-install': manager.install_deps,
                    'capture-start': manager.capture_start,
                    'capture-stop': manager.capture_stop,
                    'probe': lambda: self.probe(manager), 'exit': lambda: self.exit_app(manager)}
                if name not in actions:
                    return self.reply(404, {'error': '操作不存在'})
                if not manager.acquire_operation(name):
                    return self.reply(409, {'error': '上一项操作仍在进行，请稍候'})
                job_id = secrets.token_hex(6)
                if name == 'deps-install':
                    manager.download_cancel = threading.Event()
                manager.state['operation'] = name
                manager.state['job'] = {'id': job_id, 'running': True}
                def worker():
                    try:
                        value = actions[name]()
                        manager.state['job'] = {'id': job_id, 'running': False, 'result': value}
                    except Exception as e:
                        manager.state['job'] = {'id': job_id, 'running': False, 'error': str(e)}
                        manager.store.event('error', str(e))
                    finally:
                        manager.state['operation'] = ''
                        manager.operation.release()
                threading.Thread(target=worker, daemon=True).start()
                self.reply(202, {'job': job_id})
            except Exception as e:
                self.reply(400, {'error': str(e)})

        @staticmethod
        def probe(manager):
            from campuslink.probe import check_trace
            cfg = manager.active_config or manager.source()
            port = cfg.get('mixed-port') or manager.settings.value['mixed_port']
            result = check_trace(port)
            manager.state['health'] = result
            warp = next((p for p in cfg.get('proxies',[]) if p.get('type') == 'socks5'), None)
            wh = check_trace(warp['port'], True) if warp else {'ok': False, 'error': '没有本地 SOCKS 节点'}
            manager.state['existing_warp_health'] = wh
            return {'message': 'HTTPS 探针完成', 'airport': result, 'warp': wh}

        @staticmethod
        def exit_app(manager):
            manager.close()
            threading.Thread(target=holder['server'].shutdown, daemon=True).start()
            return {'message': '已恢复原配置并退出'}

    try:
        server = LocalHTTPServer(('127.0.0.1', port), Handler)
    except OSError:
        if not headless:
            # A second launch restores the existing window, including from the tray.
            import urllib.request
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{port}/api/token', timeout=3) as response:
                existing_token = json.load(response)['token']
            request = urllib.request.Request(f'http://127.0.0.1:{port}/api/show', data=b'{}',
                headers={'X-CampusLink': existing_token, 'Content-Type': 'application/json'}, method='POST')
            with opener.open(request, timeout=3):
                pass
        return
    manager = Manager(auto_capture=auto_capture)
    desktop = None if headless else Desktop(port)
    holder.update(manager=manager, server=server, desktop=desktop)
    if auto_import and not (DATA / 'source.yaml').exists():
        try:
            manager.import_config()
        except Exception as e:
            manager.state['error'] = '自动导入尚未完成：' + str(e)
    write_json(DATA / 'app.json', {'pid': os.getpid(), 'port': port, 'started': time.time(), 'desktop': not headless})
    if sys.stdout:  # 打包成窗口程序时 stdout 为 None
        # 英文 Windows 控制台的 stdout 是 cp1252，非 ASCII 字符会抛异常并终止启动。
        try:
            print(f'CampusLink {__version__} -> http://127.0.0.1:{port}/', flush=True)
        except (OSError, UnicodeError):
            pass
    if desktop:
        desktop.show()
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        manager.close()
    finally:
        server.server_close()
        if desktop:
            desktop.close_after_restore()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='CampusLink: IPv6-first egress manager for Windows')
    parser.add_argument('--port', type=int, default=18686)
    parser.add_argument('--headless', action='store_true', help='只运行本机接口，不打开窗口')
    parser.add_argument('--no-import', action='store_true', help='启动时不自动导入本机配置')
    parser.add_argument('--no-capture', action='store_true', help='启动时不开启物理网卡监控')
    parser.add_argument('--version', action='version', version=f'CampusLink {__version__}')
    parser.add_argument('--deps-worker', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.deps_worker:
        from campuslink.deps import worker_main
        raise SystemExit(worker_main(args.deps_worker))
    serve(args.port, args.headless, not args.no_import, not args.no_capture)
