#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Shared output helpers for live-session operator scripts.
source "$SCRIPT_DIR/live_helpers.sh"

print_header "Live Strategy Restart"
cat <<'EOF'
Warning:
- strategy runtime state does not fully rehydrate after restart
- broker and database positions can remain visible even if bot runtime positions reset

Only continue if:
- there are no pending opens or closes
- there is no in-flight order workflow you are waiting on
- you understand any currently open broker positions
EOF

print_dashboard_checks
confirm_step "Have you completed the preflight checks and still want to restart strategy? [y/N] "
restart_unit "project-mai-tai-strategy.service"

echo
echo "Post-checks:"
echo "- Verify /api/scanner is live, not stuck on restored data."
echo "- Verify /api/bots watchlists rebuild."
echo "- Verify /api/positions still shows expected broker/account positions."
echo "- Verify /api/reconciliation stays clean."
print_dashboard_checks
print_log_hint
