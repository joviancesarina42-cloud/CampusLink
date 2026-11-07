import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from campuslink.capture import physical_adapters
from campuslink.common import powershell
from campuslink.manager import Manager


ADAPTER = {'Name': 'WLAN', 'InterfaceGuid': '{test-wlan}', 'MacAddress': '00-11-22-33-44-55'}


class AdapterEnumerationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell regression')
    def test_powershell_zero_one_and_two_adapters_always_return_valid_arrays(self):
        # Override only the cmdlet inside the subprocess. Exercise real PowerShell
        # JSON semantics without disabling or changing any physical interface.
        for count in (0, 1, 2):
            with self.subTest(count=count):
                body = ';'.join("[pscustomobject]@{Name='WLAN%d';Status='Up';InterfaceGuid='{test-%d}';MacAddress='00-11-22-33-44-55'}" % (i, i) for i in range(count))
                prefix = 'function Get-NetAdapter { param([switch]$Physical) ' + body + ' }; '
                with patch('campuslink.capture.powershell', side_effect=lambda script: powershell(prefix + script)):
                    adapters = physical_adapters()
                self.assertIsInstance(adapters, list)
                self.assertEqual(len(adapters), count)

    def test_empty_or_malformed_command_output_has_context(self):
        for output in (b'', b' ', b'not-json'):
            with self.subTest(output=output), patch('campuslink.common.run', return_value=SimpleNamespace(returncode=0, stdout=output)):
                with self.assertRaisesRegex(RuntimeError, '读取 Windows 状态失败'):
                    powershell('ignored')


class CaptureReconnectTests(unittest.TestCase):
    def manager(self, running=False, requested=True):
        manager = Manager.__new__(Manager)
        manager.capture_lock = threading.RLock()
        manager.settings = SimpleNamespace(value={'capture_interfaces': []})
        manager.state = {'phase': 'idle', 'capture_requested': requested, 'capture_error': 'old-error'}
        manager.capture = SimpleNamespace(status={'running': running, 'error': '', 'interfaces': [dict(ADAPTER)]})
        def start(_):
            manager.capture.status.update(running=True, error='', interfaces=[dict(ADAPTER)])
        def stop():
            manager.capture.status['running'] = False
        manager.capture.start = Mock(side_effect=start)
        manager.capture.stop = Mock(side_effect=stop)
        manager.store = SimpleNamespace(event=Mock())
        return manager

    def test_initial_failure_is_retried_and_error_clears(self):
        manager = self.manager()
        start = manager.capture.start.side_effect
        manager.capture.start.side_effect = [RuntimeError('没有已连接网卡'), None]
        manager._refresh_capture()
        self.assertTrue(manager.state['capture_requested'])
        self.assertIn('自动重试', manager.state['capture_error'])
        manager.capture.start.side_effect = start
        manager._refresh_capture()
        self.assertTrue(manager.capture.status['running'])
        self.assertEqual(manager.state['capture_error'], '')

    def test_disconnect_then_reconnect_restarts_capture(self):
        manager = self.manager(running=True)
        with patch('campuslink.manager.physical_adapters', return_value=[]):
            manager._refresh_capture()
        self.assertFalse(manager.capture.status['running'])
        self.assertTrue(manager.state['capture_requested'])
        self.assertIn('网络恢复', manager.state['capture_error'])
        manager._refresh_capture()
        self.assertTrue(manager.capture.status['running'])
        self.assertEqual(manager.state['capture_error'], '')

    def test_enumeration_recovers_without_restarting_healthy_handle(self):
        manager = self.manager(running=True)
        with patch('campuslink.manager.physical_adapters', side_effect=[RuntimeError('temporary WMI failure'), [ADAPTER]]):
            manager._refresh_capture()
            self.assertIn('temporary WMI failure', manager.state['capture_error'])
            manager._refresh_capture()
        self.assertEqual(manager.state['capture_error'], '')
        manager.capture.start.assert_not_called()
        manager.capture.stop.assert_not_called()

    def test_rebind_failure_is_retried_after_capture_stops(self):
        manager = self.manager(running=True)
        start = manager.capture.start.side_effect
        def failed(_):
            manager.capture.status['running'] = False
            raise RuntimeError('adapter changed during open')
        manager.capture.start.side_effect = failed
        with patch('campuslink.manager.physical_adapters', return_value=[dict(ADAPTER, InterfaceGuid='{new-wlan}')]):
            manager._refresh_capture()
        self.assertIn('自动重试', manager.state['capture_error'])
        manager.capture.start.side_effect = start
        manager._refresh_capture()
        self.assertEqual(manager.state['capture_error'], '')
        self.assertTrue(manager.capture.status['running'])

    def test_manual_pause_cancels_pending_retry_and_clears_stale_warning(self):
        manager = self.manager()
        manager.capture.status['error'] = 'old-driver-error'
        manager.capture_stop()
        with patch('campuslink.manager.physical_adapters') as enumerate_adapters:
            manager._refresh_capture()
        self.assertFalse(manager.state['capture_requested'])
        self.assertEqual(manager.state['capture_error'], '')
        self.assertEqual(manager.capture.status['error'], '')
        manager.capture.start.assert_not_called()
        enumerate_adapters.assert_not_called()

    def test_failed_manual_start_keeps_retry_enabled(self):
        manager = self.manager(requested=False)
        manager.capture.start.side_effect = RuntimeError('no adapter')
        with self.assertRaisesRegex(RuntimeError, '物理流量监控暂不可用'):
            manager.capture_start()
        self.assertTrue(manager.state['capture_requested'])

    def test_selected_adapter_disappears_without_using_other_interfaces(self):
        manager = self.manager(running=True)
        manager.settings.value['capture_interfaces'] = ['WLAN']
        with patch('campuslink.manager.physical_adapters', return_value=[dict(ADAPTER, Name='Ethernet')]):
            manager._refresh_capture()
        self.assertFalse(manager.capture.status['running'])
        manager.capture.start.assert_not_called()

    def test_recovery_or_stopping_does_not_restart_capture(self):
        for phase in ('recovery', 'stopping'):
            with self.subTest(phase=phase):
                manager = self.manager()
                manager.state['phase'] = phase
                manager._refresh_capture()
                manager.capture.start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
