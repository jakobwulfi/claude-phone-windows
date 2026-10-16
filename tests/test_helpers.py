import asyncio
import math
import struct
import wave

import pytest

from claude_phone import daemon, stt, tts
from claude_phone.daemon import browser_xml, caller_id
from claude_phone.config import Settings
from claude_phone.registry import Session


def test_caller_id_fits_17():
    assert caller_id("claude-phone/master", 17) == "claude-phone/mas~"
    assert caller_id("lemon-stand/feat/ags-2", 17) == "lemon-stand/ags-2"
    assert caller_id("site-be-v2", 17) == "site-be-v2"
    for label in ("a-very-long-repository-name/feature/branch", "x" * 40):
        assert len(caller_id(label, 17)) <= 17


def test_spoken_label():
    assert tts.spoken_label("lemon-stand/feat/ags-2") == "lemon stand, feat ags 2"
    assert tts.spoken_label("site-be-v2") == "site be v2"


def test_browser_xml_lists_sessions_and_escapes():
    s = Session(session_id="x", ext=301, repo="a&b", branch="main", worktree=None, cwd=None, term_id=None,
                status="idle", started_at=0, updated_at=0)
    xml = browser_xml([s], Settings())
    assert "<Prompt>301 a&amp;b/main</Prompt><URI>301</URI>" in xml
    assert "<Title>Claude (1 live)</Title>" in xml


def _wav(path, seconds, amp):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 300 * i / 16000)))
                               for i in range(int(16000 * seconds))))


def test_speech_gate(tmp_path):
    _wav(tmp_path / "quiet.wav", 1, 50)
    _wav(tmp_path / "voice.wav", 1, 800)  # quiet handset speech: RMS ~565
    assert stt.speech_seconds(tmp_path / "quiet.wav") == 0
    assert stt.speech_seconds(tmp_path / "voice.wav") > 0.9


def test_whisper_timeout_grows_with_the_recording(tmp_path):
    _wav(tmp_path / "short.wav", 2, 800)
    with wave.open(str(tmp_path / "long.wav"), "wb") as w:  # 5 minutes of silence, written in one go
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(bytes(2 * 16000 * 300))
    assert stt.wav_seconds(tmp_path / "long.wav") == 300
    assert stt.whisper_timeout(tmp_path / "short.wav") == 31
    assert stt.whisper_timeout(tmp_path / "long.wav") == 180
    assert stt.whisper_timeout(tmp_path / "missing.wav") == 30


def test_wav_complete_only_once_the_header_is_written(tmp_path):
    (tmp_path / "empty.wav").write_bytes(b"")
    (tmp_path / "partial.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVE")  # no fmt or data chunk yet
    _wav(tmp_path / "done.wav", 0.1, 800)
    assert not stt.wav_complete(tmp_path / "missing.wav")
    assert not stt.wav_complete(tmp_path / "empty.wav")
    assert not stt.wav_complete(tmp_path / "partial.wav")
    assert stt.wav_complete(tmp_path / "done.wav")


def test_transcribe_waits_for_freeswitch_to_finish_the_recording(tmp_path, monkeypatch):
    path = tmp_path / "cj-1.wav"
    path.write_bytes(b"")  # created when recording starts; the header lands just after hangup
    monkeypatch.setattr(daemon, "recording_path", lambda name: path)

    async def transcribe(p):
        stt.speech_seconds(p)  # raised "fmt chunk and/or data chunk missing" before the fix
        return "Green."
    monkeypatch.setattr(stt, "transcribe", transcribe)

    async def scenario():
        async def finish():
            await asyncio.sleep(0.3)
            _wav(path, 0.5, 800)
        writer = asyncio.create_task(finish())
        text = await daemon.Controller.__new__(daemon.Controller).transcribe("cj-1")
        await writer
        return text
    assert asyncio.run(scenario()) == "Green."


def test_transcribe_gives_up_on_a_missing_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "recording_path", lambda name: tmp_path / "gone.wav")
    monkeypatch.setattr(daemon, "RECORDING_WAIT", 0.2)
    assert asyncio.run(daemon.Controller.__new__(daemon.Controller).transcribe("gone")) == ""


def test_sound_tags_are_dropped_from_transcripts(tmp_path, monkeypatch):
    _wav(tmp_path / "voice.wav", 1, 800)

    class Reply:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        async def json(self):
            return {"text": " Ship it on Friday. [BEEP] (beeping) "}

    class Session(Reply):
        def post(self, *a, **k):
            return Reply()
    monkeypatch.setattr(stt.aiohttp, "ClientSession", Session)
    assert asyncio.run(stt.transcribe(tmp_path / "voice.wav")) == "Ship it on Friday."


def test_message_seconds_stays_within_its_range():
    from claude_phone import config
    try:
        for given, used in ((5, 30), (300, 300), (9000, 600)):
            config.CONFIG_FILE.write_text(f"message_seconds = {given}")
            assert config.load().message_seconds == used
    finally:
        config.CONFIG_FILE.unlink(missing_ok=True)


def test_noise_transcripts_discarded():
    for t in ("Thank you.", "[BLANK_AUDIO]", "(silence)", "you", "..."):
        assert stt._NOISE.match(t)
    assert not stt._NOISE.match("Yes, ship it.")


def test_owner_name_sets_tool_suffix_and_sentence_case():
    assert Settings().owner_slug == "user" and Settings().owner == "The user"
    s = Settings(owner_name="Sam Lee")
    assert s.owner_slug == "sam_lee" and s.owner == "Sam Lee"


def test_lan_cidr_follows_detected_ip(monkeypatch):
    from claude_phone import config
    monkeypatch.setattr(config, "detect_lan_ip", lambda: "10.1.2.33")
    s = config.load()
    assert (s.lan_ip, s.lan_cidr) == ("10.1.2.33", "10.1.2.0/24")


def test_changing_the_default_voice_renders_new_speech(monkeypatch):
    from claude_phone import config
    rendered = []
    monkeypatch.setattr(tts.PROVIDERS["sapi"], "render", lambda text, voice, out: (rendered.append(voice), out.write_bytes(b"")))
    monkeypatch.setattr(config, "load", lambda: Settings(sapi_voice="Microsoft David Desktop"))
    a = tts.render("Hello there.")
    monkeypatch.setattr(config, "load", lambda: Settings(sapi_voice="Microsoft Zira Desktop"))
    b = tts.render("Hello there.")
    assert a != b and rendered == ["Microsoft David Desktop", "Microsoft Zira Desktop"]


def test_windows_speech_gets_the_text_outside_the_script(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tts.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or
                        tts.subprocess.CompletedProcess(argv, 0, "", ""))
    text = "It's $(Remove-Item x) \"quoted\""
    tts.PROVIDERS["sapi"].render(text, "Microsoft Zira Desktop", tmp_path / "out.wav")
    (argv, kw), = calls
    assert text not in " ".join(argv)
    assert kw["env"]["CP_TEXT"] == text and kw["env"]["CP_VOICE"] == "Microsoft Zira Desktop"


def test_lan_addresses_leave_out_loopback():
    from claude_phone import config
    assert not any(ip.startswith("127.") for ip in config.current_lan_ips())


@pytest.mark.parametrize("heard, kept", [("Ship it.", False), ("", True), (RuntimeError("whisper down"), True)])
def test_a_recording_is_deleted_once_transcribed_and_kept_when_not(tmp_path, monkeypatch, heard, kept):
    path = tmp_path / "in-301-1.wav"
    _wav(path, 0.2, 800)
    monkeypatch.setattr(daemon, "recording_path", lambda name: path)

    async def transcribe(p):
        if isinstance(heard, Exception):
            raise heard
        return heard
    monkeypatch.setattr(stt, "transcribe", transcribe)
    text = asyncio.run(daemon.Controller.__new__(daemon.Controller).transcribe("in-301-1"))
    assert text == (None if isinstance(heard, Exception) else heard)
    assert path.exists() == kept


def test_reused_speech_counts_as_used_and_expired_speech_is_rendered_again(monkeypatch):
    import os
    from pathlib import Path
    rendered = []
    monkeypatch.setattr(tts.PROVIDERS["sapi"], "render", lambda text, voice, out: (rendered.append(text), out.write_bytes(b"x")))
    path = Path(tts.render("Reuse me."))
    os.utime(path, (0, 0))  # as if last used in 1970
    assert tts.render("Reuse me.") == path.as_posix() and path.stat().st_mtime > 0 and rendered == ["Reuse me."]
    path.unlink()  # housekeeping expired it
    assert Path(tts.render("Reuse me.")).read_bytes() == b"x" and rendered == ["Reuse me.", "Reuse me."]


def test_a_recording_that_cannot_be_deleted_still_delivers_the_text(tmp_path, monkeypatch):
    from pathlib import Path
    path = tmp_path / "in-301-2.wav"
    _wav(path, 0.2, 800)
    monkeypatch.setattr(daemon, "recording_path", lambda name: path)

    async def transcribe(p):
        return "Ship it."
    monkeypatch.setattr(stt, "transcribe", transcribe)

    def locked(self, missing_ok=False):
        raise PermissionError(32, "being used by another process")
    monkeypatch.setattr(Path, "unlink", locked)
    assert asyncio.run(daemon.Controller.__new__(daemon.Controller).transcribe("in-301-2")) == "Ship it."
