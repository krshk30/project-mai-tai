#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
APP_HEALTH_URL="${APP_HEALTH_URL:-http://127.0.0.1:8100/health}"
HEALTH_OUTPUT_FILE="${HEALTH_OUTPUT_FILE:-/tmp/project_mai_tai_reset_health.json}"

cd "$REPO_DIR"

set -a
source /etc/project-mai-tai/project-mai-tai.env
set +a

sudo systemctl stop project-mai-tai-strategy.service

"$REPO_DIR/.venv/bin/python" -m project_mai_tai.maintenance.reset_active_state

sudo systemctl restart project-mai-tai-oms.service
sudo systemctl restart project-mai-tai-reconciler.service
sudo systemctl start project-mai-tai-strategy.service

for _attempt in {1..45}; do
  if sudo systemctl is-active --quiet \
    project-mai-tai-strategy.service \
    project-mai-tai-oms.service \
    project-mai-tai-reconciler.service; then
    if curl -fsS "$APP_HEALTH_URL" > "$HEALTH_OUTPUT_FILE"; then
      cat "$HEALTH_OUTPUT_FILE"
      exit 0
    fi
  fi
  sleep 2
done

echo "reset completed but healthy control-plane state was not observed"
if [[ -f "$HEALTH_OUTPUT_FILE" ]]; then
    cat "$HEALTH_OUTPUT_FILE"
fi
exit 1
