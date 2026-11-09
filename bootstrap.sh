#!/usr/bin/env bash
# bootstrap.sh — one command that makes a fresh panel container work.
#
#   bash bootstrap.sh
#
# It is safe to run twice: every step checks before it does anything.
#
#   1. removes the PyPI "chromium" stub if someone installed it (it is 2.4 kB
#      of nothing and is the usual reason people think pip can bring a browser)
#   2. installs requirements.txt into ./.local
#   3. downloads + unpacks a self-contained Chromium into ./.chromium
#   4. writes SCRIBD_CHROMIUM (and the fontconfig) into .env
#   5. proves the browser actually starts
#
# Afterwards the Startup command is:   bash start.sh

set -u
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

GREEN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s ok %s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%s  ! %s %s\n' "$YEL" "$OFF" "$*"; }
bad()  { printf '%s  x %s %s\n' "$RED" "$OFF" "$*"; }
step() { printf '\n%s== %s ==%s\n' "$DIM" "$*" "$OFF"; }

# everything large goes on the volume, never in the container's small overlay
export PIP_NO_CACHE_DIR=1
export TMPDIR="${TMPDIR:-$PWD/.tmp}"
mkdir -p "$TMPDIR"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python
command -v "$PY" >/dev/null 2>&1 || { bad "no python on PATH"; exit 1; }
PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "?")"
say "python  : $("$PY" -c 'import sys;print(sys.executable)') ($PYVER)"
say "volume  : $PWD  ($(df -Pk . 2>/dev/null | awk 'NR==2 {print int($4/1024)}') MB free)"

# ---------------------------------------------------------------- 1. the stub
step "1/5  the PyPI 'chromium' trap"
if "$PY" -c 'import chromium' >/dev/null 2>&1; then
  warn "the PyPI package 'chromium' is installed - it contains no browser"
  "$PY" -m pip uninstall -y chromium >/dev/null 2>&1 && ok "removed it" || warn "could not remove it (harmless, just ignored)"
else
  ok "not installed - good"
fi
say "${DIM}  (it only prints a download URL; pip can never install a browser)${OFF}"

# ---------------------------------------------------------- 2. python packages
step "2/5  python packages"
if "$PY" -c 'import playwright, img2pdf, pypdf, PIL, requests' >/dev/null 2>&1; then
  ok "requirements already satisfied"
else
  # PEP 668 (python 3.12+ on some hosts) refuses a plain install; the panel's
  # own bootstrap uses --prefix .local, which sidesteps it everywhere
  if "$PY" -m pip install -U --prefix .local -r requirements.txt; then
    ok "installed into ./.local"
  elif "$PY" -m pip install -U -r requirements.txt; then
    ok "installed into the interpreter"
  else
    bad "pip failed - paste the error rather than guessing"
    exit 1
  fi
fi

# guard: newer python needs the .local path on sys.path when it is not the
# interpreter's own prefix (the panel export does this for us, we do it too)
export PYTHONPATH="$PWD/.local/lib/python${PYVER}/site-packages:${PYTHONPATH:-}"

# ----------------------------------------------------------- 3. the browser
step "3/5  browser"
EXE="$PWD/.chromium/chromium"
if [ -x "$EXE" ] && "$PY" - <<'PY' >/dev/null 2>&1
import sys, pathlib
sys.path.insert(0, ".")
import chromium_launch as cl
sys.exit(1 if cl.inspect_exe(pathlib.Path(".chromium/chromium"))["problem"] else 0)
PY
then
  ok "already unpacked: .chromium/chromium"
else
  if [ -e "$EXE" ]; then
    warn "the existing .chromium/chromium is not runnable - re-extracting"
    rm -rf .chromium
  fi
  say "${DIM}  downloading @sparticuz/chromium (~80 MB packed, ~200 MB unpacked)${OFF}"
  if "$PY" chromium_setup.py; then
    ok "browser unpacked"
  else
    bad "chromium_setup.py failed - read the lines above, they name the cause"
    exit 1
  fi
fi

# --------------------------------------------------------------- 4. .env
step "4/5  .env"
if [ ! -f .env ]; then
  cp .env.example .env 2>/dev/null && ok "created .env from .env.example" || { touch .env; warn "created an empty .env"; }
else
  ok ".env exists - left alone"
fi

set_env() {  # set_env KEY VALUE  (only if the key is absent or empty)
  local key="$1" value="$2"
  if grep -qE "^${key}=.+" .env 2>/dev/null; then
    say "${DIM}  ${key} already set - keeping it${OFF}"
    return
  fi
  # drop an existing empty KEY= line first, then append
  if grep -qE "^${key}=" .env 2>/dev/null; then
    grep -vE "^${key}=" .env > .env.tmp && mv .env.tmp .env
  fi
  printf '%s=%s\n' "$key" "$value" >> .env
  ok "set ${key}"
}

set_env SCRIBD_CHROMIUM "$EXE"
if [ -f .chromium/fonts/fonts.conf ]; then
  set_env FONTCONFIG_FILE "$PWD/.chromium/fonts/fonts.conf"
fi

# --------------------------------------------------------------- 5. proof
step "5/5  does it actually start?"
if "$PY" - <<'PY'
import sys
sys.path.insert(0, ".")
try:
    from playwright.sync_api import sync_playwright
except Exception as exc:  # noqa: BLE001
    print(f"  playwright import failed: {exc}")
    raise SystemExit(1)
import chromium_launch as cl
try:
    with sync_playwright() as pw:
        browser = cl.launch(pw)
        page = browser.new_page()
        page.set_content("<h1 style='font:32px sans-serif'>hello</h1>")
        text = page.inner_text("h1")
        browser.close()
except Exception as exc:  # noqa: BLE001
    print(f"  {exc}")
    raise SystemExit(1)
print(f"  launched and rendered: {text!r}")
raise SystemExit(0 if text.strip() == "hello" else 1)
PY
then
  ok "browser launches and renders text"
else
  warn "the browser did not start. Use the line above, or:"
  warn "  LANG=C.UTF-8 HOME=$PWD/.chromium $EXE --headless --dump-dom about:blank"
  exit 1
fi

printf '\n%sready%s\n' "$GREEN" "$OFF"
say "  set the panel Startup command to:   bash start.sh"
if [ -n "${PUBLIC_HOST:-}" ]; then
  set_env PUBLIC_HOST "$PUBLIC_HOST"
fi
say "  or run one document:                python scribd_all.py <id> -o output"
say "  check the offload config:           python drime.py --check"
