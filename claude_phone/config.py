"""Paths and settings. Settings come from ~/.claude-phone/config.toml (see config.example.toml)."""

from __future__ import annotations

import os
import re
import socket
import tomllib
from dataclasses import dataclass, field, fields
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
MESSAGE_SECONDS_MIN, MESSAGE_SECONDS_MAX = 30, 600


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
    # Answers to Claude's call_<owner> questions. The whole call (ring + question + answer + transcription) must end
    # within Claude Code's 120 s limit for MCP calls, so keep ring_seconds + record_seconds <= 90.
    record_seconds: int = 60
    # Messages you dial in with (a session, all-hands, a voicemail reply), kept within MESSAGE_SECONDS_MIN-MAX.
    # Nothing waits on these, so they can run long; whisper takes ~25 s for 5 minutes on a 6-core CPU.
    message_seconds: int = 300
    callerid_max: int = 17  # T31P shows 17 chars before scrolling (tested 2026-10-09)
    tts_provider: str = "sapi"
    sapi_voice: str = ""  # empty: the Windows default voice
    # Yealink Push XML: summaries of calls and meetings on the desk phone's screen. The phone must list lan_ip
    # as its Push XML Server. desk_ip: empty means the address the phone registered from.
    screen_push: bool = False
    desk_ip: str = ""
    # Meeting alerts: a whisper and a screen warn_minutes before each meeting, and Claude's calls are held
    # (voicemail, no pushes) from then until it ends. Meetings come from the calendar feeds (ICS) of the
    # sources listed here; each feed's link is a secret stored with `phone calendar add google|outlook`.
    meeting_alerts: bool = False
    meeting_warn_minutes: int = 2
    calendar_sources: list = field(default_factory=lambda: ["google"])  # "google", "outlook", or both
    calendar_refresh_minutes: int = 5
    calendar_email: str = ""  # your address, to skip meetings you declined; empty: the feed's name, if an address
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


# What each setting is for: the comment on its line when `phone config` writes config.toml.
DESCRIPTIONS = {
    "owner_name": "your name: spoken on calls; the tools become call_<name> and notify_<name>",
    "lan_ip": "this PC's address on the LAN, where the phones register (empty: detected)",
    "lan_cidr": "who may reach the phonebook and DND listener (empty: lan_ip's /24)",
    "desk_ext": "the desk phone's extension",
    "softphone_ext": "the softphone's extension (Linphone)",
    "ring_ext": "the phone Claude's calls ring: `phone change_phone desk|softphone`",
    "voicemail_ext": "dial it to hear and answer Claude's waiting questions",
    "allhands_ext": "dial it to send one message to every session",
    "operator_ext": "dial it and say which session you want",
    "operator_provider": "the operator's voice provider: sapi, elevenlabs or elevenlabs_v3",
    "operator_voice": "the operator's voice (`phone voices` lists the Windows ones)",
    "first_session_ext": "the first extension handed to sessions",
    "last_session_ext": "the last extension handed to sessions",
    "freeswitch_exe": "where FreeSWITCH is installed",
    "esl_port": "FreeSWITCH's event socket, on 127.0.0.1 only",
    "sip_port": "where the phones register (5070 when Linphone on this PC holds 5060)",
    "daemon_port": "the daemon, on 127.0.0.1 only",
    "whisper_port": "whisper-server, on 127.0.0.1 only",
    "whisper_model": "the speech-to-text model file",
    "whisper_server": "the full path to whisper-server.exe (empty: found on PATH)",
    "whisper_language": "the language you speak (other than en needs a multilingual model)",
    "ring_seconds": "how long Claude's calls ring before voicemail (ring + answer must fit in 90 s)",
    "record_seconds": "how long you can answer Claude's call",
    "message_seconds": "how long you can talk when you dial in (30-600)",
    "callerid_max": "the longest caller ID (repo/branch) shown on the phone",
    "tts_provider": "Claude's voice provider: sapi (Windows voices), elevenlabs or elevenlabs_v3",
    "sapi_voice": "the Windows voice Claude speaks with (empty: the default voice)",
    "screen_push": "show call and meeting summaries on a Yealink desk phone's screen",
    "desk_ip": "the desk phone's address for screen pushes (empty: where it registered from)",
    "meeting_alerts": "warn before meetings and hold Claude's calls during them",
    "meeting_warn_minutes": "how many minutes before a meeting the warning comes",
    "calendar_sources": "the calendars the meeting alerts read: google, outlook or both",
    "calendar_refresh_minutes": "how often the calendars are read again",
    "calendar_email": "your address, to skip meetings you declined (empty: the calendar's name)",
    "whisper_provider": "the meeting whisper's voice provider: sapi, elevenlabs or elevenlabs_v3",
    "whisper_voice": "the meeting whisper's voice (empty: Claude's voice for that provider)",
    "whisper_text": "what the meeting whisper says",
    "elevenlabs_voice_id": "the ElevenLabs voice, used wherever a voice provider is elevenlabs",
    "elevenlabs_model": "the ElevenLabs model",
    "repo_voices": "a voice per repo: [repo_voices.<repo>] with provider and voice",
    "phonebook_port": "the port on lan_ip for the T31P's Directory and DND key",
    "repo_ringtones": "a Yealink ringtone (1-8) per repo",
    "ntfy_server": "the ntfy server missed-call pushes go through",
    "push_text": "include the question in missed-call pushes (false: only who called)",
}


def _read() -> dict:
    # UTF-8 whatever the console's code page: names may hold æøå.
    return tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}


def unknown_keys() -> list[str]:
    """Keys in config.toml that aren't settings (typos); load() ignores them."""
    known = {f.name for f in fields(Settings)}
    return sorted(k for k in _read() if k not in known)


def load() -> Settings:
    s = Settings()
    known = {f.name for f in fields(Settings)}
    for k, v in _read().items():
        if k in known:
            setattr(s, k, v)
    s.message_seconds = min(max(int(s.message_seconds), MESSAGE_SECONDS_MIN), MESSAGE_SECONDS_MAX)
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
