#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${1:-/home/trader/project-mai-tai}"
SYSTEMD_DIR="/etc/systemd/system"
SOURCE_DIR="$REPO_DIR/ops/systemd"

for unit in \
  project-mai-tai-control.service \
  project-mai-tai-market-data.service \
  project-mai-tai-strategy.service \
  project-mai-tai-oms.service \
  project-mai-tai-reconciler.service \
  project-mai-tai-trade-coach.service \
  project-mai-tai.target
do
  sudo cp "$SOURCE_DIR/$unit" "$SYSTEMD_DIR/$unit"
done

sudo systemctl daemon-reload
