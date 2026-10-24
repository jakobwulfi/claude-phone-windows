"""The editor extensions: which editors are installed, which version of ours each has, and putting ours in.

VS Code and Visual Studio are independent of each other and of the core install: nothing here touches the venv, the
services, the hooks or the config. Both extensions only need the daemon's API, so a version mismatch between them and
the daemon is tolerated by design; setup just says when an extension is older than the one in this clone. The .vsix
comes from a local build for now (npm for VS Code, the .NET SDK for Visual Studio); the processes are parameters so
the tests run without any editor."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

from . import config, tools

RELEASES_REPO = "jakobwulfi/claude-phone-windows"  # where the release workflow attaches the .vsix files
VSCODE_ID = "jakobwulfi.claude-phone"
VS_ID = "ClaudePhone.6f1b6d0e-3c1e-4c53-9a4e-5b7a0f2d9c11"
VS_MIN_VERSION = (17, 14)  # the extension's InstallationTarget
VS_EDITION = "Microsoft.VisualStudio.Product.Community"  # the only edition that manifest names
VS_VSIX = Path("ClaudePhone.Extension") / "bin" / "Release" / "net8.0-windows8.0" / "ClaudePhone.Extension.vsix"
BUILD_TIMEOUT_S = 900


class ToolMissing(RuntimeError):
    """A build tool (Node.js, the .NET SDK) isn't installed."""

    def __init__(self, tool: str) -> None:
        super().__init__(tool)
        self.tool = tool


class BuildError(RuntimeError):
    """A build or install command failed."""


def parse_version(text: str) -> tuple[int, ...]:
    """'1.1.0.0' and '1.1' are the same version: trailing zeros don't count."""
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    while numbers and numbers[-1] == 0:
        numbers.pop()
    return tuple(numbers)


def is_older(installed: str, available: str) -> bool:
    return parse_version(installed) < parse_version(available)


# --- released builds ---

def project_version(repo: Path | None = None) -> str:
    """The claude-phone version in this clone's pyproject.toml: the release tag is v<this>."""
    import tomllib
    with ((repo or config.REPO) / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def release_asset_name(kind: str, version: str) -> str:
    """What .github/workflows/release.yml calls the file: kind is 'vscode' or 'visualstudio'."""
    return f"claude-phone-{version}.vsix" if kind == "vscode" else f"ClaudePhone.Extension-{version}.vsix"


def release_vsix(kind: str, repo: Path | None, folder: Path, progress: tools.Progress | None = None,
                 fetch: Callable[[str], bytes] = tools.http_get, download: Callable = tools.download) -> Path | None:
    """This clone's extension version as built by the release for this clone's tag, checked against the sha256 GitHub
    lists for it. None when there is no such release, asset or checksum (a clone ahead of its last release, no
    network): the caller builds locally."""
    repo = repo or config.REPO
    try:
        version = available_vscode_version(repo) if kind == "vscode" else available_visual_studio_version(repo)
        name = release_asset_name(kind, version)
        release = json.loads(fetch(f"https://api.github.com/repos/{RELEASES_REPO}/releases/tags/v{project_version(repo)}"))
        asset = next(a for a in release["assets"] if a["name"] == name)
        sha256 = str(asset.get("digest") or "").removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            return None
        return download(asset["browser_download_url"], folder / name, sha256=sha256, progress=progress)
    except (OSError, ValueError, KeyError, StopIteration, tools.DownloadError, BuildError):
        return None


# --- VS Code ---

def available_vscode_version(repo: Path | None = None) -> str:
    package = (repo or config.REPO) / "vscode-extension" / "package.json"
    return json.loads(package.read_text(encoding="utf-8"))["version"]


def find_code() -> Path | None:
    """VS Code's command-line launcher: on PATH, else where its per-user and system installers put it."""
    on_path = shutil.which("code")
    candidates = [Path(on_path)] if on_path else []
    for base, rest in ((os.environ.get("LOCALAPPDATA"), "Programs/Microsoft VS Code/bin/code.cmd"),
                       (os.environ.get("PROGRAMFILES"), "Microsoft VS Code/bin/code.cmd")):
        if base:
            candidates.append(Path(base) / rest)
    return next((c for c in candidates if c.is_file()), None)


def vscode_installed_version(code: Path, run: Callable = subprocess.run) -> str | None:
    try:
        done = run([str(code), "--list-extensions", "--show-versions"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (done.stdout or "").splitlines():
        name, _, version = line.strip().partition("@")
        if name.lower() == VSCODE_ID:
            return version
    return None


def _run_checked(argv: list[str], cwd: Path, run: Callable) -> None:
    try:
        done = run(argv, cwd=cwd, capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        raise BuildError(f"{Path(argv[0]).name} didn't run: {e}") from e
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-5:]
        raise BuildError(f"{' '.join(argv[:3])} failed (exit {done.returncode}): " + " / ".join(tail))


def build_vscode(repo: Path | None = None, run: Callable = subprocess.run) -> Path:
    """The .vsix, built fresh (an old file of the same version may hold older code)."""
    repo = repo or config.REPO
    folder = repo / "vscode-extension"
    npm = shutil.which("npm")
    if not npm:
        raise ToolMissing("Node.js (npm)")
    if not (folder / "node_modules").is_dir():
        _run_checked([npm, "install"], folder, run)
    _run_checked([npm, "run", "package"], folder, run)
    vsix = folder / f"claude-phone-{available_vscode_version(repo)}.vsix"
    if not vsix.is_file():
        raise BuildError(f"the build didn't leave {vsix.name}")
    return vsix


def install_vscode(code: Path, vsix: Path, run: Callable = subprocess.run) -> bool:
    try:
        done = run([str(code), "--install-extension", str(vsix), "--force"], capture_output=True, text=True,
                   timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


# --- Visual Studio ---

@dataclass(frozen=True)
class VisualStudio:
    instance_id: str
    name: str
    version: str
    path: Path  # the installation folder
    product_id: str

    @property
    def supported_version(self) -> bool:
        return parse_version(self.version)[:2] >= VS_MIN_VERSION

    @property
    def supported_edition(self) -> bool:
        return self.product_id == VS_EDITION

    @property
    def installer(self) -> Path:
        return self.path / "Common7" / "IDE" / "VSIXInstaller.exe"

    @property
    def devenv(self) -> Path:
        return self.path / "Common7" / "IDE" / "devenv.exe"


def available_visual_studio_version(repo: Path | None = None) -> str:
    folder = (repo or config.REPO) / "visualstudio-extension" / "ClaudePhone.Extension"
    found = re.search(r"<Version>\s*([\d.]+)\s*</Version>",
                      (folder / "ClaudePhone.Extension.csproj").read_text(encoding="utf-8"))
    if not found:
        raise BuildError("no <Version> in ClaudePhone.Extension.csproj")
    return found.group(1)


def visual_studios(run: Callable = subprocess.run) -> list[VisualStudio]:
    """Every Visual Studio that vswhere knows (it ships with the Visual Studio Installer)."""
    base = os.environ.get("PROGRAMFILES(X86)")
    vswhere = Path(base) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe" if base else None
    if not vswhere or not vswhere.is_file():
        return []
    try:
        done = run([str(vswhere), "-products", "*", "-format", "json", "-utf8"], capture_output=True, text=True,
                   encoding="utf-8", timeout=60)
        entries = json.loads(done.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return [VisualStudio(e["instanceId"], e.get("displayName", "Visual Studio"), e.get("installationVersion", "0"),
                         Path(e["installationPath"]), e.get("productId", ""))
            for e in entries if e.get("instanceId") and e.get("installationPath") and not e.get("isPrerelease")]


def extension_folders(vs: VisualStudio) -> list[Path]:
    """The per-user folders Visual Studio unpacks extensions into for this instance."""
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) / "Microsoft" / "VisualStudio" if local else None
    if not root or not root.is_dir():
        return []
    return [ext for instance in root.glob(f"*_{vs.instance_id}") for ext in (instance / "Extensions").glob("*")]


def vs_installed_version(vs: VisualStudio) -> str | None:
    for folder in extension_folders(vs):
        try:
            root = ET.parse(folder / "extension.vsixmanifest").getroot()
        except (OSError, ET.ParseError):
            continue
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == "Identity" and element.get("Id") == VS_ID:
                return element.get("Version")
    return None


def visual_studio_running(vs: VisualStudio) -> bool:
    for process in psutil.process_iter(["name", "exe"]):
        if (process.info["name"] or "").lower() == "devenv.exe":
            exe = process.info["exe"]
            if not exe or os.path.normcase(exe) == os.path.normcase(str(vs.devenv)):
                return True
    return False


def build_visual_studio(repo: Path | None = None, run: Callable = subprocess.run) -> Path:
    repo = repo or config.REPO
    folder = repo / "visualstudio-extension"
    dotnet = shutil.which("dotnet")
    if not dotnet:
        raise ToolMissing(".NET SDK")
    _run_checked([dotnet, "build", "ClaudePhone.Extension", "-c", "Release"], folder, run)
    vsix = folder / VS_VSIX
    if not vsix.is_file():
        raise BuildError(f"the build didn't leave {vsix.name}")
    return vsix


def install_visual_studio(vs: VisualStudio, vsix: Path, run: Callable = subprocess.run) -> bool:
    """Visual Studio must be closed. Whether it took is for the caller to check by reading the version back."""
    try:
        done = run([str(vs.installer), "/quiet", f"/instanceIds:{vs.instance_id}", str(vsix)], capture_output=True,
                   text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0
