#!/usr/bin/env bash
# Continuation of the completed service sequence. No service/env/schema action.
set -Eeuo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1
JOB=$(cd -- "$(dirname -- "$0")" && pwd)
PY=/home/trader/project-mai-tai/.venv/bin/python
ORIGINAL=/home/trader/after-hours/2026-10-05/owned-entry-rpg-all-on-2045-standing-job/attempt-1730-go
ATTEMPT=$JOB/closeout-2107
[[ $EUID == 0 && $# == 1 ]]
[[ $(sha256sum "$JOB/release.json" | cut -d' ' -f1) == "$1" ]]
"$PY" - "$JOB" <<'PY'
import hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1]);r=json.loads((p/'release.json').read_text())
assert r['approved_sha']=='7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf'
for name,h in r['artifacts'].items():
 assert Path(name).name==name and hashlib.sha256((p/name).read_bytes()).hexdigest()==h,name
print('CLOSEOUT exact artifacts verified; no service actions')
PY
exec 9>/run/lock/project-mai-tai-deploy.lock
flock -n 9
mkdir "$ATTEMPT"
exec > >(tee -a "$ATTEMPT/runner.log") 2>&1
STAGE=read-only-final
abort() {
  local rc=$?
  trap - EXIT
  if ((rc)); then
    systemctl show project-mai-tai-oms.service project-mai-tai-schwab-1m-v2.service project-mai-tai-strategy.service -p Id -p MainPID -p ActiveState -p NRestarts
    "$PY" "$JOB/actions.py" journal "CLOSEOUT STOP stage=$STAGE rc=$rc attempt=$ATTEMPT; no recovery"
    "$PY" "$JOB/actions.py" page "CLOSEOUT STOP stage=$STAGE rc=$rc attempt=$ATTEMPT; no recovery"
  fi
  exit "$rc"
}
trap abort EXIT
run() { printf '\nUTC=%s CALL' "$(date -u --iso-8601=ns)"; printf ' %q' "$@"; printf '\n'; "$@"; }
receipt() { local name=$1; shift; printf 'RECEIPT=%s\n' "$name"; (set -C; "$@" > "$ATTEMPT/$name"); cat "$ATTEMPT/$name"; }
for name in before.json before-restart.json v2-stopped.json; do cp --no-clobber "$ORIGINAL/$name" "$ATTEMPT/$name"; done
cp --no-clobber "$ORIGINAL/before.json" "$ATTEMPT/proof-before.json"
receipt final.json "$PY" "$JOB/proof.py" checkpoint --before "$ATTEMPT/before.json" --phase final
receipt redis-after.json "$PY" "$JOB/proof.py" redis "$ATTEMPT"
receipt bar-continuity.json "$PY" "$JOB/proof.py" bar-holes --stopped "$ATTEMPT/v2-stopped.json" --after "$ATTEMPT/final.json"
receipt proc-four-keys.json "$PY" "$JOB/proof.py" proc --after "$ATTEMPT/final.json"
receipt ticket-dispositions.json "$PY" "$JOB/proof.py" census-after --before "$ATTEMPT/before.json"
STAGE=isolated-catalogs
run "$PY" "$JOB/actions.py" record "$ATTEMPT"
run "$PY" "$JOB/actions.py" catalogs "$ATTEMPT"
CHECKER_SHA=$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["application_blobs"]["ops/health/expected_flags_check.py"])' "$JOB/release.json")
CATALOG_SHA=$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["application_blobs"]["ops/health/expected_flags.json"])' "$JOB/release.json")
NUMERIC_SHA=$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["application_blobs"]["ops/health/expected_numeric.json"])' "$JOB/release.json")
receipt flaggate.json "$PY" "$JOB/proof.py" gates --checker /home/trader/restart_evidence/expected_flags_check.py --checker-sha256 "$CHECKER_SHA" --catalog /home/trader/restart_evidence/expected_flags.json --catalog-sha256 "$CATALOG_SHA" --numeric /home/trader/restart_evidence/expected_numeric.json --numeric-sha256 "$NUMERIC_SHA"
STAGE=single-preopen-repin
receipt preopen-candidate.json "$PY" "$JOB/proof.py" repin --before "$ATTEMPT/before.json" --after "$ATTEMPT/final.json" --old-script /home/trader/preopen.sh --old-sha256 2ef22340d34ebe5630d140729384409e69c8953485d3ee409e7c5551832f5af3 --snapshot "$ATTEMPT/before-restart.json" --install-record "$ATTEMPT/install-record.json"
run "$PY" "$JOB/actions.py" repin "$ATTEMPT"
STAGE=complete
run "$PY" "$JOB/actions.py" journal "COMPLETE application=7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf original_attempt=$ORIGINAL continuation=$ATTEMPT; migration0022; four switches true both PIDs; ticket recovery verified; no startup buys; FLAGGATE147/147 numeric8/8; evictions0 unchanged; bar delivery NOT_APPLICABLE_OFFHOURS, process downtime141.263886s, next-session delivery UNMEASURED; preopen10-06; separately authorized daily guard/paper active; scanner validation owner claude-1. Original STOP retained; no extra trading restart, rollback or ledger write. Mechanics: fractional final-sample drain, recorded local-no-wire identity recovery proof, active-paper census/catalog/pins, after-hours no-bar receipt."
printf 'COMPLETE continuation=%s\n' "$ATTEMPT"
