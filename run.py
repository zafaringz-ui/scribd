#!/usr/bin/env python3
"""run.py — the panel Startup file. Pure Python, because the egg runs
`python /home/container/${PY_FILE}` and nothing else.

    PY_FILE=run.py            <- put this in the Startup variables
    PY_PACKAGES=              <- clear it (the PyPI "chromium" stub lives there)
    REQUIREMENTS_FILE=requirements.txt

What it does, in order:

  1. makes ./.local importable (the egg installs there with --prefix .local)
  2. reads .env
  3. checks the browser, and **unpacks it automatically if it is missing**
     (~80 MB download, once - it is skipped forever after)
  4. hands over to scribd_web.py with os.execv, so the panel's Stop button and
     signals reach the real process

Nothing here exits on its own: the panel sees a long-running server and does not
report "Exit code: 0".

Environment knobs:
    SCRIBD_MODE=web|bot|tool     what to run (default: web)
    AUTO_BROWSER=0               never download the browser automatically
    PUBLIC_HOST=toby...          so the banner prints a clickable URL
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable


def say(msg: str = "") -> None:
    """A plain line (blank lines, indented notes) outside the log columns."""
    print(msg, flush=True)


def note(key: str, msg: str) -> None:
    try:
        import logfmt

        logfmt.info(key, msg)
    except Exception:  # noqa: BLE001
        say(f"  {key:<9} {msg}")


def load_local() -> None:
    """The egg installs with `pip --prefix .local`, which is not on sys.path."""
    tag = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for cand in (HERE / ".local" / "lib" / tag / "site-packages",):
        if cand.is_dir() and str(cand) not in sys.path:
            sys.path.insert(0, str(cand))
            os.environ["PYTHONPATH"] = (
                f"{cand}{os.pathsep}{os.environ['PYTHONPATH']}"
                if os.environ.get("PYTHONPATH")
                else str(cand)
            )


def load_env() -> None:
    try:
        import envfile

        applied = envfile.load()
        if applied:
            note(".env", str(envfile.LOADED_FROM))
    except Exception as exc:  # noqa: BLE001 - .env is optional
        try:
            import logfmt

            logfmt.warn(".env", f"not loaded ({exc})")
        except Exception:  # noqa: BLE001
            say(f"  .env    : not loaded ({exc})")


def have_browser() -> tuple[bool, str]:
    """(usable, description) - a set-but-empty path must not count as usable."""
    try:
        import chromium_launch
    except Exception as exc:  # noqa: BLE001
        return False, f"chromium_launch missing ({exc})"
    st = chromium_launch.status()
    if st.get("ok"):
        return True, st.get("path") or st.get("source") or "ok"
    return False, str(st.get("hint") or "no browser")


def try_cloak() -> tuple[bool, str]:
    """SCRIBD_BROWSER=cloak: install + verify CloakBrowser, never fall back silently."""
    try:
        import cloakbrowser  # noqa: F401
    except Exception:
        note("cloak", "not installed - installing (pip install cloakbrowser)")
        if subprocess.call([PY, "-m", "pip", "install", "-q", "cloakbrowser"]) != 0:
            return False, "pip install cloakbrowser failed"
    say()
    note("cloak", "checking the browser binary and its libraries")
    rc = subprocess.call(
        [
            PY,
            "-u",
            str(HERE / "chromium_setup.py"),
            "--engine",
            "cloak",
            "--fetch-libs",
        ],
        cwd=str(HERE),
    )
    if rc != 0:
        return False, "run: python chromium_setup.py --engine cloak --fetch-libs"
    return True, "cloakbrowser (stealth)"


def provision_browser() -> bool:
    """Unpack a self-contained Chromium into ./.chromium. Idempotent."""
    if os.environ.get("AUTO_BROWSER", "1").strip() in ("0", "false", "no"):
        say("  !! AUTO_BROWSER=0 - not downloading; the UI will run but not convert")
        return False
    note("browser", "no usable browser - unpacking one into ./.chromium (~80 MB)")
    note("browser", "this happens once; later boots skip it")
    try:
        # --engine auto picks sparticuz on x86-64 and Chrome for Testing on
        # arm64. If CloakBrowser is installed, auto means *it* - so provision
        # that engine, not a browser nothing will use. --fetch-libs pulls any
        # missing .so into ./.chromium/lib with apt-get download + dpkg -x, no root.
        engine = []
        try:
            import chromium_launch

            if chromium_launch.browser_choice() == "cloak":
                engine = ["--engine", "cloak"]
        except Exception:  # noqa: BLE001
            pass
        rc = subprocess.call(
            [PY, "-u", str(HERE / "chromium_setup.py"), *engine, "--fetch-libs"],
            cwd=str(HERE),
        )
    except Exception as exc:  # noqa: BLE001
        say(f"  chromium_setup.py could not run: {exc}")
        return False
    if rc == 0 and have_browser()[0]:
        # remember it for the child process and for next time
        exe = HERE / ".chromium" / "chromium"
        os.environ["SCRIBD_CHROMIUM"] = str(exe)
        conf = HERE / ".chromium" / "fonts" / "fonts.conf"
        if conf.is_file():
            os.environ["FONTCONFIG_FILE"] = str(conf)
        remember("SCRIBD_CHROMIUM", str(exe))
        if conf.is_file():
            remember("FONTCONFIG_FILE", str(conf))
        note("browser", "ready")
        return True
    note("browser", "still not usable - read the lines above")
    return False


def remember(key: str, value: str) -> None:
    """Write a setting into .env so the next restart skips the download check."""
    path = HERE / ".env"
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if f"{key}={value}" in text:
            return
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith(f"{key}=")]
        lines.append(f"{key}={value}")
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    except OSError as exc:
        say(f"  could not update .env ({exc}) - harmless, the path is exported")


def warn_stub() -> None:
    """`pip install chromium` is a 2.4 kB stub; the egg may keep reinstalling it."""
    try:
        import importlib.util

        if importlib.util.find_spec("chromium") is not None:
            say("  note    : the PyPI package 'chromium' is installed - it is a 2.4 kB")
            say("            stub with no browser in it. Clear PY_PACKAGES in the")
            say("            panel's Startup variables so it stops coming back.")
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    load_local()
    try:
        import logfmt

        logfmt.setup(color=None)
        logfmt.quiet_third_party()
        logfmt.header("scribd -> pdf", "panel boot")
    except Exception:  # noqa: BLE001
        pass
    load_env()

    mode = (os.environ.get("SCRIBD_MODE") or "web").strip().lower()
    if mode == "tool":
        target = HERE / "scribd_all.py"
        argv = [str(target)]
    elif mode == "bot":
        target = HERE / "discord_bot.py"
        argv = [str(target), "--with-web"]
    else:
        target = HERE / "scribd_web.py"
        port = os.environ.get("SERVER_PORT") or os.environ.get("PORT") or "8080"
        argv = [str(target), "--port", str(port)]

    if not target.exists():
        say(f"!! {target.name} is missing next to run.py")
        return 2

    # a browser on the volume wins over anything stale in the environment
    local_exe = HERE / ".chromium" / "chromium"
    local_conf = HERE / ".chromium" / "fonts" / "fonts.conf"
    if local_exe.is_file():
        os.environ.setdefault("SCRIBD_CHROMIUM", str(local_exe))
    if local_conf.is_file():
        os.environ.setdefault("FONTCONFIG_FILE", str(local_conf))

    os.environ.setdefault("TMPDIR", str(HERE / ".tmp"))
    (HERE / ".tmp").mkdir(exist_ok=True)

    # CloakBrowser is the default engine: unset means stealth, not plain
    # Chromium. Only an explicit SCRIBD_BROWSER=chrome takes the other path.
    wanted = (os.environ.get("SCRIBD_BROWSER") or "cloak").strip().lower()
    stealth = not wanted.startswith(("chrome", "chromium", "playwright"))
    if stealth:
        # never replaced by plain Chromium: that is the engine that gets stuck
        # on Scribd's challenge, so a hard failure here is the honest outcome
        ok, why = try_cloak()
        if not ok:
            say()
            say(f"  browser : NOT USABLE - {why}")
            say("  plain Chromium was not substituted: it cannot clear Scribd's")
            say("  bot check anyway. The web UI starts so the port answers;")
            say("  conversions will fail until the stealth browser is in place.")
    else:
        ok, why = have_browser()
        if not ok:
            say(f"  browser : {why}")
            ok = provision_browser()
            if not ok:
                say()
                say("  the web UI will start anyway so the port answers; conversions")
                say("  will fail until the browser is in place.")
    if ok:
        note("browser", why if stealth else have_browser()[1])

    warn_stub()

    try:
        import logfmt

        logfmt.quiet_third_party()
        logfmt.info("start", f"{target.name} (mode {mode})")
    except Exception:  # noqa: BLE001
        pass

    # no second URL line here: scribd_web prints the banner with the clickable
    # address, and printing it twice makes the boot log twice as long
    sys.stdout.flush()
    # exec, so the panel's stop/signal handling reaches the server itself
    try:
        os.execv(PY, [PY, "-u", *argv])
    except OSError as exc:
        say(f"!! could not start {target.name}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
