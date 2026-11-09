"""Offline sanity checks for scribd_shot.py (no network, no browser)."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scribd_shot as ss  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(
        ("PASS  " if cond else "FAIL  ")
        + name
        + (f"   {extra}" if extra and not cond else "")
    )
    if not cond:
        fails.append(name)


# --- document ids -------------------------------------------------------------
cases = {
    "https://www.scribd.com/document/949773556/Sample-PDF": "949773556",
    "https://www.scribd.com/document/400968959": "400968959",
    "https://www.scribd.com/embeds/400968959/content": "400968959",
    "https://www.scribd.com/presentation/1234567/slides": "1234567",
    "879249648": "879249648",
}
for src, want in cases.items():
    got = ss.document_id(src)
    check(f"document_id({src[:42]})", got == want, f"got {got}")

check(
    "document_url keeps slug",
    ss.document_url("https://www.scribd.com/document/949773556/Sample-PDF")
    == "https://www.scribd.com/document/949773556/Sample-PDF",
)
check(
    "document_url from id",
    ss.document_url("949773556") == "https://www.scribd.com/document/949773556",
)

# --- page ranges --------------------------------------------------------------
avail = list(range(1, 11))
check("range 1-3", ss.parse_page_range("1-3", avail) == [1, 2, 3])
check("range 1,4,9-10", ss.parse_page_range("1,4,9-10", avail) == [1, 4, 9, 10])
check("range open-ended", ss.parse_page_range("8-", avail) == [8, 9, 10])
check("range clamped", ss.parse_page_range("1-99", avail) == avail)
try:
    ss.parse_page_range("50-60", avail)
    ok = False
except ValueError:
    ok = True
check("range with no match raises", ok)

# --- compact ranges -----------------------------------------------------------
check(
    "compact ranges",
    ss.compact_ranges([1, 2, 3, 7, 9, 10]) == "1-3, 7, 9-10",
    ss.compact_ranges([1, 2, 3, 7, 9, 10]),
)

# --- slug / filenames ---------------------------------------------------------
check(
    "slugify basic",
    ss.slugify("20 MB Sample PDF / Download", "x") == "20-MB-Sample-PDF-Download",
)
check("slugify fallback", ss.slugify("///", "scribd-1") == "scribd-1")

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
ck = ss.load_cookies(nspath)
check("netscape cookies parsed", len(ck) == 2, [c["name"] for c in ck])
check("httpOnly kept", any(c["name"] == "sess" and c["httpOnly"] for c in ck))
check("domain normalised", all(c["domain"] == "scribd.com" for c in ck))
check(
    "session cookie expiry", any(c["name"] == "lang" and c["expires"] == -1 for c in ck)
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
ck2 = ss.load_cookies(jspath)
check("json cookies parsed", len(ck2) == 1 and ck2[0]["name"] == "sess", ck2)
check(
    "json cookie is playwright-shaped",
    set(ck2[0]) >= {"name", "value", "domain", "path", "expires", "httpOnly", "secure"},
    sorted(ck2[0]),
)

try:
    ss.load_cookies("/nonexistent/cookies.txt")
    ok = False
except ss.CookieFileError:
    ok = True
check("missing cookie file raises", ok)

# --- static page document -----------------------------------------------------
size = (902, 1167)
css = ss._PRINT_CSS_FORMS(size)
check("page box size in css", "width: 902px; height: 1167px" in css, css[:80])
check("transform reset in css", "transform: none !important" in css)

html = ss._static_html(
    ['<div class="newpage">a</div>', '<div class="newpage">b</div>'],
    "body{}",
    [],
    size,
    css,
)
check("one wrapper per page", html.count('class="pg"') == 2, html.count('class="pg"'))
check("no page breaks via print css", "@page" not in html)
check("css embedded inline", "<style>body{}</style>" in html)

# --- overlay selector list ----------------------------------------------------
check("overlay selectors defined", len(ss.OVERLAY_SELECTORS) >= 5)
check(
    "consent banner selectors included",
    any("osano" in s for s in ss.OVERLAY_SELECTORS)
    and any("onetrust" in s for s in ss.OVERLAY_SELECTORS),
)

# --- shot dataclass -----------------------------------------------------------
from pathlib import Path  # noqa: E402

shot = ss.Shot(
    number=3,
    path=Path("/tmp/x.jpg"),
    width=1804,
    height=2334,
    css_width=902,
    css_height=1167,
)
check("shot carries geometry", (shot.width, shot.css_width) == (1804, 902))

# --- dpi maths used for the PDF page size ------------------------------------
dpi = 96.0 * shot.width / shot.css_width
check("scale 2 -> 192 dpi", abs(dpi - 192.0) < 0.01, dpi)
check("192 dpi keeps A4-ish width", abs(shot.css_width - 902) < 1)

# --- page cache: keys are content-addressed and stable ------------------------
import tempfile  # noqa: E402

cache_root = Path(tempfile.mkdtemp())
cache = ss.PageCache(cache_root, ss.Log(False))

k1 = cache.page_key("949773556", 3, "abc123", 2, "jpeg", 92)
k2 = cache.page_key("949773556", 3, "abc123", 2, "jpeg", 92)
k3 = cache.page_key("949773556", 3, "DIFFERENT", 2, "jpeg", 92)
k4 = cache.page_key("949773556", 3, "abc123", 3, "jpeg", 92)
check("cache key is deterministic", k1 == k2)
check("cache key changes with content hash", k1 != k3)
check("cache key changes with scale", k1 != k4)
check("cache key is short", len(k1) == 24, len(k1))

src = Path(tempfile.mkdtemp()) / "page.jpg"
src.write_bytes(b"\xff\xd8\xff" + b"x" * 100)
check("miss before store", cache.get_page(k1, "jpg") is None)
cache.put_page(k1, "jpg", src)
hit = cache.get_page(k1, "jpg")
check("hit after store", hit is not None and hit.read_bytes() == src.read_bytes())
check(
    "cache counts hits/misses",
    (cache.hits, cache.misses) == (1, 1),
    (cache.hits, cache.misses),
)

pdfkey = cache.pdf_key([k1, k3], "auto")
check("pdf key deterministic", pdfkey == cache.pdf_key([k1, k3], "auto"))
check("pdf key depends on engine", pdfkey != cache.pdf_key([k1, k3], "img2pdf"))

# --- net-log filtering --------------------------------------------------------
log = ss.NetLog(Path(tempfile.mkdtemp()), ss.Log(False))
for url, want in [
    ("https://html.scribdassets.com/abc/pages/1-a1.jsonp", True),
    ("https://html.scribdassets.com/abc/images/1-a1.jpg?token=x", True),
    ("https://html.scribdassets.com/abc/fonts/0001.woff2", True),
    ("https://s-f.scribdassets.com/webpack/monolith/8260.abc.css", True),
    ("https://tpc.googlesyndication.com/pagead/images/ad.png", False),
    ("https://www.scribd.com/document/1", False),
]:
    got = bool(log.INTERESTING.search(url))
    check(f"net-log {'keeps' if want else 'ignores'} {url.split('/')[2]}", got == want)

# --- manifest parsing (regexes used by quick_manifest) ------------------------
import re as _re  # noqa: E402

html_doc = (
    '<meta property="og:title" content="My Doc | Scribd">'
    "<script>docManager.addPage({pageNum:1, origWidth:902, origHeight:1168, "
    'contentUrl:"https://html.scribdassets.com/pfx/pages/1-aaaa1111.jsonp"});'
    "docManager.addPage({pageNum:2, origWidth:902, origHeight:1168, "
    'contentUrl:"https://html.scribdassets.com/pfx/pages/2-bbbb2222.jsonp"});</script>'
)
found = {}
for m in _re.finditer(
    r"html\.scribdassets\.com/[\w]+/pages/(\d+)-([0-9a-z]+)\.jsonp", html_doc
):
    found.setdefault(int(m.group(1)), m.group(2))
check("manifest finds page hashes", found == {1: "aaaa1111", 2: "bbbb2222"}, found)

# --- works without pymupdf (tight disk on a panel) ---------------------------
import shutil as _shutil  # noqa: E402
import tempfile  # noqa: E402

from PIL import Image as _Image  # noqa: E402

_tmp = Path(tempfile.mkdtemp())
_shutil.copy(ss.__file__, _tmp / "scribd_shot_copy.py")
shots = []
for n in (1, 2, 3):
    dest = _tmp / f"{n:05d}.png"
    _Image.new("RGB", (200, 280), (255, 255, 255)).save(dest)
    shots.append(
        ss.Shot(
            number=n, path=dest, width=200, height=280, css_width=100, css_height=140
        )
    )
cap = ss.Capture(doc_id="1", title="No MuPDF", url="u", shots=shots)

saved = sys.modules.get("pymupdf")
sys.modules["pymupdf"] = None  # type: ignore[assignment]
try:
    out = ss.build_pdf(cap, _tmp, ss.Log(False), pdf_engine="auto")
    check(
        "auto falls back to img2pdf when pymupdf is missing",
        out.exists() and out.stat().st_size > 0,
    )
    import pypdf as _pypdf

    check("fallback pdf has every page", len(_pypdf.PdfReader(str(out)).pages) == 3)
    check("no stray .img2pdf.pdf left behind", not list(_tmp.glob("*.img2pdf.pdf")))
finally:
    if saved is None:
        del sys.modules["pymupdf"]
    else:
        sys.modules["pymupdf"] = saved
    _shutil.rmtree(_tmp, ignore_errors=True)

# --- bot-challenge detection (Scribd now challenges plain HTTP) -------------
try:
    import scribd_pdf as _spdf

    check(
        "Client Challenge HTML detected",
        _spdf._is_challenge(
            "<html><head><title>Client Challenge</title></head></html>"
        ),
    )
    check(
        "real document HTML not flagged",
        not _spdf._is_challenge(
            "<html>" + "addPage({pageNum:1,origWidth:600});" * 200 + "</html>"
        ),
    )
    check(
        "ChallengeError is a RuntimeError",
        issubclass(_spdf.ChallengeError, RuntimeError),
    )
except Exception as exc:  # noqa: BLE001
    check(f"scribd_pdf challenge helpers ({exc})", False)

# --- cookie jars (browser exports, curl jars) --------------------------------
import tempfile as _tempfile  # noqa: E402

jar = Path(_tempfile.mkdtemp()) / "cookies.txt"
jar.write_text(
    "# Netscape HTTP Cookie File\n"
    ".scribd.com\tTRUE\t/\tTRUE\t1900000000\tscribd_ubtc\tu=abc\n"
    "#HttpOnly_.scribd.com\tTRUE\t/\tTRUE\t1900000000\t_scribd_session\tsess\n"
    "#HttpOnly_auth.scribd.com\tFALSE\t/\tTRUE\t1900000000\tauth0\ts%3Axyz\n"
    ".scribd.com\tTRUE\t/document/750213011/\tTRUE\t1900000000\tspv_750213011\tproof\n"
    ".scribd.com\tTRUE\t/\tTRUE\t1000000000\tstale\told\n",
    encoding="utf-8",
)
cks = ss.load_cookies(jar)
names = {c["name"]: c for c in cks}
check("netscape jar parsed", len(cks) == 5, len(cks))
check("#HttpOnly_ prefix is not treated as a comment", "_scribd_session" in names)
check("httpOnly flag kept", names["_scribd_session"]["httpOnly"] is True)
check("plain cookie is not httpOnly", names["scribd_ubtc"]["httpOnly"] is False)
check("path scope kept", names["spv_750213011"]["path"] == "/document/750213011/")
check("domain has no leading dot", names["scribd_ubtc"]["domain"] == "scribd.com")
check("subdomain kept", names["auth0"]["domain"] == "auth.scribd.com")
check("auth0 value untouched", names["auth0"]["value"] == "s%3Axyz")

notes = ss.cookie_report(cks, "750213011")
check("expired cookie reported", any("expired" in n for n in notes), notes)
check(
    "scoped cookie for the right doc is fine",
    not any("will NOT be sent" in n for n in notes),
    notes,
)

notes = ss.cookie_report(cks, "272351848")
check(
    "scope mismatch is explained",
    any("will NOT be sent" in n and "272351848" in n for n in notes),
    notes,
)

clean = [{"name": "a", "value": "b", "expires": -1}]
check("clean jar has no notes", ss.cookie_report(clean, "123") == [])

holes = [{"name": "sess", "value": "abc<<PASTE_MIDDLE>>def", "expires": -1}]
check(
    "placeholder detected",
    any("placeholder" in n for n in ss.cookie_report(holes)),
    ss.cookie_report(holes),
)

check(
    "no false positive on a normal value",
    ss.cookie_report([{"name": "x", "value": "abc", "expires": -1}]) == [],
)

# --- ad blocklist -------------------------------------------------------------
for url, want in [
    ("https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js", True),
    ("https://securepubads.g.doubleclick.net/tag/js/gpt.js", True),
    ("https://c.amazon-adsystem.com/aax2/apstag.js", True),
    ("https://cdn.taboola.com/libtrc/loader.js", True),
    ("https://srv.stackadapt.com/sa.js", True),
    ("https://html.scribdassets.com/abc/images/1-a1.jpg", False),
    ("https://s-f.scribdassets.com/webpack/monolith/x.css", False),
    ("https://www.scribd.com/document/1", False),
]:
    got = bool(_re.search(ss.AD_HOST_RE, url, _re.I))
    check(
        f"ad-block {'blocks' if want else 'allows'} {url.split('/')[2][:32]}",
        got == want,
    )

check("paywall markers defined", len(ss.PAYWALL_MARKERS) >= 4)
check(
    "paywall markers cover the interstitial text",
    any("Unlock the next" in m for m in ss.PAYWALL_MARKERS),
)
check("capture carries paywall state", "paywall" in ss.Capture.__dataclass_fields__)
check("capture carries ad counter", "ads_blocked" in ss.Capture.__dataclass_fields__)

# --- CLI contract: no arguments must not look like a crash -------------------
import subprocess  # noqa: E402

here = Path(__file__).resolve().parent
res = subprocess.run(
    [sys.executable, str(here / "scribd_shot.py")],
    capture_output=True,
    text=True,
    timeout=60,
)
check(
    "no-args exits 0 (panels treat exit!=0 as a crash)",
    res.returncode == 0,
    res.returncode,
)
check("no-args prints usage", "URL_OR_ID" in res.stdout, res.stdout[:120])
check("no-args points at --from-file", "--from-file" in res.stdout)

# --- bundle is present, parses, and exposes the same CLI ---------------------
bundle = here / "scribd_all.py"
if bundle.exists():
    import ast as _ast  # noqa: E402

    _ast.parse(bundle.read_text(encoding="utf-8"))
    check("scribd_all.py parses", True)
    res = subprocess.run(
        [sys.executable, str(bundle)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    check("bundle no-args exits 0", res.returncode == 0, res.returncode)
    check(
        "bundle embeds scribd_pdf",
        "scribd_pdf" in bundle.read_text(encoding="utf-8")[:4000],
    )
else:
    check("scribd_all.py exists (run make_bundle.py)", False)

# --- targets file parsing ----------------------------------------------------
targets_file = Path(tempfile.mkdtemp()) / "ids.txt"
targets_file.write_text("272351848\n# comment\n\n949773556\n", encoding="utf-8")
lines = [
    line.strip()
    for line in targets_file.read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.strip().startswith("#")
]
check(
    "from-file skips comments/blank lines", lines == ["272351848", "949773556"], lines
)

# --- cookie file: no editing needed ------------------------------------------
hdr = "scribd_ubtc=ux=1&h=2; _scribd_session=eyJhbGciOi; sssuid=bf6aab6d; __pdst=abc"
names = [c["name"] for c in ss.parse_cookies(hdr)]
check(
    "pasted Cookie: header is parsed as-is",
    names == ["scribd_ubtc", "_scribd_session", "sssuid", "__pdst"],
    names,
)
check(
    "Cookie: prefix is stripped",
    [c["name"] for c in ss.parse_cookies("Cookie: a=b; c=d")] == ["a", "c"],
)
check(
    "header cookies default to the scribd.com domain",
    all(c["domain"] == "scribd.com" for c in ss.parse_cookies(hdr)),
)
check(
    "session-only cookies (expires -1) are accepted",
    all(c["expires"] == -1 for c in ss.parse_cookies(hdr)),
)
check(
    "header with a trailing semicolon still parses",
    len(ss.parse_cookies(hdr + ";")) == 4,
)
check(
    "an empty header yields no cookies (no exception)",
    ss.parse_cookies("") == [],
)
check(
    "a quoted value keeps its inner '=' signs",
    ss.parse_cookies("a=b=c; d=e")[0]["value"] == "b=c",
)

# --- env route: .env is the only file the user touches ----------------------
import os as _os  # noqa: E402

_os.environ["SCRIBD_COOKIES"] = hdr
check("cookies_from_env reads SCRIBD_COOKIES", len(ss.cookies_from_env()) == 4)
_os.environ["SCRIBD_COOKIES"] = "  "
check("blank SCRIBD_COOKIES means no cookies", ss.cookies_from_env() == [])
_os.environ.pop("SCRIBD_COOKIES", None)

# --- offload flags -----------------------------------------------------------
src = Path(ss.__file__).read_text(encoding="utf-8")
check("--offload flag exists", '"--offload"' in src)
check("--no-offload flag exists", '"--no-offload"' in src)
check("offload helper exists", "def _offload_pdf" in src)


class _NullLog(str):
    def __call__(self, *a):  # noqa: D102
        return None

    def warn(self, *a):  # noqa: D102
        return None


check(
    "offload is skipped when Drime is not configured",
    ss._offload_pdf(Path("/nonexistent.pdf"), _NullLog()) is None,
)
# machine lines may move to logfmt.record(), but they must stay parseable:
# "<NAME> <field> <field> ..." on one stdout line, no stamp, no prefix
check(
    "the DRIME line is machine readable",
    'logfmt.record("DRIME"' in src or 'print(f"DRIME {' in src,
)
check(
    "progress still uses the @@P channel",
    "logfmt.progress(" in src or 'print(f"@@P ' in src,
)

# --------------------------------------------------------------------------- #
print("-- CloakBrowser load-event workaround --")


class _FakePage:
    def __init__(self, raise_on_set_content=True):
        self.calls = []
        self.raise_on_set_content = raise_on_set_content

    def set_content(self, html, wait_until="load"):
        self.calls.append(("set_content", html, wait_until))
        if self.raise_on_set_content:
            raise RuntimeError("Page.set_content: Timeout 120000ms exceeded.")

    def goto(self, url, wait_until="load"):
        self.calls.append(("goto", url, wait_until))


import scribd_shot as S  # noqa: E402

_real_choice = S.chromium_launch.browser_choice
work = Path(tempfile.mkdtemp()) / "batchwork"
work.mkdir(parents=True, exist_ok=True)
html = "<h1>hello</h1>"

S.chromium_launch.browser_choice = lambda: "chrome"
pg = _FakePage(raise_on_set_content=False)
S._load_html(pg, html, work)
check("chrome: html via set_content", pg.calls[0][0] == "set_content", pg.calls)

S.chromium_launch.browser_choice = lambda: "cloak"
pg = _FakePage()
S._load_html(pg, html, work)
check(
    "cloak: no set_content call", all(c[0] != "set_content" for c in pg.calls), pg.calls
)
check("cloak: navigates to file://", pg.calls[0][1].startswith("file://"), pg.calls)
# the batch file is per-call and cleaned up: two tabs must never share it
check(
    "cloak: html went to a unique file",
    pg.calls[0][1].startswith("file://") and "_batch-" in pg.calls[0][1],
    pg.calls,
)
check(
    "cloak: the temp file is removed afterwards",
    not list(work.glob("_batch-*.html")),
    list(work.iterdir()),
)
check("cloak: waits for load", pg.calls[0][2] == "load", pg.calls)

pg = _FakePage()
S._load_html(pg, html)
check("cloak: falls back to a tmp dir", pg.calls[0][1].startswith("file://"), pg.calls)
S.chromium_launch.browser_choice = _real_choice

# --------------------------------------------------------------------------- #
print("-- page audit names the bad page --")


class _JobLog:
    """Collects what audit_pages reports."""

    def __init__(self):
        self.lines = []

    def __call__(self, msg):
        self.lines.append(("info", msg))

    def info(self, msg):
        self.lines.append(("info", msg))

    def warn(self, msg):
        self.lines.append(("warn", msg))

    def debug(self, msg):
        self.lines.append(("debug", msg))


def _shot(number, size=(100, 140), ink=b"same"):
    path = Path(tempfile.mkdtemp()) / f"{number:05d}.jpg"
    path.write_bytes(ink + str(number).encode())
    return ss.Shot(number=number, path=path, width=size[0], height=size[1], blank=False)


_jl = _JobLog()
ss.audit_pages([_shot(1), _shot(2), _shot(3)], _jl)
check(
    "clean pages report nothing at warn level",
    not [x for x in _jl.lines if x[0] == "warn"],
    _jl.lines,
)

# a page with a different shape
_jl = _JobLog()
odd = _shot(2, size=(800, 140))
ss.audit_pages([_shot(1), odd, _shot(3)], _jl)
warns = " ".join(m for lvl, m in _jl.lines if lvl == "warn")
check("odd-sized page is named", "page(s) 2" in warns, warns)
check("and the reason is given", "viewport/scale" in warns, warns)

# a page identical to its neighbour (a stale navigation)
_jl = _JobLog()
# byte-identical to page 1, the way a page the reader never re-rendered looks
_dup_source = _shot(1, ink=b"dup")
same = Path(tempfile.mkdtemp()) / "00002.jpg"
same.write_bytes(Path(_dup_source.path).read_bytes())
ss.audit_pages(
    [
        _dup_source,
        ss.Shot(number=2, path=same, width=100, height=140),
        _shot(3),
    ],
    _jl,
)
warns = " ".join(m for lvl, m in _jl.lines if lvl == "warn")
check("duplicate page is named", "identical" in warns and "page(s) 2" in warns, warns)
check("and suggests the reader engine", "--engine live" in warns, warns)

_jl = _JobLog()
ss.audit_pages([], _jl)
check("no pages, no crash", _jl.lines == [])

# --------------------------------------------------------------------------- #
print("-- live-reader PDF policy with no renderer fallback --")
_src = Path("scribd_shot.py").read_text()
_convert = _src[_src.index("def convert("): _src.index("def _offload_pdf(")]
check(
    "auto uses the live reader before the explicit fragment route",
    _convert.index('if engine in ("auto", "live")')
    < _convert.index("if cap is None:")
    < _convert.index("cap = capture_fragments("),
)
check(
    "auto never reassigns itself to fragments",
    'engine = "fragment"' not in _convert,
)
check(
    "live mode requires the explicit allow flag",
    'if engine == "live" and not allow_live:' in _convert,
)
check(
    "the build is stamped so an old panel file is visible",
    'BUILD = "2026-10-30e"' in _src,
)
check(
    "CloakBrowser context preserves its browser identity",
    'if chromium_launch.browser_choice() == "cloak":' in _src
    and 'kw.pop("user_agent", None)' in _src,
)

_web = Path("scribd_web.py").read_text()
check(
    "the web UI defaults to auto",
    '<option value="auto" selected>' in _web
    and 'body.get("engine") or "auto"' in _web,
)
check(
    "explicit live selection enables the guarded reader",
    'if engine == "live":\n        cmd.append("--allow-live")' in _web,
)
check(
    "the web ui never sends an unknown engine",
    'engine not in ("fragment", "live", "auto")' in _web,
)
_bot = Path("discord_bot.py").read_text()
check(
    "the bot defaults to auto and keeps the supported engine allowlist",
    'engine: str = "auto"' in _bot
    and 'engine in ("fragment", "auto")' in _bot,
)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
