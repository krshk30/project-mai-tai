#!/bin/bash
# V2-RESTART PRE-FLIGHT — BLOCKING. Exit 0 = SAFE TO RESTART v2. Non-zero = DO NOT.
#
# ⛔⭐ WHY THIS EXISTS. Bug 2: `cw_entries_this_flip` is a plain in-memory field on SymbolState.
# #644 moved its reset from ARM to DISARM but did NOT persist it, so a v2 restart reconstructs
# segments from the DB seed with the count at ZERO and the one-entry-per-segment cap silently
# re-issues on every armed segment. That is the CPHI mechanism, unchanged.
#
# ⛔ THE FLEET-FLAT RULE DOES NOT COVER IT. Flat checks POSITIONS; the reset fires on ARMED
# SEGMENTS, which exist with no position at all. Both conditions must hold.
#
# ⛔ ASSERTS, never merely prints. The 07-15 restart incident was a check whose output nobody read.
#
# ⛔⭐ NOTE ON REPLAY ARMS — the opposite rule from the entry-fix watch, deliberately.
# The watch EXCLUDES warmup-replay arms because it asks "was this a live trading opportunity?".
# This asks "is the bot ARMED RIGHT NOW?" — and a replay-armed segment is genuinely armed in
# memory and WILL re-issue on restart. So this counts EVERY arm. Same signal, different job,
# opposite correct treatment. [[feedback_authoritative_for_a_is_not_for_b]]
set -u

# ---------- OPERATOR OVERRIDE (gate 1 only) ----------
# ⛔⭐ THIS IS NOT A BYPASS AND MUST NOT BECOME ONE.
# It exists for ONE situation: the armed set is INERT (symbols receiving no bars can never
# self-clear, so the gate can never go green) and the operator has accepted Bug 2 with the facts
# stated. Restarting then re-issues the entry CAP on those segments -- which BLOCKS entries, so the
# cost is a LOST entry, never a duplicate.
# THREE THINGS MAKE IT DELIBERATE RATHER THAN HABITUAL:
#   1. the operator must NAME every armed symbol; a bare flag does nothing
#   2. the named set must match the LIVE armed set EXACTLY -- so a list copied from an earlier run
#      is REFUSED the moment state moves. It cannot be reused tomorrow without being retyped.
#   3. a second literal token must be passed. Two things to type = a decision, not a reflex.
# It overrides GATE 1 ONLY. Fleet-flat and the clock still block. Both the override and the symbols
# are printed and must be quoted in the deploy report.
OVERRIDE_SYMS=""; OVERRIDE_CONFIRM=""
CLOCK_REASON=""; CLOCK_CONFIRM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --operator-override) OVERRIDE_SYMS="${2:-}"; shift 2 ;;
    --i-accept-bug2)     OVERRIDE_CONFIRM="yes"; shift ;;
    --clock-override)    CLOCK_REASON="${2:-}"; shift 2 ;;
    --i-accept-clock)    CLOCK_CONFIRM="yes"; shift ;;
    *) shift ;;
  esac
done

V2_LOG=/var/log/project-mai-tai/schwab-1m-v2.log
ENV=/etc/project-mai-tai/project-mai-tai.env
STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
FAIL=0

echo "=== V2 RESTART PRE-FLIGHT — $STAMP ==="

# ---------- gate 0: the clock ----------
# ⛔⭐ THE STATED REASON BELOW WAS WRONG — CORRECTED 2026-08-06, THRESHOLD DELIBERATELY UNCHANGED.
#
#   The original text said "v2's ENTRY WINDOW RUNS TO 18:00, so segments can still arm."
#   BOTH halves are wrong, and they are wrong about DIFFERENT things:
#
#   (a) The entry window ends at 16:00, not 18:00. settings.py
#       `strategy_schwab_1m_v2_entry_window_end_hour_et = 16`, no env override, and ZERO live
#       post-16:00 entries in 30 days (the last eleven were 07-07..07-15, stopping exactly at the
#       07-15 rule change).
#
#   (b) ⭐ BUT 16:00 IS NOT THE RIGHT NUMBER EITHER, AND ENCODING IT WOULD BE WORSE THAN 18:00.
#       ARMING IS BAR-DRIVEN AND BARS FLOW UNTIL 20:00. The entry window is a DIFFERENT gate on a
#       DIFFERENT event, checked at EMIT. A segment CAN still arm at 17:00 — it simply cannot enter.
#       Lowering this threshold to 16:00 would permit restarts during a period when the guarded
#       event STILL OCCURS. 18:00 is wrong; 16:00 is wrong in the DANGEROUS direction.
#
#   ⇒ The threshold is left at 18:00 precisely because the correct value is not known, and a
#     safety assertion is not the place to guess one under time pressure.
#
# ⭐⭐ BOARD ITEM (redesign, NOT a hurried edit): GATE 0 IS A PROXY FOR GATE 1.
#   The clock approximates "nothing is armed"; gate 1 MEASURES it directly. A proxy that blocks
#   while the direct measurement passes is the PROXY being wrong. The likely redesign is "drop the
#   clock entirely when published state is fresh and zero-armed" — but that wants design and a
#   test, not a patch. [[feedback_authoritative_for_a_is_not_for_b]]
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))
if [ "$ETMIN" -lt 1080 ]; then
  if [ -n "$CLOCK_REASON" ] && [ "$CLOCK_CONFIRM" = "yes" ]; then
    # ⛔ TWO TOKENS, mirroring gate 1: a reason must be NAMED and a literal confirm passed.
    # ⛔ THIS OVERRIDES THE CLOCK ONLY. Gate 1 (armed segments), managed rows and fleet-flat all
    #    still block — and gate 1 is the DIRECT measure this clock was only approximating.
    echo "  [OVERRIDE] clock gate (<18:00 ET) overridden by OPERATOR"
    echo "             reason: $CLOCK_REASON"
    echo "             ⛔ this asserts NOTHING about armed state — gate 1 below is the real check."
  else
    echo "  [BLOCK] it is before 18:00 ET. ⛔ See the corrected note above: this threshold is a"
    echo "          PROXY for 'nothing is armed', and it is known to be the wrong number in both"
    echo "          directions. Gate 1 measures armed state directly — if it is green and you need"
    echo "          to proceed, override deliberately:"
    echo "            --clock-override '<reason>' --i-accept-clock"
    FAIL=1
  fi
else
  echo "  [ok]    past 18:00 ET"
fi

# ---------- gate 1: ARMED SEGMENTS (the one fleet-flat misses) ----------
# ⛔⭐⭐ THE PUBLISHED STATE IS AUTHORITATIVE. THE LOG IS A RECONSTRUCTION (2026-08-05).
# This gate used to pair ARM/DISARM over today's log and take the last event per symbol. That is
# wrong in BOTH directions and was measured wrong on 2026-08-05 at 20:15 ET:
#   under-reported FUSE HYFM AXTL — armed 08-03, so NO arm line exists in today's files at all
#   over-reported  PAVS ZCMD      — DANGLING ARMs: `_apply_session_anchor_reset` clears cw_armed
#                                   with NO [V2-CW-DISARM] line, so a replay-armed segment leaves
#                                   an ARM that is never closed and the pairing reads it as live.
# The bot publishes `cw_armed_segments` (its own in-memory truth) every 5s. Use that.
#
# ⛔ FAIL CLOSED. Reading a snapshot introduces a NEW failure mode the log did not have: a wedged
# bot or a stalled publisher leaves an OLD CLEAN board that would read as GO. So freshness is
# ASSERTED, and missing/stale/unparseable all BLOCK. A gate that cannot see must never say GO.
#
# ⛔ THE LOG SET IS STILL COMPUTED — and divergence is REPORTED. Tonight's divergence WAS the
# finding; a gate showing both would have surfaced the silent-disarm path weeks ago. Divergence
# warns, it does not block: the published state already decided.
# ⛔ Log files rotate at 00:00 UTC = 20:00 ET, so an ET trading day SPLITS across two files.
# Glob them — a single-file read after 20:00 ET silently loses the day (7466 lines -> 0, measured).
ARM_MAX_AGE_S=60
GATE1=$(sudo /home/trader/project-mai-tai/.venv/bin/python - "$ARM_MAX_AGE_S" <<'PY'
import sys, json, re, subprocess, glob, datetime as dt
max_age = float(sys.argv[1])
try:
    raw = subprocess.run(["redis-cli","XREVRANGE","mai_tai:strategy-state-isolated","+","-","COUNT","1"],
                         capture_output=True, text=True, timeout=10).stdout
    blobs = re.findall(r'\{.*\}', raw, re.S)
    if not blobs:
        print("STATE_MISSING no-snapshot-in-stream"); sys.exit(0)
    d = json.loads(max(blobs, key=len))
    prod = dt.datetime.fromisoformat(d["produced_at"].replace("Z","+00:00"))
    age = (dt.datetime.now(dt.timezone.utc) - prod).total_seconds()
    if age > max_age:
        print("STATE_STALE %.1f" % age); sys.exit(0)
    armed = sorted(s["symbol"] for s in d["payload"].get("cw_armed_segments", []))
    print("STATE_OK %.1f %s" % (age, " ".join(armed)))
except Exception as exc:
    print("STATE_ERROR %s" % type(exc).__name__)
PY
)
# log-derived reconstruction, ACROSS ROTATIONS, for divergence reporting only
# ⛔ SCOPE: current file + the single most recent rotation. That is exactly the ET trading day,
# which SPLITS at the 00:00 UTC = 20:00 ET rotation. Globbing ALL history instead makes this warn
# line permanently ~12 symbols long (every dangling ARM ever) — and a warning nobody reads is a
# false-clean generator, which is the failure this gate exists to avoid.
LOGSET=$(for f in $(ls -1t /var/log/project-mai-tai/schwab-1m-v2.log* 2>/dev/null | head -2); do
           case "$f" in *.gz) sudo zcat "$f" 2>/dev/null;; *) sudo cat "$f" 2>/dev/null;; esac
         done | awk '
  /\[V2-CW-ARM\]/    { for(i=1;i<=NF;i++) if($i=="[V2-CW-ARM]")    { s=$(i+1); st[s]="ARM"  } }
  /\[V2-CW-DISARM\]/ { for(i=1;i<=NF;i++) if($i=="[V2-CW-DISARM]") { s=$(i+1); st[s]="DIS" } }
  END { for (s in st) if (st[s]=="ARM") print s }' | sort | tr '\n' ' ')

KIND=$(echo "$GATE1" | awk '{print $1}')
case "$KIND" in
  STATE_OK)
    AGE=$(echo "$GATE1" | awk '{print $2}')
    ARMED=$(echo "$GATE1" | cut -d' ' -f3-)
    ARMED_N=$(echo $ARMED | wc -w)
    if [ "$ARMED_N" -gt 0 ]; then
      WANT=$(echo $ARMED   | tr ' ' '
' | sort | tr '
' ' ')
      GOT=$(echo $OVERRIDE_SYMS | tr ',' '
' | tr ' ' '
' | grep -v '^$' | sort | tr '
' ' ')
      if [ -n "$OVERRIDE_SYMS" ] && [ "$OVERRIDE_CONFIRM" = "yes" ] && [ "$WANT" = "$GOT" ]; then
        echo "  [OVERRIDE] $ARMED_N ARMED SEGMENT(S) accepted by the OPERATOR: $ARMED"
        echo "             Bug 2 WILL re-issue the entry cap on each. That BLOCKS entries;"
        echo "             the cost is a lost entry, never a duplicate. Quote this line in the report."
      else
        echo "  [BLOCK] $ARMED_N ARMED SEGMENT(S) [published state, ${AGE}s old]: $ARMED"
        echo "          A restart re-issues the entry cap on each of these (Bug 2). DO NOT RESTART v2."
        if [ -n "$OVERRIDE_SYMS" ]; then
          [ "$OVERRIDE_CONFIRM" != "yes" ] && echo "          override REFUSED: --i-accept-bug2 not given"
          [ "$WANT" != "$GOT" ] && {
            echo "          override REFUSED: named set does not match the LIVE armed set."
            echo "            live:  $WANT"
            echo "            named: $GOT"
            echo "          State moved since the list was written. Re-read and retype it."; }
        fi
        FAIL=1
      fi
    else
      echo "  [ok]    zero armed segments [published state, ${AGE}s old]"
    fi
    ONLY_LOG=""; for s in $LOGSET; do echo " $ARMED " | grep -q " $s " || ONLY_LOG="$ONLY_LOG $s"; done
    ONLY_ST="";  for s in $ARMED;  do echo " $LOGSET " | grep -q " $s " || ONLY_ST="$ONLY_ST $s"; done
    if [ -n "${ONLY_LOG// /}" ] || [ -n "${ONLY_ST// /}" ]; then
      echo "  [warn]  log/state DIVERGENCE (reported, not blocking — state already decided)"
      [ -n "${ONLY_LOG// /}" ] && echo "          log-only (dangling ARMs, silent anchor disarm):$ONLY_LOG"
      [ -n "${ONLY_ST// /}"  ] && echo "          state-only (armed on an earlier day, no arm line today):$ONLY_ST"
    fi
    ;;
  STATE_STALE)
    echo "  [BLOCK] published state is STALE ($(echo "$GATE1" | awk '{print $2}')s > ${ARM_MAX_AGE_S}s)."
    echo "          A stalled publisher shows an OLD CLEAN board. Failing closed. Log said: $LOGSET"
    FAIL=1 ;;
  *)
    echo "  [BLOCK] cannot read published state ($GATE1). Failing closed. Log said: $LOGSET"
    FAIL=1 ;;
esac

# ---------- gate 2: fleet flat ----------
URL=$(sudo grep -E '^MAI_TAI_DATABASE_URL=' "$ENV" | head -1 | cut -d= -f2-)
export PGPASSWORD=$(echo "$URL" | sed -E 's|^[^:]+://[^:]+:([^@]+)@.*|\1|')
PGUSER=$(echo "$URL" | sed -E 's|^[^:]+://([^:]+):.*|\1|')
DSN="dbname=project_mai_tai user=${PGUSER} host=localhost"

ROWS=$(psql "$DSN" -tAc "SELECT count(*) FROM oms_managed_positions WHERE status='open';" 2>/dev/null)
if [ "${ROWS:-x}" != "0" ]; then
  echo "  [BLOCK] ${ROWS:-?} open managed row(s) — not flat"
  psql "$DSN" -tAc "SELECT '          '||broker_account_name||' '||symbol||' qty='||current_quantity FROM oms_managed_positions WHERE status='open';" 2>/dev/null
  FAIL=1
else
  echo "  [ok]    zero open managed rows"
fi

# Broker positions, EXCLUDING the operator's manual holdings (not ours -- scoping invariant).
#
# ⛔⭐ THIS HARDCODED TUPLE IS A SECOND, HAND-MAINTAINED COPY OF MAI_TAI_PROTECTED_SYMBOLS.
#   It does not read the env. CELZ removed 2026-08-06 IN LOCKSTEP with its removal from the env --
#   had it been left, a v2-held CELZ position would have been EXCLUDED from this flat check and the
#   gate would have reported flat while the bot held it. That hole was created by tonight's env
#   change and is closed with it.
#
# ⭐⭐ BOARD ITEM — A LIST IS THE WRONG MECHANISM ENTIRELY.
#   The OMS already owns the discriminator: `virtual_quantity == 0` => NOT OURS. The bot never acts
#   on a manual position and needs no list to avoid one. This gate should assert over POSITIONS THE
#   OMS OPENED, not "broker-flat minus a hand-typed tuple" -- then it never asks again, for any
#   manual name, with nothing to maintain.
#   ⛔ BEFORE changing it, ENUMERATE EVERY READER of protected_symbol_set: if a flatten / EOD sweep
#   / reconcile path also reads it, the list is doing several jobs under one name and is NOT
#   redundant there. [[feedback_authoritative_for_a_is_not_for_b]]
#   ⛔ NOT a hurried edit. 2026-08-06 nearly saw a threshold changed under time pressure on this
#   same file; the correct value was not what it looked like.
# ⛔⭐⭐ A FAILED QUERY IS NOT A FLAT ACCOUNT. This was `psql ... 2>/dev/null` with a bare
# emptiness test: a dead DSN, a stopped Postgres, or any SQL error printed nothing on stdout and
# the gate said "[ok] broker flat". ⇒ THE ONE FAILURE MODE A LIVE-MONEY PREFLIGHT MUST NOT HAVE,
# in the exact check that authorises restarting a service while we might be holding.
# ⇒ A SENTINEL makes "no rows" distinguishable from "the query died". `COALESCE` cannot help here
#   — it returns '' for both — so the shape is carried in the payload itself, and the exit status
#   is captured separately rather than swallowed.
POS=$(psql "$DSN" -tAc "
  SELECT 'POSOK|'||COALESCE(string_agg(ap.symbol||'='||ap.quantity,' '),'')
  FROM account_positions ap JOIN broker_accounts ba ON ba.id=ap.broker_account_id
  WHERE ba.name IN ('live:schwab_1m_v2','live:orb')
    AND ap.quantity <> 0 AND ap.symbol NOT IN ('CYN','TE');" 2>&1)
POS_RC=$?
case "$POS" in
  POSOK\|*) POS="${POS#POSOK|}" ;;
  *)         POS_RC=1 ;;
esac
if [ "$POS_RC" -ne 0 ]; then
  echo "  [BLOCK] COULD_NOT_TELL — the broker-position query FAILED; this is NOT 'flat'."
  echo "          $(printf '%s' "$POS" | head -1 | cut -c1-120)"
  FAIL=1
elif [ -n "${POS// /}" ]; then
  echo "  [BLOCK] broker not flat (excluding operator manuals): $POS"
  FAIL=1
else
  echo "  [ok]    broker flat on both real-money accounts (operator manuals excluded)"
fi

echo
if [ "$FAIL" -eq 0 ]; then
  # ⛔ The GO line must describe what ACTUALLY happened. It used to hard-code "Zero armed
  # segments AND flat", which is FALSE on an override run -- it printed "zero" while seven
  # segments were accepted, straight into the deploy record. A gate that misreports its own
  # verdict is worse than no gate.
  if [ -n "$OVERRIDE_SYMS" ] && [ "$OVERRIDE_CONFIRM" = "yes" ]; then
    echo "  ===> GO **BY OPERATOR OVERRIDE**. NOT zero-armed: $ARMED_N segment(s) accepted."
    echo "       overridden: $ARMED"
    echo "       Bug 2 will re-issue the entry cap on those. Quote this verbatim in the report."
  else
    echo "  ===> GO. Zero armed segments AND flat. Safe to restart v2."
  fi
  exit 0
fi
echo "  ===> NO-GO. Do not restart v2. Re-run when the blocking lines clear."
exit 1
