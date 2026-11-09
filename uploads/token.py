"""token.py — obtain and refresh the per-document asset JWT.

Scribd now serves ``html.scribdassets.com`` page/image URLs behind a short-lived
``?token=`` JWT. The site mints it via:

    POST https://www.scribd.com/document/<id>/token
    headers: X-CSRF-Token: <csrf>   body: csrf_token=<csrf>
    -> {"token": "<jwt>", "enabled": true|false}

``enabled=False`` means the asset host doesn't currently require a token.

IMPORTANT — stdlib shadowing
----------------------------
This file is named ``token.py``, so for any script/REPL launched from this
directory it shadows the stdlib ``token`` module. Stdlib ``tokenize`` (reached
via traceback -> linecache, or inspect -> dataclasses) does::

    from token import *
    from token import EXACT_TOKEN_TYPES
    __all__ = token.__all__ + [...]

and expects the real ``token`` API — including ``__all__``. The graft below
loads the genuine stdlib module from disk and copies its names in *before* we
import anything heavier, so nothing downstream breaks.
"""
from __future__ import annotations


def _graft_stdlib_token() -> None:
    import importlib.util
    import os

    # Names that belong to *this* module and must not be clobbered.
    skip = {
        "__name__", "__file__", "__loader__", "__spec__",
        "__cached__", "__builtins__", "__package__", "__doc__",
    }

    stdlib_dir = os.path.dirname(getattr(os, "__file__", "") or "")
    candidates = [os.path.join(stdlib_dir, "token.py")]
    try:
        import sysconfig
        candidates.append(os.path.join(sysconfig.get_paths()["stdlib"], "token.py"))
    except Exception:
        pass

    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("_stdlib_token", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            continue
        g = globals()
        for key, value in vars(module).items():
            if key in skip:
                continue
            g.setdefault(key, value)   # <-- includes __all__, EXACT_TOKEN_TYPES, ...
        return


_graft_stdlib_token()

import re                                          # noqa: E402, I001
from urllib.parse import (                          # noqa: E402, I001
    parse_qsl, urlencode, urlsplit, urlunsplit,
)

import requests                                     # noqa: E402, I001

SCRIBD = "https://www.scribd.com"
_CSRF_ENDPOINTS = (f"{SCRIBD}/csrf_token",)
_META_CSRF = re.compile(
    r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_CSRF_COOKIE_NAMES = ("csrf_token", "_csrf_token", "csrf")


def _csrf(session, html: str | None = None) -> str | None:
    # 1) dedicated endpoint (JSON or plain text)
    for endpoint in _CSRF_ENDPOINTS:
        try:
            r = session.get(
                endpoint,
                headers={"Accept": "application/json, text/plain, */*"},
            )
        except requests.RequestException:
            continue
        if not r.ok:
            continue
        try:
            data = r.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            tok = data.get("csrf_token") or data.get("token")
            if tok:
                return tok
        else:
            txt = r.text.strip().strip('"')
            if txt and "<" not in txt:
                return txt

    # 2) <meta name="csrf-token"> in the already-fetched HTML
    if html:
        m = _META_CSRF.search(html)
        if m:
            return m.group(1)

    # 3) cookie fallback
    for name in _CSRF_COOKIE_NAMES:
        tok = session.cookies.get(name)
        if tok:
            return tok
    return None


def refresh_token(session, doc_id: str, html: str | None = None):
    """Return ``(token, enabled)``. ``enabled=False`` -> no token required."""
    csrf = _csrf(session, html=html)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{SCRIBD}/document/{doc_id}",
    }
    if csrf:
        headers["X-CSRF-Token"] = csrf
    data = {"csrf_token": csrf} if csrf else {}

    r = session.post(f"{SCRIBD}/document/{doc_id}/token", data=data, headers=headers)
    r.raise_for_status()
    payload = r.json()
    tok = payload.get("token")
    enabled = bool(payload.get("enabled", bool(tok)))
    return tok, enabled


def append_token(url: str, token: str | None) -> str:
    if not token:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["token"] = token
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )
