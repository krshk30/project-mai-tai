"""Paper-only case identity controls; BCpC prices below are synthetic controls."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.events import (
    MarketSnapshotPayload, SnapshotBatchEvent, SnapshotBatchPayload,
)
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine, symbol_is_eligible
from project_mai_tai.momentum_paper.models import TradePrint
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.momentum_paper.warning_window import (
    AggregateObservation, ObservedRawPaperPath, WarningWindow,
)
from project_mai_tai.services import momentum_paper_app as app
from project_mai_tai.settings import Settings
from tests.unit.test_momentum_paper_service import _condition_snapshot
from tests.unit.test_momentum_warning import bar, evaluation


NOW = datetime(2026, 10, 13, 17, 26, tzinfo=UTC)
MS = int(NOW.timestamp() * 1000)
PAIRS = [('TpC', 'TPC', '15.73', '82.77', '15.62', '83.49'),
         ('BCpC', 'BCPC', '16', '80', '15', '81')]


def engine(prior=None):
    return MomentumPaperEngine(prior_closes=prior or {'TPC': Decimal('83.49')},
                               condition_version='case-control', coverage_started_ms=MS - 60_000)


def snapshot(symbol, price, offset=0):
    return MarketSnapshotPayload(symbol=symbol, last_trade_price=Decimal(price),
                                 last_trade_timestamp_ns=(MS + offset) * 1_000_000)


@pytest.mark.parametrize('symbol', ['TpC', 'BCpC', 'tpc', ' TPC', 'TPC '])
def test_nonexact_symbols_are_ineligible_before_normalization(symbol):
    assert not symbol_is_eligible(symbol)


@pytest.mark.parametrize('preferred,common,low,high,pref_close,common_close', PAIRS)
@pytest.mark.parametrize('preferred_last', [False, True])
def test_prior_close_collision_never_overwrites_common(
    preferred, common, low, high, pref_close, common_close, preferred_last,
):
    pairs = [(preferred, Decimal(pref_close)), (common, Decimal(common_close))]
    if preferred_last:
        pairs.reverse()
    evaluator = engine(dict(pairs))
    assert evaluator.prior_closes == {common: Decimal(common_close)}


def test_bad_preferred_price_is_rejected_before_decimal_conversion():
    assert engine({'TpC': object(), 'TPC': Decimal('83.49')}).prior_closes == {
        'TPC': Decimal('83.49'),
    }


@pytest.mark.parametrize('preferred,common,low,high,pref_close,common_close', PAIRS)
def test_alternating_snapshot_collision_has_zero_detections(
    preferred, common, low, high, pref_close, common_close,
):
    evaluator = engine({common: Decimal(common_close), preferred: Decimal(pref_close)})
    records = []
    for offset, symbol, price in [(0, preferred, low), (5_000, common, high),
                                  (10_000, preferred, low), (15_000, common, high)]:
        found, capped = evaluator.detect_from_snapshots([snapshot(symbol, price, offset)],
            completed_at=NOW + timedelta(milliseconds=offset), max_symbols=16)
        assert capped == 0
        records.extend(found)
    assert records == [] and not evaluator.active_symbols
    assert set(evaluator._snapshot_history) == {common}
    assert all(item.symbol == common and item.price == Decimal(high)
               for item in evaluator._snapshot_history[common])


@pytest.mark.parametrize('preferred,common,low,high,pref_close,common_close', PAIRS)
def test_preferred_trade_never_enters_common_reference_pool_or_fills(
    preferred, common, low, high, pref_close, common_close,
):
    evaluator = engine({common: Decimal(common_close)})
    assert evaluator.ingest(TradePrint(preferred, MS, Decimal(low), 100)) == ()
    assert not evaluator._eligible_history and not evaluator._excluded_history
    assert evaluator.ingest(TradePrint(common, MS + 5_000, Decimal(high), 100)) == ()
    found = evaluator.ingest(TradePrint(common, MS + 10_000, Decimal(high) * Decimal('1.20'), 100))
    assert sum(row.event_type == 'DETECTED' for row in found) == 2
    assert evaluator.ingest(TradePrint(preferred, MS + 10_001, Decimal(high), 100),
                            allow_detection=False) == ()
    assert all(not row.get('fill') for row in evaluator.active_events)


@pytest.mark.parametrize('kind', ['snapshot', 'trade'])
def test_common_tpc_real_twenty_percent_move_still_detects(kind):
    evaluator = engine({'TpC': Decimal('15.62'), 'TPC': Decimal('83.49')})
    if kind == 'snapshot':
        evaluator.detect_from_snapshots([snapshot('TPC', '82.77')], completed_at=NOW, max_symbols=16)
        records, _ = evaluator.detect_from_snapshots([snapshot('TPC', '99.324', 5_000)],
            completed_at=NOW + timedelta(seconds=5), max_symbols=16)
    else:
        evaluator.ingest(TradePrint('TPC', MS, Decimal('82.77'), 100))
        records = evaluator.ingest(TradePrint('TPC', MS + 5_000, Decimal('99.324'), 100))
    assert sum(row.event_type == 'DETECTED' for row in records) == 2
    assert {row.symbol for row in records} == {'TPC'}
    assert all(row['prior_close'] == '83.49' for row in evaluator.active_events)


@pytest.mark.parametrize('preferred', ['TpC', 'BCpC'])
def test_raw_trade_parser_refuses_case_marker(preferred):
    assert app.normalize_raw_trade({'ev': 'T', 'sym': preferred, 'p': '15.73',
        't': MS, 's': 100, 'c': [14]}, conditions=_condition_snapshot()) is None


@pytest.mark.parametrize('preferred_last', [True, False])
def test_reference_load_exact_keys_before_price_parsing(preferred_last, monkeypatch):
    rows = [{'ticker': 'TPC', 'close': '83.49'}, {'ticker': 'TpC', 'close': '15.62'},
            {'ticker': 'BCPC', 'close': '81'}, {'ticker': 'BCpC', 'close': object()}]
    if not preferred_last:
        rows.reverse()
    client = SimpleNamespace(list_conditions=lambda **_: [], get_grouped_daily_aggs=lambda *_a, **_k: rows)
    monkeypatch.setattr(app, 'build_condition_snapshot', lambda *_a, **_k: _condition_snapshot())
    service = app.MomentumPaperService(Settings(), clock=lambda: NOW, rest_client_factory=lambda: client)
    _, closes = service._fetch_reference_inputs(date(2026, 10, 12))
    assert closes == {'TPC': Decimal('83.49'), 'BCPC': Decimal('81')}


@pytest.mark.asyncio
@pytest.mark.parametrize('preferred', ['TpC', 'BCpC'])
async def test_service_rejects_preferred_before_subscription_or_condition_processing(preferred, monkeypatch):
    service = app.MomentumPaperService(Settings(), clock=lambda: NOW)
    service._engine = engine()
    service._condition_snapshot = _condition_snapshot()
    service._subscribed_symbols = {preferred.upper()}
    service._condition_feed_verified = service._connected = True
    persist = AsyncMock()
    monkeypatch.setattr(service, '_persist', persist)
    await service._handle_gateway_event(json.dumps({'source_service': 'market-data-gateway',
        'event_type': 'trade_tick', 'payload': {'symbol': preferred}}))
    assert service._condition_feed_verified and service._connected
    persist.assert_not_called()
    assert not service._engine._eligible_history


@pytest.mark.parametrize('preferred,common', [('TpC', 'TPC'), ('BCpC', 'BCPC')])
def test_warning_raw_case_marker_cannot_satisfy_common_owner(preferred, common):
    consumer = MomentumWarningConsumer(prior_closes={common: Decimal('83.49')},
        condition_version='case-control', coverage_started_ms=MS - 60_000)
    consumer.candidates.observe(evaluation(0, symbol=common, warning=True), active_path_symbols=set())
    assert consumer.observe_trade(TradePrint(preferred, MS, Decimal('15.73'), 100),
        sip_timestamp_ns=MS * 1_000_000, observed_ns=MS * 1_000_000) == ()
    assert consumer._last_raw_ns == {} and consumer._raw_first_ms == {}


@pytest.mark.parametrize('preferred', ['TpC', 'BCpC'])
def test_warning_aggregate_rejects_case_marker_without_coercion(preferred):
    original = bar(symbol='TPC')
    with pytest.raises(ValueError, match='invalid_aggregate_metadata'):
        replace(original, symbol=preferred)
    payload = {**original.__dict__, 'symbol': preferred, 'version': 1, 'event_type': 'aggregate'}
    with pytest.raises(ValueError, match='invalid_aggregate_metadata'):
        AggregateObservation.from_payload(payload)


@pytest.mark.parametrize('preferred', ['TpC', 'BCpC'])
def test_warning_reference_window_and_raw_path_refuse_mixed_case_identity(preferred):
    with pytest.raises(ValueError, match='ineligible_warning_symbol'):
        WarningWindow(preferred)
    with pytest.raises(ValueError, match='ineligible_warning_symbol'):
        ObservedRawPaperPath(symbol=preferred, prior_close=Decimal('15.62'), condition_version='fixture')


@pytest.mark.asyncio
async def test_snapshot_metric_uses_exact_common_source_not_preferred(monkeypatch):
    service = app.MomentumPaperService(Settings(), clock=lambda: NOW + timedelta(seconds=5))
    service._engine = engine()
    service._condition_snapshot = _condition_snapshot()
    service._warning = MomentumWarningConsumer(prior_closes={'TPC': Decimal('83.49')},
        condition_version='case-control', coverage_started_ms=MS - 60_000)
    service._engine.detect_from_snapshots([snapshot('TPC', '82.77')], completed_at=NOW, max_symbols=16)
    persist, metrics = AsyncMock(), AsyncMock()
    monkeypatch.setattr(service, '_persist', persist)
    monkeypatch.setattr(service, '_warning_metrics', metrics)
    monkeypatch.setattr(service, '_sync_gateway_subscriptions', AsyncMock())
    event = SnapshotBatchEvent(source_service='market-data-gateway', payload=SnapshotBatchPayload(
        snapshots=[snapshot('TpC', '15.73', 4_000), snapshot('TPC', '99.324', 5_000)],
        completed_at=NOW + timedelta(seconds=5)))
    await service._handle_gateway_event(event.model_dump_json())
    assert metrics.await_count == 2
    assert all(call.kwargs['snapshot_last_trade_sip_ns'] == (MS + 5_000) * 1_000_000
               for call in metrics.await_args_list)


@pytest.mark.parametrize('kind', ['snapshot', 'trade'])
def test_reference_pool_requires_exact_symbol_even_if_history_is_misbound(kind):
    evaluator = engine()
    foreign = TradePrint('TpC', MS, Decimal('15.73'), 100)
    if kind == 'snapshot':
        evaluator._snapshot_history['TPC'].append(foreign)
        records, _ = evaluator.detect_from_snapshots([snapshot('TPC', '82.77', 5_000)],
            completed_at=NOW + timedelta(seconds=5), max_symbols=16)
    else:
        evaluator._eligible_history['TPC'].append(foreign)
        records = evaluator.ingest(TradePrint('TPC', MS + 5_000, Decimal('82.77'), 100))
    assert not records and not evaluator.active_symbols


def test_reentry_boundary_restore_never_merges_preferred_into_common():
    producer = engine()
    producer.ingest(TradePrint('TPC', MS, Decimal('82.77'), 100))
    producer.ingest(TradePrint('TPC', MS + 5_000, Decimal('99.324'), 100))
    terminal = producer.advance_clock(MS + 16_000)
    assert sum(row.event_type == 'NO_FILL' for row in terminal) == 2
    evaluator = engine()
    evaluator.seed_reentry_boundaries([replace(row, symbol='TpC') for row in terminal])
    assert not evaluator._reference_after_ms
    evaluator.seed_reentry_boundaries(terminal)
    assert set(evaluator._reference_after_ms) == {('momentum_30s', 'TPC'), ('momentum_60s', 'TPC')}


def test_observed_raw_path_refuses_preferred_before_clock_or_coverage_mutation():
    path = ObservedRawPaperPath(symbol='TPC', prior_close=Decimal('83.49'), condition_version='fixture')
    with pytest.raises(ValueError, match='raw_trade_identity_mismatch'):
        path.observe(TradePrint('TpC', MS, Decimal('15.73'), 100),
            sip_timestamp_ns=MS * 1_000_000, observed_ns=MS * 1_000_000, coverage=None)
    assert path._engine is None and path._last_observed_ns == 0
