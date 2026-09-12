#!/bin/bash
# Lightweight dispatcher for the known-defect regression watch.
#
# Install into root's cron for every five minutes. The Python evaluator enforces exact polarity,
# uses read-only DB/log evidence, and imports the existing unexercised watch's ntfy delivery path.
# This wrapper deliberately contains no curl command and no notification endpoint.
set -u

OUT="${KNOWN_DEFECT_WATCH_OUT:-/home/trader/known_defect_regression_watch}"
REPO="${KNOWN_DEFECT_WATCH_REPO:-/home/trader/project-mai-tai}"
PYTHON="${KNOWN_DEFECT_WATCH_PYTHON:-$REPO/.venv/bin/python}"
CHECK="${KNOWN_DEFECT_WATCH_SCRIPT:-$REPO/ops/health/known_defect_regression_watch.py}"
mkdir -p "$OUT"

if [ "${1:-}" = "--selftest" ]; then
  exec "$PYTHON" "$CHECK" --selftest
fi

# Cron timezone directives are ignored on the production host. Keep the all-day cron simple and
# enforce the trading evidence window here in ET. The explicit override is test-only.
if [ -n "${KNOWN_DEFECT_WATCH_NOW_ET:-}" ]; then
  now_et="$KNOWN_DEFECT_WATCH_NOW_ET"
else
  now_et="$(TZ=America/New_York date '+%u %H%M')"
fi
read -r weekday hhmm <<<"$now_et"
if (( weekday > 5 || 10#$hhmm < 350 || 10#$hhmm > 2015 )); then
  exit 0
fi

exec nice -n 19 "$PYTHON" "$CHECK" --state "$OUT/state.json" --status "$OUT/STATUS.txt"
