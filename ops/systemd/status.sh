#!/usr/bin/env bash
set -euo pipefail

sudo systemctl status \
  project-mai-tai-market-data.service \
  project-mai-tai-strategy.service \
  project-mai-tai-tv-alerts.service \
  project-mai-tai-oms.service \
  project-mai-tai-reconciler.service \
  project-mai-tai-control.service \
  --no-pager
