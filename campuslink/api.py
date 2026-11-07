import json
import urllib.error
import urllib.request


class Mihomo:
    def __init__(self, base, secret=''):
        self.base = base.rstrip('/')
        self.secret = secret or ''
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, method='GET', data=None, timeout=6):
        headers = {'Content-Type': 'application/json'}
        if self.secret:
            headers['Authorization'] = 'Bearer ' + self.secret
        req = urllib.request.Request(self.base + path, method=method, headers=headers,
                                     data=None if data is None else json.dumps(data).encode())
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f'内核接口 {path.split("?")[0]} 返回 HTTP {e.code}') from e

    def reload(self, text):
        return self.request('/configs?force=true', 'PUT', {'payload': text}, timeout=35)
