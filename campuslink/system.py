import ctypes as C
import os
from pathlib import Path


def process_identity(pid):
    """Avoid terminating a reused PID after a crash/reboot."""
    if os.name != 'nt':
        return None
    from ctypes import wintypes as W
    k = C.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    k.CloseHandle.argtypes = [W.HANDLE]
    k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
    k.GetProcessTimes.argtypes = [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4
    handle = k.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        name = C.create_unicode_buffer(32768)
        size = W.DWORD(len(name))
        times = [W.FILETIME() for _ in range(4)]
        if not k.QueryFullProcessImageNameW(handle, 0, name, C.byref(size)):
            return None
        if not k.GetProcessTimes(handle, *[C.byref(t) for t in times]):
            return None
        return {'pid': pid, 'path': name.value, 'created': times[0].dwHighDateTime << 32 | times[0].dwLowDateTime}
    finally:
        k.CloseHandle(handle)


def terminate_identity(identity):
    if not identity or process_identity(identity['pid']) != identity:
        return
    from ctypes import wintypes as W
    k = C.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    k.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
    k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    k.CloseHandle.argtypes = [W.HANDLE]
    handle = k.OpenProcess(0x00100001, False, identity['pid'])
    if handle:
        try:
            if not k.TerminateProcess(handle, 0):
                raise OSError(C.get_last_error(), '无法停止工具拥有的进程')
            k.WaitForSingleObject(handle, 5000)
        finally:
            k.CloseHandle(handle)


def get_system_proxy():
    import winreg
    result = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
        for name in ('ProxyEnable', 'ProxyServer', 'ProxyOverride', 'AutoConfigURL'):
            try:
                value, kind = winreg.QueryValueEx(key, name)
                result[name] = [value, kind]
            except FileNotFoundError:
                result[name] = None
    return result


def set_system_proxy(values):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Internet Settings',
                        0, winreg.KEY_SET_VALUE) as key:
        for name, data in values.items():
            if data is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, name, 0, data[1], data[0])
    wininet = C.WinDLL('wininet', use_last_error=True)
    wininet.InternetSetOptionW(None, 39, None, 0)
    wininet.InternetSetOptionW(None, 37, None, 0)


def enable_system_proxy(port):
    import winreg
    set_system_proxy({'ProxyEnable': [1, winreg.REG_DWORD],
        'ProxyServer': [f'127.0.0.1:{port}', winreg.REG_SZ],
        'ProxyOverride': ['<local>;127.*;localhost;[::1]', winreg.REG_SZ], 'AutoConfigURL': None})
