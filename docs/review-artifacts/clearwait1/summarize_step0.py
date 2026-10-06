"""Generate a reviewable receipt; never interpret absent rows as safe clearing."""

import hashlib
import json
import re
import sys

raw = open(sys.argv[1], 'rb').read()
source = json.loads(raw)
queries = source['queries']
limits = {'orders': 10000, 'fills': 10000, 'intents': 10000,
          'managed': 10000, 'ownership': 20000, 'case_order_events': 100}
for name, limit in limits.items():
    assert isinstance(queries[name], list), (name, queries[name])
    assert len(queries[name]) < limit, (name, 'row cap exhausted')

cases = {'AIXI': ('1791294242804', '8271449a-be0a-4325-9216-c8ead327376b'),
         'XHG': ('1791297123018', 'b8d3c8cc-59da-4086-9c8f-40e1b7ed7567')}
case_orders = []
for symbol, (opportunity, order_id) in cases.items():
    row, = [r for r in queries['orders'] if r['id'] == order_id]
    assert row['symbol'] == symbol
    assert row['payload']['fanout_segment_id'] == opportunity
    assert row['broker_order_id']
    assert not any(r['order_id'] == order_id for r in queries['fills'])
    case_orders.append(row)
events = queries['case_order_events']
assert any(r['order_id'] == cases['AIXI'][1]
           and r['event_type'] == 'cancelled' and r['event_source'] == 'broker'
           and r['payload']['metadata']['fanout_segment_id'] == cases['AIXI'][0]
           for r in events)
assert any(r['order_id'] == cases['XHG'][1]
           and r['event_type'] == 'rejected' and r['event_source'] == 'broker'
           for r in events)
jagx, = [r for r in queries['fills'] if r['symbol'] == 'JAGX'
         and r['strategy'] == 'orb_schwab' and r['side'] == 'buy'
         and r['filled_at'].startswith('2026-10-06 13:30:14')]
jagx_owner, = [r for r in queries['ownership']
               if r['snapshot_type'] == 'v2_flip_entry_ownership'
               and r['payload']['symbol'] == 'JAGX'
               and r['payload']['reason'] == 'schwab_first_rest_filled_not_first_rest'
               and r['created_at'].startswith('2026-10-06')]
assert not jagx_owner['payload']['fill_accounts']
assert not jagx_owner['payload']['position_ids']
assert jagx['filled_at'] < jagx_owner['created_at']

sweep = []
for event in sorted(source['removals'], key=lambda r: r['line']):
    match = re.search(r'OWNER-UNKNOWN\] (\w+) opportunity_id=(\d+)', event['line'])
    if not match:
        continue
    symbol, opportunity = match.groups()
    orders = [r for r in queries['orders'] if r['symbol'] == symbol
              and str(r['payload'].get('fanout_segment_id')) == opportunity]
    ids = {r['id'] for r in orders}
    fills = [r for r in queries['fills'] if r['order_id'] in ids]
    sweep.append({'removal': event, 'symbol': symbol, 'opportunity': opportunity,
                  'orders': [{k: r[k] for k in ('id', 'client_order_id',
                    'broker_order_id', 'submitted_at', 'status', 'strategy', 'account')}
                    for r in orders],
                  'fill_ids': [r['id'] for r in fills],
                  'classification': 'filled' if fills else 'submitted' if any(
                      r['broker_order_id'] for r in orders) else 'UNMEASURED'})
assert len(sweep) == 24
assert len({(r['symbol'], r['opportunity']) for r in sweep}) == 16
assert sum(bool(r['orders']) for r in sweep) == 13
assert sum(not r['orders'] for r in sweep) == 11
out = {'verdict': 'DISAGREE', 'blocker_count': 1,
       'base_sha': '3ebde364d4634fdad45992e2ab1cbdf43ffeb221',
       'production_head': source['production_head'], 'read_at': source['read_at'],
       'raw_sha256': hashlib.sha256(raw).hexdigest(),
       'query_counts': {k: len(v) for k, v in queries.items()},
       'case_orders': case_orders, 'case_order_events': events,
       'jagx_foreign_fill': jagx, 'jagx_unknown_owner': jagx_owner,
       'case_owner_snapshots': [r for r in queries['ownership']
           if str(r['payload'].get('opportunity_id')) in {v[0] for v in cases.values()}],
       'case_logs': [r for r in source['case_logs'] if any(t in r['line'] for t in
           ('FIRST-REST-QUOTE-WAIT', 'watchlist_removed_before_position_episode_ended',
            'proven_empty_first_rest_released', 'bar_ts=1791296460000'))],
       'sweep': sweep, 'all_removal_markers': source['removals'],
       'scanned_log_files': source['files']}
print(json.dumps(out, indent=2))
