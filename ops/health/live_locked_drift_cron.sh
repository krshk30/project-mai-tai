#!/bin/bash
# LIVE-LOCKED DRIFT WATCH — page when the box stops running the configuration we think it runs.
#
# ⭐ WHY THIS EXISTS AT ALL (operator, 2026-09-09): "If those settings ever get wiped, the bot
# silently falls back to +2% / -5% - and the tool that watches for drift files that as 'unset',
# not as drift. So it would look fine while running the old numbers."
#
# ⛔⭐⭐ THE WATCHDOG WAS REAL AND UNSCHEDULED FOR THREE WEEKS. scripts/audit_live_locked_drift.py
# was written 2026-08-19 and appeared in NEITHER crontab - not root's, not trader's (trader has no
# crontab at all), no cron.d entry, no systemd timer. A watchdog that exists and is not scheduled
# is the same as no watchdog. That is the SIL1 lesson and this board has now met it four times.
#
# ⛔⭐⭐ AND IT COULD NOT HAVE CAUGHT THE THING IT WAS FOR. Until 2026-09-09 a key MISSING from the
# env file was filed in its quietest bucket and printed "the mirrored value IS the live path".
# That sentence is true only when settings.py's default equals the mirror; for 13 of the 26
# mirrored keys it does not - including oms_v2_cw_target_pct (mirror 5.0, default 2.0) and
# strategy_schwab_1m_v2_enabled (mirror True, default False). So a rebuilt env that dropped a
# load-bearing line made the audit assert the OPPOSITE of the truth and exit 0. Fixed same day;
# scheduling it before that fix would have bought a watch that fails to a FALSE CLEAN.
#
# ⛔ ops/health/env_default_drift.py does NOT cover this. It compares LIVE settings against code
# defaults, so once the env line is gone live EQUALS the default and it correctly reports no drift.
# It goes quiet exactly when the line goes missing. Only mirror-vs-default sees a LOST setting.
#
# ⛔ NOT WINDOW-GUARDED TO MARKET HOURS. The hazard is an env rebuild, and those happen at DEPLOY
# time - after the close, on weekends. A market-hours guard would blind this to its own trigger.
#
# ⛔ CRON_TZ IS IGNORED ON THIS BOX, so any crontab hour range is a UTC range. Timestamps here are
# taken in ET explicitly. This runs from ROOT's crontab: the env file is root-readable only.
#
#   `--selftest`: bypass the cooldown and FORCE the alert path, to verify the push lands.
set -u

SELFTEST=0
[ "${1:-}" = "--selftest" ] && SELFTEST=1

REPO=/home/trader/project-mai-tai
OUT=/home/trader/live_locked_drift
LOG="$OUT/watch.log"
STATE="$OUT/state"                 # holds: <STATUS> <LAST_ALERT_EPOCH>
STATUS_TXT="$OUT/STATUS.txt"       # ⛔ read THIS; silence is not green
NTFY_URL="https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
COOLDOWN_SECS=21600                # 6h: config changes are rare, so re-pages should be too
mkdir -p "$OUT"

STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
NOW=$(date +%s)

if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 5000000 ]; then
  mv -f "$LOG" "$LOG.1"
fi

cd "$REPO" || { echo "$STAMP  ERROR: no $REPO" >> "$LOG"; exit 1; }

# ⛔⭐⭐ THE ENV IS *NOT* SOURCED, AND THAT IS DELIBERATE - IT IS THE SUBJECT, NOT THE CONTEXT.
# Every other wrapper here sources the service env because it needs a DSN. This one is auditing
# that file, and the audit reads it by PATH via --env-file. Sourcing it would put MAI_TAI_* into
# this process, and if the audit ever read defaults from a populated Settings() instead of the
# model class, it would be comparing the env against itself - a check that cannot come out false.
# tests/unit/test_audit_live_locked_drift.py pins that it reads the class. Do not add `set -a`.

REPORT=$(nice -n 19 "$REPO"/.venv/bin/python "$REPO"/scripts/audit_live_locked_drift.py 2>&1)
RC=$?

case "$RC" in
  0) LEVEL="OK" ;;
  1) LEVEL="RED" ;;
  # ⛔ UNKNOWN IS NOT PASS. A refusal (unreadable env, unimportable mirror) means we did not
  # measure - it must page, not pass silently. That is the whole point of exit 2 being distinct.
  2) LEVEL="CANNOT_SEE" ;;
  *) LEVEL="CANNOT_SEE" ;;
esac

{
  echo "as-of : $STAMP"
  echo "verdict: $LEVEL (exit $RC)"
  echo "⛔ This file is the reading. An absent page is NOT a green."
  echo
  echo "$REPORT"
} > "$STATUS_TXT"

echo "$STAMP  $LEVEL (exit $RC)" >> "$LOG"

send_ntfy() {  # $1=title $2=priority $3=tags $4=body
  # ⛔ Titles must be ASCII - an em-dash silently LOSES the push (learned on the OCO watch).
  curl -s -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" "$NTFY_URL" \
    >/dev/null 2>>"$OUT/alert.log"
}

PREV_STATUS="OK"; LAST_ALERT=0
[ -f "$STATE" ] && read -r PREV_STATUS LAST_ALERT < "$STATE" 2>/dev/null || true
case "${LAST_ALERT:-}" in ''|*[!0-9]*) LAST_ALERT=0;; esac

if [ "$LEVEL" != "OK" ] || [ "$SELFTEST" -eq 1 ]; then
  if [ "$PREV_STATUS" = "OK" ] || [ $(( NOW - LAST_ALERT )) -ge "$COOLDOWN_SECS" ] || [ "$SELFTEST" -eq 1 ]; then
    BODY="$REPORT

WHAT THIS MEANS. Production is not running the configuration the replay mirror describes.
If a key is listed as UNSET AND DIVERGENT there is no env override for it, so the box is running
settings.py's default - NOT the value shown as LIVE_LOCKED. That is what a rebuilt
/etc/project-mai-tai/project-mai-tai.env that dropped a line looks like.

⛔ DO NOT 'fix' this by editing LIVE_LOCKED to match. That silences the alarm and keeps the box on
the wrong numbers. Restore the env line, then restart the affected service - and remember the oms
target restarts oms AND strategy together.

⛔ Three of the env-set disagreements may be DELIBERATE: test_env_set_values_beat_live_locked
asserts LIVE_LOCKED is False for them on purpose. Read that test before changing anything.

Full reading: $STATUS_TXT"
    [ "$SELFTEST" -eq 1 ] && BODY="[SELFTEST] $BODY"
    if [ "$LEVEL" = "CANNOT_SEE" ]; then
      send_ntfy "AMBER live-locked audit CANNOT SEE" "high" "warning" "$BODY"
    else
      send_ntfy "RED live config drift" "urgent" "rotating_light" "$BODY"
    fi
    echo "$STAMP  ALERT[$LEVEL] sent" >> "$OUT/alert.log"
    LAST_ALERT=$NOW
  fi
  [ "$SELFTEST" -eq 0 ] && echo "$LEVEL $LAST_ALERT" > "$STATE"
else
  if [ "$PREV_STATUS" != "OK" ]; then
    send_ntfy "OK live config matches the mirror" "default" "white_check_mark" \
      "Every env-set flag matches LIVE_LOCKED, and every unset key's settings.py default agrees
with it. Recovered at $STAMP."
    echo "$STAMP  ALERT[GREEN] recovery sent" >> "$OUT/alert.log"
  fi
  echo "OK $LAST_ALERT" > "$STATE"
fi
exit 0
