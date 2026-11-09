#!/usr/bin/env python3
"""logfmt.py — one small logger for every script here.

The console is read by two audiences, so every line is shaped for both:

    a human        aligned, timestamped, one fact per line
    a script/pane  a stable prefix, no colour, and a separate machine channel

Shape:

    16:22:11  INFO   browser   cloakbrowser 0.6.4 (stealth)
    16:22:11  INFO   target    949773556  Free Sample PDF Files for Testing
    16:22:14  INFO   pages   [949773556]  7 in DOM, rendering 7
    16:22:22  OK     saved   [949773556]  Free-Sample….pdf  7 pages  1.4 MB  17.8s
    16:22:23  WARN   drime     no API key - the PDF stays on local disk
    16:22:23  ERROR  convert [949773556]  Scribd served its bot challenge

Rules kept deliberately:

  * `logfmt.progress()` is the only thing that prints a bare `@@P` line - the web
    UI parses it, so it is never coloured, timestamped or reordered.
  * colour only when stdout is a real terminal and NO_COLOR is unset.
  * nothing is printed twice: warnings and errors go to stderr, everything else
    to stdout, and both are line-buffered so a pipe sees the order.
  * `logfmt.suppress()` mutes third-party noise (pip, the CloakBrowser welcome
    banner) without swallowing a real error: the traceback is re-raised.

    import logfmt
    logfmt.setup(verbose=args.verbose)
    logfmt.info("browser", "cloakbrowser 0.6.4 (stealth)")
    logfmt.set_context("949773556")
    logfmt.ok("saved", "file.pdf  7 pages")
"""

from __future__ import annotations

import contextlib
import os
import sys
import time

VERBOSE = False
QUIET = False
_COLOR = False
_CONTEXT = ""
_started = time.time()

_TTY = None

# widths keep columns steady as values change; over-long keys still line up
LEVEL_WIDTH = 5
KEY_WIDTH = 9

_RESET = "\033[0m"
_STYLES = {
    "DEBUG": "\033[2m",  # dim
    "INFO": "",
    "OK": "\033[32m",  # green
    "WARN": "\033[33m",  # yellow
    "ERROR": "\033[31m",  # red
    "STEP": "\033[36m",  # cyan
}
_TAGS = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "OK": "OK",
    "WARN": "WARN",
    "ERROR": "ERROR",
    "STEP": "STEP",
}


def _is_tty() -> bool:
    global _TTY
    if _TTY is None:
        try:
            _TTY = bool(sys.stdout.isatty())
        except Exception:  # noqa: BLE001
            _TTY = False
    return _TTY


def setup(
    verbose: bool = False, quiet: bool = False, color: bool | None = None
) -> None:
    """Configure once, at the top of a program."""
    global VERBOSE, QUIET, _COLOR
    env_quiet = os.environ.get("SCRIBD_QUIET", "").strip().lower()
    VERBOSE = bool(verbose)
    QUIET = bool(quiet) or env_quiet in ("1", "true", "yes", "on")
    if color is None:
        color = _is_tty() and not os.environ.get("NO_COLOR") and not QUIET
    _COLOR = bool(color)
    # a pipeline that dies early must not produce a traceback of noise
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
        sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def set_context(value: object) -> None:
    """A short tag that every later line carries, e.g. the document id."""
    global _CONTEXT
    _CONTEXT = str(value).strip() if value else ""


def clear_context() -> None:
    global _CONTEXT
    _CONTEXT = ""


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def _emit(
    level: str,
    key: str,
    msg: object,
    *,
    stream=None,
    context: bool = True,
) -> None:
    if QUIET and level not in ("WARN", "ERROR"):
        return
    if level == "DEBUG" and not VERBOSE:
        return
    stream = stream or (sys.stderr if level in ("WARN", "ERROR") else sys.stdout)
    tag = _TAGS[level]
    if _COLOR and _STYLES[level]:
        tag = f"{_STYLES[level]}{tag:<{LEVEL_WIDTH}}{_RESET}"
    else:
        tag = f"{tag:<{LEVEL_WIDTH}}"
    keytxt = f"{str(key):<{KEY_WIDTH}}" if key else " " * KEY_WIDTH
    scope = f"[{_CONTEXT}] " if (context and _CONTEXT) else ""
    line = f"{_stamp()}  {tag} {keytxt} {scope}{msg}"
    try:
        print(line.rstrip(), file=stream, flush=True)
    except (BrokenPipeError, ValueError):
        # the reader went away (Ctrl-C through a pipe); stay silent
        pass


# --------------------------------------------------------------------------- #
# the four levels
# --------------------------------------------------------------------------- #
def debug(key: str, msg: object) -> None:
    _emit("DEBUG", key, msg)


def info(key: str, msg: object) -> None:
    _emit("INFO", key, msg)


def ok(key: str, msg: object) -> None:
    _emit("OK", key, msg)


def step(key: str, msg: object) -> None:
    _emit("STEP", key, msg)


def warn(key: str, msg: object) -> None:
    _emit("WARN", key, msg)


def error(key: str, msg: object) -> None:
    _emit("ERROR", key, msg)


def detail(msg: object, *, indent: int = 2) -> None:
    """A continuation line under the one before it (hints, fix lines)."""
    if QUIET:
        return
    pad = " " * indent
    try:
        print(f"{pad}{msg}", file=sys.stdout, flush=True)
    except (BrokenPipeError, ValueError):
        pass


# --------------------------------------------------------------------------- #
# banners: printed once, never repeated
# --------------------------------------------------------------------------- #
def rule(char: str = "-", width: int = 68) -> None:
    if QUIET:
        return
    try:
        print(char * width, flush=True)
    except (BrokenPipeError, ValueError):
        pass


def header(title: str, subtitle: str = "", width: int = 68) -> None:
    """A one-time title block, e.g. `scribd -> pdf` with a version on the right."""
    if QUIET:
        return
    line = f" {title.strip()}"
    if subtitle:
        pad = max(2, width - len(line) - len(subtitle) - 1)
        line += " " * pad + subtitle.strip()
    rule("=", width)
    try:
        print(line, flush=True)
    except (BrokenPipeError, ValueError):
        pass


def banner(title: str, rows: list[tuple[str, str]], width: int = 68) -> None:
    """A titled block of aligned key/value rows - used by run.py and the UI."""
    if QUIET:
        return
    try:
        print(flush=True)
        print(f" {title}", flush=True)
        rule("-", width)
        for key, value in rows:
            print(f"  {key:<9} {value}", flush=True)
        rule("-", width)
    except (BrokenPipeError, ValueError):
        pass


# --------------------------------------------------------------------------- #
# machine channel (the web UI parses this - never reformat it)
# --------------------------------------------------------------------------- #
def progress(done: int, total: int) -> None:
    try:
        print(f"@@P {done} {total}", flush=True)
    except (BrokenPipeError, ValueError):
        pass


def record(name: str, *parts: object) -> None:
    """A machine-readable result line, e.g. DRIME <id> <url> <file>."""
    try:
        print(" ".join([name, *(str(p) for p in parts)]), flush=True)
    except (BrokenPipeError, ValueError):
        pass


# --------------------------------------------------------------------------- #
# silencing third-party libraries
# --------------------------------------------------------------------------- #
class _Null:
    def write(self, *_a, **_k) -> int:
        return 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return -1

    def __getattr__(self, _name):  # pragma: no cover - defensive
        return lambda *_a, **_k: None


@contextlib.contextmanager
def suppress(stdout: bool = True, stderr: bool = True):
    """Silence library chatter (pip, the CloakBrowser banner) for a block.

    Only stdio-level prints are hidden; exceptions still propagate so a real
    failure is never swallowed by the pretty output.
    """
    olds = []
    if stdout:
        olds.append((sys, "stdout", sys.stdout))
        sys.stdout = _Null()  # type: ignore[assignment]
    if stderr:
        olds.append((sys, "stderr", sys.stderr))
        sys.stderr = _Null()  # type: ignore[assignment]
    try:
        yield
    finally:
        for mod, name, old in olds:
            setattr(mod, name, old)


def quiet_third_party() -> None:
    """Environment switches that keep other packages from printing banners."""
    os.environ.setdefault("CLOAKBROWSER_SUPPRESS_FONT_WARNING", "1")
    os.environ.setdefault("PYTHONWARNINGS", "ignore")
    os.environ.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")


def elapsed(since: float | None = None) -> str:
    """`17.8s` / `2m 04s` - for the closing line of a job."""
    seconds = time.time() - (_started if since is None else since)
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds) // 60}m {int(seconds) % 60:02d}s"
