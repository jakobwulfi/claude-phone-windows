# Build notes

What it took to get each piece working on Windows 10 (VS Code, FreeSWITCH, Linphone), and why. The code already
does all of this; read it when something breaks. The original macOS version (Asterisk, iTerm2) is upstream at
[risingpower/claude-phone](https://github.com/risingpower/claude-phone).

## 1. FreeSWITCH instead of Asterisk

Asterisk has no Windows build; the FreeSWITCH 1.11.x MSI runs natively.

- **Our own config directory.** `phone up` starts `FreeSwitchConsole.exe -nonat -nonatmap` with `-conf`, `-log`,
  `-db` and the other directories under `~/.claude-phone/freeswitch`, from `freeswitch/freeswitch.xml.tmpl`. So the
  MSI's demo users (1000-1019, password 1234) are never exposed, which matters once a desk phone is on the LAN. If
  the MSI installed a Windows service, set it to Manual.
- **Call control over the event socket** (`claude_phone/esl.py`, 127.0.0.1:8021, `event json`). Every call to the
  daemon's extensions is `park`ed by the dialplan and routed in Python on `CHANNEL_PARK`.
- **Play only once the outgoing call is parked.** `CHANNEL_ANSWER` arrives before `originate … &park()` has finished;
  anything played in between is cut off, so the question's first words were lost. Wait for `CHANNEL_PARK`.
- **Read a recording only once FreeSWITCH has finished writing it.** The `record` app returns before the WAV header
  is complete; reading it then gave "fmt chunk and/or data chunk missing". The daemon waits (up to 3 s) for a
  complete WAV before transcribing.
- **Registration state:** ask the SIP profile (`sofia_contact internal/<ext>@<lan_ip>`). `show registrations`
  reads a table that survives FreeSWITCH restarts, so it lists phones that haven't re-registered yet.
- **Message light:** `sendevent MESSAGE_WAITING` makes FreeSWITCH send an unsolicited `NOTIFY`. Linphone 6 ignores
  it; a Yealink with MWI subscription off (its default) should show it.

## 2. Linphone on the same PC

- Linphone holds UDP 5060 and resets its own port setting on every start, so FreeSWITCH's replies to a
  registration on 5060 reached FreeSWITCH itself and the registration timed out. `sip_port = 5070` fixes it, with
  Linphone's domain set to `<lan_ip>:5070`.
- Linphone 6 opens its incoming-call window out of sight, so calls look unanswerable. Its auto-answer setting
  works around that for testing.
- Linphone re-registers on its own timer, so after a FreeSWITCH restart it can take a while to show as registered.

## 3. Typing into sessions: a VS Code extension

`vscode-extension/` runs a small HTTP server per VS Code window on 127.0.0.1 (random port, random token, no
`Origin` header allowed) and leaves `~/.claude-phone/bridges/<window>.json` for the daemon.
`Terminal.sendText(text, false)`, a 0.3 s wait, then `sendText("\r", false)`: Claude Code treats a trailing
newline sent with the text as part of a paste, so Enter has to go separately. It types into unfocused tabs, and
an idle Claude Code session submits at once; a busy one queues the text until its turn ends. Answers are typed
rather than sent over Claude Code's cross-session messaging because they must arrive as genuine user turns.

The Claude Code chat panel can't be reached this way: its extension only pre-fills a prompt from outside
(`claude-vscode.editor.open(session, prompt)`) and never submits it. Channels (an MCP server pushing into a
session) would work, but custom ones need `--dangerously-load-development-channels`, which Claude Code ignores
through the Agent SDK that the chat panel runs on. Hence `claudeCode.useTerminal`.

## 4. Session identity

The process chain is `claude.exe` → the terminal's shell (`Terminal.processId`) → VS Code's pty host. Hooks and the
MCP server are descendants of `claude`, so `inject.terminal_id()` walks up their parent processes (psutil) and asks
the bridges which ancestor is a terminal's shell: `vscode:<shell pid>`. It stays the same across `/clear` and
restarting `claude` in the tab. The MCP server also gets `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR`. Tool
calls carry no subagent id, hence the optional `from_agent` input. The SessionEnd hook has a 1.5 s default
timeout, so it is a single SQLite delete.

Esc runs no hook (Stop doesn't fire on an interrupt), so a session would stay "working" and callers would get hold
music until its next turn. Claude Code writes the interrupt to the session's transcript at once
(`~/.claude/projects/<project>/<session id>.jsonl`, a user message starting `[Request interrupted by user`), so the
daemon checks the end of the transcript before holding a call and when listing sessions. A Notification hook on
`idle_prompt` was tried first: it never fired in VS Code's terminal.

## 5. Timeouts

Claude Code moves MCP calls that run longer than 120 s to the background
(`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`). The budget is ring 25 s + answer 60 s + transcription about 2 s.

## 6. Windows details

- `os.kill(pid, 0)` **terminates** a process on Windows rather than checking it; `cli.py` uses psutil.
- Services start with `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` and outlive the shell (or Claude Code session)
  that started them. Not `DETACHED_PROCESS`: the venv's `python.exe` is a launcher, and the real interpreter it
  starts would then open a console window of its own, where closing it stops the daemon.
- A log file that's still open can't be renamed, so rotation copies and truncates.
- Windows error messages come in the system language (æøå), which the console's code page may not cover; the CLI
  prints them with replacement characters rather than crashing.
- Secrets live in Windows Credential Manager through `keyring` (service `claude-phone`).

## 7. Yealink T31P

- Remote phonebook XML: `<YealinkIPPhoneDirectory><DirectoryEntry><Name/><Telephone/></DirectoryEntry>`.
- Distinctive rings: `Alert-Info: <http://127.0.0.1>;info=Ring3`.
- Message light: with MWI subscription off (the default) the phone accepts unsolicited NOTIFY.
- Do Not Disturb rejects with 480; 480/486/603 are treated as "not answered".
- The screen shows 17 characters of caller ID before scrolling.

## 8. Speech

- **whisper.cpp:** whisper-server keeps the model loaded. Live repo and branch names are passed as the prompt, or
  names like "lemon stand" come back as near-misses. 8 kHz phone audio is fine. On a 6-core CPU without a GPU
  (Ryzen 5 5500, `whisper-blas-bin-x64`): `small.en-q5_1` takes ~1.8 s per answer, `large-v3-turbo` ~8.4 s and
  `base.en` ~0.55 s, so `small.en` is the default.
- **Text to speech:** the Windows voices through System.Speech, rendered straight to 16 kHz mono WAV; the text is
  passed outside the PowerShell script, never spliced into it. ElevenLabs is optional and falls back to Windows
  speech if it fails.
