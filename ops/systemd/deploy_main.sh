#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
BRANCH="${2:-main}"
ALLOW_LIVE_RESTART="${MAI_TAI_ALLOW_LIVE_RESTART:-0}"
APP_HEALTH_URL="${APP_HEALTH_URL:-http://127.0.0.1:8100/health}"
HEALTH_OUTPUT_FILE="${HEALTH_OUTPUT_FILE:-/tmp/project_mai_tai_health.json}"

if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "missing git repo: $REPO_DIR"
  exit 1
fi

eastern_hour=$((10#$(TZ=America/New_York date +%H)))
eastern_weekday=$((10#$(TZ=America/New_York date +%u)))
if [[ "$ALLOW_LIVE_RESTART" != "1" && "$eastern_weekday" -le 5 && "$eastern_hour" -ge 7 && "$eastern_hour" -lt 16 ]]; then
  echo "refusing automated deploy during ET market hours"
  echo "rerun with MAI_TAI_ALLOW_LIVE_RESTART=1 only if the live-session restart risk is understood"
  exit 1
fi

cd "$REPO_DIR"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "refusing deploy because repo has local changes"
  git status --short
  exit 1
fi

git fetch origin
git checkout "$BRANCH"
git merge --ff-only "origin/$BRANCH"

sudo bash ops/bootstrap/08_install_runtime.sh "$REPO_DIR"
bash ops/systemd/restart_all.sh

for _attempt in {1..45}; do
  if sudo systemctl is-active --quiet \
    project-mai-tai-control.service \
    project-mai-tai-market-data.service \
    project-mai-tai-strategy.service \
    project-mai-tai-tv-alerts.service \
    project-mai-tai-oms.service \
    project-mai-tai-reconciler.service; then
    if curl -fsS "$APP_HEALTH_URL" > "$HEALTH_OUTPUT_FILE"; then
      if python3 - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("/tmp/project_mai_tai_health.json").read_text(encoding="utf-8"))
if payload.get("status") != "healthy":
    raise SystemExit(1)
PY
      then
        cat "$HEALTH_OUTPUT_FILE"
        exit 0
      fi
    fi
  fi
  sleep 2
done

echo "deploy finished but healthy control-plane state was not observed"
if [[ -f "$HEALTH_OUTPUT_FILE" ]]; then
  cat "$HEALTH_OUTPUT_FILE"
fi
exit 1
