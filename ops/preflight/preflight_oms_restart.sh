#!/bin/bash
# OMS-RESTART PRE-FLIGHT — BLOCKING. Exit 0 = SAFE TO RESTART the OMS. Non-zero = DO NOT.
#
# ⛔⭐⭐ WHY THIS EXISTS (Q0/§81.4). THE SOFTWARE EXIT LADDER IS IN-PROCESS IN THE OMS.
# In extended hours a held position has NO broker-side cover:
#   * Schwab REFUSES a stop leg in extended hours — 09:30 is the earliest a native stop exists.
#   * The Webull fan-out leg rests at its own broker and fills BARE; the #689 re-protect attach has
#     NEVER ONCE SUCCEEDED (0 ATTACHED in 7 days).
#   * `oms/service.py` says it in its own words when the attach fails: "THE POSITION IS HELD WITH NO
#     BROKER-SIDE STOP; the software ladder is the only cover."
# So restarting the OMS while a pre-market position is open removes THE ONLY THING PROTECTING IT.
# On 2026-08-18 that fence was a human remembering, for 26 minutes.
#
# ⛔ THIS IS A SIBLING, NOT AN EDIT. `preflight_v2_restart.sh` gates **v2** restarts and asks a
# different question (armed segments / Bug 2). This one gates an **OMS** restart and asks whether a
# position is open with the ladder as its only cover. Neither subsumes the other; do not merge them.
#
# ⛔⭐ IT FAILS CLOSED. Every source it cannot read, and every source it cannot prove FRESH, BLOCKS.
# A fence that opens on uncertainty is the same defect class as an empty list read as a flat
# account — which is exactly how `virtual_positions` reports ZERO for a position we hold.
#
# ⛔⭐⭐ THE DEADLOCK IS REAL AND IS HANDLED DELIBERATELY.
#   The OMS is the SOLE WRITER of `account_positions` (`oms/store.py`). So when the OMS is wedged or
#   dead, broker truth goes STALE — precisely when restarting it is the remedy. Refusing is still
#   correct: at that moment we genuinely DO NOT KNOW whether a position is naked. But a fence that
#   can only ever say NO would block the fix, so the override below is the escape hatch. It states
#   the cost, demands the symbol set be named, and is recorded — see the OVERRIDE block.
#
# ⛔ THE PROTECTED-SYMBOL LIST IS READ FROM THE ENV, NOT HARDCODED. The v2 sibling keeps a
# hand-maintained second copy of `MAI_TAI_PROTECTED_SYMBOLS` and its own comment records that the
# copy drifted once (CELZ), briefly hiding a bot-held position from a flat check. If the env cannot
# be read this script uses an EMPTY exclusion set, which OVER-blocks — the safe direction. It never
# silently assumes a symbol is the operator's.
#
# Exit codes:  0 = GO  ·  1 = BLOCKED (a position is open)  ·  2 = CANNOT SEE (refused)
set -u

ENV_FILE=${MAI_TAI_ENV_FILE:-/etc/project-mai-tai/project-mai-tai.env}
# Real-money accounts only. Per the fleet roster: Schwab v2 is the real-money bot and `live:orb` is
# its Webull FAN-OUT leg (fills there are fan-out legs, NOT ORB trades). Paper accounts carry no
# risk and must not block a restart.
REAL_ACCOUNTS="live:schwab_1m_v2 live:orb"
# account_positions is refreshed on a ~15s cadence in normal operation; 300s is generous enough to
# absorb a slow sync and tight enough that a dead OMS is caught within one deploy decision.
POS_MAX_AGE_S=${POS_MAX_AGE_S:-300}

OVERRIDE_SYMS=""; OVERRIDE_CONFIRM=""
while [ $# -gt 0 ]; do
  case "$1" in
    # ⛔⭐ NOT A BYPASS, AND MUST NOT BECOME ONE. Mirrors the v2 sibling's design so the habit
    # transfers: the operator must NAME every symbol, and the named set must match the LIVE set
    # EXACTLY — so a list copied from an earlier run is REFUSED the moment state moves. Two things
    # to type = a decision, not a reflex. Both lines print and belong in the deploy report.
    # ⛔ It overrides the POSITION gate only. It can NEVER override "cannot see"; overriding an
    #    unknown is not accepting a cost, it is declining to find out what the cost is.
    --operator-override)   OVERRIDE_SYMS="${2:-}"; shift 2 ;;
    --i-accept-naked-position) OVERRIDE_CONFIRM="yes"; shift ;;
    --max-age-seconds)     POS_MAX_AGE_S="${2:-300}"; shift 2 ;;
    *) shift ;;
  esac
done

STAMP=$(TZ=America/New_York date '+%F %H:%M:%S %Z')
FAIL=0        # 1 => a position is open (exit 1)
BLIND=0       # 1 => could not see (exit 2, and BLIND outranks FAIL)

echo "=== OMS RESTART PRE-FLIGHT — $STAMP ==="

# ---------- gate 0: can we reach the database at all ----------
URL=$(sudo grep -E '^MAI_TAI_DATABASE_URL=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
if [ -z "${URL:-}" ]; then
  echo "  [BLIND] cannot read MAI_TAI_DATABASE_URL from $ENV_FILE"
  echo "          ⛔ This is UNKNOWN, not clean. Refusing."
  echo "  ===> NO-GO (cannot see). Do not restart the OMS."
  exit 2
fi
export PGPASSWORD=$(echo "$URL" | sed -E 's|^[^:]+://[^:]+:([^@]+)@.*|\1|')
PGUSER=$(echo "$URL" | sed -E 's|^[^:]+://([^:]+):.*|\1|')
# ⛔ The database NAME is derived from the URL, not hardcoded. The v2 sibling hardcodes
# `dbname=project_mai_tai`, which silently ignores the configured URL — so it would keep reporting
# on the old database after any cutover, and it cannot be pointed at a scratch DB to prove its own
# blocking paths fire. Deriving it is both more correct and what makes this script testable.
PGDB=$(echo "$URL" | sed -E 's|^[^/]+//[^/]+/([^?]+).*|\1|')
[ -z "${PGDB:-}" ] && PGDB=project_mai_tai
DSN="dbname=${PGDB} user=${PGUSER} host=localhost"

if ! psql "$DSN" -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "  [BLIND] database unreachable with the configured DSN"
  echo "          ⛔ Position truth is unknowable. Refusing."
  echo "  ===> NO-GO (cannot see). Do not restart the OMS."
  exit 2
fi
echo "  [ok]    database reachable"

# ---------- gate 1: the session ----------
# ⛔ Boundaries copied from the OMS's OWN `_extended_hours_session`: regular session is
# 09:30 <= t < 16:00 ET; anything else is extended hours (AM before, PM after). Keeping the same
# numbers matters — a fence that disagreed with the code about when cover exists would be worse
# than no fence.
ETMIN=$(( 10#$(TZ=America/New_York date '+%H') * 60 + 10#$(TZ=America/New_York date '+%M') ))
if [ "$ETMIN" -ge 570 ] && [ "$ETMIN" -lt 960 ]; then
  SESSION="RTH"
  echo "  [info]  session=REGULAR (09:30-16:00 ET) — a native stop CAN exist here"
else
  SESSION="EH"
  [ "$ETMIN" -lt 570 ] && EHK="AM" || EHK="PM"
  echo "  [info]  session=EXTENDED HOURS ($EHK) — ⛔ NO broker-side stop is possible; the"
  echo "          in-process software ladder is the ONLY cover for anything held right now."
fi

# ---------- gate 2: protected (operator-manual) symbols, from the ENV ----------
PROT_RAW=$(sudo grep -E '^MAI_TAI_PROTECTED_SYMBOLS=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
if [ -z "${PROT_RAW:-}" ]; then
  echo "  [warn]  MAI_TAI_PROTECTED_SYMBOLS unreadable/unset — using an EMPTY exclusion set."
  echo "          That OVER-blocks (operator manuals will also block). Safe direction, on purpose."
  PROT_SQL="''"
else
  PROT_SQL=$(echo "$PROT_RAW" | tr ',' '\n' | sed "s/^[ \t]*//;s/[ \t]*$//" | grep -v '^$' \
             | sed "s/.*/'&'/" | paste -sd, -)
  echo "  [ok]    operator manuals excluded (from env): $PROT_RAW"
fi

# ---------- gate 3: the OMS's own managed rows ----------
# These are BY DEFINITION ours — the OMS writes a managed row per position its ladder owns. They
# live in the DB and survive an OMS crash, so this signal still works when the OMS is down, which is
# exactly the case broker truth cannot cover.
MROWS=$(psql "$DSN" -tAc "SELECT count(*) FROM oms_managed_positions WHERE status='open';" 2>/dev/null)
if ! [[ "${MROWS:-}" =~ ^[0-9]+$ ]]; then
  echo "  [BLIND] cannot count open managed rows"
  BLIND=1
elif [ "$MROWS" -gt 0 ]; then
  echo "  [BLOCK] $MROWS open managed row(s) — the ladder is actively managing a position:"
  psql "$DSN" -tAc "SELECT '          '||broker_account_name||' '||symbol||' qty='||current_quantity
                    FROM oms_managed_positions WHERE status='open';" 2>/dev/null
  FAIL=1
else
  echo "  [ok]    zero open managed rows"
fi

# ---------- gate 4: broker truth, PER ACCOUNT, with freshness ASSERTED ----------
# ⛔⭐ FRESHNESS IS ASSERTED PER ACCOUNT, NEVER GLOBALLY. A global max(updated_at) reads FRESH
# whenever ANY account syncs — and on 2026-08-19 the newest rows in the whole table belonged to the
# PAPER account. A real-money account could sit hours stale behind a healthy-looking global gauge.
OPEN_POS=""
for acct in $REAL_ACCOUNTS; do
  AGE=$(psql "$DSN" -tAc "
    SELECT COALESCE(round(extract(epoch from (now()-max(ap.updated_at)))::numeric,0)::text,'NULL')
    FROM account_positions ap JOIN broker_accounts ba ON ba.id=ap.broker_account_id
    WHERE ba.name='$acct';" 2>/dev/null)
  if [ -z "${AGE:-}" ] || [ "$AGE" = "NULL" ]; then
    echo "  [BLIND] $acct: no position rows / no sync timestamp — broker truth unknowable"
    echo "          ⛔ The OMS is the SOLE WRITER of account_positions; if it is down this is"
    echo "             EXPECTED, and it is still UNKNOWN. It must not read as flat."
    BLIND=1
    continue
  fi
  if [ "$AGE" -gt "$POS_MAX_AGE_S" ]; then
    echo "  [BLIND] $acct: broker positions are STALE (${AGE}s > ${POS_MAX_AGE_S}s)"
    echo "          ⛔ A stalled sync shows an OLD FLAT board, which is the false-clean this"
    echo "             fence exists to refuse."
    BLIND=1
    continue
  fi
  P=$(psql "$DSN" -tAc "
    SELECT COALESCE(string_agg(ap.symbol||'='||ap.quantity,' '),'')
    FROM account_positions ap JOIN broker_accounts ba ON ba.id=ap.broker_account_id
    WHERE ba.name='$acct' AND ap.quantity <> 0 AND ap.symbol NOT IN ($PROT_SQL);" 2>/dev/null)
  if [ -n "${P// /}" ]; then
    echo "  [BLOCK] $acct NOT FLAT [${AGE}s old]: $P"
    OPEN_POS="$OPEN_POS $(echo "$P" | tr ' ' '\n' | cut -d= -f1 | tr '\n' ' ')"
    FAIL=1
  else
    echo "  [ok]    $acct flat [${AGE}s old]"
  fi
done

# ---------- verdict ----------
echo
# ⛔ BLIND OUTRANKS BLOCK, AND OUTRANKS THE OVERRIDE. "I could not see" is not a cost the operator
# can accept on the spot, because the size of the cost is the very thing that is unknown.
if [ "$BLIND" -eq 1 ]; then
  echo "  ===> NO-GO (CANNOT SEE). One or more sources could not be read or proven fresh."
  echo "       ⛔ Do NOT restart the OMS on an unknown. If the OMS itself is down and that is why"
  echo "          broker truth is stale, confirm the position by hand ON THE BROKER'S SCREEN —"
  echo "          the broker's screen outranks our tables — then re-run, or override deliberately:"
  echo "            --operator-override '<SYM,SYM>' --i-accept-naked-position"
  exit 2
fi

if [ "$FAIL" -eq 1 ]; then
  WANT=$(echo $OPEN_POS | tr ' ' '\n' | grep -v '^$' | sort -u | tr '\n' ' ')
  GOT=$(echo $OVERRIDE_SYMS | tr ',' '\n' | tr ' ' '\n' | grep -v '^$' | sort -u | tr '\n' ' ')
  if [ -n "$OVERRIDE_SYMS" ] && [ "$OVERRIDE_CONFIRM" = "yes" ] && [ "$WANT" = "$GOT" ]; then
    echo "  ===> GO **BY OPERATOR OVERRIDE**. NOT flat: $WANT"
    if [ "$SESSION" = "EH" ]; then
      echo "       ⛔⛔ EXTENDED HOURS: these positions have NO broker-side stop. For the whole"
      echo "            restart they have NO cover at all. Place a manual stop FIRST if you can —"
      echo "            in EH you usually cannot, which is the entire point of this fence."
    else
      echo "       ⛔ The software ladder dies with the process. Any native OCO keeps working;"
      echo "          anything relying on the ladder does not. Confirm cover before proceeding."
    fi
    echo "       Quote this line verbatim in the deploy report."
    exit 0
  fi
  echo "  ===> NO-GO. A position is open; the OMS ladder is managing it."
  if [ "$SESSION" = "EH" ]; then
    echo "       ⛔⛔ EXTENDED HOURS — restarting now leaves it with NO cover whatsoever."
  fi
  if [ -n "$OVERRIDE_SYMS" ]; then
    [ "$OVERRIDE_CONFIRM" != "yes" ] && \
      echo "       override REFUSED: --i-accept-naked-position not given"
    [ "$WANT" != "$GOT" ] && {
      echo "       override REFUSED: named set does not match the LIVE open set."
      echo "         live:  $WANT"
      echo "         named: $GOT"
      echo "       State moved since the list was written. Re-read and retype it."; }
  fi
  exit 1
fi

echo "  ===> GO. Flat on every real-money account, zero managed rows, all sources fresh."
echo "       Safe to restart the OMS."
exit 0
