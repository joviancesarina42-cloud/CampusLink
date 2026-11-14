"""Exercise the release repair entry with real Windows download-marker streams."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'packaging' / 'repair-start.ps1'


@unittest.skipUnless(os.name == 'nt', 'Windows download markers')
class ReleaseLauncherTests(unittest.TestCase):
    def test_cmd_passes_special_path_without_interpreting_it_as_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "校园网 O'Brien & [release]!"
            root.mkdir()
            launcher = root / 'start.cmd'
            shutil.copyfile(SCRIPT.with_name('启动 CampusLink.cmd'), launcher)
            (root / 'repair-start.ps1').write_text(
                "[IO.File]::WriteAllText((Join-Path $env:CAMPUSLINK_HOME 'called.txt'), 'ok')",
                encoding='utf-8-sig')
            env = dict(os.environ, CL_CMD=str(launcher))
            result = subprocess.run('cmd.exe /d /c call "%CL_CMD%"',
                                    env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'called.txt').read_text(), 'ok')

    def run_script(self, root, prelude='', repair=True):
        env = dict(os.environ, CL_TEST_ROOT=str(root), CL_TEST_SCRIPT=str(SCRIPT))
        command = prelude + "& $env:CL_TEST_SCRIPT -PackageDirectory $env:CL_TEST_ROOT"
        if repair:
            command += ' -RepairOnly'
        return subprocess.run(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
             '[Console]::OutputEncoding = [Text.Encoding]::UTF8; ' + command],
            env=env, capture_output=True, encoding='utf-8', timeout=30)

    def test_removes_nested_markers_preserves_data_and_handles_special_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "校园网 O'Brien & [release]!"
            paths = ['CampusLink.exe', '_internal/a.pyd', '_desktop/a.dll',
                     'vendor/mihomo.exe', 'data/account.json']
            for name in paths:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'original')
                Path(str(path) + ':Zone.Identifier').write_text('[ZoneTransfer]\nZoneId=3\n')
            for _ in range(2):  # Repair must also work when there are no markers left.
                result = self.run_script(root)
                self.assertEqual(result.returncode, 0, result.stderr)
            for name in paths:
                path = root / name
                self.assertEqual(path.read_bytes(), b'original')
                self.assertEqual(Path(str(path) + ':Zone.Identifier').exists(),
                                 name.startswith('data/'))

    def test_missing_executable_reports_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_script(Path(temp))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('CampusLink.exe', result.stderr)

    def test_unblock_failure_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'CampusLink.exe').touch()
            result = self.run_script(Path(temp), "function Unblock-File { throw 'marker-access-denied' }; ")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('marker-access-denied', result.stderr)

    def test_cancelled_uac_reports_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'CampusLink.exe').touch()
            result = self.run_script(Path(temp),
                'function Start-Process { throw (New-Object ComponentModel.Win32Exception 1223) }; ',
                repair=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('已取消管理员权限请求', result.stderr)

    def test_start_requests_elevation_with_package_working_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'CampusLink.exe').touch()
            result = self.run_script(Path(temp), """
function Start-Process {
    param($FilePath, $WorkingDirectory, $Verb, $ErrorAction)
    if ($FilePath -ne (Join-Path $env:CL_TEST_ROOT 'CampusLink.exe') -or
        $WorkingDirectory -ne $env:CL_TEST_ROOT -or $Verb -ne 'RunAs') {
        throw 'wrong-launch-arguments'
    }
};
""", repair=False)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
