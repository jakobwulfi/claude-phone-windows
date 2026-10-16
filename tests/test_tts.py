"""Text to speech: which voice renders, reusing speech, sentences, and joining recordings."""

import os
import wave
from pathlib import Path

import pytest

from claude_phone import config, tts
from claude_phone.config import Settings


def test_changing_the_default_voice_renders_new_speech(monkeypatch):
    rendered = []
    monkeypatch.setattr(tts.PROVIDERS["sapi"], "render",
                        lambda text, voice, out: (rendered.append(voice), out.write_bytes(b"")))
    monkeypatch.setattr(config, "load", lambda: Settings(sapi_voice="Microsoft David Desktop"))
    a = tts.render("Hello there.")
    monkeypatch.setattr(config, "load", lambda: Settings(sapi_voice="Microsoft Zira Desktop"))
    b = tts.render("Hello there.")
    assert a != b and rendered == ["Microsoft David Desktop", "Microsoft Zira Desktop"]


def test_windows_speech_gets_the_text_outside_the_script(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tts.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or
                        tts.subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(tts, "_ensure_wav16", lambda path: None)
    text = "It's $(Remove-Item x) \"quoted\""
    tts.PROVIDERS["sapi"].render(text, "Microsoft Zira Desktop", tmp_path / "out.wav")
    (argv, kw), = calls
    assert text not in " ".join(argv)
    assert kw["env"]["CP_TEXT"] == text and kw["env"]["CP_VOICE"] == "Microsoft Zira Desktop"


def test_reused_speech_counts_as_used_and_expired_speech_is_rendered_again(monkeypatch):
    rendered = []
    monkeypatch.setattr(tts.PROVIDERS["sapi"], "render",
                        lambda text, voice, out: (rendered.append(text), out.write_bytes(b"x")))
    path = Path(tts.render("Reuse me."))
    os.utime(path, (0, 0))  # as if last used in 1970
    assert tts.render("Reuse me.") == path.as_posix() and path.stat().st_mtime > 0 and rendered == ["Reuse me."]
    path.unlink()  # housekeeping expired it
    assert Path(tts.render("Reuse me.")).read_bytes() == b"x" and rendered == ["Reuse me.", "Reuse me."]


@pytest.mark.parametrize("text, parts", [
    ("Done. The tests pass, all two hundred of them. Shall I merge it now or wait for you?",
     ["Done. The tests pass, all two hundred of them.", "Shall I merge it now or wait for you?"]),
    ("Done. The tests pass, all two hundred of them. Merge it?",
     ["Done. The tests pass, all two hundred of them. Merge it?"]),  # a short last one isn't on its own
    ("Use e.g. the 3.5 model, or ask Dr. Who.", ["Use e.g. the 3.5 model, or ask Dr. Who."]),
    ("The whole build took a while to finish, approx. 5 minutes on this machine and more on the server.",
     ["The whole build took a while to finish, approx. 5 minutes on this machine and more on the server."]),
    ("That fix went into the release we shipped as No. 5 earlier today, along with the others.",
     ["That fix went into the release we shipped as No. 5 earlier today, along with the others."]),
    ('It said "Stop!" Then it stopped working entirely, for good.',
     ['It said "Stop!" Then it stopped working entirely, for good.']),
    ("One long first sentence, long enough to stand alone. Then a second one of good length. Ok.",
     ["One long first sentence, long enough to stand alone.", "Then a second one of good length. Ok."]),
    ("  ", []),
])
def test_text_is_spoken_in_sentences_with_short_ones_kept_with_the_next(text, parts):
    assert tts.sentences(text) == parts


def test_sentences_are_joined_into_one_recording_for_voicemail(tmp_path, monkeypatch, write_wav):
    monkeypatch.setattr(tts.config, "SOUNDS", tmp_path)
    a, b = write_wav(tmp_path / "a.wav", 0.5, 800), write_wav(tmp_path / "b.wav", 0.25, 800)
    joined = tts.join_wavs([str(a), str(b)])
    with wave.open(joined) as w:
        assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (16000, 1, 12000)
    assert tts.join_wavs([str(a), str(b)]) == joined  # reused, not joined again
    assert tts.join_wavs([str(a)]) == str(a)


def test_a_part_in_another_format_is_converted_before_joining(tmp_path, monkeypatch, write_wav):
    monkeypatch.setattr(tts.config, "SOUNDS", tmp_path)
    a = write_wav(tmp_path / "a.wav", 0.5, 800)
    b = write_wav(tmp_path / "b.wav", 0.5, 0, rate=24000)  # 24 kHz, as Kokoro writes before conversion
    with wave.open(tts.join_wavs([str(a), str(b)])) as w:
        assert w.getframerate() == 16000 and abs(w.getnframes() - 16000) < 50
