#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Shared output helpers for live-session operator scripts.
source "$SCRIPT_DIR/live_helpers.sh"

print_header "Live Reconciler Restart"
cat <<'EOF'
This restarts only the reconciliation worker.
Trading services should continue running, but reconciliation checks pause briefly.
EOF

confirm_step "Restart project-mai-tai-reconciler.service now? [y/N] "
restart_unit "project-mai-tai-reconciler.service"

echo
echo "Post-checks:"
echo "- Review /api/reconciliation for new critical findings."
print_dashboard_checks
print_log_hint
