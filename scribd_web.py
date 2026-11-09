#!/usr/bin/env python3
# ruff: noqa: E501
"""
scribd_web.py - a tiny web UI for scribd_shot.py: search, tick, download.

    python scribd_web.py                      # http://<server-ip>:8080
    python scribd_web.py --port 25811         # the allocation port of your panel
    python scribd_web.py --token s3cret       # require ?t=s3cret (do this on a VPS)

What it does:

  * searches Scribd through a real Chromium (plain HTTP gets a JS challenge),
  * shows the results as a grid of cards with thumbnails, page counts and views,
  * you tick the documents you want, hit Download, and it runs scribd_shot.py
    for each one in the background (one at a time - kinder to 4 GB of RAM),
  * shows per-document progress, then gives you the PDF or one zip of everything.

Only the standard library is used here; the heavy lifting stays in the tool.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent


from chromium_launch import BrowserLaunchError  # noqa: E402


def _challenged(pg) -> bool:
    """True while the edge is still showing its JS interstitial."""
    try:
        title = (pg.title() or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return "client challenge" in title or "just a moment" in title


def _challenge_message() -> str:
    """Say what actually fixes it, not just 'wait'."""
    try:
        import chromium_launch

        backend = chromium_launch.browser_choice()
    except Exception:  # noqa: BLE001
        backend = "chrome"
    if backend == "cloak":
        return (
            "Scribd kept serving its bot challenge for 20 s even with "
            "CloakBrowser.\nThat is unusual - the IP is probably rate limited. "
            "Wait a few minutes and try again."
        )
    return (
        "Scribd is serving its JS challenge to this browser, and plain Chromium "
        "cannot clear it.\nScribd's edge fingerprints the browser: measured on "
        "the same IP, plain Chromium\n(and full Chrome) stayed blocked while "
        "CloakBrowser cleared it in 3 seconds.\n"
        "  fix: pip install cloakbrowser\n"
        "       python chromium_setup.py --engine cloak --fetch-libs\n"
        "       then set SCRIBD_BROWSER=cloak in .env and restart"
    )


class ChallengeError(RuntimeError):
    """Scribd answered with its bot challenge instead of results."""


class BrowserCrashError(RuntimeError):
    """The browser process died while loading the page."""


def _browser_status() -> dict:
    try:
        import chromium_launch

        return chromium_launch.status()
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "path": None, "source": "unknown", "hint": str(exc)}


def _browser_line() -> str:
    try:
        import chromium_launch

        return chromium_launch.describe().replace("chromium: ", "")
    except Exception as exc:  # pragma: no cover
        return f"unknown ({exc})"


def _find_tool() -> Path:
    for name in ("scribd_all.py", "scribd_shot.py"):
        if (HERE / name).exists():
            return HERE / name
    raise SystemExit("scribd_all.py / scribd_shot.py not found next to scribd_web.py")


TOOL = _find_tool()

try:  # the UA the tool uses - keeps search and capture looking identical
    from scribd_shot import DEFAULT_UA
except Exception:  # pragma: no cover - bundle-only installs
    DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    )


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

LOCK = threading.Lock()
SEARCH_CACHE: dict[tuple[str, int], tuple[float, dict]] = {}
SEARCH_TTL = 600.0
SEARCH_BUSY = threading.Semaphore(1)  # one Chromium at a time on a small box

JOB: dict = {
    "id": 0,
    "items": [],
    "cancelled": False,
    "worker": None,
}
WORKER: threading.Thread | None = None
STATE_FILE = HERE / "output" / ".job.json"
PROC: subprocess.Popen | None = None
OUTDIR = HERE / "output"

# what the converter prints, and what it prints when a PDF has been offloaded
PDF_RE = re.compile(r"-> (\S.*\.pdf)\s*$")
DRIME_RE = re.compile(r"^DRIME (\S+) (\S*) (.*)$")
CACHE_DIR: Path | None = None
THUMBS = HERE / ".thumbs"
ALLOWED_THUMB_HOSTS = ("scribdassets.com", "scribd.com")
TOKEN = ""


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #

SEARCH_JS = r"""
() => {
  const out = new Map();
  for (const a of document.querySelectorAll('a[href*="/document/"]')) {
    const href = a.getAttribute('href').split('?')[0];
    if (out.has(href)) continue;
    let el = a;
    while (el.parentElement) {
      const n = new Set(
        [...el.parentElement.querySelectorAll('a[href*="/document/"]')]
          .map(x => x.getAttribute('href').split('?')[0])
      ).size;
      if (n > 1) break;
      el = el.parentElement;
    }
    const img = el.querySelector('img');
    out.set(href, {
      url: href,
      title: (a.innerText || '').trim().slice(0, 200),
      text: (el.innerText || '').replace(/\s*\n\s*/g, ' | ').slice(0, 400),
      thumb: img ? (img.currentSrc || img.src || '') : '',
    });
  }
  return [...out.values()];
}
"""

PAGES_RE = re.compile(r"([\d][\d,]*)\s*pages?\b", re.I)
VIEWS_RE = re.compile(r"([\d][\d.,]*\s*[KM]?)\s*views?", re.I)
AUTHOR_RE = re.compile(r"Uploaded by (.+?) on ", re.I)
DESC_RE = re.compile(r"Description\s*\|\s*(.+?)(?:\s*\|\s*Uploaded by|$)", re.I | re.S)


def _clean(text: str) -> dict:
    pages = PAGES_RE.search(text)
    views = VIEWS_RE.search(text)
    author = AUTHOR_RE.search(text)
    desc = DESC_RE.search(text)
    return {
        "pages": int(pages.group(1).replace(",", "")) if pages else None,
        "views": views.group(1).strip() if views else "",
        "author": author.group(1).strip() if author else "",
        "desc": (desc.group(1).strip() if desc else "")[:220],
    }


def scribd_search(query: str, page: int = 1) -> dict:
    """Search Scribd in a real browser and return normalised results."""
    key = (query.lower().strip(), page)
    with LOCK:
        hit = SEARCH_CACHE.get(key)
        if hit and time.time() - hit[0] < SEARCH_TTL:
            return hit[1]

    from playwright.sync_api import sync_playwright

    import chromium_launch

    url = f"https://www.scribd.com/search?query={quote_plus(query)}&page={page}"
    with SEARCH_BUSY, sync_playwright() as pw:
        browser = chromium_launch.launch(pw)
        try:
            ctx = browser.new_context(
                user_agent=DEFAULT_UA,
                viewport={"width": 1400, "height": 1200},
                locale="en-US",
            )
            ctx.route(
                re.compile(
                    r"(googlesyndication|doubleclick|adservice|amazon-adsystem|"
                    r"taboola|outbrain|criteo|pubmatic|adnxs|moatads|scorecardresearch)"
                ),
                lambda route: route.abort(),
            )
            pg = ctx.new_page()
            try:
                pg.goto(url, wait_until="domcontentloaded", timeout=90_000)
            except Exception as exc:
                if "Target page, context or browser has been closed" in str(exc):
                    raise BrowserCrashError(
                        "the browser process died while loading the search page.\n"
                        "On @sparticuz/chromium this is nearly always the font "
                        "config:\n"
                        "  python chromium_setup.py    (rewrites fonts.conf, no "
                        "re-download)\n"
                        "Full detail: run with --verbose and look for 'SIGTRAP' or "
                        "'SkFontMgr'."
                    ) from exc
                raise
            # Scribd answers with a Fastly JS "Client Challenge" first. That
            # page *solves itself* in ~3 s in a browser the edge trusts - no
            # reload needed, and reloading actually restarts the challenge.
            # Plain Chromium never clears it (measured: still challenged after
            # 18 s), while CloakBrowser clears it in 3 s from the same IP.
            deadline = time.time() + 20
            reloaded = False
            while time.time() < deadline:
                if not _challenged(pg):
                    break
                pg.wait_for_timeout(500)
                if not reloaded and time.time() > deadline - 12:
                    reloaded = True  # one retry, only after the first attempt sat
                    try:
                        pg.reload(wait_until="domcontentloaded")
                    except Exception:  # noqa: BLE001
                        pass
            if _challenged(pg):
                raise ChallengeError(_challenge_message())
            try:
                pg.wait_for_selector('a[href*="/document/"]', timeout=25_000)
            except Exception:
                pass
            pg.wait_for_timeout(2500)
            for _ in range(3):  # lazy-loaded rows
                pg.mouse.wheel(0, 2500)
                pg.wait_for_timeout(700)
            raw = pg.evaluate(SEARCH_JS)
            title = pg.title()
        finally:
            browser.close()

    items = []
    seen = set()
    for r in raw:
        m = re.search(r"/document/(\d+)", r["url"])
        if not m:
            continue
        doc_id = m.group(1)
        if doc_id in seen:
            continue
        seen.add(doc_id)
        meta = _clean(r["text"])
        items.append(
            {
                "id": doc_id,
                "title": r["title"] or meta["desc"] or f"document {doc_id}",
                "url": r["url"],
                "thumb": r["thumb"],
                **meta,
            }
        )

    payload = {"query": query, "page": page, "title": title, "items": items}
    with LOCK:
        SEARCH_CACHE[key] = (time.time(), payload)
    return payload


def fetch_thumb(url: str) -> tuple[bytes, str]:
    """Proxy a thumbnail so the browser never talks to Scribd directly."""
    host = (urlparse(url).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in ALLOWED_THUMB_HOSTS):
        raise ValueError("refusing to proxy that host")
    THUMBS.mkdir(exist_ok=True)
    key = re.sub(r"[^a-zA-Z0-9]", "_", url)[-120:]
    dest = THUMBS / key
    if dest.exists() and dest.stat().st_size > 0:
        return dest.read_bytes(), "image/jpeg"
    req = Request(
        url, headers={"User-Agent": DEFAULT_UA, "Referer": "https://www.scribd.com/"}
    )
    with urlopen(req, timeout=20) as resp:  # noqa: S310 - host is checked above
        data = resp.read()
        ctype = resp.headers.get("Content-Type", "image/jpeg")
    if len(data) > 32:
        dest.write_bytes(data)
    return data, ctype


# --------------------------------------------------------------------------- #
# job runner
# --------------------------------------------------------------------------- #


def _save() -> None:
    """Keep the queue on disk: panels restart containers without warning."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with LOCK:
            data = {"items": JOB["items"]}
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        pass


def _load() -> None:
    """Restore the queue; anything that was mid-flight becomes 'interrupted'."""
    try:
        if not STATE_FILE.exists():
            return
        items = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("items", [])
    except Exception:
        return
    for i in items:
        if i.get("status") in ("running", "queued"):
            i["status"] = "failed"
            i["error"] = "interrupted - the server restarted mid-run, tick it again"

    # a run that was killed may still have produced its PDF - never hide a file
    # that exists on disk just because the bookkeeping was lost
    claimed = {i.get("pdf") for i in items if i.get("pdf")}
    try:
        orphans = sorted(OUTDIR.glob("*.pdf"))
    except Exception:
        orphans = []
    for path in orphans:
        if path.name in claimed:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        items.append(
            {
                "id": f"disk:{path.name[:60]}",
                "title": path.name,
                "pages": None,
                "range": "",
                "status": "done",
                "done": 0,
                "total": 0,
                "detail": "from an earlier run",
                "pdf": path.name,
                "error": "",
                "size": size,
                "recovered": True,
            }
        )
        claimed.add(path.name)

    with LOCK:
        JOB["items"] = items


def _new_item(doc: dict, pages: str) -> dict:
    return {
        "id": str(doc.get("id", "")),
        "title": doc.get("title") or f"document {doc.get('id')}",
        "pages": doc.get("pages"),
        "range": pages,
        "status": "queued",
        "done": 0,
        "total": doc.get("pages") or 0,
        "detail": "",
        "pdf": "",
        "error": "",
    }


def drime_folder_label(mod) -> str:
    """A short, safe label for the configured Drime folder (never the key)."""
    try:
        raw = mod.folder_hash(mod.env("DRIME_FOLDER"))
    except Exception:  # noqa: BLE001
        return "configured folder"
    return raw[:24] + ("..." if len(raw) > 24 else "")


UNUSABLE_HOSTS = {"0.0.0.0", "::", "[::]", "127.0.0.1", "localhost", "0"}
PRIVATE_PREFIXES = (
    "10.",
    "127.",
    "192.168.",
    "169.254.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",
    "100.64.",
)


def is_private_host(host: str) -> bool:
    """Container/docker addresses (10.0.0.4) are not reachable from a browser."""
    host = host.strip("[]")
    if host in UNUSABLE_HOSTS or host.endswith(".local") or host.endswith(".internal"):
        return True
    if host.startswith(PRIVATE_PREFIXES):
        return True
    return "." not in host  # a bare container name like "box"


def public_url(port: int, token: str = "") -> tuple[str, str]:
    """A URL the user can actually click, and a note when we have to guess.

    `0.0.0.0` is a *bind* address: it means "listen on every interface inside
    this container". It is not routable, so printing it as a link is a trap -
    a browser answers ERR_ADDRESS_INVALID. Panels give you their own hostname
    instead, and some export it as an environment variable.
    """
    token_qs = f"?t={token}" if token else ""
    for key in ("PUBLIC_URL", "WEB_URL", "SERVER_URL", "PANEL_URL"):
        raw = (os.environ.get(key) or "").strip().rstrip("/")
        if raw.startswith("http://") or raw.startswith("https://"):
            return f"{raw}/{token_qs}", ""

    for key in ("PUBLIC_HOST", "SERVER_HOST", "SERVER_DOMAIN", "SERVER_IP"):
        host = (os.environ.get(key) or "").strip().strip("/")
        if not host or host in UNUSABLE_HOSTS:
            continue
        if "://" in host:
            host = host.split("://", 1)[1]
        host = host.split("/", 1)[0]
        bare = host.rsplit(":", 1)[0] if ":" in host else host
        if is_private_host(bare):
            continue
        if ":" not in host:
            host = f"{host}:{port}"
        return f"http://{host}/{token_qs}", ""

    return (
        f"http://<server-address>:{port}/{token_qs}",
        "use the hostname on the panel's allocations list - 0.0.0.0 and "
        "10.x/192.168.x addresses only work inside the container. Put it in "
        ".env as PUBLIC_HOST=<hostname> and this line becomes a link",
    )


def _public_state() -> dict:
    with LOCK:
        items = [dict(i) for i in JOB["items"]]
        active = sum(1 for i in items if i["status"] in ("queued", "running"))
    return {
        "items": items,
        "active": active,
        "cancelled": JOB["cancelled"],
        "finished": sum(1 for i in items if i["status"] == "done"),
        "failed": sum(1 for i in items if i["status"] == "failed"),
    }


# "16:36:37  INFO  capture   [949773556] 3/3 pages" - a line from the child tool
_PRETTY = re.compile(
    r"^\d{2}:\d{2}:\d{2}\s{2}(?:DEBUG|INFO|OK|WARN|ERROR|STEP)\s+\S+\s+(?:\[\d+\]\s+)?"
)


def _job_log(level: str, doc_id: str, msg: str) -> None:
    """Print one job line to the console in the shared format."""
    try:
        import logfmt

        logfmt.set_context(doc_id)
        getattr(logfmt, level, logfmt.info)(f"job/{doc_id[:4]}", msg)
        logfmt.clear_context()
    except Exception:  # noqa: BLE001 - logging must never break a job
        pass


def _run_item(item: dict, engine: str, tabs: int, scale: float) -> None:
    global PROC
    item["status"] = "running"
    item["started"] = time.time()
    _save()
    cmd = [
        sys.executable,
        "-u",
        str(TOOL),
        item["id"],
        "-o",
        str(OUTDIR),
        "--engine",
        engine,
        "--tabs",
        str(tabs),
        "--scale",
        str(scale),
        "--progress",
    ]
    if engine == "live":
        cmd.append("--allow-live")
    if item["range"]:
        cmd += ["--pages", item["range"]]
    if CACHE_DIR is not None:
        cmd += ["--cache", str(CACHE_DIR)]

    _started = time.time()
    _job_log(
        "info",
        item["id"],
        f"queued: {engine} engine"
        + (f", pages {item['range']}" if item["range"] else "")
        + (f", {tabs} tab(s)" if tabs > 1 else ""),
    )

    pdf_re = PDF_RE
    with subprocess.Popen(
        cmd,
        cwd=str(HERE),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
    ) as proc:
        PROC = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("@@P "):
                try:
                    _, done, total = line.split()
                    item["done"], item["total"] = int(done), int(total)
                except ValueError:
                    pass
                continue
            if line.strip():
                plain = line.strip()
                if _PRETTY.match(plain):
                    # the child tool already logs in this format, context and
                    # all - echo it verbatim instead of stamping it twice
                    print(line.rstrip("\n"), flush=True)
                    item["detail"] = _PRETTY.sub("", plain)[:200]
                else:
                    # anything else (a traceback, a third-party notice) is
                    # attributed to this document and kept for the browser UI
                    item["detail"] = plain[:200]
                    _job_log("info", item["id"], plain)
            m = pdf_re.search(line)
            if m:
                item["pdf"] = m.group(1)
            m = DRIME_RE.match(line)
            if m:
                item["drime"] = {
                    "id": m.group(1),
                    "url": m.group(2),
                    "name": m.group(3),
                }
        rc = proc.wait()
        PROC = None

    if rc == 0 and not item["pdf"]:
        cands = sorted(OUTDIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
        if cands:
            item["pdf"] = str(cands[-1])
    if rc == 0 and item["pdf"]:
        item["status"] = "done"
        path = Path(item["pdf"])
        item["pdf"] = path.name
        if path.exists():
            item["size"] = path.stat().st_size
        elif item.get("drime"):
            item["offloaded"] = True  # uploaded and deleted locally on purpose
    else:
        item["status"] = "failed"
        item["error"] = item["detail"] or f"exit code {rc}"

    if item["status"] == "done":
        where = item.get("drime", {}).get("url") or item["pdf"]
        _job_log("ok", item["id"], f"done in {time.time() - _started:.1f}s -> {where}")
    else:
        _job_log("error", item["id"], item["error"])
    _save()


def _worker() -> None:
    while True:
        with LOCK:
            if JOB["cancelled"]:
                for i in JOB["items"]:
                    if i["status"] == "queued":
                        i["status"] = "cancelled"
                return
            nxt = next((i for i in JOB["items"] if i["status"] == "queued"), None)
        if nxt is None:
            return
        try:
            _run_item(nxt, JOB["engine"], JOB["tabs"], JOB["scale"])
        except Exception as exc:  # keep the queue alive
            nxt["status"] = "failed"
            nxt["error"] = f"{exc}"
            traceback.print_exc()


def start_job(
    docs: list[dict], pages: str, engine: str, tabs: int, scale: float
) -> dict:
    global WORKER
    docs = docs if isinstance(docs, list) else []
    with LOCK:
        JOB["engine"] = engine
        JOB["tabs"] = tabs
        JOB["scale"] = scale
        JOB["cancelled"] = False
        have = {i["id"] for i in JOB["items"] if i["status"] in ("queued", "running")}
        for doc in docs:
            if str(doc.get("id")) in have:
                continue
            if any(i["id"] == str(doc.get("id")) for i in JOB["items"]):
                continue
            JOB["items"].append(_new_item(doc, pages))
        alive = WORKER is not None and WORKER.is_alive()
        if not alive:
            WORKER = threading.Thread(target=_worker, daemon=True, name="job")
            WORKER.start()
    return _public_state()


# --------------------------------------------------------------------------- #
# html
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scribd → PDF</title>
<style>
  :root{
    --bg:#0b0d10; --panel:#12161b; --panel2:#171c23; --line:#242b34;
    --txt:#e6ebf2; --dim:#8d99a8; --acc:#4da3ff; --ok:#38d39f; --warn:#ffb454; --bad:#ff6b6b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
  a{color:var(--acc);text-decoration:none}
  header{position:sticky;top:0;z-index:20;background:rgba(11,13,16,.92);
    backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
  .wrap{max-width:1180px;margin:0 auto;padding:0 18px}
  .bar{display:flex;align-items:center;gap:14px;height:60px}
  .logo{font-weight:700;letter-spacing:.2px}
  .logo span{color:var(--acc)}
  .chip{margin-left:auto;display:flex;gap:10px;align-items:center;font-size:13px;color:var(--dim)}
  .dot{width:8px;height:8px;border-radius:50%;background:#39424e}
  .dot.live{background:var(--ok);box-shadow:0 0 8px var(--ok)}
  form.search{display:flex;gap:10px;padding:16px 0 6px}
  input[type=search],input[type=text],select{
    background:var(--panel);border:1px solid var(--line);color:var(--txt);
    border-radius:10px;padding:11px 13px;font-size:15px;outline:none;width:100%}
  input:focus,select:focus{border-color:var(--acc)}
  button{background:var(--acc);color:#071016;border:0;border-radius:10px;
    padding:11px 16px;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap}
  button.ghost{background:transparent;border:1px solid var(--line);color:var(--txt)}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.busy{position:relative;color:transparent}
  button.busy::after{content:"";position:absolute;inset:0;margin:auto;width:14px;height:14px;
    border:2px solid var(--dim);border-top-color:transparent;border-radius:50%;
    animation:spin .7s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .muted{color:var(--dim);font-size:13px}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(196px,1fr));
    gap:14px;padding:14px 0 120px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    overflow:hidden;cursor:pointer;transition:border-color .12s,transform .12s;position:relative}
  .card:hover{border-color:#3a4553;transform:translateY(-1px)}
  .card.on{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc) inset}
  .thumb{aspect-ratio:3/4;background:var(--panel2);display:block;width:100%;object-fit:cover}
  .meta{padding:9px 10px 11px}
  .t{font-size:13px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;
    -webkit-box-orient:vertical;overflow:hidden;min-height:36px}
  .s{color:var(--dim);font-size:12px;margin-top:5px;display:flex;gap:6px;flex-wrap:wrap}
  .tick{position:absolute;top:8px;left:8px;width:22px;height:22px;border-radius:6px;
    border:1.5px solid #6b7787;background:rgba(8,10,13,.72);display:grid;place-items:center;
    font-size:13px;color:#071016}
  .card.on .tick{background:var(--acc);border-color:var(--acc)}
  .empty{padding:60px 0;text-align:center;color:var(--dim)}
  footer{position:fixed;left:0;right:0;bottom:0;background:rgba(15,18,23,.96);
    border-top:1px solid var(--line);z-index:30}
  .foot{display:flex;align-items:center;gap:12px;height:68px}
  .pill{background:var(--panel2);border:1px solid var(--line);border-radius:999px;
    padding:4px 10px;font-size:12px;color:var(--dim)}
  .log{max-width:1180px;margin:0 auto;padding:8px 18px 26px}
  .job{background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:10px 12px;margin-bottom:8px}
  .job .hd{display:flex;gap:10px;align-items:center}
  .job .name{font-size:13px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .st{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}
  .st.running{color:var(--acc);border-color:#254a73}
  .st.done{color:var(--ok);border-color:#1e5c48}
  .st.failed{color:var(--bad);border-color:#6b2a2a}
  .st.queued{color:var(--warn);border-color:#5c4620}
  .prog{height:4px;background:#1b222b;border-radius:99px;margin-top:8px;overflow:hidden}
  .prog i{display:block;height:100%;background:var(--acc);width:0;transition:width .3s}
  .detail{color:var(--dim);font-size:11.5px;margin-top:6px;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .opts{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:6px 0 10px}
  .opts select,.opts input{width:auto;padding:7px 9px;font-size:13px}
  .opts input[type=text]{width:128px}
  .opts input[type=search]{width:78px}
  .log:not(:empty){padding:12px 0 6px;margin-bottom:6px;border-bottom:1px solid var(--line)}
  .err{background:#2a1414;border:1px solid #6b2a2a;color:#ffb3b3;padding:10px 12px;
    border-radius:10px;margin:10px 0;font-size:13px;white-space:pre-line;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  @media(max-width:640px){ .bar{height:auto;padding:10px 0;flex-wrap:wrap} .cards{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))} }
</style>
</head><body>
<header><div class="wrap"><div class="bar">
  <div class="logo">scribd<span>→</span>pdf</div>
  <div class="chip"><span class="dot" id="dot"></span><span id="statusline">idle</span></div>
</div></div></header>

<div class="wrap">
  <form class="search" id="f" autocomplete="off">
    <input type="search" id="q" placeholder="search scribd — e.g. linear algebra lecture notes" required>
    <button type="submit" id="go">Search</button>
  </form>

  <div class="opts">
    <select id="engine" title="capture engine">
      <option value="fragment">fragment (fast, no paywall overlay)</option>
      <option value="auto" selected>auto (live reader only, no fallback)</option>
      <option value="live">live (drive the reader)</option>
    </select>
    <select id="tabs" title="parallel browsers">
      <option value="1">1 browser</option>
      <option value="2" selected>2 browsers</option>
      <option value="3">3 browsers</option>
    </select>
    <input type="text" id="range" placeholder="pages e.g. 1-20" title="leave empty for the whole document">
    <input type="search" id="scale" placeholder="scale 2" value="2" size="4" title="device pixel ratio (2 = retina, 3 = huge files)">
    <span class="muted" id="hint"></span>
  </div>

  <div id="error" class="err" hidden></div>
  <div class="log" id="log"></div>
  <div class="cards" id="cards"></div>
  <div class="empty" id="empty">Search Scribd to get started.</div>
  <div class="row" style="justify-content:center;padding:0 0 40px">
    <button class="ghost" id="more" hidden>Load more</button>
    <span class="muted" id="moremsg"></span>
  </div>
</div>

<footer><div class="wrap foot">
  <label class="row" style="cursor:pointer">
    <input type="checkbox" id="all"> <span class="muted">select all</span>
  </label>
  <span class="pill" id="sel">0 selected</span>
  <button class="ghost" id="cancel" style="margin-left:auto" hidden>Cancel</button>
  <button class="ghost" id="clear">Clear queue</button>
  <button id="dl" disabled>Download selected</button>
</div></footer>

<script>
const T = new URLSearchParams(location.search).get('t') || '';
const qs = (s) => s + (s.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(T);
const $ = (id) => document.getElementById(id);
let results = [], selected = new Map(), polling = null;
let query = '', page = 1, loadingMore = false, exhausted = false;

function showErr(msg){ const e=$('error'); e.hidden=!msg; e.textContent=msg||''; }

async function api(path, opts){
  const r = await fetch(qs(path), opts);
  if(!r.ok) throw new Error((await r.text()).slice(0,300) || ('HTTP '+r.status));
  return r.json();
}

async function fetchPage(q, p){
  const data = await api('/api/search?q=' + encodeURIComponent(q) + '&page=' + p);
  return data;
}

async function search(ev){
  if(ev) ev.preventDefault();
  const q = $('q').value.trim(); if(!q) return;
  query = q; page = 1; exhausted = false; loadingMore = false;
  showErr(''); $('go').disabled = true; $('go').textContent = 'Searching…';
  $('hint').textContent = 'launching a browser, this takes ~8s';
  $('more').hidden = true; $('moremsg').textContent = '';
  try{
    const data = await fetchPage(q, 1);
    results = data.items || [];
    render();                       // full redraw only for a new search
    if(!results.length) exhausted = true;
    updateMore();
  }catch(e){
    const hint = $('hint');
    hint.textContent = '';
    showErr('Search failed—— ' + e.message.split('\n')[0] +
            '\n\n' + (e.message.includes('chromium_setup') ? 'Run once on the server:\n  python chromium_setup.py' : ''));
  }
  finally{ $('go').disabled = false; $('go').textContent = 'Search'; }
}

async function loadMore(){
  if(loadingMore || exhausted || !query) return;
  loadingMore = true;
  $('more').classList.add('busy'); $('more').disabled = true;
  $('moremsg').textContent = 'loading page ' + (page + 1) + '…';
  try{
    const data = await fetchPage(query, page + 1);
    const items = data.items || [];
    const seen = new Set(results.map(r => r.id));
    const fresh = items.filter(i => !seen.has(i.id));
    page += 1;
    if(!fresh.length){ exhausted = true; }
    else { results = results.concat(fresh); appendCards(fresh); }
    updateMore();
  }catch(e){
    $('moremsg').textContent = 'could not load more: ' + e.message.split('\n')[0];
  }finally{
    loadingMore = false;
    $('more').classList.remove('busy'); $('more').disabled = false;
  }
}

function updateMore(){
  $('more').hidden = exhausted || !results.length;
  if(exhausted && results.length){
    $('moremsg').textContent = 'that is everything (' + results.length + ')';
  } else if(!exhausted){
    $('moremsg').textContent = '';
  }
  $('hint').textContent = results.length + ' results' + (exhausted ? '' : ' · page ' + page);
}

function cardFor(it){
  const c = document.createElement('div');
  c.className = 'card' + (selected.has(it.id) ? ' on' : '');
  c.dataset.id = it.id;
  c.onclick = () => toggle(it, c);
  const bits = [];
  if(it.pages) bits.push(it.pages + ' pages');
  if(it.views) bits.push(it.views + ' views');
  if(it.author) bits.push(it.author.slice(0,22));
  c.innerHTML =
    '<div class="tick">' + (selected.has(it.id) ? '✓' : '') + '</div>' +
    (it.thumb ? '<img class="thumb" loading="lazy" src="' + qs('/api/thumb?u=' + encodeURIComponent(it.thumb)) + '" alt="">' : '<div class="thumb"></div>') +
    '<div class="meta"><div class="t"></div><div class="s"></div></div>';
  c.querySelector('.t').textContent = it.title;
  c.querySelector('.s').textContent = bits.join(' · ');
  return c;
}

function render(){                       // new search: replace everything
  const box = $('cards'); box.innerHTML = '';
  $('empty').style.display = results.length ? 'none' : 'block';
  for(const it of results) box.appendChild(cardFor(it));
  update();
}

function appendCards(items){             // load more: add without touching ticks
  const box = $('cards');
  for(const it of items) box.appendChild(cardFor(it));
  $('empty').style.display = 'none';
  update();
}

function toggle(it, el){
  if(selected.has(it.id)){ selected.delete(it.id); el.classList.remove('on'); }
  else { selected.set(it.id, it); el.classList.add('on'); }
  el.querySelector('.tick').textContent = selected.has(it.id) ? '✓' : '';
  update();
}

function update(){
  $('sel').textContent = selected.size + ' selected';
  $('dl').disabled = selected.size === 0;
}

async function download(){
  if(!selected.size) return;
  $('dl').disabled = true;
  try{
    await api('/api/download', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        docs:[...selected.values()],
        pages: $('range').value.trim(),
        engine: $('engine').value,
        tabs: +$('tabs').value,
        scale: +($('scale').value || 2),
      })
    });
    selected.clear(); render(); poll();
  }catch(e){ showErr('Could not queue: ' + e.message); }
  finally{ update(); }
}

function chip(status){
  const cls = {running:'running', done:'done', failed:'failed', queued:'queued', cancelled:'failed'}[status] || '';
  return '<span class="st ' + cls + '">' + status + '</span>';
}
function kb(n){ return n ? (n/1024).toFixed(0) + ' KB' : ''; }

function renderJob(st){
  const log = $('log');
  if(!st.items.length){ log.innerHTML=''; $('cancel').hidden = true; return; }
  const running = st.active > 0;
  $('dot').className = 'dot' + (running ? ' live' : '');
  $('cancel').hidden = !running;
  $('statusline').textContent = running
    ? 'working: ' + st.active + ' left'
    : (st.finished ? st.finished + ' done' : 'idle') + (st.failed ? ', ' + st.failed + ' failed' : '');

  let html = '';
  if(st.finished > 1) html += '<div class="row" style="margin:4px 0 10px"><a href="' + qs('/api/zip') + '"><button class="ghost">Download all finished (.zip)</button></a></div>';
  for(const it of st.items){
    const pct = it.total ? Math.min(100, Math.round(100 * it.done / it.total)) : (it.status==='done'?100:0);
    html += '<div class="job"><div class="hd">' + chip(it.status) +
      '<div class="name">' + (it.title || it.id) + '</div>' +
      (it.recovered ? '<span class="muted">earlier run</span>' : '') +
      (it.status === 'done'
        ? (it.drime
            ? '<a class="muted" ' + (it.drime.url ? 'href="' + it.drime.url + '" target="_blank" rel="noopener"' : 'href="#" title="no share link - open Drime and search the name"') +
              '>on Drime' + (it.size ? ' ' + kb(it.size) : '') + (it.drime.url ? ' ↗' : '') + '</a>'
            : '<a class="muted" href="' + qs('/files/' + encodeURIComponent(it.pdf)) + '">get PDF ' + kb(it.size) + '</a>')
        : '<span class="muted">' + (it.total ? it.done + '/' + it.total + ' pages' : '') + '</span>') +
      '</div>';
    if(it.status === 'running' || it.status === 'queued')
      html += '<div class="prog"><i style="width:' + pct + '%"></i></div>';
    if(it.detail && it.status !== 'done')
      html += '<div class="detail">' + it.detail.replace(/</g,'&lt;') + '</div>';
    if(it.error)
      html += '<div class="detail" style="color:#ff9c9c">' + it.error.replace(/</g,'&lt;') + '</div>';
    html += '</div>';
  }
  log.innerHTML = html;
}

async function poll(){
  try{
    const st = await api('/api/job');
    renderJob(st);
    clearTimeout(polling);
    if(st.active > 0) polling = setTimeout(poll, 1200);
  }catch(e){ /* server restarting */ clearTimeout(polling); polling = setTimeout(poll, 3000); }
}

$('f').addEventListener('submit', search);
$('more').addEventListener('click', loadMore);
$('dl').addEventListener('click', download);
$('clear').addEventListener('click', async () => {
  await api('/api/clear', {method:'POST'});
  poll();
});
$('cancel').addEventListener('click', async () => {
  await api('/api/cancel', {method:'POST'}); poll();
});
$('all').addEventListener('change', (e) => {
  if(e.target.checked) results.forEach(it => selected.set(it.id, it));
  else selected.clear();
  render();
});

poll();
</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #


BROKEN_PIPE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def _write(handler: BaseHTTPRequestHandler, data: bytes, chunk: int = 1 << 16) -> bool:
    """Write a response body, tolerating a client that disconnected.

    Browsers close the socket all the time - a cancelled download, a closed tab,
    a flaky mobile connection. socketserver then dumps "Exception occurred during
    processing of request" with a BrokenPipeError, which is noise, not a bug.
    Returns False if the peer went away.
    """
    try:
        view = memoryview(data)
        for start in range(0, len(view), chunk):
            handler.wfile.write(view[start : start + chunk])
        return True
    except BROKEN_PIPE:
        return False


def _json(handler: BaseHTTPRequestHandler, obj: dict, status: int = 200) -> None:
    body = json.dumps(obj).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    _write(handler, body)


def _text(handler: BaseHTTPRequestHandler, body: str, status: int = 200) -> None:
    data = body.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    _write(handler, data)


class QuietServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that does not shout when a client hangs up."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, BROKEN_PIPE):
            return  # a cancelled download is not an error
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "scribd-web/1.0"

    def log_message(self, fmt: str, *args) -> None:  # quieter console
        if "--verbose" in sys.argv:
            sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    # -- auth ------------------------------------------------------------- #
    def _authed(self) -> bool:
        if not TOKEN:
            return True
        q = parse_qs(urlparse(self.path).query)
        if q.get("t", [""])[0] == TOKEN:
            return True
        cookie = self.headers.get("Cookie", "")
        return f"t={TOKEN}" in cookie

    def _deny(self) -> None:
        _text(self, "401 - add ?t=<token> to the URL", 401)

    # -- routes ----------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            return _text(self, "ok")
        if not self._authed():
            return self._deny()

        if path in ("/", "/index.html"):
            data = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            _write(self, data)
            return

        if path == "/api/search":
            q = parse_qs(urlparse(self.path).query)
            query = (q.get("q", [""])[0] or "").strip()
            page = int(q.get("page", ["1"])[0] or 1)
            if not query:
                return _json(self, {"error": "q is required"}, 400)
            try:
                return _json(self, scribd_search(query, page))
            except BrowserCrashError as exc:
                print(f"!! search: {str(exc).splitlines()[0]}", flush=True)
                return _json(self, {"error": str(exc), "kind": "crash"}, 502)
            except ChallengeError as exc:
                print(f"!! search: {exc}".splitlines()[0], flush=True)
                return _json(self, {"error": str(exc), "kind": "challenge"}, 429)
            except BrowserLaunchError as exc:
                # one readable line, not a chained Playwright stack
                print(f"!! search: {exc}", flush=True)
                return _json(
                    self,
                    {"error": str(exc), "kind": "browser", "hint": str(exc)},
                    503,
                )
            except Exception as exc:
                traceback.print_exc()
                return _json(self, {"error": f"{type(exc).__name__}: {exc}"}, 500)

        if path == "/api/thumb":
            q = parse_qs(urlparse(self.path).query)
            url = q.get("u", [""])[0]
            try:
                data, ctype = fetch_thumb(url)
            except Exception:
                data, ctype = b"", "image/jpeg"
            self.send_response(200 if data else 404)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            _write(self, data)
            return

        if path == "/api/job":
            return _json(self, _public_state())

        if path == "/api/zip":
            return self._send_zip()

        if path.startswith("/files/"):
            return self._send_file(unquote(path[len("/files/") :]))

        return _text(self, "not found", 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._authed():
            return self._deny()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        body = {}
        if raw:
            try:
                body = json.loads(raw)
            except Exception:
                body = {}

        if path == "/api/download":
            docs = body.get("docs") or []
            pages = str(body.get("pages") or "").strip()
            # Auto uses only the live reader; fragments are never selected
            # implicitly. Explicit live is passed with --allow-live.
            engine = body.get("engine") or "auto"
            if engine not in ("fragment", "live", "auto"):
                engine = "fragment"
            tabs = max(1, min(int(body.get("tabs") or 1), 4))
            scale = float(body.get("scale") or 2)
            if not docs:
                return _json(self, {"error": "no documents"}, 400)
            return _json(self, start_job(docs, pages, engine, tabs, scale))

        if path == "/api/clear":
            global WORKER
            with LOCK:
                JOB["items"] = [i for i in JOB["items"] if i["status"] == "running"]
                if not JOB["items"]:
                    JOB["cancelled"] = False
            return _json(self, _public_state())

        if path == "/api/cancel":
            global PROC
            with LOCK:
                JOB["cancelled"] = True
                for i in JOB["items"]:
                    if i["status"] == "queued":
                        i["status"] = "cancelled"
            if PROC is not None:
                PROC.terminate()
            return _json(self, _public_state())

        return _text(self, "not found", 404)

    # -- file serving ----------------------------------------------------- #
    def _send_file(self, name: str) -> None:
        name = Path(name).name  # no traversal
        path = OUTDIR / name
        if not path.exists() or path.suffix.lower() != ".pdf":
            return _text(self, "not found", 404)
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Accept-Ranges", "none")
        self.end_headers()
        # stream it: a 40-page PDF is 10 MB, but a cancelled download should not
        # keep a copy in RAM or raise BrokenPipe through socketserver
        try:
            with path.open("rb") as fh:
                while chunk := fh.read(1 << 16):
                    self.wfile.write(chunk)
        except BROKEN_PIPE:
            return

    def _send_zip(self) -> None:
        with LOCK:
            names = [
                i["pdf"] for i in JOB["items"] if i["status"] == "done" and i["pdf"]
            ]
        names = [n for n in names if (OUTDIR / n).exists()]
        if not names:
            return _text(
                self,
                "nothing finished yet (files already moved to Drime are not zipped)",
                404,
            )
        tmp = OUTDIR / f".scribd-{int(time.time())}.zip"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
            for n in names:
                zf.write(OUTDIR / n, arcname=n)
        size = tmp.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition", 'attachment; filename="scribd-pdfs.zip"'
        )
        self.end_headers()
        try:
            with tmp.open("rb") as fh:
                while chunk := fh.read(1 << 16):
                    self.wfile.write(chunk)
        except BROKEN_PIPE:
            pass
        finally:
            tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    global OUTDIR, CACHE_DIR, TOKEN
    ap = argparse.ArgumentParser(
        prog="scribd_web.py",
        description="Small web UI: search Scribd, tick documents, download PDFs.",
    )
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    ap.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SERVER_PORT") or 8080),
        help="port (default: $SERVER_PORT, the panel allocation, else 8080)",
    )
    ap.add_argument(
        "--outdir", default=None, help="where PDFs are written (default ./output)"
    )
    ap.add_argument(
        "--cache",
        nargs="?",
        const=str(HERE / ".scribd-cache"),
        default=None,
        help="page/PDF cache for the tool (default off)",
    )
    ap.add_argument(
        "--token",
        default=None,
        help='require ?t=<token> on every request. "auto" (the default) '
        'generates one and remembers it in .webtoken; "" or "off" disables it; '
        "anything else is used as the token. Also settable as WEB_TOKEN in .env",
    )
    ap.add_argument(
        "--chromium-path",
        default=None,
        metavar="PATH",
        help="browser executable (or $SCRIBD_CHROMIUM); see chromium_setup.py",
    )
    ap.add_argument("--verbose", action="store_true", help="log every request")
    args = ap.parse_args(argv)

    # .env may hold SCRIBD_CHROMIUM / WEB_TOKEN / SERVER_PORT
    try:
        import envfile

        applied = envfile.load()
        if applied:
            # the path and how many settings, not a wall of key names
            print(f"  .env    : {envfile.LOADED_FROM} ({len(applied)} setting(s))")
    except Exception as exc:  # noqa: BLE001
        print(f"  .env    : unreadable ({exc})")

    OUTDIR = Path(args.outdir).resolve() if args.outdir else HERE / "output"
    OUTDIR.mkdir(parents=True, exist_ok=True)
    if args.cache:
        CACHE_DIR = Path(args.cache).resolve()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # --token wins; otherwise .env's WEB_TOKEN (read just above), otherwise auto
    raw_token = args.token if args.token is not None else os.environ.get("WEB_TOKEN")
    raw_token = "auto" if raw_token is None else str(raw_token).strip()
    if raw_token.lower() in ("off", "none", "open", "0"):
        raw_token = ""  # an explicit opt-out is honoured, no surprise token
    TOKEN = raw_token
    if TOKEN == "auto":
        # keep the same token across restarts, otherwise every panel reboot
        # gives you a new URL to remember
        token_file = HERE / ".webtoken"
        try:
            TOKEN = token_file.read_text(encoding="utf-8").strip()
        except Exception:
            TOKEN = ""
        if not TOKEN:
            import secrets

            TOKEN = secrets.token_urlsafe(9)
            try:
                token_file.write_text(TOKEN, encoding="utf-8")
            except Exception:
                pass

    global STATE_FILE
    STATE_FILE = OUTDIR / ".job.json"
    _load()

    srv = QuietServer((args.host, args.port), Handler)
    url, url_note = public_url(args.port, TOKEN)
    # the boot banner: one aligned block, then anything that needs attention
    bstate = _browser_status()
    try:
        import logfmt

        logfmt.setup()
        logfmt.quiet_third_party()
        rows = [
            (
                "port",
                f"{args.port}"
                + (" (from $SERVER_PORT)" if os.environ.get("SERVER_PORT") else ""),
            )
        ]
        rows.append(("tool", str(TOOL.name)))
        rows.append(("browser", _browser_line()))
        rows.append(("output", str(OUTDIR)))
        if CACHE_DIR:
            rows.append(("cache", str(CACHE_DIR)))
        try:
            import drime as _drime

            if _drime.enabled():
                rows.append(
                    (
                        "offload",
                        f"drime ON -> {drime_folder_label(_drime)} (local PDFs deleted)",
                    )
                )
            else:
                rows.append(("offload", "drime off (set DRIME_API_KEY + DRIME_FOLDER)"))
        except Exception:  # noqa: BLE001 - optional
            pass
        rows.append(("listen", f"{args.host}:{args.port}  (bind address, not a URL)"))
        rows.append(("open", url))
        if TOKEN:
            rows.append(("token", f"?t={TOKEN}  (kept in .webtoken)"))
        logfmt.banner("scribd web ui", rows)

        if url_note:
            logfmt.info("note", url_note)
        if not bstate["ok"]:
            logfmt.error("browser", "search and downloads fail until one is in place")
            for line in str(bstate.get("hint") or "").splitlines():
                if line.strip():
                    logfmt.detail(line, indent=4)
            logfmt.detail("quick fix on a fresh panel:", indent=4)
            logfmt.detail(
                "python chromium_setup.py    (~200 MB, no root, no npm)", indent=6
            )
        if not TOKEN:
            logfmt.warn("token", "OFF - anyone reaching this port can start downloads")
            logfmt.detail(
                "drop WEB_TOKEN=off / --token off to get an automatic one", indent=4
            )
    except Exception as exc:  # noqa: BLE001 - never let pretty output break boot
        print(f"scribd_web - search, tick, download ({exc})", flush=True)
        print(f"  browser : {_browser_line()}", flush=True)
        print(f"  open    : {url}", flush=True)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        try:
            import logfmt

            logfmt.info("stop", "bye")
        except Exception:  # noqa: BLE001
            print("\nbye")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
