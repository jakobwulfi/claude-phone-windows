"""Claude Code hook handlers. Wired as `phone hook <event>`; payload arrives as JSON on stdin.
Hooks must never break a session, so every handler swallows its own errors."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import inject, log, registry


def _git(cwd: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def repo_info(cwd: str) -> tuple[str, str | None, str | None]:
    """(repo, branch, worktree). Worktrees report the main repo's name, plus the worktree dir name."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return Path(cwd).name, None, None
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    main_root = Path(common).parent if common else Path(top)
    repo = main_root.name
    worktree = Path(top).name if Path(top) != main_root else None
    branch = _git(cwd, "symbolic-ref", "--short", "-q", "HEAD") or _git(cwd, "rev-parse", "--short", "HEAD")
    return repo, branch, worktree


def run(event: str) -> int:
    lg = log.get("hook")
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        sid = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
        if not sid:
            return 0
        if payload.get("agent_id"):
            return 0  # subagent events share the parent's session; nothing to register
        if event == "session-start":
            cwd = payload.get("cwd") or os.getcwd()
            term = inject.terminal_id()
            if not term:
                return 0  # not in a VS Code terminal: nothing we could type into
            repo, branch, worktree = repo_info(cwd)
            s = registry.register(sid, repo, branch, worktree, cwd, term)
            registry.ringtone(repo)  # assign on first sight so repos get distinct rings
            lg.info("session start ext=%s %s sid=%s source=%s", s.ext, s.label, sid, payload.get("source"))
        elif event == "session-end":
            if payload.get("reason") == "clear":
                return 0  # the tab's next SessionStart takes over this extension
            registry.unregister(sid)
            lg.info("session end sid=%s reason=%s", sid, payload.get("reason"))
        elif event == "prompt":
            registry.set_status(sid, "working")
        elif event == "stop":
            registry.set_status(sid, "idle")
        elif event == "idle" and payload.get("notification_type") == "idle_prompt":
            # Esc skips the Stop hook, so a session would stay "working" (and callers on hold) until its next turn.
            # Claude Code sends idle_prompt about 60 s after the prompt goes quiet.
            registry.set_status(sid, "idle")
    except Exception as e:  # never block Claude Code
        lg.warning("hook %s failed: %r", event, e)
    return 0
