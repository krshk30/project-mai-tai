"""Read-only measurements, then one exclusive receipt and journal append."""
import hashlib
import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path('/home/trader/option-a-daily-guard')
OUTPUT = Path('/home/trader/after-hours/2026-10-06/option-a-daily/run-20261006T011059600358Z')
spec = importlib.util.spec_from_file_location('installed_daily', ROOT/'daily_guard.py')
daily = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily)
daily.verify_manifest()
guard = daily.load_guard()
from redis import Redis
from project_mai_tai.settings import Settings

now = datetime.now(UTC)
start = datetime(2026, 10, 6, 1, 11, 1, tzinfo=UTC)
assert (now-start).total_seconds() >= 901, 'first15minutes not yet completed'
states = {name: daily.state('project-mai-tai-'+name+'.service') for name in ('market-data','momentum-paper','option-a-daily-guard')}
for name, pid in [('market-data','2907'), ('momentum-paper','366242'), ('option-a-daily-guard','366236')]:
    assert states[name]['MainPID']==pid and states[name]['ActiveState']=='active' and states[name]['NRestarts']=='0', states[name]
path = OUTPUT/'option-a-1008.jsonl'
assert path.stat().st_size < 10_000_000
rows = [json.loads(line) for line in path.open()]
assert rows[0]['status']=='BASELINE' and all(r['status']=='OK' for r in rows[1:])
assert sum(r['new_1008_lines'] for r in rows)==0
times = [datetime.fromisoformat(r['sampled_at_utc']) for r in rows]
assert (times[-1]-times[0]).total_seconds()>=900
intervals = [(b-a).total_seconds() for a,b in zip(times,times[1:])]
assert all(.5<=x<=1.5 for x in intervals)
assert 0 <= (now-times[-1]).total_seconds() <= 5
audit = [json.loads(line) for line in (OUTPUT/'option-a-guard.jsonl').open()]
assert not any(r.get('action') in {'stop','blind','stop_paper'} for r in audit)
settings = Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env')
client = Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
try:
    trigger, safety = guard.RedisSafety(client).sample()
    assert not trigger and safety['evicted_keys']==0
    owners = guard._owners(client, settings.redis_stream_prefix)
    assert set(owners)=={'strategy-engine','schwab-1m-v2','orb','orb-schwab','momentum-paper'} and len(owners['momentum-paper'])<=16
    status, age, heartbeat = guard.LiveSignals(client, settings.redis_stream_prefix).heartbeat(now)
    assert status=='healthy' and age<=guard.HEARTBEAT_AGE_BOUND
    union = set().union(*owners.values())
    assert int(heartbeat['payload']['details']['active_symbols'])==len(union)
finally:
    client.close()
result = {'as_of_utc': now.isoformat(), 'verdict': 'PASS', 'observed_seconds': (times[-1]-times[0]).total_seconds(),
          'states': states, 'sampler_path': str(path), 'sampler_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
          'rows': len(rows), 'interval_min': min(intervals), 'interval_max': max(intervals), 'new_1008_lines': 0,
          'redis': safety, 'owners': {k: sorted(v) for k,v in owners.items()}, 'active_symbols': len(union), 'gateway_heartbeat_age_s': age,
          'load_warning_only_unchanged': list(os.getloadavg()), 'window_end_et': audit[0]['end_et']}
text = json.dumps(result, indent=2)+'\n'
with os.fdopen(os.open(OUTPUT/'first-15-minutes.json', os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), 'w') as f:
    f.write(text)
    f.flush()
    os.fsync(f.fileno())
print(text)
