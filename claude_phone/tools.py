"""The two programs claude-phone runs but doesn't ship: FreeSWITCH and whisper.cpp's server. Finds them, and downloads
and installs them for `phone setup`. Every download is checked: whisper.cpp's zip and the speech model against the
sha256 their host publishes, FreeSWITCH's MSI against its Authenticode signature (SignalWire publishes no checksum).
The network and the elevated installer are parameters so the tests run without either."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import config

FREESWITCH_LISTING = "https://files.freeswitch.org/windows/installer/x64/"
WHISPER_RELEASES = "https://api.github.com/repos/ggml-org/whisper.cpp/releases?per_page=30"
# whisper.cpp keeps its Windows builds on the b<number> tags: the release marked latest has no files attached.
WHISPER_ASSET = "whisper-bin-x64.zip"
FREESWITCH_EXE, WHISPER_EXE = "FreeSwitchConsole.exe", "whisper-server.exe"
FREESWITCH_SIGNER = "SignalWire"
MSI_OK_EXIT_CODES = (0, 3010)  # 3010: installed, a restart is wanted (FreeSWITCH doesn't need one)
CHUNK = 1 << 20
TIMEOUT_S = 60
USER_AGENT = "claude-phone"

Progress = Callable[[int, int], None]  # bytes so far, bytes in all (0 when unknown)


class DownloadError(RuntimeError):
    """A download that arrived damaged or isn't what it should be."""


@dataclass(frozen=True)
class Download:
    url: str
    name: str
    size: int = 0
    sha256: str | None = None


def http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return response.read()


def _open(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=TIMEOUT_S)


# --- finding what is installed ---

def find_freeswitch() -> Path | None:
    candidates = [Path(config.load().freeswitch_exe)]
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / "FreeSWITCH" / FREESWITCH_EXE)
    candidates.append(Path("C:/FreeSWITCH") / FREESWITCH_EXE)
    on_path = shutil.which(FREESWITCH_EXE)
    if on_path:
        candidates.append(Path(on_path))
    return next((c for c in candidates if c.is_file()), None)


def ffmpeg_on_path() -> bool:
    return shutil.which("ffmpeg") is not None


def find_ffmpeg() -> Path | None:
    """ffmpeg on PATH, or where winget puts it when this window's PATH hasn't heard of it yet."""
    on_path = shutil.which("ffmpeg")
    if on_path:
        return Path(on_path)
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    winget = Path(local) / "Microsoft" / "WinGet"
    packages = sorted((winget / "Packages").glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"))
    return next((c for c in [winget / "Links" / "ffmpeg.exe", *packages] if c.is_file()), None)


def install_ffmpeg(run: Callable = subprocess.run) -> bool:
    """ffmpeg (audio conversion for calls) from winget, the build `audio.py` tells users to install."""
    try:
        done = run(["winget", "install", "--id", "Gyan.FFmpeg", "-e", "--accept-package-agreements",
                    "--accept-source-agreements"], capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def whisper_dir() -> Path:
    return config.ENGINES / "whisper.cpp"


def find_whisper() -> Path | None:
    candidates = [Path(config.load().whisper_server)] if config.load().whisper_server else []
    on_path = shutil.which("whisper-server")
    if on_path:
        candidates.append(Path(on_path))
    candidates.append(whisper_dir() / WHISPER_EXE)
    return next((c for c in candidates if c.is_file()), None)


# --- what to download ---

def latest_freeswitch(fetch: Callable[[str], bytes] = http_get) -> Download:
    listing = fetch(FREESWITCH_LISTING).decode("utf-8", "replace")
    found = re.findall(r'href="(FreeSWITCH-(\d+)\.(\d+)\.(\d+)-Release-x64\.msi)"', listing)
    if not found:
        raise DownloadError(f"no FreeSWITCH installer is listed at {FREESWITCH_LISTING}")
    name = max(found, key=lambda f: tuple(int(n) for n in f[1:]))[0]
    return Download(FREESWITCH_LISTING + name, name)


def latest_whisper(fetch: Callable[[str], bytes] = http_get) -> Download:
    for release in json.loads(fetch(WHISPER_RELEASES)):
        if release.get("draft"):
            continue
        for asset in release.get("assets") or []:
            if asset.get("name") == WHISPER_ASSET:
                digest = str(asset.get("digest") or "")
                return Download(asset["browser_download_url"], WHISPER_ASSET, int(asset.get("size") or 0),
                                digest.removeprefix("sha256:") if digest.startswith("sha256:") else None)
    raise DownloadError("no recent whisper.cpp release has the Windows build (" + WHISPER_ASSET + ")")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def hugging_face_checksum(url: str) -> tuple[int, str | None]:
    """Size and sha256 of a Hugging Face file: its first (redirect) answer carries both for large files."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        response = urllib.request.build_opener(_NoRedirect).open(request, timeout=TIMEOUT_S)
        headers = response.headers
    except urllib.error.HTTPError as e:  # the redirect, refused on purpose
        headers = e.headers
    sha = (headers.get("x-linked-etag") or "").strip('"')
    return int(headers.get("x-linked-size") or 0), sha if re.fullmatch(r"[0-9a-f]{64}", sha) else None


# --- downloading and installing ---

def download(url: str, dest: Path, *, sha256: str | None = None, progress: Progress | None = None,
             open_url: Callable = _open) -> Path:
    """The file at `url` as `dest`, through a .part file, so a stopped download is never taken for a whole one."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    digest = hashlib.sha256()
    try:
        with open_url(url) as response, part.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(CHUNK):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if total and done != total:
            raise DownloadError(f"{dest.name} stopped at {done} of {total} bytes")
        if sha256 and digest.hexdigest() != sha256.lower():
            raise DownloadError(f"{dest.name} doesn't match its published checksum; it was not kept")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)
    return dest


def is_signed_by(path: Path, signer: str = FREESWITCH_SIGNER, run: Callable = subprocess.run) -> bool:
    """Does Windows accept the file's Authenticode signature, from a certificate naming `signer`?"""
    quoted = str(path).replace("'", "''")
    command = f"$s = Get-AuthenticodeSignature -LiteralPath '{quoted}'; $s.Status; $s.SignerCertificate.Subject"
    try:
        done = run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    lines = (done.stdout or "").splitlines()
    return done.returncode == 0 and len(lines) >= 2 and lines[0].strip() == "Valid" and signer in lines[1]


def run_elevated(program: str, arguments: list[str], run: Callable = subprocess.run) -> int | None:
    """Run a program as administrator (one UAC prompt) and wait. Its exit code, or None when it didn't run."""
    def quote(text: str) -> str:
        return f'"{text}"' if " " in text else text
    argument_list = " ".join(quote(a) for a in arguments).replace("'", "''")
    command = (f"$p = Start-Process '{program}' -ArgumentList '{argument_list}' -Verb RunAs -Wait -PassThru; "
               "exit $p.ExitCode")
    try:
        return run(["powershell", "-NoProfile", "-Command", command], capture_output=True, timeout=1800).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def freeswitch_install_script(msi: Path) -> str:
    """The MSI, then FreeSWITCH's Windows service (if the MSI made one) stopped and set to Manual: `phone up` runs
    FreeSWITCH itself with its own config, and a service would hold the SIP port first."""
    quoted = str(msi).replace("'", "''")
    return (f"$p = Start-Process msiexec.exe -ArgumentList '/i','\"{quoted}\"','/qb','/norestart' -Wait -PassThru; "
            "$s = Get-Service -Name FreeSWITCH -ErrorAction SilentlyContinue; "
            "if ($s) { Stop-Service -Name FreeSWITCH -Force -ErrorAction SilentlyContinue; "
            "Set-Service -Name FreeSWITCH -StartupType Manual }; "
            "exit $p.ExitCode")


def install_freeswitch(msi: Path, elevate: Callable[[str, list[str]], int | None] = run_elevated) -> Path | None:
    """Run the MSI (administrator rights needed). The FreeSwitchConsole.exe it installed, or None."""
    encoded = base64.b64encode(freeswitch_install_script(msi).encode("utf-16-le")).decode("ascii")
    code = elevate("powershell.exe", ["-NoProfile", "-EncodedCommand", encoded])
    return find_freeswitch() if code in MSI_OK_EXIT_CODES else None


def install_whisper(archive: Path, dest: Path | None = None) -> Path:
    """whisper-server.exe and the DLLs it loads out of the release zip into one folder; the rest (about 40 test and
    demo programs) stays out. Only file names are used, never the zip's own paths."""
    dest = dest or whisper_dir()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        wanted = [(info, PurePosixPath(info.filename).name) for info in zf.infolist() if not info.is_dir()]
        wanted = [(info, name) for info, name in wanted if name.lower() == WHISPER_EXE or name.lower().endswith(".dll")]
        if not any(name.lower() == WHISPER_EXE for _, name in wanted):
            raise DownloadError(f"{archive.name} has no {WHISPER_EXE}")
        for info, name in wanted:
            (dest / name).write_bytes(zf.read(info))
    return dest / WHISPER_EXE
