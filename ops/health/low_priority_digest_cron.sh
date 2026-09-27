#!/usr/bin/env bash
# UTC cron runs at both DST candidates; the Python ET guard accepts exactly 20:00.
set -euo pipefail
exec /home/trader/project-mai-tai/.venv/bin/python \
  /home/trader/project-mai-tai/ops/health/low_priority_alerts.py --digest
