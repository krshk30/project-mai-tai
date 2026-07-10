#!/usr/bin/env bash
# Dual-broker v2 qty-1 Webull plumbing harness — scheduled runner.
#
# Runs scripts/v2_webull_qty1_harness.py (--confirm, REAL qty-1) on the live:orb Webull account
# and pushes the verdict to ntfy. Invoked by a systemd oneshot unit so it inherits the SAME
# EnvironmentFile (/etc/project-mai-tai/project-mai-tai.env) the OMS uses — massive key + Webull
# creds present, run as `trader`.
#
#   Usage: run_v2_webull_harness.sh <AM|RTH> [SYMBOL]
#     AM  -> extended-hours: marketable LIMIT entry/flat + live-priced --auto-price
#     RTH -> regular hours:  MARKET entry/flat
#
# The harness itself ALWAYS cancels resting orders + flattens any held share + verifies FLAT in a
# finally block; this wrapper only schedules + reports. Tiny (qty 1), but LIVE money.
set -uo pipefail

SESSION="${1:-RTH}"
SYMBOL="${2:-F}"
ACCOUNT="live:orb"
REPO="/home/trader/project-mai-tai"
PY="$REPO/.venv/bin/python"
NTFY_URL="https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
LOGDIR="/home/trader/harness_out"
mkdir -p "$LOGDIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOGDIR/webull_harness_${SESSION}_${STAMP}.log"

ARGS=(--account "$ACCOUNT" --symbol "$SYMBOL" --confirm --session "$SESSION")
[ "$SESSION" = "AM" ] || [ "$SESSION" = "PM" ] && ARGS+=(--auto-price)

cd "$REPO" || exit 3
"$PY" scripts/v2_webull_qty1_harness.py "${ARGS[@]}" >"$LOG" 2>&1
RC=$?

# Pull the signal lines for the push body (verdict + submit statuses + fill).
VERDICT="$(grep -oE 'HARNESS (PASS|FAIL[^$]*)' "$LOG" | tail -1)"
[ -z "$VERDICT" ] && VERDICT="NO VERDICT (rc=$RC — likely crashed before finally)"
BODY="$(grep -hE 'ENTRY|HARD-STOP|SCALE|FLATTEN|fill:|auto-price|NOT FLAT|rejected|HARNESS' "$LOG" | tail -12)"

case "$VERDICT" in
  "HARNESS PASS") TITLE="✅ v2 Webull harness PASS ($SESSION $SYMBOL)"; PRIO="default"; TAGS="white_check_mark" ;;
  *)             TITLE="🔴 v2 Webull harness $VERDICT ($SESSION $SYMBOL)"; PRIO="urgent"; TAGS="rotating_light" ;;
esac

curl -s -H "Title: $TITLE" -H "Priority: $PRIO" -H "Tags: $TAGS" \
  -d "${BODY:-no output} — full: ssh mai-tai-vps 'cat $LOG'" "$NTFY_URL" >/dev/null 2>&1
exit "$RC"
