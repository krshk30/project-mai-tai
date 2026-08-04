#!/bin/bash
# BAR-GAP WATCH — push an alert the moment the v2 bar series develops a hole.
#
#   Operator ask (2026-07-30): "if any DB holes or something, you have to get the notification.
#   You have to automatically start looking into it... you can start fixing it and let me know."
#   Window 07:00-16:00 ET, operator-stated.
#
# ⭐ IT REPAIRS, NOT JUST ALERTS. A cloud agent cannot do this job — it runs in Anthropic's cloud
# with no route to this box — so the repair belongs here, where the credentials already are.
#
# ⭐ WHY. A hole in `strategy_bar_history` makes true range span the gap — `href`/`lref` reference
# `prev.close`, so ONE bar carries the whole outage into a 5-period Wilder. On 2026-07-30 NUWE's
# ATR read 0.149 against a true 1-minute ATR of ~0.06, and `loss = 3.5 * ATR` put the resting
# buy-stop at 4.74 while the operator's TOS chart showed ~4.40. The operator found it on a chart;
# nothing in the fleet was watching. #620 stops the ATR spanning a gap; this makes the gap VISIBLE.
#
# ⛔ Holes are NOT restart-only. 2026-07-30 with the bot healthy: MF lost 13/11/31 minutes between
# 11:36 and 12:35, CRWU 25 min at 09:30, SNDG 3 min at 09:39. The feed drops bars on its own.
#
# ⛔ Guards are enforced HERE in ET and the crontab is deliberately `*/5 * * * *`: CRON_TZ is
# IGNORED on this box, so a crontab hour range is a UTC range, and a UTC range cannot express an ET
# window. Guarding in ET is also automatically DST-correct.
#
# ⛔ Runs as ROOT from ROOT's crontab — /etc/project-mai-tai/project-mai-tai.env is root-readable
# only. (The trade recorder sat in TRADER's crontab and could never write a byte; found 2026-07-30.)
#
#   `--selftest`: bypass window/holiday/cooldown and FORCE the alert path, to verify the push lands.
set -u

SELFTEST=0
[ "${1:-}" = "--selftest" ] && SELFTEST=1

REPO=/home/trader/project-mai-tai
OUT=/home/trader/bar_gap_watch
LOG="$OUT/watch.log"
STATE="$OUT/state"                 # holds: <STATUS> <LAST_ALERT_EPOCH>
NTFY_URL="https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
COOLDOWN_SECS=900                  # re-alert at most every 15 min while holed
mkdir -p "$OUT"

STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
TODAY=$(TZ=America/New_York date +%F)
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))
ETDOW=$(TZ=America/New_York date '+%u')

if [ "$SELFTEST" -eq 0 ]; then
  [ "$ETDOW" -gt 5 ] && exit 0
  # 07:00 (420) .. 16:00 (960) ET — the operator's stated window. Bars only flow in it, so
  # outside it this check has nothing to measure and would only manufacture noise.
  { [ "$ETMIN" -lt 420 ] || [ "$ETMIN" -ge 960 ]; } && exit 0
  HOLIDAYS_2026="2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25"
  case "$HOLIDAYS_2026" in *"$TODAY"*) exit 0 ;; esac
fi

if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 5000000 ]; then
  mv -f "$LOG" "$LOG.1"
fi

cd "$REPO" || { echo "$STAMP  ERROR: no $REPO" >> "$LOG"; exit 1; }
set -a
# shellcheck disable=SC1091
. /etc/project-mai-tai/project-mai-tai.env
set +a

# ⛔ Only the LAST 30 MINUTES. A hole from three hours ago has already aged out of a 5-period
# Wilder — alerting on it forever would train the operator to ignore this channel. What matters is
# a hole that is still inside the ATR window.
VERDICT=$(nice -n 19 "$REPO"/.venv/bin/python "$REPO"/ops/health/fleet_health_check.py 2>&1 \
          | grep 'v2-bar-continuity' | head -1)
LEVEL=$(printf '%s' "$VERDICT" | awk '{print $2}')
[ -z "$LEVEL" ] && LEVEL="AMBER" && VERDICT="bar-continuity check produced no verdict"

PREV_STATUS="OK"; LAST_ALERT=0
[ -f "$STATE" ] && read -r PREV_STATUS LAST_ALERT < "$STATE" 2>/dev/null || true
NOW=$(date +%s)

send_ntfy() {  # $1=title $2=priority $3=tags $4=body
  # ⛔ Titles must be ASCII — an em-dash silently LOSES the push (learned on the OCO watch).
  curl -s -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" "$NTFY_URL" \
    >/dev/null 2>>"$OUT/alert.log"
}

echo "$STAMP  $VERDICT" >> "$LOG"

if [ "$LEVEL" = "RED" ] || [ "$LEVEL" = "AMBER" ] || [ "$SELFTEST" -eq 1 ]; then
  # ---- AUTO-REPAIR -------------------------------------------------------------------------
  # ⛔ Safe to run unattended for three structural reasons, not because it "seems fine":
  #   1. INSERT-ONLY — ON CONFLICT DO NOTHING on the unique key, so a bar recorded LIVE can never
  #      be overwritten by a REST snapshot, even if the gap arithmetic were wrong.
  #   2. PROVENANCE-STAMPED — every filled row is source='rest'; studies filter WHERE source='live'.
  #   3. CLEAN UNDO — DELETE FROM strategy_bar_history WHERE source='rest'.
  # ⛔ It repairs the DATABASE only. Live trading is fixed by a RESTART (the in-memory series), and
  # a restart is attended — this must never bounce a trading service on its own.
  REPAIR="(repair skipped)"
  HALT_DOWNGRADE=0
  if [ "$SELFTEST" -eq 0 ]; then
    # ⛔ Capture the FULL output. `tail -3` threw away the ONE line that says whether the gap is
    # our data loss or the market's: report_bar_gaps prints, PER SYMBOL, either
    #   "<SYM>: REST fetch FAILED (...)"             -> we could not ask; conclude NOTHING
    #   "<SYM>: filled H/N missing bar(s) from REST" -> REST answered
    REPAIR_FULL=$(nice -n 19 "$REPO"/.venv/bin/python "$REPO"/scripts/report_bar_gaps.py                     --day "$TODAY" --go 2>&1)
    REPAIR=$(printf '%s' "$REPAIR_FULL" | tail -4)
    echo "$STAMP  REPAIR: $REPAIR" >> "$LOG"

    # ---- HALT DOWNGRADE --------------------------------------------------------------------
    # ⛔⭐ "INSERTED 0" ALONE IS NOT A HALT SIGNATURE. It is equally true when the REST call
    # ERRORED — so a genuine DUAL-SOURCE OUTAGE (streamer dead AND REST erroring) would look
    # exactly like a quiet market and be silently downgraded. That is the worst outcome for this
    # pager, and it is not hypothetical: Schwab REST 401'd for 2h41m on 2026-08-03 (08:00-10:40
    # UTC); only the 07:00 ET window guard kept the two from overlapping.
    #
    # Downgrade ONLY on the full signature: at least one symbol was ASKED and ANSWERED, and NO
    # symbol failed to answer. Any REST failure => stay RED, because we cannot conclude.
    # A real subscription drop on a non-halted name still pages: REST WOULD have those bars, so
    # they get filled (hit>0) and this branch does not fire.
    REST_FAILED=$(printf '%s' "$REPAIR_FULL" | grep -c 'REST fetch FAILED' || true)
    REST_ANSWERED_EMPTY=$(printf '%s' "$REPAIR_FULL" | grep -cE 'filled 0/[0-9]+ missing bar\(s\) from REST' || true)
    REST_ANSWERED_ANY=$(printf '%s' "$REPAIR_FULL" | grep -cE 'filled [0-9]+/[0-9]+ missing bar\(s\) from REST' || true)
    if [ "${REST_FAILED:-0}" -eq 0 ]        && [ "${REST_ANSWERED_ANY:-0}" -gt 0 ]        && [ "${REST_ANSWERED_EMPTY:-0}" -eq "${REST_ANSWERED_ANY:-0}" ]; then
      HALT_DOWNGRADE=1
      echo "$STAMP  HALT-DOWNGRADE: REST answered for all $REST_ANSWERED_ANY holed symbol(s) and had NONE of the bars => the market produced no prints (halt), not our data loss" >> "$LOG"
    fi
  fi
  if [ "$PREV_STATUS" = "OK" ] || [ $(( NOW - LAST_ALERT )) -ge "$COOLDOWN_SECS" ] || [ "$SELFTEST" -eq 1 ]; then
    BODY="$VERDICT

AUTO-REPAIR (database only):
$REPAIR

Bars are missing from the DB series (backtest/parity/recorder read it), so the fill above matters.
LIVE ATR is already protected: #620 refuses to compute true range across a gap and logs
[V2-ATR-BAR-GAP] per symbol — grep the v2 log to confirm it fired for these names.
DO NOT restart schwab-1m-v2 on account of this alert: a restart punches a fresh hole of its own,
which is the very condition this watch exists to catch. Restart only if [V2-ATR-BAR-GAP] is ABSENT
for a gapped symbol that v2 is actually holding or resting an order on.
Undo the fill: DELETE FROM strategy_bar_history WHERE source='rest';"
    [ "$SELFTEST" -eq 1 ] && BODY="[SELFTEST] $BODY"
    if [ "${HALT_DOWNGRADE:-0}" -eq 1 ]; then
      BODY="MARKET QUIET / HALT - not our data loss.
REST was asked for every holed symbol, ANSWERED, and had none of the bars, so the market produced
no prints in those minutes. Nothing to repair and nothing to restart.
(A REST FAILURE does NOT reach this branch - it stays RED, because a dead streamer plus a dead
REST is indistinguishable from a quiet market and must never be downgraded.)

$BODY"
      send_ntfy "INFO v2 bar gap - market halt" "low" "information_source" "$BODY"
      echo "$STAMP  ALERT[INFO-halt-downgrade] sent (was $LEVEL)" >> "$OUT/alert.log"
    elif [ "$LEVEL" = "RED" ]; then
      send_ntfy "RED v2 BAR HOLE" "urgent" "rotating_light" "$BODY"
      echo "$STAMP  ALERT[$LEVEL] sent" >> "$OUT/alert.log"
    else
      send_ntfy "AMBER v2 bar gap" "default" "warning" "$BODY"
      echo "$STAMP  ALERT[$LEVEL] sent" >> "$OUT/alert.log"
    fi
    LAST_ALERT=$NOW
  fi
  [ "$SELFTEST" -eq 0 ] && echo "$LEVEL $LAST_ALERT" > "$STATE"
else
  if [ "$PREV_STATUS" != "OK" ]; then
    send_ntfy "OK v2 bar series contiguous again" "default" "white_check_mark" "$VERDICT"
    echo "$STAMP  ALERT[GREEN] recovery sent" >> "$OUT/alert.log"
  fi
  echo "OK $LAST_ALERT" > "$STATE"
fi
exit 0
