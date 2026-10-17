"""The desk phone's screen and keys (Yealink Push XML and Action URIs), with aiohttp's client replaced by one that
records what would be sent: the phone only speaks HTTPS, and a local TLS server would need a certificate made."""

import asyncio
import base64
import hashlib
import types
import warnings

import aiohttp
import pytest

from claude_phone import config, keychain, registry, screen
from claude_phone.config import Settings


class Phone:
    """aiohttp as screen.py uses it, answering every request with status (or raising error)."""

    def __init__(self, monkeypatch, status=200, error=None):
        self.status, self.error, self.sent = status, error, []
        monkeypatch.setattr(screen, "aiohttp", types.SimpleNamespace(
            ClientSession=self.session, TCPConnector=lambda **kw: kw, ClientTimeout=aiohttp.ClientTimeout,
            Fingerprint=aiohttp.Fingerprint, ServerFingerprintMismatch=aiohttp.ServerFingerprintMismatch))

    def session(self, connector, **kw):
        self.connector = connector
        return self

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        pass

    def post(self, url, **kw):
        self.sent.append(("POST", url, kw))
        return self

    def get(self, url, **kw):
        self.sent.append(("GET", url, kw))
        return self


PINNED = hashlib.sha256(b"the desk phone's certificate").digest()


@pytest.fixture
def desk(monkeypatch):
    s = Settings(screen_push=True, lan_ip="192.168.1.10", desk_ip="192.168.1.50")
    monkeypatch.setattr(config, "load", lambda: s)
    monkeypatch.setattr(screen, "_login_failed", False)
    monkeypatch.setenv("CLAUDE_PHONE_DESK_ADMIN", "s3cret")  # not the real Credential Manager
    registry.set_state(screen.CERT_STATE, PINNED.hex())  # met before
    return s


def pinned_to(connector) -> bytes:
    return connector["ssl"].fingerprint


def test_the_phones_certificate_is_pinned_on_first_contact_and_checked_after(desk, monkeypatch):
    # Without it, anything posing as the phone's address on the LAN would receive the admin password.
    registry.set_state(screen.CERT_STATE, None)
    der = b"first certificate"
    fetched = []
    monkeypatch.setattr(screen.ssl, "get_server_certificate",
                        lambda addr, timeout: fetched.append(addr) or screen.ssl.DER_cert_to_PEM_cert(der))
    phone = Phone(monkeypatch)
    assert asyncio.run(screen.show("t", "x"))
    assert fetched == [("192.168.1.50", 443)] and pinned_to(phone.connector) == hashlib.sha256(der).digest()
    assert asyncio.run(screen.press("DNDOn")) and len(fetched) == 1  # the pin is kept
    assert registry.get_state(screen.CERT_STATE) == hashlib.sha256(der).hexdigest()


def test_a_phone_with_another_certificate_gets_no_password(desk, monkeypatch, caplog):
    Phone(monkeypatch, error=aiohttp.ServerFingerprintMismatch(PINNED, b"other", "192.168.1.50", 443))
    assert not asyncio.run(screen.press("DNDOn"))
    assert "certificate" in caplog.text and "phone desk-cert reset" in caplog.text


def test_desk_cert_reset_forgets_the_pin(desk, capsys):
    from claude_phone import cli
    cli.main(["desk-cert", "reset"])
    assert registry.get_state(screen.CERT_STATE) is None and "next" in capsys.readouterr().out


def test_a_summary_goes_to_the_desk_phone_from_the_lan_address_escaped(desk, monkeypatch):
    phone = Phone(monkeypatch)
    assert asyncio.run(screen.show("lemon-stand <main>", "Ship it & merge?", timeout=30))
    (method, url, kw), = phone.sent
    assert (method, url) == ("POST", "https://192.168.1.50/servlet?push=xml")
    assert phone.connector["local_addr"] == ("192.168.1.10", 0)  # the phone's Push XML Server
    assert pinned_to(phone.connector) == PINNED
    assert kw["data"] == ('<?xml version="1.0" encoding="UTF-8"?><YealinkIPPhoneTextScreen Beep="no" Timeout="30">'
                          "<Title>lemon-stand &lt;main&gt;</Title><Text>Ship it &amp; merge?</Text>"
                          "</YealinkIPPhoneTextScreen>")


@pytest.mark.parametrize("phone_says", [403, aiohttp.ClientConnectionError("unreachable")])
def test_a_refused_or_unreachable_screen_says_so(desk, monkeypatch, phone_says):
    if isinstance(phone_says, int):
        Phone(monkeypatch, status=phone_says)
    else:
        Phone(monkeypatch, error=phone_says)
    assert not asyncio.run(screen.show("t", "x"))


def test_no_screen_push_unless_switched_on_or_the_phone_is_known(desk, monkeypatch):
    phone = Phone(monkeypatch)
    desk.screen_push = False
    assert not asyncio.run(screen.show("t", "x"))
    desk.screen_push, desk.desk_ip = True, ""
    monkeypatch.setattr(screen, "desk_ip", lambda s: None)
    assert not asyncio.run(screen.show("t", "x")) and phone.sent == []


def test_the_desk_phone_is_found_where_it_registered_from(monkeypatch):
    from claude_phone import esl
    asked = []
    s = Settings(desk_ext="200", lan_ip="192.168.1.10")
    monkeypatch.setattr(esl, "api_local", lambda cmd: asked.append(cmd) or
                        "sofia/internal/sip:200@192.168.1.57:5060;transport=udp")
    assert screen.desk_ip(s) == "192.168.1.57" and asked == ["sofia_contact internal/200@192.168.1.10"]
    monkeypatch.setattr(esl, "api_local", lambda cmd: "error/user_not_registered")
    assert screen.desk_ip(s) is None

    def down(cmd):
        raise esl.ESLError("FreeSWITCH isn't running")
    monkeypatch.setattr(esl, "api_local", down)
    assert screen.desk_ip(s) is None
    assert screen.desk_ip(Settings(desk_ip="10.0.0.9")) == "10.0.0.9"  # set in config: asked of no one


def test_a_key_is_pressed_as_admin(desk, monkeypatch):
    phone = Phone(monkeypatch)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)  # aiohttp.BasicAuth goes in aiohttp 4
        assert asyncio.run(screen.press("DNDOn"))
    (method, url, kw), = phone.sent
    assert (method, url) == ("GET", "https://192.168.1.50/servlet?key=DNDOn")
    assert kw["headers"] == {"Authorization": "Basic " + base64.b64encode(b"admin:s3cret").decode()}


def test_a_refused_password_stops_key_presses_until_restart(desk, monkeypatch):
    # The phone locks its web login after a few failures, which would lock the owner out too.
    phone = Phone(monkeypatch, status=401)
    assert not asyncio.run(screen.press("DNDOn"))
    phone.status = 200
    assert not asyncio.run(screen.press("DNDOff")) and len(phone.sent) == 1


def test_no_key_press_without_the_admin_password(desk, monkeypatch):
    phone = Phone(monkeypatch)
    monkeypatch.delenv("CLAUDE_PHONE_DESK_ADMIN")
    monkeypatch.setattr(keychain, "get", lambda name: None)
    assert not asyncio.run(screen.press("DNDOn")) and phone.sent == []
