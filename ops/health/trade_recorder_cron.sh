#!/bin/bash
# Per-trade recorder — cron target. Runs ALL DAY and appends every completed round trip.
#
#   Operator ask (2026-07-29): "run the job whole day, record each and every transaction... slippage,
#   exit, trailing floor, everything. I think that's only way we can get the actual real traits."
#
#   Built because reconstructing the day AFTERWARDS gave three different answers, two of them wrong
#   (see the module docstring in trade_recorder.py). This captures the broker's own order ids at the
#   moment a trade closes, so attribution is never inferred later.
#
#   Window: 07:00 <= ET < 20:30 — covers the v2 pre-market entry window through the 19:55 overnight
#   flatten plus a tail, so a late EH exit is still captured. Weekdays enforced by crontab (1-5).
#   ET wall-clock via `TZ=America/New_York date` (⛔ CRON_TZ is IGNORED on this box); the DST-safe
#   dual-UTC crontab means the body must re-check the ET window itself.
#
#   APPEND-ONLY + idempotent (keyed on the exit broker_order_id), so running it every 5 minutes
#   costs nothing and a missed run self-heals on the next pass.
#   Read-only against the DB and the broker: it can never alter a trade.
set -u

REPO=/home/trader/project-mai-tai
OUT=/home/trader/trade_records
LOG=$OUT/cron.log
mkdir -p "$OUT"

STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
ETH=$(TZ=America/New_York date '+%H')
ETMIN=$((10#$ETH * 60 + 10#$(TZ=America/New_York date '+%M')))

# 07:00 (420) .. 20:30 (1230) ET
if [ "$ETMIN" -lt 420 ] || [ "$ETMIN" -ge 1230 ]; then
  echo "$STAMP  guard: outside 07:00-20:30 ET — skip" >> "$LOG"
  exit 0
fi

# NYSE full-closure holidays (same list the other health crons use)
HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
TODAY=$(TZ=America/New_York date +%F)
case "$HOLIDAYS_2026" in
  *"$TODAY"*) echo "$STAMP  guard: NYSE holiday — skip" >> "$LOG"; exit 0 ;;
esac

cd "$REPO" || { echo "$STAMP  ERROR: no $REPO" >> "$LOG"; exit 1; }

# `nice` because the box is 2 vCPU and routinely runs >100% during RTH; this job must never
# contend with the OMS loop (see the trade-coach CPU finding, 2026-07-29).
set -a
# shellcheck disable=SC1091
. /etc/project-mai-tai/project-mai-tai.env
set +a

nice -n 19 "$REPO"/.venv/bin/python "$REPO"/ops/health/trade_recorder.py \
    --since-mins 1440 --out "$OUT" >> "$LOG" 2>&1
echo "$STAMP  done (rc=$?)" >> "$LOG"
