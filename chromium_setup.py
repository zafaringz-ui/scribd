#!/usr/bin/env python3
"""
chromium_setup.py — get a working Chromium on a host where you have no root.

Why this exists
---------------
`python -m playwright install chromium` needs to run a browser it downloads into
~/.cache/ms-playwright.  On a shared panel that either fails (noexec /tmp, tiny
quota) or is forbidden.  `@sparticuz/chromium` solves the problem by shipping a
self-contained AL2023 build plus its own shared libraries, which is exactly what
this script unpacks — in pure Python, no Node, no root, no system packages.

    python chromium_setup.py                  # download + unpack into ./.chromium
    python chromium_setup.py --dir /path/bin  # somewhere else
    python chromium_setup.py --force           # re-extract
    python chromium_setup.py --version 147.0.0 # pin a build

**Node and npm are not required.** If node_modules/@sparticuz/chromium is not
present it downloads the package tarball straight from the npm registry with
`requests` and unpacks the payload itself. It writes:

    <dir>/chromium            the browser executable (chmod 755)
    <dir>/al2023/lib/*.so     the libraries it needs on a minimal host
    <dir>/fonts/              fonts, so text is not blank
    <dir>/swiftshader/        software GL

Then point the tools at it:

    export SCRIBD_CHROMIUM=$PWD/.chromium/chromium
    python scribd_shot.py <id> -o out

or pass --chromium-path on any of the three tools.
"""

from __future__ import annotations

import argparse
import os
import sys
import tarfile
from pathlib import Path

_builtin_print = print


def print(*parts, sep=" ", end="\n", file=None, flush=True) -> None:  # noqa: A001
    """Every line this script emits goes through logfmt.

    This file grew ~100 plain print() calls, most shaped "key  : value" or
    "   continuation". Rather than rewrite them all (and every one added later),
    the builtin is shadowed for this module and the shapes are mapped:

        "engine  : Chrome for Testing"   ->  INFO  engine   Chrome for Testing
        "!! still failing: ..."          ->  ERROR fail     ...
        "   it started"                  ->  a quiet continuation line
        ""                               ->  nothing

    If logfmt is missing the real print is used, so this can never hide output.
    """
    text = sep.join(str(p) for p in parts)
    try:
        import logfmt
    except Exception:  # noqa: BLE001
        _builtin_print(text, end=end, file=file, flush=flush)
        return
    raw = text.rstrip("\n")
    if not raw.strip():
        return
    stripped = raw.strip()
    if stripped.startswith("!!"):
        logfmt.error("fail", stripped[2:].strip())
        return
    for sep_at in (" : ", ": "):
        head, found, rest = stripped.partition(sep_at)
        if found and " " not in head.strip() and len(head.strip()) <= 12:
            logfmt.info(head.strip(), rest.strip())
            return
    if stripped.lower().startswith("it started"):
        logfmt.ok("check", "the browser started")
        return
    if raw.startswith(("  ", "\t")):
        logfmt.detail(stripped, indent=4)
        return
    logfmt.info("setup", stripped)


HERE = Path(__file__).resolve().parent
DEFAULT_DIR = HERE / ".chromium"
PAYLOAD = Path("@sparticuz/chromium/bin")

# Chrome for Testing publishes real linux-arm64 builds; @sparticuz does not.
CFT_INDEX = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)
CFT_BASE = "https://storage.googleapis.com/chrome-for-testing-public"
CFT_FALLBACK = "156.0.8078.4"  # used when the index cannot be fetched

# a missing .so, and the Debian/Ubuntu package that provides it
SO_PACKAGES = {
    "libnss3.so": ("libnss3",),
    "libnssutil3.so": ("libnss3",),
    "libsmime3.so": ("libnss3",),
    "libssl3.so": ("libnss3",),
    "libnspr4.so": ("libnspr4",),
    "libplc4.so": ("libnspr4",),
    "libplds4.so": ("libnspr4",),
    "libatk-1.0.so.0": ("libatk1.0-0t64", "libatk1.0-0"),
    "libatk-bridge-2.0.so.0": ("libatk-bridge2.0-0t64", "libatk-bridge2.0-0"),
    "libatspi.so.0": ("libatspi2.0-0t64", "libatspi2.0-0"),
    "libcups.so.2": ("libcups2t64", "libcups2"),
    "libdrm.so.2": ("libdrm2",),
    "libgbm.so.1": ("libgbm1",),
    "libxkbcommon.so.0": ("libxkbcommon0",),
    "libxcomposite.so.1": ("libxcomposite1",),
    "libXdamage.so.1": ("libxdamage1",),
    "libxdamage.so.1": ("libxdamage1",),
    "libXcomposite.so.1": ("libxcomposite1",),
    "libXfixes.so.3": ("libxfixes3",),
    "libXrandr.so.2": ("libxrandr2",),
    "libXtst.so.6": ("libxtst6",),
    "libXext.so.6": ("libxext6",),
    "libXrender.so.1": ("libxrender1",),
    "libXau.so.6": ("libxau6",),
    "libXdmcp.so.6": ("libxdmcp6",),
    "libXi.so.6": ("libxi6",),
    "libxfixes.so.3": ("libxfixes3",),
    "libxrandr.so.2": ("libxrandr2",),
    "libpango-1.0.so.0": ("libpango-1.0-0",),
    "libcairo.so.2": ("libcairo2",),
    "libasound.so.2": ("libasound2t64", "libasound2"),
    "libdbus-1.so.3": ("libdbus-1-3",),
    "libexpat.so.1": ("libexpat1",),
    "libxcb.so.1": ("libxcb1",),
    "libX11.so.6": ("libx11-6",),
    "libX11-xcb.so.1": ("libx11-xcb1",),
    "libglib-2.0.so.0": ("libglib2.0-0t64", "libglib2.0-0"),
    "libavahi-common.so.3": ("libavahi-common3", "libavahi-common3t64"),
    "libavahi-client.so.3": ("libavahi-client3", "libavahi-client3t64"),
}


def host_arch() -> str:
    """'x86_64' or 'aarch64' - the CPU this container actually has."""
    import platform

    m = (platform.machine() or "").lower()
    if m in ("amd64", "x86_64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    return m or "unknown"


def cft_platform() -> str:
    """Chrome for Testing's directory name for this CPU."""
    return "linux-arm64" if host_arch() == "aarch64" else "linux64"


def cft_version(pinned: str | None = None) -> str:
    """Newest Stable from the CfT index, or a pinned/fallback version."""
    if pinned:
        return pinned
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(CFT_INDEX, timeout=20) as rsp:
            data = json.load(rsp)
        return data["channels"]["Stable"]["version"]
    except Exception:  # noqa: BLE001 - offline is fine, we have a fallback
        return CFT_FALLBACK


def extract_so_names(text: str) -> list[str]:
    """Pull 'libfoo.so.N' out of a loader error, in the order it complained."""
    import re

    found: list[str] = []
    for m in re.finditer(
        r"(?:error while loading shared libraries: |"
        r"cannot open shared object file: )([\w.+-]+?\.so[\w.]*)",
        text,
    ):
        if m.group(1) not in found:
            found.append(m.group(1))
    return found


_last_test_output: list[str] = []
_already_fetched: set[tuple[str, ...]] = set()


def cft_verify(out: Path, exe: Path, *, allow_fetch: bool) -> bool:
    """Run the browser; if it needs libraries, fetch them and try again.

    Never reports success without having actually started the binary: an
    "already unpacked" file can be for another CPU, or miss a library.
    """
    for attempt in range(6):
        if cft_self_test(out, exe, auto_fetch=allow_fetch):
            return True
        missing = ldd_missing(exe, out / "lib")
        for name in extract_so_names(_last_test_output[0]):
            if name not in missing:
                missing.append(name)
        if not missing:
            return False
        if not allow_fetch:
            print()
            print("   rerun with --fetch-libs to pull them in without root:")
            print("     python chromium_setup.py --engine cft --fetch-libs")
            return False
        pkgs = {SO_PACKAGES[m] for m in missing if m in SO_PACKAGES}
        if not pkgs or pkgs <= _already_fetched:
            return False
        _already_fetched.update(pkgs)
        print(f"   (pass {attempt + 1}: {len(missing)} library file(s) to fetch)")
        fetch_libs(out, missing)
    return cft_self_test(out, exe, auto_fetch=allow_fetch)


def cloak_doctor() -> int:
    """Start the stealth chrome once and heal its missing libraries.

    The loader names one missing .so per run, so healing it error-by-error costs
    one 20 MB apt-get download round per library and a restart each time. `ldd`
    answers for all of them at once, so the first round usually finishes the job.
    """
    libdir = Path(__file__).resolve().parent / ".chromium" / "lib"
    libdir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ}
    env["LD_LIBRARY_PATH"] = f"{libdir}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(":")

    def attempt() -> tuple[bool, str, list[str]]:
        """(worked, error text, still-missing libraries)"""
        try:
            from cloakbrowser import launch as cloak_launch

            browser = _quiet(
                lambda: cloak_launch(headless=True, args=["--no-sandbox"], env=env)
            )
            page = browser.new_page()
            page.goto("about:blank")
            page.title()
            browser.close()
            return True, "", []
        except Exception as exc:  # noqa: BLE001
            text = f"{type(exc).__name__}: {exc}"
            # the loader names one library per start; ldd names them all. Union,
            # so a bare box heals in one round instead of a dozen.
            named = extract_so_names(text)
            all_missing = ldd_missing_cloak(libdir.parent)
            return False, text, sorted({*named, *all_missing})

    print("check   : starting the stealth chrome once")
    ok, text, missing = attempt()
    if ok:
        print("   it started")
        return 0
    if not missing:
        print(f"!! {text.splitlines()[0][:200]}")
        print("   (the browser started but Playwright could not drive it)")
        return 1
    print("   needs " + ", ".join(missing))
    seen = set()
    for round_no in range(1, 13):
        todo = [m for m in missing if m not in seen]
        if not todo:
            print(f"!! still failing: {text.splitlines()[0][:200]}")
            print("   no new libraries to fetch - this is not a library problem.")
            return 1
        seen.update(todo)
        print(f"libs    : round {round_no}, fetching {len(todo)} package(s)")
        fetch_libs(out_dir(), todo)
        ok, text, missing = attempt()
        if ok:
            print("   it started")
            return 0
        if missing:
            print("   still missing " + ", ".join(missing))
    print(f"!! still failing: {text.splitlines()[0][:200]}")
    return 1


def out_dir() -> Path:
    """Where a --dir-less run keeps the browser."""
    return Path(__file__).resolve().parent / ".chromium"


def _quiet(fn):
    """Call something while the library's banners are suppressed.

    cloakbrowser prints a welcome block and a font notice on stdout. They are
    not this tool's output and they land in the middle of its log.
    """
    try:
        import logfmt

        logfmt.quiet_third_party()
        with logfmt.suppress():
            return fn()
    except ImportError:
        return fn()


def cloak_importable() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("cloakbrowser") is not None
    except Exception:  # noqa: BLE001
        return False


def cloak_install(out: Path, *, allow_fetch: bool) -> int:
    """Install CloakBrowser and make its chrome runnable on this host.

    The wrapper pip-installs fine anywhere, but the chrome it downloads is a
    plain Linux build: on a bare container it needs the same libraries as any
    other Chrome, so they are fetched the same unprivileged way as for CfT.
    """
    import importlib.util
    import subprocess
    import sys

    print("engine  : CloakBrowser (stealth Chromium)")
    try:
        if importlib.util.find_spec("cloakbrowser") is None:
            print("install : pip install cloakbrowser")
            rc = subprocess.call(
                [sys.executable, "-m", "pip", "install", "-q", "cloakbrowser"]
            )
            if rc != 0:
                print("!! pip install failed - paste the error above")
                return 1
        import cloakbrowser  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"!! cloakbrowser unavailable: {exc}")
        return 1

    print("binary  : making sure the chrome is downloaded")
    try:
        from cloakbrowser.browser import ensure_binary

        exe = _quiet(ensure_binary)
        print(f"   {exe}")
    except Exception as exc:  # noqa: BLE001
        text = f"{type(exc).__name__}: {exc}"
        print(f"!! could not download the chrome: {text.splitlines()[0][:200]}")
        print("   it needs ~150 MB free in $HOME; that download is the only one.")
        return 1

    rc = cloak_doctor() if allow_fetch else cloak_check_only()
    if rc != 0:
        return rc
    # No SCRIBD_CHROMIUM here: the wrapper owns its own binary path, and pointing
    # that variable at a directory breaks resolve(). One switch is enough.
    print("\nready.  nothing to configure: CloakBrowser is the default engine.")
    return 0


def cloak_check_only() -> int:
    """Start it once, report what it would need, change nothing."""
    try:
        from cloakbrowser import launch as cloak_launch

        libdir = out_dir() / "lib"
        libdir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ}
        env["LD_LIBRARY_PATH"] = f"{libdir}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(
            ":"
        )
        browser = _quiet(
            lambda: cloak_launch(headless=True, args=["--no-sandbox"], env=env)
        )
        page = browser.new_page()
        page.goto("about:blank")
        browser.close()
        print("   it started")
        return 0
    except Exception as exc:  # noqa: BLE001
        missing = sorted({*extract_so_names(f"{exc}"), *ldd_missing_cloak(out_dir())})
        print(f"!! {type(exc).__name__}: {str(exc).splitlines()[0][:200]}")
        if missing:
            print("   needs libraries: " + ", ".join(missing))
        print("   rerun with --fetch-libs to pull them in without root")
        return 1


def ldd_missing_cloak(out: Path) -> list[str]:
    """Ask ldd about whatever chrome the wrapper downloaded."""
    import glob

    home = Path(os.path.expanduser("~/.cloakbrowser"))
    hits = sorted(glob.glob(str(home / "chromium-*" / "chrome")))
    if not hits:
        return []
    missing = ldd_missing(Path(hits[-1]), out / "lib")
    print(f"   chrome: {hits[-1]}  ({len(missing)} library file(s) missing)")
    return missing


def cft_download(
    out: Path,
    *,
    version: str | None = None,
    full: bool = False,
    force: bool = False,
    args_fetch_libs: bool = False,
) -> int:
    """Download Chrome for Testing for *this* CPU and install it into `out`.

    Uses the headless shell by default (125 MB arm64 vs 200 MB full Chrome):
    the same renderer, with far fewer X/CUPS dependencies on a bare container.
    """
    import urllib.request
    import zipfile

    exe = out / "chromium"
    if exe.exists() and not force:
        print(f"already unpacked: {exe}")
        # still verify: it may be for another CPU, or missing a library
        if cft_verify(out, exe, allow_fetch=args_fetch_libs):
            ensure_fonts(out, allow_fetch=args_fetch_libs)
            print_env(exe, out / "lib", out / "fonts")
            return 0
        print()
        print("   it is on disk but does not run. If the message above names a")
        print("   library, add --fetch-libs. If it says 'built for', the file is")
        print("   for another CPU - rerun with --force to replace it:")
        print("     python chromium_setup.py --engine cft --force --fetch-libs")
        return 1

    want = cft_version(version)
    plat = cft_platform()
    kind = "chrome" if full else "chrome-headless-shell"
    name = f"{kind}-{plat}.zip"
    url = f"{CFT_BASE}/{want}/{plat}/{name}"

    print(f"cpu     : {host_arch()} -> {plat}")
    print(f"engine  : Chrome for Testing {want} ({kind})")
    print(f"url     : {url}")

    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "_cft.zip"
    try:
        with urllib.request.urlopen(url, timeout=180) as rsp, tmp.open("wb") as fh:
            total = int(rsp.headers.get("Content-Length") or 0)
            done = 0
            step = 0
            while chunk := rsp.read(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if total and done * 100 // total >= step + 25:
                    step = done * 100 // total // 25 * 25
                    print(
                        f"   {done / 1e6:6.1f} / {total / 1e6:.1f} MB  {step:3d}%",
                        flush=True,
                    )
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        print(f"!! download failed: {exc}")
        print("   if this host cannot reach storage.googleapis.com, say so and")
        print("   we will use a different engine.")
        return 1

    print(f"unzip   : {name}")
    try:
        with zipfile.ZipFile(tmp) as zf:
            zf.extractall(out)
    except Exception as exc:  # noqa: BLE001
        print(f"!! unzip failed: {exc}")
        return 1
    finally:
        tmp.unlink(missing_ok=True)

    binary = "chrome" if full else "chrome-headless-shell"
    inner = out / f"{kind}-{plat}"
    src = inner / binary
    if not src.is_file():
        found = [c.name for c in inner.glob("*")][:6] if inner.is_dir() else []
        print(f"!! expected {src}, found {found}")
        return 1
    src.chmod(0o755)
    if exe.is_symlink() or exe.exists():
        exe.unlink()
    try:
        exe.symlink_to(src)
    except OSError:
        import shutil as _sh

        _sh.copy2(src, exe)
        exe.chmod(0o755)
    print(f"   chromium: {src.stat().st_size / 1e6:.0f} MB -> {exe}")

    if not cft_verify(out, exe, allow_fetch=args_fetch_libs):
        return 1
    ensure_fonts(out, allow_fetch=args_fetch_libs)
    print_env(exe, out / "lib", out / "fonts")
    return 0


def ldd_missing(exe: Path, libdir: Path | None = None) -> list[str]:
    """Every library the loader cannot find, in one shot.

    The loader reports one missing .so per run, so healing it error-by-error
    needs as many restarts as there are libraries. `ldd` answers the whole
    question at once (and it is a plain binary, no root needed).
    """
    import os
    import shutil
    import subprocess

    if not shutil.which("ldd"):
        return []
    env = dict(os.environ)
    if libdir and libdir.is_dir():
        env["LD_LIBRARY_PATH"] = f"{libdir}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(
            ":"
        )
    try:
        proc = subprocess.run(  # noqa: S603
            ["ldd", str(exe)], capture_output=True, text=True, timeout=120, env=env
        )
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for line in (proc.stdout or "").splitlines():
        if "not found" not in line:
            continue
        name = line.strip().split("=>")[0].strip()
        if name and name not in out:
            out.append(name)
    return out


FONT_PACKAGES = ("fonts-dejavu-core", "fonts-liberation", "fonts-dejavu")
FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/usr/share/fonts/truetype",
    os.path.expanduser("~/.fonts"),
    os.path.expanduser("~/.local/share/fonts"),
)
FONT_SUFFIXES = (".ttf", ".ttc", ".otf", ".otc", ".pfb")


def system_fonts() -> int:
    """How many usable font files this container actually has."""
    import glob

    seen = 0
    for root in FONT_DIRS:
        for suf in FONT_SUFFIXES:
            seen += len(glob.glob(f"{root}/**/*{suf}", recursive=True))
    return seen


def ensure_fonts(out: Path, *, allow_fetch: bool) -> bool:
    """A browser with no fonts paints blank pages - and a blank PDF is silent.

    A minimal container often has none. Fetch a couple of small font packages
    the same unprivileged way as the libraries and point fontconfig at them.
    """
    have = system_fonts()
    if have:
        print(f"fonts   : {have} system font file(s)")
        return True
    print("fonts   : none installed - text would render blank")
    if not allow_fetch:
        print("   rerun with --fetch-libs to pull fonts-dejavu-core in")
        return False

    fonts = out / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    stage = out / "lib" / "_fonts"
    stage.mkdir(parents=True, exist_ok=True)
    import shutil
    import subprocess

    got = 0
    for pkg in FONT_PACKAGES:
        before = set(stage.glob("*.deb"))
        proc = subprocess.run(  # noqa: S603
            ["apt-get", "download", pkg],
            cwd=str(stage),
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0 or not set(stage.glob("*.deb")) - before:
            continue
        got += 1
        if got >= 1:  # one family is enough for fallback text
            break
    if not got:
        print("   !! could not download a font package")
        return False

    unmixed = fonts / "_x"
    unmixed.mkdir(exist_ok=True)
    for deb in sorted(stage.glob("*.deb")):
        subprocess.run(  # noqa: S603
            ["dpkg", "-x", str(deb), str(unmixed)],
            capture_output=True,
            text=True,
            timeout=180,
        )
    copied = 0
    for root, _dirs, files in os.walk(unmixed):
        for name in files:
            if not name.lower().endswith(FONT_SUFFIXES):
                continue
            try:
                shutil.copy2(Path(root) / name, fonts / name)
                copied += 1
            except OSError as exc:
                print(f"   !! {name}: {exc}")
    shutil.rmtree(unmixed, ignore_errors=True)
    shutil.rmtree(stage, ignore_errors=True)
    if not copied:
        print("   !! the package had no font files")
        return False
    conf = write_fonts_conf(fonts, fonts)
    print(f"   {copied} font file(s) -> {fonts}  (fontconfig: {conf.name})")
    return True


def fetch_libs(out: Path, missing: list[str]) -> bool:
    """Fetch missing .so files into <out>/lib without root.

    `apt-get download` and `dpkg -x` both work as an unprivileged user - they
    only write files - which is exactly what a panel container allows. The
    packages come from this container's own architecture, so the same code
    works on arm64.
    """
    import shutil
    import subprocess

    wanted: list[tuple[str, ...]] = []
    for name in missing:
        cands = SO_PACKAGES.get(name)
        if cands and cands not in wanted:
            wanted.append(cands)
    unknown = [m for m in missing if m not in SO_PACKAGES]
    if unknown:
        print(f"   (no package known for: {', '.join(unknown)})")
    if not wanted:
        return False
    if not (shutil.which("apt-get") and shutil.which("dpkg")):
        print("   apt-get/dpkg are not available here - fetch these by hand:")
        print(f"     {' '.join(c[0] for c in wanted)}")
        return False

    libdir = out / "lib"
    stage = libdir / "_deb"
    stage.mkdir(parents=True, exist_ok=True)
    print(f"libs    : fetching {' '.join(c[0] for c in wanted)} (no root needed)")

    got_any = False
    for cands in wanted:
        before = set(stage.glob("*.deb"))
        landed = ""
        for pkg in cands:
            proc = subprocess.run(  # noqa: S603
                ["apt-get", "download", pkg],
                cwd=str(stage),
                capture_output=True,
                text=True,
                timeout=180,
            )
            if proc.returncode == 0 and set(stage.glob("*.deb")) - before:
                landed = pkg
                break
        if landed:
            got_any = True
            # only mention the name when it was not the first guess: distros
            # disagree about the t64 suffixes, and that is not an error
            if landed != cands[0]:
                print(f"   ok {landed} (instead of {cands[0]}, not packaged here)")
        else:
            print(f"   !! none of {' / '.join(cands)} could be downloaded")
    if not got_any:
        print("   !! nothing could be downloaded (no apt sources?)")
        return False

    unmixed = libdir / "_x"
    unmixed.mkdir(exist_ok=True)
    for deb in sorted(stage.glob("*.deb")):
        subprocess.run(  # noqa: S603
            ["dpkg", "-x", str(deb), str(unmixed)],
            capture_output=True,
            text=True,
            timeout=180,
        )

    # copy every .so in the packages: nss needs nssutil/smime/ssl from the same
    # .deb, so copying only the requested names would just move the error along
    copied = 0
    for root, _dirs, files in os.walk(unmixed):
        for name in files:
            if ".so" not in name:
                continue
            dst = libdir / name
            try:
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                shutil.copy2(Path(root) / name, dst)  # follows the symlink chain
                copied += 1
            except OSError as exc:
                print(f"   !! {name}: {exc}")
    shutil.rmtree(unmixed, ignore_errors=True)
    shutil.rmtree(stage, ignore_errors=True)
    print(f"   {copied} library file(s) -> {libdir}")
    return copied > 0


def cft_self_test(out: Path, exe: Path, auto_fetch: bool = False) -> bool:
    """Run the browser once. Report missing libraries instead of guessing."""
    import os
    import subprocess

    env = {**os.environ, "HOME": str(out)}
    libdir = out / "lib"
    if libdir.is_dir():
        env["LD_LIBRARY_PATH"] = f"{libdir}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(
            ":"
        )
    fonts = out / "fonts"
    if (fonts / "fonts.conf").is_file():
        env["FONTCONFIG_FILE"] = str(fonts / "fonts.conf")
        env.pop("FONTCONFIG_PATH", None)

    print("test    : running the browser once")
    try:
        proc = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, timeout=90, env=env
        )
    except Exception as exc:  # noqa: BLE001
        print(f"!! could not run it: {exc}")
        return False
    text = (proc.stdout or "") + (proc.stderr or "")
    _last_test_output[:] = [text]
    if proc.returncode == 0 and "Chrom" in text:
        print(f"   {text.strip().splitlines()[0]}")
        return True

    missing = extract_so_names(text)
    if missing and auto_fetch:
        # we are about to fix this ourselves - no need to scare anyone
        for name in missing:
            print(f"   needs {name}")
        return False
    if missing:
        print("!! missing shared libraries:")
        known: list[str] = []
        for name in missing:
            cands = SO_PACKAGES.get(name)
            label = cands[0] if cands else "unknown"
            print(f"     {name:28} (package: {label})")
            if cands and cands[0] not in known:
                known.append(cands[0])
        print()
        print("   On a host with root:")
        print(f"     apt-get install -y {' '.join(known)}")
        print("   Without root, pull the same packages and unpack them next to")
        print("   the browser, then point LD_LIBRARY_PATH at them:")
        print(f"     mkdir -p {out / 'lib'} && cd {out / 'lib'}")
        print(f"     apt-get download {' '.join(known)}    # or fetch from a mirror")
        print('     for f in *.deb; do dpkg -x "$f" .; done')
        print(f"     export LD_LIBRARY_PATH={out / 'lib'}/usr/lib/*:$LD_LIBRARY_PATH")
        print()
        print("   Zero-dependency alternative on any CPU: --engine obscura")
    else:
        print("!! it did not start:")
        for line in text.strip().splitlines()[:8]:
            print(f"     {line}")
    return False


BROTLI_HINT = "pip install brotli      (1 MB, pure wheel)"


def find_payload(explicit: str | None = None) -> Path:
    """Locate the @sparticuz/chromium bin directory that npm downloaded."""
    if explicit:
        path = Path(explicit)
        if path.is_dir():
            return path
        raise SystemExit(f"--from {path} is not a directory")

    env = os.environ.get("SPARTICUZ_BIN")
    if env and Path(env).is_dir():
        return Path(env)

    # walk up from here and from the cwd: node_modules may live one level up
    for start in (HERE, Path.cwd()):
        for parent in [start, *start.parents]:
            cand = parent / "node_modules" / PAYLOAD
            if cand.is_dir():
                return cand
            cand = parent / "browser-test" / "node_modules" / PAYLOAD
            if cand.is_dir():
                return cand

    raise SystemExit(
        "could not find node_modules/@sparticuz/chromium/bin\n"
        "  fix: python chromium_setup.py                  (downloads the payload)\n"
        "  or : python chromium_setup.py --from /path/to/@sparticuz/chromium/bin"
    )


REGISTRY = "https://registry.npmjs.org/@sparticuz/chromium"
PAYLOAD_FILES = ("chromium.br", "al2023.tar.br", "fonts.tar.br", "swiftshader.tar.br")


def registry_meta(version: str | None = None, timeout: int = 30) -> dict:
    """Ask the npm registry where the tarball is (no npm, no node)."""
    import requests

    url = f"{REGISTRY}/{version or 'latest'}"
    resp = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
    resp.raise_for_status()
    data = resp.json()
    return {"version": data["version"], "tarball": data["dist"]["tarball"]}


def download_payload(dest: Path, version: str | None = None, log=print) -> Path:
    """Fetch the @sparticuz tarball and extract just bin/*.br into dest."""
    import io
    import tarfile

    import requests

    meta = registry_meta(version)
    log(f"registry: version {meta['version']}")
    log(f"downloading {meta['tarball']}")
    with requests.get(meta["tarball"], stream=True, timeout=120) as resp:
        resp.raise_for_status()
        blob = resp.content
    log(f"  {len(blob) / 1e6:.0f} MB downloaded")

    dest.mkdir(parents=True, exist_ok=True)
    wanted = {f"package/bin/{name}" for name in PAYLOAD_FILES}
    found = []
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for member in tf.getmembers():
            if member.name not in wanted or not member.isfile():
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            out = dest / os.path.basename(member.name)
            out.write_bytes(fh.read())
            found.append(out.name)
    if "chromium.br" not in found:
        raise SystemExit(
            f"the tarball did not contain chromium.br (found: {found or 'nothing'})"
        )
    log(f"  payload: {', '.join(found)}")
    return dest


def ensure_payload(explicit: str | None, version: str | None = None, log=print) -> Path:
    """Find the payload, or download it from the registry if npm was never run."""
    try:
        return find_payload(explicit)
    except SystemExit:
        if explicit:
            raise
    log("no local payload - downloading from the npm registry (no npm needed)")
    cache = Path(os.environ.get("SPARTICUZ_CACHE") or DEFAULT_DIR / "_payload")
    return download_payload(cache, version=version, log=log)


def _brotli_module():
    try:
        import brotli  # type: ignore

        return brotli
    except ImportError:
        pass
    try:
        import brotlicffi as brotli  # type: ignore

        return brotli
    except ImportError:
        raise SystemExit(
            "this needs a brotli decompressor:\n  " + BROTLI_HINT
        ) from None


def decompress(src: Path, dest: Path, brotli) -> int:
    """brotli-decompress one file, streaming so 200 MB does not sit in RAM."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    dec = brotli.Decompressor()
    # the API differs between bindings: Brotli -> decompress(), brotli -> process()
    step = getattr(dec, "decompress", None) or dec.process
    with src.open("rb") as fin, dest.open("wb") as fout:
        while True:
            chunk = fin.read(1 << 20)
            if not chunk:
                break
            out = step(chunk)
            if out:
                fout.write(out)
                total += len(out)
        finish = getattr(dec, "finish", None)
        if finish is not None:
            tail = finish()
            if tail:
                fout.write(tail)
                total += len(tail)
    return total


def extract_tar_br(src: Path, dest: Path, brotli, label: str) -> None:
    """brotli -> tar -> dest, streaming through a temp file."""
    import tempfile

    with tempfile.TemporaryDirectory(dir=str(dest.parent)) as tmp:
        tarball = Path(tmp) / "payload.tar"
        size = decompress(src, tarball, brotli)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tarball) as tf:
            members = [m for m in tf.getmembers() if not m.name.startswith(("/", ".."))]
            tf.extractall(dest, members=members)  # noqa: S202 - members filtered
        print(f"   {label}: {size / 1e6:.0f} MB -> {dest}")


def write_fonts_conf(fonts_dir: Path, out_dir: Path) -> Path:
    """Rewrite fonts.conf for *this* machine.

    The shipped file points at Lambda paths (/var/task/.fonts, /tmp/fonts). On a
    normal host fontconfig finds no font, Skia hits an unimplemented path and the
    browser dies with SIGTRAP the moment a page needs to render text - which is
    every real page. Point it at the directory we actually extracted.
    """
    conf = out_dir / "fonts.conf"
    cache = out_dir / "fonts-cache"
    cache.mkdir(parents=True, exist_ok=True)
    conf.write_text(
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
        "<fontconfig>\n"
        f"  <dir>{fonts_dir}</dir>\n"
        f"  <cachedir>{cache}</cachedir>\n"
        "  <config></config>\n"
        "</fontconfig>\n",
        encoding="utf-8",
    )
    return conf


def free_mb(path: Path) -> int:
    try:
        st = os.statvfs(path)
        return int(st.f_bavail * st.f_frsize / 1e6)
    except Exception:
        return -1


def main(argv: list[str] | None = None) -> int:
    try:
        import logfmt

        logfmt.setup()
        logfmt.quiet_third_party()
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(
        prog="chromium_setup.py",
        description="Unpack @sparticuz/chromium for use with Python Playwright.",
    )
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="where to unpack")
    ap.add_argument("--from", dest="src", default=None, help="the @sparticuz bin dir")
    ap.add_argument("--force", action="store_true", help="re-extract even if present")
    ap.add_argument(
        "--version",
        default=None,
        help="pin the @sparticuz/chromium version (default: whatever the registry "
        "has as 'latest'); ignored when node_modules is present",
    )
    ap.add_argument(
        "--engine",
        choices=("auto", "sparticuz", "cft", "cloak"),
        default="auto",
        help="auto picks sparticuz on x86-64 (self-contained libraries) and "
        "Chrome for Testing on ARM64 (sparticuz has no arm64 build)",
    )
    ap.add_argument(
        "--fetch-libs",
        action="store_true",
        help="if the browser needs libraries that are missing, download them "
        "with apt-get download + dpkg -x (works without root)",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="download the complete Chrome instead of the headless shell "
        "(cft engine only, ~75 MB more)",
    )
    ap.add_argument(
        "--no-download",
        action="store_true",
        help="never touch the network; fail if node_modules is missing",
    )
    ap.add_argument(
        "--print-env",
        action="store_true",
        help="only print the export lines (for eval/CI)",
    )
    args = ap.parse_args(argv)

    out = Path(args.dir).expanduser().resolve()
    exe = out / "chromium"
    libdir = out / "al2023" / "lib"
    fontdir = out / "fonts"

    engine = args.engine
    if engine == "auto":
        if cloak_importable():
            # the default engine: it is the only one that clears Scribd's check
            engine = "cloak"
        else:
            # cloakbrowser is a normal requirement now, so this is the odd case:
            # a box mid-upgrade. Provision something runnable for this run.
            # (@sparticuz is x86-64: on an ARM panel it unpacks happily and
            # then dies with "Exec format error".)
            engine = "cft" if host_arch() == "aarch64" else "sparticuz"
            print(f"engine  : {engine} (cloakbrowser not importable)")
    if engine == "cloak":
        return cloak_install(out, allow_fetch=args.fetch_libs)
    if engine == "cft":
        return cft_download(
            out,
            version=args.version,
            full=args.full,
            force=args.force,
            args_fetch_libs=args.fetch_libs,
        )
    if host_arch() == "aarch64" and engine == "sparticuz":
        print(
            "!! @sparticuz/chromium ships an x86-64 binary only and this is "
            f"{host_arch()}.\n   use --engine cft (Chrome for Testing has arm64 "
            "builds), or --engine obscura.",
            file=sys.stderr,
        )
        return 1

    if exe.exists() and not args.force:
        print(f"already unpacked: {exe} ({exe.stat().st_size / 1e6:.0f} MB)")
        # repair mode: an older run may have left the Lambda fontconfig in place
        if (out / "fonts" / "fonts").is_dir():
            conf = write_fonts_conf(out / "fonts" / "fonts", out / "fonts")
            print(f"   fontconfig: {conf}")
        print_env(exe, libdir, fontdir)
        return 0

    need = 500  # 200 MB binary + ~85 MB tarball + temp copy + slack
    have = free_mb(out.parent if out.parent.exists() else HERE)
    print(f"free    : {have} MB (need ~{need} MB)")
    if 0 <= have < need:
        print(
            "!! not enough space. the extracted browser is ~196 MB, the download is\n"
            "   ~80 MB and decompressing needs a temp copy while it works.\n"
            "   free some space, or use --dir to unpack onto a bigger volume.",
            file=sys.stderr,
        )
        return 1

    if args.no_download:
        bin_dir = find_payload(args.src)
    else:
        bin_dir = ensure_payload(args.src, version=args.version)
    print(f"payload : {bin_dir}")

    brotli = _brotli_module()
    out.mkdir(parents=True, exist_ok=True)

    print("-> chromium")
    size = decompress(bin_dir / "chromium.br", exe, brotli)
    exe.chmod(0o755)
    print(f"   chromium: {size / 1e6:.0f} MB -> {exe}")

    for name, label in (
        ("al2023.tar.br", "libraries"),
        ("fonts.tar.br", "fonts"),
        ("swiftshader.tar.br", "swiftshader"),
    ):
        src = bin_dir / name
        if not src.exists():
            print(f"   {label}: not shipped ({name}), skipping")
            continue
        target = out / ("al2023" if name.startswith("al2023") else name.split(".")[0])
        extract_tar_br(src, target, brotli, label)

    # the extracted fonts need a config that matches this host
    font_root = out / "fonts"
    if (font_root / "fonts").is_dir():
        conf = write_fonts_conf(font_root / "fonts", font_root)
        print(f"   fontconfig: {conf}")
    else:
        print("   fontconfig: no fonts extracted - pages with text may crash")

    print()
    print(f"ready: {exe}")
    print_env(exe, libdir, fontdir)
    return 0


def print_env(exe: Path, libdir: Path, fontdir: Path) -> None:
    libs = f"{libdir}:" if libdir.is_dir() else ""
    conf = fontdir / "fonts.conf"
    print("\nexport these (or pass --chromium-path):")
    print(f"  export SCRIBD_CHROMIUM={exe}")
    if libs:
        print(f'  export LD_LIBRARY_PATH="{libdir}:$LD_LIBRARY_PATH"')
    if conf.is_file():
        print(f"  export FONTCONFIG_FILE={conf}")
    print(f"  export HOME={exe.parent}")


if __name__ == "__main__":
    raise SystemExit(main())
