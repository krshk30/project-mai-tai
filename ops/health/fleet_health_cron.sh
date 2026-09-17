#!/bin/bash
# Fleet health cron target. LIVE_MONEY findings page only during the established market-hours
# run. FLEET_RUNTIME restart storms page around the clock. PAPER, DIAGNOSTIC, SCOREBOARD, and the
# aggregate summary remain on-box evidence.
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
ETDOW=$(TZ=America/New_York date +%u)
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

MODE="${FLEET_HEALTH_MODE:-auto}"
if [ "${FLEET_HEALTH_TEST_MODE:-0}" = "1" ]; then
  MODE="${FLEET_HEALTH_MODE:-full}"
elif [ "$MODE" = "auto" ]; then
  MODE="runtime"
  HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
  HOLIDAYS_2027="2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24"
  case " $HOLIDAYS_2026 $HOLIDAYS_2027 " in *" $TODAY "*) HOLIDAY=1;; *) HOLIDAY=0;; esac
  if [ "$ETDOW" -le 5 ] && [ "$HOLIDAY" -eq 0 ] \
    && [ "$ETMIN" -ge 575 ] && [ "$ETMIN" -lt 965 ]; then
    MODE="full"
  fi
fi
case "$MODE" in full|runtime) ;; *) echo "invalid FLEET_HEALTH_MODE=$MODE" >&2; exit 3;; esac

OUTFILE="$OUT/latest.txt"
if [ "$MODE" = "runtime" ]; then
  "$PYTHON" "$CHECK" --runtime-only > "$OUTFILE" 2>&1
else
  "$PYTHON" "$CHECK" > "$OUTFILE" 2>&1
fi
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
      grep -E '^VERDICT: RED .* class=(LIVE_MONEY|FLEET_RUNTIME) ' "$OUTFILE" \
        | awk '{class=""; for (i=1;i<=NF;i++) if ($i ~ /^class=/) {class=$i; sub(/^class=/,"",class)}; prefix=(class=="FLEET_RUNTIME" ? "fleet-runtime" : "live-money"); print prefix ":" $3 "|" $0}' > "$CURRENT" || true
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
  elif [ "${FP#fleet-runtime:}" != "$FP" ]; then
    NAME=${FP#fleet-runtime:}
    TITLE="RED mai-tai service runtime: $NAME"
    BODY="$DETAIL

An expected-running project service is stopped or restarting repeatedly. Inspect systemd before relying on that component."
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
