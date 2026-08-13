#!/bin/bash
# RECONCILER CRITICAL-FINDING ALERT — push the drift the reconciler already detects.
#
# ⭐⭐ WHY (2026-07-30). The reconciler works. On IRE it flagged
#     `position_quantity_mismatch` severity=CRITICAL at 12:55:22 -- eight minutes after a phantom
#     2-share fill -- and repeated it 71 times over 35 minutes. NOBODY WAS EVER TOLD. There was no
#     alerting on reconciliation findings of any kind. The operator found the discrepancy himself,
#     on a chart, hours later.
#
# ⛔ The detection was never the missing piece. The PATH FROM FINDING TO HUMAN was.
#   (Standing question: "has the other bot already solved this?" -- here, yes. Do not rebuild it.)
#
# ⭐ WHY FINGERPRINT-DEDUPED AND CRITICAL-ONLY. Raw volume on 2026-07-30:
#       stuck_intent                warning   3954
#       position_quantity_mismatch  CRITICAL  1149
#       stuck_order                 warning     69
#       average_price_mismatch      warning     64
#   -- but only **7 DISTINCT critical fingerprints** all day. Alerting per finding would push 1149
#   times and be muted within a week; alerting per fingerprint pushes 7. The reconciler already
#   emits a stable `fingerprint` (e.g. `position-quantity:live:schwab_1m_v2:IRE`) -- use it.
#
# ⛔ Guards enforced HERE in ET; crontab is deliberately `*/5 * * * *`. CRON_TZ is IGNORED on this
# box, so a crontab hour range is a UTC range and cannot express an ET window.
# ⛔ Runs as ROOT from root's crontab -- the env file is root-readable only.
#
#   `--selftest`: bypass window/cooldown and force one push.
set -u

SELFTEST=0
[ "${1:-}" = "--selftest" ] && SELFTEST=1

OUT=/home/trader/reconcile_alert
LOG="$OUT/watch.log"
SEEN="$OUT/seen"                   # fingerprints already alerted: "<epoch> <fingerprint>"
NTFY_URL="https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
COOLDOWN_SECS=3600                 # re-alert an UNRESOLVED fingerprint at most hourly
LOOKBACK_MIN=10
mkdir -p "$OUT"; touch "$SEEN"

STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
TODAY=$(TZ=America/New_York date +%F)
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))
ETDOW=$(TZ=America/New_York date '+%u')

if [ "$SELFTEST" -eq 0 ]; then
  [ "$ETDOW" -gt 5 ] && exit 0
  # 07:00 (420) .. 20:30 (1230) ET — a position can exist through the EH tail, and drift on a
  # position we hold overnight is exactly the thing worth waking up for.
  { [ "$ETMIN" -lt 420 ] || [ "$ETMIN" -ge 1230 ]; } && exit 0
  HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
  case "$HOLIDAYS_2026" in *"$TODAY"*) exit 0 ;; esac
fi

if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 5000000 ]; then
  mv -f "$LOG" "$LOG.1"
fi

# ⛔⭐ EXCLUDE THE OPERATOR'S OWN POSITIONS. They mismatch by DESIGN -- the broker holds them and
# our books correctly do not (the OMS acts only on positions it placed). CYN alone would push once
# an hour forever and mute this channel inside a week. Sourced from MAI_TAI_PROTECTED_SYMBOLS so
# the exclusion list and the trading hard-block can never drift apart.
# ⚠️ A manual position NOT in PROTECTED_SYMBOLS still alerts -- correctly. On 2026-07-30 the
# operator held TE -3000 which was NOT protected; that is a gap to close, not noise to hide.
PROTECTED=$(sudo -n grep -E '^MAI_TAI_PROTECTED_SYMBOLS=' /etc/project-mai-tai/project-mai-tai.env 2>/dev/null             | head -1 | cut -d= -f2- | tr -d '"'"'"'"'"'" )
EXCLUDE_SQL=""
if [ -n "$PROTECTED" ]; then
  EXCLUDE_SQL=" AND coalesce(symbol,'-') NOT IN ('$(printf '%s' "$PROTECTED" | sed "s/,/','/g")')"
fi

DSN=$(sudo -n grep -E '^MAI_TAI_DATABASE_URL=' /etc/project-mai-tai/project-mai-tai.env 2>/dev/null \
      | head -1 | cut -d= -f2- | sed 's|postgresql+psycopg://|postgresql://|')
[ -z "$DSN" ] && { echo "$STAMP  ERROR: no DSN" >> "$LOG"; exit 1; }

send_ntfy() {  # $1=title $2=priority $3=tags $4=body
  # ⛔ Titles must be ASCII — an em-dash silently LOSES the push (learned on the OCO watch).
  curl -s -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" "$NTFY_URL" \
    >/dev/null 2>>"$OUT/alert.log"
}

NOW=$(date +%s)

# One row per DISTINCT critical fingerprint seen recently, newest payload wins.
ROWS=$(psql "$DSN" -tAF'|' -c "
  SELECT DISTINCT ON (payload->>'fingerprint')
         payload->>'fingerprint', finding_type, coalesce(symbol,'-'),
         coalesce(payload->>'title', finding_type)
  FROM reconciliation_findings
  WHERE severity='critical'
    AND created_at >= now() - make_interval(mins => ${LOOKBACK_MIN})${EXCLUDE_SQL}
  ORDER BY payload->>'fingerprint', created_at DESC;" 2>>"$LOG")

if [ "$SELFTEST" -eq 1 ]; then
  ROWS="selftest:fingerprint|position_quantity_mismatch|TEST|[SELFTEST] Position quantity mismatch"
fi

[ -z "$ROWS" ] && { echo "$STAMP  no critical findings in the last ${LOOKBACK_MIN}m" >> "$LOG"; exit 0; }

echo "$ROWS" | while IFS='|' read -r FP KIND SYM TITLE; do
  [ -z "$FP" ] && continue
  LAST=$(grep -F " $FP" "$SEEN" 2>/dev/null | tail -1 | awk '{print $1}')
  if [ -n "$LAST" ] && [ "$SELFTEST" -eq 0 ] && [ $(( NOW - LAST )) -lt "$COOLDOWN_SECS" ]; then
    continue
  fi
  BODY="$TITLE
type=$KIND symbol=$SYM
fingerprint=$FP

The reconciler DETECTS drift but never repairs it -- this is a report, nothing has been changed.
Broker truth vs our books: ssh mai-tai-vps then compare account_positions with virtual_positions.
⛔ A position the OMS does not know about is one it will NOT exit. Check before the close."
  send_ntfy "RED reconcile drift: $SYM" "urgent" "rotating_light" "$BODY"
  echo "$NOW $FP" >> "$SEEN"
  echo "$STAMP  ALERT sent $KIND $SYM $FP" >> "$OUT/alert.log"
done

# keep the seen-file bounded
tail -500 "$SEEN" > "$SEEN.tmp" 2>/dev/null && mv -f "$SEEN.tmp" "$SEEN"
exit 0
