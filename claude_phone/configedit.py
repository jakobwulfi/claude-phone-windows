"""The settings a user may change from `phone config`, /claude-phone config and chat, and `change_phone`.
config.toml is rewritten with tomlkit, so its comments and order survive. Nothing restarts: each change says
what has to be restarted for it to apply."""

from __future__ import annotations

import os
import re
import tempfile
import time
import tomllib
from dataclasses import dataclass, fields
from typing import Any, Callable

from . import config, keychain

RESTART = "phone restart daemon"
APPLIES = {
    "live": "Takes effect at the next calendar refresh (within a few minutes).",
    "daemon": f"Takes effect after `{RESTART}`.",
    "sessions": f"Takes effect after `{RESTART}`; Claude Code sessions get the new tool names (call_<name>) "
                "once they are started again.",
}
# The whole call_<owner> call must end within Claude Code's 120 s limit for MCP calls.
RING_PLUS_RECORD_MAX = 90
PHONES = {"desk": "desk_ext", "softphone": "softphone_ext"}
_TRUE, _FALSE = {"true", "yes", "on", "1"}, {"false", "no", "off", "0"}
# A name is spoken on calls and typed into sessions' prompts, so only name-like characters (any alphabet).
_NAME = re.compile(r"[\w .'-]+")
REPLACE_TRIES, REPLACE_WAIT_S = 5, 0.05  # Windows refuses to replace a file another program has open


@dataclass(frozen=True)
class Spec:
    help: str
    parse: Callable[[str], Any]  # raw text -> value; raises ValueError with a message for the user
    applies: str  # a key of APPLIES


def _int(lo: int, hi: int, unit: str = "") -> Callable[[str], int]:
    def parse(raw: str) -> int:
        try:
            n = int(raw.strip())
        except ValueError:
            raise ValueError(f"expected a whole number from {lo} to {hi}{unit}") from None
        if not lo <= n <= hi:
            raise ValueError(f"must be from {lo} to {hi}{unit}")
        return n
    return parse


def _text(hi: int, pattern: re.Pattern | None = None, allowed: str = "") -> Callable[[str], str]:
    def parse(raw: str) -> str:
        text = " ".join(raw.split())
        if not 1 <= len(text) <= hi:
            raise ValueError(f"must be 1 to {hi} characters")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("contains characters that can't be saved") from None
        if pattern and not pattern.fullmatch(text):
            raise ValueError(f"may only contain {allowed}")
        return text
    return parse


def _bool(raw: str) -> bool:
    word = raw.strip().lower()
    if word not in _TRUE | _FALSE:
        raise ValueError("expected on or off (true/false, yes/no)")
    return word in _TRUE


def _sources(raw: str) -> list[str]:
    from .diary import SOURCES
    chosen = list(dict.fromkeys(raw.replace(",", " ").lower().split()))
    if not chosen or any(x not in SOURCES for x in chosen):
        raise ValueError(f"expected {' or '.join(SOURCES)}, or both separated by a comma")
    return chosen


def _ring_seconds(raw: str) -> int:
    n = _int(10, 40, " seconds")(raw)
    try:
        record = int(config.load().record_seconds)
    except (TypeError, ValueError):
        raise ValueError("record_seconds in config.toml isn't a whole number; fix it there first") from None
    if n + record > RING_PLUS_RECORD_MAX:
        raise ValueError(f"ring_seconds + record_seconds ({record}) must stay at {RING_PLUS_RECORD_MAX} or less, "
                         "so Claude's call ends within Claude Code's 120 s limit")
    return n


SPECS: dict[str, Spec] = {
    "calendar_sources": Spec("which calendar feeds the meeting alerts read: google, outlook or both", _sources,
                             "live"),
    "meeting_alerts": Spec("warn before meetings and hold Claude's calls during them: on or off", _bool, "daemon"),
    "meeting_warn_minutes": Spec("how many minutes before a meeting to warn, 1-30", _int(1, 30, " minutes"),
                                 "daemon"),
    "callerid_max": Spec("the longest caller ID (repo/branch) shown on the phone, 10-40 characters", _int(10, 40),
                         "daemon"),
    "owner_name": Spec("your name, as spoken on calls and used in the tool names (call_<name>)",
                       _text(40, _NAME, "letters, digits, spaces, dots, apostrophes and dashes"), "sessions"),
    "ring_seconds": Spec("how long Claude's calls ring before going to voicemail, 10-40 seconds and at most 90 "
                         "minus record_seconds (so 30 by default)", _ring_seconds, "daemon"),
    "whisper_text": Spec("what the meeting whisper says, up to 120 characters", _text(120), "daemon"),
}


def _key(key: str) -> str:
    """The canonical key, after checking it's one users may change."""
    key = key.strip().lower().replace("-", "_")
    if key in SPECS:
        return key
    if key in {f.name for f in fields(config.Settings)}:
        raise ValueError(f"{key} can only be changed by editing {config.CONFIG_FILE}")
    raise ValueError(f"there's no setting called {key!r}. The ones you can change: {', '.join(SPECS)}")


def _default(key: str) -> Any:
    return getattr(config.Settings(), key)


def _quoted(value: Any) -> str:
    """For sentences: text in quotes, so its own punctuation doesn't run into ours."""
    return f'"{value}"' if isinstance(value, str) else _show(value)


def _show(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


def _file_keys() -> set[str]:
    path = config.CONFIG_FILE
    return set(tomllib.loads(path.read_text(encoding="utf-8"))) if path.exists() else set()


def show() -> str:
    s, ours = config.load(), _file_keys()
    lines = [f"{key} = {_show(getattr(s, key))}  ({'set' if key in ours else 'default'}): {spec.help}"
             for key, spec in SPECS.items()]
    unknown = config.unknown_keys()
    if unknown:
        lines.append(f"Not recognised in {config.CONFIG_FILE.name}, so ignored (a typo?): {', '.join(unknown)}")
    return "\n".join(lines)


def get(key: str) -> str:
    key = _key(key)
    spec = SPECS[key]
    where = (f"set in config.toml; the default is {_show(_default(key))}" if key in _file_keys()
             else "the default")
    return f"{key} = {_show(getattr(config.load(), key))} ({where}): {spec.help}. {APPLIES[spec.applies]}"


def set_value(key: str, raw: str) -> str:
    key = _key(key)
    spec = SPECS[key]
    value = spec.parse(raw)
    _write(key, value)
    return f"Saved: {key} = {_quoted(value)}. {APPLIES[spec.applies]}" + _warnings(key, value)


def unset(key: str) -> str:
    key = _key(key)
    _write(key, None)
    return f"Removed {key}, so it's back to the default ({_quoted(_default(key))}). {APPLIES[SPECS[key].applies]}"


def _warnings(key: str, value: Any) -> str:
    notes = []
    if key == "calendar_sources":
        missing = [x for x in value if not _linked(x)]
        if missing:
            notes.append("No link is stored for " + " or ".join(missing) + ": run "
                         + " and ".join(f"`phone calendar add {x}`" for x in missing) + ".")
    if key == "calendar_sources" and not config.load().meeting_alerts:
        notes.append("Meeting alerts are off: `phone config set meeting_alerts on` turns them on.")
    return "".join(f" {n}" for n in notes)


def _linked(source: str) -> bool:
    try:
        return bool(keychain.get(f"calendar-{source}"))
    except Exception:  # Credential Manager failing counts as no link
        return False


def _write(key: str, value: Any) -> None:
    """Sets one top-level key (None removes it), keeping the rest of the file (comments, order, line endings) as it
    was. The result is read back and checked before it replaces the file."""
    import tomlkit
    from tomlkit.items import AoT, Table
    path = config.CONFIG_FILE
    old = path.read_bytes().decode("utf-8") if path.exists() else ""
    doc = tomlkit.parse(old)
    if isinstance(doc.get(key), (Table, AoT)):
        raise ValueError(f"{key} is a [table] in {path}; fix that by hand")
    if value is None:
        doc.pop(key, None)
        new = doc.as_string()
    elif key in doc:
        doc[key] = value
        new = doc.as_string()
    else:
        new = _add_top_level(old, key, value)
    expected = {k: v for k, v in tomllib.loads(old).items() if k != key}
    if value is not None:
        expected[key] = value
    if tomllib.loads(new) != expected:
        raise ValueError(f"couldn't place {key} in {path} safely; set it there by hand")
    _replace(path, new.encode("utf-8"))


def _add_top_level(text: str, key: str, value: Any) -> str:
    """A new key goes just after the last top-level key: below a [table] header TOML would read it as part of that
    table, and a comment just above a table belongs to the table."""
    import tomlkit
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += eol
    first_table = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    at = first_table
    while at and (not lines[at - 1].strip() or lines[at - 1].lstrip().startswith("#")):
        at -= 1
    if at == 0:  # only comments above the first table: they head the file, so the key goes just above the table
        at = first_table
    entry = tomlkit.dumps({key: value}).rstrip("\n") + eol
    return "".join(lines[:at] + [entry] + lines[at:])


def _replace(path, data: bytes) -> None:
    """Atomic: a reader sees the old file or the new one. Retries while another program has the file open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_TRIES - 1:
                    raise ValueError(f"{path.name} is in use by another program; try again in a moment") from None
                time.sleep(REPLACE_WAIT_S)
    except OSError as e:
        raise ValueError(f"couldn't write {path}: {e}") from None
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# --- which phone Claude's calls ring ---

def _registered(ext: str) -> str:
    from . import esl
    s = config.load()
    try:
        contact = esl.api_sync(f"sofia_contact internal/{ext}@{s.lan_ip}", s.esl_port, keychain.get_or_create("esl"))
    except Exception:  # FreeSWITCH down, or the password store failing: the setting is saved either way
        return "not checkable right now (is FreeSWITCH running?)"
    return "not registered" if contact.strip().startswith("error/") else "registered"


def _phone_name(s: config.Settings, ext: str) -> str:
    if ext == s.desk_ext:
        return f"the desk phone (ext {ext})"
    if ext == s.softphone_ext:
        return f"the softphone (ext {ext})"
    return f"ext {ext}"


def change_phone(phone: str | None = None) -> str:
    """No phone: which one Claude's calls ring now. 'desk' or 'softphone': ring that one instead."""
    s = config.load()
    if not phone:
        return f"Claude's calls ring {_phone_name(s, s.ring_ext)}, which is {_registered(s.ring_ext)}."
    choice = phone.strip().lower()
    if choice not in PHONES:
        raise ValueError("expected desk or softphone")
    ext = getattr(s, PHONES[choice])
    if ext == s.ring_ext:
        return f"Claude's calls already ring {_phone_name(s, ext)}."
    _write("ring_ext", ext)
    state = _registered(ext)
    note = "" if state == "registered" else f" Note: that phone is {state} right now."
    return f"Saved: Claude's calls will ring {_phone_name(s, ext)}. {APPLIES['daemon']}{note}"
