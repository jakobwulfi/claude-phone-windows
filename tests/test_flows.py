"""Call flows against a scripted fake call: no FreeSWITCH, audio, whisper or VS Code."""

import asyncio

import pytest

from claude_phone import daemon, inject, push, registry, tts


class FakeCall:
    def __init__(self, digits=(), recordings=()):
        self.id = "c1"
        self.up = True
        self.played = []
        self.digits = list(digits)  # what each interruptible play returns
        self.recordings = list(recordings)
        self.limits = []  # max_seconds of each recording

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
def ctl(monkeypatch):
    """A Controller with speech, transcription, typing and the message light stubbed out."""
    c = daemon.Controller.__new__(daemon.Controller)
    c.s = daemon.config.load()
    c.line = asyncio.Lock()
    c.meeting, c.window, c.released, c.phone_dnd, c.dnd_ours = None, set(), set(), False, False
    c.greeting_retry, c.warming = {}, None

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


def test_dial_in_types_what_was_said(ctl):
    registry.register("s", "site-be-v2", "main", None, "/r", "tab")
    ctl.heard = ["What colour should the button be?"]
    call = FakeCall()
    assert run(ctl.dial_session(call, 301)) == "What colour should the button be?"
    assert ctl.typed == [(301, "What colour should the button be?")]
    assert "tts:Sent." in call.played


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

    async def online():
        return True

    async def ring(c, prefix):
        return answer, ""
    monkeypatch.setattr(ctl, "desk_online", online)
    monkeypatch.setattr(ctl, "ring", ring)
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


def test_transfer_prefix_reaches_target(ctl):
    registry.register("t", "site-be-v2", "main", None, "/r", "tab")
    ctl.heard = ["Testing."]
    run(ctl.dial_session(FakeCall(), 301, prefix="[Phone transfer from app-fe-v2] Sam says:"))
    assert ctl.typed == [(301, "[Phone transfer from app-fe-v2] Sam says: Testing.")]


def test_voicemail_answer_skip_delete(ctl):
    registry.register("s", "repo", "main", None, "/r", "tab")
    registry.add_voicemail("s", 301, "repo/main", "Merge it?", None, None)
    b = registry.add_voicemail("s", 301, "repo/main", "Which colour?", None, None)
    registry.add_voicemail("s", 301, "repo/main", "Delete me", None, None)
    ctl.heard = ["Yes, merge it."]
    call = FakeCall(digits=[None, "#", "7"])  # answer the first, skip the second, delete the third
    run(ctl.voicemail(call))
    assert ctl.typed == [(301, '[Phone answer to your earlier question] Yes, merge it. (Your question was: "Merge it?")')]
    assert [v.id for v in registry.pending_voicemails()] == [b]


def test_answer_for_closed_session_is_kept(ctl):
    vm = registry.add_voicemail("gone", None, "repo/main", "Ship?", None, None)
    ctl.heard = ["Yes."]
    call = FakeCall()
    run(ctl.voicemail(call))
    assert ctl.typed == [] and registry.pending_voicemails() == []
    assert "tts:" + tts.PHRASES["vm_session_gone"] in call.played


def test_unanswered_call_leaves_voicemail_and_pushes(ctl, monkeypatch):
    registry.register("s", "repo", "main", None, "/r", "tab")

    async def online():
        return True

    async def ring(c, prefix):
        return None, "not answered"
    monkeypatch.setattr(ctl, "desk_online", online)
    monkeypatch.setattr(ctl, "ring", ring)
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


def answering(ctl, monkeypatch, call):
    async def online():
        return True

    async def ring(c, prefix, auto_answer=False):
        return call, ""
    monkeypatch.setattr(ctl, "desk_online", online)
    monkeypatch.setattr(ctl, "ring", ring)


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


def test_the_meeting_whisper_speaks_with_claudes_voice_unless_set_otherwise(ctl, monkeypatch):
    voices = []

    async def render_async(text, repo=None, voice=None):
        voices.append(voice)
        return f"tts:{text}"
    monkeypatch.setattr(tts, "render_async", render_async)
    answering(ctl, monkeypatch, FakeCall())
    ctl.s.whisper_provider, ctl.s.whisper_voice = "", ""
    assert run(ctl.whisper("Meeting in two minutes.", "14:00 Board")) == "delivered"
    ctl.s.whisper_provider, ctl.s.whisper_voice = "sapi", "Microsoft Zira Desktop"
    run(ctl.whisper("Meeting in two minutes.", "14:00 Board"))
    assert voices == [None, ("sapi", "Microsoft Zira Desktop")]  # None: Claude's voice, whichever it is now
    assert daemon.config.Settings().whisper_provider == ""


def test_the_meeting_whisper_is_ready_before_a_meeting(ctl, monkeypatch):
    rendered = []
    monkeypatch.setattr(tts, "render", lambda text, repo=None, voice=None, fallback=True:
                        rendered.append((text, voice)))
    ctl.s.meeting_alerts, ctl.s.whisper_text, ctl.s.whisper_provider = True, "Psst. Meeting soon.", ""
    run(ctl.warm_greetings())
    assert rendered == [("Psst. Meeting soon.", None)]  # answered on the speaker at once: no ringing to hide it
    ctl.s.meeting_alerts, rendered[:] = False, []
    run(ctl.warm_greetings())
    assert rendered == []


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


def test_hanging_up_on_a_question_does_not_push(ctl):
    registry.register("s", "repo", "main", None, "/r", "tab")
    res = run(ctl.call_owner({"question": "Merge it", "session_id": "s", "_force_voicemail": "hung up before answering"}))
    assert res["status"] == "voicemail" and ctl.pushed == []


def test_voicemail_message_names_the_owner(ctl):
    ctl.s.owner_name = "Sam"
    registry.register("s", "repo", "main", None, "/r", "tab")
    res = run(ctl.call_owner({"question": "Merge it", "session_id": "s", "_force_voicemail": "line busy"}))
    assert res["message"].startswith("Sam didn't answer.")


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


def test_meeting_already_under_way_holds_calls_without_alert(ctl, monkeypatch):
    from claude_phone import diary
    now, m = meeting(-10)
    monkeypatch.setattr(diary, "window", lambda *a: [m])

    async def boom(*a, **k):
        raise AssertionError("no alert expected")
    monkeypatch.setattr(ctl, "whisper", boom)
    run(ctl.check_meetings(now, set()))
    assert ctl.meeting == m


def test_meeting_summary_trims_people():
    from claude_phone import diary
    _, m = meeting(2, people=("Ann Lee", "bob@x.com", "Cy Ng", "Di Fox"))
    assert diary.summary(m)[1].startswith("with Ann, bob +2\n")


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
