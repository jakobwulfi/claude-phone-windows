# Changelog

claude-phone, the VS Code extension and the Visual Studio extension have their own version numbers
(`pyproject.toml`, `vscode-extension/package.json`, `visualstudio-extension/ClaudePhone.Extension/*.csproj`). A release
tag is `v<claude-phone version>`; its notes say which extension versions go with it. Visual Studio only replaces an
installed extension with a higher version, so raise the Visual Studio extension's version whenever its code changes.

## Unreleased

### Added

- `install.ps1`: installs from a clone in one go (finds or installs Python 3.12, makes `.venv`, installs claude-phone)
  and hands over to `phone setup`. Run again, it updates and shows what is set up. Without a clone,
  `irm https://raw.githubusercontent.com/jakobwulfi/claude-phone-windows/main/install.ps1 | iex` installs Git if
  needed, clones into `%USERPROFILE%\claude-phone-windows` and runs it from there.
- `phone setup [--all]`: shows what is set up, then asks only about what is missing: settings, which phone rings
  (Linphone, desk phone or both, with the firewall rules for a desk phone), FreeSWITCH and whisper-server (found,
  downloaded and installed, or linked by path), the speech model, the SIP password, the Claude Code wiring, and the
  editor extensions. Safe to repeat; it remembers only what you declined.
- Claude Code wiring from one place (`claudewire.py`): the five hooks are merged into `settings.json` after showing
  the change (with a `.bak`), plan mode is optional, the MCP server is registered with `claude mcp add`, the
  `/claude-phone` command gets this clone's paths, and the CLAUDE.md snippet is written between
  `<!-- claude-phone -->` markers so a re-run replaces only its own block.
- `phone update`: pulls the newest claude-phone (not while Claude sessions run), restarts what was running, installs
  new libraries only when `pyproject.toml` changed, and offers to update older editor extensions.
- `phone status` also warns when the pre-tool hook's matcher leaves out the call and notify tools, and when Claude Code
  has no claude-phone MCP server (or one pointing at another `phone.exe`).
- The editor extensions install independently of each other and of the core: each is found, compared by version, and
  put in from the GitHub Release for this version, or built locally when there is none.
- CI on a Windows runner (pytest, ruff, the VS Code extension's tests, the Visual Studio extension's logic tests) and
  a release workflow: push a `v<version>` tag to publish a Release with both `.vsix` files.

### Changed

- The speech model download is checked against the sha256 Hugging Face publishes for it, and shows progress.
- README and build notes: autostart is "when an editor opens", Windows Terminal is described as untested, the files
  and commands that were missing are listed, and there is a section on the editor extensions and autostart.
