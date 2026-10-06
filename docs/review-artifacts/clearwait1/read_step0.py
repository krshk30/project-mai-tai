"""Bounded, read-only CLEARWAIT1 population and durable evidence capture."""

import glob
import gzip
import json
import re
import subprocess
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from project_mai_tai.settings import Settings

START = '2026-09-28 00:00:00'
out = {'read_at': datetime.now(timezone.utc).isoformat(), 'removals': [],
       'case_logs': [], 'files': [], 'queries': {},
       'production_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()}
for path in sorted(glob.glob('/var/log/project-mai-tai/schwab-1m-v2.log*')):
    suffix = path.split('.log')[-1].strip('-').strip('.gz')
    if suffix and suffix < '20260928':
        continue
    out['files'].append(path)
    opener = gzip.open if path.endswith('.gz') else open
    with opener(path, 'rt', errors='replace') as stream:
        for line in stream:
            if line[:19] < START:
                continue
            if ('watchlist_removed_before_position_episode_ended' in line
                    or 'reason=watchlist-removed' in line):
                out['removals'].append({'path': path, 'line': line.strip()})
            if ('2026-10-06 13:25' <= line[:16] < '2026-10-06 14:50'
                    and re.search(r'\b(AIXI|XHG|JAGX)\b', line)
                    and any(x in line for x in ('FLIP-OWNER', 'RESTING', 'QUOTE-WAIT',
                        'ATR-FLIP', 'CW-ARM', 'WATCH', 'RECOVER', 'SEED'))):
                out['case_logs'].append({'path': path, 'line': line.strip()})
symbols = sorted(set(re.findall(r'\] ([A-Z][A-Z0-9.]*) ', '\n'.join(
    x['line'] for x in out['removals']))) | {'AIXI', 'XHG', 'JAGX'})
out['symbols'] = symbols
queries = {
    'orders': "SELECT b.id,b.symbol,b.client_order_id,b.broker_order_id,b.submitted_at,b.updated_at,b.side,b.quantity,b.status,b.order_type,b.payload,s.code strategy,a.name account FROM broker_orders b JOIN strategies s ON s.id=b.strategy_id JOIN broker_accounts a ON a.id=b.broker_account_id WHERE b.symbol=ANY(:symbols) AND b.submitted_at>='2026-09-28T04:00Z' ORDER BY b.submitted_at LIMIT 10000",
    'fills': "SELECT f.id,f.symbol,f.order_id,f.side,f.quantity,f.price,f.filled_at,s.code strategy,a.name account,b.client_order_id FROM fills f JOIN broker_orders b ON b.id=f.order_id JOIN strategies s ON s.id=b.strategy_id JOIN broker_accounts a ON a.id=b.broker_account_id WHERE f.symbol=ANY(:symbols) AND f.filled_at>='2026-09-28T04:00Z' ORDER BY f.filled_at LIMIT 10000",
    'intents': "SELECT t.id,t.symbol,t.created_at,t.updated_at,t.intent_type,t.side,t.status,t.reason,t.quantity,t.payload,s.code strategy,a.name account FROM trade_intents t JOIN broker_accounts a ON a.id=t.broker_account_id JOIN strategies s ON s.id=t.strategy_id WHERE t.symbol=ANY(:symbols) AND t.created_at>='2026-09-28T04:00Z' ORDER BY t.created_at LIMIT 10000",
    'case_order_events': "SELECT e.* FROM broker_order_events e JOIN broker_orders b ON b.id=e.order_id WHERE b.id IN ('8271449a-be0a-4325-9216-c8ead327376b','b8d3c8cc-59da-4086-9c8f-40e1b7ed7567') ORDER BY e.event_at LIMIT 100",
    'managed': "SELECT * FROM oms_managed_positions WHERE symbol=ANY(:symbols) AND entry_time>='2026-09-28T04:00Z' ORDER BY entry_time LIMIT 10000",
    'ownership': "SELECT id,snapshot_type,created_at,payload FROM dashboard_snapshots WHERE snapshot_type IN ('v2_flip_entry_ownership','v2_fanout_segment_identity','v2_retry_one_budget') AND created_at>='2026-09-28T04:00Z' AND payload->>'symbol'=ANY(:symbols) ORDER BY created_at,id LIMIT 20000",
}
def scrub(value):
    if isinstance(value, dict):
        return {k: ('REDACTED' if any(x in k.lower() for x in
            ('token', 'accountnumber', 'accounthash', 'authorization', 'secret'))
            else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value
engine = create_engine(Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env').database_url)
with engine.connect() as conn:
    for name, query in queries.items():
        try:
            conn.execute(text('SET TRANSACTION READ ONLY'))
            conn.execute(text("SET LOCAL statement_timeout='8s'"))
            conn.execute(text("SET LOCAL lock_timeout='500ms'"))
            out['queries'][name] = scrub([dict(row) for row in
                conn.execute(text(query), {'symbols': symbols}).mappings()])
        except Exception as exc:
            out['queries'][name] = {'error': type(exc).__name__ + ':' + str(exc).split('[SQL:')[0]}
        finally:
            conn.rollback()
print(json.dumps(out, default=str, indent=2))
