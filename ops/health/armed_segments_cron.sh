#!/bin/bash
# CW-v2 ARMED-SEGMENT check — cron wrapper (P1.4's external pager, the last leg of #475).
#
# Pages when: a reconstructed-UNCAPPED ("dangerous") segment survived P1.3 · the boot-hold never
# released · the snapshot is stale/absent while v2 is ACTIVE. See armed_segments_check.py for why
# each is a fault and why v2-inactive / flag-off are deliberately NOT.
#
# WINDOW: v2's entry window is 07:00-16:30 ET, and all three faults only have consequence while v2
# could be entering — so that is the window. Every 5 min (matches the F3 fleet-health cadence).
#
# CRON_TZ IS IGNORED ON THIS BOX (learned the hard way — see the pre-open readiness infra). So the
# cron runs over a UTC hour range wide enough to cover BOTH offsets (EDT 11:00-20:30, EST
# 12:00-21:30 UTC) and the ET guard below does the precision:
#   */5 11-21 * * 1-5   /home/trader/project-mai-tai/ops/health/armed_segments_cron.sh
#
# Read-only. Fail-loud. Exit 0 green/skip, 2 red (paged).
set -u
OUT=/home/trader/armed_segments; mkdir -p "$OUT"
STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
TODAY=$(TZ=America/New_York date +%F)
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))
WD=$(TZ=America/New_York date +%u)   # 1..7 (Mon..Sun)
CHECK=/home/trader/project-mai-tai/ops/health/armed_segments_check.py

# --selftest bypasses every guard on purpose: you must be able to prove the pager works off-window.
if [ "${1:-}" = "--selftest" ]; then
  python3 "$CHECK" --selftest
  echo "$STAMP  SELFTEST push sent" >> "$OUT/cron.log"; exit 0
fi

# --- ET guard: 07:00 (420) <= ET < 16:30 (990), Mon-Fri ---
if [ "$ETMIN" -lt 420 ] || [ "$ETMIN" -ge 990 ]; then
  exit 0   # outside v2's entry window — quiet, not even logged (this fires ~130x/day off-window)
fi
if [ "$WD" -ge 6 ]; then exit 0; fi
HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
HOLIDAYS_2027="2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24"
case " $HOLIDAYS_2026 $HOLIDAYS_2027 " in
  *" $TODAY "*) echo "$STAMP  HOLIDAY $TODAY — skip" >> "$OUT/cron.log"; exit 0 ;;
esac

out=$(python3 "$CHECK" 2>&1); rc=$?
echo "$STAMP  rc=$rc  $out" >> "$OUT/cron.log"
exit $rc
