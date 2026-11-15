#!/usr/bin/env python3
"""session-ack: the mailbox and receipt service of the Claude Code sessions (Marko, 15.11.2026).

"we must develop the protocol where everything is confirmed that it's received."

Runs on chromium-vm (always on), HTTP on the Tailscale address only, port 8899 (SESSION_ACK_PORT). Standard library only.
Who may call: a Tailscale peer whose `tailscale whois` user is the owner (/etc/session-ack.conf OWNER=...), or 127.0.0.1.
Nothing here is secret: message text is stored so the receiver can pull it (GET /inbox) when the push route fails.

  GET  /health
  POST /msg     {id?, from, to, text, route?, attempt?}      -> {id, state}      (register / re-register on a retry)
  POST /ack     {id, state, by, note?, auto?}                -> {id, state}      state: delivered|read|done|refused|lost
  GET  /status/<id>                                          -> {id, from, to, state, events:[{t, state, by, note}]}
  GET  /wait/<id>?state=read&timeout=60                      -> long poll until the state is reached (or later) or timeout
  GET  /inbox/<to>?since=<epoch>                             -> messages for a session name (pull route), newest last
  GET  /sent/<from>?since=<epoch>                            -> state changes of messages a session sent (for its hook)

States only move forward: sent < delivered < lost < read < done|refused. lost is the sender's verdict after the retry;
a late receipt from the receiver still overrides it. An automatic done (auto: true, the Stop hook) never replaces a
done/refused the receiving session gave itself; its note (the reply's first line) is kept as an event either way.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = 1
PORT = int(os.environ.get('SESSION_ACK_PORT', '8899'))
STATE_DIR = os.environ.get('SESSION_ACK_STATE', '/var/lib/session-ack')
CONF = '/etc/session-ack.conf'
RANK = {'sent': 0, 'delivered': 1, 'lost': 1.5, 'read': 2, 'done': 3, 'refused': 3}   # a late receipt still beats lost
ID_RE = re.compile(r'^m-[0-9A-Za-z-]{6,60}$')
COND = threading.Condition()


def norm(name):
    """A session name as ListAgents shows it, without the (topic) part, lower case: mac24_14-11_(message flows) -> mac24_14-11"""
    n = re.sub(r'[_ ]*\(.*?\)\s*$', '', (name or '').strip())
    return n.strip().lower()


class Store:
    def __init__(self, path):
        os.makedirs(path, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(path, 'session-ack.db'), check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS msg(id TEXT PRIMARY KEY, frm TEXT, frm_n TEXT, too TEXT, too_n TEXT, text TEXT,
               route TEXT, attempt INTEGER, state TEXT, created REAL, updated REAL);
          CREATE TABLE IF NOT EXISTS ev(id TEXT, t REAL, state TEXT, by TEXT, note TEXT);
          CREATE INDEX IF NOT EXISTS ev_id ON ev(id);
          CREATE INDEX IF NOT EXISTS msg_too ON msg(too_n, created);
          CREATE INDEX IF NOT EXISTS msg_frm ON msg(frm_n, updated);''')

    def put_msg(self, d):
        now = time.time()
        mid = d['id']
        with self.lock:
            row = self.db.execute('SELECT state FROM msg WHERE id=?', (mid,)).fetchone()
            if row:
                self.db.execute('UPDATE msg SET attempt=?, route=COALESCE(?, route), updated=? WHERE id=?',
                                (int(d.get('attempt') or 1), d.get('route'), now, mid))
                self.db.execute('INSERT INTO ev VALUES(?,?,?,?,?)', (mid, now, 'sent', d.get('from', ''),
                                f"attempt {d.get('attempt') or 1}"))
                return row[0]
            self.db.execute('INSERT INTO msg VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (mid, d.get('from', ''), norm(d.get('from')), d.get('to', ''), norm(d.get('to')),
                             (d.get('text') or '')[:20000], d.get('route'), int(d.get('attempt') or 1), 'sent', now, now))
            self.db.execute('INSERT INTO ev VALUES(?,?,?,?,?)', (mid, now, 'sent', d.get('from', ''), 'attempt 1'))
            return 'sent'

    def ack(self, mid, state, by, note, auto=False):
        now = time.time()
        with self.lock:
            row = self.db.execute('SELECT state FROM msg WHERE id=?', (mid,)).fetchone()
            if not row:      # an ack for a message nobody registered (sent through the git route): keep it anyway
                self.db.execute('INSERT INTO msg VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                (mid, '', '', by, norm(by), '', 'unregistered', 1, 'sent', now, now))
                row = ('sent',)
            self.db.execute('INSERT INTO ev VALUES(?,?,?,?,?)', (mid, now, state, by, (note or '')[:500]))
            cur = row[0]
            if RANK[state] > RANK[cur] or (RANK[state] == RANK[cur] == 3 and not auto):
                cur = state
                self.db.execute('UPDATE msg SET state=?, updated=? WHERE id=?', (cur, now, mid))
            return cur

    def status(self, mid):
        with self.lock:
            m = self.db.execute('SELECT id,frm,too,state,attempt,route,created,updated FROM msg WHERE id=?', (mid,)).fetchone()
            if not m:
                return None
            ev = self.db.execute('SELECT t,state,by,note FROM ev WHERE id=? ORDER BY t', (mid,)).fetchall()
        k = ['id', 'from', 'to', 'state', 'attempt', 'route', 'created', 'updated']
        out = dict(zip(k, m))
        out['events'] = [dict(zip(['t', 'state', 'by', 'note'], e)) for e in ev]
        return out

    def inbox(self, to, since):
        with self.lock:
            rows = self.db.execute('SELECT id,frm,text,state,created FROM msg WHERE too_n=? AND created>? ORDER BY created',
                                   (norm(to), since)).fetchall()
        return [dict(zip(['id', 'from', 'text', 'state', 'created'], r)) for r in rows]

    def sent(self, frm, since):
        with self.lock:
            rows = self.db.execute('SELECT id,too,state,updated FROM msg WHERE frm_n=? AND updated>? ORDER BY updated',
                                   (norm(frm), since)).fetchall()
            out = []
            for r in rows:
                e = self.db.execute('SELECT by,note FROM ev WHERE id=? ORDER BY t DESC LIMIT 1', (r[0],)).fetchone()
                out.append({'id': r[0], 'to': r[1], 'state': r[2], 'updated': r[3], 'by': e[0], 'note': e[1]})
        return out


_whois = {}


def tailscale_user(ip):
    hit = _whois.get(ip)
    if hit and hit[1] > time.time():
        return hit[0]
    user = None
    try:
        out = subprocess.run(['tailscale', 'whois', '--json', ip], capture_output=True, timeout=5, text=True).stdout
        user = (json.loads(out).get('UserProfile') or {}).get('LoginName')
    except Exception:
        pass
    _whois[ip] = (user, time.time() + 600)
    return user


def conf():
    c = {}
    try:
        for line in open(CONF):
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1)
                c[k.strip()] = v.strip()
    except OSError:
        pass
    return c


class Handler(BaseHTTPRequestHandler):
    server_version = 'session-ack'
    store = None
    owner = None

    def log_message(self, fmt, *args):
        pass

    def answer(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def allowed(self):
        ip = self.client_address[0]
        if ip == '127.0.0.1':
            return True
        return bool(self.owner) and ip.startswith('100.') and tailscale_user(ip) == self.owner

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 64000:
            raise ValueError('too big')
        return json.loads(self.rfile.read(n) or b'{}')

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = [p for p in u.path.split('/') if p]
        if parts == ['health']:
            return self.answer(200, {'ok': True, 'service': 'session-ack', 'version': VERSION})
        if not self.allowed():
            return self.answer(403, {'error': 'not on the owner tailnet'})
        if len(parts) == 2 and parts[0] == 'status':
            s = self.store.status(parts[1])
            return self.answer(200 if s else 404, s or {'error': 'unknown id', 'id': parts[1]})
        if len(parts) == 2 and parts[0] == 'wait':
            want = q.get('state', 'read')
            if want not in RANK:
                return self.answer(400, {'error': 'bad state'})
            end = time.time() + min(float(q.get('timeout', 60)), 300)
            with COND:
                while True:
                    s = self.store.status(parts[1])
                    if s and RANK.get(s['state'], 0) >= RANK[want]:
                        return self.answer(200, dict(s, reached=True))
                    left = end - time.time()
                    if left <= 0:
                        return self.answer(200, dict(s or {'id': parts[1], 'state': 'unknown'}, reached=False))
                    COND.wait(min(left, 5))
        if len(parts) == 2 and parts[0] == 'inbox':
            return self.answer(200, {'messages': self.store.inbox(parts[1], float(q.get('since', 0)))})
        if len(parts) == 2 and parts[0] == 'sent':
            return self.answer(200, {'messages': self.store.sent(parts[1], float(q.get('since', 0)))})
        return self.answer(404, {'error': 'no such path'})

    def do_POST(self):
        if not self.allowed():
            return self.answer(403, {'error': 'not on the owner tailnet'})
        try:
            d = self.body()
        except Exception:
            return self.answer(400, {'error': 'bad json'})
        path = urlparse(self.path).path.rstrip('/')
        if path == '/msg':
            if not ID_RE.match(d.get('id') or ''):
                return self.answer(400, {'error': 'id must look like m-<time>-<rand>'})
            st = self.store.put_msg(d)
        elif path == '/ack':
            if not ID_RE.match(d.get('id') or '') or d.get('state') not in RANK:
                return self.answer(400, {'error': 'need id and state (delivered|read|done|refused|lost)'})
            st = self.store.ack(d['id'], d['state'], d.get('by', ''), d.get('note', ''), bool(d.get('auto')))
        else:
            return self.answer(404, {'error': 'no such path'})
        with COND:
            COND.notify_all()
        return self.answer(200, {'id': d['id'], 'state': st})


def tailscale_ip():
    try:
        return subprocess.run(['tailscale', 'ip', '-4'], capture_output=True, text=True, timeout=5).stdout.split()[0]
    except Exception:
        return None


def main():
    c = conf()
    Handler.store = Store(STATE_DIR)
    Handler.owner = c.get('OWNER') or os.environ.get('SESSION_ACK_OWNER')
    binds = [c.get('BIND') or os.environ.get('SESSION_ACK_BIND') or tailscale_ip(), '127.0.0.1']
    if not binds[0]:
        print('no Tailscale address yet', file=sys.stderr)
        sys.exit(1)
    servers = []
    for b in dict.fromkeys(binds):
        s = ThreadingHTTPServer((b, PORT), Handler)
        s.daemon_threads = True
        servers.append(s)
        print(f'session-ack v{VERSION} on {b}:{PORT}', flush=True)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    servers[0].serve_forever()


if __name__ == '__main__':
    main()
