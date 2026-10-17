"""The daemon's HTTP side: the API on 127.0.0.1 (the MCP server's calls, `phone status`, the VS Code extension)
and the listener on the LAN (the desk phone's phonebook and DND key). Each handler takes the Controller."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from ipaddress import ip_address
from typing import TYPE_CHECKING

from aiohttp import web

from . import config, diary, log, registry
from .phonebook import browser_xml, phonebook_xml

if TYPE_CHECKING:
    from .daemon import Controller

lg = log.get("daemon")


def bad_request(message: str) -> web.HTTPBadRequest:
    """A 400 in the shape the MCP tools read: they pass `message` on to Claude."""
    return web.HTTPBadRequest(text=json.dumps({"status": "error", "message": message}),
                              content_type="application/json")


async def json_body(request: web.Request) -> dict:
    """The request's JSON object; anything else is refused with a 400."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise bad_request("the request body must be a JSON object")
    return body


# --- the API on 127.0.0.1 ---

async def http_transfer(c: Controller, request: web.Request) -> web.Response:
    """Replies "connecting" as soon as the owner is put through: a hold behind the target's running turn
    would outlast the tool call. The rest runs on and is only logged. Earlier outcomes (no match, not
    answered, an error) are the reply."""
    body = await json_body(request)
    connected = asyncio.get_running_loop().create_future()
    task = c.spawn(c.transfer(body, connected), "transfer")  # logs a failure
    await asyncio.wait({task, connected}, return_when=asyncio.FIRST_COMPLETED)
    if not connected.done():  # over before the owner was put through
        if task.cancelled() or task.exception():
            why = "cancelled" if task.cancelled() else task.exception().__class__.__name__
            return web.json_response({"status": "not_connected", "reason": f"error: {why}"})
        return web.json_response(task.result())

    def finished(t: asyncio.Task) -> None:  # runs at once if the transfer is already over
        if not t.cancelled() and not t.exception():
            lg.info("transfer to %s finished: %s", connected.result()["ext"], t.result()["status"])
    task.add_done_callback(finished)
    return web.json_response({"status": "connecting", **connected.result()})


async def http_notify(c: Controller, request: web.Request) -> web.Response:
    body = await json_body(request)
    try:
        res = await c.notify_owner(body)
        c.spawn(c.show_on_screen(body, body.get("message"), res), "screen summary")
        return web.json_response(res)
    except Exception as e:
        lg.exception("notify_owner failed: %r", e)
        return web.json_response({"status": "not_delivered", "reason": f"error: {e.__class__.__name__}"})


async def http_call(c: Controller, request: web.Request) -> web.Response:
    body = await json_body(request)
    try:
        res = await c.call_owner(body)
        c.spawn(c.show_on_screen(body, body.get("question"), res), "screen summary")
        return web.json_response(res)
    except Exception as e:
        # Never lose the question: whatever broke, it becomes a voicemail.
        lg.exception("call_owner failed: %r", e)
        try:
            return web.json_response(
                await c.call_owner({**body, "_force_voicemail": f"error: {e.__class__.__name__}"}))
        except Exception:
            return web.json_response(
                {"status": "error", "message": "The phone system failed. Carry on with your best assumption."})


async def http_whisper(c: Controller, request: web.Request) -> web.Response:
    body = await json_body(request)
    if not isinstance(body.get("text"), str) or not body["text"].strip():
        raise bad_request("text is required")
    return web.json_response({"status": await c.whisper(body["text"], body.get("label") or "Psst")})


async def http_meeting_test(c: Controller, request: web.Request) -> web.Response:
    """`phone meeting-test`: the alert for a made-up meeting. Calls are not held."""
    now = datetime.now().astimezone()
    start = now + timedelta(minutes=c.s.meeting_warn_minutes)
    await c.alert(diary.Meeting("test", "Test meeting", start, start + timedelta(minutes=30),
                                ("Sarah Jones", "Dan Smith")), now, dnd=False)
    return web.json_response({"ok": True})


async def http_health(c: Controller, request: web.Request) -> web.Response:
    return web.json_response(await c.health())


async def http_shutdown(c: Controller, request: web.Request) -> web.Response:
    """`phone down`: answered once the calls are hung up; the daemon exits right after."""
    await c.shutdown()
    return web.json_response({"status": "stopping"})


async def http_session_list(c: Controller, request: web.Request) -> web.Response:
    """Live sessions and the system's state, for the VS Code status bar."""
    sessions = [{"ext": x.ext, "label": x.label, "repo": x.repo, "branch": x.branch, "worktree": x.worktree,
                 "status": "working" if await c.still_working(x) else "idle", "term_id": x.term_id}
                for x in registry.all_sessions()]
    return web.json_response({"sessions": sessions, "health": await c.health()})


LOCAL_HOSTS = {"127.0.0.1", "localhost"}


@web.middleware
async def local_callers_only(request: web.Request, handler):
    """The localhost API rings the phone and lists sessions, so refuse web pages: a browser sends Origin on
    cross-site requests, and a DNS-rebound name shows up in Host."""
    if request.headers.get("Origin") is not None:
        raise web.HTTPForbidden(text="browser requests are not allowed")
    if (request.host or "").rsplit(":", 1)[0] not in LOCAL_HOSTS:
        raise web.HTTPForbidden(text="unexpected host")
    return await handler(request)


Handler = Callable[["Controller", web.Request], Awaitable[web.StreamResponse]]


def _for(c: Controller, handler: Handler) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    """handler(c, request) as the aiohttp handler for one Controller."""
    async def handle(request: web.Request) -> web.StreamResponse:
        return await handler(c, request)
    return handle


def local_app(c: Controller) -> web.Application:
    """The API on 127.0.0.1: the MCP server's calls, `phone status` and the VS Code extension."""
    app = web.Application(middlewares=[local_callers_only])
    app.router.add_post("/call", _for(c, http_call))
    app.router.add_post("/notify", _for(c, http_notify))
    app.router.add_post("/transfer", _for(c, http_transfer))
    app.router.add_post("/whisper", _for(c, http_whisper))
    app.router.add_post("/meeting-test", _for(c, http_meeting_test))
    app.router.add_get("/health", _for(c, http_health))
    app.router.add_get("/sessions", _for(c, http_session_list))
    app.router.add_post("/shutdown", _for(c, http_shutdown))
    return app


# --- the listener on the LAN ---

def _from_lan(c: Controller, request: web.Request) -> None:
    """Refuses (403) a request from outside lan_cidr, the only callers the LAN listener serves."""
    try:
        ok = c.lan_net is not None and ip_address(request.remote or "") in c.lan_net
    except ValueError:  # not an IP address
        ok = False
    if not ok:
        raise web.HTTPForbidden()


async def http_sessions(c: Controller, request: web.Request) -> web.Response:
    """Live sessions as a Yealink directory. Served at /phonebook.xml (Directory → Claude; the phone
    caches it for an hour or more) and /sessions.xml (an XML Browser soft key fetches it on every press)."""
    _from_lan(c, request)
    lg.info("phonebook %s fetched by %s", request.path, request.remote)
    render = browser_xml if request.path == "/sessions.xml" else phonebook_xml
    return web.Response(text=render(registry.all_sessions(), c.s), content_type="text/xml")


async def http_dnd(c: Controller, request: web.Request) -> web.Response:
    """The phone's Action URLs: Open DND -> /dnd/on, Close DND -> /dnd/off."""
    _from_lan(c, request)
    c.set_phone_dnd(request.match_info["state"] == "on")
    return web.Response(text="ok")


def phonebook_app(c: Controller) -> web.Application:
    """The listener on lan_ip: the T31P's Directory and XML Browser, and its DND key's Action URLs."""
    book = web.Application()
    book.router.add_get("/phonebook.xml", _for(c, http_sessions))
    book.router.add_get("/sessions.xml", _for(c, http_sessions))
    book.router.add_get("/dnd/{state:on|off}", _for(c, http_dnd))
    return book


async def serve_phonebook(runner: web.AppRunner, s: config.Settings) -> None:
    """The phonebook binds to the home LAN address, which is missing while the PC is on another network.
    Keep retrying rather than taking the daemon (and missed-call pushes) down with it."""
    while True:
        try:
            await web.TCPSite(runner, s.lan_ip, s.phonebook_port).start()
            lg.info("phonebook on %s:%s", s.lan_ip, s.phonebook_port)
            return
        except OSError:
            await asyncio.sleep(60)
