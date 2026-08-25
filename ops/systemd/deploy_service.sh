#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
BRANCH="${2:-main}"
SERVICE_TARGET="${3:-}"
ALLOW_LIVE_RESTART="${MAI_TAI_ALLOW_LIVE_RESTART:-0}"
RUN_MIGRATIONS="${MAI_TAI_RUN_MIGRATIONS:-0}"
HOLD_STRATEGY="${MAI_TAI_HOLD_STRATEGY:-0}"
APP_HEALTH_URL="${APP_HEALTH_URL:-http://127.0.0.1:8100/health}"
APP_OVERVIEW_URL="${APP_OVERVIEW_URL:-http://127.0.0.1:8100/api/overview}"
DEFAULT_POST_RESTART_HEALTH_SLA_SECONDS=60
STRATEGY_POST_RESTART_HEALTH_SLA_SECONDS=240
declare -a RESTARTED_UNITS=()
declare -a RESTARTED_PIDS=()
declare -a RESTARTED_START_EPOCHS=()

if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "missing git repo: $REPO_DIR"
  exit 1
fi

if [[ -z "$SERVICE_TARGET" ]]; then
  echo "usage: deploy_service.sh <repo_dir> <branch> <control|reconciler|strategy|oms|market-data|schwab-1m-v2>"
  exit 1
fi

case "$SERVICE_TARGET" in
  control)
    PRIMARY_UNIT="project-mai-tai-control.service"
    HIGH_RISK=0
    ;;
  reconciler)
    PRIMARY_UNIT="project-mai-tai-reconciler.service"
    HIGH_RISK=0
    ;;
  strategy)
    PRIMARY_UNIT="project-mai-tai-strategy.service"
    HIGH_RISK=1
    ;;
  oms)
    PRIMARY_UNIT="project-mai-tai-oms.service"
    HIGH_RISK=1
    ;;
  market-data)
    PRIMARY_UNIT="project-mai-tai-market-data.service"
    HIGH_RISK=1
    ;;
  schwab-1m-v2)
    PRIMARY_UNIT="project-mai-tai-schwab-1m-v2.service"
    HIGH_RISK=0
    ;;
  *)
    echo "unknown service target: $SERVICE_TARGET"
    exit 1
    ;;
esac

if [[ "$HOLD_STRATEGY" == "1" && "$SERVICE_TARGET" != "oms" && "$SERVICE_TARGET" != "market-data" ]]; then
  echo "--hold-strategy only applies to oms and market-data deploys"
  exit 1
fi

eastern_hour=$((10#$(TZ=America/New_York date +%H)))
eastern_weekday=$((10#$(TZ=America/New_York date +%u)))
IN_MARKET_WINDOW=0
if [[ "$eastern_weekday" -le 5 && "$eastern_hour" -ge 7 && "$eastern_hour" -lt 16 ]]; then
  IN_MARKET_WINDOW=1
fi

if [[ "$HIGH_RISK" == "1" && "$ALLOW_LIVE_RESTART" != "1" && "$IN_MARKET_WINDOW" == "1" ]]; then
  echo "refusing $SERVICE_TARGET deploy during ET market hours without MAI_TAI_ALLOW_LIVE_RESTART=1"
  echo "control and reconciler are lower-risk; strategy, oms, and market-data require explicit live approval"
  exit 1
fi

if [[ "$RUN_MIGRATIONS" == "1" && "$IN_MARKET_WINDOW" == "1" ]]; then
  echo "refusing live service deploy with migrations enabled"
  echo "schema migrations during ET market hours remain a human-approved red-zone operation"
  exit 1
fi

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

report_no_automatic_rollback() {
  echo "No automatic rollback attempted; operator intervention is required."
  echo "A rollback is a separate production mutation, and the prior revision is not proven healthy."
}

record_unit_identity() {
  local unit="$1"
  local pid
  local start_timestamp
  local start_epoch

  if ! pid="$(sudo systemctl show "$unit" --property MainPID --value)"; then
    echo "POST-RESTART GATE: COULD_NOT_TELL"
    echo "Could not read MainPID for $unit."
    report_no_automatic_rollback
    exit 3
  fi
  if ! start_timestamp="$(
    sudo systemctl show "$unit" --property ExecMainStartTimestamp --value
  )"; then
    echo "POST-RESTART GATE: COULD_NOT_TELL"
    echo "Could not read ExecMainStartTimestamp for $unit."
    report_no_automatic_rollback
    exit 3
  fi
  if [[ ! "$pid" =~ ^[1-9][0-9]*$ || -z "$start_timestamp" ]]; then
    echo "POST-RESTART GATE: COULD_NOT_TELL"
    echo "Could not establish the new process identity for $unit."
    sudo systemctl status "$unit" --no-pager || true
    report_no_automatic_rollback
    exit 3
  fi
  if ! start_epoch="$(date --date="$start_timestamp" +%s)"; then
    echo "POST-RESTART GATE: COULD_NOT_TELL"
    echo "Could not parse process start timestamp for $unit: $start_timestamp"
    report_no_automatic_rollback
    exit 3
  fi

  RESTARTED_UNITS+=("$unit")
  RESTARTED_PIDS+=("$pid")
  RESTARTED_START_EPOCHS+=("$start_epoch")
  echo "New process identity: unit=$unit pid=$pid started=$start_timestamp"
}

show_health_payload() {
  if curl -fsS "$APP_HEALTH_URL"; then
    echo
  else
    echo "warning: control-plane /health did not return 200"
  fi
}

restart_unit() {
  local unit="$1"

  echo "Restarting $unit..."
  sudo systemctl restart "$unit"
  wait_for_unit_active "$unit"
  record_unit_identity "$unit"
}

stop_unit() {
  local unit="$1"
  echo "Stopping $unit..."
  sudo systemctl stop "$unit"
}

start_unit() {
  local unit="$1"
  echo "Starting $unit..."
  sudo systemctl start "$unit"
  wait_for_unit_active "$unit"
  record_unit_identity "$unit"
}

heartbeat_service_for_unit() {
  case "$1" in
    project-mai-tai-control.service) echo "control-plane" ;;
    project-mai-tai-reconciler.service) echo "reconciler" ;;
    project-mai-tai-strategy.service) echo "strategy-engine" ;;
    project-mai-tai-oms.service) echo "oms-risk" ;;
    project-mai-tai-market-data.service) echo "market-data-gateway" ;;
    project-mai-tai-schwab-1m-v2.service) echo "schwab-1m-v2" ;;
    *)
      echo "No heartbeat service mapping for $1" >&2
      return 3
      ;;
  esac
}

health_sla_for_unit() {
  case "$1" in
    project-mai-tai-strategy.service)
      echo "$STRATEGY_POST_RESTART_HEALTH_SLA_SECONDS"
      ;;
    *)
      echo "$DEFAULT_POST_RESTART_HEALTH_SLA_SECONDS"
      ;;
  esac
}

health_sla_basis_for_unit() {
  case "$1" in
    project-mai-tai-strategy.service)
      echo "measured 2026-08-24 strategy restart: first fresh heartbeat at 113s, healthy at 181s; 240s allows almost four more 15s heartbeat intervals"
      ;;
    *)
      echo "9 successful deploys from 2026-07-25 through 2026-08-23: 0.5-13.1s restart-to-active; 60s allows four 15s heartbeat intervals"
      ;;
  esac
}

verify_unit_identity() {
  local unit="$1"
  local expected_pid="$2"
  local expected_start_epoch="$3"
  local current_pid
  local current_start_timestamp
  local current_start_epoch

  if ! current_pid="$(sudo systemctl show "$unit" --property MainPID --value)"; then
    echo "POST-RESTART GATE: COULD_NOT_TELL"
    echo "Could not re-read MainPID for $unit."
    return 3
  fi
  if ! current_start_timestamp="$(
    sudo systemctl show "$unit" --property ExecMainStartTimestamp --value
  )"; then
    echo "POST-RESTART GATE: COULD_NOT_TELL"
    echo "Could not re-read ExecMainStartTimestamp for $unit."
    return 3
  fi
  if ! current_start_epoch="$(date --date="$current_start_timestamp" +%s 2>/dev/null)"; then
    echo "POST-RESTART GATE: COULD_NOT_TELL"
    echo "Could not parse current process start timestamp for $unit: $current_start_timestamp"
    return 3
  fi
  if [[ "$current_pid" != "$expected_pid" || "$current_start_epoch" != "$expected_start_epoch" ]]; then
    echo "POST-RESTART GATE: NOT_HEALTHY_WITHIN_SLA"
    echo "Process identity changed during validation for $unit."
    echo "Expected pid=$expected_pid start_epoch=$expected_start_epoch; " \
      "found pid=$current_pid start_epoch=$current_start_epoch."
    return 1
  fi
}

run_post_restart_health_gates() {
  local deployed_sha="$1"
  local index
  local unit
  local pid
  local start_epoch
  local heartbeat_service
  local sla_seconds
  local sla_basis
  local gate_rc
  local identity_rc

  for index in "${!RESTARTED_UNITS[@]}"; do
    unit="${RESTARTED_UNITS[$index]}"
    pid="${RESTARTED_PIDS[$index]}"
    start_epoch="${RESTARTED_START_EPOCHS[$index]}"
    if ! heartbeat_service="$(heartbeat_service_for_unit "$unit")"; then
      echo "POST-RESTART GATE: COULD_NOT_TELL"
      echo "No health identity is defined for restarted unit $unit."
      report_no_automatic_rollback
      return 3
    fi
    sla_seconds="$(health_sla_for_unit "$unit")"
    sla_basis="$(health_sla_basis_for_unit "$unit")"
    echo "Post-restart SLA: unit=$unit service=$heartbeat_service ${sla_seconds}s from process start; $sla_basis."

    set +e
    "$REPO_DIR/.venv/bin/python" -m project_mai_tai.post_restart_health_gate \
      --health-url "$APP_HEALTH_URL" \
      --service-name "$heartbeat_service" \
      --process-start-epoch "$start_epoch" \
      --pid "$pid" \
      --expected-sha "$deployed_sha" \
      --sla-seconds "$sla_seconds"
    gate_rc=$?
    set -e
    if (( gate_rc != 0 )); then
      report_no_automatic_rollback
      echo "Current /health payload:"
      show_health_payload
      return "$gate_rc"
    fi
    set +e
    verify_unit_identity "$unit" "$pid" "$start_epoch"
    identity_rc=$?
    set -e
    if (( identity_rc != 0 )); then
      report_no_automatic_rollback
      return "$identity_rc"
    fi
  done
}

run_live_preflight() {
  echo "Running live deploy preflight for $SERVICE_TARGET..."
  python3 "$REPO_DIR/src/project_mai_tai/deploy_preflight.py" \
    --service "$SERVICE_TARGET" \
    --overview-url "$APP_OVERVIEW_URL"
}

cd "$REPO_DIR"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "refusing deploy because repo has local changes"
  git status --short
  exit 1
fi

git fetch origin
git checkout "$BRANCH"
git merge --ff-only "origin/$BRANCH"
DEPLOYED_SHA="$(git rev-parse HEAD)"

if [[ "$HIGH_RISK" == "1" && "$ALLOW_LIVE_RESTART" == "1" && "$IN_MARKET_WINDOW" == "1" ]]; then
  run_live_preflight
fi

echo "Refreshing runtime in $REPO_DIR (migrations=$RUN_MIGRATIONS)..."
sudo MAI_TAI_RUN_MIGRATIONS="$RUN_MIGRATIONS" bash ops/bootstrap/08_install_runtime.sh "$REPO_DIR"

case "$SERVICE_TARGET" in
  control|reconciler|strategy|schwab-1m-v2)
    restart_unit "$PRIMARY_UNIT"
    ;;
  oms)
    stop_unit "project-mai-tai-strategy.service"
    restart_unit "$PRIMARY_UNIT"
    if [[ "$HOLD_STRATEGY" == "1" ]]; then
      echo "Strategy remains stopped because MAI_TAI_HOLD_STRATEGY=1"
    else
      start_unit "project-mai-tai-strategy.service"
    fi
    ;;
  market-data)
    stop_unit "project-mai-tai-strategy.service"
    restart_unit "$PRIMARY_UNIT"
    if [[ "$HOLD_STRATEGY" == "1" ]]; then
      echo "Strategy remains stopped because MAI_TAI_HOLD_STRATEGY=1"
    else
      start_unit "project-mai-tai-strategy.service"
    fi
    ;;
esac

sudo systemctl status "$PRIMARY_UNIT" --no-pager || true
if [[ "$SERVICE_TARGET" == "oms" || "$SERVICE_TARGET" == "market-data" || "$SERVICE_TARGET" == "strategy" ]]; then
  sudo systemctl status "project-mai-tai-strategy.service" --no-pager || true
fi

run_post_restart_health_gates "$DEPLOYED_SHA"

echo
echo "Current /health payload:"
show_health_payload

echo "Service deploy finished for $SERVICE_TARGET."
