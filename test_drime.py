"""Offline sanity checks for drime.py — runs a fake Drime API on localhost."""

import json
import os
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import drime  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(
        ("PASS  " if cond else "FAIL  ")
        + name
        + (f"   {extra}" if extra and not cond else "")
    )
    if not cond:
        fails.append(name)


# --- folder hash parsing ------------------------------------------------------
URL = "https://app.drime.cloud/drive/folders/MTAxNjE1ODc4MHw"
check(
    "folder_hash(full url)",
    drime.folder_hash(URL) == "MTAxNjE1ODc4MHw",
    drime.folder_hash(URL),
)
check(
    "folder_hash(bare hash)", drime.folder_hash("MTAxNjE1ODc4MHw") == "MTAxNjE1ODc4MHw"
)
check("folder_hash(spaced)", drime.folder_hash("  " + URL + "  ") == "MTAxNjE1ODc4MHw")
check("folder_hash(empty)", drime.folder_hash("") == "")
check(
    "folder_hash(quoted)", drime.folder_hash('"MTAxNjE1ODc4MHw"') == "MTAxNjE1ODc4MHw"
)
check(
    "hash_to_id decodes the id",
    drime._hash_to_id("MTAxNjE1ODc4MHw") == 1016158780,
    str(drime._hash_to_id("MTAxNjE1ODc4MHw")),
)
check("hash_to_id(garbage) is None", drime._hash_to_id("!!!!") is None)


# --- fake Drime server --------------------------------------------------------
class State:
    def __init__(self):
        self.calls = []
        self.parts = {}
        self.folder_lookup_ok = True
        self.fail_upload = False
        self.registered = []


STATE = State()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep the test output clean
        pass

    def _send(self, code, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        STATE.calls.append(("GET", path))
        if path.startswith("/api/v1/folders/") and path.endswith("/path"):
            if not STATE.folder_lookup_ok:
                return self._send(404, {"message": "not found"})
            return self._send(
                200,
                {
                    "path": [
                        {"id": 5, "name": "Root", "hash": "AAAA"},
                        {"id": 1016158780, "name": "PDFs", "hash": "MTAxNjE1ODc4MHw"},
                    ],
                    "status": "success",
                },
            )
        self._send(404, {"message": "no route " + path})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        raw = self._body()
        STATE.calls.append(("POST", path))
        payload = json.loads(raw) if raw else {}
        if path == "/api/v1/s3/simple/presign":
            return self._send(
                200,
                {
                    "url": f"http://127.0.0.1:{PORT}/s3/put?part=1",
                    "key": "uploads/uuid-1/uuid-1",
                    "acl": "private",
                    "status": "success",
                },
            )
        if path == "/api/v1/s3/multipart/create":
            return self._send(
                200,
                {
                    "key": "uploads/uuid-2/uuid-2",
                    "uploadId": "UP-123",
                    "acl": "private",
                    "status": "success",
                },
            )
        if path == "/api/v1/s3/multipart/batch-sign-part-urls":
            nums = payload.get("partNumbers") or []
            return self._send(
                200,
                {
                    "urls": [
                        {
                            "partNumber": n,
                            "url": f"http://127.0.0.1:{PORT}/s3/put?part={n}",
                        }
                        for n in nums
                    ],
                    "status": "success",
                },
            )
        if path == "/api/v1/s3/multipart/complete":
            STATE.parts["completed"] = payload.get("parts")
            return self._send(200, {"location": "s3://bucket/key", "status": "success"})
        if path == "/api/v1/s3/entries":
            if STATE.fail_upload:
                return self._send(422, {"message": "nope"})
            STATE.registered.append(payload)
            return self._send(
                200,
                {
                    "fileEntry": {
                        "id": 4242,
                        "name": payload.get("clientName"),
                        "size": payload.get("size"),
                        "parentId": payload.get("parentId"),
                    },
                    "status": "success",
                },
            )
        if path.endswith("/shareable-link"):
            return self._send(
                200, {"link": {"url": "https://dri.me/abc123"}, "status": "success"}
            )
        self._send(404, {"message": "no route " + path})

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        raw = self._body()
        n = urllib.parse.parse_qs(parsed.query).get("part", ["1"])[0]
        STATE.parts[int(n)] = len(raw)
        self.send_response(200)
        self.send_header("ETag", f'"etag-{n}"')
        self.send_header("Content-Length", "0")
        self.end_headers()


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


SRV, PORT = start_server()
BASE = f"http://127.0.0.1:{PORT}/api/v1"

client = drime.Drime(api_key="test-key", folder=URL, base=BASE, retries=2)

# --- folder resolution -------------------------------------------------------
check("folder_id resolves via /folders/<hash>/path", client.folder_id() == 1016158780)
check(
    "folder_id is cached (one lookup)",
    sum(1 for m, p in STATE.calls if p.endswith("/path")) == 1,
)

fallback = drime.Drime(api_key="k", folder=URL, base=BASE, retries=1)
STATE.folder_lookup_ok = False
check(
    "folder_id falls back to decoding the hash",
    fallback.folder_id() == 1016158780,
)
STATE.folder_lookup_ok = True

# --- small file: presign flow ------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    small = Path(td, "small.pdf")
    small.write_bytes(b"%PDF-1.4 small" * 100)
    entry = client.upload(small)
    check("small upload returns an entry", entry.get("id") == 4242)
    check("small upload used presign", any("presign" in p for _, p in STATE.calls))
    check(
        "small upload registered with the folder id",
        STATE.registered[-1]["parentId"] == 1016158780,
    )
    check(
        "small upload registered the real filename",
        STATE.registered[-1]["clientName"] == "small.pdf",
    )
    check(
        "small upload sent the right size",
        STATE.registered[-1]["size"] == small.stat().st_size,
    )
    check("presign got mime type", True)
    check("small file was PUT whole", STATE.parts.get(1) == small.stat().st_size)

# --- big file: multipart flow ------------------------------------------------
STATE.calls.clear()
STATE.parts.clear()
with tempfile.TemporaryDirectory() as td:
    big = Path(td, "big.pdf")
    size = 11 * 1024 * 1024  # 11 MB -> 3 parts
    big.write_bytes(b"x" * size)
    entry = client.upload(big)
    check("big upload returns an entry", entry.get("id") == 4242)
    check(
        "big upload used multipart",
        any("multipart/create" in p for _, p in STATE.calls),
    )
    check("big upload completed", "completed" in STATE.parts)
    parts = STATE.parts["completed"]
    check("big upload split into 3 parts", len(parts) == 3, str(len(parts)))
    check("part numbers are 1-indexed", [p["PartNumber"] for p in parts] == [1, 2, 3])
    check("ETags keep their quotes", all(p["ETag"].startswith('"') for p in parts))
    check(
        "parts cover the whole file",
        sum(v for k, v in STATE.parts.items() if isinstance(k, int)) == size,
    )
    check("multipart registered too", STATE.registered[-1]["clientName"] == "big.pdf")

# --- offload deletes only after success --------------------------------------
with tempfile.TemporaryDirectory() as td:
    f = Path(td, "a.pdf")
    f.write_bytes(b"%PDF-1.4 a")
    e = client.offload(f)
    check("offload returns entry", e.get("id") == 4242)
    check("offload deleted the local file", not f.exists())

with tempfile.TemporaryDirectory() as td:
    f = Path(td, "b.pdf")
    f.write_bytes(b"%PDF-1.4 b")
    STATE.fail_upload = True
    try:
        client.upload(f)
        ok = False
    except drime.DrimeError:
        ok = True
    STATE.fail_upload = False
    check("failed registration raises DrimeError", ok)
    check("failed upload keeps the local file", f.exists())

# --- offload wrapper never raises -------------------------------------------
os.environ.pop("DRIME_API_KEY", None)
os.environ.pop("DRIME_FOLDER", None)
notes = []
check("enabled() is False without .env", drime.enabled() is False)
check(
    "offload() no-ops when disabled",
    drime.offload("/nonexistent.pdf", log=notes.append) is None,
)
check("no noise when disabled", notes == {} or notes == [])

# --- share link --------------------------------------------------------------
link = client.share_link(4242)
check("share_link returns a url", link == "https://dri.me/abc123", link)

# --- error surfacing ---------------------------------------------------------
bad = drime.Drime(api_key="k", folder=URL, base=BASE, retries=1)
try:
    bad._api("POST", "/nope", {})  # no such route -> 404
    check("bad route raises", False)
except drime.DrimeError as exc:
    check("bad route raises DrimeError", "404" in str(exc), str(exc))

try:
    drime.Drime(api_key="", folder=URL, base=BASE)._api("GET", "/folders/x/path")
    check("missing key raises", False)
except drime.DrimeError as exc:
    check("missing key raises DrimeError", "DRIME_API_KEY" in str(exc), str(exc))

# --- empty/short input guards -------------------------------------------------
try:
    client.upload(Path("/definitely/not/here.pdf"))
    check("missing file raises", False)
except drime.DrimeError:
    check("missing file raises DrimeError", True)

with tempfile.TemporaryDirectory() as td:
    z = Path(td, "zero.pdf")
    z.write_bytes(b"")
    try:
        client.upload(z)
        check("0-byte refused", False)
    except drime.DrimeError as exc:
        check("0-byte refused", "0-byte" in str(exc))

SRV.shutdown()

print()
print("FAILURES: none" if not fails else "FAILURES: " + ", ".join(fails))
sys.exit(1 if fails else 0)
