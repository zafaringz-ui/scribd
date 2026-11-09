#!/usr/bin/env python3
"""
scribd_shot.py — screenshot every page of a Scribd document, then compile a PDF.

One uniform strategy for every kind of document (text pages, scans, slide decks,
comic/image pages): open the document in headless Chromium, let Scribd do all the
rendering itself, take one screenshot per page container, and bind the images
into a PDF with one PDF page per screenshot.  No HTML re-assembly, no font
guessing, no per-document special cases.

    Scribd page  ──►  #outer_page_1 .. #outer_page_N
                        │  (scroll into view → wait for content)
                        └─ screenshot .newpage  →  0001.png  0002.png ...
                                        │
                                        └─ img2pdf → document.pdf

Page boundaries are exact because the screenshot *is* the page box, and the PDF
page size is derived from the document's own page dimensions, so the result has
the same page geometry as the original.

Usage
-----
    python scribd_shot.py <url-or-id> [<url-or-id> ...] [-o OUTDIR]

    python scribd_shot.py https://www.scribd.com/document/949773556
    python scribd_shot.py 949773556 1062697898 -o out
    python scribd_shot.py 949773556 --pages 1-20 --scale 3
    python scribd_shot.py 1234567 --cookies cookies.txt      # private documents

Options that matter
-------------------
--scale N     device pixel ratio for the screenshots (default 2 → 2x the page's
              native CSS size, i.e. 192 dpi).  Use 3 for very small text.
--format      jpg (default, small) or png (lossless, large)
--pages       only render these pages, e.g. 1-20 or 1,4,9-12

Requirements
------------
    pip install playwright img2pdf pypdf pillow
    python -m playwright install chromium        # chrome-headless-shell

On a bare Linux box chromium also needs its shared libraries; see README.md.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import chromium_launch
import logfmt

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Bump this whenever the capture logic changes. It is printed in the banner, so
# a panel running an older scribd_all.py is obvious at a glance.
BUILD = "2026-10-30e"  # live-reader auto rendering; no renderer fallback
SCRIBD = "https://www.scribd.com"

# Elements that Scribd (or its ad/consent vendors) paint on top of the page box.
# They must be hidden or they end up baked into the screenshots.
OVERLAY_SELECTORS = (
    ".osano-cm-window",
    "#onetrust-consent-sdk",
    "#onetrust-banner-sdk",
    ".adsbygoogle",
    "ins.adsbygoogle",
    "iframe",
    "[id^='scribd_ad']",
    "[id^='google_ads']",
    "[class*='ad_container']",
    "[data-testid*='ad']",
)

# Ad/tracker hosts aborted at the network layer.  Blocking the request (instead of
# letting it load and hiding the element) is what stops the black rectangles: an
# aborted ad iframe paints nothing, an unloaded one paints an empty black box.
AD_HOST_RE = (
    r"(doubleclick\.net|googlesyndication\.com|googleadservices\.com"
    r"|adservice\.google\.|pagead2\.googlesyndication|amazon-adsystem\.com"
    r"|taboola\.com|outbrain\.com|criteo\.|pubmatic\.com|rubiconproject\.com"
    r"|openx\.net|casalemedia\.com|adnxs\.com|moatads\.com|scorecardresearch\.com"
    r"|quantserve\.com|zedo\.com|srv\.stackadapt\.com|survicate"
    r"|hotjar\.com|fullstory\.com|clarity\.ms|facebook\.net/tr"
    r"|adsafeprotected\.com|bidswitch\.net|smartadserver\.com)"
)

# Text the reader shows when it decides to gate a document.  Detected after load so
# the tool can switch engines instead of handing over blurred pages.
PAYWALL_MARKERS = (
    "Unlock the next",
    "subscribe to read",
    "Subscribe to read",
    "Continue reading with a subscription",
    "Unlock this document",
    "Sign in to continue",
    "This document is not available",
)


# Stylesheet injected after load: neutralise Scribd's zoom transform so a page
# is screenshotted at its native size, and unhook the containers so nothing of
# the page gets clipped by the zoomed wrapper.
SHOT_CSS = """
.outer_page, .outer_page_container {
  contain: none !important;
  overflow: visible !important;
}
.newpage {
  transform: none !important;
  transform-origin: 0 0 !important;
}
html.shot-mode .shot-hidden { display: none !important; }
"""


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
class Log:
    """Every line the tool prints goes through here, so it is one shape.

    Call sites already spell out their own prefixes ("[<id>] ...", "  -> ...",
    "  cookie note: ..."). Rather than touch 30 of them, this strips the ad-hoc
    noise and re-emits the same text in the shared format - the document id
    becomes the line's context column, which is where it belongs.
    """

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    @staticmethod
    def _clean(msg: str) -> tuple[str, str]:
        """(key, text) - pull the fact out of a hand-written log string."""
        text = str(msg).strip()
        key = "run"
        # "[949773556] loading ..." -> context + text
        m = re.match(r"^\[(\d+)\]\s*(.*)$", text)
        if m:
            logfmt.set_context(m.group(1))
            text = m.group(2)
        # "  -> name.pdf: 7 pages, ..." / "  ! ..." / "  cookie note: ..."
        text = re.sub(r"^[-!>]+\s*", "", text)
        low = text.lower()
        for pat, k, strip in (
            ("fetching styles", "styles", True),
            ("fetched page html", "fetch", True),
            ("fetching", "fetch", True),
            ("page markup", "markup", True),
            ("live rendering stopped", "render", False),
            ("pages in dom", "render", False),
            ("captured", "capture", True),
            ("cache hit", "cache", True),
            ("on drime", "drime", True),
            ("loading", "load", True),
            ("page(s) selected", "select", False),
            ("recovered the manifest", "recover", True),
            ("fonts", "fonts", False),
            ("cookie note", "cookie", False),
            ("displaytype", "document", False),
        ):
            if pat in low:
                key = k
                if strip and low.startswith(pat):
                    text = text[len(pat) :].lstrip(" :.")
                if not text:
                    text = "started"  # "loading ..." carries no fact of its own
                break
        else:
            head, sep, rest = text.partition(": ")
            if sep and head and " " not in head.strip() and len(head) < 14:
                # "cookies: 12 from .env" - a key/value pair
                key, text = head.strip().lower(), rest
            elif ".pdf" in low:
                key = "pdf"
            elif " | " in text:
                key = "title"  # a document title taken from the page itself
        return key, text

    def __call__(self, msg: str) -> None:
        key, text = self._clean(msg)
        logfmt.info(key, text)

    def debug(self, msg: str) -> None:
        key, text = self._clean(msg)
        logfmt.debug(key, text)

    def warn(self, msg: str) -> None:
        key, text = self._clean(msg)
        lines = text.splitlines() or [""]
        logfmt.warn(key, lines[0])
        for extra in lines[1:]:
            logfmt.detail(extra)

    def ok(self, msg: str) -> None:
        key, text = self._clean(msg)
        logfmt.ok(key, text)

    def error(self, msg: str) -> None:
        key, text = self._clean(msg)
        lines = text.splitlines() or [""]
        logfmt.error(key, lines[0])
        for extra in lines[1:]:
            logfmt.detail(extra)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
DOC_ID_PATTERNS = (
    re.compile(r"/(?:document|embeds|doc|presentation|read)/(\d+)"),
    re.compile(r"[?&]doc(?:ument)?_?id=(\d+)"),
    re.compile(r"^(\d{6,})$"),
)


def document_id(url_or_id: str) -> str:
    s = url_or_id.strip()
    for pat in DOC_ID_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    raise ValueError(f"no Scribd document id in {url_or_id!r}")


def document_url(url_or_id: str) -> str:
    doc_id = document_id(url_or_id)
    m = re.search(r"/(?:document|presentation|doc)/(\d+)(?:/([^?#]+))?", url_or_id)
    if m and m.group(2):
        return f"{SCRIBD}/document/{doc_id}/{m.group(2)}"
    return f"{SCRIBD}/document/{doc_id}"


def slugify(text: str, fallback: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text or "").strip()
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:80].strip("-") or fallback


def parse_page_range(spec: str, available: list[int]) -> list[int]:
    """'1-3,7,12-' -> [1,2,3,7,12,...], clamped to what exists."""
    wanted: set[int] = set()
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            a, _, b = chunk.partition("-")
            start = int(a) if a else 1
            stop = int(b) if b else max(available, default=1)
            wanted.update(range(start, stop + 1))
        else:
            wanted.add(int(chunk))
    keep = [n for n in available if n in wanted]
    if not keep:
        raise ValueError(
            f"--pages {spec!r} matched no pages (document has {len(available)})"
        )
    return keep


def compact_ranges(numbers: Iterable[int]) -> str:
    ns = sorted(set(numbers))
    if not ns:
        return ""
    out: list[str] = []
    start = prev = ns[0]
    for n in ns[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    out.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(out)


def append_token(url: str, token: str | None) -> str:
    if not token:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["token"] = token
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


# --------------------------------------------------------------------------- #
# cookies (Netscape cookies.txt or a JSON export), handed straight to Chromium
# --------------------------------------------------------------------------- #
class CookieFileError(RuntimeError):
    pass


COOKIE_HEADER_HINT = re.compile(r"^[\w.\-]+=[^;\s]+(;\s*[\w.\-]+=[^;]*)*\s*;?$")


def _cookies_from_header(text: str) -> list[dict]:
    """Parse a pasted `Cookie:` header: name=value; name2=value2.

    This is what DevTools > Network > Request Headers gives you, and it needs no
    editing: no expiry column, no tabs, no tab-separated fields to get wrong.
    """
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:") :].strip()
    out = []
    for part in text.split(";"):
        part = part.strip().strip('"')
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "value": value,
                "domain": "scribd.com",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return out


def parse_cookies(text: str, p: Path | None = None) -> list[dict]:
    """Accept the three shapes people actually have: Netscape jar, JSON export,
    or a pasted Cookie: header."""
    stripped = text.lstrip()
    if stripped.startswith(("[", "{")):
        return _cookies_from_json(stripped, p or Path("<paste>"))
    if "\t" in text or stripped.startswith("# Netscape") or "#HttpOnly_" in text:
        return _cookies_from_netscape(text, p or Path("<paste>"))
    if COOKIE_HEADER_HINT.match(stripped):
        return _cookies_from_header(stripped)
    return _cookies_from_netscape(text, p or Path("<paste>")) or _cookies_from_header(
        stripped
    )


def cookies_from_env(name: str = "SCRIBD_COOKIES") -> list[dict]:
    """Read cookies straight from .env - no file to create or edit."""
    try:
        import envfile

        envfile.load()
        raw = envfile.get(name) or ""
    except Exception:  # noqa: BLE001
        raw = os.environ.get(name, "")
    if not raw:
        return []
    if raw.lower().endswith((".txt", ".json")) and Path(raw).exists():
        return load_cookies(raw)
    return parse_cookies(raw)


def load_cookies(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise CookieFileError(f"cookie file not found: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    cookies = parse_cookies(text, p)
    if not cookies:
        raise CookieFileError(
            f"{p}: no cookies found (expected a Netscape jar, a JSON export, or a "
            "pasted Cookie: header)"
        )
    return cookies


SCOPED_COOKIE_RE = re.compile(r"^spv_(\d+)$")


def cookie_report(cookies: list[dict], doc_id: str | None = None) -> list[str]:
    """Plain-language summary of a cookie jar, and the traps in it.

    Real exports (browser "Get cookies.txt", curl jars) contain:
      * expired rows, which silently do nothing
      * document-scoped proof-of-view cookies (spv_<id>) that only apply to that
        one document - passing them for a different id looks like a broken login
      * placeholders left by whoever edited the file by hand
    """
    notes: list[str] = []
    now = time.time()
    expired = [
        c for c in cookies if c.get("expires", -1) not in (-1, 0) and c["expires"] < now
    ]
    if expired:
        notes.append(
            f"{len(expired)} cookie(s) already expired and will be ignored: "
            + ", ".join(sorted(c["name"] for c in expired))[:120]
        )
    scoped = {}
    for c in cookies:
        m = SCOPED_COOKIE_RE.match(c["name"])
        if m:
            scoped.setdefault(m.group(1), []).append(c["name"])
    if scoped:
        ids = ", ".join(sorted(scoped))
        if doc_id and doc_id not in scoped:
            notes.append(
                f"the jar has document-scoped cookies for {ids}, but you asked for "
                f"{doc_id}: those cookies are path-scoped and will NOT be sent - "
                "this is expected, not a broken login"
            )
        else:
            notes.append(f"document-scoped cookies present for {ids}")
    holes = [c["name"] for c in cookies if "<<PASTE" in str(c.get("value", ""))]
    if holes:
        notes.append(
            "placeholder text still in: "
            + ", ".join(sorted(holes))
            + " - those cookies are truncated and will be rejected"
        )
    return notes


def _cookies_from_json(text: str, p: Path) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CookieFileError(f"{p}: invalid JSON cookie export ({exc})") from exc
    if isinstance(data, dict):
        data = data.get("cookies", [])
    out = []
    for c in data:
        if not isinstance(c, dict) or "name" not in c:
            continue
        out.append(
            {
                "name": c["name"],
                "value": str(c.get("value", "")),
                "domain": (c.get("domain") or ".scribd.com").lstrip("."),
                "path": c.get("path") or "/",
                "expires": float(c.get("expirationDate") or c.get("expires") or -1),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
                "sameSite": "Lax",
            }
        )
    return out


def _cookies_from_netscape(text: str, p: Path) -> list[dict]:
    out = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            fields = line.split()
        if len(fields) != 7:
            continue
        domain, _flag, cpath, secure, expires, name, value = fields
        try:
            exp = int(float(expires))
        except (TypeError, ValueError):
            exp = 0
        out.append(
            {
                "name": name,
                "value": value,
                "domain": (domain or ".scribd.com").lstrip("."),
                "path": cpath or "/",
                "expires": float(exp) if exp else -1,
                "httpOnly": raw.startswith("#HttpOnly_"),
                "secure": secure.strip().upper() == "TRUE",
                "sameSite": "Lax",
            }
        )
    return out


# --------------------------------------------------------------------------- #
# captured page
# --------------------------------------------------------------------------- #
@dataclass
class Shot:
    number: int
    path: Path
    width: int = 0  # pixels
    height: int = 0
    css_width: int = 0  # the document's own page size in CSS px
    css_height: int = 0
    blank: bool = False


@dataclass
class Capture:
    doc_id: str
    url: str
    title: str = ""
    total_pages: int = 0
    shots: list[Shot] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    paywall: list[str] = field(default_factory=list)
    ads_blocked: int = 0


# --------------------------------------------------------------------------- #
# browser work
# --------------------------------------------------------------------------- #
class BrowserUnavailable(RuntimeError):
    pass


class RendererWedged(RuntimeError):
    """Scribd's reader stopped responding (typical on multi-thousand-page docs)."""

    def __init__(self, page_number: int) -> None:
        super().__init__(f"the reader stopped responding at page {page_number}")
        self.page_number = page_number


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "this tool needs Playwright + Chromium:\n"
            "    pip install playwright && python -m playwright install chromium"
        ) from exc
    return sync_playwright


def _wait_for_page_ready(page, n: int, timeout_ms: int, log: Log) -> bool:
    """Wait until page n has a .newpage with actual content (text or loaded image)."""
    script = """
    (n) => {
      const outer = document.getElementById('outer_page_' + n);
      if (!outer) return false;
      const np = outer.querySelector('.newpage');
      if (!np) return false;
      const text = np.querySelector('.text_layer span');
      const imgs = [...np.querySelectorAll('img')];
      const imgsReady = imgs.length === 0 ||
        imgs.every(i => i.complete && i.naturalWidth > 0);
      const hasContent = !!text || imgs.length > 0;
      return hasContent && imgsReady;
    }
    """
    try:
        page.wait_for_function(script, arg=n, timeout=timeout_ms)
        return True
    except Exception:
        return False


def _screenshot_page(
    page,
    n: int,
    dest: Path,
    fmt: str,
    quality: int,
    log: Log,
    *,
    ready_timeout_ms: int = 10_000,
    settle_ms: int = 250,
) -> Shot | None:
    selector = f"#outer_page_{n} .newpage"
    loc = page.locator(selector)

    _require_responsive(page, n)

    # scrolling is what makes Scribd fetch the page content at all, so it must
    # happen *before* waiting.  A plain JS scrollIntoView is used on purpose:
    # Playwright's scroll_into_view_if_needed waits for the element to stop
    # animating, which never happens on Scribd's smooth-scrolled reader and
    # stalls for the full timeout on every single page.
    try:
        page.evaluate(
            """(n) => {
              const el = document.querySelector('#outer_page_' + n);
              if (el) el.scrollIntoView({block: 'center', inline: 'nearest'});
            }""",
            n,
        )
    except Exception:
        log.debug(f"  page {n}: could not scroll into view")

    if not _wait_for_page_ready(page, n, ready_timeout_ms, log):
        log.debug(f"  page {n}: content not confirmed ready, capturing anyway")
    _hide_overlays(page, log)
    if settle_ms:
        page.wait_for_timeout(settle_ms)

    # geometry *before* the screenshot, measured on the page element
    try:
        box = page.evaluate(
            """(n) => {
              const np = document.querySelector('#outer_page_' + n + ' .newpage');
              if (!np) return null;
              const m = /width:\\s*(\\d+)px/.exec(np.getAttribute('style') || '');
              const r = np.getBoundingClientRect();
              return {cssW: m ? +m[1] : Math.round(r.width),
                      cssH: (function(){
                        const st = np.getAttribute('style') || '';
                        const h = /height:\\s*(\\d+)px/.exec(st);
                        return h ? +h[1] : Math.round(r.height);
                      })(),
                      w: Math.round(r.width), h: Math.round(r.height)};
            }""",
            n,
        )
    except Exception:
        box = None
    if not box or box["w"] < 10 or box["h"] < 10:
        return None

    _hide_occluders(page, n, log)

    dest.parent.mkdir(parents=True, exist_ok=True)
    # caret:"initial" keeps Playwright from waiting on animation stability
    kwargs = {
        "path": str(dest),
        "animations": "disabled",
        "caret": "initial",
        "timeout": 45_000,
    }
    if fmt == "jpeg":
        kwargs["type"] = "jpeg"
        kwargs["quality"] = quality
    else:
        kwargs["type"] = "png"
    try:
        loc.screenshot(**kwargs)
    except Exception as exc:
        log.debug(f"  page {n}: screenshot failed ({type(exc).__name__}: {exc})")
        return None

    if not dest.exists() or dest.stat().st_size == 0:
        return None

    width = height = 0
    blank = False
    try:
        from PIL import Image

        with Image.open(dest) as im:
            width, height = im.size
            if im.width > 8 and im.height > 8:
                lo, hi = im.convert("L").getextrema()
                blank = (hi - lo) < 8
    except Exception:
        pass

    return Shot(
        number=n,
        path=dest,
        width=width,
        height=height,
        css_width=box["cssW"],
        css_height=box["cssH"],
        blank=blank,
    )


def _hide_overlays(page, log: Log) -> None:
    """Accept the consent banner if possible, then hide anything floating over pages."""
    for sel in (
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        ".osano-cm-accept-all",
        "#onetrust-accept-btn-handler",
        "button:has-text('I agree')",
    ):
        try:
            btn = page.locator(sel)
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(400)
                log.debug(f"  dismissed consent via {sel}")
                break
        except Exception:
            continue

    page.evaluate(
        """(selectors) => {
          document.documentElement.classList.add('shot-mode');
          for (const sel of selectors) {
            for (const el of document.querySelectorAll(sel)) {
              el.classList.add('shot-hidden');
            }
          }
        }""",
        list(OVERLAY_SELECTORS),
    )

    # anything fixed/sticky that floats over the page boxes (ads, the floating
    # reader widget, chat bubbles, sticky headers) is noise in a page capture
    page.evaluate(
        """() => {
          const pages = [...document.querySelectorAll('[id^=outer_page_]')]
              .map(o => o.getBoundingClientRect())
              .filter(r => r.width > 40 && r.height > 40);
          if (!pages.length) return;
          for (const el of document.body.querySelectorAll('*')) {
            const cs = getComputedStyle(el);
            if (cs.position !== 'fixed' && cs.position !== 'sticky') continue;
            const r = el.getBoundingClientRect();
            if (r.width < 4 || r.height < 4) continue;
            const hits = pages.some(p =>
              !(r.bottom < p.top || r.top > p.bottom ||
                r.right < p.left || r.left > p.right));
            if (hits) el.classList.add('shot-hidden');
          }
        }"""
    )


def _install_ad_block(ctx, log: Log) -> dict:
    """Abort ad/tracker requests at the protocol layer. Returns a hit counter."""
    hits = {"n": 0}

    def handler(route):
        try:
            if re.search(AD_HOST_RE, route.request.url, re.I):
                hits["n"] += 1
                route.abort()
            else:
                route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    ctx.route("**/*", handler)
    return hits


def _hide_occluders(page, n: int, log: Log) -> int:
    """Hide anything painted over page *n* that is not part of the page itself.

    Sampling ``elementsFromPoint`` on a grid over the page box catches every kind
    of overlay - fixed bars, absolute widgets, ad iframes, shadow-DOM hosts - which
    a selector/position based approach always misses a few of.
    """
    try:
        hidden = page.evaluate(
            """(n) => {
              const box = document.querySelector('#outer_page_' + n + ' .newpage');
              if (!box) return 0;
              const r = box.getBoundingClientRect();
              const xs = 6, ys = 8;
              const seen = new Set();
              const targets = [];
              for (let i = 1; i < xs; i++) {
                for (let j = 1; j < ys; j++) {
                  const x = r.left + (r.width * i) / xs;
                  const y = r.top + (r.height * j) / ys;
                  for (const el of document.elementsFromPoint(x, y)) {
                    if (el === box || box.contains(el)) continue;
                    if (el === document.documentElement ||
                        el === document.body) continue;
                    if (seen.has(el)) continue;
                    seen.add(el);
                    targets.push(el);
                  }
                }
              }
              let hidden = 0;
              for (const el of targets) {
                // never hide an ancestor of the page - that would hide the page
                if (el.contains(box)) continue;
                el.classList.add('shot-hidden');
                hidden++;
              }
              return hidden;
            }""",
            n,
        )
    except Exception as exc:
        log.debug(f"  page {n}: occluder sweep failed ({type(exc).__name__})")
        return 0
    if hidden:
        log.debug(f"  page {n}: hid {hidden} overlay element(s)")
    return hidden


def _wait_for_containers(page, timeout_ms: int, log: Log, doc_id: str) -> int:
    """Wait for the reader to build its #outer_page_* containers.

    Uses a driver-enforced wait (not a JS poll): on very long documents Scribd's
    JS keeps the renderer's main thread busy, and page.evaluate() then hangs
    forever with no way to time out.  wait_for_selector's timeout is enforced by
    Playwright itself, so a wedged reader turns into a fast, catchable failure.
    """
    budget = max(5_000, timeout_ms)
    try:
        page.wait_for_selector("#outer_page_1", state="attached", timeout=budget)
    except Exception as exc:
        log.debug(f"[{doc_id}] containers never appeared ({type(exc).__name__})")
        return 0
    try:
        return page.evaluate(
            "() => document.querySelectorAll('[id^=outer_page_]').length"
        )
    except Exception:
        # containers exist but the thread is too busy to count them
        return 1


class NetLog:
    """Capture what the viewer actually receives, at the protocol layer.

    Attached to the *context*, so it also sees requests issued from web workers,
    and it pulls the response body through CDP rather than re-requesting the URL -
    no second download, no rendering cost.  Useful for auditing which assets a
    document really uses (`--net-log DIR`).
    """

    INTERESTING = re.compile(
        r"html\.scribdassets\.com/.*(pages/|images/|fonts/)|s-f\.scribdassets\.com/.*\.(css|woff2?)$"
    )

    def __init__(self, root: Path, log: Log) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.index: list[dict] = []
        self.saved = 0

    def attach(self, context) -> None:
        context.on("response", self._on_response)

    def _on_response(self, response) -> None:
        try:
            url = response.url
            if not self.INTERESTING.search(url):
                return
            ctype = (response.headers or {}).get("content-type", "")
            if "css" in ctype:
                ext = "css"
            elif "woff2" in ctype or url.endswith(".woff2"):
                ext = "woff2"
            elif "png" in ctype:
                ext = "png"
            elif "jpeg" in ctype or "jpg" in ctype:
                ext = "jpg"
            elif "webp" in ctype:
                ext = "webp"
            elif "javascript" in ctype:
                ext = "jsonp"
            else:
                ext = "bin"
            body = response.body()
            name = f"{self.saved:04d}.{ext}"
            (self.root / name).write_bytes(body)
            self.index.append(
                {
                    "file": name,
                    "url": url,
                    "status": response.status,
                    "content_type": ctype,
                    "bytes": len(body),
                }
            )
            self.saved += 1
        except Exception as exc:
            self.log.debug(f"  net-log skipped a response ({type(exc).__name__})")

    def close(self) -> None:
        if not self.index:
            return
        (self.root / "index.json").write_text(
            json.dumps(self.index, indent=2), encoding="utf-8"
        )
        self.log(f"  net-log: {len(self.index)} responses -> {self.root}")


def capture_document(
    url_or_id: str,
    outdir: Path,
    log: Log,
    *,
    cookies: list[dict] | None = None,
    scale: float = 2,
    fmt: str = "jpeg",
    quality: int = 92,
    pages: str | None = None,
    timeout_ms: int = 120_000,
    settle_ms: int = 250,
    keep_pages: bool = False,
    viewport_width: int = 1400,
    net_log_dir: Path | None = None,
) -> Capture:
    sync_playwright = _playwright()

    doc_id = document_id(url_or_id)
    url = document_url(url_or_id)
    log(f"[{doc_id}] {url}")

    work = outdir / f".{doc_id}-pages"
    work.mkdir(parents=True, exist_ok=True)
    cap = Capture(doc_id=doc_id, url=url)

    with sync_playwright() as pw:
        browser = chromium_launch.launch(
            pw,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                "--force-color-profile=srgb",
                "--font-render-hinting=none",
            ],
        )
        try:
            ctx = _new_context(
                browser,
                user_agent=DEFAULT_UA,
                viewport={"width": viewport_width, "height": 1400},
                device_scale_factor=scale,
                locale="en-US",
            )
            if cookies:
                try:
                    ctx.add_cookies(cookies)
                except Exception as exc:
                    log.warn(f"could not inject cookies: {exc}")

            netlog = None
            if net_log_dir is not None:
                netlog = NetLog(net_log_dir / doc_id, log)
                netlog.attach(ctx)

            page = ctx.new_page()
            page.set_default_timeout(min(timeout_ms, 30_000))

            log(f"[{doc_id}] loading ...")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as exc:
                log.warn(f"page load reported {type(exc).__name__}; continuing")
            count = _wait_for_containers(page, timeout_ms, log, doc_id)
            if not count:
                raise RuntimeError(
                    f"[{doc_id}] Scribd never built its page containers. Either the "
                    "document is private/age-gated (export cookies and pass "
                    "--cookies cookies.txt) or the reader failed to start."
                )

            # the reader is only usable if its main thread still answers - check
            # before touching it (large documents wedge it for minutes, and every
            # later page.evaluate() would hang with no timeout to save us)
            _require_responsive(page, 1, timeout_ms=10_000)

            page.wait_for_timeout(1200)
            cap.title = _document_title(page)
            _hide_overlays(page, log)
            page.add_style_tag(content=SHOT_CSS)
            try:
                page.evaluate(
                    "() => Promise.race([document.fonts.ready,"
                    " new Promise(r => setTimeout(r, 4000))])"
                )
            except Exception:
                pass
            page.wait_for_timeout(300)

            numbers = page.evaluate(
                """() => [...document.querySelectorAll('[id^=outer_page_]')]
                    .map(o => parseInt(o.id.replace('outer_page_', ''), 10))
                    .filter(n => !isNaN(n))
                    .sort((a, b) => a - b)"""
            )
            if not numbers:
                raise RuntimeError(f"[{doc_id}] found no page containers")
            cap.total_pages = max(numbers)

            wanted = parse_page_range(pages, numbers) if pages else numbers
            if pages:
                log(f"[{doc_id}] {len(wanted)} of {len(numbers)} pages selected")

            if cap.total_pages > 800 and len(wanted) > 800:
                raise RendererWedged(wanted[0])

            info = page.evaluate(
                """() => {
                  const q = s => document.querySelector(s);
                  const t = q('meta[property="og:title"]');
                  return {
                    displayType: (window.docManager &&
                                  window.docManager.displayType) || '',
                    paid: (function(){
                      const m = /setupPaidDocument\\(allowedPagesTemp\\s*,\\s*(\\d+)/;
                      const hit = m.exec(document.documentElement.innerHTML);
                      return hit ? +hit[1] : null;
                    })(),
                  };
                }"""
            )
            log(f"[{doc_id}] {len(numbers)} pages in DOM, rendering {len(wanted)}")
            if len(wanted) > 200 and not pages:
                log(
                    f"[{doc_id}] note: {len(wanted)} pages at roughly 1 page/s - "
                    "use --pages to convert it in slices, or --scale 1 to go faster"
                )

            started = time.time()
            for i, n in enumerate(wanted, start=1):
                ext = "jpg" if fmt == "jpeg" else "png"
                dest = work / f"{n:05d}.{ext}"
                shot = _screenshot_page(
                    page,
                    n,
                    dest,
                    fmt,
                    quality,
                    log,
                    ready_timeout_ms=min(timeout_ms, 15_000),
                    settle_ms=settle_ms,
                )
                if shot is None:
                    cap.failed.append(n)
                    log.warn(f"page {n}: could not capture")
                    if not _renderer_alive(page):
                        raise RendererWedged(n)
                else:
                    cap.shots.append(shot)
                    _progress_tick()
                    if i % 10 == 0 or i == len(wanted):
                        rate = i / max(time.time() - started, 1e-6)
                        left = (len(wanted) - i) / max(rate, 1e-6)
                        log(
                            f"[{doc_id}] captured {i}/{len(wanted)} pages "
                            f"({rate:.1f} pages/s, ~{left / 60:.0f} min left)"
                        )

            if info.get("paid") and cap.total_pages and info["paid"] < cap.total_pages:
                log.warn(
                    f"only {info['paid']} of {cap.total_pages} pages are public for "
                    "anonymous visitors - later pages will be blank. Log in and use "
                    "--cookies cookies.txt to get the rest."
                )
            if netlog is not None:
                netlog.close()
        finally:
            browser.close()

    audit_pages(cap.shots, log)

    blanks = [s.number for s in cap.shots if s.blank]
    if blanks:
        log.warn(f"{len(blanks)} captured page(s) look blank: {compact_ranges(blanks)}")
    if cap.failed:
        log.warn(
            f"{len(cap.failed)} page(s) failed to capture: {compact_ranges(cap.failed)}"
        )

    if not keep_pages:
        log.debug(f"  page images in {work}")
    return cap


def _document_title(page) -> str:
    try:
        title = page.title()
    except Exception:
        title = ""
    title = re.sub(r"\s+", " ", title or "").strip()
    title = re.sub(r"\s*[|\-–]\s*Scribd\s*$", "", title, flags=re.IGNORECASE)
    return title.strip() or "scribd-document"


# --------------------------------------------------------------------------- #
# fallback engine: build the pages ourselves, then screenshot them
# --------------------------------------------------------------------------- #
def _require_responsive(page, n: int, timeout_ms: int = 5000) -> None:
    """Abort early if the renderer has stopped answering.

    Every per-page step (measuring the box, screenshotting) is a call into the
    page's main thread.  If Scribd's reader has wedged that thread, those calls
    never return and no timeout protects us - so the health probe happens first.
    """
    try:
        page.wait_for_function("() => true", timeout=timeout_ms)
    except Exception as exc:
        raise RendererWedged(n) from exc


def _renderer_alive(page, timeout_ms: int = 8000) -> bool:
    """True if the page's main thread still answers.

    A wedged renderer makes every page.evaluate() hang forever, so this probe is
    what turns "hangs" into "fails with a reason".  The timeout is enforced by the
    driver, not by the page, so it fires even when the thread is stuck.
    """
    try:
        page.wait_for_function("() => true", timeout=timeout_ms)
        return True
    except Exception:
        return False


_PROGRESS_LOCK = threading.Lock()
_PROGRESS = {"on": False, "done": 0, "total": 0}


def _progress_start(total: int) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS["done"] = 0
        _PROGRESS["total"] = total


def _progress_tick(n: int = 1) -> None:
    """Emit a machine-readable progress line ("@@P done total") on stdout.

    Used by scribd_web.py to draw a progress bar; harmless noise for humans.
    """
    with _PROGRESS_LOCK:
        _PROGRESS["done"] += n
        if not _PROGRESS["on"]:
            return
        logfmt.progress(_PROGRESS["done"], _PROGRESS["total"])


def _capture_chunk(
    chunk: list[tuple[int, str]],
    work: Path,
    *,
    css: str,
    links: list[str],
    size: tuple[int, int],
    fmt: str,
    quality: int,
    scale: float,
    batch: int,
    log: Log,
    doc_id: str,
    label: str,
) -> tuple[list[Shot], list[int]]:
    """Render and screenshot a slice of pages in its own browser.

    Playwright's sync API is bound to a thread, so real parallelism means one
    browser per thread - which is fine: capture is CPU/GPU work inside Chromium
    and the bottlenecks (layout, paint, PNG/JPEG encode) parallelise well.
    """
    shots: list[Shot] = []
    failed: list[int] = []

    with _playwright()() as pw:
        browser = chromium_launch.launch(
            pw,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                "--force-color-profile=srgb",
                "--font-render-hinting=none",
            ],
        )
        try:
            ctx = _new_context(
                browser,
                user_agent=DEFAULT_UA,
                viewport={"width": 1500, "height": 1400},
                device_scale_factor=scale,
                locale="en-US",
            )
            _install_ad_block(ctx, log)
            page = ctx.new_page()
            page.set_default_timeout(120_000)
            page_css = _PRINT_CSS_FORMS(size)

            for start in range(0, len(chunk), batch):
                group = chunk[start : start + batch]
                html = _static_html([h for _, h in group], css, links, size, page_css)
                _load_html(page, html, work)
                try:
                    page.wait_for_function(
                        "() => document.fonts.status === 'loaded'", timeout=20_000
                    )
                except Exception:
                    pass
                page.wait_for_timeout(250)

                for idx, (n, _) in enumerate(group):
                    ext = "jpg" if fmt == "jpeg" else "png"
                    dest = work / f"{n:05d}.{ext}"
                    kwargs = {
                        "path": str(dest),
                        "animations": "disabled",
                        "caret": "initial",
                        "timeout": 45_000,
                    }
                    if fmt == "jpeg":
                        kwargs["type"] = "jpeg"
                        kwargs["quality"] = quality
                    else:
                        kwargs["type"] = "png"
                    try:
                        page.locator(".pg").nth(idx).screenshot(**kwargs)
                    except Exception as exc:
                        log.debug(f"  page {n}: screenshot failed ({exc})")
                        failed.append(n)
                        _progress_tick()
                        continue
                    shot = _make_shot(n, dest, size)
                    if shot is None:
                        failed.append(n)
                    else:
                        shots.append(shot)
                    _progress_tick()
                log.debug(f"[{doc_id}{label}] finished batch at page {group[-1][0]}")
        finally:
            browser.close()
    return shots, failed


def _static_css_once(url: str, scale: float, log: Log) -> tuple[str, list[str]]:
    with _playwright()() as pw:
        browser = chromium_launch.launch(pw)
        try:
            ctx = _new_context(
                browser,
                user_agent=DEFAULT_UA,
                viewport={"width": 1500, "height": 1200},
                device_scale_factor=scale,
            )
            return _static_css(ctx, url, log)
        finally:
            browser.close()


def audit_pages(shots: list[Shot], log: Log) -> None:
    """Check the captured pages before they become a PDF.

    "Messy PDF" is almost always one of three things, each with a cheap
    signature: a blank page (no ink), a page of the wrong shape (a viewport or
    scale problem), or a page byte-identical to an earlier one (a navigation
    that did not take). Naming the page turns a vague complaint into a fix.
    """
    if not shots:
        return
    counts: dict[tuple[int, int], list[int]] = {}
    for s in shots:
        if s.width and s.height:
            counts.setdefault((s.width, s.height), []).append(s.number)

    if len(counts) > 1:
        ranked = sorted(counts.items(), key=lambda kv: -len(kv[1]))
        (main_w, main_h), _ = ranked[0]
        for (w, h), pages in ranked[1:]:
            log.warn(
                f"page(s) {compact_ranges(pages)} are {w}x{h}, most are "
                f"{main_w}x{main_h} - a viewport/scale problem, not the document"
            )

    seen: dict[str, int] = {}
    dupes: list[int] = []
    for shot in shots:
        path = Path(shot.path)
        if not path.exists():
            continue
        digest = hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324 - not crypto
        if digest in seen:
            dupes.append(shot.number)
        else:
            seen[digest] = shot.number
    if dupes:
        log.warn(
            f"page(s) {compact_ranges(dupes)} are identical to an earlier page - "
            "the reader served a stale page; try --engine live"
        )

    ink = len([s for s in shots if not s.blank])
    if ink == len(shots) and not dupes and len(counts) <= 1:
        log.debug(f"pages checked: {len(shots)} ok, no blanks, consistent size")
    elif ink:
        log.info(f"pages checked: {ink} of {len(shots)} have content")


def _load_html(page, html: str, work: Path | None = None, wait: str = "load"):
    """Put HTML on a page and wait for it to load.

    CloakBrowser's build never completes page.set_content() - the load event does
    not fire, so even "<h1>hello</h1>" times out after 120 s (measured). A real
    navigation to the same bytes does fire it, in one millisecond, and images
    from the CDN still load. So on that backend the batch goes through a file
    instead of document.write().
    """
    if chromium_launch.browser_choice() == "cloak":
        # one file per call: with --tabs N several chunks are live at once and a
        # shared name lets one tab navigate to another tab's markup
        stem = f"_batch-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        target = (work or Path(tempfile.gettempdir())) / f"{stem}.html"
        target.write_text(html, encoding="utf-8")
        try:
            page.goto(target.as_uri(), wait_until=wait)
        finally:
            target.unlink(missing_ok=True)
    else:
        page.set_content(html, wait_until=wait)


def _new_context(browser, **kw):
    """Open a context, leaving a stealth browser's own identity alone.

    CloakBrowser ships a coherent UA + fingerprint (Windows, real GPU strings).
    Pasting DEFAULT_UA on top of that makes the two disagree, which is exactly
    what a bot check looks for.
    """
    if chromium_launch.browser_choice() == "cloak":
        kw.pop("user_agent", None)
    return browser.new_context(**kw)


def _browser_html(url: str, log: Log, wait_ms: int = 3500) -> str:
    """Fetch a document page through Chromium, for when plain HTTP is challenged.

    Scribd serves its JS "Client Challenge" to bare requests (rate limits, new
    IPs, VPS ranges).  A real browser clears it in a couple of seconds, so the
    fragment engine borrows one just for the HTML - the page fragments
    themselves come from the CDN and are not challenged.
    """
    with _playwright()() as pw:
        browser = chromium_launch.launch(pw)
        try:
            ctx = _new_context(browser, user_agent=DEFAULT_UA, locale="en-US")
            _install_ad_block(ctx, log)
            page = ctx.new_page()
            page.set_default_timeout(90_000)
            page.goto(url, wait_until="domcontentloaded")
            # The edge's JS challenge clears itself in ~3 s in a browser it
            # trusts; give it up to 25 s before settling.
            deadline = time.time() + 25
            while time.time() < deadline:
                try:
                    title = (page.title() or "").lower()
                except Exception:  # noqa: BLE001
                    break
                if "client challenge" not in title and "just a moment" not in title:
                    break
                page.wait_for_timeout(500)
            page.wait_for_timeout(wait_ms)
            html = page.content()
            log(f"  fetched page HTML in a browser ({len(html) // 1024} KB)")
            return html
        finally:
            browser.close()


def capture_fragments(
    url_or_id: str,
    outdir: Path,
    log: Log,
    *,
    scale: float = 2,
    fmt: str = "jpeg",
    quality: int = 92,
    pages: str | None = None,
    keep_pages: bool = False,
    batch: int = 60,
    cookies_path: str | None = None,
    cache: PageCache | None = None,
    manifest: dict | None = None,
    tabs: int = 1,
) -> Capture:
    """Screenshot every page without using Scribd's reader JS at all.

    Scribd's page manager builds one DOM object per page and, on documents with
    thousands of pages, keeps the renderer's main thread busy for minutes.  This
    engine fetches the page fragments directly, rebuilds the pages into a static
    document, and screenshots each page box - same output, one screenshot per
    page, but fast and bounded regardless of document length.
    """
    try:
        import scribd_pdf as frag
    except ImportError as exc:
        raise RuntimeError(
            "the fragment engine needs scribd_pdf.py next to scribd_shot.py"
        ) from exc

    flog = frag.Log(log.verbose)
    session = frag.build_session(cookies_path, user_agent=DEFAULT_UA)
    target = document_url(url_or_id)
    try:
        doc = frag.fetch_document(session, target, flog)
    except frag.ChallengeError:
        log.warn(
            "plain HTTP got Scribd's bot challenge - retrying the page request "
            "through the browser"
        )
        html = _browser_html(target, log)
        doc = frag.parse_document(html, frag.document_id(target), target)
        if not doc.pages:
            raise RuntimeError(
                f"[{doc.doc_id}] the browser could not read the document either "
                "- it may need a login (see --cookies)"
            ) from None
        log(f"[{doc.doc_id}] recovered the manifest through the browser")
    log(f"[{doc.doc_id}] {doc.title}")
    log(f"[{doc.doc_id}] {len(doc.pages)} pages, displayType={doc.display_type or '?'}")

    wanted_numbers = None
    if pages:
        wanted_numbers = parse_page_range(pages, [p.number for p in doc.pages])
        doc.pages = [p for p in doc.pages if p.number in wanted_numbers]
        log(f"[{doc.doc_id}] {len(doc.pages)} page(s) selected")

    _progress_start(len(doc.pages))
    frag.probe_pages(session, doc, flog, jobs=8)
    readable = [p for p in doc.pages if p.ok]
    if not readable:
        raise RuntimeError(f"[{doc.doc_id}] no readable page fragments")
    token = frag.try_page_token(session, doc, flog)
    patched = [(p.number, frag._patch_page_html(p.html, doc, token)) for p in readable]
    font_css = frag.fetch_font_css(session, doc, [h for _, h in patched], flog)

    sizes = [(p.width, p.height) for p in readable if p.width and p.height]
    size = max(set(sizes), key=sizes.count) if sizes else (902, 1167)

    cap = Capture(
        doc_id=doc.doc_id, url=doc.url, title=doc.title, total_pages=len(doc.pages)
    )
    work = outdir / f".{doc.doc_id}-pages"
    work.mkdir(parents=True, exist_ok=True)
    ext = "jpg" if fmt == "jpeg" else "png"

    # ---- weird case: cache says we already rendered these exact pages
    hashes = (manifest or {}).get("pages", {})
    todo: list[tuple[int, str]] = []
    if cache is not None:
        for n, html in patched:
            key = cache.page_key(doc.doc_id, n, hashes.get(n, "-"), scale, fmt, quality)
            hit = cache.get_page(key, ext)
            if hit:
                dest = work / f"{n:05d}.{ext}"
                shutil.copyfile(hit, dest)
                shot = _make_shot(n, dest, size)
                if shot:
                    cap.shots.append(shot)
            else:
                todo.append((n, html))
        log(
            f"[{doc.doc_id}] cache: {len(cap.shots)}/{len(patched)} pages reused, "
            f"{len(todo)} to render"
        )
    else:
        todo = list(patched)

    log(f"[{doc.doc_id}] fetching styles ...")
    css, links = _static_css_once(doc.url, scale, log)
    if font_css:
        css = font_css + "\n" + css

    if todo:
        started = time.time()
        kwargs = {
            "css": css,
            "links": links,
            "size": size,
            "fmt": fmt,
            "quality": quality,
            "scale": scale,
            "batch": batch,
            "log": log,
            "doc_id": doc.doc_id,
        }
        if tabs <= 1 or len(todo) <= batch:
            shots, failed = _capture_chunk(todo, work, label="", **kwargs)
            cap.shots.extend(shots)
            cap.failed.extend(failed)
        else:
            slices = [todo[i::tabs] for i in range(tabs)]
            log(
                f"[{doc.doc_id}] rendering with {tabs} parallel browser(s); "
                "each one needs ~250 MB of RAM"
            )
            with futures.ThreadPoolExecutor(max_workers=tabs) as pool:
                pending = [
                    pool.submit(_capture_chunk, sl, work, label=f"#{i + 1}", **kwargs)
                    for i, sl in enumerate(slices)
                    if sl
                ]
                for fut in futures.as_completed(pending):
                    shots, failed = fut.result()
                    cap.shots.extend(shots)
                    cap.failed.extend(failed)
        done = len(cap.shots) + len(cap.failed)
        rate = done / max(time.time() - started, 1e-6)
        log(f"[{doc.doc_id}] captured {done}/{len(patched)} pages ({rate:.1f} pages/s)")

        # parallel workers can lose pages to resource pressure - retry them one at
        # a time, where they almost always succeed
        if cap.failed:
            retry = [(n, html) for n, html in patched if n in set(cap.failed)]
            log(
                f"[{doc.doc_id}] retrying {len(retry)} page(s) sequentially: "
                f"{compact_ranges([n for n, _ in retry])}"
            )
            shots, still = _capture_chunk(
                retry,
                work,
                label=" (retry)",
                **{k: v for k, v in kwargs.items()},
            )
            cap.shots.extend(shots)
            cap.failed = [n for n in cap.failed if n in set(still)]

        if cache is not None:
            for shot in cap.shots:
                key = cache.page_key(
                    doc.doc_id,
                    shot.number,
                    hashes.get(shot.number, "-"),
                    scale,
                    fmt,
                    quality,
                )
                cache.put_page(key, ext, shot.path)

    audit_pages(cap.shots, log)

    blanks = [s.number for s in cap.shots if s.blank]
    if blanks:
        log.warn(f"{len(blanks)} captured page(s) look blank: {compact_ranges(blanks)}")
    if cap.failed:
        log.warn(
            f"{len(cap.failed)} page(s) failed to capture: {compact_ranges(cap.failed)}"
        )

    if not keep_pages:
        log.debug(f"  page images in {work}")
    return cap


# --------------------------------------------------------------------------- #
# fallback engine: build the pages ourselves, then screenshot them
# --------------------------------------------------------------------------- #
def _make_shot(n: int, dest: Path, size: tuple[int, int]) -> Shot | None:
    if not dest.exists() or dest.stat().st_size == 0:
        return None
    width = height = 0
    blank = False
    try:
        from PIL import Image

        with Image.open(dest) as im:
            width, height = im.size
            if im.width > 8 and im.height > 8:
                lo, hi = im.convert("L").getextrema()
                blank = (hi - lo) < 8
    except Exception:
        pass
    return Shot(
        number=n,
        path=dest,
        width=width,
        height=height,
        css_width=size[0],
        css_height=size[1],
        blank=blank,
    )


def _static_css(ctx, url: str, log: Log) -> tuple[str, list[str]]:
    """Scribd's stylesheet, harvested with JS blocked (no reader, no fonts)."""
    page = ctx.new_page()
    try:
        page.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in ("script", "media")
                else route.continue_()
            ),
        )
        page.goto(url, wait_until="load", timeout=60_000)
        css = page.evaluate(
            "() => [...document.querySelectorAll('style')]"
            ".map(s => s.textContent).join('\\n')"
        )
        links = page.evaluate(
            "() => [...document.querySelectorAll('link[rel=stylesheet]')]"
            ".map(l => l.href)"
        )
        return css or "", [h for h in links if h]
    except Exception as exc:
        log.warn(f"could not harvest Scribd CSS ({type(exc).__name__}); using defaults")
        return "", []
    finally:
        page.close()


def _PRINT_CSS_FORMS(size: tuple[int, int]) -> str:
    w, h = size
    return f"""
.pg {{ position: relative; width: {w}px; height: {h}px; overflow: hidden;
       background: #fff; margin: 0 auto 24px auto; }}
.pg > .newpage {{ transform: none !important; position: absolute !important;
       left: 0 !important; top: 0 !important; width: {w}px !important;
       height: {h}px !important; }}
.pg .highlighter_canvas {{ display: none !important; }}
"""


def _static_html(
    htmls: list[str], css: str, links: list[str], size, page_css: str
) -> str:
    body = "".join(f'<div class="pg">{h}</div>' for h in htmls)
    link_tags = "".join(f'<link rel="stylesheet" href="{h}">' for h in links)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"{link_tags}<style>{css}</style>"
        f"<style>html,body{{margin:0;padding:0;background:#fff}} {page_css}</style>"
        f"</head><body>{body}</body></html>"
    )


# --------------------------------------------------------------------------- #
# PDF assembly
# --------------------------------------------------------------------------- #
def _assemble_mupdf(shots: list[Shot], out_pdf: Path, pt_scale: float = 0.75) -> None:
    """Assemble with PyMuPDF instead of img2pdf.

    Same pixels, but PyMuPDF re-encodes the JPEGs with Flate (lossless) instead of
    embedding them - on text-heavy captures that is ~2.7x smaller for no quality
    cost.  Requires pymupdf.
    """
    import pymupdf

    doc = pymupdf.open()
    for shot in shots:
        w = (shot.css_width or 902) * pt_scale
        h = (shot.css_height or 1167) * pt_scale
        page = doc.new_page(width=w, height=h)
        page.insert_image(pymupdf.Rect(0, 0, w, h), filename=str(shot.path))
    doc.save(str(out_pdf), deflate=True, garbage=3)
    doc.close()


def build_pdf(
    cap: Capture,
    outdir: Path,
    log: Log,
    pdf_engine: str = "auto",
) -> Path:
    if not cap.shots:
        raise RuntimeError(f"[{cap.doc_id}] nothing was captured")

    out_pdf = outdir / f"{slugify(cap.title, cap.doc_id)}.pdf"
    shots = sorted(cap.shots, key=lambda s: s.number)

    # Each shot is the page rendered at native CSS size * device scale, so the
    # physical page size is: pixels / (CSS px) * 96 dpi.
    layouts: list[float] = []
    for s in shots:
        dpi = 96.0 * s.width / s.css_width if s.css_width and s.width else 96.0
        layouts.append(round(dpi, 2))

    # both assemblers are lossless, so "auto" simply keeps the smaller file:
    # mupdf re-encodes Flate (wins big on text-heavy captures), img2pdf embeds
    # the JPEGs untouched (usually wins on photos)
    if pdf_engine in ("auto", "mupdf"):
        try:
            _assemble_mupdf(shots, out_pdf)
        except ImportError:
            if pdf_engine == "mupdf":
                raise RuntimeError(
                    "--pdf-engine mupdf needs: pip install pymupdf"
                ) from None
            log.debug("  pymupdf not installed - using img2pdf")
        except Exception as exc:
            log.debug(f"  pymupdf assembly failed ({exc}) - using img2pdf")
        else:
            if pdf_engine == "mupdf":
                return out_pdf
            alt = out_pdf.with_name(out_pdf.stem + ".img2pdf.pdf")
            try:
                _assemble_img2pdf(shots, layouts, alt)
            except Exception:
                return out_pdf
            if alt.exists() and alt.stat().st_size < out_pdf.stat().st_size:
                log.debug("  img2pdf assembly was smaller - keeping it")
                alt.replace(out_pdf)
            else:
                alt.unlink(missing_ok=True)
            return out_pdf

    try:
        _assemble_img2pdf(shots, layouts, out_pdf)
    except ImportError:
        log.debug("  img2pdf not installed - falling back to Pillow")
        _pillow_pdf(shots, layouts, out_pdf)

    return out_pdf


def _assemble_img2pdf(shots: list[Shot], layouts: list[float], out_pdf: Path) -> None:
    import img2pdf

    if len(set(layouts)) == 1:
        dpi = layouts[0] if layouts else 96.0
        data = img2pdf.convert(
            [str(s.path) for s in shots],
            layout_fun=img2pdf.get_fixed_dpi_layout_fun((dpi, dpi)),
        )
        out_pdf.write_bytes(data)
        return

    # mixed page sizes: render each page with its own geometry, then merge
    import pypdf

    writer = pypdf.PdfWriter()
    tmp_pages: list[Path] = []
    for shot, page_dpi in zip(shots, layouts):
        one = shot.path.with_suffix(".page.pdf")
        one.write_bytes(
            img2pdf.convert(
                [str(shot.path)],
                layout_fun=img2pdf.get_fixed_dpi_layout_fun((page_dpi, page_dpi)),
            )
        )
        tmp_pages.append(one)
    for one in tmp_pages:
        writer.append(str(one))
    with open(out_pdf, "wb") as fh:
        writer.write(fh)
    for one in tmp_pages:
        one.unlink(missing_ok=True)


def _pillow_pdf(shots: list[Shot], layouts: list[float], out_pdf: Path) -> None:
    from PIL import Image

    frames = []
    for shot in shots:
        with Image.open(shot.path) as im:
            frames.append(im.convert("RGB") if im.mode != "RGB" else im.copy())
    first, rest = frames[0], frames[1:]
    first.save(
        out_pdf,
        "PDF",
        resolution=layouts[0] if layouts else 96.0,
        save_all=True,
        append_images=rest,
    )


def verify_pdf(path: Path, expected: int, log: Log) -> None:
    try:
        import pypdf
    except ImportError:
        log.debug("  pypdf not installed - skipping verification")
        return
    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:
        log.warn(f"could not re-open {path.name}: {exc}")
        return

    blanks = []
    for i, pg in enumerate(reader.pages, start=1):
        text = (pg.extract_text() or "").strip()
        try:
            has_img = len(pg.images) > 0
        except Exception:
            has_img = False
        if not text and not has_img:
            blanks.append(i)

    size_kb = path.stat().st_size / 1024
    box = reader.pages[0].mediabox if reader.pages else None
    dims = f", {float(box.width):.0f}x{float(box.height):.0f}pt" if box else ""
    log(f"  -> {path.name}: {len(reader.pages)} pages, {size_kb:,.0f} KB{dims}")
    if expected and len(reader.pages) != expected:
        log.warn(f"page count {len(reader.pages)} != {expected} captured pages")
    if blanks:
        log.warn(f"{len(blanks)} blank page(s) in the PDF: {compact_ranges(blanks)}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def quick_page_count(target: str, timeout: int = 30) -> int:
    """Page count straight from the document HTML - no browser needed."""
    try:
        import requests
    except ImportError:
        return 0
    try:
        r = requests.get(
            document_url(target),
            headers={"User-Agent": DEFAULT_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return 0
        return len(
            set(
                re.findall(
                    r"html\.scribdassets\.com/[\w]+/pages/(\d+)-[0-9a-z]+\.jsonp",
                    r.text,
                )
            )
        )
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# manifest (one cheap request: titles, page hashes, sizes)
# --------------------------------------------------------------------------- #
def quick_manifest(target: str, timeout: int = 30) -> dict:
    """Read the page manifest without a browser.

    Scribd names every page fragment after a content hash - `pages/7-d3bf133d02.jsonp`
    - which is exactly what a content-addressed cache wants as a key.
    """
    try:
        import requests
    except ImportError:
        return {}
    try:
        r = requests.get(
            document_url(target),
            headers={"User-Agent": DEFAULT_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return {}
    except Exception:
        return {}

    html = r.text
    pages: dict[int, str] = {}
    for m in re.finditer(
        r"html\.scribdassets\.com/[\w]+/pages/(\d+)-([0-9a-z]+)\.jsonp", html
    ):
        pages.setdefault(int(m.group(1)), m.group(2))

    dims: dict[int, tuple[int, int]] = {}
    for m in re.finditer(r"addPage\(\{(.*?)\}\)", html, re.DOTALL):
        body = m.group(1)
        num = re.search(r"pageNum\s*:\s*(\d+)", body)
        w = re.search(r"origWidth\s*:\s*(\d+)", body)
        h = re.search(r"origHeight\s*:\s*(\d+)", body)
        if num and w and h:
            dims[int(num.group(1))] = (int(w.group(1)), int(h.group(1)))

    title = ""
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I
    )
    if m:
        title = m.group(1)
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.I)
        if m:
            title = m.group(1)

    return {"pages": pages, "dims": dims, "title": title, "doc_id": document_id(target)}


# --------------------------------------------------------------------------- #
# content-addressed page cache
# --------------------------------------------------------------------------- #
class PageCache:
    """Hash-addressed store for page screenshots and finished PDFs.

    Page images are keyed by Scribd's own fragment hash + the render parameters,
    so a page is captured at most once per (document, page, scale, format,
    quality) and re-runs cost one manifest request.
    """

    def __init__(self, root: Path, log: Log) -> None:
        self.root = root
        self.pages = root / "pages"
        self.pdfs = root / "pdfs"
        self.pages.mkdir(parents=True, exist_ok=True)
        self.pdfs.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.hits = 0
        self.misses = 0

    def page_key(
        self,
        doc_id: str,
        number: int,
        content_hash: str,
        scale: float,
        fmt: str,
        quality: int,
    ) -> str:
        raw = f"{doc_id}|{number}|{content_hash}|{scale}|{fmt}|{quality}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def get_page(self, key: str, ext: str) -> Path | None:
        p = self.pages / f"{key}.{ext}"
        if p.exists() and p.stat().st_size > 0:
            self.hits += 1
            return p
        self.misses += 1
        return None

    def put_page(self, key: str, ext: str, src: Path) -> Path:
        dest = self.pages / f"{key}.{ext}"
        if not dest.exists():
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            shutil.copyfile(src, tmp)
            tmp.replace(dest)
        return dest

    def pdf_key(self, page_keys: list[str], engine: str) -> str:
        raw = engine + "|" + "|".join(page_keys)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def get_pdf(self, key: str) -> Path | None:
        p = self.pdfs / f"{key}.pdf"
        return p if p.exists() and p.stat().st_size > 0 else None

    def put_pdf(self, key: str, src: Path) -> Path:
        dest = self.pdfs / f"{key}.pdf"
        if not dest.exists():
            tmp = dest.with_suffix(".pdf.tmp")
            shutil.copyfile(src, tmp)
            tmp.replace(dest)
        return dest


def _browser_errors() -> tuple[type[BaseException], ...]:
    """Playwright's own error class, so browser crashes are handled like our own."""
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError:
        return ()
    return (PlaywrightError,)


def convert(
    target: str,
    outdir: Path,
    log: Log,
    *,
    cookies: list[dict] | None = None,
    cookies_path: str | None = None,
    scale: float = 2,
    fmt: str = "jpeg",
    quality: int = 92,
    pages: str | None = None,
    timeout_ms: int = 120_000,
    settle_ms: int = 250,
    keep_pages: bool = False,
    engine: str = "auto",
    cache_dir: Path | None = None,
    tabs: int = 1,
    pdf_engine: str = "auto",
    net_log_dir: Path | None = None,
    allow_live: bool = False,
) -> Path:
    common = {
        "scale": scale,
        "fmt": fmt,
        "quality": quality,
        "pages": pages,
        "keep_pages": keep_pages,
    }

    if tabs > 1:
        cpus = os.cpu_count() or 1
        if tabs > cpus:
            log(
                f"--tabs {tabs} clamped to {cpus}: each parallel tab runs its own "
                "Chromium and this machine has "
                f"{cpus} CPU(s)"
            )
            tabs = cpus

    manifest: dict = {}
    cache: PageCache | None = None
    if cache_dir is not None:
        cache = PageCache(cache_dir, log)
        manifest = quick_manifest(target)
        if not manifest.get("pages"):
            log.warn("could not read the page manifest - caching disabled for this run")
            cache = None
    # finished-PDF cache: a re-run of the same job costs one manifest request
    pdf_key = None
    if cache is not None and manifest.get("pages"):
        numbers = sorted(manifest["pages"])
        if pages:
            numbers = parse_page_range(pages, numbers)
        keys = [
            cache.page_key(
                manifest["doc_id"], n, manifest["pages"][n], scale, fmt, quality
            )
            for n in numbers
        ]
        # Include the build and selected capture route so an older clipped PDF
        # from the cache cannot mask the updated renderer policy.
        pdf_key = cache.pdf_key(keys, f"{pdf_engine}|{BUILD}|{engine}")
        hit = cache.get_pdf(pdf_key)
        if hit:
            title = manifest.get("title") or f"scribd-{manifest['doc_id']}"
            dest = outdir / f"{slugify(title, manifest['doc_id'])}.pdf"
            shutil.copyfile(hit, dest)
            size_kb = dest.stat().st_size / 1024
            log(f"  cache hit - PDF reused ({dest.name}, {size_kb:,.0f} KB)")
            return dest

    # Auto uses the live reader only. Never switch renderers implicitly: doing
    # so can change page geometry and reintroduce clipped or incorrectly sized
    # PDF pages. Fragment reconstruction remains an explicit user selection.
    if engine == "live" and not allow_live:
        raise ValueError("--engine live requires --allow-live")

    cap: Capture | None = None
    if engine in ("auto", "live"):
        cap = capture_document(
            target,
            outdir,
            log,
            cookies=cookies,
            timeout_ms=timeout_ms,
            settle_ms=settle_ms,
            net_log_dir=net_log_dir,
            **common,
        )

    if cap is None:
        cap = capture_fragments(
            target,
            outdir,
            log,
            cookies_path=cookies_path,
            cache=cache,
            manifest=manifest,
            tabs=tabs,
            **{k: common[k] for k in ("scale", "fmt", "quality", "pages")},
        )

    if cap.paywall:
        log.warn(
            "the live reader shows a paywall; no alternate renderer was tried. "
            "Use Scribd login cookies or select --engine fragment explicitly."
        )

    pdf = build_pdf(cap, outdir, log, pdf_engine=pdf_engine)
    verify_pdf(pdf, len(cap.shots), log)

    if cache is not None and pdf_key and not cap.failed:
        cache.put_pdf(pdf_key, pdf)
        log(f"  stored in cache ({cache.hits} page hits, {cache.misses} misses)")

    work = outdir / f".{cap.doc_id}-pages"
    if keep_pages:
        dest = outdir / f"{slugify(cap.title, cap.doc_id)}-pages"
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.move(str(work), str(dest))
        log(f"  page images kept in {dest}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return pdf


def _offload_pdf(pdf: Path, log: Log, wait: int = 0) -> dict | None:
    """Move a finished PDF to Drime so the panel's disk does not fill up."""
    try:
        import drime
    except Exception as exc:  # noqa: BLE001 - offload is optional
        log.warn(f"drime: not available ({exc}) - keeping {pdf.name} locally")
        return None
    if not drime.enabled():
        return None
    if wait:
        time.sleep(wait)  # give a watching UI/bot a moment to pick the file up
    log(f"  offloading {pdf.name} to Drime ...")
    entry = drime.offload(pdf, log=lambda m: log(f"  {m}"))
    if entry:
        link = entry.get("url") or ""
        log(f"  on drime: id {entry.get('id')}" + (f" {link}" if link else ""))
        logfmt.record("DRIME", entry.get("id"), link, pdf.name)
    return entry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scribd_shot.py",
        description="Screenshot every page of a Scribd document and compile a PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python scribd_shot.py 949773556\n"
            "  python scribd_shot.py "
            "https://www.scribd.com/document/1062697898 -o out\n"
            "  python scribd_shot.py 949773556 --pages 1-20 --scale 3 --format png\n"
        ),
    )
    ap.add_argument(
        "targets",
        nargs="*",
        metavar="URL_OR_ID",
        help="Scribd document URL or numeric document id",
    )
    ap.add_argument(
        "--from-file",
        default=None,
        metavar="PATH",
        help="read targets from a file, one URL/id per line (# starts a comment)",
    )
    ap.add_argument("-o", "--outdir", default=".", help="output directory (default: .)")
    ap.add_argument(
        "-c",
        "--cookies",
        default=None,
        help="cookies.txt (Netscape), JSON export, or a pasted Cookie: header",
    )
    ap.add_argument(
        "--cookies-env",
        action="store_true",
        help="read cookies from SCRIBD_COOKIES in .env (no file to edit)",
    )
    ap.add_argument(
        "--scale",
        type=float,
        default=2.0,
        help="device pixel ratio for screenshots (default: 2 = 192 dpi)",
    )
    ap.add_argument(
        "--format",
        choices=("jpg", "png"),
        default="jpg",
        help="screenshot format (default: jpg)",
    )
    ap.add_argument(
        "--quality", type=int, default=92, help="JPEG quality, 1-100 (default: 92)"
    )
    ap.add_argument(
        "--pages",
        default=None,
        metavar="RANGE",
        help="only these pages, e.g. 1-20 or 1,4,9-12 (default: all)",
    )
    ap.add_argument(
        "--settle",
        type=int,
        default=600,
        metavar="MS",
        help="extra wait per page in ms (default: 600)",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="page load timeout in seconds (default: 120)",
    )
    ap.add_argument(
        "--engine",
        choices=("auto", "live", "fragment"),
        default="auto",
        help="auto = use the live reader only, with no renderer fallback (default); "
        "fragment = rebuild and screenshot page markup; live = force the reader "
        "(requires --allow-live)",
    )
    ap.add_argument(
        "--cache",
        default=None,
        metavar="DIR",
        help="content-addressed cache dir: rendered pages and finished PDFs are "
        "reused on re-runs (page images are keyed by Scribd's own fragment hash)",
    )
    ap.add_argument(
        "--tabs",
        type=int,
        default=1,
        metavar="N",
        help="render pages in N parallel browsers (fragment engine, default: 1)",
    )
    ap.add_argument(
        "--pdf-engine",
        choices=("auto", "img2pdf", "mupdf"),
        default="auto",
        help="PDF assembly: mupdf re-encodes losslessly and is ~2.7x smaller "
        "(default: auto)",
    )
    ap.add_argument(
        "--net-log",
        default=None,
        metavar="DIR",
        help="dump every page/image/font response the viewer receives, "
        "captured at the protocol layer (debugging/auditing)",
    )
    ap.add_argument(
        "--keep-pages",
        action="store_true",
        help="keep the page screenshots next to the PDF",
    )
    ap.add_argument(
        "--chromium-path",
        default=None,
        metavar="PATH",
        help="browser executable to use (or set $SCRIBD_CHROMIUM); "
        "see chromium_setup.py for hosts without root",
    )
    ap.add_argument(
        "--progress",
        action="store_true",
        help="print machine-readable '@@P done total' progress lines",
    )
    ap.add_argument(
        "--offload",
        dest="offload",
        action="store_true",
        default=None,
        help="upload the finished PDF to Drime and delete it locally "
        "(default: on when DRIME_API_KEY and DRIME_FOLDER are in .env)",
    )
    ap.add_argument(
        "--no-offload",
        dest="offload",
        action="store_false",
        help="keep the PDF on local disk even when Drime is configured",
    )
    ap.add_argument(
        "--allow-live",
        action="store_true",
        help="permit --engine live (renders Scribd's reader as-is; pages can come "
        "out unscaled when its stylesheet does not load)",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    ap.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="warnings and errors only (same as SCRIBD_QUIET=1)",
    )
    args = ap.parse_args(argv)

    targets = list(args.targets)
    if args.from_file:
        path = Path(args.from_file)
        if not path.exists():
            logfmt.error("targets", f"file not found: {path}")
            return 2
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                targets.append(line)

    if getattr(args, "chromium_path", None):
        chromium_launch.set_override(args.chromium_path)

    logfmt.setup(verbose=args.verbose, quiet=args.quiet)
    logfmt.quiet_third_party()
    log = Log(args.verbose)
    if args.progress:
        _PROGRESS["on"] = True

    if not targets:
        # A hosting panel (Pterodactyl, Discord-bot hosts, ...) treats any exit as a
        # server crash and restart-loops it, so this must not look like a failure:
        # print how to use it and exit cleanly.
        me = Path(sys.argv[0]).name or "scribd_shot.py"
        print(
            "Nothing to do - no document URL or id was given.\n\n"
            f"  python {me} <URL_OR_ID> [-o OUTDIR]\n\n"
            "Examples:\n"
            f"  python {me} 272351848 -o output --engine fragment\n"
            f"  python {me} https://www.scribd.com/document/949773556\n"
            f"  python {me} --from-file ids.txt -o output\n\n"
            "If this is your panel's Startup command, either put the arguments there\n"
            "or run it in the background, e.g.:\n"
            f"  nohup python {me} <id> -o output > job.log 2>&1 &",
            flush=True,
        )
        return 0
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    logfmt.info("build", f"{BUILD} (live-reader auto, no renderer fallback)")
    logfmt.info("output", str(outdir.resolve()))
    _browser_line = chromium_launch.describe().replace("chromium: ", "")
    logfmt.info("browser", _browser_line.splitlines()[0])
    if not chromium_launch.status().get("ok"):
        for _extra in _browser_line.splitlines()[1:]:
            logfmt.detail(_extra)

    cookies = None
    if args.cookies_env or (not args.cookies and cookies_from_env()):
        cookies = cookies_from_env()
        if cookies:
            log(f"loaded {len(cookies)} cookies from SCRIBD_COOKIES (.env)")
        else:
            log.warn(
                "SCRIBD_COOKIES is empty - put a Cookie: header in .env, e.g.\n"
                "  SCRIBD_COOKIES=scribd_ubtc=...; _scribd_session=..."
            )
    elif args.cookies:
        try:
            cookies = load_cookies(args.cookies)
            log(f"loaded {len(cookies)} cookies from {args.cookies}")
        except CookieFileError as exc:
            log.warn(f"{exc} - continuing without cookies")
            cookies = None

    if cookies:
        hint = None
        if targets:
            m = re.search(r"(\d{5,})", str(targets[0]))
            hint = m.group(1) if m else None
        for note in cookie_report(cookies, hint):
            log(f"  cookie note: {note}")

    failures = 0
    for target in targets:
        started = time.time()
        try:
            pdf = convert(
                target,
                outdir,
                log,
                cookies=cookies,
                cookies_path=args.cookies,
                engine=args.engine,
                cache_dir=Path(args.cache) if args.cache else None,
                tabs=max(1, args.tabs),
                pdf_engine=args.pdf_engine,
                net_log_dir=Path(args.net_log) if args.net_log else None,
                scale=args.scale,
                fmt="jpeg" if args.format == "jpg" else "png",
                quality=max(1, min(100, args.quality)),
                pages=args.pages,
                timeout_ms=args.timeout * 1000,
                settle_ms=max(0, args.settle),
                keep_pages=args.keep_pages,
                allow_live=args.allow_live,
            )
        except BrowserUnavailable as exc:
            log.warn(str(exc))
            return 3
        except Exception as exc:
            failures += 1
            m = re.search(r"(\d{5,})", str(target))
            logfmt.set_context(m.group(1) if m else target)
            lines = str(exc).splitlines() or [""]
            logfmt.error("convert", lines[0])
            for extra in lines[1:]:
                logfmt.detail(extra)
            if args.verbose:
                import traceback

                traceback.print_exc()
        else:
            logfmt.ok("done", f"{pdf.resolve()}  {time.time() - started:.1f}s")
            if args.offload is not False:
                _offload_pdf(pdf, log)

    if failures:
        logfmt.error("summary", f"{failures} of {len(targets)} target(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
