"""`phone voice`: list Claude's voices, switch between them, download the optional ones and delete them again.

Engines: the Windows voices (always there, never deleted), Piper and Kokoro (downloaded on request: the engine's
Python packages go to ~/.claude-phone/engines/<engine>, outside the venv, and its voice files to
~/.claude-phone/voices/<engine>), and ElevenLabs (online, needs an API key in Credential Manager). Voices are named
"engine:name"; "windows" alone is the Windows default voice. The operator's voice is separate (operator_voice)."""

from __future__ import annotations

import difflib
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from . import config, keychain, tts

LOCAL = ("windows", "piper", "kokoro")
ENGINES = (*LOCAL, "elevenlabs")
PIP = {"piper": "piper-tts==1.8.0", "kokoro": "kokoro-onnx==0.6.1"}
# On disk once installed, with onnxruntime and numpy (measured on Windows x64, Python 3.13).
ENGINE_BYTES = {"piper": 152_000_000, "kokoro": 129_000_000}
INSTALLED_MARK = ".installed"  # written last, so a half-finished install doesn't count
PIP_TIMEOUT_S = 900
DELETE_TRIES, DELETE_WAIT_S = 5, 0.5  # a stopped worker can take a moment to let go of its files

PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
PIPER_CATALOGUE_DAYS = 7
KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
# The int8 model: a third the size of the full one, for nearly the same sound. name -> (bytes, sha256)
KOKORO_FILES = {
    "kokoro-v1.0.int8.onnx": (92_361_271, "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb"),
    "voices-v1.0.bin": (28_214_398, "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
}
# The first letter is the language (a American English, b British, e Spanish, f French, h Hindi, i Italian,
# j Japanese, p Portuguese, z Chinese), the second f or m.
KOKORO_VOICES = (
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova", "af_river",
    "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck",
    "am_santa", "bf_alice", "bf_emma", "bf_isabella", "bf_lily", "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "ef_dora", "em_alex", "em_santa", "ff_siwis", "hf_alpha", "hf_beta", "hm_omega", "hm_psi", "if_sara", "im_nicola",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo", "pf_dora", "pm_alex", "pm_santa",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi", "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
)
ELEVENLABS_API = "https://api.elevenlabs.io/v1"
ELEVENLABS_KEY = "elevenlabs"  # the Credential Manager item
HTTP_TIMEOUT_S = 30
SAMPLE = "Hello, this is Claude. This is how I'll sound on the phone."


class NeedsDownload(Exception):
    """The voice isn't on this PC; downloading it needs the user's yes."""

    def __init__(self, voice: Voice, size: int) -> None:
        super().__init__(f"{_cap(voice.label)} needs a one-off download of about {_mb(size)}")
        self.voice, self.size = voice, size


class NeedsKey(Exception):
    """ElevenLabs needs the user's API key, which only they may type in."""


@dataclass(frozen=True)
class Voice:
    engine: str  # one of ENGINES
    name: str  # "" for the engine's default voice

    @property
    def spec(self) -> str:
        return f"{self.engine}:{self.name}" if self.name else self.engine

    @property
    def label(self) -> str:
        if self.engine == "windows":
            return f"the Windows voice {self.name}" if self.name else "the Windows default voice"
        if self.engine == "elevenlabs":
            return f"the ElevenLabs voice {self.name}" if self.name else "the default ElevenLabs voice"
        return f"the {self.engine.capitalize()} voice {self.name}"


DEFAULT = Voice("windows", "")


def _mb(n: int) -> str:
    return f"{n / 1e6:,.0f} MB"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


# --- where things live ---

def engine_dir(engine: str) -> Path:
    return config.ENGINES / engine


def piper_model(key: str) -> Path:
    # The name can come from a hand-edited config.toml (voice, repo_voices): keep it inside the voices folder.
    if not key or Path(key).name != key or "/" in key:
        raise ValueError(f"{key!r} is not a Piper voice name")
    return config.VOICES / "piper" / f"{key}.onnx"


def kokoro_files() -> tuple[Path, Path]:
    folder = config.VOICES / "kokoro"
    return folder / "kokoro-v1.0.int8.onnx", folder / "voices-v1.0.bin"


def engine_installed(engine: str) -> bool:
    return (engine_dir(engine) / INSTALLED_MARK).exists()


def piper_installed() -> list[str]:
    folder = config.VOICES / "piper"
    if not folder.exists():
        return []
    return sorted(p.stem for p in folder.glob("*.onnx") if p.with_suffix(".onnx.json").exists())


def kokoro_installed() -> bool:
    return engine_installed("kokoro") and all(p.exists() for p in kokoro_files())


def installed(v: Voice) -> bool:
    if v.engine == "windows":
        return True
    if v.engine == "piper":
        return engine_installed("piper") and v.name in piper_installed()
    if v.engine == "kokoro":
        return kokoro_installed()
    return bool(elevenlabs_key())


def elevenlabs_key() -> str | None:
    try:
        return keychain.elevenlabs_key()
    except Exception:  # Credential Manager failing counts as no key
        return None


def current() -> Voice:
    provider, name = tts.claude_voice()
    if provider.startswith("elevenlabs"):
        return Voice("elevenlabs", name or tts.ELEVENLABS_DEFAULT)
    return Voice(provider, name or (tts.KOKORO_DEFAULT if provider == "kokoro" else ""))


# --- the voices there are ---

def windows_voices() -> list[tuple[str, str]]:
    """(name, language) of each Windows voice."""
    return [(*line.split("\t", 1), "")[:2] for line in tts.windows_voice_lines()]


def _fetch(url: str, headers: dict | None = None) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=HTTP_TIMEOUT_S) as r:
        return r.read()


def piper_catalogue() -> dict:
    """Piper's list of voices (name -> language, files and sizes), fetched at most once a week and kept."""
    path = config.VOICES / "piper" / "voices.json"
    kept = _read_catalogue(path)
    if kept is not None and time.time() - path.stat().st_mtime < PIPER_CATALOGUE_DAYS * 86400:
        return kept
    try:
        data = _fetch(PIPER_BASE + "voices.json")
        if not isinstance(json.loads(data), dict):
            raise ValueError("it isn't a list of voices")
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        part.write_bytes(data)
        part.replace(path)  # never a half-written list
        return json.loads(data)
    except (OSError, ValueError, http.client.HTTPException) as e:
        if kept is None:
            raise ValueError(f"couldn't fetch Piper's list of voices ({e}); check the internet "
                             "connection") from None
        os.utime(path)  # offline: keep the old list, without waiting on the network again each time
        return kept


def _read_catalogue(path: Path) -> dict | None:
    """The kept list of Piper voices, or None if there's none or it's damaged (which is then deleted)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        return data
    path.unlink(missing_ok=True)
    return None


def elevenlabs_voices(key: str) -> list[tuple[str, str]]:
    """(name, voice id) of each voice on the ElevenLabs account."""
    try:
        data = json.loads(_fetch(f"{ELEVENLABS_API}/voices", {"xi-api-key": key}))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ValueError("ElevenLabs refused that key (it needs the Text to Speech and Voices: Read "
                             "permissions)") from None
        raise ValueError(f"ElevenLabs answered with error {e.code}") from None
    except OSError as e:
        raise ValueError(f"couldn't reach ElevenLabs ({e})") from None
    return [(v["name"], v["voice_id"]) for v in data.get("voices", [])]


def _pick(engine: str, name: str, choices: dict[str, str]) -> Voice:
    """choices: lower-case spelling -> the voice's real name."""
    hit = choices.get(name.lower())
    if hit is not None:
        return Voice(engine, hit)
    close = dict.fromkeys(choices[c] for c in difflib.get_close_matches(name.lower(), list(choices), n=6, cutoff=0.5))
    hint = (f" Did you mean {' or '.join(f'{engine}:{c}' for c in list(close)[:3])}?" if close
            else f" `phone voice list {engine}` lists them.")
    raise ValueError(f"there's no {engine} voice called {name!r}.{hint}")


def resolve(text: str) -> Voice:
    """The voice a user means: "engine:name", "engine" for its default, or a name only one engine has."""
    raw = " ".join(text.split())
    word, sep, name = raw.partition(":")
    engine, name = config.engine(word), name.strip()
    if engine not in ENGINES:
        if not sep:
            return _guess(raw)
        raise ValueError(f"there's no voice engine called {word!r}: use {', '.join(ENGINES)}")
    if engine == "windows":
        return _pick("windows", name, {n.lower(): n for n, _ in windows_voices()}) if name else DEFAULT
    if engine == "kokoro":
        return _pick("kokoro", name, {n: n for n in KOKORO_VOICES}) if name else Voice("kokoro", tts.KOKORO_DEFAULT)
    if engine == "piper":
        if not name:
            raise ValueError("which Piper voice? `phone voice list piper en_US` lists the American English ones")
        if name in piper_installed():
            return Voice("piper", name)
        cat = piper_catalogue()
        choices = {a.lower(): k for k, v in cat.items() for a in v.get("aliases", [])}
        choices.update({k.lower(): k for k in cat})
        return _pick("piper", name, choices)
    key = elevenlabs_key()
    if not name or not key:
        return Voice("elevenlabs", name)  # checked once there's a key
    found = elevenlabs_voices(key)
    choices = {i.lower(): i for _, i in found}
    choices.update({n.lower(): i for n, i in found})
    return _pick("elevenlabs", name, choices)


def _guess(name: str) -> Voice:
    """A bare name: whichever engine has a voice by that name."""
    low = name.lower()
    hits = [Voice("windows", n) for n, _ in windows_voices() if n.lower() == low]
    hits += [Voice("kokoro", n) for n in KOKORO_VOICES if n == low]
    hits += [Voice("piper", n) for n in piper_installed() if n.lower() == low]
    if not hits:
        try:
            hits = [Voice("piper", k) for k in piper_catalogue() if k.lower() == low]
        except ValueError:
            pass
    if len(hits) == 1:
        return hits[0]
    raise ValueError(f"which voice is {name!r}? Name it as engine:name, e.g. windows:Microsoft Zira Desktop, "
                     "piper:en_US-ryan-medium or kokoro:af_heart; `phone voice` lists them")


# --- downloading ---

def download_size(v: Voice) -> int:
    """Bytes still to download and install for this voice."""
    if v.engine not in ("piper", "kokoro"):
        return 0
    size = 0 if engine_installed(v.engine) else ENGINE_BYTES[v.engine]
    if v.engine == "piper" and v.name not in piper_installed():
        size += sum(f["size_bytes"] for f in piper_catalogue()[v.name]["files"].values())
    if v.engine == "kokoro":
        size += sum(n for f, (n, _) in KOKORO_FILES.items() if not (config.VOICES / "kokoro" / f).exists())
    return size


def _download(url: str, dest: Path, size: int, digest: str, algo: str, say: Callable[[str], None]) -> None:
    """To a .part file, checked against its size and hash, then renamed into place."""
    say(f"Downloading {dest.name} ({_mb(size)}) ...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.new(algo)
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                if f.tell() + len(chunk) > size:  # more than announced: stop before it fills the disk
                    raise ValueError(f"{dest.name} arrived damaged; try again")
                f.write(chunk)
                h.update(chunk)
        if part.stat().st_size != size or h.hexdigest() != digest:
            raise ValueError(f"{dest.name} arrived damaged; try again")
        part.replace(dest)
    except (OSError, http.client.HTTPException) as e:
        raise ValueError(f"couldn't download {dest.name} ({e})") from None
    finally:
        part.unlink(missing_ok=True)


def _pip_install(engine: str, say: Callable[[str], None]) -> None:
    """The engine's Python packages into a folder of their own: the venv can't change while claude-phone runs,
    and deleting the engine is then deleting the folder."""
    say(f"Installing the {engine.capitalize()} engine (about {_mb(ENGINE_BYTES[engine])}) ...")
    target = engine_dir(engine)
    part = target.with_name(engine + ".part")
    shutil.rmtree(_long(part), ignore_errors=True)
    shutil.rmtree(_long(target), ignore_errors=True)
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                            "--quiet", "--target", str(part), PIP[engine]],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=PIP_TIMEOUT_S, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        shutil.rmtree(_long(part), ignore_errors=True)
        raise ValueError(f"couldn't install the {engine} engine ({e})") from None
    if r.returncode != 0:
        shutil.rmtree(_long(part), ignore_errors=True)
        last = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["pip failed"]
        raise ValueError(f"couldn't install the {engine} engine: {last[0][:200]}")
    # A virus scanner often still has the new DLLs open for a moment.
    _retry(lambda: part.rename(target), f"couldn't put the {engine} engine in place ({target} is in use); try again")
    (target / INSTALLED_MARK).write_text(PIP[engine], encoding="utf-8")


def _retry(action: Callable[[], None], failure: str, between: Callable[[], None] = lambda: None) -> None:
    """Windows often holds a file a moment longer: a stopped worker, a virus scanner, the indexer."""
    for attempt in range(DELETE_TRIES):
        try:
            action()
            return
        except OSError:
            if attempt == DELETE_TRIES - 1:
                raise ValueError(failure) from None
            between()
            time.sleep(DELETE_WAIT_S)


def install(v: Voice, say: Callable[[str], None] = print) -> None:
    if v.engine not in ("piper", "kokoro"):
        return
    try:
        _sweep()
    except OSError:
        pass  # still held; it goes next time
    if not engine_installed(v.engine):
        _pip_install(v.engine, say)
    if v.engine == "piper" and v.name not in piper_installed():
        files = piper_catalogue()[v.name]["files"]
        # The model first and its .json last: a voice counts as downloaded once both are there.
        for suffix in (".onnx", ".onnx.json"):
            path, meta = next((p, m) for p, m in files.items() if p.endswith(suffix))
            dest = piper_model(v.name).with_suffix(suffix)
            _download(PIPER_BASE + path, dest, meta["size_bytes"], meta["md5_digest"], "md5", say)
    if v.engine == "kokoro":
        for name, (size, sha) in KOKORO_FILES.items():
            dest = config.VOICES / "kokoro" / name
            if not dest.exists():
                _download(KOKORO_BASE + name, dest, size, sha, "sha256", say)


# --- switching ---

def _save(v: Voice) -> None:
    from . import configedit
    spec = v.spec
    if v.engine == "elevenlabs" and v.name and tts.claude_voice()[0] == "elevenlabs_v3":
        spec = f"elevenlabs_v3:{v.name}"  # keep Eleven v3, which acts out audio tags such as "[whispers]"
    configedit.write_setting("voice", spec)


def _back_to_windows() -> str:
    """Switches to the Windows default voice and says which voice that is."""
    _save(DEFAULT)
    return f"Claude now speaks with {current().label}. "


def choose(text: str, download: bool = False, ask: Callable[[str], bool] | None = None,
           say: Callable[[str], None] = print, warm: bool = True) -> str:
    """Make Claude speak with this voice. Downloads only with download=True or a yes from ask(question); otherwise
    raises NeedsDownload. ElevenLabs without a key raises NeedsKey. warm: render the fixed phrases in it now."""
    v = resolve(text)
    if v.engine == "elevenlabs" and not elevenlabs_key():
        raise NeedsKey("ElevenLabs voices need your ElevenLabs API key: run `phone voice key` in a terminal and "
                       "paste it there (it's kept in Windows Credential Manager)")
    if not installed(v):
        size = download_size(v)
        if not download and not (ask and ask(f"{_cap(v.label)} needs a one-off download of about {_mb(size)}. "
                                             "Download it?")):
            raise NeedsDownload(v, size)
        install(v, say)
    if v.engine in LOCAL:  # ElevenLabs was checked against the account already, and a sample would cost credits
        _check(v)
    _save(v)
    if warm and v.engine in LOCAL:
        say("Preparing the phone's fixed phrases in the new voice ...")
        tts.prerender()
    return f"Claude now speaks with {v.label}, from the next thing it says; nothing needs restarting."


def try_voice(text: str) -> str:
    """Renders a sample in an installed voice, without switching to it. Returns the audio file's path."""
    v = resolve(text)
    if not installed(v):
        raise ValueError(f"{v.label} isn't downloaded yet: `phone voice {v.spec}` downloads it")
    return _speak(v, SAMPLE)


def _failed(v: Voice, e: Exception) -> ValueError:
    return ValueError(f"{v.label} didn't work ({e}); Claude still speaks with {current().label}")


def _speak(v: Voice, text: str) -> str:
    """Speech in exactly this voice: a broken engine raises instead of the Windows voice standing in for it."""
    try:
        return tts.render(text, voice=(v.engine, v.name or None), fallback=False)
    except Exception as e:
        raise _failed(v, e) from None


def _check(v: Voice) -> None:
    """That the voice speaks now, before switching to it: rendered afresh, as speech cached while the engine still
    worked would hide a broken one."""
    out = config.SOUNDS / "tts" / f"check-{os.getpid()}.tmp.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        tts.PROVIDERS[v.engine].render(SAMPLE, v.name or None, out)
    except Exception as e:
        raise _failed(v, e) from None
    finally:
        out.unlink(missing_ok=True)


# --- deleting ---

def _long(path: Path) -> Path:
    """The \\\\?\\ form, so files deep inside an engine's packages are reachable past Windows' 260-character
    path limit."""
    return Path("\\\\?\\" + str(path.resolve()))


def _size(path: Path) -> int:
    path = _long(path)
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


TRASH = ".deleting"


def _sweep() -> None:
    """Finishes deletions that Windows held up last time (a virus scanner or indexer still had a file open)."""
    for folder in (config.ENGINES, config.VOICES, config.VOICES / "piper"):
        for p in folder.glob(f"*{TRASH}") if folder.exists() else ():
            if p.is_dir():
                shutil.rmtree(_long(p), ignore_errors=True)
            else:
                p.unlink(missing_ok=True)


def _delete(paths: list[Path], engine: str) -> int:
    """Each path is renamed out of the way first, so it's gone at once even if Windows holds up the deleting."""
    from . import voiceworker
    voiceworker.stop_all(engine)  # a running worker holds the files open
    paths = [p for p in paths if p.exists()]
    freed = sum(_size(p) for p in paths)
    for p in paths:
        trash = p.with_name(p.name + TRASH)

        def move(p=p, trash=trash) -> None:
            if trash.exists():
                if trash.is_dir():
                    shutil.rmtree(_long(trash))
                else:
                    trash.unlink()
            p.rename(trash)
        # Stopped again between tries: a render that was under way when the worker was stopped starts a new one.
        _retry(move, f"{p} is in use; stop claude-phone (`phone down`) and try again",
               lambda: voiceworker.stop_all(engine))
    for _ in range(DELETE_TRIES):
        try:
            _sweep()
            break
        except OSError:
            time.sleep(DELETE_WAIT_S)  # what's left goes next time
    return freed


def remove(text: str) -> str:
    """Deletes a downloaded voice, or forgets the ElevenLabs key. The voice in use is switched to the Windows
    default voice first. The Windows voices can't be deleted."""
    raw = " ".join(text.split())
    word, _, name = raw.partition(":")
    engine, name = config.engine(word), name.strip()
    if engine not in ENGINES:
        v = resolve(raw)
        engine, name = v.engine, v.name
    if engine == "windows":
        raise ValueError("the Windows voices come with Windows, so they can't be deleted here")
    now = current()
    if engine == "elevenlabs":
        return _forget_key(now)
    if engine == "piper":
        mine = piper_installed()
        if name and name not in mine:
            raise ValueError(f"the Piper voice {name!r} isn't downloaded, so there's nothing to delete")
        if not mine and not engine_installed("piper"):
            raise ValueError("Piper isn't downloaded, so there's nothing to delete")
        targets = [name] if name else mine
        left = [k for k in mine if k not in targets]
        paths = [p for k in targets for p in (piper_model(k), piper_model(k).with_suffix(".onnx.json"))]
        if not left:
            paths.append(engine_dir("piper"))
        in_use = now.engine == "piper" and now.name in targets
        what = f"the Piper voice {name}" if name else "Piper and its voices"
        extra = "" if left or not name else " That was the last Piper voice, so the Piper engine went too."
    else:
        if not engine_installed("kokoro") and not any(p.exists() for p in kokoro_files()):
            raise ValueError("Kokoro isn't downloaded, so there's nothing to delete")
        paths, in_use = [config.VOICES / "kokoro", engine_dir("kokoro")], now.engine == "kokoro"
        what, extra = "Kokoro", " Its voices come as one download, so all of them went, with the engine."
    switched = _back_to_windows() if in_use else ""
    freed = _delete(paths, engine)
    return switched + f"Deleted {what}, freeing {_mb(freed)}.{extra}" + _repo_warning(engine, name)


def _forget_key(now: Voice) -> str:
    if not elevenlabs_key():
        raise ValueError("there's no ElevenLabs key stored, so there's nothing to delete")
    switched = _back_to_windows() if now.engine == "elevenlabs" else ""
    keychain.delete(ELEVENLABS_KEY)
    note = " ELEVENLABS_API_KEY is still set in the environment, so that one is still used." if elevenlabs_key() \
        else ""
    return switched + f"Forgot the ElevenLabs key.{note}"


def _repo_warning(engine: str, name: str) -> str:
    used = {r: tts.parse_voice(rv) for r, rv in config.load().repo_voices.items() if isinstance(rv, str)}
    repos = [r for r, (e, n) in used.items() if e == engine and (not name or n == name)]
    return (f" Note: repo_voices for {', '.join(repos)} used it, so those repos now get the Windows default voice."
            if repos else "")


# --- the ElevenLabs key ---

def set_key(raw: str) -> str:
    """Checks the key against ElevenLabs, then keeps it in Credential Manager."""
    key = raw.strip()
    if not key:
        raise ValueError("no key given")
    found = elevenlabs_voices(key)
    keychain.put(ELEVENLABS_KEY, key)
    names = ", ".join(n for n, _ in found[:8]) + (" ..." if len(found) > 8 else "")
    return (f"Saved the ElevenLabs key. Your account has {len(found)} voice(s){': ' + names if found else ''}. "
            "Switch with `phone voice elevenlabs:<name>`.")


# --- listing ---

def describe() -> str:
    """What `phone voice` prints: the voice now, what's on this PC, and what can be added."""
    now = current()
    lines = [f"Claude speaks with {now.label} ({now.spec}).", "", "On this PC:"]
    lines += [f"  windows:{n}  ({lang})" for n, lang in windows_voices()]
    if engine_installed("piper"):
        lines += [f"  piper:{k}" for k in piper_installed()]
    if kokoro_installed():
        lines.append(f"  kokoro: all {len(KOKORO_VOICES)} voices (`phone voice list kokoro`)")
    if elevenlabs_key():
        lines.append("  elevenlabs: key stored (`phone voice list elevenlabs`)")
    lines += ["", "Can be added:",
              f"  piper: free and offline, 20-120 MB per voice plus the engine once ({_mb(ENGINE_BYTES['piper'])}); "
              "`phone voice list piper en_US` lists them"]
    if not kokoro_installed():
        total = ENGINE_BYTES["kokoro"] + sum(n for n, _ in KOKORO_FILES.values())
        lines.append(f"  kokoro: free and offline, the most natural; {len(KOKORO_VOICES)} voices in one download "
                     f"of {_mb(total)}; `phone voice list kokoro` lists them")
    if not elevenlabs_key():
        lines.append("  elevenlabs: online, paid per character; needs your API key (`phone voice key`)")
    lines += ["", "Switch: phone voice <engine:name>    Hear one first: phone voice try <engine:name>",
              "Delete: phone voice remove <engine:name>"]
    return "\n".join(lines)


def list_engine(engine: str, match: str = "") -> str:
    engine, want = config.engine(engine), match.strip().lower()
    if engine == "windows":
        rows = [f"windows:{n}  ({lang})" for n, lang in windows_voices()]
    elif engine == "kokoro":
        rows = [f"kokoro:{n}" for n in KOKORO_VOICES]
    elif engine == "piper":
        mine = set(piper_installed())
        rows = []
        for k, v in sorted(piper_catalogue().items()):
            lang, size = v["language"], sum(f["size_bytes"] for f in v["files"].values())
            rows.append(f"piper:{k}  {lang.get('name_english', '')} ({lang.get('country_english', '')}), "
                        f"{'downloaded' if k in mine else _mb(size)}")
    elif engine == "elevenlabs":
        key = elevenlabs_key()
        if not key:
            return "No ElevenLabs key stored: run `phone voice key` first."
        rows = [f"elevenlabs:{n}  (id {i})" for n, i in elevenlabs_voices(key)]
    else:
        raise ValueError(f"there's no voice engine called {engine!r}: use {', '.join(ENGINES)}")
    rows = [r for r in rows if want in r.lower()]
    if not rows:
        return f"No {engine} voice matches {match!r}."
    if engine == "kokoro":
        here = "downloaded" if kokoro_installed() else "not downloaded yet"
        rows.append(f"({here}; one download for all {len(KOKORO_VOICES)})")
    return "\n".join(rows)
