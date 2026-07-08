#!/usr/bin/env bash
# ORB trail-study FORWARD ACCRUAL — runs daily post-close, appends today's confirmed-window-gated
# ORB name-days to a growing sample and recomputes the running robustness of the intrabar-2%
# config (median $/nd, win%, drop-top-3, by behavior). Grows the trail-study sample ~+4/day so we
# can watch whether the positive median holds toward 15-20+ MORE name-days.
#
# Runs as ROOT (needs the root-owned strategy.log for the momentum_confirmed confirmed-windows).
# Root cron:  0 22 * * 1-5  /home/trader/wt-atr-study/scripts/orb_trail_accrual.sh
#   22:00 UTC = AFTER the 21:00Z post-close market_capture gather (full qualified universe captured)
#   and after the ORB window+exit data — in both EDT (18:00 ET) and EST (17:00 ET).
# Read-only vs production (reads market_capture DB + strategy.log; writes only to DATA below).
set -uo pipefail

WT=/home/trader/wt-atr-study
DATA=/home/trader/orb_trail_accrual
PY=/home/trader/project-mai-tai/.venv/bin/python
ENVF=/etc/project-mai-tai/project-mai-tai.env
LOGF=/var/log/project-mai-tai/strategy.log

mkdir -p "$DATA"
exec >>"$DATA/cron.log" 2>&1
echo "=== $(date -u +%FT%TZ) accrual run ==="

DATE_ET=$(TZ=America/New_York date +%F)
if [ "$(TZ=America/New_York date +%u)" -ge 6 ]; then echo "weekend ($DATE_ET) — skip"; exit 0; fi

MAI_TAI_DATABASE_URL=$(grep -E '^MAI_TAI_DATABASE_URL=' "$ENVF" | head -1 | cut -d= -f2-)
export MAI_TAI_DATABASE_URL
export PYTHONPATH="$WT/src"

# 1. today's confirmed windows from the momentum_confirmed events
if ! grep -hE momentum_confirmed "$LOGF" | grep "$DATE_ET" \
     | "$PY" -m project_mai_tai.backtest.scanner_windows "$DATE_ET" > "$WT/windows/windows_$DATE_ET.json"; then
  echo "ERROR: confirmed-window extract failed for $DATE_ET"; exit 1
fi

# 2. gated ORB exit sweep for today -> today's rows
if ! "$PY" -m project_mai_tai.backtest.orb_exit_sweep "$DATE_ET" \
     --windows-dir="$WT/windows" --json="$DATA/day_$DATE_ET.json"; then
  echo "ERROR: orb sweep failed for $DATE_ET"; exit 1
fi

# 3. merge into the master set + recompute + log the running robustness
"$PY" "$WT/scripts/orb_accrual_merge.py" "$DATA" "$DATE_ET"
echo "=== done $DATE_ET ==="
