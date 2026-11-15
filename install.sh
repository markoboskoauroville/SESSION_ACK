#!/bin/sh
# install.sh: ccmsg + its three Claude Code hooks on this device (Mac, VM, Termux, PRoot). Safe to run again.
#   git clone https://github.com/markoboskoauroville/SESSION_ACK ~/SESSION_ACK && sh ~/SESSION_ACK/install.sh
# --no-hooks   only the command (e.g. a device whose settings someone else edits)
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
BIN="$HOME/.local/bin"; [ -n "$PREFIX" ] && [ -d "$PREFIX/bin" ] && BIN="$PREFIX/bin"      # Termux
mkdir -p "$BIN"; chmod +x "$DIR/ccmsg.py"; ln -sf "$DIR/ccmsg.py" "$BIN/ccmsg"
echo "ccmsg -> $BIN/ccmsg"
[ "$1" = "--no-hooks" ] && exit 0
python3 - "$DIR" <<'PY'
import json, os, sys
d = sys.argv[1]; p = os.path.expanduser('~/.claude/settings.json')
s = json.load(open(p)) if os.path.exists(p) else {}
h = s.setdefault('hooks', {})
def add(ev, kind, matcher=None):
    if any('ccmsg.py hook' in x.get('command', '') for g in h.get(ev, []) for x in g.get('hooks', [])):
        return print(f'  {ev}: already there')
    g = {'hooks': [{'type': 'command', 'command': f'python3 {d}/ccmsg.py hook {kind} 2>/dev/null || true', 'timeout': 6}]}
    if matcher: g['matcher'] = matcher
    h.setdefault(ev, []).append(g); print(f'  {ev}: added')
add('UserPromptSubmit', 'prompt'); add('Stop', 'stop'); add('PostToolUse', 'posttool', 'SendMessage|mcp__.*send_message')
os.makedirs(os.path.dirname(p), exist_ok=True)
tmp = p + '.ccmsg-tmp'; json.dump(s, open(tmp, 'w'), indent=2); os.replace(tmp, p)
PY
curl -s -m 5 "${CCMSG_URL:-http://100.102.86.30:8899}/health" && echo || echo "mailbox not reachable from here: receipts use the git route (CCMSG_GIT_DIR) or the spool"
