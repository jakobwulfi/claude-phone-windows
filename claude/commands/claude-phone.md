---
description: Start, stop or check claude-phone (FreeSWITCH, whisper and the daemon)
argument-hint: "[up|down|restart|status]"
allowed-tools: Bash(C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe:*)
---

Manage claude-phone with its `phone` command: `C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe`.

The argument is "$ARGUMENTS". Empty means `up`. Anything other than `up`, `down`, `restart` or `status`: reply with
`Usage: /claude-phone [up|down|restart|status]` and stop.

1. For `up`, `down` or `restart`, run `phone.exe <argument>`. For `status`, skip this step.
2. Run `phone.exe status`, then `phone.exe list`.
3. Reply in at most four short lines:
   - which of freeswitch, whisper and daemon are running;
   - whether the phone marked "(rings)" is registered;
   - the live sessions as `301 repo/branch`;
   - anything that failed: quote the error line and say what to do about it.

Run no other commands and change no files.
