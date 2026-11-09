"""extract.py — recover a document's page manifest from its Scribd page.

The page list lives inline in the document HTML inside
``docManager.addPage({pageNum, origWidth, origHeight, containerElem, contentUrl})``
calls. We pull the ``.../pages/<n>-<hash>.jsonp`` values with a regex and derive
the matching ``.../images/<n>-<hash>.jpg`` paths from them.

Both URLs are returned BARE (no ``?token=``) — the token is appended at request
time by ``download.py`` so a mid-run refresh is trivial.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

ASSET_HOST = "html.scribdassets.com"

DOC_ID_RE = re.compile(r"/(?:document|embeds|doc)/(\d+)")
BARE_ID_RE = re.compile(r"^\d{6,}$")

_ASSET_RE = re.compile(
    r"(?:https?:)?//" + re.escape(ASSET_HOST) +
    r"/(?P<docid>\d+)/pages/(?P<num>\d+)-(?P<hash>[0-9a-z]+)\.jsonp",
    re.IGNORECASE,
)
_REL_RE = re.compile(
    r"/(?P<docid>\d+)/pages/(?P<num>\d+)-(?P<hash>[0-9a-z]+)\.jsonp",
    re.IGNORECASE,
)
_ADD_PAGE_RE = re.compile(r"addPage\(\{(?P<body>.*?)\}\)", re.DOTALL)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)


@dataclass
class Page:
    number: int
    page_url: str      # .../pages/<n>-<hash>.jsonp  (bare)
    image_url: str     # .../images/<n>-<hash>.jpg   (bare)
    width: int = 0
    height: int = 0


def document_id(url_or_id: str) -> str:
    s = url_or_id.strip()
    if BARE_ID_RE.match(s):
        return s
    m = DOC_ID_RE.search(s)
    if not m:
        raise ValueError(f"No Scribd document id found in: {url_or_id!r}")
    return m.group(1)


def canonical_url(url_or_id: str) -> str:
    s = url_or_id.strip()
    if BARE_ID_RE.match(s):
        return f"https://www.scribd.com/document/{s}"
    m = DOC_ID_RE.search(s)
    if m and "/embeds/" in s:
        return f"https://www.scribd.com/document/{m.group(1)}"
    return s


def document_title(html: str, fallback: str) -> str:
    m = _TITLE_RE.search(html)
    if not m:
        return fallback
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    title = re.sub(r"\s*\|\s*Scribd\s*$", "", title, flags=re.IGNORECASE)
    return title or fallback


def fetch_html(session, url: str) -> str:
    r = session.get(url, headers={"Accept": "text/html,*/*"})
    r.raise_for_status()
    return r.text


def _asset_to_image(page_url: str) -> str:
    return page_url.replace("/pages/", "/images/").replace(".jsonp", ".jpg")


def _dimensions(html: str) -> dict:
    dims = {}
    for m in _ADD_PAGE_RE.finditer(html):
        body = m.group("body")
        num = re.search(r"pageNum\s*:\s*(\d+)", body)
        w = re.search(r"origWidth\s*:\s*(\d+)", body)
        h = re.search(r"origHeight\s*:\s*(\d+)", body)
        if num and w and h:
            dims[int(num.group(1))] = (int(w.group(1)), int(h.group(1)))
    return dims


def extract_pages(html: str) -> List[Page]:
    found: dict[int, str] = {}

    for m in _ASSET_RE.finditer(html):
        num = int(m.group("num"))
        found.setdefault(
            num,
            f"https://{ASSET_HOST}/{m.group('docid')}/pages/"
            f"{num}-{m.group('hash')}.jsonp",
        )

    if not found:
        for m in _REL_RE.finditer(html):
            num = int(m.group("num"))
            found.setdefault(
                num,
                f"https://{ASSET_HOST}/{m.group('docid')}/pages/"
                f"{num}-{m.group('hash')}.jsonp",
            )

    if not found:
        raise RuntimeError(
            "No page contentUrls found in the HTML. The document may be "
            "text-only, or Scribd changed its bootstrap markup."
        )

    dims = _dimensions(html)
    pages: List[Page] = []
    for num in sorted(found):
        w, h = dims.get(num, (0, 0))
        pages.append(Page(
            number=num,
            page_url=found[num],
            image_url=_asset_to_image(found[num]),
            width=w,
            height=h,
        ))
    return pages