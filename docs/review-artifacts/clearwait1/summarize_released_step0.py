"""Derive the RELEASED receipt and exact recorded replay inputs."""

import hashlib
import json
import re
import sys
from collections import Counter

raw = open(sys.argv[1], 'rb').read()
source = json.loads(raw)
queries = source['queries']
for name, rows in queries.items():
    assert isinstance(rows, list), (name, rows)
    limit = 20000 if name in {'intents', 'ownership'} else 2000 if name == 'case_order_events' else 1000 if name.endswith('positions') else 10000
    assert len(rows) < limit, (name, 'exhausted cap')
removals = [r for r in source['removals'] if 'OWNER-UNKNOWN' in r['line']]
episodes = set()
counts = Counter()
sweep = []
for event in sorted(removals, key=lambda r: r['line']):
    match = re.search(r'OWNER-UNKNOWN\] (\w+) opportunity_id=(\d+)', event['line'])
    assert match
    symbol, opportunity = match.groups()
    episodes.add((symbol, opportunity))
    orders = [r for r in queries['orders'] if r['symbol'] == symbol
              and str(r['payload'].get('fanout_segment_id')) == opportunity]
    fills = [r for r in queries['fills'] if r['order_id'] in {o['id'] for o in orders}]
    owners = [r for r in queries['ownership'] if r['payload'].get('symbol') == symbol
              and r['payload'].get('opportunity_id') == opportunity]
    filled = fills or any(r['payload'].get('fill_accounts') or r['payload'].get('position_ids') for r in owners)
    kind = 'filled' if filled else 'terminal_unfilled_rows' if orders and all(
        o['status'] in {'cancelled', 'canceled', 'rejected', 'expired'} for o in orders
    ) else 'no_matched_order' if not orders else 'unknown'
    counts[kind] += 1
    sweep.append({'removal': event, 'symbol': symbol, 'opportunity_id': opportunity,
                  'order_ids': [o['id'] for o in orders], 'fill_ids': [f['id'] for f in fills],
                  'retained_class': kind})
assert len(removals) == 63
assert len(episodes) == 43
assert sum(r['line'].startswith('2026-10-06') for r in removals) == 6
assert len({r['line'][:10] for r in removals}) == 11
current = {'AIXI': '1791294242804', 'XHG': '1791297123018', 'AIFA': '1791299283099'}
case_orders = [r for r in queries['orders'] if str(r['payload'].get('fanout_segment_id')) in current.values()]
assert len(case_orders) == 10
assert not any(r['order_id'] in {o['id'] for o in case_orders} for r in queries['fills'])
case_ids = {o['intent_id'] for o in case_orders}
today_intents = [r for r in queries['intents'] if r['symbol'] in current
                 and r['payload'].get('metadata', {}).get('fanout_segment_id') in current.values()]
controls = [r for r in queries['orders'] if r['symbol'] in {'MI', 'JAGX'}
            and r['submitted_at'] >= '2026-10-05' and r['side'] == 'buy']
control_ids = {r['id'] for r in controls}
fixture = {'verdict': 'AGREE', 'scope': 'RELEASED',
           'read_at': source['read_at'], 'production_head': source['production_head'],
           'base_sha': 'c21d8274', 'raw_sha256': hashlib.sha256(raw).hexdigest(),
           'counts': dict(counts), 'query_counts': {k: len(v) for k, v in queries.items()},
           'sweep': sweep, 'scanned_files': source['files'], 'current_opportunities': current,
           'orders': case_orders + controls, 'intents': today_intents + [r for r in queries['intents'] if r['id'] in {o['intent_id'] for o in controls}],
           'fills': [r for r in queries['fills'] if r['order_id'] in control_ids],
           'order_events': [r for r in queries['case_order_events'] if r['order_id'] in {o['id'] for o in case_orders}],
           'owner_snapshots': [r for r in queries['ownership'] if r['payload'].get('symbol') in set(current) | {'MI', 'JAGX'} and r['created_at'] >= '2026-10-05'],
           'managed': [r for r in queries['managed'] if r['symbol'] in set(current) | {'MI', 'JAGX'} and r['entry_time'] >= '2026-10-05'],
           'retry_owners': [r for r in queries['retry_owners'] if r['payload'].get('old', {}).get('symbol') in current and r['created_at'] >= '2026-10-06'],
           'case_logs': [r for r in source['case_logs'] if any(x in r['line'] for x in ('watchlist_removed_before', 'FIRST-REST-QUOTE-WAIT', 'proven_empty_first_rest_released'))]}
print(json.dumps(fixture, indent=2))
