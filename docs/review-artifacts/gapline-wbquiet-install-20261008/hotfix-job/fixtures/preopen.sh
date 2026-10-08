#!/usr/bin/env bash
# Fail-closed pre-open gate for 2026-10-06.
set -uo pipefail

REPO=/home/trader/project-mai-tai
UNIT=project-mai-tai-schwab-1m-v2.service
OMS_UNIT=project-mai-tai-oms.service
STRATEGY_UNIT=project-mai-tai-strategy.service
ORB_UNIT=project-mai-tai-orb.service
ORB_SCHWAB_UNIT=project-mai-tai-orb-schwab.service
MARKET_DATA_UNIT=project-mai-tai-market-data.service
PAPER_UNIT=project-mai-tai-momentum-paper.service
CONTROL_UNIT=project-mai-tai-control.service
EXPECTED_DATE="$(TZ=America/New_York date +%F)"
EXPECTED_SHA=d244f602491e5d316a05670b205315466aed2a4d
EXPECTED_PID=1412228
EXPECTED_START='Wed 2026-10-07 22:17:35 UTC'
EXPECTED_OMS_PID=1408231
EXPECTED_OMS_START='Wed 2026-10-07 22:12:50 UTC'
EXPECTED_STRATEGY_PID=1408242
EXPECTED_STRATEGY_START='Wed 2026-10-07 22:12:50 UTC'
EXPECTED_ORB_PID=1322003
EXPECTED_ORB_START='Wed 2026-10-07 20:14:37 UTC'
EXPECTED_ORB_SCHWAB_PID=765206
EXPECTED_ORB_SCHWAB_START='Wed 2026-10-07 06:31:14 UTC'
EXPECTED_MARKET_DATA_PID=2907
EXPECTED_MARKET_DATA_START='Sat 2026-10-03 22:03:17 UTC'
EXPECTED_PAPER_PID=1749554
EXPECTED_PAPER_START='Thu 2026-10-08 07:40:03 UTC'
EXPECTED_CONTROL_PID=1464286
EXPECTED_CONTROL_START='Wed 2026-10-07 23:43:00 UTC'
# Original Install1 snapshot bytes retained; DERIVED two-install action union, not a fresh baseline.
SNAPSHOT=/home/trader/after-hours/2026-10-06/install2-5b8b4f64/job-archive64/attempt-install2-oct6-attended/install1-original-before-restart.json
INSTALL_RECORD=/home/trader/after-hours/2026-10-06/install2-5b8b4f64/job-archive64/attempt-install2-oct6-attended/cumulative-install-record.json
REPORT="/home/trader/known_defect_regression_watch/v2-restart-evidence-${EXPECTED_DATE//-/}.md"
STATUS=/home/trader/known_defect_regression_watch/STATUS.txt
CRON=/etc/cron.d/project-mai-tai-known-defect-regression-watch
SCHEDULE='*/5 * * * * root /home/trader/project-mai-tai/ops/health/known_defect_regression_watch_cron.sh >> /var/log/project-mai-tai/known-defect-regression-watch.log 2>&1'

failures=0
unknowns=0
fail() {
  printf 'FAIL: %s\n' "$*"
  failures=$((failures + 1))
}

pass() {
  printf 'PASS: %s\n' "$*"
}

source /home/trader/restart_evidence/preopen_restart_evidence.sh

printf '=== DAILY PRE-OPEN GATE ===\n'
printf 'UTC %s   ET %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" \
  "$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S %Z')"

et_date="$(TZ=America/New_York date '+%Y-%m-%d')"
et_hhmm="$(TZ=America/New_York date '+%H%M')"
if [[ "$et_date" != "$EXPECTED_DATE" ]]; then
  fail "this one-day gate is for $EXPECTED_DATE ET, not $et_date"
elif (( 10#$et_hhmm >= 700 )); then
  fail "pre-open gate ran at $et_hhmm ET; it must pass before 07:00"
else
  pass "run window is $et_date $et_hhmm ET, before 07:00"
fi

printf '\n=== CHECKOUT + PROCESS IDENTITY ===\n'
if ! cd "$REPO"; then
  fail "cannot enter $REPO"
else
  head="$(git rev-parse HEAD 2>/dev/null || true)"
  dirty="$(git status --porcelain 2>/dev/null || printf 'unreadable\n')"
  [[ "$head" == "$EXPECTED_SHA" ]] && pass "checkout SHA=$head" \
    || fail "checkout SHA=${head:-unreadable}, expected $EXPECTED_SHA"
  [[ -z "$dirty" ]] && pass "checkout tree is clean" \
    || fail "checkout tree is dirty or unreadable: ${dirty//$'\n'/,}"
fi

check_identity() {
  local label="$1" unit="$2" expected_pid="$3" expected_start="$4"
  local pid restarts active sub started
  pid="$(systemctl show "$unit" -p MainPID --value 2>/dev/null || true)"
  restarts="$(systemctl show "$unit" -p NRestarts --value 2>/dev/null || true)"
  active="$(systemctl show "$unit" -p ActiveState --value 2>/dev/null || true)"
  sub="$(systemctl show "$unit" -p SubState --value 2>/dev/null || true)"
  started="$(systemctl show "$unit" -p ExecMainStartTimestamp --value 2>/dev/null || true)"
  if [[ "$pid" == "$expected_pid" && "$restarts" == 0 && "$active" == active \
        && "$sub" == running && "$started" == "$expected_start" ]]; then
    pass "$label identity pid=$pid NRestarts=$restarts active/running started=$started"
  else
    fail "$label identity changed: pid=${pid:-?} NRestarts=${restarts:-?} state=${active:-?}/${sub:-?} started=${started:-?}"
  fi
}
check_identity v2 "$UNIT" "$EXPECTED_PID" "$EXPECTED_START"
check_identity oms "$OMS_UNIT" "$EXPECTED_OMS_PID" "$EXPECTED_OMS_START"
check_identity strategy "$STRATEGY_UNIT" "$EXPECTED_STRATEGY_PID" "$EXPECTED_STRATEGY_START"
check_identity orb "$ORB_UNIT" "$EXPECTED_ORB_PID" "$EXPECTED_ORB_START"
if "$REPO/.venv/bin/python" /home/trader/preopen-daily/upgrade_ack.py; then
  pass "orb-schwab exact Redis-upgrade restart ACKNOWLEDGED; actual NRestarts=1"
else
  fail "orb-schwab upgrade acknowledgement mismatch"
fi
check_identity market-data "$MARKET_DATA_UNIT" "$EXPECTED_MARKET_DATA_PID" "$EXPECTED_MARKET_DATA_START"
if "$REPO/.venv/bin/python" /home/trader/preopen-daily/daily.py paper; then
  pass "paper active/NRestarts0 and guard-linked today's03:40 admission"
else
  fail "paper/guard admission unreadable or failed; no identity exemption"
fi
check_identity control "$CONTROL_UNIT" "$EXPECTED_CONTROL_PID" "$EXPECTED_CONTROL_START"

printf '\n=== INSTALLED WATCH ===\n'
source_hash="$(sha256sum "$REPO/ops/health/known_defect_regression_watch.cron" 2>/dev/null | awk '{print $1}')"
target_hash="$(sudo -n sha256sum "$CRON" 2>/dev/null | awk '{print $1}')"
schedule_count="$(sudo -n grep -Fxc "$SCHEDULE" "$CRON" 2>/dev/null || true)"
cron_stat="$(sudo -n stat -c '%U:%G %a' "$CRON" 2>/dev/null || true)"
if [[ -n "$source_hash" && "$source_hash" == "$target_hash" \
      && "$schedule_count" == 1 && "$cron_stat" == 'root:root 644' ]]; then
  pass "one reviewed schedule installed, root:root 0644, sha256=$target_hash"
else
  fail "watch install mismatch: source=$source_hash target=$target_hash schedules=${schedule_count:-?} stat=${cron_stat:-?}"
fi

printf '\n=== DURABLE V2 RESTART EVIDENCE ===\n'
evidence_output="$(sudo -n "$REPO/.venv/bin/python" /home/trader/preopen-daily/restart_report.py report \
  --snapshot "$SNAPSHOT" \
  --install-record "$INSTALL_RECORD" \
  --restarted oms \
  --restarted schwab-1m-v2 \
  --restarted strategy \
  --restarted orb-schwab \
  --restarted control \
  --expect-flag 'oms:MAI_TAI_OMS_V2_EOD_OCO_TRANSITION_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_OMS_V2_EH_ENTRY_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_STRATEGY_SCHWAB_1M_V2_DUAL_BROKER_FANOUT_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_OMS_V2_WEBULL_LATE_CLOSE_GUARD_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_OMS_V2_WEBULL_MIRROR_DEFERRED_RESUBMIT_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_WINDOW_END_HOUR_ET=15' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_WINDOW_END_MINUTE_ET=45' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_TICK_CAPTURE_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_V2_EH_RESTING_ENTRY_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_V2_RESTING_TRIGGER_OFFSET_PCT=0.5' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_GAP_HOLD_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_MASSIVE_SEED_ENABLED=false' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_RETRY_ONE_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_FLIP_OWNED_FIRST_ENTRY_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_CONFIRMATION_ACCOUNT_NEUTRAL_DISCOVERY_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_V2_RECLAIM_ENABLED=false' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_FLIP_PROBE_SYMBOLS=*' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_MACD_PROBE_SYMBOLS=*' \
  --expect-flag 'oms:MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_PRINT_ASK_CONFIRM_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_FLIP_WAIT_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_REST_REPRICE_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_REPRICE_HANDOFF_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_PRINT_ASK_CONFIRM_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_FLIP_WAIT_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_PM_REST_REPRICE_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_REPRICE_HANDOFF_ENABLED=true' \
  --no-schema-change \
  --expect-flag 'oms:MAI_TAI_OMS_V2_WEBULL_MIRROR_FRESH_PRICE_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_OMS_V2_WEBULL_MIRROR_RETAINED_HOLD_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_ORB_SCHWAB_ATR_ENTRY_GATE_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_STRATEGY_SCHWAB_1M_V2_GAP_HOLD_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_STRATEGY_SCHWAB_1M_V2_KEEP_REST_AFTER_BUY_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_STRATEGY_SCHWAB_1M_V2_LINE_CHART_RESTORATION_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_STRATEGY_SCHWAB_1M_V2_REMOVED_WAIT_CLEAR_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_STRATEGY_SCHWAB_1M_V2_RESTING_BUY_ROUND_UP_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_STRATEGY_SCHWAB_1M_V2_RETRY_ONE_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_WEBULL_LIST_PRIMARY_READS_ENABLED=true' \
  --expect-flag 'orb-schwab:MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED=true' \
  --expect-flag 'orb-schwab:MAI_TAI_ORB_SCHWAB_ATR_ENTRY_GATE_ENABLED=true' \
  --expect-flag 'orb-schwab:MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED=false' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_OMS_V2_WEBULL_MIRROR_FRESH_PRICE_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_OMS_V2_WEBULL_MIRROR_RETAINED_HOLD_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_KEEP_REST_AFTER_BUY_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_LINE_CHART_RESTORATION_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_REMOVED_WAIT_CLEAR_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_RESTING_BUY_ROUND_UP_ENABLED=true' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_WEBULL_LIST_PRIMARY_READS_ENABLED=true' \
  --expect-flag 'oms:MAI_TAI_STRATEGY_SCHWAB_1M_V2_RETRY_ONE_MAX_RETRIES=0' \
  --expect-flag 'schwab-1m-v2:MAI_TAI_STRATEGY_SCHWAB_1M_V2_RETRY_ONE_MAX_RETRIES=0' \
  --expected-alembic-head 20261005_0022 \
  --output "$REPORT" 2>&1)"
evidence_rc=$?
printf '%s\n' "$evidence_output"
if [[ "$evidence_rc" == 0 && "$evidence_output" == *"Final call: ACCEPTED_OPEN_LINESRC1;"* ]]; then
  accepted_open_linesrc1=1
  printf "ACCEPTED_OPEN_LINESRC1: inspect exact counted after16 history dispositions in %s\n" "$REPORT"
else
  preopen_record_restart_evidence "$evidence_rc" "$REPORT" "$evidence_output"
fi
if ! "$REPO/.venv/bin/python" /home/trader/preopen-daily/daily.py report-date "$REPORT"; then
  fail "restart report missing or its generated ET date differs from today's gate"
fi
preopen_check_expected_flags "$REPO/.venv/bin/python" \
  /home/trader/restart_evidence/expected_flags_check.py \
  /home/trader/restart_evidence/expected_flags.json

printf '\n=== REGRESSION WATCH STATUS ===\n'
sudo -n python3 - "$STATUS" <<'PY'
from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

path = Path(sys.argv[1])
try:
    lines = path.read_text(encoding="utf-8").splitlines()
except OSError as exc:
    print(f"FAIL: cannot read {path}: {exc}")
    raise SystemExit(1)

header = re.fullmatch(
    r"KNOWN DEFECT REGRESSION WATCH at=([^ ]+) rows=(\d+)", lines[0] if lines else ""
)
if header is None:
    print("FAIL: malformed or missing watch header")
    raise SystemExit(1)

try:
    generated = datetime.fromisoformat(header.group(1)).astimezone(UTC)
except ValueError:
    print(f"FAIL: unparseable watch timestamp {header.group(1)!r}")
    raise SystemExit(1)

age = (datetime.now(UTC) - generated).total_seconds()
row_re = re.compile(
    r"^\[(OBSERVED_CLEAN|GUARD_WORKING|RECURRENCE|COULD_NOT_TELL|UNEXERCISED|UNARMED|DELEGATED)\] ([A-Z0-9]+) "
)
rows = [match.groups() for line in lines if (match := row_re.match(line))]
keys = [key for _, key in rows]
expected = {
    "BOOT1", "ROLL1", "OWNERROLL1", "SLOTCLEAR1", "LIQPULL1", "DISARM1",
    "ORPHAN1", "CAP1", "PHANTOM1", "REDIS1", "BARGAP1", "SAW1", "CHURN1",
    "RESERVE1", "LATECLOSE1", "EXITDONE1", "W4291", "CEILING1", "FALSEFLAT1",
    "VPZERO1", "SEED1",
}
counts = Counter(status for status, _ in rows)
errors: list[str] = []
if not 0 <= age <= 420:
    errors.append(f"status age is {age:.0f}s, expected no more than 420s for a 5-minute schedule")
if int(header.group(2)) != 21 or len(rows) != 21 or set(keys) != expected or len(keys) != len(set(keys)):
    errors.append(
        f"catalog mismatch header={header.group(2)} parsed={len(rows)} unique={len(set(keys))}"
    )
if counts["RECURRENCE"]:
    errors.append(f"RECURRENCE rows={counts['RECURRENCE']}")
if counts["COULD_NOT_TELL"]:
    errors.append(f"COULD_NOT_TELL rows={counts['COULD_NOT_TELL']}")

print(
    "watch generated=" + generated.isoformat()
    + f" age_seconds={age:.0f} rows={len(rows)} statuses="
    + ",".join(f"{key}:{counts[key]}" for key in sorted(counts))
)
for key in ("SLOTCLEAR1", "LIQPULL1"):
    print(next((line for line in lines if re.match(rf"^\[[^]]+\] {key} ", line)), f"MISSING {key}"))
if errors:
    for error in errors:
        print(f"FAIL: {error}")
    raise SystemExit(1)
print("PASS: watch status is fresh, complete, and has no recurrence or blind reading")
PY
watch_rc=$?
if (( watch_rc != 0 )); then
  failures=$((failures + 1))
fi

printf '\n=== VERDICT ===\n'
if (( failures == 0 && unknowns == 0 && ${accepted_open_linesrc1:-0} == 1 )); then
  printf "ACCEPTED_OPEN_LINESRC1: other preopen checks measured; not zero-error/PASS.\n"
  exit 0
fi
preopen_final_verdict
exit $?
