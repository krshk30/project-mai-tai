#!/usr/bin/env bash
# §185 — SIGNAL 4: duplicate Webull fan-out legs per armed segment. Read-only.
#
# Usage: ssh mai-tai-vps 'bash -s' -- [FROM_ET_DATE] [TO_ET_DATE] < signal4_duplicate_legs.sh
#        Dates are ET calendar dates, YYYY-MM-DD, TO exclusive. Default = today's ET session.
# ⛔ DATES, NOT TIMESTAMPS, DELIBERATELY. `ssh host 'bash -s' -- "2026-08-11 00:00:00-04"`
#    joins argv with spaces before the remote shell re-splits it, so a timestamp argument
#    silently arrives as TWO arguments and the window becomes nonsense. A date has no
#    space and cannot be split. (Hit while proving this script's own alarm branch.)
#
# ⛔⭐⭐ WHY THIS FILE EXISTS. The deploy sheet listed "duplicate legs per segment,
# above 19-of-119 ⇒ STOP" as a live stop condition while NO QUERY EXISTED for it. A stop
# condition you cannot measure is not a stop condition. Reported UNMEASURED on 08-21 and
# pinned here the same day.
#
# ── THE DEFINITION (states what it EXCLUDES as well as what it counts) ───────────────
# COUNTS  : orders on `live:orb` that are FILLED **BUYS**, carry `fanout_source`, and carry a
#           NON-ZERO segment identity. New rows use `fanout_segment_id`; historical rows fall back
#           to `cw_arm_bar_ts`. Segment = (symbol, segment identity).
#           A segment is a DUPLICATE when any one `cw_entry_n` inside it holds >1 leg.
#           extra legs = (legs in a duplicate segment) - 1.
# EXCLUDES: ⛔⭐⭐ SELLS — added §262, 2026-08-24, BEFORE #766 could reach this population.
#           §186 already settled that an entry is a filled BUY, so this filter was always the
#           definition; it was merely unstated, and unstated held only because the exit-pair
#           success path was DEAD. #766 revives it: that path builds its report from
#           `{**request.metadata, webull_exit_only_pair}`, so a recorded exit leg can inherit
#           `fanout_source` and a non-zero `cw_arm_bar_ts` and walk straight into this
#           population as a SELL — inflating the denominator and, where an exit pairs with its
#           own entry under one `cw_entry_n`, the numerator too.
#           ⭐ Pinned the day before it could happen, not after: measured 08-01..08-24 the
#           population is 150 rows, 100% side='buy', and ZERO `live:orb` rows have ever carried
#           `webull_exit_only_pair` — so this filter is a NO-OP TODAY and the control below
#           still reproduces 119|19|22 unchanged. That is the point: a filter added while it
#           changes nothing is provable; the same filter added after the fact is a number that
#           moved for two reasons at once.
#           ⭐ AND IT IS NOT A NO-OP FOREVER — proven, not asserted. Injecting ONE synthetic
#           exit leg into a read-only CTE (same symbol/segment/`cw_entry_n` as a real buy,
#           `side='sell'`, `webull_exit_only_pair` stamped — exactly the row #766 starts
#           recording) scores the control window at **119|20|23 WITHOUT this filter** and
#           **119|19|22 WITH it**. One exit row manufactures one duplicate segment and one
#           extra leg. The mutant is killed by the filter and by nothing else.
# INCLUDES: `rth_resting_mirror` once it FILLS. The old exclusion claimed a measured
#           720 orders / 0 fills and called the mirror "not a second execution." Direct DB control
#           disproves that premise: ONFO (08-14) and TNON (08-19) filled before the §82 cutoff,
#           then 6 more filled on 08-25. A filled mirror is an execution and can duplicate another
#           fan-out leg, so excluding it would hide exactly the new population. The historical
#           duplicate result remains 119|19|22 because those two old fills carried no identity;
#           the attribution denominator correctly exposes them instead of silently excluding them.
# EXCLUDES: unfilled emissions. ⭐ This is the load-bearing choice and it is NOT cosmetic:
#           on emissions the same window scores 24 dup segments / 26 extra legs; on FILLS
#           it scores 19 / 22. §82's own cost sentence — "all 22 filled worse than the
#           first leg of their own segment" — is about FILLS, so FILLS is the definition
#           that matches the published number. Measure the executions, not the attempts.
#
# ── ⛔ WHAT THIS CANNOT SEE — declared next to the number, not in a runbook ──────────
# Historical legs whose both identity fields are 0 remain INVISIBLE: with no segment id they
# cannot be grouped at all. New drafts bind `fanout_segment_id` before emission, including resting
# legs that precede the BUY arm. ⇒ ANY unattributed filled leg in the requested window makes the
# duplicate result UNEXERCISED; a zero over only the attributable subset is never printed as clean.
#
# ⭐ THE NUMERATOR IS ROBUST, THE DENOMINATOR IS WINDOW-SENSITIVE. Measured across three
# plausible end-cuts the numerator held at 19/22 while the denominator moved 116/119/120.
# ⇒ Always quote the rate WITH its window; never carry "19 of 119" to a different window.
set -u
FROM_D="${1:-}"
TO_D="${2:-}"
Q() { sudo -u postgres psql -d project_mai_tai -X -q -tA -c "$1"; }

# ⛔ A MALFORMED RESULT IS **VOID**, NOT ZERO. If the query errors, psql prints nothing on
# stdout and every downstream count reads empty -> which `${x:-0}` would helpfully turn
# into a clean 0. "The query broke" and "there was no population" must never render the
# same. Sentinel checked FIRST, before any arithmetic touches the value.
ok_shape() { [[ "$1" =~ ^[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+$ ]]; }

# ── the one query, parameterised by window ──────────────────────────────────────────
measure() {  # -> "segments|dup_segments|extra_legs|all_legs|attributed|unattributed"
  Q "WITH l AS (
       SELECT bo.symbol, bo.payload::jsonb AS p
         FROM broker_orders bo JOIN broker_accounts ba ON ba.id = bo.broker_account_id
        WHERE ba.name = 'live:orb'
          AND bo.status = 'filled'
          AND bo.side = 'buy'
          AND bo.payload::jsonb ? 'fanout_source'
          AND bo.submitted_at >= timestamptz '$1' AND bo.submitted_at < timestamptz '$2'),
     a AS (SELECT *, coalesce(nullif(p->>'fanout_segment_id',''),
                             nullif(p->>'cw_arm_bar_ts',''), '0') AS seg FROM l),
     g AS (SELECT symbol, seg, p->>'cw_entry_n' AS n, count(*) AS legs
             FROM a WHERE seg <> '0' GROUP BY 1,2,3),
     s AS (SELECT symbol, seg, sum(legs) AS sl, max(legs) AS mx FROM g GROUP BY 1,2)
     SELECT (SELECT count(*) FROM s) || '|'
            || (SELECT count(*) FROM s WHERE mx > 1) || '|'
            || coalesce((SELECT sum(sl - 1) FROM s WHERE mx > 1), 0) || '|'
            || (SELECT count(*) FROM a) || '|'
            || (SELECT count(*) FROM a WHERE seg <> '0') || '|'
            || (SELECT count(*) FROM a WHERE seg = '0');"
}

quiet_control() {
  Q "WITH a(symbol,seg,n) AS (VALUES ('A','1','1'),('B','2','1')),
          g AS (SELECT symbol,seg,n,count(*) legs FROM a GROUP BY 1,2,3),
          s AS (SELECT symbol,seg,sum(legs) sl,max(legs) mx FROM g GROUP BY 1,2)
     SELECT count(*) || '|' || count(*) FILTER (WHERE mx > 1) || '|'
            || coalesce(sum(sl - 1) FILTER (WHERE mx > 1),0) FROM s;"
}

# ── ⛔ THE CONTROL RUNS FIRST AND CAN VOID THE WHOLE RUN ─────────────────────────────
# A validator that has only ever printed PASS proves nothing. The §82 baseline is a known
# positive with a published answer, so the query must reproduce it EXACTLY or every number
# below it is void. ⛔ VOID is not FAIL and is not PASS — it means the instrument is broken.
CTL_FROM='2026-08-01 00:00:00+00'
CTL_TO='2026-08-19 17:19:55+00'   # the instant PR #739 was opened, i.e. what §82 could see
CTL=$(measure "$CTL_FROM" "$CTL_TO")
echo "### CONTROL — §82 baseline window ($CTL_FROM .. $CTL_TO)"
echo "    expected 119|19|22|225|141|84    got ${CTL:-<empty: query produced nothing>}"
if ! ok_shape "$CTL" || [ "$CTL" != "119|19|22|225|141|84" ]; then
  echo "⛔⛔ CONTROL FAILED — the query no longer reproduces the §82 baseline."
  echo "    EVERY NUMBER BELOW IS VOID. Do not read this run as a pass or a fail."
  echo "    (A schema change, a payload-key rename, or data ageing out of retention all land here.)"
  exit 2
fi
echo "    ✅ control reproduced — the instrument measures what §82 measured."
echo "    the 19 are: UPC YXT×2 INLF×2 WYHG CLRO AZI STKH×2 JWEL×2 PLAG WXM×3 DOGZ AKAN SLE"
QUIET=$(quiet_control)
echo "    quiet control expected 2|0|0    got ${QUIET:-<empty>}"
if [ "$QUIET" != "2|0|0" ]; then
  echo "⛔⛔ QUIET CONTROL FAILED — the instrument cannot prove it stays quiet on clean input."
  echo "    EVERY NUMBER BELOW IS VOID."
  exit 2
fi
echo "    ✅ quiet control reproduced — a non-zero population with no duplicate stays quiet."
echo

# ── the measurement ─────────────────────────────────────────────────────────────────
[ -z "$FROM_D" ] && FROM_D=$(TZ=America/New_York date '+%Y-%m-%d')
[ -z "$TO_D" ]   && TO_D=$(TZ=America/New_York date -d "$FROM_D + 1 day" '+%Y-%m-%d')
# ET midnights, built through the zone so a DST boundary cannot shift the window.
FROM="${FROM_D} 00:00:00 America/New_York"
TO="${TO_D} 00:00:00 America/New_York"

R=$(measure "$FROM" "$TO")
echo "### MEASURED — ET ${FROM_D} .. ${TO_D} (exclusive)"
if ! ok_shape "$R"; then
  echo "    ⛔⛔ VOID — the measurement query returned nothing usable (${R:-<empty>})."
  echo "       This is NOT zero duplicates. Check the window arguments and the DB."
  exit 2
fi
IFS='|' read -r SEG DUP EX TOTAL ATTR UNATTR <<<"$R"
echo "    segments carrying a segment id : ${SEG}"
echo "    duplicate segments             : ${DUP}"
echo "    extra legs                     : ${EX}"
echo "    filled fan-out attribution     : total=${TOTAL} attributed=${ATTR} unattributed=${UNATTR} — trigger=filled buy with fanout_source; polarity: unattributed=0 is REQUIRED to grade"
if [ "${TOTAL}" -eq 0 ] 2>/dev/null; then
  echo "    ⇒ ⛔ UNEXERCISED — denominator is 0. No filled fan-out leg reached this window."
elif [ "${UNATTR}" -ne 0 ] 2>/dev/null; then
  echo "    ⇒ ⛔ UNEXERCISED — ${UNATTR} of ${TOTAL} filled legs cannot be assigned to a segment."
  echo "         The duplicate zero above covers only ${ATTR} attributable legs and is NOT a grade."
elif [ "${SEG}" -eq 0 ] 2>/dev/null; then
  echo "    ⇒ ⛔ UNMEASURED — denominator is 0. No segment carried a segment id in this window,"
  echo "         so 0 duplicates is a NON-RESULT, not a pass. Never report this as 'not worse'."
else
  RATE=$(awk -v d="$DUP" -v s="$SEG" 'BEGIN{printf "%.1f", 100*d/s}')
  BASE=$(awk 'BEGIN{printf "%.1f", 100*19/119}')
  echo "    ⇒ rate ${RATE}% of segments (baseline ${BASE}% = 19 of 119)"
  if awk -v r="$RATE" -v b="$BASE" 'BEGIN{exit !(r>b)}'; then
    echo "    ⇒ 🚨 STOP CONDITION: duplicate rate is ABOVE the 19-of-119 baseline."
  else
    echo "    ⇒ not worse than baseline."
  fi
fi
