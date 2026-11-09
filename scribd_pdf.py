#!/usr/bin/env python3
"""
scribd_pdf.py — save a Scribd document as a real PDF (selectable text when possible).

Why this works when the old image-only scraper stopped working
--------------------------------------------------------------
Scribd renders most documents as *HTML text layers* (`displayType = "vector"`),
not as page images.  `html.scribdassets.com/<prefix>/images/<n>-<hash>.jpg` is then
only the decoration layer: often a 1x1 blank placeholder, or 403 AccessDenied.
That is why page-image scraping "works" but produces blank/garbage PDFs.

This tool:

  1. reads the page manifest straight out of the document HTML,
  2. fetches each page's `.jsonp` fragment (the real page markup),
  3. classifies the document:
       * image document  -> downloads the page images losslessly (fast, no browser)
       * text  document  -> rebuilds every page into a standalone HTML page with
                            Scribd's own CSS + embedded fonts and prints it with
                            headless Chromium => one PDF, selectable text, tiny file,
  4. verifies the result (page count, blank-page check) and reports honestly.

Usage
-----
    python scribd_pdf.py <url-or-id> [<url-or-id> ...] [-o OUTDIR]

    python scribd_pdf.py https://www.scribd.com/document/949773556
    python scribd_pdf.py 949773556 400968959 -o out
    python scribd_pdf.py 949773556 --cookies cookies.txt      # for private docs
    python scribd_pdf.py 949773556 --mode image               # force image scrape

Requirements
------------
    pip install requests img2pdf pillow playwright pypdf
    python -m playwright install chromium        # only needed for text documents

`--mode image` needs no browser at all.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as futures
import json
import random
import re
import shutil
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

import chromium_launch

SCRIBD = "https://www.scribd.com"
ASSETS_HOST = "https://html.scribdassets.com"
LEGACY_ASSETS_HOST = "http://html.scribd.com"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
class Log:
    """Route this engine's lines through the shared formatter.

    scribd_pdf is imported both on its own and inside the bundle, so the import
    is lazy: a missing logfmt must never stop a conversion.
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    @staticmethod
    def _fmt(msg: str):
        try:
            import logfmt
        except Exception:  # noqa: BLE001
            return None, str(msg)
        text = str(msg).strip()
        m = re.match(r"^\[(\d+)\]\s*(.*)$", text)
        if m:
            logfmt.set_context(m.group(1))
            text = m.group(2)
        low = text.lower()
        for pat, key in (
            ("fetching", "fetch"),
            ("page markup", "markup"),
            ("pages, displaytype", "document"),
            ("page(s) selected", "select"),
            ("printing pages", "print"),
            ("recovered the manifest", "recover"),
            ("styles", "styles"),
        ):
            if pat in low:
                return key, text
        return "fragment", text

    def __call__(self, msg: str) -> None:
        try:
            import logfmt
        except Exception:  # noqa: BLE001
            print(msg, flush=True)
            return
        key, text = self._fmt(msg)
        logfmt.info(key, text)

    def debug(self, msg: str) -> None:
        if not self.verbose:
            return
        try:
            import logfmt

            key, text = self._fmt(msg)
            logfmt.debug(key, text)
        except Exception:  # noqa: BLE001
            print(msg, flush=True)

    def warn(self, msg: str) -> None:
        try:
            import logfmt

            key, text = self._fmt(msg)
            lines = text.splitlines() or [""]
            logfmt.warn(key, lines[0])
            for extra in lines[1:]:
                logfmt.detail(extra)
        except Exception:  # noqa: BLE001
            print(f"  ! {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# url helpers
# --------------------------------------------------------------------------- #
DOC_ID_PATTERNS = [
    re.compile(r"/(?:document|embeds|doc|presentation|read)/(\d+)"),
    re.compile(r"[?&]doc(?:ument)?_?id=(\d+)"),
    re.compile(r"^(\d{6,})$"),
]


def document_id(url_or_id: str) -> str:
    s = url_or_id.strip()
    for pat in DOC_ID_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    raise ValueError(f"no Scribd document id in {url_or_id!r}")


def document_url(url_or_id: str) -> str:
    doc_id = document_id(url_or_id)
    s = url_or_id.strip()
    # keep the human-readable slug when we already have one
    m = re.search(r"/(?:document|presentation|doc)/(\d+)(?:/([^?#]+))?", s)
    if m and m.group(2):
        return f"{SCRIBD}/document/{doc_id}/{m.group(2)}"
    return f"{SCRIBD}/document/{doc_id}"


def append_token(url: str, token: str | None) -> str:
    if not token:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["token"] = token
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def slugify(text: str, fallback: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:80].strip("-") or fallback


# --------------------------------------------------------------------------- #
# cookies (Netscape cookies.txt *or* a JSON export)
# --------------------------------------------------------------------------- #
class CookieFileError(RuntimeError):
    pass


def load_cookie_list(path: str | Path) -> list[dict]:
    """Return Playwright-style cookie dicts.  Accepts Netscape or JSON exports."""
    p = Path(path)
    if not p.exists():
        raise CookieFileError(f"cookie file not found: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")

    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        return _load_json_cookies(stripped, p)
    return _load_netscape_cookies(text, p)


def _norm_domain(domain: str) -> str:
    return domain.lstrip(".") or ".scribd.com"


def _load_json_cookies(text: str, p: Path) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CookieFileError(
            f"{p}: looks like JSON but does not parse ({exc})"
        ) from exc
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
                "domain": _norm_domain(c.get("domain", ".scribd.com")),
                "path": c.get("path") or "/",
                "expires": float(c.get("expirationDate") or c.get("expires") or -1),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
                "sameSite": "Lax",
            }
        )
    if not out:
        raise CookieFileError(f"{p}: no cookies found in JSON export")
    return out


def _load_netscape_cookies(text: str, p: Path) -> list[dict]:
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
                "domain": _norm_domain(domain),
                "path": cpath or "/",
                "expires": float(exp) if exp else -1,
                "httpOnly": raw.startswith("#HttpOnly_"),
                "secure": secure.strip().upper() == "TRUE",
                "sameSite": "Lax",
            }
        )
    if not out:
        raise CookieFileError(f"{p}: no usable Netscape cookie rows found")
    return out


def build_session(
    cookies_path: str | None = None, user_agent: str | None = None
) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": user_agent or DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    if cookies_path:
        for c in load_cookie_list(cookies_path):
            s.cookies.set(
                c["name"], c["value"], domain=c["domain"].lstrip("."), path=c["path"]
            )
    return s


# --------------------------------------------------------------------------- #
# document metadata + page manifest
# --------------------------------------------------------------------------- #
@dataclass
class PageRef:
    number: int
    jsonp_url: str
    width: int = 0
    height: int = 0
    html: str = ""  # filled in by probe_pages()
    has_text: bool = False
    image_names: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.html.strip())


@dataclass
class DocInfo:
    doc_id: str
    url: str
    title: str
    prefix: str
    pages: list[PageRef]
    display_type: str = ""
    embedded: bool = False
    allowed_pages: int | None = None
    total_pages: int | None = None
    logged_in: bool = False

    @property
    def is_text_document(self) -> bool:
        return any(p.has_text for p in self.pages)

    @property
    def truncated(self) -> bool:
        if self.allowed_pages is None or self.total_pages is None:
            return False
        return self.allowed_pages < self.total_pages


_ASSET_PAGE_RE = re.compile(
    r"https?://html\.scribdassets\.com/(?P<prefix>[0-9a-zA-Z]+)/pages/"
    r"(?P<num>\d+)-(?P<hash>[0-9a-z]+)\.jsonp",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I
)
_ADD_PAGE_RE = re.compile(r"addPage\(\{(?P<body>.*?)\}\)", re.DOTALL)
_PREFIX_RE = re.compile(r'assetPrefix\s*=\s*"([^"]+)"')
_DISPLAY_RE = re.compile(r'displayType\s*=\s*"([^"]+)"')
_EMBEDDED_RE = re.compile(r"setEmbeddedDoc\('(\w+)'\)")
_ALLOWED_RE = re.compile(r"allowedPagesTemp\s*=\s*\[([^\]]*)\]")
_TOTAL_RE = re.compile(r"setupPaidDocument\(\s*allowedPagesTemp\s*,\s*(\d+)")
_LOGGED_IN_RE = re.compile(
    r'"is_logged_in"\s*:\s*(true|false)|isLoggedIn\s*[:=]\s*(true|false)'
)


def _clean_title(title: str, fallback: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    title = re.sub(r"\s*[|\-–]\s*Scribd\s*$", "", title, flags=re.I)
    title = re.sub(r"\s*\((?:free download|read online)\)\s*$", "", title, flags=re.I)
    title = title.strip(" |")
    return title or fallback


def parse_document(html: str, doc_id: str, url: str) -> DocInfo:
    prefix = ""
    m = _PREFIX_RE.search(html)
    if m:
        prefix = m.group(1)

    dims: dict[int, tuple[int, int]] = {}
    for m in _ADD_PAGE_RE.finditer(html):
        body = m.group("body")
        num = re.search(r"pageNum\s*:\s*(\d+)", body)
        w = re.search(r"origWidth\s*:\s*(\d+)", body)
        h = re.search(r"origHeight\s*:\s*(\d+)", body)
        if num and w and h:
            dims[int(num.group(1))] = (int(w.group(1)), int(h.group(1)))

    pages: dict[int, PageRef] = {}
    for m in _ASSET_PAGE_RE.finditer(html):
        num = int(m.group("num"))
        prefix = prefix or m.group("prefix")
        if num in pages:
            continue
        w, h = dims.get(num, (0, 0))
        pages[num] = PageRef(
            number=num,
            jsonp_url=f"{ASSETS_HOST}/{m.group('prefix')}/pages/{num}-{m.group('hash')}.jsonp",
            width=w,
            height=h,
        )

    if not pages:
        raise RuntimeError(
            "no page manifest found in the document HTML — Scribd may have changed "
            "its markup, or this is not a document page (use the /document/<id> URL)"
        )

    allowed = None
    m = _ALLOWED_RE.search(html)
    if m and m.group(1).strip():
        allowed = len([x for x in m.group(1).split(",") if x.strip()])
    total = None
    m = _TOTAL_RE.search(html)
    if m:
        total = int(m.group(1))

    title = ""
    m = _OG_TITLE_RE.search(html)
    if m:
        title = m.group(1)
    if not title:
        m = _TITLE_RE.search(html)
        if m:
            title = m.group(1)
    title = _clean_title(title, f"scribd-{doc_id}")

    logged_in = False
    m = _LOGGED_IN_RE.search(html)
    if m:
        logged_in = (m.group(1) or m.group(2) or "").lower() == "true"

    dt = _DISPLAY_RE.search(html)
    emb = _EMBEDDED_RE.search(html)

    return DocInfo(
        doc_id=doc_id,
        url=url,
        title=title,
        prefix=prefix,
        pages=[pages[n] for n in sorted(pages)],
        display_type=dt.group(1) if dt else "",
        embedded=bool(emb and emb.group(1).lower() == "true"),
        allowed_pages=allowed,
        total_pages=total,
        logged_in=logged_in,
    )


class ChallengeError(RuntimeError):
    """Scribd answered with its JS "Client Challenge" page instead of the doc."""


def _is_challenge(html: str) -> bool:
    head = html[:4000].lower()
    return ("client challenge" in head or "captcha" in head) and len(html) < 20000


def fetch_document(
    session: requests.Session, url: str, log: Log, timeout: int = 60
) -> DocInfo:
    doc_id = document_id(url)
    target = document_url(url)
    log(f"[{doc_id}] fetching {target}")
    r = _request(session, target, log, timeout=timeout)
    if _is_challenge(r.text):
        raise ChallengeError(
            f"[{doc_id}] Scribd served a bot challenge to this IP instead of the "
            "document (plain HTTP is challenged first). Use --engine live, which "
            "runs a real browser, or pass --cookies cookies.txt"
        )
    if r.status_code == 404:
        raise RuntimeError(f"[{doc_id}] document not found (404) — check the URL/id")
    if r.status_code in (401, 403):
        raise RuntimeError(
            f"[{doc_id}] HTTP {r.status_code} — needs a logged-in session. "
            "Export your Scribd cookies and pass --cookies cookies.txt"
        )
    if r.status_code != 200:
        raise RuntimeError(
            f"[{doc_id}] HTTP {r.status_code} while fetching the document page"
        )
    return parse_document(r.text, doc_id, target)


def _request(session, url, log, *, timeout=60, tries=4, headers=None, stream=False):
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = session.get(url, timeout=timeout, headers=headers, stream=stream)
        except requests.RequestException as exc:
            last = exc
            log.debug(f"    network error on {url[:90]}: {exc}")
        else:
            if r.status_code not in RETRYABLE:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
            log.debug(
                f"    HTTP {r.status_code} on {url[:90]} (attempt {attempt}/{tries})"
            )
        # jitter: several pages retry at once, and a synchronised retry wave is
        # exactly what a rate limiter wants to reject
        time.sleep(min(2 ** (attempt - 1), 8) + random.random())
    raise RuntimeError(f"giving up on {url} ({last})")


# --------------------------------------------------------------------------- #
# per-page probing
# --------------------------------------------------------------------------- #
_PAGE_IMG_RE = re.compile(
    r"<img\b[^>]*?orig=[\"']"
    + re.escape(LEGACY_ASSETS_HOST)
    + r"/[\w]+/images/(?P<name>[^\"'\\]+)[\"'][^>]*>",
    re.IGNORECASE,
)
_CLIP_RE = re.compile(
    r"clip:\s*rect\(\s*(-?\d+)px,?\s*(-?\d+)px,?\s*(-?\d+)px,?\s*(-?\d+)px\s*\)", re.I
)
_JSONP_CALLBACK_RE = re.compile(
    r"^\s*window\.page\d+_callback\(\[(.*)\]\)\s*;?\s*$", re.DOTALL
)
_NEWPAGE_SIZE_RE = re.compile(
    r'class=[\\"\']*newpage[^>]*?width:\s*(\d+)px;\s*height:\s*(\d+)px', re.IGNORECASE
)


def _unwrap_jsonp(text: str) -> str:
    m = _JSONP_CALLBACK_RE.search(text)
    if not m:
        return text
    try:
        return json.loads("[" + m.group(1) + "]")[0]
    except ValueError:
        # fall back to a crude unescape
        try:
            return json.loads('"' + m.group(1).replace('"', '\\"') + '"')
        except ValueError:
            return m.group(1)


def _image_candidates(html: str) -> list[str]:
    """Image names for a page, biggest visible area first.

    Scribd reuses one bitmap for decoration strips via CSS `clip: rect(...)`; the
    full-page scan (or the only image of an image-document) is the one whose clip
    rectangle covers the most area, so rank by that instead of document order.
    """
    best: dict[str, int] = {}
    for m in _PAGE_IMG_RE.finditer(html):
        tag = m.group(0)
        name = m.group("name")
        area = 0
        c = _CLIP_RE.search(tag)
        if c:
            top, right, bottom, left = (int(x) for x in c.groups())
            area = max(0, right - left) * max(0, bottom - top)
        else:
            area = 1
        if area > best.get(name, -1):
            best[name] = area
    return [n for n, _ in sorted(best.items(), key=lambda kv: kv[1], reverse=True)]


def probe_page(
    session: requests.Session, page: PageRef, log: Log, timeout: int = 60
) -> None:
    try:
        r = _request(session, page.jsonp_url, log, timeout=timeout)
    except RuntimeError as exc:
        log.debug(f"  page {page.number}: {exc}")
        return
    if r.status_code != 200:
        log.debug(f"  page {page.number}: jsonp HTTP {r.status_code}")
        return
    html = _unwrap_jsonp(r.text)
    page.html = html
    page.has_text = "text_layer" in html and html.count("<span") > 0
    page.image_names = _image_candidates(html)
    m = _NEWPAGE_SIZE_RE.search(html)
    if m:
        page.width = int(m.group(1))
        page.height = int(m.group(2))


def refresh_urls(session, doc: DocInfo, log: Log, timeout: int = 60) -> int:
    """Re-fetch the document page for fresh fragment URLs (content hashes rotate).

    Scribd serves /pages/<n>-<hash>.jsonp and the hash changes.  A 404 is not a
    missing page, it is a stale URL: re-read the document page and re-match the
    same page numbers.  Returns how many URLs actually changed.
    """
    try:
        fresh = fetch_document(session, doc.url, log, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a failure here must not abort the run
        log.debug(f"  re-resolve failed: {exc}")
        return 0
    by_number = {p.number: p.jsonp_url for p in fresh.pages}
    changed = 0
    for page in doc.pages:
        new_url = by_number.get(page.number)
        if new_url and new_url != page.jsonp_url:
            page.jsonp_url = new_url
            page.html = ""
            changed += 1
    if changed:
        log(f"[{doc.doc_id}] refreshed {changed} page URL(s) after a stale hash")
    return changed


def probe_pages(
    session,
    doc: DocInfo,
    log: Log,
    jobs: int = 6,
    delay: float = 0.0,
    only: list[PageRef] | None = None,
) -> int:
    """Fetch page fragments.  Returns how many of the probed pages were readable."""
    todo = [p for p in (only if only is not None else doc.pages) if not p.html]
    if not todo:
        return sum(1 for p in (only if only is not None else doc.pages) if p.ok)
    with futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        list(pool.map(lambda p: probe_page(session, p, log), todo))
        if delay:
            time.sleep(delay)
    got = sum(1 for p in todo if p.ok)
    if got < len(todo):
        # a rotated hash shows up as 404s; one re-resolve usually fixes all of them
        missing = [p for p in todo if not p.ok]
        log.debug(f"  {len(missing)} page(s) unreadable, re-resolving the manifest")
        if refresh_urls(session, doc, log):
            with futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
                list(pool.map(lambda p: probe_page(session, p, log), missing))
            got = sum(1 for p in todo if p.ok)
    log(f"[{doc.doc_id}] page markup: {got}/{len(todo)} pages readable")
    return got


# --------------------------------------------------------------------------- #
# image documents
# --------------------------------------------------------------------------- #
def _is_blank(data: bytes) -> bool:
    """True if the image is (near) empty — Scribd's placeholder looks like this."""
    try:
        from PIL import Image, ImageStat
    except ImportError:
        return len(data) < 3_000  # crude but effective for 1x1 / blank JPEGs
    try:
        import io

        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("L")
            if im.width * im.height < 40_000:
                return True
            lo, hi = im.getextrema()
            if hi - lo < 12:
                return True
            return float(ImageStat.Stat(im).stddev[0]) < 6.0
    except Exception:
        return True


def _image_url(doc: DocInfo, name: str, token: str | None = None) -> str:
    return append_token(f"{ASSETS_HOST}/{doc.prefix}/images/{name}", token)


def download_image(session, url, log: Log, referer: str, timeout: int = 90):
    try:
        r = _request(
            session,
            url,
            log,
            timeout=timeout,
            headers={"Referer": referer, "Accept": "image/*,*/*;q=0.8"},
        )
    except RuntimeError as exc:
        log.debug(f"    {exc}")
        return None
    if r.status_code != 200 or not r.content:
        log.debug(f"    image HTTP {r.status_code}: {url[-60:]}")
        return None
    if not _looks_like_image(r.content):
        log.debug(f"    not an image ({r.headers.get('Content-Type')}): {url[-60:]}")
        return None
    return r.content


def _looks_like_image(data: bytes) -> bool:
    return (
        data[:3] == b"\xff\xd8\xff"
        or data[:8] == b"\x89PNG\r\n\x1a\n"
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or data[:6] in (b"GIF87a", b"GIF89a")
        or data[:2] == b"BM"
    )


def build_image_document(
    session,
    doc: DocInfo,
    outdir: Path,
    log: Log,
    jobs: int = 6,
    keep_images: bool = False,
) -> Path | None:
    """Download page images and assemble a PDF; None if impossible."""
    usable = [p for p in doc.pages if p.image_names and p.ok]
    if not usable:
        return None

    referer = doc.url
    name = slugify(doc.title, doc.doc_id)
    img_dir = (
        (outdir / f"{name}-images") if keep_images else outdir / f".{doc.doc_id}-images"
    )
    img_dir.mkdir(parents=True, exist_ok=True)

    state = {"token": None, "tried": False}

    def job(page: PageRef):
        # try candidates in order: full-page scan first, decoration layers later
        for idx, img_name in enumerate(page.image_names):
            suffix = Path(img_name).suffix or ".jpg"
            dest = img_dir / f"{page.number:05d}-{idx}{suffix}"
            if dest.exists() and dest.stat().st_size > 0:
                return page.number, dest
            data = download_image(
                session, _image_url(doc, img_name, state["token"]), log, referer
            )
            if not data and not state["tried"]:
                # some documents gate the asset host behind a short-lived JWT
                state["tried"] = True
                state["token"] = try_page_token(session, doc, log)
                if state["token"]:
                    data = download_image(
                        session, _image_url(doc, img_name, state["token"]), log, referer
                    )
            if data and not _is_blank(data):
                dest.write_bytes(data)
                log.debug(
                    f"  page {page.number}: {img_name} ({len(data) / 1024:.0f} KB)"
                )
                return page.number, dest
        return page.number, None

    results: dict[int, Path] = {}
    try:
        with futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for number, path in pool.map(job, usable):
                if path:
                    results[number] = path
    except BaseException:
        if not keep_images:
            shutil.rmtree(img_dir, ignore_errors=True)
        raise

    missing = [p.number for p in doc.pages if p.number not in results]
    if not results:
        # distinguish "no bitmap for this page" from "Scribd handed us blanks"
        tried = sum(len(p.image_names) for p in usable)
        if tried:
            log.warn(
                f"Scribd served {tried} candidate image URL(s) but every one was "
                "blank or refused. The document page is blank for anonymous "
                "visitors too, so this one needs a session: export your cookies "
                "and rerun with --cookies cookies.txt"
            )
        else:
            log.warn(
                "the page fragments contain no usable bitmap - Scribd serves "
                "this document as text, try --mode vector"
            )
        if not keep_images:
            shutil.rmtree(img_dir, ignore_errors=True)
        return None
    if missing:
        log.warn(f"no usable image for page(s): {_compact_ranges(missing)}")

    paths = [results[n] for n in sorted(results)]
    out_pdf = outdir / f"{name}.pdf"
    pages_by_number = {p.number: p for p in doc.pages}
    _assemble_pdf(paths, out_pdf, log, pages_by_number, list(results))
    if not keep_images:
        shutil.rmtree(img_dir, ignore_errors=True)
    return out_pdf


def _implied_dpi(img_path: Path, page: PageRef | None) -> float | None:
    """Scan pixels / page CSS pixels * 96 = the DPI that reproduces the page size."""
    if not page or not page.width:
        return None
    try:
        from PIL import Image

        with Image.open(img_path) as im:
            w = im.width
    except Exception:
        return None
    dpi = 96.0 * w / float(page.width)
    return dpi if 60 <= dpi <= 600 else None


def _assemble_pdf(
    paths: list[Path],
    out_pdf: Path,
    log: Log,
    pages_by_number: dict | None = None,
    numbers: list[int] | None = None,
) -> None:
    dpi = None
    if pages_by_number and numbers:
        for p, n in zip(paths, numbers):
            dpi = _implied_dpi(p, pages_by_number.get(n))
            if dpi:
                break

    try:
        import img2pdf

        kwargs = {}
        if dpi:
            kwargs["layout_fun"] = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))
        tmp = out_pdf.with_suffix(".pdf.part")
        with open(tmp, "wb") as fh:
            fh.write(img2pdf.convert([str(p) for p in paths], **kwargs))
        tmp.replace(out_pdf)
        return
    except ImportError:
        log.debug("  img2pdf not installed — falling back to Pillow")
    except Exception as exc:  # mixed formats etc.
        log.debug(f"  img2pdf failed ({exc}) — falling back to Pillow")

    from PIL import Image

    frames = []
    for p in paths:
        with Image.open(p) as im:
            frames.append(im.convert("RGB") if im.mode != "RGB" else im.copy())
    first, rest = frames[0], frames[1:]
    first.save(
        out_pdf, "PDF", resolution=dpi or 150.0, save_all=True, append_images=rest
    )


def _compact_ranges(numbers: Iterable[int]) -> str:
    ns = sorted(set(numbers))
    if not ns:
        return ""
    out, start, prev = [], ns[0], ns[0]
    for n in ns[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    out.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(out)


# --------------------------------------------------------------------------- #
# text documents  ->  rebuilt HTML page  ->  Chromium print-to-PDF
# --------------------------------------------------------------------------- #
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ORIG_ATTR_RE = re.compile(r"""orig=(?P<q>["'])(?P<url>[^"']+)(?P=q)""", re.IGNORECASE)
_LL_RE = re.compile(r"""<a\b[^>]*class=["']?ll["']?[^>]*>""", re.IGNORECASE)
_ORIG_B64_RE = re.compile(r"""orig=(?P<q>["'])(?P<b64>[A-Za-z0-9+/=_\-]+)(?P=q)""")


def _patch_page_html(html: str, doc: DocInfo, token: str | None = None) -> str:
    """Reproduce what Scribd's JS does at runtime: give <img> tags their real `src`
    and decode the base64 link targets."""

    def img_sub(m: re.Match) -> str:
        tag = m.group(0)
        orig = _ORIG_ATTR_RE.search(tag)
        if not orig:
            return tag
        url = orig.group("url").replace(LEGACY_ASSETS_HOST + "/", ASSETS_HOST + "/")
        url = append_token(url, token)
        tag = re.sub(r"""\ssrc=(["']).*?\1""", "", tag, flags=re.IGNORECASE)
        tag = re.sub(r"""\sorig=(["']).*?\1""", "", tag, flags=re.IGNORECASE)
        if re.search(r"style=([\"'])", tag, re.IGNORECASE):
            tag = re.sub(
                r"""style=(["'])(.*?)\1""",
                lambda mm: f'style="{mm.group(2).rstrip(";")};display:block"',
                tag,
                count=1,
                flags=re.IGNORECASE,
            )
        elif tag.endswith(">"):
            tag = tag[:-1] + ' style="display:block">'
        if tag.endswith(">"):
            tag = tag[:-1] + f' src="{url}">'
        return tag

    html = _IMG_TAG_RE.sub(img_sub, html)

    def link_sub(m: re.Match) -> str:
        tag = m.group(0)
        b64 = _ORIG_B64_RE.search(tag)
        if not b64:
            return tag
        raw = b64.group("b64")
        try:
            href = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode(
                "utf-8", "replace"
            )
        except Exception:
            return tag
        if not href.lower().startswith(("http://", "https://")):
            return tag
        tag = re.sub(r"""\sorig=(["']).*?\1""", "", tag, flags=re.IGNORECASE)
        tag = tag[:-1].rstrip() if tag.endswith(">") else tag
        return f'{tag} href="{href}" target="_blank" rel="noreferrer">'

    return _LL_RE.sub(link_sub, html)


_FF_CLASS_RE = re.compile(r"\bff(\d{1,2})\b")


def fetch_font_css(session, doc: DocInfo, page_htmls: list[str], log: Log) -> str:
    """Build @font-face rules for the document's `ffN` fonts.

    Scribd's JS normally injects these at runtime (FontFace API), which is why the
    text fell back to Arial and overflowed the page.  The font files themselves are
    plain files on the asset host: ``<prefix>/fonts/000N.woff2``.
    """
    used = set()
    for html in page_htmls:
        used.update(int(m) for m in _FF_CLASS_RE.findall(html))
    if not used:
        return ""

    rules: list[str] = []

    def probe(n: int) -> str | None:
        for ext in ("woff2", "woff", "ttf"):
            url = f"{ASSETS_HOST}/{doc.prefix}/fonts/{n:04d}.{ext}"
            try:
                r = session.get(url, timeout=30, headers={"Referer": doc.url})
            except requests.RequestException:
                continue
            if r.ok and len(r.content) > 100:
                return url
        return None

    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        for n, url in zip(sorted(used), pool.map(probe, sorted(used))):
            if url:
                rules.append(
                    f'@font-face{{font-family:ff{n};src:url("{url}") format("woff2");'
                    "font-weight:normal;font-style:normal;font-display:block}"
                )
    have = len(rules)
    log.debug(f"  embedded {have}/{len(used)} document fonts")
    return "\n".join(rules)


_PRINT_CSS = """
@page {{ size: {W}px {H}px; margin: 0; }}
html, body {{ margin: 0; padding: 0; background: #fff; }}
.pg {{
  position: relative; width: {W}px; height: {H}px; overflow: hidden;
  break-after: page; page-break-after: always; background: #fff;
}}
.pg:last-of-type {{ break-after: auto; page-break-after: auto; }}
.pg > .newpage {{
  transform: none !important;
  position: absolute !important; left: 0 !important; top: 0 !important;
  width: {W}px !important; height: {H}px !important;
  contain: strict !important;
}}
.pg .highlighter_canvas, .pg .ie_fix > br {{ display: none !important; }}
"""


def _load_html(page, html: str, wait: str = "load", work: Path | None = None):
    """set_content() never finishes under the CloakBrowser binary (no load
    event); a file:// navigation of the same bytes does, instantly."""
    try:
        import chromium_launch

        cloak = chromium_launch.browser_choice() == "cloak"
    except Exception:  # noqa: BLE001
        cloak = False
    if cloak:
        import tempfile

        target = Path(work or tempfile.gettempdir()) / "_print.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        page.goto(target.as_uri(), wait_until=wait)
    else:
        page.set_content(html, wait_until=wait)


def _standalone_html(
    page_htmls: list[str], css: str, links: list[str], size: tuple[int, int]
) -> str:
    w, h = size
    body = "".join(f'<div class="pg">{html}</div>' for html in page_htmls)
    css_links = "".join(f'<link rel="stylesheet" href="{href}">' for href in links)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"{css_links}<style>{css}</style>"
        f"<style>{_PRINT_CSS.format(W=w, H=h)}</style>"
        f"</head><body>{body}</body></html>"
    )


class BrowserUnavailable(RuntimeError):
    pass


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "text documents need headless Chromium:\n"
            "    pip install playwright img2pdf\n"
            "    python -m playwright install chromium\n"
            "    python chromium_setup.py            (no root needed)\n"
            "or rerun with --mode image to try the image scraper instead"
        ) from exc
    return sync_playwright


def harvest_css(page) -> tuple[str, list[str]]:
    css = page.evaluate(
        """() => [...document.querySelectorAll('style')]"""
        """.map(s => s.textContent).join('\\n')"""
    )
    links = page.evaluate(
        """() => [...document.querySelectorAll('link[rel=stylesheet]')]"""
        """.map(l => l.href)"""
    )
    return css or "", [h for h in links if h]


def try_page_token(session, doc: DocInfo, log: Log) -> str | None:
    """Fetch the short-lived asset JWT if this document requires one."""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": doc.url,
        "Origin": SCRIBD,
    }
    try:
        r = session.post(
            f"{SCRIBD}/document/{doc.doc_id}/token",
            json={},
            headers=headers,
            timeout=30,
        )
        if r.ok:
            payload = r.json()
            tok = payload.get("token")
            if tok:
                log.debug("  asset token acquired")
            return tok
    except (requests.RequestException, ValueError) as exc:
        log.debug(f"  token endpoint: {exc}")
    return None


def browser_render(
    session,
    doc: DocInfo,
    outdir: Path,
    log: Log,
    *,
    cookies: list[dict] | None = None,
    timeout_ms: int = 90_000,
    scale: int = 1,
    debug_dir: Path | None = None,
    chunk: int = 250,
) -> tuple[Path, int]:
    sync_playwright = _require_playwright()

    readable = [p for p in doc.pages if p.ok]
    if not readable:
        raise RuntimeError(
            f"[{doc.doc_id}] no readable pages (all page fragments failed)"
        )

    token = try_page_token(session, doc, log)
    patched = [_patch_page_html(p.html, doc, token) for p in readable]

    sizes = {(p.width or 0, p.height or 0) for p in readable}
    sizes.discard((0, 0))
    if len(sizes) == 1:
        size = sizes.pop()
    else:
        # mixed page sizes: fall back to the most common one, pages are scaled in CSS
        counts: dict[tuple[int, int], int] = {}
        for p in readable:
            if p.width and p.height:
                counts[(p.width, p.height)] = counts.get((p.width, p.height), 0) + 1
        size = max(counts, key=counts.get) if counts else (902, 1167)
    log(f"[{doc.doc_id}] page size {size[0]}x{size[1]} px, {len(readable)} pages")

    out_pdf = outdir / f"{slugify(doc.title, doc.doc_id)}.pdf"
    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = chromium_launch.launch(
            pw,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--font-render-hinting=none",
            ],
        )
        try:
            ctx = browser.new_context(
                user_agent=session.headers.get("User-Agent", DEFAULT_UA),
                viewport={"width": 1280, "height": 1400},
                device_scale_factor=scale,
                locale="en-US",
            )
            # Scribd's own page-manager JS is not just unnecessary here, it makes
            # very long documents unusable (it builds one object per page and
            # blocks the main thread).  We only want the CSS, so drop scripts.
            ctx.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in ("script", "media")
                    else route.continue_()
                ),
            )

            if cookies:
                try:
                    ctx.add_cookies(
                        [
                            {
                                "name": c["name"],
                                "value": c["value"],
                                "domain": c["domain"],
                                "path": c.get("path", "/"),
                                "expires": c.get("expires", -1),
                                "httpOnly": c.get("httpOnly", False),
                                "secure": c.get("secure", False),
                                "sameSite": c.get("sameSite", "Lax"),
                            }
                            for c in cookies
                        ]
                    )
                except Exception as exc:
                    log.debug(f"  could not inject cookies: {exc}")

            # one real page load: this gives us Scribd's own CSS + the per-document
            # font rules that its JS builds at runtime
            page = ctx.new_page()
            page.set_default_timeout(timeout_ms)
            log(f"[{doc.doc_id}] loading page for styles/fonts ...")
            try:
                page.goto(doc.url, wait_until="load", timeout=timeout_ms)
            except Exception as exc:
                log.warn(
                    f"document page did not fully load ({type(exc).__name__}); "
                    "continuing with whatever CSS arrived"
                )
            css, links = harvest_css(page)
            log.debug(
                f"  harvested {len(css) / 1024:.0f} KB of CSS, {len(links)} stylesheets"
            )
            page.close()

            # the document's own fonts, rebuilt from the asset host
            font_css = fetch_font_css(session, doc, patched, log)
            if font_css:
                css = font_css + "\n" + css

            if debug_dir:
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / f"{doc.doc_id}.html").write_text(
                    _standalone_html(patched, css, links, size), encoding="utf-8"
                )

            # very long documents are printed in chunks and merged; a single
            # print job that big can exhaust Chromium's memory
            groups = [patched[i : i + chunk] for i in range(0, len(patched), chunk)]
            groups = groups or [[]]
            parts: list[Path] = []
            printer = ctx.new_page()
            printer.set_default_timeout(timeout_ms)
            for idx, group in enumerate(groups, start=1):
                if len(groups) > 1:
                    first = (idx - 1) * chunk + 1
                    log(
                        f"[{doc.doc_id}] printing pages {first}-"
                        f"{first + len(group) - 1} of {len(patched)} ..."
                    )
                _load_html(printer, _standalone_html(group, css, links, size), "load")
                try:
                    printer.wait_for_function(
                        "document.fonts.status === 'loaded'", timeout=30_000
                    )
                except Exception:
                    log.debug("  fonts did not all report loaded; printing anyway")
                printer.wait_for_timeout(1200)
                part = out_pdf.with_name(f"{out_pdf.stem}.part{idx:03d}.pdf")
                printer.pdf(
                    path=str(part),
                    width=f"{size[0]}px",
                    height=f"{size[1]}px",
                    print_background=True,
                    margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                    prefer_css_page_size=True,
                )
                parts.append(part)
            printer.close()

            if len(parts) == 1:
                parts[0].replace(out_pdf)
            else:
                _merge_pdfs(parts, out_pdf, log)
                for part in parts:
                    part.unlink(missing_ok=True)
        except Exception:
            for stale in out_pdf.parent.glob(f"{out_pdf.stem}.part*.pdf"):
                stale.unlink(missing_ok=True)
            raise
        finally:
            browser.close()

    return out_pdf, len(readable)


def _merge_pdfs(parts: list[Path], out_pdf: Path, log: Log) -> None:
    try:
        import pypdf
    except ImportError as exc:
        raise RuntimeError(
            "merging a large document needs pypdf: pip install pypdf"
        ) from exc
    writer = pypdf.PdfWriter()
    for part in parts:
        writer.append(str(part))
    with open(out_pdf, "wb") as fh:
        writer.write(fh)


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #
def verify_pdf(path: Path, expected_pages: int, log: Log) -> None:
    try:
        import pypdf
    except ImportError:
        log.debug("  pypdf not installed — skipping PDF verification")
        return
    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:
        log.warn(f"could not re-open the produced PDF: {exc}")
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
    log(f"  -> {path.name}: {len(reader.pages)} pages, {size_kb:,.0f} KB")
    if expected_pages and len(reader.pages) != expected_pages:
        log.warn(f"page count {len(reader.pages)} != {expected_pages} expected pages")
    if blanks:
        log.warn(
            f"{len(blanks)} blank page(s): {_compact_ranges(blanks)} "
            "(usually pages Scribd hides behind its preview limit)"
        )


def parse_page_range(spec: str, available: list[int]) -> list[int]:
    """'1-3,7,12-' -> [1,2,3,7,12,...]; only numbers that exist are returned."""
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


# --------------------------------------------------------------------------- #
# per-document pipeline
# --------------------------------------------------------------------------- #
def convert(
    session,
    target: str,
    outdir: Path,
    log: Log,
    *,
    mode: str = "auto",
    jobs: int = 6,
    keep_images: bool = False,
    cookies: list[dict] | None = None,
    debug_dir: Path | None = None,
    timeout_ms: int = 90_000,
    pages: str | None = None,
) -> Path:
    doc = fetch_document(session, target, log)
    log(f"[{doc.doc_id}] {doc.title}")
    log(
        f"[{doc.doc_id}] {len(doc.pages)} pages, displayType={doc.display_type or '?'}"
        f"{', logged in' if doc.logged_in else ''}"
    )
    if doc.truncated:
        log.warn(
            f"only {doc.allowed_pages} of {doc.total_pages} pages are public "
            "without a Scribd subscription — the rest will be blank. "
            "Log in, export cookies and use --cookies cookies.txt"
        )

    if pages:
        keep = set(parse_page_range(pages, [p.number for p in doc.pages]))
        doc.pages = [p for p in doc.pages if p.number in keep]
        log(
            f"[{doc.doc_id}] restricted to {len(doc.pages)} page(s): "
            f"{_compact_ranges(sorted(keep))}"
        )

    # classify from a sample first: a 6000-page document should not cost 6000
    # requests before we even know which pipeline to use
    sample = doc.pages[: min(len(doc.pages), 8)]
    probe_pages(session, doc, log, jobs=jobs, only=sample)
    sampled_text = sum(1 for p in sample if p.has_text)

    chosen = mode
    if mode == "auto":
        chosen = "vector" if sampled_text else "image"
    log(
        f"[{doc.doc_id}] pipeline: "
        f"{'text (vector PDF)' if chosen == 'vector' else 'scan (image PDF)'}"
    )

    if chosen == "vector":
        probe_pages(session, doc, log, jobs=jobs)

    text_pages = [p for p in doc.pages if p.has_text]
    image_pages = [p for p in doc.pages if p.ok and not p.has_text and p.image_names]
    log(
        f"[{doc.doc_id}] text pages: {len(text_pages)}, image pages: {len(image_pages)}"
    )

    if chosen == "vector":
        try:
            pdf, rendered = browser_render(
                session,
                doc,
                outdir,
                log,
                cookies=cookies,
                timeout_ms=timeout_ms,
                debug_dir=debug_dir,
            )
        except BrowserUnavailable as exc:
            log.warn(str(exc))
            chosen = "image"
        else:
            verify_pdf(pdf, rendered, log)
            return pdf

    # image mode (also the fallback when the browser is unavailable / not needed)
    pdf = build_image_document(
        session, doc, outdir, log, jobs=jobs, keep_images=keep_images
    )
    if pdf:
        verify_pdf(pdf, len(doc.pages), log)
        return pdf

    if chosen == "image" and text_pages:
        log.warn(
            "this is a text document — rerun without --mode image and install "
            "playwright + chromium to get a real PDF"
        )
    raise RuntimeError(f"[{doc.doc_id}] could not produce a PDF (no usable pages)")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="scribd_pdf.py",
        description="Download Scribd documents as PDFs (with real text).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage\n-----")[-1],
    )
    ap.add_argument(
        "targets",
        nargs="+",
        metavar="URL_OR_ID",
        help="Scribd document URL or numeric document id",
    )
    ap.add_argument("-o", "--outdir", default=".", help="output directory (default: .)")
    ap.add_argument(
        "-c",
        "--cookies",
        default=None,
        help="cookies.txt (Netscape) or JSON cookie export, for private documents",
    )
    ap.add_argument(
        "--mode",
        choices=("auto", "vector", "image"),
        default="auto",
        help="auto (default) picks vector for text documents, image for scans",
    )
    ap.add_argument(
        "--jobs", type=int, default=6, help="parallel page fetches (default: 6)"
    )
    ap.add_argument(
        "--pages",
        default=None,
        metavar="RANGE",
        help="only these pages, e.g. 1-20 or 1,4,9-12 (default: all)",
    )
    ap.add_argument(
        "--keep-images",
        action="store_true",
        help="keep downloaded page images next to the PDF",
    )
    ap.add_argument("--ua", default=None, help="override the User-Agent")
    ap.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="per-request / browser timeout in seconds (default: 90)",
    )
    ap.add_argument(
        "--debug-dir",
        default=None,
        help="dump the rebuilt HTML here (useful for bug reports)",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    args = ap.parse_args(argv)

    log = Log(args.verbose)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    cookie_list = None
    if args.cookies:
        try:
            cookie_list = load_cookie_list(args.cookies)
            log(f"loaded {len(cookie_list)} cookies from {args.cookies}")
        except CookieFileError as exc:
            log.warn(str(exc))
            return 2

    session = build_session(args.cookies, user_agent=args.ua)
    debug_dir = Path(args.debug_dir) if args.debug_dir else None

    failures = 0
    for target in args.targets:
        started = time.time()
        try:
            pdf = convert(
                session,
                target,
                outdir,
                log,
                mode=args.mode,
                jobs=args.jobs,
                keep_images=args.keep_images,
                cookies=cookie_list,
                debug_dir=debug_dir,
                timeout_ms=args.timeout * 1000,
                pages=args.pages,
            )
        except Exception as exc:
            failures += 1
            print(f"!! {target}: {exc}", file=sys.stderr, flush=True)
            if args.verbose:
                import traceback

                traceback.print_exc()
        else:
            log(f"  done in {time.time() - started:.1f}s -> {pdf}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
