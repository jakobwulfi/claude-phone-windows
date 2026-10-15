"""Paths and settings. Settings come from ~/.claude-phone/config.toml (see config.example.toml)."""

from __future__ import annotations

import os
import re
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import psutil

HOME = Path(os.environ.get("CLAUDE_PHONE_HOME", Path.home() / ".claude-phone"))
REPO = Path(__file__).resolve().parent.parent

# FreeSWITCH runs from its install folder with everything it writes under here: config, log, db, run, cache.
FREESWITCH = HOME / "freeswitch"
FREESWITCH_CONF = FREESWITCH / "conf"
DB_PATH = HOME / "registry.db"
SOUNDS = HOME / "sounds"
RECORDINGS = HOME / "recordings"
MODELS = HOME / "models"
LOGS = HOME / "logs"
LOG_FILE = LOGS / "phone.log"
# One file per VS Code window, written by the claude-phone extension: how to reach that window's terminals.
BRIDGES = HOME / "bridges"
CONFIG_FILE = HOME / "config.toml"


@dataclass
class Settings:
    # Who the phone is for. Spoken and typed as this name; the MCP tools become call_<name> and notify_<name>.
    owner_name: str = "the user"
    # Empty: this PC's address on its default route, and that /24. Pin both once the PC has a DHCP reservation.
    lan_ip: str = ""
    lan_cidr: str = ""
    desk_ext: str = "200"
    softphone_ext: str = "201"
    # Which endpoint call_<owner> rings. Switch to softphone_ext while testing on Linphone.
    ring_ext: str = "200"
    voicemail_ext: str = "500"
    allhands_ext: str = "9"
    operator_ext: str = "0"
    operator_provider: str = "sapi"
    operator_voice: str = "Microsoft Zira Desktop"  # `phone voices` lists the installed Windows voices
    first_session_ext: int = 301  # not 1xx: UK 101 is the police, 111 the NHS
    last_session_ext: int = 399
    freeswitch_exe: str = r"C:\Program Files\FreeSWITCH\FreeSwitchConsole.exe"
    esl_port: int = 8021  # FreeSWITCH's event socket, on 127.0.0.1 only
    # Where the phones register. Linphone on the same PC holds 5060 itself (and resets any change on start),
    # so FreeSWITCH then needs another port, e.g. 5070, with the phone's domain set to <lan_ip>:5070.
    sip_port: int = 5060
    daemon_port: int = 8765
    whisper_port: int = 8910
    # On a Ryzen 5 5500 CPU: ~1.8 s per answer. large-v3-turbo-q5_0 took ~8.4 s (fine with a GPU), base.en-q5_1 ~0.55 s.
    whisper_model: str = "ggml-small.en-q5_1.bin"
    # Empty: whisper-server on PATH. Else the full path to whisper-server.exe.
    whisper_server: str = ""
    whisper_language: str = "en"  # "da", "auto", ...: needs a multilingual model (no ".en" in its name)
    ring_seconds: int = 25
    record_seconds: int = 60
    callerid_max: int = 17  # T31P shows 17 chars before scrolling (tested 2026-10-09)
    tts_provider: str = "sapi"
    sapi_voice: str = ""  # empty: the Windows default voice
    # Yealink Push XML: summaries of calls and meetings on the desk phone's screen. The phone must list lan_ip
    # as its Push XML Server. desk_ip: empty means the address the phone registered from.
    screen_push: bool = False
    desk_ip: str = ""
    # Meeting alerts: a whisper and a screen warn_minutes before each meeting, and Claude's calls are held
    # (voicemail, no pushes) from then until it ends. No calendar source on Windows yet, so they stay quiet.
    meeting_alerts: bool = False
    meeting_warn_minutes: int = 2
    meeting_calendars: list = field(default_factory=list)
    # The meeting whisper, played on the speaker without ringing. "elevenlabs_v3" acts out tags like "[whispers]".
    whisper_provider: str = "sapi"
    whisper_voice: str = ""
    whisper_text: str = "Pssst. You have a meeting."
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"
    elevenlabs_model: str = "eleven_turbo_v2_5"
    # repo name -> {"provider": ..., "voice": ...}
    repo_voices: dict = field(default_factory=dict)
    # Phonebook for the T31P's Directory: the one listener on the LAN, limited to lan_cidr.
    phonebook_port: int = 8766
    # repo name -> T31P ringtone 1-8. Unlisted repos get a stable pick from their name.
    repo_ringtones: dict = field(default_factory=dict)
    # Push to the ntfy app when a call is missed. The topic is a secret in the keychain (`phone push`).
    ntfy_server: str = "https://ntfy.sh"
    push_text: bool = True  # include the question/announcement; False sends only who called

    @property
    def owner(self) -> str:
        """owner_name for the start of a sentence."""
        return self.owner_name[:1].upper() + self.owner_name[1:]

    @property
    def owner_slug(self) -> str:
        """'Sam Lee' -> 'sam_lee', 'the user' -> 'user': the suffix of the MCP tool names."""
        name = re.sub(r"^the\s+", "", self.owner_name.strip().lower())
        return re.sub(r"[^a-z0-9]+", "_", name).strip("_") or "user"

    @property
    def daemon_url(self) -> str:
        return f"http://127.0.0.1:{self.daemon_port}"

    @property
    def whisper_url(self) -> str:
        return f"http://127.0.0.1:{self.whisper_port}"


def load() -> Settings:
    s = Settings()
    if CONFIG_FILE.exists():
        data = tomllib.loads(CONFIG_FILE.read_text())
        for k, v in data.items():
            if hasattr(s, k):
                setattr(s, k, v)
    if not s.lan_ip:
        s.lan_ip = detect_lan_ip() or ""
    if s.lan_ip and not s.lan_cidr:
        s.lan_cidr = s.lan_ip.rsplit(".", 1)[0] + ".0/24"
    return s


def bound_ip() -> str | None:
    """The address the generated FreeSWITCH config listens on, which stays put when a detected lan_ip moves."""
    conf = FREESWITCH_CONF / "freeswitch.xml"
    m = re.search(r'data="lan_ip=([\d.]+)"', conf.read_text()) if conf.exists() else None
    return m.group(1) if m else None


def detect_lan_ip() -> str | None:
    """IPv4 address of the interface on this PC's default route. Connecting a UDP socket sends nothing;
    it only makes Windows pick the outgoing interface."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed anywhere real
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith(("0.", "127.")) else ip


def current_lan_ips() -> set[str]:
    """IPv4 addresses on this PC, to spot the Wi-Fi address drifting away from lan_ip."""
    return {a.address for addrs in psutil.net_if_addrs().values() for a in addrs
            if a.family == socket.AF_INET and not a.address.startswith("127.")}


def ip_warning() -> str | None:
    s = load()
    ip = bound_ip() or s.lan_ip
    if not ip:
        return "This PC has no network address, so the desk phone can't be reached."
    if ip in current_lan_ips():
        return None
    return (f"This PC doesn't have {ip}, so the desk phone can't be reached: calls go to voicemail "
            f"and missed-call pushes. Away from home, that's expected. If the home address changed for good, set "
            f"lan_ip in {CONFIG_FILE}, run `phone restart freeswitch`, and update the SIP server on the phones.")


def ensure_dirs() -> None:
    for d in (HOME, FREESWITCH_CONF, SOUNDS, RECORDINGS, MODELS, LOGS):
        d.mkdir(parents=True, exist_ok=True)
