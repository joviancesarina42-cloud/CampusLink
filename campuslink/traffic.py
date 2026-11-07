from __future__ import annotations

import ipaddress
import sqlite3
import threading
import time
import uuid
import copy
from collections import OrderedDict
from datetime import datetime, timedelta
from functools import lru_cache


FAKE_NETWORK = ipaddress.ip_network('198.18.0.0/15')


@lru_cache(maxsize=4096)
def ip_family(value):
    try:
        addr = ipaddress.ip_address(value.strip('[]'))
        # fake-ip is an application destination, never physical IPv4 evidence.
        if addr.version == 4 and addr in FAKE_NETWORK:
            return 0
        return addr.version
    except (ValueError, AttributeError):
        return 0


def classify_connection(c, proxies, warp_evidence=None, egress_evidence=None):
    m = c.get('metadata', {})
    process = m.get('process', '').lower()
    if process in ('warpscout.exe', 'mihomo.exe', 'verge-mihomo.exe', 'verge-mihomo-alpha.exe'):
        return 'outer', m.get('remoteDestination', ''), '隧道外层，不重复计业务负载'
    chains = c.get('chains') or []
    leaf = chains[0] if chains else ''
    p = proxies.get(leaf, {})
    remote = m.get('remoteDestination') or c.get('remoteDestination') or ''
    if p.get('dialer-proxy'):
        outer = p
        seen = set()
        while outer.get('dialer-proxy'):
            name = outer['dialer-proxy']
            if name in seen or name not in proxies:
                return 'unknown', remote, '链式代理依赖缺失或形成循环'
            seen.add(name)
            outer = proxies[name]
        first_hop = outer.get('server', '')
        observed = (egress_evidence or {}).get(first_hop, 0)
        runtime_matches = ip_family(remote) == 6 and remote == first_hop
        if ip_family(first_hop) == 6 and (runtime_matches or time.time() - observed <= 20):
            return 'tunnel6', first_hop, f'物理首跳 IPv6 已观测；链式出口 {leaf}；内层入口 {remote}'
        return 'unknown', remote, '链式出口；尚缺实际 IPv6 首跳证据'
    if leaf in ('REJECT', 'REJECT-DROP'):
        return 'blocked', remote, '内核拒绝'
    if p.get('type') == 'socks5' and p.get('server') in ('127.0.0.1', '::1', 'localhost'):
        if (warp_evidence and warp_evidence.get('ok') and p.get('port')
                and p.get('port') == warp_evidence.get('port')
                and ip_family(warp_evidence.get('endpoint', '')) == 6):
            return 'tunnel6', warp_evidence['endpoint'], 'WARP HTTPS 探针和已知外层 IPv6 端点'
        return 'unknown', '', '本地 SOCKS；尚缺外层 IPv6 证据'
    if remote and ip_family(remote):
        family = ip_family(remote)
        try:
            public = ipaddress.ip_address(remote).is_global
        except ValueError:
            public = False
        if not public:
            return 'lan', remote, '运行时出站为本地/私有地址'
        if family == 4:
            return 'ipv4', remote, '运行时外连 IPv4'
        direct = leaf == 'DIRECT' or p.get('type') == 'direct'
        return ('native6' if direct else 'tunnel6'), remote, '运行时 remoteDestination IPv6'
    # A configured literal is useful context, but is not measured physical egress.
    return 'unknown', remote, '运行时未提供可确认的外连地址'


class DeltaTracker:
    def __init__(self):
        self.previous = {}
        self.initialized = False

    def sample(self, connections):
        current = {}
        result = []
        for c in connections:
            key = (c.get('id'), c.get('start'))
            value = (int(c.get('upload') or 0), int(c.get('download') or 0))
            current[key] = value
            old = self.previous.get(key)
            # The first view establishes the baseline for already open connections.
            if old is None:
                old = value if not self.initialized else (0, 0)
            up = max(0, value[0] - old[0])
            down = max(0, value[1] - old[1])
            result.append((c, up, down))
        self.previous = current
        self.initialized = True
        return result


SCHEMA = '''
CREATE TABLE IF NOT EXISTS samples (
 id INTEGER PRIMARY KEY, time REAL NOT NULL, day TEXT NOT NULL, session TEXT NOT NULL,
 plane TEXT NOT NULL, category TEXT NOT NULL, process TEXT NOT NULL DEFAULT '',
 host TEXT NOT NULL DEFAULT '', remote TEXT NOT NULL DEFAULT '', iface TEXT NOT NULL DEFAULT '',
 protocol TEXT NOT NULL DEFAULT '', up INTEGER NOT NULL, down INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_samples_day_plane ON samples(day, plane);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL, kind TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS daily (
 day TEXT NOT NULL, plane TEXT NOT NULL, category TEXT NOT NULL, process TEXT NOT NULL,
 host TEXT NOT NULL, remote TEXT NOT NULL, iface TEXT NOT NULL, protocol TEXT NOT NULL,
 up INTEGER NOT NULL, down INTEGER NOT NULL,
 PRIMARY KEY(day,plane,category,process,host,remote,iface,protocol)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value TEXT);
'''


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.execute('PRAGMA journal_mode=WAL')
        # WAL preserves database consistency; a power loss can lose the latest batch.
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA journal_size_limit=4194304')
        self.cache = OrderedDict()
        self.last_maintenance = 0
        with self.db:
            if not self.db.execute("SELECT 1 FROM store_meta WHERE key='daily_v1'").fetchone():
                self.db.execute('DELETE FROM daily')
                self.db.execute('''INSERT INTO daily SELECT day,plane,category,process,host,remote,iface,protocol,
                    SUM(up),SUM(down) FROM samples GROUP BY day,plane,category,process,host,remote,iface,protocol''')
                self.db.execute("INSERT INTO store_meta VALUES('daily_v1','1')")
        self.session = uuid.uuid4().hex

    def add(self, rows, day=None):
        if not rows:
            return
        now = time.time()
        day = day or datetime.now().strftime('%Y-%m-%d')
        with self.lock, self.db:
            self.db.executemany('INSERT INTO samples(time,day,session,plane,category,process,host,remote,iface,protocol,up,down) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                [(now, day, self.session, *r) for r in rows])
            self.db.executemany('''INSERT INTO daily VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(day,plane,category,process,host,remote,iface,protocol)
                DO UPDATE SET up=up+excluded.up, down=down+excluded.down''', [(day, *r) for r in rows])
            self.cache.clear()

    def event(self, kind, message):
        with self.lock, self.db:
            self.db.execute('INSERT INTO events(time,kind,message) VALUES(?,?,?)', (time.time(), kind, message))
            self.cache.clear()

    def snapshot(self, day=None, include_top=True, include_events=True):
        day = day or datetime.now().strftime('%Y-%m-%d')
        key = (day, include_top, include_events)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return copy.deepcopy(self.cache[key])
            totals = self.db.execute('SELECT plane,category,SUM(up),SUM(down) FROM daily WHERE day=? GROUP BY plane,category', (day,)).fetchall()
            top = self.db.execute('''SELECT plane,category,process,host,remote,iface,protocol,SUM(up) AS u,SUM(down) AS d
                FROM daily WHERE day=? GROUP BY plane,category,process,host,remote,iface,protocol ORDER BY u+d DESC LIMIT 250''', (day,)).fetchall() if include_top else []
            events = self.db.execute('SELECT time,kind,message FROM events ORDER BY id DESC LIMIT 35').fetchall() if include_events else []
            days = [r[0] for r in self.db.execute('SELECT DISTINCT day FROM daily ORDER BY day DESC LIMIT 90')]
            result = {'totals': [{'plane': p, 'category': c, 'up': u, 'down': d} for p,c,u,d in totals],
                'top': [dict(zip(['plane','category','process','host','remote','interface','protocol','up','down'], r)) for r in top],
                'events': [{'time': t, 'kind': k, 'message': m} for t,k,m in events], 'days': days,
                'day': day, 'session': self.session,
                'storage': {'detail_days': 30, 'older': '按日合并增量，保留会话、目标与字节总量'}}
            self.cache[key] = result
            if len(self.cache) > 8:
                self.cache.popitem(last=False)
            return copy.deepcopy(result)

    def maintain(self):
        """Compact old deltas without discarding bytes, destinations or sessions."""
        cutoff = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        with self.lock, self.db:
            row = self.db.execute("SELECT value FROM store_meta WHERE key='compacted_before'").fetchone()
            lower = row[0] if row else ''
            if lower < cutoff:
                # A failed transaction can leave the temporary staging table
                # created before the first DML statement. Make retries safe.
                self.db.execute('DROP TABLE IF EXISTS temp.compact')
                self.db.execute('''CREATE TEMP TABLE compact AS SELECT MIN(time) AS time,day,session,
                    plane,category,process,host,remote,iface,protocol,SUM(up) AS up,SUM(down) AS down
                    FROM samples WHERE day>=? AND day<?
                    GROUP BY day,session,plane,category,process,host,remote,iface,protocol''', (lower, cutoff))
                self.db.execute('DELETE FROM samples WHERE day>=? AND day<?', (lower, cutoff))
                self.db.execute('''INSERT INTO samples(time,day,session,plane,category,process,host,remote,iface,protocol,up,down)
                    SELECT * FROM compact''')
                self.db.execute('DROP TABLE compact')
                self.db.execute("INSERT OR REPLACE INTO store_meta VALUES('compacted_before',?)", (cutoff,))
            self.db.execute('DELETE FROM events WHERE id < COALESCE((SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET 1999),0)')
            self.cache.clear()

    def close(self):
        with self.lock:
            self.db.close()

    def export(self, day):
        with self.lock:
            return self.db.execute('SELECT time,day,session,plane,category,process,host,remote,iface,protocol,up,down FROM samples WHERE day=? ORDER BY id', (day,)).fetchall()

    def iter_export(self, day):
        # A dedicated read connection streams a stable WAL snapshot without
        # holding the sampler's lock or loading the entire CSV into memory.
        db = sqlite3.connect(self.path)
        try:
            db.execute('PRAGMA query_only=ON')
            cursor = db.execute('SELECT time,day,session,plane,category,process,host,remote,iface,protocol,up,down FROM samples WHERE day=? ORDER BY id', (day,))
            while rows := cursor.fetchmany(512):
                yield from rows
        finally:
            db.close()
