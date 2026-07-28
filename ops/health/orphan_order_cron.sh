#!/bin/bash
# ORPHAN-ORDER check — cron wrapper for the two flags enabled 2026-07-27 evening
# (webull_bracket_realign_on_fill_enabled + oms_record_native_oco_exit_fills_enabled).
#
# Pages when: a WORKING broker order sits >=5% from the market and >=15min old (the 2026-07-28
# POLA shape — v2 cleared its own resting flag without cancelling, so the order went orphaned and
# the OPERATOR found it by eye on a chart) · or a stale WORKING order whose symbol is not even in
# the bot's watchlist, i.e. no strategy is evaluating it.
#
# WINDOW: a resting order can be placed from the 07:00 ET entry window onward, so 09:00-16:30 ET
# (MIN_AGE_MIN=15 means nothing before ~07:15 could qualify anyway). Every 10 min — an orphaned
# order is money at risk, so this runs tighter than the 15-min capture watch.
#
# CRON_TZ IS IGNORED ON THIS BOX (learned the hard way — see the pre-open readiness infra). So the
# cron runs over a UTC hour range wide enough to cover BOTH offsets (EDT 14:00-20:30,
# EST 15:00-21:30 UTC) and the ET guard below does the precision:
#   */10 13-21 * * 1-5   /home/trader/project-mai-tai/ops/health/orphan_order_cron.sh
#
# Read-only. Exit 0 green/skip, 1 amber, 2 red.
set -u
OUT=/home/trader/orphan_order; mkdir -p "$OUT"
STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
TODAY=$(TZ=America/New_York date +%F)
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))
WD=$(TZ=America/New_York date +%u)   # 1..7 (Mon..Sun)
CHECK=/home/trader/project-mai-tai/ops/health/orphan_order_check.py
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
if [ "$ETMIN" -lt 540 ] || [ "$ETMIN" -ge 990 ]; then exit 0; fi   # 09:00-16:30 ET
if [ "$WD" -ge 6 ]; then exit 0; fi
HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
HOLIDAYS_2027="2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24"
case " $HOLIDAYS_2026 $HOLIDAYS_2027 " in
  *" $TODAY "*) echo "$STAMP  HOLIDAY $TODAY — skip" >> "$OUT/cron.log"; exit 0 ;;
esac

out=$(run 2>&1); rc=$?
echo "$STAMP  rc=$rc  $out" >> "$OUT/cron.log"
exit $rc
