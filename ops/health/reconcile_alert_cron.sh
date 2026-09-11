#!/bin/bash
# Page actionable, live-money reconciliation incidents once per active transition.
# Manual broker positions are classified INFO by the reconciler and never reach this
# wrapper. A delivery is recorded only after ntfy accepts it.
set -u

SELFTEST=0
[ "${1:-}" = "--selftest" ] && SELFTEST=1

OUT="${RECONCILE_ALERT_OUT:-/home/trader/reconcile_alert}"
LOG="$OUT/watch.log"
ACTIVE="$OUT/paged.active"
CURL="${RECONCILE_ALERT_CURL:-curl}"
NTFY_URL="${RECONCILE_ALERT_NTFY_URL:-https://ntfy.sh/mai-tai-preopen-28806a5a97b7}"
mkdir -p "$OUT"
touch "$ACTIVE"

STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
TODAY=$(TZ=America/New_York date +%F)
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))
ETDOW=$(TZ=America/New_York date '+%u')

if [ "$SELFTEST" -eq 0 ] && [ "${RECONCILE_ALERT_TEST_MODE:-0}" != "1" ]; then
  [ "$ETDOW" -gt 5 ] && exit 0
  # A position can remain exposed through the extended-hours tail.
  { [ "$ETMIN" -lt 420 ] || [ "$ETMIN" -ge 1230 ]; } && exit 0
  HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
  case " $HOLIDAYS_2026 " in *" $TODAY "*) exit 0 ;; esac
fi

if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 5000000 ]; then
  mv -f "$LOG" "$LOG.1"
fi

send_ntfy() {  # $1=title $2=priority $3=tags $4=body
  "$CURL" -sS --fail-with-body --connect-timeout 10 --max-time 30 \
    -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" "$NTFY_URL" \
    >/dev/null 2>>"$OUT/alert.log"
}

if [ "$SELFTEST" -eq 1 ]; then
  ROWS="selftest:fingerprint|position_quantity_mismatch|TEST|[SELFTEST] owned position mismatch|live:orb|broker_missing_owned_position|0|1|1"
elif [ "${RECONCILE_ALERT_ROWS+x}" = "x" ]; then
  ROWS=$RECONCILE_ALERT_ROWS
else
  DSN=$(sudo -n grep -E '^MAI_TAI_DATABASE_URL=' /etc/project-mai-tai/project-mai-tai.env 2>>"$LOG" \
        | head -1 | cut -d= -f2- | sed 's|postgresql+psycopg://|postgresql://|')
  [ -z "$DSN" ] && { echo "$STAMP  ERROR: no DSN" >> "$LOG"; exit 1; }

  # Open incidents are the reconciler's durable transition state. Using recent finding rows
  # would falsely clear an active page whenever one poll was late.
  ROWS=$(psql "$DSN" -tAF'|' -c "
    SELECT payload->>'fingerprint', payload->>'finding_type', coalesce(payload->>'symbol','-'),
           title, payload->>'account_name', coalesce(payload->>'direction','unknown'),
           coalesce(payload->>'account_quantity','0'), coalesce(payload->>'our_quantity','0'),
           coalesce(payload->>'net_fill_balance','0')
      FROM system_incidents
     WHERE service_name='reconciler'
       AND severity='critical'
       AND status IN ('open','acknowledged')
       AND payload->>'account_name' IN ('live:schwab_1m_v2','live:orb')
     ORDER BY payload->>'fingerprint';" 2>>"$LOG")
fi

CURRENT="$OUT/current.active"
NEXT="$OUT/next.active"
: > "$CURRENT"
: > "$NEXT"

if [ -n "$ROWS" ]; then
  printf '%s\n' "$ROWS" | while IFS='|' read -r FP KIND SYM TITLE ACCOUNT DIRECTION BROKER BOOK FILLS; do
    [ -z "$FP" ] && continue
    printf '%s\n' "$FP" >> "$CURRENT"
    if grep -Fxq "$FP" "$ACTIVE"; then
      printf '%s\n' "$FP" >> "$NEXT"
      continue
    fi

    BODY="$TITLE
type=$KIND symbol=$SYM account=$ACCOUNT direction=$DIRECTION
broker_quantity=$BROKER live_book_quantity=$BOOK net_fill_balance=$FILLS
fingerprint=$FP

The reconciler reports only; nothing has been changed. A nonzero ownership signal that disagrees with the broker is a live-money finding."
    if send_ntfy "RED reconcile drift: $SYM" "urgent" "rotating_light" "$BODY"; then
      printf '%s\n' "$FP" >> "$NEXT"
      echo "$STAMP  ALERT sent $KIND $ACCOUNT $SYM $FP" >> "$OUT/alert.log"
    else
      echo "$STAMP  ALERT delivery failed $FP; retry next run" >> "$OUT/alert.log"
    fi
  done
else
  echo "$STAMP  no active critical live-money reconciliation incidents" >> "$LOG"
fi

sort -u "$NEXT" -o "$NEXT"
mv -f "$NEXT" "$ACTIVE"
exit 0
