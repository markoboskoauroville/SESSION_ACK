#!/bin/bash
# server/install.sh: session-ack on chromium-vm (Marko, 15.11.2026). Run on the VM from a checkout of SESSION_ACK:
#   sudo server/install.sh
# Its own system user, state in /var/lib/session-ack (sqlite), the unit enabled at boot (core: receipts must always work).
# Who may call: the Tailscale owner (tailscale whois), and 127.0.0.1. Safe to run again.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
cd "$(dirname "$0")"
OWNER=$(tailscale status --json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['User'][str(d['Self']['UserID'])]['LoginName'])")
id session-ack >/dev/null 2>&1 || useradd --system --home /var/lib/session-ack --shell /usr/sbin/nologin session-ack
install -d -m 700 -o session-ack -g session-ack /var/lib/session-ack
install -d -m 755 /usr/local/lib/chromium-vm
install -m 755 session_ack_server.py /usr/local/lib/chromium-vm/session_ack_server.py
[ -f /etc/session-ack.conf ] || printf '# who may call session-ack: the Tailscale user of the devices\nOWNER=%s\n' "$OWNER" > /etc/session-ack.conf
chmod 644 /etc/session-ack.conf
install -m 644 systemd/session-ack.service /etc/systemd/system/session-ack.service
systemctl daemon-reload
systemctl enable session-ack.service
systemctl restart session-ack.service
sleep 1.5
IP=$(tailscale ip -4)
curl -fsS "http://$IP:8899/health" && echo && echo "session-ack: active on $IP:8899"
