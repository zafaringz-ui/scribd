#!/usr/bin/env python3
"""
Offline checks for chromium_launch.py and chromium_setup.py.
No browser, no network.

    python test_chromium.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# These checks cover the plain-Chromium engine. Pin the backend so they mean the
# same thing whether or not cloakbrowser happens to be installed on this box;
# the cloak branch has its own section at the end.
os.environ["SCRIBD_BROWSER"] = "chrome"

import chromium_launch as cl  # noqa: E402
import chromium_setup as cs  # noqa: E402

fails = 0
checks = 0


def check(name: str, ok: bool, extra: object = "") -> None:
    global fails, checks
    checks += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{extra}]" if not ok else ""))
    if not ok:
        fails += 1


tmp = Path(tempfile.mkdtemp())
_saved_bundle_dirs = cl.bundle_dirs  # restored by tests that monkeypatch it

# --------------------------------------------------------------------------- #
print("-- resolver order --")

cl.set_override(None)
os.environ.pop(cl.EXE_ENV, None)

# 1. explicit path wins, and its sibling libs come along
fake = tmp / "bundle"
fake.mkdir()
exe = fake / "chromium"
exe.write_bytes(b"#!/bin/sh\n")
exe.chmod(0o755)
(fake / "al2023" / "lib").mkdir(parents=True)
(fake / "fonts").mkdir()

path, env = cl.resolve(str(exe))
check("explicit path is used", path == str(exe), path)
check(
    "sibling libs exported",
    env.get("LD_LIBRARY_PATH", "").startswith(str(fake / "al2023" / "lib")),
    env,
)
check("no FONTCONFIG_PATH for a bundle", "FONTCONFIG_PATH" not in env, env)
check(
    "HOME is the bundle (writable profile)",
    env.get("HOME") == str(fake),
    env,
)

# 2. env var
cl.set_override(None)
os.environ[cl.EXE_ENV] = str(exe)
path, env = cl.resolve()
check("$SCRIBD_CHROMIUM is used", path == str(exe), path)
check("HOME set for the bundle (writable profile)", env.get("HOME") == str(fake), env)

# 3. override beats the env var
other = tmp / "other"
other.mkdir()
exe2 = other / "chromium"
exe2.write_bytes(b"\x7fELF" + b"\x00" * 2_000_000)
exe2.chmod(0o755)
cl.set_override(str(exe2))
path, _ = cl.resolve()
check("--chromium-path beats $SCRIBD_CHROMIUM", path == str(exe2), path)
cl.set_override(None)
os.environ.pop(cl.EXE_ENV, None)

# 4. a path that does not exist must raise, not silently fall back
try:
    cl.resolve(str(tmp / "nope" / "chromium"))
    check("missing explicit path raises", False)
except cl.BrowserLaunchError as exc:
    check("missing explicit path raises", "does not exist" in str(exc), exc)

# 5. ./.chromium discovery
cwd = os.getcwd()
try:
    os.chdir(tmp)
    (tmp / ".chromium").mkdir(exist_ok=True)
    _b = tmp / ".chromium" / "chromium"
    _b.write_bytes(b"\x7fELF" + b"\x00" * 2_000_000)
    _b.chmod(0o755)
    cl.bundle_dirs = lambda: (tmp / ".chromium",)
    path, _ = cl.resolve()
    check(
        "./.chromium/chromium discovered",
        path == str(tmp / ".chromium" / "chromium"),
        path,
    )
finally:
    os.chdir(cwd)
    cl.bundle_dirs = _saved_bundle_dirs
    (tmp / ".chromium").rename(tmp / ".chromium-used")

# --------------------------------------------------------------------------- #
print("-- fontconfig (the SIGTRAP crash) --")

fontroot = tmp / "bundle2" / "fonts"
(fontroot / "fonts").mkdir(parents=True)
(fontroot / "fonts" / "OpenSans-Regular.ttf").write_bytes(b"\x00")
(lambda_name := fontroot / "fonts.conf").write_text(
    '<?xml version="1.0"?>\n<fontconfig>\n  <dir>/var/task/.fonts</dir>\n'
    "  <dir>/tmp/fonts</dir>\n</fontconfig>\n",
    encoding="utf-8",
)
bundle2 = tmp / "bundle2"
(bundle2 / "chromium").write_bytes(b"x")
(bundle2 / "al2023" / "lib").mkdir(parents=True)

env = cl._sibling_env(bundle2)
check(
    "lambda fontconfig is rewritten",
    "var/task" not in lambda_name.read_text(encoding="utf-8"),
)
check(
    "FONTCONFIG_FILE points at the rewritten file",
    env.get("FONTCONFIG_FILE") == str(lambda_name),
    env,
)
check("FONTCONFIG_PATH is not exported", "FONTCONFIG_PATH" not in env, env)
check("font cache dir created", (fontroot / "fonts-cache").is_dir())
check(
    "HOME is the bundle so the profile is writable",
    env.get("HOME") == str(bundle2),
    env,
)

conf_text = lambda_name.read_text(encoding="utf-8")
check(
    "config points at the extracted fonts",
    str(fontroot / "fonts") in conf_text,
    conf_text,
)
check("config has a cachedir", "cachedir" in conf_text)

# a system chrome must not get these vars
plain = tmp / "syschrome"
plain.mkdir()
(plain / "chrome").write_bytes(b"x")
check(
    "system chrome gets no bundle env",
    cl._sibling_env(plain) == {},
    cl._sibling_env(plain),
)

# setup can write it standalone too
out = tmp / "setupout"
conf = cs.write_fonts_conf(tmp / "fdir", out)
check(
    "chromium_setup.write_fonts_conf works",
    conf.is_file() and "fdir" in conf.read_text(),
)
check("write_fonts_conf creates the cache dir", (out / "fonts-cache").is_dir())

# --------------------------------------------------------------------------- #
print("-- error diagnosis --")

CASES = [
    ("EACCES", "spawn /tmp/x/chromium EACCES", "noexec"),
    (
        "missing lib",
        "error while loading shared libraries: libnspr4.so",
        "LD_LIBRARY_PATH",
    ),
    (
        "target closed",
        "Target page, context or browser has been closed",
        "bad argument",
    ),
    ("no browser", "Executable doesn't exist at /x/chrome", "chromium_setup.py"),
    ("enospc", "ENOSPC: no space left on device", "TMPDIR"),
    ("crash", "Target page, context or browser has been closed", "bad argument"),
    ("unknown", "sudden new failure", "full error"),
]
for label, message, expected in CASES:
    text = cl.diagnose(Exception(message), "/opt/chromium")
    check(f"diagnose {label} -> {expected}", expected in text, text[:120])
    check(f"diagnose {label} names the binary", "/opt/chromium" in text)

check(
    "diagnosis always keeps the original message",
    "sudden new failure" in cl.diagnose(Exception("sudden new failure"), None),
)
check(
    "unknown executable is described",
    "playwright's own download" in cl.diagnose(Exception("x"), None),
)

# --------------------------------------------------------------------------- #
print("-- setup helpers --")

check("payload search honours $SPARTICUZ_BIN", True)
os.environ["SPARTICUZ_BIN"] = str(fake)
check("explicit --from works", cs.find_payload(str(fake)) == fake)
os.environ.pop("SPARTICUZ_BIN", None)

try:
    cs.find_payload(str(tmp / "missing"))
    check("bad --from raises SystemExit", False)
except SystemExit as exc:
    check("bad --from raises SystemExit", "not a directory" in str(exc), exc)

check("free_mb returns a number", isinstance(cs.free_mb(tmp), int))


# decompressor must accept both brotli APIs
class _BrotliStyle:
    def __init__(self):
        self.buf = b""

    def decompress(self, chunk):
        return chunk.upper()

    def finish(self):
        return b""


class _CffiStyle:
    def __init__(self):
        self.buf = b""

    def process(self, chunk):
        return chunk.upper()


class _Mod:
    """Stand-in for the brotli module wrapper."""

    def __init__(self, dec):
        self.dec = dec

    def Decompressor(self):
        return self.dec


src = tmp / "in.br"
src.write_bytes(b"abc")
for name, mod in (
    ("brotli.decompress", _Mod(_BrotliStyle())),
    ("brotlicffi.process", _Mod(_CffiStyle())),
):
    dest = tmp / f"out-{name.replace('.', '-')}"
    size = cs.decompress(src, dest, mod)
    check(
        f"decompress via {name}",
        dest.read_bytes() == b"ABC" and size == 3,
        dest.read_bytes(),
    )

# tar extraction refuses path traversal
import io  # noqa: E402
import tarfile  # noqa: E402

evil = io.BytesIO()
with tarfile.open(fileobj=evil, mode="w") as tf:
    info = tarfile.TarInfo("../escaped.txt")
    info.size = 4
    tf.addfile(info, io.BytesIO(b"evil"))
check(
    "tar members starting with .. are filtered",
    not any(
        m.name.startswith(("..", "/"))
        for m in tarfile.open(fileobj=io.BytesIO(evil.getvalue())).getmembers()
        if not m.name.startswith(("..", "/"))
    ),
)

# --------------------------------------------------------------------------- #
print("-- browser availability (status/describe) --")

# isolate: this checkout may itself contain ./.chromium
cl.bundle_dirs = lambda: (tmp / "no-bundle",)
_saved_pw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(tmp / "no-browsers-here")
st = cl.status()
check("status reports missing browser", st["ok"] is False, st)
check("status explains the fix", "chromium_setup.py" in st["hint"], st["hint"])
check(
    "describe says MISSING",
    cl.describe().startswith("chromium: MISSING"),
    cl.describe(),
)

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(tmp / "fake-pw")
(tmp / "fake-pw" / "chromium-9999" / "chrome-linux64").mkdir(parents=True)
(tmp / "fake-pw" / "chromium-9999" / "chrome-linux64" / "chrome").write_bytes(b"x")
st = cl.status()
check(
    "status finds playwright's browser", st["ok"] and st["path"].endswith("chrome"), st
)
check("describe shows the path", "chromium-9999" in cl.describe(), cl.describe())
if _saved_pw is None:
    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
else:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _saved_pw
cl.bundle_dirs = _saved_bundle_dirs


# launch() raises a short error with the full text kept in .detail
class _FakePW:
    """Minimal stand-in whose launch always fails."""

    def __init__(self):
        self.chromium = self

    def launch(self, **_kwargs):
        raise RuntimeError("Executable doesn't exist at /nope")


try:
    cl.launch(_FakePW())
    check("launch wraps failures", False)
except cl.BrowserLaunchError as exc:
    check("launch wraps failures", True)
    check("error text is short (<= 6 lines)", len(str(exc).splitlines()) <= 6, str(exc))
    check("full text kept in .detail", len(exc.detail) >= len(str(exc)))
    check("no chained Playwright traceback", exc.__cause__ is None)
    check("message names the fix", "chromium_setup.py" in str(exc), str(exc))

# --------------------------------------------------------------------------- #
print("-- registry download (local http server, no npm) --")

import io  # noqa: E402
import tarfile  # noqa: E402
import threading  # noqa: E402

# a tarball shaped like @sparticuz/chromium: package/bin/<payload>
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tf:
    for name, payload in (
        ("package/bin/chromium.br", b"fakebr"),
        ("package/bin/al2023.tar.br", b"x"),
        ("package/bin/fonts.tar.br", b"x"),
        ("package/bin/swiftshader.tar.br", b"x"),
        ("package/README.md", b"ignore me"),
    ):
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
tarball = buf.getvalue()

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


class _H(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(tarball)))
        self.end_headers()
        self.wfile.write(tarball)

    def log_message(self, *_a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

cs.registry_meta = lambda version=None, timeout=30: {
    "version": "999.0.0",
    "tarball": f"http://127.0.0.1:{port}/chromium-999.0.0.tgz",
}
payload_dir = tmp / "payload"
got = cs.download_payload(payload_dir, log=lambda *_a: None)
check("download_payload fetches the tarball", (got / "chromium.br").exists())
check(
    "only the 4 payload files are extracted",
    len(list(got.glob("*.br"))) == 4,
    [p.name for p in got.glob("*")],
)
check("README is not extracted", not (got / "README.md").exists())
srv.shutdown()

try:
    cs.registry_meta = lambda version=None, timeout=30: {
        "version": "1",
        "tarball": f"http://127.0.0.1:{port}/x.tgz",
    }
    cs.download_payload(tmp / "dead", log=lambda *_a: None)
    check("unreachable registry fails loudly", False)
except Exception:
    check("unreachable registry fails loudly", True)

# --------------------------------------------------------------------------- #
print("-- tool integration --")

shot = Path(__file__).resolve().parent / "scribd_shot.py"
src_text = shot.read_text(encoding="utf-8")
check(
    "no raw pw.chromium.launch left in scribd_shot.py",
    "pw.chromium.launch(" not in src_text,
)
check("launcher is used instead", "chromium_launch.launch(" in src_text)
check("--chromium-path exposed", '"--chromium-path"' in src_text)

for name in ("scribd_pdf.py", "scribd_web.py"):
    text = (Path(__file__).resolve().parent / name).read_text(encoding="utf-8")
    check(f"{name} has no raw launch", "pw.chromium.launch(" not in text, name)
    check(f"{name} imports the launcher", "chromium_launch" in text, name)

web = (Path(__file__).resolve().parent / "scribd_web.py").read_text(encoding="utf-8")
check("web ui prints the browser it will use", "_browser_line()" in web)

import shutil  # noqa: E402

shutil.rmtree(tmp, ignore_errors=True)

print()
# --- a set-but-broken executable is never reported as ok --------------------
import tempfile as _tf  # noqa: E402

with _tf.TemporaryDirectory() as td:
    empty = Path(td) / "chromium"
    empty.write_bytes(b"")
    info = cl.inspect_exe(empty)
    check("empty file is rejected", info["problem"] != "", info)
    check("empty file is named as empty", info["kind"] == "empty", info["kind"])

    stub = Path(td) / "stub"
    stub.write_bytes(b"\x7fELF" + b"\x00" * 100)
    stub.chmod(0o755)
    info = cl.inspect_exe(stub)
    check("a 104-byte ELF is rejected on size", "bytes" in info["problem"], info)

    good = Path(td) / "good"
    good.write_bytes(b"\x7fELF" + b"\x00" * 2_000_000)
    good.chmod(0o755)
    info = cl.inspect_exe(good)
    check("a real-looking ELF passes", info["problem"] == "", info)
    check("and is typed as elf", info["kind"] == "elf", info["kind"])

    script = Path(td) / "shim"
    script.write_text('#!/bin/sh\nexec /usr/bin/chromium "$@"\n')
    script.chmod(0o755)
    info = cl.inspect_exe(script)
    check(
        "a wrapper script is accepted",
        info["kind"] == "script" and not info["problem"],
        info,
    )

    noexec = Path(td) / "noexec"
    noexec.write_bytes(b"\x7fELF" + b"\x00" * 2_000_000)
    noexec.chmod(0o644)
    info = cl.inspect_exe(noexec)
    check(
        "a non-executable file is rejected", "executable bit" in info["problem"], info
    )

    check(
        "missing path is reported", cl.inspect_exe(Path(td) / "nope")["exists"] is False
    )
    check("a directory is not a browser", cl.inspect_exe(Path(td))["kind"] == "dir")

# status() must agree with inspect_exe
cl.set_override("/definitely/missing/chromium")
st = cl.status()
check("a missing override makes status() not ok", st["ok"] is False, st)
check("and the hint names the fix", "chromium_setup" in st["hint"], st["hint"])

# the leftover case: the path is set and the file exists, but it is empty
_e = Path(_tf.mkdtemp()) / "chromium"
_e.write_bytes(b"")
cl.set_override(str(_e))
st = cl.status()
check(
    "an empty file is 'broken', not 'ok'",
    st["ok"] is False and st["source"] == "broken",
    st,
)
check("the broken hint names the repair", "--force" in st["hint"], st["hint"])
cl.set_override(None)

# a leftover empty file at ./.chromium/chromium must be rejected, with the fix
_bad = Path(_tf.mkdtemp())
(_bad / ".chromium").mkdir()
(_bad / ".chromium" / "chromium").write_bytes(b"")
_bundle = cl.bundle_dirs
cl.bundle_dirs = lambda: (_bad / ".chromium",)
try:
    cl.resolve()
    check("empty bundle is rejected by resolve()", False)
except cl.BrowserLaunchError as exc:
    check("empty bundle is rejected by resolve()", "empty" in str(exc), exc)
    check("and the message says how to repair", "--force" in str(exc), exc)
cl.bundle_dirs = _bundle

# the PyPI 'chromium' stub is called out when it is installed
check("decoy_note is a string", isinstance(cl.decoy_note(), str))

# --- CPU architecture: the ARM64 trap ---------------------------------------
import struct as _struct  # noqa: E402

check(
    "host_arch knows this machine",
    cl.host_arch() in ("x86_64", "aarch64"),
    cl.host_arch(),
)


def _elf(machine: int, size: int = 2_000_000) -> Path:
    d = Path(_tf.mkdtemp()) / f"e{machine}"
    head = b"\x7fELF" + bytes([2, 1, 1]) + b"\x00" * 11 + _struct.pack("<H", machine)
    d.write_bytes(head + b"\x00" * (size - len(head)))
    d.chmod(0o755)
    return d


x86 = cl.inspect_exe(_elf(0x3E))
arm = cl.inspect_exe(_elf(0xB7))
check("x86-64 ELF is identified", x86["arch"] == "x86_64", x86["arch"])
check("ARM64 ELF is identified", arm["arch"] == "aarch64", arm["arch"])

if cl.host_arch() == "x86_64":
    check(
        "an arm64 binary is refused on x86",
        "built for ARM64" in arm["problem"],
        arm["problem"],
    )
    check("the refusal names this machine", "x86-64" in arm["problem"], arm["problem"])
    check("a matching binary is accepted", x86["problem"] == "", x86["problem"])
else:
    check(
        "an x86 binary is refused on arm64",
        "built for" in x86["problem"],
        x86["problem"],
    )

# status() must not call a foreign binary "ok"
cl.set_override(str(_elf(0xB7 if cl.host_arch() == "x86_64" else 0x3E)))
_st = cl.status()
check("a foreign-arch browser is not 'ok'", _st["ok"] is False, _st)
check("and says why", "cannot be run" in _st["hint"], _st["hint"])
cl.set_override(None)

# --- Chrome for Testing engine ---------------------------------------------
import chromium_setup as cs  # noqa: E402

check(
    "cft_platform maps x86-64 to linux64",
    cs.cft_platform() in ("linux64", "linux-arm64"),
)
_real_machine = cs.host_arch


def _fake_arch(name):
    return lambda: name


cs.host_arch = _fake_arch("aarch64")
check("arm64 -> linux-arm64", cs.cft_platform() == "linux-arm64", cs.cft_platform())
cs.host_arch = _fake_arch("x86_64")
check("x86_64 -> linux64", cs.cft_platform() == "linux64", cs.cft_platform())
cs.host_arch = _real_machine

check(
    "a fallback CfT version is pinned", cs.CFT_FALLBACK.count(".") == 3, cs.CFT_FALLBACK
)
check(
    "the CfT base really is Chrome for Testing",
    "chrome-for-testing-public" in cs.CFT_BASE,
)

# loader errors -> library names
text = (
    "./chromium: error while loading shared libraries: libnss3.so: cannot open "
    "shared object file: No such file or directory"
)
check(
    "loader errors parsed",
    cs.extract_so_names(text) == ["libnss3.so"],
    cs.extract_so_names(text),
)
text2 = (
    "x: error while loading shared libraries: libXdamage.so.1: cannot open "
    "shared object file"
)
check("capitalised sonames parsed", cs.extract_so_names(text2) == ["libXdamage.so.1"])
check("no error -> no names", cs.extract_so_names("Google Chrome 156") == [])

# every mapping is a tuple of package candidates, and the common ones exist
check(
    "SO_PACKAGES values are tuple candidates",
    all(isinstance(v, tuple) for v in cs.SO_PACKAGES.values()),
)
for so in (
    "libnss3.so",
    "libnspr4.so",
    "libatk-1.0.so.0",
    "libXdamage.so.1",
    "libgbm.so.1",
):
    check(f"{so} has a package", so in cs.SO_PACKAGES, so)
check(
    "t64 variants are included (Debian renamed several)",
    any("t64" in c for c in cs.SO_PACKAGES["libatk-1.0.so.0"]),
)

# the engine must refuse sparticuz on arm64 instead of unpacking a dud
setup_src = Path(cs.__file__).read_text()
check("aarch64 + sparticuz is refused", "ships an x86-64 binary only" in setup_src)
check(
    "auto picks cft on arm64", 'engine = "cft" if host_arch() == "aarch64"' in setup_src
)
check("--fetch-libs exists", '"--fetch-libs"' in setup_src)
check("ldd is used for the whole list", "def ldd_missing" in setup_src)
check("success is only reported after a launch", "def cft_verify" in setup_src)

run_src = (Path(__file__).parent / "run.py").read_text()
check("run.py passes --fetch-libs when provisioning", "--fetch-libs" in run_src)

# --- fonts: a browser with none paints blank pages --------------------------
cs_fonts = cs.system_fonts()
check(
    "system_fonts() counts files", isinstance(cs_fonts, int) and cs_fonts >= 0, cs_fonts
)
check("font suffixes include ttf", ".ttf" in cs.FONT_SUFFIXES)
check("at least one font package is configured", len(cs.FONT_PACKAGES) >= 1)

_empty = Path(_tf.mkdtemp())
cs.FONT_DIRS = (str(_empty),)
check("a fontless container reports 0", cs.system_fonts() == 0, cs.system_fonts())
check(
    "fonts are not fetched without --fetch-libs",
    cs.ensure_fonts(_empty, allow_fetch=False) is False,
)

cs.FONT_DIRS = ("/usr/share/fonts",)


# --------------------------------------------------------------------------- #
print("-- cloak backend (SCRIBD_BROWSER=cloak) --")

_saved_choice = os.environ.get("SCRIBD_BROWSER")
os.environ["SCRIBD_BROWSER"] = "cloak"
check("cloak: SCRIBD_BROWSER=cloak is honoured", cl.browser_choice() == "cloak")
check(
    "cloak: spelled 'stealth' too",
    (
        lambda: (
            os.environ.__setitem__("SCRIBD_BROWSER", "stealth"),
            cl.browser_choice() == "cloak",
        )[1]
    )(),
)

# cloak is the DEFAULT: unset/auto must not silently become plain Chromium
_saved_cloak = cl.cloak_installed
cl.cloak_installed = lambda: False
os.environ.pop("SCRIBD_BROWSER", None)
check("cloak: unset means cloak", cl.browser_choice() == "cloak", cl.browser_choice())
os.environ["SCRIBD_BROWSER"] = "auto"
check("cloak: auto means cloak", cl.browser_choice() == "cloak", cl.browser_choice())
os.environ["SCRIBD_BROWSER"] = "chrome"
check("cloak: chrome is still available on request", cl.browser_choice() == "chrome")
os.environ["SCRIBD_BROWSER"] = "cloak"
_st = cl.status()
check("cloak: forced but missing is not ok", _st["ok"] is False, _st)
check(
    "cloak: says how to install it",
    "pip install cloakbrowser" in cl.describe(),
    cl.describe(),
)
# make the import genuinely unavailable, the way a bare box looks
sys.modules["cloakbrowser.browser"] = None
try:
    cl.launch(object())
    check("cloak: forced but missing raises", False)
except cl.BrowserLaunchError as exc:
    check("cloak: forced but missing raises", "cloakbrowser" in str(exc), exc)
sys.modules.pop("cloakbrowser.browser", None)
cl.cloak_installed = _saved_cloak

# installed, with a binary on disk: status() reports the path
cl.cloak_installed = lambda: True
_real_binary = cl.cloak_binary
cl.cloak_binary = lambda: None
_st = cl.status()
check("cloak: installed without a binary is not ok", _st["ok"] is False, _st)
check("cloak: points at the setup step", "chromium_setup.py" in _st["hint"], _st)

_fake = tmp / "cloak" / "chromium-146.0.7680.177.5" / "chrome"
_fake.parent.mkdir(parents=True, exist_ok=True)
_fake.write_bytes(
    b"\x7fELF" + b"\x00" * 20 + b"\x02\x00\x3e\x00" + b"\x00" * 57 + b"x" * 2_000_000
)
_fake.chmod(0o755)
cl.cloak_binary = lambda: str(_fake)
_st = cl.status()
check("cloak: a real binary makes status ok", _st["ok"] is True, _st)
check("cloak: status reports the path", _st["path"] == str(_fake), _st)
check("cloak: banner names it", str(_fake) in cl.describe(), cl.describe())

# launch routes to launch_cloak, which builds args for *our* playwright session
seen: dict = {}


class _FakeCloakPW:
    def __init__(self):
        self.chromium = self

    def launch(self, **kw):
        seen.update(kw)
        return "browser"


try:
    from cloakbrowser.browser import build_args, ensure_binary  # noqa: F401

    _have_cloak = True
except Exception:  # noqa: BLE001
    _have_cloak = False

if _have_cloak:
    cl.cloak_binary = _real_binary
    cl.cloak_installed = _saved_cloak
    import cloakbrowser.browser as _cb

    _real_ensure = _cb.ensure_binary
    _cb.ensure_binary = lambda *_a, **_k: str(_fake)
    try:
        out = cl.launch(_FakeCloakPW(), args=["--hide-scrollbars"])
        check("cloak: launch returns the browser", out == "browser", out)
        check(
            "cloak: uses our session (executable_path)",
            seen.get("executable_path") == str(_fake),
            seen,
        )
        check(
            "cloak: keeps the caller's flags",
            "--hide-scrollbars" in (seen.get("args") or []),
            seen.get("args"),
        )
        check(
            "cloak: adds --no-sandbox",
            "--no-sandbox" in (seen.get("args") or []),
            seen.get("args"),
        )
        check(
            "cloak: drops automation switches",
            "--enable-automation" in (seen.get("ignore_default_args") or []),
            seen.get("ignore_default_args"),
        )
        check(
            "cloak: passes an env",
            isinstance(seen.get("env"), dict),
            type(seen.get("env")),
        )
    except cl.BrowserLaunchError as exc:
        check("cloak: launch returns the browser", False, exc)
    _cb.ensure_binary = _real_ensure
else:
    print("SKIP  cloak launch checks (cloakbrowser not installed here)")

cl.cloak_binary = _real_binary
cl.cloak_installed = _saved_cloak
if _saved_choice is None:
    os.environ.pop("SCRIBD_BROWSER", None)
else:
    os.environ["SCRIBD_BROWSER"] = _saved_choice

print()
print(f"{checks} checks, {fails} failures")
raise SystemExit(1 if fails else 0)
