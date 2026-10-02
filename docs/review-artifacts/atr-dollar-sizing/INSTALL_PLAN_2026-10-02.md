# 2026-10-02 ATR dollar sizing and guard install plan

Status: prepared for independent review. Execution requires the operator's final
full application SHA and this plan SHA. No production write is authorized by this
document alone. The application SHA is intentionally unfilled until #1082 and
#1083 are pinned and merged. Do not substitute a moving main head.

## Candidate and service scope

- Current verified box HEAD: `fd69004a08c812429b5ba3ac01837c05b132a92b`, clean.
- #1084 is merged at `ed3c84a8e930ea75bf5bddfe1edc45057d8ba26c`.
- #1082 review candidate: `775e348a962ea050a7d621d1e402d04955960297`, four commits
  patch-identical after rebase onto #1084. Pin and merge required.
- #1083 candidate: `8ddac119938e7876c8df0fa456958b516d82b694`; include only its
  independently pinned, merged XRANGE COUNT 1 revision.
  The old Lua/XINFO revision is not eligible. Record its final head and merge SHA.
- Restart **OMS, schwab-1m-v2 and the strategy companion** in the coordinated
  sequence below. The dollar defaults are ON in code; old v2 and new OMS must not
  be left as a completed deployment because old market opens lack the price basis.
- **No gateway restart in this plan.** Gateway PID 2346625 remains on its existing
  in-memory code. Thus #1084 full-state persistence and #1077 gateway logging do
  not become live through this install. The owner repair remains essential.
- No orb or orb-schwab restart, watch reinstall, migration, replay, paper restart,
  or guard start tonight. The existing guard unit points at the checkout, so the
  merged #1083 file and narrowed #1077 sampler are loaded on its next invocation.
  Starting the dated Monday guard and paper requires the separately reviewed
  10-05 start procedure; do not invoke an old treatment date.
- A later gateway restart onto #1077/#1084 needs a separate plan: install and
  hash-verify every narrowed 1008 detector copy first (sampler, fleet-health and
  any installed pager reader), then fresh preserved-owner/content proof. This
  plan does not silently make that later restart safe.

## Preconditions and evidence

Run after the Friday session has closed, after 20:10 ET and the observed log
rotation. Keep an attended operator until all three new process identities and
the final read-only proofs are journaled. If the final GO chooses an earlier
window, review a new plan; do not cross the 20:00 rotation during this sequence.

Record a no-concurrent-deploy check, six service PID/start/NRestarts snapshots
(OMS, strategy, v2, orb, orb-schwab, gateway), paper state, installed guard unit
hashes, old env hash, preopen hash, isolated gate file hashes and git diff paths.
The current preopen hash is
`d58aa3510d16977ad245d98e05e0d0c4dff9fc6db41d58907d22f86e7e46cbd2`.
Any drift needs evidence and review before writing; never silently re-pin it.

Use an O_EXCL evidence directory under `/home/trader/after-hours/2026-10-02/`
and journal `/home/trader/fleet_health/deployments-20261002.md`. Capture outputs
without printing env secrets. Backups are root-owned mode 0600; preserve original
file owner and mode when installing replacements.

### Flatness before every stop/start/restart

Use the existing reviewed helper at
`/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py`, SHA256
`831913206678c5bd2fa6718173c4c5d7b5887c62e44217ad10b6776851f0381e`.
It reads both brokers directly and rejects incomplete/unreadable responses.
For this install, add the following strict wrapper checks to its output: both
open-managed counts zero, `nonzero_virtual=[]`, and `net_bot_fills=[]`.
This removes yesterday's NXL row/net-fill exception. Require Webull holding rows
zero. The existing recorded manual Schwab-holding rule remains allowed only with
zero bot books and fills. Any additional exception requires an operator ruling.

```bash
flat_now() {
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
```

Also query working bot BUY orders on both accounts (pending/submitted/accepted/
partially_filled); any such order blocks. Record armed segments and v2's 15:45
entry-window end from `/proc`. Arms alone are recorded, not a reason to alter
them. No database write or manual position-row closure is allowed.

Before the first write, rc 1/2 means wait and reread within the approved evening;
after any stop/start, rc 1/2 halts remaining operations and pages with actual
service states. A command success alone is not proof of flatness or readiness.

## Literal checkout and env changes

```bash
set -euo pipefail
REPO=/home/trader/project-mai-tai
BOX_SHA=fd69004a08c812429b5ba3ac01837c05b132a92b
APPROVED_SHA='<operator-named final 40-hex main SHA>'
RUN=/home/trader/after-hours/2026-10-02/atr-sizing-install
FLAT_CHECK=/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py
test "$(TZ=America/New_York date +%F)" = 2026-10-02
test "$(TZ=America/New_York date +%H%M)" -ge 2010
[[ "$APPROVED_SHA" =~ ^[0-9a-f]{40}$ ]]
test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$BOX_SHA"
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
sudo mkdir -m 0700 "$RUN" # Refuse if an earlier run already owns this path.
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
flat_now
# Record working BUY orders=0, armed segments, identities and Redis checkpoint here.
sudo cp -p /etc/project-mai-tai/project-mai-tai.env "$RUN/env.before"
sudo cp -p /home/trader/preopen.sh "$RUN/preopen.before"
sudo sha256sum "$RUN/env.before" "$RUN/preopen.before" | sudo tee "$RUN/backups.sha256"
```

Edit only these values in the backed-up env, refusing duplicate definitions or an
unreadable file. If absent, append them; if present, replace that one definition.
Preserve every other byte and log a redacted diff of just these three keys:

```text
MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_NOTIONAL_USD=600
MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_ENTRY_NOTIONAL_USD=300
MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_MAX_SHARES=1000
```

The two notional fields have `require_process_env=true` in expected_numeric.json
and are checked on both v2 and OMS. Explicit max shares pins the same cap.

```bash
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
```

```bash
flat_now
sudo -u trader git -C "$REPO" switch --detach "$APPROVED_SHA"
sudo -u trader "$REPO/.venv/bin/python" -m pip install --no-deps --disable-pip-version-check -e "$REPO"
test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$APPROVED_SHA"
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
sudo -u trader "$REPO/.venv/bin/python" -c 'import pathlib, project_mai_tai; p=pathlib.Path(project_mai_tai.__file__).resolve(); print(p); assert str(p)=="/home/trader/project-mai-tai/src/project_mai_tai/__init__.py"'
```

Runtime refresh only changes the trader-owned editable package/entry-point
metadata. It does not install dependencies or touch service definitions.

## Coordinated service sequence

Do not declare success with one member of the pair still on old code. The exact
sequence, with the working-entry query repeated alongside every `flat_now`, is:

```bash
flat_now
sudo systemctl stop project-mai-tai-schwab-1m-v2.service
flat_now
sudo systemctl stop project-mai-tai-strategy.service
flat_now
sudo systemctl restart project-mai-tai-oms.service
sudo systemctl is-active --quiet project-mai-tai-oms.service
# Verify new OMS PID/start, NRestarts=0, healthy post-start heartbeat and flags below.
flat_now
sudo systemctl start project-mai-tai-schwab-1m-v2.service
sudo systemctl is-active --quiet project-mai-tai-schwab-1m-v2.service
# Verify new v2 PID/start, NRestarts=0 and its loaded dollar settings.
flat_now
redis_strategy_proof before "$RUN/strategy-redis-before.json"
sudo systemctl start project-mai-tai-strategy.service
sudo systemctl is-active --quiet project-mai-tai-strategy.service
STRATEGY_AFTER=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
redis_strategy_proof after "$RUN/strategy-redis-before.json" "$STRATEGY_AFTER"
```

Install an EXIT handler around this attended block that journals actual states of
all three services and pages on any abort. Do not silently start an old/new mixed
pair after an error. No rollback or extra restart is pre-authorized by this plan;
an abort after checkout advance may leave old in-memory code on a new checkout,
and an abort after stopping a unit may leave it stopped. Stay attended, name that
state in the page, and obtain the operator's recovery instruction.

Define this function before running the sequence. `before` writes one new small
metadata file; `after` checks immediately and once per second until initialization
is proven. It never reads snapshot-batches. INFO/HGETALL are small metadata;
the heartbeat call is capped at 25 small events, not snapshot payloads.

```bash
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
```

### Mandatory strategy Redis checkpoint

At preflight and immediately before strategy start, record `INFO stats` evicted_keys,
`INFO memory` used_memory and `HGETALL mai_tai:market-data-subscription-owners`.
Require memory <=1,600,000,000, `_migration_complete=1` and all five consumer fields:
`strategy-engine`, `schwab-1m-v2`, `orb`, `orb-schwab`, `momentum-paper`. Require valid
symbol lists and a valid stream checkpoint; record an empty list as a real owner.
These small metadata reads do not fetch any snapshot batches.

**Immediately after the strategy start**, require evicted_keys unchanged from its
pre-step value, every prior consumer still present, and `_migration_complete=1`.
Repeat each second until a healthy strategy-engine heartbeat produced after its
new start confirms initialization completed, up to 180 seconds; check once more
after it. Heartbeat reads are small events, XREVRANGE COUNT 25 maximum. Record the
new strategy warm-up log evidence. A mere active systemd state is insufficient:
warm-up runs asynchronously after startup and can read roughly 660-720 MB.

Any eviction increase, missing/malformed owner, vanished migration marker, memory
above the bound, unreadable evidence, or warm-up timeout => **UNKNOWN, STOP remaining
install actions and page**. Do not repair Redis or retry a service here. The running
gateway is still pre-#1084 and will not repair a lost full hash itself. Read and
record the six process identities again; any unintended PID change also stops.

## Loaded values and single preopen re-pin

Read `/proc/<new MainPID>/environ` directly, with a second MainPID read proving it
did not move during inspection. On both new v2 and new OMS, paste the three sizing
env values: 600, 300, 1000. Also construct Settings from only that process env with
`_env_file=None` (do not fall back to the disk env), to report loaded/default values.
OMS must retain target 5.0, hard stop 8.0, floor=false, RETRY_ONE=true,
EOD_OCO_TRANSITION=true, OVERNIGHT_FLATTEN=true, ORB live=true and
oms_v2_cw_target_stay_enabled=true. Verify polygon_30s=false on new strategy.

Back up the three isolated flag files and install from the final approved Git blob:

```bash
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
```

Run the numeric-only audit too, and paste every value and its final call:

```bash
sudo "$REPO/.venv/bin/python" - <<'PY' | sudo tee "$RUN/numeric-only.txt"
import runpy
from pathlib import Path
ns = runpy.run_path('/home/trader/restart_evidence/expected_flags_check.py')
entries = ns['load_numeric_catalog'](Path('/home/trader/restart_evidence/expected_numeric.json'))
rc, lines = ns['audit'](entries)
print('\n'.join(lines))
raise SystemExit(rc)
PY
```

Require six numeric readings to match (three settings on two services), with both
notionals sourced from process env; `Final call: PASS; checked=6/6`. This is an
expected result, not evidence already collected. Record an actual UNKNOWN or
mismatch if encountered. Do not label a stopped paper service as healthy.

Make **one** backed-up change to `/home/trader/preopen.sh`: date 2026-10-05, final
application SHA, freshly verified OMS/strategy/v2 PIDs and starts. Retain the exact
orb, orb-schwab and gateway identities because they were not restarted. Keep the
existing snapshot, install-record classifications, watch checks and three-way
routing. Its flag checker automatically loads expected_numeric.json beside it.
Change the dated report output path to 20261005, preserve trader ownership/mode
0700, run `bash -n`, log the entire diff and SHA256. Run the standalone numeric
and flag checks tonight; do not alter the date to make Monday's full gate pass
on Friday. Monday's 06:20 run as trader is to be scheduled only after the operator
accepts the completed install evidence.

```bash
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
```

The old isolated restart checker and untimestamped gateway logs can still yield
UNKNOWN. #1077 merging does not make that proof green; report its actual call.

## Completion evidence and accepted limits

Report exact application/plan SHAs; new OMS/v2/strategy PIDs and start times;
unchanged gateway/orb/orb-schwab/paper identities; env before/after hashes and only
the three-key diff; loaded sizing values on both processes; Redis before/after
numbers and all owner fields; flag/numeric catalogs and checker hashes; numeric
6/6 lines; preopen backup/diff/hash; all raw paths.

Case b, native OCO child activation/scaling after a partial parent, is accepted
**UNEXERCISED** by the operator. Unit partial-fill tests are not live exercise.
PARA1 is cancelled. Dollar sizing starts with the first eligible Monday session;
no artificial orders are submitted to prove it tonight. Installed INC1 watch
coverage, including oms_v2_cw_target_cancel_unconfirmed, is not expanded here.
