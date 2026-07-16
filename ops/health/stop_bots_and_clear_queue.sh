#!/usr/bin/env bash
# stop_bots_and_clear_queue.sh — the 10:01 STOP procedure (2026-07-16 plan).
# LIVE-MONEY, DESTRUCTIVE. Run ATTENDED.
#
# HARD GATE: does NOTHING until assert_fleet_flat.sh exits 0. If anything is held it
#   ABORTS and stops nothing — a held position is a conscious decision, not a script's.
# ⛔ NEVER touches the OMS (project-mai-tai-oms). The OMS owns the exits; it must stay up.
#   (Stopping v2 while it holds is survivable — the OMS keeps managing the exit — but this
#    script refuses on held anyway, per the plan's "refuse unless the assert passes first".)
#
# Sequence: assert bot-flat → stop ORB + v2 → verify OMS still up → verify bots inactive
#   (armed segments die with the process) → clear the intent queue (safe: nothing is adding now).
#
# Env:  MAI_TAI_DB_URL (DSN, or DATABASE_URL) · MAI_TAI_REDIS_URL (or REDIS_URL; default local)
# Exit: 0 = bots down + queue cleared (deploy window open) · 1 = not flat, aborted · 2 = fail-closed
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ASSERT="$HERE/assert_fleet_flat.sh"
DSN="${MAI_TAI_DB_URL:-${DATABASE_URL:-}}"
REDIS="${MAI_TAI_REDIS_URL:-${REDIS_URL:-redis://127.0.0.1:6379}}"
STREAM="mai_tai:strategy-intents"
ORB_UNIT="project-mai-tai-orb"
V2_UNIT="project-mai-tai-schwab-1m-v2"
OMS_UNIT="project-mai-tai-oms"   # ⛔ NEVER stopped here

[ -x "$ASSERT" ] || { echo "FATAL: $ASSERT missing/not executable — FAIL CLOSED"; exit 2; }
[ -n "$DSN" ]    || { echo "FATAL: no DSN (set MAI_TAI_DB_URL) — FAIL CLOSED"; exit 2; }

# --- 1. HARD GATE ---------------------------------------------------------------
echo "== [1/4] ASSERT bot-flat (hard gate) =="
if ! "$ASSERT" "$DSN"; then
  rc=$?
  echo ""
  echo "⛔ ABORT — fleet is NOT confirmed flat (assert exit $rc). Nothing was stopped."
  echo "   Options (your call, not the script's):"
  echo "     • wait for the OMS to exit the held position, then re-run; OR"
  echo "     • stop ONLY v2 by hand if you accept the OMS managing the exit (survivable — it saved ASTN)."
  echo "   ⛔ Do NOT stop the OMS while anything is held."
  exit 1
fi

# --- 2. stop the two BOTS (never the OMS) ---------------------------------------
echo ""
echo "== [2/4] stopping bots (ORB, v2) — OMS left running =="
for u in "$ORB_UNIT" "$V2_UNIT"; do
  echo "  sudo systemctl stop $u"
  sudo systemctl stop "$u" || { echo "FATAL: failed to stop $u — FAIL CLOSED"; exit 2; }
done
# guardrail: the OMS must NOT have been touched
if ! systemctl is-active --quiet "$OMS_UNIT"; then
  echo "⛔⛔ FATAL: $OMS_UNIT is not active — the OMS must stay up to own exits. Investigate NOW."
  exit 2
fi
echo "  ✅ $OMS_UNIT still active (untouched)"

# --- 3. confirm bots truly inactive = no armed segments -------------------------
echo ""
echo "== [3/4] confirming bots inactive (armed segments die with the process) =="
for u in "$ORB_UNIT" "$V2_UNIT"; do
  st="$(systemctl is-active "$u" || true)"
  [ "$st" != "active" ] || { echo "⛔ FATAL: $u still active — armed segments may persist. Do NOT deploy."; exit 2; }
  echo "  ✅ $u = $st"
done

# --- 4. clear the intent queue (only now: bots down, nothing is adding) ---------
echo ""
echo "== [4/4] clearing the intent queue ($STREAM) =="
rc() { redis-cli -u "$REDIS" "$@"; }
before="$(rc XLEN "$STREAM" 2>/dev/null)" || { echo "FATAL: redis XLEN failed — FAIL CLOSED"; exit 2; }
echo "  XLEN before = ${before:-?}"
rc XTRIM "$STREAM" MAXLEN 0 >/dev/null 2>&1 || { echo "FATAL: redis XTRIM failed — FAIL CLOSED"; exit 2; }
after="$(rc XLEN "$STREAM" 2>/dev/null)" || { echo "FATAL: redis XLEN(after) failed — FAIL CLOSED"; exit 2; }
echo "  XLEN after  = ${after:-?}"
[ "${after:-1}" = "0" ] || echo "  ⚠ WARN: queue not empty after trim (=$after) — investigate before deploy."

echo ""
echo "----"
echo "✅✅ DONE: ORB + v2 stopped & inactive · OMS still up · intent queue cleared."
echo "   No armed segments exist → the cap reset can't fire → the DEPLOY WINDOW IS OPEN."
echo "   ⚠ The v2 watchdog ($V2_UNIT-watchdog) will now report stalled_rth — EXPECTED while down (stop it if the RED noise bothers you; restart it with v2)."
echo "   ⛔ Do NOT restart v2 until P1.3 + P1.4 land."
exit 0
