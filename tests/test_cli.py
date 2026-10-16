"""The `phone` command line: starting services and changing settings."""

import argparse
import json
import os
import subprocess
import time
import tomllib

import pytest

from claude_phone import cli, config


class FakePopen:
    started: list[dict] = []

    def __init__(self, argv, **kwargs):
        self.pid = 4242
        FakePopen.started.append({"argv": argv, **kwargs})


def _spawn(monkeypatch, tmp_path, name: str, service: cli.Service) -> dict:
    FakePopen.started = []
    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli, "_pid", lambda n: None)
    monkeypatch.setattr(cli, "RUN", tmp_path / "run")
    monkeypatch.setattr(config, "LOGS", tmp_path)
    cli._spawn(name, service, config.Settings())
    return FakePopen.started[-1]


def test_spawn_applies_the_priority_and_adds_any_extra_env(monkeypatch, tmp_path):
    started = _spawn(monkeypatch, tmp_path, "whisper",
                     cli.Service(lambda s: ["prog.exe", "--flag"], subprocess.BELOW_NORMAL_PRIORITY_CLASS,
                                 {"OMP_WAIT_POLICY": "PASSIVE"}))
    assert started["argv"] == ["prog.exe", "--flag"]
    assert started["creationflags"] == cli.DETACHED | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    assert started["env"]["OMP_WAIT_POLICY"] == "PASSIVE"
    assert started["env"]["PATH"] == os.environ["PATH"]  # the rest of the environment is kept
    assert (cli.RUN / "whisper.pid").read_text() == "4242"
    plain = _spawn(monkeypatch, tmp_path, "daemon", cli.Service(lambda s: []))
    assert plain["creationflags"] == cli.DETACHED and plain["env"] is None  # inherits the environment as it is


def _pid_file(monkeypatch, tmp_path, written_at: float) -> None:
    """daemon.pid naming this test's own process, as if written at `written_at`."""
    monkeypatch.setattr(cli, "RUN", tmp_path)
    f = tmp_path / "daemon.pid"
    f.write_text(str(os.getpid()))
    os.utime(f, (written_at, written_at))


def test_a_pid_counts_when_its_process_started_before_the_pid_file(monkeypatch, tmp_path):
    _pid_file(monkeypatch, tmp_path, cli.psutil.Process().create_time() + 5)
    assert cli._pid("daemon") == os.getpid()


def test_a_pid_reused_after_a_reboot_is_not_the_service(monkeypatch, tmp_path):
    # The PC restarted after the pid file was written, and another program got the same PID.
    _pid_file(monkeypatch, tmp_path, cli.psutil.Process().create_time() - 3600)
    assert cli._pid("daemon") is None


def test_stop_leaves_a_process_that_reused_the_pid_alone(monkeypatch, tmp_path):
    _pid_file(monkeypatch, tmp_path, cli.psutil.Process().create_time() - 3600)
    monkeypatch.setattr(cli.psutil.Process, "terminate", lambda self: pytest.fail("terminated someone else's process"))
    cli._stop("daemon")
    assert not (tmp_path / "daemon.pid").exists()


def test_a_second_phone_up_leaves_the_starting_to_the_first(monkeypatch, tmp_path, capsys):
    # Two VS Code windows restored together both run `phone up`.
    monkeypatch.setattr(cli, "RUN", tmp_path)
    monkeypatch.setattr(cli, "render_freeswitch_config", lambda: pytest.fail("started a second time"))
    (tmp_path / "up.lock").touch()
    cli.cmd_up(argparse.Namespace(only=None))
    assert "another `phone up`" in capsys.readouterr().out


def test_a_lock_left_by_a_killed_phone_up_is_taken_over(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "RUN", tmp_path)
    lock = tmp_path / "up.lock"
    lock.touch()
    old = time.time() - cli.UP_LOCK_STALE_S - 1
    os.utime(lock, (old, old))
    assert cli._take_up_lock() == lock


def test_status_names_the_claude_code_hooks_that_are_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert cli.missing_hooks() == list(cli.HOOKS.values())  # no settings.json yet
    hook = {"type": "command", "command": "C:/x/phone.exe", "args": ["hook", "session-start"]}
    (tmp_path / "settings.json").write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [hook]}],
        "Stop": [{"hooks": [{"type": "command", "command": "C:/x/phone.exe hook stop"}]}],
    }}), encoding="utf-8")
    assert cli.missing_hooks() == ["session-end", "prompt", "pre-tool"]


def test_audio_usage_counts_recordings_and_speech(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RECORDINGS", tmp_path / "rec")
    monkeypatch.setattr(config, "SOUNDS", tmp_path / "sounds")
    (tmp_path / "rec").mkdir()
    (tmp_path / "sounds" / "tts").mkdir(parents=True)
    (tmp_path / "rec" / "a.wav").write_bytes(bytes(1_500_000))
    (tmp_path / "sounds" / "tts" / "b.wav").write_bytes(bytes(600_000))
    (tmp_path / "sounds" / "beep.wav").write_bytes(bytes(100))  # fixed sounds aren't counted
    assert cli.audio_usage() == "2 files, 2.1 MB (recordings 1, speech 1)"


def test_phone_config_lists_gets_sets_and_unsets(settings_file, capsys):
    cli.main(["config"])
    assert "callerid_max = 17  (default)" in capsys.readouterr().out
    cli.main(["config", "set", "whisper_text", "Psst.", "Meeting", "soon."])
    assert capsys.readouterr().out.startswith('Saved: whisper_text = "Psst. Meeting soon.".')
    cli.main(["config", "get", "whisper_text"])
    assert capsys.readouterr().out.startswith("whisper_text = Psst. Meeting soon. (set in config.toml")
    cli.main(["config", "unset", "whisper_text"])
    assert "back to the default" in capsys.readouterr().out
    assert tomllib.loads(settings_file.read_text(encoding="utf-8")) == {"ring_ext": "201"}


def test_phone_config_refuses_bad_values_with_a_message(settings_file):
    with pytest.raises(SystemExit, match="Not changed: must be from 10 to 40"):
        cli.main(["config", "set", "callerid_max", "41"])
    with pytest.raises(SystemExit, match="Usage: phone config set <setting> <value>"):
        cli.main(["config", "set", "callerid_max"])


def test_phone_config_render_rewrites_the_freeswitch_config(monkeypatch, capsys):
    rendered = []
    monkeypatch.setattr(cli, "render_freeswitch_config", lambda: rendered.append(True))
    cli.main(["config", "render"])
    assert rendered == [True] and "phone restart freeswitch" in capsys.readouterr().out


def test_change_phone_with_either_spelling(settings_file, capsys):
    cli.main(["change_phone"])
    assert "softphone (ext 201)" in capsys.readouterr().out
    cli.main(["change-phone", "DESK"])
    assert "will ring the desk phone (ext 200)" in capsys.readouterr().out
    assert 'ring_ext = "200"' in settings_file.read_text(encoding="utf-8")
