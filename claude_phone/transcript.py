"""Reads the end of a Claude Code session's transcript (~/.claude/projects/<project>/<session id>.jsonl).
Esc fires no hook, but Claude Code records the interrupt there at once as a user message."""

from __future__ import annotations

import json
import os
from pathlib import Path

TAIL_BYTES = 64 * 1024
INTERRUPTED = "[Request interrupted by user"  # "…]" after a running turn, "… for tool use]" at a permission prompt


def projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def find(session_id: str) -> Path | None:
    """Session ids are UUIDs, so the file name alone identifies it, whatever project folder it's in."""
    return next(projects_dir().glob(f"*/{session_id}.jsonl"), None)


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    return ""


def last_message(path: Path) -> dict | None:
    """The last user or assistant entry. Attachments and system lines (summaries, timings) come after it."""
    with path.open("rb") as f:
        f.seek(max(0, f.seek(0, os.SEEK_END) - TAIL_BYTES))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # the first line of the tail is usually cut off
        if isinstance(entry, dict) and entry.get("type") in ("user", "assistant"):
            return entry
    return None


def interrupted(session_id: str) -> bool:
    """True when the session's last message is an Esc interrupt, so it's waiting for input, not working."""
    path = find(session_id)
    if not path:
        return False
    entry = last_message(path)
    return bool(entry and entry["type"] == "user" and _text(entry.get("message") or {}).startswith(INTERRUPTED))
