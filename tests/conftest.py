import math
import os
import struct
import tempfile
import wave

# Point every path at a throwaway home before claude_phone is imported: tests never touch the live
# registry, logs, keychain-backed config or sounds.
os.environ["CLAUDE_PHONE_HOME"] = tempfile.mkdtemp(prefix="cphone-test-")
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="cphone-test-claude-")  # session transcripts

import pytest

from claude_phone import config, configedit, daemon


@pytest.fixture(autouse=True)
def fresh_db():
    config.HOME.mkdir(parents=True, exist_ok=True)
    config.DB_PATH.unlink(missing_ok=True)
    yield


@pytest.fixture(autouse=True)
def fresh_config():
    """No settings or LAN address kept from an earlier test, which may have patched detect_lan_ip."""
    config.forget()
    yield


@pytest.fixture
def settings_file(monkeypatch):
    """A config.toml of the user's, with the phones' registration answered without FreeSWITCH."""
    path = config.CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('ring_ext = "201"\n', encoding="utf-8")
    monkeypatch.setattr(configedit, "_registered", lambda ext: "registered")
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def write_wav():
    """write_wav(path, seconds, amplitude, rate=16000): a 300 Hz tone, as a phone recording would hold speech."""
    def write(path, seconds, amp, rate=16000):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
            w.writeframes(b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 300 * i / rate)))
                                   for i in range(int(rate * seconds))))
        return path
    return write


@pytest.fixture
def controller() -> daemon.Controller:
    """The daemon's Controller with no event socket: nothing connects or starts."""
    return daemon.Controller(config.load(), esl=None)
