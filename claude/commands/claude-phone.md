---
description: Start, stop or check claude-phone, change its settings, or switch which phone rings
argument-hint: "[up|down|restart|status|config [get|set|unset] …|change_phone [desk|softphone]]"
allowed-tools: Bash(C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe:*)
---

Manage claude-phone with its `phone` command: `C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe`.

The argument is "$ARGUMENTS". Empty means `up`. Its first word picks what to do:

- `up`, `down`, `restart` or `status`:
  1. For `up`, `down` or `restart`, run `phone.exe <argument>`. For `status`, skip this step.
  2. Run `phone.exe status`, then `phone.exe list`.
  3. Reply in at most four short lines:
     - which of freeswitch, whisper and daemon are running;
     - whether the phone marked "(rings)" is registered;
     - the live sessions as `301 repo/branch`;
     - anything that failed: quote the error line and say what to do about it.
- `config`: run `phone.exe config` with the rest of the argument: `config` alone lists the settings, then
  `get <setting>`, `unset <setting>` or `set <setting> <value>`. Pass the value as one single-quoted argument, so
  spaces and punctuation reach it unchanged, e.g. `phone.exe config set whisper_text 'Psst. Meeting soon.'`.
  Reply with its output: a list as it is, anything else in one or two lines that keep every note and warning
  (what to restart, a missing calendar link, a phone that isn't registered). Restart nothing yourself.
- `change_phone` (or `change-phone`): run `phone.exe change_phone` with `desk`, `softphone` or nothing. Reply in one
  or two lines that keep every note and warning. Restart nothing yourself.
- Anything else: reply with
  `Usage: /claude-phone [up|down|restart|status|config [get|set|unset] <setting> [value]|change_phone [desk|softphone]]`
  and stop.

Run no other commands and change no files yourself.
