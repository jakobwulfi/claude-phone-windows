import os
import time

from claude_phone import config, housekeeping, registry


def _file(path, age_days, size=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - age_days * 86400
    os.utime(path, (t, t))
    return path


def test_kept_recordings_expire_after_a_day_and_speech_after_a_week_but_pending_question_kept():
    rec = config.RECORDINGS
    old_rec, new_rec = _file(rec / "old.wav", 1.1), _file(rec / "new.wav", 0.5)
    tts = config.SOUNDS / "tts"
    old_tts, new_tts, pending = _file(tts / "old.wav", 8), _file(tts / "new.wav", 6), _file(tts / "pending.wav", 40)
    registry.add_voicemail("s", 301, "repo", "Q?", None, pending.as_posix())
    done = housekeeping.run()
    assert (done["recordings"], done["speech"]) == (1, 1)
    assert not old_rec.exists() and new_rec.exists()
    assert not old_tts.exists() and new_tts.exists() and pending.exists()


def test_log_rotation(monkeypatch):
    monkeypatch.setattr(housekeeping, "LOG_MAX_BYTES", 100)
    log = config.LOG_FILE
    for i in range(4):
        _file(log, 0, size=200)
        assert housekeeping._rotate(log)
    assert sorted(p.name for p in log.parent.glob("phone.log.*")) == ["phone.log.1", "phone.log.2", "phone.log.3"]
