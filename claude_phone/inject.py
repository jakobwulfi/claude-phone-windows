"""Type text into a Claude Code session's terminal (works with the tab unfocused and another app in front).
Two routes. A VS Code terminal is typed into by the claude-phone VS Code extension: each window runs a small localhost
server and leaves a bridge file with its port and token in ~/.claude-phone/bridges. Any other Windows console
(Visual Studio's Terminal, Windows Terminal, PowerShell, cmd) is typed into by consoleinject, which writes key events
into the console itself. Only ever types into sessions from the registry; every attempt is logged."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass

import psutil

from . import config, log, registry

BRIDGE_TIMEOUT_S = 3
CONSOLE_TIMEOUT_S = 15  # python starting up, the 0.3 s before Enter, and a slow antivirus scan
TERM_PREFIX = "vscode:"
CONSOLE_PREFIX = "con:"  # con:<shell pid>:<its creation time in ms>; the time keeps a recycled pid from matching
# Localhost only: never route these requests through a configured HTTP proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# The process that owns a console is its shell. Lower-case image names. Not bash.exe: Git Bash's default terminal
# (mintty) isn't a Windows console, so typing into it would report "sent" and reach nothing. Claude Code's own Bash
# tool also runs under bash.exe, one more reason not to take it for a session's terminal.
SHELLS = {"pwsh.exe": "PowerShell", "powershell.exe": "PowerShell", "cmd.exe": "Command Prompt", "nu.exe": "Nushell"}
VSCODE_LIKE = {"code.exe", "code - insiders.exe", "cursor.exe", "windsurf.exe", "vscodium.exe"}
DESKTOP_APP_DIRS = ("anthropicclaude", "windowsapps")  # where the Claude desktop app installs (Squirrel, MSIX)
# What a shell runs inside, found by walking up its parents (DevHub.exe, between Visual Studio and its terminal,
# isn't listed and is passed over).
HOSTS = {"devenv.exe": "Visual Studio", "code.exe": "VS Code", "code - insiders.exe": "VS Code Insiders",
         "cursor.exe": "Cursor", "windsurf.exe": "Windsurf", "vscodium.exe": "VSCodium",
         "windowsterminal.exe": "Windows Terminal"}
DEFAULT_HOST = "Terminal"
# One console at a time: two messages typed into the same one at once would mix.
_console_lock = threading.Lock()


@dataclass(frozen=True)
class Bridge:
    """One VS Code window's extension server."""
    port: int
    token: str


def bridges() -> list[Bridge]:
    """The bridges of running VS Code windows. Files left behind by a window that crashed are removed."""
    found = []
    for f in sorted(config.BRIDGES.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            pid, port, token = int(d["pid"]), int(d["port"]), str(d["token"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not psutil.pid_exists(pid):
            f.unlink(missing_ok=True)
            continue
        found.append(Bridge(port, token))
    return found


def _request(bridge: Bridge, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{bridge.port}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {bridge.token}", "Content-Type": "application/json"},
    )
    with _OPENER.open(req, timeout=BRIDGE_TIMEOUT_S) as r:
        return json.loads(r.read())


def _terminal_pids(strict: bool) -> tuple[set[int], bool]:
    """Every VS Code terminal's shell pid, and whether every window answered. strict: a window that doesn't answer
    raises instead of being skipped."""
    pids: set[int] = set()
    complete = True
    for b in bridges():
        try:
            pids.update(int(t["pid"]) for t in _request(b, "GET", "/terminals")["terminals"])
        except Exception:
            if strict:
                raise
            complete = False
    return pids, complete


# --- console terminals ---

def _name(p: psutil.Process) -> str:
    try:
        return p.name().lower()
    except psutil.Error:
        return ""


def _console_id(shell: psutil.Process) -> str:
    return f"{CONSOLE_PREFIX}{shell.pid}:{int(shell.create_time() * 1000)}"


def _parse_console_id(term_id: str | None) -> tuple[int, int] | None:
    if not term_id or not term_id.startswith(CONSOLE_PREFIX):
        return None
    try:
        pid, created = term_id.removeprefix(CONSOLE_PREFIX).split(":")
        return int(pid), int(created)
    except ValueError:
        return None


def _process(pid: int) -> psutil.Process | None:
    """None for a pid that isn't running or isn't a pid at all (ids come from callers)."""
    try:
        return psutil.Process(pid)
    except (psutil.Error, ValueError, OverflowError):
        return None


def _live_shell(term_id: str) -> psutil.Process | None:
    """The shell a console id names, if that very process is still running."""
    parsed = _parse_console_id(term_id)
    p = _process(parsed[0]) if parsed else None
    try:
        return p if p and int(p.create_time() * 1000) == parsed[1] and _name(p) in SHELLS else None
    except psutil.Error:
        return None


def _is_claude(p: psutil.Process) -> bool:
    name = _name(p)
    try:
        if name == "claude.exe":  # Claude Code's, not the Claude desktop app's (also claude.exe)
            return not any(d in p.exe().lower() for d in DESKTOP_APP_DIRS)
        # an npm install runs it under node: match the package, not any script with "claude" in its path
        return name == "node.exe" and any("claude-code" in a.lower() or "@anthropic-ai" in a.lower()
                                          for a in p.cmdline())
    except psutil.Error:
        return False


def _headless(p: psutil.Process) -> bool:
    """claude -p (and the SDK) never read the console: text typed there would wait for the shell instead."""
    try:
        return any(a in ("-p", "--print") for a in p.cmdline()[1:])
    except psutil.Error:
        return False


def _runs_claude(shell: psutil.Process) -> bool:
    """An interactive claude is running somewhere under this shell."""
    try:
        return any(_is_claude(c) and not _headless(c) for c in shell.children(recursive=True))
    except psutil.Error:
        return False


def accepts(term_id: str | None) -> bool:
    """False for an id that names no running shell with claude under it: the daemon's API takes ids from callers,
    and an invented one must not become a place to type into."""
    if not isinstance(term_id, str):
        return False
    vscode = _shell_pid(term_id)
    if vscode is not None:
        shell = _process(vscode)
    elif term_id.startswith(CONSOLE_PREFIX):
        shell = _live_shell(term_id)
    else:
        return True  # no way to type into it
    return shell is not None and _runs_claude(shell)


def _live_console_ids() -> set[str]:
    ids = set()
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() in SHELLS:
            try:
                ids.add(_console_id(p))
            except psutil.Error:
                pass  # gone while we looked
    return ids


def _session_shell() -> psutil.Process | None:
    """The shell of the interactive claude this process runs under. Shells below claude (Claude Code's Bash tool, a
    `cmd /c` around a hook) aren't it; neither is the shell of an outer claude when this one was started by it, which
    would take over the outer session's console; and a claude in print mode has no console to type into. With no
    claude above at all (not run by Claude Code), the nearest shell."""
    ancestors = psutil.Process().parents()
    shell = None
    seen_claude = False
    for p in ancestors:
        if _is_claude(p):
            if shell is not None:
                return None  # a claude above the shell we found: this session was started by another one
            if _headless(p):
                return None
            seen_claude = True
        elif _name(p) in SHELLS and seen_claude and shell is None:
            shell = p
    if shell is None and not seen_claude:
        shell = next((p for p in ancestors if _name(p) in SHELLS), None)
    return shell


def _in_vscode(shell: psutil.Process) -> bool:
    try:
        return any(_name(p) in VSCODE_LIKE for p in shell.parents())
    except psutil.Error:
        return False


def host_of(term_id: str | None) -> str | None:
    """Where a terminal runs, for the session list: the nearest known app above its shell (Visual Studio, VS Code,
    Windows Terminal), else the shell's own name."""
    vscode = _shell_pid(term_id)
    parsed = _parse_console_id(term_id)
    pid = vscode if vscode is not None else (parsed[0] if parsed else None)
    shell = _process(pid) if pid is not None else None
    if shell is None:
        return "VS Code" if vscode is not None else None
    try:
        for parent in shell.parents():
            if host := HOSTS.get(_name(parent)):
                return host
    except psutil.Error:
        pass
    return "VS Code" if vscode is not None else SHELLS.get(_name(shell), DEFAULT_HOST)


# --- ids ---

def live_term_ids() -> set[str]:
    """Raises if a VS Code window doesn't answer, so the caller can skip a sweep rather than drop its sessions."""
    pids, _ = _terminal_pids(strict=True)
    return {f"{TERM_PREFIX}{pid}" for pid in pids} | _live_console_ids()


def _ancestor_pids() -> list[int]:
    return [p.pid for p in psutil.Process().parents()]


def terminal_id() -> str | None:
    """The terminal this process runs in: a VS Code terminal's shell when a bridge knows it, else the nearest
    ancestor that is a shell. Hooks and the MCP server are descendants of `claude`, which runs in that shell."""
    try:
        terminals, complete = _terminal_pids(strict=False)
        vscode = next((f"{TERM_PREFIX}{pid}" for pid in _ancestor_pids() if pid in terminals), None)
        if vscode:
            return vscode
        shell = _session_shell()
        if shell is None or (not complete and _in_vscode(shell)):
            return None  # a VS Code window that didn't answer may own it: a console id now would be a new tab tomorrow
        return _console_id(shell)
    except Exception as e:
        log.get("inject").warning("terminal lookup failed: %r", e)
        return None


def _shell_pid(term_id: str | None) -> int | None:
    if not term_id or not term_id.startswith(TERM_PREFIX):
        return None
    try:
        return int(term_id.removeprefix(TERM_PREFIX))
    except ValueError:
        return None


# --- typing ---

def _type_vscode(pid: int, text: str) -> str:
    """Ask each window in turn; the one that owns the terminal types into it."""
    error = None
    for b in bridges():
        try:
            result = _request(b, "POST", "/type", {"pid": pid, "text": text})["result"]
        except Exception as e:
            error = f"error:{e}"
            continue
        if result != "notfound":
            return result
    return error or "notfound"


def _run_helper(pid: int, created_ms: int, text: str) -> tuple[str, str, int] | None:
    """(stdout, stderr, exit code) of the console helper, or None if it timed out. The venv's python.exe is a launcher
    that starts the real interpreter, so a timeout kills the whole tree: a helper left running could type later."""
    helper = subprocess.Popen(
        [sys.executable, "-I", "-m", "claude_phone.consoleinject", str(pid), str(created_ms)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        out, err = helper.communicate(text.encode("utf-8"), timeout=CONSOLE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            tree = psutil.Process(helper.pid)
            for p in [*tree.children(recursive=True), tree]:
                p.kill()
        except psutil.Error:
            pass
        helper.communicate()
        return None
    return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), helper.returncode


def _type_console(term_id: str, text: str) -> str:
    # Under the lock, so the check is not older than the typing: a message queued behind another would otherwise
    # be checked up to CONSOLE_TIMEOUT_S before it is typed. (The helper checks once more before Enter.)
    with _console_lock:
        shell = _live_shell(term_id)
        if shell is None:
            return "notfound"
        if not _runs_claude(shell):
            # Claude was killed without its SessionEnd hook: Enter would run the text as a shell command.
            return "error:claude isn't running in that terminal"
        done = _run_helper(shell.pid, int(shell.create_time() * 1000), text)
    if done is None:
        return "error:typing into the console timed out"
    out, err, code = done
    answer = " ".join(out.split())
    if answer in ("sent", "notfound") or answer.startswith("error:"):
        return answer
    return f"error:the console helper failed (exit {code}): {err[-200:]}"


def _type(term_id: str | None, text: str) -> str:
    vscode_pid = _shell_pid(term_id)
    if vscode_pid is not None:
        return _type_vscode(vscode_pid, text)
    if _parse_console_id(term_id):
        return _type_console(term_id, text)
    return "error:no terminal id"


def type_into(session: registry.Session, text: str, source: str) -> str:
    """Returns 'sent', 'notfound' or 'error:<msg>'."""
    lg = log.get("inject")
    # A single line: newlines inside would submit early. No leading / ! or #: Claude Code would run the words as a
    # slash command or a shell line. (A leading space doesn't help: the extension trims it.)
    text = re.sub(r"^[/!#\s]+", "", " ".join(text.split()))
    if not text:
        result = "error:empty"
    else:
        try:
            result = _type(session.term_id, text)
        except Exception as e:
            result = f"error:{e}"
    registry.log_injection(session, source, text, result)
    lg.info("inject ext=%s %s source=%s result=%s text=%r", session.ext, session.label, source, result, text)
    return result
