"""Meetings for the meeting alerts, from the calendar feeds (ICS) that Google Calendar and Outlook publish.

Each feed's link is a secret (anyone with it can read the calendar), kept in the keychain as calendar-<source> by
`phone calendar add`; `calendar_sources` picks which feeds are read. A feed is fetched gzipped at most every
calendar_refresh_minutes (asking "if modified", though Google ignores that) and parsed only when its content
changed: its meetings for the next few days are kept and the parsed calendar is dropped. Times keep each event's own zone
and are shown in the PC's. The calendar libraries are an optional extra, imported only when a feed is read."""

from __future__ import annotations

import hashlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import config, keychain, log

lg = log.get("diary")

SOURCES = ("google", "outlook")
LIBRARIES = '"icalendar>=7" "recurring-ical-events>=3.8" tzdata'  # the `calendar` extra in pyproject.toml
MAX_BYTES = 20 * 1024 * 1024
TIMEOUT_SECONDS = 15  # per network wait
DOWNLOAD_SECONDS = 60  # for the whole download, so a server sending a trickle can't stall the alerts
CHUNK_BYTES = 64 * 1024
MAX_MEETING = timedelta(hours=24)  # longer timed events are trips or out-of-office blocks, not meetings
KEEP_BEFORE, KEEP_AFTER = timedelta(days=1), timedelta(days=3)  # the meetings kept from each fetch
REFETCH_IN_FULL = timedelta(days=2)  # once fewer kept days than this remain, fetch without "if modified"


@dataclass(frozen=True)
class Meeting:
    key: str  # event id + start: a recurring meeting's occurrences share an id
    title: str
    start: datetime
    end: datetime
    people: tuple[str, ...]  # other attendees, by name


@dataclass
class _Feed:
    url: str
    name: str = ""  # the calendar's own name (X-WR-CALNAME)
    meetings: list[Meeting] = field(default_factory=list)
    until: datetime | None = None  # meetings are kept up to here
    etag: str | None = None
    modified: str | None = None
    digest: str = ""  # of the last body parsed: Google sends no ETag, so an unchanged body is spotted by its hash
    checked: datetime | None = None  # last fetch attempt
    fetched: datetime | None = None  # last fetch that worked
    error: str | None = None


_feeds: dict[str, _Feed] = {}
_lock = threading.Lock()  # the daemon reads feeds from worker threads


def _now() -> datetime:
    return datetime.now().astimezone()


def normalise_url(url: str) -> str:
    """The link as fetched: webcal:// is https:// under another name; anything else but https is refused."""
    url = url.strip()
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("a calendar link has no spaces in it: copy it again")
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError("a calendar link starts with https:// or webcal://")
    return url


def link(source: str) -> str | None:
    return keychain.get(f"calendar-{source}")


def _linked(source: str) -> bool:
    try:
        return bool(link(source))
    except Exception:  # Credential Manager failing counts as no link; _feed reports why
        return False


def _selected(s: config.Settings) -> list[str]:
    return [x for x in (str(v).strip().lower() for v in s.calendar_sources) if x in SOURCES]


def reason(e: Exception, url: str = "") -> str:
    """A short, link-free reason a feed couldn't be read."""
    if isinstance(e, urllib.error.HTTPError):
        text = f"HTTP {e.code} {e.reason}"
    elif isinstance(e, urllib.error.URLError):
        text = str(e.reason)
    else:
        text = str(e) or type(e).__name__
    if url:  # the secret token is in the path or query; some errors quote just those
        parts = urllib.parse.urlsplit(url)
        for secret in (url, parts.path, parts.query):
            if len(secret) > 1:
                text = text.replace(secret, "<link>")
    return text[:200]


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another https address, before connecting to it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise ValueError("the calendar link redirected away from https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_open = urllib.request.build_opener(_HttpsOnly).open


def _download(r) -> bytes:
    deadline, chunks, size = time.monotonic() + DOWNLOAD_SECONDS, [], 0
    while chunk := r.read(CHUNK_BYTES):
        size += len(chunk)
        if size > MAX_BYTES:
            raise ValueError(f"the calendar is larger than {MAX_BYTES // 2**20} MB")
        if time.monotonic() > deadline:
            raise TimeoutError(f"the calendar took over {DOWNLOAD_SECONDS} s to download")
        chunks.append(chunk)
    return b"".join(chunks)


def _gunzip(body: bytes) -> bytes:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(body, MAX_BYTES + 1)  # capped, so a small "zip bomb" can't fill memory
    if len(out) > MAX_BYTES:
        raise ValueError(f"the calendar is larger than {MAX_BYTES // 2**20} MB")
    if not d.eof or d.unused_data:
        raise ValueError("the calendar arrived damaged (bad gzip)")
    return out


def _fetch(url: str, etag: str | None, modified: str | None) -> tuple[bytes | None, str | None, str | None]:
    """(body, etag, last-modified); body is None when the server says the feed hasn't changed."""
    headers = {"User-Agent": "claude-phone", "Accept-Encoding": "gzip"}  # ICS packs ~10x
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    try:
        with _open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_SECONDS) as r:
            if urllib.parse.urlsplit(r.geturl()).scheme.lower() != "https":
                raise ValueError("the calendar link redirected away from https")
            body = _download(r)
            if str(r.headers.get("Content-Encoding", "")).lower() == "gzip":
                body = _gunzip(body)
            return body, r.headers.get("ETag"), r.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag, modified
        raise


def parse(body: bytes, start: datetime, end: datetime, me: str = "") -> tuple[str, list[Meeting]]:
    """(the calendar's name, its meetings overlapping start..end by start time), without all-day, cancelled or
    declined events. `me` is the owner's address; empty: the calendar's name when that's an address, as Google
    names a primary calendar."""
    try:
        import icalendar
        import recurring_ical_events
    except ImportError:
        raise RuntimeError(f"the calendar libraries aren't installed: run .venv\\Scripts\\python -m pip install "
                           f"{LIBRARIES}") from None
    cal = icalendar.Calendar.from_ical(body)
    name = str(cal.get("X-WR-CALNAME", "")).strip()
    me = (me or (name if "@" in name else "")).strip().lower()
    unknown = _unknown_zones(cal)
    if unknown:
        lg.warning("calendar %s: %d event(s) skipped, their time zone is unknown", name or "?", len(unknown))
    found = (_meeting(ev, me) for ev in recurring_ical_events.of(cal).between(start, end)
             if str(ev.get("UID", "")) not in unknown)
    return name, sorted((m for m in found if m), key=lambda m: m.start)


def _unknown_zones(cal) -> set[str]:
    """UIDs of events whose TZID is neither defined in the feed nor a known zone: they'd come out as the PC's time."""
    out = set()
    for ev in cal.walk("VEVENT"):
        d = ev.get("DTSTART")
        if d is not None and d.params.get("TZID") and isinstance(d.dt, datetime) and d.dt.tzinfo is None:
            out.add(str(ev.get("UID", "")))
    return out


def _as_list(v) -> list:
    return [] if v is None else v if isinstance(v, list) else [v]


def _meeting(ev, me: str) -> Meeting | None:
    start = ev.get("DTSTART")
    if start is None or not isinstance(start.dt, datetime):
        return None  # all-day
    if str(ev.get("STATUS", "")).upper() == "CANCELLED" or str(ev.get("TRANSP", "")).upper() == "TRANSPARENT":
        return None  # cancelled, or marked "free"
    people = []
    for a in _as_list(ev.get("ATTENDEE")):
        address = str(a).strip().lower().removeprefix("mailto:")
        if me and address == me:
            if str(a.params.get("PARTSTAT", "")).upper() == "DECLINED":
                return None
            continue
        if str(a.params.get("CUTYPE", "")).upper() in ("ROOM", "RESOURCE"):
            continue
        people.append(str(a.params.get("CN") or address))
    if ev.get("DTEND") is not None:
        end = ev["DTEND"].dt
    elif ev.get("DURATION") is not None:
        end = start.dt + ev["DURATION"].dt
    else:
        end = start.dt
    s, e = start.dt.astimezone(), end.astimezone()  # a floating time (no zone) is taken as the PC's own
    if e - s > MAX_MEETING:
        return None
    return Meeting(f"{ev.get('UID', '')}@{s.isoformat()}", str(ev.get("SUMMARY") or "Meeting"), s, e, tuple(people))


def _feed(source: str, now: datetime, s: config.Settings) -> _Feed | None:
    """The source's feed, fetched again once calendar_refresh_minutes have passed. None without a stored link."""
    f = _feeds.get(source)
    if f and f.checked and now - f.checked < timedelta(minutes=max(1, s.calendar_refresh_minutes)):
        return f
    try:
        url = link(source)
    except Exception as e:  # Credential Manager itself failed: keep what we had, try again at the next refresh
        f = f or _feeds.setdefault(source, _Feed(""))
        f.checked = now
        _failed(source, f, f"can't read its link from Credential Manager ({type(e).__name__}: {e})")
        return f
    if not url:
        _feeds.pop(source, None)
        return None
    if not f or f.url != url:
        f = _feeds[source] = _Feed(url)
    full = not f.until or f.until - now < REFETCH_IN_FULL
    f.checked = now
    try:
        body, etag, modified = _fetch(url, None if full else f.etag, None if full else f.modified)
        digest = hashlib.sha256(body).hexdigest() if body is not None else f.digest
        if body is not None and (full or digest != f.digest):
            f.name, f.meetings = parse(body, now - KEEP_BEFORE, now + KEEP_AFTER, s.calendar_email)
            f.until, f.digest = now + KEEP_AFTER, digest
        f.etag, f.modified = etag, modified
        f.fetched = now
        if f.error:
            lg.info("calendar %s readable again", source)
        f.error = None
    except Exception as e:
        _failed(source, f, reason(e, url))
    return f


def _failed(source: str, f: _Feed, why: str) -> None:
    """Keep the feed's last meetings; warn once per run of failures."""
    if f.error is None:
        lg.warning("calendar %s can't be read (keeping what it had): %s", source, why)
    f.error = why


def _read() -> list[tuple[str, _Feed]]:
    s, now = config.load(), _now()
    with _lock:
        return [(x, f) for x in _selected(s) if (f := _feed(x, now, s))]


def access() -> bool:
    """True when a selected source has a stored link."""
    return any(_linked(x) for x in _selected(config.load()))


def calendars() -> list[str]:
    return [f"{x.capitalize()}: {f.name or 'calendar'}" for x, f in _read() if f.fetched]


def meetings(start: datetime, end: datetime) -> list[Meeting]:
    """Meetings overlapping start..end from the selected feeds, by start time. Only the days around now are kept.
    A meeting in both calendars has a different id in each, so it's matched on its time and title."""
    found = sorted((m for _, f in _read() for m in f.meetings if m.end > start and m.start < end),
                   key=lambda m: m.start)
    seen: set[tuple] = set()
    out = []
    for m in found:
        same = (m.start, m.end, m.title.strip().casefold())
        if same not in seen:
            seen.add(same)
            out.append(m)
    return out


def status() -> list[dict]:
    """Per source, for `phone calendar`: selected, link stored, the calendar's name, last good fetch, last error."""
    s, now = config.load(), _now()
    chosen = _selected(s)
    out = []
    with _lock:
        for x in SOURCES:
            f = _feed(x, now, s) if x in chosen else None
            out.append({"source": x, "selected": x in chosen, "linked": f is not None or _linked(x),
                        "name": f.name if f else "", "fetched": f.fetched if f else None,
                        "error": f.error if f else None, "meetings": len(f.meetings) if f else 0})
    return out


def check(url: str) -> tuple[str, int]:
    """Read a feed once, for `phone calendar add`: (the calendar's name, meetings in the next two days)."""
    body, _, _ = _fetch(url, None, None)
    now = _now()
    name, found = parse(body or b"", now, now + timedelta(days=2), config.load().calendar_email)
    return name, len(found)


def summary(m: Meeting) -> tuple[str, str]:
    """(title, text) for the phone's screen: "15:00 Board review" / "with Sarah, Dan +2" / "DND until 15:30"."""
    first = [p.split()[0] if "@" not in p else p.split("@")[0] for p in m.people]
    who = ""
    if first:
        who = "with " + ", ".join(first[:2]) + (f" +{len(first) - 2}" if len(first) > 2 else "") + "\n"
    return f"{m.start:%H:%M} {m.title}", f"{who}DND until {m.end:%H:%M}"


def window(now: datetime, warn: timedelta) -> list[Meeting]:
    """Meetings in progress or starting within `warn`."""
    return [m for m in meetings(now - timedelta(hours=12), now + warn) if m.end > now and m.start - warn <= now]
