# claude-phone for Visual Studio

A phone button in Visual Studio. It opens a small window listing the Claude Code sessions claude-phone knows
about, grouped by where each runs (Visual Studio's Terminal, PowerShell, Windows Terminal, VS Code...), with each
session's extension and status and a line on the phone system. It refreshes every 15 seconds while open.

It only displays. The daemon types phone messages into a session in Visual Studio's Terminal window itself, by writing
key events into that console, so nothing here needs a token or a port of its own. It reads the daemon's
`GET /sessions` on `127.0.0.1`, on the `daemon_port` in `~/.claude-phone/config.toml` (8765 if there isn't one).

Where it appears after you install it: the **claude-phone** toolbar (View → Toolbars), **Tools → claude-phone** and
**View → Other Windows → claude-phone**. Needs Visual Studio 2022 17.14 or newer, including Visual Studio 2026.
It can't focus a session's terminal tab: Visual Studio has no API for that, so the list is read-only.

## Build, test, install

You need the .NET SDK (8 or newer); the Visual Studio "extension development" workload is not needed to build.

```powershell
cd visualstudio-extension
dotnet test ClaudePhone.Core.Tests                         # the logic: parsing, grouping, the daemon client
dotnet build ClaudePhone.Extension -c Release              # bin\Release\net8.0-windows8.0\ClaudePhone.Extension.vsix
```

Close Visual Studio, then open the `.vsix` (or run `VSIXInstaller.exe` from Visual Studio's `Common7\IDE` folder
on it) and start Visual Studio again.

## Layout

- `ClaudePhone.Core` is plain .NET: parsing and checking the daemon's answer, grouping, wording, finding the port. It
  mirrors `vscode-extension/src/sessions.ts`, and the tests live in `ClaudePhone.Core.Tests`.
- `ClaudePhone.Extension` is the thin Visual Studio part: the command and toolbar, the tool window and its XAML. It is
  built on VisualStudio.Extensibility (out of process), which is why it can run in Visual Studio 2022 and 2026 alike.
- `ClaudePhone.Extension/Images/Phone.*.png` is the button's icon, in the sizes Visual Studio asks for.
