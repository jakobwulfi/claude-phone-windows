"""`phone update`: bring this clone up to date and restart what was running.

It refuses while Claude sessions run (their MCP servers hold phone.exe open, and a pip run over it breaks `phone` and
every hook), so it checks first and pulls second. An editable install picks up new code by itself, so pip only runs
when pyproject.toml changed (new libraries, entry points), and then through the venv's own python rather than through
phone.exe. Everything outside this module (stopping and starting the services, git, pip) is a parameter, so the tests
run the sequence without touching a real checkout."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

from . import config

GIT_TIMEOUT_S = 120
PIP_TIMEOUT_S = 900


class UpdateError(RuntimeError):
    """A step that can't go on; the message says what to do."""


@dataclass(frozen=True)
class Blockers:
    sessions: list[str]  # "301 repo/branch"
    processes: list[int]  # pids of `phone.exe mcp`

    def __bool__(self) -> bool:
        return bool(self.sessions or self.processes)


def mcp_processes() -> list[int]:
    """Every Claude session that has claude-phone's MCP server holds phone.exe open through one of these."""
    found = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        command = process.info["cmdline"] or []
        if len(command) >= 2 and command[-1] == "mcp" and any(Path(c).name.lower() == "phone.exe"
                                                              for c in command[:-1]):
            found.append(process.info["pid"])
    return sorted(found)


def live_sessions() -> list[str]:
    """Sessions the registry lists, if the daemon runs to keep that list clean (without it, rows go stale)."""
    from . import procs, registry
    if not procs.pid("daemon"):
        return []
    return [f"{s.ext} {s.repo}" + (f"/{s.branch}" if s.branch else "") for s in registry.all_sessions()]


def find_blockers() -> Blockers:
    return Blockers(live_sessions(), mcp_processes())


def git(repo: Path, *args: str, run: Callable = subprocess.run) -> str:
    try:
        done = run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        raise UpdateError(f"git didn't run: {e}") from e
    if done.returncode != 0:
        raise UpdateError(f"`git {' '.join(args)}` failed: {(done.stderr or done.stdout).strip()}")
    return (done.stdout or "").strip()


def pip_install(repo: Path, run: Callable = subprocess.run) -> None:
    """The project again, through this venv's python: phone.exe may be the one thing that runs right now."""
    try:
        done = run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-e", str(repo)],
                   capture_output=True, text=True, timeout=PIP_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        raise UpdateError(f"pip didn't run: {e}") from e
    if done.returncode != 0:
        tail = " / ".join((done.stderr or done.stdout or "").strip().splitlines()[-4:])
        raise UpdateError(f"pip failed: {tail}")


@dataclass
class Updater:
    say: Callable[[str], None] = print
    repo: Path = config.REPO
    run: Callable = subprocess.run
    find_blockers: Callable[[], Blockers] = find_blockers
    running: Callable[[], bool] = lambda: False
    stop: Callable[[], None] = lambda: None
    start: Callable[[], None] = lambda: None
    pip: Callable[[Path], None] = pip_install
    offer_editors: Callable[[], None] = lambda: None

    def incoming(self) -> int:
        """How many commits the branch's upstream has that this clone doesn't."""
        git(self.repo, "fetch", "--quiet", run=self.run)
        try:
            return int(git(self.repo, "rev-list", "--count", "HEAD..@{upstream}", run=self.run))
        except ValueError as e:
            raise UpdateError("git gave no commit count") from e
        except UpdateError as e:
            raise UpdateError("this branch has no upstream to update from (`git branch --set-upstream-to=origin/main`, "
                              f"or switch to main). {e}") from e

    def update(self) -> bool:
        """True when this clone is current at the end."""
        try:
            return self._update()
        except UpdateError as e:
            self.say(f"Not updated: {e}")
            return False

    def _update(self) -> bool:
        self.say("Looking for a newer version ...")
        count = self.incoming()
        if count == 0:
            self.say("claude-phone is up to date.")
            return True
        self.say(f"{count} new commit{'s' if count != 1 else ''}.")
        blockers = self.find_blockers()
        if blockers:
            self.say("Not updated: close the Claude sessions first, then run `phone update` again.")
            if blockers.sessions:
                self.say("  sessions in the list: " + ", ".join(blockers.sessions))
            if blockers.processes:
                self.say("  running `phone.exe mcp`: pids " + ", ".join(map(str, blockers.processes)))
            return False
        old = git(self.repo, "rev-parse", "HEAD", run=self.run)
        git(self.repo, "pull", "--ff-only", "--quiet", run=self.run)
        new = git(self.repo, "rev-parse", "HEAD", run=self.run)
        dependencies_changed = bool(git(self.repo, "diff", "--name-only", old, new, "--", "pyproject.toml",
                                        run=self.run))
        was_running = self.running()
        if was_running:
            self.stop()
        try:
            if dependencies_changed:
                self.say("pyproject.toml changed: installing the new libraries ...")
                self.pip(self.repo)
        except UpdateError as e:
            self.say(f"The code is updated but the install step failed ({e}). With every Claude session closed, run "
                     r".venv\Scripts\python -m pip install -e . in the clone.")
            return False
        finally:
            if was_running:
                self.start()
        self.say(f"Updated {old[:7]} to {new[:7]}.")
        self.offer_editors()
        return True
