#!/usr/bin/env python3
"""ccmsg: every message between Claude Code sessions gets an id and a receipt (Marko, 15.11.2026).

  ccmsg send <to-session> "text"        register the message, print the exact text to pass to SendMessage / send_message
  ccmsg wait <id> [--timeout 120] [--state read]   block until the receipt; on the 1st timeout: resend once; 2nd: NOT RECEIVED
  ccmsg resend <id>                     the same text again, "retry 2" in its header
  ccmsg status [id]                     one message with its events, or the last messages this session sent
  ccmsg ack <id> read|done|refused "note"   the receiver's manual receipt (the hooks do read and done by themselves)
  ccmsg inbox [name]                    messages addressed to this session (the pull route)
  ccmsg flush                           send receipts that were spooled while the mailbox was out of reach
  ccmsg hook prompt|stop|posttool       the Claude Code hooks (UserPromptSubmit, Stop, PostToolUse on SendMessage)

The mailbox is session-ack on chromium-vm (CCMSG_URL, default http://100.102.86.30:8899, Tailscale only). Where it cannot
be reached (a cloud session), receipts go to the hub repository: ACKS/<id>.log on main (CCMSG_GIT_DIR, a clone of the hub).
Standard library only; runs on macOS, Termux, Linux. Never prints or needs a key.
"""
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get('CCMSG_URL', 'http://100.102.86.30:8899').rstrip('/')
HOME = os.path.expanduser('~')
CACHE = os.environ.get('CCMSG_CACHE', os.path.join(HOME, '.cache', 'ccmsg'))
SPOOL = os.path.join(CACHE, 'spool.jsonl')
SENT = os.path.join(CACHE, 'sent.json')
TURN = os.path.join(CACHE, 'turn')
HEAD_RE = re.compile(r'\[msg (m-[0-9A-Za-z-]+) from ([^\]·]+?)(?: · retry (\d+))?(?: · [^\]]*)?\]')
REFUSAL_RE = re.compile(r"\b(i (will not|won't|cannot|can't|refuse)|refus(e|ed|ing)|blocked by|was blocked|denied|declin(e|ed))\b", re.I)
FINAL = ('done', 'refused')


# ------------------------------------------------------------------ identity

def sessions_dir():
    return os.path.join(HOME, '.claude', 'sessions')


def strip_topic(name):
    return re.sub(r'[_ ]*\(.*?\)\s*$', '', (name or '').strip()).strip()


def session_record(sid=None):
    """The ~/.claude/sessions/<pid>.json of this session: by session id (hooks) or by walking up the parent processes."""
    d = sessions_dir()
    try:
        files = [f for f in os.listdir(d) if f.endswith('.json')]
    except OSError:
        return {}
    recs = {}
    for f in files:
        try:
            recs[f[:-5]] = json.load(open(os.path.join(d, f)))
        except Exception:
            pass
    if sid:
        for r in recs.values():
            if r.get('sessionId') == sid:
                return r
    pid = os.getpid()
    for _ in range(12):
        if str(pid) in recs:
            return recs[str(pid)]
        try:
            pid = int(subprocess.run(['ps', '-o', 'ppid=', '-p', str(pid)], capture_output=True, text=True,
                                     timeout=2).stdout.strip() or 0)
        except Exception:
            break
        if pid <= 1:
            break
    return {}


def me(sid=None):
    if os.environ.get('CCMSG_FROM'):
        return os.environ['CCMSG_FROM']
    r = session_record(sid)
    if r.get('name'):
        return strip_topic(r['name'])
    host = socket.gethostname().split('.')[0]
    sid = sid or r.get('sessionId') or ''
    return f'{host}-{sid[:8]}' if sid else host


# ------------------------------------------------------------------ transport

def http(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b'{}') | {'_http': e.code}
        except Exception:
            return {'_http': e.code}


def spool(kind, body):
    os.makedirs(CACHE, exist_ok=True)
    with open(SPOOL, 'a') as f:
        f.write(json.dumps({'kind': kind, 'body': body, 't': time.time()}) + '\n')


def post(kind, body, timeout=4):
    """POST /msg or /ack; on failure: the git route if configured, else the spool (sent at the next ccmsg call)."""
    try:
        return http('POST', '/' + kind, body, timeout)
    except Exception:
        if kind == 'ack' and git_dir():
            if git_ack(body):
                return {'id': body['id'], 'state': body['state'], 'route': 'git'}
        spool(kind, body)
        return {'id': body.get('id'), 'spooled': True}


def flush(quiet=True, timeout=3):
    try:
        lines = open(SPOOL).read().splitlines()
    except OSError:
        return 0
    if not lines:
        return 0
    left, n = [], 0
    for i, line in enumerate(lines):
        try:
            j = json.loads(line)
            http('POST', '/' + j['kind'], j['body'], timeout)
            n += 1
        except Exception:
            left = lines[i:]
            break
    with open(SPOOL, 'w') as f:
        f.write(''.join(l + '\n' for l in left))
    if not quiet:
        print(f'flushed {n}, left {len(left)}')
    return n


# ------------------------------------------------------------------ git route (no Tailscale: cloud sessions)

def git_dir():
    d = os.environ.get('CCMSG_GIT_DIR')
    if d and os.path.isdir(os.path.join(d, '.git')):
        return d
    return None


def git(d, *a, inp=None, env=None):
    return subprocess.run(['git', '-C', d, *a], input=inp, capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, **(env or {}))).stdout.strip()


def git_ack(body):
    """Append one line to ACKS/<id>.log on origin/main without touching the working tree (as session_reply.sh does)."""
    d = git_dir()
    f = f"ACKS/{body['id']}.log"
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\t{body['state']}\t{body.get('by', '')}\t{body.get('note', '')}"
    for attempt in range(3):
        try:
            git(d, 'fetch', '-q', 'origin', 'main')
            idx = os.path.join(CACHE, f'git-index-{os.getpid()}')
            os.makedirs(CACHE, exist_ok=True)
            env = {'GIT_INDEX_FILE': idx}
            git(d, 'read-tree', 'origin/main', env=env)
            old = subprocess.run(['git', '-C', d, 'show', f'origin/main:{f}'], capture_output=True, text=True).stdout
            blob = git(d, 'hash-object', '-w', '--stdin', inp=old + line + '\n')
            git(d, 'update-index', '--add', '--cacheinfo', f'100644,{blob},{f}', env=env)
            tree = git(d, 'write-tree', env=env)
            c = git(d, 'commit-tree', tree, '-p', 'origin/main', '-m', f"ACK {body['id']} {body['state']} by {body.get('by', '')}")
            os.unlink(idx)
            if subprocess.run(['git', '-C', d, 'push', '-q', 'origin', f'{c}:refs/heads/main'], capture_output=True).returncode == 0:
                return True
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    return False


def git_events(mid):
    d = git_dir() or (os.path.join(HOME, 'Developer', 'claude-code') if os.path.isdir(os.path.join(HOME, 'Developer', 'claude-code', '.git')) else None)
    if not d:
        return []
    try:
        git(d, 'fetch', '-q', 'origin', 'main')
        out = subprocess.run(['git', '-C', d, 'show', f'origin/main:ACKS/{mid}.log'], capture_output=True, text=True).stdout
    except Exception:
        return []
    ev = []
    for line in out.splitlines():
        p = line.split('\t')
        if len(p) >= 3:
            ev.append({'t': p[0], 'state': p[1], 'by': p[2], 'note': p[3] if len(p) > 3 else '', 'route': 'git'})
    return ev


# ------------------------------------------------------------------ local book of sent messages

def load_sent():
    try:
        return json.load(open(SENT))
    except Exception:
        return {}


def save_sent(s):
    os.makedirs(CACHE, exist_ok=True)
    tmp = SENT + '.tmp'
    json.dump(s, open(tmp, 'w'), indent=1)
    os.replace(tmp, SENT)


def new_id():
    return 'm-' + time.strftime('%Y%m%dT%H%M%S', time.gmtime()) + '-' + '%04x' % random.getrandbits(16)


def wrap(mid, frm, text, attempt=1):
    retry = f' · retry {attempt}' if attempt > 1 else ''
    return (f'[msg {mid} from {frm}{retry} · receipts automatic; if no hook: ccmsg ack {mid} read]\n{text}\n'
            f'[end {mid} · reply to {frm}]')


# ------------------------------------------------------------------ commands

def cmd_send(to, text, route='sendmessage'):
    flush()
    frm = me()
    mid = new_id()
    body = {'id': mid, 'from': frm, 'to': to, 'text': text, 'route': route, 'attempt': 1}
    r = post('msg', body)
    s = load_sent()
    rec = session_record()
    s[mid] = {'to': to, 'from': frm, 'text': text, 'attempt': 1, 't': time.time(), 'state': 'sent',
              'sid': rec.get('sessionId', ''), 'route': route}
    save_sent(s)
    where = 'mailbox' if not r.get('spooled') else 'spooled (mailbox out of reach: receipts come by git or not at all)'
    print(f'# {mid} registered ({where}). Pass the text between the lines to SendMessage to "{to}", then: ccmsg wait {mid}',
          file=sys.stderr)
    print(wrap(mid, frm, text))
    return 0


def cmd_resend(mid):
    s = load_sent()
    m = s.get(mid)
    if not m:
        print(f'{mid}: not sent from this machine', file=sys.stderr)
        return 2
    m['attempt'] = m.get('attempt', 1) + 1
    save_sent(s)
    post('msg', {'id': mid, 'from': m['from'], 'to': m['to'], 'text': m['text'], 'attempt': m['attempt']})
    print(f'# {mid} attempt {m["attempt"]}: pass it to SendMessage again, then: ccmsg wait {mid}', file=sys.stderr)
    print(wrap(mid, m['from'], m['text'], m['attempt']))
    return 0


def describe(st):
    ev = [e for e in st.get('events', []) if e.get('state') == st.get('state')] or st.get('events', [])
    last = ev[-1] if ev else {}
    note = f" — {last.get('note')}" if last.get('note') else ''
    return f"{st.get('id')} {st.get('state', '?').upper()} by {last.get('by') or st.get('to') or '?'}{note}"


def status(mid, timeout=6):
    try:
        st = http('GET', f'/status/{mid}', timeout=timeout)
    except Exception:
        st = {'id': mid, 'state': 'unknown', 'events': [], 'unreachable': True}
    weak = st.get('_http') == 404 or st.get('unreachable') or st.get('state') in ('sent', 'delivered', 'lost')
    gev = git_events(mid) if weak else []
    if gev:
        from_git = {'sent': 0, 'delivered': 1, 'lost': 1.5, 'read': 2, 'done': 3, 'refused': 3}
        best = max(gev, key=lambda e: from_git.get(e['state'], 0))
        if from_git.get(best['state'], 0) > from_git.get(st.get('state'), -1):
            st = {'id': mid, 'state': best['state'], 'events': st.get('events', []) + gev, 'route': 'git'}
    return st


def cmd_wait(mid, timeout=120, want='read'):
    flush()
    s = load_sent()
    t0 = s.get(mid, {}).get('t', time.time())
    end = time.time() + timeout
    st = {}
    rank = {'sent': 0, 'delivered': 1, 'lost': 1.5, 'read': 2, 'done': 3, 'refused': 3}
    while time.time() < end:
        left = max(1, int(end - time.time()))
        try:
            st = http('GET', f'/wait/{mid}?state={want}&timeout={min(left, 30)}', timeout=min(left, 30) + 10)
        except Exception:
            st = status(mid)                    # mailbox out of reach: the git route
            if rank.get(st.get('state'), 0) < rank[want]:
                time.sleep(min(20, left))
                continue
        if not st.get('reached') and rank.get(st.get('state'), 0) < rank[want]:
            gev = git_events(mid)               # the receiver may have answered by the git route (a cloud session)
            if gev:
                best = max(gev, key=lambda e: rank.get(e['state'], 0))
                if rank.get(best['state'], 0) >= rank[want]:
                    st = {'id': mid, 'state': best['state'], 'events': gev, 'reached': True}
        if st.get('reached') or rank.get(st.get('state'), 0) >= rank[want]:
            hit = [e for e in st.get('events', []) if rank.get(e.get('state'), 0) >= rank[want]]
            et = hit[0]['t'] if hit else time.time()
            if isinstance(et, str):
                et = time.mktime(time.strptime(et, '%Y-%m-%dT%H:%M:%SZ')) - time.timezone
            dt = max(0, et - t0)
            print(f'RECEIVED: {describe(st)} (receipt {dt:.0f} s after sending)')
            if mid in s:
                s[mid]['state'] = st['state']
                save_sent(s)
            return 0
    attempt = s.get(mid, {}).get('attempt', 1)
    got = st.get('state', 'unknown')
    if attempt < 2 and mid in s:
        print(f'NO RECEIPT yet for {mid} after {timeout} s (state {got}). Retry once: ccmsg resend {mid}, pass it to '
              f'SendMessage again, then ccmsg wait {mid}')
        return 3
    post('ack', {'id': mid, 'state': 'lost', 'by': me(), 'note': f'no receipt after {attempt} attempts'})
    if mid in s:
        s[mid]['state'] = 'lost'
        save_sent(s)
    print(f'NOT RECEIVED: {mid} to {s.get(mid, {}).get("to", "?")} — no receipt after {attempt} attempt(s), '
          f'{timeout} s each (last state: {got}). Say so plainly; try another route (session-link.md §0b).')
    return 4


def cmd_status(mid=None):
    flush()
    if mid:
        st = status(mid)
        print(describe(st))
        for e in st.get('events', []):
            t = e['t'] if isinstance(e['t'], str) else time.strftime('%d.%m %H:%M:%S', time.localtime(e['t']))
            print(f"  {t}  {e['state']:<9} {e.get('by', '')}  {e.get('note', '')}")
        return 0
    s = load_sent()
    for k in sorted(s, key=lambda k: s[k]['t'])[-15:]:
        try:
            st = http('GET', f'/status/{k}', timeout=4)
            print(describe(st) + f"  (to {s[k]['to']})")
        except Exception:
            print(f"{k} {s[k].get('state', '?').upper()} (to {s[k]['to']}, mailbox out of reach)")
    return 0


def cmd_ack(mid, state, note=''):
    r = post('ack', {'id': mid, 'state': state, 'by': me(), 'note': note})
    print(f"{mid}: {state} sent" + (' (spooled)' if r.get('spooled') else f" (route {r.get('route', 'mailbox')})"))
    return 0


def cmd_inbox(name=None):
    flush()
    name = name or me()
    r = http('GET', f'/inbox/{name}', timeout=8)
    for m in r.get('messages', []):
        print(f"{m['id']} from {m['from']} [{m['state']}]\n  {m['text'][:300]}")
    if not r.get('messages'):
        print(f'inbox of {name}: empty')
    return 0


# ------------------------------------------------------------------ hooks (never block, never fail the turn)

def last_assistant_text(d):
    t = d.get('last_assistant_message')
    if isinstance(t, str) and t.strip():
        return t
    try:
        with open(d.get('transcript_path') or '', 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400000))
            lines = f.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return ''
    for line in reversed(lines):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get('type') == 'assistant':
            for c in (o.get('message') or {}).get('content') or []:
                if isinstance(c, dict) and c.get('type') == 'text' and c.get('text', '').strip():
                    return c['text']
    return ''


def hook_prompt(d):
    sid = d.get('session_id', '')
    prompt = d.get('prompt') or ''
    ctx = []
    found = HEAD_RE.findall(prompt) if '[msg m-' in prompt else []
    if found:
        who = me(sid)
        os.makedirs(TURN, exist_ok=True)
        pend = []
        for mid, frm, retry in dict.fromkeys(found):
            r = post('ack', {'id': mid, 'state': 'read', 'by': who, 'note': 'seen at the start of a turn' +
                             (f' (retry {retry})' if retry else '')}, timeout=3)
            pend.append({'id': mid, 'from': frm.strip()})
            how = 'spooled, sent later' if r.get('spooled') else 'sent'
            ctx.append(f'ccmsg: receipt READ for {mid} from {frm.strip()} {how} automatically. When this turn ends the Stop '
                       f'hook reports DONE with your reply\'s first line. If you refuse or cannot do it, say so in the '
                       f'reply, or run: ccmsg ack {mid} refused "<why>".')
        json.dump(pend, open(os.path.join(TURN, sid or 'nosid') + '.json', 'w'))
    # the sender's side: state changes of messages this session sent, told at its next turn
    s = load_sent()
    mine = [k for k, v in s.items() if v.get('sid') == sid and v.get('state') not in FINAL + ('lost',)]
    if mine and sid:
        try:
            rows = http('GET', f"/sent/{me(sid)}?since={min(s[k]['t'] for k in mine) - 1}", timeout=1.5).get('messages', [])
            for row in rows:
                k = row['id']
                if k in mine and row['state'] != s[k].get('state'):
                    s[k]['state'] = row['state']
                    note = f" — {row['note']}" if row.get('note') else ''
                    ctx.append(f"ccmsg: your message {k} to {s[k]['to']} is now {row['state'].upper()} (by {row['by']}){note}")
            save_sent(s)
        except Exception:
            pass
    if ctx:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': '\n'.join(ctx)}}))
    flush(timeout=1.5)


def hook_stop(d):
    sid = d.get('session_id', '') or 'nosid'
    p = os.path.join(TURN, sid + '.json')
    try:
        pend = json.load(open(p))
    except Exception:
        return
    os.unlink(p)
    text = last_assistant_text(d).strip()
    first = ' '.join(text.split())[:300]
    state = 'refused' if REFUSAL_RE.search(text[:600]) else 'done'
    who = me(d.get('session_id'))
    for m in pend:
        post('ack', {'id': m['id'], 'state': state, 'by': who, 'note': first or 'turn ended', 'auto': True}, timeout=3)


def hook_posttool(d):
    """PostToolUse on SendMessage / send_message: the route took the message -> DELIVERED (to the route, not yet read)."""
    inp = json.dumps(d.get('tool_input') or {})
    ids = set(re.findall(r'\[msg (m-[0-9A-Za-z-]+) from', inp))
    if not ids:
        return
    resp = json.dumps(d.get('tool_response') or '')[:400]
    bad = re.search(r'error|fail|not found|no such|unknown', resp, re.I)
    for mid in ids:
        if bad:
            post('ack', {'id': mid, 'state': 'sent', 'by': me(d.get('session_id')), 'note': 'route error: ' + resp[:200]}, timeout=3)
        else:
            post('ack', {'id': mid, 'state': 'delivered', 'by': d.get('tool_name', 'route'),
                         'note': 'handed to the route: ' + resp[:160]}, timeout=3)


def cmd_hook(kind):
    try:
        d = json.load(sys.stdin)
    except Exception:
        d = {}
    try:
        {'prompt': hook_prompt, 'stop': hook_stop, 'posttool': hook_posttool}[kind](d)
    except Exception as e:                     # a receipt must never break a turn
        try:
            os.makedirs(CACHE, exist_ok=True)
            with open(os.path.join(CACHE, 'hook-errors.log'), 'a') as f:
                f.write(f"{time.strftime('%F %T')} {kind}: {e!r}\n")
        except Exception:
            pass
    return 0


def main(a):
    if not a or a[0] in ('-h', '--help', 'help'):
        print(__doc__)
        return 0
    c, rest = a[0], a[1:]

    def opt(name, default):
        if name in rest:
            i = rest.index(name)
            v = rest[i + 1]
            del rest[i:i + 2]
            return v
        return default
    if c == 'send':
        route = opt('--route', 'sendmessage')
        text = ' '.join(rest[1:]) if len(rest) > 1 else sys.stdin.read()
        return cmd_send(rest[0], text.strip(), route)
    if c == 'wait':
        t = int(opt('--timeout', '120'))
        w = opt('--state', 'read')
        return cmd_wait(rest[0], t, w)
    if c == 'resend':
        return cmd_resend(rest[0])
    if c == 'status':
        return cmd_status(rest[0] if rest else None)
    if c == 'ack':
        return cmd_ack(rest[0], rest[1], ' '.join(rest[2:]))
    if c == 'inbox':
        return cmd_inbox(rest[0] if rest else None)
    if c == 'flush':
        return 0 if flush(quiet=False) >= 0 else 1
    if c == 'hook':
        return cmd_hook(rest[0])
    if c == 'id':
        print(me())
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
