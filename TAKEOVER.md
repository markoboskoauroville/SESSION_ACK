# TAKEOVER: SESSION_ACK (ccmsg + session-ack)

Receipts for every message between Claude Code sessions (Marko, 15.11.2026: "we must develop the protocol where everything
is confirmed that it's received"). Rule: MANTRA_MANIFEST modules/session-ack.md.

## The protocol

- Every message has an id `m-<UTC time>-<4 hex>` and a header line the receiver sees:
  `[msg m-… from <sender> · receipts automatic; if no hook: ccmsg ack m-… read]` … text … `[end m-… · reply to <sender>]`.
- States, only forward: `sent` → `delivered` (the route took it: PostToolUse hook on SendMessage / send_message) →
  `read` (the receiving session's UserPromptSubmit hook saw the header) → `done` / `refused` (its Stop hook, with the
  reply's first line; or the session itself: `ccmsg ack <id> refused "why"`). `lost` = the sender's verdict after one
  retry; a late receipt still overrides it. An automatic done never replaces a done/refused the session gave itself.
- Sender: `ccmsg send <to> "text"` prints the wrapped text → pass it to SendMessage → `ccmsg wait <id>` (run in the
  background; long-polls the mailbox, no polling by hand). Timeout (120 s): `ccmsg resend <id>` once, `ccmsg wait` again;
  then `NOT RECEIVED`, said plainly. The sender's own UserPromptSubmit hook also tells it at its next turn when one of
  its open messages changed state.
- Mailbox: `session-ack` on chromium-vm, http://100.102.86.30:8899 (Tailscale address + 127.0.0.1, owner check by
  `tailscale whois`, no token). sqlite in /var/lib/session-ack. Endpoints in the server's docstring.
- No Tailscale (cloud sessions): receipts go as lines in `ACKS/<id>.log` on the hub's main (claude-code, private), via
  git plumbing like session_reply.sh (`CCMSG_GIT_DIR=<hub clone>`). The Mac's `ccmsg wait/status` also read that file.
- Mailbox down: receipts are spooled in ~/.cache/ccmsg/spool.jsonl and sent at the next ccmsg call or hook.

## Files

- `ccmsg.py`: the CLI and the three hooks (`ccmsg hook prompt|stop|posttool`), stdlib only, never fails a turn
  (errors to ~/.cache/ccmsg/hook-errors.log). Local book of sent messages: ~/.cache/ccmsg/sent.json.
- `server/session_ack_server.py`, `server/install.sh` (`sudo server/install.sh` on the VM), `server/systemd/`.
- `install.sh`: links `ccmsg` (~/.local/bin, or $PREFIX/bin on Termux) and adds the three hooks to ~/.claude/settings.json.

## Where it is installed (15.11.2026)

- chromium-vm: service `session-ack` enabled (core in vmctl services.json, vmsvc.slice, MemoryMax 120M); ccmsg + hooks
  in ~/SESSION_ACK for the VM's own Claude Code.
- Mac: `~/.local/bin/ccmsg` → ~/Developer/SESSION_ACK/ccmsg.py; hooks in ~/.claude/settings.json (UserPromptSubmit,
  Stop, PostToolUse matcher `SendMessage|mcp__.*send_message`).
- Phones (Pixel 7 Termux, Nothing PRoot): not installed; the chain is in the manifest module.

## Proven

- 15.11.2026: Mac → VM mailbox → headless `claude -p` receiver on the Mac with the hooks: read 6 s, done 9 s (note "OK").
  Timeout case: NO RECEIPT → resend → NOT RECEIVED (exit 4), state lost. Git route: ack with the mailbox unreachable
  landed in hub ACKS/<id>.log and `ccmsg wait` saw it.
- That UserPromptSubmit fires for an incoming cross-session message (2.1.323/2.1.325) was read from the transcripts:
  every `<cross-session-message …>` user turn has its UserPromptSubmit hook attachment.

## Open

- The first live SendMessage between two interactive sessions with the hooks is still to be watched end to end.
- A cloud session needs the hooks in its repo's `.claude/settings.json` (or a manual `ccmsg ack`), and a hub clone for the git route.
- Refused vs done from the Stop hook is a keyword guess on the reply's first lines; the note always carries the reply.
