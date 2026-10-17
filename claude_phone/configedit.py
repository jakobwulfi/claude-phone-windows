"""The settings a user may change from `phone config`, /claude-phone config and chat, and `change_phone`.
A change writes config.toml again with a comment on each line saying what that setting is for. Nothing restarts:
each change says what has to be restarted for it to apply."""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
import time
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from collections.abc import Callable

from . import config, keychain

RESTART = "phone restart daemon"
APPLIES = {
    "live": "Takes effect at the next calendar refresh (within a few minutes).",
    "daemon": f"Takes effect after `{RESTART}`.",
    "sessions": f"Takes effect after `{RESTART}`; Claude Code sessions get the new tool names (call_<name>) "
                "once they are started again.",
    "vscode": "Takes effect the next time a VS Code window opens.",
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
    """What a setting may be. What it's for is config.DESCRIPTIONS[key]."""
    allowed: str  # the values it takes, e.g. "1-30 minutes"
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
    chosen = list(dict.fromkeys(raw.replace(",", " ").lower().split()))
    if not chosen or any(x not in config.CALENDAR_SOURCES for x in chosen):
        raise ValueError(f"expected {' or '.join(config.CALENDAR_SOURCES)}, or both separated by a comma")
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
    "calendar_sources": Spec("google, outlook, or both separated by a comma", _sources, "live"),
    "meeting_alerts": Spec("on or off", _bool, "daemon"),
    "meeting_warn_minutes": Spec("1-30 minutes", _int(1, 30, " minutes"), "daemon"),
    "callerid_max": Spec("10-40 characters", _int(10, 40), "daemon"),
    "owner_name": Spec("up to 40 letters, digits, spaces, dots, apostrophes and dashes",
                       _text(40, _NAME, "letters, digits, spaces, dots, apostrophes and dashes"), "sessions"),
    "ring_seconds": Spec(f"10-40 seconds, at most {RING_PLUS_RECORD_MAX} minus record_seconds", _ring_seconds,
                         "daemon"),
    "whisper_text": Spec("up to 120 characters", _text(120), "daemon"),
    "autostart": Spec("on or off", _bool, "vscode"),
}


def described(key: str) -> str:
    """What a setting is for and what it may be."""
    return f"{config.DESCRIPTIONS[key]} ({SPECS[key].allowed})"


def _key(key: str) -> str:
    """The canonical key, after checking it's one users may change."""
    key = key.strip().lower().replace("-", "_")
    if key in SPECS:
        return key
    if key == "voice":
        raise ValueError("Claude's voice is changed with `phone voice`, which also downloads it if needed")
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
    lines = [f"{key} = {_show(getattr(s, key))}  ({'set' if key in ours else 'default'}): {described(key)}"
             for key in SPECS]
    return "\n".join(lines + config.problems())


def get(key: str) -> str:
    key = _key(key)
    spec = SPECS[key]
    where = (f"set in config.toml; the default is {_show(_default(key))}" if key in _file_keys()
             else "the default")
    return f"{key} = {_show(getattr(config.load(), key))} ({where}): {described(key)}. {APPLIES[spec.applies]}"


def set_value(key: str, raw: str) -> str:
    key = _key(key)
    spec = SPECS[key]
    value = spec.parse(raw)
    write_setting(key, value)
    if key == "autostart" and value:
        config.remember_phone_exe()
    return f"Saved: {key} = {_quoted(value)}. {APPLIES[spec.applies]}" + _warnings(key, value)


def unset(key: str) -> str:
    key = _key(key)
    write_setting(key, None)
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


def write_setting(key: str, value: Any) -> None:
    """Sets one top-level key (None removes it) and writes config.toml again, each line with a comment saying
    what the setting is for. The result is read back and checked before it replaces the file."""
    path, old, data = _current()
    if isinstance(data.get(key), dict):
        raise ValueError(f"{key} is a [table] in {path}; fix that by hand")
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    _save(path, old, data, key)


def migrate() -> str:
    """`phone config migrate`: the old voice settings in config.toml rewritten in the new form, now."""
    path, old, data = _current()
    if not (found := config.legacy(tomllib.loads(old))):
        return f"{path.name} has no old voice settings: nothing to rewrite."
    _save(path, old, data, "the voice settings")
    return f"Rewrote {', '.join(found)} in the new form; Claude's voices are the same as before."


def _current() -> tuple[Path, str, dict]:
    """config.toml: its path, its text, and its settings with any old voice settings in the new form."""
    path = config.CONFIG_FILE
    old = path.read_bytes().decode("utf-8") if path.exists() else ""
    data = tomllib.loads(old)
    return path, old, config.migrate_legacy(data) if config.legacy(data) else data


def _save(path: Path, old: str, data: dict, what: str) -> None:
    """data written as config.toml, in old's line endings; checked by reading it back before it replaces the file."""
    try:
        new = render(data, "\r\n" if "\r\n" in old else "\n")
    except ValueError as e:
        raise ValueError(f"{path.name} holds something `phone config` can't write back ({e}); "
                         "edit it by hand") from None
    if tomllib.loads(new) != data:
        raise ValueError(f"couldn't write {what} to {path} safely; set it there by hand")
    _replace(path, new.encode("utf-8"))


HEADER = "# claude-phone settings. `phone config` changes the common ones; config.example.toml lists them all."
TYPO = "not a claude-phone setting, so it's ignored (a typo?)"
COMMENT_COLUMN = 40
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_LITERAL = re.compile(r"[^'\x00-\x1f\x7f]*")  # fits in a TOML 'literal string' as it is


def render(data: dict, eol: str = "\n") -> str:
    """config.toml for these settings: the plain ones first, each with what it's for, then the [tables]."""
    lines = [HEADER]
    for k, v in data.items():
        if not isinstance(v, dict):
            line = f"{_toml_key(k)} = {_toml_value(v)}"
            lines.append(f"{line.ljust(COMMENT_COLUMN - 2)}  # {config.DESCRIPTIONS.get(k, TYPO)}")
    for k, v in data.items():
        if isinstance(v, dict):
            lines += ["", f"# {config.DESCRIPTIONS.get(k, TYPO)}", *_table([k], v)]
    return eol.join(lines) + eol


def _table(path: list[str], table: dict) -> list[str]:
    entries = [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in table.items() if not isinstance(v, dict)]
    only_subtables = table and not entries
    lines = [] if only_subtables else [f"[{'.'.join(map(_toml_key, path))}]", *entries]
    for k, v in table.items():
        if isinstance(v, dict):
            lines += _table([*path, k], v)
    return lines


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _toml_value(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        if "\\" in value and _LITERAL.fullmatch(value):
            return f"'{value}'"  # Windows paths read better without doubled backslashes
        # JSON's string escapes are valid in TOML; TOML also wants DEL escaped.
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, list):
        return "[" + ", ".join(map(_toml_value, value)) + "]"
    if isinstance(value, (datetime.date, datetime.time)):  # a datetime is a date too
        return value.isoformat()
    raise ValueError(f"a {type(value).__name__} such as {value!r}")


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
        contact = esl.api_local(f"sofia_contact internal/{ext}@{s.lan_ip}")
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
    write_setting("ring_ext", ext)
    state = _registered(ext)
    note = "" if state == "registered" else f" Note: that phone is {state} right now."
    return f"Saved: Claude's calls will ring {_phone_name(s, ext)}. {APPLIES['daemon']}{note}"
