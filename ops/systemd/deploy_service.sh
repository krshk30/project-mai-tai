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

if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "missing git repo: $REPO_DIR"
  exit 1
fi

if [[ -z "$SERVICE_TARGET" ]]; then
  echo "usage: deploy_service.sh <repo_dir> <branch> <control|reconciler|strategy|tv-alerts|oms|market-data>"
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
  tv-alerts)
    PRIMARY_UNIT="project-mai-tai-tv-alerts.service"
    HIGH_RISK=0
    ;;
  oms)
    PRIMARY_UNIT="project-mai-tai-oms.service"
    HIGH_RISK=1
    ;;
  market-data)
    PRIMARY_UNIT="project-mai-tai-market-data.service"
    HIGH_RISK=1
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
  echo "control, reconciler, and tv-alerts are lower-risk; strategy, oms, and market-data require explicit live approval"
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

if [[ "$HIGH_RISK" == "1" && "$ALLOW_LIVE_RESTART" == "1" && "$IN_MARKET_WINDOW" == "1" ]]; then
  run_live_preflight
fi

echo "Refreshing runtime in $REPO_DIR (migrations=$RUN_MIGRATIONS)..."
sudo MAI_TAI_RUN_MIGRATIONS="$RUN_MIGRATIONS" bash ops/bootstrap/08_install_runtime.sh "$REPO_DIR"

case "$SERVICE_TARGET" in
  control|reconciler|strategy|tv-alerts)
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

echo
echo "Current /health payload:"
show_health_payload

echo "Service deploy finished for $SERVICE_TARGET."
