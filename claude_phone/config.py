"""Paths and settings. Settings come from ~/.claude-phone/config.toml (see config.example.toml)."""

from __future__ import annotations

import copy
import json
import os
import re
import socket
import sys
import time
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
# Optional voices, downloaded by `phone voice`: each engine's Python packages, and the voice files it reads.
ENGINES = HOME / "engines"
VOICES = HOME / "voices"
LOGS = HOME / "logs"
LOG_FILE = LOGS / "phone.log"
# One file per VS Code window, written by the claude-phone extension: how to reach that window's terminals.
BRIDGES = HOME / "bridges"
CONFIG_FILE = HOME / "config.toml"
# Where phone.exe is, for the VS Code extension to run `phone up` when autostart is on.
PHONE_FILE = HOME / "phone.json"
MESSAGE_SECONDS_MIN, MESSAGE_SECONDS_MAX = 30, 600
CALENDAR_SOURCES = ("google", "outlook")  # what calendar_sources may list; diary reads each one's ICS feed
LAN_CACHE_S = 30  # how long load() trusts a detected lan_ip before asking Windows again


@dataclass
class Settings:
    """What each setting is for: DESCRIPTIONS. The comments here give the reasons and measurements behind them."""
    owner_name: str = "the user"
    # Empty: this PC's address on its default route, and that /24. Pin both once the PC has a DHCP reservation.
    lan_ip: str = ""
    lan_cidr: str = ""
    desk_ext: str = "200"
    softphone_ext: str = "201"
    ring_ext: str = "200"
    voicemail_ext: str = "500"
    allhands_ext: str = "9"
    operator_ext: str = "0"
    # Voices, here and below, are "engine:name"; an engine alone is its default voice.
    operator_voice: str = "windows:Microsoft Zira Desktop"
    first_session_ext: int = 301  # not 1xx: UK 101 is the police, 111 the NHS
    last_session_ext: int = 399
    freeswitch_exe: str = r"C:\Program Files\FreeSWITCH\FreeSwitchConsole.exe"
    esl_port: int = 8021
    # Where the phones register. Linphone on the same PC holds 5060 itself (and resets any change on start),
    # so FreeSWITCH then needs another port, e.g. 5070, with the phone's domain set to <lan_ip>:5070.
    sip_port: int = 5060
    daemon_port: int = 8765
    whisper_port: int = 8910
    # On a Ryzen 5 5500 CPU: ~1.8 s per answer. large-v3-turbo-q5_0 took ~8.4 s (fine with a GPU), base.en-q5_1 ~0.55 s.
    whisper_model: str = "ggml-small.en-q5_1.bin"
    whisper_server: str = ""
    whisper_language: str = "en"  # "da", "auto", ...: a multilingual model has no ".en" in its name
    ring_seconds: int = 25
    # Answers to Claude's call_<owner> questions. The whole call (ring + question + answer + transcription) must end
    # within Claude Code's 120 s limit for MCP calls, so keep ring_seconds + record_seconds <= 90.
    record_seconds: int = 60
    # Messages you dial in with (a session, all-hands, a voicemail reply), kept within MESSAGE_SECONDS_MIN-MAX.
    # Nothing waits on these, so they can run long; whisper takes ~25 s for 5 minutes on a 6-core CPU.
    message_seconds: int = 300
    callerid_max: int = 17  # T31P shows 17 chars before scrolling (tested)
    voice: str = ""
    # Yealink Push XML: summaries of calls and meetings on the desk phone's screen. The phone must list lan_ip
    # as its Push XML Server. desk_ip: empty means the address the phone registered from.
    screen_push: bool = False
    desk_ip: str = ""
    # Meeting alerts: a whisper and a screen warn_minutes before each meeting, and Claude's calls are held
    # (voicemail, no pushes) from then until it ends. Meetings come from the calendar feeds (ICS) of the
    # sources listed here; each feed's link is a secret stored with `phone calendar add google|outlook`.
    meeting_alerts: bool = False
    meeting_warn_minutes: int = 2
    calendar_sources: list = field(default_factory=lambda: ["google"])
    calendar_refresh_minutes: int = 5
    calendar_email: str = ""  # empty: the feed's name, if that's an address
    # The meeting whisper, played on the speaker without ringing. "elevenlabs_v3" acts out tags like "[whispers]".
    whisper_voice: str = ""
    whisper_text: str = "Pssst. You have a meeting."
    elevenlabs_model: str = "eleven_turbo_v2_5"
    repo_voices: dict = field(default_factory=dict)  # repo name -> "engine:name"
    # Phonebook for the T31P's Directory: the one listener on the LAN, limited to lan_cidr.
    phonebook_port: int = 8766
    # repo name -> T31P ringtone 1-8. Unlisted repos get a stable pick from their name.
    repo_ringtones: dict = field(default_factory=dict)
    # Push to the ntfy app when a call is missed. The topic is a secret in the keychain (`phone push`).
    ntfy_server: str = "https://ntfy.sh"
    push_text: bool = True
    autostart: bool = False

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
    "operator_voice": "the operator's voice, as engine:name (`phone voice` lists them)",
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
    "ring_seconds": "how long Claude's calls ring before voicemail",
    "record_seconds": "how long you can answer Claude's call",
    "message_seconds": "how long you can talk when you dial in (30-600)",
    "callerid_max": "the longest caller ID (repo/branch) shown on the phone",
    "voice": "Claude's voice, as engine:name (empty: the Windows default voice); `phone voice` switches it",
    "screen_push": "show call and meeting summaries on a Yealink desk phone's screen",
    "desk_ip": "the desk phone's address for screen pushes (empty: where it registered from)",
    "meeting_alerts": "warn before meetings and hold Claude's calls during them",
    "meeting_warn_minutes": "how many minutes before a meeting the warning comes",
    "calendar_sources": "the calendars the meeting alerts read",
    "calendar_refresh_minutes": "how often the calendars are read again",
    "calendar_email": "your address, to skip meetings you declined (empty: the calendar's name)",
    "whisper_voice": "the meeting whisper's voice, as engine:name (empty: Claude's voice)",
    "whisper_text": "what the meeting whisper says",
    "elevenlabs_model": "the ElevenLabs model",
    "repo_voices": "a voice per repo: [repo_voices] with <repo> = \"engine:name\"",
    "phonebook_port": "the port on lan_ip for the T31P's Directory and DND key",
    "repo_ringtones": "a Yealink ringtone (1-8) per repo",
    "ntfy_server": "the ntfy server missed-call pushes go through",
    "push_text": "include the question in missed-call pushes (false: only who called)",
    "autostart": "start claude-phone when VS Code opens (false: only with `phone up` or /claude-phone)",
}


def _read() -> dict:
    # UTF-8 whatever the console's code page: names may hold æøå.
    return tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}


# --- the voice settings from before `voice = "engine:name"` ---

# The voice engines, as `phone voice` and config.toml name them.
VOICE_ENGINES = ("windows", "piper", "kokoro", "elevenlabs", "elevenlabs_v3")
# Still read, and written in the new form the next time `phone config` or `phone voice` changes something.
LEGACY_KEYS = ("tts_provider", "sapi_voice", "elevenlabs_voice_id", "operator_provider", "whisper_provider")


def engine(word: str) -> str:
    """An engine's name as written anywhere: config.toml used to call the Windows voices "sapi"."""
    word = word.strip().lower()
    return "windows" if word == "sapi" else word


def _is_voice(value: object) -> bool:
    """"engine:name", or an engine alone (its default voice)."""
    return isinstance(value, str) and engine(value.partition(":")[0]) in VOICE_ENGINES


def _old_name(value: object) -> bool:
    """A voice's name alone, as operator_voice and whisper_voice used to hold. A Windows voice or an ElevenLabs id
    has no colon; one with a colon is engine:name, if perhaps a mistyped one (which problems() reports)."""
    return isinstance(value, str) and bool(value.strip()) and ":" not in value and not _is_voice(value)


def _old_repo_tables(raw: dict) -> bool:
    repos = raw.get("repo_voices")
    return isinstance(repos, dict) and any(isinstance(v, dict) for v in repos.values())


def legacy(raw: dict) -> list[str]:
    """The old voice settings in config.toml's raw data."""
    return ([k for k in LEGACY_KEYS if k in raw] + [k for k in ("operator_voice", "whisper_voice")
                                                     if _old_name(raw.get(k))]
            + (["repo_voices tables"] if _old_repo_tables(raw) else []))


def migrate_legacy(raw: dict) -> dict:
    """raw with the old voice settings in the new form. A new key that has a value of its own keeps it."""
    new = {k: v for k, v in raw.items() if k not in LEGACY_KEYS}
    old = {k: raw[k].strip() for k in LEGACY_KEYS if isinstance(raw.get(k), str)}  # others: problems() says so
    named = {"windows": old.get("sapi_voice", ""), "elevenlabs": old.get("elevenlabs_voice_id", ""),
             "elevenlabs_v3": old.get("elevenlabs_voice_id", "")}

    def voice(eng: object, name: object = "") -> str:
        """An engine without a name spoke with sapi_voice or elevenlabs_voice_id, if set."""
        eng = engine(str(eng))
        name = str(name or "").strip() or named.get(eng, "")
        return f"{eng}:{name}" if name else eng

    # Claude's voice: tts_provider (sapi by default) with its voice's name.
    given, mine = str(new.get("voice", "")).strip(), ""
    if given in named:  # an engine alone: an old setting may name its voice
        mine = voice(given)
    elif given:
        mine = given
    elif old:
        mine = voice(old.get("tts_provider", "windows"))
        if mine == "windows":
            mine = ""  # the Windows default voice, as an empty voice already is
    if mine != given:
        new["voice"] = mine
    # The operator: operator_voice was a name in operator_provider's engine.
    op, provider = new.get("operator_voice"), old.get("operator_provider", "")
    if _old_name(op):
        new["operator_voice"] = voice(provider or "windows", op)
    elif op is None and provider and engine(provider) != "windows":
        new["operator_voice"] = voice(provider)  # its engine's own voice, not Zira's name
    # The meeting whisper: whisper_voice was a name in whisper_provider's engine, and ignored without one.
    wv, provider = new.get("whisper_voice", ""), old.get("whisper_provider", "")
    if _old_name(wv) or (wv == "" and provider):
        new["whisper_voice"] = voice(provider, wv) if provider else ""
    # [repo_voices.<repo>] tables of provider and voice. One without a provider meant Claude's voice.
    if _old_repo_tables(new):
        new["repo_voices"] = {repo: voice(v["provider"], v.get("voice")) if isinstance(v, dict) else v
                              for repo, v in new["repo_voices"].items()
                              if not isinstance(v, dict) or v.get("provider")}
    return new


def _voice_problems(raw: dict) -> list[str]:
    """Old settings of the wrong type, and voices naming an engine there isn't (a typo, most likely)."""
    found = [f"{k}: expected text, not {raw[k]!r}; ignored" for k in LEGACY_KEYS
             if k in raw and not isinstance(raw[k], str)]
    named = [("voice", raw.get("voice"))] + [(k, raw.get(k)) for k in ("operator_voice", "whisper_voice")
                                             if not _old_name(raw.get(k))]
    repos = raw.get("repo_voices")
    named += [(f"repo_voices.{r}", v) for r, v in repos.items()] if isinstance(repos, dict) else []
    for key, value in named:
        if isinstance(value, str) and value.strip() and not _is_voice(value):
            found.append(f"{key}: there's no voice engine called {value.partition(':')[0].strip()!r} "
                         f"({', '.join(VOICE_ENGINES)}), so the default voice speaks")
    return found


def unknown_keys() -> list[str]:
    """Keys in config.toml that aren't settings (typos); load() ignores them."""
    known = {f.name for f in fields(Settings)} | set(LEGACY_KEYS)
    return sorted(k for k in _read() if k not in known)


# A setting's type (its default's) -> how a value from config.toml is taken as one, None when it can't be.
_KINDS = {
    int: ("a whole number", lambda v: v if type(v) is int else int(v) if isinstance(v, str) and v.isdigit() else None),
    str: ("text", lambda v: v if isinstance(v, str) else str(v) if type(v) is int else None),  # desk_ext = 200
    bool: ("true or false", lambda v: v if isinstance(v, bool) else None),
    list: ("a list", lambda v: v if isinstance(v, list) else [v] if isinstance(v, str) else None),
    dict: ("a table", lambda v: v if isinstance(v, dict) else None),
}


def _parse(raw: dict) -> tuple[Settings, list[str]]:
    """The settings in config.toml, each as its setting's type; a value that can't be keeps the default, with a
    message saying so."""
    s, problems = Settings(), []
    known = {f.name for f in fields(Settings)}
    for k, v in raw.items():
        if k not in known:
            continue
        default = getattr(s, k)
        what, convert = _KINDS[type(default)]
        value = convert(v)
        if value is None:
            problems.append(f"{k}: expected {what}, not {v!r}; using the default ({default!r})")
        else:
            setattr(s, k, value)
    return s, problems


def problems() -> list[str]:
    """What's wrong in config.toml: unknown keys, values of the wrong type, and limits load() can't enforce."""
    found = []
    if unknown := unknown_keys():
        found.append(f"Not recognised in {CONFIG_FILE.name}, so ignored (a typo?): {', '.join(unknown)}")
    raw = _read()
    if old := legacy(raw):
        found.append(f"Old voice settings ({', '.join(old)}) still work; `phone config migrate` rewrites them in "
                     "the new form (as does the next `phone config set` or `phone voice`)")
    found += _voice_problems(raw)
    s, wrong = _parse(migrate_legacy(raw))
    found += wrong
    if (total := s.ring_seconds + s.record_seconds) > 90:
        found.append(f"ring_seconds + record_seconds is {total} s: keep it at 90 or less, or a call can outlast "
                     "Claude Code's 120 s limit for a tool")
    return found


# load() runs several times a call. config.toml's bytes and what they hold, kept until the file changes: keyed by
# its bytes, not its mtime, which can miss two writes of the same size in a row. And the detected LAN address.
_parsed: tuple[bytes, Settings] | None = None
_lan: tuple[float, str] | None = None


def forget() -> None:
    """Drops what load() keeps, so the next load reads and detects afresh."""
    global _parsed, _lan
    _parsed = _lan = None


def _lan_ip() -> str:
    """The detected address, kept LAN_CACHE_S. Not finding one isn't kept: it's looked for again next time."""
    global _lan
    now, lan = time.monotonic(), _lan  # one read: forget() may run meanwhile
    if lan is None or now - lan[0] >= LAN_CACHE_S:
        ip = detect_lan_ip() or ""
        if not ip:
            return ""
        lan = _lan = now, ip
    return lan[1]


def load() -> Settings:
    """The settings, parsed again only when config.toml changes. Each caller gets its own copy to change."""
    global _parsed
    cached = _parsed  # one read: forget() may run meanwhile
    try:
        data = CONFIG_FILE.read_bytes()
    except FileNotFoundError:
        data = b""
    except OSError:  # held open for a moment by an editor or a virus scanner
        if cached is None:
            raise
        data = cached[0]
    if cached is None or cached[0] != data:
        s, _ = _parse(migrate_legacy(tomllib.loads(data.decode("utf-8"))))  # UTF-8 whatever the code page
        s.message_seconds = min(max(int(s.message_seconds), MESSAGE_SECONDS_MIN), MESSAGE_SECONDS_MAX)
        cached = _parsed = data, s
    s = copy.deepcopy(cached[1])
    if not s.lan_ip:
        s.lan_ip = _lan_ip()
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


def remember_phone_exe() -> None:
    """Leaves phone.exe's path (beside this venv's python.exe) where the VS Code extension looks for it."""
    exe = Path(sys.executable).with_name("phone.exe")
    if exe.exists():
        HOME.mkdir(parents=True, exist_ok=True)
        PHONE_FILE.write_text(json.dumps({"exe": str(exe)}), encoding="utf-8")


def ensure_dirs() -> None:
    for d in (HOME, FREESWITCH_CONF, SOUNDS, RECORDINGS, MODELS, LOGS):
        d.mkdir(parents=True, exist_ok=True)
