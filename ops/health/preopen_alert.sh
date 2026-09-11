#!/bin/bash
# Shared ntfy adapter for pre-open and seed-exposure alerts. A non-zero exit means
# delivery was not confirmed and callers must not consume their transition latch.
set -u

LEVEL="$1"
VERDICT="${2:-}"
FILE="${3:-}"
OUT="${PREOPEN_ALERT_OUT:-/home/trader/preopen_out}"
URL="${PREOPEN_ALERT_URL:-https://ntfy.sh/mai-tai-preopen-28806a5a97b7}"
CURL="${PREOPEN_ALERT_CURL:-curl}"
STAMP=$(date '+%F %H:%M:%S %Z')
mkdir -p "$OUT"

case "$LEVEL" in
  RED|ERROR)
    TITLE="RED mai-tai NOT READY ($LEVEL)"
    PRIORITY="urgent"
    TAGS="rotating_light"
    BODY="${VERDICT:-readiness $LEVEL}; you have until 09:30 ET. Detail: $FILE"
    ;;
  AMBER)
    TITLE="AMBER mai-tai readiness warnings"
    PRIORITY="default"
    TAGS="warning"
    BODY="$VERDICT"
    ;;
  GREEN)
    TITLE="GREEN mai-tai fleet ready"
    PRIORITY="min"
    TAGS="white_check_mark"
    BODY="$VERDICT"
    ;;
  *)
    echo "$STAMP  ERROR unsupported alert level=$LEVEL" >> "$OUT/alert.log"
    exit 2
    ;;
esac

if "$CURL" -sS --fail-with-body --connect-timeout 10 --max-time 30 \
  -H "Title: $TITLE" -H "Priority: $PRIORITY" -H "Tags: $TAGS" -d "$BODY" "$URL" \
  >/dev/null 2>>"$OUT/alert.log"; then
  echo "$STAMP  DELIVERED[$LEVEL] $VERDICT" >> "$OUT/alert.log"
  exit 0
fi

echo "$STAMP  DELIVERY-FAILED[$LEVEL] $VERDICT" >> "$OUT/alert.log"
exit 1
