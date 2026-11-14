from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import subprocess
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

import yaml

from .api import Mihomo
from .capture import Capture, physical_adapters
from .common import CREATE_NO_WINDOW, DATA, VENDOR, atomic_write, is_admin, read_json, write_json
from .config import (Settings, build_config, discover, import_local, register_warp,
                     render, validate_kernel)
from .deps import run_install_job
from .probe import check_trace
from .system import enable_system_proxy, get_system_proxy, process_identity, set_system_proxy, terminate_identity
from .traffic import DeltaTracker, Store, classify_connection, ip_family
from .warp import Warp
from .nodes import AI_GROUP, DEFAULT_GROUP, catalog, choices
from .logs import start_logged


class Manager:
    def __init__(self, auto_capture=True):
        self.settings = Settings()
        self.store = Store(DATA / 'traffic.sqlite3')
        self.capture = Capture()
        self.capture_lock = threading.RLock()
        self.warp = Warp(self.store.event, self.owned)
        self.core_process = None
        self.operation = threading.Lock()
        self.download_cancel = threading.Event()
        self.journal_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.tracker = DeltaTracker()
        self.active_config = None
        self.active_text = None
        self.journal = read_json(DATA / 'recovery.json')
        self.state = {'phase': 'recovery' if self.journal else 'idle', 'error': '', 'api_online': False,
            'tun': False, 'mode': self.settings.value['mode'], 'health': {}, 'connections': [],
            'sample_time': None, 'capture_error': '', 'operation': '', 'watchdog_restores': 0,
            'kernel_counters': {}, 'admin': is_admin(), 'rates': {}, 'coverage': '尚未开始采样'}
        self.history = deque(maxlen=90)
        self.state['capture_requested'] = bool(auto_capture and self.settings.value['auto_capture'] and not self.journal)
        self.egress_evidence = {}
        self.source_cache = None
        self.source_lock = threading.Lock()
        self.static_cache = None
        self.monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self.monitor_thread.start()
        self.health_thread = threading.Thread(target=self._health, daemon=True)
        self.health_thread.start()

    def source(self):
        path = DATA / 'source.yaml'
        if not path.exists():
            return {}
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        with self.source_lock:
            if self.source_cache is None or self.source_cache[0] != key:
                self.source_cache = (key, yaml.safe_load(path.read_text('utf-8-sig')) or {})
            return self.source_cache[1]

    def client(self, existing=False):
        s = self.settings.value
        if not existing and self.state['phase'] == 'running' and self.state['mode'] == 'standalone':
            return Mihomo(f'http://127.0.0.1:{s["core_api_port"]}')
        source = self.source() if (DATA / 'source.yaml').exists() else {}
        return Mihomo(s['api'], source.get('secret', ''))

    def owned(self, name, process):
        with self.journal_lock:
            if self.journal:
                identity = process_identity(process.pid)
                if identity is None:
                    raise RuntimeError('无法记录进程身份，取消启动以保证可恢复')
                self.journal.setdefault('owned', {})[name] = identity
                write_json(DATA / 'recovery.json', self.journal)

    def import_config(self):
        if self.state['phase'] in ('running', 'starting', 'recovery'):
            raise RuntimeError('请先停止并回滚，再重新导入配置')
        result = import_local(self.settings)
        parts = []
        parts.append('机场：' + result['airport'] if result['airport'] else '机场：未配置，仅用 WARP')
        parts.append('WARP：' + ('已就绪' if result['warp'] else '未启用'))
        detail = '；'.join(parts)
        if result.get('notes'):
            detail += '（' + '；'.join(result['notes']) + '）'
        self.store.event('config', '已初始化本机依赖：' + detail)
        return result

    def install_deps(self):
        if self.state['phase'] in ('running', 'starting', 'recovery'):
            raise RuntimeError('请先停止并回滚，再下载依赖')
        try:
            result = run_install_job(self.download_cancel)
        finally:
            # A completed component or atomic settings save can survive a later timeout.
            self.settings = Settings()
            self.source_cache = None
            self.static_cache = None
        done = result['downloaded']
        self.store.event('config', '已下载 ' + '、'.join(done) if done else '依赖已齐全，无需下载')
        return result

    def acquire_operation(self, name):
        if name in ('deps-cancel', 'stop', 'recover', 'exit') and self.state['operation'] == 'deps-install':
            self.download_cancel.set()
            return self.operation.acquire(timeout=5)
        return self.operation.acquire(blocking=False)

    def register_warp_account(self):
        if self.state['phase'] in ('running', 'starting', 'recovery'):
            raise RuntimeError('请先停止并回滚，再注册 WARP 账户')
        path = register_warp(self.settings)
        value = copy.deepcopy(self.settings.value)
        value['warp_enable'] = True
        self.settings.save(value)
        self.store.event('config', '已注册 WARP 账户')
        return {'message': 'WARP 账户已就绪', 'account': path}

    def preview(self):
        cfg = build_config(self.source(), self.settings.value, self.settings.value['mode'] == 'standalone')
        text = render(cfg)
        validate_kernel(text, discover(self.settings.value)['kernel'])
        # Return only reviewable policy; never proxy passwords or subscription URLs.
        return {'valid': True, 'rules': cfg['rules'], 'dns': cfg['dns']['nameserver'],
                'proxies': [{k: p[k] for k in ('name','type','server','port','ip-version','dialer-proxy') if k in p} for p in cfg['proxies']],
                'groups': cfg['proxy-groups'],
                'mode': self.settings.value['mode'], 'sha256': hashlib.sha256(text.encode()).hexdigest()}

    def select_nodes(self, values):
        if self.state['phase'] not in ('idle', 'running'):
            raise RuntimeError('请先完成当前操作或恢复上次现场')
        previous = copy.deepcopy(self.settings.value)
        updated = copy.deepcopy(previous)
        for key in ('default_node', 'ai_node'):
            value = values.get(key, previous.get(key, ''))
            if not isinstance(value, str):
                raise ValueError('节点选择无效')
            updated[key] = value
        cfg = build_config(self.source(), updated, updated['mode'] == 'standalone')
        text = render(cfg)
        validate_kernel(text, discover(updated)['kernel'])
        running = self.state['phase'] == 'running'
        before = {}
        api = self.client() if running else None
        if api:
            current = api.request('/proxies').get('proxies', {})
            for name in (DEFAULT_GROUP, AI_GROUP):
                if name not in current or 'now' not in current[name]:
                    raise RuntimeError('当前内核尚未加载多节点策略，请停止并重新开启链路')
                before[name] = current[name]['now']
        try:
            if api:
                from urllib.parse import quote
                for group in cfg['proxy-groups']:
                    api.request('/proxies/' + quote(group['name'], safe=''), 'PUT', {'name': group['proxies'][0]})
            self.settings.save(updated)
            if running:
                atomic_write(DATA / 'active.yaml', text)
        except Exception as error:
            failures = []
            if api:
                from urllib.parse import quote
                for name, selected in before.items():
                    try:
                        api.request('/proxies/' + quote(name, safe=''), 'PUT', {'name': selected})
                    except Exception as restore_error:
                        failures.append(str(restore_error))
            if self.settings.value != previous:
                try:
                    self.settings.save(previous)
                except Exception as restore_error:
                    failures.append(str(restore_error))
            if failures:
                raise RuntimeError(f'切换失败：{error}；恢复选择未完成：' + '；'.join(failures)) from error
            raise
        if running:
            self.active_config, self.active_text = cfg, text
        selected = choices(updated)
        self.state['node_selections'] = selected
        self.state['node_health'] = {}
        self.state['health'] = {}
        self.store.event('nodes', f'AI 服务 → {selected["ai"]}；默认出口 → {selected["default"]}')
        return {'message': '节点选择已保存并生效；已有长连接继续使用原节点，新连接使用新选择' if running else '节点选择已保存，开启链路后生效',
                'selected': selected}

    def probe_nodes(self):
        if self.state['phase'] != 'running':
            raise RuntimeError('请先开启链路，再验证两个策略组的出口')
        cfg = self.active_config
        port = self.settings.value['mixed_port'] if self.settings.value['mode'] == 'standalone' else cfg.get('mixed-port') or self.settings.value['mixed_port']
        result = {'default': check_trace(port), 'ai': check_trace(port, host='one.one.one.one')}
        self.state['node_health'] = result
        self.state['health'] = result['default']
        return {'message': '两个策略组的 HTTPS 出口验证已完成', 'health': result}

    def _runtime_backup(self, api):
        runtime = discover(self.settings.value)['runtime']
        path = Path(runtime) if runtime else None
        if not path or not path.is_file():
            raise RuntimeError('无法为现有内核建立完整回滚点：没有找到它的配置文件')
        text = path.read_text('utf-8-sig')
        cfg = yaml.safe_load(text)
        self._assert_original(cfg)
        actual = api.request('/rules').get('rules', [])
        if any(str(r.get('proxy', '')).startswith('CL-') for r in actual):
            raise RuntimeError('现有内核仍运行 CampusLink 配置，请先在 Verge 重新应用订阅')
        # GET /configs is only a partial view. Verify the full file against live rules.
        if len(actual) != len(cfg.get('rules', [])):
            raise RuntimeError('生成文件与运行时规则数量不符，无法确认回滚源。请在 Verge 重新应用订阅后重试')
        expected = [r.split(',')[-2] if r.endswith(',no-resolve') else r.split(',')[-1] for r in cfg['rules']]
        if any(r.get('proxy') != target for r, target in zip(actual, expected)):
            raise RuntimeError('运行时策略与文件不符，无法确认回滚源。请先在 Verge 重新应用订阅')
        general = api.request('/configs')
        # Preserve toggles changed through PATCH after the file was generated.
        for key in ('mode','ipv6','mixed-port','port','socks-port','allow-lan','log-level'):
            if key in general:
                cfg[key] = general[key]
        if 'tun' in general:
            cfg['tun'] = general['tun']
        selectors = {name: p['now'] for name,p in api.request('/proxies').get('proxies', {}).items()
                     if p.get('type') == 'Selector' and 'now' in p}
        return render(cfg), selectors, general

    @staticmethod
    def _assert_original(cfg):
        if not isinstance(cfg, dict):
            raise RuntimeError('原内核配置备份格式无效')
        names = [p.get('name', '') for p in cfg.get('proxies', []) + cfg.get('proxy-groups', [])]
        targets = [r.split(',')[-2] if r.endswith(',no-resolve') else r.split(',')[-1]
                   for r in cfg.get('rules', [])]
        if any(str(name).startswith('CL-') for name in names + targets) \
                or (cfg.get('tun') or {}).get('device') == 'CampusLink':
            raise RuntimeError('回滚源包含 CampusLink 自身配置，已拒绝写入原内核；请在 Verge 重新应用订阅')

    @staticmethod
    def _verify_general(api, expected):
        actual = api.request('/configs')
        for key in ('mixed-port', 'port', 'socks-port', 'mode', 'ipv6'):
            if key in expected and actual.get(key) != expected[key]:
                raise RuntimeError(f'原内核 {key} 恢复校验失败')
        if expected.get('tun') is not None and actual.get('tun', {}).get('enable') != expected['tun'].get('enable'):
            raise RuntimeError('原内核 TUN 恢复校验失败')

    def _restore_attached(self, api, journal):
        path = Path(journal['runtime'])
        if path.resolve() != (Path(journal['backup']) / 'before.yaml').resolve():
            raise RuntimeError('回滚路径不是原内核 before.yaml 备份')
        original = path.read_text('utf-8-sig')
        cfg = yaml.safe_load(original)
        self._assert_original(cfg)
        digest = journal.get('runtime_sha256')
        if digest and hashlib.sha256(original.encode()).hexdigest() != digest:
            raise RuntimeError('原配置备份校验和不符')
        expected = journal.get('previous_general') or cfg
        # Check the snapshot before any PUT, not only after an incorrect restore.
        for key in ('mixed-port', 'port', 'socks-port', 'mode', 'ipv6'):
            if key in expected and cfg.get(key) != expected[key]:
                raise RuntimeError(f'原配置备份 {key} 与开启前状态不符')
        if expected.get('tun') is not None and (cfg.get('tun') or {}).get('enable') != expected['tun'].get('enable'):
            raise RuntimeError('原配置备份 TUN 与开启前状态不符')
        api.reload(original)
        self._verify_general(api, expected)
        from urllib.parse import quote
        for name, selected in journal.get('selectors', {}).items():
            api.request('/proxies/' + quote(name, safe=''), 'PUT', {'name': selected})

    def _restore_policy_if_changed(self, api, rules, cfg):
        targets = [r.split(',')[-2] if r.endswith(',no-resolve') else r.split(',')[-1]
                   for r in cfg['rules']]
        if len(rules) == len(targets) and all(r.get('proxy') == t for r, t in zip(rules, targets)):
            return
        with self.operation:
            # A sample may have acquired Verge's client while we were still idle.
            # Never send a standalone config through that stale client after start.
            if self.state['phase'] != 'running' or self.state['mode'] != 'attach' \
                    or self.active_config is not cfg or not self.active_text \
                    or not self.journal or self.journal.get('mode') != 'attach' \
                    or api.base != self.journal['previous_api']:
                return
            api.reload(self.active_text)
            self.state['watchdog_restores'] += 1
            self.store.event('config', '检测到外部覆盖，已重载本工具最后校验通过的 IPv6 配置')

    def start(self):
        if self.state['phase'] == 'running':
            return {'message': '链路已开启'}
        if self.journal:
            raise RuntimeError('检测到未完成的恢复记录，请先点击“恢复上次现场”')
        s = copy.deepcopy(self.settings.value)
        if s['warp_enable'] and not discover(s).get('account'):
            try:
                register_warp(self.settings)
            except Exception as error:
                raise RuntimeError(f'{error}；也可以先关闭 WARP 再开启链路') from error
        if s['mode'] == 'standalone' and s['tun'] and not is_admin():
            raise RuntimeError('独立 TUN 模式需要管理员权限：请以管理员身份重新运行本程序')
        if s['mode'] == 'standalone':
            for port in (s['core_api_port'], s['mixed_port'], s['socks_port']):
                with socket.socket() as listener:
                    try:
                        listener.bind(('127.0.0.1', int(port)))
                    except OSError as e:
                        raise RuntimeError(f'独立链路端口 {port} 已被占用，请修改端口后再开启') from e
        cfg = build_config(self.source(), s, s['mode'] == 'standalone')
        text = render(cfg)
        kernel = discover(s)['kernel']
        validate_kernel(text, kernel)
        backup_dir = DATA / 'backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        backup_dir.mkdir(parents=True)
        atomic_write(backup_dir / 'candidate.yaml', text)
        write_json(backup_dir / 'settings.json', s)
        self.state.update(phase='starting', error='', mode=s['mode'])
        try:
            existing = self.client(existing=True)
            journal = {'mode': s['mode'], 'backup': str(backup_dir), 'owned': {}, 'proxy': None,
                       'previous_api': existing.base, 'previous_secret': existing.secret,
                       'previous_tun': None, 'runtime': None, 'selectors': {}, 'stage': 'prepared'}
            if s['mode'] == 'attach':
                original, selected, general = self._runtime_backup(existing)
                self._assert_original(yaml.safe_load(original))
                atomic_write(backup_dir / 'before.yaml', original)
                journal.update(runtime=str(backup_dir / 'before.yaml'), selectors=selected,
                    previous_general=general, runtime_sha256=hashlib.sha256(original.encode()).hexdigest())
            else:
                journal['proxy'] = get_system_proxy()
                try:
                    general = existing.request('/configs')
                    journal['previous_tun'] = general.get('tun')
                    journal['previous_general'] = general
                except Exception:
                    pass  # A genuinely standalone start does not require Verge.
            with self.journal_lock:
                self.journal = journal
                write_json(DATA / 'recovery.json', journal)
            if s['mode'] == 'standalone':
                if journal['previous_tun'] and journal['previous_tun'].get('enable'):
                    paused = dict(journal['previous_tun'], enable=False)
                    existing.request('/configs', 'PATCH', {'tun': paused})
                # Disable listeners belonging to other programs only through their documented API.
                atomic_write(DATA / 'active.yaml', text)
                if not kernel:
                    raise RuntimeError('没有找到 mihomo 内核，无法启动独立链路')
                self.core_process = start_logged([kernel, '-d', str(VENDOR),
                    '-f', str(DATA / 'active.yaml')], DATA / 'mihomo.log', creationflags=CREATE_NO_WINDOW)
                self.owned('core', self.core_process)
                api = Mihomo(f'http://127.0.0.1:{s["core_api_port"]}')
                for _ in range(80):
                    if self.core_process.poll() is not None:
                        raise RuntimeError('独立内核启动失败，请查看 data/mihomo.log')
                    try:
                        api.request('/version', timeout=.5)
                        break
                    except Exception:
                        time.sleep(.25)
                else:
                    raise RuntimeError('独立内核启动超时')
                # Start WARP only after exact outer IPv6 exceptions are loaded in the new core.
                if s['warp_enable']:
                    self.warp.start(s)
                enable_system_proxy(s['mixed_port'])
            else:
                existing.reload(text)
                if s['warp_enable']:
                    self.warp.start(s)
            self.active_config, self.active_text = cfg, text
            self.tracker = DeltaTracker()
            with self.journal_lock:
                self.journal['stage'] = 'active'
                write_json(DATA / 'recovery.json', self.journal)
            self.state.update(phase='running', mode=s['mode'], error='')
            self.store.event('start', '已启用 IPv6 广覆盖策略；公网 IPv4 仅允许显式例外')
            return {'message': 'IPv6 链路已开启；WARP 正在执行 HTTPS 健康验证'}
        except Exception as e:
            self.state['error'] = str(e)
            if self.journal:
                try:
                    self.rollback()
                except Exception as restore_error:
                    self.state.update(phase='recovery', error=f'{e}；自动恢复未完成：{restore_error}')
            else:
                self.state['phase'] = 'idle'
            raise

    def rollback(self):
        with self.journal_lock:
            journal = copy.deepcopy(self.journal)
        self.state['phase'] = 'stopping'
        errors = []
        try:
            self.capture_stop()
        except Exception as e:
            errors.append('物理监控停止失败：' + str(e))
        if not journal:
            self.state.update(phase='idle', error='；'.join(errors))
            if errors:
                raise RuntimeError(self.state['error'])
            return {'message': '已停止监控，没有待回滚的网络变更'}
        try:
            self.warp.stop()
        except Exception as e:
            errors.append(str(e))
        for identity in journal.get('owned', {}).values():
            try:
                terminate_identity(identity)
            except Exception as e:
                errors.append(str(e))
        api = Mihomo(journal['previous_api'], journal.get('previous_secret', ''))
        # Standalone never loads any file into Verge, including legacy recovery records.
        mode = journal.get('mode')
        if mode not in ('standalone', 'attach'):
            errors.append('恢复记录缺少有效运行模式，已拒绝修改原内核')
        elif mode == 'attach':
            try:
                self._restore_attached(api, journal)
            except Exception as e:
                errors.append('内核配置恢复失败：' + str(e))
        if mode == 'standalone' and journal.get('previous_tun') is not None:
            try:
                if journal['previous_tun'].get('device') == 'CampusLink':
                    raise RuntimeError('原 TUN 备份包含 CampusLink 设备，已拒绝写入 Verge')
                api.request('/configs', 'PATCH', {'tun': journal['previous_tun']})
            except Exception as e:
                errors.append('原 TUN 恢复失败：' + str(e))
        if journal.get('mode') == 'standalone' and journal.get('previous_general'):
            try:
                self._verify_general(api, journal['previous_general'])
            except Exception as e:
                errors.append('原内核恢复校验失败：' + str(e))
        if journal.get('proxy') is not None:
            try:
                set_system_proxy(journal['proxy'])
            except Exception as e:
                errors.append('系统代理恢复失败：' + str(e))
        if errors:
            self.state.update(phase='recovery', error='；'.join(errors))
            raise RuntimeError(self.state['error'])
        with self.journal_lock:
            self.journal = None
            (DATA / 'recovery.json').unlink(missing_ok=True)
        self.active_config = self.active_text = None
        # TUN 状态跟着内核一起结束，别让界面停在“TUN 已开启”。
        self.state.update(phase='idle', error='', tun=False,
                          coverage='TUN 未开启；业务列表只覆盖已接管连接')
        self.tracker = DeltaTracker()
        self.store.event('rollback', '已停止本工具链路并恢复原配置、原 TUN 和系统代理')
        return {'message': '已停止并恢复原网络配置'}

    def save_settings(self, value):
        if self.journal:
            raise RuntimeError('修改配置前请先停止并回滚，然后保存、预览并重新开启')
        self.settings.save(value)
        self.state['mode'] = self.settings.value['mode']
        self.store.event('config', '已保存工具配置；尚未应用到网络')
        return {'message': '配置已保存，点击校验后可开启'}

    def capture_start(self):
        with self.capture_lock:
            self.state['capture_requested'] = True
            try:
                self.capture.start(self.settings.value['capture_interfaces'])
            except Exception as error:
                message = f'物理流量监控暂不可用：{error}；网络恢复后将自动重试'
                self.state['capture_error'] = message
                raise RuntimeError(message) from error
            self.state['capture_error'] = ''
        self.store.event('monitor', '物理网卡监控已开启')
        return {'message': '物理网卡监控已开启'}

    def capture_stop(self):
        with self.capture_lock:
            # Manual pause and rollback must cancel delayed reconnect attempts.
            self.state['capture_requested'] = False
            self.capture.stop()
            self.capture.status['error'] = ''
            self.state['capture_error'] = ''
        return {'message': '物理监控已暂停'}

    def _refresh_capture(self):
        """Retry requested monitoring after disconnects, but respect manual pauses."""
        with self.capture_lock:
            if not self.state['capture_requested'] or self.state['phase'] in ('recovery', 'stopping'):
                return
            try:
                selected = self.settings.value['capture_interfaces']
                if self.capture.status['running']:
                    adapters = physical_adapters()
                    if selected:
                        adapters = [a for a in adapters if a['Name'] in selected]
                    if not adapters:
                        self.capture.stop()
                        self.state['capture_error'] = '未检测到已连接的物理网卡；网络恢复后将自动重试流量监控'
                        return
                    before = {a['InterfaceGuid'] for a in self.capture.status['interfaces']}
                    after = {a['InterfaceGuid'] for a in adapters}
                    if before != after:
                        self.capture.start(selected)
                else:
                    self.capture.start(selected)
                # Successful enumeration also clears errors from earlier checks.
                self.state['capture_error'] = ''
            except Exception as error:
                self.state['capture_error'] = f'物理流量监控暂不可用：{error}；网络恢复后将自动重试'

    def _monitor(self):
        pending = defaultdict(lambda: [0, 0])
        last_flush = 0
        tick = 0
        previous_time = time.monotonic()
        last_maintenance = 0
        cached_config = None
        proxies = {}
        pending_day = datetime.now().strftime('%Y-%m-%d')
        next_capture_check = 0
        while not self.stop_event.is_set():
            tick += 1
            capture_time = time.monotonic()
            if capture_time >= next_capture_check:
                self._refresh_capture()
                next_capture_check = time.monotonic() + 10
            rates = {'ipv4': [0,0], 'ipv6': [0,0], 'lan': [0,0]}
            # Commit the previous day's deltas before consuming a new day's samples.
            day = datetime.now().strftime('%Y-%m-%d')
            if day != pending_day:
                try:
                    self.store.add([(*key, *value) for key,value in pending.items()], pending_day)
                    pending.clear()
                    pending_day = day
                except Exception as error:
                    self.state['storage_error'] = '跨日写入失败：' + str(error)
                    self.stop_event.wait(1)
                    continue
            for key, amount in self.capture.drain().items():
                iface, family, scope, remote, port, protocol = key
                if family == 6 and scope == 'public':
                    self.egress_evidence[remote] = time.time()
                category = 'lan' if scope == 'lan' else 'ipv' + str(family)
                k = ('wire', category, '', '', remote + (f':{port}' if family == 4 else f' [{port}]'), iface, protocol)
                pending[k][0] += amount[0]
                pending[k][1] += amount[1]
                rates[category][0] += amount[0]
                rates[category][1] += amount[1]
            cutoff = time.time() - 20
            self.egress_evidence = {address: stamp for address, stamp in self.egress_evidence.items() if stamp >= cutoff}
            now = time.monotonic()
            elapsed = max(.1, now - previous_time)
            previous_time = now
            self.state['rates'] = {k: [round(n / elapsed) for n in v] for k,v in rates.items()}
            self.history.append({'time': time.time(), **{k: sum(v) / elapsed for k,v in rates.items()}})
            try:
                api = self.client()
                payload = api.request('/connections', timeout=2)
                connections = payload.get('connections') or []
                cfg = self.active_config or (self.source() if (DATA / 'source.yaml').exists() else {})
                if cfg is not cached_config:
                    proxies = {p['name']:p for p in cfg.get('proxies', [])}
                    cached_config = cfg
                current = []
                evidence = self.state.get('warp_evidence')
                if self.state['phase'] == 'running' and self.settings.value['warp_enable']:
                    wh = self.warp.state['health']
                    endpoint = self.warp.state['endpoint'].split(']:')[0].lstrip('[')
                    evidence = {'ok': wh.get('ok', False), 'endpoint': endpoint,
                                'port': self.settings.value['socks_port']}
                for c, up, down in self.tracker.sample(connections):
                    category, remote, reason = classify_connection(c, proxies, evidence, self.egress_evidence)
                    m = c.get('metadata', {})
                    row = {'id': c.get('id'), 'process': m.get('process') or '未识别进程',
                           'host': m.get('host') or m.get('destinationIP') or '未知目标',
                           'remote': remote, 'category': category, 'reason': reason,
                           'up': up, 'down': down, 'protocol': m.get('network', '').upper(),
                           'chain': ' → '.join(reversed(c.get('chains') or [])), 'interface': ''}
                    current.append(row)
                    if (up or down) and category != 'outer':
                        key = ('business', category, row['process'], row['host'], remote, '', row['protocol'])
                        pending[key][0] += up
                        pending[key][1] += down
                self.state.update(api_online=True, connections=current, sample_time=time.time(),
                    kernel_counters={'up': payload.get('uploadTotal'), 'down': payload.get('downloadTotal')})
                if tick % 5 == 0:
                    general = api.request('/configs', timeout=2)
                    self.state['tun'] = general.get('tun', {}).get('enable', False)
                    self.state['coverage'] = 'TUN 已开启；物理统计包含绕过代理的流量' if self.state['tun'] else 'TUN 未开启；业务列表只覆盖已接管连接'
                    if self.state['phase'] == 'running' and self.state['mode'] == 'attach' and self.active_config:
                        rules = api.request('/rules', timeout=2).get('rules', [])
                        self._restore_policy_if_changed(api, rules, cfg)
                outer = [c for c in connections if c.get('metadata', {}).get('process', '').lower() == 'warpscout.exe']
                self.state['observed_warp_ipv6'] = next((c['metadata']['remoteDestination'] for c in outer
                    if ip_family(c['metadata'].get('remoteDestination', '')) == 6), '')
            except Exception as e:
                self.state.update(api_online=False, api_error=str(e), connections=[])
                # Preserve baselines across transient API failure. Successful reloaded cores
                # have fresh connection UUIDs, so their counters cannot become negative.
            if now - last_flush >= 10:
                try:
                    self.store.add([(*key, *value) for key,value in pending.items()], pending_day)
                    pending.clear()
                    self.state['storage_error'] = ''
                except Exception as error:
                    self.state['storage_error'] = '流量写入失败，正在重试：' + str(error)
                    # Collapse target detail under sustained failure, preserving all bytes.
                    if len(pending) > 10000:
                        compact = defaultdict(lambda: [0, 0])
                        for key, amount in pending.items():
                            group = (key[0], key[1], '', '写盘失败期间合并记录', '', key[5], key[6])
                            compact[group][0] += amount[0]
                            compact[group][1] += amount[1]
                        pending = compact
                last_flush = now
            if now - last_maintenance >= 3600:
                try:
                    self.store.maintain()
                except Exception as error:
                    self.state['storage_error'] = '历史整理失败：' + str(error)
                last_maintenance = now
            self.stop_event.wait(1)
        # Capture.stop publishes its last batch before this thread is joined.
        for key, amount in self.capture.drain().items():
            iface, family, scope, remote, port, protocol = key
            category = 'lan' if scope == 'lan' else 'ipv' + str(family)
            group = ('wire', category, '', '', remote + (f':{port}' if family == 4 else f' [{port}]'), iface, protocol)
            pending[group][0] += amount[0]
            pending[group][1] += amount[1]
        try:
            self.store.add([(*key, *value) for key,value in pending.items()], pending_day)
        except Exception as error:
            self.state['storage_error'] = '退出前流量写入失败：' + str(error)

    def _health(self):
        while not self.stop_event.is_set():
            try:
                s = self.settings.value
                running = self.state['phase'] == 'running'
                cfg = self.active_config or self.source()
                if not running and not cfg:
                    # Nothing is configured yet: do not probe a port this tool does not own.
                    self.state['health'] = {}
                    self.state['existing_warp_health'] = {}
                    self.stop_event.wait(s['check_seconds'])
                    continue
                warp = next((p for p in cfg.get('proxies', []) if p.get('type') == 'socks5'
                            and p.get('server') in ('127.0.0.1', '::1')), None)
                port = s['mixed_port'] if running and s['mode'] == 'standalone' else cfg.get('mixed-port') or s['mixed_port']
                health = check_trace(port)
                if running:
                    ai_health = check_trace(port, host='one.one.one.one')
                    if s is self.settings.value and cfg is self.active_config:
                        self.state['node_health'] = {'default': health, 'ai': ai_health}
                        self.state['health'] = health
                else:
                    self.state['health'] = health
                if running:
                    wh = self.warp.state['health']
                    endpoint = self.warp.state['endpoint'].split(']:')[0].lstrip('[')
                else:
                    wh = check_trace(warp['port'], True) if warp else {}
                    endpoint = self.state.get('observed_warp_ipv6', '')
                self.state['warp_evidence'] = {'ok': wh.get('ok', False), 'endpoint': endpoint,
                                               'time': time.time(), 'port': warp['port'] if warp else None}
                self.state['existing_warp_health'] = wh
            except Exception as e:
                self.state['health'] = {'ok': False, 'error': str(e)}
            self.stop_event.wait(self.settings.value['check_seconds'])

    def snapshot(self, day=None, view='full', plane='live'):
        if view == 'status':
            return {'state': copy.deepcopy({k: v for k, v in self.state.items() if k != 'connections'})}
        full = view == 'full'
        data = self.store.snapshot(day, include_top=full or (view == 'traffic' and plane != 'live'),
                                   include_events=full or view == 'activity')
        # Select before copying; hidden connection tables can contain thousands of rows.
        state = {k: v for k, v in self.state.items() if k != 'connections'}
        if full or (view == 'traffic' and plane == 'live'):
            rows = self.state['connections']
            state['connection_count'] = len(rows)
            state['connections'] = rows if full else sorted(rows, key=lambda r: r['up'] + r['down'], reverse=True)[:200]
        data.update(state=copy.deepcopy(state), capture=copy.deepcopy(self.capture.status),
                    warp=copy.deepcopy(self.warp.state), settings=copy.deepcopy(self.settings.value),
                    history=list(self.history) if full or view == 'overview' else [])
        if full:
            data['discovery'] = discover(self.settings.value)
        data['state']['recovery_required'] = self.journal is not None and self.state['phase'] == 'recovery'
        try:
            source = self.source()
            cached = self.static_cache
            if cached is None or cached[0] is not source or cached[1] is not self.settings.value:
                nodes = {'catalog': catalog(source, self.settings.value),
                             'selected': choices(self.settings.value),
                             'ai_rule_count': len({'DOMAIN-SUFFIX,' + d for d in self.settings.value.get('ai_domains', [])}
                                                  | set(self.settings.value.get('ai_extra_rules', [])))}
                self.static_cache = (source, self.settings.value, nodes)
            if full or view in ('nodes', 'settings'):
                data['nodes'] = copy.deepcopy(self.static_cache[2])
        except Exception as error:
            data['nodes'] = {'catalog': [], 'selected': {}, 'error': str(error)}
        return data

    def close(self):
        if self.journal:
            self.rollback()
        self.capture.stop()
        self.stop_event.set()
        self.monitor_thread.join(timeout=15)
        if self.monitor_thread.is_alive():
            raise RuntimeError('流量采样尚未结束，请稍后重试退出')
        self.store.close()
