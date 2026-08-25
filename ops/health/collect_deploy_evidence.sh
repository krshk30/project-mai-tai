#!/usr/bin/env bash
# Deploy-evidence collector — 2026-08-20 window. Read-only.
# Emits exactly the values the handoff skeleton has FILL slots for.
#
# Usage: ssh mai-tai-vps 'bash -s' -- PRE|POST [--since <"YYYY-MM-DD HH:MM:SS" UTC>|boot|all] < collect_deploy_evidence.sh
#
# ⛔⭐⭐ EVERY COUNT IS SCOPED, AND THE SCOPE IS PRINTED NEXT TO IT (2026-08-21).
# The 08-20 POST run reported seed-gap fail-open = 30 because `cnt` summed EVERY retained
# rotation — 24 of those belonged to the process the deploy REPLACED. Restart-scoped truth
# was 0. A number that grades the old process alongside the new one is not a deploy signal,
# and it errs in the direction that makes a working fix look broken.
# ⇒ Counts now print as  `scoped / all-retained`. Both are kept on purpose: the scoped half
#   grades THIS process, the all-retained half is the baseline it must be compared against.
#   Neither one alone is the answer, so neither one is printed alone.
PHASE="${1:-POST}"
SCOPE_ARG="boot"
if [ "${2:-}" = "--since" ]; then SCOPE_ARG="${3:-boot}"; fi
LOGDIR=/var/log/project-mai-tai
cd /home/trader/project-mai-tai || exit 1
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ── scope resolution ────────────────────────────────────────────────────────────────────
# `boot` resolves PER SERVICE — oms and schwab-1m-v2 do not restart at the same instant
# (08-20: 20:14:49 vs 20:16:46), so one shared cutoff would mis-scope one of them.
svc_boot() {  # svc_boot <basename> -> "YYYY-MM-DD HH:MM:SS" UTC, or empty
  local raw
  raw=$(systemctl show "project-mai-tai-$1.service" -p ActiveEnterTimestamp --value 2>/dev/null)
  [ -n "$raw" ] && date -u -d "$raw" '+%Y-%m-%d %H:%M:%S' 2>/dev/null
}
scope_for() {  # scope_for <basename> -> cutoff string, or empty for "all"
  case "$SCOPE_ARG" in
    all)  echo "" ;;
    boot) svc_boot "$1" ;;
    *)    echo "$SCOPE_ARG" ;;
  esac
}

# ── the timestamped stream ──────────────────────────────────────────────────────────────
# ⛔⭐⭐ A TIMESTAMP FILTER THAT STRING-COMPARES AGAINST MULTI-LINE RECORDS IS NOT A TIME
# FILTER (rule earned 08-20). `awk '$0 >= ts'` passes every traceback continuation line in
# the file — and the fail-open path logs with exc_info=True, so it is exactly the path that
# produces them. The stream is therefore reduced to lines that BEGIN with a real timestamp
# BEFORE any comparison; a continuation line can then never be counted or dated.
# ⛔ `zcat -f` reads .gz AND plain rotations. Plain grep silently skips compressed days,
# which is a truncated answer wearing a confident number.
# ⛔⭐⭐ THE STREAM IS SORTED, AND THAT IS NOT COSMETIC (08-21). `zcat` emits the CURRENT
# file first and the rotations after it, so the concatenation runs newest-block-then-oldest-
# block. Any `tail -N` on it therefore returns the end of the OLDEST rotation, not the most
# recent N — the 6c census read `tail -3` and silently omitted TODAY'S line while looking
# perfectly well-formed. Counting is order-independent; every head/tail is not. Sort once
# here so no reader downstream has to remember which.
prep() {  # prep <basename> -> $TMP/<basename>.tl  (timestamped lines only, chronological)
  sudo -n zcat -f -- "$LOGDIR/$1".log "$LOGDIR/$1".log-* 2>/dev/null \
    | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},' \
    | sort > "$TMP/$1.tl"
}
cnt() {  # cnt <pattern> <basename>  ->  "<scoped> / <all-retained>  [since <cutoff>]"
  local pat="$1" b="$2" since all scoped
  since=$(scope_for "$b")
  all=$(grep -c -- "$pat" "$TMP/$b.tl" 2>/dev/null || true)
  all=${all:-0}
  if [ -z "$since" ]; then
    printf '%s / %s  [ALL RETAINED — no scope]' "$all" "$all"
  else
    scoped=$(grep -- "$pat" "$TMP/$b.tl" 2>/dev/null | awk -v s="$since" 'substr($0,1,19) >= s' | wc -l)
    printf '%s / %s  [scoped since %s UTC]' "$scoped" "$all" "$since"
  fi
}
# ⛔ A zero is only a MEASUREMENT if the file was readable. 08-21: a plain `grep` on these
# 0640 root-owned files returns "Permission denied" and `|| echo 0` turns that into a clean
# zero — the same number a perfect day produces. Readability is proven, never assumed.
readable() {
  local hits
  hits=$(wc -l < "$TMP/$1.tl" 2>/dev/null || echo 0)
  if [ "${hits:-0}" -gt 0 ]; then
    echo "readable (${hits} timestamped lines across all rotations)"
  else
    echo "⛔ UNREADABLE OR EMPTY — every 0 below is UNMEASURED, not zero"
  fi
}

prep oms
prep schwab-1m-v2

echo "=================== PHASE: $PHASE   box: $(date '+%Y-%m-%d %H:%M:%S %Z') ==================="
echo "   count scope: '$SCOPE_ARG'   (oms boot $(svc_boot oms) UTC · v2 boot $(svc_boot schwab-1m-v2) UTC)"
echo
echo "### 0. LOG READABILITY (a zero from an unreadable file is not a zero)"
echo "   oms.log          : $(readable oms)"
echo "   schwab-1m-v2.log : $(readable schwab-1m-v2)"

echo
echo "### 1. HEAD + src diff"
echo "   HEAD          : $(git rev-parse --short HEAD)   origin/main: $(git rev-parse --short origin/main 2>/dev/null)"
echo "   src diff      : $(git diff --stat origin/main HEAD -- src | tail -1 || echo none)"
echo "   local changes : $(git status --porcelain | wc -l) file(s)"

echo
echo "### 2. SERVICE-SCOPED SOURCE vs PROCESS-START   ⛔ src diff=0 is NOT evidence"
echo "   Scope: static project import graph from each console entry point."
echo "   Newer startup-required source = STALE; newer conditional/lazy source = COULD_NOT_TELL."
echo "   Unrelated source is ignored. Resolution failure is COULD_NOT_TELL, never FRESH."
printf "   %-18s %-22s %-10s %s\n" SERVICE PROC_START_UTC SUBSTATE "RELEVANT SOURCE"
for u in oms schwab-1m-v2 strategy market-data control reconciler market-capture; do
  unit="project-mai-tai-${u}.service"
  raw=$(systemctl show "$unit" -p ActiveEnterTimestamp --value 2>/dev/null)
  ep=$(date -u -d "$raw" '+%s' 2>/dev/null || echo 0)
  ph=$(date -u -d "@$ep" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
  sub=$(systemctl show "$unit" -p SubState --value 2>/dev/null)
  if [ "$sub" != "running" ]; then
    v="⛔ NOT_RUNNING — source freshness is not health"
  else
    v=$(.venv/bin/python ops/health/service_source_freshness.py \
      --repo "$PWD" --service "$u" --process-start-epoch "$ep" 2>&1)
    rc=$?
    case "$rc" in
      0) ;;
      1) v="⛔ $v" ;;
      *) v="⚠ $v" ;;
    esac
  fi
  printf "   %-18s %-22s %-10s %s\n" "$u" "$ph" "$sub" "$v"
done

echo
echo "### 3. THE TWO OPT-INS — verified FROM THE SINK"
PID=$(systemctl show -p MainPID --value project-mai-tai-schwab-1m-v2.service)
echo "   flag @ /proc/$PID/environ : $(sudo tr '\0' '\n' < /proc/$PID/environ 2>/dev/null | grep WEBULL_RESTING_MIRROR || echo '⛔ ABSENT FROM THE PROCESS')"
COL=$(sudo -u postgres psql -d project_mai_tai -X -tA -c "SELECT count(*) FROM information_schema.columns WHERE table_name='broker_order_events' AND column_name='event_source';")
if [ "$COL" = "1" ]; then COLV="OK"; else COLV="⛔ 0 = STOP, do not restart the OMS"; fi
echo "   event_source column      : ${COL} row(s)   ${COLV}"
echo "   alembic head             : $(sudo -u postgres psql -d project_mai_tai -X -tA -c 'SELECT version_num FROM alembic_version;')"

echo
echo "### 4. §183 — was a broker_order_events write swallowed?"
echo "   [OMS-V2-MIRROR] failures      : $(cnt 'OMS-V2-MIRROR.*fail' oms)"
echo "   failed syncing broker state   : $(cnt 'failed syncing broker state' oms)"
echo "   managed-exit emit failed      : $(cnt 'managed-exit emit failed' oms)"
echo "   UndefinedColumn / no column   : $(cnt 'UndefinedColumn\|has no column' oms)"

echo
echo "### 5. ACCEPTANCE SIGNALS"
# ⛔⭐⭐ SIGNAL 1 AND 2 ARE DB QUERIES, NOT LOG GREPS. Verified 08-20 pre-window: every log
# pattern for the mirror leg — including `rth_resting_mirror` — returns ZERO, while broker_orders
# holds the 720 exactly. A grep here would have printed 0 and read as "rejects -> 0, #735 worked".
# ⛔ The success criterion IS zero, so a broken watch is indistinguishable from a pass.
echo "   1. mirror STOP_LIMIT rejects (broker_orders, live:orb, last 2d):"
sudo -u postgres psql -d project_mai_tai -X -q -c  "SELECT bo.order_type, bo.status, count(*) AS n, max(bo.submitted_at)::date AS last
    FROM broker_orders bo JOIN broker_accounts ba ON ba.id=bo.broker_account_id
   WHERE ba.name='live:orb' AND bo.submitted_at > now() - interval '2 days'
     AND bo.order_type ILIKE '%STOP_LIMIT%'
   GROUP BY 1,2 ORDER BY n DESC;" | sed 's/^/      /'
echo "      baseline: 720 rejected 08-14..08-19  ⇒ expect 0 NEW after the flag"
echo "      ⛔ ZERO MEANS: no mirror leg was REFUSED. It does NOT mean one was placed —"
echo "         an entry-less day produces the same 0. Read it beside signal 2's fill count."
echo "   2. entry fills/day — orb WITH schwab beside it (never differenced):"
# ⛔⭐⭐ §186 — AN ENTRY IS A FILLED **BUY**. NOTHING ELSE (settled 08-21, before reading).
# The previous query filtered `order_type IN ('limit','market')` and did not filter side, so it
# was wrong in BOTH directions: it counted 18 exits as entries over 08-14..08-19 AND dropped 49
# real Schwab entries, because the resting flip-entry is a STOP_LIMIT **buy**. Schwab's rate read
# 11 when it was 56. Verified total over 08-01..08-19: every buy is an entry (none carries an exit
# marker), every sell is an exit (oco_exit, or oms_v2_managed_exit=true).
sudo -u postgres psql -d project_mai_tai -X -q -c  "SELECT ba.name AS account, bo.submitted_at::date AS d, count(*) AS entries
    FROM broker_orders bo JOIN broker_accounts ba ON ba.id=bo.broker_account_id
   WHERE ba.name IN ('live:orb','live:schwab_1m_v2') AND bo.status='filled' AND bo.side='buy'
     AND bo.submitted_at > now() - interval '3 days'
   GROUP BY 1,2 ORDER BY 2 DESC, 1;" | sed 's/^/      /'
echo "      ⛔ The '12-25' band came from the CONTAMINATED count and is NOT a pass/fail until"
echo "         re-derived. orb's '6-7/day' survives as a MEDIAN only (08-14 was 19, an outlier)."
echo "      ⛔ Side by side, never differenced — one Schwab entry can fan out to one orb leg."
echo "   4. duplicate legs per segment — §185, pinned:"
# ⛔ NOT "$(dirname "$0")" — this script is fed to the box over `ssh 'bash -s'`, so $0 is the
#    shell, not a path. The collector already cd'd to the repo, so address it from there.
#    If the file is absent the || branch says UNMEASURED; it must never fall through to 0.
S4=ops/health/signal4_duplicate_legs.sh
if [ -r "$S4" ]; then
  bash "$S4" | sed 's/^/      /'
else
  echo "      ⛔ UNMEASURED — $S4 not present on the box (deploy it). NOT zero duplicates."
fi
echo "   3. WEBULL-BARE-FILL           : $(cnt 'WEBULL-BARE-FILL' oms)   (⛔ >20/day STOP)"
# ⛔⭐⭐ SIGNAL 3'S ZERO NEEDED A DENOMINATOR TOO (08-21). `[WEBULL-BARE-FILL]` shipped in
# #735 (committed 08-19, RUNNING only since the 08-20 20:14:49 deploy) and has never been
# logged: 0 all-time, 0 since boot. That reads as a regression against the sheet's "~9/day"
# and is nothing of the kind — the whole `[WEBULL-PROTECT-*]` family is ALSO 0 since boot
# (61/18/9/2 all-time, newest line 08-20 19:44, i.e. 30 min BEFORE the deploy), and
# `[OMS-V2-MANAGED-OPEN]` since boot is 0. No v2 entry has been booked since the restart, so
# the counter has had zero opportunities. UNEXERCISED, not passing and not failing.
# ⇒ Print the denominator beside it, the same way 6c rescues 6a's zero.
echo "      denominator — v2 entries booked since boot: $(cnt 'OMS-V2-MANAGED-OPEN' oms)"
echo "      sibling watch — WEBULL-PROTECT-FAILED     : $(cnt 'WEBULL-PROTECT-FAILED' oms)"
echo "      ⛔ ZERO MEANS: read it against the denominator on the line above. Denominator 0 ⇒"
echo "         UNEXERCISED (no fill could have been bare). Denominator > 0 with bare-fill 0 ⇒"
echo "         a genuine pass OR a dead counter — and the sibling watch tells you which, since"
echo "         both sit on the SAME branch one line apart (pinned by test_webull_attach_"
echo "         protection.py::the count must sit on the bare branch). ⛔ The '~9/day' figure on"
echo "         the sheet is a FORWARD EXPECTATION for a working mirror, never a retained baseline."
echo "   #736 OCO-TARGET-BELOW-FILL    : $(cnt 'OCO-TARGET-BELOW-FILL' oms)   (⛔ success is ZERO; one line IS the finding)"
echo "      ⛔ ZERO MEANS: UNEXERCISED. This watch has never matched anything, so its zero is"
echo "         consistent-with-success and is not evidence-of-success. Prove it on a known-positive."
echo "   B19 disarm-on-removal         : $(cnt 'reason=watchlist-removed' schwab-1m-v2)"
echo "   B20 arm release               : $(cnt 'V2-ENTRY-WINDOW-ARM-RELEASE' schwab-1m-v2)"

echo
echo "   ── 6. THE SEED-GAP FAMILY — THREE MEANINGS, ONE PREFIX ────────────────────────────"
# ⛔⭐⭐ `[V2-DB-SEED-GAP]` PREFIXES BOTH THE GUARD WORKING AND THE GUARD FAILING (08-21).
# A bare `grep -c 'V2-DB-SEED-GAP'` on 08-21 returned 9 = 7 refusals + 2 census lines + 0
# fail-opens, summing three populations whose zeros mean OPPOSITE things. Split, always.
echo "   6a. REFUSAL — stale history was DROPPED (the guard doing its job)"
echo "       boundary 'dropped ALL'    : $(cnt 'V2-DB-SEED-GAP\].*dropped ALL' schwab-1m-v2)"
echo "       internal 'series SKIPS'   : $(cnt 'V2-DB-SEED-GAP\].*the series SKIPS' schwab-1m-v2)"
echo "       ⛔ ZERO MEANS: AMBIGUOUS ON ITS OWN. Either no stale history was offered (a clean"
echo "          day) or the guard stopped running. Only 6c's DENOMINATOR separates those two."
echo "          A NON-zero here is GOOD NEWS — it is the P0 arm-off-stale-bars defect PREVENTED."
echo "   6b. FAIL-OPEN — the calendar lookup FAILED and seeding proceeded UNGUARDED"
echo "       (both variants; each returns 0 = 'no sessions missed' ⇒ nothing is truncated)"
echo "       'session-calendar lookup failed' : $(cnt 'session-calendar lookup failed' schwab-1m-v2)"
echo "       ⛔ ZERO MEANS: GOOD NEWS — every calendar lookup answered, so every seed was"
echo "          actually judged. This is the OPPOSITE polarity to 6a. A NON-zero here is the"
echo "          guard going INERT: it reverts to the pre-fix behaviour that armed CAST at 7.99"
echo "          while it traded 1.21. ⛔ This zero is only load-bearing if section 0 says"
echo "          READABLE and 6c's evaluations > 0 — no seeding attempted, no lookup to fail."
echo "   6c. CENSUS — the denominator that makes 6a's zero readable (last 3, all rotations):"
# Reads the SORTED stream, so 'last 3' is genuinely the last 3. See the note on prep().
grep 'V2-DB-SEED-GAP-CENSUS' "$TMP/schwab-1m-v2.tl" | tail -3 \
  | sed -E 's/^(.{19}).*(truncations=[0-9]+ of [0-9]+[^—]*).*/       \1  \2/'
echo "       ⛔ 'truncations=0 of 0' is NOT a clean day — it is a census with no population."
echo "          Emitted once per 04:00 session-roll boundary; NO census line at all = the roll"
echo "          never crossed, i.e. UNMEASURED."

echo
echo "### 7. fleet + uptime"
echo "   units running: $(systemctl list-units 'project-mai-tai-*.service' --state=running --no-legend --plain | wc -l)/7"
echo "   uptime       : $(uptime -p)"
echo
echo "=================== END $PHASE ==================="
