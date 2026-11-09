#!/usr/bin/env python3
"""drime.py — move finished PDFs off the local disk and into Drime Cloud.

The panel has very little disk, so a finished download must not sit around. This
module uploads a file to a Drime folder and *only then* deletes the local copy.

Everything here uses the standard library (urllib) — nothing new to pip install
on the panel.

Configuration (.env, next to this file):

    DRIME_API_KEY=...            # Account Settings -> Developers -> Create token
    DRIME_FOLDER=...             # https://app.drime.cloud/drive/folders/<hash>
                                 # (a bare <hash> works too)
    DRIME_SHARE=0                # 1 = also make a public dri.me share link
    DRIME_PART_MB=5              # multipart chunk size, 5 is the server minimum
    DRIME_KEEP=0                 # 1 = keep local copies (debugging only)

API flow (https://docs.drime.cloud):
    < 5 MB   POST /s3/simple/presign -> PUT url -> POST /s3/entries
    >= 5 MB  POST /s3/multipart/create -> POST /s3/multipart/batch-sign-part-urls
             -> PUT each part -> POST /s3/multipart/complete -> POST /s3/entries
    both     POST /file-entries/{id}/shareable-link   (optional)

CLI:
    python drime.py --check                 # validate key + folder, no upload
    python drime.py output/foo.pdf          # upload, then delete locally
    python drime.py output --keep           # upload a whole folder, keep files
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://app.drime.cloud/api/v1"
SIMPLE_LIMIT = 5 * 1024 * 1024  # bytes; API wants multipart at or above this
FOLDER_RE = re.compile(r"([A-Za-z0-9_\-]{10,})")


class DrimeError(RuntimeError):
    """Any Drime failure we want the caller to see instead of a traceback."""


def _env(name: str, default: str = "") -> str:
    """os.environ, but .env is consulted first so one file configures everything."""
    try:
        import envfile

        envfile.load()
        val = envfile.get(name)
        if val:
            return val
    except Exception:  # noqa: BLE001 - .env is optional
        pass
    return os.environ.get(name, default)


def env(name: str, default: str = "") -> str:
    """Public alias for _env, for callers that only want to display a setting."""
    return _env(name, default)


def folder_hash(raw: str) -> str:
    """Accept a full drive URL, a /folders/<hash> fragment, or a bare hash."""
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    m = re.search(r"/folders/([A-Za-z0-9_\-]+)", raw)
    if m:
        return m.group(1)
    m = FOLDER_RE.search(raw)
    return m.group(1) if m else raw


def _hash_to_id(h: str) -> int | None:
    """Drime folder hashes are base64 of '<id>|<something>' - cheap fallback."""
    try:
        pad = "=" * (-len(h) % 4)
        text = base64.urlsafe_b64decode(h + pad).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    m = re.match(r"(\d{3,})", text)
    return int(m.group(1)) if m else None


class Drime:
    """Minimal Drime Cloud client: resolve a folder, upload a file, share it."""

    def __init__(
        self,
        api_key: str | None = None,
        folder: str | None = None,
        *,
        base: str = BASE,
        timeout: int = 120,
        retries: int = 3,
        log=None,
    ):
        self.api_key = (
            api_key if api_key is not None else _env("DRIME_API_KEY")
        ).strip()
        self.folder_raw = folder if folder is not None else _env("DRIME_FOLDER")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.log = log or (lambda msg: None)
        self._folder_id: int | None = None

    # ---------------------------------------------------------------- transport
    def _request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict | None = None,
        auth: bool = True,
    ) -> tuple[int, dict, dict]:
        hdrs = {"Accept": "application/json", "User-Agent": "scribd-shot/drime"}
        if auth:
            if not self.api_key:
                raise DrimeError("DRIME_API_KEY is not set in .env")
            hdrs["Authorization"] = f"Bearer {self.api_key}"
        hdrs.update(headers or {})

        last: Exception | None = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as rsp:
                    body = rsp.read()
                    return (
                        rsp.status,
                        dict(rsp.headers),
                        self._decode(body),
                    )
            except urllib.error.HTTPError as exc:
                body = exc.read()
                payload = self._decode(body)
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries - 1:
                    wait = 2**attempt
                    self.log(f"drime: {exc.code} on {method} {url} - retry in {wait}s")
                    time.sleep(wait)
                    last = exc
                    continue
                msg = payload.get("message") or payload.get("error") or body[:200]
                raise DrimeError(f"{method} {url} -> HTTP {exc.code}: {msg}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self.retries - 1:
                    wait = 2**attempt
                    self.log(f"drime: {exc} on {method} {url} - retry in {wait}s")
                    time.sleep(wait)
                    last = exc
                    continue
                raise DrimeError(f"{method} {url} failed: {exc}") from exc
        raise DrimeError(f"{method} {url} failed after {self.retries} tries: {last}")

    @staticmethod
    def _decode(body: bytes) -> dict:
        if not body:
            return {}
        try:
            out = json.loads(body.decode("utf-8", "replace"))
            return out if isinstance(out, dict) else {"data": out}
        except Exception:  # noqa: BLE001
            return {"raw": body[:500].decode("utf-8", "replace")}

    def _api(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        _, _, out = self._request(
            method, f"{self.base}{path}", data=data, headers=headers
        )
        return out

    # ------------------------------------------------------------------- folder
    def folder_id(self) -> int:
        """Resolve the configured folder hash to the numeric parentId the API wants."""
        if self._folder_id is not None:
            return self._folder_id
        h = folder_hash(self.folder_raw)
        if not h:
            raise DrimeError("DRIME_FOLDER is not set in .env")
        try:
            out = self._api("GET", f"/folders/{h}/path")
            path = out.get("path") or []
            if path:
                fid = path[-1].get("id")
                if fid is not None:
                    self._folder_id = int(fid)
                    names = " / ".join(str(p.get("name", "?")) for p in path)
                    self.log(f"drime: folder '{names}' -> id {self._folder_id}")
                    return self._folder_id
        except DrimeError as exc:
            self.log(f"drime: folder lookup failed ({exc}) - trying hash decode")
        fid = _hash_to_id(h)
        if fid is None:
            raise DrimeError(f"could not resolve Drime folder {h!r}")
        self._folder_id = fid
        return fid

    # ------------------------------------------------------------------- upload
    def _entry(self, key: str, path: Path, parent: int) -> dict:
        """Register the uploaded object so it appears in the Drive UI."""
        uuid = key.rstrip("/").split("/")[-1]
        out = self._api(
            "POST",
            "/s3/entries",
            {
                "filename": uuid,
                "size": path.stat().st_size,
                "clientName": path.name,
                "clientMime": mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
                "clientExtension": path.suffix.lstrip(".").lower() or "bin",
                "workspaceId": 0,
                "parentId": parent,
            },
        )
        entry = out.get("fileEntry") or out.get("file_entry") or {}
        if not entry.get("id"):
            raise DrimeError(f"Drime did not return a file entry: {out}")
        return entry

    def upload(self, path: str | Path, parent: int | None = None) -> dict:
        """Upload one file. Returns the Drime file entry (with .id and .name)."""
        path = Path(path)
        if not path.is_file():
            raise DrimeError(f"{path}: not a file")
        size = path.stat().st_size
        if size == 0:
            raise DrimeError(f"{path}: refusing to upload a 0-byte file")
        parent = self.folder_id() if parent is None else parent
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        ext = path.suffix.lstrip(".").lower() or "bin"
        self.log(f"drime: uploading {path.name} ({size / 1048576:.1f} MB)")

        if size < SIMPLE_LIMIT:
            pre = self._api(
                "POST",
                "/s3/simple/presign",
                {
                    "filename": path.name,
                    "mime": mime,
                    "size": size,
                    "extension": ext,
                    "workspaceId": 0,
                    "parentId": parent,
                },
            )
            url, key = pre.get("url"), pre.get("key")
            if not url or not key:
                raise DrimeError(f"presign returned no url/key: {pre}")
            self._request(
                "PUT",
                url,
                data=path.read_bytes(),
                headers={"Content-Type": mime},
                auth=False,
            )
        else:
            created = self._api(
                "POST",
                "/s3/multipart/create",
                {
                    "filename": path.name,
                    "mime": mime,
                    "size": size,
                    "extension": ext,
                    "workspaceId": 0,
                    "parentId": parent,
                },
            )
            key, upload_id = created.get("key"), created.get("uploadId")
            if not key or not upload_id:
                raise DrimeError(
                    f"multipart create returned no key/uploadId: {created}"
                )
            parts = self._upload_parts(path, key, upload_id)
            self._api(
                "POST",
                "/s3/multipart/complete",
                {"key": key, "uploadId": upload_id, "parts": parts},
            )

        entry = self._entry(key, path, parent)
        entry.setdefault("clientName", path.name)
        self.log(f"drime: done -> id {entry['id']} ({entry.get('name')})")
        return entry

    def _upload_parts(self, path: Path, key: str, upload_id: str) -> list[dict]:
        part_size = max(5, int(_env("DRIME_PART_MB", "5") or 5)) * 1024 * 1024
        size = path.stat().st_size
        total = max(1, -(-size // part_size))
        self.log(f"drime: multipart, {total} part(s) of {part_size // 1048576} MB")
        urls: dict[int, str] = {}
        for start in range(1, total + 1, 20):
            nums = list(range(start, min(start + 20, total + 1)))
            signed = self._api(
                "POST",
                "/s3/multipart/batch-sign-part-urls",
                {"key": key, "uploadId": upload_id, "partNumbers": nums},
            )
            for item in signed.get("urls") or []:
                urls[int(item["partNumber"])] = item["url"]
        if len(urls) != total:
            raise DrimeError(f"expected {total} signed part URLs, got {len(urls)}")

        parts = []
        with path.open("rb") as fh:
            for n in range(1, total + 1):
                chunk = fh.read(part_size)
                if not chunk:
                    raise DrimeError(f"{path.name}: file shrank during upload")
                _, headers, _ = self._request(
                    "PUT",
                    urls[n],
                    data=chunk,
                    headers={"Content-Type": "application/octet-stream"},
                    auth=False,
                )
                etag = headers.get("ETag") or headers.get("Etag") or ""
                if not etag:
                    raise DrimeError(f"part {n}: no ETag from storage")
                if not etag.startswith('"'):
                    etag = f'"{etag.strip(chr(34))}"'
                parts.append({"PartNumber": n, "ETag": etag})
                self.log(f"drime:   part {n}/{total} ok")
        return parts

    # -------------------------------------------------------------------- share
    def share_link(self, entry_id: int | str) -> str:
        """Best-effort public link (this is what becomes a dri.me URL)."""
        out = self._api(
            "POST",
            f"/file-entries/{entry_id}/shareable-link",
            {"allow_download": True, "allow_edit": False},
        )
        link = out.get("link") or {}
        return link.get("url") or link.get("short_url") or link.get("link") or ""

    # ------------------------------------------------------------------ offload
    def offload(self, path: str | Path, *, delete: bool = True) -> dict:
        """Upload, then delete the local file. Never deletes on failure."""
        path = Path(path)
        entry = self.upload(path)
        if _env("DRIME_SHARE", "0").strip() in ("1", "true", "yes"):
            try:
                url = self.share_link(entry["id"])
                if url:
                    entry["url"] = url
            except DrimeError as exc:
                self.log(f"drime: share link skipped ({exc})")
        if delete and _env("DRIME_KEEP", "0").strip() not in ("1", "true", "yes"):
            try:
                path.unlink()
                self.log(f"drime: removed local {path.name}")
            except OSError as exc:
                self.log(f"drime: uploaded but could not delete {path}: {exc}")
        return entry


def enabled() -> bool:
    """True when .env is set up enough for offloading to work."""
    return bool(_env("DRIME_API_KEY").strip() and _env("DRIME_FOLDER").strip())


def offload(path: str | Path, *, delete: bool = True, log=None) -> dict | None:
    """Safe wrapper for the pipeline: never raise, return None when disabled."""
    log = log or (lambda msg: None)
    if not enabled():
        return None
    try:
        return Drime(log=log).offload(path, delete=delete)
    except Exception as exc:  # noqa: BLE001 - a failed upload must not kill a job
        log(f"drime: offload failed for {Path(path).name}: {exc} (file kept locally)")
        return None


# ------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="drime.py",
        description="Upload finished PDFs to Drime Cloud and free the local disk.",
    )
    ap.add_argument("path", nargs="?", help="file or directory to upload")
    ap.add_argument("--check", action="store_true", help="validate key + folder only")
    ap.add_argument("--keep", action="store_true", help="do not delete local files")
    ap.add_argument("--share", action="store_true", help="also create a share link")
    args = ap.parse_args(argv)

    def say(msg: str) -> None:
        print(msg, flush=True)

    key = _env("DRIME_API_KEY").strip()
    if not key:
        print("DRIME_API_KEY is not set in .env", file=sys.stderr)
        return 2
    client = Drime(api_key=key, log=say)

    if args.check:
        try:
            fid = client.folder_id()
        except DrimeError as exc:
            print(f"folder check failed: {exc}", file=sys.stderr)
            return 1
        print(f"key:    ok ({key[:6]}...{key[-4:]})")
        print(f"folder: {folder_hash(client.folder_raw)} -> id {fid}")
        return 0

    if not args.path:
        ap.print_help()
        return 2

    target = Path(args.path)
    files = (
        sorted(p for p in target.rglob("*") if p.is_file())
        if target.is_dir()
        else [target]
    )
    if not files:
        print(f"{target}: nothing to upload", file=sys.stderr)
        return 1

    failed = 0
    for f in files:
        try:
            entry = client.offload(f, delete=not args.keep)
            extra = f" {entry.get('url')}" if entry.get("url") else ""
            say(f"ok  {f.name} -> id {entry['id']}{extra}")
        except DrimeError as exc:
            failed += 1
            say(f"ERR {f.name}: {exc}")
    say(f"\n{len(files) - failed} uploaded, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
