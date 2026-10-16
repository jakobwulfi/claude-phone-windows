"""`phone` command line."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import psutil

from . import config, keychain, registry

RUN = config.HOME / "run"
# Services outlive the console that ran `phone up`.
# A hidden console, not none (DETACHED_PROCESS): the venv's python.exe is a launcher, and the real interpreter it
# starts would open a console window of its own, where closing it kills the daemon.
DETACHED = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
STOP_TIMEOUT_S = 5
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{}"


# --- FreeSWITCH ---

# Media ports for calls. scripts/firewall.ps1 opens these (UDP) and sip_port for a desk phone on the LAN.
RTP_START, RTP_END = 16384, 16484


def render_freeswitch_config() -> None:
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


def freeswitch_api(cmd: str) -> str:
    """One FreeSWITCH command, for the command line."""
    from . import esl
    return esl.api_sync(cmd, config.load().esl_port, keychain.get_or_create("esl"))


# --- processes ---

def _pid(name: str) -> int | None:
    f = RUN / f"{name}.pid"
    try:
        pid = int(f.read_text())
    except (OSError, ValueError):
        return None
    # Not os.kill(pid, 0): on Windows that terminates the process instead of probing it.
    return pid if psutil.pid_exists(pid) else None


@dataclass(frozen=True)
class Service:
    argv: Callable[[config.Settings], list[str]]
    # A Windows priority class, set as the process starts. HIGH needs no admin rights (only REALTIME does).
    priority: int = 0
    env: dict[str, str] = field(default_factory=dict)  # added to the environment


def _spawn(name: str, svc: Service, s: config.Settings) -> None:
    if _pid(name):
        print(f"{name}: already running")
        return
    RUN.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **svc.env} if svc.env else None
    # The child keeps its own handle to the output file.
    with open(config.LOGS / f"{name}.out", "a") as out:
        p = subprocess.Popen(svc.argv(s), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             creationflags=DETACHED | svc.priority, env=env)
    (RUN / f"{name}.pid").write_text(str(p.pid))
    print(f"{name}: started (pid {p.pid})")


def _stop(name: str) -> None:
    pid = _pid(name)
    if not pid:
        print(f"{name}: not running")
        return
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=STOP_TIMEOUT_S)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        pass
    (RUN / f"{name}.pid").unlink(missing_ok=True)
    print(f"{name}: stopped")


def model_path() -> Path:
    return config.MODELS / config.load().whisper_model


def ensure_model() -> None:
    p = model_path()
    if not p.exists():
        print(f"downloading {p.name} ...")
        config.MODELS.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(MODEL_URL.format(p.name), p.with_suffix(".part"))
        p.with_suffix(".part").replace(p)


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


def cmd_up(a) -> None:
    config.ensure_dirs()
    s = config.load()
    render_freeswitch_config()
    ensure_model()
    names = [a.only] if a.only else list(SERVICES)
    for n in names:
        _spawn(n, SERVICES[n], s)
        if n == "freeswitch":
            time.sleep(3)
        if n == "whisper":
            time.sleep(2)


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


def cmd_status(a) -> None:
    w = config.ip_warning()
    if w:
        print("WARNING:", w)
    for n in SERVICES:
        pid = _pid(n)
        print(f"{n:9} {'running pid ' + str(pid) if pid else 'stopped'}")
    print(f"{'audio':9} {audio_usage()}")
    try:
        with urllib.request.urlopen(f"{config.load().daemon_url}/health", timeout=3) as r:
            print("health   ", json.loads(r.read()))
    except Exception as e:
        print("health    unavailable:", e)
    if _pid("freeswitch"):
        # Ask the SIP profile: `show registrations` reads a table that survives FreeSWITCH restarts.
        s = config.load()
        for ext in dict.fromkeys((s.ring_ext, s.desk_ext, s.softphone_ext)):
            try:
                contact = freeswitch_api(f"sofia_contact internal/{ext}@{s.lan_ip}").strip()
            except Exception as e:
                contact = f"unavailable: {e}"
            state = "not registered" if contact.startswith("error/") else contact
            print(f"phone {ext:4} {state}{'  (rings)' if ext == s.ring_ext else ''}")


def cmd_list(a) -> None:
    rows = registry.all_sessions()
    if not rows:
        print("No live sessions.")
        return
    print(f"{'EXT':4} {'REPO':22} {'BRANCH':28} {'STATUS':8} {'RING':5} SINCE")
    for s in rows:
        b = s.branch or ""
        if s.worktree:
            b = f"{b} [{s.worktree}]"
        since = datetime.fromtimestamp(s.started_at).strftime("%H:%M")
        ring = config.load().repo_ringtones.get(s.repo) or registry.ringtone(s.repo)
        print(f"{s.ext:<4} {s.repo[:22]:22} {b[:28]:28} {s.status:8} {ring:<5} {since}")


def cmd_type(a) -> None:
    """Type text into a session as if it came from the phone, to test the VS Code bridge without a call."""
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
    print(keychain.get_or_create(a.name))


def cmd_hold_music(a) -> None:
    from . import hold
    print(hold.generate(a.prompt or hold.DEFAULT_PROMPT, a.seconds))


def cmd_push(a) -> None:
    from . import push
    if a.action == "test":
        print("sent" if push.send("Claude Phone", "Test push: missed calls will show up here.") else "failed (see phone.log)")
        return
    t = push.create_topic()
    print(f"In the ntfy app, subscribe to this topic on {config.load().ntfy_server}:\n\n  {t}\n\n"
          "Treat it like a password. Then run: phone push test")


def cmd_meeting_test(a) -> None:
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
    from . import diary
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
    from . import diary
    print(f"Paste the {source.capitalize()} calendar's secret ICS link (it won't be shown), then press Enter.")
    raw = getpass.getpass("link: ") if sys.stdin.isatty() else sys.stdin.readline()
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
    if a.action in ("get", "set", "unset") and not a.key:
        sys.exit(f"Usage: phone config {a.action} <setting>" + (" <value>" if a.action == "set" else ""))
    if a.action == "set" and not a.value:
        sys.exit("Usage: phone config set <setting> <value>")
    if (a.action != "set" and a.value) or (not a.action and a.key):
        sys.exit("Usage: phone config [get <setting> | set <setting> <value> | unset <setting> | render]")
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


def cmd_change_phone(a) -> None:
    from . import configedit
    try:
        print(configedit.change_phone(a.phone))
    except ValueError as e:
        sys.exit(f"Not changed: {e}")


def cmd_say(a) -> None:
    from . import tts
    print(tts.render(a.text))


def cmd_voices(a) -> None:
    from . import tts
    for line in tts.sapi_voices():
        print(line)


def main(argv: list[str] | None = None) -> None:
    # Windows error messages come in the system language (æøå), which a console's code page may not cover.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
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
    sp.add_argument("event", choices=["session-start", "session-end", "prompt", "stop"])
    sp.set_defaults(fn=cmd_hook)
    sub.add_parser("daemon").set_defaults(fn=cmd_daemon)
    sub.add_parser("mcp").set_defaults(fn=cmd_mcp)
    sp = sub.add_parser("secret", help="print (creating if needed) a secret: sip (the phones' password) | esl")
    sp.add_argument("name", choices=["sip", "esl"])
    sp.set_defaults(fn=cmd_secret)
    sp = sub.add_parser("hold-music", help="generate new hold music with ElevenLabs Music")
    sp.add_argument("prompt", nargs="?")
    sp.add_argument("--seconds", type=int, default=60)
    sp.set_defaults(fn=cmd_hold_music)
    sp = sub.add_parser("push", help="set up (or test) missed-call pushes to the ntfy app")
    sp.add_argument("action", nargs="?", choices=["setup", "test"], default="setup")
    sp.set_defaults(fn=cmd_push)
    sp = sub.add_parser("say", help="render text to speech, print the audio file's path")
    sp.add_argument("text")
    sp.set_defaults(fn=cmd_say)
    sub.add_parser("voices", help="list the installed Windows voices").set_defaults(fn=cmd_voices)
    sub.add_parser("meeting-test", help="preview a meeting alert (whisper and screen)").set_defaults(fn=cmd_meeting_test)
    sub.add_parser("meetings", help="upcoming meetings the alerts will use").set_defaults(fn=cmd_meetings)
    from . import diary
    sp = sub.add_parser("calendar", help="the calendar feeds for meeting alerts: show, add or remove a link")
    sp.add_argument("action", nargs="?", choices=["add", "remove"])
    sp.add_argument("source", nargs="?", choices=diary.SOURCES)
    sp.set_defaults(fn=cmd_calendar)
    sp = sub.add_parser("config", help="show or change settings: config [get|set|unset] <setting> [value]; "
                        "config render rewrites the FreeSWITCH config")
    sp.add_argument("action", nargs="?", choices=["get", "set", "unset", "render"])
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="*")
    sp.set_defaults(fn=cmd_config)
    sp = sub.add_parser("change_phone", aliases=["change-phone"],
                        help="which phone Claude's calls ring: desk or softphone (none: show)")
    sp.add_argument("phone", nargs="?", choices=["desk", "softphone"], type=str.lower)
    sp.set_defaults(fn=cmd_change_phone)
    a = p.parse_args(argv)
    if a.cmd == "calendar" and a.action and not a.source:
        p.error(f"phone calendar {a.action} needs a source: {' or '.join(diary.SOURCES)}")
    a.fn(a)


if __name__ == "__main__":
    main()
