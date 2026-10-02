"""Second local first-touch implementation using Decimal, independent of scorer."""
import json
import sys
from itertools import chain
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

root = Path(sys.argv[1])
extension = '--extension' in sys.argv
trades = json.loads((root / ('three-ledger.json' if extension else 'result.json.trades.json')).read_text())
by_day = defaultdict(list)
for trade in trades:
    eligible = (any(f['bucket'] != 'unknown' for f in trade['features'].values()) if extension else
                trade['match_reason'] == 'ok' and trade['feature_status'] == 'timestamp_eligible')
    if eligible:
        by_day[(trade['symbol'], trade['day'])].append(trade)
checks = Counter()
extra = {}
if extension:
    with (root / 'three-blind-paths.ndjson').open() as stream:
        for line in stream:
            path = json.loads(line)
            extra[(path['symbol'], path['day'])] = path
with (root / 'paths.ndjson').open() as stream:
    originals = (json.loads(line) for line in stream)
    originals = (p for p in originals if (p['symbol'], p['day']) not in extra)
    for path in chain(originals, extra.values()):
        assert path['complete']
        for trade in by_day[(path['symbol'], path['day'])]:
            start = datetime.fromisoformat(trade['at'])
            start_ms = start.timestamp() * 1000
            end = start.astimezone(ZoneInfo('America/New_York')).replace(
                hour=19, minute=55, second=0, microsecond=0)
            end_ms = end.timestamp() * 1000
            assert path['from_ms'] <= start_ms
            price = Decimal(str(trade['price']))
            target, stop = price * Decimal('1.05'), price * Decimal('.92')
            hits = []
            endpoint = None
            for bar in path['bars']:
                if bar['t'] + 1000 <= start_ms or bar['t'] >= end_ms:
                    continue
                up, down = Decimal(str(bar['h'])) >= target, Decimal(str(bar['l'])) <= stop
                if up or down:
                    label = ('entry_second_ambiguous' if bar['t'] < start_ms else
                             'same_second_ambiguous' if up and down else
                             'target' if up else 'stop')
                    hits.append((bar['t'], label))
                if bar['t'] >= start_ms and bar['t'] + 1000 <= end_ms:
                    endpoint = bar
            if hits:
                label = min(hits)[1]
            elif datetime.fromisoformat(path['asof']) < end:
                label = 'horizon_not_closed'
            elif endpoint is None or end_ms - endpoint['t'] - 1000 > 300000:
                label = 'endpoint_unavailable'
            else:
                label = 'eod'
                value = float((Decimal(str(endpoint['c'])) / price - 1) * 100)
                assert abs(value - trade['uniform']) < 1e-10
            assert label == trade['outcome'], (trade['id'], label, trade['outcome'])
            checks[label] += 1
print(json.dumps({'checked': sum(checks.values()), 'labels': dict(checks), 'mismatches': 0}))
