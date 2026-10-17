"""Claude Code hook handlers: sessions registering as they start, their status, plan mode by phone, and calling the
owner."""

import io
import json
import subprocess

from claude_phone import hooks, inject, registry


def hook(monkeypatch, event: str, **payload) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s", **payload})))
    assert hooks.run(event) == 0


def git(cwd, *args) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_a_session_started_in_a_vscode_terminal_gets_an_extension_named_after_its_repo_and_worktree(
        monkeypatch, tmp_path):
    repo = tmp_path / "site-be"
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "start")
    git(repo, "worktree", "add", "-q", "-b", "fix-login", str(tmp_path / "login-tree"))
    monkeypatch.setattr(inject, "terminal_id", lambda: "vscode:1")
    hook(monkeypatch, "session-start", cwd=str(repo))
    monkeypatch.setattr(inject, "terminal_id", lambda: "vscode:2")
    hook(monkeypatch, "session-start", session_id="w", cwd=str(tmp_path / "login-tree"))
    assert [(s.ext, s.label) for s in registry.all_sessions()] == [(301, "site-be/main"), (302, "site-be/login-tree")]


def test_a_session_outside_a_vscode_terminal_or_a_subagent_is_not_registered(monkeypatch, tmp_path):
    monkeypatch.setattr(inject, "terminal_id", lambda: None)  # nothing could type into it
    hook(monkeypatch, "session-start", cwd=str(tmp_path))
    monkeypatch.setattr(inject, "terminal_id", lambda: "vscode:1")
    hook(monkeypatch, "session-start", cwd=str(tmp_path), agent_id="a1")
    assert registry.all_sessions() == []


def test_prompt_and_stop_mark_the_session_working_then_idle(monkeypatch):
    registry.register("s", "repo", "main", None, "/r", "vscode:1")
    hook(monkeypatch, "prompt")
    assert registry.find("s").status == "working"
    hook(monkeypatch, "stop")
    assert registry.find("s").status == "idle"


def test_plan_mode_asked_for_by_phone_needs_no_approval_but_typed_at_the_keyboard_it_does(monkeypatch, capsys):
    s = registry.register("s", "repo", "main", None, "/r", "vscode:1")
    registry.log_injection(s, "dial-in", "Switch to plan mode and call me back.", "sent")
    hook(monkeypatch, "prompt", prompt="Switch to plan mode  and call me back.")
    hook(monkeypatch, "pre-tool", tool_name="EnterPlanMode")
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "allow"
    hook(monkeypatch, "pre-tool", tool_name="ExitPlanMode")  # approving the plan always asks
    assert capsys.readouterr().out == ""
    hook(monkeypatch, "prompt", prompt="Now plan the next part.")
    hook(monkeypatch, "pre-tool", tool_name="EnterPlanMode")
    assert capsys.readouterr().out == ""


def test_calling_or_messaging_the_owner_needs_no_approval_but_changing_settings_does(monkeypatch, capsys):
    registry.register("s", "repo", "main", None, "/r", "vscode:1")
    hook(monkeypatch, "prompt", prompt="Enter plan mode and call me back.")  # typed at the keyboard
    for tool in ("mcp__claude-phone__call_jakob", "mcp__claude-phone__notify_jakob"):
        hook(monkeypatch, "pre-tool", tool_name=tool)
        assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "allow"
    for tool in ("mcp__claude-phone__change_phone_setting", "mcp__claude-phone__transfer_call"):
        hook(monkeypatch, "pre-tool", tool_name=tool)
        assert capsys.readouterr().out == ""


def test_a_subagent_calling_the_owner_needs_no_approval_either(monkeypatch, capsys):
    hook(monkeypatch, "pre-tool", tool_name="mcp__claude-phone__call_sam", agent_id="reviewer-1")
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "allow"
