"""stdio MCP server exposing call_<owner>, notify_<owner>, transfer_call and the settings tools. One instance per Claude
Code session; the calling session is identified by CLAUDE_CODE_SESSION_ID in the env and by the VS Code terminal this
process runs under."""

from __future__ import annotations

import asyncio
import os
from typing import Literal

import aiohttp
from mcp.server.mcpserver import MCPServer

from . import config, configedit, inject

mcp = MCPServer("claude-phone")


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
        # Ring 25 s + answer 60 s + transcription stays under Claude Code's 120 s auto-background.
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
    if st == "transferred":
        return f'{S.owner} was put through to {res["to"]} (ext {res["ext"]}) and told it: "{res["owner_said"]}"'
    if st == "no_match":
        return res["message"] + " Live sessions: " + "; ".join(res["sessions"])
    if st == "connected_no_message":
        return res["message"]
    return f"The transfer didn't connect (reason: {res.get('reason')}). Tell {S.owner} on screen which session to use."


async def phone_settings(key: str | None = None) -> str:
    """Show claude-phone's settings that {owner} can change, or explain one of them.

    Use when {owner} asks about a phone setting: which calendars the meeting alerts read, whether they're on, how
    many minutes before a meeting the warning comes, the caller ID length, their name, how long Claude's calls ring,
    or what the meeting whisper says.

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

    The settings: calendar_sources (google, outlook or both, comma-separated), meeting_alerts (on/off),
    meeting_warn_minutes (1-30), callerid_max (10-40), owner_name, ring_seconds (10-40) and whisper_text (what the
    meeting whisper says). Nothing is restarted: tell {owner} the result, including what needs restarting.

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


# Tool names and descriptions carry the owner's name: call_sam, notify_sam for owner_name = "Sam".
S = config.load()
for fn, name in ((call_owner, f"call_{S.owner_slug}"), (notify_owner, f"notify_{S.owner_slug}"),
                 (transfer_call, "transfer_call"), (phone_settings, "phone_settings"),
                 (change_phone_setting, "change_phone_setting"), (change_phone, "change_phone")):
    mcp.add_tool(fn, name=name, description=fn.__doc__.format(owner=S.owner_name, slug=S.owner_slug))


def run() -> None:
    mcp.run()
