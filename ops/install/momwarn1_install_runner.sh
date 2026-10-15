#!/usr/bin/env bash
# Preparation only. Main executes an explicitly reviewed future request; workers do not.
set -Eeuo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=/home/trader/project-mai-tai
BASE=860fba4c7d59edff18eb23468ba576dcb83f4d57
SOURCE=32d5a2f7aecbbc4fc7490f05409be1be053f3a9d
MODE=${1:---plan}
if [[ $MODE == --plan ]]; then
  printf '%s\n' \
    'LOCAL PREPARATION ONLY. No future PASS or install authorization.' \
    'Read-only: --rehearse [existing SSH host alias].' \
    'Main only, remote root: --install-approved ROOT600_REVIEWED_REQUEST.json.' \
    'Main only, remote root: --rollback-approved ROOT600_NEW_ROLLBACK_REQUEST.json.' \
    'Exact E860 + warning32d runtime; no F/B; all main M1 gates remain required.' \
    'Gateway only required restart; paper start/restart required if not already warning-ready.' \
    'Optional control restart solely loads warning display, with explicit rationale/approval.' \
    'OMS/strategy/v2 untouched. Native after18ET clock, no inherited overrides.' \
    'Actual A window is04:00-09:30 ET, not04:11; main must acknowledge discrepancy.' \
    'Rollback: warning flag=false and gateway restart only; never broader money-logic rollback.'
  exit 0
fi
if [[ $MODE == --rehearse ]]; then
  ssh -o BatchMode=yes -o ConnectTimeout=10 "${2:-mai-tai-vps}" \
    'sudo -n env PYTHONDONTWRITEBYTECODE=1 python3 -' < "$HERE/momwarn1_readonly_rehearsal.py"
  exit $?
fi
case $MODE in
  --install-approved) ACTION=install ;;
  --rollback-approved) ACTION=rollback ;;
  *) echo 'STOP: unknown mode'; exit 2 ;;
esac
[[ $# == 2 && $(id -u) == 0 && -d $REPO/.git ]] || { echo 'STOP: main-approved remote-root mode only'; exit 2; }
REQUEST=$(realpath "${2:?separately reviewed root600 approval required}")
PY=$REPO/.venv/bin/python
SUPPORT=$HERE/momwarn1_runner_support.py
export PYTHONDONTWRITEBYTECODE=1
export PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=5000'
# Verify authorization before opening any lock, backup or evidence file.
APPROVAL=$($PY "$SUPPORT" approval "$REQUEST" "$ACTION")
field() { printf '%s' "$APPROVAL" | python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
SHA=$(field target_sha)
EVIDENCE=$(field evidence_directory)
exec 9>/var/lock/project-mai-tai-oct12-approved-install.lock
flock -n 9 || { echo 'STOP: existing installer lock held'; exit 2; }
cd "$REPO"
if [[ $ACTION == install ]]; then
  test "$(sudo -n -u trader git rev-parse HEAD)" = "$BASE"
else
  test "$(sudo -n -u trader git rev-parse HEAD)" = "$SHA"
fi
test -z "$(sudo -n -u trader git status --porcelain)"
test ! -e "$EVIDENCE"
install -d -m 0700 "$EVIDENCE"
exec > >(tee -a "$EVIDENCE/runner.log") 2>&1
STAGE=authorized_start
trap 'rc=$?; printf "STOP stage=%s rc=%s at=%s\n" "$STAGE" "$rc" "$(date -u --iso-8601=seconds)" >> "$EVIDENCE/stages.txt"; echo "STOP; no automatic rollback/restart. Main reviews actual stage."; exit "$rc"' ERR
cp --preserve=all "$REQUEST" "$EVIDENCE/approved-request.json"
gates() {
  bash "$REPO/ops/preflight/preflight_oms_restart.sh" --max-age-seconds 30 --require-all-account-positions-flat
  bash "$REPO/ops/preflight/preflight_v2_restart.sh"
  "$PY" "$SUPPORT" flat
}
capture() { "$PY" "$HERE/momwarn1_readonly_rehearsal.py" --observe-seconds 0 > "$1"; }
restart_checked() {
  local unit=$1
  systemctl restart "project-mai-tai-$unit.service"
  sleep 2
  test "$(systemctl show "project-mai-tai-$unit.service" -p ActiveState --value)" = active
  test "$(systemctl show "project-mai-tai-$unit.service" -p SubState --value)" = running
  test "$(systemctl show "project-mai-tai-$unit.service" -p NRestarts --value)" = 0
  systemctl show "project-mai-tai-$unit.service" -p MainPID -p ExecMainStartTimestamp -p ActiveState -p SubState -p NRestarts
}
health() {
  local unit=$1 service=$2 pid started epoch
  pid=$(systemctl show "project-mai-tai-$unit.service" -p MainPID --value)
  started=$(systemctl show "project-mai-tai-$unit.service" -p ExecMainStartTimestamp --value)
  epoch=$(date -d "$started" +%s)
  "$PY" -m project_mai_tai.post_restart_health_gate --health-url http://127.0.0.1:8100/health \
    --service-name "$service" --process-start-epoch "$epoch" --pid "$pid" --expected-sha "$SHA" --sla-seconds 60
}
STAGE=prewrite_gates
gates
capture "$EVIDENCE/before.json"
cp --preserve=all /etc/project-mai-tai/project-mai-tai.env "$EVIDENCE/env-before"
chmod 0600 "$EVIDENCE/env-before"
sha256sum /etc/project-mai-tai/project-mai-tai.env > "$EVIDENCE/env-before.sha256"
STAGE=warning_health_backup
CONFIG=/run/project-mai-tai/momentum-warning-health.json
test ! -L /run/project-mai-tai
test ! -L "$CONFIG"
if [[ -f $CONFIG ]]; then
  cp --preserve=all "$CONFIG" "$EVIDENCE/health-config-before.json"
else
  printf '%s\n' absent > "$EVIDENCE/health-config-before.absent"
fi
if [[ $ACTION == rollback ]]; then
  STAGE=warning_flag_false
  "$PY" "$SUPPORT" set-flag false "$REQUEST"
  STAGE=gateway_rollback_restart
  restart_checked market-data
  health market-data market-data-gateway
  gates
  capture "$EVIDENCE/after.json"
  "$PY" - "$EVIDENCE/after.json" <<'PY'
import json,sys
data=json.load(open(sys.argv[1]))
assert data['units']['market-data']['flags_and_settings']['MAI_TAI_MARKET_DATA_MOMENTUM_WARNING_ENABLED']=='false'
PY
  printf 'A_ONLY_ROLLBACK_RESTART_DONE sha=%s at=%s\n' "$SHA" "$(date -u --iso-8601=seconds)" > "$EVIDENCE/ROLLBACK_RESTART_DONE"
  echo 'Main must verify unchanged T/Q, scanner/v2/OMS, post-baseline logs and bookkeeping; not a live-window PASS.'
  exit 0
fi
STAGE=verify_bundle
BUNDLE=$(field bundle_path)
test "$(sha256sum "$BUNDLE" | cut -d' ' -f1)" = "$(field bundle_sha256)"
sudo -n -u trader git bundle verify "$BUNDLE"
sudo -n -u trader git fetch "$BUNDLE" "$(field bundle_ref)"
test "$(sudo -n -u trader git rev-parse FETCH_HEAD)" = "$SHA"
sudo -n -u trader git merge-base --is-ancestor "$BASE" "$SHA"
sudo -n -u trader git merge-base --is-ancestor "$SOURCE" "$SHA"
# Qualification applies only to frozen32d runtime. Runner/docs-only descendants are allowed.
sudo -n -u trader git diff --exit-code "$SOURCE" "$SHA" -- src tests pyproject.toml uv.lock alembic ops/health/expected_flags.json
STAGE=native_snapshot
snapshot_checked() {
  local attempt rc
  for attempt in 1 2 3; do
    if "$PY" "$REPO/ops/health/v2_restart_evidence.py" snapshot --output "$EVIDENCE/snapshot.json" > "$EVIDENCE/snapshot-attempt-$attempt.txt" 2>&1; then
      cat "$EVIDENCE/snapshot-attempt-$attempt.txt"; return 0
    else
      rc=$?; cat "$EVIDENCE/snapshot-attempt-$attempt.txt"
      if [[ $rc != 2 ]] || ! grep -Eq 'latest v2 watchlist state age=-[0-9.]+s is not fresh' "$EVIDENCE/snapshot-attempt-$attempt.txt"; then return "$rc"; fi
    fi
    sleep 1
  done
  return "$rc"
}
snapshot_checked
STAGE=final_precheckout_gates
gates
STAGE=checkout
sudo -n -u trader git checkout --detach "$SHA"
test "$(sudo -n -u trader git rev-parse HEAD)" = "$SHA"
test -z "$(sudo -n -u trader git status --porcelain)"
STAGE=health_source_configuration
HEALTH_SOURCE=$(field health_config_path)
test "$(sha256sum "$HEALTH_SOURCE" | cut -d' ' -f1)" = "$(field health_config_sha256)"
if [[ ! -d /run/project-mai-tai ]]; then install -d -o trader -g trader -m 0750 /run/project-mai-tai; fi
test ! -e "$CONFIG.momwarn1-new"
test ! -L "$CONFIG.momwarn1-new"
install -o trader -g trader -m 0600 "$HEALTH_SOURCE" "$CONFIG.momwarn1-new"
mv -T "$CONFIG.momwarn1-new" "$CONFIG"
STAGE=warning_flag_true
"$PY" "$SUPPORT" set-flag true "$REQUEST"
SCOPE=$(printf '%s' "$APPROVAL" | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)["restart_scope"]))')
if [[ " $SCOPE " == *' control '* ]]; then
  STAGE=optional_display_restart
  restart_checked control
  health control control-plane
fi
if [[ " $SCOPE " == *' momentum-paper '* ]]; then
  STAGE=paper_consumer_start_or_restart
  restart_checked momentum-paper
  health momentum-paper momentum-paper
fi
STAGE=last_gateway_gates
gates
STAGE=gateway_restart
# systemctl serial stop/start replaces the existing gateway socket; no second gateway is launched.
restart_checked market-data
health market-data market-data-gateway
STAGE=postrestart_receipts
sleep 20
gates
capture "$EVIDENCE/after.json"
"$PY" "$SUPPORT" check-after "$EVIDENCE/before.json" "$EVIDENCE/after.json" "$SHA" "$REQUEST"
"$PY" "$SUPPORT" install-record "$EVIDENCE/snapshot.json" "$REQUEST" > "$EVIDENCE/native-install-record.json"
sha256sum /etc/project-mai-tai/project-mai-tai.env "$CONFIG" > "$EVIDENCE/after-config-hashes.txt"
printf 'RESTARTS_DONE sha=%s at=%s\n' "$SHA" "$(date -u --iso-8601=seconds)" > "$EVIDENCE/RESTARTS_DONE"
echo 'Native snapshot + before/after instance/flag/log-baseline receipts retained.'
echo 'Main post-restart T/Q/scanner/v2/OMS proof and reviewed restart metadata/bookkeeping remain pending.'
echo '04:00-09:30 live A entitlement, health adapter coverage and warning performance NOT proven afterclose.'
