"""Hourly tidy-up run by the daemon: recordings of the owner's voice and unused speech expire, logs rotate.
The injection audit trail lives in the database and is never pruned."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import config, registry

# A recording is deleted as soon as it's transcribed; one that failed is kept a day so it can be checked.
RECORDING_DAYS = 1
# Cached speech expires by last use (tts.render touches a file it reuses); a phrase unused for a week is rendered
# again when next needed.
SPEECH_DAYS = 7
LOG_MAX_BYTES = 5_000_000
LOG_KEEP = 3


def _expire(folder: Path, cutoff: float, keep: set[Path] = frozenset()) -> int:
    n = 0
    for p in folder.glob("*") if folder.exists() else ():
        try:
            if p.is_file() and p not in keep and p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:  # gone already, or still open: next hour
            pass
    return n


def _rotate(path: Path) -> bool:
    """Copy, then truncate in place: Windows can't rename a log that the daemon's handler holds open."""
    if not path.exists() or path.stat().st_size < LOG_MAX_BYTES:
        return False
    for i in range(LOG_KEEP - 1, 0, -1):
        older = path.with_name(f"{path.name}.{i}")
        if older.exists():
            older.replace(path.with_name(f"{path.name}.{i + 1}"))
    shutil.copyfile(path, path.with_name(f"{path.name}.1"))
    return _truncate(path)


def _truncate(path: Path) -> bool:
    """Service stdout files are held open by the running process, so truncate in place instead of renaming."""
    if path.stat().st_size < LOG_MAX_BYTES:
        return False
    with open(path, "r+b") as f:
        f.truncate(0)
    return True


def run(now: float | None = None) -> dict:
    now = now or time.time()
    # A pending voicemail still needs its question audio.
    pending = {Path(v.audio) for v in registry.pending_voicemails() if v.audio}
    return {
        "recordings": _expire(config.RECORDINGS, now - RECORDING_DAYS * 86400),
        "speech": _expire(config.SOUNDS / "tts", now - SPEECH_DAYS * 86400, pending),
        "rotated": _rotate(config.LOG_FILE),
        "truncated": sum(_truncate(p) for p in config.LOGS.glob("*.out")),
    }
