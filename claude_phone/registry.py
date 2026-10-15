"""SQLite registry of live Claude Code sessions, pending voicemail questions and injections."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  ext        INTEGER UNIQUE NOT NULL,
  repo       TEXT NOT NULL,
  branch     TEXT,
  worktree   TEXT,
  cwd        TEXT,
  term_id   TEXT,
  status     TEXT NOT NULL DEFAULT 'idle',
  started_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS voicemails (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL,
  ext         INTEGER,
  label       TEXT NOT NULL,
  question    TEXT NOT NULL,
  context     TEXT,
  audio       TEXT,
  created_at  REAL NOT NULL,
  state       TEXT NOT NULL DEFAULT 'pending',  -- pending | answered | deleted
  answer      TEXT,
  answered_at REAL,
  delivery    TEXT                              -- sent | session-ended | failed:<why>
);
CREATE TABLE IF NOT EXISTS ringtones (
  repo TEXT PRIMARY KEY,
  ring INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS injections (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         REAL NOT NULL,
  session_id TEXT,
  ext        INTEGER,
  term_id   TEXT,
  source     TEXT NOT NULL,
  text       TEXT NOT NULL,
  result     TEXT NOT NULL
);
"""


@dataclass
class Session:
    session_id: str
    ext: int
    repo: str
    branch: str | None
    worktree: str | None
    cwd: str | None
    term_id: str | None
    status: str
    started_at: float
    updated_at: float

    @property
    def label(self) -> str:
        """repo/branch, for caller ID and spoken prompts."""
        b = self.worktree or self.branch
        return f"{self.repo}/{b}" if b else self.repo


@dataclass
class Voicemail:
    id: int
    session_id: str
    ext: int | None
    label: str
    question: str
    context: str | None
    audio: str | None
    created_at: float
    state: str
    answer: str | None
    answered_at: float | None
    delivery: str | None


@contextmanager
def connect():
    config.HOME.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DB_PATH, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    try:
        yield db
    finally:
        db.close()


def _session(row) -> Session | None:
    return Session(**dict(row)) if row else None


def register(session_id: str, repo: str, branch: str | None, worktree: str | None,
             cwd: str | None, term_id: str | None) -> Session:
    """Add a session and give it an extension. A new session in a tab that already has one
    (after /clear, resume, compact) inherits that tab's extension."""
    s = config.load()
    now = time.time()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        ext = existing["ext"] if existing else None
        if ext is None and term_id:
            prev = db.execute("SELECT * FROM sessions WHERE term_id=?", (term_id,)).fetchone()
            if prev:
                ext = prev["ext"]
                db.execute("DELETE FROM sessions WHERE session_id=?", (prev["session_id"],))
                # Pending questions from the replaced session now belong to the tab's new session.
                db.execute("UPDATE voicemails SET session_id=? WHERE session_id=? AND state='pending'",
                           (session_id, prev["session_id"]))
        if ext is None:
            used = {r[0] for r in db.execute("SELECT ext FROM sessions")}
            ext = next((e for e in range(s.first_session_ext, s.last_session_ext + 1) if e not in used), None)
            if ext is None:
                db.execute("ROLLBACK")
                raise RuntimeError("no free extensions")
        db.execute(
            """INSERT INTO sessions (session_id, ext, repo, branch, worktree, cwd, term_id, status, started_at, updated_at)
               VALUES (?,?,?,?,?,?,?, 'idle', ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET repo=excluded.repo, branch=excluded.branch,
                 worktree=excluded.worktree, cwd=excluded.cwd, term_id=excluded.term_id, updated_at=excluded.updated_at""",
            (session_id, ext, repo, branch, worktree, cwd, term_id, now, now),
        )
        db.execute("COMMIT")
        return _session(db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone())


def unregister(session_id: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))


def set_status(session_id: str, status: str) -> None:
    with connect() as db:
        db.execute("UPDATE sessions SET status=?, updated_at=? WHERE session_id=?", (status, time.time(), session_id))


def all_sessions() -> list[Session]:
    with connect() as db:
        return [_session(r) for r in db.execute("SELECT * FROM sessions ORDER BY ext")]


def by_ext(ext: int) -> Session | None:
    with connect() as db:
        return _session(db.execute("SELECT * FROM sessions WHERE ext=?", (ext,)).fetchone())


def find(session_id: str | None, term_id: str | None = None) -> Session | None:
    """Look up by session id, falling back to the terminal tab (the MCP server's env keeps the
    first session id even after /clear starts a new one in the same tab)."""
    with connect() as db:
        if session_id:
            r = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if r:
                return _session(r)
        if term_id:
            return _session(db.execute("SELECT * FROM sessions WHERE term_id=?", (term_id,)).fetchone())
    return None


def remove_missing(live_term_ids: set[str]) -> list[Session]:
    """Drop sessions whose terminal no longer exists. Returns the removed rows."""
    gone = [s for s in all_sessions() if s.term_id and s.term_id not in live_term_ids]
    with connect() as db:
        for s in gone:
            db.execute("DELETE FROM sessions WHERE session_id=?", (s.session_id,))
    return gone


# --- voicemail ---

def _vm(row) -> Voicemail | None:
    return Voicemail(**dict(row)) if row else None


def add_voicemail(session_id: str, ext: int | None, label: str, question: str,
                  context: str | None, audio: str | None) -> int:
    with connect() as db:
        cur = db.execute(
            "INSERT INTO voicemails (session_id, ext, label, question, context, audio, created_at) VALUES (?,?,?,?,?,?,?)",
            (session_id, ext, label, question, context, audio, time.time()),
        )
        return cur.lastrowid


def pending_voicemails() -> list[Voicemail]:
    with connect() as db:
        return [_vm(r) for r in db.execute("SELECT * FROM voicemails WHERE state='pending' ORDER BY created_at")]


def answer_voicemail(vm_id: int, answer: str, delivery: str) -> None:
    with connect() as db:
        db.execute("UPDATE voicemails SET state='answered', answer=?, answered_at=?, delivery=? WHERE id=?",
                   (answer, time.time(), delivery, vm_id))


def delete_voicemail(vm_id: int) -> None:
    with connect() as db:
        db.execute("UPDATE voicemails SET state='deleted', answered_at=? WHERE id=?", (time.time(), vm_id))


def ringtone(repo: str, choices: int = 8) -> int:
    """A repo's ringtone (1..choices). First sight assigns the least-used one, then it never changes."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT ring FROM ringtones WHERE repo=?", (repo,)).fetchone()
        if row:
            db.execute("COMMIT")
            return row[0]
        used = dict(db.execute("SELECT ring, COUNT(*) FROM ringtones GROUP BY ring").fetchall())
        ring = min(range(1, choices + 1), key=lambda r: (used.get(r, 0), r))
        db.execute("INSERT INTO ringtones (repo, ring) VALUES (?, ?)", (repo, ring))
        db.execute("COMMIT")
        return ring


def log_injection(session: Session | None, source: str, text: str, result: str) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO injections (ts, session_id, ext, term_id, source, text, result) VALUES (?,?,?,?,?,?,?)",
            (time.time(), session.session_id if session else None, session.ext if session else None,
             session.term_id if session else None, source, text, result),
        )
