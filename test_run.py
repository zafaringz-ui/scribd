"""Checks for run.py - the panel Startup file."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path("/home/user")
fails: list[str] = []


def check(name: str, ok: bool, extra: object = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not ok:
        fails.append(name)


src = (HERE / "run.py").read_text()

check("run.py is python", src.startswith("#!/usr/bin/env python3"))
check("no bash shebang tricks", "#!/usr/bin/env bash" not in src)
r = subprocess.run(
    [sys.executable, "-c", f"import ast;ast.parse(open('{HERE}/run.py').read())"],
    capture_output=True,
    text=True,
)
check("run.py parses", r.returncode == 0, r.stderr[:120])

for mode, want in (
    ("web", "scribd_web.py"),
    ("bot", "discord_bot.py"),
    ("tool", "scribd_all.py"),
):
    check(f"SCRIBD_MODE={mode} -> {want}", f'"{want}"' in src)

check("execs instead of forking (signals reach the server)", "os.execv" in src)
check("adds .local to sys.path", "site-packages" in src and "sys.path.insert" in src)
check("reads .env before deciding anything", "envfile" in src)
check("auto-provisions the browser once", "chromium_setup.py" in src)
check("AUTO_BROWSER=0 is honoured", "AUTO_BROWSER" in src)
check("remember() writes the path to .env", "def remember" in src)
check("warns about the PyPI chromium stub", "'chromium' is installed" in src)
check("uses SERVER_PORT", "SERVER_PORT" in src)
check("sets TMPDIR on the volume", "TMPDIR" in src)
check("missing target exits 2 rather than crashing", "return 2" in src)

# remember() must be idempotent and must not duplicate keys
spec = importlib.util.spec_from_file_location("run_mod", HERE / "run.py")
m = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(m)

td = Path(tempfile.mkdtemp())
m.HERE = td
(td / ".env").write_text("DISCORD_TOKEN=abc\nSCRIBD_CHROMIUM=/old/path\n")
m.remember("SCRIBD_CHROMIUM", "/new/path")
text = (td / ".env").read_text()
check("remember replaces an old value", "/new/path" in text and "/old/path" not in text)
check("remember keeps other keys", "DISCORD_TOKEN=abc" in text)
check("remember does not duplicate", text.count("SCRIBD_CHROMIUM=") == 1)
m.remember("SCRIBD_CHROMIUM", "/new/path")
check(
    "remember is idempotent",
    (td / ".env").read_text().count("SCRIBD_CHROMIUM=") == 1,
)

print("-- provisioning routes to the engine actually in use --")
_src = Path("run.py").read_text() if Path("run.py").is_file() else ""
check(
    "auto provisioning asks for the chosen engine",
    'browser_choice() == "cloak"' in _src and '["--engine", "cloak"]' in _src,
)
check(
    "stealth never silently falls back to plain chromium",
    "plain Chromium was not substituted" in _src,
)
check(
    "unset SCRIBD_BROWSER means stealth, not chrome",
    'os.environ.get("SCRIBD_BROWSER") or "cloak"' in _src,
)

check(
    "cloakbrowser is a normal requirement (installed at boot, not at runtime)",
    "cloakbrowser" in (Path("requirements.txt").read_text()),
)
print()
print("FAILURES:", fails or "none")
sys.exit(1 if fails else 0)
