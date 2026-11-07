import subprocess
import unittest
from unittest.mock import Mock, patch

from campuslink.desktop import Desktop, EXIT_MESSAGE
from app import LocalHTTPServer


class DesktopLifecycleTests(unittest.TestCase):
    def test_backend_port_cannot_be_bound_by_a_second_instance(self):
        from http.server import BaseHTTPRequestHandler
        first = LocalHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
        try:
            with self.assertRaises(OSError):
                LocalHTTPServer(first.server_address, BaseHTTPRequestHandler)
        finally:
            first.server_close()

    def test_running_window_is_restored_without_second_process(self):
        host = Desktop(18686)
        host.process = Mock()
        host.process.poll.return_value = None
        with patch('campuslink.desktop.window_message', return_value=True) as send, \
                patch('campuslink.desktop.subprocess.Popen') as start:
            host.show()
            # The restoration dispatch is asynchronous; join it deterministically.
            import time
            for _ in range(40):
                if send.called:
                    break
                time.sleep(.01)
            self.assertTrue(send.called)
            start.assert_not_called()

    def test_finished_host_is_not_forced_to_close_again(self):
        host = Desktop(18686)
        host.process = Mock()
        host.process.poll.return_value = 0
        with patch('campuslink.desktop.window_message') as send:
            host.close_after_restore()
            send.assert_not_called()

    def test_successful_network_restore_allows_shell_shutdown(self):
        host = Desktop(18686)
        host.process = Mock()
        host.process.poll.return_value = None
        with patch('campuslink.desktop.window_message') as send:
            host.close_after_restore()
            send.assert_called_once_with(EXIT_MESSAGE)
            host.process.wait.assert_called_once_with(timeout=5)
            host.process.terminate.assert_not_called()

    def test_unresponsive_shell_only_terminated_after_restore_callback(self):
        host = Desktop(18686)
        host.process = Mock()
        host.process.poll.return_value = None
        host.process.wait.side_effect = [subprocess.TimeoutExpired('desktop',5), None]
        with patch('campuslink.desktop.window_message'):
            host.close_after_restore()
        host.process.terminate.assert_called_once()


if __name__ == '__main__':
    unittest.main()
