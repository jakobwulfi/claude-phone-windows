"""Claude Code's side of claude-phone: the five hooks in settings.json, the MCP registration, the /claude-phone
command and the CLAUDE.md block. `phone status` checks them and `phone setup` writes them from here, so there is one
list of what belongs where. Hooks run on every prompt, so this imports only the standard library.

Claude itself can't write settings.json (the auto-mode classifier blocks it): this runs in the user's own terminal.
Every write keeps what the user already has: settings.json is merged, with a .bak first, and the CLAUDE.md block sits
between markers, so a second run changes nothing and a changed snippet replaces only its own block."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config

# Claude Code event -> `phone hook` argument.
HOOKS = {"SessionStart": "session-start", "SessionEnd": "session-end", "UserPromptSubmit": "prompt",
         "Stop": "stop", "PreToolUse": "pre-tool"}
# PreToolUse only fires for these: the call/notify tools run without a prompt, and EnterPlanMode too (only when the
# latest prompt came by phone, which the hook checks). The plan-mode part is optional.
PLAN_MATCHER = "EnterPlanMode"
CALL_MATCHER = "mcp__claude-phone__(call|notify)_.*"
MCP_NAME = "claude-phone"
BLOCK_START, BLOCK_END = "<!-- claude-phone -->", "<!-- /claude-phone -->"
COMMAND_PLACEHOLDER = "C:/path/to/claude-phone-windows"  # in claude/commands/claude-phone.md
CLAUDE_TIMEOUT_S = 30  # `claude mcp get` health-checks the server, about 3 s


class SettingsError(ValueError):
    """settings.json exists but isn't something we can merge into safely."""


@dataclass(frozen=True)
class SettingsChange:
    settings: dict
    notes: list[str] = field(default_factory=list)  # one line per change, for showing before writing


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def settings_file() -> Path:
    return claude_dir() / "settings.json"


def read_settings(path: Path | None = None) -> dict:
    """The user's settings.json; {} when there is none. SettingsError when it can't be read as a JSON object."""
    path = path or settings_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise SettingsError(f"can't read {path}: {e}") from e
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as e:
        raise SettingsError(f"{path} isn't valid JSON ({e}); fix it first, nothing was changed") from e
    if not isinstance(data, dict):
        raise SettingsError(f"{path} doesn't hold a JSON object; nothing was changed")
    return data


def _entries(settings: dict, event: str) -> list[dict]:
    hooks = settings.get("hooks")
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    return [e for e in entries or [] if isinstance(e, dict)]


def _runs(entry: dict, arg: str) -> bool:
    return any(isinstance(h, dict) and (arg in (h.get("args") or []) or f"hook {arg}" in str(h.get("command", "")))
               for h in entry.get("hooks") or [])


def _settings_or_empty() -> dict:
    try:
        return read_settings()
    except SettingsError:
        return {}


def missing_hooks(settings: dict | None = None) -> list[str]:
    """The `phone hook` arguments whose event Claude Code's user settings don't run them for."""
    settings = _settings_or_empty() if settings is None else settings
    return [arg for event, arg in HOOKS.items() if not any(_runs(e, arg) for e in _entries(settings, event))]


def _covers_calls(matcher: str) -> bool:
    return matcher.strip() in ("", "*") or CALL_MATCHER in matcher


def weak_matcher(settings: dict | None = None) -> bool:
    """True when the pre-tool hook is wired but its matcher leaves out the call/notify tools (they'd need approval)."""
    settings = _settings_or_empty() if settings is None else settings
    entries = [e for e in _entries(settings, "PreToolUse") if _runs(e, HOOKS["PreToolUse"])]
    return bool(entries) and not any(_covers_calls(str(e.get("matcher") or "")) for e in entries)


def hook_command(exe: Path | str, arg: str) -> dict:
    return {"type": "command", "command": Path(exe).as_posix(), "args": ["hook", arg]}


def _wanted_matcher(current: str, plan_mode: bool) -> str:
    """`current` with the call tools (and plan mode, if wanted) added, keeping any parts the user has."""
    if current.strip() in ("", "*"):
        return current
    parts = [PLAN_MATCHER] if plan_mode and PLAN_MATCHER not in current else []
    parts.append(current)
    if CALL_MATCHER not in current:
        parts.append(CALL_MATCHER)
    return "|".join(parts)


def plan_settings(settings: dict, exe: Path | str, *, plan_mode: bool = True) -> SettingsChange:
    """`settings` with claude-phone's five hooks added where they're missing. Nothing else is touched, and when it all
    is wired already the notes are empty."""
    new = copy.deepcopy(settings)
    hooks = new.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError('the "hooks" key in settings.json isn\'t an object; nothing was changed')
    notes = []
    for event, arg in HOOKS.items():
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise SettingsError(f'the "{event}" hooks in settings.json aren\'t a list; nothing was changed')
        ours = next((e for e in entries if isinstance(e, dict) and _runs(e, arg)), None)
        if ours is None:
            entry = {"hooks": [hook_command(exe, arg)]}
            if event == "PreToolUse":
                entry = {"matcher": f"{PLAN_MATCHER}|{CALL_MATCHER}" if plan_mode else CALL_MATCHER, **entry}
            entries.append(entry)
            notes.append(f"{event}: add `phone hook {arg}`"
                         + (f" (matcher {entry['matcher']})" if event == "PreToolUse" else ""))
        elif event == "PreToolUse":
            current = str(ours.get("matcher") or "")
            wanted = _wanted_matcher(current, plan_mode)
            if wanted != current:
                ours["matcher"] = wanted
                notes.append(f"{event}: matcher `{current}` becomes `{wanted}`")
    return SettingsChange(new, notes) if notes else SettingsChange(settings)


def apply_settings(change: SettingsChange, path: Path | None = None) -> bool:
    """Write the change, keeping the old file as settings.json.bak. False when there was nothing to change."""
    if not change.notes:
        return False
    path = path or settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(change.settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True


# --- the MCP server ---

def _run_claude(*args: str) -> subprocess.CompletedProcess | None:
    """`claude <args>`; None when Claude Code isn't on PATH or doesn't answer."""
    claude = shutil.which("claude")
    if not claude:
        return None
    try:
        return subprocess.run([claude, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=CLAUDE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a.strip())) == os.path.normcase(os.path.normpath(b.strip()))


def mcp_state(exe: Path | str) -> str:
    """'ok', 'missing', 'other-path' (registered, but to another phone.exe) or 'unknown' (can't ask Claude Code)."""
    done = _run_claude("mcp", "get", MCP_NAME)
    if done is None:
        return "unknown"
    if done.returncode != 0:
        return "missing"
    command = re.search(r"^\s*Command:\s*(.+?)\s*$", done.stdout, re.MULTILINE)
    args = re.search(r"^\s*Args:\s*(.+?)\s*$", done.stdout, re.MULTILINE)
    if command and _same_path(command.group(1), str(exe)) and args and args.group(1).split() == ["mcp"]:
        return "ok"
    return "other-path"


def register_mcp(exe: Path | str) -> tuple[bool, str]:
    """Register this install's MCP server for every project, replacing one that points elsewhere."""
    state = mcp_state(exe)
    if state == "ok":
        return True, "already registered"
    if state == "unknown":
        return False, "Claude Code (`claude`) isn't on PATH or didn't answer"
    if state == "other-path":
        _run_claude("mcp", "remove", MCP_NAME, "--scope", "user")
    done = _run_claude("mcp", "add", "--scope", "user", MCP_NAME, "--", str(exe), "mcp")
    if done is None or done.returncode != 0:
        detail = (done.stderr or done.stdout).strip() if done else "no answer"
        return False, f"`claude mcp add` failed: {detail}"
    return True, "registered" if state == "missing" else "registered again, now for this install"


# --- the /claude-phone command and the CLAUDE.md block ---

def command_text(exe: Path | str, repo: Path) -> str:
    source = (repo / "claude" / "commands" / "claude-phone.md").read_text(encoding="utf-8")
    source = source.replace(f"{COMMAND_PLACEHOLDER}/.venv/Scripts/phone.exe", Path(exe).as_posix())
    return source.replace(COMMAND_PLACEHOLDER, repo.as_posix())


def _write_if_changed(path: Path, text: str) -> str:
    """'written', 'updated' or 'unchanged'."""
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return "written" if old is None else "updated"


def install_command(exe: Path | str, repo: Path | None = None) -> str:
    return _write_if_changed(claude_dir() / "commands" / "claude-phone.md", command_text(exe, repo or config.REPO))


def claude_md_block(owner_name: str, owner_slug: str, repo: Path | None = None) -> str:
    """docs/claude-md-snippet.md with Sam and sam replaced by this owner, between the markers."""
    text = ((repo or config.REPO) / "docs" / "claude-md-snippet.md").read_text(encoding="utf-8")
    found = re.search(r"```markdown\n(.*?)\n```", text, re.DOTALL)
    if not found:
        raise ValueError("docs/claude-md-snippet.md has no ```markdown block")
    body = re.sub(r"\bSam\b", lambda _: owner_name, found.group(1))
    body = re.sub(r"(?<![A-Za-z])sam(?![A-Za-z])", lambda _: owner_slug, body)
    return f"{BLOCK_START}\n{body}\n{BLOCK_END}\n"


def install_claude_md(owner_name: str, owner_slug: str, repo: Path | None = None) -> str:
    """Put the block in ~/.claude/CLAUDE.md: replacing the old block if there is one, else after what's there."""
    path = claude_dir() / "CLAUDE.md"
    block = claude_md_block(owner_name, owner_slug, repo)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END) + r"\n?", re.DOTALL)
    if pattern.search(existing):
        text = pattern.sub(lambda _: block, existing, count=1)
    else:
        separator = "" if not existing else "\n" if existing.endswith("\n") else "\n\n"
        text = existing + separator + block
    return _write_if_changed(path, text)
