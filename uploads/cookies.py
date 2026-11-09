"""cookies.py

Load a Netscape-format ``cookies.txt`` (exported from your logged-in browser)
into a ``requests.Session``.

Handles the junk real browser exports contain:
  * ``#HttpOnly_`` line prefixes
  * ``# Netscape HTTP Cookie File`` magic header and ``#`` comments
  * session cookies (expiry 0) and already-expired rows
  * tab- OR whitespace-separated rows

Scribd ties page/image access to the session that requested the doc, so the
session built here is reused for every later request.
"""
from __future__ import annotations

from pathlib import Path

import requests
from requests.cookies import create_cookie

DEFAULT_COOKIE_FILE = Path("cookies.txt")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class CookieFileError(RuntimeError):
    pass


def _iter_rows(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) != 7:
                fields = line.split()
            if len(fields) != 7:
                continue
            yield fields


def parse_cookie_file(cookie_file: str | Path = DEFAULT_COOKIE_FILE):
    path = Path(cookie_file)
    if not path.exists():
        raise CookieFileError(
            f"Cookie file not found: {path}\n"
            "Export your logged-in Scribd cookies in Netscape format "
            "(e.g. the 'Get cookies.txt LOCALLY' browser extension) and save "
            f"it as '{DEFAULT_COOKIE_FILE}'."
        )

    jar = requests.cookies.RequestsCookieJar()
    count = 0
    for domain, _flag, cpath, secure, expires, name, value in _iter_rows(path):
        try:
            expires_ts = int(expires)
        except (TypeError, ValueError):
            expires_ts = 0
        jar.set_cookie(
            create_cookie(
                name=name,
                value=value,
                domain=domain or None,
                path=cpath or "/",
                secure=secure.strip().upper() == "TRUE",
                expires=expires_ts or None,   # 0 -> session cookie
            )
        )
        count += 1

    if not count:
        raise CookieFileError(f"No usable cookie rows parsed from {path}.")
    return jar


def build_session(cookie_file: str | Path = DEFAULT_COOKIE_FILE,
                  user_agent: str | None = None) -> requests.Session:
    session = requests.Session()
    session.cookies = parse_cookie_file(cookie_file)
    session.headers.update({
        "User-Agent": user_agent or DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.scribd.com/",
    })
    return session