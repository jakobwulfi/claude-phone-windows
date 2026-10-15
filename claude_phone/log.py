"""One append-only log for calls, transcripts and injections: tail -f ~/.claude-phone/logs/phone.log"""

from __future__ import annotations

import logging

from . import config

_configured = False


def get(name: str = "phone") -> logging.Logger:
    global _configured
    if not _configured:
        config.LOGS.mkdir(parents=True, exist_ok=True)
        # Housekeeping rotates by copy-and-truncate, so the handle stays valid; append mode keeps writes at the end.
        h = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s"))
        root = logging.getLogger("phone")
        root.addHandler(h)
        root.setLevel(logging.INFO)
        _configured = True
    return logging.getLogger("phone" if name == "phone" else f"phone.{name}")
