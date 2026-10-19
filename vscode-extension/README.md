# claude-phone for VS Code

Lets the claude-phone daemon type phone messages into the Claude Code sessions running in this window's terminals,
even when the terminal isn't focused.

Each VS Code window starts a small server on `127.0.0.1` with a random port and token. It writes both to
`~/.claude-phone/bridges/<window>.json` so the daemon can find it, and removes the file when the window closes.
Requests from browsers are refused, and typed text has control characters stripped, so only plain text reaches
the terminal.

The phone icon in the status bar shows how many Claude Code sessions claude-phone knows about (`off` when the
daemon isn't running). Click it to list them with their extensions, grouped into this window, other VS Code
windows and the other places sessions run (Visual Studio, Windows Terminal, PowerShell...), with the phone's
registration in the title and each session's host beside its status. Pick a session in this window to bring its
terminal forward; for one elsewhere, the message says where it runs. Sessions outside VS Code are typed into by the
daemon itself, so this extension only lists them.

The list comes from the daemon's `GET /sessions` on `127.0.0.1`. If you changed `daemon_port` in
`~/.claude-phone/config.toml`, set `claudePhone.daemonPort` to match.

Only sessions running in a terminal can take phone calls. Sessions in the Claude Code chat panel can still call
you, but you can't dial in to them: set `claudeCode.useTerminal` to `true` to open new Claude Code sessions in a
terminal instead.

Build: `npm install`, then `npm test` and `npm run package`. Install the resulting `.vsix` from the Extensions
view (… → Install from VSIX).
