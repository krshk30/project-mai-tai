#!/bin/bash
# Schwab refresh-token expiry warning — cron target. Runs ~08:00 and ~18:00 ET.
#   The refresh_token's ~7-day clock is invisible everywhere else; this is the only
#   proactive heads-up before it dies (a dead token = every Schwab call invalid_grant
#   until a human re-auths at /auth/schwab/start).
#   ET wall-clock guard (TZ=America/New_York, reliable on this box) makes a DST-safe
#   dual-UTC schedule work: fire at 12,13,22,23 UTC; the guard runs only the two that
#   are ~08:00 / ~18:00 ET. No holiday/weekend skip — the 7-day clock does not care.
#   Verdict exit 0/1/2 (green/amber/red) -> ntfy (green is silent to avoid daily spam).
#   Crontab:  2 12,13,22,23 * * *  /home/trader/schwab_token_expiry_cron.sh
set -u
OUT=/home/trader/preopen_out
mkdir -p "$OUT"
STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
ETH=$(TZ=America/New_York date '+%H')
ETM=$(TZ=America/New_York date '+%M')

# ---- ET wall-clock guard: only run near 08:00 or 18:00 ET (tolerate cron jitter) ----
if { [ "$ETH" != "08" ] && [ "$ETH" != "18" ]; } || [ "$ETM" -gt 12 ]; then
  echo "$STAMP  guard: not ~08/18:00 ET (now ${ETH}:${ETM} ET) — skip" >> "$OUT/schwab_token_cron.log"
  exit 0
fi

OUTFILE="$OUT/schwab_token_expiry_latest.txt"
export MAI_TAI_SCHWAB_TOKEN_STORE_PATH="${MAI_TAI_SCHWAB_TOKEN_STORE_PATH:-/var/lib/macd-webhook-server/data/schwab_tokens.json}"
python3 /home/trader/schwab_token_expiry_check.py > "$OUTFILE" 2>&1
CODE=$?
VERDICT=$(grep '^VERDICT:' "$OUTFILE" | head -1)
echo "$STAMP  exit=$CODE  $VERDICT" >> "$OUT/schwab_token_cron.log"

TOPIC=${SCHWAB_TOKEN_TOPIC:-mai-tai-preopen-28806a5a97b7}
URL="https://ntfy.sh/$TOPIC"
case "$CODE" in
  2) curl -s -H "Title: 🔴 Schwab token expiring" -H "Priority: urgent" -H "Tags: rotating_light" \
        -d "${VERDICT:-Schwab refresh_token RED}" "$URL" >/dev/null 2>>"$OUT/schwab_token_cron.log" ;;
  1) curl -s -H "Title: 🟡 Schwab token" -H "Priority: default" -H "Tags: warning" \
        -d "${VERDICT:-Schwab refresh_token AMBER}" "$URL" >/dev/null 2>>"$OUT/schwab_token_cron.log" ;;
  0) : ;;  # GREEN — silent (detail stays on-box in schwab_token_expiry_latest.txt)
  *) curl -s -H "Title: 🔴 Schwab token check ERROR" -H "Priority: urgent" -H "Tags: rotating_light" \
        -d "${VERDICT:-token expiry check crashed}" "$URL" >/dev/null 2>>"$OUT/schwab_token_cron.log" ;;
esac
exit 0
