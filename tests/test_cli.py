"""The `phone` command line: starting services and changing settings."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import tomllib
from typing import ClassVar

import pytest

from claude_phone import cli, config, procs


class FakePopen:
    started: ClassVar[list[dict]] = []

    def __init__(self, argv, **kwargs):
        self.pid = 4242
        FakePopen.started.append({"argv": argv, **kwargs})


def _spawn(monkeypatch, tmp_path, name: str, service: cli.Service) -> dict:
    FakePopen.started = []
    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(procs, "pid", lambda n: None)
    monkeypatch.setattr(procs, "RUN", tmp_path / "run")
    monkeypatch.setattr(config, "LOGS", tmp_path)
    cli._spawn(name, service, config.Settings())
    return FakePopen.started[-1]


def test_spawn_applies_the_priority_and_adds_any_extra_env(monkeypatch, tmp_path):
    started = _spawn(monkeypatch, tmp_path, "whisper",
                     cli.Service(lambda s: ["prog.exe", "--flag"], subprocess.BELOW_NORMAL_PRIORITY_CLASS,
                                 {"OMP_WAIT_POLICY": "PASSIVE"}))
    assert started["argv"] == ["prog.exe", "--flag"]
    assert started["creationflags"] == procs.DETACHED | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    assert started["env"]["OMP_WAIT_POLICY"] == "PASSIVE"
    assert started["env"]["PATH"] == os.environ["PATH"]  # the rest of the environment is kept
    assert (procs.RUN / "whisper.pid").read_text() == "4242"
    plain = _spawn(monkeypatch, tmp_path, "daemon", cli.Service(lambda s: []))
    assert plain["creationflags"] == procs.DETACHED and plain["env"] is None  # inherits the environment as it is


def _pid_file(monkeypatch, tmp_path, written_at: float) -> None:
    """daemon.pid naming this test's own process, as if written at `written_at`."""
    monkeypatch.setattr(procs, "RUN", tmp_path)
    f = tmp_path / "daemon.pid"
    f.write_text(str(os.getpid()))
    os.utime(f, (written_at, written_at))


def test_a_pid_counts_when_its_process_started_before_the_pid_file(monkeypatch, tmp_path):
    _pid_file(monkeypatch, tmp_path, procs.psutil.Process().create_time() + 5)
    assert procs.pid("daemon") == os.getpid()


def test_a_pid_reused_after_a_reboot_is_not_the_service(monkeypatch, tmp_path):
    # The PC restarted after the pid file was written, and another program got the same PID.
    _pid_file(monkeypatch, tmp_path, procs.psutil.Process().create_time() - 3600)
    assert procs.pid("daemon") is None


def test_stop_leaves_a_process_that_reused_the_pid_alone(monkeypatch, tmp_path):
    _pid_file(monkeypatch, tmp_path, procs.psutil.Process().create_time() - 3600)
    monkeypatch.setattr(procs.psutil.Process, "terminate",
                        lambda self: pytest.fail("terminated someone else's process"))
    cli._stop("daemon")
    assert not (tmp_path / "daemon.pid").exists()


def test_a_second_phone_up_leaves_the_starting_to_the_first(monkeypatch, tmp_path, capsys):
    # Two VS Code windows restored together both run `phone up`.
    monkeypatch.setattr(procs, "RUN", tmp_path)
    monkeypatch.setattr(cli, "render_freeswitch_config", lambda: pytest.fail("started a second time"))
    (tmp_path / "up.lock").touch()
    cli.cmd_up(argparse.Namespace(only=None))
    assert "another `phone up`" in capsys.readouterr().out


def test_a_lock_left_by_a_killed_phone_up_is_taken_over(monkeypatch, tmp_path):
    monkeypatch.setattr(procs, "RUN", tmp_path)
    lock = tmp_path / "up.lock"
    lock.touch()
    old = time.time() - cli.UP_LOCK_STALE_S - 1
    os.utime(lock, (old, old))
    assert cli._take_up_lock() == lock


def test_starting_only_the_daemon_needs_no_lan_address_or_speech_model(monkeypatch, tmp_path):
    # Away from home: the daemon still takes missed-call pushes. FreeSWITCH's config and whisper's model are theirs.
    monkeypatch.setattr(procs, "RUN", tmp_path)
    monkeypatch.setattr(config, "detect_lan_ip", lambda: None)
    monkeypatch.setattr(cli, "ensure_model", lambda **kw: pytest.fail("fetched whisper's model"))
    started = []
    monkeypatch.setattr(cli, "_spawn", lambda name, svc, s: started.append(name))
    cli.cmd_up(argparse.Namespace(only="daemon"))
    assert started == ["daemon"]


def _up(monkeypatch, tmp_path, spawn) -> list[str]:
    """`phone up` with nothing really started: what it did, in order."""
    done = []
    monkeypatch.setattr(procs, "RUN", tmp_path)
    monkeypatch.setattr(cli, "render_freeswitch_config", lambda: done.append("config"))
    monkeypatch.setattr(cli, "ensure_model", lambda keep_alive: done.append("model"))
    monkeypatch.setattr(cli, "_answers", lambda port, wait_s: done.append(f"port {port}") or True)
    monkeypatch.setattr(cli, "_spawn", lambda name, svc, s: spawn(name) or done.append(name))
    return done


def test_up_starts_each_service_once_the_one_before_answers_and_lets_go_of_the_lock(monkeypatch, tmp_path):
    done = _up(monkeypatch, tmp_path, lambda name: None)
    cli.cmd_up(argparse.Namespace(only=None))
    s = config.load()
    assert done == ["config", "model", "freeswitch", f"port {s.esl_port}", "whisper", f"port {s.whisper_port}",
                    "daemon"]
    assert not (tmp_path / "up.lock").exists()


def test_a_service_that_never_answers_is_named(monkeypatch, tmp_path, capsys):
    _up(monkeypatch, tmp_path, lambda name: None)
    monkeypatch.setattr(cli, "_answers", lambda port, wait_s: False)
    cli.cmd_up(argparse.Namespace(only="whisper"))
    assert f"whisper: not answering on port {config.load().whisper_port} after {cli.SERVICE_WAIT_S} s" \
        in capsys.readouterr().out


def test_a_port_answers_once_something_listens_on_it():
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        port = listening.getsockname()[1]
        assert cli._answers(port, 1)
    started = time.monotonic()
    assert not cli._answers(port, 0.3)  # closed now
    assert time.monotonic() - started < 3


def test_a_failed_start_lets_go_of_the_up_lock(monkeypatch, tmp_path):
    def broken(name):
        raise OSError("FreeSwitchConsole.exe not found")
    _up(monkeypatch, tmp_path, broken)
    with pytest.raises(OSError):
        cli.cmd_up(argparse.Namespace(only=None))
    assert not (tmp_path / "up.lock").exists()  # or every later `phone up` would wait for it to go stale


def test_stopping_the_daemon_asks_it_to_hang_up_its_calls_first(monkeypatch):
    asked, stopped = [], []

    class Reply:
        def close(self):
            pass
    monkeypatch.setattr(procs, "pid", lambda name: 4242)
    monkeypatch.setattr(procs, "stop", lambda name: stopped.append((name, len(asked))) or True)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: asked.append(req.full_url) or Reply())
    cli._stop("daemon")
    assert asked == [f"{config.load().daemon_url}/shutdown"] and stopped == [("daemon", 1)]
    cli._stop("whisper")  # the others have no calls to end
    assert len(asked) == 1


def test_a_daemon_that_doesnt_answer_is_stopped_anyway(monkeypatch):
    def refused(req, timeout):
        raise ConnectionRefusedError("nothing on the port")
    stopped = []
    monkeypatch.setattr(procs, "pid", lambda name: 4242)
    monkeypatch.setattr(procs, "stop", lambda name: stopped.append(name) or True)
    monkeypatch.setattr("urllib.request.urlopen", refused)
    cli._stop("daemon")
    assert stopped == ["daemon"]


def test_down_stops_the_daemon_first_and_freeswitch_last(monkeypatch):
    stopped = []
    monkeypatch.setattr(cli, "_stop", stopped.append)
    cli.cmd_down(argparse.Namespace(only=None))
    assert stopped == ["daemon", "whisper", "freeswitch"]


def test_a_service_already_running_is_not_started_twice(monkeypatch, capsys):
    monkeypatch.setattr(procs, "pid", lambda name: 4242)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **kw: pytest.fail("started twice"))
    cli._spawn("daemon", cli.SERVICES["daemon"], config.Settings())
    assert "daemon: already running" in capsys.readouterr().out


def test_stop_ends_a_running_service_and_forgets_its_pid(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(procs, "RUN", tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (tmp_path / "daemon.pid").write_text(str(child.pid))
        cli._stop("daemon")
        assert child.wait(timeout=5) is not None
    finally:
        child.kill()
    assert not (tmp_path / "daemon.pid").exists() and "daemon: stopped" in capsys.readouterr().out


def test_a_long_model_download_keeps_the_up_lock_fresh(monkeypatch, tmp_path):
    # The model is ~500 MB: on a slow line the download outlasts UP_LOCK_STALE_S, and a second `phone up` would
    # take the lock over and start everything twice.
    monkeypatch.setattr(config, "MODELS", tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])

    def urlretrieve(url, path, reporthook):
        for _ in range(4):
            clock[0] += 20
            reporthook(1, 8192, 500_000_000)
        path.write_bytes(b"model")
    monkeypatch.setattr("urllib.request.urlretrieve", urlretrieve)
    touched = []
    cli.ensure_model(keep_alive=lambda: touched.append(clock[0]))
    assert touched == [1040, 1080]  # every LOCK_TOUCH_S, not every block
    assert cli.model_path().read_bytes() == b"model"


def test_a_hook_loads_only_what_it_needs():
    # Hooks run on every prompt and turn, each a process of its own: the keychain (keyring) and the web client
    # took half their start-up time. A fresh process, since this one has imported everything already.
    code = ("import io, sys\nsys.stdin = io.StringIO('{}')\nfrom claude_phone import cli\n"
            "try:\n    cli.main(['hook', 'stop'])\nexcept SystemExit:\n    pass\n"
            "heavy = ('keyring', 'urllib.request', 'argparse', 'claude_phone.diary')\n"
            "print(sorted(m for m in heavy if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


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
