#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
ENV_FILE="${2:-/etc/project-mai-tai/project-mai-tai.env}"
UNIT="project-mai-tai-momentum-paper.service"

eastern_hour=$((10#$(TZ=America/New_York date +%H)))
eastern_weekday=$((10#$(TZ=America/New_York date +%u)))
if [[ "$eastern_weekday" -le 5 && "$eastern_hour" -lt 16 ]]; then
  echo "refusing Momentum paper installation before 16:00 ET"
  exit 1
fi

flag_count="$(grep -Ec '^MAI_TAI_MOMENTUM_PAPER_ENABLED=true$' "$ENV_FILE" || true)"
if [[ "$flag_count" != "1" ]]; then
  echo "expected exactly one MAI_TAI_MOMENTUM_PAPER_ENABLED=true in $ENV_FILE"
  exit 1
fi

sudo -u trader "$REPO_DIR/.venv/bin/pip" install -e "$REPO_DIR"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
sudo --preserve-env=MAI_TAI_DATABASE_URL -u trader \
  "$REPO_DIR/.venv/bin/alembic" upgrade head

"$REPO_DIR/ops/systemd/verify_momentum_paper_entitlement.sh" "$REPO_DIR" 15
sudo cp "$REPO_DIR/ops/systemd/$UNIT" "/etc/systemd/system/$UNIT"
sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT"

pid="$(sudo systemctl show "$UNIT" --property MainPID --value)"
if [[ ! "$pid" =~ ^[1-9][0-9]*$ ]]; then
  echo "Momentum paper unit did not start"
  exit 1
fi
if ! sudo tr '\0' '\n' < "/proc/$pid/environ" \
  | grep -Fxq 'MAI_TAI_MOMENTUM_PAPER_ENABLED=true'; then
  echo "running Momentum paper process did not load its enable flag"
  exit 1
fi

echo "MOMENTUM PAPER INSTALL: PASS pid=$pid; no existing service restarted"
