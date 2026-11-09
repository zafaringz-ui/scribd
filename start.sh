#!/usr/bin/env bash
# start.sh — Startup command for hosts that run a shell command.
#
# NOTE: HidenCloud/Pterodactyl eggs run `python $PY_FILE`, so do NOT point
# PY_FILE at this file - use run.py there. This script is for shells.
#
#   bash start.sh
#
# One command, two jobs: the web UI (so the port answers) and optionally the
# Discord bot, in this process tree. Nothing exits, so the panel does not see
# "Exit code: 0" and mark the server as crashed.
#
#   bash start.sh              # web UI only
#   bash start.sh --bot        # web UI + Discord bot (DISCORD_TOKEN in .env)
#   bash start.sh --tool       # CLI converter only (no web UI, no bot)

set -u
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

# the panel put dependencies in ./.local; make sure they are importable
PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "")"
if [ -n "$PYVER" ] && [ -d ".local/lib/python${PYVER}/site-packages" ]; then
  export PYTHONPATH="$PWD/.local/lib/python${PYVER}/site-packages:${PYTHONPATH:-}"
fi

MODE="web"
for arg in "$@"; do
  case "$arg" in
    --bot) MODE="bot" ;;
    --web) MODE="web" ;;
    --tool) MODE="tool" ;;
  esac
done

if [ -f .env ]; then
  # export .env into the environment so both children see the same values;
  # python reads it too, this just makes shell-level checks agree with it
  set -a
  # shellcheck disable=SC1091
  . ./.env 2>/dev/null || true
  set +a
fi

# a browser on the volume, if nothing else was configured
if [ -z "${SCRIBD_CHROMIUM:-}" ] && [ -x "$PWD/.chromium/chromium" ]; then
  export SCRIBD_CHROMIUM="$PWD/.chromium/chromium"
fi
if [ -z "${FONTCONFIG_FILE:-}" ] && [ -f "$PWD/.chromium/fonts/fonts.conf" ]; then
  export FONTCONFIG_FILE="$PWD/.chromium/fonts/fonts.conf"
fi
export TMPDIR="${TMPDIR:-$PWD/.tmp}"
mkdir -p "$TMPDIR"

PORT="${SERVER_PORT:-${PORT:-8080}}"

# HidenCloud/Pterodactyl-style panels expose the container, not a hostname; if
# you know yours, set PUBLIC_HOST in .env so the banner prints a clickable URL.
if [ -z "${PUBLIC_HOST:-}" ] && [ -n "${SERVER_HOST:-}" ] && [ "${SERVER_HOST}" != "0.0.0.0" ]; then
  export PUBLIC_HOST="$SERVER_HOST"
fi

case "$MODE" in
  tool)
    exec "$PY" -u scribd_all.py "$@"
    ;;
  bot)
    exec "$PY" -u discord_bot.py --with-web
    ;;
  *)
    exec "$PY" -u scribd_web.py --port "$PORT"
    ;;
esac
