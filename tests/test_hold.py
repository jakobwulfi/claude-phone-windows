"""Hold music: Greensleeves synthesised once, unless a generated track takes its place."""

import wave
from pathlib import Path

import pytest

from claude_phone import config, hold


@pytest.fixture
def no_music():
    for name in ("hold-music.wav", "hold-greensleeves.wav"):
        (config.SOUNDS / name).unlink(missing_ok=True)
    yield
    (config.SOUNDS / "hold-music.wav").unlink(missing_ok=True)


def test_greensleeves_is_synthesised_once_as_16khz_mono(no_music, monkeypatch):
    path = Path(hold.render())
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        seconds = w.getnframes() / w.getframerate()
    assert seconds == pytest.approx(sum(beats for _, beats in hold.MELODY) * hold.BEAT + 0.5, abs=0.01)
    monkeypatch.setattr(hold, "_tone", lambda *a: pytest.fail("synthesised again"))
    assert hold.render() == path.as_posix()


def test_a_generated_track_plays_instead(no_music):
    generated = config.SOUNDS / "hold-music.wav"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_bytes(b"RIFF")
    assert hold.render() == generated.as_posix()


def test_the_bass_has_a_note_for_every_bar_after_the_pickup():
    assert sum(beats for _, beats in hold.MELODY) == 1 + 3 * len(hold.BASS)


@pytest.mark.parametrize("note, hz", [("A4", 440.0), ("A5", 880.0), ("C5", 523.25), ("F#4", 369.99)])
def test_notes_are_tuned_to_a440(note, hz):
    assert hold._freq(note) == pytest.approx(hz, abs=0.01)
