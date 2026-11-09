"""download.py — fetch page images, refreshing the asset token on 403/429."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, List, Optional

import requests
from token import append_token

RETRYABLE = {403, 429, 500, 502, 503, 504}


def _is_image(body: bytes, ctype: str) -> bool:
    if "image" in (ctype or "").lower():
        return True
    return (
        body[:3] == b"\xff\xd8\xff"                          # JPEG
        or body[:8] == b"\x89PNG\r\n\x1a\n"                  # PNG
        or (body[:4] == b"RIFF" and body[8:12] == b"WEBP")   # WEBP
    )


def _get(session, url: str, referer: Optional[str]):
    headers = {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer
    r = session.get(url, headers=headers, timeout=60)
    return r.status_code, r.content, r.headers.get("Content-Type", "")


def download_pages(session,
                   pages,
                   outdir,
                   token: Optional[str],
                   referer: Optional[str] = None,
                   refresh: Optional[Callable[[], Optional[str]]] = None,
                   max_retries: int = 5,
                   delay_between: float = 0.25,
                   log=print) -> List[Path]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    saved: List[Path] = []
    current = token

    for page in pages:
        dest = outdir / f"{page.number:04d}.jpg"
        if dest.exists() and dest.stat().st_size > 0:
            saved.append(dest)
            continue

        backoff = 1.0
        for attempt in range(1, max_retries + 1):
            url = append_token(page.image_url, current)
            try:
                status, body, ctype = _get(session, url, referer)
            except requests.RequestException as exc:
                status, body, ctype = 0, b"", str(exc)

            if status == 200 and _is_image(body, ctype):
                dest.write_bytes(body)
                saved.append(dest)
                log(f"  page {page.number}: {len(body) / 1024:.0f} KB")
                break

            if status in RETRYABLE and refresh and attempt < max_retries:
                new_token = refresh()
                if new_token:
                    current = new_token
                    log(f"  page {page.number}: token refreshed, retry ({status})")
                time.sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue

            if attempt == max_retries:
                log(f"  page {page.number}: FAILED status={status} ctype={ctype!r}")
                break

            time.sleep(backoff)
            backoff = min(backoff * 2, 10)

        if delay_between:
            time.sleep(delay_between)

    return saved
