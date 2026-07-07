#!/bin/bash
# Intraday OMS-liveness watchdog — cron target (built 2026-07-05).
#   Detects an OMS zombie in ~minutes, not hours (the 07-02 SPOF ran ~5h
#   undetected; the readiness cron is pre-open-only). Reads the oms-risk
#   heartbeat from Redis via oms_liveness_check.py (INDEPENDENT of the OMS
#   process — a blocked OMS event loop cannot kill this).
#
#   Window: 07:00 <= ET < 18:15 (full pre-market -> post-market to the v2 bot
#   cutoff + a 15-min tail for a late EH exit that needs the OMS alive). Weekdays
#   enforced by crontab (1-5). NYSE full-closure holiday-skip (reused list).
#   ET wall-clock via `TZ=America/New_York date` (CRON_TZ not honored on this box);
#   DST-safe dual-UTC crontab, the guard runs the body only inside the ET window.
#
#   Anti-spam: alerts on healthy->stale, then at most every COOLDOWN_SECS while
#   stale; one GREEN 'recovered' on stale->healthy.
#
#   `--selftest`: bypass guard/holiday/cooldown, FORCE a stale verdict, send the
#   RED push (phone-landing verification). Does NOT touch the OMS or Redis.
#
#   TEMPLATE for the health-system's future per-component watchdogs: copy this +
#   set WATCH_SERVICE / WATCH_STALE_SECS.
set -u

SELFTEST=0
[ "${1:-}" = "--selftest" ] && SELFTEST=1

WATCH_SERVICE="oms-risk"
WATCH_STALE_SECS=180          # 3 min = 12x the 15s heartbeat interval (zero-false-positive priority)
COOLDOWN_SECS=600             # re-alert at most every 10 min while stale
NTFY_URL="https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
CHECK=/home/trader/oms_liveness_check.py
OUT=/home/trader/oms_watch
STATE="$OUT/state"            # holds: <STATUS> <LAST_ALERT_EPOCH>
mkdir -p "$OUT"

STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
TODAY=$(TZ=America/New_York date +%F)
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))

if [ "$SELFTEST" -eq 0 ]; then
  # ---- ET window guard: 07:00 (420) <= ET < 18:15 (1095) ----
  if [ "$ETMIN" -lt 420 ] || [ "$ETMIN" -ge 1095 ]; then
    echo "$STAMP  guard: outside 07:00-18:15 ET (now ${ETMIN}min ET) — skip" >> "$OUT/cron.log"
    exit 0
  fi
  # ---- NYSE full-closure holidays (reused from readiness cron; UPDATE ANNUALLY) ----
  HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
  HOLIDAYS_2027="2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24"
  case " $HOLIDAYS_2026 $HOLIDAYS_2027 " in
    *" $TODAY "*)
      echo "$STAMP  HOLIDAY $TODAY — skipped" >> "$OUT/cron.log"
      exit 0 ;;
  esac
fi

# ---- run the liveness check (independent Redis read) ----
if [ "$SELFTEST" -eq 1 ]; then
  VERDICT=$(WATCH_SERVICE="$WATCH_SERVICE" WATCH_STALE_SECS="$WATCH_STALE_SECS" WATCH_SIMULATE_AGE=99999 python3 "$CHECK")
  CODE=$?
else
  VERDICT=$(WATCH_SERVICE="$WATCH_SERVICE" WATCH_STALE_SECS="$WATCH_STALE_SECS" python3 "$CHECK")
  CODE=$?
fi
echo "$STAMP  exit=$CODE  $VERDICT" >> "$OUT/cron.log"

# ---- state + cooldown ----
PREV_STATUS="OK"; LAST_ALERT=0
[ -f "$STATE" ] && read -r PREV_STATUS LAST_ALERT < "$STATE" 2>/dev/null || true
NOW=$(date +%s)

send_ntfy() {  # $1=title $2=priority $3=tags $4=body
  curl -s -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" "$NTFY_URL" \
    >/dev/null 2>>"$OUT/alert.log"
}

if [ "$CODE" -ne 0 ]; then
  # RED / ERROR (check crash also != 0 -> treat as RED)
  if [ "$PREV_STATUS" != "RED" ] || [ $(( NOW - LAST_ALERT )) -ge "$COOLDOWN_SECS" ] || [ "$SELFTEST" -eq 1 ]; then
    BODY="${VERDICT:-oms-risk liveness RED} — check: ssh mai-tai-vps 'systemctl status project-mai-tai-oms' + py-spy; restart is attended."
    [ "$SELFTEST" -eq 1 ] && BODY="[SELFTEST] $BODY"
    send_ntfy "🔴 OMS LIVENESS ALERT" "urgent" "rotating_light" "$BODY"
    echo "$STAMP  ALERT[RED] sent  $VERDICT" >> "$OUT/alert.log"
    LAST_ALERT=$NOW
  fi
  [ "$SELFTEST" -eq 0 ] && echo "RED $LAST_ALERT" > "$STATE"
else
  # OK — send one recovery green if we were RED
  if [ "$PREV_STATUS" = "RED" ]; then
    send_ntfy "✅ OMS LIVENESS recovered" "default" "white_check_mark" "$VERDICT"
    echo "$STAMP  ALERT[GREEN] recovery sent  $VERDICT" >> "$OUT/alert.log"
  fi
  echo "OK $LAST_ALERT" > "$STATE"
fi
exit 0
