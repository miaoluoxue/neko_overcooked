"""Durable high-level control and telemetry shared by supervisor, chefs and HTTP API."""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / 'runtime' / 'agents.sqlite3'
MODES = ('coop', 'clumsy', 'sabotage')


def chef_for_player(chefs, player):
    identity={'P1':'One','P2':'Two'}[player]
    return next((c for c in chefs if c.get('player')==identity),None)


class Conflict(ValueError):
    pass


class Bus:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get('NEKO_AGENT_DB') or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, player TEXT, type TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, actor TEXT, key TEXT, kind TEXT,
                    payload TEXT, status TEXT, created REAL, updated REAL, expires REAL, result TEXT,
                    UNIQUE(actor,key));
                CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, actor TEXT, key TEXT, payload TEXT,
                    epoch TEXT, status TEXT, expires REAL, votes TEXT, UNIQUE(actor,key));
            ''')

    @contextmanager
    def connect(self, transaction=False):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            if transaction: c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def put(self, key, value):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def get(self, key, default=None):
        with self.connect() as c:
            r = c.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
            return json.loads(r[0]) if r else default

    def event(self, kind, data, player=None):
        with self.connect() as c:
            c.execute('INSERT INTO events(at,player,type,data) VALUES (?,?,?,?)',
                      (time.time(), player, kind, json.dumps(data, ensure_ascii=False)))

    def events(self, player, after=0, limit=100):
        with self.connect() as c:
            rows = c.execute('SELECT * FROM events WHERE id>? AND (player IS NULL OR player=?) ORDER BY id LIMIT ?',
                             (after, player, limit)).fetchall()
        return [dict(id=r['id'], at=r['at'], player=r['player'], type=r['type'], data=json.loads(r['data'])) for r in rows]

    @staticmethod
    def decode(row):
        if not row: return None
        d = dict(row)
        for key in ('payload', 'result', 'votes'):
            if key in d: d[key] = json.loads(d[key]) if d[key] is not None else None
        return d

    def submit(self, actor, key, kind, payload, ttl=120):
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        with self.connect(True) as c:
            old = c.execute('SELECT * FROM requests WHERE actor=? AND key=?', (actor,key)).fetchone()
            if old:
                if old['kind'] != kind or old['payload'] != raw: raise Conflict('idempotency_key_reused')
                return self.decode(old)
            rid = str(uuid.uuid4()); now = time.time()
            c.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (rid, actor, key, kind, raw, 'queued', now, now, now+ttl, None))
            return self.decode(c.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone())

    def claim(self, kind, actor=None):
        with self.connect(True) as c:
            now = time.time()
            c.execute("UPDATE requests SET status='expired',updated=? WHERE status='queued' AND expires<?", (now,now))
            row = c.execute("SELECT * FROM requests WHERE status='queued' AND kind=? AND (? IS NULL OR actor=?) ORDER BY created LIMIT 1",
                            (kind,actor,actor)).fetchone()
            if row:
                c.execute("UPDATE requests SET status='running',updated=? WHERE id=?", (now,row['id']))
                return self.decode(row)

    def finish(self, rid, status, result):
        with self.connect() as c:
            c.execute('UPDATE requests SET status=?,result=?,updated=? WHERE id=?',
                      (status,json.dumps(result,ensure_ascii=False),time.time(),rid))
        self.event('request_'+status, dict(request_id=rid, result=result))

    def request(self, rid):
        with self.connect() as c:
            now=time.time()
            c.execute("UPDATE requests SET status='expired',updated=? WHERE id=? AND status='queued' AND expires<?",(now,rid,now))
            return self.decode(c.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone())

    def propose(self, actor, key, payload, epoch):
        raw = json.dumps(payload,sort_keys=True,ensure_ascii=False)
        with self.connect(True) as c:
            old = c.execute('SELECT * FROM proposals WHERE actor=? AND key=?',(actor,key)).fetchone()
            if old:
                if old['payload'] != raw: raise Conflict('idempotency_key_reused')
                return self.decode(old)
            c.execute("UPDATE proposals SET status='expired' WHERE status='proposed' AND expires<?", (time.time(),))
            c.execute("UPDATE requests SET status='expired',updated=? WHERE status='queued' AND expires<?", (time.time(),time.time()))
            if c.execute("SELECT 1 FROM proposals WHERE status='proposed'").fetchone() or c.execute("SELECT 1 FROM requests WHERE kind='session' AND status IN ('queued','running')").fetchone():
                raise Conflict('session_change_already_pending')
            pid = str(uuid.uuid4())
            c.execute('INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?)',
                      (pid,actor,key,raw,epoch,'proposed',time.time()+60,json.dumps({actor:True})))
            return self.decode(c.execute('SELECT * FROM proposals WHERE id=?',(pid,)).fetchone())

    def proposal(self, pid):
        with self.connect() as c:
            c.execute("UPDATE proposals SET status='expired' WHERE status='proposed' AND expires<?",(time.time(),))
            d=self.decode(c.execute('SELECT * FROM proposals WHERE id=?',(pid,)).fetchone())
        if d: d['operation'] = self.request(pid)
        return d

    def vote(self, pid, actor, approve, epoch):
        with self.connect(True) as c:
            r=self.decode(c.execute('SELECT * FROM proposals WHERE id=?',(pid,)).fetchone())
            if not r: raise KeyError('proposal_not_found')
            if r['status'] != 'proposed': return r
            if r['expires'] < time.time() or r['epoch'] != epoch: raise Conflict('proposal_expired_or_scene_changed')
            r['votes'][actor] = approve
            status = 'rejected' if not approve else ('approved' if r['votes'].get('P1') and r['votes'].get('P2') else 'proposed')
            c.execute('UPDATE proposals SET votes=?,status=? WHERE id=?',(json.dumps(r['votes']),status,pid))
            if status == 'approved':
                now=time.time()
                payload=dict(r['payload'], expected_epoch=epoch)
                c.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?)',
                          (pid,'both',pid,'session',json.dumps(payload,sort_keys=True),'queued',now,now,now+120,None))
        return self.proposal(pid)

    def interrupt_running(self, kind, actor=None):
        with self.connect() as c:
            c.execute("UPDATE requests SET status='failed', result=?,updated=? WHERE kind=? AND status='running' AND (? IS NULL OR actor=?)",
                      (json.dumps({'error':'consumer_restarted_no_automatic_replay'}),time.time(),kind,actor,actor))
