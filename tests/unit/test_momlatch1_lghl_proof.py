"""LGHL causal model; sparse reviewer points never count as recorded full tape."""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
import heapq
import json
import os
from pathlib import Path

import pytest

from project_mai_tai.momentum_paper.conditions import condition_snapshot_from_payload
from project_mai_tai.momentum_paper.models import TradePrint
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.momentum_paper.warning_window import AggregateObservation


CATALOG = Path('/Users/velkris/.codex/task-queues/analysis/momentum-stop-study-20261012/momentum-stop-study-events-20261012.json')


def retained_conditions():
    if not CATALOG.exists():
        pytest.skip('retained catalog missing; no invented condition rule')
    ready = next(row['payload'] for row in json.loads(CATALOG.read_bytes())['rows']
                 if row['event_type'] == 'SESSION_READY' and row['session_date'] == '2026-10-12')
    return condition_snapshot_from_payload(ready['condition_snapshot'])


def stamp(clock):
    return int(datetime.fromisoformat('2026-10-13T' + clock + '-04:00').timestamp() * 1_000_000_000)


def replay_lghl(rows, conditions, *, prior_close, owner_delay_ns=0, aggregate_delay_ns=0):
    if owner_delay_ns < 0 or aggregate_delay_ns < 0:
        raise ValueError('nonnegative declared CONTROLLED delay required')
    rows = sorted(rows, key=lambda row: row['sip_timestamp'])
    model = MomentumWarningConsumer(prior_closes={'LGHL': Decimal(prior_close)},
        condition_version=conditions.version, coverage_started_ms=rows[0]['sip_timestamp']//1_000_000)
    buckets, events = defaultdict(list), []
    for index, row in enumerate(rows):
        eligible, reason = conditions.classify(row['conditions'])
        ns = row['sip_timestamp']
        trade = TradePrint('LGHL', ns//1_000_000, Decimal(str(row['price'])), row['size'],
            trade_id=str(row.get('id', '')), conditions=tuple(row['conditions']), eligible=eligible,
            exclusion_reason=reason)
        events.append((ns, 2, index, trade))
        if eligible:
            buckets[ns//1_000_000_000].append(trade)
    for seq, (second, trades) in enumerate(sorted(buckets.items()), 1):
        received = (second+1)*1_000_000_000 + aggregate_delay_ns
        prices = [trade.price for trade in trades]
        bar = AggregateObservation('LGHL', second*1000, (second+1)*1000, prices[0], max(prices),
            min(prices), prices[-1], sum(trade.size for trade in trades), received, received,
            'CONTROLLED-LGHL-CAUSAL', 1, seq, 0)
        events.append((received, 0, seq, bar))
    heapq.heapify(events)
    owner, pending = False, False
    trace, records, references = [], [], []
    while events:
        observed, kind, seq, payload = heapq.heappop(events)
        if kind == 0:
            assert observed >= payload.end_ms*1_000_000
            outcome, emitted = model.observe_aggregate(payload, observed_ns=observed, legacy_active=set())
            model.complete_read(f'{payload.end_ms}-0', observed_ns=observed)
            trace.append(dict(kind='MODEL_BUCKET_END', observed_ns=observed, outcome=outcome.status,
                              start_ms=payload.start_ms, end_ms=payload.end_ms, low=str(payload.low)))
            if outcome.status == 'ADMITTED' and not owner and not pending:
                pending = True
                trace.append(dict(kind='CONTROLLED_OWNER_REQUEST', observed_ns=observed))
                heapq.heappush(events, (observed+owner_delay_ns, 1, seq, None))
        elif kind == 1:
            owner = True
            trace.append(dict(kind='CONTROLLED_OWNER_APPLICATION_NOT_PROVIDER_ACK', observed_ns=observed))
            continue
        elif owner:
            for seconds in (30, 60):
                whole = model.windows['LGHL'].whole_buckets(observed_ms=payload.sip_ts_ms, seconds=seconds)
                if whole and payload.price >= Decimal('3.96'):
                    low = min(whole, key=lambda bar: (bar.low, bar.start_ms))
                    references.append(dict(seconds=seconds, at_ns=observed, low=str(low.low),
                        start_ms=low.start_ms, end_ms=low.end_ms, whole_buckets=len(whole)))
            emitted = model.observe_trade(payload, sip_timestamp_ns=observed, observed_ns=observed)
        else:
            continue
        emitted = model.engine.seal_warning_decisions(emitted, observed_decision_ns=observed)
        records.extend(dict(kind=row.event_type, strategy=row.strategy_code, observed_ns=observed,
                            payload=row.payload, cause='raw_print' if kind == 2 else 'MODEL_BUCKET_END') for row in emitted)
    return dict(input_rows=len(rows), trace=trace, records=records, references=references,
                provider_ACK='NOT_RECORDED', transport_latency='UNMEASURED',
                bucket_provenance='MODEL_eligible_SIP_NOT_RECORDED_WS_A',
                owner_delay_ns=owner_delay_ns, aggregate_delay_ns=aggregate_delay_ns)


def controlled_rows():
    # First three/final points paraphrase reviewer times; all IDs and 3.70 admission point are synthetic.
    return [dict(sip_timestamp=stamp(t), price=p, size=q, conditions=[12], id=f'CONTROLLED-{i}')
            for i, (t, p, q) in enumerate([
                ('16:01:26.720', '3.30', 500), ('16:01:27.050', '3.30', 348),
                ('16:01:27.470', '3.40', 1), ('16:01:45.000', '3.70', 1),
                ('16:02:18.000', '3.98', 325)])]


def test_retained_condition12_and_unknown_combination_after1600():
    conditions = retained_conditions()
    assert conditions.rules[12].name == 'Form T/Extended Hours'
    assert not conditions.rules[12].updates_consolidated_ohlc
    assert conditions.classify([12]) == (True, 'aggregate_eligible_extended_hours')
    assert not conditions.classify([12, 999999])[0]


def test_CONTROLLED_sparse_LGHL_M60_first396_M30_none(record_property):
    result = replay_lghl(controlled_rows(), retained_conditions(), prior_close='3.00')
    detections = [row for row in result['records'] if row['kind'] == 'DETECTED']
    assert len(detections) == 1 and detections[0]['strategy'] == 'momentum_60s_warn'
    assert detections[0]['observed_ns'] == stamp('16:02:18.000') and detections[0]['cause'] == 'raw_print'
    assert Decimal(detections[0]['payload']['detect']['price']) == Decimal('3.98')
    owner = next(row for row in result['trace'] if row['kind'] == 'CONTROLLED_OWNER_APPLICATION_NOT_PROVIDER_ACK')
    assert owner['observed_ns'] < detections[0]['observed_ns']
    assert not [row for row in result['records'] if row['kind'] == 'FILLED']
    record_property('CONTROLLED_not_native_LGHL_causal_table', json.dumps(result))


def test_CONTROLLED_owner_delay_cannot_backdate_first_LGHL_signal():
    result = replay_lghl(controlled_rows(), retained_conditions(), prior_close='3.00', owner_delay_ns=40_000_000_000)
    assert not [row for row in result['records'] if row['kind'] == 'DETECTED' and row['cause'] == 'raw_print']


def test_native_full_LGHL_replay_requires_supplied_retained_tape(record_property):
    source = os.environ.get('MOMLATCH_LGHL_RETAINED')
    if not source:
        pytest.skip('full native LGHL tape/catalog/priorclose unretained; sparse model is not native acceptance')
    packet = json.loads(Path(source).read_bytes())
    assert packet['symbol'] == 'LGHL' and packet['session_date'] == '2026-10-13'
    assert packet['evidence_kind'] == 'RETAINED_NATIVE_TRADES'
    assert packet['request_start_ns'] <= stamp('16:01:00.000')
    assert packet['request_end_ns'] >= stamp('16:03:00.000') and packet['pagination_complete'] is True
    result = replay_lghl(packet['rows'], condition_snapshot_from_payload(packet['condition_snapshot']),
                         prior_close=packet['prior_close'])
    assert result['input_rows'] == len(packet['rows'])
    first = min(row['sip_timestamp'] for row in packet['rows']
                if Decimal(str(row['price'])) >= Decimal('3.96')
                and condition_snapshot_from_payload(packet['condition_snapshot']).classify(row['conditions'])[0])
    m60 = [row for row in result['records'] if row['kind'] == 'DETECTED' and row['strategy'] == 'momentum_60s_warn']
    assert m60 and m60[0]['observed_ns'] == first and m60[0]['cause'] == 'raw_print'
    assert not [row for row in result['records'] if row['kind'] == 'DETECTED' and row['strategy'] == 'momentum_30s_warn']
    record_property('native_LGHL_model_from_retained_SIP_not_WS_ACK', json.dumps(result))
