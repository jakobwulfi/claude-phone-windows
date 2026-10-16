"""The `phone` command line: how services are started."""

import os
import subprocess

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
