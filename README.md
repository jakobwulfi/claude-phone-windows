# claude-phone for Windows

A desk phone (or a softphone) becomes the line between you and your Claude Code sessions.

This is a Windows port of [risingpower/claude-phone](https://github.com/risingpower/claude-phone), which runs on
macOS. [Watch the original in action](https://www.instagram.com/reel/DeY8JPmszXs/).

> **Status:** every call flow works end to end on Windows with Linphone as the phone: dialling in, Claude
> calling you, voicemail, hold music, the operator, all-hands, announcements and transfers. Next: a Yealink T31P
> desk phone ([docs/t31p-setup.md](docs/t31p-setup.md)) and its message light, which Linphone doesn't show.

- **Claude rings you:** a session calls the `call_<name>` MCP tool (`call_sam` for `owner_name = "Sam"`). The phone rings with `repo/branch` as caller ID; the spoken answer comes back as the tool result.
- **Claude announces:** `notify_<name>` rings, reads a short announcement and hangs up.
- **Transfers:** `transfer_call` puts you through to another session when you ask the wrong one.
- **You ring Claude:** every live session has an extension (301-399). Dial it, speak, hang up; the words are typed into that session's VS Code terminal and submitted.
- **Voicemail:** unanswered questions light the message LED. Press Message, answer each one; answers are typed into the asking sessions.
- **How long you can talk:** up to 5 minutes when you dial in (a session, all-hands or a voicemail reply), and up
  to 60 s when answering Claude's call. A double beep warns you 10 s before the end; press # to finish early.

Everything runs locally on one PC: FreeSWITCH (the phone exchange), whisper.cpp (speech to text), Windows voices
or ElevenLabs (text to speech), a small Python daemon, and a VS Code extension that types into terminals. Your voice
never leaves the PC; the only things sent out are text to ElevenLabs, if you choose it, and missed-call pushes, if
you set them up.

## What you need

- Windows 10 or 11 and VS Code. To dial in to a session, Claude Code must run in **VS Code's terminal**: nothing
  can submit a prompt to the Claude Code chat panel or the desktop app from outside. Sessions there can still call
  you. With the Claude Code extension, set `"claudeCode.useTerminal": true` so it opens sessions in a terminal.
- Python 3.12+, Node.js LTS (to build the extension), ffmpeg, whisper.cpp and FreeSWITCH. About 200 MB of disk
  for the speech model.
- [Linphone](https://linphone.org) (free) as the phone while testing. A SIP desk phone later: the original was
  built on a Yealink T31P ([docs/t31p-setup.md](docs/t31p-setup.md)); a T30 should behave the same but is untested.
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

1. **Tools** (then restart VS Code so they're on PATH):
   ```powershell
   winget install --id Python.Python.3.13 -e --source winget
   winget install OpenJS.NodeJS.LTS
   winget install Gyan.FFmpeg
   ```
2. **whisper.cpp:** from its [releases](https://github.com/ggml-org/whisper.cpp/releases), the Windows builds are
   attached to the build tag published next to each version (for v1.9.5: `b5454`). Take
   `whisper-blas-bin-x64.zip` (CPU) or a `cuda` build (NVIDIA), unzip it to `C:\tools\whisper` and add
   `C:\tools\whisper\Release` to PATH, or set `whisper_server` in config.toml to the full path of
   `whisper-server.exe`. The first `phone up` downloads the speech model (`small.en`, ~190 MB; about 2 s per
   answer on a 6-core CPU, see `whisper_model` in config.example.toml for faster or more accurate ones).
3. **FreeSWITCH:** install the Windows MSI (1.11.x) from the
   [FreeSWITCH install page](https://developer.signalwire.com/freeswitch/FreeSWITCH-Explained/Installation/Windows-Install_1966780).
   If it adds a "FreeSWITCH" Windows service, set it to Manual: `phone up` runs FreeSWITCH itself, with its own
   config (two SIP accounts, nothing else) under `~/.claude-phone/freeswitch`. Allow it through Windows Firewall
   on private networks when asked.
4. **Python:** `py -3.13 -m venv .venv`, then `.venv\Scripts\pip install -e .`
5. **VS Code extension:**
   ```powershell
   cd vscode-extension; npm install; npm run package
   code --install-extension claude-phone-0.2.0.vsix
   ```
   Reload VS Code. The phone icon in the status bar shows how many sessions claude-phone knows (`off` when it isn't
   running). Click it for the list with each session's extension; pick one to show its terminal.
6. **Config:** copy `config.example.toml` to `%USERPROFILE%\.claude-phone\config.toml` and set `owner_name`.
   To test on Linphone on the same PC: `ring_ext = "201"` and `sip_port = 5070` (Linphone keeps 5060 for itself).
   Then in Linphone add a third-party SIP account: username `201`, password from `phone secret sip`, domain
   `<your LAN address>:5070` (`phone status` shows the address in its registrations), UDP. Dial **600**: you
   should hear yourself.
7. **Secrets** live in Windows Credential Manager (service `claude-phone`): `phone secret sip` prints the SIP
   password for the phones. ElevenLabs: `ELEVENLABS_API_KEY`, or `.venv\Scripts\python -m keyring set claude-phone elevenlabs`.
8. **Claude Code wiring** (below), and add [docs/claude-md-snippet.md](docs/claude-md-snippet.md) to `~/.claude/CLAUDE.md`.
   For the `/claude-phone` command, copy [claude/commands/claude-phone.md](claude/commands/claude-phone.md) to
   `%USERPROFILE%\.claude\commands\` and replace `C:/path/to/claude-phone-windows` in it with your clone.
9. **Try it:** start `claude` in two VS Code terminals. `phone list` shows them as 301 and 302, and
   `phone type 301 "what is 2+2"` types into the first one while you work in the other.

### Claude Code wiring

Five hooks in `%USERPROFILE%\.claude\settings.json` (replace the path with your clone; merge into an existing
`"hooks"` key). The `PreToolUse` one lets Claude enter plan mode without an approval prompt waiting in the terminal, but only
when you asked for it by phone; typed at the keyboard it asks as usual, and approving the plan always asks. It also
lets Claude call or message you without a prompt; changing your settings still asks. In plan mode no hook is asked,
but the call and notify tools are marked read-only, which plan mode lets through without a prompt.
`phone status` warns about any of these hooks that are missing.

```json
"hooks": {
  "PreToolUse":       [{ "matcher": "EnterPlanMode|mcp__claude-phone__(call|notify)_.*", "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "pre-tool"] }] }],
  "SessionStart":     [{ "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "session-start"] }] }],
  "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "session-end"] }] }],
  "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "prompt"] }] }],
  "Stop":             [{ "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "stop"] }] }]
}
```

The MCP server (the `call_<name>` tools) is registered once calls work:
`claude mcp add --scope user claude-phone -- C:\path\to\claude-phone-windows\.venv\Scripts\phone.exe mcp`

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

The operator on 0 keeps its own voice (`operator_voice`). `phone say "hello"` renders a phrase and prints where the
audio went.

The older settings still work when `voice` is empty: `tts_provider` with `sapi_voice` or `elevenlabs_voice_id`, e.g.

```toml
tts_provider = "elevenlabs"
elevenlabs_voice_id = "r1KmysJdVYZjJCm4mL3b"   # Jessica, the voice in the original demo clips
operator_provider = "elevenlabs"
operator_voice = "r1KmysJdVYZjJCm4mL3b"
```

Speech is cached, so repeated phrases cost nothing. If ElevenLabs fails or there's no key, that call falls back to
the Windows voice rather than going silent. Each repo can have its own voice (`repo_voices` in
[config.example.toml](config.example.toml)).

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
the VS Code extension runs `phone up` when a window opens and claude-phone isn't running. The services keep running
after the Claude session or VS Code window that started them ends.

```powershell
phone up               # FreeSWITCH, whisper-server and the daemon (each also: phone up daemon)
phone status           # processes, daemon health, and whether each phone is registered
phone list             # live sessions and extensions
phone type 301 "text"  # type into a session as a phone message would
phone vm               # pending voicemail questions
phone restart daemon   # after config changes
phone down
Get-Content -Wait $HOME\.claude-phone\logs\phone.log   # calls, transcripts, every injection
```

`phone` is `.venv\Scripts\phone.exe`.

## Settings

Everything lives in `%USERPROFILE%\.claude-phone\config.toml` (see `config.example.toml`). The everyday ones can
also be changed without opening it, in three ways:

| Setting | What | Allowed |
|---|---|---|
| `calendar_sources` | which calendars the meeting alerts read | `google`, `outlook`, or both |
| `meeting_alerts` | warn before meetings, hold Claude's calls during them | on / off |
| `meeting_warn_minutes` | how long before a meeting the warning comes | 1-30 |
| `callerid_max` | the longest caller ID (repo/branch) shown on the phone | 10-40 |
| `owner_name` | your name, spoken on calls and used in the tool names (`call_<name>`) | up to 40 characters |
| `ring_seconds` | how long Claude's calls ring before voicemail | 10-40 (ring + answer must fit in 90 s) |
| `whisper_text` | what the meeting whisper says | up to 120 characters |
| `autostart` | start claude-phone when VS Code opens (off: only by command) | on / off |

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
| `~/.claude-phone/registry.db` | sessions, voicemails, injection log (SQLite) |
| `~/.claude-phone/logs/phone.log` | the log to tail |
| `~/.claude-phone/logs/{freeswitch,whisper,daemon}.out` | process output |
| `~/.claude-phone/freeswitch/` | generated FreeSWITCH config (from `freeswitch/freeswitch.xml.tmpl` by `phone up` or `phone config render`), its log and database |
| `~/.claude-phone/recordings/` | what was said on calls: deleted once transcribed; kept 1 day if transcription failed or heard nothing |
| `~/.claude-phone/bridges/` | one file per VS Code window: how the daemon reaches its terminals |
| `~/.claude-phone/sounds/` | hold music, the beep, and `tts/`: Claude's speech, cached 7 days (a waiting voicemail's question is kept until answered). `phone status` shows the audio's size |

## Tests

`.venv\Scripts\pip install -e .[dev]`, then `.venv\Scripts\pytest`. They run against a throwaway home, a fake call
and a fake VS Code bridge: no phone exchange, audio, whisper or VS Code needed. The extension's own tests:
`npm test` in `vscode-extension`.

## Troubleshooting

- **Text not typed:** the session must be in `phone list`, and its window must show the phone icon in the status
  bar. Sessions started before the hooks or the extension were installed have no extension: restart `claude`.
- **`whisper-server not found`:** put its folder on PATH or set `whisper_server` in config.toml.
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
