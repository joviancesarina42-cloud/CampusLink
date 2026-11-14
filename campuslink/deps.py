"""Fetch the two external executables from their official GitHub releases.

Only used when the local copy has no kernel or no warpscout: an unpacked
release ships neither, and asking a new user to find them by hand is where
most people stop.
"""
from __future__ import annotations

import io
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .common import CREATE_NO_WINDOW, DATA, ROOT, VENDOR, atomic_write, read_json, write_json

INSTALL_TIMEOUT = 60
REQUEST_TIMEOUT = 5

API = 'https://api.github.com/repos/{}/releases/latest'
ASSETS = {
    'mihomo': {'repo': 'MetaCubeX/mihomo',
               'asset': 'mihomo-windows-amd64-{tag}.zip',
               'target': 'mihomo.exe'},
    'warpscout': {'repo': 'vernette/warpscout',
                  'asset': 'warpscout_{ver}_windows_amd64.zip',
                  'target': 'warpscout.exe'},
}
HEADERS = {'User-Agent': 'CampusLink', 'Accept': 'application/vnd.github+json'}


def _open(url, timeout):
    request = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(request, timeout=timeout)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('下载及初始化已超过 60 秒，已停止，请检查网络后重新下载')
    return remaining


def _download(url, timeout=REQUEST_TIMEOUT, *, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + INSTALL_TIMEOUT
    attempt = 0
    while True:
        remaining = _remaining(deadline)
        try:
            with _open(url, min(timeout, remaining)) as response:
                chunks = []
                size = 0
                while True:
                    _remaining(deadline)
                    chunk = response.read1(256 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                expected = response.headers.get('Content-Length')
                if expected and size != int(expected):
                    raise OSError('下载未完整传输')
                return b''.join(chunks)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in (408, 429, 500, 502, 503, 504):
                raise
        except (OSError, EOFError, http.client.IncompleteRead):
            pass
        # Both API queries and asset transfers retry within the same total budget.
        attempt += 1
        time.sleep(min(0.5 * min(attempt, 4), _remaining(deadline)))


def _latest(repo, timeout=REQUEST_TIMEOUT, *, deadline=None):
    try:
        return json.loads(_download(API.format(repo), timeout, deadline=deadline).decode('utf-8'))
    except Exception as error:
        raise RuntimeError(f'无法查询 {repo} 的最新版本：{error}') from error


def install(name, dest=None, *, deadline=None):
    """Download one component and return the installed path."""
    spec = ASSETS[name]
    deadline = deadline if deadline is not None else time.monotonic() + INSTALL_TIMEOUT
    target = Path(dest) if dest else VENDOR / spec['target']
    target.parent.mkdir(parents=True, exist_ok=True)
    release = _latest(spec['repo'], deadline=deadline)
    tag = str(release.get('tag_name', ''))
    wanted = spec['asset'].format(tag=tag, ver=tag.lstrip('v'))
    asset = next((a for a in release.get('assets', []) if a.get('name') == wanted), None)
    if not asset:
        raise RuntimeError(f'{spec["repo"]} 的最新版本里没有 {wanted}')
    payload = _download(asset['browser_download_url'], deadline=deadline)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        member = next((m for m in archive.namelist() if m.lower().endswith('.exe')), None)
        if not member:
            raise RuntimeError(f'{wanted} 里没有可执行文件')
        executable = archive.read(member)
        _remaining(deadline)
        atomic_write(target, executable)
    return str(target)


def install_missing(kernel=False, warp=False):
    deadline = time.monotonic() + INSTALL_TIMEOUT
    done = []
    if kernel:
        done.append('mihomo ' + Path(install('mihomo', deadline=deadline)).name)
    if warp:
        done.append('warpscout ' + Path(install('warpscout', deadline=deadline)).name)
    return done


def worker_main(result_path):
    """Isolated job: DNS, HTTPS and WARP registration may all block in native code."""
    import copy
    from .config import Settings, discover, import_local, register_warp
    try:
        deadline = time.monotonic() + INSTALL_TIMEOUT
        settings = Settings()
        found = discover(settings.value)
        done = []
        for name, key in (('mihomo', 'kernel'), ('warpscout', 'warp')):
            if not found[key]:
                install(name, deadline=deadline)
                done.append(name)
        _remaining(deadline)
        found = discover(settings.value)
        if found['warp'] and not found['account']:
            account_path = Path(result_path).parent / 'warp-account.json'
            register_warp(settings, target=account_path)
            _remaining(deadline)
            atomic_write(DATA / 'warp-account.json', account_path.read_bytes())
            value = copy.deepcopy(settings.value)
            value['warp_enable'] = True
            settings.save(value)
        _remaining(deadline)
        result = import_local(settings)
        result['downloaded'] = done
        _remaining(deadline)
        write_json(Path(result_path), {'result': result})
        return 0
    except Exception as error:
        write_json(Path(result_path), {'error': str(error)})
        return 1


def _stop_worker(process):
    if process.poll() is None:
        if os.name == 'nt':
            # Include the warpscout registration child, not just its Python parent.
            try:
                subprocess.run([str(Path(os.environ['SystemRoot']) / 'System32' / 'taskkill.exe'),
                                '/PID', str(process.pid), '/T', '/F'],
                               capture_output=True, timeout=3, creationflags=CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


def run_install_job(cancel, timeout=INSTALL_TIMEOUT):
    """Enforce a wall-clock deadline even if OS DNS or a child process hangs."""
    deadline = time.monotonic() + timeout
    DATA.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='deps-', dir=DATA) as folder:
        result_path = Path(folder) / 'result.json'
        command = [sys.executable]
        if not getattr(sys, 'frozen', False):
            command.append(str(ROOT / 'app.py'))
        command += ['--deps-worker', str(result_path)]
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=CREATE_NO_WINDOW)
        try:
            while True:
                if cancel.is_set():
                    raise RuntimeError('下载已取消，可重新下载或退出程序')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('下载及初始化已超过 60 秒，已停止，请检查网络后重新下载')
                if process.poll() is not None:
                    break
                cancel.wait(min(0.1, remaining))
            result = read_json(result_path, {})
            if process.returncode or 'error' in result:
                raise RuntimeError(result.get('error') or '下载任务异常退出，请重新下载')
            if 'result' not in result:
                raise RuntimeError('下载任务未返回结果，请重新下载')
            return result['result']
        finally:
            _stop_worker(process)
