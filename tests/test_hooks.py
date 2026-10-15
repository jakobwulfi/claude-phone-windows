"""Claude Code hook handlers: session status as the hooks report it."""

import io
import json

from claude_phone import hooks, registry


def hook(monkeypatch, event: str, **payload) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s", **payload})))
    assert hooks.run(event) == 0


def status() -> str:
    return registry.find("s").status


def test_prompt_and_stop_mark_the_session_working_then_idle(monkeypatch):
    registry.register("s", "repo", "main", None, "/r", "vscode:1")
    hook(monkeypatch, "prompt")
    assert status() == "working"
    hook(monkeypatch, "stop")
    assert status() == "idle"


def test_an_idle_prompt_marks_an_interrupted_session_idle(monkeypatch):
    registry.register("s", "repo", "main", None, "/r", "vscode:1")
    hook(monkeypatch, "prompt")  # then Esc: no Stop hook

    hook(monkeypatch, "idle", hook_event_name="Notification", notification_type="idle_prompt")

    assert status() == "idle"


def test_other_notifications_leave_the_status_alone(monkeypatch):
    registry.register("s", "repo", "main", None, "/r", "vscode:1")
    hook(monkeypatch, "prompt")

    hook(monkeypatch, "idle", hook_event_name="Notification", notification_type="permission_prompt")

    assert status() == "working"
