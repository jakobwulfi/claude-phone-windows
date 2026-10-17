"""Meetings from ICS feeds. Expected times are built in each event's own zone and compared as instants, so the
tests pass whatever zone the PC running them is in."""

import gzip
import io
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from claude_phone import config, diary

NY, KOLKATA, SYDNEY, UTC = (ZoneInfo(z) for z in ("America/New_York", "Asia/Kolkata", "Australia/Sydney", "UTC"))
START, END = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 30, tzinfo=UTC)


def ics(*events: str, name: str = "sam@example.com", extra: str = "") -> bytes:
    body = "\r\n".join(e.strip().replace("\n", "\r\n") for e in events)
    return (f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nX-WR-CALNAME:{name}\r\n{extra}"
            f"{body}\r\nEND:VCALENDAR\r\n").encode()


def event(uid: str, start: str, end: str = "", *lines: str, title: str = "Sync") -> str:
    out = ["BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20261001T000000Z", f"SUMMARY:{title}", start]
    if end:
        out.append(end)
    return "\n".join([*out, *lines, "END:VEVENT"])


def parse(body: bytes, me: str = "") -> list[diary.Meeting]:
    return diary.parse(body, START, END, me)[1]


# --- times, anywhere in the world ---

def test_times_come_from_each_events_own_zone():
    ms = parse(ics(
        event("ny", "DTSTART;TZID=America/New_York:20261020T090000", "DTEND;TZID=America/New_York:20261020T093000"),
        event("in", "DTSTART;TZID=Asia/Kolkata:20261021T093000", "DTEND;TZID=Asia/Kolkata:20261021T100000"),
        event("utc", "DTSTART:20261022T140000Z", "DTEND:20261022T150000Z"),
    ))
    assert [m.start for m in ms] == [datetime(2026, 10, 20, 9, tzinfo=NY),
                                     datetime(2026, 10, 21, 9, 30, tzinfo=KOLKATA),
                                     datetime(2026, 10, 22, 14, tzinfo=UTC)]
    assert ms[1].start == datetime(2026, 10, 21, 4, tzinfo=UTC)  # +5:30
    assert all(m.start.tzinfo is not None for m in ms)


def test_a_meeting_across_a_clock_change_keeps_its_real_length():
    # Sydney's clocks go from 02:00 to 03:00 on 4 October 2026: 01:30-03:30 on the wall is one real hour.
    ms = parse(ics(event("syd", "DTSTART;TZID=Australia/Sydney:20261004T013000",
                         "DTEND;TZID=Australia/Sydney:20261004T033000")))
    assert ms[0].end - ms[0].start == timedelta(hours=1)


def test_floating_times_are_the_pcs_own_time():
    ms = parse(ics(event("f", "DTSTART:20261020T090000", "DTEND:20261020T100000")))
    assert ms[0].start == datetime(2026, 10, 20, 9).astimezone()


def test_outlook_windows_zone_names():
    vtimezone = """BEGIN:VTIMEZONE
TZID:Pacific Standard Time
BEGIN:STANDARD
DTSTART:16010101T020000
TZOFFSETFROM:-0700
TZOFFSETTO:-0800
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=1SU;BYMONTH=11
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:-0800
TZOFFSETTO:-0700
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=2SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE
""".replace("\n", "\r\n")
    ms = parse(ics(event("o", "DTSTART;TZID=Pacific Standard Time:20261020T090000",
                         "DTEND;TZID=Pacific Standard Time:20261020T100000"), name="Calendar", extra=vtimezone))
    assert ms[0].start == datetime(2026, 10, 20, 16, tzinfo=UTC)


def test_a_duration_instead_of_an_end():
    ms = parse(ics(event("d", "DTSTART:20261020T090000Z", "DURATION:PT45M")))
    assert ms[0].end - ms[0].start == timedelta(minutes=45)


# --- recurring meetings ---

def test_recurring_meeting_with_a_skipped_and_a_moved_occurrence():
    ms = parse(ics(
        event("weekly", "DTSTART;TZID=America/New_York:20261019T090000", "DTEND;TZID=America/New_York:20261019T093000",
              "RRULE:FREQ=WEEKLY;COUNT=4", "EXDATE;TZID=America/New_York:20261026T090000"),
        event("weekly", "DTSTART;TZID=America/New_York:20261103T100000", "DTEND;TZID=America/New_York:20261103T103000",
              "RECURRENCE-ID;TZID=America/New_York:20261102T090000"),
    ))
    # New York leaves daylight time on 1 November: 09:00 stays 09:00 on the wall, an hour later in UTC.
    assert [m.start for m in ms] == [datetime(2026, 10, 19, 9, tzinfo=NY), datetime(2026, 11, 3, 10, tzinfo=NY),
                                     datetime(2026, 11, 9, 9, tzinfo=NY)]
    assert ms[0].start.astimezone(UTC).hour == 13 and ms[2].start.astimezone(UTC).hour == 14
    assert len({m.key for m in ms}) == 3 and all(m.key.startswith("weekly@") for m in ms)


# --- what counts as a meeting ---

def test_all_day_cancelled_and_declined_events_are_skipped():
    me = "ATTENDEE;CN=Sam;PARTSTAT=DECLINED:mailto:sam@example.com"
    ms = parse(ics(
        event("allday", "DTSTART;VALUE=DATE:20261020", "DTEND;VALUE=DATE:20261021"),
        event("cancelled", "DTSTART:20261020T090000Z", "DTEND:20261020T100000Z", "STATUS:CANCELLED"),
        event("declined", "DTSTART:20261020T110000Z", "DTEND:20261020T120000Z", me),
        event("kept", "DTSTART:20261020T130000Z", "DTEND:20261020T140000Z",
              "ATTENDEE;CN=Sam;PARTSTAT=ACCEPTED:mailto:sam@example.com"),
    ))
    assert [m.key.split("@")[0] for m in ms] == ["kept"]


def test_people_are_the_others_by_name_without_rooms():
    ms = parse(ics(event(
        "p", "DTSTART:20261020T090000Z", "DTEND:20261020T100000Z",
        "ATTENDEE;CN=Sam Lee;PARTSTAT=ACCEPTED:mailto:SAM@example.com",
        "ATTENDEE;CN=Priya Nair;PARTSTAT=ACCEPTED:mailto:priya@example.com",
        "ATTENDEE:mailto:kenji@example.jp",
        "ATTENDEE;CUTYPE=ROOM;CN=Room 4.12:mailto:room412@example.com",
        title="Revisión trimestral",
    )))
    assert ms[0].people == ("Priya Nair", "kenji@example.jp")
    assert ms[0].title == "Revisión trimestral"


def test_free_and_multi_day_blocks_are_not_meetings():
    ms = parse(ics(
        event("free", "DTSTART:20261020T090000Z", "DTEND:20261020T100000Z", "TRANSP:TRANSPARENT"),
        event("away", "DTSTART:20261020T090000Z", "DTEND:20261023T090000Z", title="Out of office"),
        event("kept", "DTSTART:20261020T110000Z", "DTEND:20261020T120000Z", "TRANSP:OPAQUE"),
    ))
    assert [m.key.split("@")[0] for m in ms] == ["kept"]


def test_an_unknown_time_zone_is_skipped_not_guessed():
    # No VTIMEZONE for it and not a known zone: reading it as the PC's time would alert at the wrong hour.
    ms = parse(ics(event("odd", "DTSTART;TZID=Foo/Bar:20261020T230000", "DTEND;TZID=Foo/Bar:20261020T233000",
                         "RRULE:FREQ=DAILY;COUNT=3"),
                   event("ok", "DTSTART:20261020T090000Z", "DTEND:20261020T100000Z")))
    assert [m.key.split("@")[0] for m in ms] == ["ok"]


def test_the_summary_names_a_few_people_by_first_name():
    start = datetime(2026, 10, 20, 9, tzinfo=UTC)
    m = diary.Meeting("k", "Sync", start, start + timedelta(minutes=30), ("Ann Lee", "bob@x.com", "Cy Ng", "Di Fox"))
    assert diary.summary(m)[1].startswith("with Ann, bob +2\n")


def test_calendar_email_wins_over_the_feeds_name():
    body = ics(event("x", "DTSTART:20261020T090000Z", "DTEND:20261020T100000Z",
                     "ATTENDEE;PARTSTAT=DECLINED:mailto:work@corp.example"), name="Calendar")
    assert parse(body) != [] and parse(body, me="work@corp.example") == []


# --- links ---

def test_links_must_be_https():
    assert diary.normalise_url(" webcal://calendar.example/a.ics ") == "https://calendar.example/a.ics"
    assert diary.normalise_url("https://calendar.example/a.ics") == "https://calendar.example/a.ics"
    for bad in ("http://calendar.example/a.ics", "file:///C:/a.ics", "calendar.example/a.ics", "https://",
                "https://calendar.example/secret abc/a.ics", "https://calendar.example/a\x07.ics"):
        with pytest.raises(ValueError):
            diary.normalise_url(bad)


def test_reasons_never_carry_the_secret_part_of_a_link():
    url = "https://calendar.example/ical/sam/private-5ecret/basic.ics?token=t0ken"
    for e in (ValueError(f"bad URL {url}"), ValueError("can't contain '/ical/sam/private-5ecret/basic.ics'"),
              OSError("query token=t0ken refused")):
        why = diary.reason(e, url)
        assert "5ecret" not in why and "t0ken" not in why


# --- refreshing feeds ---

NOW = datetime(2026, 10, 20, 8, tzinfo=UTC)


@pytest.fixture
def feeds(monkeypatch):
    """Two stored links, both sources selected, and a fake network: `served[source]` is what the next fetch
    returns (bytes, None for "not modified", or an exception to raise)."""
    s = config.Settings(calendar_sources=["google", "outlook"], calendar_refresh_minutes=5)
    monkeypatch.setattr(diary.config, "load", lambda: s)
    links = {"calendar-google": "https://g.example/basic.ics", "calendar-outlook": "https://o.example/cal.ics"}
    monkeypatch.setattr(diary.keychain, "get", links.get)
    monkeypatch.setattr(diary, "_feeds", {})
    clock = {"now": NOW}
    monkeypatch.setattr(diary, "_now", lambda: clock["now"])
    served, asked = {}, []

    def fetch(url, etag, modified):
        source = "google" if "g.example" in url else "outlook"
        asked.append((source, etag))
        r = served[source]
        if isinstance(r, Exception):
            raise r
        return (None, etag, modified) if r is None else (r, f"etag-{len(asked)}", None)
    monkeypatch.setattr(diary, "_fetch", fetch)
    return s, links, clock, served, asked


def test_two_sources_are_merged_by_start_time(feeds):
    s, links, clock, served, asked = feeds
    served["google"] = ics(event("g", "DTSTART:20261020T110000Z", "DTEND:20261020T113000Z"))
    served["outlook"] = ics(event("o", "DTSTART:20261020T090000Z", "DTEND:20261020T100000Z"), name="Calendar")
    ms = diary.meetings(NOW, NOW + timedelta(days=1))
    assert [m.key.split("@")[0] for m in ms] == ["o", "g"]
    assert diary.calendars() == ["Google: sam@example.com", "Outlook: Calendar"]
    assert diary.access()


def test_only_selected_sources_with_a_link_are_read(feeds):
    s, links, clock, served, asked = feeds
    s.calendar_sources = ["google"]
    served["google"] = ics()
    diary.meetings(NOW, NOW + timedelta(days=1))
    assert [a[0] for a in asked] == ["google"]
    del links["calendar-google"]
    assert not diary.access()


def test_refetches_only_after_the_refresh_interval_and_asks_if_modified(feeds):
    s, links, clock, served, asked = feeds
    s.calendar_sources = ["google"]
    served["google"] = ics(event("g", "DTSTART:20261020T110000Z", "DTEND:20261020T113000Z"))
    diary.meetings(NOW, NOW + timedelta(days=1))
    diary.meetings(NOW, NOW + timedelta(days=1))
    assert asked == [("google", None)]
    clock["now"] = NOW + timedelta(minutes=6)
    served["google"] = None  # not modified
    assert len(diary.meetings(NOW, NOW + timedelta(days=1))) == 1
    assert asked[-1] == ("google", "etag-1")


def test_a_meeting_in_both_calendars_alerts_once(feeds):
    s, links, clock, served, asked = feeds
    served["google"] = ics(event("g-uid", "DTSTART:20261020T090000Z", "DTEND:20261020T100000Z", title="Sync"))
    served["outlook"] = ics(event("o-uid", "DTSTART:20261020T090000Z", "DTEND:20261020T100000Z", title="Sync"),
                            name="Calendar")
    assert len(diary.meetings(NOW, NOW + timedelta(days=1))) == 1


@pytest.mark.parametrize("failure", ["keychain", "network"])
def test_a_failed_refresh_keeps_the_last_meetings_and_reports_why_without_the_link(feeds, monkeypatch, failure):
    s, links, clock, served, asked = feeds
    s.calendar_sources = ["google"]
    served["google"] = ics(event("g", "DTSTART:20261020T110000Z", "DTEND:20261020T113000Z"))
    diary.meetings(NOW, NOW + timedelta(days=1))
    if failure == "keychain":
        def broken(name):
            raise RuntimeError("Credential Manager unavailable")
        monkeypatch.setattr(diary.keychain, "get", broken)
    else:
        served["google"] = urllib.error.URLError("no network")
    clock["now"] = NOW + timedelta(minutes=6)
    assert len(diary.meetings(NOW, NOW + timedelta(days=1))) == 1
    error = {x["source"]: x for x in diary.status()}["google"]["error"]
    assert {"keychain": "Credential Manager", "network": "no network"}[failure] in error
    assert "g.example" not in error


def test_an_unchanged_feed_is_not_parsed_again(feeds, monkeypatch):
    # Google sends no ETag or Last-Modified, so the body comes every time; only a change is worth parsing.
    s, links, clock, served, asked = feeds
    s.calendar_sources = ["google"]
    served["google"] = ics(event("g", "DTSTART:20261020T110000Z", "DTEND:20261020T113000Z"))
    parsed = []
    real = diary.parse
    monkeypatch.setattr(diary, "parse", lambda *a: parsed.append(1) or real(*a))
    diary.meetings(NOW, NOW + timedelta(days=1))
    clock["now"] = NOW + timedelta(minutes=6)
    assert len(diary.meetings(NOW, NOW + timedelta(days=1))) == 1
    assert len(parsed) == 1
    served["google"] = ics()
    clock["now"] = NOW + timedelta(minutes=12)
    assert diary.meetings(NOW, NOW + timedelta(days=1)) == []
    assert len(parsed) == 2


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, headers: dict, url: str = "https://g.example/basic.ics"):
        super().__init__(body)
        self.headers, self.url = headers, url

    def geturl(self):
        return self.url


def serve(monkeypatch, body: bytes, headers: dict | None = None, url: str = "https://g.example/basic.ics") -> list:
    sent = []

    def open_(req, timeout):
        sent.append(req.get_header("Accept-encoding"))
        return FakeResponse(body, headers or {}, url)
    monkeypatch.setattr(diary, "_open", open_)
    return sent


def test_fetch_asks_for_gzip_and_caps_what_it_unpacks(monkeypatch):
    sent = serve(monkeypatch, gzip.compress(b"BEGIN:VCALENDAR"), {"Content-Encoding": "gzip"})
    assert diary._fetch("https://g.example/basic.ics", None, None)[0] == b"BEGIN:VCALENDAR"
    assert sent == ["gzip"]
    monkeypatch.setattr(diary, "MAX_BYTES", 10)
    with pytest.raises(ValueError, match="larger than"):
        diary._fetch("https://g.example/basic.ics", None, None)


def test_fetch_refuses_damaged_gzip(monkeypatch):
    for body in (gzip.compress(b"BEGIN:VCALENDAR")[:-6], gzip.compress(b"a") + gzip.compress(b"b")):
        serve(monkeypatch, body, {"Content-Encoding": "gzip"})
        with pytest.raises(ValueError, match="damaged"):
            diary._fetch("https://g.example/basic.ics", None, None)


def test_fetch_gives_up_on_a_download_that_never_ends(monkeypatch):
    serve(monkeypatch, b"x" * 200_000)
    monkeypatch.setattr(diary, "DOWNLOAD_SECONDS", -1)
    with pytest.raises(TimeoutError):
        diary._fetch("https://g.example/basic.ics", None, None)


def test_redirects_must_stay_on_https(monkeypatch):
    req = urllib.request.Request("https://g.example/basic.ics")
    with pytest.raises(ValueError, match="https"):
        diary._HttpsOnly().redirect_request(req, None, 302, "Found", {}, "http://g.example/basic.ics")
    serve(monkeypatch, b"x", url="http://g.example/basic.ics")
    with pytest.raises(ValueError, match="https"):
        diary._fetch("https://g.example/basic.ics", None, None)


def test_a_changed_link_starts_over(feeds):
    s, links, clock, served, asked = feeds
    s.calendar_sources = ["google"]
    served["google"] = ics(event("g", "DTSTART:20261020T110000Z", "DTEND:20261020T113000Z"))
    diary.meetings(NOW, NOW + timedelta(days=1))
    links["calendar-google"] = "https://g.example/other.ics"
    clock["now"] = NOW + timedelta(minutes=6)
    served["google"] = ics()
    assert diary.meetings(NOW, NOW + timedelta(days=1)) == []
    assert asked[-1] == ("google", None)


def test_window_is_meetings_under_way_or_starting_soon(feeds):
    s, links, clock, served, asked = feeds
    s.calendar_sources = ["google"]
    served["google"] = ics(
        event("now", "DTSTART:20261020T073000Z", "DTEND:20261020T083000Z"),
        event("soon", "DTSTART:20261020T080100Z", "DTEND:20261020T083000Z"),
        event("later", "DTSTART:20261020T090000Z", "DTEND:20261020T093000Z"),
        event("over", "DTSTART:20261020T070000Z", "DTEND:20261020T073000Z"),
    )
    assert [m.key.split("@")[0] for m in diary.window(NOW, timedelta(minutes=2))] == ["now", "soon"]
