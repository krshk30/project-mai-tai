#!/usr/bin/env bash
set -euo pipefail

readonly MAI_TAI_CONTROL_API_BASE="${MAI_TAI_CONTROL_API_BASE:-https://project-mai-tai.live/api}"

print_header() {
  local title="$1"
  echo
  echo "== $title =="
  echo
}

confirm_step() {
  local prompt="${1:-Continue? [y/N] }"
  local reply

  read -r -p "$prompt" reply || true
  case "${reply,,}" in
    y|yes)
      ;;
    *)
      echo "Aborted."
      exit 1
      ;;
  esac
}

wait_for_unit_active() {
  local unit="$1"
  local timeout_secs="${2:-30}"
  local elapsed=0

  while (( elapsed < timeout_secs )); do
    if sudo systemctl is-active --quiet "$unit"; then
      return 0
    fi

    sleep 1
    elapsed=$((elapsed + 1))
  done

  echo "Timed out waiting for $unit to become active."
  sudo systemctl status "$unit" --no-pager || true
  exit 1
}

restart_unit() {
  local unit="$1"

  echo "Restarting $unit..."
  sudo systemctl restart "$unit"
  wait_for_unit_active "$unit"
  sudo systemctl status "$unit" --no-pager
}

stop_unit() {
  local unit="$1"

  echo "Stopping $unit..."
  sudo systemctl stop "$unit"
  sudo systemctl status "$unit" --no-pager || true
}

start_unit() {
  local unit="$1"

  echo "Starting $unit..."
  sudo systemctl start "$unit"
  wait_for_unit_active "$unit"
  sudo systemctl status "$unit" --no-pager
}

print_dashboard_checks() {
  cat <<EOF
Check these endpoints now:
- ${MAI_TAI_CONTROL_API_BASE}/overview
- ${MAI_TAI_CONTROL_API_BASE}/scanner
- ${MAI_TAI_CONTROL_API_BASE}/bots
- ${MAI_TAI_CONTROL_API_BASE}/orders
- ${MAI_TAI_CONTROL_API_BASE}/positions
- ${MAI_TAI_CONTROL_API_BASE}/reconciliation
EOF
}

print_log_hint() {
  cat <<'EOF'
Useful logs:
- /var/log/project-mai-tai/control.log
- /var/log/project-mai-tai/market-data.log
- /var/log/project-mai-tai/strategy.log
- /var/log/project-mai-tai/oms.log
- /var/log/project-mai-tai/reconciler.log
EOF
}
