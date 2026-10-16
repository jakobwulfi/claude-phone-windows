"""Text to speech with swappable providers. Output is 16 kHz mono 16-bit WAV: FreeSWITCH plays it as is on a G.722
call and resamples it to 8 kHz on PCMU/PCMA, which a phone without G.722 enabled falls back to. Results are cached
by content hash. Paths use forward slashes, as FreeSWITCH wants."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

from . import config, keychain


class Provider:
    name = "base"

    def render(self, text: str, voice: str | None, out_wav: Path) -> None:
        raise NotImplementedError


def _to_wav16(src: Path, dst: Path) -> None:
    """Any audio file to 16 kHz mono 16-bit WAV. dst may carry a non-.wav suffix, so the format is named."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found: winget install Gyan.FFmpeg, then open a new terminal")
    subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", str(dst)],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


SAPI_TIMEOUT_S = 60
# Windows has two sets of voices: System.Speech's ("Microsoft David Desktop") and OneCore's ("Microsoft Mark"),
# which System.Speech can't see. A System.Speech voice is written straight to 16 kHz; a OneCore one comes as WAV at
# its own rate and is converted after. Text, voice and output path arrive as environment variables, so nothing in
# them is parsed as PowerShell.
_ONECORE = r"""
$null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime]
$onecore = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices
"""
_SAPI_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $installed = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Name -eq $env:CP_VOICE }
  if ($env:CP_VOICE -and -not $installed) {
""" + _ONECORE + r"""
    $v = $onecore | Where-Object { $_.DisplayName -eq $env:CP_VOICE } | Select-Object -First 1
    if ($v) {
      Add-Type -AssemblyName System.Runtime.WindowsRuntime
      $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
      function Await($op, [Type]$t) {
        $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
      $o = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
      $o.Voice = $v
      $stream = Await ($o.SynthesizeTextToStreamAsync($env:CP_TEXT)) ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
      $reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
      $n = Await ($reader.LoadAsync([uint32]$stream.Size)) ([uint32])
      $bytes = New-Object byte[] $n
      $reader.ReadBytes($bytes)
      [IO.File]::WriteAllBytes($env:CP_OUT, $bytes)
      exit 0
    }
  }
  # A voice that isn't installed keeps the default voice rather than leaving a call silent.
  if ($env:CP_VOICE -and $installed) { $s.SelectVoice($env:CP_VOICE) }
  $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
  $s.SetOutputToWaveFile($env:CP_OUT, $fmt)
  $s.Speak($env:CP_TEXT)
} finally { $s.Dispose() }
"""
# Every voice once: System.Speech's, then OneCore's that aren't one of those under a name without "Desktop".
_SAPI_VOICES = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$names = @{}
$s.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object {
  $names[$_.VoiceInfo.Name] = 1; $_.VoiceInfo.Name + "`t" + $_.VoiceInfo.Culture }
$s.Dispose()
try {
""" + _ONECORE + r"""
  $onecore | Where-Object { -not $names.ContainsKey($_.DisplayName + ' Desktop') } |
    ForEach-Object { $_.DisplayName + "`t" + $_.Language }
} catch {}
"""


def _powershell(script: str, env: dict[str, str] | None = None) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=SAPI_TIMEOUT_S,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"Windows speech failed: {r.stderr.strip()[:300]}")
    return r.stdout


def _ensure_wav16(path: Path) -> None:
    """Converts a WAV in place unless it's already 16 kHz mono 16-bit."""
    import wave
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2):
            return
    tmp = path.with_name(path.stem + ".16k.wav")
    _to_wav16(path, tmp)
    tmp.replace(path)


class Sapi(Provider):
    """Windows' built-in voices: System.Speech's and OneCore's."""
    name = "sapi"

    def render(self, text: str, voice: str | None, out_wav: Path) -> None:
        voice = voice or config.load().sapi_voice
        _powershell(_SAPI_SCRIPT, {"CP_TEXT": text, "CP_VOICE": voice or "", "CP_OUT": str(out_wav)})
        _ensure_wav16(out_wav)


def sapi_voices() -> list[str]:
    """Installed Windows voices, as 'name<TAB>culture' lines, for `phone voice`."""
    return [line for line in _powershell(_SAPI_VOICES).splitlines() if line.strip()]


class Piper(Provider):
    """Piper voices downloaded by `phone voice`, spoken by a worker process (voiceworker.py)."""
    name = "piper"

    def render(self, text: str, voice: str | None, out_wav: Path) -> None:
        from . import voices, voiceworker
        model = voices.piper_model(voice or "")
        if not model.exists():
            raise RuntimeError(f"Piper voice {voice} isn't downloaded")
        voiceworker.worker("piper", voices.engine_dir("piper")).render(model=str(model), text=text, out=str(out_wav))
        _ensure_wav16(out_wav)


class Kokoro(Provider):
    """Kokoro voices downloaded by `phone voice`, spoken by a worker process (voiceworker.py)."""
    name = "kokoro"

    def render(self, text: str, voice: str | None, out_wav: Path) -> None:
        from . import voices, voiceworker
        model, styles = voices.kokoro_files()
        if not (model.exists() and styles.exists()):
            raise RuntimeError("Kokoro isn't downloaded")
        voiceworker.worker("kokoro", voices.engine_dir("kokoro")).render(
            model=str(model), voices=str(styles), voice=voice or voices.KOKORO_DEFAULT, text=text, out=str(out_wav))
        _ensure_wav16(out_wav)


class ElevenLabs(Provider):
    name = "elevenlabs"
    model: str | None = None  # None: elevenlabs_model from config

    def render(self, text: str, voice: str | None, out_wav: Path) -> None:
        key = keychain.elevenlabs_key()
        if not key:
            raise RuntimeError("no ElevenLabs key (set ELEVENLABS_API_KEY or keychain item claude-phone/elevenlabs)")
        s = config.load()
        voice = voice or s.elevenlabs_voice_id
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000",
            data=json.dumps({"text": text, "model_id": self.model or s.elevenlabs_model}).encode(),
            headers={"xi-api-key": key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            pcm = r.read()
        _write_wav(out_wav, pcm, 16000)


class ElevenLabsV3(ElevenLabs):
    """Eleven v3, which acts out audio tags such as "[whispers]"."""
    name = "elevenlabs_v3"
    model = "eleven_v3"


def _write_wav(path: Path, pcm: bytes, rate: int) -> None:
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


PROVIDERS: dict[str, Provider] = {p.name: p for p in (Sapi(), Piper(), Kokoro(), ElevenLabs(), ElevenLabsV3())}
# `phone voice` calls the Windows voices "windows"; config.toml has always said "sapi".
ENGINE_ALIASES = {"windows": "sapi"}


def parse_voice(text: str) -> tuple[str, str | None]:
    """'piper:en_US-ryan-medium' -> ('piper', 'en_US-ryan-medium'); 'windows' -> ('sapi', None): the default."""
    engine, _, name = text.strip().partition(":")
    engine = engine.strip().lower()
    return ENGINE_ALIASES.get(engine, engine), name.strip() or None


def claude_voice() -> tuple[str, str | None]:
    """Claude's voice: `voice` from `phone voice`, else the older tts_provider setting."""
    s = config.load()
    if s.voice.strip():
        provider, name = parse_voice(s.voice)
        if provider in PROVIDERS:
            return provider, name
    return s.tts_provider, None


def voice_for(repo: str | None) -> tuple[str, str | None]:
    s = config.load()
    rv = s.repo_voices.get(repo or "", {}) if repo else {}
    if "provider" in rv:
        return ENGINE_ALIASES.get(rv["provider"], rv["provider"]), rv.get("voice")
    return claude_voice()


def render(text: str, repo: str | None = None, voice: tuple[str, str | None] | None = None,
           fallback: bool = True) -> str:
    """Render (or reuse) speech for text. Returns the .wav file's path, ready to play.
    `voice` is (provider, voice) and overrides the repo's voice. fallback=False: raise rather than speak with the
    Windows voice, for checking that a voice works."""
    provider, voice = voice or voice_for(repo)
    s = config.load()
    if not voice:  # name the default voice in the cache key, so changing it in config isn't masked by old speech
        voice = {"sapi": s.sapi_voice, "piper": "", "kokoro": "af_heart"}.get(provider, s.elevenlabs_voice_id)
    try:
        return _cached(provider, voice, text)
    except Exception as e:
        if provider == "sapi" or not fallback:
            raise
        from . import log
        log.get("tts").warning("%s:%s failed (%r); speaking with the Windows voice", provider, voice, e)
        # Kept under the Windows voice's own name, so the voice that failed is tried again next time.
        return _cached("sapi", s.sapi_voice, text)  # never leave a call silent


def _cached(provider: str, voice: str, text: str) -> str:
    key = hashlib.sha256(json.dumps([provider, voice, text]).encode()).hexdigest()[:20]
    base = config.SOUNDS / "tts" / key
    out = base.with_suffix(".wav")
    try:
        os.utime(out)  # housekeeping expires speech by last use
        return out.as_posix()
    except FileNotFoundError:  # not rendered yet, or just expired
        pass
    out.parent.mkdir(parents=True, exist_ok=True)
    # One temp file per render: two threads can render the same text at once (a call and the prerender).
    tmp = base.with_name(f"{key}.{os.getpid()}-{threading.get_ident()}.tmp.wav")
    try:
        PROVIDERS[provider].render(text, voice, tmp)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return out.as_posix()


async def render_async(text: str, repo: str | None = None, voice: tuple[str, str | None] | None = None) -> str:
    return await asyncio.to_thread(render, text, repo, voice)


# --- long speech, a sentence at a time ---

# A sentence ends at . ! or ? followed by a space and a capital or digit (perhaps after a quote or bracket), so
# "e.g. the" and "3.5" don't split. "Dr. Who" does; that only costs a short pause.
_NOT_AN_END = "".join(rf"(?<!\b{re.escape(a)})" for a in ("e.g.", "i.e.", "vs.", "approx.", "No.", "Dr.", "Mr.",
                                                           "Mrs.", "Ms.", "St.", "etc."))
_SENTENCE_END = re.compile(rf"(?<=[.!?]){_NOT_AN_END}[\"')\]]?\s+(?=[\"'(\[]?[A-Z0-9])")
SHORT_SENTENCE = 25  # characters; a sentence this short is spoken with the next one, so speech doesn't sound choppy


def sentences(text: str) -> list[str]:
    """Text in the parts it is spoken in: each sentence, with short ones kept with the next (a short last one with
    the one before: waiting for it costs nothing)."""
    parts: list[str] = []
    for m in _split_points(text.strip()):
        if parts and len(parts[-1]) < SHORT_SENTENCE:
            parts[-1] = f"{parts[-1]} {m}"
        else:
            parts.append(m)
    if len(parts) > 1 and len(parts[-1]) < SHORT_SENTENCE:
        parts[-2:] = [f"{parts[-2]} {parts[-1]}"]
    return parts


def _split_points(text: str) -> list[str]:
    out, start = [], 0
    for m in _SENTENCE_END.finditer(text):
        end = m.start() + len(m.group().rstrip())  # keep a closing quote or bracket with its sentence
        out.append(text[start:end].strip())
        start = m.end()
    out.append(text[start:].strip())
    return [p for p in out if p]


class Speech:
    """Speech rendered part by part in the background, in order, so a call can start playing the first part (the
    greeting) while the rest renders: Kokoro takes about 0.4 s per second of speech, so a 50-second message
    rendered whole kept the caller waiting 20 s."""

    def __init__(self, parts: list[str], repo: str | None = None,
                 voice: tuple[str, str | None] | None = None, start_after: int = 1) -> None:
        """start_after: how many parts must be rendered before the first plays."""
        loop = asyncio.get_running_loop()
        self.parts = [p for p in parts if p.strip()]
        self.start_after = min(start_after, len(self.parts))
        self._ready = [loop.create_future() for _ in self.parts]
        for f in self._ready:  # an error nobody waited for (the call never connected) isn't a warning
            f.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._task = asyncio.create_task(self._render(repo, voice))

    async def _render(self, repo: str | None, voice: tuple[str, str | None] | None) -> None:
        for i, (text, ready) in enumerate(zip(self.parts, self._ready)):
            try:
                path = await render_async(text, repo, voice)
            except Exception as e:
                ready.set_exception(e)
                for later in self._ready[i + 1:]:
                    later.cancel()
                return
            if not ready.done():
                ready.set_result(path)

    async def files(self):
        """Each part's audio file, in order, as soon as it's ready (the first once start_after parts are)."""
        if self.start_after > 1:
            await self._ready[self.start_after - 1]
        for ready in self._ready:
            yield await ready

    async def whole(self) -> str:
        """One audio file of it all, for voicemail."""
        paths = [await ready for ready in self._ready]
        return await asyncio.to_thread(join_wavs, paths)

    def cancel(self) -> None:
        self._task.cancel()
        for ready in self._ready:
            ready.cancel()


def join_wavs(paths: list[str]) -> str:
    """The parts as one WAV at 16 kHz mono 16-bit (a part that isn't is converted first), kept in the speech cache."""
    import wave
    if len(paths) == 1:
        return paths[0]
    key = hashlib.sha256(json.dumps(paths).encode()).hexdigest()[:20]
    base = config.SOUNDS / "tts" / f"joined-{key}"
    out = base.with_suffix(".wav")
    try:
        os.utime(out)  # housekeeping expires speech by last use
        return out.as_posix()
    except FileNotFoundError:
        pass
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = base.with_name(f"{base.name}.{os.getpid()}-{threading.get_ident()}.tmp.wav")
    converted = tmp.with_suffix(".part.wav")
    try:
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
            for p in paths:
                with wave.open(str(p), "rb") as part:
                    ok = (part.getframerate(), part.getnchannels(), part.getsampwidth()) == (16000, 1, 2)
                    frames = part.readframes(part.getnframes()) if ok else None
                if frames is None:  # appended raw, another format would come out garbled
                    _to_wav16(Path(p), converted)
                    with wave.open(str(converted), "rb") as part:
                        frames = part.readframes(part.getnframes())
                w.writeframes(frames)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
        converted.unlink(missing_ok=True)
    return out.as_posix()


def operator_voice() -> tuple[str, str | None]:
    s = config.load()
    return s.operator_provider, s.operator_voice


def op(text: str) -> str:
    return render(text, voice=operator_voice())


async def op_async(text: str) -> str:
    return await asyncio.to_thread(op, text)


def beep() -> str:
    """A short 880 Hz beep, generated once."""
    import math
    import struct
    base = config.SOUNDS / "beep"
    out = base.with_suffix(".wav")
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        rate, dur = 16000, 0.25
        n = int(rate * dur)
        frames = b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * 880 * i / rate) * min(1, i / 400, (n - i) / 400)))
            for i in range(n)
        )
        tmp = base.with_suffix(".tmp.wav")
        _write_wav(tmp, frames, rate)
        tmp.replace(out)
    return out.as_posix()


# Fixed phrases, pre-rendered by `phone prerender` and on daemon start.
PHRASES = {
    "no_session": "There's no session on that extension.",
    "vm_none": "You have no messages. Back to work.",
    "vm_intro": "You have {n} {questions} waiting.",
    "vm_done": "That's everything. The message light is off.",
    "vm_from": "Question from {label}.",
    "vm_keys": "Answer after the beep. Press hash to skip, or seven to delete.",
    "vm_saved": "Got it.",
    "vm_skipped": "Skipped.",
    "vm_deleted": "Deleted.",
    "vm_session_gone": "That session has closed, so I've kept your answer in the log.",
    "connected": "You're through to {label}.",
    "sent": "Sent.",
    "nothing_heard": "I didn't catch anything, so nothing was sent.",
    "transcribe_failed": "Sorry, I couldn't transcribe that. Nothing was sent.",
    "inject_failed": "I couldn't reach that tab. Nothing was sent.",
    "busy": "The line's busy.",
    "goodbye": "That's all. Bye!",
    "allhands_none": "There are no live sessions to talk to.",
    "allhands_intro": "All hands. Your message goes to {n} {sessions}.",
    "allhands_sent": "Sent to {n} {sessions}.",
}


def phrase(key: str, repo: str | None = None, **kw) -> str:
    return render(PHRASES[key].format(**kw), repo)


def spoken_label(label: str) -> str:
    """'lemon-stand/feat/ags-2' -> 'lemon stand, feat ags 2' so the voices read it naturally."""
    repo, _, branch = label.partition("/")
    clean = lambda x: " ".join(x.replace("-", " ").replace("_", " ").replace("/", " ").split())
    return f"{clean(repo)}, {clean(branch)}" if branch else clean(repo)


OPERATOR = {
    "greeting": "Operator! Number please. Who would you like to speak to?",
    "connecting": "Putting you through to {label}. Hold the line.",
    "which": "I've got {options}. Which one would you like?",
    "unplaced": "Sorry caller, I can't place that one. Try dialling the extension directly. Goodbye!",
    "again": "Sorry caller, I didn't catch that. Who was it you wanted?",
    "nobody": "Sorry caller, there's nobody on the switchboard right now. Goodbye!",
    "hold": "{label} is busy on a task. Please hold, and I'll put you through the moment it's done. "
            "Or press hash to leave your message now.",
    "important": "Your call is important to us. Please continue to hold, or press hash to leave your message now.",
}


def prerender() -> int:
    from . import hold
    beep()
    hold.render()
    n = 0
    for v in OPERATOR.values():
        if "{" not in v:
            op(v)
    for k, v in PHRASES.items():
        if "{" not in v:
            render(v)
            n += 1
    return n
