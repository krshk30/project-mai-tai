#!/usr/bin/env bash
# Deploy-evidence collector — 2026-08-20 window. Read-only.
# Emits exactly the values the handoff skeleton has FILL slots for.
# Usage: ssh mai-tai-vps 'bash -s' -- PRE|POST < collect_deploy_evidence.sh
PHASE="${1:-POST}"
LOGDIR=/var/log/project-mai-tai
cd /home/trader/project-mai-tai || exit 1

# ⛔ Counts EVERY rotation including .gz. Plain grep silently skips compressed days,
# which is a truncated answer wearing a confident number.
cnt() {  # cnt <pattern> <basename>
  local n
  n=$(sudo zgrep -hc -- "$1" "$LOGDIR/$2".log "$LOGDIR/$2".log-* 2>/dev/null | paste -sd+ | bc 2>/dev/null)
  echo "${n:-0}"
}
# ⛔ A zero is only a MEASUREMENT if the file was readable. Proves readability with a
# control pattern known to be present, and says UNREADABLE otherwise.
readable() {
  local hits
  hits=$(sudo grep -hc "" "$LOGDIR/$1.log" 2>/dev/null || echo 0)
  if [ "${hits:-0}" -gt 0 ]; then echo "readable (${hits} lines)"; else echo "⛔ UNREADABLE OR EMPTY — every 0 below is UNMEASURED"; fi
}

echo "=================== PHASE: $PHASE   box: $(date '+%Y-%m-%d %H:%M:%S %Z') ==================="
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
echo "### 2. FILE-WRITE vs PROCESS-START   ⛔ src diff=0 is NOT evidence"
PULL=$(find src -type f -name '*.py' -printf '%T@\n' | sort -rn | head -1)
PULL_H=$(date -u -d "@${PULL%.*}" '+%Y-%m-%d %H:%M:%S')
echo "   files written (newest .py in src, i.e. the pull): ${PULL_H} UTC"
printf "   %-18s %-22s %-10s %s\n" SERVICE PROC_START_UTC SUBSTATE "RUNNING PULLED CODE?"
for u in oms schwab-1m-v2 strategy market-data control reconciler market-capture; do
  unit="project-mai-tai-${u}.service"
  raw=$(systemctl show "$unit" -p ActiveEnterTimestamp --value 2>/dev/null)
  ep=$(date -u -d "$raw" '+%s' 2>/dev/null || echo 0)
  ph=$(date -u -d "@$ep" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
  sub=$(systemctl show "$unit" -p SubState --value 2>/dev/null)
  if [ "$ep" -gt "${PULL%.*}" ]; then v="YES"; else v="⛔ NO — on disk, not running"; fi
  printf "   %-18s %-22s %-10s %s\n" "$u" "$ph" "$sub" "$v"
done

echo
echo "### 3. THE TWO OPT-INS — verified FROM THE SINK"
PID=$(systemctl show -p MainPID --value project-mai-tai-schwab-1m-v2.service)
echo "   flag @ /proc/$PID/environ : $(sudo tr '\0' '\n' < /proc/$PID/environ 2>/dev/null | grep WEBULL_RESTING_MIRROR || echo '⛔ ABSENT FROM THE PROCESS')"
COL=$(sudo -u postgres psql -d project_mai_tai -X -tA -c "SELECT count(*) FROM information_schema.columns WHERE table_name='broker_order_events' AND column_name='event_source';")
echo "   event_source column      : ${COL} row(s)   $( [ "$COL" = "1" ] && echo OK || echo '⛔ 0 = STOP, do not restart the OMS' )"
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
echo "   2. entry fills/day — orb WITH schwab beside it (never differenced):"
sudo -u postgres psql -d project_mai_tai -X -q -c  "SELECT ba.name AS account, bo.submitted_at::date AS d, count(*) AS fills
    FROM broker_orders bo JOIN broker_accounts ba ON ba.id=bo.broker_account_id
   WHERE ba.name IN ('live:orb','live:schwab_1m_v2') AND bo.status='filled'
     AND bo.order_type IN ('limit','market') AND bo.submitted_at > now() - interval '3 days'
   GROUP BY 1,2 ORDER BY 2 DESC, 1;" | sed 's/^/      /'
echo "      baseline: orb 6-7/day  ⇒ expect 12-25"
echo "   4. duplicate legs per segment : ⛔ QUERY NOT PINNED — report as UNMEASURED, never as 0"
echo "      (baseline 19-of-119 came from the §82 work; needs the segment join before it is a number)"
echo "   3. WEBULL-BARE-FILL           : $(cnt 'WEBULL-BARE-FILL' oms)   (expect ~9/day; ⛔ >20 STOP)"
echo "   #736 OCO-TARGET-BELOW-FILL    : $(cnt 'OCO-TARGET-BELOW-FILL' oms)   (⛔ success is ZERO; one line IS the finding)"
echo "   6. seed-gap fail-open         : $(cnt 'session-calendar lookup failed' schwab-1m-v2)   (expect 0)"
echo "   B19 disarm-on-removal         : $(cnt 'reason=watchlist-removed' schwab-1m-v2)"
echo "   B20 arm release               : $(cnt 'V2-ENTRY-WINDOW-ARM-RELEASE' schwab-1m-v2)"
echo "   5. census (last 3):"
sudo zgrep -h 'V2-DB-SEED-GAP-CENSUS' $LOGDIR/schwab-1m-v2.log $LOGDIR/schwab-1m-v2.log-* 2>/dev/null \
  | tail -3 | sed -E 's/.*(truncations=[0-9]+ of [0-9]+[^—]*).*/      \1/'

echo
echo "### 6. fleet + uptime"
echo "   units running: $(systemctl list-units 'project-mai-tai-*.service' --state=running --no-legend --plain | wc -l)/7"
echo "   uptime       : $(uptime -p)"
echo
echo "=================== END $PHASE ==================="
