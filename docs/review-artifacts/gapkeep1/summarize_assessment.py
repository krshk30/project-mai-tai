"""Assessment tables, not a proposed implementation or fill simulation."""
import json
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
data = json.load(open(sys.argv[1]))
replay = json.load(open(sys.argv[2]))

def state(line):
    match = re.search(r'state=(\w+)', line or '')
    return match.group(1) if match else 'UNMEASURED'

print('| Date ET | Symbol / detect ET | Kind (offline census) | Schwab / capture prints | Before (same-day log) | After today (first logged seed) | Continuous oracle at same bar (NOT proven deploy result) |')
print('|---|---|---|---:|---|---|---|')
for h, r in zip([h for h in data['holds'] if h['at'] >= '2026-09-28'], replay['holds']):
    at = datetime.fromisoformat(h['at']).astimezone(ET)
    before = h.get('before') or {}
    before_state = state(before.get('line')) if str(before.get('at', ''))[:10] == h['at'][:10] else 'no same-day reading'
    oracle = r.get('oracle') or {}
    oracle_text = f"{oracle.get('state')} / {oracle.get('trail')}" if oracle else 'UNMEASURED'
    after = h.get('first_after') or {}
    after_state = state(after.get('line')) if str(after.get('at', ''))[:10] == h['at'][:10] else 'UNMEASURED'
    print(f"| {at:%m-%d} | {h['symbol']} {at:%H:%M:%S} | {r['kind']} | {r['schwab_prints']} / {r['capture_prints']} | {before_state} | {after_state} | {oracle_text} |")

for key in ('2026-10-05:APUS', '2026-10-05:MI'):
    print(key, json.dumps([r for r in replay['sessions'][key]['oracle'] if r['flip']]))
    print('today math flips', json.dumps([r for r in replay['sessions'][key]['today_math'] if r['flip']]))
    print('carry math flips', json.dumps([r for r in replay['sessions'][key]['carry_only_math'] if r['flip']]))

ap = replay['sessions']['2026-10-05:APUS']
buy_index = next(i for i, r in enumerate(ap['oracle']) if r['et'] == '10:11')
print('APUS buy neighborhood', json.dumps(ap['oracle'][buy_index-1:buy_index+2]))
print('APUS source bars', json.dumps([b for b in data['bars']['2026-10-05:APUS'] if b['bar_time'].startswith(('2026-10-05 14:11', '2026-10-05 14:12'))]))

selected = [('2026-09-28:CLRO','12:42'), ('2026-09-29:DXST','12:45'),
            *[('2026-09-30:TGE', t) for t in ('11:54','12:48','14:36','15:27')],
            *[('2026-10-01:NXL', t) for t in ('11:42','13:46')],
            *[('2026-10-02:AIXI', t) for t in ('11:05','15:17')],
            *[('2026-10-02:AMOD', t) for t in ('11:22','12:26','15:43')]]
print('ENTRY CHECK (stored-bar replay, not actual log-delivery replay)')
for key, clock in selected:
    series = replay['sessions'].get(key, {})
    o = next((r for r in series.get('oracle', []) if r['et'] == clock), None)
    t = next((r for r in series.get('today_math', []) if o and r['ts'] == o['ts']), None)
    print(key, clock, 'oracle', o, 'today', t)
