"""stdio MCP server exposing call_<owner>, notify_<owner>, transfer_call and the settings tools. One instance per Claude
Code session; the calling session is identified by CLAUDE_CODE_SESSION_ID in the env and by the VS Code terminal this
process runs under."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Literal

import aiohttp
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import config, configedit, inject

# Shown to every session with this server: what's said by phone ([By phone], [Phone answer …], [Phone transfer …],
# call_<owner>'s answer) arrives as whisper's transcript, not as typed words.
INSTRUCTIONS = (
    "Whatever the user says on the phone reaches you through speech-to-text. Read it for what the user means, not "
    "word for word. Names, numbers, repo names and technical terms could be misheard, and a hiccup can drop words. "
    "If a word looks wrong, take the most likely meaning that fits the context. If the meaning stays unclear and "
    "acting on the wrong reading would be costly or hard to undo, ask instead of guessing.")

mcp = MCPServer("claude-phone", instructions=INSTRUCTIONS)


def _body(**kw) -> dict:
    return {
        **kw,
        "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID"),
        "term_id": inject.terminal_id(),
        "cwd": os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
    }


async def _post(path: str, body: dict) -> dict:
    s = config.load()
    async with aiohttp.ClientSession() as http:
        # ring_seconds + record_seconds (at most 90 s, see config.problems) plus transcription stays under Claude
        # Code's 120 s auto-background.
        async with http.post(f"{s.daemon_url}{path}", json=body, timeout=aiohttp.ClientTimeout(total=115)) as r:
            return await r.json()


async def call_owner(question: str, context: str | None = None, screen: str | None = None,
                     from_agent: str | None = None) -> str:
    """Ring {owner}'s desk phone, ask one question out loud, and return the spoken answer as text.

    Use only when you are blocked on a decision that is genuinely {owner}'s to make. For news that
    needs no reply, use notify_{slug} instead.

    Keep it short; {owner} is listening, not reading. The question should be one sentence of about 20
    words or fewer, phrased so it can be answered out loud ("yes or no", "A or B"). Never read out
    code, paths, URLs, PR numbers or step-by-step instructions; put detail on screen and say "details
    are on your screen". If nobody answers, a voicemail is left and the answer arrives later as a
    new message in this session, so carry on with other work.

    Args:
        question: The single question, written to be spoken aloud. One sentence, about 20 words or fewer.
        context: Optional background, only if the question makes no sense without it. About 10 words.
        screen: The question summarised for the desk phone's small screen, about 8 words.
        from_agent: If you are a subagent, a short name for yourself (e.g. "reviewer").
    """
    try:
        res = await _post("/call", _body(question=question, context=context, screen=screen, from_agent=from_agent))
    except Exception as e:
        return f"The phone system is unavailable ({e.__class__.__name__}). Carry on with your best assumption."
    if res.get("status") == "answered":
        return f'{S.owner} answered by phone: "{res["answer"]}"'
    if res.get("status") == "voicemail":
        return f"{res['message']} (Reason: {res['reason']}.)"
    return res.get("message", "The call failed.")


async def notify_owner(message: str, screen: str | None = None, from_agent: str | None = None) -> str:
    """Ring {owner}'s desk phone and read a short announcement, then hang up. No reply is expected.

    Use for news {owner} would want to hear now without being asked anything: a long job finished,
    something is ready, or something broke. If nobody answers, nothing is left behind, so put
    anything that must not be missed on screen as well.

    Args:
        message: What {owner} needs to know, written to be spoken. One or two short sentences, about 25
            words or fewer. Never read out code, paths, URLs or PR numbers.
        screen: The message summarised for the desk phone's small screen, about 8 words.
        from_agent: If you are a subagent, a short name for yourself (e.g. "reviewer").
    """
    try:
        res = await _post("/notify", _body(message=message, screen=screen, from_agent=from_agent))
    except Exception as e:
        return f"The phone system is unavailable ({e.__class__.__name__}). The notification was not delivered."
    if res.get("status") == "delivered":
        return f"{S.owner} heard the notification." + (f" ({res['note']}.)" if res.get("note") else "")
    if res.get("status") == "on_screen":
        return (f"{S.owner} isn't taking calls right now ({res['reason']}), so the announcement was shown on the "
                "desk phone's screen without ringing.")
    if res.get("status") == "not_delivered":
        return (f"{S.owner} didn't pick up, so the notification was not delivered (reason: {res['reason']}). "
                "No voicemail was left.")
    return res.get("message", "The notification failed.")


async def transfer_call(to: str, reason: str, handover: str | None = None, from_agent: str | None = None) -> str:
    """Put {owner} through by phone to another live Claude Code session.

    Use when {owner} has asked you something that belongs to a different repo or session (for example a
    backend question asked in a frontend session). The phone rings, {owner} hears your reason and is
    connected to the other session; whatever {owner} says is typed into that session with your handover note.
    This returns once {owner} is being put through: what they say goes to that session, not back to you.

    Args:
        to: The session to reach, as a repo/branch name ("site-be-v2", "the backend") or its extension
            ("303"). If it doesn't match exactly one session, the live sessions are returned so you can retry.
        reason: One short spoken sentence, e.g. "That's a backend question."
        handover: Optional note for the receiving session (not spoken), e.g. what {owner} originally asked.
        from_agent: If you are a subagent, a short name for yourself.
    """
    try:
        res = await _post("/transfer", _body(to=to, reason=reason, handover=handover, from_agent=from_agent))
    except Exception as e:
        return f"The phone system is unavailable ({e.__class__.__name__}). No transfer was made."
    st = res.get("status")
    if st == "connecting":
        return (f"{S.owner} is being put through to {res['to']} (ext {res['ext']}). Whatever {S.owner_name} says is "
                "typed into that session with your handover note; nothing more comes back here.")
    if st == "no_match":
        return res["message"] + " Live sessions: " + "; ".join(res["sessions"])
    return f"The transfer didn't connect (reason: {res.get('reason')}). Tell {S.owner} on screen which session to use."


async def phone_settings(key: str | None = None) -> str:
    """Show claude-phone's settings that {owner} can change, or explain one of them.

    Use when {owner} asks about a phone setting: which calendars the meeting alerts read, whether they're on, how
    many minutes before a meeting the warning comes, the caller ID length, their name, how long Claude's calls ring,
    what the meeting whisper says, or whether claude-phone starts when VS Code opens.

    Args:
        key: One setting's name (e.g. "meeting_warn_minutes"). Leave it out to list them all.
    """
    try:
        if key:
            return await asyncio.to_thread(configedit.get, key)
        return await asyncio.to_thread(configedit.show)
    except Exception as e:  # a bad key, or a config.toml that can't be read
        text = str(e)
        return f"{text[:1].upper()}{text[1:]}."


async def change_phone_setting(key: str, value: str) -> str:
    """Change one of claude-phone's settings in its config file. Only when {owner} has asked for this change.

    The settings: {settings}.
    Nothing is restarted: tell {owner} the result, including what needs restarting.

    Args:
        key: The setting's name, e.g. "meeting_warn_minutes".
        value: The new value as text, e.g. "3", "on", "google,outlook".
    """
    try:
        return await asyncio.to_thread(configedit.set_value, key, value)
    except Exception as e:
        return f"Not changed: {e}."


async def change_phone(phone: Literal["desk", "softphone"] | None = None) -> str:
    """Choose which phone Claude's calls ring: "desk" (the desk phone) or "softphone" (e.g. Linphone on the PC).

    Leave phone out to report which one rings now and whether it's registered. Switch only when {owner} asks.
    Nothing is restarted: tell {owner} the result, including what needs restarting.

    Args:
        phone: "desk" or "softphone"; leave it out to just check.
    """
    try:
        return await asyncio.to_thread(configedit.change_phone, phone)
    except Exception as e:
        return f"Not changed: {e}."


def _prerender_elsewhere() -> None:
    """The fixed phrases in the new voice, by a `phone prerender` of its own: not in this process, which lives as
    long as the Claude session and would keep a 150-250 MB engine for 10 minutes, nor within this tool's time.
    One that is still going (the voice before) is stopped first. Best effort: phrases not ready are rendered when
    first needed."""
    from . import log, procs
    procs.kill("prerender")
    try:
        procs.spawn("prerender", [sys.executable, "-m", "claude_phone", "prerender"], log=None)
    except OSError as e:
        log.get("mcp").warning("couldn't start `phone prerender`: %r", e)


def _after_switch(now) -> None:
    """The check before switching ran Piper or Kokoro here: let go of that engine, and warm the phrases elsewhere.
    Never for ElevenLabs, where every phrase would cost credits."""
    from . import voices, voiceworker
    if now.engine in ("piper", "kokoro"):
        voiceworker.stop_here(now.engine)
    if now.engine in voices.LOCAL:
        _prerender_elsewhere()


async def change_voice(voice: str | None = None) -> str:
    """Show or change the voice Claude speaks with on the phone. Change it only when {owner} asks.

    Leave voice out to list the voices: the current one, those on this PC, and those that can be added. Voices are
    named engine:name, e.g. "windows:Microsoft Zira Desktop", "piper:en_US-ryan-medium", "kokoro:af_heart",
    "elevenlabs:<name>". A voice that isn't on this PC yet is not downloaded here: the reply gives its size and a
    command. Ask {owner} for permission (say the size), and only if they agree run that command in the terminal.
    Never ask for or handle an ElevenLabs API key: {owner} runs `phone voice key` in a terminal themselves.

    Args:
        voice: The voice to switch to, as engine:name; leave it out to list them.
    """
    from . import voices
    phone = Path(sys.executable).with_name("phone.exe").as_posix()
    try:
        if not voice:
            return await asyncio.to_thread(voices.describe)
        said = await asyncio.to_thread(voices.choose, voice, False, None, lambda _: None, False)
        await asyncio.to_thread(_after_switch, voices.current())
        return said
    except voices.NeedsDownload as e:
        return (f"Not changed: {e}. Ask {S.owner_name} whether to download it; if they agree, run "
                f"`{phone} voice {e.voice.spec} --download` in the terminal.")
    except voices.NeedsKey as e:
        return f"Not changed: {e}. Tell {S.owner_name}; don't ask for the key in chat."
    except Exception as e:
        return f"Not changed: {e}."


# Tool names and descriptions carry the owner's name: call_sam, notify_sam for owner_name = "Sam".
S = config.load()
# These change no files or settings, so Claude Code may run them in plan mode without a prompt, as it does a question
# typed in the terminal. Plan mode asks before any hook or allow rule is consulted, so only this annotation helps there.
READ_ONLY = {call_owner, notify_owner, phone_settings}
# For change_phone_setting: each one Claude may change, with what it may be and what it's for.
SETTINGS = "; ".join(f"{k} ({spec.allowed}): {config.DESCRIPTIONS[k]}" for k, spec in configedit.SPECS.items())
for fn, name in ((call_owner, f"call_{S.owner_slug}"), (notify_owner, f"notify_{S.owner_slug}"),
                 (transfer_call, "transfer_call"), (phone_settings, "phone_settings"),
                 (change_phone_setting, "change_phone_setting"), (change_phone, "change_phone"),
                 (change_voice, "change_voice")):
    mcp.add_tool(fn, name=name, description=fn.__doc__.format(owner=S.owner_name, slug=S.owner_slug,
                                                              settings=SETTINGS),
                 annotations=ToolAnnotations(read_only_hint=True) if fn in READ_ONLY else None)


def run() -> None:
    mcp.run()
