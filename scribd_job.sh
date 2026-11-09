#!/usr/bin/env bash
# scribd_job.sh — run downloads in the background on a hosting panel
# (Pterodactyl / Discord-bot hosts / any SSH box).
#
# Why this exists: panels treat "the process exited" as "the server crashed" and
# restart-loop it.  This script detaches the job with nohup, writes a log and a
# PID file, and exits immediately -- so the panel stays happy and your bot keeps
# running while pages render.
#
# Usage:
#   ./scribd_job.sh 272351848
#   ./scribd_job.sh 272351848 949773556 -o output --engine fragment
#   ./scribd_job.sh --from-file ids.txt -o output
#   ./scribd_job.sh --status          # is a job running? show the tail
#   ./scribd_job.sh --stop
#
# Everything after the first argument is passed straight to the tool.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="${HERE}/scribd-job.log"
PIDFILE="${HERE}/scribd-job.pid"

# prefer the single-file build, fall back to the two-file layout
if [ -f "${HERE}/scribd_all.py" ]; then
  TOOL="${HERE}/scribd_all.py"
else
  TOOL="${HERE}/scribd_shot.py"
fi

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

status() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "running (pid $(cat "$PIDFILE"))"
    echo "log: $LOG"
    tail -n 20 "$LOG" 2>/dev/null
  else
    echo "no job running"
    [ -f "$LOG" ] && { echo "last log: $LOG"; tail -n 20 "$LOG"; }
  fi
}

stop() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" && echo "stopped pid $(cat "$PIDFILE")"
    rm -f "$PIDFILE"
  else
    echo "nothing to stop"
  fi
}

case "${1:-}" in
  --status) status; exit 0 ;;
  --stop)   stop;   exit 0 ;;
  "")       echo "usage: $0 <URL_OR_ID | --from-file LIST> [tool options]"; exit 1 ;;
esac

# Auto uses only the live reader and never switches renderers implicitly.
# One tab keeps the memory footprint bounded on a small box. Explicit caller
# options win over these defaults.
DEFAULT_ARGS=()
has_engine=0
has_tabs=0
for arg in "$@"; do
  case "$arg" in
    --engine|--engine=*) has_engine=1 ;;
    --tabs|--tabs=*) has_tabs=1 ;;
  esac
done
[ "$has_engine" = 1 ] || DEFAULT_ARGS+=(--engine auto)
[ "$has_tabs" = 1 ] || DEFAULT_ARGS+=(--tabs 1)

mkdir -p "${HERE}/output"
{
  echo "=== $(date -u '+%Y-%m-%d %H:%M:%SZ') starting: $* ==="
} >>"$LOG"

nohup "$PY" -u "$TOOL" "$@" "${DEFAULT_ARGS[@]}" -o "${HERE}/output" >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"

sleep 1
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "started in the background (pid $(cat "$PIDFILE"))"
  echo "  log    : $LOG"
  echo "  output : ${HERE}/output"
  echo "  follow : tail -f $LOG"
  echo "  status : $0 --status"
else
  echo "failed to start - see $LOG"
  tail -n 30 "$LOG"
  rm -f "$PIDFILE"
  exit 1
fi
