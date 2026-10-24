"""`phone` command line."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from collections.abc import Callable

import psutil

# keychain (keyring) and urllib.request are imported where they're used: together they were half a hook's start-up.
from . import claudewire, config, procs, registry

MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{}"


# --- FreeSWITCH ---

# Media ports for calls. scripts/firewall.ps1 opens these (UDP) and sip_port for a desk phone on the LAN.
RTP_START, RTP_END = 16384, 16484


def render_freeswitch_config() -> None:
    from . import keychain
    s = config.load()
    if not s.lan_ip:
        sys.exit(f"No LAN address found: connect to the network, or set lan_ip in {config.CONFIG_FILE}")
    subs = {
        "LAN_IP": s.lan_ip, "SIP_PORT": str(s.sip_port),
        "ESL_PORT": str(s.esl_port), "ESL_PASSWORD": keychain.get_or_create("esl"),
        "DESK_EXT": s.desk_ext, "SOFT_EXT": s.softphone_ext, "SIP_PASSWORD": keychain.get_or_create("sip"),
        "RTP_START": str(RTP_START), "RTP_END": str(RTP_END),
    }
    text = (config.REPO / "freeswitch" / "freeswitch.xml.tmpl").read_text(encoding="utf-8")
    for k, v in subs.items():
        text = text.replace(f"@@{k}@@", v)
    config.FREESWITCH_CONF.mkdir(parents=True, exist_ok=True)
    (config.FREESWITCH_CONF / "freeswitch.xml").write_text(text, encoding="utf-8")


def freeswitch_argv(s: config.Settings) -> list[str]:
    """FreeSWITCH from its install folder, with config, logs, database and recordings under ~/.claude-phone."""
    fs = config.FREESWITCH
    dirs = {"-conf": config.FREESWITCH_CONF, "-log": fs / "log", "-db": fs / "db", "-run": fs / "run",
            "-cache": fs / "cache", "-temp": fs / "temp", "-storage": fs / "storage", "-recordings": config.RECORDINGS}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return [s.freeswitch_exe, "-nonat", "-nonatmap", *(x for flag, d in dirs.items() for x in (flag, str(d)))]



# --- services ---

@dataclass(frozen=True)
class Service:
    argv: Callable[[config.Settings], list[str]]
    # A Windows priority class, set as the process starts. HIGH needs no admin rights (only REALTIME does).
    priority: int = 0
    env: dict[str, str] = field(default_factory=dict)  # added to the environment


def _spawn(name: str, svc: Service, s: config.Settings) -> None:
    if procs.pid(name):
        print(f"{name}: already running")
        return
    env = {**os.environ, **svc.env} if svc.env else None
    pid = procs.spawn(name, svc.argv(s), config.LOGS / f"{name}.out", svc.priority, env)
    print(f"{name}: started (pid {pid})")


def _stop(name: str) -> None:
    if name == "daemon" and procs.pid(name):
        _ask_daemon_to_stop()
    if name == "freeswitch" and procs.pid(name):
        _ask_freeswitch_to_hang_up()
    print(f"{name}: {'stopped' if procs.stop(name) else 'not running'}")


# How long FreeSWITCH gets to send its hang-ups before it's ended.
HANGUP_WAIT_S = 2


def _ask_freeswitch_to_hang_up() -> None:
    """Ended from outside (TerminateProcess on Windows), FreeSWITCH sends no hang-up, so a call the daemon doesn't run,
    like the echo test, stays open on the phone as a silent line. One that doesn't answer is ended anyway."""
    from . import esl
    try:
        esl.api_local("hupall NORMAL_CLEARING")
        deadline = time.monotonic() + HANGUP_WAIT_S
        while not esl.api_local("show channels count").startswith("0 total") and time.monotonic() < deadline:
            time.sleep(0.1)
    except (OSError, esl.ESLError):
        pass


def _ask_daemon_to_stop() -> None:
    """The daemon hangs up its calls, then exits; ended from outside, it would leave them open. One that doesn't
    answer is ended anyway."""
    import urllib.request
    req = urllib.request.Request(f"{config.load().daemon_url}/shutdown", data=b"{}", method="POST")
    try:
        urllib.request.urlopen(req, timeout=procs.STOP_TIMEOUT_S).close()
    except OSError:
        pass


def model_path() -> Path:
    return config.MODELS / config.load().whisper_model


def ensure_model(keep_alive: Callable[[], None] = lambda: None, progress: Callable[[int, int], None] | None = None,
                 ) -> None:
    """Downloads the speech model if it's missing, checked against the sha256 Hugging Face publishes for it, and
    calling keep_alive every LOCK_TOUCH_S meanwhile."""
    from . import tools
    p = model_path()
    if p.exists():
        return
    url = MODEL_URL.format(p.name)
    print(f"downloading {p.name} ...")
    try:
        _, sha256 = tools.hugging_face_checksum(url)
    except OSError:
        sha256 = None  # can't ask: take the file unchecked rather than not at all
    last = time.monotonic()

    def touch(done: int, total: int) -> None:
        nonlocal last
        if time.monotonic() - last >= LOCK_TOUCH_S:
            last = time.monotonic()
            keep_alive()
        if progress:
            progress(done, total)
    tools.download(url, p, sha256=sha256, progress=touch)


def py_argv(*args: str) -> list[str]:
    return [sys.executable, "-m", "claude_phone", *args]


def whisper_server(s: config.Settings) -> str:
    exe = s.whisper_server or shutil.which("whisper-server")
    if not exe:
        sys.exit("whisper-server not found: put its folder on PATH, or set whisper_server in "
                 f"{config.CONFIG_FILE} to the full path of whisper-server.exe")
    return exe


# Physical cores: whisper gains nothing from hyperthreads.
WHISPER_THREADS = psutil.cpu_count(logical=False) or 4

# FreeSWITCH plays every call's audio on a 20 ms clock, so it runs above everything else on the PC, while whisper's
# bursts run below it. OMP_WAIT_POLICY=PASSIVE: whisper's threads sleep after a transcription instead of spinning
# (measured: about one core for ~0.5 s after each answer without it, nothing with it).
SERVICES = {
    "freeswitch": Service(freeswitch_argv, subprocess.HIGH_PRIORITY_CLASS),
    "whisper": Service(lambda s: [whisper_server(s), "-m", str(model_path()), "--host", "127.0.0.1",
                                  "--port", str(s.whisper_port), "-t", str(WHISPER_THREADS), "--no-timestamps",
                                  "-l", s.whisper_language],
                       subprocess.BELOW_NORMAL_PRIORITY_CLASS, {"OMP_WAIT_POLICY": "PASSIVE"}),
    "daemon": Service(lambda s: py_argv("daemon")),
}


# Editor windows restored together each run `phone up` (autostart). The first takes this lock and the others leave
# the starting to it. A lock older than this was left by an `up` that was killed; a live one touches its lock every
# LOCK_TOUCH_S while it downloads the speech model.
UP_LOCK_STALE_S = 300
LOCK_TOUCH_S = 30


def _take_up_lock() -> Path | None:
    lock = procs.RUN / "up.lock"
    procs.RUN.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - lock.stat().st_mtime > UP_LOCK_STALE_S:
            lock.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return None
    return lock


def cmd_up(a) -> None:
    config.ensure_dirs()
    lock = _take_up_lock()
    if not lock:
        print("another `phone up` is starting claude-phone")
        return
    try:
        config.remember_phone_exe()
        s = config.load()
        names = [a.only] if a.only else list(SERVICES)
        # Each only for the service that reads it: `phone up daemon` works away from home, without a LAN address.
        if "freeswitch" in names:
            render_freeswitch_config()
        if "whisper" in names:
            ensure_model(keep_alive=lambda: os.utime(lock))
        # The next service relies on these: the daemon connects to FreeSWITCH's event socket and to whisper.
        ports = {"freeswitch": s.esl_port, "whisper": s.whisper_port}
        for n in names:
            _spawn(n, SERVICES[n], s)
            if n in ports and not _answers(ports[n], SERVICE_WAIT_S):
                print(f"{n}: not answering on port {ports[n]} after {SERVICE_WAIT_S} s "
                      f"(see {config.LOGS / f'{n}.out'})")
    finally:
        lock.unlink(missing_ok=True)


SERVICE_WAIT_S = 15  # how long `phone up` waits for a service to answer before going on without it


def _answers(port: int, wait_s: float) -> bool:
    """Something accepts connections on 127.0.0.1:port within wait_s. whisper-server only listens once its model
    has loaded, so for it this means ready."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)


def cmd_down(a) -> None:
    names = [a.only] if a.only else list(reversed(list(SERVICES)))
    for n in names:
        _stop(n)


def cmd_restart(a) -> None:
    cmd_down(a)
    cmd_up(a)


def _sizes(folder: Path) -> list[int]:
    """Sizes of the files in a folder; the daemon may delete one while we look."""
    sizes = []
    for p in folder.glob("*") if folder.exists() else ():
        try:
            if p.is_file():
                sizes.append(p.stat().st_size)
        except FileNotFoundError:
            pass
    return sizes


def audio_usage() -> str:
    """Recordings and cached speech on disk; housekeeping keeps both small."""
    rec, speech = _sizes(config.RECORDINGS), _sizes(config.SOUNDS / "tts")
    return (f"{len(rec) + len(speech)} files, {(sum(rec) + sum(speech)) / 1e6:.1f} MB "
            f"(recordings {len(rec)}, speech {len(speech)})")


# Claude Code event -> `phone hook` argument; claudewire.py has what goes into Claude Code's settings for each.
HOOKS = claudewire.HOOKS
HOOK_EVENTS = tuple(HOOKS.values())


def missing_hooks() -> list[str]:
    """The `phone hook` events that Claude Code's user settings don't run."""
    return claudewire.missing_hooks()


def claude_wiring_warnings() -> list[str]:
    """What `phone status` says is wrong with Claude Code's side: hooks, their matcher, the MCP registration."""
    out = []
    missing = missing_hooks()
    if missing:
        out.append(f"Claude Code's settings ({claudewire.settings_file()}) don't run `phone hook` for "
                   f"{', '.join(missing)}. Copy those hooks from the README's \"Claude Code wiring\".")
    elif claudewire.weak_matcher():
        out.append(f"the pre-tool hook's matcher in {claudewire.settings_file()} leaves out the call and notify "
                   f"tools, so Claude would ask before phoning. It should include `{claudewire.CALL_MATCHER}`.")
    exe = Path(sys.executable).with_name("phone.exe")
    state = claudewire.mcp_state(exe) if exe.exists() else "unknown"
    if state == "missing":
        out.append("Claude Code doesn't have the claude-phone MCP server, so there are no call_/notify_ tools. "
                   f"Register it: claude mcp add --scope user claude-phone -- {exe} mcp")
    elif state == "other-path":
        out.append(f"Claude Code's claude-phone MCP server points at another phone.exe than {exe}.")
    return out


def cmd_status(a) -> None:
    w = config.ip_warning()
    if w:
        print("WARNING:", w)
    for problem in config.problems():
        print(f"WARNING: {config.CONFIG_FILE}: {problem}")
    for warning in claude_wiring_warnings():
        print("WARNING:", warning)
    for n in SERVICES:
        pid = procs.pid(n)
        print(f"{n:9} {'running pid ' + str(pid) if pid else 'stopped'}")
    print(f"{'audio':9} {audio_usage()}")
    import urllib.request

    from . import esl, voices
    print(f"{'voice':9} {voices.current().spec}")
    try:
        with urllib.request.urlopen(f"{config.load().daemon_url}/health", timeout=3) as r:
            print("health   ", json.loads(r.read()))
    except Exception as e:
        print("health    unavailable:", e)
    if procs.pid("freeswitch"):
        # Ask the SIP profile: `show registrations` reads a table that survives FreeSWITCH restarts.
        s = config.load()
        for ext in dict.fromkeys((s.ring_ext, s.desk_ext, s.softphone_ext)):
            try:
                contact = esl.api_local(f"sofia_contact internal/{ext}@{s.lan_ip}").strip()
            except Exception as e:
                contact = f"unavailable: {e}"
            state = "not registered" if contact.startswith("error/") else contact
            print(f"phone {ext:4} {state}{'  (rings)' if ext == s.ring_ext else ''}")


def cmd_list(a) -> None:
    rows = registry.all_sessions()
    if not rows:
        print("No live sessions.")
        return
    print(f"{'EXT':4} {'REPO':22} {'BRANCH':28} {'HOST':14} {'STATUS':8} {'RING':5} SINCE")
    overrides = config.load().repo_ringtones
    for s in rows:
        b = s.branch or ""
        if s.worktree:
            b = f"{b} [{s.worktree}]"
        since = datetime.fromtimestamp(s.started_at).strftime("%H:%M")
        ring = overrides.get(s.repo) or registry.ringtone(s.repo)
        print(f"{s.ext:<4} {s.repo[:22]:22} {b[:28]:28} {(s.host or '-')[:14]:14} {s.status:8} {ring:<5} {since}")


def cmd_type(a) -> None:
    """Type text into a session as if it came from the phone, to test typing into its terminal without a call."""
    from . import inject
    sess = registry.by_ext(a.ext)
    if not sess:
        sys.exit(f"No session on extension {a.ext}. See: phone list")
    print(inject.type_into(sess, a.text, "phone type"))


def cmd_vm(a) -> None:
    for v in registry.pending_voicemails():
        t = datetime.fromtimestamp(v.created_at).strftime("%H:%M")
        print(f"#{v.id} {t} {v.label}: {v.question}")


def cmd_hook(a) -> None:
    from . import hooks
    sys.exit(hooks.run(a.event))


def cmd_daemon(a) -> None:
    from . import daemon
    daemon.run()


def cmd_mcp(a) -> None:
    from . import mcp_server
    mcp_server.run()


def cmd_secret(a) -> None:
    from . import keychain
    print(keychain.get_or_create(a.name))


def cmd_hold_music(a) -> None:
    from . import hold
    print(hold.generate(a.prompt or hold.DEFAULT_PROMPT, a.seconds))


def cmd_desk_cert(a) -> None:
    """Forgets the desk phone's pinned certificate, for a new or factory-reset phone (which makes a new one)."""
    from . import screen
    registry.set_state(screen.CERT_STATE, None)
    print("Forgot the desk phone's certificate; the next screen push or key press pins the one it shows then.")


def cmd_push(a) -> None:
    from . import push
    if a.action == "test":
        sent = push.send("Claude Phone", "Test push: missed calls will show up here.")
        print("sent" if sent else "failed (see phone.log)")
        return
    t = push.create_topic()
    print(f"In the ntfy app, subscribe to this topic on {config.load().ntfy_server}:\n\n  {t}\n\n"
          "Treat it like a password. Then run: phone push test")


def cmd_meeting_test(a) -> None:
    import urllib.request
    req = urllib.request.Request(f"{config.load().daemon_url}/meeting-test", data=b"{}", method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        print(json.loads(r.read()))


def cmd_meetings(a) -> None:
    from datetime import datetime, timedelta
    from . import diary
    if not diary.access():
        print("No calendar link for calendar_sources yet: run `phone calendar add google` (or outlook).")
        return
    now = datetime.now().astimezone()
    found = diary.meetings(now, now + timedelta(days=2))
    print("calendars:", ", ".join(diary.calendars()) or "none could be read (see `phone calendar`)")
    for m in found:
        title, text = diary.summary(m)
        print(f"{m.start:%a} {title} ({text.replace(chr(10), '; ')})")


def cmd_calendar(a) -> None:
    from . import diary, keychain
    if a.action == "remove":
        keychain.delete(f"calendar-{a.source}")
        print(f"Removed the {a.source.capitalize()} calendar link.")
    elif a.action == "add":
        add_calendar(a.source)
    else:
        for st in diary.status():
            parts = ["selected" if st["selected"] else "not selected", "link stored" if st["linked"] else "no link"]
            if st["name"]:
                parts.append(st["name"])
            if st["fetched"]:
                parts.append(f"read {st['fetched']:%H:%M}, {st['meetings']} meeting(s) in the coming days")
            if st["error"]:
                parts.append(f"can't be read: {st['error']}")
            print(f"{st['source']:<8} " + "; ".join(parts))


def add_calendar(source: str) -> None:
    """The link is a secret: read without echo (or from a pipe), so it stays out of shell history and logs."""
    import getpass

    from . import keychain
    from . import diary
    print(f"Paste the {source.capitalize()} calendar's secret ICS link (it won't be shown), then press Enter.")
    raw = getpass.getpass("link: ") if _interactive() else sys.stdin.readline()
    url = ""
    try:
        url = diary.normalise_url(raw)
        name, count = diary.check(url)
    except Exception as e:
        sys.exit(f"Not saved: {diary.reason(e, url or raw.strip())}")
    keychain.put(f"calendar-{source}", url)
    print(f"Saved. {name or 'The calendar'} has {count} meeting(s) in the next two days.")
    s = config.load()
    chosen = [str(x).lower() for x in s.calendar_sources]
    if source not in chosen:
        print(f"To use it: phone config set calendar_sources {', '.join([*chosen, source])}")
    if not s.meeting_alerts:
        print("To turn the alerts on: phone config set meeting_alerts on")
    print("Then run: phone restart daemon")


def cmd_config(a) -> None:
    from . import configedit
    if a.action == "render":
        render_freeswitch_config()
        print(f"FreeSWITCH config written to {config.FREESWITCH_CONF}. Run `phone restart freeswitch` to use it.")
        return
    if a.action == "migrate":
        try:
            print(configedit.migrate())
        except ValueError as e:
            sys.exit(f"Not changed: {e}")
        return
    if a.action in ("get", "set", "unset") and not a.key:
        sys.exit(f"Usage: phone config {a.action} <setting>" + (" <value>" if a.action == "set" else ""))
    if a.action == "set" and not a.value:
        sys.exit("Usage: phone config set <setting> <value>")
    if (a.action != "set" and a.value) or (not a.action and a.key):
        sys.exit("Usage: phone config [get <setting> | set <setting> <value> | unset <setting> | render | migrate]")
    try:
        if a.action == "get":
            print(configedit.get(a.key))
        elif a.action == "set":
            print(configedit.set_value(a.key, " ".join(a.value)))
        elif a.action == "unset":
            print(configedit.unset(a.key))
        else:
            print(configedit.show())
    except ValueError as e:
        sys.exit(f"Not changed: {e}" if a.action in ("set", "unset") else str(e))


def cmd_setup(a) -> None:
    from . import wizard
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("phone setup asks questions: run it in a terminal.")
    try:
        wizard.Wizard(redo_all=a.all).run()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nStopped. Nothing half-written: run `phone setup` again to pick up where you left off.")


def cmd_update(a) -> None:
    from types import SimpleNamespace

    from . import updater, wizard

    def start() -> None:
        # A new process, so the services start from the code that was just pulled.
        subprocess.run([sys.executable, "-c", "from claude_phone.cli import main; main(['up'])"], check=False)
    ok = updater.Updater(
        repo=config.REPO,
        running=lambda: any(procs.pid(name) for name in SERVICES),
        stop=lambda: cmd_down(SimpleNamespace(only=None)),
        start=start,
        offer_editors=wizard.Wizard().offer_editor_updates,
    ).update()
    if not ok:
        sys.exit(1)


def cmd_change_phone(a) -> None:
    from . import configedit
    try:
        print(configedit.change_phone(a.phone))
    except ValueError as e:
        sys.exit(f"Not changed: {e}")


def cmd_prerender(a) -> None:
    from . import tts
    print(f"rendered {tts.prerender()} phrases")


def cmd_say(a) -> None:
    from . import tts
    print(tts.render(a.text))


VOICE_USAGE = ("Usage: phone voice [<engine:name> [--download] | list <engine> [filter] | try <engine:name> | "
               "remove <engine:name> | key]")


def cmd_voice(a) -> None:
    """Claude's voice: show, switch (downloading with the user's yes), hear, delete, or store the ElevenLabs key."""
    from . import voices
    first, rest = (a.words[0].lower(), " ".join(a.words[1:])) if a.words else ("", "")
    try:
        if not a.words:
            print(voices.describe())
        elif first == "list":
            print(voices.list_engine(a.words[1], " ".join(a.words[2:])) if rest else voices.describe())
        elif first in ("remove", "delete"):
            if not rest:
                sys.exit(VOICE_USAGE)
            print(voices.remove(rest))
        elif first == "try":
            if not rest:
                sys.exit(VOICE_USAGE)
            _play(voices.try_voice(rest))
        elif first == "key":
            print(voices.set_key(_read_secret("ElevenLabs API key")))
        else:
            _choose(" ".join(a.words), a.download)
    except ValueError as e:
        sys.exit(f"Not changed: {e}" if first not in ("", "list", "try") else str(e))


def _interactive() -> bool:
    """A person at a console. Not isatty() alone: Windows' NUL device passes that, and Claude runs commands with
    NUL as input, where a prompt would wait forever."""
    if not sys.stdin.isatty():
        return False
    try:
        import ctypes
        import msvcrt
        mode = ctypes.c_uint()
        return bool(ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(sys.stdin.fileno()),
                                                          ctypes.byref(mode)))
    except (OSError, AttributeError, ValueError):
        return False


def _yes(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _choose(name: str, download: bool) -> None:
    from . import voices
    interactive = _interactive()
    ask = _yes if interactive else None
    try:
        print(voices.choose(name, download=download, ask=ask))
    except voices.NeedsDownload as e:
        if interactive:
            sys.exit("Nothing downloaded or changed.")
        # Claude ran this: it asks the user, then runs it again with --download.
        print(f"{e}. Ask the user first; if they agree, run: phone voice {e.voice.spec} --download")
        sys.exit(2)
    except voices.NeedsKey as e:
        if not interactive:
            print(f"{e}.")
            sys.exit(3)
        print(voices.set_key(_read_secret("ElevenLabs API key")))
        print(voices.choose(name, download=download, ask=ask))


def _read_secret(what: str) -> str:
    """Without echo (or from a pipe), so it stays out of shell history and logs."""
    import getpass
    print(f"Paste your {what} (it won't be shown), then press Enter.")
    return getpass.getpass(f"{what}: ") if _interactive() else sys.stdin.readline()


def _play(path: str) -> None:
    """On this PC's speakers."""
    import winsound
    print(f"Playing {path}")
    winsound.PlaySound(path, winsound.SND_FILENAME)


def main(argv: list[str] | None = None) -> None:
    # Windows error messages come in the system language (æøå), which a console's code page may not cover.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "hook" and args[1] in HOOK_EVENTS:
        # Every prompt and turn runs one, as a process of its own: straight there, before argparse and its imports.
        from . import hooks
        sys.exit(hooks.run(args[1]))
    import argparse
    p = argparse.ArgumentParser(prog="phone")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("up", cmd_up), ("down", cmd_down), ("restart", cmd_restart)):
        sp = sub.add_parser(name)
        sp.add_argument("only", nargs="?", choices=list(SERVICES))
        sp.set_defaults(fn=fn)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("vm", help="pending voicemail questions").set_defaults(fn=cmd_vm)
    sp = sub.add_parser("type", help="type text into a session, as a phone message would be")
    sp.add_argument("ext", type=int)
    sp.add_argument("text")
    sp.set_defaults(fn=cmd_type)
    sp = sub.add_parser("hook")
    sp.add_argument("event", choices=HOOK_EVENTS)
    sp.set_defaults(fn=cmd_hook)
    sub.add_parser("daemon").set_defaults(fn=cmd_daemon)
    sub.add_parser("prerender", help="render the fixed phrases in the current voice").set_defaults(fn=cmd_prerender)
    sub.add_parser("mcp").set_defaults(fn=cmd_mcp)
    sp = sub.add_parser("secret", help="print (creating if needed) a secret: sip (the phones' password) | esl")
    sp.add_argument("name", choices=["sip", "esl"])
    sp.set_defaults(fn=cmd_secret)
    sp = sub.add_parser("hold-music", help="generate new hold music with ElevenLabs Music")
    sp.add_argument("prompt", nargs="?")
    sp.add_argument("--seconds", type=int, default=60)
    sp.set_defaults(fn=cmd_hold_music)
    sp = sub.add_parser("desk-cert", help="desk-cert reset: forget the desk phone's certificate (a new or reset phone)")
    sp.add_argument("action", choices=["reset"])
    sp.set_defaults(fn=cmd_desk_cert)
    sp = sub.add_parser("push", help="set up (or test) missed-call pushes to the ntfy app")
    sp.add_argument("action", nargs="?", choices=["setup", "test"], default="setup")
    sp.set_defaults(fn=cmd_push)
    sp = sub.add_parser("say", help="render text to speech, print the audio file's path")
    sp.add_argument("text")
    sp.set_defaults(fn=cmd_say)
    sp = sub.add_parser("voice", aliases=["voices"],
                        help="Claude's voice: show, switch, download, try or remove voices (`phone voice` lists them)")
    sp.add_argument("words", nargs="*", help="<engine:name> | list <engine> [filter] | try <engine:name> | "
                                              "remove <engine:name> | key")
    sp.add_argument("--download", action="store_true", help="download the voice if needed, without asking")
    sp.set_defaults(fn=cmd_voice)
    sp = sub.add_parser("meeting-test", help="preview a meeting alert (whisper and screen)")
    sp.set_defaults(fn=cmd_meeting_test)
    sub.add_parser("meetings", help="upcoming meetings the alerts will use").set_defaults(fn=cmd_meetings)
    sp = sub.add_parser("calendar", help="the calendar feeds for meeting alerts: show, add or remove a link")
    sp.add_argument("action", nargs="?", choices=["add", "remove"])
    sp.add_argument("source", nargs="?", choices=config.CALENDAR_SOURCES)
    sp.set_defaults(fn=cmd_calendar)
    sp = sub.add_parser("config", help="show or change settings: config [get|set|unset] <setting> [value]; "
                        "config render rewrites the FreeSWITCH config; config migrate rewrites old voice settings")
    sp.add_argument("action", nargs="?", choices=["get", "set", "unset", "render", "migrate"])
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="*")
    sp.set_defaults(fn=cmd_config)
    sp = sub.add_parser("change_phone", aliases=["change-phone"],
                        help="which phone Claude's calls ring: desk or softphone (none: show)")
    sp.add_argument("phone", nargs="?", choices=["desk", "softphone"], type=str.lower)
    sp.set_defaults(fn=cmd_change_phone)
    sp = sub.add_parser("setup", help="set claude-phone up: settings, phones, Claude Code wiring (safe to repeat)")
    sp.add_argument("--all", action="store_true", help="ask about everything again, including what you declined")
    sp.set_defaults(fn=cmd_setup)
    sub.add_parser("update", help="pull the newest claude-phone and restart it (not while Claude sessions run)"
                   ).set_defaults(fn=cmd_update)
    a = p.parse_args(argv)
    if a.cmd == "calendar" and a.action and not a.source:
        p.error(f"phone calendar {a.action} needs a source: {' or '.join(config.CALENDAR_SOURCES)}")
    a.fn(a)


if __name__ == "__main__":
    main()
