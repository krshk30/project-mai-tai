"""Study-only bounded extraction. Run via SSH stdin under nice/ionice; no DB writes."""
import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from project_mai_tai.settings import Settings

ET = ZoneInfo('America/New_York')
START = datetime(2026, 8, 3, tzinfo=ET)
CUTOFF = datetime(2026, 10, 2, 14, 40, tzinfo=ET)
s = Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env')

def emit(value):
    print(json.dumps(value, default=str), flush=True)

dsn = s.database_url.replace('postgresql+psycopg://', 'postgresql://')
with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5,
        options='-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=1000') as conn:
    with conn.cursor() as c:
        c.execute('SELECT now() AS extracted_at, current_setting(\'transaction_read_only\') AS readonly')
        stamp = c.fetchone()
        c.execute('''SELECT id, strategy_code, broker_account_name AS account, symbol,
            entry_price, original_quantity, entry_time, status, current_quantity, entry_path
            FROM oms_managed_positions WHERE strategy_code='schwab_1m_v2'
            AND broker_account_name IN ('live:schwab_1m_v2','live:orb')
            AND entry_time >= %s AND entry_time <= %s ORDER BY entry_time,id''', (START, CUTOFF))
        positions = c.fetchall()
        c.execute('''SELECT f.id, f.order_id, a.name AS account, f.symbol, f.side,
            f.quantity, f.price, f.filled_at, o.intent_id, o.submitted_at,
            i.created_at AS intent_at, o.order_type
            FROM fills f JOIN strategies st ON st.id=f.strategy_id
            JOIN broker_accounts a ON a.id=f.broker_account_id
            JOIN broker_orders o ON o.id=f.order_id LEFT JOIN trade_intents i ON i.id=o.intent_id
            WHERE st.code='schwab_1m_v2' AND a.name IN ('live:schwab_1m_v2','live:orb')
            AND f.filled_at >= %s AND f.filled_at <= %s ORDER BY f.filled_at,f.id''',
            (START - timedelta(days=3), CUTOFF))
        fills = c.fetchall()
        orders = defaultdict(list)
        for f in fills:
            if f['side'].lower() == 'buy': orders[f['order_id']].append(f)
        buys = []
        for key, group in orders.items():
            qty = sum(x['quantity'] for x in group)
            buys.append(dict(order_id=key, account=group[0]['account'], symbol=group[0]['symbol'],
                time=min(x['filled_at'] for x in group), quantity=qty,
                price=sum(x['quantity']*x['price'] for x in group)/qty,
                intent_at=group[0]['intent_at'], submitted_at=group[0]['submitted_at']))
        emit({'type':'metadata', **stamp, 'cutoff':CUTOFF, 'positions':len(positions), 'fills':len(fills)})
        emit({'type':'fills', 'rows':fills})
        for n, p in enumerate(positions):
            candidates = sorted((b for b in buys if b['account']==p['account'] and b['symbol']==p['symbol']
                and abs((b['time']-p['entry_time']).total_seconds())<=120),
                key=lambda b:abs((b['time']-p['entry_time']).total_seconds()))
            p['buy'] = candidates[0] if candidates else None
            p['match_tie'] = bool(len(candidates)>1 and
                abs((candidates[0]['time']-p['entry_time']).total_seconds()) ==
                abs((candidates[1]['time']-p['entry_time']).total_seconds()))
            if p['buy']:
                b = p['buy']
                p['schwab_twin'] = any(x['account']=='live:schwab_1m_v2' and x['symbol']==p['symbol']
                    and abs((x['time']-b['time']).total_seconds())<=120 for x in buys)
                anchors = {'entry':b['time'], 'placement':b['intent_at'] or b['submitted_at']}
                for name, anchor in anchors.items():
                    if anchor is None:
                        p[name+'_bars']=[]
                        continue
                    c.execute('''SELECT bar_time,open_price,high_price,low_price,close_price,
                        volume,source,created_at,updated_at FROM strategy_bar_history
                        WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 AND symbol=%s
                        AND bar_time < %s - interval '1 minute'
                        ORDER BY bar_time DESC LIMIT 10''', (p['symbol'],anchor))
                    p[name+'_bars']=c.fetchall()
            emit({'type':'position','row':p})
            time.sleep(.03)
        emit({'type':'complete', 'positions':len(positions), 'at':datetime.now(UTC)})
