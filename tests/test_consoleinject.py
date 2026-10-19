"""Typing into another console: the key events built and the Win32 calls made. The real injection into a live console
is a manual check (it needs a terminal with claude in it)."""

import ctypes
import io
from ctypes import wintypes

import pytest

from claude_phone import consoleinject as ci
from claude_phone import inject

CONIN = 4242


class FakeKernel32:
    """Stands in for kernel32: records the calls and the key events written."""

    def __init__(self, attach_error: int = 0, conin=CONIN, take: int | None = None):
        self.attach_error, self.conin, self.take = attach_error, conin, take
        self.log: list = []
        self.events: list[tuple[bool, str, int]] = []
        self.writes: list[int] = []

    def FreeConsole(self):
        self.log.append("free")
        return 1

    def AttachConsole(self, pid):
        self.log.append(("attach", pid))
        if self.attach_error:
            ctypes.set_last_error(self.attach_error)
            return 0
        return 1

    def CreateFileW(self, name, *args):
        self.log.append(("open", name))
        ctypes.set_last_error(6)
        return self.conin

    def WriteConsoleInputW(self, handle, records, count, written):
        assert handle == CONIN
        self.log.append(("write", count))
        self.writes.append(count)
        for i in range(count):
            k = records[i].KeyEvent
            self.events.append((bool(k.bKeyDown), k.UnicodeChar, k.wVirtualKeyCode))
        written._obj.value = count if self.take is None else self.take
        return 1

    def CloseHandle(self, handle):
        self.log.append(("close", handle))
        return 1


@pytest.fixture
def k32(monkeypatch):
    fake = FakeKernel32()
    monkeypatch.setattr(ci, "_kernel32", lambda: fake)
    monkeypatch.setattr(ci.time, "sleep", lambda s: fake.log.append(("sleep", s)))
    return fake


def typed(events) -> str:
    units = "".join(ch for down, ch, vk in events if down and vk in (0, ci.VK_RETURN))
    return units.encode("utf-16-le", "surrogatepass").decode("utf-16-le")  # the console joins a surrogate pair again


def test_each_character_is_a_key_down_and_a_key_up():
    recs = ci.text_records("hi")
    assert [(r.KeyEvent.bKeyDown, r.KeyEvent.UnicodeChar) for r in recs] == [
        (True, "h"), (False, "h"), (True, "i"), (False, "i")]
    assert all(r.EventType == ci.KEY_EVENT for r in recs)


def test_an_emoji_is_its_two_utf16_units_in_order():
    recs = ci.text_records("a😀å")
    downs = [ord(r.KeyEvent.UnicodeChar) for r in recs if r.KeyEvent.bKeyDown]
    assert downs == [ord("a"), 0xD83D, 0xDE00, ord("å")]


def test_enter_is_the_return_key_down_then_up():
    down, up = ci.enter_records()
    assert (down.KeyEvent.bKeyDown, up.KeyEvent.bKeyDown) == (True, False)
    assert down.KeyEvent.wVirtualKeyCode == ci.VK_RETURN and down.KeyEvent.UnicodeChar == "\r"


def test_control_characters_never_reach_the_console():
    assert ci.clean("run\x1b[31m this\r\nnow\x7f ") == "run [31m this  now"


def test_a_leading_slash_bang_or_hash_is_dropped_here_too_for_a_direct_call():
    assert ci.clean(" /compact now") == "compact now"
    assert ci.clean("!dir") == "dir"


def test_claude_having_exited_before_the_text_or_before_enter_stops_the_typing(k32):
    assert ci.send(77, "hello", lambda: False) == ci.NO_CLAUDE
    assert k32.events == []
    answers = iter([True, False])  # gone during the 0.3 s before Enter: Enter would run the text in the shell
    assert ci.send(77, "hello", lambda: next(answers)) == ci.NO_CLAUDE
    assert "\r" not in typed(k32.events) and typed(k32.events) == "hello"
    assert k32.log[-2:] == [("close", CONIN), "free"]


def test_text_then_a_pause_then_enter_into_the_attached_console(k32):
    assert ci.send(77, "ship æøå 😀") == "sent"
    # 9 characters and an emoji of two UTF-16 units: 11 units, 22 key events. Enter is its own write, after the pause.
    assert k32.log == ["free", ("attach", 77), ("open", "CONIN$"), ("write", 22), ("sleep", ci.SUBMIT_DELAY_S),
                       ("write", 2), ("close", CONIN), "free"]
    assert typed(k32.events) == "ship æøå 😀\r"


def test_a_long_text_is_written_in_chunks(k32):
    ci.send(77, "x" * 600)
    assert max(k32.writes) <= ci.WRITE_CHUNK
    assert typed(k32.events) == "x" * 600 + "\r"


@pytest.mark.parametrize("error, expected", [
    (87, "notfound"),
    (5, "error:couldn't attach to the console (Windows error 5): that console is elevated"),
    (6, "error:couldn't attach to the console (Windows error 6)"),
])
def test_a_console_that_cannot_be_attached_is_reported(monkeypatch, error, expected):
    fake = FakeKernel32(attach_error=error)
    monkeypatch.setattr(ci, "_kernel32", lambda: fake)
    assert ci.send(77, "hello") == expected
    assert fake.events == []


def test_a_console_input_that_cannot_be_opened_is_an_error_and_is_detached_again(monkeypatch):
    fake = FakeKernel32(conin=wintypes.HANDLE(-1).value)
    monkeypatch.setattr(ci, "_kernel32", lambda: fake)
    assert ci.send(77, "hello").startswith("error:couldn't open the console input")
    assert fake.log[-1] == "free" and ("close", fake.conin) not in fake.log


def test_a_console_that_takes_only_some_of_the_keys_is_an_error(monkeypatch):
    fake = FakeKernel32(take=1)
    monkeypatch.setattr(ci, "_kernel32", lambda: fake)
    monkeypatch.setattr(ci.time, "sleep", lambda s: None)
    result = ci.send(77, "hello")
    assert result.startswith("error:") and "1 of" in result
    assert fake.log[-2:] == [("close", CONIN), "free"]


@pytest.mark.parametrize("text", ["", "   ", "\x1b\r\n", "x" * (ci.MAX_TEXT_CHARS + 1)])
def test_empty_or_oversized_text_is_refused_before_touching_any_console(k32, text):
    assert ci.send(77, text).startswith("error:text must be")
    assert k32.log == []


def test_the_helper_reads_utf8_from_stdin_and_asks_whether_that_shell_still_runs_claude(k32, monkeypatch, capsys):
    asked = []
    monkeypatch.setattr(inject, "accepts", lambda term_id: asked.append(term_id) or True)
    monkeypatch.setattr("sys.stdin", type("In", (), {"buffer": io.BytesIO("hej æøå".encode())})())
    assert ci.main(["77", "1234"]) == 0
    assert capsys.readouterr().out.strip() == "sent"
    assert typed(k32.events) == "hej æøå\r"
    assert asked == ["con:77:1234", "con:77:1234"]  # before the text and before Enter


@pytest.mark.parametrize("argv", [[], ["abc"], ["77"], ["77", "x"]])
def test_the_helper_needs_a_pid(k32, capsys, argv):
    assert ci.main(argv) == 2
    assert capsys.readouterr().out.startswith("error:usage")
    assert k32.log == []
