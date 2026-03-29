#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Shared output helpers for live-session operator scripts.
source "$SCRIPT_DIR/live_helpers.sh"

HOLD_STRATEGY=0
if [[ "${1:-}" == "--hold-strategy" ]]; then
  HOLD_STRATEGY=1
fi

print_header "Live Market-Data Restart"
cat <<'EOF'
This script uses the safe live-session order:
1. stop strategy so no new trade decisions are emitted during data interruption
2. confirm OMS is quiet
3. restart market data
4. optionally start strategy again so subscriptions rebuild cleanly

This is safer than restarting market data by itself.
EOF

print_dashboard_checks
confirm_step "Stop strategy and begin market-data maintenance? [y/N] "
stop_unit "project-mai-tai-strategy.service"

echo
echo "Quiet-state check required before restarting market data:"
echo "- confirm no new intents are arriving"
echo "- confirm there is no in-flight OMS workflow you are waiting on"
confirm_step "Is OMS quiet and is it safe to restart market data now? [y/N] "

restart_unit "project-mai-tai-market-data.service"

if (( HOLD_STRATEGY )); then
  echo
  echo "Strategy remains stopped because --hold-strategy was used."
else
  confirm_step "Start strategy again now so subscriptions can rebuild? [y/N] "
  start_unit "project-mai-tai-strategy.service"
fi

echo
echo "Post-checks:"
echo "- Verify /api/scanner returns live rows again."
echo "- Verify /api/bots watchlists repopulate."
echo "- Verify subscriptions rebuild after strategy comes back."
echo "- Verify /api/reconciliation remains clean."
print_dashboard_checks
print_log_hint
