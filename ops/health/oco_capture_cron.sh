#!/bin/bash
# OCO CAPTURE check — cron wrapper for the two flags enabled 2026-07-27 evening
# (webull_bracket_realign_on_fill_enabled + oms_record_native_oco_exit_fills_enabled).
#
# Pages when: a SELL fill is booked at price<=0 (the $0 cancelled-leg trap = a -100% trade) ·
# entries close with no exit fill recorded (P&L stays blank) · 'bracket realign failed' (the
# broker rejects a partial combo replace — safe, but the fix is not working).
#
# WINDOW: exits only resolve while the market is open, and the first bracket of the day needs time
# to complete, so 10:00-16:30 ET. Every 15 min — this is a "did the new code work" watch, not a
# liveness monitor, so a slower cadence than the 5-min fleet checks is right.
#
# CRON_TZ IS IGNORED ON THIS BOX (learned the hard way — see the pre-open readiness infra). So the
# cron runs over a UTC hour range wide enough to cover BOTH offsets (EDT 14:00-20:30,
# EST 15:00-21:30 UTC) and the ET guard below does the precision:
#   */15 14-21 * * 1-5   /home/trader/project-mai-tai/ops/health/oco_capture_cron.sh
#
# Read-only. Exit 0 green/skip, 1 amber, 2 red.
set -u
OUT=/home/trader/oco_capture; mkdir -p "$OUT"
STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
TODAY=$(TZ=America/New_York date +%F)
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))
WD=$(TZ=America/New_York date +%u)   # 1..7 (Mon..Sun)
CHECK=/home/trader/project-mai-tai/ops/health/oco_capture_check.py
PY=/home/trader/project-mai-tai/.venv/bin/python
ENVF=/etc/project-mai-tai/project-mai-tai.env

run() {
  # systemd's own EnvironmentFile parser — `env $(grep ...)` chokes on values containing parens
  sudo systemd-run --quiet --uid=trader --pipe --wait --collect \
    -p EnvironmentFile="$ENVF" \
    -p WorkingDirectory=/home/trader/project-mai-tai \
    "$PY" "$CHECK" "$@"
}

# --selftest bypasses every guard on purpose: you must be able to prove the pager works off-window.
if [ "${1:-}" = "--selftest" ]; then
  run --selftest
  echo "$STAMP  SELFTEST push sent" >> "$OUT/cron.log"; exit 0
fi

# --- ET guard: 10:00 (600) <= ET < 16:30 (990), Mon-Fri ---
if [ "$ETMIN" -lt 600 ] || [ "$ETMIN" -ge 990 ]; then exit 0; fi
if [ "$WD" -ge 6 ]; then exit 0; fi
HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
HOLIDAYS_2027="2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24"
case " $HOLIDAYS_2026 $HOLIDAYS_2027 " in
  *" $TODAY "*) echo "$STAMP  HOLIDAY $TODAY — skip" >> "$OUT/cron.log"; exit 0 ;;
esac

out=$(run 2>&1); rc=$?
echo "$STAMP  rc=$rc  $out" >> "$OUT/cron.log"
exit $rc
