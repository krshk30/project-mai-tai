#!/usr/bin/env bash
# Q5/§197 cron wrapper for broker_blind_check.sh.
#
# Install (Monday, root crontab — the trader crontab was found to be a strict SUBSET of
# root's on 08-19, with 8 scripts double-executing, so ONE owner only):
#
#   */2 7-18 * * 1-5  /home/trader/project-mai-tai/ops/health/broker_blind_cron.sh
#
# ⛔ Every 2 minutes across the trading window. The trip needs >=90s of blindness, so a 2-min
# cadence cannot miss a qualifying run, and the dedupe file stops a long outage re-paging.
# ⛔ CRON_TZ IS IGNORED on this box — the hour range above is UTC. 7-18 UTC is 03:00-14:00 ET,
# which does NOT cover the 7-18 ET window. Use 11-23 UTC for 07:00-19:00 ET, and confirm
# against `date` on the box before installing rather than trusting this comment.
# ⛔ A cron script committed from Windows lands 100644 and silently never runs. The exec bit
# is committed (100755); do NOT hand-chmod on the box -- a manual mode change there has
# blocked every deploy before (#693).
set -u
DIR=$(cd -- "$(dirname -- "$0")" && pwd)
LOG=/var/log/project-mai-tai/q5_broker_blind.log

{
  echo "----- $(date -u '+%Y-%m-%d %H:%M:%S UTC') -----"
  bash "$DIR/broker_blind_check.sh"
  echo "exit=$?"
} >> "$LOG" 2>&1
