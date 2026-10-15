# claude-phone for VS Code

Lets the claude-phone daemon type phone messages into the Claude Code sessions running in this window's terminals,
even when the terminal isn't focused.

Each VS Code window starts a small server on `127.0.0.1` with a random port and token. It writes both to
`~/.claude-phone/bridges/<window>.json` so the daemon can find it, and removes the file when the window closes.
Requests from browsers are refused, and typed text has control characters stripped, so only plain text reaches
the terminal.

The phone icon in the status bar shows the bridge is running. Click it to list this window's terminals and
their process IDs.

Build: `npm install`, then `npm test` and `npm run package`. Install the resulting `.vsix` from the Extensions
view (… → Install from VSIX).
