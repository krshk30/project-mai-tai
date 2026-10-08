"""SSH-stdin study extractor: bounded SQL and log reads, JSON stdout, no live API calls."""

import collections
import datetime as dt
import glob
import gzip
import hashlib
import json
import re
import subprocess
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
ET = ZoneInfo('America/New_York')
DAYS = {'2026-10-02', '2026-10-06', '2026-10-07'}
STAMP = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:[,.](\d+))?')
TAGS = ('WEBULL-ENDPOINT-CALLS', 'BROKER-SYNC-CENSUS', 'BROKER-SYNC-UNREADABLE',
        'BROKER-SYNC-OK', 'OMS-BROKER-SYNC-PASS', 'POSITION-BOOK', 'VIRTUAL-CLEAR',
        'VIRTUAL-RESTORE', 'VIRTUAL-CLEAR-DEFERRED', 'OMS-CANCEL', 'OMS-RESERVE',
        'BARE-FILL', 'PROTECT-ATTACHED', 'OMS-NATIVE', 'OMS-OCO', 'OMS-V2',
        'OMS-ENTRY-OWNERSHIP', 'OMS-FLAT', 'SETTLE-LAG', 'WEBULL-LIST', 'WEBULL-ORDER',
        'WEBULL429', 'WEBULL-CANCEL', 'OMS-P0A-CENSUS', 'OMS-RECONCILE')


def sql(query):
    result = subprocess.run(
        ['sudo','-n','-u','postgres','psql','-X','-At','-d','project_mai_tai','-c',
         "BEGIN READ ONLY; SET LOCAL statement_timeout='20s'; SET LOCAL lock_timeout='500ms'; "
         + query + '; COMMIT'], capture_output=True, text=True, cwd='/tmp', check=True,
    )
    lines = [line for line in result.stdout.splitlines() if line not in {'BEGIN','SET','COMMIT'}]
    return json.loads('\n'.join(lines))


def in_scope(when):
    return when.astimezone(ET).date().isoformat() in DAYS


events = []
failures = []
sources = []
for service in ('oms', 'schwab-1m-v2'):
    for path in sorted(glob.glob('/var/log/project-mai-tai/' + service + '.log*')):
        suffix = path.rsplit('-', 1)[-1].removesuffix('.gz')
        if suffix not in {'20261002','20261003','20261004','20261006','20261007','20261008','20261009'}:
            if not path.endswith('.log'):
                continue
        digest = hashlib.sha256()
        before = collections.deque(maxlen=90)
        last_when = None
        retained = 0
        opener = gzip.open if path.endswith('.gz') else open
        with opener(path, 'rb') as stream:
            for number, raw in enumerate(stream, 1):
                digest.update(raw)
                line = raw.decode(errors='replace').rstrip()
                match = STAMP.match(line)
                if match:
                    last_when = dt.datetime.strptime(match[1], '%Y-%m-%d %H:%M:%S').replace(tzinfo=UTC)
                    if match[2]:
                        last_when += dt.timedelta(microseconds=int(match[2].ljust(6,'0')[:6]))
                counter_minute = re.search(r'\[WEBULL-ENDPOINT-CALLS\] minute=(\S+)', line)
                counter_in_scope = bool(counter_minute and in_scope(
                    dt.datetime.fromisoformat(counter_minute[1])
                ))
                if last_when is not None and (in_scope(last_when) or counter_in_scope):
                    if (any('[' + tag in line for tag in TAGS)
                            or '0e40052d917c' in line and match
                            or 'missing Alpaca credentials for broker account' in line):
                        # Only structured application lines, never SDK request headers/secrets.
                        events.append(dict(at=last_when.isoformat(), service=service, path=path,
                                           line_number=number, line=line))
                        retained += 1
                    if service == 'oms' and 'ServerException:HTTP Status: 417, Code: ORDER_CAN_NOT_BE_CANCEL' in line:
                        context = '\n'.join(before).replace('\\"','"')
                        ids = re.findall(r'"client_order_id"\s*:\s*"([^"]+)"', context)
                        failures.append(dict(at=last_when.isoformat(), path=path, line_number=number,
                                             coid=ids[-1] if ids else None,
                                             request_id=line.rsplit('RequestID:',1)[-1].strip()))
                before.append(line)
        sources.append(dict(path=path, uncompressed_sha256=digest.hexdigest(), retained_lines=retained))

orders_query = """SELECT coalesce(json_agg(t),'[]'::json) FROM (
 SELECT o.id,o.client_order_id,o.broker_order_id,o.symbol,o.side,o.order_type,o.quantity,
 o.status,o.submitted_at,o.updated_at,a.name AS account,s.code AS strategy
 FROM broker_orders o JOIN broker_accounts a ON a.id=o.broker_account_id
 LEFT JOIN strategies s ON s.id=o.strategy_id
 WHERE a.provider='webull' AND o.submitted_at>='2026-09-20T00:00:00Z'
 AND o.submitted_at<'2026-10-08T04:00:00Z' ORDER BY o.submitted_at LIMIT 10000) t"""
orders = sql(orders_query)
if len(orders) >= 10000:
    raise RuntimeError('order bound exhausted')
events_query = """SELECT coalesce(json_agg(t),'[]'::json) FROM (
 SELECT e.order_id,e.event_type,e.event_at,e.event_source,e.payload
 FROM broker_order_events e JOIN broker_orders o ON o.id=e.order_id
 JOIN broker_accounts a ON a.id=o.broker_account_id
 WHERE a.provider='webull' AND e.event_at>='2026-10-02T00:00:00Z'
 AND e.event_at<'2026-10-08T04:00:00Z' ORDER BY e.event_at LIMIT 25000) t"""
order_events = sql(events_query)
if len(order_events) >= 25000:
    raise RuntimeError('event bound exhausted')
fills_query = """SELECT coalesce(json_agg(t),'[]'::json) FROM (
 SELECT f.order_id,f.symbol,f.side,f.quantity,f.price,f.filled_at,a.name AS account
 FROM fills f JOIN broker_accounts a ON a.id=f.broker_account_id
 WHERE a.provider='webull' AND f.filled_at>='2026-10-01T00:00:00Z'
 AND f.filled_at<'2026-10-08T04:00:00Z' ORDER BY f.filled_at LIMIT 10000) t"""
fills = sql(fills_query)
managed_query = """SELECT coalesce(json_agg(t),'[]'::json) FROM (
 SELECT id,broker_account_name,symbol,status,entry_time,created_at,updated_at,current_quantity
 FROM oms_managed_positions WHERE broker_account_name='live:orb'
 AND entry_time>='2026-10-01T00:00:00Z' AND entry_time<'2026-10-08T04:00:00Z'
 ORDER BY entry_time LIMIT 2000) t"""
managed = sql(managed_query)
accounts = sql("SELECT coalesce(json_agg(t),'[]'::json) FROM (SELECT name,provider,is_active "
               "FROM broker_accounts WHERE is_active ORDER BY name) t")
proofs_query = """SELECT coalesce(json_agg(t),'[]'::json) FROM (
 SELECT id,payload->>'client_order_id' AS client_order_id,
 payload->>'acquired_at' AS acquired_at,payload->>'source' AS source,
 payload->'execution' AS execution FROM dashboard_snapshots
 WHERE snapshot_type='webull_terminal_read_proof'
 AND payload->>'acquired_at'>='2026-10-02'
 AND payload->>'acquired_at'<'2026-10-08T04:00:00'
 ORDER BY payload->>'acquired_at' LIMIT 2000) t"""
terminal_proofs = sql(proofs_query)
if len(terminal_proofs) >= 2000:
    raise RuntimeError('terminal proof bound exhausted')
print(json.dumps(dict(as_of_utc=dt.datetime.now(UTC).isoformat(), source_files=sources,
                      log_events=events, cancel_417=failures, orders=orders, order_events=order_events,
                      fills=fills, managed=managed, accounts=accounts,terminal_proofs=terminal_proofs,
                      queries=dict(orders=orders_query,events=events_query,fills=fills_query,
                                   managed=managed_query,terminal_proofs=proofs_query)),
                 separators=(',',':')))
