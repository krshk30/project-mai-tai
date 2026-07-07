#!/bin/bash
# Fleet FUNCTION-health — cron target (F3). Runs fleet_health_check.py (validates
# FUNCTION vs ground truth, not self-report) and pushes ntfy on a genuine RED.
#
#   Independent monitoring (stdlib + psql/redis-cli), read-only — NOT a service, so it
#   can't hang the way the thing it watches can. Same machinery as the OMS-liveness
#   watchdog: ET wall-clock window guard (CRON_TZ is ignored on this box), NYSE holiday
#   skip, anti-spam state (alert healthy->RED, cooldown while RED, one GREEN recovery).
#
#   Window: 09:35 <= ET < 16:05 (after the open settles, through the close) — when bars
#   are expected. Each check is ALSO self-gating (check #1 only reds when the upstream
#   feed is simultaneously live), so the window is just noise-avoidance, not correctness.
#
#   `--selftest`: bypass guard/holiday, FORCE a RED push (phone-landing check). No DB/Redis.
set -u

SELFTEST=0
[ "${1:-}" = "--selftest" ] && SELFTEST=1

CHECK=/home/trader/project-mai-tai/ops/health/fleet_health_check.py
NTFY_URL="https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
COOLDOWN_SECS=900   # re-alert at most every 15 min while RED
OUT=/home/trader/fleet_health
STATE="$OUT/state"  # holds: <STATUS> <LAST_ALERT_EPOCH>
mkdir -p "$OUT"

STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
TODAY=$(TZ=America/New_York date +%F)
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))

send_ntfy() {  # $1=title $2=priority $3=tags $4=body
  curl -s -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" "$NTFY_URL" \
    >/dev/null 2>>"$OUT/alert.log"
}

if [ "$SELFTEST" -eq 1 ]; then
  # Benign GREEN confirmation — a selftest verifies the end-to-end alerting path LANDS; it
  # must NOT look like a real alarm (default priority so it reliably lands, not min).
  send_ntfy "✅ mai-tai function-health SELFTEST" "default" "white_check_mark" \
    "[SELFTEST] end-to-end alerting path OK — benign confirmation, no action needed."
  echo "$STAMP  SELFTEST green-confirmation push sent" >> "$OUT/cron.log"
  exit 0
fi

# ---- ET window guard: 09:35 (575) <= ET < 16:05 (965) ----
if [ "$ETMIN" -lt 575 ] || [ "$ETMIN" -ge 965 ]; then
  echo "$STAMP  guard: outside 09:35-16:05 ET (now ${ETMIN}min ET) — skip" >> "$OUT/cron.log"
  exit 0
fi
# ---- NYSE full-closure holidays (reused list; UPDATE ANNUALLY) ----
HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
HOLIDAYS_2027="2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24"
case " $HOLIDAYS_2026 $HOLIDAYS_2027 " in
  *" $TODAY "*)
    echo "$STAMP  HOLIDAY $TODAY — skipped" >> "$OUT/cron.log"; exit 0 ;;
esac

OUTFILE="$OUT/latest.txt"
python3 "$CHECK" > "$OUTFILE" 2>&1
CODE=$?
VERDICT=$(grep '^VERDICT:' "$OUTFILE" | tail -1)   # the aggregate line
echo "$STAMP  exit=$CODE  $VERDICT" >> "$OUT/cron.log"

PREV_STATUS="OK"; LAST_ALERT=0
[ -f "$STATE" ] && read -r PREV_STATUS LAST_ALERT < "$STATE" 2>/dev/null || true
NOW=$(date +%s)

# Alert policy (keep pushes to genuine, actionable transitions):
#   exit 0 GREEN -> OK (+ one recovery push on RED->GREEN)
#   exit 1 AMBER -> on-box log ONLY, no push (precursor; treated OK for anti-spam)
#   exit 2 RED   -> urgent push (with a real VERDICT), anti-spammed
#   other/crash  -> urgent push labeled MONITOR ERROR (distinct from fleet-unhealthy, so a
#                   broken check is never mistaken for a broken fleet)
if [ "$CODE" -eq 0 ] || [ "$CODE" -eq 1 ]; then
  STATUS=OK
else
  STATUS=RED
fi

if [ "$STATUS" = "RED" ]; then
  if [ "$PREV_STATUS" != "RED" ] || [ $(( NOW - LAST_ALERT )) -ge "$COOLDOWN_SECS" ]; then
    if [ -z "$VERDICT" ]; then
      send_ntfy "⚠️ mai-tai health-check ERROR" "urgent" "rotating_light" \
        "fleet_health_check produced no verdict (exit=$CODE) — the MONITOR may be broken, not necessarily the fleet. ssh mai-tai-vps 'cat $OUTFILE'"
      echo "$STAMP  ALERT[MONITOR-ERROR] sent exit=$CODE" >> "$OUT/alert.log"
    else
      send_ntfy "🔴 mai-tai FUNCTION UNHEALTHY" "urgent" "rotating_light" \
        "$VERDICT — detail: ssh mai-tai-vps 'cat $OUTFILE'"
      echo "$STAMP  ALERT[RED] sent  $VERDICT" >> "$OUT/alert.log"
    fi
    LAST_ALERT=$NOW
  fi
  echo "RED $LAST_ALERT" > "$STATE"
else
  if [ "$PREV_STATUS" = "RED" ]; then
    send_ntfy "✅ mai-tai function-health recovered" "default" "white_check_mark" "$VERDICT"
    echo "$STAMP  ALERT[GREEN] recovery sent  $VERDICT" >> "$OUT/alert.log"
  fi
  echo "OK $LAST_ALERT" > "$STATE"
fi
exit 0
