#!/bin/bash
# Alert channel adapter — ntfy.sh push (operator-chosen 2026-07-01).
#   Args: $1=LEVEL (RED|AMBER|GREEN|ERROR)  $2=verdict line  $3=full-output file
#   RED/ERROR => urgent push; AMBER => default; GREEN => min (daily liveness ping).
#   Full detail stays on-box (readiness_latest.txt); only the verdict transits ntfy.
set -u
LEVEL="$1"; VERDICT="${2:-}"; FILE="${3:-}"
OUT=/home/trader/preopen_out
STAMP=$(date '+%F %H:%M:%S %Z')
echo "$STAMP  ALERT[$LEVEL]  $VERDICT" >> "$OUT/alert.log"
URL="https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
case "$LEVEL" in
  RED|ERROR)
    curl -s -H "Title: 🔴 mai-tai NOT READY ($LEVEL)" -H "Priority: urgent" -H "Tags: rotating_light"       -d "${VERDICT:-readiness $LEVEL} — you have until 09:30 ET. Detail: ssh mai-tai-vps 'cat /home/trader/preopen_out/readiness_latest.txt'"       "$URL" >/dev/null 2>>"$OUT/alert.log" ;;
  AMBER)
    curl -s -H "Title: 🟡 mai-tai readiness: warnings" -H "Priority: default" -H "Tags: warning"       -d "$VERDICT" "$URL" >/dev/null 2>>"$OUT/alert.log" ;;
  GREEN)
    curl -s -H "Title: ✅ mai-tai fleet ready" -H "Priority: min" -H "Tags: white_check_mark"       -d "$VERDICT" "$URL" >/dev/null 2>>"$OUT/alert.log" ;;
esac
exit 0
