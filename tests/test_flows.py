"""Call flows against a scripted fake call: no FreeSWITCH, audio, whisper or VS Code."""

import asyncio
import json

import pytest

from claude_phone import daemon, daemon_http, inject, push, registry, tts
from claude_phone.config import Settings
from claude_phone.phonebook import browser_xml, caller_id, phonebook_xml
from claude_phone.registry import Session


class FakeCall:
    def __init__(self, digits=(), recordings=()):
        self.id = "c1"
        self.up = True
        self.played = []
        self.digits = list(digits)  # what each interruptible play returns
        self.recordings = list(recordings)
        self.limits = []  # max_seconds of each recording

    async def answer(self):
        pass

    async def play(self, *media, interrupt="", until=None):
        self.played.extend(media)
        return self.digits.pop(0) if interrupt and self.digits else None

    async def record(self, name, max_seconds, terminate_on="#", max_silence=None):
        self.limits.append(max_seconds)
        return self.recordings.pop(0) if self.recordings else ("finished", None)

    def drain_digits(self):
        pass

    async def hangup(self):
        self.up = False

    def close(self):
        pass


@pytest.fixture
def ctl(controller, monkeypatch):
    """A Controller with speech, transcription, typing and the message light stubbed out."""
    c = controller

    async def no_key(key):
        return False
    monkeypatch.setattr(daemon.screen, "press", no_key)
    c.typed, c.heard, c.pushed = [], [], []

    async def render_async(text, repo=None, voice=None):
        return f"tts:{text}"
    monkeypatch.setattr(tts, "render", lambda text, repo=None, voice=None: f"tts:{text}")
    monkeypatch.setattr(tts, "render_async", render_async)
    monkeypatch.setattr(tts, "beep", lambda: "beep")
    monkeypatch.setattr(tts, "join_wavs", lambda paths: "+".join(paths))

    async def transcribe(name):
        return c.heard.pop(0) if c.heard else ""
    monkeypatch.setattr(c, "transcribe", transcribe)

    async def nothing(*a, **k):
        return False
    monkeypatch.setattr(c, "update_mwi", nothing)
    monkeypatch.setattr(c, "mid_task", nothing)

    def type_into(sess, text, source):
        c.typed.append((sess.ext, text))
        return "sent"
    monkeypatch.setattr(inject, "type_into", type_into)
    monkeypatch.setattr(push, "missed", lambda *a: c.pushed.append(a))
    return c


def run(coro):
    return asyncio.run(coro)


def answering(ctl, monkeypatch, call, why=""):
    """The phone is online and ring() returns this call, or None with `why` for an unanswered one."""
    async def online():
        return True

    async def ring(c, prefix, auto_answer=False):
        return call, why
    monkeypatch.setattr(ctl, "desk_online", online)
    monkeypatch.setattr(ctl, "ring", ring)


# --- dialling in ---

def test_dial_in_types_what_was_said_marked_by_phone_or_by_the_transfer_prefix(ctl):
    registry.register("s", "site-be-v2", "main", None, "/r", "tab")
    ctl.heard = ["What colour should the button be?", "Testing."]
    call = FakeCall()
    assert run(ctl.dial_session(call, 301)) == "What colour should the button be?"
    assert "tts:Sent." in call.played
    run(ctl.dial_session(FakeCall(), 301, prefix="[Phone transfer from app-fe-v2] Sam says:"))
    assert ctl.typed == [(301, "[By phone] What colour should the button be?"),
                         (301, "[Phone transfer from app-fe-v2] Sam says: Testing.")]


def test_messages_get_the_long_limit_and_answers_the_short_one(ctl, monkeypatch):
    ctl.s.message_seconds, ctl.s.record_seconds = 300, 60
    registry.register("s", "repo", "main", None, "/r", "tab")
    dial_in, all_hands, reply = FakeCall(), FakeCall(), FakeCall()
    ctl.heard = ["One.", "Two.", "Three."]
    run(ctl.dial_session(dial_in, 301))
    run(ctl.all_hands(all_hands))
    registry.add_voicemail("s", 301, "repo/main", "Ship?", None, None)
    run(ctl.voicemail(reply))
    assert dial_in.limits == all_hands.limits == reply.limits == [300]

    answer = FakeCall()
    answering(ctl, monkeypatch, answer)
    ctl.heard = ["Yes."]
    res = run(ctl.call_owner({"question": "Ship it", "session_id": "s", "term_id": "tab"}))
    assert res == {"status": "answered", "answer": "Yes."} and answer.limits == [60]


def test_dial_in_silence_sends_nothing(ctl):
    registry.register("s", "repo", "main", None, "/r", "tab")
    call = FakeCall()
    assert run(ctl.dial_session(call, 301)) is None
    assert ctl.typed == []
    assert "tts:" + tts.PHRASES["nothing_heard"] in call.played


def test_dial_in_empty_extension(ctl):
    call = FakeCall()
    run(ctl.dial_session(call, 399))
    assert call.played == ["tts:" + tts.PHRASES["no_session"]]


@pytest.mark.parametrize("dialled, flow", [
    ("500", "voicemail"), ("0", "operator"), ("9", "all_hands"), ("301", "session 301"), ("123", "no_session"),
])
def test_each_number_you_dial_reaches_its_flow(ctl, monkeypatch, dialled, flow):
    reached, call = [], FakeCall()
    monkeypatch.setattr(daemon, "Call", lambda esl, uid: call)
    for name in ("voicemail", "operator", "all_hands"):
        async def flow_(c, name=name):
            reached.append(name)
        monkeypatch.setattr(ctl, name, flow_)

    async def dial_session(c, ext):
        reached.append(f"session {ext}")
    monkeypatch.setattr(ctl, "dial_session", dial_session)
    run(ctl.inbound("u1", dialled, "200"))
    assert (reached or ["no_session"]) == [flow]
    assert call.up is False  # hung up afterwards, whichever flow ran


def test_a_second_caller_hears_the_line_is_busy(ctl, monkeypatch):
    call = FakeCall()
    monkeypatch.setattr(daemon, "Call", lambda esl, uid: call)

    async def scenario():
        async with ctl.line:  # a call with the owner is under way
            await ctl.inbound("u2", "301", "201")
    run(scenario())
    assert call.played == ["tts:" + tts.PHRASES["busy"]] and ctl.typed == []


# --- the operator (dial 0) ---

@pytest.fixture
def operator_voice(monkeypatch):
    """The operator's lines as "op:<text>", rendered off the event loop: a cold cache would hold up every call."""
    async def op_async(text):
        return f"op:{text}"
    monkeypatch.setattr(tts, "op", lambda text: pytest.fail(f"rendered on the event loop: {text!r}"))
    monkeypatch.setattr(tts, "op_async", op_async)


def test_the_operator_puts_you_through_to_the_session_you_name(ctl, operator_voice):
    registry.register("a", "app-fe-v2", "main", None, "/a", "t1")
    be = registry.register("b", "site-be-v2", "main", None, "/b", "t2")
    ctl.heard = ["The backend, please.", "Is the API up?"]
    call = FakeCall()
    run(ctl.operator(call))
    assert "op:" + tts.OPERATOR["connecting"].format(label=ctl.spoken_name(be)) in call.played
    assert ctl.typed == [(be.ext, "[By phone] Is the API up?")]


def test_the_operator_asks_which_one_then_gives_up(ctl, operator_voice):
    registry.register("a", "site-be-v2", "main", None, "/a", "t1")
    registry.register("b", "site-be-v2", "staging", None, "/b", "t2")
    ctl.heard = ["The site backend.", "The weather."]
    call = FakeCall()
    run(ctl.operator(call))
    assert any(p.startswith("op:I've got site be v2, main, extension 301 or") for p in call.played)
    assert call.played[-1] == "op:" + tts.OPERATOR["unplaced"] and ctl.typed == []


# --- voicemail ---

def test_holding_for_a_busy_session_renders_its_lines_off_the_event_loop(ctl, monkeypatch, operator_voice):
    sess = registry.register("s", "repo", "main", None, "/r", "tab")

    async def busy(ext):
        return True
    monkeypatch.setattr(ctl, "mid_task", busy)
    monkeypatch.setattr(daemon.hold, "render", lambda: "music")
    call = FakeCall(digits=["#"])  # the caller leaves a message now rather than hold
    run(ctl.hold(call, sess))
    assert call.played == [f"op:{tts.OPERATOR['hold'].format(label=ctl.spoken_name(sess))}"]


def test_voicemail_answer_skip_delete(ctl):
    registry.register("s", "repo", "main", None, "/r", "tab")
    registry.add_voicemail("s", 301, "repo/main", "Merge it?", None, None)
    b = registry.add_voicemail("s", 301, "repo/main", "Which colour?", None, None)
    registry.add_voicemail("s", 301, "repo/main", "Delete me", None, None)
    ctl.heard = ["Yes, merge it."]
    call = FakeCall(digits=[None, "#", "7"])  # answer the first, skip the second, delete the third
    run(ctl.voicemail(call))
    assert ctl.typed == [
        (301, '[Phone answer to your earlier question] Yes, merge it. (Your question was: "Merge it?")')]
    assert [v.id for v in registry.pending_voicemails()] == [b]


def test_answer_for_closed_session_is_kept(ctl):
    registry.add_voicemail("gone", None, "repo/main", "Ship?", None, None)
    ctl.heard = ["Yes."]
    call = FakeCall()
    run(ctl.voicemail(call))
    assert ctl.typed == [] and registry.pending_voicemails() == []
    assert "tts:" + tts.PHRASES["vm_session_gone"] in call.played


# --- Claude calling ---

def test_unanswered_call_leaves_voicemail_and_pushes(ctl, monkeypatch):
    registry.register("s", "repo", "main", None, "/r", "tab")
    answering(ctl, monkeypatch, None, "not answered")
    res = run(ctl.call_owner({"question": "Merge it", "session_id": "s", "term_id": "tab"}))
    assert res["status"] == "voicemail"
    assert [v.question for v in registry.pending_voicemails()] == ["Merge it."]
    assert ctl.pushed == [("Missed call", "repo/main", "Merge it.", "not answered")]


def test_a_question_left_as_voicemail_is_one_recording(ctl):
    registry.register("s", "repo", "main", None, "/r", "tab")
    run(ctl.call_owner({"question": "Merge it", "context": "The build is green", "session_id": "s",
                        "_force_voicemail": "line busy"}))
    vm, = registry.pending_voicemails()
    assert vm.audio == "tts:repo.+tts:The build is green. Merge it."


def test_a_long_message_is_spoken_a_sentence_at_a_time_after_the_greeting(ctl, monkeypatch):
    registry.register("s", "repo", "main", None, "/r", "tab")
    call = FakeCall()
    answering(ctl, monkeypatch, call)
    message = "Done. The tests pass, all two hundred of them. Shall I merge it? Next I'll write the docs."
    assert run(ctl.notify_owner({"message": message, "session_id": "s"}))["status"] == "delivered"
    assert call.played == ["tts:repo.", "tts:Done. The tests pass, all two hundred of them.",
                           "tts:Shall I merge it? Next I'll write the docs.", tts.phrase("goodbye")]


def test_speaking_starts_before_the_rest_is_rendered(ctl, monkeypatch):
    registry.register("s", "repo", "main", None, "/r", "tab")

    async def scenario():
        heard = asyncio.Event()

        class Call(FakeCall):
            async def play(self, *media, **kw):
                await super().play(*media, **kw)
                heard.set()

        async def render_async(text, repo=None, voice=None):
            if text.startswith("And"):
                await asyncio.wait_for(heard.wait(), 2)  # rendered only once the greeting was heard
            return f"tts:{text}"
        monkeypatch.setattr(tts, "render_async", render_async)
        call = Call()
        answering(ctl, monkeypatch, call)
        await ctl.notify_owner({"message": "The first part is ready now. And the rest comes later.",
                                "session_id": "s"})
        return call.played
    assert run(scenario())[-2] == "tts:And the rest comes later."


def test_a_cold_engine_starts_before_anything_plays(ctl, monkeypatch):
    from claude_phone import voiceworker
    registry.register("s", "repo", "main", None, "/r", "tab")
    events = []

    async def render_async(text, repo=None, voice=None):
        events.append(f"render {text}")
        return f"tts:{text}"

    class Call(FakeCall):
        async def play(self, *media, **kw):
            events.append(f"play {media[0]}")
            await super().play(*media, **kw)
    monkeypatch.setattr(tts, "render_async", render_async)
    monkeypatch.setattr(tts, "voice_for", lambda repo: ("kokoro", "am_adam"))
    monkeypatch.setattr(voiceworker, "is_warm", lambda engine: False)
    answering(ctl, monkeypatch, Call())
    run(ctl.notify_owner({"message": "The first sentence is long enough to stand alone. Then more.",
                          "session_id": "s"}))
    # Its start (loading the model) made the first sentence stutter when it overlapped the playing.
    assert events.index("render The first sentence is long enough to stand alone. Then more.") \
        < events.index("play tts:repo.")


def test_two_sessions_on_one_branch_are_told_apart_by_their_extension(ctl, monkeypatch):
    a = registry.register("s1", "site", "fix-login", None, "/r", "t1")
    b = registry.register("s2", "site", "fix-login", None, "/r", "t2")
    call = FakeCall()
    answering(ctl, monkeypatch, call)
    run(ctl.transfer({"to": str(b.ext), "reason": "Over to you.", "session_id": "s1", "term_id": "t1"}))
    assert call.played[:2] == [f"tts:site, fix login, {a.ext}.", f"tts:Over to you. Putting you through to site, "
                                                                f"fix login, {b.ext}."]
    assert f"tts:You're through to site, fix login, {b.ext}." in call.played
    registry.unregister("s2")
    alone = FakeCall()
    answering(ctl, monkeypatch, alone)
    run(ctl.notify_owner({"message": "Hi", "session_id": "s1"}))
    assert alone.played[0] == "tts:site, fix login."  # no one to confuse it with


def test_a_transfer_replies_once_connected_and_the_words_follow_after_the_hold(ctl, monkeypatch):
    registry.register("s1", "site-fe", "main", None, "/r", "t1")
    be = registry.register("s2", "site-be", "main", None, "/r", "t2")
    answering(ctl, monkeypatch, FakeCall())
    ctl.heard = ["Is the API up?"]

    class Request:
        async def json(self):
            return {"to": "site-be", "reason": "A backend question.", "session_id": "s1", "term_id": "t1"}

    async def scenario():
        turn_over = asyncio.Event()

        async def hold(call, sess):  # the target is mid-task, for longer than Claude's tool call may wait
            await turn_over.wait()
        monkeypatch.setattr(ctl, "hold", hold)
        reply = await asyncio.wait_for(daemon_http.http_transfer(ctl, Request()), 2)
        assert json.loads(reply.text) == {"status": "connecting", "to": "site-be/main", "ext": be.ext}
        assert ctl.typed == []
        turn_over.set()
        await asyncio.gather(*ctl._tasks)
    run(scenario())
    assert ctl.typed == [(be.ext, f"[Phone transfer from site-fe/main] {ctl.s.owner} says: Is the API up?")]


def test_a_transfer_over_before_the_reply_still_says_connecting(ctl, monkeypatch):
    registry.register("s1", "site-fe", "main", None, "/r", "t1")
    registry.register("s2", "site-be", "main", None, "/r", "t2")
    answering(ctl, monkeypatch, FakeCall())  # the target is idle and the message quick
    ctl.heard = ["Is the API up?"]

    class Request:
        async def json(self):
            return {"to": "site-be", "reason": "Over to you.", "session_id": "s1", "term_id": "t1"}
    assert json.loads(run(daemon_http.http_transfer(ctl, Request())).text)["status"] == "connecting"


def test_each_live_sessions_greeting_is_ready_before_it_calls(ctl, monkeypatch):
    registry.register("s", "site", "fix-login", None, "/r", "tab")
    rendered = []
    monkeypatch.setattr(tts, "render", lambda text, repo=None, voice=None, fallback=True:
                        rendered.append((text, repo)))
    run(ctl.warm_greetings())
    call = FakeCall()
    answering(ctl, monkeypatch, call)
    monkeypatch.setattr(tts, "render", lambda text, repo=None, voice=None: f"tts:{text}")
    run(ctl.notify_owner({"message": "Hi", "session_id": "s"}))
    assert rendered == [(call.played[0].removeprefix("tts:"), "site")]


def test_a_greeting_that_fails_waits_before_it_is_tried_again(ctl, monkeypatch):
    registry.register("s1", "broken", "main", None, "/r", "tab1")
    registry.register("s2", "fine", "main", None, "/r", "tab2")
    tried = []

    def render(text, repo=None, voice=None, fallback=True):
        tried.append(repo)
        assert not fallback  # a fallback would hide the failure
        if repo == "broken":
            raise RuntimeError("the kokoro engine didn't start")
        return f"tts:{text}"
    monkeypatch.setattr(tts, "render", render)
    run(ctl.warm_greetings())
    run(ctl.warm_greetings())  # a minute later
    assert sorted(tried) == ["broken", "fine", "fine"]  # the other session still got its greeting


def test_hanging_up_stops_rendering_the_rest(ctl, monkeypatch):
    registry.register("s", "repo", "main", None, "/r", "tab")

    async def scenario():
        rendered = []

        async def render_async(text, repo=None, voice=None):
            rendered.append(text)
            await asyncio.sleep(0)
            return f"tts:{text}"
        monkeypatch.setattr(tts, "render_async", render_async)

        class HangsUp(FakeCall):
            async def play(self, *media, **kw):
                raise daemon.Hangup()
        answering(ctl, monkeypatch, HangsUp())
        res = await ctl.notify_owner({"message": "The first part is ready now. And then a lot more is said "
                                      "after it. And more again, for a long while yet.", "session_id": "s"})
        await asyncio.sleep(0.05)
        return res, rendered
    res, rendered = run(scenario())
    assert res["status"] == "delivered" and len(rendered) < 4


def test_a_question_that_fails_midway_stops_rendering_its_speech(ctl, monkeypatch):
    # http_call follows with a forced voicemail, which renders its own: this one would only hold up the engine.
    registry.register("s", "repo", "main", None, "/r", "tab")
    cancelled = []

    class Speech:
        def cancel(self):
            cancelled.append(True)
    monkeypatch.setattr(ctl, "speech", lambda c, text: Speech())

    async def broken():
        raise ConnectionError("event socket closed")
    monkeypatch.setattr(ctl, "desk_online", broken)
    with pytest.raises(ConnectionError):
        run(ctl.call_owner({"question": "Merge it", "session_id": "s"}))
    assert cancelled


def test_hanging_up_on_a_question_leaves_a_voicemail_naming_the_owner_without_a_push(ctl):
    ctl.s.owner_name = "Sam"
    registry.register("s", "repo", "main", None, "/r", "tab")
    res = run(ctl.call_owner({"question": "Merge it", "session_id": "s",
                              "_force_voicemail": "hung up before answering"}))
    assert res["status"] == "voicemail" and ctl.pushed == []
    assert res["message"].startswith("Sam didn't answer.")


def test_a_missed_call_push_leaves_the_question_out_when_push_text_is_off(monkeypatch):
    sent = []
    monkeypatch.setattr(push, "send", lambda title, body: sent.append((title, body)))

    async def missed(push_text):
        monkeypatch.setattr(push.config, "load", lambda: Settings(push_text=push_text))
        push.missed("Missed call", "repo/main", "Merge the payments PR?", "not answered")
        await asyncio.sleep(0.05)  # the executor runs the send
    run(missed(True))
    run(missed(False))
    assert sent == [("Missed call from repo/main", "Merge the payments PR?\n(not answered)"),
                    ("Missed call from repo/main", "(not answered)")]


# --- meetings ---

def test_the_meeting_whisper_speaks_with_claudes_voice_unless_set_otherwise(ctl, monkeypatch):
    voices = []

    async def render_async(text, repo=None, voice=None):
        voices.append(voice)
        return f"tts:{text}"
    monkeypatch.setattr(tts, "render_async", render_async)
    answering(ctl, monkeypatch, FakeCall())
    ctl.s.whisper_voice = ""
    assert run(ctl.whisper("Meeting in two minutes.", "14:00 Board")) == "delivered"
    ctl.s.whisper_voice = "windows:Microsoft Zira Desktop"
    run(ctl.whisper("Meeting in two minutes.", "14:00 Board"))
    ctl.s.whisper_voice = "kokro:bf_emma"  # no such engine: Claude's voice rather than the Windows stand-in
    run(ctl.whisper("Meeting in two minutes.", "14:00 Board"))
    assert voices == [None, ("windows", "Microsoft Zira Desktop"), None]  # None: Claude's voice, whichever it is


def test_the_meeting_whisper_is_ready_before_a_meeting(ctl, monkeypatch):
    rendered = []
    monkeypatch.setattr(tts, "render", lambda text, repo=None, voice=None, fallback=True:
                        rendered.append((text, voice)))
    ctl.s.meeting_alerts, ctl.s.whisper_text, ctl.s.whisper_voice = True, "Psst. Meeting soon.", ""
    run(ctl.warm_greetings())
    assert rendered == [("Psst. Meeting soon.", None)]  # answered on the speaker at once: no ringing to hide it
    ctl.s.meeting_alerts, rendered[:] = False, []
    run(ctl.warm_greetings())
    assert rendered == []


def meeting(start_min, length_min=30, title="Board review", people=("Sarah Jones", "Dan Smith")):
    from datetime import datetime, timedelta
    from claude_phone import diary
    now = datetime(2026, 10, 14, 14, 58).astimezone()
    start = now + timedelta(minutes=start_min)
    return now, diary.Meeting(f"e1@{start}", title, start, start + timedelta(minutes=length_min), people)


def test_meeting_whispers_once_shows_screen_and_holds_calls(ctl, monkeypatch):
    from claude_phone import diary, screen
    now, m = meeting(2)
    monkeypatch.setattr(diary, "window", lambda *a: [m])
    shown, whispered = [], []

    async def show(title, text, timeout=120):
        shown.append((title, text))

    async def whisper(text, label):
        whispered.append(label)
    monkeypatch.setattr(screen, "show", show)
    monkeypatch.setattr(ctl, "whisper", whisper)
    alerted = set()
    run(ctl.check_meetings(now, alerted))
    run(ctl.check_meetings(now, alerted))
    assert whispered == [f"{m.start:%H:%M} Board review"]
    assert shown == [(f"{m.start:%H:%M} Board review", f"with Sarah, Dan\nDND until {m.end:%H:%M}")]
    assert ctl.meeting == m

    registry.register("s", "repo", "main", None, "/r", "tab")
    res = run(ctl.call_owner({"question": "Merge it", "session_id": "s"}))
    assert res["reason"] == "in a meeting" and ctl.pushed == []
    ctl.s.screen_push = True
    assert run(ctl.notify_owner({"message": "Build done", "session_id": "s"}))["status"] == "on_screen"


def test_a_past_meeting_is_forgotten_once_it_leaves_the_window(ctl, monkeypatch, keys):
    from claude_phone import diary
    now, m = meeting(2)
    window = [m]
    monkeypatch.setattr(diary, "window", lambda *a: window)
    alerted = set()
    run(ctl.check_meetings(now, alerted))
    ctl.set_phone_dnd(False)  # ended early with the DND key
    assert alerted == ctl.released == {m.key}
    window.clear()
    run(ctl.check_meetings(m.end, alerted))
    assert alerted == ctl.released == set()  # a daemon running for weeks doesn't collect every meeting


def test_meeting_already_under_way_holds_calls_without_alert(ctl, monkeypatch):
    from claude_phone import diary
    now, m = meeting(-10)
    monkeypatch.setattr(diary, "window", lambda *a: [m])

    async def boom(*a, **k):
        raise AssertionError("no alert expected")
    monkeypatch.setattr(ctl, "whisper", boom)
    run(ctl.check_meetings(now, set()))
    assert ctl.meeting == m


def test_dnd_key_off_ends_a_meeting_hold_and_on_holds_calls(ctl, monkeypatch):
    from claude_phone import diary, screen
    now, m = meeting(2)
    monkeypatch.setattr(diary, "window", lambda *a: [m])

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(screen, "show", nothing)
    monkeypatch.setattr(ctl, "whisper", nothing)
    run(ctl.check_meetings(now, set()))
    assert ctl.hold_reason() == "in a meeting"
    ctl.set_phone_dnd(False)  # DND key off: the meeting finished early
    assert ctl.hold_reason() is None
    ctl.set_phone_dnd(True)
    assert ctl.hold_reason() == "in a meeting"
    monkeypatch.setattr(diary, "window", lambda *a: [])
    run(ctl.check_meetings(now, set()))
    assert ctl.hold_reason() == "do not disturb"
    ctl.set_phone_dnd(False)
    assert ctl.hold_reason() is None


@pytest.fixture
def keys(ctl, monkeypatch):
    """The desk phone's remote keys: (keys pressed, {"ok": what the next press returns}). No whisper, no screen."""
    from claude_phone import screen
    pressed, answer = [], {"ok": True}

    async def press(key):
        pressed.append(key)
        return answer["ok"]

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(screen, "press", press)
    monkeypatch.setattr(screen, "show", nothing)
    monkeypatch.setattr(ctl, "whisper", nothing)
    return pressed, answer


def test_the_dnd_switched_on_for_a_meeting_is_switched_off_after_it(ctl, monkeypatch, keys):
    from claude_phone import diary
    pressed, _ = keys
    now, m = meeting(2)
    monkeypatch.setattr(diary, "window", lambda *a: [m])
    run(ctl.check_meetings(now, set()))
    monkeypatch.setattr(diary, "window", lambda *a: [])
    run(ctl.check_meetings(m.end, set()))
    assert pressed == ["DNDOn", "DNDOff"] and not ctl.dnd_ours  # though no Action URL reported DND as on


def test_a_phone_that_misses_the_dnd_off_key_gets_it_again_for_an_hour(ctl, monkeypatch, keys):
    from datetime import timedelta
    from claude_phone import diary
    pressed, answer = keys
    now, m = meeting(2)
    ctl.started = now  # the daemon has been running since before the meeting
    window = [m]
    monkeypatch.setattr(diary, "window", lambda *a: window)
    run(ctl.check_meetings(now, set()))
    window.clear()
    answer["ok"] = False  # the phone is offline
    run(ctl.check_meetings(m.end, set()))
    run(ctl.check_meetings(m.end + timedelta(seconds=15), set()))
    assert pressed == ["DNDOn", "DNDOff", "DNDOff"] and ctl.dnd_ours
    run(ctl.check_meetings(m.end + timedelta(hours=1, seconds=15), set()))
    run(ctl.check_meetings(m.end + timedelta(hours=1, seconds=30), set()))
    assert pressed.count("DNDOff") == 3 and not ctl.dnd_ours  # given up


def test_a_restarted_daemon_still_switches_off_its_dnd_unless_that_was_long_ago(ctl, monkeypatch, keys):
    from datetime import datetime, timedelta
    from claude_phone import config, diary
    pressed, _ = keys
    now = datetime.now().astimezone()
    m = diary.Meeting("e2", "Standup", now + timedelta(minutes=2), now + timedelta(minutes=17), ())
    monkeypatch.setattr(diary, "window", lambda *a: [m])
    run(ctl.check_meetings(now, set()))
    restarted = daemon.Controller(config.load(), esl=None)
    assert restarted.dnd_ours == m.end
    monkeypatch.setattr(diary, "window", lambda *a: [])
    run(restarted.check_meetings(m.end, set()))
    assert pressed == ["DNDOn", "DNDOff"]

    registry.set_state("dnd_ours", (now - timedelta(hours=13)).isoformat())
    assert not daemon.Controller(config.load(), esl=None).dnd_ours  # a DND on now is more likely the owner's
    assert registry.get_state("dnd_ours") is None


def test_without_a_meeting_watch_a_restored_dnd_is_still_retried_until_the_phone_takes_it(ctl, monkeypatch, keys):
    # Meeting alerts off (or no calendar link) after a restart, the PC just booted: the phone isn't reachable yet.
    from datetime import datetime, timedelta
    pressed, answer = keys
    ctl.set_dnd_ours(datetime.now().astimezone() - timedelta(hours=2))  # past the hour's grace: from start-up on

    async def sleep(seconds):
        answer["ok"] = True  # online by the next try
    monkeypatch.setattr(daemon.asyncio, "sleep", sleep)
    answer["ok"] = False
    run(ctl.dnd_watch())
    assert pressed == ["DNDOff", "DNDOff"] and not ctl.dnd_ours


def test_dnd_reported_off_while_the_key_is_being_pressed_ends_the_release_quietly(ctl, keys, monkeypatch):
    from datetime import datetime
    from claude_phone import screen

    async def press(key):  # the phone's Action URL reports DND off before the key's reply arrives
        ctl.set_phone_dnd(False)
        return False
    monkeypatch.setattr(screen, "press", press)
    ctl.set_dnd_ours(datetime.now().astimezone())
    run(ctl.release_dnd(datetime.now().astimezone()))
    assert not ctl.dnd_ours


def test_a_lan_cidr_written_with_the_pcs_own_address_still_admits_the_phone(ctl):
    from aiohttp import web
    ctl.s.lan_cidr = "192.168.1.50/24"  # host bits set: a strict parse refused every request
    c = daemon.Controller(ctl.s, esl=None)

    class Request:
        remote, match_info = "192.168.1.7", {"state": "on"}
    run(daemon_http.http_dnd(c, Request()))
    assert c.phone_dnd
    Request.remote = "10.0.0.9"
    with pytest.raises(web.HTTPForbidden):
        run(daemon_http.http_dnd(c, Request()))


# --- FreeSWITCH's events ---

def test_a_phone_coming_back_online_gets_its_message_light_again(ctl, monkeypatch):
    lights = []

    async def update_mwi():
        lights.append(1)
    monkeypatch.setattr(ctl, "update_mwi", update_mwi)

    async def scenario():
        for sub in ("sofia::register", "sofia::register", "sofia::unregister", "sofia::register"):
            ctl.on_event({"Event-Name": "CUSTOM", "Event-Subclass": sub, "from-user": "200"})
        await asyncio.sleep(0)
    run(scenario())
    assert lights == [1, 1]  # not for the re-registration every few minutes
    assert ctl.registered == {"200": True}


def test_only_inbound_parked_calls_are_answered_and_each_once(ctl, monkeypatch):
    answered = []

    async def inbound(uid, exten, who):
        answered.append((uid, exten))
    monkeypatch.setattr(ctl, "inbound", inbound)

    async def scenario():
        park = {"Event-Name": "CHANNEL_PARK", "Unique-ID": "u1", "Call-Direction": "inbound",
                "Caller-Destination-Number": "301"}
        ctl.on_event(park)
        ctl.on_event(park)  # FreeSWITCH can park the same call twice
        ctl.on_event({**park, "Unique-ID": "u2", "Call-Direction": "outbound"})  # Claude's call: ring() has it
        await asyncio.sleep(0)
    run(scenario())
    assert answered == [("u1", "301")]


class FakeESL:
    """The event socket, connecting at once and then idle until cancelled."""
    def __init__(self):
        self.handlers, self.on_connect, self.connected = [], [], asyncio.Event()

    def on_event(self, handler):
        self.handlers.append(handler)

    async def run(self):
        self.connected.set()
        await asyncio.Event().wait()


@pytest.mark.parametrize("alerts, dnd_ours, watch", [(True, False, "meeting_watch"), (False, True, "dnd_watch"),
                                                      (False, False, None)])
def test_start_listens_prerenders_and_starts_the_watch_it_needs(monkeypatch, alerts, dnd_ours, watch):
    began = []
    monkeypatch.setattr(tts, "prerender", lambda: began.append("prerender"))

    async def scenario():
        esl = FakeESL()
        c = daemon.Controller(Settings(meeting_alerts=alerts, lan_cidr="192.168.1.0/24"), esl=esl)
        c.dnd_ours = daemon.datetime.now().astimezone() if dnd_ours else None  # our DND from before a restart
        for name in ("sweeper", "meeting_watch", "dnd_watch"):
            async def mark(name=name):
                began.append(name)
            monkeypatch.setattr(c, name, mark)
        await c.start()
        await asyncio.sleep(0)
        assert esl.handlers == [c.on_event] and esl.on_connect == [c.update_mwi]
    run(scenario())
    assert began == ["prerender", "sweeper", *([watch] if watch else [])]


def test_a_background_task_that_fails_is_logged_and_let_go(ctl, caplog):
    async def boom():
        raise RuntimeError("the screen is unplugged")

    async def scenario():
        failed, stopped = ctl.spawn(boom(), "screen push"), ctl.spawn(asyncio.sleep(60), "sweeper")
        assert ctl._tasks == {failed, stopped}  # held: the loop keeps only a weak reference
        stopped.cancel()
        await asyncio.gather(failed, stopped, return_exceptions=True)
        await asyncio.sleep(0)
    run(scenario())
    assert ctl._tasks == set()
    assert "screen push failed" in caplog.text and "the screen is unplugged" in caplog.text
    assert "sweeper" not in caplog.text  # cancelling isn't failing


@pytest.mark.parametrize("windows_answer", [True, False])
def test_the_sweep_drops_sessions_whose_terminal_closed_but_not_when_a_window_doesnt_answer(
        ctl, monkeypatch, windows_answer):
    registry.register("gone", "repo", "main", None, "/r", "vscode:1")
    registry.register("live", "repo", "dev", None, "/r", "vscode:2")

    def live_term_ids():
        if not windows_answer:
            raise OSError("a VS Code window is busy")
        return {"vscode:2"}
    monkeypatch.setattr(inject, "live_term_ids", live_term_ids)
    monkeypatch.setattr(daemon.config, "ip_warning", lambda: None)
    monkeypatch.setattr(daemon.housekeeping, "run", lambda: {})

    async def nothing():
        pass
    monkeypatch.setattr(ctl, "warm_greetings", nothing)

    class OneRound(Exception):
        pass

    async def sleep(seconds):
        raise OneRound
    monkeypatch.setattr(daemon.asyncio, "sleep", sleep)
    with pytest.raises(OneRound):
        run(ctl.sweeper())
    left = {s.session_id for s in registry.all_sessions()}
    assert left == ({"live"} if windows_answer else {"gone", "live"})


# --- what the phone shows ---

def test_caller_id_fits_17():
    assert caller_id("claude-phone/master", 17) == "claude-phone/mas~"
    assert caller_id("lemon-stand/feat/ags-2", 17) == "lemon-stand/ags-2"
    assert caller_id("site-be-v2", 17) == "site-be-v2"
    for label in ("a-very-long-repository-name/feature/branch", "x" * 40):
        assert len(caller_id(label, 17)) <= 17


def test_spoken_label():
    assert tts.spoken_label("lemon-stand/feat/ags-2") == "lemon stand, feat ags 2"
    assert tts.spoken_label("site-be-v2") == "site be v2"


def test_the_phonebook_and_directory_list_sessions_and_escape_them():
    s = Session(session_id="x", ext=301, repo="a&b", branch="main", worktree=None, cwd=None, term_id=None,
                status="idle", started_at=0, updated_at=0)
    xml = browser_xml([s], Settings())
    assert "<Prompt>301 a&amp;b/main</Prompt><URI>301</URI>" in xml
    assert "<Title>Claude (1 live)</Title>" in xml
    book = phonebook_xml([s], Settings())
    assert "<Name>a&amp;b/main</Name><Telephone>301</Telephone>" in book
    assert "<Name>Voicemail</Name><Telephone>500</Telephone>" in book


def test_call_summary_on_screen_prefers_the_sessions_summary(ctl, monkeypatch):
    from claude_phone import screen
    shown = []

    async def show(title, text, timeout=120):
        shown.append((title, text))
    monkeypatch.setattr(screen, "show", show)
    ctl.s.screen_push = True
    body = {"_label": "repo/main", "screen": "Merge the payments PR?"}
    run(ctl.show_on_screen(body, "Shall I merge the payments pull request now?", {"status": "voicemail"}))
    run(ctl.show_on_screen({"_label": "repo/main"}, "x" * 100, {"status": "delivered"}))
    assert shown == [("repo/main", "Merge the payments PR?\n(voicemail waiting)"), ("repo/main", "x" * 77 + "...")]


def test_the_desk_phones_address_is_looked_up_off_the_event_loop(monkeypatch):
    # It can need a FreeSWITCH query, and the keychain read is slow too: neither may stall a call's audio.
    import threading
    from claude_phone import screen
    threads = []
    monkeypatch.setattr(screen, "desk_ip", lambda s: threads.append(threading.current_thread()))
    monkeypatch.setattr(screen.config, "load", lambda: Settings(screen_push=True))
    monkeypatch.setenv("CLAUDE_PHONE_DESK_ADMIN", "pw")  # not the real keychain
    run(screen.show("Title", "text"))
    run(screen.press("DNDOn"))
    assert len(threads) == 2 and threading.main_thread() not in threads


def test_who_is_calling_is_worked_out_off_the_event_loop(ctl, monkeypatch):
    import threading  # it runs git, and may register the session
    threads, real_caller = [], ctl.caller
    monkeypatch.setattr(ctl, "caller", lambda body: threads.append(threading.current_thread()) or real_caller(body))
    answering(ctl, monkeypatch, None, "not answered")
    run(ctl.notify_owner({"message": "Hi", "session_id": "s"}))
    run(ctl.call_owner({"question": "Ship it", "session_id": "s"}))
    run(ctl.transfer({"to": "nobody", "session_id": "s"}))
    assert len(threads) == 3 and threading.main_thread() not in threads


def test_the_screen_text_is_escaped():
    from claude_phone import screen
    xml = screen.text_screen("a<b", "x & y", 60)
    assert "<Title>a&lt;b</Title><Text>x &amp; y</Text>" in xml and 'Timeout="60"' in xml
