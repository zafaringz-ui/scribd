#!/usr/bin/env python3
"""
Offline checks for logfmt.py - no browser, no network.

    python test_logfmt.py
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import logfmt  # noqa: E402

fails = 0
checks = 0


def check(name: str, ok: bool, extra: object = "") -> None:
    global fails, checks
    checks += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{extra}]" if not ok else ""))
    if not ok:
        fails += 1


def capture(fn) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        fn()
    return out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- #
print("-- line shape --")

logfmt.setup(verbose=False, quiet=False, color=False)
logfmt.clear_context()
out, err = capture(lambda: logfmt.info("browser", "cloakbrowser 0.6.4"))
line = out.rstrip("\n")
check("one line per call", out.count("\n") == 1, repr(out))
check("starts with a HH:MM:SS stamp", line[:2].isdigit() and line[2] == ":", line)
#            "16:32:09  INFO  browser   msg"
check("level column is fixed width", line[10:15] == "INFO ", repr(line[10:15]))
check("key column is aligned", "browser  " in line, line)
check("info goes to stdout, not stderr", err == "", repr(err))
check("no trailing spaces", line == line.rstrip(), repr(line))

# the column positions must not move between levels or keys
rows = []
for fn, key in (
    (logfmt.info, "a"),
    (logfmt.ok, "bcdefgh"),
    (logfmt.step, "x"),
):
    o, _e = capture(lambda f=fn, k=key: f(k, "message"))
    rows.append(o.rstrip("\n"))
check(
    "message column is identical for every level/key",
    len({r.index("message") for r in rows}) == 1,
    rows,
)

# --------------------------------------------------------------------------- #
print("-- levels --")

out, err = capture(lambda: logfmt.warn("drime", "no API key"))
check("warn goes to stderr", "WARN" in err and out == "", (out, err))
out, err = capture(lambda: logfmt.error("convert", "boom"))
check("error goes to stderr", "ERROR" in err, err)
out, err = capture(lambda: logfmt.debug("x", "hidden"))
check("debug is silent unless --verbose", out == "" and err == "", (out, err))
logfmt.setup(verbose=True, color=False)
out, _e = capture(lambda: logfmt.debug("x", "shown"))
check("debug prints with verbose", "shown" in out, out)
logfmt.setup(verbose=False, color=False)

# --------------------------------------------------------------------------- #
print("-- context column --")

logfmt.set_context("949773556")
out, _e = capture(lambda: logfmt.info("render", "7 pages"))
check("context is bracketed", "[949773556]" in out, out)
logfmt.clear_context()
out, _e = capture(lambda: logfmt.info("render", "7 pages"))
check("no context, no brackets", "[" not in out, out)

# --------------------------------------------------------------------------- #
print("-- detail / banners --")

out, _e = capture(lambda: logfmt.detail("fix: pip install x"))
check("detail is indented", out.startswith("  fix:"), repr(out))
out, _e = capture(lambda: logfmt.banner("web ui", [("port", "1"), ("tool", "t")]))
check("banner has a title", "web ui" in out)
check("banner aligns its rows", out.count("  port") == 1 and "tool" in out, out)
out, _e = capture(lambda: logfmt.header("scribd -> pdf", "v1"))
check(
    "header shows title and subtitle on one line",
    "scribd -> pdf" in out and "v1" in out,
)

# --------------------------------------------------------------------------- #
print("-- machine channel is never reformatted --")

out, err = capture(lambda: logfmt.progress(3, 7))
check("progress is exactly '@@P done total'", out == "@@P 3 7\n", repr(out))
check("progress never goes to stderr", err == "", repr(err))
out, _e = capture(lambda: logfmt.record("DRIME", "id1", "https://x", "f.pdf"))
check("record is space separated", out == "DRIME id1 https://x f.pdf\n", repr(out))

# the web UI parses these; a quiet run must still emit them
logfmt.setup(quiet=True)
out, _e = capture(lambda: logfmt.progress(1, 1))
check("progress survives quiet mode", out == "@@P 1 1\n", repr(out))
out, err = capture(lambda: logfmt.info("x", "muted"))
check("quiet silences info", out == "" and err == "", (out, err))
out, err = capture(lambda: logfmt.error("x", "still loud"))
check("quiet keeps errors", "still loud" in err, err)
logfmt.setup(color=False)

# --------------------------------------------------------------------------- #
print("-- colour --")

logfmt.setup(color=True)
out, _e = capture(lambda: logfmt.ok("k", "m"))
check("colour wraps the level only", "\033[" in out and "\033[0m" in out, repr(out))
check("reset comes before the key", out.index("\033[0m") < out.index("k   "), repr(out))
logfmt.setup(color=False)
out, _e = capture(lambda: logfmt.ok("k", "m"))
check("no escapes when colour is off", "\033[" not in out, repr(out))

# SCRIBD_QUIET=1 in .env makes every entry point quiet, without a flag
os.environ["SCRIBD_QUIET"] = "1"
logfmt.setup()
check("SCRIBD_QUIET=1 silences info", logfmt.QUIET is True)
del os.environ["SCRIBD_QUIET"]
logfmt.setup()
check("unset SCRIBD_QUIET is normal again", logfmt.QUIET is False)

# NO_COLOR is honoured by setup()'s default
os.environ["NO_COLOR"] = "1"
logfmt.setup()
check("NO_COLOR disables colour", logfmt._COLOR is False)
os.environ.pop("NO_COLOR", None)

# --------------------------------------------------------------------------- #
print("-- suppressing library noise --")

out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    with logfmt.suppress():
        print("cloakbrowser welcome banner")
        sys.stderr.write("font notice\n")
    print("after")
check(
    "suppress hides library stdio", "cloakbrowser" not in out.getvalue(), out.getvalue()
)
check("suppress restores stdout", "after" in out.getvalue(), out.getvalue())
check("suppress restores stderr", "font notice" not in err.getvalue(), err.getvalue())

# an exception inside suppress must still propagate
raised = False
try:
    with logfmt.suppress():
        raise RuntimeError("real failure")
except RuntimeError:
    raised = True
check("suppress does not swallow exceptions", raised)

# third-party switches, set once and never overwritten
logfmt.quiet_third_party()
check(
    "font notice silenced by default",
    os.environ.get("CLOAKBROWSER_SUPPRESS_FONT_WARNING") == "1",
)
os.environ["CLOAKBROWSER_SUPPRESS_FONT_WARNING"] = "0"
logfmt.quiet_third_party()
check(
    "an explicit setting is not overridden",
    os.environ.get("CLOAKBROWSER_SUPPRESS_FONT_WARNING") == "0",
)

# --------------------------------------------------------------------------- #
print("-- writing to a closed pipe must not raise --")


class _Dead:
    def write(self, *_a):
        raise BrokenPipeError

    def flush(self):
        pass


_real = sys.stdout
sys.stdout = _Dead()
try:
    logfmt.info("k", "m")
    _swallowed = True
except BrokenPipeError:
    _swallowed = False
finally:
    sys.stdout = _real  # restore before printing the result
check("BrokenPipeError is swallowed", _swallowed)

# --------------------------------------------------------------------------- #
print("-- elapsed --")

check("sub-minute is seconds", logfmt.elapsed().endswith("s"), logfmt.elapsed())
check(
    "over a minute is m/s",
    "m " in logfmt.elapsed(since=__import__("time").time() - 130),
)

print()
print(f"{checks} checks")
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
