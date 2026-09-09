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

# ⛔ Defaults ARE production. The overrides exist so tests can drive this wrapper against a
# stubbed HTTP layer and a real state file; a fixture that cannot reach the real code path proves
# nothing about it. Nothing here changes what cron runs.
REPO="${DRIFT1_REPO:-/home/trader/project-mai-tai}"
OUT="${DRIFT1_OUT:-/home/trader/live_locked_drift}"
PYBIN="${DRIFT1_PYTHON:-$REPO/.venv/bin/python}"
ENV_FILE_ARG="${DRIFT1_ENV_FILE:-}"
LOG="$OUT/watch.log"
STATE="$OUT/state"                 # holds: <STATUS> <LAST_ALERT_EPOCH>
STATUS_TXT="$OUT/STATUS.txt"       # ⛔ read THIS; silence is not green
NTFY_URL="${DRIFT1_NTFY_URL:-https://ntfy.sh/mai-tai-preopen-28806a5a97b7}"
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

if [ -n "$ENV_FILE_ARG" ]; then
  REPORT=$(nice -n 19 "$PYBIN" "$REPO"/scripts/audit_live_locked_drift.py --env-file "$ENV_FILE_ARG" 2>&1)
else
  REPORT=$(nice -n 19 "$PYBIN" "$REPO"/scripts/audit_live_locked_drift.py 2>&1)
fi
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

# ⛔⭐⭐ DELIVERY IS VERIFIED, NEVER ASSUMED (codex-2 P1 on #924, 2026-09-09).
#
# The first version called `curl -s` and then logged "sent", advanced LAST_ALERT and persisted the
# RED state REGARDLESS of the result. Measured: `curl -s` exits 0 on an HTTP 500 - so an ntfy
# outage would have been recorded as a delivered page and then suppressed for the full six-hour
# cooldown. The watchdog built to stop a false clean would itself have failed to one, at the
# delivery layer. Returns 0 ONLY on confirmed acceptance.
#
#   --fail-with-body : non-2xx becomes exit 22 (plain `-s` returns 0). This is the whole fix.
#   --connect-timeout/--max-time : a hang is a delivery FAILURE, not an indefinite block in cron.
#   -sS : silent, but still write the error to alert.log so a failure is diagnosable.
# ⛔ No `|| true` on this call, and no `set -e` reliance: the exit code IS the verdict.
send_ntfy() {  # $1=title $2=priority $3=tags $4=body   -> 0 delivered, non-0 NOT delivered
  # ⛔ Titles must be ASCII - an em-dash silently LOSES the push (learned on the OCO watch).
  curl -sS --fail-with-body --connect-timeout 10 --max-time 30 \
    -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" "$NTFY_URL" \
    >/dev/null 2>>"$OUT/alert.log"
  CURL_RC=$?
  return $CURL_RC
}

PREV_STATUS="OK"; LAST_ALERT=0
[ -f "$STATE" ] && read -r PREV_STATUS LAST_ALERT < "$STATE" 2>/dev/null || true
case "${LAST_ALERT:-}" in ''|*[!0-9]*) LAST_ALERT=0;; esac

if [ "$LEVEL" != "OK" ] || [ "$SELFTEST" -eq 1 ]; then
  if [ "$PREV_STATUS" = "OK" ] || [ $(( NOW - LAST_ALERT )) -ge "$COOLDOWN_SECS" ] || [ "$SELFTEST" -eq 1 ]; then
    # ⛔⭐⭐ THE PAGE MUST NOT CONTRADICT THE REPORT IT QUOTES (operator, 2026-09-09).
    # The first version built ONE body for every case: the fixed paragraph "Production is not
    # running the configuration the replay mirror describes" was sent even for a --selftest of a
    # CLEAN audit, so the operator received a page asserting drift directly above this script's
    # own "No drift: every env-set flag matches the mirror" output. A page whose prose contradicts
    # its own evidence teaches the reader to distrust the channel, which is how a real RED gets
    # skimmed. The interpretation is now chosen from what was actually measured.
    if [ "$LEVEL" = "OK" ]; then
      # Only reachable via --selftest: the audit is clean and this is a DELIVERY-PATH test.
      TITLE="SELFTEST live-config watch delivery"
      PRIORITY="low"
      TAGS="white_check_mark"
      BODY="[SELFTEST] DELIVERY-PATH TEST - THIS IS NOT A DRIFT ALERT.

The audit below is CLEAN: production matches the mirror. This message exists only to prove the
alarm can reach you, because a watchdog whose page has never been delivered is unproven.
⛔ NO ACTION IS REQUIRED. A real drift alert is titled 'RED live config drift'.

$REPORT

Full reading: $STATUS_TXT"
    else
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
      # ⛔ A SELFTEST OF A GENUINELY RED BOX KEEPS THE RED WORDING. The box really is drifted; only
      # the delivery is being rehearsed, so suppressing the interpretation would be the same
      # contradiction in the other direction.
      [ "$SELFTEST" -eq 1 ] && BODY="[SELFTEST] $BODY"
      if [ "$LEVEL" = "CANNOT_SEE" ]; then
        TITLE="AMBER live-locked audit CANNOT SEE"
        PRIORITY="high"
        TAGS="warning"
      else
        TITLE="RED live config drift"
        PRIORITY="urgent"
        TAGS="rotating_light"
      fi
    fi
    send_ntfy "$TITLE" "$PRIORITY" "$TAGS" "$BODY"
    DELIVERY_RC=$?
    if [ "$DELIVERY_RC" -eq 0 ]; then
      echo "$STAMP  ALERT[$LEVEL] DELIVERED" >> "$OUT/alert.log"
      DELIVERY="delivered"
      # ⛔ The cooldown starts at CONFIRMED delivery, never at the attempt.
      LAST_ALERT=$NOW
    else
      echo "$STAMP  ALERT[$LEVEL] DELIVERY FAILED (curl exit $DELIVERY_RC) - NOT entering cooldown, will retry next run" >> "$OUT/alert.log"
      DELIVERY="FAILED (curl exit $DELIVERY_RC)"
      # ⛔ LAST_ALERT is deliberately NOT advanced. Leaving it stale is what makes the next cron
      # run retry immediately instead of sitting silent for six hours on an undelivered page.
    fi
    printf 'delivery: %s\n' "$DELIVERY" >> "$STATUS_TXT"
  fi
  [ "$SELFTEST" -eq 0 ] && echo "$LEVEL $LAST_ALERT" > "$STATE"
else
  if [ "$PREV_STATUS" != "OK" ]; then
    send_ntfy "OK live config matches the mirror" "default" "white_check_mark" \
      "Every env-set flag matches LIVE_LOCKED, and every unset key's settings.py default agrees
with it. Recovered at $STAMP."
    DELIVERY_RC=$?
    if [ "$DELIVERY_RC" -ne 0 ]; then
      # ⛔ An undelivered all-clear must not be recorded as sent either. Hold the previous status
      # so the next run retries the recovery; clearing to OK here would lose it silently.
      echo "$STAMP  RECOVERY DELIVERY FAILED (curl exit $DELIVERY_RC) - holding $PREV_STATUS, will retry next run" >> "$OUT/alert.log"
      printf 'delivery: recovery FAILED (curl exit %s)\n' "$DELIVERY_RC" >> "$STATUS_TXT"
      echo "$PREV_STATUS $LAST_ALERT" > "$STATE"
      exit 0
    fi
    echo "$STAMP  ALERT[GREEN] recovery DELIVERED" >> "$OUT/alert.log"
    printf 'delivery: recovery delivered\n' >> "$STATUS_TXT"
  fi
  echo "OK $LAST_ALERT" > "$STATE"
fi
exit 0
