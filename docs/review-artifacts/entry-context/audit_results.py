"""Local coverage/cost audit; no database, broker or network access."""
import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

from score_study import ET, dt, stats, table


def audit(root):
    rows = json.loads((root / 'result.json.trades.json').read_text())
    result = {'cohorts': {}, 'paths': {}, 'hashes': {}}
    for cohort in ('holdout', 'replication'):
        all_rows = [p for p in rows if p['cohort'] == cohort]
        population = [p for p in all_rows if p['match_reason'] == 'ok']
        eligible = [p for p in population if p['feature_status'] == 'timestamp_eligible']
        measured = [p for p in eligible if 'uniform' in p]
        skipped = [p for p in eligible if not p['calm']]
        retained = [p for p in eligible if p['calm']]
        complete = [p for p in eligible if 'actual_dollars' in p]
        match = [p for p in measured if 'actual_dollars' in p]
        placement = [dict(p, calm=p['placement_feature'] < 1.5) for p in eligible
                     if p['placement_status'] == 'timestamp_eligible']
        raw_sources = Counter(b['source'] for p in population for b in p['entry_bars'])
        times = [dt(p['at']).astimezone(ET) for p in population]
        cross = [p for p in population if any(
            dt(b['bar_time']).astimezone(ET).date() != dt(p['at']).astimezone(ET).date()
            for b in p['entry_bars'])]
        different = [p for p in population if abs(float(p['entry_price']) - p['price']) > 0.000001]
        result['cohorts'][cohort] = {
            'raw_rows': len(all_rows), 'exclusions': dict(Counter(p['match_reason'] for p in all_rows)),
            'raw_ten_bars': sum(len(p['entry_bars']) == 10 for p in population),
            'cross_session': len(cross), 'cross_session_timestamp_eligible': sum(
                p['feature_status'] == 'timestamp_eligible' for p in cross),
            'raw_sources': dict(raw_sources), 'min_entry': str(min(times)), 'max_entry': str(max(times)),
            'pre_0710': sum(t.hour * 60 + t.minute < 430 for t in times),
            'created_late': sum(any(dt(b['created_at']) > dt(p['at']) for b in p['entry_bars']) for p in population),
            'updated_late': sum(any(dt(b['updated_at']) > dt(p['at']) for b in p['entry_bars']) for p in population),
            'managed_price_differs_from_fill_vwap': len(different),
            'max_match_seconds': max(abs((dt(p['at']) - dt(p['entry_time'])).total_seconds()) for p in population),
            'skip_count': len(skipped), 'eligible_count': len(eligible),
            'skipped_winners': sum(p.get('outcome') == 'target' for p in skipped),
            'all_winners': sum(p.get('outcome') == 'target' for p in measured),
            'actual_complete_n': len(complete), 'actual_missing_n': len(eligible) - len(complete),
            'actual_dollars_all': sum(p['actual_dollars'] for p in complete),
            'actual_dollars_skipped': sum(p.get('actual_dollars', 0) for p in skipped),
            'actual_dollars_retained': sum(p.get('actual_dollars', 0) for p in retained),
            'matched_outcome_and_actual': table(match),
            'placement_comparable_table': table(placement),
            'all_uniform': stats(measured, 'uniform'),
            'feature_clock_changes': dict(Counter(
                ('calm' if p['calm'] else 'other') + '_at_fill__' +
                ('calm' if p['placement_feature'] < 1.5 else 'other') + '_at_placement'
                for p in eligible if p['placement_status'] == 'timestamp_eligible')),
            'symbols': len(set(p['symbol'] for p in measured)),
            'ambiguous_entries': [{k: p[k] for k in ('id', 'symbol', 'at', 'outcome')}
                                  for p in eligible if 'uniform' not in p],
        }
    count = bars = byte_sum = max_bytes = 0
    statuses = Counter()
    asofs = []
    earliest_day = latest_day = None
    with (root / 'paths.ndjson').open() as stream:
        for line in stream:
            p = json.loads(line)
            count += 1
            bars += len(p['bars'])
            statuses[str(p['complete'])] += 1
            asofs.append(dt(p['asof']))
            earliest_day = min(earliest_day or p['day'], p['day'])
            latest_day = max(latest_day or p['day'], p['day'])
            for page in p['pages']:
                statuses['HTTP_' + str(page['http_status'])] += 1
                byte_sum += page['bytes']
                max_bytes = max(max_bytes, page['bytes'])
    result['paths'] = dict(symbol_days=count, seconds=bars, status=dict(statuses),
                           reply_bytes=byte_sum, largest_reply_bytes=max_bytes,
                           first_request_asof=str(min(asofs)), last_request_asof=str(max(asofs)),
                           earliest_day=earliest_day, latest_day=latest_day)
    for name in ('db.ndjson', 'db-exact.ndjson', 'price_jobs.json', 'price_jobs_exact.json',
                 'paths.ndjson', 'result.json', 'result.json.trades.json'):
        path = root / name
        sha = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1 << 20), b''):
                sha.update(block)
        result['hashes'][name] = dict(bytes=path.stat().st_size, sha256=sha.hexdigest())
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root)
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
