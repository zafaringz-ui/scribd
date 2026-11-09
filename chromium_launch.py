#!/usr/bin/env python3
"""
chromium_launch.py — one place that decides which Chromium to start.

Python's Playwright normally wants `python -m playwright install chromium`, which
downloads into ~/.cache/ms-playwright.  On a shared panel that fails (no root, a
tiny or noexec /tmp, a disk quota).  So this module looks for a browser in this
order and launches whatever it finds:

  1. an explicit path (--chromium-path) or $SCRIBD_CHROMIUM
  2. $PLAYWRIGHT_CHROMIUM_EXECUTABLE / $CHROME_PATH / $CHROMIUM_PATH
  3. ./.chromium/chromium            <- what chromium_setup.py unpacks
  4. Playwright's own download       <- the normal case on a workstation
  5. a system chromium / chrome      <- apt, snap, /usr/bin/google-chrome

When the browser comes from @sparticuz/chromium it also needs its private
libraries, fonts and a writable HOME, so those are passed through the `env`
parameter instead of being global state.

Usage
-----
    import chromium_launch as cl
    browser = cl.launch(pw, args=["--no-sandbox", "--disable-dev-shm-usage"])

Every failure is raised as BrowserLaunchError with the full Playwright message,
the executable that was tried and the fix that matches the error text.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

EXE_ENV = "SCRIBD_CHROMIUM"
BROWSER_ENV = "SCRIBD_BROWSER"  # auto | chrome | cloak
CLOAK_HINT = (
    "CloakBrowser is not installed - fix: pip install cloakbrowser\n"
    "  pip install cloakbrowser\n"
    "  then: python chromium_setup.py --engine cloak --fetch-libs"
)
_OVERRIDE: str | None = None

# names tried in step 5
SYSTEM_NAMES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)


class BrowserLaunchError(RuntimeError):
    """Launch failed.  str() is short enough for a log line; .detail is full."""

    def __init__(self, summary: str, detail: str | None = None) -> None:
        super().__init__(summary)
        self.detail = detail or summary


def cloak_installed() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("cloakbrowser") is not None
    except Exception:  # noqa: BLE001
        return False


def cloak_binary() -> str | None:
    """CloakBrowser's chrome, if it has already been downloaded.

    ensure_binary() would fetch ~150 MB as a side effect, which is not something
    a startup banner may do - so this only looks.
    """
    try:
        from cloakbrowser import config as cbcfg

        root = Path(cbcfg.get_binary_dir())
    except Exception:  # noqa: BLE001
        root = Path.home() / ".cloakbrowser"
    if not root.is_dir():
        return None
    for pattern in ("chromium-*/chrome", "*/chrome", "chrome"):
        hits = sorted(root.glob(pattern))
        for hit in hits:
            if hit.is_file() and os.access(hit, os.X_OK):
                return str(hit)
    return None


def browser_choice() -> str:
    """Which backend to use: 'cloak' (default) or 'chrome'.

    unset / auto / cloak  CloakBrowser - the default, and the only engine that
                          clears Scribd's challenge
    chrome / chromium     plain Chromium, for debugging a suspicion that the
                          stealth build is the problem (it will be challenged)

    Scribd's edge serves a JS challenge that plain Chromium - headless shell
    *and* full Chrome - never clears, while CloakBrowser's source-level
    fingerprint patches clear it in ~3 seconds from the very same IP. Measured,
    not assumed, so the stealth engine is the default rather than an opt-in.
    """
    raw = (os.environ.get(BROWSER_ENV) or "auto").strip().lower()
    if raw in ("chrome", "chromium", "playwright"):
        return "chrome"
    return "cloak"


def set_override(path: str | None) -> None:
    """Force a specific executable (from --chromium-path)."""
    global _OVERRIDE
    _OVERRIDE = str(path) if path else None


# ELF e_machine values that matter here
ARCH_IDS = {0x3E: "x86_64", 0xB7: "aarch64", 0x03: "i386", 0x28: "armv7l"}
ARCH_NAMES = {
    "x86_64": "x86-64",
    "aarch64": "ARM64 (aarch64)",
    "i386": "32-bit x86",
    "armv7l": "32-bit ARM",
}


def host_arch() -> str:
    """The CPU we are running on, spelled the way uname does."""
    import platform

    machine = (platform.machine() or "").lower()
    if machine in ("amd64", "x86_64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    if machine.startswith("armv"):
        return "armv7l"
    if machine in ("i386", "i686", "x86"):
        return "i386"
    return machine


def _elf_arch(path: Path) -> str:
    """Read e_machine out of an ELF header (offset 18, 2 bytes, endian-aware)."""
    import struct

    try:
        with path.open("rb") as fh:
            head = fh.read(20)
    except OSError:
        return ""
    if len(head) < 20 or head[:4] != b"\x7fELF":
        return ""
    endian = "<" if head[5] == 1 else ">"
    try:
        machine = struct.unpack(endian + "H", head[18:20])[0]
    except struct.error:
        return ""
    if machine == 0:  # ELFOSABI "no specific instruction set"
        return ""
    return ARCH_IDS.get(machine, f"elf:{machine:#x}")


def inspect_exe(path: str | Path) -> dict:
    """Is this actually a runnable browser?  A leftover empty file is not.

    Playwright only says "Target page, context or browser has been closed" when
    the thing at the path cannot be executed, which sends people hunting in the
    wrong direction.  This looks at the file itself so the message can be exact.
    """
    p = Path(path)
    info: dict = {
        "path": str(p),
        "exists": p.exists(),
        "size": 0,
        "executable": False,
        "kind": "missing",
        "arch": "",
        "problem": "",
    }
    if not p.exists():
        info["problem"] = "the file does not exist"
        return info
    if p.is_dir():
        info["kind"] = "dir"
        info["problem"] = "that path is a directory, not a browser"
        return info
    try:
        info["size"] = p.stat().st_size
    except OSError as exc:
        info["problem"] = str(exc)
        return info
    info["executable"] = os.access(p, os.X_OK)
    try:
        head = p.open("rb").read(4)
    except OSError as exc:
        info["problem"] = str(exc)
        return info
    if head[:4] == b"\x7fELF":
        info["kind"] = "elf"
        info["arch"] = _elf_arch(p)
        want = host_arch()
        if info["arch"] and want and info["arch"] != want:
            info["problem"] = (
                f"built for {ARCH_NAMES.get(info['arch'], info['arch'])}, this "
                f"machine is {ARCH_NAMES.get(want, want)} - it cannot execute"
            )
    elif head[:2] == b"#!":
        info["kind"] = "script"
    elif info["size"] == 0:
        info["kind"] = "empty"
        info["problem"] = "the file is empty (a download or unpack that died)"
    else:
        # not ELF and not a script, but it may still be something this host can
        # execute - let exec decide rather than guessing wrongly
        info["kind"] = "unknown"
    if not info["problem"] and info["size"] < 1_000_000 and info["kind"] == "elf":
        info["problem"] = f"only {info['size']} bytes - a real chromium is ~150 MB"
    if not info["problem"] and not info["executable"]:
        info["problem"] = "the executable bit is not set (chmod +x)"
    return info


def _from_bundle(directory: Path) -> tuple[str, dict] | None:
    exe = directory / "chromium"
    if not exe.exists():
        return None
    info = inspect_exe(exe)
    if info["problem"]:
        raise BrowserLaunchError(
            f"{exe} exists but cannot be run: {info['problem']}\n"
            "  fix: python chromium_setup.py --force",
            detail=info.get("problem"),
        )
    return str(exe), _sibling_env(directory)


def bundle_dirs() -> tuple[Path, ...]:
    """Where a self-contained browser may have been unpacked."""
    here = Path(__file__).resolve().parent
    return (Path.cwd() / ".chromium", here / ".chromium")


def _playwright_browser() -> str | None:
    """Return Playwright's own chromium if it is really there."""
    try:
        cache = Path(
            os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
            or Path.home() / ".cache" / "ms-playwright"
        )
    except Exception:
        return None
    for pattern in (
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-linux64/chrome",
        "chromium_headless_shell-*/chrome-linux/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-linux64/chrome-headless-shell",
    ):
        hits = sorted(cache.glob(pattern))
        if hits:
            return str(hits[-1])
    return None


def status() -> dict:
    """What would launch, and will it work? Used for startup banners."""
    choice = browser_choice()
    if choice == "cloak":
        if not cloak_installed():
            return {
                "ok": False,
                "path": None,
                "source": "cloak (missing)",
                "hint": CLOAK_HINT,
            }
        exe = cloak_binary()
        if not exe:
            return {
                "ok": False,
                "path": None,
                "source": "cloak (no binary)",
                "hint": "CloakBrowser is installed but has no browser binary yet.\n"
                "  fix: python chromium_setup.py --engine cloak --fetch-libs",
            }
        info = inspect_exe(exe)
        if info["problem"]:
            return {
                "ok": False,
                "path": exe,
                "source": "cloak (broken)",
                "hint": f"{exe} cannot be run: {info['problem']}\n"
                "  fix: python chromium_setup.py --engine cloak --fetch-libs",
            }
        return {"ok": True, "path": exe, "source": "cloakbrowser (stealth)", "hint": ""}
    try:
        path, _env = resolve()
    except BrowserLaunchError as exc:
        text = str(exc)
        broken = "cannot be run" in text
        return {
            "ok": False,
            "path": None,
            "source": "broken" if broken else "invalid path",
            "hint": text,
        }
    if path:
        info = inspect_exe(path)
        if info["problem"]:
            return {
                "ok": False,
                "path": path,
                "source": "broken",
                "hint": f"{path} cannot be run: {info['problem']}\n"
                "  fix: python chromium_setup.py --force",
            }
        return {"ok": True, "path": path, "source": "explicit or bundled", "hint": ""}
    found = _playwright_browser()
    if found:
        return {
            "ok": True,
            "path": found,
            "source": "playwright's own download",
            "hint": "",
        }
    return {
        "ok": False,
        "path": None,
        "source": "none",
        "hint": "no browser installed - run: python chromium_setup.py",
    }


def resolve(explicit: str | None = None) -> tuple[str | None, dict[str, str]]:
    """Return (executable or None, extra environment)."""
    for path in (_OVERRIDE, explicit, os.environ.get(EXE_ENV)):
        if path:
            p = Path(path).expanduser()
            if p.is_file():
                info = inspect_exe(p)
                if info["problem"]:
                    raise BrowserLaunchError(
                        f"{p} exists but cannot be run: {info['problem']}\n"
                        "  fix: python chromium_setup.py --force",
                        detail=info.get("problem"),
                    )
                return str(p), _sibling_env(p.parent)
            raise BrowserLaunchError(
                f"{path} was given as the Chromium path but does not exist.\n"
                "  fix: python chromium_setup.py     (unpacks it into ./.chromium)"
            )

    for var in ("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "CHROME_PATH", "CHROMIUM_PATH"):
        val = os.environ.get(var)
        if val and Path(val).is_file():
            return val, _sibling_env(Path(val).parent)

    for directory in bundle_dirs():
        found = _from_bundle(directory)
        if found:
            return found

    # let Playwright use its own download if it has one
    if _playwright_browser():
        return None, {}

    for name in SYSTEM_NAMES:
        path = shutil.which(name)
        if path:
            return path, {}
    return None, {}


def _sibling_env(directory: Path) -> dict[str, str]:
    """Environment a @sparticuz bundle needs; nothing for a normal system chrome.

    LD_LIBRARY_PATH  the private userland libraries it ships (al2023/lib)
    FONTCONFIG_FILE  a fontconfig pointing at *these* fonts.  The file shipped in
                     the tarball lists Lambda paths (/var/task/.fonts), which makes
                     Skia abort the renderer with SIGTRAP as soon as a page draws
                     text - "Target closed" on the first real navigation.
    HOME             a writable profile directory
    TMPDIR           must not be a noexec/small /tmp
    """
    env: dict[str, str] = {}
    # @sparticuz ships its libraries in al2023/lib; a Chrome-for-Testing install
    # gets the ones it was missing fetched into <dir>/lib (see fetch_libs)
    libs = [d for d in (directory / "al2023" / "lib", directory / "lib") if d.is_dir()]
    if libs:
        joined = ":".join(str(d) for d in libs)
        env["LD_LIBRARY_PATH"] = (
            f"{joined}:{os.environ.get('LD_LIBRARY_PATH', '')}".rstrip(":")
        )
        env["HOME"] = str(directory)

    fonts_root = directory / "fonts"
    conf = fonts_root / "fonts.conf"
    if conf.is_file():
        text = conf.read_text(encoding="utf-8", errors="replace")
        if str(fonts_root) not in text:
            # the shipped Lambda config - rewrite it for this host
            try:
                import chromium_setup

                conf = chromium_setup.write_fonts_conf(fonts_root / "fonts", fonts_root)
            except Exception:
                conf = None
        if conf is not None:
            env["FONTCONFIG_FILE"] = str(conf)
            env.pop("FONTCONFIG_PATH", None)
            (fonts_root / "fonts-cache").mkdir(exist_ok=True)
    return env


def decoy_note() -> str:
    """`pip install chromium` is a 2.4 kB stub, not a browser - say so."""
    try:
        import importlib.util

        spec = importlib.util.find_spec("chromium")
    except Exception:  # noqa: BLE001
        spec = None
    if spec is None:
        return ""
    return (
        "\n\nnote: the PyPI package 'chromium' is installed.  It is a 2.4 kB "
        "stub that only\n  prints a download URL - it contains no browser, so "
        "pip cannot fix this.\n  You can uninstall it: pip uninstall -y chromium"
    )


def diagnose(exc: Exception, exe: str | None) -> str:
    """Map the Playwright error text onto the known causes."""
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    where = exe or "playwright's own download"
    head = f"could not launch Chromium ({where})\n  {text.splitlines()[0]}"

    if "exec format error" in low or "cannot execute binary file" in low:
        return (
            f"{head}\n\nthat browser was built for another CPU. "
            f"This machine is {ARCH_NAMES.get(host_arch(), host_arch())}.\n"
            "  fix: python chromium_setup.py --engine cft   (Chrome for Testing "
            "has arm64 builds)\n"
            "  (@sparticuz/chromium ships x86-64 only.)"
        )
    if "executable doesn't exist" in low or "playwright install" in low:
        return (
            f"{head}\n\nno browser is installed. Any host, no root needed:\n"
            "  python chromium_setup.py\n"
            "or, if you can install system packages:\n"
            "  python -m playwright install chromium" + decoy_note()
        )
    if "eacces" in low or "permission denied" in low:
        return (
            f"{head}\n\nEACCES: the filesystem holding the browser does not allow "
            "execution (a noexec mount).\n"
            "  fix: put it on an exec-allowed volume, e.g.\n"
            "    python chromium_setup.py --dir ~/chromium && "
            "export SCRIBD_CHROMIUM=~/chromium/chromium\n"
            "    (and run with TMPDIR=~/tmp so the profile lands there too)"
        )
    if "error while loading shared libraries" in low:
        line = next(
            (ln for ln in text.splitlines() if "shared libraries" in ln), text
        ).strip()
        return (
            f"{head}\n\n{line}\n"
            "a system library is missing. If you used @sparticuz/chromium, make sure "
            "the al2023 libraries are exported:\n"
            "  export LD_LIBRARY_PATH=<dir>/al2023/lib:$LD_LIBRARY_PATH"
        )
    if "enoent" in low or "no such file" in low:
        return (
            f"{head}\n\nthe executable path does not exist - "
            "check --chromium-path / $SCRIBD_CHROMIUM."
        )
    if (
        "target closed" in low
        or "target page, context or browser has been closed" in low
    ):
        return (
            f"{head}\n\n'Target closed' at launch is almost always a bad argument. "
            "Bisect:\n"
            "  launch with args=['--no-sandbox'] only, then add flags back.\n"
            "  Usual suspects: --single-process, --in-process-gpu, --no-zygote."
        )
    if "enospc" in low or "no space left" in low:
        return (
            f"{head}\n\nthe disk filled up. Point TMPDIR at a volume with ~300 MB "
            "free before extracting the browser."
        )
    return f"{head}\n\nunrecognised failure - full error:\n{text}"


def _cloak_env() -> dict[str, str]:
    """The environment a CloakBrowser chrome needs (fetched libs, writable HOME)."""
    here = Path(__file__).resolve().parent
    env = dict(os.environ)
    libs = [here / ".chromium" / "lib", here / ".chromium" / "al2023" / "lib"]
    have = [str(d) for d in libs if d.is_dir()]
    if have:
        joined = ":".join(have)
        if env.get("LD_LIBRARY_PATH"):
            joined += ":" + env["LD_LIBRARY_PATH"]
        env["LD_LIBRARY_PATH"] = joined
        env.setdefault("HOME", str(here / ".chromium"))
    return env


def launch_cloak(pw, args: list[str] | None = None, **kwargs):
    """Start the CloakBrowser binary inside *our* Playwright session.

    cloakbrowser.launch() opens a Playwright session of its own, which cannot
    be nested inside the one every caller already has open. The wrapper exposes
    the pieces it uses internally - the binary path, the stealth args and the
    env - so the same browser can be started with pw.chromium.launch() and the
    rest of this file keeps working unchanged.
    """
    try:
        from cloakbrowser.browser import (
            IGNORE_DEFAULT_ARGS,
            build_args,
            build_launch_env,
            ensure_binary,
        )
    except Exception as exc:  # noqa: BLE001
        raise BrowserLaunchError(CLOAK_HINT, detail=str(exc)) from None

    try:
        # the wrapper prints a welcome banner and a font notice on stdout;
        # neither belongs in this tool's log
        try:
            import logfmt

            logfmt.quiet_third_party()
            with logfmt.suppress():
                exe = ensure_binary()
        except ImportError:
            exe = ensure_binary()
    except Exception as exc:  # noqa: BLE001
        raise BrowserLaunchError(
            f"CloakBrowser has no binary yet: {type(exc).__name__}: {exc}\n"
            "  fix: python chromium_setup.py --engine cloak --fetch-libs",
            detail=str(exc),
        ) from None

    extra = list(args or [])
    for flag in ("--no-sandbox", "--disable-dev-shm-usage"):
        if flag not in extra:
            extra.append(flag)
    stealth_args = kwargs.pop("stealth_args", True)
    built = build_args(
        stealth_args,
        extra,
        timezone=kwargs.pop("timezone", None),
        locale=kwargs.pop("locale", "en-US"),
        headless=kwargs.pop("headless", True),
    )
    env = build_launch_env(user_env=_cloak_env()) or _cloak_env()
    try:
        return pw.chromium.launch(
            executable_path=exe,
            args=built,
            env=env,
            ignore_default_args=list(IGNORE_DEFAULT_ARGS),
            headless=kwargs.pop("headless_flag", True),
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        text = f"{type(exc).__name__}: {exc}"
        if "shared libraries" in text or "exitCode=127" in text:
            raise BrowserLaunchError(
                "CloakBrowser's chrome is missing system libraries.\n"
                "  fix: python chromium_setup.py --engine cloak --fetch-libs",
                detail=text,
            ) from None
        raise BrowserLaunchError(
            f"could not start CloakBrowser: {text.splitlines()[0]}", detail=text
        ) from None


def launch(pw, args: list[str] | None = None, **kwargs):
    """Launch a browser, choosing engine, executable and env automatically."""
    if browser_choice() == "cloak":
        return launch_cloak(pw, args, **kwargs)
    exe, extra_env = resolve(kwargs.pop("chromium_path", None))
    env = {**os.environ, **extra_env} if extra_env else None
    try:
        return pw.chromium.launch(
            executable_path=exe,
            args=args or ["--no-sandbox", "--disable-dev-shm-usage"],
            env=env,
            **kwargs,
        )
    except Exception as exc:
        full = diagnose(exc, exe)
        first = full.splitlines()[0] if full else "launch failed"
        # the fix paragraph, without the chained Playwright stack
        rest = [ln for ln in full.splitlines()[1:] if ln.strip()][:6]
        raise BrowserLaunchError("\n".join([first, *rest]), full) from None


def describe() -> str:
    """One-line summary for startup banners, honest about a missing browser."""
    choice = browser_choice()
    if choice == "cloak":
        st = status()
        if not st["ok"]:
            return f"chromium: MISSING - {st['hint'].splitlines()[0]}"
        try:
            import cloakbrowser

            ver = getattr(cloakbrowser, "__version__", "?")
        except Exception:  # noqa: BLE001
            ver = "?"
        return f"chromium: cloakbrowser {ver} (stealth) -> {st['path']}"
    st = status()
    if not st["ok"]:
        return f"chromium: MISSING - {st['hint']}"
    if st["path"]:
        return f"chromium: {st['path']}"
    return f"chromium: {st['source']}"
