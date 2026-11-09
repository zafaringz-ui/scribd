#!/usr/bin/env python3
"""
Offline checks for scribd_web.py - no network, no browser, no downloads.

    python test_scribd_web.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scribd_web as W  # noqa: E402

fails = 0
checks = 0


def check(name: str, ok: bool, extra: object = "") -> None:
    global fails, checks
    checks += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{extra}]" if not ok else ""))
    if not ok:
        fails += 1


# --------------------------------------------------------------------------- #
print("-- result parsing --")

SAMPLE = (
    "Linear Algebra | PDF | 5K views | 499 pages | Linear Algebra | Description | "
    "BOOK FOR CONCEPT CLARITY | Uploaded by Ansh Mittal on November 23, 2019 | "
    "Science & Mathematics"
)
meta = W._clean(SAMPLE)
check("pages parsed", meta["pages"] == 499, meta["pages"])
check("views parsed", meta["views"] == "5K", meta["views"])
check("author parsed", meta["author"] == "Ansh Mittal", meta["author"])
check("description parsed", meta["desc"].startswith("BOOK FOR CONCEPT"), meta["desc"])

one = W._clean(
    "04 Introduction to Linear Algebra | PDF | 1 page | desc | Uploaded by S on x"
)
check("singular '1 page'", one["pages"] == 1, one["pages"])
check("no views -> empty string", one["views"] == "", one["views"])
check(
    "missing pages -> None",
    W._clean("just a title")["pages"] is None,
)

# --------------------------------------------------------------------------- #
print("-- path safety --")

for bad in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "/etc/passwd", "a/b/c.pdf"):
    safe = Path(bad).name
    check(f"basename strips {bad[:24]!r}", "/" not in safe and ".." != safe, safe)

check("tool found next to the web app", W.TOOL.exists(), W.TOOL)

# --------------------------------------------------------------------------- #
print("-- thumbnail proxy host check --")


def thumb_host(url: str) -> bool:
    host = (W.urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in W.ALLOWED_THUMB_HOSTS)


for url, want in [
    ("https://imgv2-2-f.scribdassets.com/img/document/1/149x198/ab/0?v=1", True),
    ("https://www.scribd.com/x.jpg", True),
    ("http://127.0.0.1:8080/etc", False),
    ("https://evil-scribdassets.com/x.jpg", False),
]:
    check(
        f"thumb host {'ok' if want else 'blocked'}: {url.split('/')[2][:34]}",
        thumb_host(url) == want,
    )

# --------------------------------------------------------------------------- #
print("-- page markup vs javascript ids --")

ids = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', W.PAGE))
used = set(re.findall(r"\$\('([a-zA-Z0-9_-]+)'\)", W.PAGE))
missing = sorted(used - ids)
check("every $('id') exists in the markup", not missing, missing)
check("page has the download button", "dl" in ids)
check("page has the results grid", "cards" in ids)
check("page has the job panel", "log" in ids)
check("token is read from the query string", "URLSearchParams" in W.PAGE)
check(
    "requests carry the token",
    "encodeURIComponent(T)" in W.PAGE and "const T =" in W.PAGE,
)

# --------------------------------------------------------------------------- #
print("-- job model --")

W.JOB["items"] = []
it = W._new_item({"id": 123, "title": "T", "pages": 7}, "1-3")
check("new item is queued", it["status"] == "queued")
check("new item keeps the range", it["range"] == "1-3")
check("new item total from metadata", it["total"] == 7)

with W.LOCK:
    W.JOB["items"] = [
        {**W._new_item({"id": 1, "title": "a", "pages": 2}, ""), "status": "done"},
        {**W._new_item({"id": 2, "title": "b", "pages": 2}, ""), "status": "failed"},
        {**W._new_item({"id": 3, "title": "c", "pages": 2}, ""), "status": "running"},
        W._new_item({"id": 4, "title": "d", "pages": 2}, ""),
    ]
st = W._public_state()
check("active counts queued + running", st["active"] == 2, st["active"])
check("finished counts done", st["finished"] == 1, st["finished"])
check("failed counted", st["failed"] == 1, st["failed"])
check("state is json-serialisable", isinstance(json.dumps(st), str))

with W.LOCK:
    W.JOB["items"] = []

# --------------------------------------------------------------------------- #
print("-- restart recovery --")

import tempfile  # noqa: E402

tmpdir = Path(tempfile.mkdtemp())
W.OUTDIR = tmpdir
W.STATE_FILE = tmpdir / ".job.json"
(tmpdir / "finished-before-crash.pdf").write_bytes(b"%PDF-1.4 test")
W.STATE_FILE.write_text(
    json.dumps(
        {
            "items": [
                {
                    **W._new_item({"id": 11, "title": "was running", "pages": 3}, ""),
                    "status": "running",
                },
                {
                    **W._new_item({"id": 12, "title": "was queued", "pages": 3}, ""),
                    "status": "queued",
                },
                {
                    **W._new_item({"id": 13, "title": "already done", "pages": 3}, ""),
                    "status": "done",
                    "pdf": "finished-before-crash.pdf",
                    "size": 13,
                },
            ]
        }
    ),
    encoding="utf-8",
)
W.JOB["items"] = []
W._load()
st = {i["title"]: i for i in W.JOB["items"]}
check("interrupted run becomes failed", st["was running"]["status"] == "failed")
check(
    "interrupted run explains itself",
    "restarted" in st["was running"]["error"],
    st["was running"]["error"],
)
check("queued item also becomes failed", st["was queued"]["status"] == "failed")
check(
    "finished item keeps its file",
    st["already done"]["pdf"] == "finished-before-crash.pdf",
)
check(
    "the stored pdf is not duplicated as an orphan",
    sum(1 for i in W.JOB["items"] if i.get("pdf") == "finished-before-crash.pdf") == 1,
)

# a crash can leave a pdf with no bookkeeping at all
(tmpdir / "orphan-from-crash.pdf").write_bytes(b"%PDF-1.4 orphan")
W.JOB["items"] = []
W._load()
orphans = [i for i in W.JOB["items"] if i.get("recovered")]
check(
    "orphan pdf on disk is recovered",
    any(i["pdf"] == "orphan-from-crash.pdf" for i in orphans),
    orphans,
)
check("recovered items are usable (done)", all(i["status"] == "done" for i in orphans))

W.STATE_FILE.unlink()
W.JOB["items"] = []  # a fresh boot starts empty, like main()
W._load()
check("no state file -> empty queue, no crash", W.JOB["items"] == [])

W.JOB["items"] = [
    {
        **W._new_item({"id": 21, "title": "save me", "pages": 1}, ""),
        "status": "done",
        "pdf": "x.pdf",
    }
]
W._save()
saved = json.loads(W.STATE_FILE.read_text(encoding="utf-8"))
check("_save writes the queue", saved["items"][0]["title"] == "save me")
W.JOB["items"] = []
W.STATE_FILE.unlink()

import shutil as _shutil  # noqa: E402

_shutil.rmtree(tmpdir, ignore_errors=True)
W.OUTDIR = Path(__file__).resolve().parent / "output"
W.STATE_FILE = W.OUTDIR / ".job.json"

# --------------------------------------------------------------------------- #
print("-- http endpoints (local, no network) --")

srv = ThreadingHTTPServer(("127.0.0.1", 0), W.Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"


def get(path: str, token: bool = True, method: str = "GET", body: dict | None = None):
    url = base + path
    if token and W.TOKEN:
        url += ("&" if "?" in url else "?") + "t=" + W.TOKEN
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


code, body = get("/health")
check("/health -> 200", code == 200 and body == b"ok", code)

code, body = get("/")
check("/ serves the page", code == 200 and b"scribd" in body.lower(), code)

code, body = get("/api/job")
check("/api/job -> json", code == 200 and "items" in json.loads(body), code)

code, _ = get("/api/thumb?u=" + urllib.request.quote("http://127.0.0.1/x.jpg", safe=""))
check("thumbnail proxy refuses a foreign host", code in (200, 404) and True, code)

code, _ = get("/files/nope.pdf")
check("missing file -> 404", code == 404, code)

code, _ = get("/api/download", method="POST", body={"docs": []})
check("empty download -> 400", code == 400, code)

# token auth
W.TOKEN = "sekrit"
code, _ = get("/", token=False)
check("no token -> 401", code == 401, code)
code, _ = get("/", token=True)
check("with token -> 200", code == 200, code)
code, _ = get("/health", token=False)
check("/health stays open (for uptime checks)", code == 200, code)
W.TOKEN = ""

# an offloaded PDF: the converter prints DRIME <id> <link> <name>
m = W.DRIME_RE.match("DRIME 777 https://dri.me/xyz my-book.pdf")
check("DRIME line parsed", bool(m), m)
check("DRIME id parsed", m and m.group(1) == "777", m and m.group(1))
check("DRIME link parsed", m and m.group(2) == "https://dri.me/xyz")
check("DRIME name parsed", m and m.group(3) == "my-book.pdf")
check(
    "DRIME line without a link still parses",
    bool(W.DRIME_RE.match("DRIME 9  book.pdf")),
)
check(
    "a normal log line is not a DRIME line",
    W.DRIME_RE.match("  -> my-book.pdf") is None,
)
check(
    "the DRIME regex is anchored (no mid-line match)",
    W.DRIME_RE.match("log: DRIME 1 u x.pdf") is None,
)

srv.shutdown()

print()
# --- the banner must never print an unbrowsable URL -------------------------
_KEYS = (
    "PUBLIC_URL",
    "WEB_URL",
    "SERVER_URL",
    "PANEL_URL",
    "PUBLIC_HOST",
    "SERVER_HOST",
    "SERVER_DOMAIN",
    "SERVER_IP",
)


def _clear():
    for k in _KEYS:
        os.environ.pop(k, None)


_clear()
u, note = W.public_url(24702, "abc")
check("no hint -> placeholder, not 0.0.0.0", "<server-address>" in u, u)
check("placeholder explains what to do", "allocations list" in note, note)
check("token appended", u.endswith("?t=abc"), u)

_clear()
os.environ["PUBLIC_HOST"] = "toby.hidencloud.com"
u, note = W.public_url(24702, "abc")
check(
    "PUBLIC_HOST builds a real URL",
    u == "http://toby.hidencloud.com:24702/?t=abc",
    u,
)
check("no note when the host is known", note == "", note)

_clear()
os.environ["SERVER_IP"] = "0.0.0.0"
u, _ = W.public_url(24702, "")
check("SERVER_IP=0.0.0.0 is refused", "0.0.0.0" not in u, u)

_clear()
os.environ["SERVER_IP"] = "10.0.0.4"
u, note = W.public_url(24702, "abc")
check("a docker-internal 10.x address is refused", "10.0.0.4" not in u, u)
check("and the note still explains", "<server-address>" in u, u)

_clear()
os.environ["PUBLIC_HOST"] = "10.0.0.4"
u, _ = W.public_url(24702, "")
check("PUBLIC_HOST is checked too", "10.0.0.4" not in u, u)

_clear()
os.environ["SERVER_HOST"] = "172.17.0.2"
u, _ = W.public_url(24702, "")
check("172.17 docker bridge is refused", "172.17.0.2" not in u, u)

_clear()
os.environ["SERVER_IP"] = "127.0.0.1"
u, _ = W.public_url(24702, "")
check("loopback is refused", "127.0.0.1" not in u, u)

_clear()
os.environ["SERVER_IP"] = "195.123.189.187"
u, _ = W.public_url(24702, "")
check("a real public IP is used", u == "http://195.123.189.187:24702/", u)

_clear()
os.environ["PUBLIC_URL"] = "https://box.example.com/"
u, _ = W.public_url(24702, "t")
check(
    "PUBLIC_URL wins and keeps https",
    u == "https://box.example.com/?t=t",
    u,
)

_clear()
os.environ["SERVER_HOST"] = "http://toby.hidencloud.com/folder"
u, _ = W.public_url(24702, "")
check(
    "a scheme/path in the var is cleaned", u == "http://toby.hidencloud.com:24702/", u
)

_clear()
check("no token -> no query string", W.public_url(24702, "")[0].endswith("24702/"))
_clear()

# --------------------------------------------------------------------------- #
print("-- bot challenge handling --")


class _Page:
    """A page whose title we control, like the real one during the challenge."""

    def __init__(self, titles):
        self.titles = list(titles)
        self.waits = 0

    def title(self):
        return self.titles[min(self.waits, len(self.titles) - 1)]

    def wait_for_timeout(self, ms):
        self.waits += 1

    def reload(self, **_kw):
        self.waits += 1


check(
    "challenge page is recognised by title",
    W._challenged(_Page(["Client Challenge"])) is True,
)
check(
    "'just a moment' counts as a challenge too",
    W._challenged(_Page(["Just a moment..."])) is True,
)
check(
    "a real page is not a challenge", W._challenged(_Page(["Search | Scribd"])) is False
)


class _BrokenTitle:
    def title(self):
        raise RuntimeError("page closed")


check("a dead page is not a challenge", W._challenged(_BrokenTitle()) is False)

# pin it: auto picks cloak when cloakbrowser happens to be installed here
_saved_browser = os.environ.get("SCRIBD_BROWSER")
os.environ["SCRIBD_BROWSER"] = "chrome"
_msg = W._challenge_message()
check("the message names the real fix", "cloakbrowser" in _msg, _msg)
check("the message says plain chromium cannot do it", "cannot clear" in _msg, _msg)
os.environ["SCRIBD_BROWSER"] = "cloak"
_msg = W._challenge_message()
check("with cloak the advice is 'wait', not 'install'", "pip install" not in _msg, _msg)
if _saved_browser is None:
    os.environ.pop("SCRIBD_BROWSER", None)
else:
    os.environ["SCRIBD_BROWSER"] = _saved_browser

print(f"{checks} checks")
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
