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

/home/trader/preopen_alert.sh "$LEVEL" "$VERDICT" "$OUTFILE"
exit 0
