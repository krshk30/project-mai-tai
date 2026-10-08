"""Run over SSH stdin as root; read logs and bounded SQL, write JSON to stdout only."""

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
ET = ZoneInfo("America/New_York")


def sql(query):
    result = subprocess.run(
        ["sudo", "-n", "-u", "postgres", "psql", "-X", "-At", "-d",
         "project_mai_tai", "-c", "BEGIN READ ONLY; SET LOCAL statement_timeout='15s'; "
         + query + "; COMMIT"],
        capture_output=True, text=True, cwd="/tmp", check=True,
    )
    rows = [line for line in result.stdout.splitlines() if line not in {"BEGIN", "SET", "COMMIT"}]
    return json.loads('\n'.join(rows)) if rows else []


events = []
sources = []
tags = ("V2-GAP-HOLD", "V2-GAP-RESUME", "V2-ATR-PROBE", "V2-GAP-DETECT")
stamp = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:[,.](\d+))?")
for path in sorted(glob.glob('/var/log/project-mai-tai/schwab-1m-v2.log*')):
    digest = hashlib.sha256()
    opener = gzip.open if path.endswith('.gz') else open
    with opener(path, 'rb') as stream:
        for number, raw in enumerate(stream, 1):
            digest.update(raw)
            line = raw.decode(errors='replace').rstrip()
            if not any('[' + tag + ']' in line for tag in tags):
                continue
            match = stamp.match(line)
            if not match:
                continue
            when = dt.datetime.strptime(match[1], '%Y-%m-%d %H:%M:%S').replace(tzinfo=UTC)
            if match[2]:
                when += dt.timedelta(microseconds=int(match[2].ljust(6, '0')[:6]))
            tag = next(tag for tag in tags if '[' + tag + ']' in line)
            tail = line.split('[' + tag + ']', 1)[1].strip()
            symbol = tail.split()[0].removeprefix('sym=')
            events.append(dict(ms=int(when.timestamp()*1000), symbol=symbol, tag=tag,
                               line=line, path=path, line_number=number))
    sources.append(dict(path=path, uncompressed_sha256=digest.hexdigest()))
events.sort(key=lambda event: event['ms'])
by_symbol = collections.defaultdict(list)
holds = []
for event in events:
    key = (dt.datetime.fromtimestamp(event['ms']/1000, UTC).date().isoformat(), event['symbol'])
    if event['tag'] == 'V2-GAP-HOLD':
        prior = next((item for item in reversed(by_symbol[key]) if item['tag']=='V2-ATR-PROBE'), None)
        holds.append(dict(hold=event, prior=prior))
    by_symbol[key].append(event)
for case in holds:
    hold = case['hold']
    key = (dt.datetime.fromtimestamp(hold['ms']/1000, UTC).date().isoformat(), hold['symbol'])
    following = [item for item in by_symbol[key] if item['ms'] > hold['ms']]
    case['resume'] = next((item for item in following if item['tag']=='V2-GAP-RESUME'), None)
    case['first_probe'] = next((item for item in following if item['tag']=='V2-ATR-PROBE'), None)
    case['probes_after'] = [item for item in following if item['tag']=='V2-ATR-PROBE']
purple = [case for case in holds if case['prior'] and 'state=short' in case['prior']['line']]
sessions = {}
keys = {(dt.datetime.fromtimestamp(case['hold']['ms']/1000, UTC).astimezone(ET).date().isoformat(),
         case['hold']['symbol']) for case in purple}
keys |= {('2026-09-28','CLRO'), ('2026-09-29','DXST'), ('2026-09-30','TGE'),
         ('2026-10-01','NXL'), ('2026-10-02','AIXI'), ('2026-10-02','AMOD')}
keys |= {(dt.datetime.fromtimestamp(case['hold']['ms']/1000, UTC).astimezone(ET).date().isoformat(),
          case['hold']['symbol']) for case in holds
         if dt.datetime.fromtimestamp(case['hold']['ms']/1000, UTC).astimezone(ET).date().isoformat()
         == '2026-09-24'}
for day, symbol in sorted(keys):
    lo = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(4), ET).astimezone(UTC)
    hi = lo + dt.timedelta(hours=16)
    query = ("SELECT coalesce(json_agg(t ORDER BY t.bar_time),'[]'::json) FROM "
             "(SELECT bar_time,open_price,high_price,low_price,close_price,volume,source,created_at "
             "FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 "
             f"AND symbol='{symbol}' AND bar_time>='{lo.isoformat()}' AND bar_time<'{hi.isoformat()}') t")
    sessions[day + ':' + symbol] = sql(query)
schema = sql("SELECT json_agg(t) FROM (SELECT table_name,column_name FROM information_schema.columns "
             "WHERE table_name IN ('fills','broker_orders','accounts','trade_intents') "
             "ORDER BY table_name,ordinal_position) t")
print(json.dumps(dict(as_of_utc=dt.datetime.now(UTC).isoformat(), sources=sources,
                      hold_count=len(holds), purple_count=len(purple), holds=holds,
                      purple=purple, sessions=sessions, schema=schema), separators=(',',':')))
