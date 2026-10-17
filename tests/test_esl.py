"""The event socket client and the daemon's ring() against a fake FreeSWITCH: no real FreeSWITCH or phone."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from collections.abc import Callable

import pytest

from claude_phone import esl, phonebook

PASSWORD = "secret"
CHANNEL = "c-1"


class FakeFreeSwitch:
    """Speaks just enough of the event socket protocol. Tests script api replies and the events that follow
    a command (hooks)."""

    def __init__(self):
        self.commands: list[tuple[str, dict[str, str]]] = []
        self.api: Callable[[str], str] = lambda cmd: "+OK"
        self.hooks: list[Callable[[str, dict[str, str]], list[dict]]] = []
        self.writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def close(self) -> None:
        for w in self.writers:
            w.close()
        self.server.close()

    def _frame(self, writer, headers: dict[str, str], body: str = "") -> None:
        if body:
            headers = {**headers, "Content-Length": str(len(body.encode()))}
        writer.write(("".join(f"{k}: {v}\n" for k, v in headers.items()) + "\n" + body).encode())

    def send_event(self, ev: dict) -> None:
        for w in self.writers:
            self._frame(w, {"Content-Type": "text/event-json"}, json.dumps(ev))

    async def _client(self, reader, writer) -> None:
        self.writers.append(writer)
        self._frame(writer, {"Content-Type": "auth/request"})
        while True:
            try:
                raw = await reader.readuntil(b"\n\n")
            except (asyncio.IncompleteReadError, ConnectionError):
                return
            first, *rest = raw.decode().strip().split("\n")
            headers = dict(line.split(": ", 1) for line in rest)
            self.commands.append((first, headers))
            verb, _, arg = first.partition(" ")
            if verb == "auth":
                ok = arg == PASSWORD
                self._frame(writer, {"Content-Type": "command/reply",
                                     "Reply-Text": "+OK accepted" if ok else "-ERR invalid"})
                if not ok:
                    writer.close()
                    return
            elif verb == "api":
                self._frame(writer, {"Content-Type": "api/response"}, self.api(arg) + "\n")
            else:
                self._frame(writer, {"Content-Type": "command/reply", "Reply-Text": "+OK"})
            for hook in self.hooks:
                for ev in hook(first, headers) or []:
                    self.send_event(ev)

    def sent(self, prefix: str) -> list[tuple[str, dict[str, str]]]:
        return [c for c in self.commands if c[0].startswith(prefix)]


@asynccontextmanager
async def fake_freeswitch(tmp_path, password: str = PASSWORD, wait: bool = True):
    """A fake FreeSWITCH and a client logged in to it. Both are shut down however the test ends."""
    fs = FakeFreeSwitch()
    await fs.start()
    client = esl.ESL("127.0.0.1", fs.port, password, tmp_path)
    task = asyncio.create_task(client.run())
    try:
        if wait:
            await asyncio.wait_for(client.connected.wait(), 2)
        yield fs, client
    finally:
        task.cancel()
        fs.close()


def on_execute(app: str, events: Callable[[dict[str, str]], list[dict]]):
    """A hook: when a sendmsg runs `app`, send these events (given the sendmsg headers)."""
    def hook(first, headers):
        return events(headers) if first.startswith("sendmsg") and headers.get("execute-app-name") == app else []
    return hook


def complete(headers: dict[str, str], **extra) -> dict:
    return {"Event-Name": "CHANNEL_EXECUTE_COMPLETE", "Unique-ID": CHANNEL,
            "Application-UUID": headers["Event-UUID"], **extra}


def run(coro):
    return asyncio.run(coro)


def test_logs_in_and_subscribes(tmp_path):
    async def go():
        async with fake_freeswitch(tmp_path) as (fs, _):
            return [c[0] for c in fs.commands]
    first, second = run(go())[:2]
    assert first == f"auth {PASSWORD}"
    assert second.startswith("event json ") and "CHANNEL_PARK" in second and "sofia::register" in second
    assert "CHANNEL_HANGUP_COMPLETE" in second.split()


def test_wrong_password_never_connects(tmp_path):
    async def go():
        async with fake_freeswitch(tmp_path, password="wrong", wait=False) as (_, client):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(client.connected.wait(), 0.3)
    run(go())


def test_api_returns_output_and_raises_on_err(tmp_path):
    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            fs.api = lambda cmd: "-ERR no such thing" if cmd == "bad" else f"ran {cmd}"
            with pytest.raises(esl.ESLError):
                await client.api("bad")
            return await client.api("status")
    assert run(go()) == "ran status"


def test_play_queues_the_files_in_order_and_waits_for_the_end(tmp_path):
    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            fs.hooks.append(on_execute("playback", lambda h: [complete(h)]))
            return await esl.Call(client, CHANNEL).play("C:/s/a.wav", "C:/s/b.wav"), fs
    result, fs = run(go())
    assert result is None
    (_, headers), = fs.sent("sendmsg")
    assert headers["execute-app-arg"] == "file_string://C:/s/a.wav!C:/s/b.wav"
    assert ("api uuid_setvar c-1 playback_terminators none", {}) in fs.commands


def test_a_digit_interrupts_playback(tmp_path):
    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            fs.hooks.append(on_execute("playback", lambda h: [
                {"Event-Name": "DTMF", "Unique-ID": CHANNEL, "DTMF-Digit": "7"}]))
            return await esl.Call(client, CHANNEL).play("C:/s/q.wav", interrupt="#7"), fs
    digit, fs = run(go())
    assert digit == "7"
    assert fs.sent("api uuid_break c-1")


def test_hanging_up_during_playback_raises_hangup(tmp_path):
    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            fs.hooks.append(on_execute("playback", lambda h: [{"Event-Name": "CHANNEL_HANGUP", "Unique-ID": CHANNEL}]))
            call = esl.Call(client, CHANNEL)
            with pytest.raises(esl.Hangup):
                await call.play("C:/s/q.wav")
            return call.up
    assert run(go()) is False


def test_answering_a_channel_that_is_gone_raises_hangup(tmp_path):
    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            fs.api = lambda cmd: "-ERR No such channel!"
            with pytest.raises(esl.Hangup):
                await esl.Call(client, CHANNEL).answer()
    run(go())


def test_record_writes_16k_wav_and_reports_the_digit_that_ended_it(tmp_path):
    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            fs.hooks.append(on_execute("record", lambda h: [complete(h, variable_playback_terminator_used="#")]))
            return await esl.Call(client, CHANNEL).record("in-301-1", 60), fs
    result, fs = run(go())
    assert result == ("digit", "#")
    (_, headers), = fs.sent("sendmsg")
    assert headers["execute-app-arg"] == f"{(tmp_path / 'in-301-1.wav').as_posix()} 60"
    assert ("api uuid_setvar c-1 record_sample_rate 16000", {}) in fs.commands
    assert ("api uuid_setvar c-1 playback_terminators #", {}) in fs.commands


def test_record_reports_only_a_digit_that_ends_it(tmp_path):
    # A 5 pressed while talking doesn't end a #-terminated recording, so the recording ended some other way.
    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            fs.hooks.append(on_execute("record", lambda h: [
                {"Event-Name": "DTMF", "Unique-ID": CHANNEL, "DTMF-Digit": "5"}, complete(h)]))
            return await esl.Call(client, CHANNEL).record("in-301-4", 60)
    assert run(go()) == ("finished", None)


def test_record_with_a_silence_limit_and_no_digit_finishes(tmp_path):
    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            fs.hooks.append(on_execute("record", lambda h: [complete(h)]))
            return await esl.Call(client, CHANNEL).record("op-1-0", 10, max_silence=2), fs
    result, fs = run(go())
    assert result == ("finished", None)
    (_, headers), = fs.sent("sendmsg")
    assert headers["execute-app-arg"].endswith(f" 10 {esl.SILENCE_THRESHOLD} 2")
    assert fs.sent("api uuid_displace") == []  # too short for a warning


def test_record_beeps_shortly_before_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(esl, "RECORD_WARN_S", 59.9)  # warn 0.1 s into a 60 s recording
    record = {}

    def hook(first, headers):
        if first.startswith("sendmsg") and headers.get("execute-app-name") == "record":
            record.update(headers)
        if first.startswith("api uuid_displace"):  # the caller heard the warning; now the time runs out
            return [complete(record)]
        return []

    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            fs.hooks.append(hook)
            return await esl.Call(client, CHANNEL).record("in-301-2", 60), fs
    result, fs = run(go())
    assert result == ("finished", None)
    (cmd, _), = fs.sent("api uuid_displace")
    assert cmd == f"api uuid_displace {CHANNEL} start {esl.TIME_UP_TONE} 0 mux"


@pytest.mark.parametrize("ending", ["digit", "hangup"])
def test_a_recording_that_ends_early_never_beeps(tmp_path, monkeypatch, ending):
    monkeypatch.setattr(esl, "RECORD_WARN_S", 59.8)  # the beep would come 0.2 s in
    events = {"digit": lambda h: [complete(h, variable_playback_terminator_used="#")],
              "hangup": lambda h: [{"Event-Name": "CHANNEL_HANGUP", "Unique-ID": CHANNEL}]}

    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            fs.hooks.append(on_execute("record", events[ending]))
            result = await esl.Call(client, CHANNEL).record("in-301-3", 60)
            await asyncio.sleep(0.4)  # past when the beep would have come
            return result, fs
    result, fs = run(go())
    assert result[0] == ending
    assert fs.sent("api uuid_displace") == []


def test_reconnects_after_freeswitch_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(esl, "RECONNECT_S", 0.05)

    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            for w in fs.writers:
                w.close()
            await asyncio.sleep(0.02)
            await asyncio.wait_for(client.connected.wait(), 2)
            return len(fs.sent("auth"))
    assert run(go()) == 2


def test_originate_variables_are_quoted():
    assert esl.originate_vars({"a": "x,y", "b": "it's"}) == "{a='x,y',b='its'}"


def test_an_answered_call_keeps_sending_audio_between_playbacks(tmp_path):
    # Without it FreeSWITCH stops the RTP stream whenever nothing plays, and the softphone's adaptive jitter
    # buffer stretches the audio each time it starts again.
    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            await esl.Call(client, "c1").answer()
            return [first for first, _ in fs.sent("api uuid_")]
    assert run(go()) == [f"api uuid_setvar c1 send_silence_when_idle {esl.IDLE_SILENCE}", "api uuid_answer c1"]


# --- the daemon's ring() ---

def on_originate(events: Callable[[str, dict[str, str]], list[dict]]):
    def hook(first, headers):
        if not first.startswith("bgapi originate"):
            return []
        chan = re.search(r"origination_uuid='([^']+)'", first).group(1)
        return events(chan, headers)
    return hook


CALLER = {"label": "repo/main", "repo": "repo", "ext": 301}


@pytest.fixture
def ringer(controller):
    controller.s.lan_ip, controller.s.ring_ext = "192.168.0.24", "201"
    return controller


def test_ring_returns_the_call_once_answered_and_parked(tmp_path, ringer):
    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            events = []
            client.on_event(lambda e: events.append(e["Event-Name"]))
            fs.hooks.append(on_originate(lambda chan, h: [
                {"Event-Name": "CHANNEL_ANSWER", "Unique-ID": chan},
                {"Event-Name": "CHANNEL_PARK", "Unique-ID": chan, "Call-Direction": "outbound"}]))
            ringer.esl = client
            call, why = await ringer.ring(CALLER, "cj")
            return call, why, fs, events
    call, why, fs, events = run(go())
    assert call is not None and why == "" and call.id.startswith("cj-")
    # Ready only after the park: playing on a channel that's answered but not yet parked gets cut off.
    assert events == ["CHANNEL_ANSWER", "CHANNEL_PARK"]
    (dial, _), = fs.sent("bgapi originate")
    assert "user/201@192.168.0.24 &park()" in dial
    assert "origination_caller_id_name='repo/main'" in dial and "sip_h_Alert-Info='<http://127.0.0.1>;info=Ring" in dial
    assert f"send_silence_when_idle='{esl.IDLE_SILENCE}'" in dial


def test_ring_reports_why_an_unanswered_call_failed(tmp_path, ringer):
    async def go():
        async with fake_freeswitch(tmp_path) as (fs, client):
            fs.hooks.append(on_originate(lambda chan, h: [
                {"Event-Name": "BACKGROUND_JOB", "Job-UUID": h["Job-UUID"], "_body": "-ERR NO_ANSWER\n"}]))
            ringer.esl = client
            return await ringer.ring(CALLER, "cj")
    assert run(go()) == (None, "not answered (NO_ANSWER)")


HANGUP_COMPLETE = {
    "Event-Name": "CHANNEL_HANGUP_COMPLETE", "Unique-ID": "3f2a9c1e-0000", "Call-Direction": "outbound",
    "Caller-Destination-Number": "201", "variable_read_codec": "PCMU", "variable_read_rate": "8000",
    "variable_write_codec": "PCMU", "variable_rtp_audio_in_packet_count": "1500",
    "variable_rtp_audio_in_skip_packet_count": "3", "variable_rtp_audio_in_jitter_packet_count": "12",
    "variable_rtp_audio_in_flaw_total": "2", "variable_rtp_audio_in_mos": "4.38",
    "variable_rtp_audio_in_jitter_max_variance": "21.50", "variable_rtp_audio_out_packet_count": "1498",
    "variable_rtp_audio_out_skip_packet_count": "0",
}


def test_call_stats_sum_up_a_leg_in_one_line_marking_what_is_missing():
    line = phonebook.call_stats(HANGUP_COMPLETE)
    assert line.startswith("audio 201 outbound 3f2a9c1e:")
    for part in ("codec=PCMU/8000", "write=PCMU", "in_pkts=1500", "in_skip=3", "in_jitter=12", "in_flaws=2",
                 "mos=4.38", "jitter_var=21.50", "out_pkts=1498", "out_skip=0"):
        assert part in line.split()
    partial = {k: v for k, v in HANGUP_COMPLETE.items() if k != "variable_rtp_audio_in_mos"}
    assert "mos=?" in phonebook.call_stats(partial).split()
    one_way = {k: v for k, v in HANGUP_COMPLETE.items() if "rtp_audio_in" not in k}  # the phone sent nothing
    assert "in_pkts=? " in phonebook.call_stats(one_way) and "out_pkts=1498" in phonebook.call_stats(one_way)
    no_media = {k: v for k, v in HANGUP_COMPLETE.items() if "rtp_audio" not in k}
    assert phonebook.call_stats(no_media) is None
    assert phonebook.call_stats({**HANGUP_COMPLETE, "Event-Name": "CHANNEL_HANGUP"}) is None
