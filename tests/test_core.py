import copy
import ipaddress
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from campuslink.capture import decode_frame
from campuslink.config import (CAMPUS_DIRECT, DEFAULTS, NATIVE, WARP_PROXY, build_config,
                               discover, parse_endpoint, validate_settings)
from campuslink.probe import check_trace, read_exact, socks_connect
from campuslink.traffic import DeltaTracker, Store, classify_connection, ip_family


def fixture():
    s = copy.deepcopy(DEFAULTS)
    s.update(airport_source_name='JP-test', airport_source_host='entry.example',
             airport_ipv6='2001:4860:4860::8888',
             campus_domains=['example.edu.cn'],
             warp_domains=['video.example'], warp_processes=['video.exe'])
    source = {'proxies': [{'name':'JP-test', 'type':'hysteria2','server':'entry.example',
                          'port':65000,'password':'test-only','sni':'tls.example'},
                         {'name':'Other','type':'ss','server':'1.2.3.4','port':443}],
              'proxy-groups':[{'name':'old','type':'fallback','proxies':['Other','DIRECT']}],
              'dns':{'nameserver':['8.8.8.8'],'nameserver-policy':{'+.example':['1.1.1.1']}},
              'rules':['MATCH,old']}
    return s, source


class PolicyTests(unittest.TestCase):
    def test_source_is_not_mutated(self):
        s,source=fixture();before=copy.deepcopy(source)
        build_config(source,s,True)
        self.assertEqual(before,source)

    def test_missing_or_changed_entry_fails_closed(self):
        s,c=fixture();c['proxies'][0]['server']='changed.example'
        with self.assertRaisesRegex(ValueError,'未唯一命中'):build_config(c,s)

    def test_unimported_settings_refuse_to_build(self):
        s,c=fixture();s['airport_ipv6']=''
        with self.assertRaisesRegex(ValueError,'还没有导入本机配置'):build_config(c,s)

    def test_no_silent_ipv4_fallback(self):
        s,c=fixture();cfg=build_config(c,s)
        others=[p for p in cfg['proxies'] if p.get('server')=='1.2.3.4']
        self.assertEqual(others[0]['dialer-proxy'],'CL-机场IPv6')
        self.assertFalse(any('DIRECT' in group['proxies'] for group in cfg['proxy-groups']))
        self.assertEqual(cfg['proxy-groups'][0]['proxies'][0],'CL-机场IPv6')
        self.assertEqual(cfg['rules'][-1],'MATCH,CL-IPv6出口')

    def test_outer_443_precedes_udp_reject_and_all_standard_hosts(self):
        s,c=fixture();s['endpoints']=['[2606:4700:103::2]:443'];rules=build_config(c,s)['rules']
        outer=rules.index('IP-CIDR6,2606:4700:103::2/128,DIRECT,no-resolve')
        rejects=[i for i,r in enumerate(rules) if '(NETWORK,UDP)' in r and r.endswith('REJECT')]
        self.assertLess(outer,min(rejects))
        self.assertIn('IP-CIDR6,2606:4700:104::1/128,DIRECT,no-resolve',rules)

    def test_warp_domain_and_process_udp_is_blocked(self):
        s,c=fixture();rules=build_config(c,s)['rules']
        self.assertIn('AND,((PROCESS-NAME,video.exe),(NETWORK,UDP)),REJECT',rules)
        self.assertIn('PROCESS-NAME,video.exe,'+WARP_PROXY,rules)
        self.assertIn('AND,((DOMAIN-SUFFIX,video.example),(NETWORK,UDP)),REJECT',rules)
        self.assertFalse(any('DST-PORT,443' in x for x in rules))

    def test_warp_udp_option_routes_to_ipv6_airport(self):
        s,c=fixture();s['warp_udp']='airport'
        self.assertIn('AND,((PROCESS-NAME,video.exe),(NETWORK,UDP)),CL-IPv6出口',build_config(c,s)['rules'])

    def test_warp_disabled_removes_proxy_and_rules(self):
        s,c=fixture();s['warp_enable']=False
        s['exceptions']=[{'kind':'domain','value':'warp-target.example','route':'warp'}]
        cfg=build_config(c,s)
        self.assertFalse(any(p['name']==WARP_PROXY for p in cfg['proxies']))
        self.assertFalse(any(WARP_PROXY in r for r in cfg['rules']))
        self.assertFalse(any('video.example' in r for r in cfg['rules']))
        # A WARP exception must stay defined, but falls back to the default group.
        self.assertIn('DOMAIN-SUFFIX,warp-target.example,CL-IPv6出口',cfg['rules'])

    def test_native_unresolved_udp_cannot_use_direct_ipv4(self):
        s,c=fixture();rules=build_config(c,s)['rules']
        self.assertLess(rules.index('AND,((GEOSITE,cn),(NETWORK,UDP)),CL-IPv6出口'),rules.index('GEOSITE,cn,'+NATIVE))

    def test_ipv4_requires_specific_explicit_exception(self):
        s,c=fixture();s['exceptions']=[{'kind':'domain','value':'needs-v4.example','route':'ipv4'}]
        rules=build_config(c,s)['rules']
        self.assertIn('DOMAIN-SUFFIX,needs-v4.example,DIRECT',rules)
        self.assertLess(rules.index('DOMAIN-SUFFIX,needs-v4.example,DIRECT'),rules.index('GEOSITE,cn,'+NATIVE))

    def test_exception_overrides_ai_process_and_forced_native_ipv6(self):
        s,c=fixture()
        s['exceptions']=[{'kind':'domain','value':'downloads.example','route':'ipv4'}]
        s['ai_processes'].append('python.exe')
        rules=build_config(c,s)['rules']
        explicit=rules.index('DOMAIN-SUFFIX,downloads.example,DIRECT')
        self.assertLess(explicit,rules.index('PROCESS-NAME,python.exe,CL-AI服务'))
        self.assertLess(explicit,rules.index('GEOSITE,cn,'+NATIVE))
        self.assertNotIn('PROCESS-NAME,python.exe,DIRECT',rules)

    def test_dns_has_no_inherited_v4_upstream(self):
        s,c=fixture();cfg=build_config(c,s);self.assertNotIn('nameserver-policy',cfg['dns'])
        for item in cfg['dns']['nameserver']:self.assertEqual(ipaddress.ip_address(parse_endpoint(item)[0]).version,6)

    def test_campus_domains_allow_ipv4_before_forced_ipv6_or_ai(self):
        s,c=fixture();s['ai_processes'].append('chrome.exe');s['native_domains'].append('example.edu.cn')
        cfg=build_config(c,s)
        campus=cfg['rules'].index('DOMAIN-SUFFIX,example.edu.cn,'+CAMPUS_DIRECT)
        for later in ('PROCESS-NAME,chrome.exe,CL-AI服务','DOMAIN-SUFFIX,example.edu.cn,'+NATIVE,
                      'GEOSITE,cn,'+NATIVE):
            self.assertLess(campus,cfg['rules'].index(later))
        proxy=next(p for p in cfg['proxies'] if p['name']==CAMPUS_DIRECT)
        self.assertEqual(proxy,{'name':CAMPUS_DIRECT,'type':'direct','ip-version':'ipv6-prefer'})
        # IPv4 remains limited to the internal domain; the general native route is still strict.
        native=next(p for p in cfg['proxies'] if p['name']==NATIVE)
        self.assertEqual(native['ip-version'],'ipv6')

    def test_campus_dns_uses_real_addresses_and_retains_existing_filters_and_exceptions(self):
        s,c=fixture();c['dns']['fake-ip-filter']=['+.pinned.example']
        s['exceptions']=[{'kind':'domain','value':'restricted.example.edu.cn','route':'block'}]
        cfg=build_config(c,s)
        self.assertIn('+.pinned.example',cfg['dns']['fake-ip-filter'])
        self.assertIn('+.example.edu.cn',cfg['dns']['fake-ip-filter'])
        self.assertLess(cfg['rules'].index('DOMAIN-SUFFIX,restricted.example.edu.cn,REJECT'),
                        cfg['rules'].index('DOMAIN-SUFFIX,example.edu.cn,'+CAMPUS_DIRECT))

    def test_no_campus_list_means_no_extra_proxy_or_rule(self):
        s,c=fixture();s['campus_domains']=[]
        cfg=build_config(c,s)
        self.assertFalse(any(p['name']==CAMPUS_DIRECT for p in cfg['proxies']))
        self.assertFalse(any(CAMPUS_DIRECT in r for r in cfg['rules']))
        self.assertNotIn('fake-ip-filter',cfg['dns'])

    def test_campus_cidrs_are_direct_and_validated(self):
        s,c=fixture();s['campus_cidrs']=['2001:da8:9000::/40']
        self.assertIn('IP-CIDR6,2001:da8:9000::/40,DIRECT,no-resolve',build_config(c,s)['rules'])
        s,c=fixture();s['campus_cidrs']=['10.0.0.0/8']
        with self.assertRaisesRegex(ValueError,'内网 IPv6 网段'):validate_settings(s)

    def test_invalid_settings_do_not_publish(self):
        s,_=fixture();s['airport_ipv6']='1.2.3.4'
        with self.assertRaises(ValueError):validate_settings(s)
        s,_=fixture();s['exceptions']=[{'kind':'domain','value':'x,ANY,DIRECT','route':'ipv4'}]
        with self.assertRaises(ValueError):validate_settings(s)
        s,_=fixture();s['warp_domains']=['bad,name']
        with self.assertRaises(ValueError):validate_settings(s)


class DiscoverTests(unittest.TestCase):
    def test_explicit_paths_win_over_installed_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name in ('mihomo.exe','warpscout.exe','profile.yaml','account.json'):
                (root/name).write_text('x',encoding='utf-8')
            found=discover({'kernel_path':str(root/'mihomo.exe'),'source_config':str(root/'profile.yaml'),
                            'warp_path':str(root/'warpscout.exe'),'warp_account':str(root/'account.json')})
            self.assertEqual(Path(found['kernel']).name,'mihomo.exe')
            self.assertEqual(found['source'],str(root/'profile.yaml'))
            self.assertEqual(found['account'],str(root/'account.json'))
            self.assertTrue(found['checked']['kernel'])

    def test_missing_paths_report_what_was_checked(self):
        found=discover({'kernel_path':'','source_config':'','warp_path':'','warp_account':''})
        self.assertIsInstance(found['checked']['config_dirs'],list)
        self.assertFalse(found['checked']['kernel'] and not Path(found['kernel']).is_file())


class CountersTests(unittest.TestCase):
    def c(self,u,d,identifier='a',start='1'):return {'id':identifier,'start':start,'upload':u,'download':d}

    def test_initial_baseline_then_delta_and_no_duplicate(self):
        t=DeltaTracker();self.assertEqual(t.sample([self.c(100,200)])[0][1:],(0,0))
        self.assertEqual(t.sample([self.c(110,230)])[0][1:],(10,30))
        self.assertEqual(t.sample([self.c(110,230)])[0][1:],(0,0))

    def test_new_connections_and_core_restart(self):
        t=DeltaTracker();t.sample([])
        self.assertEqual(t.sample([self.c(10,20)])[0][1:],(10,20))
        self.assertEqual(t.sample([self.c(3,4,'b','2')])[0][1:],(3,4))

    def test_direct_is_not_implicitly_ipv6(self):
        c={'chains':['DIRECT'],'metadata':{'remoteDestination':'8.8.8.8'}}
        self.assertEqual(classify_connection(c,{})[0],'ipv4')
        c['metadata']['remoteDestination']='2001:4860:4860::8888'
        self.assertEqual(classify_connection(c,{})[0],'native6')

    def test_node_name_is_not_address_evidence(self):
        c={'chains':['WARP-v6'],'metadata':{}};self.assertEqual(classify_connection(c,{})[0],'unknown')
        p={'WARP-v6':{'type':'socks5','server':'127.0.0.1','port':11080}}
        self.assertEqual(classify_connection(c,p)[0],'unknown')
        self.assertEqual(classify_connection(c,p,{'ok':True,'endpoint':'2606:4700:104::2','port':11080})[0],'tunnel6')
        self.assertEqual(classify_connection(c,p,{'ok':True,'endpoint':'2606:4700:104::2','port':9999})[0],'unknown')

    def test_ipv4_airport_and_fake_ip(self):
        c={'chains':['v6-named'],'metadata':{'remoteDestination':'8.8.8.8'}}
        self.assertEqual(classify_connection(c,{'v6-named':{'type':'ss'}})[0],'ipv4')
        self.assertEqual(ip_family('198.18.1.2'),0)

    def test_outer_not_added_to_business(self):
        c={'chains':['DIRECT'],'metadata':{'process':'warpscout.exe','remoteDestination':'2606:4700:104::2'}}
        self.assertEqual(classify_connection(c,{})[0],'outer')

    def test_session_and_delta_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'test.db';store=Store(path);row=('wire','ipv6','','','2001:4860::1','WLAN','UDP',100,200)
            store.add([row]);sid=store.session;store.db.close();again=Store(path);again.add([row]);self.assertNotEqual(sid,again.session)
            result=again.snapshot()['totals'][0];self.assertEqual(result['up'],200);self.assertEqual(result['down'],400)
            self.assertEqual(len(again.export(again.snapshot()['day'])),2);again.db.close()


class PacketTests(unittest.TestCase):
    mac=bytes.fromhex('300505935d50');peer=bytes.fromhex('001122334455')

    def test_ipv4_direction_and_full_ip_length_with_truncated_capture(self):
        header=bytearray(20);header[0]=0x45;header[2:4]=struct.pack('!H',1500);header[9]=6
        header[12:16]=ipaddress.ip_address('172.18.1.2').packed;header[16:20]=ipaddress.ip_address('8.8.8.8').packed
        d=decode_frame(self.peer+self.mac+b'\x08\x00'+header+struct.pack('!HH',34000,443),self.mac)
        self.assertEqual((d['family'],d['direction'],d['bytes'],d['port']),(4,'up',1500,443))

    def test_vlan_ipv6_down(self):
        header=bytearray(40);header[0]=0x60;header[4:6]=struct.pack('!H',1400);header[6]=17
        header[8:24]=ipaddress.ip_address('2606:4700:104::2').packed;header[24:40]=ipaddress.ip_address('2001:da8::123').packed
        d=decode_frame(self.mac+self.peer+b'\x81\x00'+b'\x00\x01\x86\xdd'+header+struct.pack('!HH',4500,50000),self.mac)
        self.assertEqual((d['family'],d['direction'],d['bytes'],d['port']),(6,'down',1440,4500))

    def test_other_mac_or_truncated_header_is_ignored(self):
        self.assertIsNone(decode_frame(b'\x00'*14,self.mac));self.assertIsNone(decode_frame(b'\x00',self.mac))


class SocksTests(unittest.TestCase):
    def server(self,atyp=1,short=False):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();port=listener.getsockname()[1];errors=[]
        def worker():
            try:
                with listener,listener.accept()[0] as sock:
                    self.assertEqual(read_exact(sock,3),b'\x05\x01\x00')
                    for byte in b'\x05\x00':sock.send(bytes([byte]));time.sleep(.005)
                    head=read_exact(sock,5);read_exact(sock,head[4]+2)
                    address={1:b'\x7f\x00\x00\x01',3:b'\x03abc',4:b'\x00'*16}[atyp]
                    response=b'\x05\x00\x00'+bytes([atyp])+address+b'\x00\x50'
                    for byte in response[:-1] if short else response:sock.send(bytes([byte]));time.sleep(.002)
                    if not short:sock.sendall(b'HTTP-START')
            except Exception as e:errors.append(e)
        t=threading.Thread(target=worker);t.start();return port,t,errors

    def test_segmented_greeting_and_variable_address_length(self):
        for atyp in (1,3,4):
            port,thread,errors=self.server(atyp)
            with socks_connect(port,'example.test',80,timeout=2) as sock:self.assertEqual(read_exact(sock,10),b'HTTP-START')
            thread.join();self.assertFalse(errors)

    def test_truncated_connect_reply_is_failure(self):
        port,thread,errors=self.server(short=True)
        with self.assertRaises(ConnectionError):socks_connect(port,'example.test',80,timeout=2)
        thread.join();self.assertFalse(errors)

    def test_http_503_redirect_and_non_warp_are_unhealthy(self):
        class FakeSocket:
            def sendall(self,_):pass
            def close(self):pass
        class FakeResponse:
            status=503
            def __init__(self,_):pass
            def begin(self):pass
            def read(self,_):return b'h=www.cloudflare.com\nip=104.28.1.1\ncolo=HKG\nwarp=off\n'
        class FakeTLS:
            def wrap_socket(self,sock,**_):return sock
        with patch('campuslink.probe.socks_connect',return_value=FakeSocket()),patch('campuslink.probe.ssl.create_default_context',return_value=FakeTLS()),patch('campuslink.probe.http.client.HTTPResponse',FakeResponse):
            for code in (503,302,200):
                FakeResponse.status=code;self.assertFalse(check_trace(1,True)['ok'])


if __name__=='__main__':unittest.main()
