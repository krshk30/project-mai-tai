"""Frozen three-claim extension. Local research only, no production mutations."""
import argparse
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from score_study import ET, dt, feature, load_db, real_cycles, score_path, stats, table

LIMITS = {'A': (1.5, 1.5), 'B': (4.0, 7.0), 'C': (20.0, 35.0)}
CLOCK = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:[,.](\d{1,6}))?')


def bucket(claim, value):
    good, bad = LIMITS[claim]
    return 'good' if value < good else 'bad' if value >= bad else 'middle'


def box(bars, anchor):
    bars = bars[:120]
    if len(bars) != 120:
        return None, 'fewer_than_120'
    values = [(Decimal(b['high_price']), Decimal(b['low_price'])) for b in bars]
    if any(not h.is_finite() or not l.is_finite() or l <= 0 or h < l for h, l in values):
        return None, 'invalid_ohlc'
    value = float((max(h for h, l in values) / min(l for h, l in values) - 1) * 100)
    status = 'timestamp_eligible'
    if any(dt(b['created_at']) > anchor or dt(b['updated_at']) > anchor for b in bars):
        status = 'not_asof'
    return value, status


def probes(path):
    result = defaultdict(list)
    files = []
    counts = Counter()
    last = None
    with path.open() as stream:
        for line in stream:
            item = json.loads(line)
            last = item
            if item['type'] == 'file':
                files.append(item)
            if item['type'] != 'probe':
                continue
            counts['raw'] += 1
            clock = CLOCK.match(item['line'])
            if clock is None:
                counts['no_clock'] += 1
                continue
            fields = dict(token.split('=', 1) for token in item['line'].split('[V2-ATR-PROBE]')[1].split() if '=' in token)
            try:
                observed = datetime.strptime(clock[1], '%Y-%m-%d %H:%M:%S').replace(
                    tzinfo=UTC, microsecond=int((clock[2] or '').ljust(6, '0')))
                record = dict(observed=observed, state=fields['state'], age=int(fields['age']),
                              low=fields['low'], high=fields['high'], close=fields['close'],
                              flip=fields['flip'], path=item['path'], line=item['line_number'])
                result[(fields['sym'], int(fields['ts_ms']))].append(record)
                counts['parsed'] += 1
            except (KeyError, ValueError):
                counts['bad_fields'] += 1
    assert last['type'] == 'complete'
    return result, dict(counts=counts, files=files, distinct_bars=len(result))


def probe_for(index, symbol, bar, anchor):
    key = (symbol, int(dt(bar['bar_time']).timestamp() * 1000))
    readings = [p for p in index.get(key, []) if p['observed'] <= anchor]
    if not readings:
        return None, 'probe_missing_before_entry'
    signatures = {tuple(p[k] for k in ('state', 'age', 'low', 'high', 'close', 'flip')) for p in readings}
    if len(signatures) != 1:
        return None, 'conflicting_probes'
    return min(readings, key=lambda x: x['observed']), None


def bounce(p, index):
    bars = p['entry_bars']
    anchor = dt(p['at'])
    if not bars:
        return None, 'no_closed_bars', {}
    last_short = None
    for offset in range(min(4, len(bars))):
        reading, error = probe_for(index, p['symbol'], bars[offset], anchor)
        if error:
            return None, error, {}
        if reading['state'] == 'short':
            last_short = (offset, reading)
            break
    if last_short is None:
        return None, 'no_short_within_three_bars', {}
    offset, tip = last_short
    if tip['age'] < 0 or offset + tip['age'] >= len(bars):
        return None, 'segment_start_outside_250_rows', {}
    run = []
    for distance in range(tip['age'] + 1):
        reading, error = probe_for(index, p['symbol'], bars[offset + distance], anchor)
        if error:
            return None, error, {}
        if reading['state'] != 'short' or reading['age'] != tip['age'] - distance:
            return None, 'segment_age_or_state_gap', {}
        run.append(reading)
    if run[-1]['flip'] != 'SELL' or run[-1]['age'] != 0:
        return None, 'missing_sell_start', {}
    low = min(Decimal(x['low']) for x in run)
    price = Decimal(p['buy']['price'])
    if low <= 0 or price <= 0:
        return None, 'invalid_price', {}
    detail = dict(segment_bars=len(run), terminal_offset=offset,
                  start_bar=bars[offset + tip['age']]['bar_time'], end_bar=bars[offset]['bar_time'],
                  low=str(low), start_low=run[-1]['low'], start_close=run[-1]['close'])
    return float((price / low - 1) * 100), 'recorded_bot_segment', detail


def claim_rows(rows, claim):
    return [dict(p, calm=p['features'][claim]['bucket'] == 'good') for p in rows
            if p['features'][claim]['bucket'] in ('good', 'bad')]


def summarize(rows, claim):
    selected = claim_rows(rows, claim)
    t = table(selected)
    contributions = defaultdict(float)
    for p in selected:
        if p['calm'] and 'uniform' in p:
            contributions[p['symbol']] += p['uniform']
    best = min(contributions, key=lambda k: (-contributions[k], k)) if contributions else None
    dropped = table([p for p in selected if p['symbol'] != best])
    blocks = {b: table([p for p in selected if p['block'] == b])
              for b in ('07:00-09:30', '09:30-11:00', '11:00-16:00')}
    a, b = t['calm'], t['other']
    evaluable = (a['uniform']['n'] >= 40 and b['uniform']['n'] > 0 and dropped['gap'] is not None
                 and sum(x['gap'] is not None for x in blocks.values()) >= 2)
    passed = (evaluable and a['win_rate'] >= .6 and b['win_rate'] <= .55 and t['gap'] >= 1
              and dropped['gap'] > 0 and sum(x['gap'] is not None and x['gap'] > 0 for x in blocks.values()) >= 2)
    measured = [p for p in rows if p['features'][claim]['bucket'] != 'unknown']
    skipped = [p for p in measured if p['features'][claim]['bucket'] != 'good']
    actual_matched = [p for p in selected if 'uniform' in p and 'actual_return' in p]
    return dict(table=t, actual_matched=table(actual_matched), best_name=best, drop_best=dropped, blocks=blocks,
                proxy_gate='PASS' if passed else 'FAIL' if evaluable else 'UNMEASURED',
                status=dict(Counter(p['features'][claim]['status'] for p in rows)),
                buckets=dict(Counter(p['features'][claim]['bucket'] for p in rows)),
                skip_count=len(skipped), eligible_count=len(measured),
                skipped_winners=sum(p.get('outcome') == 'target' for p in skipped),
                all_winners=sum(p.get('outcome') == 'target' for p in measured),
                actual_skipped_dollars=sum(p.get('actual_dollars', 0) for p in skipped),
                actual_skipped_n=sum('actual_dollars' in p for p in skipped),
                middle_uniform=stats([p for p in rows if p['features'][claim]['bucket'] == 'middle'], 'uniform'),
                halves={h: table([p for p in selected if (p['day'] < '2026-09-14') == early])
                        for h, early in [('before_Sep14', True), ('Sep14_on', False)]})


def build(root):
    index, log_meta = probes(root / 'probes.ndjson')
    blind_meta, blind, fills = load_db(root / 'three-blind.ndjson')
    _, recent, _ = load_db(root / 'three-support.ndjson')
    recent = {p['id']: p for p in recent}
    prior = json.loads((root / 'result.json.trades.json').read_text())
    cashflows = real_cycles(fills)
    blind = [p for p in blind if p['cohort'] == 'holdout' and p['match_reason'] == 'ok']
    groups = {'blind_Jun17_Jul31': blind,
              'support_Webull_Aug_Oct': [p for p in prior if p['cohort'] == 'holdout' and p['match_reason'] == 'ok'],
              'replication_Schwab_Aug_Oct': [p for p in prior if p['cohort'] == 'replication' and p['match_reason'] == 'ok']}
    paths = {}
    for source in (root / 'paths.ndjson', root / 'three-blind-paths.ndjson'):
        if source.exists():
            with source.open() as stream:
                for line in stream:
                    path = json.loads(line)
                    paths[(path['symbol'], path['day'])] = path
    jobs = {}
    ledger = []
    for cohort, rows in groups.items():
        for p in rows:
            if cohort.startswith('blind'):
                p.update(cashflows.get(p['buy']['order_id'], {}))
            else:
                p['entry_bars'] = recent[p['id']]['entry_bars']
            path = paths.get((p['symbol'], p['day']))
            if path and path['from_ms'] > dt(p['at']).timestamp() * 1000:
                path = None
            p.pop('uniform', None)
            p.update(score_path(p, path))
            b, bstatus, bdetail = bounce(p, index)
            c, cstatus = box(p['entry_bars'], dt(p['at']))
            a, astatus = p['feature'], p['feature_status']
            p['features'] = {}
            for claim, value, status in [('A', a, astatus), ('B', b, bstatus), ('C', c, cstatus)]:
                eligible = status in ('timestamp_eligible', 'recorded_bot_segment')
                p['features'][claim] = dict(value=value, status=status,
                                            bucket=bucket(claim, value) if eligible else 'unknown')
            p['features']['B']['detail'] = bdetail
            if len(p['entry_bars']) >= 120:
                b0, b119 = p['entry_bars'][0], p['entry_bars'][119]
                p['features']['C']['span_minutes'] = (dt(b0['bar_time']) - dt(b119['bar_time'])).total_seconds() / 60 + 1
                p['features']['C']['cross_session'] = dt(b119['bar_time']).astimezone(ET).date() != dt(p['at']).astimezone(ET).date()
                p['features']['C']['last_close_age_seconds'] = (dt(p['at']) - dt(b0['bar_time'])).total_seconds() - 60
                p['features']['C']['exact_clock'] = (
                    p['features']['C']['span_minutes'] == 120
                    and 0 < p['features']['C']['last_close_age_seconds'] <= 60)
            if any(x['bucket'] != 'unknown' for x in p['features'].values()):
                if cohort.startswith('blind') or p['outcome'] == 'path_unavailable':
                    key = (p['symbol'], p['day'])
                    if key not in jobs or dt(p['at']) < dt(jobs[key]['start']):
                        jobs[key] = dict(symbol=p['symbol'], day=p['day'], start=p['at'])
            keep = ('id', 'account', 'symbol', 'at', 'day', 'price', 'block', 'features', 'outcome',
                    'uniform', 'actual_return', 'actual_dollars')
            ledger.append(dict(cohort=cohort, **{k: p[k] for k in keep if k in p}))
    report = dict(blind_metadata=blind_meta, probe_metadata=log_meta, groups={})
    for cohort, rows in groups.items():
        full = [p for p in rows if all(x['bucket'] != 'unknown' for x in p['features'].values())]
        masks = Counter(''.join('1' if p['features'][c]['bucket'] == 'good' else '0' for c in 'ABC') for p in full)
        incremental = {}
        pairwise_incremental = {}
        for given in 'ABC':
            for target in 'ABC':
                if given != target:
                    incremental[given + '_then_' + target] = table(claim_rows(
                        [p for p in full if p['features'][given]['bucket'] == 'good'], target))
                    pairwise_incremental[given + '_then_' + target] = table(claim_rows(
                        [p for p in rows if p['features'][given]['bucket'] == 'good'], target))
        all_good = [p for p in full if all(p['features'][c]['bucket'] == 'good' for c in 'ABC')]
        report['groups'][cohort] = dict(population=len(rows),
            claims={claim: summarize(rows, claim) for claim in 'ABC'},
            overlap=dict(fully_measured=len(full), masks=dict(masks), good_counts={
                str(n): sum(v for k, v in masks.items() if k.count('1') == n) for n in range(4)},
                incremental=incremental, pairwise_incremental=pairwise_incremental,
                all_good_n=len(all_good),
                all_good_uniform=stats(all_good, 'uniform'), all_good_actual=stats(all_good, 'actual_return')),
            box_spans=dict(raw_120=sum('span_minutes' in p['features']['C'] for p in rows),
                cross_session=sum(p['features']['C'].get('cross_session', False) for p in rows),
                median_minutes=statistics.median([p['features']['C']['span_minutes'] for p in rows
                    if 'span_minutes' in p['features']['C']]) if rows else None),
            posthoc_exact_clock_C=summarize([p for p in rows if p['features']['C'].get('exact_clock')], 'C'))
    return report, ledger, sorted(jobs.values(), key=lambda x: (x['day'], x['symbol']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    report, ledger, jobs = build(args.root)
    for filename, data in [('three-result.json', report), ('three-ledger.json', ledger), ('three-jobs.json', jobs)]:
        (args.root / filename).write_text(json.dumps(data, indent=2, default=str))
    print(json.dumps({'jobs': len(jobs), 'populations': {k: v['population'] for k, v in report['groups'].items()},
                      'probes': report['probe_metadata']['counts']}))
