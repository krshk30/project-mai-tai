#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Shared output helpers for live-session operator scripts.
source "$SCRIPT_DIR/live_helpers.sh"

HOLD_STRATEGY=0
if [[ "${1:-}" == "--hold-strategy" ]]; then
  HOLD_STRATEGY=1
fi

print_header "Live OMS Restart"
cat <<'EOF'
This script uses the safe live-session order:
1. stop strategy so bots stop publishing new intents
2. wait for OMS to drain and for operator confirmation
3. restart OMS
4. optionally start strategy again

Do not use this while:
- a cancel is still in flight
- a fill is still settling
- you are unsure about current broker/account positions
EOF

print_dashboard_checks
confirm_step "Stop strategy and begin OMS maintenance? [y/N] "
stop_unit "project-mai-tai-strategy.service"

echo
echo "Drain check required before OMS restart:"
echo "- refresh /api/orders"
echo "- refresh /api/positions"
echo "- confirm no pending, submitted, or accepted intents remain"
echo "- confirm no fill/cancel workflow is still in progress"
confirm_step "Has OMS drained cleanly and is it safe to restart OMS now? [y/N] "

restart_unit "project-mai-tai-oms.service"

if (( HOLD_STRATEGY )); then
  echo
  echo "Strategy remains stopped because --hold-strategy was used."
else
  confirm_step "Start strategy again now? [y/N] "
  start_unit "project-mai-tai-strategy.service"
fi

echo
echo "Post-checks:"
echo "- Verify /api/positions repopulates broker account positions."
echo "- Verify /api/orders shows no unexpected duplicate or rejected orders."
echo "- Verify /api/reconciliation remains clean."
print_dashboard_checks
print_log_hint
