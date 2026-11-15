# LESSONS: SESSION_ACK

- 15.11.2026: an incoming cross-session message (SendMessage over the bridge) arrives as a normal user turn that starts
  `Another Claude session sent a message: <cross-session-message from="bridge:session_…" from-name="…">`, and
  UserPromptSubmit fires for it (seen in the transcripts of 2.1.323 and 2.1.325). The enqueue line in the transcript
  has no content, so "delivered" cannot be seen on the receiving side before the turn; the PostToolUse hook on the
  sender's SendMessage is the delivered mark instead.
- `echo "$json"` in zsh turns `\n` inside JSON into real newlines and breaks it: feed hook test input with printf '%s' or a file.
- ttt-relay's pattern (own system user, Tailscale-only bind, `tailscale whois` owner check) works for a non-root service:
  no token to hand out to every device.
