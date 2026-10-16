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


# Entering plan mode changes no files, and when it was asked for by phone its approval prompt would wait in a
# terminal nobody is watching. Typed at the keyboard it still asks, and so does ExitPlanMode, which approves the plan.
NO_APPROVAL_BY_PHONE = {"EnterPlanMode"}
# Ringing or messaging the owner changes no files, so it needs no allow rule. Plan mode asks before hooks run; there the
# tools' read-only annotation (mcp_server.READ_ONLY) is what lets them through.
# Matched by prefix since the tools carry the owner's name (call_sam). Settings tools and transfer_call still ask.
NO_APPROVAL_PREFIXES = ("mcp__claude-phone__call_", "mcp__claude-phone__notify_")


def _allow(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "permissionDecisionReason": f"claude-phone: {reason}",
    }}))


def pre_tool(sid: str, payload: dict) -> None:
    tool = payload.get("tool_name") or ""
    if tool.startswith(NO_APPROVAL_PREFIXES):
        _allow("calling the owner changes no files")
    elif tool in NO_APPROVAL_BY_PHONE and registry.last_prompt_by_phone(sid):
        _allow("asked for by phone")


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
            registry.record_prompt(sid, str(payload.get("prompt") or ""))
        elif event == "pre-tool":
            pre_tool(sid, payload)
        elif event == "stop":
            registry.set_status(sid, "idle")
    except Exception as e:  # never block Claude Code
        lg.warning("hook %s failed: %r", event, e)
    return 0
