#!/bin/bash
# Fleet function-health cron target. The evaluator reports every check, but only an
# explicitly classified LIVE_MONEY RED can page. PAPER, DIAGNOSTIC, SCOREBOARD, and
# the aggregate summary remain on-box evidence.
#
# Alerting is transition-based: one page when a live-money condition becomes RED,
# silence while it remains RED, and silent recovery. Delivery must be accepted before
# the transition is recorded, so a failed page retries on the next run.
set -u

SELFTEST=0
[ "${1:-}" = "--selftest" ] && SELFTEST=1

CHECK="${FLEET_HEALTH_CHECK:-/home/trader/project-mai-tai/ops/health/fleet_health_check.py}"
PYTHON="${FLEET_HEALTH_PYTHON:-python3}"
CURL="${FLEET_HEALTH_CURL:-curl}"
NTFY_URL="${FLEET_HEALTH_NTFY_URL:-https://ntfy.sh/mai-tai-preopen-28806a5a97b7}"
OUT="${FLEET_HEALTH_OUT:-/home/trader/fleet_health}"
ACTIVE="$OUT/paged.active"
mkdir -p "$OUT"
touch "$ACTIVE"

STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
TODAY=$(TZ=America/New_York date +%F)
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))

send_ntfy() {  # $1=title $2=priority $3=tags $4=body
  "$CURL" -sS --fail-with-body --connect-timeout 10 --max-time 30 \
    -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" "$NTFY_URL" \
    >/dev/null 2>>"$OUT/alert.log"
}

if [ "$SELFTEST" -eq 1 ]; then
  send_ntfy "mai-tai function-health SELFTEST" "default" "white_check_mark" \
    "[SELFTEST] end-to-end alerting path OK; benign confirmation, no action needed."
  RESULT=$?
  echo "$STAMP  SELFTEST delivery_exit=$RESULT" >> "$OUT/cron.log"
  exit "$RESULT"
fi

if [ "${FLEET_HEALTH_TEST_MODE:-0}" != "1" ]; then
  # 09:35 <= ET < 16:05. Every evaluator also applies its own correctness guard.
  if [ "$ETMIN" -lt 575 ] || [ "$ETMIN" -ge 965 ]; then
    echo "$STAMP  guard: outside 09:35-16:05 ET (now ${ETMIN}min ET); skip" >> "$OUT/cron.log"
    exit 0
  fi
  HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
  HOLIDAYS_2027="2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24"
  case " $HOLIDAYS_2026 $HOLIDAYS_2027 " in
    *" $TODAY "*)
      echo "$STAMP  HOLIDAY $TODAY; skipped" >> "$OUT/cron.log"
      exit 0
      ;;
  esac
fi

OUTFILE="$OUT/latest.txt"
"$PYTHON" "$CHECK" > "$OUTFILE" 2>&1
CODE=$?
SUMMARY=$(grep '^SUMMARY:' "$OUTFILE" | tail -1)
VERDICT_COUNT=$(grep -c '^VERDICT:' "$OUTFILE" || true)
echo "$STAMP  exit=$CODE verdicts=$VERDICT_COUNT ${SUMMARY:-<no-summary>}" >> "$OUT/cron.log"

CURRENT="$OUT/current.active"
NEXT="$OUT/next.active"
: > "$CURRENT"
: > "$NEXT"

case "$CODE" in
  0|1|2)
    if [ -z "$SUMMARY" ] || [ "$VERDICT_COUNT" -eq 0 ]; then
      printf '%s|%s\n' "monitor-error" "fleet_health_check produced incomplete output (exit=$CODE)" > "$CURRENT"
    else
      grep '^VERDICT: RED .* class=LIVE_MONEY ' "$OUTFILE" \
        | awk '{name=$3; print "live-money:" name "|" $0}' > "$CURRENT" || true
    fi
    ;;
  *)
    printf '%s|%s\n' "monitor-error" "fleet_health_check failed (exit=$CODE)" > "$CURRENT"
    ;;
esac

while IFS='|' read -r FP DETAIL; do
  [ -z "$FP" ] && continue
  if grep -Fxq "$FP" "$ACTIVE"; then
    printf '%s\n' "$FP" >> "$NEXT"
    continue
  fi

  if [ "$FP" = "monitor-error" ]; then
    TITLE="mai-tai health-check ERROR"
    BODY="$DETAIL. The monitor may be broken; inspect $OUTFILE."
  else
    NAME=${FP#live-money:}
    TITLE="RED mai-tai live-money check: $NAME"
    BODY="$DETAIL

This is an actionable live-money finding. PAPER, DIAGNOSTIC, SCOREBOARD, and aggregate results never page."
  fi

  if send_ntfy "$TITLE" "urgent" "rotating_light" "$BODY"; then
    printf '%s\n' "$FP" >> "$NEXT"
    echo "$STAMP  ALERT sent $FP" >> "$OUT/alert.log"
  else
    echo "$STAMP  ALERT delivery failed $FP; retry next run" >> "$OUT/alert.log"
  fi
done < "$CURRENT"

sort -u "$NEXT" -o "$NEXT"
mv -f "$NEXT" "$ACTIVE"
exit 0
