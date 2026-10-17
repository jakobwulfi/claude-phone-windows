"""Speech to text via a resident whisper-server (model stays loaded: ~0.3 s per clip vs 5 s cold)."""

from __future__ import annotations

import array
import asyncio
import itertools
import math
import re
import wave
from collections.abc import Iterator
from pathlib import Path

import aiohttp

from . import config, log, registry

# Seconds whisper may take: a base, plus a share of the audio's length (5 minutes took ~27 s on a 6-core CPU).
WHISPER_TIMEOUT_S = 30
WHISPER_TIMEOUT_PER_AUDIO_S = 0.5
# Sound tags whisper adds, like [BEEP] or (beeping): the time-up beep can echo into a speakerphone's mic.
_TAGS = re.compile(r"\[[^\]]*\]|\([^)]*\)")
# Phrases whisper produces from near-silence.
_NOISE = re.compile(r"^\s*(\[[^\]]*\]|\([^)]*\)|thank you\.?|thanks for watching!?|you|\.+)?\s*$", re.I)


# T31P handset speech can peak at RMS ~550; line silence sits well under 100.
SPEECH_THRESHOLD = 200
FRAMES_PER_S = 50  # 20 ms frames


def _loud(w: wave.Wave_read, threshold: int) -> Iterator[bool]:
    """Per whole 20 ms frame of a 16-bit mono WAV: whether its RMS exceeds threshold (compared squared, summed in C)."""
    step = w.getframerate() // FRAMES_PER_S
    limit = threshold * threshold * step
    while len(data := w.readframes(step)) == step * 2:
        frame = array.array("h", data)
        yield math.sumprod(frame, frame) > limit


def speech_seconds(path: Path, threshold: int = SPEECH_THRESHOLD) -> float:
    """Rough voiced duration: seconds of 20 ms frames whose RMS exceeds threshold."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            return 1.0  # unknown format: let whisper decide
        return sum(_loud(w, threshold)) / FRAMES_PER_S


def has_speech(path: Path, min_seconds: float = 0.1, threshold: int = SPEECH_THRESHOLD) -> bool:
    """At least min_seconds of voiced frames. Stops at the first min_seconds' worth: only a silent recording is
    read to the end."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            return True  # unknown format: let whisper decide
        needed = round(min_seconds * FRAMES_PER_S)
        return any(n >= needed for n in itertools.accumulate(_loud(w, threshold)))


def wav_seconds(path: Path) -> float:
    """Length of a WAV from its header; 0 if it can't be read."""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, OSError):
        return 0.0


def whisper_timeout(path: Path) -> float:
    return WHISPER_TIMEOUT_S + WHISPER_TIMEOUT_PER_AUDIO_S * wav_seconds(path)


def wav_complete(path: Path) -> bool:
    """True once the WAV has its header and data chunk. FreeSWITCH creates the file when recording
    starts but only finishes the header on close, a moment after the hangup."""
    try:
        with wave.open(str(path), "rb"):
            return True
    except (wave.Error, EOFError, OSError):
        return False


def vocabulary() -> str:
    """Prompt whisper with live repo and branch names so it spells them right."""
    words: set[str] = set()
    for s in registry.all_sessions():
        for part in (s.repo, s.branch, s.worktree):
            if part:
                words.add(part.replace("-", " ").replace("/", " "))
    return ("Claude Code. " + ", ".join(sorted(words)) + ".") if words else "Claude Code."


async def transcribe(path: Path) -> str:
    """Returns the transcript, or '' when nothing was said."""
    lg = log.get("stt")
    if not await asyncio.to_thread(has_speech, path):  # a long silent recording would block the daemon
        lg.info("no speech in %s", path.name)
        return ""
    s = config.load()
    form = aiohttp.FormData()
    form.add_field("file", await asyncio.to_thread(path.read_bytes), filename="audio.wav", content_type="audio/wav")
    form.add_field("response_format", "json")
    form.add_field("temperature", "0.0")
    form.add_field("prompt", vocabulary())
    async with aiohttp.ClientSession() as http:
        timeout = aiohttp.ClientTimeout(total=whisper_timeout(path))
        async with http.post(f"{s.whisper_url}/inference", data=form, timeout=timeout) as r:
            r.raise_for_status()
            text = (await r.json())["text"]
    text = " ".join(_TAGS.sub(" ", text).split())
    if _NOISE.match(text):
        lg.info("discarded noise transcript %r", text)
        return ""
    return text
