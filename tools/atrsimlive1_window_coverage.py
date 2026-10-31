"""Supplied-data coverage from SAME-RR complete minute census, not raw-full-read.

The C1 14:46 minute rules are unchanged. The new certificate binds actual
window payload + outside-window metadata separately; no omitted raw-row hash
is fabricated. FILE equality keeps its already verified full-file metrics.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from atrsimlive1_collect import digest
from atrsimlive1_working_windows import MINUTE, nanos


def derive_census(*, symbol, day, bars, census, read_proof, log, asof_ns):
    report = dict(symbol=symbol, trading_day=day, method='C1_20261028_1446_DATA_DERIVED',
        native_market_complete=False, capture_log=log, read_proof=read_proof,
        issues=[], minutes=[], windows=[])
    if (read_proof.get('schema') != 'ATRSIMLIVE1_WINDOW_CENSUS_RR_V1'
            or read_proof.get('provider') != 'massive'
            or read_proof.get('symbol') != symbol or read_proof.get('truncated') is not False
            or read_proof.get('full_read') is not False
            or read_proof.get('census_sha256') != digest(census)
            or read_proof.get('bars_sha256') != digest(bars)
            or read_proof.get('bar_provider') != 'schwab'
            or read_proof.get('bar_table') != 'strategy_bar_history'
            or read_proof.get('bars_full_read') is not True
            or read_proof.get('bars_truncated') is not False
            or read_proof.get('bar_count') != len(bars)
            or read_proof.get('row_count') != sum(row['row_count'] for row in census)):
        report['issues'] = ['UNKNOWN_SAME_SNAPSHOT_FULL_READ_CERTIFICATE']
        return [], report
    left, right = nanos(read_proof['start']), min(nanos(read_proof['end']), asof_ns)
    known_fault = any(code in {'CAPTURE_LOG_DAY_DROPPED_POSITIVE',
        'CAPTURE_LOG_RESTART_OR_FAILURE_IN_WINDOW', 'CAPTURE_LOG_COUNTER_RESET'}
        for code in log.get('issues', []))
    known_fault = known_fault or (type(log.get('dropped')) is int and log['dropped'] > 0)
    log_complete = (log.get('status') == 'AVAILABLE'
        and log.get('ruling') == 'C1_20261028_1446_DATA_DERIVED'
        and nanos(log['asof']) == asof_ns and log.get('dropped') == 0)
    if not log_complete:
        report['secondary_issues'] = ['SECONDARY_CAPTURE_LOG_UNKNOWN_NONBLOCKING', *log.get('issues', [])]
    if known_fault:
        report['issues'].append('UNKNOWN_POSITIVE_CAPTURE_LOG_FAILURE')
    report['reference_bar_source'] = 'schwab strategy_bar_history'
    by_minute, counts = {}, {}
    for row in bars:
        start = nanos(row['bar_time'])
        if (row['symbol'] != symbol or nanos(row['created_at']) > asof_ns
                or start % MINUTE or start in by_minute
                or type(row['volume']) is not int or row['volume'] < 0):
            report['issues'] = ['UNKNOWN_CAPTURE_BAR_IDENTITY_VOLUME_OR_DUPLICATE']
            return [], report
        by_minute[start] = row
    for row in census:
        minute = row['minute_ns']
        if (row['symbol'] != symbol or minute % MINUTE or minute in counts
                or row['row_count'] <= 0 or not left <= row['first_ns'] <= row['last_ns'] < right
                or not minute <= row['first_ns'] <= row['last_ns'] < minute + MINUTE):
            raise ValueError('same_snapshot_minute_census_shape')
        counts[minute] = row['row_count']
    matches = {minute for minute, row in by_minute.items()
               if row['volume'] > 0 and counts.get(minute, 0) > 0
               and left <= minute and minute + MINUTE <= right}
    independent = set() if known_fault else matches
    covered = set(independent)
    for minute in range(left // MINUTE * MINUTE, right // MINUTE * MINUTE, MINUTE):
        bar, count = by_minute.get(minute), counts.get(minute, 0)
        if minute in independent:
            status = 'COVERED_POSITIVE_BAR_WITH_PRINT'
        elif minute in matches:
            status = 'UNKNOWN_DATA_MATCH_KNOWN_LOG_FAILURE'
        elif bar is not None and bar['volume'] > 0 and count == 0:
            status = 'UNKNOWN_POSITIVE_BAR_ZERO_PRINTS'
        elif ((bar is None or bar['volume'] == 0) and count == 0
              and minute - MINUTE in independent and minute + MINUTE in independent):
            status = 'COVERED_INTERIOR_NO_TRADING_BETWEEN_INDEPENDENT_MINUTES'
            covered.add(minute)
        else:
            status = 'UNKNOWN_UNPROVEN_MINUTE_OR_LOG_BOUNDARY'
        report['minutes'].append(dict(start_ns=minute,
            capture_bar_id=None if bar is None else bar['id'],
            bar_volume=None if bar is None else bar['volume'], print_count=count, status=status))
    intervals = []
    for minute in sorted(covered):
        if intervals and intervals[-1][1] == minute:
            intervals[-1] = intervals[-1][0], minute + MINUTE
        else:
            intervals.append((minute, minute + MINUTE))
    windows = []
    for start, end in intervals:
        rows = [row for row in census if start <= row['minute_ns'] < end]
        if not rows:
            continue
        gaps = [row['max_gap_ns'] for row in rows]
        gaps.extend(b['first_ns'] - a['last_ns'] for a, b in zip(rows, rows[1:]))
        metrics = dict(start_ns=start, end_ns=end, row_count=sum(row['row_count'] for row in rows),
            rows_sha256=None, census_sha256=digest(rows), first_print_id=rows[0]['first_id'],
            last_print_id=rows[-1]['last_id'], first_print_ns=rows[0]['first_ns'],
            last_print_ns=rows[-1]['last_ns'], max_interprint_gap_ns=max(gaps, default=0),
            full_read=False, census_full_read=True, truncated=False)
        begin = datetime.fromtimestamp(start // 1000000000, timezone.utc).astimezone(ZoneInfo('America/New_York'))
        finish = datetime.fromtimestamp(end // 1000000000, timezone.utc).astimezone(ZoneInfo('America/New_York'))
        note = (f'coverage: C1 supplied watched window {begin:%H:%M}–{finish:%H:%M} ET; '
                'outside-window and native/market completeness UNKNOWN')
        windows.append((start, end, (), note, metrics))
        report['windows'].append(metrics)
    if not windows:
        report['issues'].append('UNKNOWN_NO_DATA_DERIVED_COVERED_INTERVAL')
    return windows, report
