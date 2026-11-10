import copy
import socket
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from campuslink import config
from campuslink.entries import pin_entry, select_entry, v6_entry
from campuslink.nodes import ANCHOR, AI_GROUP, DEFAULT_GROUP
from test_core import fixture


PIN = '2606:4700:4700::1111'


def answers(*addresses):
    return [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', (ip, 0, 0, 0))
            for ip in addresses]


class EntryTests(unittest.TestCase):
    def test_only_public_ipv6_answers_are_accepted(self):
        with patch('campuslink.entries.socket.getaddrinfo', return_value=answers(
                '::1', 'fe80::1', 'fd00::1', '198.18.0.1', PIN)) as lookup:
            self.assertEqual(v6_entry('entry.example'), PIN)
        self.assertEqual(lookup.call_args.args[2:], (socket.AF_INET6, socket.SOCK_STREAM))

    def test_ipv4_literal_never_becomes_a_first_hop(self):
        with patch('campuslink.entries.socket.getaddrinfo') as lookup:
            for host in ('1.2.3.4', '::ffff:8.8.8.8', 'fe80::1', ''):
                self.assertEqual(v6_entry(host), '')
        lookup.assert_not_called()

    def test_previous_address_is_kept_only_while_still_in_dns(self):
        with patch('campuslink.entries.socket.getaddrinfo', return_value=answers('2407:cdc0::2', PIN)):
            self.assertEqual(v6_entry('entry.example', PIN), PIN)
        with patch('campuslink.entries.socket.getaddrinfo', return_value=answers('2407:cdc0::2')):
            self.assertEqual(v6_entry('entry.example', PIN), '2407:cdc0::2')

    def test_unsupported_and_metadata_nodes_are_never_first_hops(self):
        source = {'proxies': [
            {'name': 'Web', 'type': 'http', 'server': PIN},
            {'name': '剩余流量: 99GB', 'type': 'trojan', 'server': PIN},
            {'name': 'Real', 'type': 'trojan', 'server': PIN}]}
        self.assertEqual(select_entry(source, {})[0]['name'], 'Real')

    def test_shared_domain_is_resolved_once_per_import(self):
        source = {'proxies': [{'name': str(i), 'type': 'trojan', 'server': 'entry.example'}
                              for i in range(5)]}
        with patch('campuslink.entries.socket.getaddrinfo', side_effect=socket.gaierror) as lookup:
            self.assertEqual(select_entry(source, {}), (None, ''))
        self.assertEqual(lookup.call_count, 1)

    def test_implicit_tls_name_is_retained_for_domain_entries(self):
        for protocol in ('trojan', 'hysteria', 'hysteria2', 'tuic', 'vmess', 'vless'):
            with self.subTest(protocol=protocol):
                proxy = {'type': protocol, 'server': 'entry.example', 'tls': True}
                pin_entry(proxy, PIN)
                key = 'servername' if protocol in ('vmess', 'vless') else 'sni'
                self.assertEqual(proxy[key], 'entry.example')
                self.assertEqual(proxy['server'], PIN)

    def test_explicit_tls_name_and_disabled_sni_are_preserved(self):
        proxy = {'type': 'hysteria2', 'server': 'entry.example', 'sni': 'tls.example'}
        pin_entry(proxy, PIN)
        self.assertEqual(proxy['sni'], 'tls.example')
        proxy = {'type': 'tuic', 'server': 'entry.example', 'disable-sni': True}
        pin_entry(proxy, PIN)
        self.assertNotIn('sni', proxy)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.data = self.root / 'app/data'
        self.vendor = self.root / 'app/vendor'
        self.verge = self.root / 'verge'
        for folder in (self.data, self.vendor, self.verge):
            folder.mkdir(parents=True)
        for name in ('mihomo.exe', 'warpscout.exe'):
            (self.vendor / name).write_bytes(b'test binary')
        (self.data / 'warp-account.json').write_text('{}')
        self.source_path = self.verge / 'clash-verge.yaml'
        for name, path in (('DATA', self.data), ('VENDOR', self.vendor), ('ROOT', self.data.parent)):
            self.stack.enter_context(patch.object(config, name, path))
        discovery = dict(runtime=str(self.source_path), source=str(self.source_path),
                         verge_dir=str(self.verge), kernel=str(self.vendor / 'mihomo.exe'),
                         warp=str(self.vendor / 'warpscout.exe'),
                         account=str(self.data / 'warp-account.json'))
        self.stack.enter_context(patch.object(config, 'discover', return_value=discovery))
        self.lookup = self.stack.enter_context(patch('campuslink.entries.socket.getaddrinfo',
                                                   return_value=answers(PIN)))
        values, self.source = fixture()
        values.update(airport_source_name='JP-test [v6]', airport_source_host=PIN,
                      airport_ipv6=PIN, default_node='JP-test [v6]', ai_node='Other')
        self.settings = config.Settings()
        self.settings.save(values)
        self.old_source = config.render({'proxies': [{'name': 'old'}]})
        (self.data / 'source.yaml').write_text(self.old_source, encoding='utf-8')

    def write_source(self):
        self.source_path.write_text(config.render(self.source), encoding='utf-8')

    def test_domain_import_remaps_legacy_suffix_and_keeps_ai_selection(self):
        decoy = copy.deepcopy(self.source['proxies'][0])
        decoy.update(name='Earlier entry', server='earlier.example')
        self.source['proxies'].insert(0, decoy)
        self.write_source()
        before = self.source_path.read_bytes()
        config.import_local(self.settings)
        values = self.settings.value
        self.assertEqual(values['airport_source_name'], 'JP-test')
        self.assertEqual(values['airport_source_host'], 'entry.example')
        self.assertEqual(values['airport_ipv6'], PIN)
        self.assertEqual(values['default_node'], 'JP-test')
        self.assertEqual(values['ai_node'], 'Other')
        self.assertEqual(self.source_path.read_bytes(), before)
        self.assertEqual((self.data / 'source.yaml').read_text('utf-8'),
                         self.source_path.read_text('utf-8'))
        built = config.build_config(self.source, values)
        anchor = next(p for p in built['proxies'] if p['name'] == ANCHOR)
        self.assertEqual(anchor['server'], PIN)
        self.assertEqual(anchor['sni'], 'tls.example')
        groups = {g['name']: g['proxies'][0] for g in built['proxy-groups']}
        self.assertEqual(groups, {DEFAULT_GROUP: ANCHOR, AI_GROUP: 'CL-节点/Other'})

    def test_added_suffix_also_migrates_a_selected_node(self):
        self.source['proxies'][1]['name'] = 'Other [v6]'
        self.write_source()
        config.import_local(self.settings)
        self.assertEqual(self.settings.value['ai_node'], 'Other [v6]')

    def test_dns_failure_keeps_existing_source_and_settings(self):
        self.write_source()
        previous = copy.deepcopy(self.settings.value)
        before = (self.data / 'settings.json').read_bytes()
        self.lookup.side_effect = socket.gaierror('no AAAA record')
        with self.assertRaisesRegex(RuntimeError, 'AAAA'):
            config.import_local(self.settings)
        self.assertEqual(self.settings.value, previous)
        self.assertEqual((self.data / 'settings.json').read_bytes(), before)
        self.assertEqual((self.data / 'source.yaml').read_text('utf-8'), self.old_source)

    def test_unrelated_deleted_selection_is_not_silently_replaced(self):
        self.source['proxies'].pop()
        self.write_source()
        before = copy.deepcopy(self.settings.value)
        with self.assertRaisesRegex(ValueError, '所选节点已不存在'):
            config.import_local(self.settings)
        self.assertEqual(self.settings.value, before)
        self.assertEqual((self.data / 'source.yaml').read_text('utf-8'), self.old_source)

    def test_ambiguous_suffix_mapping_is_rejected_before_import(self):
        second = copy.deepcopy(self.source['proxies'][0])
        self.source['proxies'].append(second)
        self.write_source()
        with self.assertRaisesRegex(ValueError, '未唯一命中'):
            config.import_local(self.settings)
        self.assertEqual((self.data / 'source.yaml').read_text('utf-8'), self.old_source)


if __name__ == '__main__':
    unittest.main()
