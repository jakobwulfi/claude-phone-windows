"""Finding, downloading and installing FreeSWITCH and whisper-server, with the network and the installer faked."""

import base64
import hashlib
import http.client
import json
import subprocess
import urllib.error
import zipfile

import pytest

from claude_phone import config, tools


@pytest.fixture
def empty_pc(monkeypatch, tmp_path):
    """No FreeSWITCH or whisper anywhere: not configured, not in Program Files, not on PATH."""
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(config, "ENGINES", tmp_path / "engines")
    # The default freeswitch_exe is the real C:\Program Files path, which exists on a PC with FreeSWITCH.
    config.CONFIG_FILE.write_text(f"freeswitch_exe = '{tmp_path / 'nowhere' / 'FreeSwitchConsole.exe'}'\n",
                                  encoding="utf-8")
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.setenv(variable, str(tmp_path / "pf" / variable))
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    config.forget()
    return tmp_path


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


# --- finding ---

def test_nothing_is_found_on_an_empty_pc(empty_pc):
    assert tools.find_freeswitch() is None
    assert tools.find_whisper() is None


def test_freeswitch_is_found_where_config_says_or_in_program_files_or_on_path(empty_pc, monkeypatch):
    in_pf = touch(empty_pc / "pf" / "ProgramFiles" / "FreeSWITCH" / "FreeSwitchConsole.exe")
    assert tools.find_freeswitch() == in_pf

    configured = touch(empty_pc / "mine" / "FreeSwitchConsole.exe")
    config.CONFIG_FILE.write_text(f"freeswitch_exe = '{configured}'\n", encoding="utf-8")
    config.forget()
    assert tools.find_freeswitch() == configured  # config wins

    in_pf.unlink()
    configured.unlink()
    on_path = touch(empty_pc / "bin" / "FreeSwitchConsole.exe")
    monkeypatch.setattr(tools.shutil, "which",
                        lambda name: str(on_path) if name == "FreeSwitchConsole.exe" else None)
    assert tools.find_freeswitch() == on_path


def test_whisper_is_found_in_config_on_path_or_in_our_engines_folder(empty_pc, monkeypatch):
    ours = touch(empty_pc / "engines" / "whisper.cpp" / "whisper-server.exe")
    assert tools.find_whisper() == ours

    on_path = touch(empty_pc / "bin" / "whisper-server.exe")
    monkeypatch.setattr(tools.shutil, "which", lambda name: str(on_path) if name == "whisper-server" else None)
    assert tools.find_whisper() == on_path

    configured = touch(empty_pc / "mine" / "whisper-server.exe")
    config.CONFIG_FILE.write_text(f"whisper_server = '{configured}'\n", encoding="utf-8")
    config.forget()
    assert tools.find_whisper() == configured


def test_ffmpeg_is_found_on_path_or_where_winget_puts_it(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    assert tools.find_ffmpeg() is None and not tools.ffmpeg_on_path()

    package = touch(tmp_path / "Microsoft" / "WinGet" / "Packages" / "Gyan.FFmpeg_x" / "ffmpeg-7.1-full" / "bin"
                    / "ffmpeg.exe")
    assert tools.find_ffmpeg() == package

    link = touch(tmp_path / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe")
    assert tools.find_ffmpeg() == link  # the shim wins

    monkeypatch.setattr(tools.shutil, "which", lambda name: str(tmp_path / "on" / "ffmpeg.exe"))
    assert tools.ffmpeg_on_path() and tools.find_ffmpeg() == tmp_path / "on" / "ffmpeg.exe"


def test_ffmpeg_is_installed_through_winget():
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert tools.install_ffmpeg(run=run)
    assert seen[0][:4] == ["winget", "install", "--id", "Gyan.FFmpeg"] and "--accept-package-agreements" in seen[0]
    assert not tools.install_ffmpeg(run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))

    def no_winget(*a, **k):
        raise FileNotFoundError("winget")
    assert not tools.install_ffmpeg(run=no_winget)


# --- what to download ---

LISTING = b"""<a href="FreeSWITCH-1.8.7-x64-Release.msi">old</a><a href="FreeSWITCH-1.10.12-Release-x64.msi">x</a>
<a href="FreeSWITCH-1.11.3-Release-x64.msi">new</a><a href="FreeSWITCH-1.11.2-Release-x64.msi">y</a>
<a href="FreeSWITCH-1.6.14.x86_64.msi">older</a><a href="freeswitch.msi">alias</a>"""


def test_the_newest_freeswitch_installer_is_picked_by_version_not_by_text():
    latest = tools.latest_freeswitch(lambda url: LISTING)

    assert latest.name == "FreeSWITCH-1.11.3-Release-x64.msi"
    assert latest.url == tools.FREESWITCH_LISTING + latest.name and latest.sha256 is None


def test_a_listing_without_installers_is_an_error():
    with pytest.raises(tools.DownloadError, match="no FreeSWITCH installer"):
        tools.latest_freeswitch(lambda url: b"<html></html>")


def whisper_releases(*releases) -> bytes:
    return json.dumps(list(releases)).encode()


def test_whisper_comes_from_the_newest_release_that_has_the_windows_build():
    asset = {"name": "whisper-bin-x64.zip", "size": 99, "browser_download_url": "https://x/b5454.zip",
             "digest": "sha256:" + "ab" * 32}
    draft = {"tag_name": "draft", "draft": True, "assets": [dict(asset, browser_download_url="no")]}
    releases = whisper_releases({"tag_name": "v1.9.5", "assets": []}, draft,
                                {"tag_name": "b5454", "assets": [{"name": "whisper-bin-Win32.zip"}, asset]})

    latest = tools.latest_whisper(lambda url: releases)

    assert (latest.url, latest.size, latest.sha256) == ("https://x/b5454.zip", 99, "ab" * 32)


def test_a_whisper_asset_without_a_digest_has_no_checksum_to_check():
    asset = {"name": "whisper-bin-x64.zip", "browser_download_url": "https://x/w.zip"}

    assert tools.latest_whisper(lambda url: whisper_releases({"assets": [asset]})).sha256 is None


def test_no_windows_build_anywhere_is_an_error():
    with pytest.raises(tools.DownloadError, match=r"whisper\.cpp"):
        tools.latest_whisper(lambda url: whisper_releases({"assets": []}))


def test_hugging_face_gives_the_checksum_in_its_redirect(monkeypatch):
    headers = http.client.HTTPMessage()
    headers["x-linked-size"] = "190098681"
    headers["x-linked-etag"] = '"' + "bf" * 32 + '"'

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 302, "Found", headers, None)
    monkeypatch.setattr(tools.urllib.request, "build_opener", lambda *handlers: Opener())

    assert tools.hugging_face_checksum("https://hf/x.bin") == (190098681, "bf" * 32)


def test_an_etag_that_is_not_a_sha256_is_not_taken_for_one(monkeypatch):
    headers = http.client.HTTPMessage()
    headers["x-linked-etag"] = '"W/abc123"'

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 302, "Found", headers, None)
    monkeypatch.setattr(tools.urllib.request, "build_opener", lambda *handlers: Opener())

    assert tools.hugging_face_checksum("https://hf/x.bin") == (0, None)


# --- downloading ---

class FakeResponse:
    def __init__(self, data: bytes, length: int | None = None) -> None:
        self.data, self.pos = data, 0
        self.headers = {"Content-Length": str(len(data) if length is None else length)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        chunk, self.pos = self.data[self.pos:self.pos + n], self.pos + n
        return chunk


def test_a_download_is_kept_only_when_whole_and_matching_its_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "CHUNK", 4)
    data = b"0123456789"
    steps = []

    path = tools.download("https://x/f.zip", tmp_path / "d" / "f.zip", sha256=hashlib.sha256(data).hexdigest().upper(),
                          progress=lambda done, total: steps.append((done, total)),
                          open_url=lambda url: FakeResponse(data))

    assert path.read_bytes() == data and steps == [(4, 10), (8, 10), (10, 10)]
    assert not list((tmp_path / "d").glob("*.part"))


def test_a_download_with_the_wrong_checksum_is_not_kept(tmp_path):
    with pytest.raises(tools.DownloadError, match="checksum"):
        tools.download("https://x/f.zip", tmp_path / "f.zip", sha256="00" * 32,
                       open_url=lambda url: FakeResponse(b"data"))

    assert not (tmp_path / "f.zip").exists() and not (tmp_path / "f.part").exists()


def test_a_download_that_stops_early_is_not_kept(tmp_path):
    with pytest.raises(tools.DownloadError, match="stopped at 4 of 9"):
        tools.download("https://x/f.zip", tmp_path / "f.zip", open_url=lambda url: FakeResponse(b"data", length=9))

    assert not (tmp_path / "f.zip").exists() and not (tmp_path / "f.part").exists()


def test_a_dropped_connection_leaves_nothing_behind(tmp_path):
    def broken(url):
        raise ConnectionResetError("reset")
    with pytest.raises(ConnectionResetError):
        tools.download("https://x/f.zip", tmp_path / "f.zip", open_url=broken)

    assert list(tmp_path.iterdir()) == []


# --- signature, elevation, installing ---

def answer(stdout="", code=0):
    return lambda *a, **k: subprocess.CompletedProcess(a, code, stdout, "")


def test_only_a_valid_signature_from_the_right_signer_passes(tmp_path):
    msi = tmp_path / "fs.msi"
    good = 'Valid\nCN="SignalWire, Inc", O="SignalWire, Inc", C=US\n'

    assert tools.is_signed_by(msi, run=answer(good))
    assert not tools.is_signed_by(msi, run=answer('NotSigned\n'))
    assert not tools.is_signed_by(msi, run=answer('HashMismatch\nCN="SignalWire, Inc"\n'))
    assert not tools.is_signed_by(msi, run=answer('Valid\nCN="Someone Else"\n'))
    assert not tools.is_signed_by(msi, run=answer(good, code=1))

    def missing(*a, **k):
        raise FileNotFoundError("powershell")
    assert not tools.is_signed_by(msi, run=missing)


def test_the_elevated_command_quotes_paths_with_spaces_and_apostrophes():
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 3010)

    assert tools.run_elevated("msiexec.exe", ["/i", r"C:\Users\O'Neil\my dir\fs.msi", "/qb"], run=run) == 3010
    command = seen[0][-1]
    assert "-Verb RunAs -Wait -PassThru" in command and "exit $p.ExitCode" in command
    assert '"' + r"C:\Users\O" + "''" + r"Neil\my dir\fs.msi" + '"' in command  # the apostrophe doubled, spaces quoted


def test_the_elevated_command_says_none_when_powershell_is_missing():
    def run(argv, **kwargs):
        raise FileNotFoundError("powershell")

    assert tools.run_elevated("msiexec.exe", [], run=run) is None


@pytest.mark.parametrize(("code", "installed"), [(0, True), (3010, True), (1223, False), (1603, False), (None, False)])
def test_freeswitch_counts_as_installed_only_after_a_good_exit_code(empty_pc, code, installed):
    exe = touch(empty_pc / "pf" / "ProgramFiles" / "FreeSWITCH" / "FreeSwitchConsole.exe")
    calls = []

    def elevate(program, args):
        calls.append((program, args))
        return code

    result = tools.install_freeswitch(empty_pc / "fs.msi", elevate=elevate)

    assert (result == exe) is installed
    assert len(calls) == 1 and calls[0][0] == "powershell.exe"


def test_the_install_script_runs_the_msi_then_makes_the_service_manual(tmp_path):
    msi = tmp_path / "O'Neil dir" / "fs.msi"
    seen = []

    tools.install_freeswitch(msi, elevate=lambda program, args: seen.append(args) or 0)

    flags = seen[0]
    assert flags[:2] == ["-NoProfile", "-EncodedCommand"]
    script = base64.b64decode(flags[2]).decode("utf-16-le")
    assert script == tools.freeswitch_install_script(msi)
    assert "msiexec.exe" in script and "'/i','\"" + str(msi).replace("'", "''") + "\"'" in script  # quoted path
    assert "Set-Service -Name FreeSWITCH -StartupType Manual" in script and "exit $p.ExitCode" in script


def make_zip(path, names):
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, name.encode())
    return path


def test_only_the_server_and_its_dlls_are_unpacked_and_only_by_file_name(tmp_path):
    archive = make_zip(tmp_path / "w.zip", [
        "Release/whisper-server.exe", "Release/whisper.dll", "Release/ggml-base.dll", "Release/main.exe",
        "Release/test-vad.exe", "Release/readme.txt", "../escape.dll"])
    dest = tmp_path / "engines" / "whisper.cpp"

    server = tools.install_whisper(archive, dest)

    assert server == dest / "whisper-server.exe"
    assert sorted(p.name for p in dest.iterdir()) == [
        "escape.dll", "ggml-base.dll", "whisper-server.exe", "whisper.dll"]
    assert not (tmp_path / "engines" / "escape.dll").exists()  # nothing written outside dest


def test_a_zip_without_the_server_is_refused(tmp_path):
    archive = make_zip(tmp_path / "w.zip", ["Release/main.exe", "Release/whisper.dll"])

    with pytest.raises(tools.DownloadError, match=r"no whisper-server\.exe"):
        tools.install_whisper(archive, tmp_path / "out")
