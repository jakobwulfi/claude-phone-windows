"""Background processes: the services `phone up` starts, and `phone prerender`. Each has its PID in
RUN/<name>.pid."""

from __future__ import annotations

import subprocess
from pathlib import Path

import psutil

from . import config

RUN = config.HOME / "run"
# Services outlive the console that ran `phone up`.
# A hidden console, not none (DETACHED_PROCESS): the venv's python.exe is a launcher, and the real interpreter it
# starts would open a console window of its own, where closing it kills the daemon.
DETACHED = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
STOP_TIMEOUT_S = 5
# A pid file outlives a reboot, and Windows soon gives its PID to another program. So the PID is the service only if
# that process started before the file was written (seen: Spotify had the daemon's PID after a restart).
PID_FILE_SLACK_S = 2


def pid(name: str) -> int | None:
    """The PID of the running process, or None."""
    f = RUN / f"{name}.pid"
    try:
        p = int(f.read_text())
        written = f.stat().st_mtime
        # Not os.kill(pid, 0): on Windows that terminates the process instead of probing it.
        started = psutil.Process(p).create_time()
    except (OSError, ValueError, psutil.Error):
        return None
    return p if started <= written + PID_FILE_SLACK_S else None


def spawn(name: str, argv: list[str], log: Path | None, priority: int = 0, env: dict[str, str] | None = None) -> int:
    """Starts argv in the background and keeps its PID. Its output is appended to log (None: dropped). priority: a
    Windows priority class, set as it starts. env: the whole environment (None: this one)."""
    RUN.mkdir(parents=True, exist_ok=True)
    if log:
        with open(log, "a") as out:  # the child keeps its own handle
            p = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 creationflags=DETACHED | priority, env=env)
    else:
        p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             creationflags=DETACHED | priority, env=env)
    (RUN / f"{name}.pid").write_text(str(p.pid))
    return p.pid


def stop(name: str) -> bool:
    """Asks the process to end and waits a moment for it. False: it wasn't running."""
    p = pid(name)
    if p:
        try:
            proc = psutil.Process(p)
            proc.terminate()
            proc.wait(timeout=STOP_TIMEOUT_S)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass
    (RUN / f"{name}.pid").unlink(missing_ok=True)  # also one left by a crash or a reboot
    return bool(p)


def kill(name: str) -> None:
    """Ends the process at once, if it's running, without waiting for it."""
    p = pid(name)
    if p:
        try:
            psutil.Process(p).kill()
        except psutil.Error:  # gone already, or not ours to end
            pass
