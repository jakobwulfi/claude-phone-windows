"""The daemon's localhost API: the session list for VS Code, and the guard that keeps web pages out."""

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_phone import daemon, daemon_http, registry


class FakeESL:
    def __init__(self):
        self.connected = asyncio.Event()
        self.connected.set()

    async def api(self, cmd):
        return "sofia/internal/sip:201@192.168.0.30:5060"


@pytest.fixture
def get(controller):
    controller.esl = FakeESL()

    async def get_(path, headers=None):
        async with TestClient(TestServer(daemon_http.local_app(controller))) as client:
            r = await client.get(path, headers=headers or {})
            return r.status, (await r.json() if r.content_type == "application/json" else await r.text())
    return get_


def test_session_list_has_every_live_session_and_the_health(get):
    registry.register("a", "site-be-v2", "main", None, "/r", "vscode:100", "VS Code")
    registry.register("b", "site-fe", "feature/x", "x-tree", "/f", "con:200:5", "Visual Studio")
    registry.set_status("b", "working")

    status, body = asyncio.run(get("/sessions"))

    assert status == 200
    assert [(s["ext"], s["label"], s["status"], s["term_id"], s["host"]) for s in body["sessions"]] == [
        (301, "site-be-v2/main", "idle", "vscode:100", "VS Code"),
        (302, "site-fe/x-tree", "working", "con:200:5", "Visual Studio"),
    ]
    assert body["health"]["freeswitch"] is True
    assert body["health"]["desk_online"] is True
    assert body["health"]["ring_ext"] == daemon.config.load().ring_ext


def test_session_list_is_empty_without_sessions(get):
    status, body = asyncio.run(get("/sessions"))
    assert status == 200 and body["sessions"] == []


def test_browser_requests_are_refused(get):
    status, _ = asyncio.run(get("/sessions", {"Origin": "https://evil.example"}))
    assert status == 403


def test_rebound_host_names_are_refused(get):
    status, _ = asyncio.run(get("/health", {"Host": "evil.example:8765"}))
    assert status == 403


def test_shutdown_hangs_up_every_call_but_leaves_our_dnd_for_the_next_daemon(controller):
    # Terminated instead, the daemon left calls open in FreeSWITCH with nobody on its side. The DND it switched on
    # for a meeting is in the registry: the next daemon switches it off once the meeting is over.
    sent, ended = [], []

    class ESL(FakeESL):
        async def api(self, cmd):
            sent.append(cmd)
            return "+OK"
    controller.esl = ESL()
    controller.dnd_ours = daemon.datetime.now().astimezone()

    async def scenario():
        async def dial_in():
            try:
                await asyncio.Event().wait()
            finally:
                ended.append("in-1")  # an inbound call hangs up in its finally
        controller.calls["in-1"] = asyncio.create_task(dial_in())
        controller.placed.add("out-1")  # a call the daemon placed, e.g. call_<owner> mid-question
        await asyncio.sleep(0)
        async with TestClient(TestServer(daemon_http.local_app(controller))) as client:
            r = await client.post("/shutdown")
            return r.status, await r.json()
    assert asyncio.run(scenario()) == (200, {"status": "stopping"})
    assert ended == ["in-1"] and sorted(sent) == ["uuid_kill in-1", "uuid_kill out-1"]
    assert controller.stopping.is_set() and controller.dnd_ours


def test_a_placed_call_is_forgotten_once_it_hangs_up(controller):
    controller.placed.add("out-1")
    controller.on_event({"Event-Name": "CHANNEL_HANGUP_COMPLETE", "Unique-ID": "out-1"})
    assert controller.placed == set()


@pytest.mark.parametrize("path, body", [
    ("/call", "not json"), ("/notify", "[1, 2]"), ("/transfer", "null"), ("/whisper", '{"text": 5}'),
])
def test_a_body_that_isnt_what_the_endpoint_needs_gets_a_400_saying_so(controller, path, body):
    async def post():
        async with TestClient(TestServer(daemon_http.local_app(controller))) as client:
            r = await client.post(path, data=body, headers={"Content-Type": "application/json"})
            return r.status, await r.json()
    status, reply = asyncio.run(post())
    assert status == 400 and reply["status"] == "error" and reply["message"]


def test_session_list_shows_a_session_interrupted_with_esc_as_idle(get):
    import json
    from claude_phone import transcript
    registry.register("c", "site-be-v2", "main", None, "/r", "vscode:100")
    registry.set_status("c", "working")  # UserPromptSubmit ran; Esc ran no Stop hook
    folder = transcript.projects_dir() / "c--r"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "c.jsonl").write_text(json.dumps(
        {"type": "user", "message": {"content": "[Request interrupted by user]"}}) + "\n", encoding="utf-8")

    status, body = asyncio.run(get("/sessions"))

    assert status == 200 and body["sessions"][0]["status"] == "idle"
    assert registry.find("c").status == "idle"
