#!/bin/bash
# Daily pre-open readiness — cron target.
#   Time logic uses `TZ=America/New_York date` (reliable on this box) so the
#   script is self-sufficient on ET regardless of whether the crontab CRON_TZ
#   variable is honored. An ET wall-clock GUARD ensures it only proceeds at
#   ~09:12 ET — which also makes a DST-safe dual-UTC cron schedule safe (fire at
#   both 13:12 and 14:12 UTC; the guard runs the check only on the one that is
#   09:12 ET that half of the year). Skips NYSE full-closure holidays.
#   Verdict exit 0/1/2 (green/amber/red) routes to preopen_alert.sh.
set -u
OUT=/home/trader/preopen_out
mkdir -p "$OUT"
STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
TODAY=$(TZ=America/New_York date +%F)
ETH=$(TZ=America/New_York date '+%H')
ETM=$(TZ=America/New_York date '+%M')

# ---- ET wall-clock guard: only run at ~09:12 ET (tolerates cron jitter) ----
if [ "$ETH" != "09" ] || [ "$ETM" -lt 5 ] || [ "$ETM" -gt 20 ]; then
  echo "$STAMP  guard: not ~09:12 ET (now ${ETH}:${ETM} ET) — skip" >> "$OUT/cron.log"
  exit 0
fi

# ---- NYSE full-closure holidays (half-days NOT skipped). UPDATE ANNUALLY. ----
HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
HOLIDAYS_2027="2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24"
case " $HOLIDAYS_2026 $HOLIDAYS_2027 " in
  *" $TODAY "*)
    echo "$STAMP  HOLIDAY $TODAY — skipped (no alert)" >> "$OUT/cron.log"
    exit 0 ;;
esac

OUTFILE="$OUT/readiness_latest.txt"
python3 /home/trader/preopen_readiness_check.py > "$OUTFILE" 2>&1
CODE=$?
VERDICT=$(grep '^VERDICT:' "$OUTFILE" | head -1)
echo "$STAMP  exit=$CODE  $VERDICT" >> "$OUT/cron.log"

case "$CODE" in
  2) LEVEL=RED   ;;
  1) LEVEL=AMBER ;;
  0) LEVEL=GREEN ;;
  *) LEVEL=ERROR ;;   # readiness script crashed — treat like RED
esac

# ---- SEED-EXPOSURE DETECTOR (§177). Run BY REPO PATH, never the retired /tmp stub. ----
# ⛔⭐ A TOOL THAT ARRIVES AND IS NEVER CALLED IS A MODULE WITH NO IMPORTERS. `broker_refusal.py`
# shipped green and inert for a day; `trade_reasons.py` has never had a consumer. This is the call
# site that stops the detector joining them.
#
# ⛔⭐⭐ AND THIS SLOT IS LATE FOR ITS PURPOSE — SAY SO RATHER THAN LET IT READ AS COVERAGE.
# The guard above pins this routine to ~09:12 ET, but seed exposure is created from 04:00 ET and at
# EVERY watchlist add: on 2026-08-19 BIVI was exposed at 06:50 and VRAX truncated at 09:13. A 09:12
# reading is a DAILY RECORD, not the watch. The real watch wants its own ET-guarded cron across
# 04:00-11:00 ET; until that exists, do not read a clean line here as a clean session.
#
# ⛔ Exit codes: 0 = swept, no exposure | 1 = EXPOSURE found | 2 = CANNOT SEE (refused).
# 2 is NOT better than 1 — an unknown must never decay into a pass — so it raises the level at
# least as far as 1 does.
SEED_OUT="$OUT/seed_exposure_latest.txt"
REPO=/home/trader/project-mai-tai
# ⛔⭐⭐ THIS CALLER NEEDS THE SERVICE ENV TOO — it is a SECOND, INDEPENDENT invocation of the same
# detector, and it was blind for the same reason and the same ten sessions (2026-08-20..09-02):
# without MAI_TAI_DATABASE_URL the detector refuses with "no DSN", which is what pinned this
# routine's verdict at AMBER every single day. Fixing only the 5-minute seed-exposure cron would
# have left this slot broken and the daily AMBER intact.
# ⛔ Runs as ROOT from ROOT's crontab; the env file is root-readable only.
SEED_ENV_FILE=/etc/project-mai-tai/project-mai-tai.env
if [ ! -r "$SEED_ENV_FILE" ]; then
  printf '  ⛔ CANNOT SEE — REFUSING: service env NOT READABLE at %s\n' "$SEED_ENV_FILE" > "$SEED_OUT"
  SEED_CODE=2   # ⛔ unreadable env = CANNOT SEE, never GREEN
elif [ -x "$REPO/.venv/bin/python" ] && [ -f "$REPO/scripts/seed_exposure_detector.py" ]; then
  # Sourced INSIDE the subshell so this wrapper's own variables are untouched.
  ( set -a
    # shellcheck disable=SC1091
    . "$SEED_ENV_FILE"
    set +a
    cd "$REPO" && ./.venv/bin/python scripts/seed_exposure_detector.py --assert-constants ) \
    > "$SEED_OUT" 2>&1
  SEED_CODE=$?
else
  printf '  ⛔ CANNOT SEE — REFUSING: detector NOT FOUND at %s\n' \
    "$REPO/scripts/seed_exposure_detector.py" > "$SEED_OUT"
  SEED_CODE=2   # ⛔ missing tool = CANNOT SEE, never GREEN
fi
SEED_LINE=$(grep -E '^\s+(VERDICT|⛔ CANNOT SEE)' "$SEED_OUT" | head -1)
echo "$STAMP  seed-exposure exit=$SEED_CODE  $SEED_LINE" >> "$OUT/cron.log"

case "$SEED_CODE" in
  # ⛔⭐ CARRY THE REASON, not just the level. This read "SEED-EXPOSURE: CANNOT SEE" — with no cause —
  # every day for ten sessions, and a reasonless repeat is what turns an alarm into wallpaper. The
  # actual cause ("no DSN") was sitting in $SEED_LINE the whole time and was being thrown away.
  2) [ "$LEVEL" = "GREEN" ] && LEVEL=RED   ; VERDICT="$VERDICT | SEED-EXPOSURE: CANNOT SEE${SEED_LINE:+ —${SEED_LINE#*REFUSING:}}" ;;
  1) [ "$LEVEL" = "GREEN" ] && LEVEL=AMBER ; VERDICT="$VERDICT | $SEED_LINE" ;;
  0) VERDICT="$VERDICT | seed-exposure: none" ;;
  *) [ "$LEVEL" = "GREEN" ] && LEVEL=RED   ; VERDICT="$VERDICT | SEED-EXPOSURE: crashed" ;;
esac

/home/trader/preopen_alert.sh "$LEVEL" "$VERDICT" "$OUTFILE"
exit 0
