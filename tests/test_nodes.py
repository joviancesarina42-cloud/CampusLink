import copy
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from campuslink.config import build_config, render
from campuslink.manager import Manager
from campuslink.nodes import catalog, ANCHOR, DEFAULT_GROUP, AI_GROUP
from campuslink.traffic import classify_connection
from test_core import fixture


class NodePolicyTests(unittest.TestCase):
    def test_two_groups_choose_nodes_independently(self):
        s,c=fixture();s['ai_node']='Other'
        config=build_config(c,s)
        groups={g['name']:g['proxies'][0] for g in config['proxy-groups']}
        self.assertEqual(groups,{DEFAULT_GROUP:ANCHOR,AI_GROUP:'CL-节点/Other'})
        proxy=next(p for p in config['proxies'] if p['name']=='CL-节点/Other')
        self.assertEqual(proxy['server'],'1.2.3.4')
        self.assertEqual(proxy['dialer-proxy'],ANCHOR)

    def test_subscription_metadata_is_not_a_node(self):
        s,c=fixture();p=copy.deepcopy(c['proxies'][1]);p['name']='剩余流量: 99 GB';c['proxies'].append(p)
        self.assertNotIn(p['name'],[n['id'] for n in catalog(c,s)])

    def test_missing_selected_node_never_falls_back(self):
        s,c=fixture();s['ai_node']='Deleted US node'
        with self.assertRaisesRegex(ValueError,'所选节点已不存在'):build_config(c,s)

    def test_ai_ipv6_targets_precede_native_and_domestic_rules(self):
        s,c=fixture();s['ai_domains'].append('tencent.com')
        rules=build_config(c,s)['rules']
        self.assertLess(rules.index('DOMAIN-SUFFIX,tencent.com,'+AI_GROUP),rules.index('IP-CIDR6,2000::/3,CL-原生IPv6,no-resolve'))
        self.assertIn('PROCESS-NAME,codex.exe,'+AI_GROUP,rules)

    def test_stale_source_chains_are_replaced_with_fixed_anchor(self):
        s,c=fixture();c['proxies'][1]['dialer-proxy']='old selector'
        config=build_config(c,s)
        proxy=next(p for p in config['proxies'] if p['name']=='CL-节点/Other')
        self.assertEqual(proxy['dialer-proxy'],ANCHOR)
        self.assertFalse(any('DIRECT' in g['proxies'] for g in config['proxy-groups']))

    def test_chained_ipv4_target_is_not_counted_as_physical_ipv4(self):
        proxies={'US':{'type':'trojan','server':'8.8.8.8','dialer-proxy':'JP'},
                 'JP':{'type':'hysteria2','server':'2001:4860::8888'}}
        conn={'chains':['US',AI_GROUP],'metadata':{'remoteDestination':'8.8.8.8'}}
        self.assertEqual(classify_connection(conn,proxies)[0],'unknown')
        category,remote,_=classify_connection(conn,proxies,None,{'2001:4860::8888':time.time()})
        self.assertEqual((category,remote),('tunnel6','2001:4860::8888'))


class SelectionTests(unittest.TestCase):
    def manager(self):
        s,c=fixture()
        manager=Manager.__new__(Manager)
        manager.state={'phase':'running','node_health':{}}
        manager.settings=SimpleNamespace(value=copy.deepcopy(s))
        manager.settings.save=lambda value:setattr(manager.settings,'value',copy.deepcopy(value))
        manager.source=lambda:copy.deepcopy(c)
        manager.active_config=build_config(c,s)
        manager.active_text=render(manager.active_config)
        calls=[]
        def request(path,method='GET',data=None):
            if path=='/proxies':return {'proxies':{DEFAULT_GROUP:{'now':ANCHOR},AI_GROUP:{'now':ANCHOR}}}
            calls.append((path,method,data));return {}
        api=SimpleNamespace(request=Mock(side_effect=request))
        manager.client=lambda:api
        manager.store=SimpleNamespace(event=Mock())
        return manager,api,calls

    def test_live_selection_updates_only_selectors_and_persists(self):
        manager,api,calls=self.manager()
        with tempfile.TemporaryDirectory() as temp,patch('campuslink.manager.DATA',Path(temp)),patch('campuslink.manager.validate_kernel'):
            result=manager.select_nodes({'ai_node':'Other'})
        self.assertEqual(manager.settings.value['ai_node'],'Other')
        self.assertEqual(result['selected']['ai'],'Other')
        self.assertEqual(len(calls),2)
        self.assertTrue(all(path.startswith('/proxies/') for path,_,_ in calls))

    def test_partial_runtime_failure_restores_both_old_selections(self):
        manager,api,calls=self.manager()
        request=api.request.side_effect
        failed=[False]
        def failure(path,method='GET',data=None):
            if method=='PUT' and data=={'name':'CL-节点/Other'} and not failed[0]:
                failed[0]=True;raise RuntimeError('AI selection failed')
            return request(path,method,data)
        api.request.side_effect=failure
        previous=copy.deepcopy(manager.settings.value)
        with patch('campuslink.manager.validate_kernel'):
            with self.assertRaisesRegex(RuntimeError,'AI selection failed'):
                manager.select_nodes({'ai_node':'Other'})
        self.assertEqual(manager.settings.value,previous)
        self.assertEqual([c[2] for c in calls[-2:]],[{'name':ANCHOR},{'name':ANCHOR}])

    def test_invalid_selection_makes_no_runtime_change(self):
        manager,api,_=self.manager()
        with self.assertRaises(ValueError):manager.select_nodes({'ai_node':'missing'})
        api.request.assert_not_called()


if __name__=='__main__':unittest.main()
