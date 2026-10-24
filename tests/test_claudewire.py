"""What claude-phone writes into Claude Code's configuration: merged, never clobbered, and the same the second time."""

import json
import subprocess
from pathlib import Path

import pytest

from claude_phone import claudewire, cli, config

EXE = Path("C:/clone/.venv/Scripts/phone.exe")
CALLS = "mcp__claude-phone__(call|notify)_.*"


@pytest.fixture
def claude_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return tmp_path


def write_settings(claude_dir, data) -> Path:
    path = claude_dir / "settings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def hook(exe, arg):
    return claudewire.hook_command(exe, arg)


def test_a_fresh_settings_file_gets_all_five_hooks(claude_dir):
    change = claudewire.plan_settings({}, EXE)

    assert len(change.notes) == 5
    hooks = change.settings["hooks"]
    assert hooks["Stop"] == [{"hooks": [{"type": "command", "command": "C:/clone/.venv/Scripts/phone.exe",
                                         "args": ["hook", "stop"]}]}]
    assert hooks["PreToolUse"][0]["matcher"] == f"EnterPlanMode|{CALLS}"


def test_plan_mode_is_left_out_of_the_matcher_when_declined():
    change = claudewire.plan_settings({}, EXE, plan_mode=False)

    assert change.settings["hooks"]["PreToolUse"][0]["matcher"] == CALLS


def test_the_merge_keeps_unrelated_keys_and_other_hooks(claude_dir):
    mine = {"matcher": "Bash", "hooks": [{"type": "command", "command": "lint.cmd"}]}
    settings = {"model": "opus", "permissions": {"allow": ["Bash(git status)"]},
                "hooks": {"PreToolUse": [mine], "Notification": [{"hooks": [{"type": "command", "command": "n"}]}]}}

    new = claudewire.plan_settings(settings, EXE).settings

    assert new["model"] == "opus" and new["permissions"] == settings["permissions"]
    assert new["hooks"]["Notification"] == settings["hooks"]["Notification"]
    assert new["hooks"]["PreToolUse"][0] == mine
    assert len(new["hooks"]["PreToolUse"]) == 2
    assert "SessionStart" not in settings["hooks"]  # the input isn't changed


def test_a_second_run_changes_nothing(claude_dir):
    path = claude_dir / "settings.json"
    claudewire.apply_settings(claudewire.plan_settings({}, EXE), path)
    before = path.read_text(encoding="utf-8")

    second = claudewire.plan_settings(claudewire.read_settings(path), EXE)

    assert second.notes == []
    assert claudewire.apply_settings(second, path) is False
    assert path.read_text(encoding="utf-8") == before
    assert claudewire.missing_hooks() == []


def test_a_hook_written_as_a_command_line_counts_as_wired(claude_dir):
    write_settings(claude_dir, {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                "command": "C:/x/phone.exe hook stop"}]}]}})

    assert "stop" not in claudewire.missing_hooks()
    assert not any("`phone hook stop`" in n for n in claudewire.plan_settings(claudewire.read_settings(), EXE).notes)


def test_apply_keeps_the_old_file_as_a_backup(claude_dir):
    path = write_settings(claude_dir, {"model": "opus"})
    original = path.read_text(encoding="utf-8")

    assert claudewire.apply_settings(claudewire.plan_settings({"model": "opus"}, EXE), path) is True

    assert (claude_dir / "settings.json.bak").read_text(encoding="utf-8") == original
    assert json.loads(path.read_text(encoding="utf-8"))["model"] == "opus"
    assert not (claude_dir / "settings.json.tmp").exists()


def test_a_wired_pre_tool_hook_with_a_narrow_matcher_is_fixed_and_flagged(claude_dir):
    narrow = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook(EXE, "pre-tool")]}]}}
    write_settings(claude_dir, narrow)
    assert claudewire.weak_matcher() is True

    change = claudewire.plan_settings(narrow, EXE)

    assert change.settings["hooks"]["PreToolUse"][0]["matcher"] == f"EnterPlanMode|Bash|{CALLS}"
    assert len(change.settings["hooks"]["PreToolUse"]) == 1
    assert claudewire.weak_matcher(change.settings) is False


def test_a_hook_without_a_matcher_already_covers_the_calls():
    wide = {"hooks": {"PreToolUse": [{"hooks": [hook(EXE, "pre-tool")]}]}}

    assert claudewire.weak_matcher(wide) is False
    assert claudewire.plan_settings(wide, EXE).settings["hooks"]["PreToolUse"][0].get("matcher") is None


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_a_settings_file_that_cant_be_merged_is_left_alone(claude_dir, content):
    path = claude_dir / "settings.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(claudewire.SettingsError):
        claudewire.read_settings(path)
    assert path.read_text(encoding="utf-8") == content
    assert claudewire.missing_hooks() == list(claudewire.HOOKS.values())  # status just calls them all missing


def test_hooks_that_are_not_an_object_or_list_are_refused():
    with pytest.raises(claudewire.SettingsError):
        claudewire.plan_settings({"hooks": []}, EXE)
    with pytest.raises(claudewire.SettingsError):
        claudewire.plan_settings({"hooks": {"Stop": "x"}}, EXE)


# --- the MCP server ---

def fake_claude(monkeypatch, answers):
    """answers: {the word after `claude mcp`: (return code, stdout)}. Returns the calls made."""
    calls = []

    def run(*args):
        calls.append(args)
        code, out = answers[args[1]]
        return subprocess.CompletedProcess(args, code, out, "")
    monkeypatch.setattr(claudewire, "_run_claude", run)
    return calls


def mcp_get(path):
    return f"claude-phone:\n  Scope: User config\n  Status: Connected\n  Type: stdio\n  Command: {path}\n  Args: mcp\n"


def test_mcp_state_tells_registered_from_missing_from_elsewhere(monkeypatch):
    fake_claude(monkeypatch, {"get": (0, mcp_get(r"C:\clone\.venv\Scripts\phone.exe"))})
    assert claudewire.mcp_state(EXE) == "ok"  # slashes and case don't matter on Windows

    fake_claude(monkeypatch, {"get": (0, mcp_get(r"C:\old\phone.exe"))})
    assert claudewire.mcp_state(EXE) == "other-path"

    fake_claude(monkeypatch, {"get": (1, "No MCP server named claude-phone")})
    assert claudewire.mcp_state(EXE) == "missing"


def test_mcp_state_is_unknown_without_claude_code():
    assert claudewire.mcp_state(EXE) == "unknown"  # conftest answers None, as when `claude` isn't on PATH


def test_register_adds_a_missing_server_for_every_project(monkeypatch):
    calls = fake_claude(monkeypatch, {"get": (1, ""), "add": (0, "")})

    assert claudewire.register_mcp(EXE) == (True, "registered")
    assert calls[-1] == ("mcp", "add", "--scope", "user", "claude-phone", "--", str(EXE), "mcp")


def test_register_replaces_a_server_pointing_elsewhere(monkeypatch):
    calls = fake_claude(monkeypatch, {"get": (0, mcp_get(r"C:\old\phone.exe")), "remove": (0, ""), "add": (0, "")})

    ok, message = claudewire.register_mcp(EXE)

    assert ok and "again" in message
    assert [c[1] for c in calls] == ["get", "remove", "add"]


def test_register_does_nothing_when_it_is_right_already(monkeypatch):
    calls = fake_claude(monkeypatch, {"get": (0, mcp_get(str(EXE)))})

    assert claudewire.register_mcp(EXE) == (True, "already registered")
    assert len(calls) == 1


def test_register_says_why_it_failed(monkeypatch):
    fake_claude(monkeypatch, {"get": (1, ""), "add": (1, "")})
    ok, message = claudewire.register_mcp(EXE)
    assert not ok and message.startswith("`claude mcp add` failed")


def test_register_without_claude_code_explains():
    ok, message = claudewire.register_mcp(EXE)
    assert not ok and "isn't on PATH" in message


# --- the /claude-phone command and the CLAUDE.md block ---

def test_the_command_file_gets_this_installs_paths(claude_dir):
    assert claudewire.install_command(EXE, config.REPO) == "written"

    text = (claude_dir / "commands" / "claude-phone.md").read_text(encoding="utf-8")
    assert "C:/path/to" not in text
    assert "C:/clone/.venv/Scripts/phone.exe" in text
    assert claudewire.install_command(EXE, config.REPO) == "unchanged"


def test_the_claude_md_block_names_the_owner_and_replaces_only_itself(claude_dir):
    path = claude_dir / "CLAUDE.md"
    path.write_text("# Mine\n\nKeep this.", encoding="utf-8")

    assert claudewire.install_claude_md("Sam Lee", "sam_lee") == "updated"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Mine\n\nKeep this.\n\n<!-- claude-phone -->")
    assert "`call_sam_lee`" in text and "Phoning Sam Lee" in text
    assert text.count("Sam") == text.count("Sam Lee")  # no bare "Sam" left from the snippet

    assert claudewire.install_claude_md("Sam Lee", "sam_lee") == "unchanged"

    assert claudewire.install_claude_md("Alex", "alex") == "updated"
    text = path.read_text(encoding="utf-8")
    assert text.count("<!-- claude-phone -->") == 1 and "call_alex" in text and "sam_lee" not in text
    assert text.startswith("# Mine\n\nKeep this.\n\n")


def test_the_claude_md_block_is_created_when_there_is_no_file(claude_dir):
    assert claudewire.install_claude_md("Jakob", "jakob") == "written"
    assert (claude_dir / "CLAUDE.md").read_text(encoding="utf-8").startswith("<!-- claude-phone -->\n")


# --- phone status ---

def test_status_warns_about_hooks_matcher_and_mcp(monkeypatch, claude_dir, tmp_path):
    exe = tmp_path / "phone.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / "python.exe"))
    fake_claude(monkeypatch, {"get": (1, "")})

    warnings = cli.claude_wiring_warnings()
    assert len(warnings) == 2 and "pre-tool" in warnings[0] and "MCP server" in warnings[1]

    write_settings(claude_dir, claudewire.plan_settings({}, exe, plan_mode=False).settings)
    fake_claude(monkeypatch, {"get": (0, mcp_get(str(exe)))})
    assert cli.claude_wiring_warnings() == []

    narrow = {"hooks": {k: [{"hooks": [hook(exe, a)]}] for k, a in claudewire.HOOKS.items()}}
    narrow["hooks"]["PreToolUse"][0]["matcher"] = "Bash"
    write_settings(claude_dir, narrow)
    assert "matcher" in cli.claude_wiring_warnings()[0]
