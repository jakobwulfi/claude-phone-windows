"""Speech to text: the speech gate, whisper's timeout, waiting for FreeSWITCH's recording, and what's kept."""

import asyncio
import wave
from pathlib import Path

import pytest

from claude_phone import daemon, stt


def test_speech_gate(tmp_path, write_wav):
    assert stt.speech_seconds(write_wav(tmp_path / "quiet.wav", 1, 50)) == 0
    assert stt.speech_seconds(write_wav(tmp_path / "voice.wav", 1, 800)) > 0.9  # quiet handset speech: RMS ~565


def test_whisper_timeout_grows_with_the_recording(tmp_path, write_wav):
    write_wav(tmp_path / "short.wav", 2, 800)
    with wave.open(str(tmp_path / "long.wav"), "wb") as w:  # 5 minutes of silence, written in one go
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(bytes(2 * 16000 * 300))
    assert stt.wav_seconds(tmp_path / "long.wav") == 300
    assert stt.whisper_timeout(tmp_path / "short.wav") == 31
    assert stt.whisper_timeout(tmp_path / "long.wav") == 180
    assert stt.whisper_timeout(tmp_path / "missing.wav") == 30


def test_transcribe_waits_for_freeswitch_to_finish_the_recording(tmp_path, monkeypatch, controller, write_wav):
    path = tmp_path / "cj-1.wav"
    path.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")  # created when recording starts; the rest lands after hangup
    assert not stt.wav_complete(path)
    monkeypatch.setattr(daemon, "recording_path", lambda name: path)

    async def transcribe(p):
        stt.speech_seconds(p)  # raised "fmt chunk and/or data chunk missing" before the fix
        return "Green."
    monkeypatch.setattr(stt, "transcribe", transcribe)

    async def scenario():
        async def finish():
            await asyncio.sleep(0.3)
            write_wav(path, 0.5, 800)
        writer = asyncio.create_task(finish())
        text = await controller.transcribe("cj-1")
        await writer
        return text
    assert asyncio.run(scenario()) == "Green."


def test_transcribe_gives_up_on_a_missing_recording(tmp_path, monkeypatch, controller):
    monkeypatch.setattr(daemon, "recording_path", lambda name: tmp_path / "gone.wav")
    monkeypatch.setattr(daemon, "RECORDING_WAIT", 0.2)
    assert asyncio.run(controller.transcribe("gone")) == ""


@pytest.mark.parametrize("whisper_said, kept", [
    (" Ship it on Friday. [BEEP] (beeping) ", "Ship it on Friday."),  # the time-up beep echoing into the mic
    ("Thank you.", ""), ("[BLANK_AUDIO]", ""), ("(silence)", ""), (" you ", ""), ("...", ""),  # near-silence
])
def test_sound_tags_and_noise_are_dropped_from_transcripts(tmp_path, monkeypatch, write_wav, whisper_said, kept):
    class Reply:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        async def json(self):
            return {"text": whisper_said}

    class Session(Reply):
        def post(self, *a, **k):
            return Reply()
    monkeypatch.setattr(stt.aiohttp, "ClientSession", Session)
    assert asyncio.run(stt.transcribe(write_wav(tmp_path / "voice.wav", 1, 800))) == kept


@pytest.mark.parametrize("heard, kept", [("Ship it.", False), ("", True), (RuntimeError("whisper down"), True)])
def test_a_recording_is_deleted_once_transcribed_and_kept_when_not(tmp_path, monkeypatch, controller, write_wav,
                                                                     heard, kept):
    path = write_wav(tmp_path / "in-301-1.wav", 0.2, 800)
    monkeypatch.setattr(daemon, "recording_path", lambda name: path)

    async def transcribe(p):
        if isinstance(heard, Exception):
            raise heard
        return heard
    monkeypatch.setattr(stt, "transcribe", transcribe)
    text = asyncio.run(controller.transcribe("in-301-1"))
    assert text == (None if isinstance(heard, Exception) else heard)
    assert path.exists() == kept


def test_a_recording_that_cannot_be_deleted_still_delivers_the_text(tmp_path, monkeypatch, controller, write_wav):
    path = write_wav(tmp_path / "in-301-2.wav", 0.2, 800)
    monkeypatch.setattr(daemon, "recording_path", lambda name: path)

    async def transcribe(p):
        return "Ship it."
    monkeypatch.setattr(stt, "transcribe", transcribe)

    def locked(self, missing_ok=False):
        raise PermissionError(32, "being used by another process")
    monkeypatch.setattr(Path, "unlink", locked)
    assert asyncio.run(controller.transcribe("in-301-2")) == "Ship it."
