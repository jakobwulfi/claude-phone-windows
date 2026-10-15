"""The daemon's localhost API: the session list for VS Code, and the guard that keeps web pages out."""

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from claude_phone import daemon, registry


class FakeESL:
    def __init__(self):
        self.connected = asyncio.Event()
        self.connected.set()

    async def api(self, cmd):
        return "sofia/internal/sip:201@192.168.0.30:5060"


def controller() -> daemon.Controller:
    c = daemon.Controller.__new__(daemon.Controller)
    c.s = daemon.config.load()
    c.esl = FakeESL()
    c.line = asyncio.Lock()
    return c


async def get(path, headers=None):
    async with TestClient(TestServer(daemon.local_app(controller()))) as client:
        r = await client.get(path, headers=headers or {})
        return r.status, (await r.json() if r.content_type == "application/json" else await r.text())


def test_session_list_has_every_live_session_and_the_health():
    registry.register("a", "site-be-v2", "main", None, "/r", "vscode:100")
    registry.register("b", "site-fe", "feature/x", "x-tree", "/f", "vscode:200")
    registry.set_status("b", "working")

    status, body = asyncio.run(get("/sessions"))

    assert status == 200
    assert [(s["ext"], s["label"], s["status"], s["term_id"]) for s in body["sessions"]] == [
        (301, "site-be-v2/main", "idle", "vscode:100"),
        (302, "site-fe/x-tree", "working", "vscode:200"),
    ]
    assert body["health"]["freeswitch"] is True
    assert body["health"]["desk_online"] is True
    assert body["health"]["ring_ext"] == daemon.config.load().ring_ext


def test_session_list_is_empty_without_sessions():
    status, body = asyncio.run(get("/sessions"))
    assert status == 200 and body["sessions"] == []


def test_browser_requests_are_refused():
    status, _ = asyncio.run(get("/sessions", {"Origin": "https://evil.example"}))
    assert status == 403


def test_rebound_host_names_are_refused():
    status, _ = asyncio.run(get("/health", {"Host": "evil.example:8765"}))
    assert status == 403


def test_session_list_shows_a_session_interrupted_with_esc_as_idle():
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
