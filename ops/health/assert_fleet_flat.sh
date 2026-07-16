#!/usr/bin/env bash
# assert_fleet_flat.sh — FAIL-CLOSED bot-flat pre-flight for the 10:01 stop (2026-07-16 plan).
#
# WHY THIS EXISTS: yesterday's pre-flight PRINTED `virtual_positions=0 / oms_managed open=1`
# and the first number got read. This ASSERTS: it exits NON-ZERO on any open row, and
# exits non-zero on ANY doubt (query error, unreachable DB, non-numeric). `set -e` cannot
# catch a check you never assert on — so every check is an explicit assertion here.
#
# Scope = the BOT ledgers only (per the plan: "both virtual_positions and oms_managed_positions").
# It deliberately does NOT read account_positions (broker truth), because that includes the
# operator's MANUAL holdings (CYN, FCUV, CELZ ...) and would false-positive. The scoping
# invariant: the bot manages only what is in these two tables.
#
# Usage:  assert_fleet_flat.sh "<DSN>"      (or set MAI_TAI_DB_URL / DATABASE_URL)
# Exit:   0 = FLAT (safe to stop bots) · 1 = HELD (do NOT stop the OMS) · 2 = CANNOT VERIFY (fail closed)
set -uo pipefail

DSN="${1:-${MAI_TAI_DATABASE_URL:-${MAI_TAI_DB_URL:-${DATABASE_URL:-}}}}"
[ -n "$DSN" ] || { echo "FATAL: no DSN (pass as arg 1 or set MAI_TAI_DATABASE_URL) — FAIL CLOSED"; exit 2; }
# psql cannot parse SQLAlchemy driver schemes (postgresql+psycopg://) — normalize to a libpq URL.
PSQL_DSN="$(printf '%s' "$DSN" | sed -E 's#^postgres(ql)?\+[a-z0-9_]+://#postgresql://#')"

fail=0

assert_zero() {  # assert_zero "<label>" "<count-sql>" "<rows-sql>"
  local label="$1" cntsql="$2" rowsql="$3" n
  n=$(psql "$PSQL_DSN" -tAc "$cntsql" 2>/dev/null) \
    || { echo "FATAL: count query failed for [$label] — FAIL CLOSED"; exit 2; }
  case "$n" in ''|*[!0-9]*) echo "FATAL: non-numeric count for [$label]='$n' — FAIL CLOSED"; exit 2;; esac
  if [ "$n" -ne 0 ]; then
    echo "❌ HELD: [$label] = $n open row(s):"
    psql "$PSQL_DSN" -P pager=off -c "$rowsql" 2>/dev/null || echo "   (row detail unavailable — count is authoritative)"
    fail=1
  else
    echo "✅ FLAT: [$label]"
  fi
}

# 1) OMS-managed (v2 exits). Held = not closed AND qty != 0.
assert_zero "oms_managed_positions" \
  "SELECT count(*) FROM oms_managed_positions WHERE status <> 'closed' AND current_quantity <> 0;" \
  "SELECT strategy_code, broker_account_name, symbol, current_quantity, status FROM oms_managed_positions WHERE status <> 'closed' AND current_quantity <> 0;"

# 2) Virtual-position ledger (BOTH bot accounts: ORB + v2). Held = qty != 0.
assert_zero "virtual_positions" \
  "SELECT count(*) FROM virtual_positions WHERE quantity <> 0;" \
  "SELECT ba.name AS account, vp.symbol, vp.quantity FROM virtual_positions vp JOIN broker_accounts ba ON ba.id = vp.broker_account_id WHERE vp.quantity <> 0;"

echo "----"
if [ "$fail" -ne 0 ]; then
  echo "⛔ NOT FLAT — do NOT stop the OMS (it owns the exits). Close the held position(s) first."
  echo "   Stopping v2 while it holds is survivable; stopping the OMS while anything is held is NOT."
  exit 1
fi
echo "✅✅ BOT-FLAT confirmed on both ledgers — safe to stop the bots."
exit 0
