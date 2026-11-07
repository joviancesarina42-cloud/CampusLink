import os
import re
import socket
import subprocess
import threading
import time

from .common import CREATE_NO_WINDOW, DATA
from .config import STANDARD_V6_HOSTS, discover, parse_endpoint
from .probe import check_trace
from .logs import drain_log, start_logged


class Warp:
    def __init__(self, log, owned):
        self.log = log
        self.owned = owned
        self.process = None
        self.stop_event = threading.Event()
        self.thread = None
        self.scan_process = None
        self.binary = ''
        self.account = ''
        self.state = {'status': 'stopped', 'endpoint': '', 'health': {}, 'restarts': 0}

    def start(self, settings):
        if not settings.get('warp_enable'):
            self.state['status'] = 'disabled'
            return
        if self.thread and self.thread.is_alive():
            return
        try:
            with socket.create_connection(('127.0.0.1', int(settings['socks_port'])), timeout=.5):
                raise RuntimeError('工具的 WARP 端口已被占用，请更换端口或停止之前的工具实例')
        except OSError:
            pass
        found = discover(settings)
        self.binary = found['warp']
        self.account = found['account']
        if not self.binary:
            raise RuntimeError('没有找到 warpscout：请把它放到 vendor/warpscout.exe 或填写路径')
        if not self.account:
            raise RuntimeError('没有找到 WARP 账户：请导入配置时一并提供，或关闭 WARP 后再开启链路')
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, args=(settings.copy(),), daemon=True)
        self.thread.start()

    def scan(self, settings):
        self.state['status'] = 'scanning'
        self.log('warp', '正在扫描 IPv6 MASQUE 端点')
        try:
            self.scan_process = subprocess.Popen([self.binary, 'scan', '-6', '-p', 'masque',
                '-a', self.account, '-best', '-no-report', '-tun-ping'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW, env=self._env())
            scan_error = self.scan_process.stderr
            # communicate owns stdout; the rotating-log thread exclusively owns stderr.
            self.scan_process.stderr = None
            threading.Thread(target=drain_log, args=(scan_error, DATA / 'scan.log'), daemon=True).start()
            self.owned('scan', self.scan_process)
            try:
                output, _ = self.scan_process.communicate(timeout=150)
                for endpoint in re.findall(rb'\[[0-9a-fA-F:]+\]:\d+', output):
                    value = endpoint.decode()
                    parse_endpoint(value)
                    return value
            except subprocess.TimeoutExpired:
                self.scan_process.kill()
                self.scan_process.wait(timeout=5)
        finally:
            if self.scan_process and self.scan_process.poll() is None:
                self.scan_process.kill()
                self.scan_process.wait(timeout=5)
            self.scan_process = None
        raise RuntimeError('IPv6 端点扫描没有通过隧道验证的结果，详见 data/scan.log')

    @staticmethod
    def _env():
        return {k: v for k, v in os.environ.items() if k.lower() not in ('http_proxy','https_proxy','all_proxy','no_proxy')}

    def _loop(self, s):
        endpoints = list(s['endpoints'])
        attempt = 0
        try:
            while not self.stop_event.is_set():
                endpoint = endpoints[attempt % len(endpoints)]
                self.state.update(status='connecting', endpoint=endpoint, health={})
                self.log('warp', '连接 IPv6 端点 ' + endpoint)
                try:
                    self.process = start_logged([self.binary, 'socks', '-e', endpoint,
                        '-p', 'masque', '-P', str(s['socks_port']), '-l', '127.0.0.1',
                        '-a', self.account], DATA / 'warp.log',
                        env=self._env(), creationflags=CREATE_NO_WINDOW)
                    self.owned('warp', self.process)
                    failures = 0
                    if self.stop_event.wait(10):
                        break
                    while not self.stop_event.is_set() and self.process.poll() is None:
                        health = check_trace(s['socks_port'], require_warp=True)
                        self.state['health'] = health
                        self.state['status'] = 'healthy' if health['ok'] else 'degraded'
                        if health['ok']:
                            failures = 0
                        else:
                            failures += 1
                            self.log('warp', f'HTTPS 探针失败 {failures}/{s["failure_threshold"]}：' + health['error'])
                        if failures >= s['failure_threshold']:
                            break
                        if self.stop_event.wait(s['check_seconds']):
                            break
                finally:
                    self._terminate()
                self._terminate()
                if self.stop_event.is_set():
                    break
                attempt += 1
                self.state['restarts'] += 1
                if attempt % 2 == 0:
                    try:
                        fresh = self.scan(s)
                        # Only candidates already allowed by the generated exact IPv6 exceptions
                        # may be used. MASQUE's four standard IPv6 addresses are pre-authorized.
                        allowed = {parse_endpoint(e)[0] for e in s['endpoints']} | set(STANDARD_V6_HOSTS)
                        if parse_endpoint(fresh)[0] in allowed:
                            endpoints = [fresh] + [e for e in endpoints if e != fresh]
                            attempt = 0
                            self.log('warp', '采用重扫端点 ' + fresh)
                    except Exception as e:
                        self.log('warp', str(e))
                self.stop_event.wait(min(30, 2 + attempt * 2))
        except Exception as e:
            self.state.update(status='error', error=str(e))
            self.log('error', 'WARP 守护异常：' + str(e))
        finally:
            self._terminate()
            if self.stop_event.is_set():
                self.state['status'] = 'stopped'

    def _terminate(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def stop(self):
        self.stop_event.set()
        if self.scan_process and self.scan_process.poll() is None:
            self.scan_process.terminate()
        self._terminate()
        if self.thread:
            self.thread.join(timeout=22)
            if self.thread.is_alive():
                raise RuntimeError('WARP 探针尚未退出，请稍后再试')
        self.state['status'] = 'stopped'
