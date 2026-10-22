#!/usr/bin/env bash
set -euo pipefail

sudo systemctl restart project-mai-tai-market-data.service
sudo systemctl restart project-mai-tai-snapshot-worker.service
sudo systemctl restart \
  project-mai-tai-strategy.service \
  project-mai-tai-oms.service \
  project-mai-tai-reconciler.service \
  project-mai-tai-control.service \
  project-mai-tai-orb-schwab.service
