"""Offline sanity checks for scribd_pdf.py (no network)."""

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image  # noqa: E402

import scribd_pdf as sp  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(
        ("PASS  " if cond else "FAIL  ")
        + name
        + (f"   {extra}" if extra and not cond else "")
    )
    if not cond:
        fails.append(name)


# --- document id parsing ------------------------------------------------------
cases = {
    "https://www.scribd.com/document/949773556/Sample-PDF": "949773556",
    "https://www.scribd.com/document/400968959": "400968959",
    "https://www.scribd.com/embeds/400968959/content": "400968959",
    "https://www.scribd.com/presentation/1234567/slides": "1234567",
    "879249648": "879249648",
}
for src, want in cases.items():
    got = sp.document_id(src)
    check(f"document_id({src[:45]})", got == want, f"got {got}")

check(
    "document_url keeps slug",
    sp.document_url("https://www.scribd.com/document/949773556/Sample-PDF")
    == "https://www.scribd.com/document/949773556/Sample-PDF",
)
check(
    "document_url from id",
    sp.document_url("949773556") == "https://www.scribd.com/document/949773556",
)

# --- page ranges --------------------------------------------------------------
avail = list(range(1, 11))
check("range 1-3", sp.parse_page_range("1-3", avail) == [1, 2, 3])
check("range 1,4,9-10", sp.parse_page_range("1,4,9-10", avail) == [1, 4, 9, 10])
check("range open-ended 8-", sp.parse_page_range("8-", avail) == [8, 9, 10])
check("range clamps", sp.parse_page_range("1-99", avail) == avail)
try:
    sp.parse_page_range("50-60", avail)
    ok = False
except ValueError:
    ok = True
check("range with no match raises", ok)

# --- compact ranges -----------------------------------------------------------
check(
    "compact",
    sp._compact_ranges([1, 2, 3, 7, 9, 10]) == "1-3, 7, 9-10",
    sp._compact_ranges([1, 2, 3, 7, 9, 10]),
)

# --- jsonp unwrapping ---------------------------------------------------------
raw = 'window.page12_callback(["<div class=\\"newpage\\" id=\\"page12\\">hi</div>"]);'
check(
    "jsonp unwrap",
    sp._unwrap_jsonp(raw) == '<div class="newpage" id="page12">hi</div>',
    sp._unwrap_jsonp(raw),
)

# --- manifest parsing (real-ish snippet) --------------------------------------
html = """
<script>
  var docManager = new DocumentManager("scroll","web"=="mobile",{hasWoffFonts:false});
  docManager.assetPrefix = "9ix78ih280exmmb8";
  docManager.displayType = "vector";
  docManager.setupPaidDocument(allowedPagesTemp, 2);
  var allowedPagesTemp = [1,2];
  docManager.addPage({pageNum:1, fonts:[], origWidth:902, origHeight:1168,
    containerElem: document.getElementById("outer_page_1"), blur:false,
    contentUrl: "https://html.scribdassets.com/9ix78ih280exmmb8/pages/1-e90455d1f5.jsonp"});
  docManager.addPage({pageNum:2, fonts:[], origWidth:902, origHeight:1168,
    containerElem: document.getElementById("outer_page_2"), blur:false,
    contentUrl: "https://html.scribdassets.com/9ix78ih280exmmb8/pages/2-70b69a8f07.jsonp"});
</script>
<title>My Doc | Scribd</title>
"""
doc = sp.parse_document(html, "879249648", "https://www.scribd.com/document/879249648")
check(
    "manifest pages",
    [p.number for p in doc.pages] == [1, 2],
    [p.number for p in doc.pages],
)
check("manifest dims", (doc.pages[0].width, doc.pages[0].height) == (902, 1168))
check("manifest prefix", doc.prefix == "9ix78ih280exmmb8", doc.prefix)
check("manifest title", doc.title == "My Doc", doc.title)
check(
    "manifest not truncated when all pages allowed",
    doc.truncated is False,
    f"{doc.allowed_pages}/{doc.total_pages}",
)
trunc = html.replace(
    "setupPaidDocument(allowedPagesTemp, 2)", "setupPaidDocument(allowedPagesTemp, 336)"
)
doc2 = sp.parse_document(trunc, "1", "https://www.scribd.com/document/1")
check(
    "manifest truncated flag",
    doc2.truncated is True,
    f"{doc2.allowed_pages}/{doc2.total_pages}",
)

# --- image candidate ranking (clip area, not document order) -----------------
page_html = (
    '<img class="absimg" style="left:40px;top:40px;width:821px;height:1064px;'
    'clip:rect(1px, 820px, 112px, 1px)" orig="http://html.scribd.com/pfx/images/1-big.png"/>'
    '<img class="absimg" style="left:1px;top:1px" orig="http://html.scribd.com/pfx/images/1-tiny.png"/>'
)
cands = sp._image_candidates(page_html)
check(
    "image candidates ranked by clip area", cands == ["1-big.png", "1-tiny.png"], cands
)

# --- img/link patching --------------------------------------------------------
patch = sp._patch_page_html(
    '<img class="absimg" style="left:40px" orig="http://html.scribd.com/pfx/images/1-a.png"/>'
    '<a class="ll" orig="aHR0cHM6Ly9leGFtcGxlLmNvbS94">link</a>',
    doc,
    "TOK",
)
check(
    "img src rewritten",
    'src="https://html.scribdassets.com/pfx/images/1-a.png?token=TOK"' in patch,
    patch,
)
check("img orig dropped", "orig=" not in patch, patch)
check("base64 link decoded", 'href="https://example.com/x"' in patch, patch)

# --- cookies ------------------------------------------------------------------

ns = (
    "# Netscape HTTP Cookie File\n"
    "#HttpOnly_.scribd.com\tTRUE\t/\tTRUE\t1999999999\tsess\tabc123\n"
    ".scribd.com\tTRUE\t/\tFALSE\t0\tlang\ten\n"
    "commented\tout\tline\tjunk\n"
)
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
    fh.write(ns)
    nspath = fh.name
ck = sp.load_cookie_list(nspath)
check("netscape cookies parsed", len(ck) == 2, [c["name"] for c in ck])
check("httponly flag kept", any(c["name"] == "sess" and c["httpOnly"] for c in ck))
check(
    "domain normalised",
    all(c["domain"] == "scribd.com" for c in ck),
    [c["domain"] for c in ck],
)

js = json.dumps(
    [
        {
            "name": "sess",
            "value": "v",
            "domain": ".scribd.com",
            "path": "/",
            "expirationDate": 1999999999,
        }
    ]
)
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
    fh.write(js)
    jspath = fh.name
ck2 = sp.load_cookie_list(jspath)
check("json cookies parsed", len(ck2) == 1 and ck2[0]["name"] == "sess", ck2)

# --- blank image detection ----------------------------------------------------
buf = io.BytesIO()
Image.new("RGB", (904, 1169), "white").save(buf, "JPEG")
check("blank placeholder detected", sp._is_blank(buf.getvalue()) is True)
buf2 = io.BytesIO()
im = Image.new("RGB", (904, 1169), "white")
im.paste((0, 0, 0), (0, 0, 400, 900))
im.save(buf2, "JPEG")
check("real scan not flagged blank", sp._is_blank(buf2.getvalue()) is False)

# --- standalone html ----------------------------------------------------------
h = sp._standalone_html(['<div class="newpage">x</div>'], "body{}", [], (902, 1167))
check(
    "html has page box", "@page { size: 902px 1167px" in h.replace("  ", " "), h[:120]
)
check("html has one .pg wrapper", h.count('class="pg"') == 1)


# --- rotated content hash: 404 means "re-resolve", not "missing" -------------
class _Doc:
    def __init__(self):
        self.doc_id = "1"
        self.url = "https://www.scribd.com/document/1"
        self.pages = [
            sp.PageRef(
                number=1,
                jsonp_url="https://html.scribdassets.com/aaa/pages/1-old.jsonp",
            ),
            sp.PageRef(
                number=2,
                jsonp_url="https://html.scribdassets.com/aaa/pages/2-old.jsonp",
            ),
        ]


doc = _Doc()
fresh = _Doc()
fresh.pages = [
    sp.PageRef(
        number=1, jsonp_url="https://html.scribdassets.com/bbb/pages/1-new.jsonp"
    ),
    sp.PageRef(
        number=2, jsonp_url="https://html.scribdassets.com/bbb/pages/2-new.jsonp"
    ),
]
_orig_fetch = sp.fetch_document
sp.fetch_document = lambda *a, **k: fresh
try:
    changed = sp.refresh_urls(None, doc, sp.Log(False))
finally:
    sp.fetch_document = _orig_fetch
check("both stale URLs replaced", changed == 2, changed)
check(
    "new hash in use",
    doc.pages[0].jsonp_url.endswith("1-new.jsonp"),
    doc.pages[0].jsonp_url,
)
check("page number preserved", [p.number for p in doc.pages] == [1, 2])
check("cached html cleared after a refresh", doc.pages[0].html == "")

# unchanged URLs report zero
doc2 = _Doc()
sp.fetch_document = lambda *a, **k: doc2
try:
    check(
        "identical URLs report no change",
        sp.refresh_urls(None, doc2, sp.Log(False)) == 0,
    )
finally:
    sp.fetch_document = _orig_fetch


# a failed re-resolve must not raise
def _boom(*a, **k):
    raise RuntimeError("network down")


sp.fetch_document = _boom
try:
    check(
        "re-resolve failure is swallowed",
        sp.refresh_urls(None, _Doc(), sp.Log(False)) == 0,
    )
finally:
    sp.fetch_document = _orig_fetch

# --- retry backoff has jitter (a synchronised retry wave gets rate limited) --

import time as _time  # noqa: E402

_sleeps = []
_orig_sleep = _time.sleep
_time.sleep = lambda s: _sleeps.append(s)


class _Resp:
    status_code = 429
    text = ""


class _Sess:
    def get(self, *a, **k):
        return _Resp()


try:
    try:
        sp._request(_Sess(), "https://x/", sp.Log(False), tries=3)
    except RuntimeError:
        pass
finally:
    _time.sleep = _orig_sleep
check("retries back off", len(_sleeps) == 3, _sleeps)
check("backoff is capped", all(s <= 9 for s in _sleeps), _sleeps)
check(
    "backoff is jittered (not all integers)",
    any(round(s) != s for s in _sleeps),
    _sleeps,
)

print("-- cloak load workaround --")
import scribd_pdf as _sp  # noqa: E402


class _P:
    def __init__(self):
        self.calls = []

    def set_content(self, html, wait_until="load"):
        self.calls.append(("set_content", wait_until))

    def goto(self, url, wait_until="load"):
        self.calls.append(("goto", wait_until))


_choice = _sp.chromium_launch.browser_choice
_sp.chromium_launch.browser_choice = lambda: "cloak"
_p = _P()
_sp._load_html(_p, "<h1>x</h1>", "load", tempfile.mkdtemp())
check("cloak: print engine uses file://", _p.calls[0][0] == "goto", _p.calls)
_sp.chromium_launch.browser_choice = lambda: "chrome"
_p = _P()
_sp._load_html(_p, "<h1>x</h1>")
check(
    "chrome: print engine keeps set_content", _p.calls[0][0] == "set_content", _p.calls
)
_sp.chromium_launch.browser_choice = _choice

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
