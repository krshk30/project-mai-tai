#!/usr/bin/env bash
# Focused, attended install for a reviewed subset of OMS/v2/strategy/control/ORB.
# C1 runs this after the Oct30 close; Main only authors/reviews its source.
set -Eeuo pipefail
umask 077
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/restart_evidence_install_lib.sh"
export PYTHONDONTWRITEBYTECODE=1 TZ=UTC LC_ALL=C

fsi_git() { sudo -n -u trader git -C "$FSI_REPO" "$@"; }
fsi_value() { rei_python field "$FSI_PLAN_JSON" "$1"; }
fsi_selected() { [[ ",$FSI_SERVICE_CSV," == *",$1,"* ]]; }

fsi_gates() {
  local reason
  # Both are fresh while all heartbeat writers are still alive. No Bug2/position bypass.
  bash "$FSI_REPO/ops/preflight/preflight_oms_restart.sh" \
    --max-age-seconds 30 --require-all-account-positions-flat
  reason=$(fsi_value clock_override_reason)
  if [[ -n "$reason" ]]; then
    bash "$FSI_REPO/ops/preflight/preflight_v2_restart.sh" \
      --clock-override "$reason" --i-accept-clock
  else
    bash "$FSI_REPO/ops/preflight/preflight_v2_restart.sh"
  fi
}

fsi_stop() {
  local name=$1 unit="project-mai-tai-$1.service" raw
  rei_python event "$FSI_JOURNAL" stop-request "$name" entered
  systemctl stop "$unit"
  raw=$(systemctl show "$unit" -p MainPID -p ActiveState -p SubState -p Result -p NRestarts)
  [[ $(printf '%s\n' "$raw" | sed -n 's/^MainPID=//p') == 0 &&
     $(printf '%s\n' "$raw" | sed -n 's/^ActiveState=//p') == inactive &&
     $(printf '%s\n' "$raw" | sed -n 's/^SubState=//p') == dead &&
     $(printf '%s\n' "$raw" | sed -n 's/^Result=//p') == success ]] || {
       printf 'STOP: %s did not stop cleanly; no reset/retry/start\n' "$name" >&2; return 2;
     }
  printf '%s\n' "$raw" >"$REI_EVIDENCE/$name.stopped.txt"
  rei_python event "$FSI_JOURNAL" stop "$name" success
}

fsi_start() {
  local name=$1 unit="project-mai-tai-$1.service" raw pid
  rei_python event "$FSI_JOURNAL" start-request "$name" entered
  systemctl start "$unit"
  raw=$(systemctl show "$unit" -p MainPID -p ActiveState -p SubState -p Result -p NRestarts -p ExecMainStartTimestamp -p InvocationID)
  pid=$(printf '%s\n' "$raw" | sed -n 's/^MainPID=//p')
  [[ $pid =~ ^[1-9][0-9]*$ &&
     $(printf '%s\n' "$raw" | sed -n 's/^ActiveState=//p') == active &&
     $(printf '%s\n' "$raw" | sed -n 's/^SubState=//p') == running &&
     $(printf '%s\n' "$raw" | sed -n 's/^Result=//p') == success &&
     $(printf '%s\n' "$raw" | sed -n 's/^NRestarts=//p') == 0 ]] || {
       printf 'STOP: %s first start is not active/running/NR0; no retry\n' "$name" >&2; return 2;
     }
  printf '%s\n' "$raw" >"$REI_EVIDENCE/$name.started.txt"
  rei_python event "$FSI_JOURNAL" start "$name" success
}

fsi_hook() {
  rei_python hook-check "$FSI_PLAN_JSON"
  "$REI_PY" "$(fsi_value verification_hook)" "$1" "$REI_EVIDENCE"
}

fsi_execute() {
  local name
  rei_python env-check "$FSI_PLAN_JSON" /etc/project-mai-tai/project-mai-tai.env
  fsi_hook before-stop
  fsi_gates
  rei_snapshot                          # MUST finish rc0 before the first stop.
  [[ $(fsi_git rev-parse HEAD) == "$(fsi_value expected_current_sha)" ]]
  [[ -z $(fsi_git status --porcelain) ]]
  rei_python env-check "$FSI_PLAN_JSON" /etc/project-mai-tai/project-mai-tai.env
  sha256sum -c "$REI_EVIDENCE/snapshot.sha256" >/dev/null
  fsi_hook pre-first-stop                # Current published native book, no new provider call.
  for name in orb-schwab schwab-1m-v2 strategy oms control; do
    if fsi_selected "$name"; then fsi_stop "$name"; fi
  done
  # A failure here leaves the actual stopped state visible. Never restore an old PID.
  fsi_git checkout --detach "$FSI_TARGET"
  [[ $(fsi_git rev-parse HEAD) == "$FSI_TARGET" && -z $(fsi_git status --porcelain) ]]
  rei_python env "$FSI_PLAN_JSON" /etc/project-mai-tai/project-mai-tai.env \
    "$REI_EVIDENCE/environment.preimage" >"$REI_EVIDENCE/environment-receipt.json"
  for name in oms schwab-1m-v2 strategy control orb-schwab; do
    if fsi_selected "$name"; then
      fsi_hook "before-start:$name"
      fsi_start "$name"
      if [[ $name == oms ]]; then fsi_hook oms-ready; fi
    fi
  done
  # Control serves /health and is deliberately stopped during this package.
  # Evaluate first-health after all starts; the original per-process SLA still applies.
  for name in oms schwab-1m-v2 strategy control orb-schwab; do
    if fsi_selected "$name"; then fsi_hook "health-ready:$name"; fi
  done
  fsi_hook post-start
  sha256sum -c "$REI_EVIDENCE/snapshot.sha256" >/dev/null
  rei_python record "$REI_SNAPSHOT" "$FSI_JOURNAL" "$FSI_SERVICE_CSV" "$REI_RECORD"
  rei_report                            # Only after every selected first start and proof succeeded.
  rei_python gate-args "$FSI_PLAN_JSON" "$FSI_SERVICE_CSV" "$REI_SNAPSHOT" "$REI_RECORD" \
    "$REI_EVIDENCE/preopen-collector-arguments.json"
  printf 'SOURCE INSTALL + OFFICIAL REPORT COMPLETE; fleet/watch/preopen acceptance remains C1-owned\n'
}

fsi_main() {
  if [[ ${1:---plan} == --plan ]]; then
    printf '%s\n' 'SOURCE PLAN ONLY; no install or query.' \
      'C1 execution: five_service_install.sh --execute TARGET_SHA ROOT600_ENV_PLAN.json SERVICE [SERVICE...]' \
      'Allowed services: oms schwab-1m-v2 strategy control orb-schwab; v2 is required.' \
      'No automatic fetch, migration, daemon reload, retry, rollback, preopen publication or gate run.'
    return 0
  fi
  [[ ${1:-} == --execute && $# -ge 4 ]] || { echo 'STOP: explicit execution arguments required' >&2; return 2; }
  shift
  FSI_TARGET=$1; FSI_PLAN_FILE=$2; shift 2
  FSI_SERVICES=("$@")
  FSI_SERVICE_CSV=$(IFS=,; printf '%s' "${FSI_SERVICES[*]}")
  FSI_REPO=/home/trader/project-mai-tai
  REI_PY=$FSI_REPO/.venv/bin/python
  REI_COLLECTOR=$FSI_REPO/ops/health/v2_restart_evidence.py
  [[ $(id -u) == 0 && $(uname -s) == Linux && $FSI_TARGET =~ ^[0-9a-f]{40}$ ]]
  [[ -x "$REI_PY" && -f "$REI_COLLECTOR" && -d "$FSI_REPO/.git" ]]
  [[ "$HERE" != "$FSI_REPO"/* ]] || { echo 'STOP: stage reviewed installer tools outside mutable checkout' >&2; return 2; }
  [[ ! -e /var/lib/project-mai-tai/option-a/safety-stop.json &&
     ! -L /var/lib/project-mai-tai/option-a/safety-stop.json ]]
  [[ $(stat -c '%u %a' "$HERE") == '0 700' ]]
  FSI_PLAN_JSON=$(rei_python validate "$FSI_PLAN_FILE" "$FSI_TARGET" "$FSI_SERVICE_CSV")
  local date_et time_et
  date_et=$(TZ=America/New_York date +%F); time_et=$(TZ=America/New_York date +%H%M)
  [[ $date_et == "$(fsi_value install_date_et)" && $date_et > 2026-10-29 && 10#$time_et -ge 1605 ]]
  [[ $(fsi_git rev-parse HEAD) == "$(fsi_value expected_current_sha)" && -z $(fsi_git status --porcelain) ]]
  fsi_git cat-file -e "$FSI_TARGET^{commit}"    # Target must already be staged/verified by C1.
  local source_file staged_hash target_hash
  for source_file in five_service_install.sh restart_evidence_install_lib.sh five_service_install_proof.py; do
    [[ ! -L "$HERE/$source_file" && $(stat -c '%u %a' "$HERE/$source_file") == '0 600' ]]
    staged_hash=$(sha256sum "$HERE/$source_file"); staged_hash=${staged_hash%% *}
    target_hash=$(fsi_git show "$FSI_TARGET:ops/install/$source_file" | sha256sum); target_hash=${target_hash%% *}
    [[ $staged_hash == "$target_hash" ]]
  done
  [[ $(fsi_value verification_hook) == "$HERE/five_service_install_proof.py" ]]
  local free_kib copy_kib
  free_kib=$(df -Pk "$FSI_REPO" | awk 'NR==2 {print $4}')
  copy_kib=$(du -sk "$FSI_REPO" | awk '{print $1}')
  (( free_kib >= 15728640 + copy_kib ))
  # Only this focused installer uses this lock. Native preopen remains a separate locked reader.
  [[ -d /run && ! -L /run && $(stat -c '%u %a' /run) == '0 755' ]]
  local lock=/run/project-mai-tai-five-service-install.lock
  [[ ! -L "$lock" ]]
  if [[ -e "$lock" ]]; then [[ $(stat -c '%u %a' "$lock") == '0 600' ]]; fi
  exec 9>"$lock"
  flock -n 9
  # The existing native gate owns this same reader/execution lock; never overlap it.
  [[ ! -L /home/trader/preopen-daily &&
     $(stat -c '%u %a' /home/trader/preopen-daily) == '0 700' &&
     ! -L /home/trader/preopen-daily/run.lock &&
     $(stat -c '%u %a' /home/trader/preopen-daily/run.lock) == '0 600' ]]
  exec 8<>/home/trader/preopen-daily/run.lock
  flock -n 8
  REI_EVIDENCE=$(fsi_value evidence_directory)
  [[ ! -e "$REI_EVIDENCE" ]]
  [[ -d $(dirname "$REI_EVIDENCE") ]]
  mkdir -m 0700 -- "$REI_EVIDENCE"
  printf '%s\n' "$FSI_PLAN_JSON" >"$REI_EVIDENCE/reviewed-plan.json"
  printf '%s\n' "${FSI_SERVICES[@]}" >"$REI_EVIDENCE/selected-services.txt"
  sha256sum "$0" "$HERE/restart_evidence_install_lib.sh" >"$REI_EVIDENCE/installer-source.sha256"
  REI_SNAPSHOT=$REI_EVIDENCE/pre-first-stop-snapshot.json
  REI_RECORD=$REI_EVIDENCE/completed-install-record.json
  FSI_JOURNAL=$REI_EVIDENCE/service-actions.jsonl
  trap 'rc=$?; printf "STOP rc=%s; preserve actual state and evidence; no automatic remediation\n" "$rc" >&2; exit "$rc"' ERR
  fsi_execute
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then fsi_main "$@"; fi
