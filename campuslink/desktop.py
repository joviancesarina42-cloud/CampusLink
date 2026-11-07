"""Windows desktop host control; the local HTTP server remains an internal API."""
from __future__ import annotations

import ctypes
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

from .common import CREATE_NO_WINDOW, ROOT

WINDOW_TITLE = 'CampusLink · IPv6 链路工具'
SHOW_MESSAGE = 'CampusLink.Desktop.Show.v1'
EXIT_MESSAGE = 'CampusLink.Desktop.Exit.v1'
HOST = Path('_desktop') / 'CampusLink.Desktop.exe'


def window_message(name):
    from ctypes import wintypes as W
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.FindWindowW.argtypes = [W.LPCWSTR, W.LPCWSTR]
    user.FindWindowW.restype = W.HWND
    user.RegisterWindowMessageW.argtypes = [W.LPCWSTR]
    user.RegisterWindowMessageW.restype = W.UINT
    user.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    user.PostMessageW.restype = W.BOOL
    handle = user.FindWindowW(None, WINDOW_TITLE)
    if not handle:
        return False
    return bool(user.PostMessageW(handle, user.RegisterWindowMessageW(name), 0, 0))


class Desktop:
    """Native window when the optional host is built, default browser otherwise."""

    def __init__(self, port):
        self.port = port
        self.process = None
        self.lock = threading.Lock()

    def url(self):
        return f'http://127.0.0.1:{self.port}/'

    def show(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                def restore_when_ready():
                    for _ in range(20):
                        if window_message(SHOW_MESSAGE):
                            break
                        time.sleep(.25)
                threading.Thread(target=restore_when_ready, daemon=True).start()
                return {'message': '已打开主界面'}
            host = ROOT / HOST
            if not host.is_file():
                webbrowser.open(self.url())
                return {'message': '已在默认浏览器中打开界面',
                        'hint': '运行 tools/build_desktop.ps1 可编译原生窗口与托盘'}
            self.process = subprocess.Popen([str(host), str(self.port)], cwd=str(ROOT),
                                            creationflags=CREATE_NO_WINDOW)
            return {'message': '已打开主界面'}

    def close_after_restore(self):
        """Called only after the network restoration and backend shutdown succeeded."""
        if not self.process or self.process.poll() is not None:
            return
        window_message(EXIT_MESSAGE)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
