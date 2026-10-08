"""Bounded, index-backed positive tape evidence for the independently pulled holes."""

import json
import re
import subprocess
import sys
from pathlib import Path

pull = json.loads(Path(sys.argv[1]).read_text())
results = []
for case in pull['purple']:
    hold = case['hold']
    if 'last_bar_age_s=' not in hold['line']:
        continue
    left = int(re.search(r'ts_ms=(\d+)', case['prior']['line'])[1]) + 60_000
    right = hold['ms']//60_000*60_000
    if not 0 < right-left <= 3_600_000:
        continue
    symbol = hold['symbol']
    if not re.fullmatch('[A-Z.]+', symbol):
        raise ValueError('invalid symbol')
    rows = []
    for table in ('market_trade_ticks', 'market_capture_trades'):
        sql = ("BEGIN READ ONLY; SET LOCAL statement_timeout='10s'; "
               f"SELECT count(*) FROM {table} WHERE symbol='{symbol}' "
               f"AND event_ts>=to_timestamp({left}/1000.0) "
               f"AND event_ts<to_timestamp({right}/1000.0); COMMIT;")
        receipt = subprocess.run(
            ['ssh','mai-tai-vps','sudo -n -u postgres psql -X -At -d project_mai_tai'],
            input=sql, text=True, capture_output=True, check=True,
        )
        count = next(int(value) for value in receipt.stdout.splitlines() if value.isdigit())
        rows.append(dict(table=table, count=count, query=sql))
    results.append(dict(symbol=symbol, hold_ms=hold['ms'], hole_start_ms=left,
                        hole_end_ms=right, sources=rows))
print(json.dumps(results, indent=2))
