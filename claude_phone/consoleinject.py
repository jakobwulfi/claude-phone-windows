"""Type text into another process's console, whatever hosts it (Visual Studio's Terminal, Windows Terminal, PowerShell,
VS Code). Every Windows console reads keystrokes from an input buffer that any process on the same console can write
to, so this attaches to the session's console and pushes key events: no IDE API involved, and it works with the window
unfocused.

Run as `python -m claude_phone.consoleinject <shell pid>` with the text on stdin (UTF-8), never on the command line,
where other processes can read it. It owns a console of its own for a moment, which is why it is a process of its own
and not a call inside the daemon: attaching to another console would detach the daemon from its log. Prints `sent`,
`notfound` or `error:<message>`, the answers the VS Code bridge gives."""

from __future__ import annotations

import ctypes
import re
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from functools import cache

KEY_EVENT = 0x0001
VK_RETURN = 0x0D
SCAN_RETURN = 0x1C
GENERIC_READ_WRITE = 0xC0000000
SHARE_READ_WRITE = 0x3
OPEN_EXISTING = 3
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87  # AttachConsole's answer for a pid that doesn't exist
NO_CLAUDE = "error:claude isn't running in that terminal"

# Text, then Enter separately: Claude Code treats a trailing newline arriving with the text as part of a paste.
SUBMIT_DELAY_S = 0.3
WRITE_CHUNK = 512  # records per WriteConsoleInputW call
MAX_TEXT_CHARS = 4000
# Escape sequences and carriage returns could do more in a console than type text.
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class _KeyEventRecord(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("UnicodeChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class _EventUnion(ctypes.Union):
    _fields_ = [("KeyEvent", _KeyEventRecord)]


class InputRecord(ctypes.Structure):
    _anonymous_ = ("event",)
    _fields_ = [("EventType", wintypes.WORD), ("event", _EventUnion)]


@cache
def _kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.WriteConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(InputRecord), wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD)]
    k32.AttachConsole.argtypes = [wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def clean(text: str) -> str:
    """One plain line. No leading / ! or #: Claude Code would run the words as a slash command or a shell line."""
    return re.sub(r"^[/!#\s]+", "", CONTROL_CHARS.sub(" ", text)).strip()


def _key(char: str, vk: int, scan: int, down: bool) -> InputRecord:
    r = InputRecord()
    r.EventType = KEY_EVENT
    k = r.KeyEvent
    k.bKeyDown, k.wRepeatCount, k.wVirtualKeyCode, k.wVirtualScanCode = down, 1, vk, scan
    k.UnicodeChar = char
    return r


def text_records(text: str) -> list[InputRecord]:
    """A key-down and a key-up per UTF-16 unit: a character outside the BMP (an emoji) is two units, which the console
    joins again."""
    units = text.encode("utf-16-le", "surrogatepass")
    chars = [units[i:i + 2].decode("utf-16-le", "surrogatepass") for i in range(0, len(units), 2)]
    return [_key(c, 0, 0, down) for c in chars for down in (True, False)]


def enter_records() -> list[InputRecord]:
    return [_key("\r", VK_RETURN, SCAN_RETURN, down) for down in (True, False)]


def _write(k32, handle, records: list[InputRecord]) -> None:
    for i in range(0, len(records), WRITE_CHUNK):
        chunk = records[i:i + WRITE_CHUNK]
        written = wintypes.DWORD(0)
        if not k32.WriteConsoleInputW(handle, (InputRecord * len(chunk))(*chunk), len(chunk), ctypes.byref(written)):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != len(chunk):
            raise OSError(f"the console took {written.value} of {len(chunk)} key events")


def _attach_error(code: int) -> str:
    if code == ERROR_INVALID_PARAMETER:
        return "notfound"
    hint = ": that console is elevated" if code == ERROR_ACCESS_DENIED else ""
    return f"error:couldn't attach to the console (Windows error {code}){hint}"


def send(pid: int, text: str, still_claude: Callable[[], bool] = lambda: True) -> str:
    """Type `text` and then Enter into the console `pid` is attached to. `still_claude` is asked just before the
    text and again before Enter, which would run the text in a bare shell if claude has exited meanwhile."""
    text = clean(text)
    if not text or len(text) > MAX_TEXT_CHARS:
        return f"error:text must be 1 to {MAX_TEXT_CHARS} characters"
    k32 = _kernel32()
    k32.FreeConsole()  # this process starts with a hidden console of its own
    if not k32.AttachConsole(pid):
        return _attach_error(ctypes.get_last_error())
    conin = None
    try:
        conin = k32.CreateFileW("CONIN$", GENERIC_READ_WRITE, SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
        if conin in (None, wintypes.HANDLE(-1).value):
            return f"error:couldn't open the console input (Windows error {ctypes.get_last_error()})"
        if not still_claude():
            return NO_CLAUDE
        _write(k32, conin, text_records(text))
        time.sleep(SUBMIT_DELAY_S)
        if not still_claude():
            return NO_CLAUDE
        _write(k32, conin, enter_records())
        return "sent"
    except OSError as e:
        return f"error:{e}"
    finally:
        if conin not in (None, wintypes.HANDLE(-1).value):
            k32.CloseHandle(conin)
        k32.FreeConsole()


def main(argv: list[str]) -> int:
    try:
        pid, created_ms = int(argv[0]), int(argv[1])
    except (IndexError, ValueError):
        print("error:usage: python -m claude_phone.consoleinject <shell pid> <shell creation time in ms>")
        return 2
    from . import inject  # only now: the checks below need it, the tests of everything above don't
    print(send(pid, sys.stdin.buffer.read().decode("utf-8", "replace"),
               lambda: inject.accepts(f"{inject.CONSOLE_PREFIX}{pid}:{created_ms}")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
