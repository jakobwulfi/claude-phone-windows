"""`phone` command line."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import psutil

from . import config, keychain, registry

RUN = config.HOME / "run"
# Services outlive the console that ran `phone up`.
DETACHED = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
STOP_TIMEOUT_S = 5
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{}"


# --- FreeSWITCH ---

# Media ports for calls; open these (UDP) and 5060 in Windows Firewall for a desk phone on the LAN.
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


def _spawn(name: str, argv: list[str]) -> None:
    if _pid(name):
        print(f"{name}: already running")
        return
    RUN.mkdir(parents=True, exist_ok=True)
    out = open(config.LOGS / f"{name}.out", "a")
    p = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=DETACHED)
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

SERVICES = {
    "freeswitch": freeswitch_argv,
    "whisper": lambda s: [whisper_server(s), "-m", str(model_path()), "--host", "127.0.0.1", "--port", str(s.whisper_port),
                          "-t", str(WHISPER_THREADS), "--no-timestamps", "-l", s.whisper_language],
    "daemon": lambda s: py_argv("daemon"),
}


def cmd_up(a) -> None:
    config.ensure_dirs()
    s = config.load()
    render_freeswitch_config()
    ensure_model()
    names = [a.only] if a.only else list(SERVICES)
    for n in names:
        _spawn(n, SERVICES[n](s))
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


def cmd_status(a) -> None:
    w = config.ip_warning()
    if w:
        print("WARNING:", w)
    for n in SERVICES:
        pid = _pid(n)
        print(f"{n:9} {'running pid ' + str(pid) if pid else 'stopped'}")
    try:
        with urllib.request.urlopen(f"{config.load().daemon_url}/health", timeout=3) as r:
            print("health   ", json.loads(r.read()))
    except Exception as e:
        print("health    unavailable:", e)
    if _pid("freeswitch"):
        try:
            print(freeswitch_api("show registrations").strip())
        except Exception as e:
            print("registrations unavailable:", e)


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
    s = config.load()
    if not diary.access():
        print("Meeting alerts have no calendar source on Windows yet.")
        return
    print("calendars:", ", ".join(diary.calendars()))
    print("used:", ", ".join(s.meeting_calendars) or "all")
    now = datetime.now().astimezone()
    for m in diary.meetings(now, now + timedelta(days=2), s.meeting_calendars or None):
        title, text = diary.summary(m)
        print(f"{m.start:%a} {title} ({text.replace(chr(10), '; ')})")


def cmd_say(a) -> None:
    from . import tts
    print(tts.render(a.text))


def cmd_voices(a) -> None:
    from . import tts
    for line in tts.sapi_voices():
        print(line)


def main() -> None:
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
    sub.add_parser("config", help="regenerate FreeSWITCH config").set_defaults(fn=lambda a: render_freeswitch_config())
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
