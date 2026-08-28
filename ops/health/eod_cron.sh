#!/bin/bash
# END-OF-SESSION COUNTS -- runs after v2's configured 16:00 ET entry window has shut.
# The existing 18:05-19:05 ET reporting window is intentionally later so same-day outcomes settle.
# ET-guarded here because CRON_TZ is ignored on this box. Read-only.
set -u

OUT=${EOD_OUT_DIR:-/home/trader/entry_fix_watch}
PYTHON_BIN=${EOD_PYTHON_BIN:-/home/trader/project-mai-tai/.venv/bin/python}
CURL_BIN=${EOD_CURL_BIN:-curl}
COUNTS_SCRIPT="$OUT/eod_counts.py"
ENV_FILE=${EOD_ENV_FILE:-/etc/project-mai-tai/eod-watch.env}
if [ -r "$ENV_FILE" ]; then
  # Root-owned 0600 environment-specific configuration. The fleet's preopen-named topic is also
  # present in older repo-managed checks; this wrapper merely avoids adding another embedded copy.
  # shellcheck disable=SC1090
  . "$ENV_FILE"
fi
NTFY_URL=${MAI_TAI_NTFY_URL:-}

if [ "${MAI_TAI_EOD_TEST_MODE:-0}" = "1" ]; then
  DAY=${MAI_TAI_EOD_TEST_DAY:?test day required}
  ETMIN=${MAI_TAI_EOD_TEST_ETMIN:?test ET minute required}
  ETDOW=${MAI_TAI_EOD_TEST_ETDOW:?test ET weekday required}
  RUN_STAMP=${MAI_TAI_EOD_TEST_STAMP:-test}
else
  DAY=$(TZ=America/New_York date +%F)
  ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))
  ETDOW=$(TZ=America/New_York date '+%u')
  RUN_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
fi

report_complete() {
  local report=$1
  [ -s "$report" ] && grep -q '^VERDICT eod ' "$report"
}

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1
CANONICAL_REPORT="$OUT/eod_${DAY}.txt"
NOTIFIED_MARKER="$OUT/eod_${DAY}.notified"
USE_EXISTING=0

mkdir -p "$OUT"
LOG_FILE="$OUT/eod.log"
echo "[EOD-CRON-TICK] day=$DAY etmin=$ETMIN dow=$ETDOW force=$FORCE" >> "$LOG_FILE"

if [ "$FORCE" -eq 0 ]; then
  if [ "$ETDOW" -gt 5 ]; then
    echo "[EOD-CRON-SKIPPED] reason=weekend day=$DAY" >> "$LOG_FILE"
    exit 0
  fi
  if [ "$ETMIN" -lt 1085 ]; then
    echo "[EOD-CRON-SKIPPED] reason=before_window day=$DAY etmin=$ETMIN" >> "$LOG_FILE"
    exit 0
  fi
  if [ "$ETMIN" -ge 1145 ]; then
    echo "[EOD-CRON-SKIPPED] reason=after_window day=$DAY etmin=$ETMIN" >> "$LOG_FILE"
    exit 0
  fi
  # A zero-byte/partial report is evidence of a crashed attempt, not a latch. A completed report
  # is not fully done until notification succeeds; later cron ticks retry only the notification.
  if report_complete "$CANONICAL_REPORT"; then
    if [ -f "$NOTIFIED_MARKER" ]; then
      echo "[EOD-CRON-SKIPPED] reason=already_notified day=$DAY" >> "$LOG_FILE"
      exit 0
    fi
    USE_EXISTING=1
  fi
  REPORT="$CANONICAL_REPORT"
else
  # A probe must never poison the canonical day or suppress the later scheduled result.
  REPORT="$OUT/eod_force_${DAY}_${RUN_STAMP}_$$.txt"
fi

STDERR_FILE="$OUT/eod_stderr.last"
DISPLAY_REPORT="$REPORT"
if [ "$USE_EXISTING" -eq 1 ]; then
  PYTHON_EXIT=0
  V=$(grep -m1 '^VERDICT eod ' "$REPORT")
else
  TMP_REPORT=$(mktemp "$OUT/.eod_${DAY}.XXXXXX") || exit 1
  trap 'rm -f "$TMP_REPORT"' EXIT
  "$PYTHON_BIN" "$COUNTS_SCRIPT" --day "$DAY" > "$TMP_REPORT" 2>"$STDERR_FILE"
  PYTHON_EXIT=$?
  if [ "$PYTHON_EXIT" -eq 0 ] && report_complete "$TMP_REPORT"; then
    mv -f "$TMP_REPORT" "$REPORT"
    trap - EXIT
    V=$(grep -m1 '^VERDICT eod ' "$REPORT")
  else
    FAILED_REPORT="$OUT/eod_failed_${DAY}_${RUN_STAMP}_$$.txt"
    mv -f "$TMP_REPORT" "$FAILED_REPORT"
    trap - EXIT
    DISPLAY_REPORT="$FAILED_REPORT"
    V="VERDICT eod MISSING -- report incomplete python_exit=$PYTHON_EXIT artifact=$FAILED_REPORT"
  fi
fi

{
  echo "--- $DAY report=$DISPLAY_REPORT force=$FORCE python_exit=$PYTHON_EXIT ---"
  [ -f "$DISPLAY_REPORT" ] && cat "$DISPLAY_REPORT"
  [ -s "$STDERR_FILE" ] && { echo "--- stderr ---"; cat "$STDERR_FILE"; }
  echo "$V"
} >> "$LOG_FILE"

if [ -z "$NTFY_URL" ]; then
  echo "ntfy push failed: MAI_TAI_NTFY_URL is unset" >> "$LOG_FILE"
  exit 1
fi
"$CURL_BIN" -sS --fail-with-body -m 20 -H "Title: v2 end-of-session counts $DAY" \
     -H "Priority: default" -d "$V

Full report: $DISPLAY_REPORT
These are RESULTS from the post-16:00 reporting run. Mid-session readings are not." \
     "$NTFY_URL" >/dev/null || {
       echo "ntfy push failed" >> "$LOG_FILE"
       exit 1
     }

if [ "$PYTHON_EXIT" -eq 0 ] && report_complete "$REPORT"; then
  [ "$FORCE" -eq 0 ] && : > "$NOTIFIED_MARKER"
  exit 0
fi
exit 1
