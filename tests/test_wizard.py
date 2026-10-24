"""`phone setup`, driven by scripted answers against a throwaway config and Claude Code folder."""

import json
import os
import subprocess

import pytest

from claude_phone import claudewire, cli, config, editors, keychain, tools, wizard

SECRET = "s3cret-sip-password"


class Script:
    """The user's answers, in order. A question nobody scripted fails the test."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []
        self.said: list[str] = []

    def ask(self, prompt: str) -> str:
        self.questions.append(prompt)
        assert self.answers, f"unexpected question: {prompt!r}"
        return self.answers.pop(0)

    def say(self, text: str) -> None:
        self.said.append(text)

    @property
    def output(self) -> str:
        return "\n".join(self.said)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A clean home, Claude Code folder and keychain; FreeSWITCH and whisper present; Claude Code's MCP list fake."""
    home, claude = tmp_path / "home", tmp_path / "claude"
    home.mkdir(), claude.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "CONFIG_FILE", home / "config.toml")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.delenv("CLAUDE_PHONE_SIP", raising=False)

    store: dict[str, str] = {}
    monkeypatch.setattr(keychain, "get", lambda name: store.get(name))
    monkeypatch.setattr(keychain, "put", lambda name, value: store.__setitem__(name, value))
    monkeypatch.setattr(keychain, "_secrets", type("S", (), {"token_hex": staticmethod(lambda n: SECRET)}))

    freeswitch, whisper = tmp_path / "FreeSwitchConsole.exe", tmp_path / "whisper-server.exe"
    freeswitch.write_text("", encoding="utf-8"), whisper.write_text("", encoding="utf-8")
    config.CONFIG_FILE.write_text(f'freeswitch_exe = "{freeswitch.as_posix()}"\n'
                                  f'whisper_server = "{whisper.as_posix()}"\n', encoding="utf-8")

    monkeypatch.setattr(config, "MODELS", tmp_path / "models")
    monkeypatch.setattr(config, "ENGINES", tmp_path / "engines")
    config.MODELS.mkdir()
    (config.MODELS / config.load().whisper_model).write_text("", encoding="utf-8")

    exe = tmp_path / "phone.exe"
    mcp = {"registered": False}

    def run_claude(*args):
        if args[1] == "get":
            out = f"claude-phone:\n  Command: {exe}\n  Args: mcp\n" if mcp["registered"] else ""
            return subprocess.CompletedProcess(args, 0 if mcp["registered"] else 1, out, "")
        if args[1] == "add":
            mcp["registered"] = True
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(claudewire, "_run_claude", run_claude)
    monkeypatch.setattr(wizard, "firewall_open", lambda: False)
    monkeypatch.setattr(wizard.Wizard, "_services_running", lambda self: True)
    monkeypatch.setattr(tools, "ffmpeg_on_path", lambda: True)
    monkeypatch.setattr(editors, "find_code", lambda: None)  # no editors, unless a test adds them
    monkeypatch.setattr(editors, "visual_studios", lambda: [])
    return type("Env", (), {"home": home, "claude": claude, "exe": exe, "store": store, "mcp": mcp,
                            "freeswitch": freeswitch, "whisper": whisper})


def wizard_for(env, script, **kwargs) -> wizard.Wizard:
    return wizard.Wizard(script.ask, script.say, exe=env.exe, **kwargs)


# owner, then Enter for ring_seconds, autostart, whisper_text, callerid_max
SETTINGS = ["Sam Lee", "", "", "", ""]


def test_a_fresh_run_sets_everything_up_and_a_second_run_asks_nothing(env):
    # Linphone, no password shown; hooks with plan mode and write it; MCP, command and CLAUDE.md block
    script = Script(*SETTINGS, "l", "n", "y", "y", "y", "y", "y")

    wizard_for(env, script).run()

    saved = config.CONFIG_FILE.read_text(encoding="utf-8")
    assert 'owner_name = "Sam Lee"' in saved and "sip_port = 5070" in saved and 'ring_ext = "201"' in saved
    hooks = json.loads((env.claude / "settings.json").read_text(encoding="utf-8"))["hooks"]
    assert sorted(hooks) == sorted(claudewire.HOOKS)
    assert (env.claude / "commands" / "claude-phone.md").exists()
    assert "call_sam_lee" in (env.claude / "CLAUDE.md").read_text(encoding="utf-8")
    assert env.mcp["registered"] and env.store["sip"] == SECRET
    assert SECRET not in script.output  # shown only when asked for
    assert wizard.load_declines() == set()
    assert not script.answers

    before = {p: p.read_text(encoding="utf-8") for p in (config.CONFIG_FILE, env.claude / "settings.json")}
    again = Script()
    wizard_for(env, again).run()

    assert "Nothing left to ask" in again.output and again.questions == []
    assert all(p.read_text(encoding="utf-8") == text for p, text in before.items())


def test_the_sip_password_is_shown_only_when_asked_for(env):
    script = Script(*SETTINGS, "l", "y", "y", "y", "y", "y", "y")

    wizard_for(env, script).run()

    assert f"Password: {SECRET}" in script.output


def test_a_decline_is_remembered_and_all_asks_again(env):
    script = Script(*SETTINGS, "l", "n", "y", "y", "n", "n", "n")  # hooks yes, then no to MCP, command and CLAUDE.md

    wizard_for(env, script).run()

    assert wizard.load_declines() == {"mcp", "command", "claude-md"}
    assert not env.mcp["registered"] and not (env.claude / "commands").exists()
    again = Script()
    wizard_for(env, again).run()
    assert again.questions == []
    assert "declined  Claude Code MCP server" in again.output

    states = {part.name: state for part, state in wizard_for(env, Script(), redo_all=True).table()}
    assert set(states.values()) == {wizard.MISSING}


def test_a_desk_phone_opens_the_firewall_and_rings_its_own_extension(env):
    opened = []
    script = Script(*SETTINGS, "d", "", "y", "y", "y", "y", "y", "y")

    wizard_for(env, script, firewall=lambda path: opened.append(path) or True).run()

    saved = config.CONFIG_FILE.read_text(encoding="utf-8")
    assert 'ring_ext = "200"' in saved and "sip_port" not in saved
    assert [p.name for p in opened] == ["firewall.ps1"]
    assert "Firewall rules added." in script.output


def test_a_new_lan_address_is_saved(env):
    script = Script("d", "192.168.1.99", "n")

    wizard_for(env, script)._run_phones()

    assert 'lan_ip = "192.168.1.99"' in config.CONFIG_FILE.read_text(encoding="utf-8")


def test_a_bad_setting_is_asked_again(env):
    script = Script("Sam <>", "Sam", "", "", "", "")

    assert wizard_for(env, script)._run_settings() is True

    assert any("Not changed" in line for line in script.said)
    assert 'owner_name = "Sam"' in config.CONFIG_FILE.read_text(encoding="utf-8")


def test_the_hooks_are_merged_into_existing_settings_after_showing_the_change(env):
    path = env.claude / "settings.json"
    mine = {"type": "command", "command": "mine.cmd"}
    path.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [mine]}]}}), encoding="utf-8")
    original = path.read_text(encoding="utf-8")
    script = Script("n", "y")  # no plan mode, write it

    assert wizard_for(env, script)._run_hooks() is True

    new = json.loads(path.read_text(encoding="utf-8"))
    assert new["model"] == "opus" and len(new["hooks"]["Stop"]) == 2
    assert new["hooks"]["PreToolUse"][0]["matcher"] == claudewire.CALL_MATCHER
    assert (env.claude / "settings.json.bak").read_text(encoding="utf-8") == original
    assert any("PreToolUse: add" in line for line in script.said)


def test_declining_the_hooks_writes_nothing(env):
    script = Script("y", "n")

    assert wizard_for(env, script)._run_hooks() is False
    assert not (env.claude / "settings.json").exists()


def test_a_broken_settings_file_is_left_alone(env):
    path = env.claude / "settings.json"
    path.write_text("{oops", encoding="utf-8")
    script = Script()

    assert wizard_for(env, script)._run_hooks() is False

    assert path.read_text(encoding="utf-8") == "{oops" and script.questions == []
    assert "isn't valid JSON" in script.output


def test_freeswitch_and_whisper_are_linked_by_path(env, tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "find_freeswitch", lambda: None)
    monkeypatch.setattr(tools, "find_whisper", lambda: None)
    config.CONFIG_FILE.write_text('freeswitch_exe = "C:/nowhere/FreeSwitchConsole.exe"\n', encoding="utf-8")
    other = tmp_path / "elsewhere" / "FreeSwitchConsole.exe"
    other.parent.mkdir()
    other.write_text("", encoding="utf-8")

    assert wizard_for(env, Script("n", str(other)))._run_freeswitch() is True  # no install, a path instead
    config.forget()
    assert config.load().freeswitch_exe == str(other)
    assert wizard_for(env, Script("n", ""))._run_freeswitch() is False
    assert wizard_for(env, Script("n", str(tmp_path / "nothing.exe")))._run_freeswitch() is False
    wrong = tmp_path / "notepad.exe"
    wrong.write_text("", encoding="utf-8")
    script = Script("n", str(wrong))
    assert wizard_for(env, script)._run_whisper() is False
    assert "not whisper-server.exe" in script.output


def test_a_program_found_somewhere_else_is_saved_without_asking(env, tmp_path, monkeypatch):
    elsewhere = tmp_path / "bin" / "FreeSwitchConsole.exe"
    elsewhere.parent.mkdir()
    elsewhere.write_text("", encoding="utf-8")
    monkeypatch.setattr(tools, "find_freeswitch", lambda: elsewhere)
    script = Script()

    assert wizard_for(env, script)._run_freeswitch() is True

    config.forget()
    assert config.load().freeswitch_exe == str(elsewhere) and script.questions == []


def fake_download(monkeypatch, content=b"data"):
    """tools.download that writes `content` and reports it in two steps."""
    def download(url, dest, *, sha256=None, progress=None, open_url=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        progress(len(content) // 2, len(content))
        progress(len(content), len(content))
        return dest
    monkeypatch.setattr(tools, "download", download)


def test_freeswitch_is_downloaded_checked_installed_and_saved(env, tmp_path, monkeypatch):
    installed = tmp_path / "Program Files" / "FreeSwitchConsole.exe"
    installed.parent.mkdir()
    installed.write_text("", encoding="utf-8")
    monkeypatch.setattr(tools, "find_freeswitch", lambda: None)
    monkeypatch.setattr(tools, "latest_freeswitch", lambda: tools.Download("https://x/fs.msi", "fs.msi"))
    monkeypatch.setattr(tools, "is_signed_by", lambda path: True)
    ran = []
    monkeypatch.setattr(tools, "install_freeswitch", lambda msi: ran.append(msi.name) or installed)
    fake_download(monkeypatch)
    script = Script("y")

    assert wizard_for(env, script)._run_freeswitch() is True

    config.forget()
    assert config.load().freeswitch_exe == str(installed)
    assert ran == ["fs.msi"] and "  50%" in script.output and "  100%" in script.output
    assert not (config.HOME / "downloads" / "fs.msi").exists()  # the installer isn't kept


def test_an_unsigned_installer_is_not_run(env, monkeypatch):
    monkeypatch.setattr(tools, "find_freeswitch", lambda: None)
    monkeypatch.setattr(tools, "latest_freeswitch", lambda: tools.Download("https://x/fs.msi", "fs.msi"))
    monkeypatch.setattr(tools, "is_signed_by", lambda path: False)
    monkeypatch.setattr(tools, "install_freeswitch", lambda msi: pytest.fail("ran an unsigned installer"))
    fake_download(monkeypatch)
    script = Script("y", "")  # install, then no path either

    assert wizard_for(env, script)._run_freeswitch() is False
    assert "isn't signed by SignalWire" in script.output


def test_a_cancelled_or_failed_install_falls_back_to_asking_for_a_path(env, monkeypatch):
    monkeypatch.setattr(tools, "find_freeswitch", lambda: None)
    monkeypatch.setattr(tools, "latest_freeswitch", lambda: tools.Download("https://x/fs.msi", "fs.msi"))
    monkeypatch.setattr(tools, "is_signed_by", lambda path: True)
    monkeypatch.setattr(tools, "install_freeswitch", lambda msi: None)  # the UAC prompt was refused
    fake_download(monkeypatch)
    script = Script("y", "")

    assert wizard_for(env, script)._run_freeswitch() is False
    assert "didn't finish" in script.output and "Path of FreeSwitchConsole.exe" in script.questions[-1]


def test_a_failed_download_is_reported_not_raised(env, monkeypatch):
    monkeypatch.setattr(tools, "find_freeswitch", lambda: None)

    def offline():
        raise OSError("no route to host")
    monkeypatch.setattr(tools, "latest_freeswitch", offline)
    script = Script("y", "")

    assert wizard_for(env, script)._run_freeswitch() is False
    assert "Couldn't install FreeSWITCH: no route to host" in script.output


def test_whisper_is_downloaded_with_its_checksum_and_unpacked(env, tmp_path, monkeypatch):
    server = tmp_path / "engines" / "whisper.cpp" / "whisper-server.exe"
    monkeypatch.setattr(tools, "find_whisper", lambda: None)
    monkeypatch.setattr(tools, "latest_whisper",
                        lambda: tools.Download("https://x/w.zip", "w.zip", 4, "ab" * 32))
    seen = {}

    def download(url, dest, *, sha256=None, progress=None, open_url=None):
        seen["sha256"] = sha256
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"zip")
        return dest
    monkeypatch.setattr(tools, "download", download)
    monkeypatch.setattr(tools, "install_whisper", lambda archive: server)

    assert wizard_for(env, Script("y"))._run_whisper() is True

    config.forget()
    assert seen["sha256"] == "ab" * 32 and config.load().whisper_server == str(server)


def test_the_speech_model_is_downloaded_here_with_progress(env, monkeypatch):
    (config.MODELS / config.load().whisper_model).unlink()
    monkeypatch.setattr(cli, "ensure_model", lambda progress: progress(1, 2) or progress(2, 2))
    script = Script("y")

    assert wizard_for(env, script)._model_done() is False
    assert wizard_for(env, script)._run_model() is True
    assert "  50%" in script.output and "  100%" in script.output


def test_a_failed_model_download_is_left_for_phone_up(env, monkeypatch):
    def broken(progress):
        raise tools.DownloadError("stopped at 5 of 9 bytes")
    monkeypatch.setattr(cli, "ensure_model", broken)
    script = Script("y")

    assert wizard_for(env, script)._run_model() is False
    assert "`phone up` tries again" in script.output


def test_ffmpeg_is_installed_with_winget_after_a_yes(env, monkeypatch, tmp_path):
    installed = tmp_path / "WinGet" / "Links" / "ffmpeg.exe"
    state = {"on_path": False, "installed": False}
    monkeypatch.setattr(tools, "ffmpeg_on_path", lambda: state["on_path"])
    monkeypatch.setattr(tools, "find_ffmpeg", lambda: installed if state["installed"] else None)
    monkeypatch.setattr(tools, "install_ffmpeg", lambda: state.__setitem__("installed", True) or True)
    monkeypatch.setenv("PATH", "C:/somewhere")
    script = Script("y")

    assert wizard_for(env, script)._run_ffmpeg() is True

    assert os.environ["PATH"].startswith(str(installed.parent))  # the services started next inherit it
    assert "New terminals will find it" in script.output


def test_declining_ffmpeg_says_calls_need_it(env, monkeypatch):
    monkeypatch.setattr(tools, "find_ffmpeg", lambda: None)
    script = Script("n")

    assert wizard_for(env, script)._run_ffmpeg() is False
    assert "calls need it" in script.output


def test_a_failed_ffmpeg_install_says_what_to_run(env, monkeypatch):
    monkeypatch.setattr(tools, "find_ffmpeg", lambda: None)
    monkeypatch.setattr(tools, "install_ffmpeg", lambda: False)
    script = Script("y")

    assert wizard_for(env, script)._run_ffmpeg() is False
    assert "winget install Gyan.FFmpeg" in script.output


def test_ffmpeg_already_on_path_needs_nothing(env, monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "find_ffmpeg", lambda: tmp_path / "ffmpeg.exe")
    monkeypatch.setattr(tools, "ffmpeg_on_path", lambda: True)
    script = Script()

    assert wizard_for(env, script)._run_ffmpeg() is True and script.questions == []


def test_a_hand_pasted_phoning_section_is_not_duplicated_without_asking(env):
    (env.claude / "CLAUDE.md").write_text("## Phoning Sam\n\nmine\n", encoding="utf-8")

    assert wizard_for(env, Script("n"))._run_claude_md() is False
    assert (env.claude / "CLAUDE.md").read_text(encoding="utf-8") == "## Phoning Sam\n\nmine\n"
    assert wizard_for(env, Script("y", "y"))._run_claude_md() is True
    assert "<!-- claude-phone -->" in (env.claude / "CLAUDE.md").read_text(encoding="utf-8")


def test_the_wizard_offers_to_start_claude_phone_when_it_is_not_running(env, monkeypatch):
    monkeypatch.setattr(wizard.Wizard, "_services_running", lambda self: False)
    started = []
    script = Script(*SETTINGS, "l", "n", "y", "y", "y", "y", "y", "y")

    wizard_for(env, script, start=started.append).run()

    assert started == [env.exe]


VS_2026 = editors.VisualStudio("87bd818d", "Visual Studio Community 2026", "18.10.12224.181",
                               config.REPO / "vs", "Microsoft.VisualStudio.Product.Community")


@pytest.fixture
def editor_world(monkeypatch, tmp_path):
    """VS Code and one Visual Studio, both without our extension; what the fake installs do is in `world`."""
    world = {"code": None, "vs": None, "vs_running": False, "built": [], "vs_installs": [], "release": None}
    code, vsix = tmp_path / "code.cmd", tmp_path / "claude-phone.vsix"
    monkeypatch.setattr(editors, "release_vsix", lambda kind, repo, folder, progress=None: world["release"])
    available_code = editors.available_vscode_version(config.REPO)
    available_vs = editors.available_visual_studio_version(config.REPO)
    monkeypatch.setattr(editors, "find_code", lambda: code)
    monkeypatch.setattr(editors, "vscode_installed_version", lambda path: world["code"])
    monkeypatch.setattr(editors, "build_vscode", lambda repo: world["built"].append("code") or vsix)
    monkeypatch.setattr(editors, "install_vscode",
                        lambda path, file: world.__setitem__("code", available_code) or True)
    monkeypatch.setattr(editors, "visual_studios", lambda: [VS_2026])
    monkeypatch.setattr(editors, "vs_installed_version", lambda vs: world["vs"])
    monkeypatch.setattr(editors, "visual_studio_running", lambda vs: world["vs_running"])
    monkeypatch.setattr(editors, "build_visual_studio", lambda repo: world["built"].append("vs") or vsix)

    def install_vs(vs, file):
        world["vs_installs"].append(vs.instance_id)
        world["vs"] = available_vs
        return True
    monkeypatch.setattr(editors, "install_visual_studio", install_vs)
    world["available_code"], world["available_vs"] = available_code, available_vs
    return world


def editor_states(env, world):
    return {part.name: (state, part.note()) for part, state in wizard_for(env, Script()).table()
            if part.name.endswith("-extension")}


def test_the_table_says_what_each_editor_has(env, editor_world):
    states = editor_states(env, editor_world)

    assert states["vscode-extension"] == ("missing", f"not installed, {editor_world['available_code']} available")
    assert states["visualstudio-extension"][0] == "missing"
    editor_world["code"] = "0.1.0"
    assert editor_states(env, editor_world)["vscode-extension"][1] == (
        f"0.1.0 installed, {editor_world['available_code']} available")
    editor_world["code"] = editor_world["available_code"]
    assert editor_states(env, editor_world)["vscode-extension"] == (
        "done", f"{editor_world['available_code']}, up to date")


def test_vs_code_alone_then_visual_studio_later_each_touching_only_its_own_part(env, editor_world):
    config_before = config.CONFIG_FILE.read_text(encoding="utf-8")
    first = Script("y")

    assert wizard_for(env, first)._run_vscode() is True

    assert editor_world["built"] == ["code"] and editor_world["code"] == editor_world["available_code"]
    assert editor_world["vs"] is None and editor_world["vs_installs"] == []  # Visual Studio untouched
    states = editor_states(env, editor_world)
    assert states["vscode-extension"][0] == "done" and states["visualstudio-extension"][0] == "missing"

    later = Script("y")  # months later, Visual Studio only

    assert wizard_for(env, later)._run_visual_studio() is True

    assert editor_world["built"] == ["code", "vs"] and editor_world["vs_installs"] == ["87bd818d"]
    assert editor_world["vs"] == editor_world["available_vs"]
    assert config.CONFIG_FILE.read_text(encoding="utf-8") == config_before  # nothing but the extensions changed
    assert {state for state, _ in editor_states(env, editor_world).values()} == {"done"}


def test_after_an_update_only_older_installed_extensions_are_offered(env, editor_world):
    editor_world["code"] = "0.1.0"  # installed, older; Visual Studio has none, so setup's job, not the update's
    script = Script("y")

    wizard_for(env, script).offer_editor_updates()

    assert editor_world["built"] == ["code"] and editor_world["vs_installs"] == []
    assert len(script.questions) == 1 and "Update (0.1.0 to" in script.questions[0]


def test_after_an_update_a_current_install_is_not_asked_about(env, editor_world):
    editor_world["code"], editor_world["vs"] = editor_world["available_code"], editor_world["available_vs"]
    script = Script()

    wizard_for(env, script).offer_editor_updates()

    assert script.questions == [] and editor_world["built"] == []


def test_after_an_update_an_older_visual_studio_extension_is_offered(env, editor_world):
    editor_world["code"], editor_world["vs"] = editor_world["available_code"], "1.0.0"
    script = Script("y")

    wizard_for(env, script).offer_editor_updates()

    assert editor_world["vs_installs"] == ["87bd818d"] and "Update (1.0.0 to" in script.questions[0]


def test_a_released_build_is_used_instead_of_building_and_then_not_kept(env, editor_world):
    released = config.HOME / "downloads" / "claude-phone-0.4.0.vsix"
    released.parent.mkdir(parents=True)
    released.write_bytes(b"vsix")
    editor_world["release"] = released
    script = Script("y")

    assert wizard_for(env, script)._run_vscode() is True

    assert editor_world["built"] == [] and "Using the released build." in script.output
    assert not released.exists()  # a download is removed once installed


def test_a_local_build_is_the_fallback_when_there_is_no_release(env, editor_world):
    script = Script("y")

    assert wizard_for(env, script)._run_vscode() is True

    assert editor_world["built"] == ["code"] and "Building the extension" in script.output


def test_saying_no_to_an_extension_declines_it(env, editor_world):
    assert wizard_for(env, Script("n"))._run_vscode() is False
    assert editor_world["built"] == []

    script = Script("n")
    assert wizard_for(env, script)._run_visual_studio() is False
    assert editor_world["built"] == [] and editor_world["vs_installs"] == []


def test_an_older_extension_is_updated_and_a_current_one_is_left_alone(env, editor_world):
    editor_world["code"] = "0.1.0"
    script = Script("y")

    assert wizard_for(env, script)._run_vscode() is True
    assert "Update (0.1.0 to" in script.questions[0] and editor_world["built"] == ["code"]

    script = Script()
    assert wizard_for(env, script)._editor_state("vscode")[1] is True and script.questions == []


def test_visual_studio_must_be_closed_first(env, editor_world, monkeypatch):
    calls = []

    def running(vs):
        calls.append(1)
        return len(calls) < 3  # open for two checks, then closed
    monkeypatch.setattr(editors, "visual_studio_running", running)
    script = Script("y", "", "")

    assert wizard_for(env, script)._run_visual_studio() is True

    assert sum("must be closed" in q for q in script.questions) == 2 and editor_world["vs_installs"] == ["87bd818d"]


def test_visual_studio_can_be_skipped_while_it_is_open(env, editor_world, monkeypatch):
    monkeypatch.setattr(editors, "visual_studio_running", lambda vs: True)
    script = Script("y", "s")

    assert wizard_for(env, script)._run_visual_studio() is False
    assert editor_world["vs_installs"] == [] and editor_world["built"] == []


def test_a_missing_build_tool_says_what_to_install(env, editor_world, monkeypatch):
    def no_npm(repo):
        raise editors.ToolMissing("Node.js (npm)")
    monkeypatch.setattr(editors, "build_vscode", no_npm)
    script = Script("y")

    assert wizard_for(env, script)._run_vscode() is False
    assert "needs Node.js (npm)" in script.output


def test_a_failed_build_is_reported(env, editor_world, monkeypatch):
    def broken(repo):
        raise editors.BuildError("npm run package failed (exit 1): tsc error")
    monkeypatch.setattr(editors, "build_visual_studio", broken)
    script = Script("y")

    assert wizard_for(env, script)._run_visual_studio() is False
    assert "The build failed: npm run package failed" in script.output


def test_an_installer_that_did_not_take_is_not_reported_as_done(env, editor_world, monkeypatch):
    monkeypatch.setattr(editors, "install_visual_studio", lambda vs, file: True)  # exit 0, but the version stays
    script = Script("y")

    assert wizard_for(env, script)._run_visual_studio() is False
    assert "didn't put version" in script.output and "Open the .vsix by hand" in script.output


def test_an_old_or_other_edition_of_visual_studio_is_named_and_skipped(env, editor_world, monkeypatch):
    old = editors.VisualStudio("aa", "Visual Studio 2022", "17.9.1", config.REPO, VS_2026.product_id)
    enterprise = editors.VisualStudio("bb", "Visual Studio Enterprise 2026", "18.1", config.REPO,
                                      "Microsoft.VisualStudio.Product.Enterprise")
    monkeypatch.setattr(editors, "visual_studios", lambda: [old, enterprise])

    state = wizard_for(env, Script())._editor_state("vs")

    assert state[1] is True and "17.14 or newer, Community" in state[0]
    assert "Visual Studio 2022" in state[0] and "Enterprise 2026" in state[0]


def test_setup_needs_a_terminal():
    with pytest.raises(SystemExit, match="run it in a terminal"):
        cli.main(["setup"])
