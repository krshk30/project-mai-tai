#!/usr/bin/env bash
set -euo pipefail
umask 077
REPO=/home/trader/project-mai-tai
JOB=/home/trader/after-hours/2026-10-04/resize-job
RUN=/home/trader/after-hours/2026-10-04/resize-run
PY="$REPO/.venv/bin/python"
MODE=${1:?prepare, postboot, postcheck or recover}
"$PY" "$JOB/review_gate.py"
exec 9>/run/lock/project-mai-tai-deploy.lock
flock -n 9
proof() { "$PY" "$JOB/resize.py" "$@"; }
abort() {
  local rc=$?
  trap - EXIT
  (( rc == 0 )) && return 0
  set +e
  proof journal "STOPPED mode=$MODE rc=$rc; no automatic retry; operator do NOT resize unless PREPARED"
  curl -sS --fail-with-body --connect-timeout 10 --max-time 30 \
    -H 'Title: Sunday resize STOP' -H 'Priority: urgent' \
    -d "Resize $MODE stopped rc=$rc; inspect $RUN; no silent retry or reboot" \
    https://ntfy.sh/mai-tai-preopen-28806a5a97b7 >/dev/null || printf 'PAGE DELIVERY UNKNOWN\n'
  exit "$rc"
}
trap abort EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
if [[ "$MODE" == prepare ]]; then
  mkdir -m 0700 "$RUN"
  exec > >(tee -a "$RUN/prepare.log") 2>&1
  systemctl disable --now project-mai-tai-resize-prepare-20261004.timer
  proof before
  proof flat
  proof idle-gates
  proof archive-intents intents-before.json
  proof backups
  "$PY" "$REPO/ops/health/v2_restart_evidence.py" snapshot --output "$RUN/before-reboot.json"
  proof ready
  printf 'WAITING_FOR_REVIEWED_LOCAL_MERGE max_seconds=600\n'
  deadline=$((SECONDS + 600))
  while [[ ! -f "$RUN/merge.json" ]]; do
    (( SECONDS < deadline ))
    sleep 2
  done
  sudo -u trader git -C "$REPO" fetch origin main:refs/remotes/origin/main
  APP=$(proof target)
  proof flat
  sudo -u trader git -C "$REPO" switch --detach "$APP"
  sudo -u trader "$PY" -m pip install --no-deps --disable-pip-version-check -e "$REPO"
  test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
  "$PY" -c 'import project_mai_tai; print(project_mai_tai.__file__); assert project_mai_tai.__file__=="/home/trader/project-mai-tai/src/project_mai_tai/__init__.py"'
  proof detectors-env
  proof boot-setup
  proof flat
  # A clock-only Sunday exception; an armed-set override is NOT authorized.
  bash "$REPO/ops/preflight/preflight_v2_restart.sh" \
    --clock-override 'Sun 2026-10-04, no session; zero armed, zero managed, both brokers flat' \
    --i-accept-clock | tee "$RUN/v2-restart-gate.txt"
  proof quiesce-ready
  systemctl stop project-mai-tai-schwab-1m-v2.service project-mai-tai-strategy.service \
    project-mai-tai-orb-schwab.service project-mai-tai-orb.service project-mai-tai-momentum-paper.service
  proof drain
  systemctl stop project-mai-tai-oms.service project-mai-tai-reconciler.service project-mai-tai-control.service \
    project-mai-tai-market-capture.service
  proof preserve-final-owners
  systemctl stop project-mai-tai-market-data.service
  proof stopped
  proof flat
  systemctl stop redis-server.service
  proof archive-rdb
  proof journal 'PREPARED; Redis and apps stopped, persistence OFF; operator alone performs 10:00 resize/reboot'
elif [[ "$MODE" == postboot ]]; then
  exec > >(tee -a "$RUN/postboot.log") 2>&1
  proof new-boot
  proof flat
  proof replay-owners
  # The bootstrap unit is the only app boot owner on this first boot.
  # Existing app target and orb-schwab have NO normal boot linkage on the old box.
  systemctl start project-mai-tai-market-data.service
  proof gateway-start-bound
  systemctl start project-mai-tai-oms.service project-mai-tai-strategy.service \
    project-mai-tai-schwab-1m-v2.service project-mai-tai-orb.service \
    project-mai-tai-orb-schwab.service project-mai-tai-momentum-paper.service \
    project-mai-tai-control.service project-mai-tai-market-capture.service project-mai-tai-reconciler.service
  proof fleet
  proof content
  proof gates
  proof repin
  proof closeout
  proof journal 'COMPLETE; weekend tick delivery UNEXERCISED; Monday proof owners recorded'
elif [[ "$MODE" == postcheck ]]; then
  # Explicit continuation after a documented one-attempt recovery; no service action.
  exec > >(tee -a "$RUN/postcheck.log") 2>&1
  proof recovered
  proof fleet
  proof content
  proof gates
  proof repin
  proof closeout
  proof journal 'COMPLETE after documented recovery; weekend tick delivery UNEXERCISED'
elif [[ "$MODE" == recover ]]; then
  UNIT=${2:?exact failed application unit, excluding Redis/Postgres}
  proof recovery-claim "$UNIT"
  proof flat
  # ONE explicit start of ONE failed/inactive app. No reset-failed, loop or reboot.
  timeout 90s systemctl start "$UNIT"
  proof recovery-result "$UNIT"
else
  printf 'unknown mode\n' >&2
  exit 2
fi
trap - EXIT
