"""Text on the desk phone's screen via Yealink Push XML. The phone only accepts pushes from the address set
as its Push XML Server (Features → Remote Control), so requests leave from lan_ip, not the default route.

The phone's HTTPS certificate is self-signed, so it's trusted on first use: its SHA-256 is kept in the registry
and every later request must meet the same one. Otherwise anything posing as the phone's address on the LAN would
receive the admin password."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import ssl
from xml.sax.saxutils import escape

import aiohttp

from . import config, log, registry

lg = log.get("screen")
CERT_STATE = "desk-cert"  # registry state: the desk phone's certificate's SHA-256, hex
CERT_RESET = "If it's a new or reset phone, run `phone desk-cert reset`."


def _fingerprint(ip: str) -> bytes:
    """The SHA-256 the phone's certificate must have: the one pinned, else (first contact) the one it shows now,
    fetched without sending anything."""
    pinned = registry.get_state(CERT_STATE)
    if pinned:
        return bytes.fromhex(pinned)
    der = ssl.PEM_cert_to_DER_cert(ssl.get_server_certificate((ip, 443), timeout=8))
    digest = hashlib.sha256(der).digest()
    registry.set_state(CERT_STATE, digest.hex())
    lg.info("desk phone at %s met: its certificate (sha256 %s) is pinned", ip, digest.hex()[:16])
    return digest


def _connector(s: config.Settings, fingerprint: bytes) -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(ssl=aiohttp.Fingerprint(fingerprint), local_addr=(s.lan_ip, 0))


def _failed(what: str, e: Exception) -> None:
    if isinstance(e, aiohttp.ServerFingerprintMismatch):
        lg.warning("%s refused: the desk phone's certificate isn't the one pinned, so nothing was sent. %s",
                   what, CERT_RESET)
    else:
        lg.warning("%s failed: %r", what, e)


def text_screen(title: str, text: str, timeout: int) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<YealinkIPPhoneTextScreen Beep="no" Timeout="{timeout}">'
            f"<Title>{escape(title)}</Title><Text>{escape(text)}</Text></YealinkIPPhoneTextScreen>")


def desk_ip(s: config.Settings) -> str | None:
    """desk_ip from config, else the address the desk phone registered from."""
    if s.desk_ip:
        return s.desk_ip
    from . import esl
    try:
        out = esl.api_local(f"sofia_contact internal/{s.desk_ext}@{s.lan_ip}")
    except (OSError, ConnectionError, esl.ESLError):
        return None
    m = re.search(rf"sip:{s.desk_ext}@([\d.]+)", out)
    return m.group(1) if m else None


async def show(title: str, text: str, timeout: int = 120) -> bool:
    s = config.load()
    if not s.screen_push:
        return False
    ip = await asyncio.to_thread(desk_ip, s)  # may query FreeSWITCH: not on the daemon's event loop
    if not ip:
        lg.warning("screen push skipped: desk phone address unknown")
        return False
    try:
        conn = _connector(s, await asyncio.to_thread(_fingerprint, ip))
        async with aiohttp.ClientSession(connector=conn) as http:
            async with http.post(f"https://{ip}/servlet?push=xml", data=text_screen(title, text, timeout),
                                 headers={"Content-Type": "text/xml"}, timeout=aiohttp.ClientTimeout(total=8)) as r:
                ok = r.status == 200
        lg.info("screen %s: %r %r", "shown" if ok else f"refused ({r.status})", title, text)
        return ok
    except Exception as e:
        _failed("screen push", e)
        return False


_login_failed = False


async def press(key: str) -> bool:
    """Yealink Action URI, e.g. press("DNDOn"). Needs this PC in the phone's Action URI Allow IP List and the
    phone's admin password in the keychain (desk-admin). After one refused login it stops trying until the
    daemon restarts: the phone locks its web login after a few failures."""
    global _login_failed
    from . import keychain
    s = config.load()
    password, ip = await asyncio.to_thread(lambda: (keychain.get("desk-admin"), desk_ip(s)))
    if _login_failed or not password or not ip:
        return False
    # Basic auth as a header: aiohttp.BasicAuth is deprecated, and goes in aiohttp 4.
    auth = {"Authorization": "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()}
    try:
        conn = _connector(s, await asyncio.to_thread(_fingerprint, ip))
        async with aiohttp.ClientSession(connector=conn) as http:
            async with http.get(f"https://{ip}/servlet?key={key}", headers=auth,
                                timeout=aiohttp.ClientTimeout(total=8)) as r:
                status = r.status
        if status == 401:
            _login_failed = True
            lg.warning("phone refused the admin password (keychain desk-admin); remote keys off until restart")
        lg.info("key %s: %s", key, "sent" if status == 200 else f"refused ({status})")
        return status == 200
    except Exception as e:
        _failed(f"key {key}", e)
        return False
