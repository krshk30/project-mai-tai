#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────────────────────────
# VALIDATE THE 2026-08-13 DEPLOY (#687 #688 #689 #690 #691 #692 #693) — HEAD 3ac4721
#
# RUN IT:   ssh mai-tai-vps 'bash -s' < ops/health/validate_0813_deploy.sh
#           (⛔ a quoted remote command HANGS on this box — always pipe on stdin)
#           optional:  ... < script.sh -- 2026-08-14      # validate a specific ET day
#
# ⛔⭐⭐ EVERY CHECK REPORTS THREE SEPARATE THINGS, NEVER COLLAPSED INTO ONE:
#       DEPLOYED   — is the code actually in the running tree?
#       EXERCISED  — did the path RUN, and out of how many opportunities? (n / N)
#       VERDICT    — PASS | FAIL | UNEXERCISED
#   "UNEXERCISED" is a RESULT, not a pass. A bare zero with no denominator is not evidence:
#   an absence only means something against a known population.
#
# ⛔ WHAT THIS SCRIPT CANNOT SEE — state it, do not pretend otherwise:
#   1. The BROKER's book. Webull OCO children are broker-created and never land in `broker_orders`,
#      so "a pair is resting at Webull right now" is UNKNOWABLE here. Only fills/rejects are visible.
#   2. The WIRE. It cannot see the client_order_id actually sent, only what we logged.
#   3. Whether a cancel LANDED. `[OMS-EXIT-RELEASE]` means submitted; confirmation is backgrounded.
#   4. Manual operator activity at either broker (the broker's book is SHARED).
#   5. Anything before 20:00 ET — logs rotate at 00:00 UTC. Run this BEFORE 20:00 ET or the day
#      is gone. The script refuses to give a clean verdict past the rotation.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
set -uo pipefail

DAY_ET="${1:-}"
[ "${DAY_ET}" = "--" ] && DAY_ET="${2:-}"
[ -z "${DAY_ET}" ] && DAY_ET="$(TZ=America/New_York date +%F)"

ENV=/etc/project-mai-tai/project-mai-tai.env
REPO=/home/trader/project-mai-tai
V2LOG=/var/log/project-mai-tai/schwab-1m-v2.log
OMSLOG=/var/log/project-mai-tai/oms.log
# ⛔ journalctl is a FALSE ZERO for v2 — its sink is the file above.

URL=$(sudo grep -E '^MAI_TAI_DATABASE_URL=' "$ENV" | head -1 | cut -d= -f2-)
export PGPASSWORD=$(echo "$URL" | sed -E 's|^[^:]+://[^:]+:([^@]+)@.*|\1|')
PGUSER=$(echo "$URL" | sed -E 's|^[^:]+://([^:]+):.*|\1|')
DSN="dbname=project_mai_tai user=${PGUSER} host=localhost"
D0="'${DAY_ET} 00:00:00'::timestamp at time zone 'America/New_York'"
D1="'${DAY_ET} 23:59:59'::timestamp at time zone 'America/New_York'"

q() { psql "$DSN" -tAF' | ' -v ON_ERROR_STOP=0 -c "$1" 2>&1; }
q1() { psql "$DSN" -tAc "$1" 2>/dev/null | tr -d ' '; }
hdr() { echo; echo "═══ $* "; }
verdict() { printf '   ➤ VERDICT: %s — %s\n' "$1" "$2"; }

# ⛔⭐⭐ LOG LINES ARE STAMPED IN **UTC**, AND THE FILE ROTATES AT 00:00 UTC (= 20:00 ET).
# A bare `grep -c` over the logfile counts WHATEVER THE FILE HAPPENS TO HOLD — which is the tail of
# the PREVIOUS ET day plus part of this one. That is a false-clean generator: pass `--day` for a past
# date and every log count silently answers about TODAY's file instead. So every log count below is
# windowed to the UTC range of the requested ET day, and the file's true coverage is printed.
UTC_FROM=$(date -u -d "TZ=\"America/New_York\" ${DAY_ET} 00:00:00" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
UTC_TO=$(date -u -d "TZ=\"America/New_York\" ${DAY_ET} 23:59:59" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
# ⛔⭐⭐ WINDOW SANITY — the `TZ="..."` prefix is a GNU-on-Linux extension. Where it is unsupported
# (Git Bash, busybox) date does NOT error: it returns the string UNSHIFTED, and the window silently
# becomes a UTC day. Every count below would then be windowed to the wrong 24h and read as a clean
# result. ET is UTC-4/-5, so a correct conversion can NEVER leave the time at 00:00:00.
if [ -z "$UTC_FROM" ] || [ -z "$UTC_TO" ] || [ "$UTC_FROM" = "${DAY_ET} 00:00:00" ]; then
  echo "⛔⛔ ABORT: date(1) here does not support the TZ=\"America/New_York\" prefix, so the ET->UTC"
  echo "     window was NOT converted (UTC_FROM='${UTC_FROM}'). Every count would be windowed to the"
  echo "     wrong 24 hours and would read as a clean result. Run this on the VPS via:"
  echo "     ssh mai-tai-vps 'bash -s' < ops/health/validate_0813_deploy.sh"
  exit 2
fi
logreadable() { sudo test -r "$1"; }   # ⛔ the logs are ROOT-ONLY; a bare [ -r ] as trader says NO
logcount() {  # logcount <file> <fixed-string>  -> a count, or -1 meaning UNKNOWN
  # ⛔⭐⭐ RETURNS -1, NOT 0, WHEN THE FILE CANNOT ANSWER FOR THIS WINDOW. An empty or freshly
  # rotated logfile otherwise yields 0 and reads as "nothing happened" — the difference between
  # "it did not fire" and "I could not see" is the whole point of this script.
  logreadable "$1" || { echo -1; return; }
  sudo awk -v a="$UTC_FROM" -v b="$UTC_TO" -v pat="$2" '
    { ts = substr($0,1,19); gsub(",",".",ts)
      if (ts ~ /^[0-9]{4}-/) { if (fst=="") fst=ts; lst=ts }
      if (ts >= a && ts <= b && index($0,pat)) n++ }
    END {
      if (fst == "" || lst < a || fst > b) { print -1 }   # file does not overlap the window at all
      else print n+0
    }' "$1"
}
logtail() {  # logtail <file> <fixed-string> <n>
  logreadable "$1" || return
  sudo awk -v a="$UTC_FROM" -v b="$UTC_TO" -v pat="$2" '
    { ts = substr($0,1,19); gsub(",",".",ts) }
    ts >= a && ts <= b && index($0,pat)' "$1" | tail -"$3"
}
# ⛔⭐⭐ AN UNKNOWN MUST NEVER BECOME A PASS. The first version of this script let an unreadable log
# fall through the numeric comparisons and printed "VERDICT: PASS" on counts it had never read —
# the exact false-clean it exists to prevent. Every verdict block now calls this FIRST.
unknown() { for v in "$@"; do case "$v" in ''|*[!0-9-]*|-1) return 0;; esac; done; return 1; }
show() { case "$1" in -1) echo "UNKNOWN(log cannot answer for this window)";; *) echo "$1";; esac; }
LOG_SILENT_WARN_SECS=${LOG_SILENT_WARN_SECS:-1800}   # 30 min of silence ⇒ suspect a dead writer
_ts_epoch() { date -d "$1 UTC" +%s 2>/dev/null; }
logcoverage() {
  local f="$1"
  logreadable "$f" || { echo "   ⛔⛔ CANNOT READ $f — counts from it are UNKNOWN, not zero."; return; }
  local first last
  # ⛔ Take the first/last line that actually CARRIES a timestamp. A traceback at the tail has none,
  # and the old `tail -1 | cut -c1-19` handed back junk — which then failed the `[ -n "$last" ]`
  # guard and SKIPPED the coverage check entirely. A skipped check prints exactly like a clean one.
  first=$(sudo head -200 "$f" | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' | head -1)
  last=$(sudo tail -200 "$f" | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' | tail -1)
  echo "   log $(basename "$f") covers ${first:-?} .. ${last:-?} UTC"
  if [ -z "$first" ] || [ -z "$last" ]; then
    echo "   ⛔⛔ NO TIMESTAMPED LINE in the head/tail of $f — coverage is UNKNOWN, not clean."
    return
  fi
  if [[ "$first" > "$UTC_FROM" ]]; then
    echo "   ⛔⛔ THE LOG STARTS AFTER THE WINDOW OPENS (${UTC_FROM} UTC). Rotation already ate part"
    echo "        of this day — every log count below is a LOWER BOUND, not a count."
  fi
  # ⛔⭐⭐ THE OTHER END IS **TWO DIFFERENT THINGS**. NEVER GIVE THEM THE SAME BANNER:
  #   (a) ROTATION LOSS — the ET day is OVER and the file still ends before the window closes. The
  #       evening (20:00-23:59 ET) is in the NEXT file and these counts are a genuine LOWER BOUND.
  #   (b) THE DAY HAS NOT HAPPENED YET — counts are partial by construction. Not a defect.
  # The first version compared `last` against min(now, window_end), so on EVERY intraday run the
  # few seconds of log latency tripped (a)'s ⛔⛔ banner. A warning that is always on is not a
  # warning: at 19:30, when rotation loss is real, it would have looked identical to 06:38 noise.
  local now_utc; now_utc=$(date -u '+%Y-%m-%d %H:%M:%S')
  if [[ "$now_utc" > "$UTC_TO" ]]; then
    if [[ "$last" < "$UTC_TO" ]]; then
      echo "   ⛔⛔ THE ET DAY IS OVER but the log ends at ${last} UTC, before the window closes"
      echo "        (${UTC_TO} UTC) — the evening (20:00-23:59 ET) is in the NEXT rotated file."
      echo "        Counts are a LOWER BOUND. Fix: also read the rotated file."
    fi
  else
    echo "   ℹ the ET day is still running (now ${now_utc} UTC, closes ${UTC_TO} UTC) — counts are"
    echo "     PARTIAL by construction, not by rotation. Only the last run before 20:00 ET is quotable."
    # ⛔ A SILENT log is a different failure from a partial day, and it is the one that fakes a
    # clean: a dead writer yields zeros that read exactly like "the path never fired".
    local le ne age; le=$(_ts_epoch "$last"); ne=$(date +%s)
    if [ -n "$le" ]; then
      age=$(( ne - le ))
      if [ "$age" -gt "$LOG_SILENT_WARN_SECS" ]; then
        echo "   ⛔ $(basename "$f") HAS BEEN SILENT FOR $(( age / 60 )) MIN — suspect a dead writer."
        echo "     Its zeros below are UNKNOWN, not counts. Check the service before reading them."
      fi
    fi
  fi
}

NOW_ET=$(TZ=America/New_York date '+%F %H:%M')
NOW_H=$(TZ=America/New_York date +%H)
echo "VALIDATING THE 2026-08-13 DEPLOY   day=${DAY_ET} (ET)   run at ${NOW_ET} ET"

# ── 0. THE SCRIPT MUST PROVE IT CAN SEE ANYTHING AT ALL ──────────────────────────────────────────
# ⛔ A watch that fails to a false clean is worse than no watch. Before trusting a single zero
# below, run the reject query against a KNOWN-BAD TAPE: 2026-08-13 had exactly 58 rejects on
# live:orb, 56 of them XHG. If this control does not reproduce, EVERY zero in this script is
# meaningless and the run is void.
hdr "0. SELF-TEST against the known-bad tape (2026-08-13 = 58 orb rejects)"
CTRL=$(q1 "select count(*) from broker_orders bo join broker_accounts ba on ba.id=bo.broker_account_id
 where ba.name='live:orb' and bo.status='rejected'
   and bo.submitted_at >= '2026-08-13 00:00:00'::timestamp at time zone 'America/New_York'
   and bo.submitted_at <  '2026-08-14 00:00:00'::timestamp at time zone 'America/New_York';")
echo "   control reject count for 2026-08-13 = ${CTRL:-ERROR} (expected 58)"
if [ "${CTRL:-0}" -ge 55 ] 2>/dev/null; then
  verdict "PASS" "the query reproduces a known-bad day, so a zero below is real"
else
  verdict "VOID" "SELF-TEST FAILED — the query cannot see a day we KNOW was bad. Every result below is untrustworthy. Fix the query before reading further."
fi

if [ "$NOW_H" -ge 20 ] 2>/dev/null && [ "${DAY_ET}" = "$(TZ=America/New_York date +%F)" ]; then
  echo
  echo "   ⛔⛔ IT IS PAST 20:00 ET. Logs rotate at 00:00 UTC, so today's log lines are GONE."
  echo "        DB checks below are still valid; every LOG-based count is a FALSE ZERO. Do not"
  echo "        read a log verdict from this run."
fi

# ── 1. IS THE DEPLOY EVEN ON THE BOX, AND ARE THE FLAGS LIVE? ────────────────────────────────────
hdr "1. DEPLOYED — code + flags (confirm by CONTENT, never by 'the deploy said success')"
cd "$REPO" || exit 1
echo "   HEAD: $(git log --oneline -1)"
echo "   tree: $(git status --porcelain | wc -l) local changes (⛔ non-zero BLOCKS the next deploy)"
for m in V2-FANOUT-CLAIM-EXPIRED V2-WEBULL-RESTING-PLACE WEBULL-PROTECT-ATTACHED OMS-EXIT-RELEASE OMS-EXIT-REPROTECT; do
  if grep -rqF "$m" src/ --include='*.py'; then echo "   ✓ marker present in source: [$m]"
  else echo "   ✗ MARKER MISSING FROM SOURCE: [$m]  ⛔ the deploy did not land"; fi
done
echo "   -- flags as the PROCESS sees them (not as the env file claims) --"
for p in $(pgrep -f 'mai-tai-oms|mai-tai-schwab-1m-v2'); do
  sudo tr '\0' '\n' < /proc/$p/environ 2>/dev/null \
    | grep -E 'WEBULL_RESTING_MIRROR_ENABLED|EXIT_RELEASE_RESERVATION_ENABLED' | sed "s/^/   pid=$p /"
done

# ── 2. THE DENOMINATORS — everything below is read against these ─────────────────────────────────
hdr "2. DENOMINATORS for ${DAY_ET} (an absence means nothing without these)"
q "select 'fills: '||ba.name||' = '||count(*) from fills f join broker_accounts ba on ba.id=f.broker_account_id
   where f.filled_at >= $D0 and f.filled_at <= $D1 and ba.name like 'live:%' group by ba.name;"
q "select 'orders by status: '||ba.name||' '||bo.status||' = '||count(*)
   from broker_orders bo join broker_accounts ba on ba.id=bo.broker_account_id
   where bo.submitted_at >= $D0 and bo.submitted_at <= $D1 and ba.name like 'live:%'
   group by ba.name, bo.status order by 1;"
echo "   ⛔ ACCOUNT VISIBILITY: this script sees live:schwab_1m_v2 + live:orb + paper:polygon_30s."
echo "   ⛔ Reject counts on broker_order_events conflate OUR aborts with BROKER refusals; the"
echo "      checks below read the verbatim reason string instead of trusting the count."

# ── 3. #688 — DO BOTH LEGS ACTUALLY REST AT THEIR OWN BROKER? ────────────────────────────────────
hdr "3. #688 RESTING MIRROR — the Webull leg should now WAIT at the broker, not watch a price"
logcoverage "$V2LOG"
SCH_REST=$(logcount "$V2LOG" '[V2-RESTING-PLACE]')
WB_REST=$(logcount "$V2LOG" '[V2-WEBULL-RESTING-PLACE]')
WB_CANC=$(logcount "$V2LOG" '[V2-WEBULL-RESTING-CANCEL]')
EH_ARM=$(logcount "$V2LOG" '[V2-RESTING-EH-ARM]')
echo "   Schwab broker rests placed (RTH)        : $(show $SCH_REST)"
echo "   EH soft-rests (mirror CANNOT fire here) : $(show $EH_ARM)   <- excluded from the denominator"
echo "   Webull mirrors placed                   : $(show $WB_REST)"
echo "   Webull mirrors cancelled                : $(show $WB_CANC)"
if unknown "$SCH_REST" "$WB_REST"; then
  verdict "VOID" "could not read the v2 log — this is an UNKNOWN, not a pass"
elif [ "$SCH_REST" -eq 0 ]; then
  verdict "UNEXERCISED" "no RTH broker rest happened at all — the mirror had no opportunity. NOT a pass."
elif [ "$WB_REST" -eq 0 ]; then
  verdict "FAIL" "${SCH_REST} Schwab rests and ZERO Webull mirrors — the flag is on but nothing mirrored"
else
  verdict "PASS" "${WB_REST} mirrors against ${SCH_REST} RTH rests"
fi
echo "   -- ⛔ ORPHAN CHECK: a mirrored rest nobody cancelled is a live order nobody owns (FRTT, 136 min) --"
q "select 'orb resting entries left non-terminal: '||count(*)
   from broker_orders bo join broker_accounts ba on ba.id=bo.broker_account_id
   where ba.name='live:orb' and lower(bo.side)='buy'
     and lower(bo.status) not in ('filled','cancelled','canceled','rejected','expired','replaced')
     and bo.submitted_at >= $D0;"
echo "   ⛔ CANNOT SEE: whether an order is still working AT WEBULL. This is our DB's view only."

# ── 4. #689 + #690 — DOES A BARE FILL GET REAL PROTECTION? ───────────────────────────────────────
hdr "4. #689/#690 ATTACH — a BARE Webull fill must get a real stop+target within seconds"
logcoverage "$OMSLOG"
ATT=$(logcount "$OMSLOG" '[WEBULL-PROTECT-ATTACHED]')
AFAIL=$(logcount "$OMSLOG" '[WEBULL-PROTECT-FAILED]')
ARETRY=$(logcount "$OMSLOG" '[WEBULL-PROTECT-RETRY]')
BAREFILL=$(q1 "select count(*) from fills f join broker_accounts ba on ba.id=f.broker_account_id
   where ba.name='live:orb' and lower(f.side)='buy' and f.filled_at >= $D0 and f.filled_at <= $D1;")
echo "   Webull BUY fills (the opportunity)  : ${BAREFILL:-?}"
echo "   [WEBULL-PROTECT-ATTACHED]           : $(show $ATT)"
echo "   [WEBULL-PROTECT-RETRY]              : $(show $ARETRY)   (a retry is fine; a FAILED is not)"
echo "   [WEBULL-PROTECT-FAILED]             : $(show $AFAIL)"
if unknown "$ATT" "$AFAIL"; then
  verdict "VOID" "could not read the OMS log — cannot tell an attach from a silent failure"
elif [ "$AFAIL" -gt 0 ]; then
  verdict "FAIL" "🔴 ${AFAIL} position(s) HELD WITH NO BROKER-SIDE STOP — act, do not file"
  logtail "$OMSLOG" '[WEBULL-PROTECT-FAILED]' 5 | sed 's/^/      /'
elif [ "${BAREFILL:-0}" -eq 0 ]; then
  verdict "UNEXERCISED" "no Webull buy filled — nothing to attach to"
elif [ "$ATT" -eq 0 ]; then
  verdict "FAIL" "${BAREFILL} Webull fills and ZERO attaches"
else
  verdict "PASS" "${ATT} attached, 0 failed"
fi
echo "   -- #690: the 40-char cap. Look for the id-length refusal, which is what it prevents --"
q "select 'ILLEGAL_PARAMETER/coid-length rejects: '||count(*) from broker_order_events e
   join broker_orders bo on bo.id=e.order_id join broker_accounts ba on ba.id=bo.broker_account_id
   where ba.name='live:orb' and lower(e.event_type)='rejected' and e.event_at >= $D0
     and (e.payload->>'reason') ~* 'ILLEGAL_PARAMETER|client_order_id';"
echo "   (expect 0. Non-zero ⇒ #690 did not cover the id being minted.)"

# ── 5. #691 — DID THE RESERVATION FIGHT ACTUALLY STOP? ───────────────────────────────────────────
hdr "5. #691 RELEASE — the close should no longer be refused as a naked short"
REL=$(logcount "$OMSLOG" '[OMS-EXIT-RELEASE]')
RELRAISE=$(logcount "$OMSLOG" '[OMS-EXIT-RELEASE-RAISED]')
echo "   [OMS-EXIT-RELEASE]        : $(show $REL)"
echo "   [OMS-EXIT-RELEASE-RAISED] : $(show $RELRAISE)  (a failed release ⇒ the close may still be refused)"
echo "   -- THE HEADLINE NUMBER: the two reservation reject strings. 2026-08-13 = 58. --"
RESV=$(q1 "select count(*) from broker_order_events e
   join broker_orders bo on bo.id=e.order_id join broker_accounts ba on ba.id=bo.broker_account_id
   where ba.name='live:orb' and lower(e.event_type)='rejected'
     and e.event_at >= $D0 and e.event_at <= $D1
     and (e.payload->>'reason') ~* 'CAN_NOT_SELL_SHORT|NOT_SUPPORT_REVERSE';")
CLOSES=$(q1 "select count(*) from broker_orders bo join broker_accounts ba on ba.id=bo.broker_account_id
   where ba.name='live:orb' and lower(bo.side)='sell' and bo.client_order_id like '%-close-%'
     and bo.submitted_at >= $D0 and bo.submitted_at <= $D1;")
echo "   reservation rejects today : ${RESV:-?}   (was 58 on 08-13)"
echo "   -close- sells attempted   : ${CLOSES:-?}   <- the denominator"
q "select '   -close- outcome: '||bo.status||' = '||count(*)
   from broker_orders bo join broker_accounts ba on ba.id=bo.broker_account_id
   where ba.name='live:orb' and lower(bo.side)='sell' and bo.client_order_id like '%-close-%'
     and bo.submitted_at >= $D0 and bo.submitted_at <= $D1 group by bo.status;"
if unknown "${CLOSES:-x}" "${RESV:-x}"; then
  verdict "VOID" "the DB query did not return a number — do not read this section"
elif [ "${CLOSES:-0}" -eq 0 ]; then
  verdict "UNEXERCISED" "the software ladder never tried to close a Webull position — #691 had no opportunity"
elif [ "${RESV:-0}" -eq 0 ]; then
  verdict "PASS" "${CLOSES} closes attempted, ZERO reservation rejects (08-13 had 58)"
elif [ "${RESV:-0}" -lt 10 ]; then
  verdict "PARTIAL" "${RESV} rejects vs 58 on 08-13 — collapsed but not gone; expect 1 per episode while the cancel lands"
else
  verdict "FAIL" "${RESV} reservation rejects — the release is not clearing the shares"
fi

# ── 6. #692 — DID ANYTHING END UP UNCOVERED? ─────────────────────────────────────────────────────
hdr "6. #692 REPROTECT — the net that #691 removes must be put back"
RP=$(logcount "$OMSLOG" '[OMS-EXIT-REPROTECT]')
RPS=$(logcount "$OMSLOG" '[OMS-EXIT-REPROTECT-SKIPPED]')
RPF=$(logcount "$OMSLOG" '[OMS-EXIT-REPROTECT-FAILED]')
echo "   [OMS-EXIT-REPROTECT]         : $(show $RP)   (of $(show $REL) releases — 0 here is GOOD, and is not a bare zero)"
echo "   [OMS-EXIT-REPROTECT-SKIPPED] : $(show $RPS)  🔴 could not price a pair ⇒ MAY BE UNCOVERED"
echo "   [OMS-EXIT-REPROTECT-FAILED]  : $(show $RPF)  🔴 re-attach itself failed ⇒ MAY BE UNCOVERED"
if unknown "$RP" "$RPS" "$RPF" "$REL"; then
  verdict "VOID" "could not read the OMS log — an uncovered position would be INVISIBLE here"
elif [ "$RPS" -gt 0 ] || [ "$RPF" -gt 0 ]; then
  verdict "FAIL" "🔴 a released position may be sitting with NO protection — check it by hand NOW"
  { logtail "$OMSLOG" '[OMS-EXIT-REPROTECT-SKIPPED]' 3; logtail "$OMSLOG" '[OMS-EXIT-REPROTECT-FAILED]' 3; } | sed 's/^/      /'
elif [ "$REL" -eq 0 ]; then
  verdict "UNEXERCISED" "no release happened, so no re-protect could be needed"
else
  verdict "PASS" "${REL} releases, ${RP} needed re-protection, none failed"
fi

# ── 7. #687 — DOES A REJECTED WEBULL LEG STOP KILLING THE FLIP? ──────────────────────────────────
hdr "7. #687 CLAIM EXPIRY — one rejection must not burn the rest of the flip"
EXP=$(logcount "$V2LOG" '[V2-FANOUT-CLAIM-EXPIRED]')
COLL=$(logcount "$OMSLOG" 'fanout_webull_collision_managed')
ONFILL=$(logcount "$V2LOG" '[V2-FANOUT-ON-FILL]')
echo "   [V2-FANOUT-CLAIM-EXPIRED] : $(show $EXP)   (fires only when a claimed leg never filled)"
echo "   [V2-FANOUT-ON-FILL]       : $(show $ONFILL)"
echo "   fanout_webull_collision_managed skips : $(show $COLL)"
if unknown "$EXP"; then
  verdict "VOID" "could not read the v2 log"
elif [ "$EXP" -gt 0 ]; then
  verdict "PASS" "the claim expired ${EXP}x instead of latching the flip shut"
else
  verdict "UNEXERCISED" "no claim needed expiring — requires a Webull leg that was claimed and never filled. NOT a pass."
fi

# ── 8. #693 — ARE THE CRONS STILL ALIVE, AND CAN WE STILL DEPLOY? ────────────────────────────────
hdr "8. #693 CRON EXEC BIT — a dirty tree blocks every deploy; a lost +x kills two pagers"
for f in ops/health/bar_gap_watch_cron.sh ops/health/reconcile_alert_cron.sh; do
  if [ -x "$REPO/$f" ]; then echo "   ✓ executable: $f"; else echo "   ✗ NOT EXECUTABLE: $f  ⛔ this cron is silently dead"; fi
done
DIRTY=$(cd "$REPO" && git status --porcelain | wc -l)
if [ "$DIRTY" -eq 0 ]; then verdict "PASS" "tree clean, next deploy will not be refused"
else verdict "FAIL" "${DIRTY} local change(s) — the next deploy WILL be refused. ⛔ do NOT 'git checkout' a cron to fix this; commit the mode."; cd "$REPO" && git status --short | sed 's/^/      /'; fi

# ── 9. WHAT IT COST — percentages, median first ──────────────────────────────────────────────────
hdr "9. THE MONEY — per-trade %, median first (never a bare dollar total)"
q "with rt as (
     select f.symbol, ba.name acct, lower(f.side) side, f.price, f.filled_at,
            row_number() over (partition by ba.name, f.symbol order by f.filled_at) rn
     from fills f join broker_accounts ba on ba.id=f.broker_account_id
     where f.filled_at >= $D0 and f.filled_at <= $D1 and ba.name like 'live:%')
   select '   '||acct||': trades='||count(*)/2||'  median%='||
          coalesce(round(percentile_cont(0.5) within group (order by pct)::numeric,2)::text,'-')||
          '  worst%='||coalesce(round(min(pct)::numeric,2)::text,'-')||
          '  best%='||coalesce(round(max(pct)::numeric,2)::text,'-')
   from (
     select b.acct, 100*(s.price-b.price)/b.price pct
     from rt b join rt s on s.acct=b.acct and s.symbol=b.symbol and s.rn=b.rn+1
     where b.side='buy' and s.side='sell') x group by acct;"
echo "   ⛔ Naive buy→next-sell pairing. It CANNOT attribute per-lot and it cannot see which route"
echo "      exited. Treat as a magnitude, not as attribution."

hdr "DONE"
echo "Read the VERDICT lines only. UNEXERCISED means the path never ran — that is a RESULT,"
echo "not a pass, and it means tomorrow has to ask again."
