# Monday 2026-10-05 paper guard start

Status: **procedure for independent review, not executed or scheduled**. Owner:
Codex-2, one-shot at **03:40 ET**, verified before **03:50 ET**. Bind the final
APPROVED_SHA to Friday's approved install journal after #1083 is re-pinned and
merged; do not resolve a moving main at execution. No permission carries to
another date. Claude-1 receives completion or the exact blocker.

Paper is already active: its existing code prepares at 03:55 and streams at
04:00. Do not start/restart it. Verify Friday's final paper identity from the
install journal; unexpected inactivity or drift is UNKNOWN. Only start
`project-mai-tai-option-a-guard@2026-10-05.service`. No gateway/live-service
restart, env edit, replay, snapshot bulk read or manual Redis repair.

## Checks and guarded start

Run as root (the environment file is root-readable). Review the literal final
SHA and paper PID/start from Friday's journal before scheduling this block.
The gateway must remain PID 2346625, started 2026-10-01 21:01:04 UTC, NRestarts=0;
an unplanned restart is not permission to re-pin it. Check actual installed
guard/OnFailure unit hashes against the approved Git blobs and the journal.

On any launch/proof error, **stop only momentum-paper, page low priority and do
not restart it**. Do not force-stop the RefuseManualStop guard, reset its failed
Monday instance, retry it, or improvise a new date. The guard's existing
OnFailure emergency path also stops paper and checks owner release; preserve
that audit if it runs. The one-shot's own stop does not claim verified owner
release: record the hash afterwards and page UNKNOWN if release is unproven.

```bash
set -euo pipefail
REPO=/home/trader/project-mai-tai
APPROVED_SHA='<final operator-approved 40-hex install SHA>'
PAPER_PID='<Friday final paper PID>'
PAPER_START='<Friday ExecMainStartTimestamp, exact systemctl text>'
UNIT=project-mai-tai-option-a-guard@2026-10-05.service
RUN=/home/trader/after-hours/2026-10-05/guard-start-0340
OUT=/home/trader/after-hours/2026-10-05/option-a-treatment
test "$(TZ=America/New_York date +%F)" = 2026-10-05
stop_and_page() {
  local rc=$?
  trap - EXIT
  (( rc == 0 )) && return 0
  set +e
  timeout 45s systemctl stop project-mai-tai-momentum-paper.service
  local states message
  states=$(systemctl show project-mai-tai-momentum-paper.service "$UNIT" \
    -p Id -p MainPID -p ActiveState -p SubState -p NRestarts)
  message="Monday guard readiness UNKNOWN rc=$rc; paper-only stop requested; $RUN; $states"
  printf '%s\n' "$message"
  curl -sS --fail-with-body --connect-timeout 10 --max-time 30 \
    -H 'Title: Option A guard readiness UNKNOWN' -H 'Priority: low' \
    -d "$message" https://ntfy.sh/mai-tai-routine-112964cc8f26787132a29538 >/dev/null || \
    printf 'PAGE DELIVERY UNKNOWN: contact operator directly\n'
  exit "$rc"
}
trap stop_and_page EXIT
test "$(TZ=America/New_York date +%H%M)" -lt 0350
[[ "$APPROVED_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$PAPER_PID" =~ ^[1-9][0-9]*$ ]]
test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$APPROVED_SHA"
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
mkdir -p -m 0700 /home/trader/after-hours/2026-10-05
mkdir -m 0700 "$RUN" # O_EXCL directory: no reuse of a previous attempt.
test ! -e "$OUT/option-a-guard.jsonl"
test ! -e "$OUT/option-a-1008.jsonl"
test "$(systemctl show -p MainPID --value "$UNIT")" = 0
for relative in ops/health/option_a_treatment_guard.py scripts/option_a_treatment_1008_sampler.py; do
  expected=$(sudo -u trader git -C "$REPO" show "$APPROVED_SHA:$relative" | sha256sum | awk '{print $1}')
  test "$(sha256sum "$REPO/$relative" | awk '{print $1}')" = "$expected"
  printf '%s %s\n' "$expected" "$relative"
done
for name in project-mai-tai-option-a-guard@.service project-mai-tai-option-a-guard-failure@.service; do
  installed=$(systemctl show -p FragmentPath --value "$name")
  test -n "$installed"
  test "$(sha256sum "$installed" | awk '{print $1}')" = \
    "$(sudo -u trader git -C "$REPO" show "$APPROVED_SHA:ops/systemd/$name" | sha256sum | awk '{print $1}')"
done
test "$(systemctl show -p MainPID --value project-mai-tai-momentum-paper.service)" = "$PAPER_PID"
test "$(systemctl show -p ExecMainStartTimestamp --value project-mai-tai-momentum-paper.service)" = "$PAPER_START"
systemctl is-active --quiet project-mai-tai-momentum-paper.service
# Keep the historical failed result visible in the journal, not as a new incident.
systemctl show project-mai-tai-option-a-guard@2026-10-02.service \
  -p ActiveState -p SubState -p Result -p MainPID -p NRestarts > "$RUN/old-guard-state.txt"
printf 'EXPECTED historical 10-02 guard failure; not reset or restarted\n'
```

Define this read-only proof before the next block. INFO, owner HGETALL and at
most 25 small heartbeat events only; **no snapshot payloads**. Record the first
RedisSafety reading using the exact approved class. It is an external one-shot
reading, **not** a claim that the guard logs its own first sample before 07:00
(it currently does not). Repeat after launch and require no eviction change.

```bash
guard_readiness() {
  "$REPO/.venv/bin/python" - "$REPO" "$RUN" "$1" <<'PY'
import json
import os
import re
import runpy
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from redis import Redis
from project_mai_tai.settings import Settings
repo, root, phase = sys.argv[1:]
def identity():
    raw = subprocess.check_output(['systemctl', 'show', 'project-mai-tai-market-data.service',
        '-p', 'MainPID', '-p', 'ActiveState', '-p', 'SubState', '-p', 'NRestarts',
        '-p', 'ExecMainStartTimestamp'], text=True)
    value = dict(line.split('=', 1) for line in raw.splitlines())
    assert value['MainPID'] == '2346625' and value['NRestarts'] == '0', value
    assert value['ActiveState'] == 'active' and value['SubState'] == 'running', value
    assert value['ExecMainStartTimestamp'] == 'Thu 2026-10-01 21:01:04 UTC', value
    return value
before = identity()
raw_env = Path('/proc/2346625/environ').read_bytes().split(b'\0')
os.environ.clear()
os.environ.update(dict(item.decode().split('=', 1) for item in raw_env if b'=' in item))
s = Settings(_env_file=None)
ns = runpy.run_path(str(Path(repo) / 'ops/health/option_a_treatment_guard.py'))
with Redis.from_url(s.redis_url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5) as r:
    trigger, detail = ns['RedisSafety'](r).sample()
    print('REDIS_SAFETY_ONE_SHOT', phase, json.dumps(detail), 'trigger=', trigger, flush=True)
    assert trigger is None, trigger
    owners = r.hgetall(f'{s.redis_stream_prefix}:market-data-subscription-owners')
    required = {'strategy-engine', 'schwab-1m-v2', 'orb', 'orb-schwab', 'momentum-paper'}
    assert {key for key in owners if not key.startswith('_')} == required, owners
    assert owners.get('_migration_complete') == '1', owners
    assert re.fullmatch(r'\d+-\d+', owners.get('_last_applied_id', '')), owners
    union = set(s.market_data_static_symbol_list)
    for consumer in required:
        symbols = json.loads(owners[consumer])
        assert isinstance(symbols, list) and all(isinstance(x, str) and x for x in symbols)
        assert consumer != 'momentum-paper' or len(symbols) <= 16
        union.update(symbols)
    now = datetime.now(UTC)
    status, age, event = ns['LiveSignals'](r, s.redis_stream_prefix).heartbeat(now)
    now = datetime.now(UTC)
    age = (now - datetime.fromisoformat(event['produced_at'].replace('Z', '+00:00'))).total_seconds()
    assert status == 'healthy' and 0 <= age < 30, (status, age)
    assert int(event['payload']['details']['active_symbols']) == len(union), (event, sorted(union))
    assert identity() == before, 'gateway changed during proof'
    result = {'at_utc': now.isoformat(), 'gateway': before, 'owners': owners,
              'union': sorted(union), 'heartbeat': event, 'heartbeat_age_s': age, 'redis': detail}
    if phase == 'after':
        prior = json.loads((Path(root) / 'before.json').read_text())
        assert prior['redis']['evicted_keys'] == detail['evicted_keys'], 'evictions changed during launch'
    with (Path(root) / (phase + '.json')).open('x') as out:
        json.dump(result, out, sort_keys=True)
    print(json.dumps(result, sort_keys=True))
PY
}
guard_readiness before
START_UTC=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
systemctl start "$UNIT"
sleep 12
systemctl is-active --quiet "$UNIT"
test "$(systemctl show -p NRestarts --value "$UNIT")" = 0
systemctl show "$UNIT" -p MainPID -p ActiveEnterTimestamp -p ExecMainStartTimestamp -p NRestarts \
  | tee "$RUN/new-guard-state.txt"
guard_readiness after
"$REPO/.venv/bin/python" - "$OUT" "$START_UTC" <<'PY'
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
root = Path(sys.argv[1])
parse = lambda text: datetime.fromisoformat(text.replace('Z', '+00:00'))
started = parse(sys.argv[2])
rows = [json.loads(x) for x in (root / 'option-a-guard.jsonl').read_text().splitlines()]
assert len(rows) == 1 and rows[0]['action'] == 'start', rows
assert rows[0]['treatment_date'] == '2026-10-05' and parse(rows[0]['at_utc']) >= started
os.kill(rows[0]['sampler_pid'], 0)
samples = [json.loads(x) for x in (root / 'option-a-1008.jsonl').read_text().splitlines()]
assert len(samples) >= 11 and samples[0]['status'] == 'BASELINE'
assert all(x['status'] == 'OK' and x['new_1008_lines'] == 0 for x in samples[1:])
assert 0 <= (datetime.now(UTC) - parse(samples[-1]['sampled_at_utc'])).total_seconds() < 5
intervals = [(parse(b['sampled_at_utc']) - parse(a['sampled_at_utc'])).total_seconds()
             for a, b in zip(samples[-11:-1], samples[-10:])]
assert all(0.5 <= x <= 1.5 for x in intervals), intervals
print('GUARD_START_VERIFIED', rows[0], 'sampler_rows=', len(samples), 'last10_intervals=', intervals)
print('JSONL_BYTES', {p.name: p.stat().st_size for p in root.glob('*.jsonl')})
PY
systemctl is-active --quiet "$UNIT"
systemctl is-active --quiet project-mai-tai-momentum-paper.service
test "$(systemctl show -p MainPID --value project-mai-tai-momentum-paper.service)" = "$PAPER_PID"
test "$(TZ=America/New_York date +%H%M)" -lt 0350
trap - EXIT
```

Capture stdout/stderr in the one-shot journal as well as the O_EXCL evidence
files. Report final SHA, unit/source hashes, guard PID/start/NRestarts, paper
identity unchanged, healthy gateway age/union, both RedisSafety reads and JSONL
sizes/intervals. A failed stop or undelivered page is reported as UNKNOWN, never
as successful containment. Historical 10-02 failed state is explicitly noted
above; no `reset-failed` is necessary to start the different Monday instance.

## What is guarded

- **Start to 06:54:30:** each second the guard checks sampler continuity/exit,
  new real gateway 1008 signature and RedisSafety (eviction increase or memory
  above 1.6 GB stops paper). Its systemd watchdog/OnFailure covers guard death.
  Continuous heartbeat/latency/OMS slowdown checks are **not active yet**;
  healthy heartbeat and complete owners were launch preconditions only.
- **06:54:30 to 07:00:** those checks continue and bounded COUNT 1 snapshot
  cadence collection begins. No multi-entry read, XINFO or EVAL. The guard's
  own first RedisSafety sample is not emitted before 07:00; do not mistake the
  unchanged start-only guard JSONL mtime for a dead process.
- **07:00 to 09:40:** full preregistered audit: 1 Hz load (3.5 warning only),
  heartbeat, snapshot cadence, v2 lag and OMS quote-refusal rules, plus the
  sampler and Redis stops. Thresholds and protocol are unchanged. Minute
  records include RedisSafety details. Stops affect only paper and page through
  the reviewed guard; no automatic restart afterward.

Codex-2 owns the startup proof and a read-only 06:30 recheck (no forced retry);
claude-1 owns the separate Monday ~07:10 bar-continuity query. This procedure
does not claim early-session full slowdown coverage or live-trading readiness.
