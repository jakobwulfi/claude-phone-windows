# Setting up by hand

`.\install.ps1` and `phone setup` do all of this and ask before each step (see the README). This page is for when you
want to do it yourself, or to see what the wizard does. Each step is safe to repeat.

1. **Tools** (open a new terminal afterwards so they're on PATH):
   ```powershell
   winget install --id Python.Python.3.12 -e --source winget
   winget install Gyan.FFmpeg
   ```
   Python 3.12 or newer. ffmpeg converts the call audio. Node.js LTS is only needed to build the VS Code extension
   yourself, and the .NET SDK only for the Visual Studio one.
2. **whisper.cpp:** from its [releases](https://github.com/ggml-org/whisper.cpp/releases). The Windows builds aren't on
   the release marked "latest" but on the `b<number>` build tag published next to each version. Take
   `whisper-bin-x64.zip` (CPU) or a `cuda` build (NVIDIA) and unzip it, for example to `C:\tools\whisper`. The files are
   in its `Release` folder: put that folder on PATH, or set `whisper_server` in config.toml to the full path of
   `whisper-server.exe`. The first `phone up` downloads the speech model (`small.en`, about 190 MB; about 2 s per
   answer on a 6-core CPU; see `whisper_model` in config.example.toml for faster or more accurate ones).
3. **FreeSWITCH:** the Windows MSI (1.11.x) from the
   [FreeSWITCH install page](https://developer.signalwire.com/freeswitch/FreeSWITCH-Explained/Installation/Windows-Install_1966780)
   (they're also listed at `https://files.freeswitch.org/windows/installer/x64/`). If it adds a "FreeSWITCH" Windows
   service, stop it and set it to Manual: `phone up` runs FreeSWITCH itself, with its own config (two SIP accounts,
   nothing else) under `~/.claude-phone/freeswitch`, and a service would hold the SIP port first. Allow it through
   Windows Firewall on private networks when asked. If it isn't in `C:\Program Files\FreeSWITCH`, set `freeswitch_exe`
   in config.toml.
4. **Python:** from the clone, `py -3 -m venv .venv`, then `.venv\Scripts\pip install -e .`
5. **Config:** copy `config.example.toml` to `%USERPROFILE%\.claude-phone\config.toml` and set `owner_name`. Or run
   `phone config set owner_name Sam`; `phone config` lists the settings it can change.
   To test on Linphone on the same PC: `ring_ext = "201"` and `sip_port = 5070` (Linphone keeps 5060 for itself).
   Then in Linphone add a third-party SIP account: username `201`, password from `phone secret sip`, domain
   `<your LAN address>:5070` (`phone status` shows the address in its registrations), UDP. Dial **600**: you should
   hear yourself. For a desk phone, give the PC a DHCP reservation, run `scripts\firewall.ps1` from an administrator
   PowerShell, and follow [t31p-setup.md](t31p-setup.md).
6. **Secrets** live in Windows Credential Manager (service `claude-phone`): `phone secret sip` prints the SIP password
   for the phones. ElevenLabs: `ELEVENLABS_API_KEY`, or `phone voice key`.
7. **Claude Code wiring:** the five hooks and the MCP server as described under "Claude Code wiring" in the README,
   plus [claude-md-snippet.md](claude-md-snippet.md) in `~/.claude/CLAUDE.md`. For the `/claude-phone` command, copy
   [claude/commands/claude-phone.md](../claude/commands/claude-phone.md) to `%USERPROFILE%\.claude\commands\` and replace
   `C:/path/to/claude-phone-windows` in it with your clone. (`phone status` tells you what is missing.)
8. **Editor extensions** (each optional, and independent of the other and of everything above):
   - *VS Code:* `cd vscode-extension; npm install; npm run package`, then
     `code --install-extension claude-phone-<version>.vsix`, and reload VS Code. The phone icon in the status bar shows
     how many sessions claude-phone knows (`off` when it isn't running).
   - *Visual Studio* (17.14 or newer, Community edition; only lists the sessions): with the .NET SDK,
     `dotnet build visualstudio-extension\ClaudePhone.Extension -c Release`, close Visual Studio, open the `.vsix` in
     `bin\Release\net8.0-windows8.0`, and start Visual Studio again. See
     [visualstudio-extension/README.md](../visualstudio-extension/README.md).
9. **Try it:** `phone up`, then start `claude` in two terminals (VS Code, Visual Studio's Terminal, PowerShell or cmd,
   in any mix; Windows Terminal should work too but is untested). `phone list` shows them as 301 and 302 with where
   each runs, and `phone type 301 "what is 2+2"` types into the first one while you work in the other.

To bring an install up to date: `phone update` (or `.\install.ps1` again).
