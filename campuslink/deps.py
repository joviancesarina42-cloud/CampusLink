"""Fetch the two external executables from their official GitHub releases.

Only used when the local copy has no kernel or no warpscout: an unpacked
release ships neither, and asking a new user to find them by hand is where
most people stop.
"""
from __future__ import annotations

import io
import json
import urllib.request
import zipfile
from pathlib import Path

from .common import VENDOR

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


def _download(url, timeout=180):
    with _open(url, timeout) as response:
        return response.read()


def _latest(repo, timeout=30):
    try:
        return json.loads(_download(API.format(repo), timeout).decode('utf-8'))
    except Exception as error:
        raise RuntimeError(f'无法查询 {repo} 的最新版本：{error}') from error


def install(name, dest=None):
    """Download one component and return the installed path."""
    spec = ASSETS[name]
    target = Path(dest) if dest else VENDOR / spec['target']
    target.parent.mkdir(parents=True, exist_ok=True)
    release = _latest(spec['repo'])
    tag = str(release.get('tag_name', ''))
    wanted = spec['asset'].format(tag=tag, ver=tag.lstrip('v'))
    asset = next((a for a in release.get('assets', []) if a.get('name') == wanted), None)
    if not asset:
        raise RuntimeError(f'{spec["repo"]} 的最新版本里没有 {wanted}')
    payload = _download(asset['browser_download_url'])
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        member = next((m for m in archive.namelist() if m.lower().endswith('.exe')), None)
        if not member:
            raise RuntimeError(f'{wanted} 里没有可执行文件')
        target.write_bytes(archive.read(member))
    return str(target)


def install_missing(kernel=False, warp=False):
    done = []
    if kernel:
        done.append('mihomo ' + Path(install('mihomo')).name)
    if warp:
        done.append('warpscout ' + Path(install('warpscout')).name)
    return done
