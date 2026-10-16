---
description: Start, stop or check claude-phone, change its settings, or switch which phone rings
argument-hint: "[up|down|restart|status|config [get|set|unset] …|change_phone [desk|softphone]|voice …]"
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
- `voice` (or `voices`): run `phone.exe voice` with the rest of the argument, as single-quoted words:
  - Nothing, or `list <engine> [filter]`: reply with its output as it is.
  - `try <engine:name>`, `remove <engine:name>` or a voice to switch to: reply in one or two lines that keep every
    note (what was freed, which voice it switched to).
  - Exit code 2 means the voice needs a download, and the output gives its size. Ask the user with AskUserQuestion
    (`Download` or `Don't`), naming the voice and its size. Only on `Download`, run the command the output gives,
    with `--download`, and reply with its output.
  - Exit code 3, or `key`: the ElevenLabs key must not go through chat. Run nothing, and tell the user to run
    `C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe voice key` in a separate PowerShell window (not with
    `!` here: that has no keyboard to type into, and its output joins the chat), then try the voice again.
- Anything else: reply with
  `Usage: /claude-phone [up|down|restart|status|config [get|set|unset] <setting> [value]|change_phone [desk|softphone]|voice [<engine:name>|list <engine>|try|remove|key]]`
  and stop.

Run no other commands and change no files yourself.
