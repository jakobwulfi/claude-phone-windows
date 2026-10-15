"""Secrets live in Windows Credential Manager under service 'claude-phone' (through keyring).
A CLAUDE_PHONE_<NAME> environment variable overrides the stored value."""

from __future__ import annotations

import os
import secrets as _secrets

import keyring

SERVICE = "claude-phone"


def get(name: str) -> str | None:
    env = os.environ.get(f"CLAUDE_PHONE_{name.upper().replace('-', '_')}")
    if env:
        return env
    return keyring.get_password(SERVICE, name)


def put(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def get_or_create(name: str, nbytes: int = 12) -> str:
    v = get(name)
    if not v:
        v = _secrets.token_hex(nbytes)
        put(name, v)
    return v


def elevenlabs_key() -> str | None:
    return os.environ.get("ELEVENLABS_API_KEY") or get("elevenlabs")
