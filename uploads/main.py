"""main.py — bulk Scribd -> PDF.

Usage:
    python main.py <url_or_id> [<url_or_id> ...] [-o OUTDIR]

Example:
    python main.py https://www.scribd.com/document/879249648/Title -o out
"""
from __future__ import annotations

import argparse
import re
import sys
import token as token_mod
from pathlib import Path

import cookies
import extract
from build_pdf import build_pdf
from download import download_pages


def slugify(text: str, fallback: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text).strip()
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:80] or fallback


def process(session, url_or_id: str, outdir: Path,
            keep_images: bool, delay: float, log=print) -> Path:
    url = extract.canonical_url(url_or_id)
    doc_id = extract.document_id(url)
    log(f"[{doc_id}] fetching document page ...")

    html = extract.fetch_html(session, url)
    title = extract.document_title(html, f"scribd-{doc_id}")
    pages = extract.extract_pages(html)
    log(f"[{doc_id}] {len(pages)} pages found")

    referer = f"https://www.scribd.com/document/{doc_id}"
    tok, enabled = token_mod.refresh_token(session, doc_id, html=html)
    if enabled and not tok:
        log(f"[{doc_id}] warning: token endpoint reported enabled but gave no token")
    log(f"[{doc_id}] asset token: {'present' if tok else 'none'} (enabled={enabled})")

    def refresh():
        new_tok, _ = token_mod.refresh_token(session, doc_id, html=html)
        return new_tok

    name = slugify(title, f"scribd-{doc_id}")
    img_dir = (outdir / name / "images") if keep_images else (outdir / f".{name}-images")

    log(f"[{doc_id}] downloading {len(pages)} images ...")
    paths = download_pages(
        session, pages, img_dir, tok, referer=referer,
        refresh=refresh, delay_between=delay, log=log,
    )
    if not paths:
        raise RuntimeError(f"[{doc_id}] no images downloaded")

    paths.sort()
    pdf_path = outdir / f"{name}.pdf"
    build_pdf(paths, pdf_path)
    log(f"[{doc_id}] wrote {pdf_path} "
        f"({pdf_path.stat().st_size / 1024:.0f} KB, {len(paths)} pages)")
    return pdf_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Download Scribd documents as PDFs.")
    parser.add_argument("urls", nargs="+", help="Scribd document URL or numeric id")
    parser.add_argument("-o", "--outdir", default=".", help="output dir (default: .)")
    parser.add_argument("-c", "--cookies", default="cookies.txt",
                        help="Netscape cookies.txt (default: cookies.txt)")
    parser.add_argument("--keep-images", action="store_true",
                        help="keep downloaded page images")
    parser.add_argument("--delay", type=float, default=0.25,
                        help="seconds between image requests (default: 0.25)")
    parser.add_argument("--ua", default=None, help="override User-Agent")
    args = parser.parse_args(argv)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    session = cookies.build_session(args.cookies, user_agent=args.ua)

    failures = 0
    for target in args.urls:
        try:
            process(session, target, outdir, args.keep_images, args.delay)
        except Exception as exc:
            failures += 1
            print(f"!! {target}: {exc}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
