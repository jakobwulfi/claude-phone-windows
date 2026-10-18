"""What the desk phone shows: its phonebook and XML Browser directory of live sessions, caller IDs that fit its
screen, and ringtones per repo. Plus the audio counters FreeSWITCH reports for each finished call leg."""

from __future__ import annotations

from . import config, registry


def ringtone(repo: str, overrides: dict) -> int:
    """T31P ringtone 1-5 for a repo: config override, else the one the registry assigned it."""
    if repo in overrides:
        return int(overrides[repo])
    return registry.ringtone(repo)


def phonebook_xml(sessions: list[registry.Session], s: config.Settings) -> str:
    """Yealink remote phonebook: live sessions, then the fixed lines."""
    from xml.sax.saxutils import escape
    entries = [(caller_id(x.label, s.callerid_max), str(x.ext)) for x in sessions]
    entries += [("Voicemail", s.voicemail_ext), ("Echo test", "600")]
    rows = "".join(f"<DirectoryEntry><Name>{escape(n)}</Name><Telephone>{escape(t)}</Telephone></DirectoryEntry>"
                   for n, t in entries)
    return f'<?xml version="1.0" encoding="UTF-8"?><YealinkIPPhoneDirectory>{rows}</YealinkIPPhoneDirectory>'


def browser_xml(sessions: list[registry.Session], s: config.Settings) -> str:
    """Yealink XML Browser directory object (a different schema from the remote phonebook).
    Each item dials its extension when selected."""
    from xml.sax.saxutils import escape
    entries = [(f"{x.ext} {caller_id(x.label, s.callerid_max)}", str(x.ext)) for x in sessions]
    entries += [(f"{s.voicemail_ext} Voicemail", s.voicemail_ext)]
    items = "".join(f"<MenuItem><Prompt>{escape(p)}</Prompt><URI>{escape(u)}</URI></MenuItem>" for p, u in entries)
    title = f"Claude ({len(sessions)} live)"
    return (f'<?xml version="1.0" encoding="UTF-8"?><YealinkIPPhoneDirectory Beep="no" Timeout="60" LockIn="no">'
            f"<Title>{escape(title)}</Title>{items}</YealinkIPPhoneDirectory>")


# Per-leg audio counters FreeSWITCH reports at hangup, as (label, channel variable). "skip" counts packets FreeSWITCH
# skipped, "jitter" the packets in its jitter statistics; neither is network loss as such.
CALL_STATS = (
    ("in_pkts", "rtp_audio_in_packet_count"), ("in_skip", "rtp_audio_in_skip_packet_count"),
    ("in_jitter", "rtp_audio_in_jitter_packet_count"), ("in_flaws", "rtp_audio_in_flaw_total"),
    ("mos", "rtp_audio_in_mos"), ("jitter_var", "rtp_audio_in_jitter_max_variance"),
    ("out_pkts", "rtp_audio_out_packet_count"), ("out_skip", "rtp_audio_out_skip_packet_count"),
)


def call_stats(ev: dict) -> str | None:
    """One log line on a finished leg's audio (codec, packets, loss, jitter), for diagnosing choppy voice.
    None for other events and for legs that never carried audio."""
    if ev.get("Event-Name") != "CHANNEL_HANGUP_COMPLETE":
        return None
    if not any(f"variable_rtp_audio_{d}_packet_count" in ev for d in ("in", "out")):
        return None

    def var(name: str) -> str:
        return ev.get(f"variable_{name}") or "?"
    counters = " ".join(f"{label}={var(name)}" for label, name in CALL_STATS)
    return (f"audio {ev.get('Caller-Destination-Number', '?')} {ev.get('Call-Direction', '?')} "
            f"{ev.get('Unique-ID', '?')[:8]}: codec={var('read_codec')}/{var('read_rate')} "
            f"write={var('write_codec')} {counters}")


def caller_id(label: str, limit: int) -> str:
    """Fit repo/branch onto the T31P screen: drop branch prefixes (feat/), then trim the branch, then the repo."""
    if len(label) <= limit:
        return label
    repo, _, branch = label.partition("/")
    branch = branch.rsplit("/", 1)[-1]
    label = f"{repo}/{branch}" if branch else repo
    if len(label) <= limit:
        return label
    if branch:
        room = limit - len(repo) - 1
        if room >= 4:
            return f"{repo}/{branch[:room - 1]}~"
        repo = repo[: max(4, limit // 2 - 1)]
        return f"{repo}/{branch}"[: limit - 1] + "~"
    return label[: limit - 1] + "~"
