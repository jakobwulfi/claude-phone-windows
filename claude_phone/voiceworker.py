"""Piper and Kokoro speak from a worker process of their own, started on first use and stopped when idle:
their Python packages live outside the venv (~/.claude-phone/engines/<engine>, see voices.py), a loaded model
holds 150-250 MB, and only ending the process gives that memory back and unlocks the files for `phone voice
remove`. One JSON request per line on stdin, one JSON reply per line on stdout. The Windows voice uses the same
client with a PowerShell loop (tts.WINDOWS_WORKER), so System.Speech loads once rather than for every sentence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

IDLE_S = 600  # a worker unused this long is stopped
START_TIMEOUT_S = 30  # to say it's ready: Python starting, before any model loads
# One request. The first loads the model (measured: 6.4 s Piper, 2.3 s Kokoro); after that a hung engine shouldn't
# keep a call silent for long before the Windows voice stands in.
FIRST_RENDER_TIMEOUT_S = 90
RENDER_TIMEOUT_S = 30
MODULE = "claude_phone.voiceworker"

# Kokoro's voice names start with their language: a = American English, b = British English, ...
KOKORO_LANG = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi", "i": "it", "j": "ja",
               "p": "pt-br", "z": "cmn"}


# --- the worker process ---

# Speech now renders while a call plays (a sentence at a time), on the same PC as FreeSWITCH and often the
# softphone. onnxruntime takes every core and spins its threads between steps by default, which made the start of a
# call stutter, like whisper's spinning OpenMP threads did. Half the physical cores, sleeping when idle: measured,
# Kokoro then renders at 0.45x real time instead of 0.36x, still well ahead of playback.
ENGINE_THREADS = max(1, (psutil.cpu_count(logical=False) or 2) // 2)  # with or without hyperthreading
_calmed = False


def _calm_onnxruntime() -> None:
    """Every model the engines load (Piper and kokoro_onnx each make their own InferenceSession) gets these
    options. Patched here because neither engine lets us pass them."""
    global _calmed
    if _calmed:
        return
    import onnxruntime as ort
    real = ort.InferenceSession

    def calm(path, sess_options=None, *args, **kwargs):
        opts = sess_options or ort.SessionOptions()
        opts.intra_op_num_threads, opts.inter_op_num_threads = ENGINE_THREADS, 1
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        return real(path, opts, *args, **kwargs)
    ort.InferenceSession = calm
    _calmed = True


# Spoken once as each voice loads, into nothing: the first inference allocates and is several times slower, and
# as part of a call it made the start stutter.
WARM_UP_TEXT = "Ready."


def _piper(models: dict, req: dict) -> None:
    import io
    import wave
    _calm_onnxruntime()
    from piper import PiperVoice
    voice = models.get(req["model"])
    if voice is None:
        voice = models[req["model"]] = PiperVoice.load(req["model"])
        with wave.open(io.BytesIO(), "wb") as w:
            voice.synthesize_wav(WARM_UP_TEXT, w)
    with wave.open(req["out"], "wb") as w:
        voice.synthesize_wav(req["text"], w)


def _kokoro(models: dict, req: dict) -> None:
    import wave
    import numpy as np
    _calm_onnxruntime()
    from kokoro_onnx import Kokoro
    key = (req["model"], req["voices"])
    k = models.get(key)
    lang = KOKORO_LANG.get(req["voice"][:1], "en-us")
    if k is None:
        k = models[key] = Kokoro(req["model"], req["voices"])
        k.create(WARM_UP_TEXT, voice=req["voice"], speed=1.0, lang=lang)
    samples, rate = k.create(req["text"], voice=req["voice"], speed=1.0, lang=lang)
    with wave.open(req["out"], "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


RENDERERS = {"piper": _piper, "kokoro": _kokoro}


def _reply_stream():
    """fd 1 kept for the replies alone. espeak (Piper's) and onnxruntime write to fd 1 from C, past sys.stdout,
    and that would break a reply line; from here on fd 1 is the log."""
    replies = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(replies, "w", encoding="utf-8")


def serve(engine: str, out=None) -> None:
    render, models = RENDERERS[engine], {}
    out = out or sys.stdout
    sys.stdout = sys.stderr  # a library's print() must not break the replies
    out.write(json.dumps({"ready": True}) + "\n")
    out.flush()
    for line in sys.stdin:
        try:
            render(models, json.loads(line))
            reply = {"ok": True}
        except Exception as e:
            reply = {"error": f"{e.__class__.__name__}: {e}"[:300]}
        out.write(json.dumps(reply) + "\n")
        out.flush()


# --- the client, in the daemon or the `phone` command ---

class Unavailable(RuntimeError):
    """The worker itself is down: it didn't start, died, hung or answered nonsense. Unlike an error it answered
    with (the text or the output path was the trouble), another way of speaking may work."""


class Worker:
    """One engine's worker process, running command. Requests are serialised; a worker that died is started again.
    first_timeout: how long the first request may take, model loading included (None: FIRST_RENDER_TIMEOUT_S)."""

    def __init__(self, engine: str, command: list[str], env: dict[str, str] | None = None,
                 first_timeout: float | None = None) -> None:
        self.engine, self.command, self.env, self.first_timeout = engine, command, env or {}, first_timeout
        self.proc: subprocess.Popen | None = None
        self.answered = False  # the running worker has answered once, so its model is loaded
        self.lock = threading.Lock()
        self.used = 0.0
        self.timer: threading.Timer | None = None

    def _log(self) -> Path:
        from . import config
        return config.LOGS / f"voiceworker-{self.engine}.log"

    def _start(self) -> subprocess.Popen:
        self._log().parent.mkdir(parents=True, exist_ok=True)
        with open(self._log(), "a", encoding="utf-8") as log:  # the child keeps its own handle
            proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=log, text=True, encoding="utf-8", env={**os.environ, **self.env},
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        try:
            ready = json.loads(_readline(proc, START_TIMEOUT_S) or "{}").get("ready") is True
        except (ValueError, TimeoutError):
            ready = False
        if not ready:
            proc.kill()
            raise Unavailable(f"the {self.engine} engine didn't start (see {self._log()}); "
                              "`phone voice` can install it again")
        return proc

    def _ask(self, req: dict) -> dict | None:
        """One request to a running worker. None: it had died (stopped by `phone voice remove`, or crashed)."""
        if self.proc is None or self.proc.poll() is not None:
            self.proc, self.answered = self._start(), False
        timeout = RENDER_TIMEOUT_S if self.answered else (self.first_timeout or FIRST_RENDER_TIMEOUT_S)
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
            line = _readline(self.proc, timeout)
        except TimeoutError:  # before OSError, which it is a kind of
            self.proc = None  # _readline killed it
            raise Unavailable(f"the {self.engine} engine didn't answer in {timeout} s") from None
        except OSError:
            line = ""
        if not line:
            self.proc = None
            return None
        try:
            reply = json.loads(line)
            self.answered = True
            return reply
        except ValueError:
            self.proc.kill()
            self.proc = None
            raise Unavailable(f"the {self.engine} engine answered {line[:80]!r}") from None

    def render(self, **req) -> None:
        with self.lock:
            reply = self._ask(req) or self._ask(req)
            self.used = time.monotonic()
            self._arm()
        if reply is None:
            raise Unavailable(f"the {self.engine} engine stopped")
        if "error" in reply:
            raise RuntimeError(reply["error"])

    def _arm(self) -> None:
        if self.timer:
            self.timer.cancel()
        self.timer = threading.Timer(IDLE_S, self._idle)
        self.timer.daemon = True
        self.timer.start()

    def _idle(self) -> None:
        with self.lock:
            if time.monotonic() - self.used >= IDLE_S - 1:
                self.stop()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.stdin.close()  # it exits at the end of its input
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


def _readline(proc, timeout: float) -> str:
    """The worker's next line. A worker that hangs (a model that won't load, text that trips the engine) is killed
    rather than holding up every later render, so the caller can fall back to the Windows voice."""
    line: list[str] = []
    reader = threading.Thread(target=lambda: line.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        proc.kill()  # which ends the read too
        raise TimeoutError
    return line[0] if line else ""


_workers: dict[str, Worker] = {}
_workers_lock = threading.Lock()


def command_worker(engine: str, command: list[str], env: dict[str, str] | None = None,
                   first_timeout: float | None = None) -> Worker:
    """This process's worker for the engine, made with these arguments the first time it's asked for (later
    arguments are ignored: one engine, one command)."""
    with _workers_lock:
        if engine not in _workers:
            _workers[engine] = Worker(engine, command, env, first_timeout)
        return _workers[engine]


def worker(engine: str, path: Path) -> Worker:
    """This process's worker for a Python engine (Piper, Kokoro), whose packages are in path."""
    return command_worker(engine, [sys.executable, "-m", MODULE, engine],
                          {"PYTHONPATH": str(path), "PYTHONIOENCODING": "utf-8"})


def is_warm(engine: str) -> bool:
    """This process has the engine running with a model loaded, so speech starts at once."""
    with _workers_lock:
        w = _workers.get(engine)
    return bool(w and w.proc and w.proc.poll() is None and w.answered)


def stop_here(engine: str) -> None:
    """Stops this process's worker for the engine, if it has one."""
    with _workers_lock:
        w = _workers.get(engine)
    if w:
        with w.lock:
            w.stop()


def stop_all(engine: str) -> None:
    """Stops this engine's workers in every process (the daemon's too), so its files can be deleted."""
    stop_here(engine)
    for p in psutil.process_iter(["cmdline"]):
        cmd = p.info.get("cmdline") or []
        if MODULE in cmd and cmd[-1:] == [engine]:
            try:
                p.kill()
                p.wait(timeout=5)
            except (psutil.Error, psutil.TimeoutExpired):
                pass


if __name__ == "__main__":
    serve(sys.argv[1], _reply_stream())
