#!/usr/bin/env bash
# panel_setup.sh — install everything on a small panel container without
# running out of disk.
#
#   bash panel_setup.sh              # core install, skips pymupdf
#   bash panel_setup.sh --with-mupdf # also install pymupdf (smaller PDFs)
#   bash panel_setup.sh --clean      # uninstall the things we do not need
#
# It checks free space before every big step and tells you what it skipped and
# why, instead of dying half way with "No space left on device".

set -u
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

GREEN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$YEL" "$OFF" "$*"; }
bad()  { printf '%s✗%s %s\n' "$RED" "$OFF" "$*"; }

free_mb() {  # free space on the workspace volume, in MB
  df -Pk . 2>/dev/null | awk 'NR==2 {print int($4/1024)}'
}

# playwright browsers and pip's build junk should live on the volume, not in
# the container's tiny overlay filesystem (that is what fills up first)
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$PWD/.pw-browsers}"
export PIP_NO_CACHE_DIR=1
export TMPDIR="${TMPDIR:-$PWD/.tmp}"
mkdir -p "$TMPDIR"

WITH_MUPDF=0
CLEAN=0
for arg in "$@"; do
  case "$arg" in
    --with-mupdf) WITH_MUPDF=1 ;;
    --clean) CLEAN=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) warn "unknown option: $arg" ;;
  esac
done

say "=============================================================="
say " scribd panel setup"
say " workspace : $PWD"
say " free disk : $(free_mb) MB"
say " browsers  : $PLAYWRIGHT_BROWSERS_PATH"
say " python    : $($PY -V 2>&1)"
say "=============================================================="

# --- 1. junk that a plain "pip install mupdf" drags in ----------------------
BLOAT="pymupdf4llm pymupdf-layout onnxruntime flatbuffers sympy mpmath pikepdf lxml tabulate networkx numpy"
PRESENT=""
for pkg in $BLOAT; do
  if "$PY" - "$pkg" <<'EOF' >/dev/null 2>&1
import importlib.util, sys
mod = sys.argv[1].replace("-", "_").split(".")[0]
names = {"onnxruntime": "onnxruntime", "pymupdf4llm": "pymupdf4llm",
         "pymupdf_layout": "pymupdf_layout", "networkx": "networkx"}
sys.exit(0 if importlib.util.find_spec(names.get(mod, mod)) else 1)
EOF
  then PRESENT="$PRESENT $pkg"; fi
done

if [ -n "$PRESENT" ]; then
  warn "installed but not used by this tool:$PRESENT"
  say  "  ${DIM}these came from installing the 'mupdf' metapackage; they are ~200 MB${OFF}"
fi

if [ "$CLEAN" = 1 ]; then
  say ""
  say "-> removing the packages this tool never imports"
  # shellcheck disable=SC2086
  "$PY" -m pip uninstall -y -q $BLOAT >/dev/null 2>&1
  # pip leaves orphaned package dirs behind when an install was interrupted by
  # a full disk - remove those too, they are still several hundred MB
  "$PY" - <<'EOF'
import importlib.util
import os
import shutil
import sys

targets = ["pymupdf4llm", "pymupdf_layout", "onnxruntime", "flatbuffers",
           "sympy", "mpmath", "pikepdf", "lxml", "tabulate", "networkx"]
freed = 0
for name in targets:
    try:
        spec = importlib.util.find_spec(name)
    except Exception:
        continue
    if not spec:
        continue
    path = spec.origin
    if not path or not spec.submodule_search_locations:
        continue
    pkg_dir = list(spec.submodule_search_locations)[0]
    if "site-packages" not in pkg_dir or os.path.basename(pkg_dir) != name:
        continue
    for root, _dirs, files in os.walk(pkg_dir):
        for f in files:
            try:
                freed += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    if shutil.rmtree(pkg_dir, ignore_errors=True) or True:
        print(f"  removed leftover {name} ({freed / 1e6:.0f} MB)")
        freed = 0
EOF
  "$PY" -m pip cache purge >/dev/null 2>&1
  rm -rf .tmp/* 2>/dev/null
  ok "cleaned: free disk is now $(free_mb) MB"
fi

# --- 2. core dependencies ---------------------------------------------------
say ""
say "-> installing core dependencies (playwright, img2pdf, pypdf, pillow, requests)"
if "$PY" -m pip install -q --no-cache-dir -r requirements.txt; then
  ok "core dependencies installed"
else
  bad "pip failed - free disk: $(free_mb) MB"
  say "   try: bash panel_setup.sh --clean   then run this again"
  exit 1
fi

# --- 3. chromium ------------------------------------------------------------
say ""
if [ -d "$PLAYWRIGHT_BROWSERS_PATH" ] && [ -n "$(ls -A "$PLAYWRIGHT_BROWSERS_PATH" 2>/dev/null)" ]; then
  ok "chromium already present ($(du -sh "$PLAYWRIGHT_BROWSERS_PATH" 2>/dev/null | cut -f1))"
else
  need=200
  have=$(free_mb)
  if [ "$have" -lt "$need" ]; then
    bad "chromium needs ~${need} MB, only ${have} MB free"
    say  "   free space first: bash panel_setup.sh --clean"
    exit 1
  fi
  say "-> downloading chromium (~120 MB) into $PLAYWRIGHT_BROWSERS_PATH"
  if "$PY" -m playwright install chromium; then
    ok "chromium installed ($(du -sh "$PLAYWRIGHT_BROWSERS_PATH" 2>/dev/null | cut -f1))"
  else
    bad "chromium download failed - free disk: $(free_mb) MB"
    exit 1
  fi
fi

# --- 4. optional pymupdf ----------------------------------------------------
say ""
if [ "$WITH_MUPDF" = 1 ]; then
  have=$(free_mb)
  if [ "$have" -lt 250 ]; then
    warn "skipping pymupdf: needs ~250 MB free (for the 69 MB wheel + temp), you have ${have} MB"
    say  "   the tool will assemble PDFs with img2pdf instead - bigger files, same quality"
  else
    say "-> installing pymupdf (69 MB) for smaller PDFs"
    if "$PY" -m pip install -q --no-cache-dir -r requirements-mupdf.txt; then
      ok "pymupdf installed"
    else
      warn "pymupdf failed - falling back to img2pdf, the tool handles it"
    fi
  fi
else
  say "-> pymupdf not requested (pass --with-mupdf to add it)"
  say "   ${DIM}PDFs are assembled with img2pdf: no quality loss, ~2.5x bigger on text pages${OFF}"
fi

# --- 5. sanity check --------------------------------------------------------
say ""
say "-> checking everything imports"
if "$PY" - <<'EOF'
import importlib.util
import sys
need = ["playwright", "img2pdf", "pypdf", "PIL", "requests"]
missing = [m for m in need if not importlib.util.find_spec(m)]
print("   missing:", missing if missing else "none")
try:
    import pymupdf  # noqa: F401
    print("   pymupdf : available (smaller PDFs)")
except ImportError:
    print("   pymupdf : not installed (using img2pdf)")
if missing:
    sys.exit(1)
EOF
then
  ok "ready"
else
  bad "something is missing - see above"
  exit 1
fi

say ""
say "free disk now: $(free_mb) MB"
say ""
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  say "created .env from .env.example - put your DISCORD_TOKEN in it"
fi

say "next:"
say "  python discord_bot.py --with-web        # bot + web UI"
say "  python scribd_web.py --token auto       # web UI only"
