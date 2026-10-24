# Build notes

What it took to get each piece working on Windows 10 (typing into any terminal, VS Code, Visual Studio, FreeSWITCH,
Linphone), and why. The code already does all of this; read it when something breaks. The original macOS version (Asterisk, iTerm2) is upstream at
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

## 3. Typing into sessions: two routes

**VS Code terminals** are typed into by an extension, the primary route there: it was proven first, needs no
console tricks, and a window's terminals are listed by VS Code itself. **Every other terminal** (Visual Studio's
Terminal window, Windows Terminal, PowerShell, cmd) is typed into through the console, see the end of this section.

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

**Through the console.** Visual Studio has no public API for typing into its Terminal window, but every Windows
console reads keystrokes from an input buffer that any process on the same console can write to. So
`claude_phone/consoleinject.py` does `FreeConsole`, `AttachConsole(shell pid)`, opens `CONIN$` and calls
`WriteConsoleInputW` with a key-down and a key-up per UTF-16 unit, waits 0.3 s, then sends Enter as a write of its
own (the same rhythm as the extension, for the same paste reason). Measured before building it, in a throwaway
spike on Windows 10, against a real `claude` with its window in the background: it works in a standalone PowerShell
window (classic conhost) and in the ConPTY terminals of VS Code and Visual Studio 2026, with æøå and an emoji
intact (Claude answered `U+1F600`), and text sent while the session is busy waits for its turn. Plain key events
were enough: no `VK_PACKET`, no key-down-only trick. Windows Terminal uses the same console route but hasn't been
tried yet (it wasn't installed); its ConPTY should behave like the other two. (An emoji read back from the console buffer as a replacement character; that was the
spike's screen reader, not the typing.)

- **A process of its own.** Attaching to another console detaches the caller from its own, which would take the
  daemon's log away, so `inject._run_helper` runs `python -I -m claude_phone.consoleinject <pid> <creation time>`
  with `CREATE_NO_WINDOW`. The text goes in on stdin: other processes can read command lines. The venv's
  `python.exe` is a launcher for the real interpreter, so a timeout kills the whole process tree, or a helper left
  behind could type later.
- **Never into a bare shell.** If `claude` died without its SessionEnd hook, Enter would run the phone transcript as
  a shell command. So typing is refused unless an interactive `claude` runs under that very shell: checked under the
  typing lock (a message queued behind another is not checked until its turn), and again by the helper before the
  text and before Enter. `claude -p` doesn't count: it never reads the console. The Claude desktop app is also a
  `claude.exe` and is told apart by its install folder.
- **Elevated consoles** can't be attached to from a daemon that isn't elevated (Windows error 5). It's reported as
  such. The daemon shouldn't be elevated either, to avoid typing into any console of the user's it was never meant
  to reach.

## 4. Session identity

The process chain is `claude.exe` → the terminal's shell (`Terminal.processId`) → VS Code's pty host. Hooks and the
MCP server are descendants of `claude`, so `inject.terminal_id()` walks up their parent processes (psutil) and asks
the bridges which ancestor is a terminal's shell: `vscode:<shell pid>`. It stays the same across `/clear` and
restarting `claude` in the tab. The MCP server also gets `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR`. Tool
calls carry no subagent id, hence the optional `from_agent` input. The SessionEnd hook has a 1.5 s default
timeout, so it is a single SQLite delete.

**Terminals that aren't VS Code's** get `con:<shell pid>:<its creation time in ms>`. The creation time is there so a
recycled pid can never match: it's checked before every injection, by the sweeper and by the helper. The shell is the
nearest known one above `claude` (`pwsh`, `powershell`, `cmd`, `nu`), found with psutil like the VS Code ids, and the
id stays the same across `/clear` and restarting `claude` in the same window, so the tab keeps its extension. Rules
that came out of review:

- Not a shell below `claude`: Claude Code's own Bash tool runs under `bash.exe`, and a hook may sit in a `cmd /c`.
  `bash.exe` isn't a shell here at all: Git Bash's default window (mintty) isn't a Windows console, so "typing" there
  would report success and reach nothing.
- A `claude` started by another `claude` (a shell sits between them) gets no id: the shell above the outer one is the
  outer session's, and registering it would take over that session's extension. `claude -p` never registers either.
- A VS Code window that doesn't answer in 3 s only holds back sessions under a VS Code-family process; others register
  (a VS Code terminal that got a console id first and a `vscode:` id later would be a new tab to the registry).
- `inject.live_term_ids()` is what the sweeper keeps: every `vscode:` terminal from the bridges (an unanswering
  window still aborts the sweep, so its sessions aren't dropped) plus every running shell's console id.
- `Controller.caller` takes `term_id` from callers: a `con:` or `vscode:` id is checked (a running shell with an
  interactive `claude` under it) before an unknown caller is registered.

Where a session runs ("VS Code", "Visual Studio", "Windows Terminal", "PowerShell", ...) is the nearest known app
above its shell, else the shell's own name, stored in `sessions.host`. Process chains seen on Windows 10 (nearest
first):

- Visual Studio's Terminal: `claude.exe`, `powershell.exe`, `DevHub.exe`, `devenv.exe`, `explorer.exe`
- VS Code's terminal: `claude.exe`, `powershell.exe`, `Code.exe`, `Code.exe`, `explorer.exe`
- a standalone PowerShell window: `claude.exe`, `powershell.exe`, then whatever launched the window
- not seen, not tried yet: Windows Terminal (`WindowsTerminal.exe`, `OpenConsole.exe`) and cmd.

Esc runs no hook (Stop doesn't fire on an interrupt), so a session would stay "working" and callers would get hold
music until its next turn. Claude Code writes the interrupt to the session's transcript at once
(`~/.claude/projects/<project>/<session id>.jsonl`, a user message starting `[Request interrupted by user`), so the
daemon checks the end of the transcript before holding a call and when listing sessions. A Notification hook on
`idle_prompt` was tried first: it never fired in VS Code's terminal.

**Permissions.** The PreToolUse hook (`phone hook pre-tool`) answers "allow" in two cases:
- `call_<name>` and `notify_<name>` change no files, so they need no allow rule. That goes for a subagent's call
  too: the hook handles `pre-tool` before it ignores subagent events. Changing settings and `transfer_call` still ask.
- `EnterPlanMode`, but only when the session's latest prompt was typed in by phone (the injection log and the
  UserPromptSubmit hook's prompt match). Its approval prompt would otherwise wait in a terminal nobody is watching.
  Typed at the keyboard it asks as usual, and `ExitPlanMode`, which approves the plan, always asks.

In plan mode Claude Code asks before any hook or allow rule runs, so the hook can't help there. What does is the
tools' MCP annotation: `call_`, `notify_` and `phone_settings` carry `read_only_hint` (`mcp_server.READ_ONLY`), and
plan mode lets read-only tools run without a prompt. Ringing a phone isn't "read only" in a strict sense, but it
changes no files or settings, which is what plan mode guards. Don't remove the hint to be precise: plan mode would
then stop every call for approval.

## 5. Timeouts

There are two recording limits.

- **Answers to Claude's call (`record_seconds`, 60 s).** Claude Code moves MCP calls that run longer than 120 s to
  the background (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`), and `call_<owner>`'s client gives up at 115 s. The budget is
  ring 25 s + question ~5-10 s + answer 60 s + transcription ~5 s, so keep `ring_seconds + record_seconds` at 90 or
  less. Longer answers would need that Claude Code setting raised.
- **Messages you dial in (`message_seconds`, 5 min).** Dial-in, all-hands and voicemail replies are typed into
  sessions afterwards, so no tool call waits on them. Transcription is what limits them: ~5 s per minute of audio
  with `small.en` on a 6-core CPU (5 minutes took ~27 s), and the whisper timeout grows with the recording. The
  caller hears silence while it runs. Hanging up after # is fine, as the message is still sent. The line counts as
  busy until then.
- Recordings of at least 30 s get a double beep 10 s before the cut: `uuid_displace … mux` mixes a tone into what
  the caller hears without stopping `record`. `record` takes only the caller's side, but a speakerphone's mic can
  pick the beep up, so `stt` drops whisper's sound tags (`[BEEP]`, `(beeping)`) from transcripts.

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

## 8. Meeting alerts (ICS feeds)

The Mac version read the Calendar app (EventKit). Windows has no shared calendar store to read, so meetings come
from the ICS feed that Google Calendar and Outlook publish (`claude_phone/diary.py`).

- **Recurrence and time zones** are `icalendar` + `recurring-ical-events`: RRULE, EXDATE and moved occurrences
  (RECURRENCE-ID) are where a hand-written parser goes wrong. Outlook names zones the Windows way ("Pacific Standard
  Time") with a VTIMEZONE in the feed, which `icalendar` turns into a real zone. Floating times count as the PC's.
- **Google ignores "if modified":** its feeds come with no ETag or Last-Modified and `no-store`, so every refresh
  downloads the whole feed. It's sent gzipped on request (120 KB → 12 KB for a holiday calendar), and a sha256 of
  the body skips the parse when nothing changed. Only the days around now are kept from a parse.
- **Declined meetings:** Google keeps them in the feed with your ATTENDEE at `PARTSTAT=DECLINED`, and names a
  primary calendar after its owner's address, which is how "you" is found without `calendar_email`. Outlook removes
  declined meetings from the calendar itself.
- **The link is a password** (a secret token in the URL), so it lives in Credential Manager, is read without echo,
  and is replaced by `<link>` in any error message.
- `pip install -e .` while claude-phone runs fails halfway: `phone.exe` is held open by the daemon and the MCP
  servers, and pip has already removed the old install by then. Stop it first (`phone down` and close Claude
  sessions), or install the extra's libraries by name.

## 9. Speech

- **whisper.cpp:** whisper-server keeps the model loaded. Live repo and branch names are passed as the prompt, or
  names like "lemon stand" come back as near-misses. 8 kHz phone audio is fine. On a 6-core CPU without a GPU
  (Ryzen 5 5500, `whisper-blas-bin-x64`): `small.en-q5_1` takes ~1.8 s per answer, `large-v3-turbo` ~8.4 s and
  `base.en` ~0.55 s, so `small.en` is the default.
- **Every voice renders to a 16 kHz mono WAV**, cached by a hash of engine, voice and text. Any voice but Windows'
  falls back to the Windows voice if it fails, so a call is never silent. `phone voice` switches between them.
- **Windows voices.** Windows has two sets: System.Speech's ("Microsoft Zira Desktop") and OneCore's ("Microsoft
  Mark"), which System.Speech can't see. `phone voice` lists both. A System.Speech voice is written straight to
  16 kHz; a OneCore one comes as WAV at its own rate and is converted when it isn't 16 kHz already.
- **The Windows worker.** One PowerShell process per daemon (or `phone` command) loads System.Speech once and
  renders a sentence per JSON request, so the text is data and never part of the script. Starting PowerShell for
  each sentence cost 0.34-0.45 s; through the worker a sentence takes ~0.02 s after the first (~0.4 s, as it
  starts). It stops after 10 minutes unused. A voice it doesn't know makes it look at the installed voices again
  (one installed since it started), and a OneCore failure leaves the default voice speaking (logged in
  `logs\voiceworker-windows.log`). If the worker itself is down (won't start, dies, or doesn't answer within 20 s
  at first and 30 s after), a one-off script per sentence speaks for the next 5 minutes. An error it answers
  with (a bad output path, say) is raised as it is: the script would fail the same way.
- **Piper and Kokoro** (optional, downloaded by `phone voice`) speak from a Python worker process each, with their
  packages under `~/.claude-phone/engines`, outside the venv. Loading a model takes a while (measured 6.4 s Piper,
  2.3 s Kokoro) and holds 150-250 MB, so a worker keeps it until 10 minutes unused, then exits to give the memory
  back. Each model speaks "Ready." once into nothing as it loads: the first inference is several times slower.
- **onnxruntime, which both use,** takes every core and spins its threads between steps by default, which made the
  start of a call stutter. The workers give it half the physical cores, sleeping when idle, at below-normal
  priority: Kokoro then renders at 0.45× real time instead of 0.36×, still well ahead of playback.
- **A sentence at a time.** A call's speech is split into sentences (a short one is kept with the next) and played
  as each is rendered, so a long message starts at once instead of after the whole render (Kokoro kept a caller
  waiting ~20 s for 50 seconds of speech). The greeting is a part of its own. While Piper or Kokoro is still
  loading, nothing plays until the first sentence is ready too, which the ringing mostly hides.
- **Greetings rendered ahead.** Every minute the daemon renders each live session's greeting and the meeting
  whisper if they aren't cached, without the Windows fallback, so a broken voice shows in the log. One that fails
  waits 10 minutes before it's tried again, rather than holding up a call's speech every minute.
- **ElevenLabs** is optional and needs a key (`phone voice key`); `elevenlabs_v3` acts out tags like
  "[whispers]".

## 10. Choppy or slow voice

The daemon isn't in the audio path: each phrase is rendered to a WAV first, and FreeSWITCH streams it to the phone
itself. So a voice that stutters (gaps, repeats) or drags (slows down, then catches up) comes from FreeSWITCH, the
network or the phone, or from the PC's CPU scheduling.

- **What `phone up` does about it.** FreeSWITCH starts at high priority, so its 20 ms packet clock isn't starved,
  and whisper-server below normal, with `OMP_WAIT_POLICY=PASSIVE` so its threads sleep after a transcription instead
  of spinning. Measured on a Ryzen 5 5500: without it, whisper kept about one core busy for ~0.5 s after each answer,
  just as the reply starts playing; with it, nothing.
- **Read the per-call line.** At every hangup `phone.log` gets one line per leg:
  `audio 201 outbound 3f2a9c1e: codec=PCMU/8000 write=PCMU in_pkts=… in_skip=… in_jitter=… in_flaws=… mos=…
  jitter_var=… out_pkts=… out_skip=…`.
  - `codec`: `G722/16000` is wideband; `PCMU/8000` means the phone has G.722 off and FreeSWITCH resamples every
    phrase.
  - `in_*` and `mos` describe audio *from* the phone, as FreeSWITCH received it. They hint at how the network and
    the phone's clock behave; how our voice sounds at the phone also depends on the phone's own jitter buffer.
    `mos` (FreeSWITCH's estimate) runs from 1 to 4.5; below ~4 is usually audible.
  - `in_skip`/`out_skip` count packets FreeSWITCH skipped, and `in_jitter` the packets in its jitter statistics;
    none of these is network loss as such. A rising `out_skip` suggests FreeSWITCH fell behind its send clock, so
    look at CPU load.
- **Linphone on the same PC.**
  1. Enable G.722 (Preferences → Audio → Codecs). It's off by default, so calls fall back to PCMU.
  2. Linphone's adaptive jitter buffer (`audio_adaptive_jitt_comp_enabled=1`, `audio_jitt_comp=60` in
     `%LOCALAPPDATA%\linphone\linphonerc`) stretches audio when packets arrive unevenly, which sounds like
     dragging. Only if the voice still drags: close Linphone, then set `audio_adaptive_jitt_comp_enabled=0` and/or
     raise `audio_jitt_comp`.
  3. Linphone shows its own codec, jitter and loss figures during a call (the call's statistics view).
- **"Reverse Clock Skew Detected"** in `freeswitch/log/freeswitch.log` means the Windows clock jumped under
  FreeSWITCH's timer. If those lines line up with stutters, look at Windows time sync.
- **A desk phone** (the T31P, G.722 first) avoids most of this: no jitter buffer competing on the same PC and no
  resampling.

## 11. Editor extensions and autostart

The Visual Studio extension only lists sessions (a tool window that polls the daemon's `GET /sessions` every 15 s);
it types nothing. Typing into Visual Studio's Terminal is the daemon's console route, with or without the extension.

Both extensions start claude-phone when an editor opens, if `autostart` is on in `config.toml`: they run
`phone.exe up` without waiting for it. They find `phone.exe` through `~/.claude-phone/phone.json`, which `phone up`
and `phone config set autostart on` write (only when `phone.exe` sits next to the venv's `python.exe`). Visual Studio
loads the extension at launch because its `LoadedWhen` is always true, solution or not
(`ClaudePhoneExtension.cs`); VS Code runs it when a window activates.

Two editors, or two windows, opening together would start the services twice, so `phone up` takes
`~/.claude-phone/run/up.lock` (created exclusively, treated as stale after 300 s, touched during the model download)
and the second one prints "another `phone up` is starting claude-phone" and leaves (`cli.py`).

## 12. The installer

`install.ps1` and `phone setup` (`wizard.py`) exist so nobody follows the README by hand. They detect the real state
every time and remember only what you declined (`setup.json`), so a run on a finished install asks nothing.

- **Why winget, an MSI and a zip.** Python and ffmpeg come from winget (`Python.Python.3.12`, `Gyan.FFmpeg`).
  FreeSWITCH isn't on winget: its MSIs are listed at files.freeswitch.org and signed by SignalWire, which is the only
  thing to check them against (no checksum is published), so the wizard asks Windows to verify the Authenticode
  signature before running it elevated. whisper.cpp's Windows build is a zip on the `b<number>` build tags (the release
  marked "latest" has no files); GitHub lists a sha256 for each asset and the zip is checked against it. Only
  `whisper-server.exe` and its DLLs are unpacked (the zip holds about 40 other programs). The speech model is checked
  against the sha256 Hugging Face gives in the redirect of its `resolve` URL.
- **FreeSWITCH's service.** The MSI may register a Windows service that holds the SIP port. The elevated install step
  stops it and sets it to Manual, because `phone up` runs FreeSWITCH itself with its own config.
- **Why a merge with a backup.** `settings.json` is the user's: setup adds the five hooks after showing the change,
  keeps every other key and hook, writes `settings.json.bak` first, and writes nothing when the hooks are there
  already. A file that isn't valid JSON is left alone. Claude can't do this itself (its auto-mode classifier blocks
  edits to its own settings), so it is the wizard, in the user's own terminal. The CLAUDE.md snippet sits between
  `<!-- claude-phone -->` markers, so a re-run replaces only that block; a hand-pasted copy is noticed and not
  duplicated without asking. The SIP password is shown only when asked for.
- **Editors are independent.** VS Code is found through `code --list-extensions --show-versions`, Visual Studio through
  `vswhere` plus the `extension.vsixmanifest` of each installed extension (under
  `%LOCALAPPDATA%\Microsoft\VisualStudio\<version>_<instance id>\Extensions`). Adding one editor's extension later
  touches nothing else. The `.vsix` comes from the GitHub Release for this clone's version (checked against GitHub's
  sha256), or is built locally. The Visual Studio extension's manifest names only the Community edition, so other
  editions are skipped with a message. Visual Studio must be closed to install an extension; the wizard asks until it
  is. The versions are compared with trailing zeros ignored (the manifest says `1.1.0.0`, the project `1.1.0`).
- **`phone update`.** An editable install picks up new code by itself, so pip runs only when `pyproject.toml` changed,
  through the venv's python. It refuses while Claude sessions run: each one's `phone.exe mcp` holds `phone.exe` open.
  (A test in a throwaway venv showed pip can replace a running console script, by renaming it aside with a warning,
  but that was one measurement.) The services are stopped before pip and started again afterwards in a fresh process,
  whatever pip did.
