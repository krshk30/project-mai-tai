#!/usr/bin/env bash
# Executable companion of INSTALL_PLAN_2026-10-03.md; no deployment before review.
set -Eeuo pipefail
umask 077
JOB=/home/trader/after-hours/2026-10-03/sizing-nfq1-job
EVIDENCE="$JOB/install_evidence_2026-10-03.py"
test "$(id -u)" = 0
/usr/bin/python3 "$JOB/verify_install_approval.py"
exec 9>/run/lock/project-mai-tai-deploy.lock
flock -n 9 || { printf 'UNKNOWN concurrent deploy lock held\n'; exit 2; }

flat_now() {
  test "$(TZ=America/New_York date +%F)" = 2026-10-03 || return 2
  local out rc=0
  out=$(timeout 45s sudo "$REPO/.venv/bin/python" "$FLAT_CHECK" 2>&1) || rc=$?
  printf '%s\nstrict_flat_rc=%s\n' "$out" "$rc" | sudo tee -a "$RUN/steps.log"
  (( rc == 0 )) || return "$rc"
  printf '%s\n' "$out" | grep -Eq '^FRESH_DIRECT_READ .*webull_holding_rows=0 ' || return 2
  printf '%s\n' "$out" | grep -Fq "open_managed={'live:schwab_1m_v2': 0, 'live:orb': 0}" || return 2
  printf '%s\n' "$out" | grep -Fq 'nonzero_virtual=[]' || return 2
  printf '%s\n' "$out" | grep -Fq 'net_bot_fills=[]' || return 2
  entry_orders_clear
}
entry_orders_clear() {
  sudo "$REPO/.venv/bin/python" - <<'PY'
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from project_mai_tai.db.models import BrokerAccount, BrokerOrder
from project_mai_tai.db.session import build_engine
from project_mai_tai.settings import Settings
s = Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env')
with Session(build_engine(s.database_url, connect_timeout_s=5, statement_timeout_ms=5000)) as db:
    rows = db.execute(select(BrokerAccount.name, BrokerOrder.id, BrokerOrder.symbol, BrokerOrder.status)
        .join(BrokerAccount, BrokerOrder.broker_account_id == BrokerAccount.id)
        .where(BrokerAccount.name.in_(('live:schwab_1m_v2', 'live:orb')),
               func.lower(BrokerOrder.side) == 'buy',
               func.lower(BrokerOrder.status).in_(('pending', 'submitted', 'accepted', 'partially_filled')))).all()
print(f'working_bot_entry_orders={len(rows)} rows={rows}')
raise SystemExit(1 if rows else 0)
PY
}

# Empty for the normal path. Set only to the operator's freshly named exact set.
V2_ARM_OVERRIDE=''
v2_restart_gate() {
  local rc=0
  local args=()
  if [[ -n "$V2_ARM_OVERRIDE" ]]; then
    args=(--operator-override "$V2_ARM_OVERRIDE" --i-accept-bug2)
  fi
  sudo bash "$REPO/ops/preflight/preflight_v2_restart.sh" "${args[@]}" \
    | sudo tee "$RUN/v2-restart-preflight.txt" || rc=$?
  printf 'v2_restart_preflight_rc=%s\n' "$rc" | sudo tee -a "$RUN/steps.log"
  (( rc == 0 )) || return "$rc"
}

v2_post_restart_check() {
  sudo "$REPO/.venv/bin/python" - "$V2_LOG" "$V2_LOG_ID" "$V2_LOG_OFFSET" \
    "$V2_NEW_PID" "$RUN/v2-$1.log" <<'PY'
import subprocess
import sys
from pathlib import Path
path, expected_id, offset, expected_pid, output = sys.argv[1:]
unit = 'project-mai-tai-schwab-1m-v2.service'
def identity():
    raw = subprocess.check_output(['systemctl', 'show', unit, '-p', 'MainPID',
        '-p', 'ActiveState', '-p', 'SubState', '-p', 'NRestarts',
        '-p', 'ExecMainStartTimestamp'], text=True)
    return dict(line.split('=', 1) for line in raw.splitlines())
before = identity()
assert before['ActiveState'] == 'active' and before['SubState'] == 'running', before
assert int(expected_pid) > 0 and before['MainPID'] == expected_pid, before
assert before['NRestarts'] == '0' and before['ExecMainStartTimestamp'], before
p = Path(path)
st = p.stat()
assert f'{st.st_dev}:{st.st_ino}' == expected_id, 'UNKNOWN v2 log rotated'
length = st.st_size - int(offset)
assert 0 <= length <= 10_000_000, 'UNKNOWN v2 log range'
with p.open('rb') as stream:
    stream.seek(int(offset))
    data = stream.read(length)
after = p.stat()
assert (after.st_dev, after.st_ino) == (st.st_dev, st.st_ino) and after.st_size >= st.st_size
assert identity() == before, 'UNKNOWN v2 identity changed during check'
with Path(output).open('xb') as out:
    out.write(data)
text = data.decode('utf-8', errors='replace')
traces = text.count('Traceback (most recent call last):')
holds = [line for line in text.splitlines() if '[V2-BOOT-HOLD]' in line]
warmup = [line for line in text.splitlines() if
          'schwab_v2 warmup feed for ' in line or '[V2-REST-WARMED]' in line]
print(f'V2_POST_RESTART identity={before} tracebacks={traces} bytes={length} path={output}')
print('V2_BOOT_HOLD latest=' + (holds[-1] if holds else 'UNEXERCISED no marker yet'))
print(f'V2_WARMUP_LINES count={len(warmup)}')
print('\n'.join(warmup))
assert traces == 0, 'UNKNOWN until current-process traceback is inspected against code/design'
PY
}

redis_strategy_proof() {
  sudo "$REPO/.venv/bin/python" - "$@" <<'PY'
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from redis import Redis
from project_mai_tai.settings import Settings
s = Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env')
r = Redis.from_url(s.redis_url, decode_responses=True, socket_timeout=5)
required = {'strategy-engine', 'schwab-1m-v2', 'orb', 'orb-schwab', 'momentum-paper'}
mode, path = sys.argv[1:3]
def capture():
    stats, memory = r.info('stats'), r.info('memory')
    owners = r.hgetall(f'{s.redis_stream_prefix}:market-data-subscription-owners')
    consumers = {k for k in owners if not k.startswith('_')}
    if not required <= consumers or owners.get('_migration_complete') != '1':
        raise RuntimeError('owner fields or migration marker missing')
    if not re.fullmatch(r'\d+-\d+', owners.get('_last_applied_id', '')):
        raise RuntimeError('owner checkpoint missing or malformed')
    for name in consumers:
        values = json.loads(owners[name])
        if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
            raise RuntimeError(f'unreadable owner {name}')
    result = {'evicted_keys': int(stats['evicted_keys']), 'used_memory': int(memory['used_memory']),
              'owners': owners, 'at_utc': datetime.now(UTC).isoformat()}
    print(json.dumps(result), flush=True)
    if result['used_memory'] > 1_600_000_000:
        raise RuntimeError('Redis used_memory above 1.6 GB')
    return result
try:
    if mode == 'before':
        current = capture()
        with Path(path).open('x') as out:
            json.dump(current, out)
    elif mode == 'after':
        before = json.loads(Path(path).read_text())
        start = datetime.fromisoformat(sys.argv[3].replace('Z', '+00:00'))
        deadline = time.monotonic() + 180
        while True:
            current = capture()
            if current['evicted_keys'] != before['evicted_keys']:
                raise RuntimeError('Redis eviction count changed during strategy restart')
            prior = {k for k in before['owners'] if not k.startswith('_')}
            if not prior <= current['owners'].keys():
                raise RuntimeError('previous consumer disappeared')
            healthy = False
            for _, fields in r.xrevrange(f'{s.redis_stream_prefix}:heartbeats', count=25):
                event = json.loads(fields['data'])
                if event.get('source_service') == 'strategy-engine':
                    stamp = datetime.fromisoformat(event['produced_at'].replace('Z', '+00:00'))
                    age = (datetime.now(UTC) - stamp).total_seconds()
                    healthy = (stamp > start and 0 <= age < 30 and event['payload']['status'] == 'healthy')
                    break
            if healthy:
                final = capture()
                if final['evicted_keys'] != before['evicted_keys']:
                    raise RuntimeError('Redis eviction count changed at final read')
                print('STRATEGY_REDIS_PROOF PASS eviction_unchanged=1 all_consumers=1 migration_complete=1')
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('new healthy strategy heartbeat not proven within 180 seconds')
            time.sleep(1)
    else:
        raise RuntimeError('unknown Redis proof mode')
except Exception as exc:
    print(f'UNKNOWN strategy Redis proof: {type(exc).__name__}: {exc}', flush=True)
    raise SystemExit(2)
finally:
    r.close()
PY
}

prewrite_flat() {
  local rc
  while true; do
    rc=0
    flat_now || rc=$?
    (( rc == 0 )) && return 0
    if (( rc != 1 && rc != 2 )); then return "$rc"; fi
    printf 'PREWRITE_WAIT flatness=%s no checkout/env/service change; reread in 300s\n' "$rc"
    sleep 300
    test "$(TZ=America/New_York date +%F)" = 2026-10-03 || return 2
    local now_et
    now_et=$(TZ=America/New_York date +%H%M)
    (( 10#$now_et < 1955 || 10#$now_et >= 2010 )) || return 2
  done
}

set -euo pipefail
REPO=/home/trader/project-mai-tai
BOX_SHA=fd69004a08c812429b5ba3ac01837c05b132a92b
APPROVED_SHA=608339894a1cfb33284e695196df55c18f312889
RUN=/home/trader/after-hours/2026-10-03/sizing-nfq1-install
FLAT_CHECK=/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py
test "$(TZ=America/New_York date +%F)" = 2026-10-03
NOW_ET=$(TZ=America/New_York date +%H%M)
(( 10#$NOW_ET < 1955 || 10#$NOW_ET >= 2010 ))
[[ "$APPROVED_SHA" =~ ^[0-9a-f]{40}$ ]]
test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$BOX_SHA"
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
sudo mkdir -m 0700 "$RUN" # O_EXCL attempt: no second automatic run.
exec > >(tee -a "$RUN/steps.log") 2>&1
cp "$JOB/release.json" "$RUN/release.json"
install_abort() {
  local rc=$?
  trap - EXIT
  (( rc == 0 )) && return 0
  set +e
  local states message
  states=$(sudo systemctl show -p Id -p MainPID -p ActiveState -p SubState -p NRestarts \
    project-mai-tai-oms.service project-mai-tai-strategy.service project-mai-tai-schwab-1m-v2.service 2>&1)
  message="Sizing install stopped rc=$rc; no automatic recovery attempted; $RUN; $states"
  printf '%s\n' "$message" | sudo tee -a "$RUN/steps.log"
  curl -sS --fail-with-body --connect-timeout 10 --max-time 30 \
    -H 'Title: ATR sizing install needs operator' -H 'Priority: urgent' \
    -d "$message" https://ntfy.sh/mai-tai-preopen-28806a5a97b7 >/dev/null || \
    printf 'PAGE DELIVERY UNKNOWN\n' | sudo tee -a "$RUN/steps.log"
  "$REPO/.venv/bin/python" "$EVIDENCE" abort || true
  exit "$rc"
}
trap install_abort EXIT
sudo -u trader git -C "$REPO" fetch origin main:refs/remotes/origin/main
sudo -u trader git -C "$REPO" merge-base --is-ancestor "$BOX_SHA" "$APPROVED_SHA"
sudo -u trader git -C "$REPO" merge-base --is-ancestor "$APPROVED_SHA" origin/main
sudo -u trader git -C "$REPO" diff --name-only "$BOX_SHA" "$APPROVED_SHA" | sudo tee "$RUN/source-paths.txt"
sudo -u trader git -C "$REPO" diff --name-only "$APPROVED_SHA" origin/main | while IFS= read -r p; do
  case "$p" in docs/*) ;; *) printf 'REFUSE unapproved later code %s\n' "$p"; exit 1 ;; esac
done
test "$(sudo sha256sum "$FLAT_CHECK" | awk '{print $1}')" = 831913206678c5bd2fa6718173c4c5d7b5887c62e44217ad10b6776851f0381e
"$REPO/.venv/bin/python" "$EVIDENCE" preflight
redis_strategy_proof before "$RUN/redis-preflight.json"
prewrite_flat
# Entry orders and both books must be clear before env or checkout writes.
sudo cp -p /etc/project-mai-tai/project-mai-tai.env "$RUN/env.before"
sudo cp -p /home/trader/preopen.sh "$RUN/preopen.before"
sudo sha256sum "$RUN/env.before" "$RUN/preopen.before" | sudo tee "$RUN/backups.sha256"

sudo "$REPO/.venv/bin/python" - <<'PY'
import os
import re
from pathlib import Path
p = Path('/etc/project-mai-tai/project-mai-tai.env')
before = p.read_text()
updated = before
changes = {
    'MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_NOTIONAL_USD': '600',
    'MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_ENTRY_NOTIONAL_USD': '300',
    'MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_MAX_SHARES': '1000',
    'MAI_TAI_OMS_V2_WEBULL_MIRROR_FRESH_PRICE_ENABLED': 'true',
    'MAI_TAI_OMS_V2_WEBULL_MIRROR_QUOTE_MAX_AGE_MS': '10000',
}
for key, value in changes.items():
    pattern = re.compile(r'^(?:export[ \t]+)?' + re.escape(key) + r'=.*$', re.M | re.I)
    found = pattern.findall(updated)
    if len(found) > 1:
        raise SystemExit(f'UNKNOWN duplicate env key {key}')
    old = found[0].split('=', 1)[1] if found else '<unset>'
    if found:
        updated = pattern.sub(key + '=' + value, updated)
    else:
        updated += ('' if updated.endswith('\n') else '\n') + key + '=' + value + '\n'
    print(f'{key}: {old} -> {value}')
st = p.stat()
tmp = p.with_name(p.name + '.atr-sizing.tmp')
fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, st.st_mode & 0o777)
with os.fdopen(fd, 'w') as out:
    out.write(updated)
    out.flush()
    os.fsync(out.fileno())
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, p)
PY

sudo sha256sum /etc/project-mai-tai/project-mai-tai.env | sudo tee "$RUN/env-after.sha256"
flat_now
sudo -u trader git -C "$REPO" switch --detach "$APPROVED_SHA"
sudo -u trader "$REPO/.venv/bin/python" -m pip install --no-deps --disable-pip-version-check -e "$REPO"
test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$APPROVED_SHA"
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
sudo -u trader "$REPO/.venv/bin/python" -c 'import pathlib, project_mai_tai; p=pathlib.Path(project_mai_tai.__file__).resolve(); print(p); assert str(p)=="/home/trader/project-mai-tai/src/project_mai_tai/__init__.py"'

flat_now
v2_restart_gate
sudo systemctl stop project-mai-tai-schwab-1m-v2.service
# Old v2 has exited: the following cursor cannot include its historical output.
V2_LOG=/var/log/project-mai-tai/schwab-1m-v2.log
V2_LOG_ID=$(sudo stat -c '%d:%i' "$V2_LOG")
V2_LOG_OFFSET=$(sudo stat -c '%s' "$V2_LOG")
flat_now
sudo systemctl stop project-mai-tai-strategy.service
flat_now
sudo systemctl restart project-mai-tai-oms.service
sudo systemctl is-active --quiet project-mai-tai-oms.service
"$REPO/.venv/bin/python" "$EVIDENCE" oms-health
flat_now
sudo systemctl start project-mai-tai-schwab-1m-v2.service
sudo systemctl is-active --quiet project-mai-tai-schwab-1m-v2.service
V2_NEW_PID=$(sudo systemctl show -p MainPID --value project-mai-tai-schwab-1m-v2.service)
v2_post_restart_check startup
# Verify loaded dollar settings below; repeat the log/identity proof at close-out.
flat_now
redis_strategy_proof before "$RUN/strategy-redis-before.json"
"$REPO/.venv/bin/python" - "$RUN" <<'PY'
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
before = json.loads((root / 'redis-preflight.json').read_text())
now = json.loads((root / 'strategy-redis-before.json').read_text())
assert now['evicted_keys'] == before['evicted_keys'], 'Redis evictions increased before strategy start'
PY
sudo systemctl start project-mai-tai-strategy.service
sudo systemctl is-active --quiet project-mai-tai-strategy.service
STRATEGY_AFTER=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
redis_strategy_proof after "$RUN/strategy-redis-before.json" "$STRATEGY_AFTER"
v2_post_restart_check closeout

"$REPO/.venv/bin/python" "$EVIDENCE" identities
"$REPO/.venv/bin/python" "$EVIDENCE" loaded
# Check both checkout detector copies before accepting the disk advance.
for relative in ops/health/fleet_health_check.py scripts/option_a_treatment_1008_sampler.py; do
  expected=$(sudo -u trader git -C "$REPO" show "$APPROVED_SHA:$relative" | sha256sum | awk '{print $1}')
  test "$(sha256sum "$REPO/$relative" | awk '{print $1}')" = "$expected"
  printf '%s %s\n' "$expected" "$REPO/$relative" | tee -a "$RUN/detectors.sha256"
done
test "$(sha256sum /home/trader/unexercised_watch/watch.py | awk '{print $1}')" = 45df60c60fe55af89406d20c29de277a44634d6bc6308586a4e0b62e718b8272
for name in expected_flags_check.py expected_flags.json expected_numeric.json; do
  source="$REPO/ops/health/$name"
  target="/home/trader/restart_evidence/$name"
  if sudo test -e "$target"; then sudo cp -p "$target" "$RUN/$name.before"; fi
  sudo install -o root -g root -m 0644 "$source" "$target"
  test "$(sudo -u trader git -C "$REPO" show "$APPROVED_SHA:ops/health/$name" | sha256sum | awk '{print $1}')" = "$(sudo sha256sum "$target" | awk '{print $1}')"
  sudo sha256sum "$target" | sudo tee -a "$RUN/installed.sha256"
done
sudo "$REPO/.venv/bin/python" /home/trader/restart_evidence/expected_flags_check.py \
  --catalog /home/trader/restart_evidence/expected_flags.json \
  --numeric-catalog /home/trader/restart_evidence/expected_numeric.json \
  | sudo tee "$RUN/flags-and-numeric.txt"

sudo "$REPO/.venv/bin/python" - <<'PY' | sudo tee "$RUN/numeric-only.txt"
import runpy
from pathlib import Path
ns = runpy.run_path('/home/trader/restart_evidence/expected_flags_check.py')
entries = ns['load_numeric_catalog'](Path('/home/trader/restart_evidence/expected_numeric.json'))
rc, lines = ns['audit'](entries)
print('\n'.join(lines))
raise SystemExit(rc)
PY

grep -Fq 'Final call: PASS; checked=7/7' "$RUN/numeric-only.txt"
sudo "$REPO/.venv/bin/python" - "$APPROVED_SHA" <<'PY'
import hashlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
p = Path('/home/trader/preopen.sh')
original = p.read_bytes()
if hashlib.sha256(original).hexdigest() != 'd58aa3510d16977ad245d98e05e0d0c4dff9fc6db41d58907d22f86e7e46cbd2':
    raise SystemExit('UNKNOWN preopen drift before single repin')
text = original.decode()
def old(name):
    found = re.findall(r'^' + name + r'=(.*)$', text, re.M)
    if len(found) != 1:
        raise SystemExit(f'UNKNOWN preopen key {name}')
    return shlex.split(found[0])[0]
changes = {'EXPECTED_DATE': '2026-10-05', 'EXPECTED_SHA': sys.argv[1],
           'REPORT': '/home/trader/known_defect_regression_watch/v2-restart-evidence-20261005.md'}
for service, pid_key, start_key, restarted in (
    ('oms', 'EXPECTED_OMS_PID', 'EXPECTED_OMS_START', True),
    ('strategy', 'EXPECTED_STRATEGY_PID', 'EXPECTED_STRATEGY_START', True),
    ('schwab-1m-v2', 'EXPECTED_PID', 'EXPECTED_START', True),
    ('orb', 'EXPECTED_ORB_PID', 'EXPECTED_ORB_START', False),
    ('orb-schwab', 'EXPECTED_ORB_SCHWAB_PID', 'EXPECTED_ORB_SCHWAB_START', False),
    ('market-data', 'EXPECTED_MARKET_DATA_PID', 'EXPECTED_MARKET_DATA_START', False),
):
    unit = f'project-mai-tai-{service}.service'
    output = subprocess.check_output(['systemctl', 'show', unit, '-p', 'MainPID', '-p',
        'ExecMainStartTimestamp', '-p', 'ActiveState', '-p', 'SubState', '-p', 'NRestarts'], text=True)
    values = dict(line.split('=', 1) for line in output.splitlines())
    pid, start = values['MainPID'], values['ExecMainStartTimestamp']
    if (int(pid) <= 0 or not start or values['ActiveState'] != 'active' or
            values['SubState'] != 'running' or values['NRestarts'] != '0'):
        raise SystemExit(f'UNKNOWN {service} identity {values}')
    if restarted:
        if pid == old(pid_key) or start == old(start_key):
            raise SystemExit(f'UNKNOWN {service} restart identity did not change')
        changes[pid_key], changes[start_key] = pid, start
    elif pid != old(pid_key) or start != old(start_key):
        raise SystemExit(f'UNKNOWN unintended {service} identity drift')
for key, value in changes.items():
    text, count = re.subn(r'^' + key + r'=.*$', key + '=' + shlex.quote(value), text, flags=re.M)
    if count != 1:
        raise SystemExit(f'UNKNOWN preopen replacement {key}')
text = text.replace('# Fail-closed pre-open gate for 2026-10-02.',
                    '# Fail-closed pre-open gate for 2026-10-05.', 1)
tmp = p.with_name('preopen.20261005.pending')
st = p.stat()
with tmp.open('x') as out:
    out.write(text)
subprocess.run(['bash', '-n', str(tmp)], check=True)
os.chown(tmp, st.st_uid, st.st_gid)
os.chmod(tmp, 0o700)
os.replace(tmp, p)
print('PREOPEN_REPIN date=2026-10-05 sha256=' + hashlib.sha256(p.read_bytes()).hexdigest())
PY
sudo diff -u "$RUN/preopen.before" /home/trader/preopen.sh | sudo tee "$RUN/preopen.diff" || test "${PIPESTATUS[0]}" = 1
sudo sha256sum /home/trader/preopen.sh | sudo tee "$RUN/preopen.sha256"

test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$APPROVED_SHA"
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
# Verify again without replacing the earlier evidence file.
"$REPO/.venv/bin/python" - "$EVIDENCE" <<'PY'
import json
import runpy
ns = runpy.run_path(__import__('sys').argv[1])
now = ns['identities']()
ns['verify_identities'](now, after=True)
assert now == json.loads((ns['ROOT'] / 'final-identities.json').read_text()), 'identity changed at close-out'
print('FINAL_IDENTITIES_STABLE')
PY
"$REPO/.venv/bin/python" "$EVIDENCE" complete
printf 'INSTALL_COMPLETE application=%s journal=/home/trader/fleet_health/deployments-20261003.md\n' "$APPROVED_SHA"
trap - EXIT
