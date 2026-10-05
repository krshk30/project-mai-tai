#!/usr/bin/env bash
# Attended single attempt. Stage/run ONLY after literal-runner review.
set -Eeuo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1
export TZ=America/New_York
REPO=/home/trader/project-mai-tai
PY=$REPO/.venv/bin/python
JOB=$(cd -- "$(dirname -- "$0")" && pwd)
APPROVED_SHA=7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf
BOX_SHA=e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89
ATTEMPT=$JOB/attempt-1730-go
STAGE=approval
[[ $EUID == 0 && $# == 1 && $1 =~ ^[0-9a-f]{64}$ ]]
# Caller first verifies the published release hash and each artifact checksum.
"$PY" "$JOB/actions.py" verify "$JOB" "$1"
exec 9>/run/lock/project-mai-tai-deploy.lock
flock -n 9
mkdir "$ATTEMPT"
exec > >(tee -a "$ATTEMPT/runner.log") 2>&1

run() {
  printf '\nUTC=%s ET=%s STAGE=%s CALL' "$(date -u --iso-8601=ns)" "$(date --iso-8601=ns)" "$STAGE"
  printf ' %q' "$@"
  printf '\n'
  "$@"
}
abort() {
  local rc=$? actual
  trap - EXIT
  if (( rc != 0 )); then
    set +e
    actual=$(systemctl show project-mai-tai-oms.service project-mai-tai-schwab-1m-v2.service \
      project-mai-tai-strategy.service -p Id -p MainPID -p ActiveState -p SubState -p NRestarts)
    printf 'STOP rc=%s stage=%s\n%s\n' "$rc" "$STAGE" "$actual"
    "$PY" "$JOB/actions.py" journal "STOP rc=$rc stage=$STAGE attempt=$ATTEMPT actual=$actual; no automatic recovery"
    "$PY" "$JOB/actions.py" page "rc=$rc stage=$STAGE attempt=$ATTEMPT actual=$actual; no automatic recovery"
  fi
  exit "$rc"
}
trap abort EXIT
# No trap starts or restarts a unit: the new GO pre-authorizes no recovery.
read_only_retry() {
  local attempt rc
  for attempt in 1 2 3; do
    printf 'READ_ONLY_ATTEMPT attempt=%s/3 STAGE=%s\n' "$attempt" "$STAGE"
    if run "$@"; then rc=0; else rc=$?; fi
    printf 'READ_ONLY_RESULT attempt=%s/3 rc=%s STAGE=%s\n' "$attempt" "$rc" "$STAGE"
    if (( rc == 0 )); then return 0; fi
    if (( rc != 2 || attempt == 3 )); then return "$rc"; fi
    printf 'READ_ONLY_UNREADABLE wait_seconds=60; no policy waiver\n'
    sleep 60
  done
}
flat_now() { read_only_retry nice -n 19 "$PY" "$JOB/strict_flat_readonly.py"; }
census_now() { read_only_retry "$PY" "$JOB/census_readonly.py" --require-reviewed; }
json_receipt() {
  local name=$1; shift
  printf 'JSON_RECEIPT %s CALL' "$name"
  printf ' %q' "$@"
  printf '\n'
  (set -C; "$@" > "$ATTEMPT/$name")
  cat "$ATTEMPT/$name"
}
redis_now() {
  json_receipt "redis-$(date -u +%s%N).json" "$PY" "$JOB/redis_checkpoint.py" \
    --baseline "$ATTEMPT/redis-baseline.json"
}
window_now() {
  [[ $(date +%F) == 2026-10-05 ]]
  [[ $(date +%H%M) < 1915 ]]
}

STAGE=initial-read-only-gates
window_now
[[ $(runuser -u trader -- git -C "$REPO" rev-parse HEAD) == "$BOX_SHA" ]]
[[ -z $(runuser -u trader -- git -C "$REPO" status --porcelain) ]]
read_only_retry nice -n 19 "$PY" "$JOB/strict_flat_readonly.py" --service oms
read_only_retry nice -n 19 "$PY" "$JOB/strict_flat_readonly.py" --service strategy
census_now
json_receipt redis-baseline.json "$PY" "$JOB/redis_checkpoint.py"
run nice -n 19 "$REPO/ops/preflight/preflight_oms_restart.sh" --require-all-account-positions-flat

STAGE=wait-unmodified-v2-gate
while [[ $(date +%H%M) < 1800 ]]; do
  window_now
  printf 'WAIT clock before18:00; no override, no checkout/env/service change\n'
  sleep 30
done
while :; do
  window_now
  flat_now
  redis_now
  set +e
  "$REPO/ops/preflight/preflight_v2_restart.sh" > "$ATTEMPT/v2-gate-latest.txt" 2>&1
  gate_rc=$?
  set -e
  cat "$ATTEMPT/v2-gate-latest.txt"
  cp "$ATTEMPT/v2-gate-latest.txt" "$ATTEMPT/v2-gate-$(date -u +%s%N).txt"
  if (( gate_rc == 0 )); then break; fi
  # Only the named armed-segment blocker may wait; stale/other evidence stops.
  "$PY" - "$ATTEMPT/v2-gate-latest.txt" <<'PY'
import re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
blocks = [line for line in text.splitlines() if "[BLOCK]" in line]
assert len(blocks) == 1 and re.search(r"\[BLOCK\] \d+ ARMED SEGMENT\(S\)", blocks[0]), blocks
assert "[ok]    past 18:00 ET" in text and "[ok]    zero open managed rows" in text
assert "[ok]    broker flat on both real-money accounts" in text and "[OVERRIDE]" not in text
PY
  printf 'WAIT live armed set; no override\n'
  sleep 60
done
json_receipt before.json "$PY" "$JOB/proof.py" capture

STAGE=source-backup-and-advance
census_now
flat_now
run runuser -u trader -- git -C "$REPO" fetch origin main
[[ $(runuser -u trader -- git -C "$REPO" rev-parse HEAD) == "$BOX_SHA" ]]
[[ -z $(runuser -u trader -- git -C "$REPO" status --porcelain) ]]
run runuser -u trader -- git -C "$REPO" merge-base --is-ancestor "$BOX_SHA" "$APPROVED_SHA"
run runuser -u trader -- git -C "$REPO" merge-base --is-ancestor "$APPROVED_SHA" origin/main
runuser -u trader -- git -C "$REPO" diff --name-only "$APPROVED_SHA" origin/main > "$ATTEMPT/main-ahead-paths.txt"
while IFS= read -r path; do [[ -z $path || $path == docs/* ]]; done < "$ATTEMPT/main-ahead-paths.txt"
run "$PY" "$JOB/actions.py" source "$JOB"
run "$PY" "$REPO/ops/health/v2_restart_evidence.py" snapshot --output "$ATTEMPT/before-restart.json"
run runuser -u trader -- git -C "$REPO" switch --detach "$APPROVED_SHA"
run runuser -u trader -- "$REPO/.venv/bin/pip" install --no-deps -e "$REPO"
[[ $(runuser -u trader -- git -C "$REPO" rev-parse HEAD) == "$APPROVED_SHA" ]]
[[ -z $(runuser -u trader -- git -C "$REPO" status --porcelain) ]]
run runuser -u trader -- "$PY" -c 'import project_mai_tai; print(project_mai_tai.__file__); assert project_mai_tai.__file__.startswith("/home/trader/project-mai-tai/src/")'
flat_now
run "$PY" "$JOB/actions.py" env "$ATTEMPT"
run "$PY" "$JOB/actions.py" journal "BEGIN exact=$APPROVED_SHA attempt=$ATTEMPT after18 unmodified gate; no clock override"

STAGE=stop-v2
window_now
flat_now
redis_now
run nice -n 19 "$REPO/ops/preflight/preflight_v2_restart.sh"
run "$PY" "$JOB/actions.py" unchanged "$ATTEMPT/before.json"
window_now
run systemctl stop project-mai-tai-schwab-1m-v2.service
json_receipt v2-stopped.json "$PY" "$JOB/proof.py" checkpoint --before "$ATTEMPT/before.json" --phase v2-stopped

STAGE=stop-strategy
flat_now
redis_now
run systemctl stop project-mai-tai-strategy.service
json_receipt strategy-stopped.json "$PY" "$JOB/proof.py" checkpoint --before "$ATTEMPT/before.json" --phase strategy-stopped

STAGE=stop-oms
census_now
flat_now
run nice -n 19 "$REPO/ops/preflight/preflight_oms_restart.sh" --require-all-account-positions-flat
redis_now
run systemctl stop project-mai-tai-oms.service
json_receipt oms-stopped.json "$PY" "$JOB/proof.py" checkpoint --before "$ATTEMPT/before.json" --phase oms-stopped

STAGE=additive-0022-migration
flat_now
redis_now
run "$PY" "$JOB/actions.py" migration "$ATTEMPT"
json_receipt migrated.json "$PY" "$JOB/proof.py" checkpoint --before "$ATTEMPT/before.json" --phase migrated

STAGE=start-oms-once
flat_now
redis_now
run systemctl start project-mai-tai-oms.service
OMS_POST_RETURN_UTC=$("$PY" -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat(timespec="microseconds"))')
printf 'OMS_POST_RETURN_UTC=%s\n' "$OMS_POST_RETURN_UTC"
json_receipt oms-started.json "$PY" "$JOB/proof.py" checkpoint --before "$ATTEMPT/before.json" --phase oms-started

STAGE=start-v2-once
flat_now
redis_now
run systemctl start project-mai-tai-schwab-1m-v2.service
json_receipt v2-started.json "$PY" "$JOB/proof.py" checkpoint --before "$ATTEMPT/before.json" --phase v2-started

STAGE=start-strategy-once
flat_now
redis_now
run systemctl start project-mai-tai-strategy.service
json_receipt post-start-logs.json "$PY" "$JOB/proof.py" logs --before "$ATTEMPT/oms-stopped.json" \
  --until 180 --started "$OMS_POST_RETURN_UTC" --redis-baseline "$ATTEMPT/redis-baseline.json"

STAGE=closeout-no-extra-restarts
redis_now
json_receipt final.json "$PY" "$JOB/proof.py" checkpoint --before "$ATTEMPT/before.json" --phase final
json_receipt bar-continuity.json "$PY" "$JOB/proof.py" bar-holes \
  --stopped "$ATTEMPT/v2-stopped.json" --after "$ATTEMPT/final.json"
json_receipt proc-four-keys.json "$PY" "$JOB/proof.py" proc --after "$ATTEMPT/final.json"
json_receipt ticket-dispositions.json "$PY" "$JOB/proof.py" census-after --before "$ATTEMPT/before.json"
run "$PY" "$JOB/actions.py" record "$ATTEMPT"
run "$PY" "$JOB/actions.py" catalogs "$ATTEMPT"
CHECKER_SHA=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["application_blobs"]["ops/health/expected_flags_check.py"])' "$JOB/release.json")
CATALOG_SHA=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["application_blobs"]["ops/health/expected_flags.json"])' "$JOB/release.json")
NUMERIC_SHA=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["application_blobs"]["ops/health/expected_numeric.json"])' "$JOB/release.json")
json_receipt flaggate.json "$PY" "$JOB/proof.py" gates \
  --checker /home/trader/restart_evidence/expected_flags_check.py --checker-sha256 "$CHECKER_SHA" \
  --catalog /home/trader/restart_evidence/expected_flags.json --catalog-sha256 "$CATALOG_SHA" \
  --numeric /home/trader/restart_evidence/expected_numeric.json --numeric-sha256 "$NUMERIC_SHA"
set +e
(set -C; "$PY" "$JOB/proof.py" repin \
  --before "$ATTEMPT/before.json" --after "$ATTEMPT/final.json" \
  --old-script /home/trader/preopen.sh --old-sha256 2ef22340d34ebe5630d140729384409e69c8953485d3ee409e7c5551832f5af3 \
  --snapshot "$ATTEMPT/before-restart.json" --install-record "$ATTEMPT/install-record.json" \
  > "$ATTEMPT/preopen-candidate.json")
candidate_rc=$?
set -e
cat "$ATTEMPT/preopen-candidate.json"
[[ $candidate_rc == 2 ]]
# D2: reviewed truth, PID0 and the existing active-check failure are retained.
run "$PY" "$JOB/actions.py" repin "$ATTEMPT"
run "$PY" "$JOB/actions.py" journal "COMPLETE application=$APPROVED_SHA attempt=$ATTEMPT; see final.json, gate outputs and preopen diff/hash; scanner validation owned by claude-1, live/rotation reads pending"
printf 'COMPLETE application=%s attempt=%s\n' "$APPROVED_SHA" "$ATTEMPT"
