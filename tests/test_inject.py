import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_phone import config, inject, registry

TOKEN = "a" * 64
SHELL_PID = 4242


class FakeBridge:
    """Stands in for one VS Code window's extension server."""

    def __init__(self, terminals: set[int]):
        self.terminals = terminals
        self.typed: list[tuple[int, str]] = []
        self.auth: list[str | None] = []
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self) -> bool:
                bridge.auth.append(self.headers.get("Authorization"))
                return self.headers.get("Authorization") == f"Bearer {TOKEN}"

            def do_GET(self):
                if not self._authorized():
                    return self._reply(401, {"error": "bad token"})
                self._reply(200, {"terminals": [{"pid": p, "name": "powershell"} for p in sorted(bridge.terminals)]})

            def do_POST(self):
                if not self._authorized():
                    return self._reply(401, {"error": "bad token"})
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if body["pid"] not in bridge.terminals:
                    return self._reply(200, {"result": "notfound"})
                bridge.typed.append((body["pid"], body["text"]))
                self._reply(200, {"result": "sent"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _write_bridge(name: str, port: int, pid: int | None = None, token: str = TOKEN):
    config.BRIDGES.mkdir(parents=True, exist_ok=True)
    f = config.BRIDGES / f"{name}.json"
    f.write_text(json.dumps({"pid": pid or os.getpid(), "port": port, "token": token}))
    return f


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def no_bridges():
    yield
    for f in config.BRIDGES.glob("*.json"):
        f.unlink()


@pytest.fixture
def bridge():
    b = FakeBridge({SHELL_PID})
    _write_bridge("window", b.port)
    yield b
    b.close()


def _session(term_id):
    return registry.register("s", "repo", "main", None, "/r", term_id)


def test_types_flattened_text_into_the_sessions_terminal(bridge):
    result = inject.type_into(_session(f"vscode:{SHELL_PID}"), "ship\n  it", "test")
    assert result == "sent"
    assert bridge.typed == [(SHELL_PID, "ship it")]
    assert bridge.auth == [f"Bearer {TOKEN}"]


def test_a_transcript_never_types_a_command_or_shell_line(bridge):
    # Claude Code runs "/..." as a slash command and "!..." in the shell: a heard "slash compact" stays words.
    sess = _session(f"vscode:{SHELL_PID}")
    assert inject.type_into(sess, "/compact now", "test") == "sent"
    assert inject.type_into(sess, " ! # dir", "test") == "sent"
    assert inject.type_into(sess, "/ !", "test") == "error:empty"
    assert bridge.typed == [(SHELL_PID, "compact now"), (SHELL_PID, "dir")]


def test_notfound_when_no_window_has_the_terminal(bridge):
    assert inject.type_into(_session("vscode:999"), "hello", "test") == "notfound"
    assert bridge.typed == []


def test_rejected_token_is_reported_as_an_error():
    b = FakeBridge({SHELL_PID})
    _write_bridge("window", b.port, token="wrong")
    try:
        assert inject.type_into(_session(f"vscode:{SHELL_PID}"), "hello", "test").startswith("error:")
    finally:
        b.close()


def test_session_without_a_terminal_is_not_typed_into(bridge):
    assert inject.type_into(_session(None), "hello", "test") == "error:no terminal id"
    assert bridge.typed == []


def test_live_terminals_come_from_every_window():
    assert inject.live_term_ids() == set()  # no VS Code window open
    one, other = FakeBridge({SHELL_PID}), FakeBridge({5151})
    _write_bridge("window", one.port)
    _write_bridge("other-window", other.port)
    try:
        assert inject.live_term_ids() == {f"vscode:{SHELL_PID}", "vscode:5151"}
    finally:
        one.close()
        other.close()


def test_window_that_does_not_answer_raises_so_the_sweep_is_skipped():
    _write_bridge("hung-window", _free_port())
    with pytest.raises(OSError):
        inject.live_term_ids()


def test_bridge_file_of_a_closed_window_is_removed():
    p = subprocess.Popen([sys.executable, "-c", ""])
    p.wait()
    f = _write_bridge("crashed-window", _free_port(), pid=p.pid)
    assert inject.bridges() == []
    assert not f.exists()


def test_terminal_id_is_the_nearest_ancestor_that_is_a_terminal(bridge, monkeypatch):
    monkeypatch.setattr(inject, "_ancestor_pids", lambda: [100, SHELL_PID, 1])
    assert inject.terminal_id() == f"vscode:{SHELL_PID}"


def test_terminal_id_is_none_outside_a_vscode_terminal(bridge, monkeypatch):
    monkeypatch.setattr(inject, "_ancestor_pids", lambda: [100, 1])
    assert inject.terminal_id() is None
