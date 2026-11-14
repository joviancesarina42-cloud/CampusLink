import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from campuslink import deps
from campuslink.manager import Manager


def response(payload):
    stream = io.BytesIO(payload)
    stream.headers = {'Content-Length': str(len(payload))}
    return stream


class DownloadTests(unittest.TestCase):
    def test_connection_failure_retries_and_succeeds(self):
        with patch.object(deps, '_open', side_effect=[OSError('offline'), response(b'ok')]) as fetch, \
                patch.object(deps.time, 'sleep'):
            self.assertEqual(deps._download('https://example.test'), b'ok')
        self.assertEqual(fetch.call_count, 2)
        self.assertLessEqual(fetch.call_args.args[1], deps.REQUEST_TIMEOUT)

    def test_truncated_transfer_retries_from_scratch(self):
        short = response(b'part')
        short.headers['Content-Length'] = '100'
        with patch.object(deps, '_open', side_effect=[short, response(b'complete')]), \
                patch.object(deps.time, 'sleep'):
            self.assertEqual(deps._download('https://example.test'), b'complete')

    def test_permanent_http_error_does_not_spin(self):
        error = urllib.error.HTTPError('https://example.test', 404, 'missing', {}, None)
        with patch.object(deps, '_open', side_effect=error) as fetch:
            with self.assertRaises(urllib.error.HTTPError):
                deps._download('https://example.test')
        self.assertEqual(fetch.call_count, 1)

    def test_retry_deadline_is_shared_and_not_reset(self):
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(deps.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(deps.time, 'sleep', side_effect=sleep), \
                patch.object(deps, '_open', side_effect=OSError('offline')) as fetch:
            with self.assertRaises(TimeoutError):
                deps._download('https://example.test', deadline=2)
        self.assertEqual(clock[0], 2)
        self.assertGreater(fetch.call_count, 1)

    def test_failed_extraction_does_not_replace_existing_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'mihomo.exe'
            target.write_bytes(b'working')
            release = {'tag_name': 'v1', 'assets': [
                {'name': 'mihomo-windows-amd64-v1.zip', 'browser_download_url': 'url'}]}
            with patch.object(deps, '_latest', return_value=release), \
                    patch.object(deps, '_download', return_value=b'broken zip'):
                with self.assertRaises(zipfile.BadZipFile):
                    deps.install('mihomo', target)
            self.assertEqual(target.read_bytes(), b'working')

    def test_both_components_use_one_deadline(self):
        with patch.object(deps, 'install', return_value='component.exe') as install:
            deps.install_missing(kernel=True, warp=True)
        self.assertEqual(install.call_args_list[0].kwargs['deadline'],
                         install.call_args_list[1].kwargs['deadline'])


class BoundedWorkerTests(unittest.TestCase):
    def run_hung_worker(self, cancel, timeout):
        real_popen = subprocess.Popen
        processes = []
        def spawn(command, **kwargs):
            if str(command[0]).lower().endswith('taskkill.exe'):
                return real_popen(command, **kwargs)
            # Keep the actual supervision/termination path; replace only the network job.
            process = real_popen([sys.executable, '-c', 'import time; time.sleep(120)'], **kwargs)
            processes.append(process)
            return process
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as temp, patch.object(deps, 'DATA', Path(temp)), \
                patch.object(deps.subprocess, 'Popen', side_effect=spawn):
            try:
                deps.run_install_job(cancel, timeout=timeout)
            finally:
                self.assertTrue(processes)
                self.assertIsNotNone(processes[0].poll(), 'worker must be reaped before unlocking')
                self.assertLess(time.monotonic() - started, 5)

    def test_wall_clock_deadline_kills_unresponsive_worker(self):
        with self.assertRaisesRegex(TimeoutError, '已停止'):
            self.run_hung_worker(threading.Event(), timeout=0.2)

    def test_cancel_kills_unresponsive_worker_without_waiting_for_deadline(self):
        cancel = threading.Event()
        timer = threading.Timer(0.2, cancel.set)
        timer.start()
        try:
            with self.assertRaisesRegex(RuntimeError, '下载已取消'):
                self.run_hung_worker(cancel, timeout=60)
        finally:
            timer.cancel()

    def test_successful_worker_result_is_returned(self):
        process = Mock(returncode=0)
        process.poll.return_value = 0
        def spawn(command, **kwargs):
            Path(command[-1]).write_text(json.dumps({'result': {'downloaded': ['mihomo']}}))
            return process
        with tempfile.TemporaryDirectory() as temp, patch.object(deps, 'DATA', Path(temp)), \
                patch.object(deps.subprocess, 'Popen', side_effect=spawn):
            self.assertEqual(deps.run_install_job(threading.Event()), {'downloaded': ['mihomo']})


class DownloadOperationTests(unittest.TestCase):
    def test_stop_and_exit_cancel_download_then_acquire_operation(self):
        for action in ('deps-cancel', 'stop', 'recover', 'exit'):
            with self.subTest(action=action):
                manager = Manager.__new__(Manager)
                manager.state = {'operation': 'deps-install'}
                manager.download_cancel = threading.Event()
                manager.operation = threading.Lock()
                manager.operation.acquire()
                def download():
                    if manager.download_cancel.wait(2):
                        manager.operation.release()
                worker = threading.Thread(target=download)
                worker.start()
                try:
                    self.assertTrue(manager.acquire_operation(action))
                    self.assertTrue(manager.download_cancel.is_set())
                    manager.operation.release()
                finally:
                    worker.join(timeout=3)

    def test_other_operations_do_not_cancel_an_active_download(self):
        manager = Manager.__new__(Manager)
        manager.state = {'operation': 'deps-install'}
        manager.download_cancel = threading.Event()
        manager.operation = threading.Lock()
        with manager.operation:
            self.assertFalse(manager.acquire_operation('save'))
            self.assertFalse(manager.download_cancel.is_set())

    def test_exit_does_not_interrupt_network_restoration(self):
        manager = Manager.__new__(Manager)
        manager.state = {'operation': 'stop'}
        manager.download_cancel = threading.Event()
        manager.operation = threading.Lock()
        with manager.operation:
            self.assertFalse(manager.acquire_operation('exit'))
            self.assertFalse(manager.download_cancel.is_set())


if __name__ == '__main__':
    unittest.main()
