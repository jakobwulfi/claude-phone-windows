"""Missed-call pushes through ntfy, against a local HTTP server standing in for ntfy.sh."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_phone import config, keychain, push
from claude_phone.config import Settings


@pytest.fixture
def ntfy(monkeypatch):
    """A server that records each push as (path, headers, body) and answers with ntfy.status."""
    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((self.path, dict(self.headers), body.decode()))
            self.send_response(server.status)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.status, server.got = 200, got
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = Settings(ntfy_server=f"http://127.0.0.1:{server.server_port}/")  # the slash is dropped
    monkeypatch.setattr(config, "load", lambda: settings)
    monkeypatch.setattr(keychain, "get", lambda name: "cphone-topic" if name == "ntfy" else None)
    yield server
    server.shutdown()
    server.server_close()


def test_a_push_goes_to_the_topic_with_its_title_and_priority(ntfy):
    assert push.send("Call from Søren's lemon-stand/main", "Merge it?")
    (path, headers, body), = ntfy.got
    assert path == "/cphone-topic" and body == "Merge it?"
    assert headers["Title"].encode("latin-1").decode("utf-8") == "Call from Søren's lemon-stand/main"
    assert (headers["Priority"], headers["Tags"]) == ("high", "telephone_receiver")


def test_a_refused_push_says_so(ntfy):
    ntfy.status = 403
    assert not push.send("Call", "x")


def test_no_topic_no_push(ntfy, monkeypatch):
    monkeypatch.setattr(keychain, "get", lambda name: None)
    assert not push.send("Call", "x") and ntfy.got == []


@pytest.mark.parametrize("push_text, body", [(True, "Merge it?\n(no answer)"), (False, "(no answer)")])
def test_a_missed_call_push_holds_the_question_only_if_asked_to(monkeypatch, push_text, body):
    sent = []
    monkeypatch.setattr(config, "load", lambda: Settings(push_text=push_text))
    monkeypatch.setattr(push, "send", lambda title, message: sent.append((title, message)))

    async def miss():
        push.missed("Call", "lemon-stand/main", "Merge it?", "no answer")
        await asyncio.sleep(0.1)  # sent from a worker thread
    asyncio.run(miss())
    assert sent == [("Call from lemon-stand/main", body)]


def test_the_topic_is_made_once_and_kept(monkeypatch):
    stored = {}
    monkeypatch.setattr(keychain, "get", stored.get)
    monkeypatch.setattr(keychain, "put", stored.__setitem__)
    first = push.create_topic()
    assert first.startswith("cphone-") and len(first) > 20 and stored == {"ntfy": first}
    assert push.create_topic() == first
