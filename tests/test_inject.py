import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import psutil
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


# Before the fixture below hides the PC's own shells and processes:
REAL_LIVE_CONSOLE_IDS = inject._live_console_ids
REAL_SESSION_SHELL = inject._session_shell


@pytest.fixture(autouse=True)
def no_real_consoles(monkeypatch):
    """The PC running the tests has shells of its own: none of them is a session's terminal here."""
    monkeypatch.setattr(inject, "_live_console_ids", lambda: set())
    monkeypatch.setattr(inject, "_session_shell", lambda: None)


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


def test_terminal_id_is_none_with_no_vscode_terminal_and_no_shell_above(bridge, monkeypatch):
    monkeypatch.setattr(inject, "_ancestor_pids", lambda: [100, 1])
    assert inject.terminal_id() is None


# --- consoles: Visual Studio's Terminal, Windows Terminal, PowerShell, cmd ---

class FakeProc:
    """A process: psutil.Process's name(), cmdline(), parents() and children()."""

    def __init__(self, name, parents=(), cmdline=(), children=()):
        self._name, self._parents, self._cmd, self._children = name, list(parents), list(cmdline), list(children)

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmd or [self._name]

    def exe(self):
        return rf"C:\Users\x\.local\bin\{self._name}"

    def parents(self):
        return self._parents

    def children(self, recursive=False):
        return self._children


@pytest.fixture
def shell(monkeypatch):
    """A running process standing in as a shell (python.exe counts as one here)."""
    monkeypatch.setitem(inject.SHELLS, "python.exe", "Python")
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    proc = psutil.Process(p.pid)
    yield proc
    p.kill()
    p.wait()


@pytest.fixture
def typed(monkeypatch):
    """Replaces the consoleinject helper process: records how it was run and answers like it."""
    calls = []
    reply = {"stdout": "sent\r\n", "returncode": 0}

    def run_helper(pid, created_ms, text):
        calls.append((pid, created_ms, text))
        return reply["stdout"], "", reply["returncode"]
    monkeypatch.setattr(inject, "_run_helper", run_helper)
    monkeypatch.setattr(inject, "_runs_claude", lambda sh: True)
    return type("Typed", (), {"calls": calls, "reply": reply})


def test_a_console_session_is_typed_into_by_the_helper_with_the_text_on_stdin(shell, typed):
    term = inject._console_id(shell)
    assert term == f"con:{shell.pid}:{int(shell.create_time() * 1000)}"
    assert inject.type_into(_session(term), "ship\n  it", "test") == "sent"
    assert typed.calls == [(shell.pid, int(shell.create_time() * 1000), "ship it")]


class FakeHelper:
    """subprocess.Popen for the helper: records its command line and creation flags."""
    made: ClassVar[list] = []

    def __init__(self, args, **kw):
        self.args, self.kw, self.pid, self.returncode = args, kw, 4321, 0
        self.input = None
        FakeHelper.made.append(self)

    def communicate(self, input=None, timeout=None):
        if input is not None and self.hangs:
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.input = input
        return b"sent\r\n", b""

    hangs = False


def test_the_helper_runs_isolated_with_the_text_on_stdin_and_never_on_the_command_line(monkeypatch):
    FakeHelper.made, FakeHelper.hangs = [], False
    monkeypatch.setattr(inject.subprocess, "Popen", FakeHelper)
    assert inject._run_helper(77, 1234, "ship it") == ("sent\r\n", "", 0)
    [h] = FakeHelper.made
    assert h.args == [sys.executable, "-I", "-m", "claude_phone.consoleinject", "77", "1234"]
    assert h.input == b"ship it" and "ship" not in " ".join(h.args)  # other processes can read command lines
    assert h.kw["creationflags"] & subprocess.CREATE_NO_WINDOW


def test_a_helper_that_hangs_is_killed_with_everything_it_started(monkeypatch):
    killed = []

    class Proc:
        def __init__(self, pid, children=()):
            self.pid, self._children = pid, children

        def children(self, recursive=False):
            return self._children

        def kill(self):
            killed.append(self.pid)

    FakeHelper.made, FakeHelper.hangs = [], True
    monkeypatch.setattr(inject.subprocess, "Popen", FakeHelper)
    monkeypatch.setattr(inject.psutil, "Process", lambda pid: Proc(pid, [Proc(8765)]))  # the launcher's real python
    assert inject._run_helper(77, 1234, "ship it") is None
    assert killed == [8765, 4321]


def test_a_recycled_pid_is_never_typed_into(shell, typed):
    stale = f"con:{shell.pid}:{int(shell.create_time() * 1000) - 5}"
    assert inject.type_into(_session(stale), "hello", "test") == "notfound"
    assert inject.type_into(_session("con:99999999:1"), "hello", "test") == "notfound"
    assert typed.calls == []


def test_a_shell_without_claude_under_it_is_not_typed_into(shell, typed, monkeypatch):
    monkeypatch.setattr(inject, "_runs_claude", lambda sh: False)  # claude was killed, its SessionEnd never ran
    result = inject.type_into(_session(inject._console_id(shell)), "rm -rf things", "test")
    assert result.startswith("error:") and "claude" in result
    assert typed.calls == []


def test_a_process_that_is_not_a_shell_is_not_typed_into(shell, typed, monkeypatch):
    term = inject._console_id(shell)
    monkeypatch.delitem(inject.SHELLS, "python.exe")
    assert inject.type_into(_session(term), "hello", "test") == "notfound"
    assert typed.calls == []


@pytest.mark.parametrize("stdout, returncode, expected", [
    ("notfound\r\n", 0, "notfound"),
    ("error:couldn't attach to the console (Windows error 5): that console is elevated\r\n", 0,
     "error:couldn't attach to the console (Windows error 5): that console is elevated"),
    ("", 1, "error:the console helper failed (exit 1): "),
])
def test_the_helpers_answer_is_passed_on_and_a_dead_helper_is_an_error(shell, typed, stdout, returncode, expected):
    typed.reply.update(stdout=stdout, returncode=returncode)
    assert inject.type_into(_session(inject._console_id(shell)), "hello", "test") == expected


def test_a_helper_that_hangs_is_an_error_not_a_stuck_call(shell, typed, monkeypatch):
    monkeypatch.setattr(inject, "_run_helper", lambda *a: None)
    assert inject.type_into(_session(inject._console_id(shell)), "hello", "test") == (
        "error:typing into the console timed out")


def test_the_check_that_claude_is_running_is_made_inside_the_lock_not_before_waiting_for_it(shell, typed, monkeypatch):
    seen = []
    monkeypatch.setattr(inject, "_runs_claude", lambda sh: seen.append(inject._console_lock.locked()) or True)
    inject.type_into(_session(inject._console_id(shell)), "hello", "test")
    assert seen == [True]


def test_every_attempt_at_a_console_is_logged_in_the_registry(shell, typed):
    sess = _session(inject._console_id(shell))
    inject.type_into(sess, "hello", "dial-in")
    with registry.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT source, text, result, term_id FROM injections")] == [
            ("dial-in", "hello", "sent", sess.term_id)]


def test_the_sweep_keeps_every_live_console_and_vscode_terminal(bridge, monkeypatch):
    monkeypatch.setattr(inject, "_live_console_ids", lambda: {"con:5:99"})
    assert inject.live_term_ids() == {f"vscode:{SHELL_PID}", "con:5:99"}


def test_a_window_that_does_not_answer_still_skips_the_sweep_when_consoles_are_live(monkeypatch):
    monkeypatch.setattr(inject, "_live_console_ids", lambda: {"con:5:99"})
    _write_bridge("hung-window", _free_port())
    with pytest.raises(OSError):
        inject.live_term_ids()


def test_the_live_consoles_are_the_running_shells_with_their_creation_times(shell):
    assert inject._console_id(shell) in REAL_LIVE_CONSOLE_IDS()


def test_terminal_id_is_the_nearest_shell_when_no_vscode_window_knows_it(bridge, shell, monkeypatch):
    monkeypatch.setattr(inject, "_ancestor_pids", lambda: [100, shell.pid, 1])
    monkeypatch.setattr(inject, "_session_shell", lambda: shell)
    assert inject.terminal_id() == inject._console_id(shell)


def test_a_vscode_terminal_a_bridge_knows_stays_a_vscode_id(bridge, shell, monkeypatch):
    monkeypatch.setattr(inject, "_ancestor_pids", lambda: [SHELL_PID, shell.pid])
    monkeypatch.setattr(inject, "_session_shell", lambda: shell)
    assert inject.terminal_id() == f"vscode:{SHELL_PID}"


@pytest.mark.parametrize("in_vscode, expected_console_id", [(True, False), (False, True)])
def test_a_window_that_does_not_answer_only_holds_back_sessions_that_could_be_its_own(
        shell, monkeypatch, in_vscode, expected_console_id):
    # A VS Code terminal listed late would get a console id today and a vscode id tomorrow: a new extension each time.
    _write_bridge("hung-window", _free_port())
    monkeypatch.setattr(inject, "_session_shell", lambda: shell)
    monkeypatch.setattr(inject, "_in_vscode", lambda sh: in_vscode)
    assert inject.terminal_id() == (inject._console_id(shell) if expected_console_id else None)


# --- which shell is the session's ---

def _chain(monkeypatch, *names):
    """The calling process's parents, nearest first: names are image names, "claude -p" is claude in print mode."""
    procs = [FakeProc("claude.exe", cmdline=["claude.exe", "-p", "hi"]) if n == "claude -p" else FakeProc(n)
             for n in names]
    monkeypatch.setattr(inject.psutil, "Process", lambda *a: FakeProc("phone.exe", parents=procs))
    monkeypatch.setattr(inject, "_session_shell", REAL_SESSION_SHELL)
    return procs


def test_the_session_shell_is_the_one_above_claude(monkeypatch):
    procs = _chain(monkeypatch, "claude.exe", "powershell.exe", "WindowsTerminal.exe")
    assert inject._session_shell() is procs[1]


def test_a_shell_claude_started_for_its_hooks_or_bash_tool_is_not_the_session_shell(monkeypatch):
    procs = _chain(monkeypatch, "cmd.exe", "claude.exe", "pwsh.exe", "devenv.exe")  # a `cmd /c phone hook ...`
    assert inject._session_shell() is procs[2]


def test_a_claude_started_by_another_claude_has_no_console_of_its_own(monkeypatch):
    # Its parent shell is a Bash tool's; the shell above the outer claude is the outer session's, not its own.
    _chain(monkeypatch, "claude.exe", "cmd.exe", "claude.exe", "powershell.exe")
    assert inject._session_shell() is None


def test_a_claude_run_through_a_node_shim_still_finds_its_shell(monkeypatch):
    shim = FakeProc("node.exe", cmdline=["node", r"C:\npm\node_modules\@anthropic-ai\claude-code\cli.js"])
    procs = _chain(monkeypatch, "claude.exe", "powershell.exe")
    procs.insert(1, shim)  # claude.exe <- node (shim) <- powershell
    assert inject._session_shell() is procs[2]


def test_a_headless_claude_is_never_a_session(monkeypatch):
    _chain(monkeypatch, "claude -p", "powershell.exe")
    assert inject._session_shell() is None


def test_with_no_claude_above_the_nearest_shell_is_used(monkeypatch):
    procs = _chain(monkeypatch, "python.exe", "cmd.exe", "explorer.exe")
    assert inject._session_shell() is procs[1]


def test_bash_is_not_a_shell_to_type_into(monkeypatch):
    _chain(monkeypatch, "claude.exe", "bash.exe", "mintty.exe")
    assert inject._session_shell() is None


def test_only_an_interactive_claude_under_a_shell_counts(monkeypatch):
    interactive = FakeProc("claude.exe")
    headless = FakeProc("claude.exe", cmdline=["claude.exe", "--print", "x"])
    tests = FakeProc("node.exe", cmdline=["node", r"C:\src\claude-phone-windows\run-tests.js"])
    assert inject._runs_claude(FakeProc("pwsh.exe", children=[tests, interactive]))
    assert not inject._runs_claude(FakeProc("pwsh.exe", children=[headless]))  # its Enter would run in the shell
    assert not inject._runs_claude(FakeProc("pwsh.exe", children=[tests]))  # claude was killed, npm test took over


def test_an_invented_console_id_is_not_accepted_but_a_real_one_with_claude_is(shell, monkeypatch):
    monkeypatch.setattr(inject, "_runs_claude", lambda sh: True)
    assert inject.accepts(inject._console_id(shell))
    assert not inject.accepts("con:99999999:1")
    assert not inject.accepts(f"con:{shell.pid}:1")  # right pid, wrong creation time
    assert not inject.accepts("con:not-a-pid")
    assert not inject.accepts("con:-5:1") and not inject.accepts(f"con:{10 ** 30}:1")  # not pids at all
    assert inject.accepts("tab")  # no scheme: there is nothing to type into, so nothing to guard
    assert not inject.accepts(None)
    monkeypatch.setattr(inject, "_runs_claude", lambda sh: False)
    assert not inject.accepts(inject._console_id(shell))


def test_claude_is_recognised_by_its_exe_or_node_running_it(monkeypatch):
    class P(FakeProc):
        def __init__(self, name, cmdline=(), exe=None):
            super().__init__(name, cmdline=cmdline)
            self._exe = exe or rf"C:\Users\x\.local\bin\{name}"

        def exe(self):
            return self._exe

    assert inject._is_claude(P("claude.exe"))
    assert inject._is_claude(P("node.exe", ["node", r"C:\npm\@anthropic-ai\claude-code\cli.js"]))
    assert not inject._is_claude(P("node.exe", ["node", "server.js"]))
    assert not inject._is_claude(P("node.exe", ["node", r"C:\src\claude-phone-windows\run-tests.js"]))
    assert not inject._is_claude(P("powershell.exe"))
    # The Claude desktop app is a claude.exe too.
    assert not inject._is_claude(P("claude.exe", exe=r"C:\Users\x\AppData\Local\AnthropicClaude\app-1\claude.exe"))
    assert not inject._is_claude(P("claude.exe", exe=r"C:\Program Files\WindowsApps\Claude_1\app\claude.exe"))


def test_a_vscode_id_is_accepted_only_for_a_terminal_with_claude_in_it(shell, monkeypatch):
    monkeypatch.setattr(inject, "_runs_claude", lambda sh: sh.pid == shell.pid)
    assert inject.accepts(f"vscode:{shell.pid}")
    assert not inject.accepts("vscode:99999999")  # nothing runs there
    monkeypatch.setattr(inject, "_runs_claude", lambda sh: False)
    assert not inject.accepts(f"vscode:{shell.pid}")  # a bare terminal: its Enter would run the words
    assert not inject.accepts(["con:1:2"]) and not inject.accepts(42)


@pytest.mark.parametrize("term_id, chain, host", [
    ("con:10:1", [("powershell.exe",), ("DevHub.exe",), ("devenv.exe",), ("explorer.exe",)], "Visual Studio"),
    ("con:10:1", [("pwsh.exe",), ("WindowsTerminal.exe",), ("explorer.exe",)], "Windows Terminal"),
    ("con:10:1", [("powershell.exe",), ("explorer.exe",)], "PowerShell"),
    ("con:10:1", [("cmd.exe",)], "Command Prompt"),
    ("con:10:1", [("powershell.exe",), ("Code.exe",), ("Code.exe",)], "VS Code"),  # a VS Code without the extension
    ("vscode:10", [("powershell.exe",), ("Cursor.exe",)], "Cursor"),
    ("vscode:10", [("powershell.exe",), ("something-new.exe",)], "VS Code"),
])
def test_the_host_is_the_nearest_known_app_above_the_shell_else_the_shell_itself(monkeypatch, term_id, chain, host):
    procs = [FakeProc(name) for (name,) in chain]
    shell = FakeProc(chain[0][0], procs[1:])
    monkeypatch.setattr(inject.psutil, "Process", lambda pid: shell)
    assert inject.host_of(term_id) == host


def test_the_host_of_a_terminal_that_is_gone_or_not_a_terminal_id_is_unknown(monkeypatch):
    def gone(pid):
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(inject.psutil, "Process", gone)
    assert inject.host_of("con:10:1") is None
    assert inject.host_of("vscode:10") == "VS Code"
    assert inject.host_of("tab") is None and inject.host_of(None) is None


def test_the_daemon_will_not_register_a_caller_with_an_invented_console_id(controller, tmp_path):
    body = {"session_id": "forged", "term_id": "con:99999999:1", "cwd": str(tmp_path)}
    assert controller.caller(body)["ext"] is None
    assert registry.all_sessions() == []


def test_the_daemon_registers_a_caller_in_a_real_console_with_its_host(controller, shell, tmp_path, monkeypatch):
    monkeypatch.setattr(inject, "_runs_claude", lambda sh: True)
    monkeypatch.setattr(inject, "host_of", lambda term: "PowerShell")
    controller.caller({"session_id": "real", "term_id": inject._console_id(shell), "cwd": str(tmp_path)})
    assert [(s.session_id, s.host) for s in registry.all_sessions()] == [("real", "PowerShell")]
