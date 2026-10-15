"""Meetings for the meeting alerts. There's no calendar source on Windows yet (an ICS feed from Google or
Outlook is planned), so no meetings are found and the alerts stay quiet even when meeting_alerts is on."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Meeting:
    key: str  # event id + start: a recurring meeting's occurrences share an id
    title: str
    start: datetime
    end: datetime
    people: tuple[str, ...]  # other attendees, by name


def access() -> bool:
    """True once a calendar source is available. None is yet."""
    return False


def calendars() -> list[str]:
    return []


def meetings(start: datetime, end: datetime, only: list[str] | None = None) -> list[Meeting]:
    """Timed events overlapping start..end, minus all-day, cancelled and declined ones, by start time."""
    return []


def summary(m: Meeting) -> tuple[str, str]:
    """(title, text) for the phone's screen: "15:00 Board review" / "with Sarah, Dan +2" / "DND until 15:30"."""
    first = [p.split()[0] if "@" not in p else p.split("@")[0] for p in m.people]
    who = ""
    if first:
        who = "with " + ", ".join(first[:2]) + (f" +{len(first) - 2}" if len(first) > 2 else "") + "\n"
    return f"{m.start:%H:%M} {m.title}", f"{who}DND until {m.end:%H:%M}"


def window(now: datetime, warn: timedelta, only: list[str] | None = None) -> list[Meeting]:
    """Meetings in progress or starting within `warn`."""
    return [m for m in meetings(now - timedelta(hours=12), now + warn, only) if m.end > now and m.start - warn <= now]
