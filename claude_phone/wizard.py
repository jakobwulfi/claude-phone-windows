"""`phone setup`: looks at what is really set up, shows it as a table, and asks about only what is missing.

Nothing is remembered but declines (~/.claude-phone/setup.json), so a second run on a finished install asks nothing
and `--all` asks everything again. Questions are plain `input()` prompts; `ask` and `say` are parameters so the tests
can script the answers. The steps that write files reuse the code behind `phone config`, `phone change_phone` and
claudewire, so setup and the commands can't drift apart."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import claudewire, config, configedit, editors, tools

DONE, MISSING, DECLINED = "done", "missing", "declined"
LINPHONE_SIP_PORT = 5070  # Linphone on the same PC keeps UDP 5060 for itself (docs/build-notes.md)
# Asked in this order, from the same list `phone config` uses. Meeting alerts and their calendars are an extra.
WIZARD_SETTINGS = ("owner_name", "ring_seconds", "autostart", "whisper_text", "callerid_max")
FIREWALL_RULE = "claude-phone SIP"
END_NOTES = (
    "Good to know:",
    "- Run claude-phone and your terminals at the same level: a terminal started as administrator can't be typed "
    "into by a daemon that isn't (never run the daemon elevated).",
    "- Not covered: Git Bash's own window, WSL, SSH and `claude -p`. Windows Terminal should work (same console "
    "route as PowerShell) but hasn't been tried.",
    "- A session that isn't listed in `phone list`: start `claude` again so the hooks register it.",
)


def setup_file() -> Path:
    return config.HOME / "setup.json"


def load_declines() -> set[str]:
    try:
        data = json.loads(setup_file().read_text(encoding="utf-8"))
        return {str(x) for x in data.get("declined", [])}
    except (OSError, ValueError, AttributeError):
        return set()


def save_declines(declined: set[str]) -> None:
    setup_file().parent.mkdir(parents=True, exist_ok=True)
    setup_file().write_text(json.dumps({"declined": sorted(declined)}, indent=2) + "\n", encoding="utf-8")


def phone_exe() -> Path:
    return Path(sys.executable).with_name("phone.exe")


def config_keys() -> set[str]:
    try:
        return set(tomllib.loads(config.CONFIG_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def firewall_open() -> bool:
    """Is claude-phone's firewall rule there (scripts/firewall.ps1 made it)? Reading rules needs no admin."""
    command = (f"if (Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -ErrorAction SilentlyContinue) "
               "{ exit 0 } exit 1")
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_firewall_script(script: Path) -> bool:
    """scripts/firewall.ps1 in an elevated window: one UAC prompt. True when the rule exists afterwards."""
    tools.run_elevated("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)])
    return firewall_open()


@dataclass(frozen=True)
class Part:
    """One thing setup can do: `check` says whether it is done, `run` does it (True when done afterwards)."""
    name: str  # also the key in setup.json
    label: str
    check: Callable[[], bool]
    run: Callable[[], bool]
    note: Callable[[], str] = lambda: ""  # a few words after the label in the table


class Wizard:
    def __init__(self, ask: Callable[[str], str] = input, say: Callable[[str], None] = print, *,
                 exe: Path | None = None, repo: Path | None = None, redo_all: bool = False,
                 firewall: Callable[[Path], bool] = run_firewall_script,
                 start: Callable[[Path], None] | None = None) -> None:
        self._ask, self.say = ask, say
        self.exe = exe or phone_exe()
        self.repo = repo or config.REPO
        self.redo_all = redo_all
        self.firewall = firewall
        self.start = start or self._start_services
        self.declined = set() if redo_all else load_declines()
        self._editor_states: dict[str, tuple[str, bool]] = {}  # asking VS Code takes a second or two
        self.parts = self._parts()

    # --- asking ---

    def text(self, question: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        return self._ask(f"{question}{suffix} ").strip() or default

    def yes(self, question: str, default: bool = True) -> bool:
        while True:
            answer = self._ask(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
            if not answer:
                return default
            if answer[0] in "yn":
                return answer[0] == "y"
            self.say("Please answer y or n.")

    def choice(self, question: str, options: dict[str, str], default: str) -> str:
        shown = ", ".join(f"{key} = {label}" for key, label in options.items())
        while True:
            answer = self.text(f"{question} ({shown})", default).lower()
            if answer in options:
                return answer
            self.say(f"Please answer one of: {', '.join(options)}.")

    # --- the parts, in the order setup does them ---

    def _parts(self) -> list[Part]:
        return [
            Part("settings", "your settings", self._settings_done, self._run_settings),
            Part("phones", "which phone rings", lambda: "ring_ext" in config_keys(), self._run_phones),
            Part("freeswitch", "FreeSWITCH", lambda: Path(config.load().freeswitch_exe).exists(),
                 self._run_freeswitch),
            Part("whisper", "whisper-server", lambda: self._whisper_path() is not None, self._run_whisper),
            Part("model", "speech model", self._model_done, self._run_model),
            Part("ffmpeg", "ffmpeg (audio conversion)", tools.ffmpeg_on_path, self._run_ffmpeg),
            Part("sip-password", "the SIP password", self._sip_done, self._run_sip),
            Part("hooks", "Claude Code hooks", self._hooks_done, self._run_hooks),
            Part("mcp", "Claude Code MCP server", lambda: claudewire.mcp_state(self.exe) == "ok", self._run_mcp),
            Part("command", "/claude-phone command", self._command_done, self._run_command),
            Part("claude-md", "CLAUDE.md block", self._claude_md_done, self._run_claude_md),
            Part("vscode-extension", "VS Code extension", lambda: self._editor_state("vscode")[1],
                 self._run_vscode, lambda: self._editor_state("vscode")[0]),
            Part("visualstudio-extension", "Visual Studio extension", lambda: self._editor_state("vs")[1],
                 self._run_visual_studio, lambda: self._editor_state("vs")[0]),
        ]

    def state(self, part: Part) -> str:
        if not self.redo_all and part.check():
            return DONE
        return DECLINED if part.name in self.declined else MISSING

    def table(self) -> list[tuple[Part, str]]:
        return [(part, self.state(part)) for part in self.parts]

    def show_table(self) -> None:
        self.say("What is set up:")
        for part, state in self.table():
            note = part.note()
            self.say(f"  {state:9} {part.label}" + (f" ({note})" if note else ""))

    def run(self) -> None:
        """Show the table, then do what is missing; declined parts only with --all."""
        self.show_table()
        todo = [part for part, state in self.table() if state == MISSING]
        for part in todo:
            self.say("")
            try:
                done = part.run()
            except OSError as e:
                self.say(f"Skipped {part.label}: {e}")
                continue
            if done:
                self.declined.discard(part.name)
            else:
                self.declined.add(part.name)
        save_declines(self.declined)
        if not todo:
            self.say("\nNothing left to ask. `phone setup --all` asks about everything again, declined parts too.")
        self._finish()

    def _finish(self) -> None:
        if self._services_running():
            self.say("\nclaude-phone is running. Dial 600 for the echo test, or call 301 from a session.")
        elif self.yes("\nStart claude-phone now?"):
            self.start(self.exe)
        self.say("")
        for line in END_NOTES:
            self.say(line)

    # --- 1. settings ---

    def _settings_done(self) -> bool:
        return "owner_name" in config_keys()

    def _run_settings(self) -> bool:
        self.say("Settings. Enter keeps what's shown; `phone config` changes any of them later.")
        for key in WIZARD_SETTINGS:
            current = getattr(config.load(), key)
            shown = ("on" if current else "off") if isinstance(current, bool) else str(current)
            self.say(f"{key}: {configedit.described(key)}")
            while True:
                raw = self.text(f"  {key}", shown)
                if raw == shown and key != "owner_name":
                    break
                try:
                    self.say("  " + configedit.set_value(key, raw))
                    break
                except ValueError as e:
                    self.say(f"  Not changed: {e}")
        config.forget()
        return self._settings_done()

    # --- 2. phones ---

    def _run_phones(self) -> bool:
        s = config.load()
        self.say("Which phone will ring? Linphone runs on this PC; the desk phone is a Yealink T31P (or similar) "
                 "on your network.")
        kind = self.choice("Phone", {"l": "Linphone", "d": "desk phone", "b": "both"}, "l")
        ring = s.softphone_ext if kind == "l" else s.desk_ext
        if kind == "b":
            first = self.choice("Which one rings for Claude's calls", {"d": "desk", "l": "Linphone"}, "d")
            ring = s.desk_ext if first == "d" else s.softphone_ext
        if kind in ("l", "b"):
            configedit.write_setting("sip_port", LINPHONE_SIP_PORT)
            self._linphone_help(s)
        if kind in ("d", "b"):
            self._desk_phone_help(s)
        configedit.write_setting("ring_ext", ring)
        config.forget()
        self.say(f"Claude's calls ring extension {ring}.")
        return True

    def _linphone_help(self, s: config.Settings) -> None:
        self.say(f"Linphone: add a third-party SIP account with username {s.softphone_ext}, domain "
                 f"<this PC's address>:{LINPHONE_SIP_PORT} (`phone status` shows the address), transport UDP. "
                 f"sip_port is now {LINPHONE_SIP_PORT}, because Linphone keeps 5060 for itself.")
        if self.yes("Show the SIP password for it here? (also: `phone secret sip`)", default=False):
            from . import keychain
            self.say(f"Password: {keychain.get_or_create('sip')}")

    def _desk_phone_help(self, s: config.Settings) -> None:
        self.say(f"Desk phone: this PC's address is {s.lan_ip or 'not detected'}. Give the PC a DHCP reservation on "
                 "your router, and if the address is wrong or missing, enter it now (Enter keeps it).")
        address = self.text("  lan_ip", s.lan_ip)
        if address and address != s.lan_ip:
            configedit.write_setting("lan_ip", address)
        if firewall_open():
            self.say("The firewall is open for the phone already.")
        elif self.yes("Open the firewall for the phone (one administrator prompt)?"):
            if self.firewall(self.repo / "scripts" / "firewall.ps1"):
                self.say("Firewall rules added.")
            else:
                self.say(r"The firewall rules aren't there. Run scripts\firewall.ps1 from an administrator PowerShell.")
        self.say("Then set the phone itself up: docs/t31p-setup.md.")

    # --- 3. FreeSWITCH, whisper-server and its speech model: found, installed, or linked by path ---

    def progress(self) -> tools.Progress:
        """A download's progress: says each tenth once, which reads the same in a terminal and in a log."""
        last = [-1]

        def show(done: int, total: int) -> None:
            tenth = done * 10 // total if total else -1
            if tenth != last[0] and tenth >= 0:
                last[0] = tenth
                self.say(f"  {tenth * 10}%")
        return show

    def _save_path(self, key: str, found: Path) -> None:
        if str(found) != getattr(config.load(), key):
            configedit.write_setting(key, str(found))
            config.forget()

    def _run_freeswitch(self) -> bool:
        found = tools.find_freeswitch()
        if found:
            self._save_path("freeswitch_exe", found)
            self.say(f"Found FreeSWITCH: {found}")
            return True
        if self.yes("FreeSWITCH (the phone exchange) isn't installed. Download and install it now? About 160 MB, "
                    "and Windows asks once for administrator rights."):
            installed = self._install_freeswitch()
            if installed:
                self._save_path("freeswitch_exe", installed)
                self.say(f"FreeSWITCH is installed: {installed}")
                return True
        path = self.text("Path of FreeSwitchConsole.exe, if it is installed somewhere else (Enter to skip)")
        if not path:
            self.say("Run `phone setup` again once FreeSWITCH is installed.")
            return False
        return self._link("freeswitch_exe", path, tools.FREESWITCH_EXE)

    def _install_freeswitch(self) -> Path | None:
        msi = None
        try:
            latest = tools.latest_freeswitch()
            self.say(f"Downloading {latest.name} ...")
            msi = tools.download(latest.url, config.HOME / "downloads" / latest.name, progress=self.progress())
            if not tools.is_signed_by(msi):
                self.say(f"{latest.name} isn't signed by {tools.FREESWITCH_SIGNER}, so it isn't installed.")
                return None
            self.say("Installing: accept the administrator prompt ...")
            installed = tools.install_freeswitch(msi)
        except (OSError, tools.DownloadError, ValueError) as e:
            self.say(f"Couldn't install FreeSWITCH: {e}")
            return None
        finally:
            if msi:
                msi.unlink(missing_ok=True)
        if not installed:
            self.say("The FreeSWITCH installer didn't finish (cancelled, or it failed).")
        return installed

    def _whisper_path(self) -> str | None:
        configured = config.load().whisper_server
        if configured:
            return configured if Path(configured).exists() else None
        return shutil.which("whisper-server")

    def _run_whisper(self) -> bool:
        found = tools.find_whisper()
        if found:
            self._save_path("whisper_server", found)
            self.say(f"Found whisper-server: {found}")
            return True
        if self.yes("whisper-server (speech to text) isn't installed. Download whisper.cpp now? About 9 MB, "
                    "unpacked into ~/.claude-phone/engines."):
            installed = self._install_whisper()
            if installed:
                self._save_path("whisper_server", installed)
                self.say(f"whisper-server is installed: {installed}")
                return True
        path = self.text("Path of whisper-server.exe, if you have it (Enter to skip)")
        if not path:
            self.say("Run `phone setup` again once whisper-server is installed.")
            return False
        return self._link("whisper_server", path, tools.WHISPER_EXE)

    def _install_whisper(self) -> Path | None:
        archive = None
        try:
            latest = tools.latest_whisper()
            self.say(f"Downloading {latest.name} ...")
            archive = tools.download(latest.url, config.HOME / "downloads" / latest.name, sha256=latest.sha256,
                                     progress=self.progress())
            return tools.install_whisper(archive)
        except (OSError, tools.DownloadError, ValueError) as e:
            self.say(f"Couldn't install whisper-server: {e}")
            return None
        finally:
            if archive:
                archive.unlink(missing_ok=True)

    def _run_ffmpeg(self) -> bool:
        found = tools.find_ffmpeg()
        if not found:
            if not self.yes("ffmpeg (converts the call audio) isn't installed. Install it with winget (about 100 MB)?"):
                self.say("Install it yourself (`winget install Gyan.FFmpeg`) and open a new terminal: calls need it.")
                return False
            self.say("Installing ffmpeg with winget ...")
            if not tools.install_ffmpeg():
                self.say("winget couldn't install ffmpeg. Is winget installed? Run `winget install Gyan.FFmpeg` "
                         "yourself and open a new terminal.")
                return False
            found = tools.find_ffmpeg()
        if not found:
            self.say("ffmpeg is installed but this window can't find it yet. Open a new terminal and run "
                     "`phone setup` again.")
            return False
        if not tools.ffmpeg_on_path():
            # The services this run starts inherit this PATH; every new terminal has it from winget already.
            os.environ["PATH"] = f"{found.parent}{os.pathsep}{os.environ.get('PATH', '')}"
            self.say(f"ffmpeg is at {found}. New terminals will find it; this one does from now on.")
        return True

    def _model_done(self) -> bool:
        from . import cli
        return cli.model_path().exists()

    def _run_model(self) -> bool:
        from . import cli
        if not self.yes(f"Download whisper's speech model ({cli.model_path().name}, about 190 MB, once)?"):
            return False
        try:
            cli.ensure_model(progress=self.progress())
        except (OSError, tools.DownloadError) as e:
            self.say(f"Couldn't download the model: {e}. `phone up` tries again.")
            return False
        return True

    def _link(self, key: str, path: str, expected: str) -> bool:
        path = path.strip().strip('"')
        if not Path(path).is_file():
            self.say(f"No file at {path}.")
            return False
        if Path(path).name.lower() != expected.lower():
            self.say(f"That is {Path(path).name}, not {expected}.")
            return False
        configedit.write_setting(key, path)
        config.forget()
        self.say(f"Saved {key}.")
        return True

    # --- 4. SIP password ---

    def _sip_done(self) -> bool:
        from . import keychain
        return bool(keychain.get("sip"))

    def _run_sip(self) -> bool:
        from . import keychain
        keychain.get_or_create("sip")
        self.say("Made the SIP password and stored it in Windows Credential Manager (`phone secret sip` shows it).")
        return True

    # --- 5. Claude Code ---

    def _hooks_done(self) -> bool:
        try:
            return not claudewire.missing_hooks() and not claudewire.weak_matcher()
        except claudewire.SettingsError:
            return False

    def _run_hooks(self) -> bool:
        try:
            current = claudewire.read_settings()
        except claudewire.SettingsError as e:
            self.say(f"Claude Code hooks: {e}")
            return False
        self.say("Claude Code hooks keep claude-phone's list of live sessions current (start, end, prompt, stop) "
                 "and let Claude phone you without asking each time.")
        plan_mode = self.yes("Also let Claude enter plan mode without a terminal prompt, but only when your "
                             "latest prompt came by phone?")
        change = claudewire.plan_settings(current, self.exe, plan_mode=plan_mode)
        if not change.notes:
            self.say("The hooks are in settings.json already.")
            return True
        self.say(f"This changes {claudewire.settings_file()} (the old file is kept as settings.json.bak):")
        for note in change.notes:
            self.say(f"  {note}")
        if not self.yes("Write it?"):
            return False
        claudewire.apply_settings(change)
        self.say("Done. Claude Code sessions started from now on use the hooks.")
        return True

    def _run_mcp(self) -> bool:
        if not self.yes("Register claude-phone's MCP server with Claude Code (the call_ and notify_ tools)?"):
            return False
        ok, message = claudewire.register_mcp(self.exe)
        self.say(f"MCP server: {message}.")
        return ok

    def _command_done(self) -> bool:
        target = claudewire.claude_dir() / "commands" / "claude-phone.md"
        return target.exists() and target.read_text(encoding="utf-8") == claudewire.command_text(self.exe, self.repo)

    def _run_command(self) -> bool:
        if not self.yes("Add the /claude-phone command (start, stop and check claude-phone from Claude Code)?"):
            return False
        self.say(f"/claude-phone command {claudewire.install_command(self.exe, self.repo)}.")
        return True

    def _claude_md_done(self) -> bool:
        s = config.load()
        target = claudewire.claude_dir() / "CLAUDE.md"
        block = claudewire.claude_md_block(s.owner, s.owner_slug, self.repo)
        return target.exists() and block in target.read_text(encoding="utf-8")

    def _run_claude_md(self) -> bool:
        s = config.load()
        target = claudewire.claude_dir() / "CLAUDE.md"
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        if "## Phoning" in existing and claudewire.BLOCK_START not in existing:
            self.say("Your CLAUDE.md already has a \"Phoning\" section, probably pasted by hand from "
                     "docs/claude-md-snippet.md. The marked block would be a second copy.")
            if not self.yes("Add the marked block anyway?", default=False):
                return False
        if not self.yes(f"Tell Claude in your CLAUDE.md when to phone {s.owner}? (a marked block; a re-run "
                        "replaces only that block)"):
            return False
        self.say(f"CLAUDE.md block {claudewire.install_claude_md(s.owner, s.owner_slug, self.repo)}.")
        return True

    # --- 6. editors: either one, both or neither; each touches nothing but its own extension ---

    def _editor_state(self, which: str) -> tuple[str, bool]:
        """(a few words for the table, whether there is nothing to do), asked once per run."""
        if which not in self._editor_states:
            self._editor_states[which] = self._vscode_state() if which == "vscode" else self._vs_state()
        return self._editor_states[which]

    def _forget_editor(self, which: str) -> None:
        self._editor_states.pop(which, None)

    def _vscode_state(self) -> tuple[str, bool]:
        code = editors.find_code()
        if not code:
            return "VS Code not found", True
        available = editors.available_vscode_version(self.repo)
        installed = editors.vscode_installed_version(code)
        if installed is None:
            return f"not installed, {available} available", False
        if editors.is_older(installed, available):
            return f"{installed} installed, {available} available", False
        return f"{installed}, up to date", True

    def _vs_usable(self) -> list[editors.VisualStudio]:
        return [vs for vs in editors.visual_studios() if vs.supported_version and vs.supported_edition]

    def _vs_state(self) -> tuple[str, bool]:
        found = editors.visual_studios()
        if not found:
            return "Visual Studio not found", True
        usable = [vs for vs in found if vs.supported_version and vs.supported_edition]
        if not usable:
            names = ", ".join(vs.name for vs in found)
            return f"not supported: {names}; it needs 17.14 or newer, Community edition", True
        available = editors.available_visual_studio_version(self.repo)
        notes, done = [], True
        for vs in usable:
            installed = editors.vs_installed_version(vs)
            if installed is None:
                notes.append(f"{vs.name}: not installed, {available} available")
                done = False
            elif editors.is_older(installed, available):
                notes.append(f"{vs.name}: {installed} installed, {available} available")
                done = False
            else:
                notes.append(f"{vs.name}: {installed}, up to date")
        return "; ".join(notes), done

    def _get_vsix(self, kind: str, build: Callable[[Path], Path]) -> Path | None:
        """The release's build of this clone's extension if there is one, else a local build."""
        released = editors.release_vsix(kind, self.repo, config.HOME / "downloads", progress=self.progress())
        if released:
            self.say("Using the released build.")
            return released
        return self._build(build)

    def _discard(self, vsix: Path) -> None:
        """A downloaded .vsix isn't kept; a local build's stays where its tools put it."""
        if vsix.parent == config.HOME / "downloads":
            vsix.unlink(missing_ok=True)

    def _build(self, build: Callable[[Path], Path]) -> Path | None:
        self.say("Building the extension from this clone (a minute or two) ...")
        try:
            return build(self.repo)
        except editors.ToolMissing as e:
            self.say(f"Building needs {e.tool}, which isn't installed. Install it and run `phone setup` again.")
        except editors.BuildError as e:
            self.say(f"The build failed: {e}")
        return None

    def _run_vscode(self) -> bool:
        code = editors.find_code()
        available = editors.available_vscode_version(self.repo)
        installed = editors.vscode_installed_version(code) if code else None
        if not code:
            return True
        verb = "Install" if installed is None else f"Update ({installed} to {available})"
        if not self.yes(f"{verb} the VS Code extension (session list in the status bar, autostart when a window "
                        "opens, typing through the bridge)?"):
            return False
        vsix = self._get_vsix("vscode", editors.build_vscode)
        if vsix is None:
            return False
        installed_ok = editors.install_vscode(code, vsix)
        self._discard(vsix)
        if not installed_ok:
            self.say("`code --install-extension` failed.")
            return False
        self._forget_editor("vscode")
        now = editors.vscode_installed_version(code)
        if now is None or editors.is_older(now, available):
            self.say(f"VS Code still reports version {now or 'none'}, not {available}.")
            return False
        self.say(f"VS Code extension {now} is installed. Reload open VS Code windows (Developer: Reload Window).")
        return True

    def _wait_until_closed(self, vs: editors.VisualStudio) -> bool:
        while editors.visual_studio_running(vs):
            answer = self.text(f"Visual Studio must be closed to install an extension. Close {vs.name}, then press "
                               "Enter (s to skip)")
            if answer.lower().startswith("s"):
                return False
        return True

    def offer_editor_updates(self) -> None:
        """After `phone update`: bring extensions that are installed but older up to date. Putting one in where there
        is none is `phone setup`'s job."""
        code = editors.find_code()
        installed = editors.vscode_installed_version(code) if code else None
        if installed and editors.is_older(installed, editors.available_vscode_version(self.repo)):
            self._run_vscode()
        available = editors.available_visual_studio_version(self.repo)
        for vs in self._vs_usable():
            installed = editors.vs_installed_version(vs)
            if installed and editors.is_older(installed, available):
                self._run_visual_studio(only_installed=True)
                break

    def _run_visual_studio(self, only_installed: bool = False) -> bool:
        available = editors.available_visual_studio_version(self.repo)
        vsix, ok = None, True
        for vs in self._vs_usable():
            installed = editors.vs_installed_version(vs)
            if installed is None and only_installed:
                continue
            if installed is not None and not editors.is_older(installed, available):
                continue
            verb = "Install" if installed is None else f"Update ({installed} to {available})"
            if not self.yes(f"{verb} the Visual Studio extension in {vs.name} (session list window, autostart when "
                            "Visual Studio opens)?"):
                ok = False
                continue
            if not self._wait_until_closed(vs):
                ok = False
                continue
            vsix = vsix or self._get_vsix("visualstudio", editors.build_visual_studio)
            if vsix is None:
                return False
            installed_ok = editors.install_visual_studio(vs, vsix)
            now = editors.vs_installed_version(vs)
            if not installed_ok or now is None or editors.is_older(now, available):
                self.say(f"The Visual Studio installer didn't put version {available} into {vs.name} "
                         f"(it reports {now or 'none'}). Open the .vsix by hand: {vsix}")
                ok = False
                continue
            self.say(f"Visual Studio extension {now} is installed in {vs.name}.")
        if vsix:
            self._discard(vsix)
        self._forget_editor("vs")
        return ok

    # --- 8. start and test ---

    def _services_running(self) -> bool:
        from . import cli, procs
        return all(procs.pid(name) for name in cli.SERVICES)

    def _start_services(self, exe: Path) -> None:
        for argument in ("up", "status"):
            subprocess.run([str(exe), argument], check=False)
        self.say("Dial 600 for the echo test, or call 301 from a session.")
