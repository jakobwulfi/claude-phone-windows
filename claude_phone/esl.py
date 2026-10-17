"""Minimal async client for FreeSWITCH's event socket (inbound mode): commands plus an event stream that call
flows can await on. `Call` has the surface the call flows use, which tests/test_flows.py fakes."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import uuid
from collections import deque
from pathlib import Path
from typing import Any
from collections.abc import Callable, Coroutine

from . import log

# The events the daemon waits on. CHANNEL_PARK: a phone dialled something the dial plan hands to us.
# CHANNEL_HANGUP_COMPLETE carries each leg's audio statistics, which the daemon logs.
EVENTS = ("CHANNEL_PARK CHANNEL_ANSWER CHANNEL_HANGUP CHANNEL_HANGUP_COMPLETE CHANNEL_EXECUTE_COMPLETE DTMF "
          "BACKGROUND_JOB CUSTOM sofia::register sofia::unregister sofia::expire")
COMMAND_TIMEOUT_S = 10
RECONNECT_S = 2
# Energy below which a frame counts as silence for `record`'s max_silence; line noise sits well under it.
SILENCE_THRESHOLD = 200
# Recordings of at least RECORD_WARN_MIN seconds get a double beep RECORD_WARN_S before they're cut off.
RECORD_WARN_MIN = 30
RECORD_WARN_S = 10
TIME_UP_TONE = "tone_stream://L=2;%(150,100,880)"
# send_silence_when_idle on every call: digital silence (-1) whenever nothing plays. FreeSWITCH otherwise stops the
# RTP stream between playbacks (speech is played a sentence at a time), and a softphone's adaptive jitter buffer
# stretches the audio each time the stream starts again: it "slows down a bit" (Linphone).
IDLE_SILENCE = "-1"


class Hangup(Exception):
    """The channel hung up while a flow was waiting on it."""


class ESLError(RuntimeError):
    """FreeSWITCH refused a command (its reply starts with -ERR)."""


def _parse_header(line: str, headers: dict[str, str]) -> None:
    key, _, value = line.partition(":")
    headers[key.strip()] = value.strip()


async def read_frame(reader: asyncio.StreamReader) -> tuple[dict[str, str], str]:
    """One event-socket frame: header lines, a blank line, then Content-Length bytes of body."""
    headers: dict[str, str] = {}
    while True:
        raw = await reader.readline()
        if not raw:
            raise ConnectionError("event socket closed")
        line = raw.decode("utf-8").rstrip("\r\n")
        if line:
            _parse_header(line, headers)
        elif headers:
            break
    length = int(headers.get("Content-Length", 0))
    body = (await reader.readexactly(length)).decode("utf-8") if length else ""
    return headers, body


def _check_reply(headers: dict[str, str], body: str) -> str:
    text = body if headers.get("Content-Type") == "api/response" else headers.get("Reply-Text", "")
    text = text.strip()
    if text.startswith("-ERR"):
        raise ESLError(text)
    return text


def spawn(tasks: set[asyncio.Task], coro: Coroutine, name: str, lg: logging.Logger) -> asyncio.Task:
    """create_task, held in `tasks` until it ends (the loop keeps only a weak reference, so an unheld task can be
    collected mid-run), and its failure logged rather than lost."""
    task = asyncio.create_task(coro, name=name)
    tasks.add(task)

    def done(t: asyncio.Task) -> None:
        tasks.discard(t)
        if not t.cancelled() and t.exception():
            lg.error("%s failed: %r", name, t.exception(), exc_info=t.exception())
    task.add_done_callback(done)
    return task


def _is_gone(err: ESLError) -> bool:
    """FreeSWITCH's ways of saying the channel no longer exists."""
    text = str(err).lower()
    return "no such channel" in text or "no such session" in text or "invalid session" in text


class ESL:
    def __init__(self, host: str, port: int, password: str, recordings: Path):
        self.host, self.port, self.password = host, port, password
        self.recordings = recordings
        self._writer: asyncio.StreamWriter | None = None
        self._replies: deque[asyncio.Future] = deque()
        self._send_lock = asyncio.Lock()
        self._waiters: list[tuple[Callable[[dict], bool], asyncio.Future]] = []
        self._handlers: list[Callable[[dict], Any]] = []
        self.on_connect: list[Callable[[], Any]] = []
        self.connected = asyncio.Event()
        self.lg = log.get("esl")
        self._tasks: set[asyncio.Task] = set()

    # --- connection ---

    async def run(self) -> None:
        """Connect, log in, subscribe, and read frames; reconnect forever (FreeSWITCH may restart)."""
        while True:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                try:
                    await self._handshake(reader, writer)
                    self._writer = writer
                    self.connected.set()
                    self.lg.info("event socket connected")
                    for cb in self.on_connect:
                        spawn(self._tasks, cb(), "on-connect callback", self.lg)
                    await self._read_loop(reader)
                finally:
                    self.connected.clear()
                    self._writer = None
                    self._fail_replies()
                    writer.close()
            except (OSError, ConnectionError, asyncio.IncompleteReadError, ESLError) as e:
                self.lg.warning("event socket unavailable: %r", e)
            await asyncio.sleep(RECONNECT_S)

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        headers, _ = await read_frame(reader)
        if headers.get("Content-Type") != "auth/request":
            raise ConnectionError(f"unexpected greeting: {headers}")
        for cmd in (f"auth {self.password}", f"event json {EVENTS}"):
            writer.write(f"{cmd}\n\n".encode())
            await writer.drain()
            _check_reply(*await read_frame(reader))

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            headers, body = await read_frame(reader)
            kind = headers.get("Content-Type")
            if kind == "text/event-json":
                self._dispatch(json.loads(body))
            elif kind in ("command/reply", "api/response"):
                fut = self._replies.popleft() if self._replies else None
                if fut and not fut.done():
                    fut.set_result((headers, body))
            elif kind == "text/disconnect-notice":
                raise ConnectionError("FreeSWITCH closed the event socket")

    def _fail_replies(self) -> None:
        while self._replies:
            fut = self._replies.popleft()
            if not fut.done():
                fut.set_exception(ConnectionError("event socket closed"))

    # --- commands ---

    async def _send(self, text: str) -> str:
        async with self._send_lock:
            if not self._writer:
                raise ConnectionError("not connected to FreeSWITCH")
            fut = asyncio.get_running_loop().create_future()
            self._replies.append(fut)
            self._writer.write(f"{text}\n\n".encode())
            await self._writer.drain()
        return _check_reply(*await asyncio.wait_for(fut, COMMAND_TIMEOUT_S))

    async def api(self, cmd: str) -> str:
        """Runs a command and returns its output. Raises ESLError on -ERR."""
        return await self._send(f"api {cmd}")

    async def bgapi(self, cmd: str, job_id: str) -> None:
        """Starts a command in the background; its result arrives as a BACKGROUND_JOB event with this Job-UUID."""
        await self._send(f"bgapi {cmd}\nJob-UUID: {job_id}")

    async def sendmsg(self, channel: str, app: str, arg: str, event_id: str) -> None:
        """Queues a dial plan application on a channel. CHANNEL_EXECUTE_COMPLETE carries event_id when it ends."""
        await self._send(f"sendmsg {channel}\ncall-command: execute\nexecute-app-name: {app}\n"
                         f"execute-app-arg: {arg}\nEvent-UUID: {event_id}")

    async def sendevent(self, name: str, headers: dict[str, str]) -> None:
        lines = "".join(f"\n{k}: {v}" for k, v in headers.items())
        await self._send(f"sendevent {name}{lines}")

    # --- events ---

    def on_event(self, handler: Callable[[dict], Any]) -> None:
        self._handlers.append(handler)

    def off_event(self, handler: Callable[[dict], Any]) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    def wait_for(self, pred: Callable[[dict], bool]) -> asyncio.Future:
        """Future resolving with the first event matching pred. Register before triggering the action."""
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((pred, fut))
        return fut

    def cancel_wait(self, fut: asyncio.Future) -> None:
        self._waiters = [(p, f) for p, f in self._waiters if f is not fut]
        if not fut.done():
            fut.cancel()

    def _dispatch(self, ev: dict) -> None:
        keep = []
        for pred, fut in self._waiters:
            if fut.done():
                continue
            try:
                hit = pred(ev)
            except Exception:
                hit = False
            if hit:
                fut.set_result(ev)
            else:
                keep.append((pred, fut))
        self._waiters = keep
        for h in list(self._handlers):
            try:
                r = h(ev)
                if asyncio.iscoroutine(r):
                    spawn(self._tasks, r, "event handler", self.lg)
            except Exception as e:
                self.lg.exception("event handler failed: %r", e)


def _media(paths: tuple[str, ...]) -> str:
    """One file, or several played in order."""
    return paths[0] if len(paths) == 1 else "file_string://" + "!".join(paths)


class Call:
    """Helpers bound to one channel. Any wait raises Hangup if the channel hangs up."""

    def __init__(self, esl: ESL, channel_id: str):
        self.esl = esl
        self.id = channel_id
        self.gone = esl.wait_for(lambda e: e.get("Event-Name") == "CHANNEL_HANGUP" and e.get("Unique-ID") == channel_id)
        self.digits: asyncio.Queue[str] = asyncio.Queue()
        esl.on_event(self._dtmf)
        self._n = 0

    def _dtmf(self, ev: dict) -> None:
        if ev.get("Event-Name") == "DTMF" and ev.get("Unique-ID") == self.id:
            self.digits.put_nowait(ev.get("DTMF-Digit", ""))

    def close(self) -> None:
        self.esl.off_event(self._dtmf)
        self.esl.cancel_wait(self.gone)

    @property
    def up(self) -> bool:
        return not self.gone.done()

    def drain_digits(self) -> None:
        while not self.digits.empty():
            self.digits.get_nowait()

    async def _race(self, fut: asyncio.Future, timeout: float | None = None):
        done, _ = await asyncio.wait({fut, self.gone}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if fut in done:
            return fut.result()
        self.esl.cancel_wait(fut)
        if self.gone in done:
            raise Hangup()
        raise TimeoutError()

    async def _api(self, cmd: str) -> str:
        """A channel command; FreeSWITCH not knowing the channel means the caller has just hung up."""
        try:
            return await self.esl.api(cmd)
        except ESLError as e:
            if _is_gone(e):
                raise Hangup() from e
            raise

    async def _set(self, name: str, value: str) -> None:
        await self._api(f"uuid_setvar {self.id} {name} {value}")

    async def _execute(self, app: str, arg: str) -> asyncio.Future:
        """Queue an application; the returned future resolves with its CHANNEL_EXECUTE_COMPLETE."""
        self._n += 1
        app_id = f"{self.id}-{app}{self._n}"
        done = self.esl.wait_for(lambda e: e.get("Event-Name") == "CHANNEL_EXECUTE_COMPLETE"
                                 and e.get("Application-UUID") == app_id)
        try:
            await self.esl.sendmsg(self.id, app, arg, app_id)
        except ESLError as e:
            self.esl.cancel_wait(done)
            raise Hangup() from e
        return done

    async def _break(self) -> None:
        """Stop whatever is playing or recording."""
        try:
            await self.esl.api(f"uuid_break {self.id} all")
        except ESLError:
            pass

    async def answer(self) -> None:
        await self._set("send_silence_when_idle", IDLE_SILENCE)
        await self._api(f"uuid_answer {self.id}")

    async def hangup(self) -> None:
        if not self.gone.done():
            try:
                await self.esl.api(f"uuid_kill {self.id}")
            except ESLError:
                pass

    async def play(self, *media: str, interrupt: str = "", until: asyncio.Future | None = None) -> str | None:
        """Play media in order. If a digit in `interrupt` is pressed, stop and return it.
        If `until` completes first, stop and return "until"."""
        await self._set("playback_terminators", "none")  # digits are ours to act on, not FreeSWITCH's
        finished = await self._execute("playback", _media(media))
        if not interrupt and until is None:
            await self._race(finished)
            return None
        while True:
            digit_task = asyncio.ensure_future(self.digits.get())
            waits = {finished, digit_task, self.gone} | ({until} if until is not None else set())
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            if until is not None and until in done:
                digit_task.cancel()
                self.esl.cancel_wait(finished)
                await self._break()
                return "until"
            if digit_task in done and digit_task.result() in interrupt:
                self.esl.cancel_wait(finished)
                await self._break()
                return digit_task.result()
            if digit_task not in done:
                digit_task.cancel()
            if finished in done:
                return None
            if self.gone in done:
                self.esl.cancel_wait(finished)
                raise Hangup()

    async def record(self, name: str, max_seconds: int, terminate_on: str = "#",
                     max_silence: int | None = None) -> tuple[str, str | None]:
        """Record to <recordings>/<name>.wav (16 kHz). Returns (how_it_ended, digit):
        'digit' (with the digit pressed), 'hangup', 'finished' (silence or max length) or 'timeout'."""
        self.drain_digits()
        await self._set("playback_terminators", terminate_on)  # "#", or "any" digit
        await self._set("record_sample_rate", "16000")
        path = (self.esl.recordings / f"{name}.wav").as_posix()
        arg = f"{path} {max_seconds}" + (f" {SILENCE_THRESHOLD} {max_silence}" if max_silence else "")
        finished = await self._execute("record", arg)
        warn = (asyncio.create_task(self._warn_time_up(max_seconds - RECORD_WARN_S))
                if max_seconds >= RECORD_WARN_MIN else None)
        try:
            ev = await self._race(finished, timeout=max_seconds + 5)
        except Hangup:
            # FreeSWITCH still writes what was captured before the hangup.
            return "hangup", None
        except TimeoutError:
            await self._break()
            return "timeout", None
        finally:
            if warn:
                warn.cancel()
        # Only a terminator ended it: any other digit was just pressed while talking.
        def ends(d: str | None) -> bool:
            return bool(d) and (terminate_on == "any" or d in terminate_on)
        pressed = []
        while not self.digits.empty():
            pressed.append(self.digits.get_nowait())
        digit = next((d for d in pressed if ends(d)), None)
        if not digit and ends(used := ev.get("variable_playback_terminator_used")):
            digit = used
        return ("digit", digit) if digit else ("finished", None)

    async def _warn_time_up(self, delay: float) -> None:
        """Mixes a double beep into what the caller hears. `record` keeps running and records only their side,
        though a speakerphone's mic may pick the beep up (stt drops such tags)."""
        await asyncio.sleep(delay)
        try:
            await self.esl.api(f"uuid_displace {self.id} start {TIME_UP_TONE} 0 mux")
        except (TimeoutError, ESLError, ConnectionError) as e:
            self.esl.lg.info("time-up beep on %s failed: %r", self.id, e)


def originate_vars(values: dict[str, str]) -> str:
    """{name='value',...} for an originate dial string. Quotes keep commas and = inside a value."""
    return "{" + ",".join(f"{k}='{str(v).replace(chr(39), '')}'" for k, v in values.items()) + "}"


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _read_frame_sync(f) -> tuple[dict[str, str], str]:
    headers: dict[str, str] = {}
    while True:
        raw = f.readline()
        if not raw:
            raise ConnectionError("event socket closed")
        line = raw.decode("utf-8").rstrip("\r\n")
        if line:
            _parse_header(line, headers)
        elif headers:
            break
    length = int(headers.get("Content-Length", 0))
    return headers, (f.read(length).decode("utf-8") if length else "")


def api_sync(cmd: str, port: int, password: str, timeout: float = 5) -> str:
    """One command over a short-lived connection, for the command line and other non-async callers."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        f = sock.makefile("rb")
        _read_frame_sync(f)  # auth/request
        sock.sendall(f"auth {password}\n\n".encode())
        _check_reply(*_read_frame_sync(f))
        sock.sendall(f"api {cmd}\n\n".encode())
        return _check_reply(*_read_frame_sync(f))


def api_local(cmd: str) -> str:
    """One command to this PC's FreeSWITCH from outside the daemon: the port from config, the password from the
    keychain."""
    from . import config, keychain
    return api_sync(cmd, config.load().esl_port, keychain.get_or_create("esl"))
