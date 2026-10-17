"""Text to speech: which voice renders, reusing speech, sentences, and joining recordings."""

import os
import types
import wave
from pathlib import Path

import pytest

from claude_phone import audio, config, tts
from claude_phone.config import Settings


def test_changing_claudes_voice_renders_new_speech(monkeypatch):
    rendered = []
    monkeypatch.setattr(tts.PROVIDERS["windows"], "render",
                        lambda text, voice, out: (rendered.append(voice), out.write_bytes(b"")))
    monkeypatch.setattr(config, "load", lambda: Settings(voice="windows:Microsoft David Desktop"))
    a = tts.render("Hello there.")
    monkeypatch.setattr(config, "load", lambda: Settings(voice="windows:Microsoft Zira Desktop"))
    b = tts.render("Hello there.")
    assert a != b and rendered == ["Microsoft David Desktop", "Microsoft Zira Desktop"]


@pytest.mark.parametrize("engine, default", [("kokoro", "af_heart"), ("elevenlabs", tts.ELEVENLABS_DEFAULT),
                                             ("windows", "")])
def test_an_engine_alone_speaks_with_its_default_voice(monkeypatch, engine, default):
    spoke = []
    monkeypatch.setattr(tts.PROVIDERS[engine], "render", lambda text, voice, out: (spoke.append(voice),
                                                                                   out.write_bytes(b"")))
    tts.render(f"Default {engine}.", voice=(engine, None))
    assert spoke == [default]


def test_an_engine_there_isnt_says_so_rather_than_a_key_error():
    with pytest.raises(RuntimeError, match="no voice engine called 'espeak'"):
        tts.render("Hello.", voice=("espeak", "x"), fallback=False)


def test_an_elevenlabs_voice_id_cant_change_the_request_path(monkeypatch):
    asked = []

    class Stop(Exception):
        pass

    def urlopen(req, timeout):
        asked.append(req.full_url)
        raise Stop
    monkeypatch.setattr(tts.keychain, "elevenlabs_key", lambda: "key")
    monkeypatch.setattr(tts.urllib.request, "urlopen", urlopen)
    with pytest.raises(Stop):
        tts.PROVIDERS["elevenlabs"].render("Hi.", "../../v1/user?x=1", Path("out.wav"))
    assert asked == ["https://api.elevenlabs.io/v1/text-to-speech/..%2F..%2Fv1%2Fuser%3Fx%3D1"
                     "?output_format=pcm_16000"]


def test_a_failing_voice_hands_over_to_the_windows_default_voice(monkeypatch):
    rendered = []

    def broken(text, voice, out):
        raise RuntimeError("the piper engine didn't start")
    monkeypatch.setattr(tts.PROVIDERS["piper"], "render", broken)
    monkeypatch.setattr(tts.PROVIDERS["windows"], "render",
                        lambda text, voice, out: (rendered.append(voice), out.write_bytes(b"")))
    monkeypatch.setattr(config, "load", lambda: Settings(voice="piper:en_US-ryan-medium"))
    tts.render("Stand in for me.")
    assert rendered == [""]  # the default voice: there's no Windows voice setting of its own any more


INJECTION = "It's $(Remove-Item x) \"quoted\" `n æøå."


class FakeWorker:
    def __init__(self, raises=None):
        self.raises, self.asked = raises, []

    def render(self, **req):
        self.asked.append(req)
        if self.raises:
            raise self.raises


@pytest.fixture
def windows(monkeypatch):
    """The Windows voice with a fake worker (windows.worker), its one-off script recorded (windows.ran) and a
    settable clock (windows.clock)."""
    from claude_phone import voiceworker
    provider, w = tts.PROVIDERS["windows"], types.SimpleNamespace(worker=FakeWorker(), ran=[], clock=[1000.0], made=[])
    monkeypatch.setattr(provider, "worker_down_until", 0.0)  # put back after the test, whatever it sets
    monkeypatch.setattr(tts, "time", types.SimpleNamespace(monotonic=lambda: w.clock[0]))
    monkeypatch.setattr(voiceworker, "command_worker",
                        lambda engine, command, **kw: w.made.append((engine, command, kw)) or w.worker)
    monkeypatch.setattr(tts.subprocess, "run", lambda argv, **kw: w.ran.append((argv, kw)) or
                        tts.subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(audio, "ensure_wav16", lambda path: None)
    w.render = lambda: provider.render(INJECTION, "Microsoft Zira Desktop", Path("out.wav"))
    return w


def test_windows_speech_comes_from_one_worker_with_the_text_as_data(windows):
    windows.render()
    assert windows.worker.asked == [{"text": INJECTION, "voice": "Microsoft Zira Desktop", "out": "out.wav"}]
    assert windows.made == [("windows", tts.WINDOWS_WORKER, {"first_timeout": tts.WINDOWS_FIRST_TIMEOUT_S})]
    assert windows.ran == []


@pytest.mark.parametrize("down", ["hung", "no powershell"])
def test_the_one_off_script_stands_in_for_a_while_when_the_worker_is_down(windows, down):
    from claude_phone import voiceworker
    windows.worker.raises = (voiceworker.Unavailable("the windows engine didn't answer in 20 s")
                             if down == "hung" else FileNotFoundError("powershell"))
    windows.render()
    (argv, kw), = windows.ran
    assert INJECTION not in " ".join(argv)
    assert kw["env"]["CP_TEXT"] == INJECTION and kw["env"]["CP_VOICE"] == "Microsoft Zira Desktop"
    # Not tried again for every sentence: a hung worker would cost each one its timeout before the script.
    windows.clock[0] += tts.WINDOWS_WORKER_BACKOFF_S - 1
    windows.render()
    assert len(windows.worker.asked) == 1 and len(windows.ran) == 2
    windows.clock[0] += 1
    windows.worker.raises = None
    windows.render()
    assert len(windows.worker.asked) == 2 and len(windows.ran) == 2


def test_text_the_windows_voice_fails_on_is_not_tried_again_by_the_one_off_script(windows):
    windows.worker.raises = RuntimeError("IOException: Could not find a part of the path")
    with pytest.raises(RuntimeError, match="part of the path"):
        windows.render()
    assert windows.ran == []


def test_the_windows_worker_speaks_each_request_and_lets_go_of_its_file(tmp_path):
    # The real PowerShell loop, with the default voice (every Windows has one): stray output would break a reply
    # line, and a WAV still held open couldn't be moved into the speech cache.
    from claude_phone import voiceworker
    made = tmp_path / "made"
    text = f"Don't run $(New-Item '{made}') or \"this\" `n æøå."
    names = [line.split("\t")[0] for line in tts.windows_voice_lines()]
    onecore = next((n for n in names if not n.endswith("Desktop")), None)
    w = voiceworker.Worker("windows", tts.WINDOWS_WORKER)
    try:
        # A voice that isn't installed keeps the default.
        for name, voice in (("default", ""), ("unknown", "No Such Voice"), ("onecore", onecore)):
            if voice is None:
                continue
            out = tmp_path / f"{name}.tmp.wav"
            w.render(text=text, voice=voice, out=str(out))
            out.rename(tmp_path / f"{name}.wav")
            with wave.open(str(tmp_path / f"{name}.wav")) as wav:
                assert wav.getnframes() > wav.getframerate()  # over a second of speech
        assert not made.exists()  # spoken, not run
        if onecore:  # really spoken in that voice, not the default one
            assert (tmp_path / "onecore.wav").read_bytes() != (tmp_path / "default.wav").read_bytes()
        # A request that fails leaves the worker answering the next.
        with pytest.raises(RuntimeError) as failed:
            w.render(text="Hello.", voice="", out=str(tmp_path / "no such folder" / "x.wav"))
        assert not isinstance(failed.value, voiceworker.Unavailable)
        w.render(text="Hello.", voice="", out=str(tmp_path / "after.wav"))
        assert (tmp_path / "after.wav").stat().st_size > 1000
    finally:
        with w.lock:
            w.stop()


def test_reused_speech_counts_as_used_and_expired_speech_is_rendered_again(monkeypatch):
    rendered = []
    monkeypatch.setattr(tts.PROVIDERS["windows"], "render",
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
