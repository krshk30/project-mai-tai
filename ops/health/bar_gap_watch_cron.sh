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

PREV_STATUS="OK"; LAST_ALERT=0; REPAIR_AT=0
# ⛔ I2 — the 3rd field is the epoch of the last REPAIR. An older 2-field state file leaves it
# empty, which defaults to 0 and disables the gate: degrades to the old behaviour, never to a
# silently wrong one.
[ -f "$STATE" ] && read -r PREV_STATUS LAST_ALERT REPAIR_AT < "$STATE" 2>/dev/null || true
case "${REPAIR_AT:-}" in ''|*[!0-9]*) REPAIR_AT=0;; esac
NOW=$(date +%s)
WATCH_WINDOW_SECS=1800   # MUST match the 30-min window fleet_health_check.py inspects
GAPPED=""; GAPPED_SQL=""

# ⛔ I2 predicate, factored out so it can be pinned by a test. TRUE (0) => the check window still
# overlaps the range we repaired, so a contiguous read proves only that our own INSERT landed.
green_held() {  # green_held <now_epoch> <repair_epoch> <window_secs>
  [ "${2:-0}" -gt 0 ] || return 1          # never repaired => nothing to hold for
  [ $(( $1 - $3 )) -lt "$2" ]
}
# ⛔ I3 helper, likewise pinned: pull the holed symbols out of report_bar_gaps.py's output.
parse_gapped() { grep -oE '\[backfill\] [A-Z]{1,6}:' | sed -E 's/.*\] ([A-Z]+):/\1/' | sort -u | tr '\n' ' '; }

# ⛔⭐⭐ I3 — THE UNANSWERABLE LINE IS DELETED, NOT REPLACED BY A MECHANISED ONE. HERE IS WHY.
# The old body asked the operator to "confirm [V2-ATR-BAR-GAP] fired for these names". That is
# unanswerable: it is unscoped in time (ANY firing that day satisfies it — on 2026-08-14 the only
# firing was on warmup-replay bars 29 DAYS stale), and ABSENT is the EXPECTED state whenever the
# symbol simply left the watchlist, because then there is a DB hole and NO in-memory gap to span.
#
# The obvious fix is to have this script answer the real question — "were we HOLDING or RESTING on
# the gapped symbol?" — instead of asking it. That was written, and then MEASURED, and it does not
# work with the current schema:
#
#   * `broker_orders.status` has only ever held THREE values across the whole table, every account,
#     all time: rejected (46819) / cancelled (14270) / filled (14004). A "non-terminal status"
#     filter is therefore ALWAYS ZERO — a guard that guards nothing.
#   * `account_positions.quantity <> 0` is empty on EVERY account (133 v2 rows, all zero), and
#     `virtual_positions` for v2 is 67 rows all zero — consistent with its known false-zero defect.
#
# So a naive exposure check would print NOT EXPOSED 100% of the time: a false-clean generator, which
# is the very failure this whole change is about. Shipping it would have been worse than the line it
# replaced, because it would have LOOKED like an answer.
#
# ⇒ The correct form is an INTERVAL OVERLAP — an order whose [submitted_at, updated_at] span crosses
#   the gap window, regardless of its final status (v2 orders live ~50s median, 334s p90, so the
#   overlap is real even though the row only ever records its terminal state), plus fills bracketing
#   the window for the holding limb. That needs the gap's minute range, which is recoverable from
#   the rows just stamped source='rest'. It is NOT built here: it needs its own validation that the
#   EXPOSED branch can actually fire, on a past day where it demonstrably should.

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
    # ⛔ I2 — stamp WHEN we wrote, so the recovery GREEN can refuse to verify our own INSERT.
    if printf '%s' "$REPAIR_FULL" | grep -qE 'INSERTED [1-9][0-9]* bar'; then REPAIR_AT=$NOW; fi
    # I3 — which symbols were holed, for the exposure lookup and for a SCOPED undo hint.
    GAPPED=$(printf '%s' "$REPAIR_FULL" | parse_gapped)
    echo "$STAMP  GAPPED: ${GAPPED:-<none parsed>}" >> "$LOG"
    [ -n "$GAPPED" ] && GAPPED_SQL=$(printf "'%s'," $GAPPED | sed 's/,$//')

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

A HOLE IS NOT A DEFECT BY ITSELF. A symbol that LEAVES THE WATCHLIST stops receiving bars BY
DESIGN, and on 2026-08-14 that was the whole of a RED (LBGJ, off-list 07:16-07:38 ET). This alert
CANNOT yet tell that apart from a real feed loss - it has no watched-minutes denominator.
DO NOT restart schwab-1m-v2 on account of this alert: a restart punches a fresh hole of its own,
which is the very condition this watch exists to catch.
RESTART ONLY IF v2 was HOLDING or RESTING on a gapped symbol across the gap. Check it directly -
the order row only ever records its FINAL status, so ask for an interval overlap, not a live status:
  select bo.symbol, bo.order_type, bo.status, bo.submitted_at, bo.updated_at
    from broker_orders bo join broker_accounts b on b.id=bo.broker_account_id
   where b.name='live:schwab_1m_v2' and bo.symbol in (${GAPPED_SQL:-'<none parsed>'})
     and bo.submitted_at <= <gap_end> and bo.updated_at >= <gap_start>;
Live ATR is separately protected by #620, which refuses to span a gap. Its [V2-ATR-BAR-GAP] marker
is deliberately NOT quoted here as confirmation: it is unscoped in time, and its ABSENCE is the
correct, expected state when the symbol was merely off the watchlist.
Undo THIS fill (scoped — never the bare source='rest' delete, which drops every bar ever repaired):
  DELETE FROM strategy_bar_history WHERE source='rest'
    AND symbol IN (${GAPPED_SQL:-'<none parsed>'}) AND bar_time >= now() - interval '3 hours';"
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
  # ⛔⭐⭐ I2 — A VERIFICATION MUST NOT BE SATISFIABLE BY OUR OWN ACTION.
  # The check inspects the last 30 minutes. Immediately after a repair that window IS the range we
  # just backfilled, so it reads contiguous BECAUSE WE MADE IT CONTIGUOUS — the GREEN would print
  # whether or not the cause persisted. Observed live 2026-08-14: RED 07:40 → auto-repair → GREEN
  # 07:45, and the symbol churned off the watchlist again at 07:47. Hold the all-clear until the
  # window has advanced entirely past the repaired range.
  if [ "$PREV_STATUS" != "OK" ]; then
    if green_held "$NOW" "$REPAIR_AT" "$WATCH_WINDOW_SECS"; then
      echo "$STAMP  GREEN HELD: the ${WATCH_WINDOW_SECS}s window still overlaps the range repaired at ${REPAIR_AT} — verifying our own INSERT proves nothing. $(( REPAIR_AT + WATCH_WINDOW_SECS - NOW ))s to go." >> "$LOG"
      # stay non-OK so the all-clear can still fire once the window clears
      echo "$PREV_STATUS $LAST_ALERT $REPAIR_AT" > "$STATE"
      exit 0
    fi
    send_ntfy "OK v2 bar series contiguous again" "default" "white_check_mark" \
      "$VERDICT
Verified on a window that does NOT overlap the repaired range (last repair $(( (NOW - REPAIR_AT) / 60 )) min ago; window ${WATCH_WINDOW_SECS}s)."
    echo "$STAMP  ALERT[GREEN] recovery sent" >> "$OUT/alert.log"
  fi
  echo "OK $LAST_ALERT $REPAIR_AT" > "$STATE"
fi
exit 0
