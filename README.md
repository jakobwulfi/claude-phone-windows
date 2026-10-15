# claude-phone for Windows

A desk phone (or a softphone) becomes the line between you and your Claude Code sessions.

This is a Windows port of [risingpower/claude-phone](https://github.com/risingpower/claude-phone), which runs on
macOS. [Watch the original in action](https://www.instagram.com/reel/DeY8JPmszXs/).

> **Work in progress.** Typing into sessions, the voices and speech to text work on Windows. Call control on
> FreeSWITCH (which replaces Asterisk) is being built, so calls can't be placed yet. Progress and the plan:
> [docs/windows-port.md](docs/windows-port.md).

- **Claude rings you:** a session calls the `call_<name>` MCP tool (`call_sam` for `owner_name = "Sam"`). The phone rings with `repo/branch` as caller ID; the spoken answer comes back as the tool result.
- **Claude announces:** `notify_<name>` rings, reads a short announcement and hangs up.
- **Transfers:** `transfer_call` puts you through to another session when you ask the wrong one.
- **You ring Claude:** every live session has an extension (301-399). Dial it, speak, hang up; the words are typed into that session's VS Code terminal and submitted.
- **Voicemail:** unanswered questions light the message LED. Press Message, answer each one; answers are typed into the asking sessions.

Everything runs locally on one PC: FreeSWITCH (the phone exchange), whisper.cpp (speech to text), Windows voices
or ElevenLabs (text to speech), a small Python daemon, and a VS Code extension that types into terminals. Your voice
never leaves the PC; the only things sent out are text to ElevenLabs, if you choose it, and missed-call pushes, if
you set them up.

## What you need

- Windows 10 or 11 and VS Code. Claude Code must run in **VS Code's terminal**: the Claude Code panel and the
  desktop app can't be typed into.
- Python 3.12+, Node.js LTS (to build the extension), ffmpeg, whisper.cpp and FreeSWITCH. About 1 GB of disk for
  the speech model.
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
   `whisper-server.exe`. The first `phone up` downloads the speech model (~550 MB).
3. **FreeSWITCH:** being ported (Phase 4 of [docs/windows-port.md](docs/windows-port.md)).
4. **Python:** `py -3.13 -m venv .venv`, then `.venv\Scripts\pip install -e .`
5. **VS Code extension:**
   ```powershell
   cd vscode-extension; npm install; npm run package
   code --install-extension claude-phone-0.1.0.vsix
   ```
   Reload VS Code. A phone icon in the status bar shows it's running; click it to list the window's terminals.
6. **Config:** copy `config.example.toml` to `%USERPROFILE%\.claude-phone\config.toml` and set `owner_name`.
   Set `ring_ext = "201"` to test on Linphone.
7. **Secrets** live in Windows Credential Manager (service `claude-phone`): `phone secret sip` prints the SIP
   password for the phones. ElevenLabs: `ELEVENLABS_API_KEY`, or `.venv\Scripts\python -m keyring set claude-phone elevenlabs`.
8. **Claude Code wiring** (below), and add [docs/claude-md-snippet.md](docs/claude-md-snippet.md) to `~/.claude/CLAUDE.md`.
9. **Try it:** start `claude` in two VS Code terminals. `phone list` shows them as 301 and 302, and
   `phone type 301 "what is 2+2"` types into the first one while you work in the other.

### Claude Code wiring

Four hooks in `%USERPROFILE%\.claude\settings.json` (replace the path with your clone; merge into an existing
`"hooks"` key):

```json
"hooks": {
  "SessionStart":     [{ "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "session-start"] }] }],
  "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "session-end"] }] }],
  "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "prompt"] }] }],
  "Stop":             [{ "hooks": [{ "type": "command", "command": "C:/path/to/claude-phone-windows/.venv/Scripts/phone.exe", "args": ["hook", "stop"] }] }]
}
```

The MCP server (the `call_<name>` tools) is registered once calls work:
`claude mcp add --scope user claude-phone -- C:\path\to\claude-phone-windows\.venv\Scripts\phone.exe mcp`

### Voices

Calls use the Windows voices (System.Speech): free, offline, no account. `phone voices` lists the installed
ones; set `sapi_voice` (and `operator_voice` for the operator on 0) in config.toml to a name from that list.
`phone say "hello"` renders a phrase and prints where the audio went.

For nicer voices, use ElevenLabs:

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
phone's screen. Meeting alerts (`meeting_alerts`) have no calendar source on Windows yet; an ICS feed is planned.

### Missed-call pushes (ntfy)

Install the free ntfy app, run `phone push` and subscribe to the topic it prints (treat it as a password), then
`phone push test`. A `call_<name>` that goes to voicemail, or a `notify_<name>` that isn't heard, then pushes to the
app. Pushes pass through the public ntfy.sh server; set `push_text = false` in config.toml to send only who called.

## Running

There's no start-at-logon yet; `phone up` starts everything.

```powershell
phone up               # whisper-server and the daemon (each also: phone up daemon)
phone status           # processes and daemon health
phone list             # live sessions and extensions
phone type 301 "text"  # type into a session as a phone message would
phone vm               # pending voicemail questions
phone restart daemon   # after config changes
phone down
Get-Content -Wait $HOME\.claude-phone\logs\phone.log   # calls, transcripts, every injection
```

`phone` is `.venv\Scripts\phone.exe`.

## Files

| Path | What |
|---|---|
| `~/.claude-phone/registry.db` | sessions, voicemails, injection log (SQLite) |
| `~/.claude-phone/logs/phone.log` | the log to tail |
| `~/.claude-phone/logs/{whisper,daemon}.out` | process output |
| `~/.claude-phone/bridges/` | one file per VS Code window: how the daemon reaches its terminals |
| `~/.claude-phone/sounds/` | TTS cache |

## Tests

`.venv\Scripts\pip install -e .[dev]`, then `.venv\Scripts\pytest`. They run against a throwaway home, a fake call
and a fake VS Code bridge: no phone exchange, audio, whisper or VS Code needed. The extension's own tests:
`npm test` in `vscode-extension`.

## Troubleshooting

- **Text not typed:** the session must be in `phone list`, and its window must show the phone icon in the status
  bar. Sessions started before the hooks or the extension were installed have no extension: restart `claude`.
- **`whisper-server not found`:** put its folder on PATH or set `whisper_server` in config.toml.
- **Wrong words for repo names:** whisper is prompted with live repo and branch names; check `phone list` spelling.

[docs/build-notes.md](docs/build-notes.md) explains why the original was built the way it is (written for the Mac version).

## Credits

Built by [@risingpower](https://www.instagram.com/risingpower/) with Claude Code. Windows port by
[@jakobwulfi](https://github.com/jakobwulfi), also with Claude Code.

## License

MIT. See [LICENSE](LICENSE).
