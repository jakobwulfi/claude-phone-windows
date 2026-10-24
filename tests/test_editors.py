"""Finding VS Code and Visual Studio, reading which extension version each has, building and installing ours."""

import json
import subprocess
from pathlib import Path

import pytest

from claude_phone import editors

VS_ID = editors.VS_ID


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


# --- versions ---

def test_trailing_zeros_do_not_make_versions_different():
    assert editors.parse_version("1.1.0.0") == editors.parse_version("1.1.0") == (1, 1)
    assert not editors.is_older("1.1.0.0", "1.1.0")
    assert editors.is_older("0.3.0", "0.4.0")
    assert editors.is_older("1.9.0", "1.10.0")  # numbers, not text
    assert not editors.is_older("2.0", "1.9.9")


def make_repo(tmp_path, code_version="0.4.0", vs_version="1.1.0"):
    (tmp_path / "vscode-extension").mkdir(parents=True)
    (tmp_path / "vscode-extension" / "package.json").write_text(json.dumps({"version": code_version}),
                                                                  encoding="utf-8")
    csproj = tmp_path / "visualstudio-extension" / "ClaudePhone.Extension" / "ClaudePhone.Extension.csproj"
    csproj.parent.mkdir(parents=True)
    csproj.write_text(f"<Project><PropertyGroup><Version>{vs_version}</Version></PropertyGroup></Project>",
                      encoding="utf-8")
    return tmp_path


def test_the_available_versions_come_from_the_clone(tmp_path):
    repo = make_repo(tmp_path, "0.5.2", "1.2.0")

    assert editors.available_vscode_version(repo) == "0.5.2"
    assert editors.available_visual_studio_version(repo) == "1.2.0"


def test_a_csproj_without_a_version_is_an_error(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "visualstudio-extension" / "ClaudePhone.Extension" / "ClaudePhone.Extension.csproj").write_text(
        "<Project/>", encoding="utf-8")

    with pytest.raises(editors.BuildError):
        editors.available_visual_studio_version(repo)


# --- released builds ---

def make_release_repo(tmp_path, project="0.1.0"):
    repo = make_repo(tmp_path)
    (repo / "pyproject.toml").write_text(f'[project]\nname = "claude-phone"\nversion = "{project}"\n', encoding="utf-8")
    return repo


def release_json(*assets) -> bytes:
    return json.dumps({"assets": list(assets)}).encode()


SHA = "cd" * 32


def asset(name, digest="sha256:" + SHA):
    return {"name": name, "browser_download_url": f"https://github.com/x/releases/download/v0.1.0/{name}",
            "digest": digest}


def test_asset_names_carry_the_extension_version():
    assert editors.release_asset_name("vscode", "0.4.0") == "claude-phone-0.4.0.vsix"
    assert editors.release_asset_name("visualstudio", "1.1.0") == "ClaudePhone.Extension-1.1.0.vsix"


def test_the_released_vsix_is_found_by_tag_and_extension_version_and_checked(tmp_path):
    repo = make_release_repo(tmp_path)
    asked, downloads = [], []

    def fetch(url):
        asked.append(url)
        return release_json(asset("claude-phone-0.3.0.vsix"), asset("claude-phone-0.4.0.vsix"))

    def download(url, dest, *, sha256, progress):
        downloads.append((url, dest.name, sha256))
        return dest

    got = editors.release_vsix("vscode", repo, tmp_path / "dl", fetch=fetch, download=download)

    assert asked == ["https://api.github.com/repos/jakobwulfi/claude-phone-windows/releases/tags/v0.1.0"]
    assert got == tmp_path / "dl" / "claude-phone-0.4.0.vsix"
    assert downloads == [("https://github.com/x/releases/download/v0.1.0/claude-phone-0.4.0.vsix",
                          "claude-phone-0.4.0.vsix", SHA)]


def test_the_visual_studio_vsix_is_picked_by_its_own_name(tmp_path):
    repo = make_release_repo(tmp_path)

    got = editors.release_vsix("visualstudio", repo, tmp_path / "dl",
                               fetch=lambda url: release_json(asset("claude-phone-0.4.0.vsix"),
                                                              asset("ClaudePhone.Extension-1.1.0.vsix")),
                               download=lambda url, dest, *, sha256, progress: dest)

    assert got.name == "ClaudePhone.Extension-1.1.0.vsix"


@pytest.mark.parametrize("fetch", [
    lambda url: release_json(asset("claude-phone-0.3.0.vsix")),  # this clone's version isn't in the release
    lambda url: release_json(asset("claude-phone-0.4.0.vsix", digest="")),  # nothing to check it against
    lambda url: release_json(asset("claude-phone-0.4.0.vsix", digest="sha256:short")),
    lambda url: b"not json",
    lambda url: b"{}",
])
def test_no_usable_release_means_none_so_the_caller_builds(tmp_path, fetch):
    repo = make_release_repo(tmp_path)

    assert editors.release_vsix("vscode", repo, tmp_path / "dl", fetch=fetch,
                                download=lambda *a, **k: pytest.fail("downloaded")) is None


def test_no_release_at_all_or_no_network_means_none(tmp_path):
    repo = make_release_repo(tmp_path)

    def missing(url):
        raise OSError("HTTP Error 404: Not Found")

    assert editors.release_vsix("vscode", repo, tmp_path / "dl", fetch=missing) is None


def test_a_damaged_download_means_none(tmp_path):
    repo = make_release_repo(tmp_path)

    def damaged(url, dest, *, sha256, progress):
        raise editors.tools.DownloadError("doesn't match its published checksum")

    assert editors.release_vsix("vscode", repo, tmp_path / "dl", download=damaged,
                                fetch=lambda url: release_json(asset("claude-phone-0.4.0.vsix"))) is None


# --- VS Code ---

def test_code_is_found_on_path_or_in_its_per_user_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(editors.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    assert editors.find_code() is None

    per_user = tmp_path / "local" / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd"
    per_user.parent.mkdir(parents=True)
    per_user.write_text("", encoding="utf-8")
    assert editors.find_code() == per_user

    on_path = tmp_path / "bin" / "code.cmd"
    on_path.parent.mkdir()
    on_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(editors.shutil, "which", lambda name: str(on_path))
    assert editors.find_code() == on_path


def test_the_installed_vscode_version_is_read_from_the_extension_list(tmp_path):
    listing = "ms-python.python@2024.1.0\njakobwulfi.claude-phone@0.3.0\nother.claude-phone-tools@9.9.9\n"
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return done(listing)

    assert editors.vscode_installed_version(tmp_path / "code.cmd", run=run) == "0.3.0"
    assert seen[0][1:] == ["--list-extensions", "--show-versions"]
    assert editors.vscode_installed_version(tmp_path / "code.cmd", run=lambda *a, **k: done("a.b@1\n")) is None


def test_asking_vscode_when_it_cannot_run_says_not_installed(tmp_path):
    def run(*a, **k):
        raise FileNotFoundError("code")

    assert editors.vscode_installed_version(tmp_path / "code.cmd", run=run) is None


def test_the_vscode_vsix_is_built_fresh_with_npm(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(editors.shutil, "which", lambda name: "npm.cmd")
    commands = []

    def run(argv, cwd, **kwargs):
        commands.append(argv[1:])
        if argv[1:] == ["run", "package"]:
            (cwd / "claude-phone-0.4.0.vsix").write_bytes(b"vsix")
        return done()

    vsix = editors.build_vscode(repo, run=run)

    assert vsix == repo / "vscode-extension" / "claude-phone-0.4.0.vsix"
    assert commands == [["install"], ["run", "package"]]  # no node_modules yet

    commands.clear()
    (repo / "vscode-extension" / "node_modules").mkdir()
    editors.build_vscode(repo, run=run)
    assert commands == [["run", "package"]]


def test_building_without_npm_names_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(editors.shutil, "which", lambda name: None)

    with pytest.raises(editors.ToolMissing) as caught:
        editors.build_vscode(make_repo(tmp_path))

    assert caught.value.tool == "Node.js (npm)"


def test_a_failing_build_shows_the_end_of_its_output(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "vscode-extension" / "node_modules").mkdir()
    monkeypatch.setattr(editors.shutil, "which", lambda name: "npm.cmd")

    with pytest.raises(editors.BuildError, match=r"exit 2.*TS2304"):
        editors.build_vscode(repo, run=lambda *a, **k: done("", 2, "line\nerror TS2304: cannot find name"))


def test_a_build_that_leaves_no_vsix_is_an_error(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "vscode-extension" / "node_modules").mkdir()
    monkeypatch.setattr(editors.shutil, "which", lambda name: "npm.cmd")

    with pytest.raises(editors.BuildError, match=r"claude-phone-0\.4\.0\.vsix"):
        editors.build_vscode(repo, run=lambda *a, **k: done())


def test_installing_into_vscode_forces_the_version_and_reports_failure(tmp_path):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return done(code=0)

    assert editors.install_vscode(tmp_path / "code.cmd", tmp_path / "x.vsix", run=run)
    assert seen[0][1:] == ["--install-extension", str(tmp_path / "x.vsix"), "--force"]
    assert not editors.install_vscode(tmp_path / "code.cmd", tmp_path / "x.vsix", run=lambda *a, **k: done(code=1))


# --- Visual Studio ---

def vs(version="18.10.12224.181", edition=editors.VS_EDITION, path="C:/VS"):
    return editors.VisualStudio("87bd818d", "Visual Studio Community 2026", version, Path(path), edition)


def test_which_visual_studios_can_take_the_extension():
    assert vs().supported_version and vs().supported_edition
    assert vs("17.14.2").supported_version
    assert not vs("17.13.9").supported_version
    assert not vs("16.11.0").supported_version
    assert not vs(edition="Microsoft.VisualStudio.Product.Enterprise").supported_edition


def test_vswhere_lists_the_instances_and_leaves_out_previews(tmp_path, monkeypatch):
    installer = tmp_path / "x86" / "Microsoft Visual Studio" / "Installer"
    installer.mkdir(parents=True)
    (installer / "vswhere.exe").write_text("", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "x86"))
    entries = [
        {"instanceId": "87bd818d", "displayName": "Visual Studio Community 2026", "installationVersion": "18.10.1",
         "installationPath": "C:\\VS\\18", "productId": editors.VS_EDITION, "isPrerelease": False},
        {"instanceId": "pre", "installationPath": "C:\\VS\\pre", "isPrerelease": True},
    ]
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return done(json.dumps(entries))

    found = editors.visual_studios(run=run)

    assert [f.instance_id for f in found] == ["87bd818d"] and found[0].path == Path("C:\\VS\\18")
    assert "-format" in seen[0] and "json" in seen[0]


def test_no_vswhere_means_no_visual_studio(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "nothing"))

    assert editors.visual_studios(run=lambda *a, **k: pytest.fail("ran vswhere")) == []


def test_garbage_from_vswhere_means_no_visual_studio(tmp_path, monkeypatch):
    installer = tmp_path / "Microsoft Visual Studio" / "Installer"
    installer.mkdir(parents=True)
    (installer / "vswhere.exe").write_text("", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))

    assert editors.visual_studios(run=lambda *a, **k: done("not json")) == []


MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata><Identity Id="{id}" Version="{version}" Language="en-US" Publisher="x" /></Metadata>
</PackageManifest>"""


def put_extension(local, instance_dir, folder, ext_id, version):
    target = local / "Microsoft" / "VisualStudio" / instance_dir / "Extensions" / folder
    target.mkdir(parents=True)
    (target / "extension.vsixmanifest").write_text(MANIFEST.format(id=ext_id, version=version), encoding="utf-8")


def test_the_installed_extension_version_is_read_from_this_instances_manifests(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert editors.vs_installed_version(vs()) is None  # no Visual Studio folder at all

    put_extension(tmp_path, "18.0_87bd818d", "abc.def", "Some.Other.Extension", "9.9")
    put_extension(tmp_path, "18.0_ffffffff", "xyz.123", VS_ID, "0.5.0")  # another instance's copy
    assert editors.vs_installed_version(vs()) is None

    put_extension(tmp_path, "18.0_87bd818d", "ghi.jkl", VS_ID, "1.1.0.0")
    assert editors.vs_installed_version(vs()) == "1.1.0.0"


def test_an_extension_installed_for_all_users_is_found_in_the_installation(tmp_path, monkeypatch):
    # Where Visual Studio 2026 put ours when it was installed by hand: Common7/IDE/VSExtensions/<random>.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    install = tmp_path / "VS" / "18" / "Community"
    folder = install / "Common7" / "IDE" / "VSExtensions" / "2aw0caai.lax"
    folder.mkdir(parents=True)
    (folder / "extension.vsixmanifest").write_text(MANIFEST.format(id=VS_ID, version="1.1.0.0"), encoding="utf-8")

    assert editors.vs_installed_version(vs(path=str(install))) == "1.1.0.0"
    assert editors.vs_installed_version(vs(path=str(tmp_path / "other"))) is None


def test_visual_studio_counts_as_running_only_for_its_own_devenv(monkeypatch):
    class Process:
        def __init__(self, name, exe):
            self.info = {"name": name, "exe": exe}
    mine = str(vs(path="C:/VS").devenv)
    other = [Process("notepad.exe", "C:/n.exe"), Process("devenv.exe", r"C:\Other\devenv.exe")]
    monkeypatch.setattr(editors.psutil, "process_iter", lambda attrs: other)
    assert not editors.visual_studio_running(vs(path="C:/VS"))

    monkeypatch.setattr(editors.psutil, "process_iter", lambda attrs: [Process("DEVENV.EXE", mine.upper())])
    assert editors.visual_studio_running(vs(path="C:/VS"))

    monkeypatch.setattr(editors.psutil, "process_iter", lambda attrs: [Process("devenv.exe", None)])
    assert editors.visual_studio_running(vs(path="C:/VS"))  # can't tell whose it is: assume it is


def test_the_visual_studio_vsix_is_built_with_dotnet(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(editors.shutil, "which", lambda name: "dotnet.exe")
    monkeypatch.setattr(editors, "VS_VSIX", Path("out.vsix"))  # the real output path is too long for a deep tmp_path
    seen = []

    def run(argv, cwd, **kwargs):
        seen.append((argv[1:], cwd))
        (cwd / editors.VS_VSIX).write_bytes(b"vsix")
        return done()

    vsix = editors.build_visual_studio(repo, run=run)

    assert vsix == repo / "visualstudio-extension" / "out.vsix"
    assert seen == [(["build", "ClaudePhone.Extension", "-c", "Release"], repo / "visualstudio-extension")]


def test_building_the_visual_studio_extension_without_dotnet_names_the_sdk(tmp_path, monkeypatch):
    monkeypatch.setattr(editors.shutil, "which", lambda name: None)

    with pytest.raises(editors.ToolMissing) as caught:
        editors.build_visual_studio(make_repo(tmp_path))

    assert caught.value.tool == ".NET SDK"


def test_the_vsix_installer_runs_as_administrator_and_is_pointed_at_the_instance(tmp_path):
    seen = []

    def elevate(program, arguments):
        seen.append((program, arguments))
        return 0

    assert editors.install_visual_studio(vs(path="C:/VS"), tmp_path / "x.vsix", elevate=elevate)
    installer, arguments = seen[0]
    assert installer.endswith("VSIXInstaller.exe") and "Common7" in installer
    assert arguments == ["/quiet", "/instanceIds:87bd818d", str(tmp_path / "x.vsix")]
    assert not editors.install_visual_studio(vs(), tmp_path / "x.vsix", elevate=lambda p, a: 1)  # as on the laptop
    assert not editors.install_visual_studio(vs(), tmp_path / "x.vsix", elevate=lambda p, a: None)  # prompt refused
