# claude-phone for Windows

A desk phone (or a softphone) becomes the line between you and your Claude Code sessions.

This is a Windows port of [risingpower/claude-phone](https://github.com/risingpower/claude-phone), which runs on
macOS. [Watch the original in action](https://www.instagram.com/reel/DeY8JPmszXs/).

> **Status:** every call flow works end to end on Windows, on a Yealink T31P desk phone
> ([docs/t31p-setup.md](docs/t31p-setup.md)) and on Linphone: dialling in, Claude calling you, voicemail and the
> message light (desk phone only), hold music, the operator, all-hands, announcements, transfers, screen messages,
> the DND key and per-repo ringtones.

- **Claude rings you:** a session calls the `call_<name>` MCP tool (`call_sam` for `owner_name = "Sam"`). The phone rings with `repo/branch` as caller ID; the spoken answer comes back as the tool result.
- **Claude announces:** `notify_<name>` rings, reads a short announcement and hangs up.
- **Transfers:** `transfer_call` puts you through to another session when you ask the wrong one.
- **You ring Claude:** every live session has an extension (301-399). Dial it, speak, hang up; the words are typed into that session's terminal and submitted, after `[By phone]` so Claude knows they're a transcript and may be misheard here and there.
- **Voicemail:** unanswered questions light the message LED. Press Message, answer each one; answers are typed into the asking sessions.
- **How long you can talk:** up to 5 minutes when you dial in (a session, all-hands or a voicemail reply), and up
  to 60 s when answering Claude's call. A double beep warns you 10 s before the end; press # to finish early.

Everything runs locally on one PC: FreeSWITCH (the phone exchange), whisper.cpp (speech to text), Windows voices
or ElevenLabs (text to speech), a small Python daemon, and optional VS Code and Visual Studio extensions. Your voice
never leaves the PC. The only things that go out are text to ElevenLabs, if you choose it (also for
`phone hold-music`, which asks ElevenLabs Music for a tune), missed-call pushes, if you set them up, and, with meeting
alerts, requests to your calendars' feed links (secret links, kept in Credential Manager).

## What you need

- Windows 10 or 11. To dial in to a session, Claude Code must run in a **terminal**: VS Code's, Visual Studio's
  Terminal window, a plain PowerShell or cmd window, or Windows Terminal (it uses the same console route as
  PowerShell and cmd, but hasn't been tried yet). Nothing can submit a prompt to the Claude
  Code chat panel or the desktop app from outside; sessions there can still call you. With the Claude Code VS Code
  extension, set `"claudeCode.useTerminal": true` so it opens sessions in a terminal.
  Not covered: Git Bash in its own window (mintty isn't a Windows console), WSL, SSH, and `claude -p` (it never
  reads the terminal). Run claude-phone and the terminal at the same privilege level: a terminal started as
  administrator can't be typed into by a daemon that isn't (the log says so), and the daemon shouldn't run elevated.
- Python 3.12+ (the installer offers to get it), and Git to clone the repo. FreeSWITCH, whisper.cpp and ffmpeg are
  installed by setup if you don't have them. About 200 MB of disk for the speech model. Node.js LTS and the .NET SDK
  are only for building the editor extensions yourself, which a release download makes unnecessary.
- A phone: [Linphone](https://linphone.org) (free) on the same PC, or a SIP desk phone. It's tested on a Yealink
  T31P ([docs/t31p-setup.md](docs/t31p-setup.md)); a T30 should behave the same but is untested.
- No accounts or subscriptions. ElevenLabs (nicer voices) and ntfy (missed-call pushes) are optional.

## Dial plan

| Ext | What |
|---|---|
| 301-399 | live Claude Code sessions (`phone list`). Mid-task, you get hold music until its turn ends; # leaves your message now |
| 200 | desk phone |
| 201 | Linphone (testing) |
| 0 | operator: say who you want ("the backend on the AGS branch") and it puts you through |
| 9 | all-hands: your message is typed into every live session |
| 500 | voicemail (the desk phone's Message key) |
| 600 | echo test |
| 601 | 1 kHz tone |

On any recording: `#` ends it, hanging up also ends it. In voicemail: `#` during a question skips it, `7` deletes it.

## Setup

Clone the repo and run the installer in PowerShell:

```powershell
git clone https://github.com/jakobwulfi/claude-phone-windows
cd claude-phone-windows
.\install.ps1
```

If PowerShell says running scripts is disabled (the default on a fresh Windows), run
`powershell -ExecutionPolicy Bypass -File .\install.ps1` instead.

It finds Python 3.12 or newer (and offers to install it with winget), makes `.venv`, installs claude-phone and hands
over to `phone setup`. Setup shows what is set up and what isn't, then asks only about what's missing, and you can say
no to any of it:

1. **Your settings:** your name (it becomes the tool names, `call_sam` for "Sam"), how long the phone rings, and a few
   more. Change them later with `phone config`.
2. **Which phone rings:** Linphone on this PC, a desk phone, or both. For Linphone it tells you the account settings;
   for a desk phone it opens the firewall (one administrator prompt) and points at [docs/t31p-setup.md](docs/t31p-setup.md).
3. **FreeSWITCH, whisper.cpp, ffmpeg and the speech model:** found if they're there, otherwise downloaded and
   installed after you say yes (FreeSWITCH's installer needs an administrator prompt and is checked against its
   signature; whisper.cpp and the model against their published checksums).
4. **The Claude Code wiring:** the hooks, the MCP server, the `/claude-phone` command and a block in your CLAUDE.md
   telling Claude when to phone you. It shows each change before making it; see [Claude Code wiring](#claude-code-wiring).
5. **Editor extensions:** for VS Code and Visual Studio, each on its own. They only add the session list and autostart
   inside the editor; sessions in any terminal work without them.
6. **Start it,** then dial **600** for the echo test, or start `claude` in two terminals: `phone list` shows them as
   301 and 302 with where each runs, and `phone type 301 "what is 2+2"` types into the first one while you work in
   the other.

Run `.\install.ps1` again whenever you like: it pulls a newer claude-phone (not while Claude sessions run), then shows
the table again. `.\install.ps1 -All` asks about everything again, including what you declined. To do it all by hand:
[docs/manual-setup.md](docs/manual-setup.md).

**The editor extensions.** In VS Code, the phone icon in the status bar shows how many sessions claude-phone knows
(`off` when it isn't running). Click it for the list, grouped by where each session runs, with its extension; pick one
to show its terminal. The Visual Studio extension only lists the sessions: its button is under Tools → claude-phone and
View → Other Windows, and the toolbar under View → Toolbars (it needs Visual Studio 17.14 or newer, Community edition;
[visualstudio-extension/README.md](visualstudio-extension/README.md)). With `autostart` on, either one starts
claude-phone when the editor opens.

### Claude Code wiring

`phone setup` writes all of this for you, after showing the change and keeping `settings.json.bak`; this is what it
writes and why, for doing it by hand or checking it. Five hooks in `%USERPROFILE%\.claude\settings.json` (replace the
path with your clone; merge into an existing `"hooks"` key). PreToolUse lets Claude call or message you, and enter plan
mode when you asked by phone, without a prompt (the plan-mode part of the matcher is optional; details in
[build-notes](docs/build-notes.md)). `phone status` warns about any of these hooks that are missing, a matcher that
leaves out the call tools, and a missing MCP server.

```json
"hooks": {
  "PreToolUse":       [{ "matcher": "EnterPlanMode|mcp__claude-phone__(call|notify)_.*", "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "pre-tool"] }] }],
  "SessionStart":     [{ "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "session-start"] }] }],
  "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "session-end"] }] }],
  "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "prompt"] }] }],
  "Stop":             [{ "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "stop"] }] }]
}
```

The MCP server (the `call_<name>` tools) is registered with
`claude mcp add --scope user claude-phone -- C:\path\to\claude-phone-windows\.venv\Scripts\phone.exe mcp`. Setup also
adds the `/claude-phone` command (from [claude/commands/claude-phone.md](claude/commands/claude-phone.md), with your
paths filled in) and the block from [docs/claude-md-snippet.md](docs/claude-md-snippet.md) in `~/.claude/CLAUDE.md`,
between `<!-- claude-phone -->` markers so a re-run replaces only that block.

### Voices

Calls use the Windows voices: free, offline, no account. `phone voice` lists the voices on this PC (System.Speech's
and the newer OneCore ones, such as Mark) and the ones that can be added. Nothing extra is downloaded until you ask:

| Engine | Cost | On disk | Notes |
|---|---|---|---|
| `windows` | free, offline | built in | the default; can't be deleted |
| `piper` | free, offline | engine 152 MB once, then 20-120 MB per voice | ~170 MB RAM while speaking |
| `kokoro` | free, offline | 250 MB for all 54 voices | the most natural; ~260 MB RAM while speaking |
| `elevenlabs` | paid per character | nothing | online; needs your API key |

```text
phone voice                          # what Claude speaks with now, and what's available
phone voice list piper en_GB         # an engine's voices, filtered
phone voice piper:en_GB-alan-medium  # switch; asks before downloading, then switches
phone voice try kokoro:bf_emma       # hear one on this PC's speakers first
phone voice remove piper:en_GB-alan-medium
phone voice key                      # store the ElevenLabs key in Windows Credential Manager
```

A switch takes effect on the next thing Claude says, with no restart. You can also ask Claude, or use
`/claude-phone voice …`; Claude asks you before any download and never handles the API key. Piper and Kokoro run in
a separate process that stops after 10 idle minutes, so they hold no memory between calls. Deleting the voice in use
switches back to the Windows default first, and deleting the last Piper voice deletes the engine too. The
downloads live in `~/.claude-phone/engines/` and `~/.claude-phone/voices/`. The Piper engine is GPL-3 licensed;
it's downloaded on request, not bundled.

For an ElevenLabs key: make an account at elevenlabs.io, open Profile → API Keys, create a key with the
**Text to Speech** and **Voices: Read** permissions, then run `phone voice key` and paste it.

The operator on 0 keeps its own voice (`operator_voice`), and so can the meeting whisper (`whisper_voice`), each
as `engine:name`, e.g.

```toml
operator_voice = "elevenlabs:r1KmysJdVYZjJCm4mL3b"   # Jessica, the voice in the original demo clips
```

`phone say "hello"` renders a phrase and prints where the audio went. Older voice settings (`tts_provider`,
`sapi_voice`, `elevenlabs_voice_id`, `operator_provider`, `whisper_provider`) still work; `phone status` notes them,
and `phone config migrate` (or the next `phone config set` or `phone voice`) writes them in this form.

Speech is cached, so repeated phrases cost nothing. If a voice fails (an engine that won't start, ElevenLabs without
a key), that call falls back to the Windows default voice rather than going silent. Each repo can have its own voice
(`repo_voices` in [config.example.toml](config.example.toml)).

### The phone's screen and meetings (optional)

With `screen_push = true` and a Yealink desk phone, every call and announcement also leaves a short summary on the
phone's screen.

**Meeting alerts** read your calendar's ICS feed. Shortly before each meeting (`meeting_warn_minutes`, default 2)
the phone whispers a warning on its speaker, shows the meeting, and switches DND on. Claude's calls are held
(voicemail, no pushes) until the meeting ends, or until you switch DND off. All-day, cancelled and declined events,
events marked "free", and anything over 24 hours (trips, out of office) are ignored, and a meeting in both
calendars counts once. Times come from each event's own time zone and are shown in this PC's.

1. Install the calendar libraries (about 1.5 MB, pure Python, and only loaded when meeting alerts are on):
   `.venv\Scripts\python -m pip install "icalendar>=7" "recurring-ical-events>=3.8" tzdata`. On a fresh setup,
   `pip install -e .[calendar]` (instead of `-e .` in Setup step 4) does the same. Don't rerun that one while claude-phone or Claude Code
   is running: `phone.exe` is in use and the reinstall fails halfway.
2. Copy your calendar's secret link:
   - **Google Calendar:** Settings → your calendar → Integrate calendar → **Secret address in iCal format**.
   - **Outlook:** Settings → Calendar → Shared calendars → Publish a calendar → *Can view all details* → **ICS**
     link. Some work accounts don't allow publishing.
3. Run `phone calendar add google` (or `outlook`) in your own terminal and paste the link. The link is a password
   (anyone with it can read your calendar), so it's kept in Credential Manager and never shown or logged.
4. Turn the alerts on and pick the calendars (or ask Claude to; see [Settings](#settings)):
   ```powershell
   phone config set meeting_alerts on
   phone config set calendar_sources google      # or outlook, or google,outlook
   phone restart daemon
   ```
   If your calendar isn't named after your address, set `calendar_email = "you@example.com"` in config.toml so
   meetings you declined are skipped.

`phone meetings` lists the next two days as the alerts see them, and `phone calendar` shows each feed's state.
Feeds are re-read every `calendar_refresh_minutes` (default 5). Google and Outlook also take a while to update
their feeds, so a meeting booked a few minutes ahead can be missed.

### Missed-call pushes (ntfy)

Install the free ntfy app, run `phone push` and subscribe to the topic it prints (treat it as a password), then
`phone push test`. A `call_<name>` that goes to voicemail, or a `notify_<name>` that isn't heard, then pushes to the
app. Pushes pass through the public ntfy.sh server; set `push_text = false` in config.toml to send only who called.

## Running

By default nothing starts by itself. Start claude-phone when you want it: type `/claude-phone` in Claude Code (also
`/claude-phone down`, `restart` or `status`), or run `phone up`. Or turn on `phone config set autostart on`: then
the VS Code and Visual Studio extensions run `phone up` when an editor opens and claude-phone isn't running. The
services keep running after the Claude session or editor window that started them ends.

```powershell
phone up               # FreeSWITCH, whisper-server and the daemon (each also: phone up daemon)
phone status           # processes, daemon health, and whether each phone is registered
phone list             # live sessions and extensions
phone type 301 "text"  # type into a session as a phone message would
phone vm               # pending voicemail questions
phone restart daemon   # after config changes
phone down
phone setup            # what's set up and what isn't, and asks about the rest (--all: ask about everything again)
phone update           # pull the newest claude-phone and restart it (not while Claude sessions run)
Get-Content -Wait $HOME\.claude-phone\logs\phone.log   # calls, transcripts, every injection
```

`phone` is `.venv\Scripts\phone.exe`. A few more commands, each with `--help`:

```powershell
phone prerender        # render the fixed phrases in the current voice
phone meeting-test     # preview a meeting alert (whisper and screen)
phone hold-music       # generate new hold music with ElevenLabs Music
phone calendar remove google   # forget a calendar link (also: outlook; add and remove take a source)
phone secret esl       # print the FreeSWITCH event-socket password (also: sip)
phone config render    # rewrite the FreeSWITCH config, then phone restart freeswitch
```

## Settings

Everything lives in `%USERPROFILE%\.claude-phone\config.toml` (see `config.example.toml`). The everyday ones can
also be changed without opening it, in three ways:

| Setting | What | Allowed |
|---|---|---|
| `calendar_sources` | the calendars the meeting alerts read | `google`, `outlook`, or both separated by a comma |
| `meeting_alerts` | warn before meetings and hold Claude's calls during them | on or off |
| `meeting_warn_minutes` | how many minutes before a meeting the warning comes | 1-30 minutes |
| `callerid_max` | the longest caller ID (repo/branch) shown on the phone | 10-40 characters |
| `owner_name` | your name: spoken on calls; the tools become `call_<name>` and `notify_<name>` | up to 40 letters, digits, spaces, dots, apostrophes and dashes |
| `ring_seconds` | how long Claude's calls ring before voicemail | 10-40 seconds, at most 90 minus `record_seconds` |
| `whisper_text` | what the meeting whisper says | up to 120 characters |
| `autostart` | start claude-phone when an editor opens (false: only with `phone up` or /claude-phone) | on or off |

- **Command line:** `phone config` lists them with their values; `phone config get <setting>`,
  `phone config set <setting> <value>`, `phone config unset <setting>` (back to the default).
- **Slash command:** `/claude-phone config …` with the same words, e.g. `/claude-phone config set callerid_max 30`.
- **Chat:** ask Claude, e.g. "warn me 3 minutes before meetings". It uses the `change_phone_setting` tool, which
  Claude Code asks you to allow.

**Which phone rings:** `phone change_phone desk` or `phone change_phone softphone` (also
`/claude-phone change_phone …`, or ask Claude); `phone change_phone` alone shows which one rings now.

Nothing restarts by itself. Each change says what it needs: usually `phone restart daemon`; a new `owner_name`
also renames the tools, which Claude Code sessions pick up when they're started again. A change writes
config.toml again with a comment on each line saying what that setting is for, so comments of your own aren't
kept. Settings that aren't recognised (typos) stay in the file, marked as such, and are also listed by
`phone config` and logged when the daemon starts.

## Files

| Path | What |
|---|---|
| `~/.claude-phone/registry.db` | sessions, voicemails, injection log, and whether the daemon switched the phone's DND on (SQLite) |
| `~/.claude-phone/logs/phone.log` | the log to tail |
| `~/.claude-phone/logs/{freeswitch,whisper,daemon}.out` | process output |
| `~/.claude-phone/logs/voiceworker-windows.log` | the Windows voices' PowerShell worker (Piper and Kokoro log to `voiceworker-<engine>.log` too) |
| `~/.claude-phone/logs/visualstudio-extension.log` | what the Visual Studio extension did at startup and since |
| `~/.claude-phone/freeswitch/` | generated FreeSWITCH config (from `freeswitch/freeswitch.xml.tmpl` by `phone up` or `phone config render`), its log and database |
| `~/.claude-phone/recordings/` | what was said on calls: deleted once transcribed; kept 1 day if transcription failed or heard nothing |
| `~/.claude-phone/bridges/` | one file per VS Code window: how the daemon reaches its terminals |
| `~/.claude-phone/sounds/` | hold music, the beep, and `tts/`: Claude's speech, cached 7 days (a waiting voicemail's question is kept until answered). `phone status` shows the audio's size |
| `~/.claude-phone/models/` | whisper's speech model, downloaded by the first `phone up` |
| `~/.claude-phone/engines/`, `voices/` | the optional voices `phone voice` adds: each engine's Python packages, and its voice files |
| `~/.claude-phone/run/` | the services' pid files |
| `~/.claude-phone/phone.json` | where `phone.exe` is, for the editor extensions' autostart |
| `~/.claude-phone/setup.json` | what you declined in `phone setup` (so it doesn't ask again; `--all` does) |
| `~/.claude-phone/downloads/` | installers `phone setup` is fetching; each is deleted once installed |
| `~/.claude-phone/engines/whisper.cpp/` | whisper-server, when setup installed it for you |

## Tests

`.venv\Scripts\pip install -e .[dev]`, then `.venv\Scripts\pytest`. They run against a throwaway home, a fake call
and a fake VS Code bridge: no phone exchange, whisper or VS Code needed. One test speaks a few sentences with the
Windows voices, into files in its temp folder. The extensions' own tests: `npm test` in `vscode-extension`, and
`dotnet test visualstudio-extension\ClaudePhone.Core.Tests`. Typing into a real console (the part that needs a
terminal with `claude` in it) is checked by hand.

Coverage: `.venv\Scripts\pytest --cov=claude_phone --cov-report=term-missing` lists, per file, the share of lines
the tests run and the lines they never reach.

Lint: `.venv\Scripts\ruff check claude_phone tests` (rules in `pyproject.toml`; it doesn't reformat anything).

## Troubleshooting

- **Text not typed:** the session must be in `phone list`. In VS Code its window must show the phone icon in the
  status bar. Sessions started before the hooks were installed have no extension: restart `claude`. In any other
  terminal `phone type 301 "hello"` prints why it failed: `claude isn't running in that terminal` (it exited, or
  something else runs there now), or `that console is elevated` (run the terminal and claude-phone at the same level).
- **A session isn't listed:** only a `claude` started by hand in a terminal registers. Not `claude -p`, not one that
  another `claude` started, and not Git Bash's own window, WSL or SSH.
- **A session in Visual Studio's Terminal isn't in the list:** start `claude` again so the hooks register it, check
  `phone list`, and read `~/.claude-phone/logs/visualstudio-extension.log` (the extension's own log).
- **`whisper-server not found`:** run `phone setup` (it installs or links it), or put its folder on PATH or set
  `whisper_server` in config.toml.
- **`ffmpeg not found`:** run `phone setup`, or `winget install Gyan.FFmpeg` and open a new terminal.
- **Linphone times out registering:** Linphone on the same PC holds UDP 5060 and resets any change to it, so
  FreeSWITCH's replies go astray. Set `sip_port = 5070`, run `phone restart freeswitch`, and use
  `<LAN address>:5070` as Linphone's domain.
- **Phone won't register or calls fail:** FreeSWITCH's log is `~/.claude-phone/freeswitch/log/freeswitch.log`;
  `phone status` shows whether each phone is registered. A desk phone also needs `scripts\firewall.ps1`
  ([docs/t31p-setup.md](docs/t31p-setup.md)).
- **Status bar says `off` while claude-phone runs:** set `claudePhone.daemonPort` in VS Code to your `daemon_port`.
- **Wrong words for repo names:** whisper is prompted with live repo and branch names; check `phone list` spelling.

[docs/build-notes.md](docs/build-notes.md) explains why it's built the way it is, and what each piece needed on Windows.

## Credits

Built by [@risingpower](https://www.instagram.com/risingpower/) with Claude Code. Windows port by
[@jakobwulfi](https://github.com/jakobwulfi), also with Claude Code.

## License

MIT. See [LICENSE](LICENSE).
