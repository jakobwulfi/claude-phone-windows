"""WAV files as FreeSWITCH plays them: 16 kHz mono 16-bit. It plays that as is on a G.722 call and resamples it to
8 kHz on PCMU/PCMA."""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path


def to_wav16(src: Path, dst: Path) -> None:
    """Any audio file to 16 kHz mono 16-bit WAV. dst may carry a non-.wav suffix, so the format is named."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found: winget install Gyan.FFmpeg, then open a new terminal")
    subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", str(dst)],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    """Mono 16-bit PCM samples as a WAV file."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def is_wav16(path: Path | str) -> bool:
    with wave.open(str(path), "rb") as w:
        return (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)


def ensure_wav16(path: Path) -> None:
    """Converts a WAV in place unless it's already 16 kHz mono 16-bit."""
    if is_wav16(path):
        return
    tmp = path.with_name(path.stem + ".16k.wav")
    to_wav16(path, tmp)
    tmp.replace(path)
