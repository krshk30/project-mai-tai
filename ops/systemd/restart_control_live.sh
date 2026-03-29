#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Shared output helpers for live-session operator scripts.
source "$SCRIPT_DIR/live_helpers.sh"

print_header "Live Control-Plane Restart"
cat <<'EOF'
This restarts only the operator UI/API service.
Trading services should continue running.
EOF

confirm_step "Restart project-mai-tai-control.service now? [y/N] "
restart_unit "project-mai-tai-control.service"

echo
echo "Post-checks:"
print_dashboard_checks
print_log_hint
