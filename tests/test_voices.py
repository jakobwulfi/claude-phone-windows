"""`phone voice`: finding voices, switching with the user's yes to downloads, deleting, and the ElevenLabs key.
Nothing is downloaded or installed: the network, pip and Credential Manager are faked."""

import asyncio
import io
import json
import shutil
import sys
import tomllib

import pytest

from claude_phone import cli, config, keychain, mcp_server, procs, tts, voices, voiceworker

REAL_DOWNLOAD = voices._download  # the fixture fakes these; their own tests need the real ones
REAL_PRERENDER_ELSEWHERE = mcp_server._prerender_elsewhere
WINDOWS = ["Microsoft David Desktop\ten-US", "Microsoft Zira Desktop\ten-US", "Microsoft Mark\ten-US"]
CATALOGUE = {
    key: {"language": {"name_english": "English", "country_english": "United States"}, "quality": "medium",
          "aliases": [key.lower().replace("_", "-")],
          "files": {f"en/en_US/{key}.onnx": {"size_bytes": 60_000_000, "md5_digest": "x"},
                    f"en/en_US/{key}.onnx.json": {"size_bytes": 5_000, "md5_digest": "y"}}}
    for key in ("en_US-ryan-medium", "en_US-norman-medium", "en_US-amy-low")
}


@pytest.fixture(autouse=True)
def home(monkeypatch):
    for d in (config.ENGINES, config.VOICES):
        shutil.rmtree(d, ignore_errors=True)
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text("", encoding="utf-8")
    cat = config.VOICES / "piper" / "voices.json"
    cat.parent.mkdir(parents=True)
    cat.write_text(json.dumps(CATALOGUE), encoding="utf-8")
    monkeypatch.setattr(tts, "windows_voice_lines", lambda: WINDOWS)
    monkeypatch.setattr(tts, "prerender", lambda: 0)
    keys: dict[str, str] = {}
    monkeypatch.setattr(keychain, "elevenlabs_key", lambda: keys.get("elevenlabs"))
    monkeypatch.setattr(keychain, "put", lambda name, value: keys.__setitem__(name, value))
    monkeypatch.setattr(keychain, "delete", lambda name: keys.pop(name, None))
    monkeypatch.setattr(voices, "_pip_install", fake_pip_install)
    monkeypatch.setattr(voices, "_download", fake_download)
    monkeypatch.setattr(voiceworker, "stop_all", lambda engine: None)
    monkeypatch.setattr(mcp_server, "_prerender_elsewhere", lambda: None)
    for p in tts.PROVIDERS.values():  # every voice speaks, without PowerShell or a worker
        monkeypatch.setattr(p, "render", lambda text, voice, out: out.write_bytes(b"speech"))
    downloads.clear()
    yield keys
    config.CONFIG_FILE.unlink(missing_ok=True)


downloads: list[str] = []


def fake_pip_install(engine, say):
    downloads.append(f"engine:{engine}")
    d = voices.engine_dir(engine)
    d.mkdir(parents=True)
    (d / "package.py").write_bytes(b"x" * 1000)
    (d / voices.INSTALLED_MARK).write_text("ok")


def fake_download(url, dest, size, digest, algo, say):
    downloads.append(dest.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"x" * 100)


def saved_voice() -> str:
    return tomllib.loads(config.CONFIG_FILE.read_text(encoding="utf-8")).get("voice", "")


def eleven_account(monkeypatch, voices_list=(("Rachel", "abc123"), ("George", "def456"))):
    def fetch(url, headers=None):
        assert headers == {"xi-api-key": "sk-good"}
        return json.dumps({"voices": [{"name": n, "voice_id": i} for n, i in voices_list]}).encode()
    monkeypatch.setattr(voices, "_fetch", fetch)


# --- finding voices ---

def test_names_are_engine_colon_name_in_any_case():
    assert voices.resolve("windows:microsoft mark") == voices.Voice("windows", "Microsoft Mark")
    assert voices.resolve("sapi:Microsoft Zira Desktop") == voices.Voice("windows", "Microsoft Zira Desktop")
    assert voices.resolve("windows") == voices.DEFAULT
    assert voices.resolve("kokoro") == voices.Voice("kokoro", "af_heart")
    assert voices.resolve("Piper:en-us-ryan-medium") == voices.Voice("piper", "en_US-ryan-medium")
    assert voices.resolve("  kokoro:bm_george ") == voices.Voice("kokoro", "bm_george")


def test_a_bare_name_works_when_only_one_engine_has_it():
    assert voices.resolve("Microsoft Mark") == voices.Voice("windows", "Microsoft Mark")
    assert voices.resolve("af_bella") == voices.Voice("kokoro", "af_bella")
    with pytest.raises(ValueError, match="Name it as engine:name"):
        voices.resolve("nobody")


def test_a_typo_suggests_each_close_voice_once():
    with pytest.raises(ValueError) as e:
        voices.resolve("piper:en_US-rian-medium")
    assert str(e.value).count("piper:en_US-ryan-medium") == 1
    with pytest.raises(ValueError, match="no voice engine called 'espeak'"):
        voices.resolve("espeak:robot")


# --- switching ---

def test_a_voice_on_this_pc_is_switched_to_without_asking():
    asked = []
    msg = voices.choose("windows:Microsoft Mark", ask=lambda q: asked.append(q) or False)
    assert msg.startswith("Claude now speaks with the Windows voice Microsoft Mark")
    assert saved_voice() == "windows:Microsoft Mark" and asked == [] and downloads == []
    assert tts.claude_voice() == ("windows", "Microsoft Mark")


def test_a_download_needs_a_yes_and_says_how_big_it_is():
    with pytest.raises(voices.NeedsDownload) as e:
        voices.choose("piper:en_US-ryan-medium")  # no one to ask (Claude ran it)
    assert e.value.size == voices.ENGINE_BYTES["piper"] + 60_005_000
    assert str(e.value) == "The Piper voice en_US-ryan-medium needs a one-off download of about 212 MB"
    asked = []
    with pytest.raises(voices.NeedsDownload):
        voices.choose("piper:en_US-ryan-medium", ask=lambda q: asked.append(q) or False)
    assert asked == ["The Piper voice en_US-ryan-medium needs a one-off download of about 212 MB. Download it?"]
    assert downloads == [] and saved_voice() == ""


def test_yes_downloads_then_switches_and_the_next_voice_reuses_the_engine():
    voices.choose("piper:en_US-ryan-medium", ask=lambda q: True)
    assert downloads == ["engine:piper", "en_US-ryan-medium.onnx", "en_US-ryan-medium.onnx.json"]
    assert saved_voice() == "piper:en_US-ryan-medium"
    assert tts.voice_for(None) == ("piper", "en_US-ryan-medium")
    downloads.clear()
    assert voices.download_size(voices.Voice("piper", "en_US-amy-low")) == 60_005_000
    voices.choose("piper:en_US-amy-low", download=True)
    assert downloads == ["en_US-amy-low.onnx", "en_US-amy-low.onnx.json"]
    downloads.clear()
    voices.choose("piper:en_US-ryan-medium")  # already here: no question, no download
    assert downloads == [] and saved_voice() == "piper:en_US-ryan-medium"


def test_a_voice_that_doesnt_speak_is_not_switched_to(monkeypatch):
    def broken(text, voice, out):
        raise RuntimeError("the piper engine didn't start")
    monkeypatch.setattr(tts.PROVIDERS["piper"], "render", broken)
    with pytest.raises(ValueError, match=r"didn't work \(the piper engine didn't start\); Claude still speaks with"):
        voices.choose("piper:en_US-ryan-medium", download=True)
    assert saved_voice() == ""
    with pytest.raises(ValueError, match="didn't work"):
        voices.try_voice("piper:en_US-ryan-medium")  # not the Windows voice in its place


def test_piper_voice_names_stay_inside_the_voices_folder():
    with pytest.raises(ValueError, match="not a Piper voice name"):
        voices.piper_model("..\\..\\x")


def test_kokoro_is_one_download_for_all_its_voices():
    voices.choose("kokoro:af_heart", download=True)
    assert downloads == ["engine:kokoro", *voices.KOKORO_FILES]
    downloads.clear()
    voices.choose("kokoro:bm_daniel")
    assert downloads == [] and saved_voice() == "kokoro:bm_daniel"


def test_elevenlabs_needs_the_key_typed_in_by_the_user(home, monkeypatch):
    with pytest.raises(voices.NeedsKey, match="phone voice key"):
        voices.choose("elevenlabs:Rachel")
    eleven_account(monkeypatch)
    assert voices.set_key(" sk-good \n").startswith("Saved the ElevenLabs key. Your account has 2 voice(s): Rachel")
    assert home == {"elevenlabs": "sk-good"}
    voices.choose("elevenlabs:rachel")
    assert saved_voice() == "elevenlabs:abc123"  # the voice id, which is what the API takes
    assert voices.current() == voices.Voice("elevenlabs", "abc123")


def test_an_elevenlabs_voice_keeps_eleven_v3_when_that_is_in_use(home, monkeypatch):
    config.CONFIG_FILE.write_text('tts_provider = "elevenlabs_v3"\n', encoding="utf-8")
    eleven_account(monkeypatch)
    voices.set_key("sk-good")
    voices.choose("elevenlabs:George")
    assert saved_voice() == "elevenlabs_v3:def456" and tts.claude_voice() == ("elevenlabs_v3", "def456")
    assert voices.current() == voices.Voice("elevenlabs", "def456")


def test_a_refused_key_is_not_stored(home, monkeypatch):
    import urllib.error

    def refuse(url, headers=None):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(voices, "_fetch", refuse)
    with pytest.raises(ValueError, match="refused that key"):
        voices.set_key("sk-bad")
    assert home == {}


def test_the_old_settings_still_choose_the_voice_and_a_repo_voice_still_wins():
    config.CONFIG_FILE.write_text('tts_provider = "sapi"\nsapi_voice = "Microsoft Zira Desktop"\n', encoding="utf-8")
    assert tts.claude_voice() == ("windows", "Microsoft Zira Desktop")  # "sapi", as config.toml used to say
    assert voices.current() == voices.Voice("windows", "Microsoft Zira Desktop")
    config.CONFIG_FILE.write_text('voice = "windows:Microsoft Mark"\n[repo_voices]\nsite = "piper:en_US-amy-low"\n'
                                  'odd = "espeak:x"\n', encoding="utf-8")
    assert tts.voice_for("site") == ("piper", "en_US-amy-low")
    assert tts.voice_for("other") == tts.voice_for("odd") == ("windows", "Microsoft Mark")  # no espeak engine


# --- deleting ---

def test_the_windows_voices_cant_be_deleted():
    for name in ("windows:Microsoft David Desktop", "windows", "Microsoft Mark"):
        with pytest.raises(ValueError, match="come with Windows"):
            voices.remove(name)


def test_deleting_the_voice_in_use_switches_to_the_windows_voice_first():
    voices.choose("piper:en_US-ryan-medium", download=True)
    voices.choose("piper:en_US-amy-low", download=True)
    msg = voices.remove("piper:en_US-amy-low")
    assert msg.startswith("Claude now speaks with the Windows default voice. Deleted the Piper voice en_US-amy-low")
    assert saved_voice() == "windows" and tts.claude_voice() == ("windows", None)
    assert voices.piper_installed() == ["en_US-ryan-medium"] and voices.engine_installed("piper")
    # An old sapi_voice was the voice until `phone voice` chose another; back on Windows, it's the default voice.
    config.CONFIG_FILE.write_text('sapi_voice = "Microsoft Zira Desktop"\n', encoding="utf-8")
    voices.choose("kokoro:af_sky", download=True)
    assert voices.remove("kokoro").startswith("Claude now speaks with the Windows default voice. ")


def test_the_last_piper_voice_takes_the_engine_with_it():
    voices.choose("piper:en_US-ryan-medium", download=True)
    voices.choose("windows:Microsoft Mark")
    msg = voices.remove("piper:en_US-ryan-medium")
    assert msg.startswith("Deleted the Piper voice en_US-ryan-medium")
    assert msg.endswith("That was the last Piper voice, so the Piper engine went too.")
    assert saved_voice() == "windows:Microsoft Mark"  # not the voice in use, so it stays
    assert not voices.engine_dir("piper").exists() and voices.piper_installed() == []
    assert not list(config.ENGINES.glob("*" + voices.TRASH))


def test_deleting_kokoro_deletes_it_all():
    voices.choose("kokoro:af_sky", download=True)
    msg = voices.remove("kokoro:af_heart")
    assert "Deleted Kokoro" in msg and "all of them went" in msg and saved_voice() == "windows"
    assert not (config.VOICES / "kokoro").exists() and not voices.engine_dir("kokoro").exists()


def test_deleting_what_isnt_there_says_so():
    with pytest.raises(ValueError, match="isn't downloaded"):
        voices.remove("piper:en_US-ryan-medium")
    with pytest.raises(ValueError, match="Kokoro isn't downloaded"):
        voices.remove("kokoro")
    with pytest.raises(ValueError, match="no ElevenLabs key"):
        voices.remove("elevenlabs")


def test_deleting_elevenlabs_forgets_the_key(home, monkeypatch):
    eleven_account(monkeypatch)
    voices.set_key("sk-good")
    voices.choose("elevenlabs:George")
    msg = voices.remove("elevenlabs")
    assert msg == "Claude now speaks with the Windows default voice. Forgot the ElevenLabs key."
    assert home == {} and saved_voice() == "windows"


def test_repos_whose_voice_was_deleted_are_named():
    voices.choose("piper:en_US-ryan-medium", download=True)
    config.CONFIG_FILE.write_text(config.CONFIG_FILE.read_text(encoding="utf-8")
                                  + '\n[repo_voices.site]\nprovider = "piper"\nvoice = "en_US-ryan-medium"\n',
                                  encoding="utf-8")
    assert voices.remove("piper").endswith("repo_voices for site used it, so those repos now get the Windows "
                                           "default voice.")


# --- speech falls back, without keeping the fallback ---

def test_a_failing_engine_falls_back_to_windows_and_is_tried_again_next_time(monkeypatch):
    rendered = []
    monkeypatch.setattr(tts.PROVIDERS["windows"], "render",
                        lambda t, v, out: (rendered.append("windows"), out.write_bytes(b"s")))

    def broken(text, voice, out):
        rendered.append("piper")
        raise RuntimeError("engine stopped")
    monkeypatch.setattr(tts.PROVIDERS["piper"], "render", broken)
    first = tts.render("Fall back please.", voice=("piper", "en_US-ryan-medium"))
    monkeypatch.setattr(tts.PROVIDERS["piper"], "render",
                        lambda t, v, out: (rendered.append("piper"), out.write_bytes(b"p")))
    second = tts.render("Fall back please.", voice=("piper", "en_US-ryan-medium"))
    assert rendered == ["piper", "windows", "piper"] and first != second


# --- the command line and Claude's tool ---

def test_claude_running_phone_voice_gets_the_download_question_to_ask(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as e:
        cli.main(["voice", "piper:en_US-ryan-medium"])
    assert e.value.code == 2 and downloads == []
    assert ("Ask the user first; if they agree, run: phone voice piper:en_US-ryan-medium --download"
            in capsys.readouterr().out)
    cli.main(["voice", "piper:en_US-ryan-medium", "--download"])
    assert saved_voice() == "piper:en_US-ryan-medium"


def test_phone_voice_lists_and_removes(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    cli.main(["voices"])
    out = capsys.readouterr().out
    assert out.startswith("Claude speaks with the Windows default voice (windows).")
    assert "windows:Microsoft Mark  (en-US)" in out and "kokoro: free and offline" in out
    cli.main(["voice", "list", "piper", "amy"])
    assert capsys.readouterr().out.strip() == "piper:en_US-amy-low  English (United States), 60 MB"
    with pytest.raises(SystemExit) as e:
        cli.main(["voice", "remove", "windows:Microsoft Mark"])
    assert e.value.code == "Not changed: the Windows voices come with Windows, so they can't be deleted here"


def test_the_key_is_never_asked_for_when_claude_runs_it(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as e:
        cli.main(["voice", "elevenlabs:Rachel"])
    assert e.value.code == 3 and "run `phone voice key` in a terminal" in capsys.readouterr().out


def test_nul_input_is_not_a_person_at_a_console(monkeypatch):
    with open("NUL") as nul:  # isatty() is True for NUL on Windows
        monkeypatch.setattr(sys, "stdin", nul)
        assert not cli._interactive()


def test_claudes_tool_switches_but_leaves_downloads_to_the_terminal(monkeypatch):
    elsewhere = []
    monkeypatch.setattr(mcp_server, "_prerender_elsewhere", lambda: elsewhere.append(1))
    monkeypatch.setattr(tts, "prerender", lambda: pytest.fail("the fixed phrases were rendered in the MCP server"))
    msg = asyncio.run(mcp_server.change_voice("kokoro:af_heart"))
    assert msg.startswith("Not changed: The Kokoro voice af_heart needs a one-off download of about 250 MB. Ask")
    assert "voice kokoro:af_heart --download` in the terminal" in msg and downloads == []
    assert asyncio.run(mcp_server.change_voice("windows:Microsoft Mark")).startswith("Claude now speaks with")
    assert elsewhere == [1]  # by a short-lived `phone prerender`, not in this long-lived process
    assert asyncio.run(mcp_server.change_voice()).startswith("Claude speaks with the Windows voice Microsoft Mark")


def test_claudes_tool_lets_go_of_the_engine_and_spends_no_credits(home, monkeypatch):
    elsewhere, stopped = [], []
    monkeypatch.setattr(mcp_server, "_prerender_elsewhere", lambda: elsewhere.append(1))
    monkeypatch.setattr(voiceworker, "stop_here", stopped.append)
    voices.choose("kokoro:af_heart", download=True)
    asyncio.run(mcp_server.change_voice("kokoro:bm_george"))
    assert stopped == ["kokoro"] and elsewhere == [1]  # the check's worker doesn't stay in the MCP server
    eleven_account(monkeypatch)
    voices.set_key("sk-good")
    assert asyncio.run(mcp_server.change_voice("elevenlabs:Rachel")).startswith("Claude now speaks with")
    assert elsewhere == [1]  # no fixed phrases rendered with paid speech


def test_a_new_prerender_replaces_the_one_still_running(monkeypatch, tmp_path):
    killed, started = [], []

    class Old:
        def __init__(self, pid):
            self.pid = pid

        def kill(self):
            killed.append(self.pid)

    class New:
        def __init__(self, argv, **kw):
            started.append(argv)
            self.pid = 4242
    monkeypatch.setattr(procs, "RUN", tmp_path)
    monkeypatch.setattr(procs, "pid", lambda name: 999 if name == "prerender" else None)
    monkeypatch.setattr(procs.psutil, "Process", Old)
    monkeypatch.setattr(procs.subprocess, "Popen", New)
    REAL_PRERENDER_ELSEWHERE()
    assert killed == [999] and started[0][-1] == "prerender"
    assert (tmp_path / "prerender.pid").read_text() == "4242"

    def cant(argv, **kw):
        raise OSError("no such file")
    monkeypatch.setattr(procs.subprocess, "Popen", cant)
    REAL_PRERENDER_ELSEWHERE()  # one that won't start is logged, not raised: the switch stands


# --- the worker protocol ---

def test_the_worker_answers_each_request_on_its_own_line(monkeypatch):
    seen = []
    monkeypatch.setitem(voiceworker.RENDERERS, "piper",
                        lambda models, req: seen.append(req["text"]) if req["text"] != "boom" else 1 / 0)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"text": "hi"}\n{"text": "boom"}\n'))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    voiceworker.serve("piper")
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0] == {"ready": True} and replies[1] == {"ok": True}
    assert replies[2]["error"].startswith("ZeroDivisionError") and seen == ["hi"]


ECHO = ("import json, sys\nprint(json.dumps({'ready': True}), flush=True)\n"
        "for line in sys.stdin:\n    print(json.dumps({'ok': True, 'heard': json.loads(line)['text']}), flush=True)\n")


def test_a_worker_runs_whatever_command_speaks_the_protocol(monkeypatch):
    monkeypatch.setattr(voiceworker, "_workers", {})
    w = voiceworker.command_worker("echo", [sys.executable, "-c", ECHO])
    try:
        w.render(text="one")
        w.render(text="two")
        assert voiceworker.command_worker("echo", ["ignored"]) is w and voiceworker.is_warm("echo")
    finally:
        voiceworker.stop_here("echo")
    assert w.proc is None


class Answers:
    """A worker process that answers each request with the next of replies."""
    def __init__(self, *replies):
        self.stdin, self.stdout, self.replies = io.StringIO(), self, list(replies)

    def readline(self):
        return self.replies.pop(0)

    def poll(self):
        return None

    def kill(self):
        pass


def test_an_engine_that_answers_with_an_error_is_not_unavailable(monkeypatch):
    # The caller falls back to another voice only when the worker itself is down, not when the text failed.
    w = voiceworker.Worker("windows", [])
    monkeypatch.setattr(w, "_start", lambda: Answers('{"error": "IOException: bad path"}\n', "not json\n"))
    monkeypatch.setattr(w, "_arm", lambda: None)
    with pytest.raises(RuntimeError, match="bad path") as failed:
        w.render(text="hello")
    assert not isinstance(failed.value, voiceworker.Unavailable)
    with pytest.raises(voiceworker.Unavailable, match="answered 'not json"):
        w.render(text="hello")


def test_a_worker_can_wait_less_for_its_first_answer(monkeypatch):
    waited = []
    monkeypatch.setattr(voiceworker, "_readline", lambda proc, timeout: waited.append(timeout) or '{"ok": true}\n')
    w = voiceworker.Worker("windows", [], first_timeout=20)
    monkeypatch.setattr(w, "_start", Answers)
    monkeypatch.setattr(w, "_arm", lambda: None)
    w.render(text="first")
    w.render(text="second")
    assert waited == [20, voiceworker.RENDER_TIMEOUT_S]


def test_a_python_engines_worker_finds_its_packages(monkeypatch):
    monkeypatch.setattr(voiceworker, "_workers", {})
    w = voiceworker.worker("piper", config.ENGINES / "piper")
    assert w.command == [sys.executable, "-m", voiceworker.MODULE, "piper"]
    assert w.env["PYTHONPATH"] == str(config.ENGINES / "piper")


def test_a_worker_that_died_is_started_again(monkeypatch):
    class Proc:
        def __init__(self, replies):
            self.stdin, self.replies = io.StringIO(), list(replies)
            self.stdout = self

        def readline(self):
            return self.replies.pop(0)

        def poll(self):
            return None

    procs = [Proc([""]), Proc(['{"ok": true}\n'])]  # the first dies mid-request
    w = voiceworker.Worker("piper", [])
    monkeypatch.setattr(w, "_start", lambda: procs.pop(0))
    monkeypatch.setattr(w, "_arm", lambda: None)
    w.render(text="hello")
    assert procs == []


def test_a_hung_worker_is_killed_so_the_call_falls_back(monkeypatch):
    import threading
    hang = threading.Event()

    class Proc:
        stdin = io.StringIO()
        killed = False

        def __init__(self):
            self.stdout = self

        def readline(self):
            hang.wait(5)
            return ""

        def poll(self):
            return None

        def kill(self):
            Proc.killed = True
            hang.set()

    monkeypatch.setattr(voiceworker, "FIRST_RENDER_TIMEOUT_S", 0.2)
    w = voiceworker.Worker("piper", [])
    monkeypatch.setattr(w, "_start", Proc)
    monkeypatch.setattr(w, "_arm", lambda: None)
    with pytest.raises(voiceworker.Unavailable, match="didn't answer"):
        w.render(text="hello")
    assert Proc.killed and w.proc is None


# --- downloads ---

class Served(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_a_download_longer_than_announced_is_stopped_and_removed(monkeypatch):
    monkeypatch.setattr(voices.urllib.request, "urlopen", lambda url, timeout: Served(b"x" * (3 << 20)))
    dest = config.VOICES / "piper" / "big.onnx"
    with pytest.raises(ValueError, match="arrived damaged"):
        REAL_DOWNLOAD("https://example/big.onnx", dest, 1 << 20, "0", "md5", lambda s: None)
    assert not dest.exists() and not dest.with_name("big.onnx.part").exists()


def test_a_dropped_download_says_so(monkeypatch):
    import http.client

    class Dropped(Served):
        def read(self, n=-1):
            raise http.client.IncompleteRead(b"")
    monkeypatch.setattr(voices.urllib.request, "urlopen", lambda url, timeout: Dropped())
    with pytest.raises(ValueError, match=r"couldn't download x\.onnx"):
        REAL_DOWNLOAD("https://example/x.onnx", config.VOICES / "piper" / "x.onnx", 10, "0", "md5", lambda s: None)


def test_a_damaged_list_of_piper_voices_is_not_trusted(monkeypatch):
    (config.VOICES / "piper" / "voices.json").write_text("[1, 2", encoding="utf-8")
    monkeypatch.setattr(voices, "_fetch", lambda url, headers=None: b'"not a list of voices"')
    with pytest.raises(ValueError, match="couldn't fetch Piper's list of voices"):
        voices.piper_catalogue()
    assert not (config.VOICES / "piper" / "voices.json").exists()


def test_a_worker_waits_longer_only_while_its_model_loads_and_is_warm_once_it_has_answered(monkeypatch):
    waited = []

    class Proc:
        stdin = io.StringIO()

        def poll(self):
            return None

    def readline(proc, timeout):
        waited.append(timeout)
        return '{"ok": true}\n'
    monkeypatch.setattr(voiceworker, "_readline", readline)
    w = voiceworker.Worker("piper", [])
    monkeypatch.setattr(w, "_start", Proc)
    monkeypatch.setattr(w, "_arm", lambda: None)
    monkeypatch.setattr(voiceworker, "_workers", {"piper": w})
    assert not voiceworker.is_warm("piper")
    w.render(text="first")
    assert voiceworker.is_warm("piper")
    w.render(text="second")
    assert waited == [voiceworker.FIRST_RENDER_TIMEOUT_S, voiceworker.RENDER_TIMEOUT_S]


def test_the_engine_leaves_cores_free_and_doesnt_spin(monkeypatch):
    import types

    class Options:
        def __init__(self):
            self.config = {}

        def add_session_config_entry(self, key, value):
            self.config[key] = value

    made = []

    class Session:
        def __init__(self, path, sess_options=None, providers=None, **kw):
            made.append(sess_options)
    fake = types.SimpleNamespace(SessionOptions=Options, InferenceSession=Session)
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    monkeypatch.setattr(voiceworker, "_calmed", False)
    voiceworker._calm_onnxruntime()
    voiceworker._calm_onnxruntime()  # once is enough
    fake.InferenceSession("model.onnx", providers=["CPUExecutionProvider"])  # as kokoro_onnx does
    opts, = made
    assert opts.intra_op_num_threads == voiceworker.ENGINE_THREADS and opts.inter_op_num_threads == 1
    assert opts.config == {"session.intra_op.allow_spinning": "0"}
    assert 1 <= voiceworker.ENGINE_THREADS < cli.psutil.cpu_count()


def test_a_voice_is_warmed_up_once_as_it_loads(monkeypatch):
    import types
    spoken = []

    class Samples:  # just enough of a numpy array for _kokoro
        def __mul__(self, n):
            return self

        def astype(self, kind):
            return self

        def tobytes(self):
            return b"\0\0" * 240

    class Kokoro:
        def __init__(self, model, voices):
            pass

        def create(self, text, voice, speed, lang):
            spoken.append(text)
            return Samples(), 24000
    monkeypatch.setitem(sys.modules, "kokoro_onnx", types.SimpleNamespace(Kokoro=Kokoro))
    monkeypatch.setitem(sys.modules, "numpy", types.SimpleNamespace(clip=lambda s, low, high: s))
    monkeypatch.setattr(voiceworker, "_calm_onnxruntime", lambda: None)
    models = {}
    for n in (1, 2):
        out = config.SOUNDS / f"warm{n}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        voiceworker._kokoro(models, {"model": "m", "voices": "v", "voice": "am_adam", "text": f"Real {n}.",
                                     "out": str(out)})
    assert spoken == [voiceworker.WARM_UP_TEXT, "Real 1.", "Real 2."]  # the slow first inference isn't a call's
