"""Exact FLYE D28 scope; read-only bounded data, safe projections, no remote writes."""
import datetime as dt
import json
import subprocess

KEYS = {'fanout_segment_id', 'fanout_slot_id', 'fanout_source', 'fanout_leg',
        'fanout_slot', 'fanout_attempt_id', 'stop_price', 'limit_price', 'reference_price',
        'order_type', 'resting_entry', 'cw_entry_slot', 'rpg_resting_generation',
        'webull_mirror_generation_id', 'mirrorhold_id', 'mirrorhold_dispatch_generation',
        'mirrorhold_token', 'webull_deferred_resubmit', 'webull_deferred_resubmit_attempt',
        'webull_error_code', 'webull_local_no_wire', 'webull_wire_submitted_at_utc',
        'webull_shape_market_price', 'webull_shape_market_at_utc', 'webull_shape_market_source'}
limits = {}


def bounded(name, query, limit):
    result = subprocess.run(['sudo', '-n', '-u', 'postgres', 'psql', '-X', '-At', '-d', 'project_mai_tai',
        '-c', "BEGIN READ ONLY; SET LOCAL statement_timeout='15s'; SET LOCAL lock_timeout='2s'; "
        + query + f' LIMIT {limit + 1}) t; ROLLBACK'], capture_output=True, text=True, check=True)
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    limits[name] = {'limit': limit, 'returned': len(rows), 'truncated': len(rows) > limit,
                    'null_strategy_identity': sum(row.get('strategy') is None for row in rows)
                    if name in {'orders', 'intents', 'historical_intents'} else None}
    return rows


def metadata(payload):
    return {k: v for k, v in (payload or {}).items() if k in KEYS}


scope = "a.name='live:orb' AND o.symbol='FLYE' AND o.side='buy' AND (o.submitted_at < '2026-10-08T15:40:00Z' OR o.submitted_at IS NULL)"
orders = bounded('orders', 'SELECT row_to_json(t) FROM (SELECT o.*,a.name AS account,s.code AS strategy '
    'FROM broker_orders o JOIN broker_accounts a ON a.id=o.broker_account_id '
    'LEFT JOIN strategies s ON s.id=o.strategy_id WHERE ' + scope + ' ORDER BY o.submitted_at,o.id', 100)
for order in orders:
    order['payload'] = metadata(order['payload'])
intents = bounded('intents', 'SELECT row_to_json(t) FROM (SELECT i.*,a.name AS account,s.code AS strategy '
    'FROM trade_intents i JOIN broker_accounts a ON a.id=i.broker_account_id '
    "LEFT JOIN strategies s ON s.id=i.strategy_id WHERE a.name='live:orb' AND i.symbol='FLYE' "
    "AND i.created_at >= '2026-10-08T15:15:00Z' AND i.created_at < '2026-10-08T15:40:00Z' ORDER BY i.created_at,i.id", 100)
historical_intents = bounded('historical_intents', 'SELECT row_to_json(t) FROM (SELECT i.*,a.name AS account,s.code AS strategy '
    'FROM trade_intents i JOIN broker_orders o ON o.intent_id=i.id JOIN broker_accounts a ON a.id=o.broker_account_id '
    'LEFT JOIN strategies s ON s.id=i.strategy_id WHERE ' + scope + ' ORDER BY i.created_at,i.id', 100)
for intent in [*intents, *historical_intents]:
    payload = intent['payload']
    intent['payload'] = {k: v for k, v in payload.items()
        if k in {'event_id', 'produced_at', 'source_service', 'refusal_code', 'refusal_origin'}} | {
            'metadata': metadata(payload.get('metadata'))}
audits = bounded('audits', 'SELECT row_to_json(t) FROM (SELECT e.* FROM broker_order_events e '
    'JOIN broker_orders o ON o.id=e.order_id JOIN broker_accounts a ON a.id=o.broker_account_id WHERE '
    + scope + " AND e.event_at < '2026-10-08T15:40:00Z' ORDER BY e.event_at,e.id", 300)
for audit in audits:
    payload = audit['payload']
    audit['payload'] = {k: v for k, v in payload.items() if k in {'client_order_id', 'reason', 'status'}} | {
        'metadata': metadata(payload.get('metadata'))}
fills = bounded('fills', 'SELECT row_to_json(t) FROM (SELECT f.id,f.order_id,f.symbol,f.side,f.quantity,f.price,f.filled_at '
    'FROM fills f JOIN broker_orders o ON o.id=f.order_id JOIN broker_accounts a ON a.id=o.broker_account_id WHERE '
    + scope + " AND f.filled_at < '2026-10-08T15:40:00Z' ORDER BY f.filled_at,f.id", 100)
print(json.dumps({'captured_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    'source': 'mai-tai-vps bounded READ ONLY PostgreSQL; all prior FLYE live:orb buys including NULL timestamps; cutoff 11:40 ET',
    'metadata_projection': sorted(KEYS), 'orders': orders, 'intents': intents, 'audits': audits,
    'fills': fills, 'historical_intents': historical_intents, 'limits': limits,
    'complete': not any(v['truncated'] for v in limits.values())}, indent=2))
