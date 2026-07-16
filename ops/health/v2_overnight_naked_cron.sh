#!/bin/bash
# v2 OVERNIGHT-NAKED backstop (B, the safety fix behind the 19:55 flatten).
#
# The 19:55 flatten is BEST-EFFORT — in a stuck/ghost-bid AH book it can rest unfilled and the
# broker cancels it at 20:00, leaving the position naked. This is the GROUND-TRUTH backstop (F3
# principle): after the 20:00 fillable gate, if ANY OMS-managed v2 position is still open, the
# flatten failed — for ANY reason it can't see about itself (stuck book, bug, flag off, OMS down) —
# ntfy RED. Read-only, independent of the OMS. Fail-LOUD (a broken check pages too).
#
# Fire-rate is ALSO the measurement: ~3 fires in 2 weeks = data on whether v2's 16:30 entry window
# should stay (the parked strategy question), not a guess.
#
# Cron (dual-UTC + ET-guard, since CRON_TZ is ignored on this box):
#   5 0 * * *   /home/trader/project-mai-tai/ops/health/v2_overnight_naked_cron.sh   # 20:05 ET (EDT)
#   5 1 * * *   /home/trader/project-mai-tai/ops/health/v2_overnight_naked_cron.sh   # 20:05 ET (EST)
set -u
NTFY_URL="https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
OUT=/home/trader/v2_overnight_naked; mkdir -p "$OUT"
STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
TODAY=$(TZ=America/New_York date +%F)
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))
WD=$(TZ=America/New_York date +%u)   # 1..7 (Mon..Sun)

send_ntfy() {  # $1=title $2=priority $3=tags $4=body
  curl -s -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" "$NTFY_URL" >/dev/null 2>>"$OUT/alert.log"
}

if [ "${1:-}" = "--selftest" ]; then
  send_ntfy "✅ v2-overnight-naked SELFTEST" "default" "white_check_mark" \
    "[SELFTEST] backstop alerting path OK — benign, no action."
  echo "$STAMP  SELFTEST push sent" >> "$OUT/cron.log"; exit 0
fi

# --- ET guard: 20:00 (1200) <= ET < 20:20 (1220), Mon-Fri (the dual-cron fires once) ---
if [ "$ETMIN" -lt 1200 ] || [ "$ETMIN" -ge 1220 ]; then
  echo "$STAMP  guard: ${ETMIN}min ET (not 20:00-20:20) — skip" >> "$OUT/cron.log"; exit 0
fi
if [ "$WD" -ge 6 ]; then
  echo "$STAMP  weekend — skip" >> "$OUT/cron.log"; exit 0
fi
HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
HOLIDAYS_2027="2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24"
case " $HOLIDAYS_2026 $HOLIDAYS_2027 " in
  *" $TODAY "*) echo "$STAMP  HOLIDAY $TODAY — skip" >> "$OUT/cron.log"; exit 0 ;;
esac

# --- DSN from the running OMS process env (avoids the root-only env file). OMS down => dsn empty. ---
pid=$(systemctl show project-mai-tai-oms -p MainPID --value 2>/dev/null)
dsn=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^MAI_TAI_DATABASE_URL=//p' \
      | sed -E 's#^postgres(ql)?\+[a-z0-9_]+://#postgresql://#')
if [ -z "$dsn" ]; then
  send_ntfy "🔴 v2-naked CHECK BLIND" "urgent" "rotating_light" \
    "[v2-overnight-naked] cannot read the DSN (OMS pid='$pid' — likely DOWN) at 20:05 ET. The naked-position backstop is BLIND — verify v2 positions BY HAND."
  echo "$STAMP  RED: no DSN (oms pid=$pid)" >> "$OUT/cron.log"; exit 2
fi

Q="SELECT count(*) FROM oms_managed_positions WHERE strategy_code='schwab_1m_v2' AND status <> 'closed' AND current_quantity <> 0;"
n=$(psql "$dsn" -tAc "$Q" 2>>"$OUT/alert.log")
case "$n" in
  ''|*[!0-9]*) send_ntfy "🔴 v2-naked CHECK ERROR" "urgent" "rotating_light" \
      "[v2-overnight-naked] position query failed (got '$n'). Backstop cannot confirm flat — verify BY HAND."
    echo "$STAMP  RED: query error ('$n')" >> "$OUT/cron.log"; exit 2 ;;
esac
if [ "$n" -gt 0 ]; then
  detail=$(psql "$dsn" -tAc "SELECT string_agg(symbol||':'||current_quantity, ', ') FROM oms_managed_positions WHERE strategy_code='schwab_1m_v2' AND status <> 'closed' AND current_quantity <> 0;" 2>/dev/null)
  send_ntfy "🔴 v2 NAKED OVERNIGHT" "urgent" "rotating_light" \
    "[v2-overnight-naked] $n open v2 position(s) past the 20:00 gate: ${detail:-?}. The 19:55 flatten FAILED — CLOSE BY HAND (no software fill until 07:00; no native stop)."
  echo "$STAMP  RED: $n open ($detail)" >> "$OUT/cron.log"; exit 2
fi
echo "$STAMP  GREEN: v2 flat after the 20:00 gate" >> "$OUT/cron.log"; exit 0
