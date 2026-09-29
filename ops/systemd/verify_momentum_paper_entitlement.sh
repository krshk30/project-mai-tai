#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
TIMEOUT_SECONDS="${2:-15}"
GATEWAY_UNIT="project-mai-tai-market-data.service"
GATEWAY_LOG="${MAI_TAI_MARKET_DATA_LOG:-/var/log/project-mai-tai/market-data.log}"
OVERVIEW_URL="${MAI_TAI_OVERVIEW_URL:-http://127.0.0.1:8100/api/overview}"
WAIT_SECONDS="${MAI_TAI_MOMENTUM_PROOF_WAIT_SECONDS:-30}"

gateway_overview() {
  curl -fsS "$OVERVIEW_URL" | "$REPO_DIR/.venv/bin/python" -c '
import json, sys
data = json.load(sys.stdin)
row = next((r for r in data.get("services", []) if r.get("service_name") == "market-data-gateway"), None)
if row is None:
    raise SystemExit("market-data-gateway absent from overview")
print(f"{row.get('"'"'status'"'"', '')}|{row.get('"'"'observed_at'"'"', '')}")
'
}

before_pid="$(sudo systemctl show "$GATEWAY_UNIT" --property MainPID --value)"
before_restarts="$(sudo systemctl show "$GATEWAY_UNIT" --property NRestarts --value)"
before_overview="$(gateway_overview)"
before_log_lines="$(sudo wc -l < "$GATEWAY_LOG")"

sudo systemd-run --quiet --uid=trader --pipe --wait --collect \
  --property="WorkingDirectory=$REPO_DIR" \
  --property="EnvironmentFile=/etc/project-mai-tai/project-mai-tai.env" \
  "$REPO_DIR/.venv/bin/mai-tai-momentum-paper" \
  --entitlement-check --timeout-seconds "$TIMEOUT_SECONDS"

sleep "$WAIT_SECONDS"

after_pid="$(sudo systemctl show "$GATEWAY_UNIT" --property MainPID --value)"
after_restarts="$(sudo systemctl show "$GATEWAY_UNIT" --property NRestarts --value)"
after_overview="$(gateway_overview)"

if [[ ! "$before_pid" =~ ^[1-9][0-9]*$ ]] || [[ "$before_pid" != "$after_pid" ]]; then
  echo "MOMENTUM CONNECTION-CAP PROOF: FAIL gateway pid $before_pid -> $after_pid"
  exit 1
fi
if [[ "$before_restarts" != "$after_restarts" ]]; then
  echo "MOMENTUM CONNECTION-CAP PROOF: FAIL gateway restarts $before_restarts -> $after_restarts"
  exit 1
fi
if [[ "$after_overview" != healthy* ]] || [[ "$before_overview" == "$after_overview" ]]; then
  echo "MOMENTUM CONNECTION-CAP PROOF: FAIL gateway heartbeat did not advance healthy"
  echo "before=$before_overview after=$after_overview"
  exit 1
fi
if sudo tail -n "+$((before_log_lines + 1))" "$GATEWAY_LOG" \
  | grep -Eiq 'Massive websocket error|policy violation|reconnecting'; then
  echo "MOMENTUM CONNECTION-CAP PROOF: FAIL gateway logged a stream disruption"
  exit 1
fi

echo "MOMENTUM CONNECTION-CAP PROOF: PASS gateway pid=$after_pid restarts=$after_restarts heartbeat=$after_overview"
