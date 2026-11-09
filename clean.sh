#!/bin/sh
# Remove everything the tools generate, keeping the tools themselves.
#   sh clean.sh            delete generated files, keep the downloaded browser
#   sh clean.sh --browser  also delete ./.chromium  (~200 MB, re-fetched in 1 min)
#
# Safe to run any time; nothing here is needed to start the tool.
set -e

keep_browser=1
[ "$1" = "--browser" ] && keep_browser=0

before=$(du -sk . 2>/dev/null | cut -f1)

# --- generated output -------------------------------------------------------
rm -rf output .convtest samples            # PDFs, per-page work dirs, test runs
rm -f  .webtoken                          # regenerated on the next start
rm -rf .tmp                               # chromium profiles / temp files
find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# --- optional: the downloaded browser --------------------------------------
if [ "$keep_browser" = "0" ]; then
    rm -rf .chromium .cloakbrowser .cloak .cftfull
fi

after=$(du -sk . 2>/dev/null | cut -f1)
echo "freed $(( (before - after) / 1024 )) MB - now $(du -sh . | cut -f1) in $(find . -type f | wc -l) files"
[ "$keep_browser" = "1" ] && [ -d .chromium ] && \
    echo "kept .chromium (the browser) - use 'sh clean.sh --browser' to drop it too"
exit 0
