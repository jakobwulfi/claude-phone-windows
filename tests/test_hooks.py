"""Claude Code hook handlers: session status as the hooks report it."""

import io
import json

from claude_phone import hooks, registry


def hook(monkeypatch, event: str, **payload) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s", **payload})))
    assert hooks.run(event) == 0


def test_prompt_and_stop_mark_the_session_working_then_idle(monkeypatch):
    registry.register("s", "repo", "main", None, "/r", "vscode:1")
    hook(monkeypatch, "prompt")
    assert registry.find("s").status == "working"
    hook(monkeypatch, "stop")
    assert registry.find("s").status == "idle"
