#!/usr/bin/env bash
set -euo pipefail
umask 077
unset TZ
export PYTHONDONTWRITEBYTECODE=1
exec /home/trader/project-mai-tai/.venv/bin/python /home/trader/preopen-daily/daily.py notify
