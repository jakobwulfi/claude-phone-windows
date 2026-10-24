"""`phone update`: the order of the steps, what stops it, and that the services come back whatever happens."""

import subprocess
import sys
from pathlib import Path

import pytest

from claude_phone import procs, registry, updater

OLD, NEW = "a" * 40, "b" * 40


class FakeGit:
    """Answers the git commands the updater uses; `calls` is what was asked, in order."""

    def __init__(self, incoming=2, pyproject_changed=False, fail=None) -> None:
        self.incoming, self.pyproject_changed, self.fail = incoming, pyproject_changed, fail or {}
        self.calls: list[tuple[str, ...]] = []
        self.pulled = False

    def __call__(self, argv, **kwargs):
        args = tuple(argv[3:])  # after: git -C <repo>
        self.calls.append(args)
        verb = args[0]
        if verb in self.fail:
            return subprocess.CompletedProcess(argv, 1, "", self.fail[verb])
        out = {
            "fetch": "",
            "rev-list": str(self.incoming),
            "rev-parse": NEW if self.pulled else OLD,
            "pull": "",
            "diff": "pyproject.toml" if self.pyproject_changed else "",
        }[verb]
        self.pulled = self.pulled or verb == "pull"
        return subprocess.CompletedProcess(argv, 0, out + "\n", "")

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


class World:
    """The services, pip and the editor offer, recording what the updater did to them."""

    def __init__(self, running=True, blockers=None, pip_error=None) -> None:
        self.events: list[str] = []
        self.said: list[str] = []
        self.is_running, self.pip_error = running, pip_error
        self.blockers = blockers or updater.Blockers([], [])

    def updater(self, git, tmp_path) -> updater.Updater:
        def pip(repo):
            self.events.append("pip")
            if self.pip_error:
                raise updater.UpdateError(self.pip_error)
        return updater.Updater(say=self.said.append, repo=tmp_path, run=git, find_blockers=lambda: self.blockers,
                               running=lambda: self.is_running, stop=lambda: self.events.append("stop"),
                               start=lambda: self.events.append("start"), pip=pip,
                               offer_editors=lambda: self.events.append("editors"))

    @property
    def output(self) -> str:
        return "\n".join(self.said)


def test_a_current_clone_is_left_alone(tmp_path):
    git, world = FakeGit(incoming=0), World()

    assert world.updater(git, tmp_path).update() is True

    assert git.verbs() == ["fetch", "rev-list"] and world.events == []
    assert "up to date" in world.output


def test_an_update_with_no_new_libraries_pulls_and_restarts_without_pip(tmp_path):
    git, world = FakeGit(incoming=3), World()

    assert world.updater(git, tmp_path).update() is True

    assert git.verbs() == ["fetch", "rev-list", "rev-parse", "pull", "rev-parse", "diff"]
    assert ("pull", "--ff-only", "--quiet") in git.calls
    assert world.events == ["stop", "start", "editors"] and "Updated aaaaaaa to bbbbbbb" in world.output


def test_new_libraries_are_installed_between_stop_and_start(tmp_path):
    git, world = FakeGit(pyproject_changed=True), World()

    assert world.updater(git, tmp_path).update() is True

    assert world.events == ["stop", "pip", "start", "editors"]


def test_a_failed_pip_still_brings_the_services_back_and_says_how_to_repair(tmp_path):
    git, world = FakeGit(pyproject_changed=True), World(pip_error="pip failed: no network")

    assert world.updater(git, tmp_path).update() is False

    assert world.events == ["stop", "pip", "start"]  # restarted, editors not offered
    assert "pip install -e ." in world.output and "no network" in world.output


def test_services_that_were_not_running_stay_stopped(tmp_path):
    git, world = FakeGit(), World(running=False)

    assert world.updater(git, tmp_path).update() is True

    assert "stop" not in world.events and "start" not in world.events


def test_running_sessions_stop_the_update_before_anything_is_pulled(tmp_path):
    git = FakeGit()
    world = World(blockers=updater.Blockers(["301 claude-phone-windows/main"], [4242, 4343]))

    assert world.updater(git, tmp_path).update() is False

    assert "pull" not in git.verbs() and world.events == []
    assert "301 claude-phone-windows/main" in world.output and "4242, 4343" in world.output


def test_a_branch_without_an_upstream_explains(tmp_path):
    git = FakeGit(fail={"rev-list": "fatal: no upstream configured for branch 'x'"})
    world = World()

    assert world.updater(git, tmp_path).update() is False

    assert "no upstream" in world.output and world.events == []


def test_a_pull_that_cannot_fast_forward_changes_nothing(tmp_path):
    git = FakeGit(fail={"pull": "fatal: Not possible to fast-forward, aborting."})
    world = World()

    assert world.updater(git, tmp_path).update() is False

    assert "fast-forward" in world.output and world.events == []  # nothing stopped or restarted


def test_git_missing_is_reported(tmp_path):
    def no_git(argv, **kwargs):
        raise FileNotFoundError("git")
    world = World()

    assert world.updater(no_git, tmp_path).update() is False
    assert "git didn't run" in world.output


# --- the pieces ---

def test_pip_runs_through_the_venvs_python_not_phone_exe():
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    updater.pip_install(Path("C:/clone"), run=run)

    assert seen[0][:4] == [sys.executable, "-m", "pip", "install"] and seen[0][-2:] == ["-e", str(Path("C:/clone"))]


def test_a_pip_failure_shows_the_end_of_its_output():
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "a\nb\nERROR: no matching distribution")

    with pytest.raises(updater.UpdateError, match="no matching distribution"):
        updater.pip_install(Path("C:/clone"), run=run)


def test_only_mcp_servers_of_phone_exe_count_as_running_sessions(monkeypatch):
    class Process:
        def __init__(self, pid, cmdline):
            self.info = {"pid": pid, "cmdline": cmdline}
    monkeypatch.setattr(updater.psutil, "process_iter", lambda attrs: [
        Process(10, [r"C:\c\.venv\Scripts\phone.exe", "mcp"]),
        Process(11, [r"C:\py\python.exe", r"C:\c\.venv\Scripts\PHONE.EXE", "mcp"]),
        Process(12, [r"C:\c\.venv\Scripts\phone.exe", "update"]),  # this very command
        Process(13, [r"C:\c\.venv\Scripts\phone.exe", "daemon"]),
        Process(14, ["node.exe", "mcp"]),
        Process(15, None),
    ])

    assert updater.mcp_processes() == [10, 11]


def test_registry_sessions_only_count_while_the_daemon_keeps_them_clean(monkeypatch):
    rows = [registry.Session("s1", 301, "claude-phone-windows", "main", None, None, None, "idle", 0.0, 0.0),
            registry.Session("s2", 302, "other", None, None, None, None, "working", 0.0, 0.0)]
    monkeypatch.setattr(registry, "all_sessions", lambda: rows)

    monkeypatch.setattr(procs, "pid", lambda name: None)
    assert updater.live_sessions() == []  # daemon down: the rows may be stale

    monkeypatch.setattr(procs, "pid", lambda name: 123)
    assert updater.live_sessions() == ["301 claude-phone-windows/main", "302 other"]
