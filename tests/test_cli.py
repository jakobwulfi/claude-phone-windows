"""The `phone` command line: starting services and changing settings."""

import os
import subprocess

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


def test_freeswitch_starts_at_high_priority_and_whisper_below_normal():
    assert cli.SERVICES["freeswitch"].priority == subprocess.HIGH_PRIORITY_CLASS
    assert cli.SERVICES["whisper"].priority == subprocess.BELOW_NORMAL_PRIORITY_CLASS
    assert cli.SERVICES["daemon"].priority == 0
    assert cli.SERVICES["whisper"].env == {"OMP_WAIT_POLICY": "PASSIVE"}


def test_spawn_applies_the_priority_and_adds_the_extra_env(monkeypatch, tmp_path):
    started = _spawn(monkeypatch, tmp_path, "whisper",
                     cli.Service(lambda s: ["prog.exe", "--flag"], subprocess.BELOW_NORMAL_PRIORITY_CLASS,
                                 {"OMP_WAIT_POLICY": "PASSIVE"}))
    assert started["argv"] == ["prog.exe", "--flag"]
    assert started["creationflags"] == cli.DETACHED | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    assert started["env"]["OMP_WAIT_POLICY"] == "PASSIVE"
    assert started["env"]["PATH"] == os.environ["PATH"]  # the rest of the environment is kept
    assert (cli.RUN / "whisper.pid").read_text() == "4242"


def test_spawn_without_extras_inherits_the_environment(monkeypatch, tmp_path):
    started = _spawn(monkeypatch, tmp_path, "daemon", cli.Service(lambda s: []))
    assert started["creationflags"] == cli.DETACHED
    assert started["env"] is None


def test_audio_usage_counts_recordings_and_speech(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RECORDINGS", tmp_path / "rec")
    monkeypatch.setattr(config, "SOUNDS", tmp_path / "sounds")
    (tmp_path / "rec").mkdir()
    (tmp_path / "sounds" / "tts").mkdir(parents=True)
    (tmp_path / "rec" / "a.wav").write_bytes(bytes(1_500_000))
    (tmp_path / "sounds" / "tts" / "b.wav").write_bytes(bytes(600_000))
    (tmp_path / "sounds" / "beep.wav").write_bytes(bytes(100))  # fixed sounds aren't counted
    assert cli.audio_usage() == "2 files, 2.1 MB (recordings 1, speech 1)"


@pytest.fixture
def settings_file(monkeypatch):
    from claude_phone import configedit
    path = config.CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('# mine\nring_ext = "201"\n', encoding="utf-8")
    monkeypatch.setattr(configedit, "_registered", lambda ext: "registered")
    yield path
    path.unlink(missing_ok=True)


def test_phone_config_lists_gets_sets_and_unsets(settings_file, capsys):
    cli.main(["config"])
    assert "callerid_max = 17  (default)" in capsys.readouterr().out
    cli.main(["config", "set", "whisper_text", "Psst.", "Meeting", "soon."])
    assert capsys.readouterr().out.startswith('Saved: whisper_text = "Psst. Meeting soon.".')
    cli.main(["config", "get", "whisper_text"])
    assert capsys.readouterr().out.startswith("whisper_text = Psst. Meeting soon. (set in config.toml")
    cli.main(["config", "unset", "whisper_text"])
    assert "back to the default" in capsys.readouterr().out
    assert settings_file.read_text(encoding="utf-8") == '# mine\nring_ext = "201"\n'


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
