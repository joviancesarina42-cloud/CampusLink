"""Exercise the same local HTTP stop/exit requests used by the UI and tray."""
import json
import os
import threading
import time
import unittest
import urllib.request
from unittest.mock import Mock, patch

import app
from campuslink.manager import Manager


@unittest.skipUnless(os.name == 'nt', 'Windows backend')
class DownloadAPITests(unittest.TestCase):
    def setUp(self):
        self.manager = Mock()
        self.manager.state = {'operation': ''}
        self.manager.operation = threading.Lock()
        self.manager.download_cancel = threading.Event()
        self.manager.acquire_operation = lambda name: Manager.acquire_operation(self.manager, name)
        self.manager.snapshot = lambda *args: {'state': dict(self.manager.state)}
        self.started = threading.Event()
        self.install_calls = 0
        self.timeout = 10

        def download():
            self.install_calls += 1
            self.started.set()
            if self.manager.download_cancel.wait(self.timeout):
                raise RuntimeError('下载已取消')
            raise TimeoutError('下载超时')
        self.manager.install_deps = download
        self.manager.rollback.return_value = {'message': '已停止'}
        self.bound = threading.Event()
        real_server = app.LocalHTTPServer
        owner = self

        class Server(real_server):
            def __init__(self, address, handler):
                super().__init__(('127.0.0.1', 0), handler)
                owner.server = self
                owner.bound.set()
        self.patches = [patch.object(app, 'Manager', return_value=self.manager),
                        patch.object(app, 'LocalHTTPServer', Server),
                        patch.object(app, 'write_json')]
        for item in self.patches:
            item.start()
        self.thread = threading.Thread(target=app.serve,
            kwargs={'port': 18686, 'headless': True, 'auto_import': False, 'auto_capture': False},
            daemon=True)
        self.thread.start()
        self.assertTrue(self.bound.wait(3))
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        # The server keeps the configured Host check; only its listening port is dynamic.
        self.token = self.request('token')['token']

    def tearDown(self):
        self.manager.download_cancel.set()
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        for item in reversed(self.patches):
            item.stop()

    def request(self, name, post=False):
        request = urllib.request.Request(self.base + '/api/' + name,
            data=b'{}' if post else None,
            headers={'Host': '127.0.0.1:18686', 'X-CampusLink': getattr(self, 'token', '')})
        with self.opener.open(request, timeout=3) as response:
            return json.load(response)

    def wait_finished(self, identifier):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            state = self.request('state')['state']
            if state.get('job', {}).get('id') == identifier and not state['job']['running']:
                return state['job']
            time.sleep(0.01)
        self.fail('operation did not finish')

    def test_timeout_releases_operation_and_next_download_is_accepted(self):
        self.timeout = 0.05
        for _ in range(2):
            job = self.request('deps-install', post=True)['job']
            self.assertIn('下载超时', self.wait_finished(job)['error'])
        self.assertEqual(self.install_calls, 2)

    def test_stop_cancels_download_and_completes_without_conflict(self):
        self.request('deps-install', post=True)
        self.assertTrue(self.started.wait(1))
        job = self.request('stop', post=True)['job']
        self.assertNotIn('error', self.wait_finished(job))
        self.manager.rollback.assert_called_once()
        # It must also be possible to start a fresh job after cancellation.
        self.started.clear()
        self.request('deps-install', post=True)
        self.assertTrue(self.started.wait(1))
        self.assertFalse(self.manager.download_cancel.is_set())
        self.request('stop', post=True)

    def test_tray_exit_cancels_download_then_shuts_down_backend(self):
        self.request('deps-install', post=True)
        self.assertTrue(self.started.wait(1))
        self.request('exit', post=True)
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive(), 'tray exit must not wait for network timeout')
        self.manager.close.assert_called_once()

    def test_cancel_download_leaves_network_and_capture_running(self):
        self.request('deps-install', post=True)
        self.assertTrue(self.started.wait(1))
        job = self.request('deps-cancel', post=True)['job']
        self.assertEqual(self.wait_finished(job)['result']['message'], '下载已停止，可重新下载')
        self.manager.rollback.assert_not_called()
        self.manager.close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
