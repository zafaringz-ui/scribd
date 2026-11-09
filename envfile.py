#!/usr/bin/env python3
"""
envfile.py — read settings from a .env file, the familiar way.

    DISCORD_TOKEN=MTIz...
    ALLOWED_GUILD_IDS=123456789,987654321
    SCRIBD_CHROMIUM=/home/container/.chromium/chromium

Deliberately dependency-free (no python-dotenv) and small, with the rules people
expect:

  * `KEY=value`, optional `export ` prefix, `#` comments, blank lines
  * single or double quotes around the value, stripped
  * `KEY=` sets an empty string; a bare `KEY` is ignored
  * CRLF files (Windows) work
  * **a real environment variable always wins** unless override=True, so the
    panel's Startup variables still take precedence over the file
  * values are never evaluated - no `$VAR` expansion, no shell

Usage:
    import envfile
    envfile.load()                 # reads ./.env if it exists
    envfile.get("DISCORD_TOKEN")   # file or real environment
"""

from __future__ import annotations

import os
from pathlib import Path

LOADED_FROM: Path | None = None
_FOUND: dict[str, str] = {}
APPLIED: dict[str, str] = {}  # keys this file actually put into the environment


def parse(text: str) -> dict[str, str]:
    """Turn .env text into a dict. Pure function, so it is easy to test."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")  # BOM from a Windows editor
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue  # a bare KEY means nothing; do not guess
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # strip a trailing comment on unquoted values:  KEY=abc   # note
            hash_at = value.find(" #")
            if hash_at != -1:
                value = value[:hash_at].rstrip()
        out[key] = value
    return out


def candidates(explicit: str | None = None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    here = Path(__file__).resolve().parent
    seen: list[Path] = []
    for path in (Path.cwd() / ".env", here / ".env"):
        if path not in seen:
            seen.append(path)
    return seen


def load(path: str | None = None, override: bool = False, quiet: bool = True) -> dict:
    """Load the first .env found. Returns the values it applied."""
    global LOADED_FROM
    applied: dict[str, str] = {}
    for candidate in candidates(path):
        if not candidate.is_file():
            if path:  # explicitly requested but missing -> say so
                raise FileNotFoundError(f"no such file: {candidate}")
            continue
        try:
            text = candidate.read_text(encoding="utf-8-sig")
        except OSError as exc:
            if not quiet:
                print(f"!! could not read {candidate}: {exc}")
            continue
        values = parse(text)
        for key, value in values.items():
            _FOUND[key] = value
            if override or key not in os.environ:
                os.environ[key] = value
                applied[key] = value
                APPLIED[key] = value
        LOADED_FROM = candidate
        break
    return applied


def get(key: str, default: str | None = None) -> str | None:
    """A setting from the environment, falling back to an unloaded .env value."""
    value = os.environ.get(key)
    if value:
        return value
    loaded = _FOUND.get(key)
    if loaded:
        return loaded
    return default
