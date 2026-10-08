"""Independent state replay; accepts the read-only raw pull, never contacts a broker."""

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
from project_mai_tai.strategy_core.session_line_restore import (
    HistoryBar, RebuildInput, build_session_line,
)

ET = ZoneInfo('America/New_York')


def ms(value):
    return int(dt.datetime.fromisoformat(value).timestamp()*1000)


def fields(line):
    return dict(re.findall(r'([\w]+)=([^\s]+)', line))


def session(pull, day, symbol):
    return [Bar(ms(row['bar_time']), *(float(row[key]) for key in
                ('open_price','high_price','low_price','close_price')), int(row['volume']))
            for row in pull['sessions'][day + ':' + symbol]]


def replay(symbol, bars, current):
    prefix = [bar for bar in bars if bar.ts <= current]
    if not prefix or prefix[-1].ts != current:
        return None
    history = tuple(HistoryBar(symbol, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.ts)
                    for bar in prefix)
    pairs = tuple((left.timestamp_ms, right.timestamp_ms) for left, right in zip(history, history[1:])
                  if right.timestamp_ms-left.timestamp_ms > 90_000)
    request = RebuildInput(symbol, 1, 1, history[0].timestamp_ms, current, history, pairs)
    result = dict(build_session_line(request, 5, 3.5).indicator)
    oracle = compute_atr_trail(prefix)[-1]
    return dict(code_state=result['atr_state'], code_trail=result['atr_trail'],
                oracle_state=oracle['state'], oracle_trail=oracle['trail'],
                matches=result['atr_state']==oracle['state']
                and (result['atr_trail']==oracle['trail'] or
                     result['atr_trail'] is not None and oracle['trail'] is not None
                     and abs(result['atr_trail']-oracle['trail'])<=0.000051))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('raw')
    args = parser.parse_args()
    pull = json.loads(Path(args.raw).read_text())
    primary = [case for case in pull['holds'] if 'last_bar_age_s=' in case['hold']['line']]
    # Do not equate wall-clock probe times with bar times or provider coverage.
    candidates = []
    for case in primary:
        hold = case['hold']
        day = dt.datetime.fromtimestamp(hold['ms']/1000, dt.UTC).astimezone(ET).date().isoformat()
        prior = [probe for other in primary if other['hold']['symbol']==hold['symbol']
                 for probe in other['probes_after']
                 if probe['ms']//1000 < hold['ms']//1000
                 and dt.datetime.fromtimestamp(probe['ms']/1000, dt.UTC).astimezone(ET).date().isoformat()==day]
        # The raw pull also records the immediately preceding probe directly.
        if case['prior'] and case['prior']['ms']//1000 < hold['ms']//1000:
            prior.append(case['prior'])
        latest = max(prior, key=lambda item:item['ms']) if prior else None
        if latest and fields(latest['line']).get('state')=='short':
            candidates.append(case)
    table = []
    for case in candidates:
        hold = case['hold']
        when = dt.datetime.fromtimestamp(hold['ms']/1000, dt.UTC).astimezone(ET)
        bars = session(pull, when.date().isoformat(), hold['symbol'])
        resume = case['resume']
        if not bars or not any(bar.ts > hold['ms'] for bar in bars):
            table.append(dict(symbol=hold['symbol'], hold=when.isoformat(), status='no_post_hold_bars'))
            continue
        resumed_bar = resume['ms']//60_000*60_000-60_000 if resume else None
        after = [bar for bar in bars if bar.ts > hold['ms']]
        current = resumed_bar if resumed_bar is not None else after[0].ts
        values = replay(hold['symbol'], bars, current)
        last_ms = int(fields(case['prior']['line'])['ts_ms'])
        missing_ids = list(range(last_ms+60_000, hold['ms']//60_000*60_000, 60_000))
        by_ts = {bar.ts for bar in bars}
        oracle = compute_atr_trail(bars)
        first_buy = next((row for row in oracle if row['ts'] > hold['ms'] and row['flip']=='BUY'), None)
        seen = bool(first_buy and any(fields(probe['line']).get('flip')=='BUY'
                    and int(fields(probe['line']).get('ts_ms','0'))==first_buy['ts']
                    for probe in case['probes_after']))
        table.append(dict(symbol=hold['symbol'], hold=when.isoformat(),
                          resume=resume['line'] if resume else None,
                          evaluated_bar_ms=current, replay=values,
                          missing_minutes=len(missing_ids), filled_minutes=sum(ts in by_ts for ts in missing_ids),
                          first_buy=first_buy, bot_saw_first_buy=seen,
                          hold_source=hold['path']+':'+str(hold['line_number'])))
    controls = json.loads(Path('tests/fixtures/line_chart_recorded_entry_controls.json').read_text())['controls']
    entries = []
    for control in controls:
        day = dt.datetime.fromtimestamp(control['timestamp_ms']/1000, dt.UTC).astimezone(ET).date().isoformat()
        bars = session(pull, day, control['symbol'])
        value = replay(control['symbol'], bars, control['timestamp_ms'])
        entries.append(dict(symbol=control['symbol'], ts=control['timestamp_ms'], recorded_state=control['state'],
                            recorded_trail=control['trail'], replay=value,
                            same_colour=bool(value and value['oracle_state']==control['state'])))
    print(json.dumps(dict(as_of_utc=pull['as_of_utc'], log_files=len(pull['sources']),
                         primary_holds=len(primary), recovery_gap_resets=pull['hold_count']-len(primary),
                         primary_short_probe_candidates=len(candidates),
                         table=table, entry_controls=entries), indent=2))


if __name__=='__main__':
    main()
