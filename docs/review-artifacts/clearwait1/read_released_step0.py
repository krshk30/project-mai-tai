"""RELEASED card: own bounded log census and SQL evidence, without remote writes."""

import glob
import gzip
import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from project_mai_tai.settings import Settings

START = '2026-09-22 00:00:00'
out = {'read_at': datetime.now(timezone.utc).isoformat(), 'removals': [],
       'case_logs': [], 'files': [], 'queries': {}, 'admission_blocks': {},
       'production_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()}
blocks = Counter()
for path in sorted(glob.glob('/var/log/project-mai-tai/schwab-1m-v2.log*')):
    suffix = path.split('.log')[-1].removeprefix('-').removesuffix('.gz')
    if suffix and suffix < '20260922':
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
            match = re.search(r'OWNER-ADMISSION\] (\w+).*reason=owner_phase_unknown', line)
            if match:
                blocks[f'{line[:10]}:{match[1]}'] += 1
            if ('2026-10-06 13:25' <= line[:16] < '2026-10-06 17:30'
                    and re.search(r'\b(AIXI|XHG|AIFA|JAGX|MI)\b', line)
                    and any(x in line for x in ('FLIP-OWNER', 'RESTING', 'QUOTE-WAIT',
                        'ATR-FLIP', 'CW-ARM', 'WATCH', 'RECOVER', 'SEED'))):
                out['case_logs'].append({'path': path, 'line': line.strip()})
out['admission_blocks'] = dict(blocks)
symbols = sorted(set(re.findall(r'\] ([A-Z][A-Z0-9.]*) ', '\n'.join(
    x['line'] for x in out['removals']))) | {'AIXI', 'XHG', 'AIFA', 'JAGX', 'MI'})
out['symbols'] = symbols
window = "'2026-09-22T04:00Z'"
queries = {
    'orders': f"SELECT b.id,b.intent_id,b.symbol,b.client_order_id,b.broker_order_id,b.submitted_at,b.updated_at,b.side,b.quantity,b.status,b.order_type,b.payload,s.code strategy,a.name account FROM broker_orders b JOIN strategies s ON s.id=b.strategy_id JOIN broker_accounts a ON a.id=b.broker_account_id WHERE b.symbol=ANY(:symbols) AND b.submitted_at>={window} ORDER BY b.submitted_at LIMIT 10000",
    'fills': f"SELECT f.id,f.symbol,f.order_id,f.side,f.quantity,f.price,f.filled_at,s.code strategy,a.name account,b.client_order_id FROM fills f JOIN broker_orders b ON b.id=f.order_id JOIN strategies s ON s.id=b.strategy_id JOIN broker_accounts a ON a.id=b.broker_account_id WHERE f.symbol=ANY(:symbols) AND f.filled_at>={window} ORDER BY f.filled_at LIMIT 10000",
    'intents': f"SELECT t.id,t.symbol,t.created_at,t.updated_at,t.intent_type,t.side,t.status,t.reason,t.quantity,t.payload,s.code strategy,a.name account FROM trade_intents t JOIN broker_accounts a ON a.id=t.broker_account_id JOIN strategies s ON s.id=t.strategy_id WHERE t.symbol=ANY(:symbols) AND t.created_at>={window} ORDER BY t.created_at LIMIT 20000",
    'case_order_events': "SELECT e.* FROM broker_order_events e JOIN broker_orders b ON b.id=e.order_id WHERE b.symbol IN ('AIXI','XHG','AIFA','JAGX','MI') AND b.submitted_at>='2026-10-06T04:00Z' ORDER BY e.event_at LIMIT 2000",
    'managed': f"SELECT * FROM oms_managed_positions WHERE symbol=ANY(:symbols) AND (entry_time>={window} OR status='open') ORDER BY entry_time LIMIT 10000",
    'ownership': f"SELECT id,snapshot_type,created_at,payload FROM dashboard_snapshots WHERE snapshot_type IN ('v2_flip_entry_ownership','v2_fanout_segment_identity','v2_retry_one_budget') AND created_at>={window} AND payload->>'symbol'=ANY(:symbols) ORDER BY created_at,id LIMIT 20000",
    'account_positions': "SELECT p.symbol,p.quantity,p.updated_at,a.name account FROM account_positions p JOIN broker_accounts a ON a.id=p.broker_account_id WHERE p.symbol=ANY(:symbols) ORDER BY p.symbol,a.name LIMIT 1000",
    'virtual_positions': "SELECT p.symbol,p.quantity,p.updated_at,s.code strategy,a.name account FROM virtual_positions p JOIN broker_accounts a ON a.id=p.broker_account_id JOIN strategies s ON s.id=p.strategy_id WHERE p.symbol=ANY(:symbols) ORDER BY p.symbol,a.name,s.code LIMIT 1000",
    'retry_owners': f"SELECT id,snapshot_type,created_at,payload FROM dashboard_snapshots WHERE snapshot_type IN ('atr_reprice_handoff','oms_webull_mirror_price_hold') AND created_at>={window} AND (payload->'old'->>'symbol'=ANY(:symbols) OR payload->'event'->'payload'->>'symbol'=ANY(:symbols)) ORDER BY created_at,id LIMIT 10000",
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
