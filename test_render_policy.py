"""Offline tests for strict live/fragment PDF renderer selection."""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import scribd_shot as shot


class QuietLog:
    def __call__(self, *_args, **_kwargs):
        pass

    def warn(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass


def capture(*, paywall=None, failed=None):
    return SimpleNamespace(
        doc_id="123456",
        title="test-document",
        paywall=paywall or [],
        failed=failed or [],
        shots=[object()],
    )


def run_case(manifest_pages, live, fragments, engine="auto"):
    events: list[str] = []
    old = {
        "quick_manifest": shot.quick_manifest,
        "capture_document": shot.capture_document,
        "capture_fragments": shot.capture_fragments,
        "build_pdf": shot.build_pdf,
        "verify_pdf": shot.verify_pdf,
        "browser_errors": shot._browser_errors,
    }
    shot.quick_manifest = lambda _target: {
        "pages": {n: f"hash-{n}" for n in range(1, manifest_pages + 1)},
        "doc_id": "123456",
        "title": "test-document",
    }

    def live_capture(*_args, **_kwargs):
        events.append("live")
        if isinstance(live, Exception):
            raise live
        return live

    def fragment_capture(*_args, **_kwargs):
        events.append("fragment")
        if isinstance(fragments, Exception):
            raise fragments
        return fragments

    shot.capture_document = live_capture
    shot.capture_fragments = fragment_capture
    shot.build_pdf = lambda _cap, outdir, _log, **_kw: Path(outdir) / "test.pdf"
    shot.verify_pdf = lambda *_args, **_kwargs: None
    shot._browser_errors = lambda: ()
    try:
        with tempfile.TemporaryDirectory() as directory:
            try:
                shot.convert("123456", Path(directory), QuietLog(), engine=engine)
            except RuntimeError:
                # A live-reader error is expected to propagate in auto mode.
                if not isinstance(live, RuntimeError):
                    raise
    finally:
        shot.quick_manifest = old["quick_manifest"]
        shot.capture_document = old["capture_document"]
        shot.capture_fragments = old["capture_fragments"]
        shot.build_pdf = old["build_pdf"]
        shot.verify_pdf = old["verify_pdf"]
        shot._browser_errors = old["browser_errors"]
    return events


def check(label: str, actual: list[str], expected: list[str]) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected}, got {actual}")
    print(f"PASS  {label}")


check("auto captures with the live reader", run_case(3, capture(), capture()), ["live"])
check(
    "auto propagates live errors without changing renderer",
    run_case(3, RuntimeError("reader timeout"), capture()),
    ["live"],
)
check(
    "paywall does not trigger a renderer switch",
    run_case(3, capture(paywall=[3]), capture()),
    ["live"],
)
check(
    "partial captures do not trigger a renderer switch",
    run_case(3, capture(failed=[2]), capture()),
    ["live"],
)
check(
    "large documents do not trigger an implicit renderer switch",
    run_case(801, capture(), capture()),
    ["live"],
)
check(
    "explicit fragment stays on fragment renderer",
    run_case(3, capture(), capture(), engine="fragment"),
    ["fragment"],
)
print("6 strict renderer-policy checks passed")
