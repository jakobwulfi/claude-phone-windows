"""Type text into a Claude Code session's VS Code terminal (works with the tab unfocused and another app in front).
The claude-phone VS Code extension does the typing: each window runs a small localhost server and leaves a bridge
file with its port and token in ~/.claude-phone/bridges. Only ever types into sessions from the registry; every
attempt is logged."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

import psutil

from . import config, log, registry

BRIDGE_TIMEOUT_S = 3
TERM_PREFIX = "vscode:"
# Localhost only: never route these requests through a configured HTTP proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


def _terminal_pids(strict: bool) -> set[int]:
    """Every VS Code terminal's shell pid. strict: a bridge that doesn't answer raises instead of being skipped."""
    pids: set[int] = set()
    for b in bridges():
        try:
            pids.update(int(t["pid"]) for t in _request(b, "GET", "/terminals")["terminals"])
        except Exception:
            if strict:
                raise
    return pids


def live_term_ids() -> set[str]:
    """Raises if a VS Code window doesn't answer, so the caller can skip a sweep rather than drop its sessions."""
    return {f"{TERM_PREFIX}{pid}" for pid in _terminal_pids(strict=True)}


def _ancestor_pids() -> list[int]:
    return [p.pid for p in psutil.Process().parents()]


def terminal_id() -> str | None:
    """The VS Code terminal this process runs in: the nearest ancestor that is a terminal's shell.
    Hooks and the MCP server are descendants of `claude`, which runs in that shell."""
    try:
        terminals = _terminal_pids(strict=False)
        return next((f"{TERM_PREFIX}{pid}" for pid in _ancestor_pids() if pid in terminals), None)
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


def _type(pid: int, text: str) -> str:
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


def type_into(session: registry.Session, text: str, source: str) -> str:
    """Returns 'sent', 'notfound' or 'error:<msg>'."""
    lg = log.get("inject")
    # A single line: newlines inside would submit early.
    text = " ".join(text.split())
    pid = _shell_pid(session.term_id)
    if pid is None:
        result = "error:no terminal id"
    else:
        try:
            result = _type(pid, text)
        except Exception as e:
            result = f"error:{e}"
    registry.log_injection(session, source, text, result)
    lg.info("inject ext=%s %s source=%s result=%s text=%r", session.ext, session.label, source, result, text)
    return result
