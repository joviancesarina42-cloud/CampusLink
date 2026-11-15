import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from campuslink import firewall
from campuslink.common import is_admin
from campuslink.config import DEFAULTS, Settings, validate_settings
import test_recovery


PROGRAM = r"D:\Bili O'Brien & [desktop]\哔哩哔哩.exe"


def rule(direction='Outbound', **changes):
    result = dict(Name=firewall.PREFIX + direction, Enabled='False', Action='Allow',
                  Profile='Private', Direction=direction, Program=PROGRAM, Protocol='UDP',
                  InterfaceAlias=['WLAN'], InterfaceType='Wireless',
                  LocalAddress=['Any'], RemoteAddress=['192.0.2.0/24'],
                  LocalPort=['1234'], RemotePort=['5678'])
    result.update(changes)
    return result


class FirewallPlanTests(unittest.TestCase):
    def test_default_enabled_and_existing_settings_get_new_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'settings.json').write_text('{"mode":"attach"}')
            with patch('campuslink.config.DATA', Path(temp)):
                settings = Settings()
            self.assertTrue(settings.value['bili_ipv4_udp_guard'])
            self.assertEqual(settings.value['bili_ipv4_udp_programs'], [])
            self.assertEqual(settings.value['mode'], 'attach')

    def test_existing_rules_are_snapshotted_without_mutation(self):
        info = {'rules': [rule(), rule('Inbound')], 'programs': []}
        before = copy.deepcopy(info)
        with patch('campuslink.firewall._command', return_value=info) as command:
            plan = firewall.prepare({})
        self.assertEqual(info, before)
        self.assertEqual(len(plan['entries']), 2)
        self.assertEqual(plan['entries'][0]['before'], before['rules'][0])
        self.assertEqual(command.call_count, 1)  # Preparing cannot change the firewall.

    def test_missing_direction_is_created_and_paths_are_deduplicated(self):
        with patch('campuslink.firewall._command', return_value={
                'rules': [rule()], 'programs': [PROGRAM, PROGRAM.lower()]}):
            plan = firewall.prepare({})
        self.assertEqual(len(plan['entries']), 2)
        new = plan['entries'][1]
        self.assertIsNone(new['before'])
        self.assertEqual(new['direction'], 'Inbound')
        self.assertTrue(new['name'].startswith(firewall.PREFIX))

    def test_explicit_paths_override_discovery_and_leave_other_rules_alone(self):
        path = r'C:\Other\player.exe'
        with patch('campuslink.firewall._command', return_value={
                'rules': [rule()], 'programs': [PROGRAM]}):
            plan = firewall.prepare({'bili_ipv4_udp_programs': [path]})
        self.assertEqual([e['program'] for e in plan['entries']], [path, path])
        self.assertTrue(all(e['before'] is None for e in plan['entries']))

    def test_unrelated_protocol_or_all_programs_is_never_adopted(self):
        with patch('campuslink.firewall._command', return_value={
                'rules': [rule(Protocol='TCP'), rule('Inbound', Program='Any')],
                'programs': [PROGRAM]}):
            plan = firewall.prepare({})
        self.assertEqual(len(plan['entries']), 2)
        self.assertTrue(all(e['before'] is None for e in plan['entries']))

    def test_no_target_has_actionable_error(self):
        with patch('campuslink.firewall._command', return_value={'rules': [], 'programs': []}):
            with self.assertRaisesRegex(RuntimeError, '完整 exe 路径'):
                firewall.prepare({})

    def test_invalid_paths_or_switch_are_rejected_before_saving(self):
        for value in (['bilibili.exe'], [r'C:\Bili\*.exe'], ['C:player.exe'],
                      [r'C:\Bili\player.exe\nother'], 'C:\\bili.exe', [None]):
            with self.subTest(value=value):
                s = copy.deepcopy(DEFAULTS)
                s['bili_ipv4_udp_programs'] = value
                with self.assertRaisesRegex(ValueError, '完整 exe 路径'):
                    validate_settings(s)
        s = copy.deepcopy(DEFAULTS)
        s['bili_ipv4_udp_guard'] = 'false'
        with self.assertRaisesRegex(ValueError, '布尔值'):
            validate_settings(s)

    def test_special_characters_in_paths_are_transferred_as_data(self):
        with patch('campuslink.firewall.powershell', return_value={'ok': True}) as ps:
            firewall.apply({'entries': [{'program': PROGRAM}]})
        script = ps.call_args.args[0]
        self.assertNotIn(PROGRAM, script)
        self.assertIn('FromBase64String', script)


class FirewallLifecycleTests(unittest.TestCase):
    def context(self, temp):
        manager, api = test_recovery.RecoveryTests().manager()
        manager.settings.value['bili_ipv4_udp_guard'] = True
        plan = {'entries': [{'name': firewall.PREFIX + 'test', 'before': rule()}]}
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target, options in (
            ('DATA', {'new': Path(temp)}), ('is_admin', {'return_value': True}),
            ('validate_kernel', {}),
            ('discover', {'return_value': {'kernel': 'mihomo.exe', 'runtime': '', 'account': 'acct.json'}}),
            ('Mihomo', {'return_value': api}), ('prepare_firewall', {'return_value': plan}),
        ):
            stack.enter_context(patch('campuslink.manager.' + target, **options))
        apply = stack.enter_context(patch('campuslink.manager.apply_firewall'))
        restore = stack.enter_context(patch('campuslink.manager.restore_firewall'))
        return manager, api, plan, apply, restore

    def test_journal_precedes_first_change_and_guard_precedes_kernel_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            manager, api, plan, apply, restore = self.context(temp)
            def applying(_):
                recorded = json.loads((Path(temp) / 'recovery.json').read_text('utf-8'))
                self.assertEqual(recorded['bili_firewall'], plan)
                api.reload.assert_not_called()
            apply.side_effect = applying
            manager.start()
            self.assertEqual(manager.state['bili_firewall'], '已启用')
            manager.rollback()
            restore.assert_called_once_with(plan)
            self.assertFalse((Path(temp) / 'recovery.json').exists())

    def test_partial_firewall_apply_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            manager, api, plan, apply, restore = self.context(temp)
            apply.side_effect = RuntimeError('second rule failed')
            with self.assertRaisesRegex(RuntimeError, 'second rule failed'):
                manager.start()
            manager.warp.start.assert_not_called()
            restore.assert_called_once_with(plan)
            self.assertEqual(manager.state['phase'], 'idle')
            self.assertIsNone(manager.journal)

    def test_kernel_start_failure_restores_firewall_too(self):
        with tempfile.TemporaryDirectory() as temp:
            manager, api, plan, apply, restore = self.context(temp)
            manager.warp.start.side_effect = RuntimeError('warp failed')
            with self.assertRaisesRegex(RuntimeError, 'warp failed'):
                manager.start()
            restore.assert_called_once_with(plan)
            self.assertEqual(manager.state['phase'], 'idle')

    def test_firewall_restore_failure_retains_journal_for_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            manager, api, plan, apply, restore = self.context(temp)
            manager.start()
            restore.side_effect = RuntimeError('firewall unavailable')
            with self.assertRaisesRegex(RuntimeError, 'B 站防火墙恢复失败'):
                manager.rollback()
            self.assertEqual(manager.state['phase'], 'recovery')
            self.assertEqual(manager.state['bili_firewall'], '恢复未完成')
            self.assertTrue((Path(temp) / 'recovery.json').exists())
            # Simulate restarting the app and reading its durable recovery record.
            manager.journal = json.loads((Path(temp) / 'recovery.json').read_text('utf-8'))
            restore.side_effect = None
            manager.rollback()
            self.assertEqual(manager.state['phase'], 'idle')
            self.assertIsNone(manager.journal)

    def test_other_restore_failure_does_not_skip_firewall_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            manager, api, plan, apply, restore = self.context(temp)
            manager.start()
            manager.warp.stop.side_effect = RuntimeError('warp busy')
            with self.assertRaisesRegex(RuntimeError, 'warp busy'):
                manager.rollback()
            restore.assert_called_once_with(plan)

    def test_disabled_guard_does_not_read_or_modify_firewall(self):
        with tempfile.TemporaryDirectory() as temp:
            manager, api, plan, apply, restore = self.context(temp)
            manager.settings.value['bili_ipv4_udp_guard'] = False
            with patch('campuslink.manager.prepare_firewall') as prepare:
                manager.start()
            self.assertNotIn('bili_firewall', manager.journal)
            manager.rollback()
            prepare.assert_not_called()
            apply.assert_not_called()
            restore.assert_not_called()

    def test_non_admin_fails_before_any_change(self):
        manager, api = test_recovery.RecoveryTests().manager()
        manager.settings.value['bili_ipv4_udp_guard'] = True
        with patch('campuslink.manager.is_admin', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '防泄露需要管理员'):
                manager.start()
        api.reload.assert_not_called()
        self.assertIsNone(manager.journal)


@unittest.skipUnless(os.name == 'nt' and is_admin() and
                     os.environ.get('CAMPUSLINK_FIREWALL_TESTS') == '1',
                     'Opt-in elevated Windows firewall integration')
class LiveFirewallTests(unittest.TestCase):
    def test_real_create_apply_restore_and_idempotent_cleanup(self):
        # Rules target an inert temporary executable, never a user's applications.
        with tempfile.TemporaryDirectory(prefix="CampusLink O'Brien & [guard] ") as temp:
            program = Path(temp) / '测试.exe'
            program.touch()
            name = firewall.PREFIX + 'integration-' + uuid.uuid4().hex
            original = rule(Name=name, Program=str(program), InterfaceAlias=['Any'],
                            InterfaceType='Any', LocalPort=['1234', '1235'])
            setup = {'entries': [{'name': name, 'program': str(program),
                                  'direction': 'Outbound', 'before': None}]}
            plan = None
            try:
                firewall.apply(setup)
                # Snapshot Windows' canonical representation, not our input notation.
                firewall._command('''
Set-NetFirewallRule -PolicyStore PersistentStore -Name $data.Name -Enabled False -Action Allow `
    -Profile Private -InterfaceAlias Any -InterfaceType Any -LocalAddress Any `
    -RemoteAddress $data.RemoteAddress -LocalPort $data.LocalPort -RemotePort $data.RemotePort `
    -Protocol UDP -ErrorAction Stop | Out-Null
@{ok=$true} | ConvertTo-Json -Compress
''', original)
                plan = firewall.prepare({'bili_ipv4_udp_programs': [str(program)]})
                self.assertEqual(len(plan['entries']), 2)
                saved = next(e['before'] for e in plan['entries'] if e['before'])
                self.assertEqual(saved['Enabled'], 'False')
                self.assertEqual(saved['LocalPort'], ['1234', '1235'])
                firewall.apply(plan)
                firewall.restore(plan)
                firewall.restore(plan)
                restored = firewall.prepare({'bili_ipv4_udp_programs': [str(program)]})
                self.assertEqual(next(e['before'] for e in restored['entries'] if e['before']), saved)
                self.assertEqual(sum(e['before'] is None for e in restored['entries']), 1)
            finally:
                if plan:
                    firewall.restore(plan)
                firewall.restore(setup)


if __name__ == '__main__':
    unittest.main()
