"""BKHA rule/counterfactual controls, NOT a replay of unretained full native tape."""

from decimal import Decimal
import json
import os
from pathlib import Path

import pytest

from project_mai_tai.momentum_paper.models import TradePrint
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.momentum_paper.warning_window import AggregateObservation
from project_mai_tai.services.momentum_paper_app import normalize_raw_trade
from tests.unit.test_momlatch1_lghl_proof import retained_conditions, stamp


SIGNAL = stamp('16:19:13.591') + 100_000  # CONTROLLED within reported millisecond, not native NS.


def controlled_model():
    conditions = retained_conditions()
    model = MomentumWarningConsumer(prior_closes={'BKHA': Decimal('10.00')},
        condition_version=conditions.version, coverage_started_ms=stamp('16:18:00.000')//1_000_000)
    trace = []
    # 15.43 admission bucket and prior-close10 are synthetic preconditions, not missing tape.
    for seq, (at, price) in enumerate([('16:18:23.000', '14.01'), ('16:19:09.000', '15.43')], 1):
        start = stamp(at)//1_000_000
        ns = (start+1000)*1_000_000
        p = Decimal(price)
        bar = AggregateObservation('BKHA', start, start+1000, p, p, p, p, 100,
                                   ns, ns, 'CONTROLLED-BKHA-PREFIX', 1, seq, 0)
        outcome, emitted = model.observe_aggregate(bar, observed_ns=ns, legacy_active=set())
        model.complete_read(f'{start+1000}-0', observed_ns=ns)
        assert not emitted
        trace.append(dict(kind='MODEL_A_BUCKET_END', observed_ns=ns, low=price,
                          status=outcome.status, synthetic=(seq == 2)))
    assert model.desired_symbols(set()) == {'BKHA'}
    trace.append(dict(kind='CONTROLLED_OWNER_APPLIED_NOT_PROVIDER_ACK', observed_ns=stamp('16:19:10.000')))
    return model, conditions, trace


def feed(model, conditions, *, ns, price, size=100, codes=(12,), trade_id='CONTROLLED'):
    eligible, reason = conditions.classify(codes)
    trade = TradePrint('BKHA', ns//1_000_000, Decimal(price), size, trade_id=trade_id,
                       conditions=codes, eligible=eligible, exclusion_reason=reason)
    records = model.observe_trade(trade, sip_timestamp_ns=ns, observed_ns=ns)
    return model.engine.seal_warning_decisions(records, observed_decision_ns=ns)


def detect(model, conditions):
    records = feed(model, conditions, ns=SIGNAL, price='16.87', trade_id='CONTROLLED-DETECT')
    detection, = [row for row in records if row.event_type == 'DETECTED']
    assert detection.strategy_code == 'momentum_60s_warn'
    assert detection.payload['reference']['price'] == '14.01'
    return detection


def summaries(records):
    return [dict(kind=row.event_type, strategy=row.strategy_code, payload=row.payload) for row in records]


def test_same_millisecond_round_lot_cannot_fill_even_with_later_SIP_nanoseconds(record_property):
    model, conditions, trace = controlled_model()
    detection = detect(model, conditions)
    records = feed(model, conditions, ns=SIGNAL+200_000, price='17.71', trade_id='CONTROLLED-SAME-MS-ROUNDLOT')
    assert not [row for row in records if row.event_type == 'FILLED']
    later = feed(model, conditions, ns=stamp('16:19:20.202'), price='18.15', trade_id='CONTROLLED-LATER-FILL')
    fill, = [row for row in later if row.event_type == 'FILLED']
    assert fill.payload['fill']['price'] == '18.15'
    record_property('CONTROLLED_BKHA_same_ms_fence', json.dumps(dict(trace=trace,
        detect=detection.payload, same_ms_round_lot_ns=SIGNAL+200_000, later_fill=fill.payload,
        native_missing='same-burst exactNS/conditions/arrival/uninterrupted tape NOT retained')))


@pytest.mark.parametrize('fill_price', ['17.71', '18.15'])
@pytest.mark.parametrize('exit_price', ['18.87', '19.50'])
@pytest.mark.parametrize('codes', [(12,), (12, 37)])
def test_one_share_exit_eligibility_depends_on_conditions_not_size(fill_price, exit_price, codes):
    model, conditions, _ = controlled_model()
    detect(model, conditions)
    fills = feed(model, conditions, ns=stamp('16:19:13.592'), price=fill_price)
    assert [row for row in fills if row.event_type == 'FILLED']
    # Alternate later-millisecond fill time is synthetic; cannot relabel same-ms17.71 as executable.
    exits = list(feed(model, conditions, ns=stamp('16:19:21.800'), price=exit_price, size=1, codes=codes))
    exits.extend(model.engine.advance_clock(stamp('16:19:22.000')//1_000_000))
    target = [row for row in exits if row.event_type == 'EXITED' and row.payload['exit_reason'] == 'TARGET']
    expected = conditions.classify(codes)[0] and Decimal(exit_price) >= Decimal(fill_price)*Decimal('1.05')
    assert bool(target) == expected
    if target:
        assert target[0].payload['exit']['size'] == 1


@pytest.mark.parametrize('size', [1, 100])
@pytest.mark.parametrize('codes,expected', [((12,), True), ((12, 37), False), ((12, 999999), False)])
def test_actual_raw_normalizer_preserves_after1600_condition_rules(size, codes, expected):
    trade = normalize_raw_trade(dict(ev='T', sym='BKHA', p='19.50', t=stamp('16:19:21.800')//1_000_000,
                                    s=size, c=codes), conditions=retained_conditions())
    assert trade is not None and trade.size == size and trade.eligible is expected


def test_CONTROLLED_native_reported_price_path_remains_STOP_when_highs_are_odd_lots(record_property):
    model, conditions, trace = controlled_model()
    records = [detect(model, conditions)]
    records.extend(feed(model, conditions, ns=SIGNAL+200_000, price='17.71'))
    records.extend(feed(model, conditions, ns=stamp('16:19:20.202'), price='18.15'))
    for at, p in [('16:19:21.700', '18.87'), ('16:19:21.800', '19.50')]:
        records.extend(feed(model, conditions, ns=stamp(at), price=p, size=1, codes=(12, 37)))
    records.extend(feed(model, conditions, ns=stamp('16:21:13.000'), price='15.17'))
    fills = [row for row in records if row.event_type == 'FILLED']
    exits = [row for row in records if row.event_type == 'EXITED']
    assert len(fills) == len(exits) == 1
    assert fills[0].payload['fill']['price'] == '18.15'
    assert exits[0].payload['exit_reason'] == 'STOP' and exits[0].payload['exit']['price'] == '15.17'
    record_property('CONTROLLED_BKHA_price_path_not_native_replay', json.dumps(dict(
        trace=trace, records=summaries(records), target=str(Decimal('18.15')*Decimal('1.05')),
        all_assumptions='Synthetic prefix/priorclose/IDs/sizes/conditions/within-msNS; prices/times from reviewer excerpts')))


def test_native_BKHA_full_tape_required_not_replaced_by_controlled_excerpt():
    selected = os.environ.get('MOMLATCH_BKHA_RETAINED')
    if not selected:
        pytest.skip('full native BKHA SIP/conditions/arrival tape missing; no native fill/exit claim')
    packet = json.loads(Path(selected).read_bytes())
    assert packet['symbol'] == 'BKHA' and packet['evidence_kind'] == 'RETAINED_NATIVE_TRADES'
    # Readiness only: implementing a general replay on actual packet follows provenance review.
    pytest.fail('native packet now supplied: provenance/full causal replay must be reviewed, not auto-PASS')
