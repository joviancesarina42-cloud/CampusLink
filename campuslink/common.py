from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
ASSETS = Path(getattr(sys, '_MEIPASS', ROOT))
DATA = ROOT / 'data'
VENDOR = ROOT / 'vendor'
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def atomic_write(path: Path, content: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode('utf-8') if isinstance(content, str) else content
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text('utf-8-sig'))
    except FileNotFoundError:
        return default


def write_json(path, value):
    atomic_write(Path(path), json.dumps(value, ensure_ascii=False, indent=2))


def run(args, timeout=20, **kwargs):
    return subprocess.run([str(x) for x in args], capture_output=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW, **kwargs)


def powershell(script, timeout=20):
    import base64
    encoded = base64.b64encode(script.encode('utf-16le')).decode()
    result = run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace')[-1000:])
    output = result.stdout.decode('utf-8-sig').strip()
    if not output:
        raise RuntimeError('读取 Windows 状态失败：命令没有返回 JSON 数据')
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError('读取 Windows 状态失败：命令返回了无效 JSON 数据') from error


def is_admin():
    if os.name != 'nt':
        return False
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())
