#!/bin/bash
set -Eeuo pipefail
umask 077
export TZ=UTC PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH PYTHONHOME
ROOT=/home/trader/after-hours/2026-10-03/rpg1-coldstart1-job
RUN=/home/trader/after-hours/2026-10-03/rpg1-coldstart1-run
PY=/home/trader/project-mai-tai/.venv/bin/python
cd "$ROOT"
"$PY" -s review_gate.py --prepare
exec 9>/run/lock/project-mai-tai-deploy.lock
flock -n 9 || { printf '%s\n' 'STOP: deploy lock already owned'; exit 2; }
"$PY" -s review_gate.py --prepare
mkdir -m 0700 "$RUN" || { printf '%s\n' 'STOP: attempt already claimed'; exit 2; }
# install.py owns failure evidence. The shell trap also catches interpreter/startup failure.
trap 'rc=$?; trap - EXIT; if (( rc != 0 )); then "$PY" -s install.py abort "$rc" || true; fi; exit "$rc"' EXIT
systemctl disable --now project-mai-tai-rpg1-coldstart1-install-20261003.timer
"$PY" -s install.py run >"$RUN/runner.log" 2>&1
