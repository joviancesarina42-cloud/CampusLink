import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from campuslink.config import build_config, render
from campuslink.manager import Manager
from campuslink.system import terminate_identity
from test_core import fixture


class RecoveryTests(unittest.TestCase):
    def manager(self):
        s,c=fixture();s['mode']='attach'
        manager=Manager.__new__(Manager)
        manager.settings=SimpleNamespace(value=s)
        manager.state={'phase':'idle','mode':'attach','error':''}
        manager.journal=None;manager.journal_lock=threading.RLock()
        manager.operation=threading.Lock()
        manager.capture_lock=threading.RLock()
        manager.capture=SimpleNamespace(stop=Mock(),start=Mock(),status={'running':False,'error':''})
        manager.state['capture_requested']=True
        manager.source=lambda:copy.deepcopy(c)
        api=SimpleNamespace(base='http://127.0.0.1:9097',secret='',reload=Mock(),request=Mock(return_value={}))
        manager.client=lambda **_:api
        manager._runtime_backup=lambda _:(render(c),{'test':'selected'},{})
        manager.warp=SimpleNamespace(start=Mock(),stop=Mock())
        manager.store=SimpleNamespace(event=Mock())
        manager.active_config=None;manager.active_text=None
        return manager,api

    def journal(self, manager, temp, mode='standalone', text=None):
        path=Path(temp)/'before.yaml'
        if text is not None:path.write_text(text,encoding='utf-8')
        manager.journal={'mode':mode,'backup':temp,'runtime':str(path),
            'previous_api':'http://127.0.0.1:9097','previous_tun':{'enable':False},
            'proxy':{'ProxyServer':['127.0.0.1:7897',1]},'owned':{}}
        (Path(temp)/'recovery.json').write_text('durable-record',encoding='utf-8')

    def test_standalone_never_reloads_verge_even_with_contaminated_legacy_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            manager,api=self.manager()
            s,c=fixture();bad=build_config(c,s,True);bad['mixed-port']=0
            self.journal(manager,temp,text=render(bad))
            manager.journal['owned']={'core':{'pid':123}}
            with patch('campuslink.manager.DATA',Path(temp)),patch('campuslink.manager.Mihomo',return_value=api), \
                    patch('campuslink.manager.set_system_proxy') as proxy,patch('campuslink.manager.terminate_identity') as kill:
                manager.rollback()
            api.reload.assert_not_called()
            self.assertEqual(api.request.call_args_list[0].args,('/configs','PATCH',{'tun':{'enable':False}}))
            proxy.assert_called_once();kill.assert_called_once_with({'pid':123})
            manager.capture.stop.assert_called_once()
            self.assertFalse(manager.state['capture_requested'])
            self.assertFalse((Path(temp)/'recovery.json').exists())

    def test_attach_rejects_campuslink_backup_before_any_put(self):
        with tempfile.TemporaryDirectory() as temp:
            manager,api=self.manager();s,c=fixture()
            self.journal(manager,temp,'attach',render(build_config(c,s,True)))
            with patch('campuslink.manager.DATA',Path(temp)),patch('campuslink.manager.Mihomo',return_value=api), \
                    patch('campuslink.manager.set_system_proxy'):
                with self.assertRaisesRegex(RuntimeError,'自身配置'):manager.rollback()
            api.reload.assert_not_called()
            api.request.assert_not_called()
            self.assertEqual(manager.state['phase'],'recovery')
            self.assertTrue((Path(temp)/'recovery.json').exists())

    def test_attach_rejects_candidate_path_and_modified_backup(self):
        for bad_path in (False,True):
            with self.subTest(bad_path=bad_path),tempfile.TemporaryDirectory() as temp:
                manager,api=self.manager();self.journal(manager,temp,'attach',render(manager.source()))
                manager.journal['runtime_sha256']='wrong-digest'
                if bad_path:manager.journal['runtime']=str(Path(temp)/'candidate.yaml')
                with patch('campuslink.manager.DATA',Path(temp)),patch('campuslink.manager.Mihomo',return_value=api), \
                        patch('campuslink.manager.set_system_proxy'):
                    with self.assertRaises(RuntimeError):manager.rollback()
                api.reload.assert_not_called()
                self.assertTrue((Path(temp)/'recovery.json').exists())

    def test_attach_port_snapshot_mismatch_is_rejected_before_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            manager,api=self.manager();c=manager.source();c['mixed-port']=0
            self.journal(manager,temp,'attach',render(c))
            manager.journal['previous_general']={'mixed-port':7897}
            with patch('campuslink.manager.DATA',Path(temp)),patch('campuslink.manager.Mihomo',return_value=api), \
                    patch('campuslink.manager.set_system_proxy'):
                with self.assertRaisesRegex(RuntimeError,'mixed-port'):manager.rollback()
            api.reload.assert_not_called()

    def test_wrong_restored_port_or_tun_keeps_recovery_record(self):
        expected={'mixed-port':7897,'tun':{'enable':False}}
        for actual in ({'mixed-port':0,'tun':{'enable':False}}, {'mixed-port':7897,'tun':{'enable':True}}):
            with self.subTest(actual=actual),tempfile.TemporaryDirectory() as temp:
                manager,api=self.manager();self.journal(manager,temp)
                manager.journal['previous_general']=expected
                api.request.return_value=actual
                with patch('campuslink.manager.DATA',Path(temp)),patch('campuslink.manager.Mihomo',return_value=api), \
                        patch('campuslink.manager.set_system_proxy'):
                    with self.assertRaisesRegex(RuntimeError,'恢复校验失败'):manager.rollback()
                api.reload.assert_not_called()
                self.assertTrue((Path(temp)/'recovery.json').exists())

    def test_standalone_start_stop_preserves_verge_ports_without_full_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            manager,api=self.manager()
            manager.settings.value.update(mode='standalone',tun=False)
            general={'mixed-port':7897,'tun':{'enable':True}}
            api.request.return_value=general
            core=Mock(pid=123);core.poll.return_value=None
            with patch('campuslink.manager.DATA',Path(temp)),patch('campuslink.manager.validate_kernel'), \
                    patch('campuslink.manager.discover',return_value={'kernel':'mihomo.exe','runtime':''}), \
                    patch('campuslink.manager.Mihomo',return_value=api),patch('campuslink.manager.socket.socket'), \
                    patch('campuslink.manager.start_logged',return_value=core), \
                    patch('campuslink.manager.process_identity',return_value={'pid':123}), \
                    patch('campuslink.manager.get_system_proxy',return_value={}), \
                    patch('campuslink.manager.enable_system_proxy'),patch('campuslink.manager.set_system_proxy'), \
                    patch('campuslink.manager.terminate_identity'):
                manager.start()
                self.assertIsNone(manager.journal['runtime'])
                self.assertEqual(manager.journal['previous_general'],general)
                manager.rollback()
            api.reload.assert_not_called()
            writes=[c.args for c in api.request.call_args_list if len(c.args)>1]
            self.assertEqual(writes,[('/configs','PATCH',{'tun':{'enable':False}}),
                                     ('/configs','PATCH',{'tun':{'enable':True}})])
            self.assertFalse((Path(temp)/'recovery.json').exists())

    def test_runtime_backup_rejects_already_contaminated_live_kernel(self):
        with tempfile.TemporaryDirectory() as temp:
            manager,api=self.manager();path=Path(temp)/'clash-verge.yaml'
            path.write_text(render(manager.source()),encoding='utf-8')
            api.request.return_value={'rules':[{'proxy':'CL-IPv6出口'}]}
            with patch('campuslink.manager.discover',return_value={'runtime':str(path)}):
                with self.assertRaisesRegex(RuntimeError,'仍运行 CampusLink'):
                    Manager._runtime_backup(manager,api)
            api.reload.assert_not_called()

    def test_stop_without_journal_still_stops_capture_and_suppresses_auto_start(self):
        manager,api=self.manager()
        manager.rollback()
        manager.capture.stop.assert_called_once()
        self.assertFalse(manager.state['capture_requested'])
        api.reload.assert_not_called()

    def test_capture_failure_does_not_skip_network_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            manager,api=self.manager();self.journal(manager,temp)
            manager.capture.stop.side_effect=RuntimeError('capture busy')
            with patch('campuslink.manager.DATA',Path(temp)),patch('campuslink.manager.Mihomo',return_value=api), \
                    patch('campuslink.manager.set_system_proxy') as proxy:
                with self.assertRaisesRegex(RuntimeError,'capture busy'):manager.rollback()
            proxy.assert_called_once();api.request.assert_called_once()
            self.assertTrue((Path(temp)/'recovery.json').exists())

    def test_stale_verge_client_cannot_receive_standalone_watchdog_reload(self):
        manager,api=self.manager();s,c=fixture()
        manager.active_config=build_config(c,s,True);manager.active_text=render(manager.active_config)
        manager.state.update(phase='running',mode='standalone',watchdog_restores=0)
        manager.journal={'mode':'standalone','previous_api':api.base}
        manager._restore_policy_if_changed(api,[],manager.active_config)
        api.reload.assert_not_called()

    def test_watchdog_requires_same_config_and_attached_api(self):
        manager,api=self.manager();s,c=fixture()
        manager.active_config=build_config(c,s);manager.active_text=render(manager.active_config)
        manager.state.update(phase='running',mode='attach',watchdog_restores=0)
        manager.journal={'mode':'attach','previous_api':api.base}
        manager._restore_policy_if_changed(api,[],copy.deepcopy(manager.active_config))
        api.reload.assert_not_called()
        manager.journal['previous_api']='http://127.0.0.1:12345'
        manager._restore_policy_if_changed(api,[],manager.active_config)
        api.reload.assert_not_called()
        manager.journal['previous_api']=api.base
        manager._restore_policy_if_changed(api,[],manager.active_config)
        api.reload.assert_called_once_with(manager.active_text)
        self.assertEqual(manager.state['watchdog_restores'],1)

    def test_warp_start_failure_restores_previous_config_and_keeps_no_false_success(self):
        with tempfile.TemporaryDirectory() as temp:
            manager,api=self.manager()
            manager.warp.start.side_effect=RuntimeError('SOCKS port occupied')
            with patch('campuslink.manager.DATA',Path(temp)),patch('campuslink.manager.validate_kernel'), \
                    patch('campuslink.manager.discover',return_value={'kernel':'mihomo.exe','runtime':''}),patch('campuslink.manager.Mihomo',return_value=api):
                with self.assertRaisesRegex(RuntimeError,'SOCKS port occupied'):manager.start()
                self.assertEqual(api.reload.call_count,2)
                self.assertIn('server: entry.example',api.reload.call_args.args[0])
                self.assertEqual(manager.state['phase'],'idle')
                self.assertIsNone(manager.journal)
                self.assertFalse((Path(temp)/'recovery.json').exists())

    def test_failed_rollback_retains_durable_recovery_record(self):
        with tempfile.TemporaryDirectory() as temp:
            manager,api=self.manager()
            manager.warp.start.side_effect=RuntimeError('start failed')
            api.reload.side_effect=[None,RuntimeError('API unavailable during rollback')]
            with patch('campuslink.manager.DATA',Path(temp)),patch('campuslink.manager.validate_kernel'), \
                    patch('campuslink.manager.discover',return_value={'kernel':'mihomo.exe','runtime':''}),patch('campuslink.manager.Mihomo',return_value=api):
                with self.assertRaisesRegex(RuntimeError,'start failed'):manager.start()
                self.assertEqual(manager.state['phase'],'recovery')
                self.assertIsNotNone(manager.journal)
                self.assertTrue((Path(temp)/'recovery.json').exists())

    def test_validation_failure_makes_no_network_change(self):
        manager,api=self.manager()
        with patch('campuslink.manager.validate_kernel',side_effect=RuntimeError('invalid candidate')):
            with self.assertRaisesRegex(RuntimeError,'invalid candidate'):manager.start()
        api.reload.assert_not_called();manager.warp.start.assert_not_called()
        self.assertIsNone(manager.journal)

    def test_normal_stop_restores_full_config_and_selector(self):
        with tempfile.TemporaryDirectory() as temp:
            manager,api=self.manager()
            with patch('campuslink.manager.DATA',Path(temp)),patch('campuslink.manager.validate_kernel'), \
                    patch('campuslink.manager.discover',return_value={'kernel':'mihomo.exe','runtime':''}),patch('campuslink.manager.Mihomo',return_value=api):
                manager.start();self.assertEqual(manager.state['phase'],'running')
                self.assertTrue((Path(temp)/'recovery.json').exists())
                manager.rollback();self.assertEqual(manager.state['phase'],'idle')
                api.request.assert_called_with('/proxies/test','PUT',{'name':'selected'})

    def test_reused_pid_is_never_terminated(self):
        expected={'pid':100,'created':1,'path':'owned.exe'}
        with patch('campuslink.system.process_identity',return_value=dict(expected,created=2)):
            # A mismatch returns before any Windows process-control call.
            terminate_identity(expected)


if __name__=='__main__':unittest.main()
