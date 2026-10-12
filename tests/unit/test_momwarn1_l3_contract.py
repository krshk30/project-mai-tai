"""Small CONTROLLED composition tests, not recorded burst/arrival evidence."""

import copy
from datetime import UTC, date, datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from massive.websocket.models import EquityAgg
import pytest

from project_mai_tai.market_data.massive_provider import MassiveTradeStream
from project_mai_tai.market_data.publisher import MarketDataPublisher
from project_mai_tai.events import (
    MarketSnapshotPayload, SnapshotBatchEvent, SnapshotBatchPayload, TradeTickEvent, TradeTickPayload,
)
from project_mai_tai.momwarn_measurements import MeasurementCollector, UNKNOWN
from project_mai_tai.momwarn_metric_contract import paper_metric_rows
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import MomentumTapeRecord
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_momentum_warning import EPOCH, START, consumer, trade
from tests.unit.test_momentum_paper_service import _condition_snapshot

NS = START * 1_000_000
DAY = date(2026, 10, 12)


def metric(status="DETECTED", *, at=NS + 2_000_000_000, code="momentum_30s_warn"):
    summary = dict(strategy_code=code, window_seconds=30, logical_id="controlled-lineage",
                   symbol="TEST", status="PENDING_FILL", observed_decision_ns=NS + 1_000_000_000,
                   detect=dict(start_ms=START, end_ms=START + 1000, price="10",
                               signal_field="close"))
    return dict(version=1, event_type="decision", source="momentum-paper", observed_ns=at,
                gateway_instance_id=EPOCH[0], generation=1, strategy_code=code,
                logical_id=summary["logical_id"], symbol="TEST", status=status,
                observed_decision_ns=at, payload=summary, subscription_confirmed_ns=None)


@pytest.mark.asyncio
async def test_controlled_actual_provider_redis_g2_metrics_g3_composition(monkeypatch):
    observed = NS + 1_000_000_000
    monkeypatch.setattr("time.time_ns", lambda: observed)
    sdk = EquityAgg.from_dict(dict(ev="A", sym="TEST", s=START, e=START + 1000,
                                   o=8, h=10, l=8, c=10, v=100))
    aggregate = MassiveTradeStream._normalize_warning_aggregate(sdk, "TEST")
    assert aggregate is not None
    provider_redis = AsyncMock()
    publisher = MarketDataPublisher(provider_redis, "controlled", "market-data-gateway")
    await publisher.publish_momentum_warning_aggregate(
        aggregate, sequence=1, dropped_total=0, gateway_instance_id=EPOCH[0], generation=1,
    )
    service = MomentumPaperService.__new__(MomentumPaperService)
    service.settings, service.redis = Settings(market_data_momentum_warning_enabled=True), AsyncMock()
    service._warning = consumer()
    service._engine = MomentumPaperEngine(prior_closes=service._warning.engine.prior_closes,
                                          condition_version="controlled", coverage_started_ms=START)
    service._persist, service._sync_gateway_subscriptions = AsyncMock(), AsyncMock()
    await service._handle_warning_event(provider_redis.xadd.await_args.args[1]["data"])
    packet = json.loads(service.redis.xadd.await_args.args[1]["data"])
    collector = MeasurementCollector(DAY)
    assert collector.record(packet, read_ns=observed, stream_id="1-0")
    assert len(collector.details) == len(collector.decisions) == 2
    assert all(row["warning_signal_basis"] == "bucket_sufficient"
               for row in collector.comparison_rows())
    assert all(row["warning_signal_sip_ns"] == UNKNOWN for row in collector.comparison_rows())

    sip = NS + 1_001_000_123  # Actual controlled callback ns, not invented bucket low time.
    fills = service._warning.observe_trade(trade(1001, "10.1"),
                                           sip_timestamp_ns=sip, observed_ns=sip)
    for index, record in enumerate(row for row in fills if row.event_type == "FILLED"):
        await service._warning_metrics(
            event_type="decision", observed_ns=sip, observed_decision_ns=sip,
            gateway_instance_id=EPOCH[0], generation=1, symbol=record.symbol,
            strategy_code=record.strategy_code, logical_id=record.logical_id,
            status=record.event_type, signal_sip_ns=sip, payload=record.payload,
        )
        packet = json.loads(service.redis.xadd.await_args.args[1]["data"])
        assert collector.record(packet, read_ns=sip, stream_id=f"2-{index}")
    rows = collector.comparison_rows()
    assert len(rows) == 2
    assert len(collector.decisions) == 2  # FILLED is an update, not a new signal.
    assert all(row["warning_entry_ns"] == UNKNOWN and row["warning_entry_price"] == "10.1"
               for row in rows)
    assert all(row["warning_callback_sip_ns"] == sip for row in rows)
    assert all(row["warning_entry_timestamp_precision"] == "integer_sip_ms" for row in rows)
    assert all(row["warning_observeddecision_ns"] == observed for row in rows)
    assert all(row["pair_status"] == "COUNTERPART_UNKNOWN" for row in rows)
    service._sync_gateway_subscriptions.assert_awaited_once()


def test_candidate_without_buy_is_not_a_fictitious_entry():
    packet = metric()
    packet.pop("strategy_code")
    packet.pop("payload")
    packet["signals"] = []
    collector = MeasurementCollector(DAY)
    assert collector.record(packet, read_ns=packet["observed_ns"], stream_id="candidate")
    assert not collector.decisions and not collector.details


@pytest.mark.parametrize("change", [
    {"gateway_instance_id": None}, {"generation": None},
    {"observed_ns": NS + 3_000_000_000},
])
def test_native_missing_epoch_or_future_observation_cannot_borrow_gateway(change):
    collector = MeasurementCollector(DAY)
    packet = {**metric(), **change}
    assert not collector.record(packet, read_ns=NS + 2_000_000_000, stream_id="unknown")
    assert not collector.decisions and not collector.details


@pytest.mark.parametrize("field", ["symbol", "window_seconds", "logical_id", "strategy_code"])
def test_native_summary_identity_mismatch_refused(field):
    packet = metric()
    packet["payload"][field] = "wrong"
    collector = MeasurementCollector(DAY)
    assert not collector.record(packet, read_ns=packet["observed_ns"], stream_id="bad-summary")


def test_two_row_bound_and_stable_dedup_of_long_original_ids():
    packet = metric()
    row = {key: packet[key] for key in ("strategy_code", "logical_id", "status", "payload")}
    packet["signals"] = [copy.deepcopy(row), copy.deepcopy(row), copy.deepcopy(row)]
    with pytest.raises(ValueError, match="two ledgers"):
        paper_metric_rows(packet)
    packet.pop("signals")
    collector = MeasurementCollector(DAY)
    assert collector.record(packet, read_ns=packet["observed_ns"], stream_id="a" * 64)
    assert not collector.record(packet, read_ns=packet["observed_ns"], stream_id="a" * 64)
    assert len(collector.details) == len(collector.decisions) == 1


def test_filled_callback_sip_is_not_reused_as_exact_signal_sip():
    packet = metric("FILLED")
    packet["signal_sip_ns"] = NS + 1_500_000_123
    packet["payload"]["detect"] = dict(sip_ts_ms=START + 1000, price="10")
    packet["payload"]["fill"] = dict(sip_ts_ms=START + 1500, price="10.1")
    row, = paper_metric_rows(packet)
    assert row["signal_basis"] == "integer_ms_bounds" and row["signal_sip_ns"] is None
    assert row["signal_sip_start_ns"] == NS + 1_000_000_000
    assert row["entry_ns"] is None
    assert row["callback_sip_ns"] == packet["signal_sip_ns"]
    assert row["entry_timestamp_precision"] == "integer_sip_ms"


def test_raw_detection_keeps_callback_ns_separate_from_snapshot_signal_bounds():
    packet = metric(code="momentum_30s")
    packet["event_type"] = "snapshot_entry"
    packet["signal_sip_ns"] = NS + 1_000_000_123
    packet["payload"]["detect"] = dict(sip_ts_ms=START + 1000, price="10")
    row, = paper_metric_rows(packet)
    assert row["ledger"] == "momentum_30s" and row["signal_basis"] == "integer_ms_bounds"
    assert row["signal_sip_ns"] is None
    assert row["callback_sip_ns"] == packet["signal_sip_ns"]
    assert row["observeddecision_ns"] == packet["observed_decision_ns"]
    assert "comparison_id" not in row and "clock_status" not in row


def test_delayed_exit_does_not_use_confirmation_callback_as_exit_timestamp():
    packet = metric("EXITED")
    packet["signal_sip_ns"] = NS + 1_900_000_123
    packet["payload"]["exit"] = dict(sip_ts_ms=START + 1800, price="10.6")
    row, = paper_metric_rows(packet)
    assert row["exit_ns"] is None
    assert row["exit_sip_start_ns"] == NS + 1_800_000_000
    assert row["exit_timestamp_precision"] == "integer_sip_ms"


def test_native_lifecycle_without_callback_ns_keeps_only_ms_bounds():
    packet = metric("FILLED")
    packet["payload"]["fill"] = dict(sip_ts_ms=START + 1500, price="10.1")
    row, = paper_metric_rows(packet)
    assert row["entry_ns"] is None and row["entry_price"] == "10.1"
    assert row["entry_timestamp_precision"] == "integer_sip_ms"
    assert row["entry_sip_end_ns"] - row["entry_sip_start_ns"] == 1_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize("status,field,stem", [("FILLED", "fill", "entry"), ("EXITED", "exit", "exit")])
async def test_actual_snapshot_lifecycle_handler_does_not_borrow_warning_epoch_or_print_identity(
    monkeypatch, status, field, stem,
):
    stored_print_ns = NS + 1_000_000_123
    sip = NS + 1_000_000_789  # Different print in the same stored millisecond.
    monkeypatch.setattr("time.time_ns", lambda: sip)
    service = MomentumPaperService.__new__(MomentumPaperService)
    service.settings, service.redis = Settings(market_data_momentum_warning_enabled=True), AsyncMock()
    service._warning = consumer()
    service._warning._stream_epoch = EPOCH
    service._condition_snapshot = _condition_snapshot()
    service._subscribed_symbols, service._warning_subscribed_symbols = {"TEST"}, set()
    record = MomentumTapeRecord(
        event_key="controlled-snapshot:filled", logical_id="controlled-snapshot",
        strategy_code="momentum_30s", event_type=status, symbol="TEST", session_date=DAY,
        observed_at=datetime.fromtimestamp(sip / 1_000_000_000, UTC),
        payload={field: {"sip_ts_ms": START + 1000, "price": "10.1"}},
    )
    service._engine = SimpleNamespace(ingest=lambda *args, **kwargs: (record,))
    service._persist = AsyncMock()
    event = TradeTickEvent(source_service="market-data-gateway", payload=TradeTickPayload(
        symbol="TEST", price="10.1", size=100, timestamp_ns=sip,
        conditions=["1"], conditions_present=True,
    ))
    await service._handle_gateway_event(event.model_dump_json())
    packet = json.loads(service.redis.xadd.await_args.args[1]["data"])
    assert packet["gateway_instance_id"] is packet["generation"] is None
    assert packet["source_epoch_status"] == "UNKNOWN"
    assert packet["observer_warning_context"] == dict(gateway_instance_id=EPOCH[0], generation=EPOCH[1])
    assert packet["callback_source_event_id"] == str(event.event_id)
    assert packet["callback_source_service"] == event.source_service
    assert packet["callback_source_produced_at"] == event.produced_at.isoformat()
    assert "signal_sip_ns" not in packet
    collector = MeasurementCollector(DAY)
    assert collector.record(packet, read_ns=sip, stream_id="snapshot-fill")
    row, = collector.comparison_rows()
    assert row[f"snapshot_{stem}_ns"] == UNKNOWN
    assert row[f"snapshot_{stem}_sip_start_ns"] <= stored_print_ns < row[f"snapshot_{stem}_sip_end_ns"]
    assert row[f"snapshot_{stem}_timestamp_precision"] == "integer_sip_ms"
    assert row["snapshot_callback_sip_ns"] == sip
    assert row["gateway_instance_id"] == row["generation"] == UNKNOWN
    assert row["snapshot_signal_sip_ns"] == UNKNOWN
    assert row["snapshot_observeddecision_ns"] == UNKNOWN
    assert row["warning_status"] == UNKNOWN and row["pair_status"] == "COUNTERPART_UNKNOWN"


@pytest.mark.parametrize("status", ["DETECTED", "FILLED", "EXITED"])
def test_two_different_prints_in_one_ms_never_upgrade_summary_to_callback_identity(status):
    packet = metric(status)
    stored_ms = START + 1500
    stored_print_ns, callback_print_ns = stored_ms*1_000_000+123, stored_ms*1_000_000+789
    packet["callback_sip_ns"] = callback_print_ns
    packet["payload"]["detect"] = dict(sip_ts_ms=stored_ms, price="10.1")
    packet["payload"]["fill"] = dict(sip_ts_ms=stored_ms, price="10.1")
    packet["payload"]["exit"] = dict(sip_ts_ms=stored_ms, price="10.1")
    row, = paper_metric_rows(packet)
    assert stored_print_ns != callback_print_ns
    assert row["callback_sip_ns"] == callback_print_ns
    assert row["signal_sip_ns"] is row["entry_ns"] is row["exit_ns"] is None
    assert row["signal_basis"] == "integer_ms_bounds"
    assert row["entry_sip_start_ns"] <= stored_print_ns < row["entry_sip_end_ns"]
    collector = MeasurementCollector(DAY)
    assert collector.record(packet, read_ns=packet["observed_ns"], stream_id=f"same-ms-{status}")
    detail, = collector.comparison_rows()
    assert detail["warning_callback_sip_ns"] == callback_print_ns
    assert all(detail[f"warning_{field}"] == UNKNOWN for field in ("signal_sip_ns", "entry_ns", "exit_ns"))


@pytest.mark.asyncio
async def test_queued_old_snapshot_after_new_warning_epoch_retains_original_envelope_only(monkeypatch):
    received = NS+5_000_000_000
    monkeypatch.setattr("time.time_ns", lambda: received)
    original = SnapshotBatchEvent(
        source_service="market-data-gateway",
        produced_at=datetime.fromtimestamp((NS+3_000_000_000)/1_000_000_000, UTC),
        payload=SnapshotBatchPayload(
            snapshots=[MarketSnapshotPayload(symbol="TEST", last_trade_price="10",
                                             last_trade_timestamp_ns=NS+1_000_000_123)],
            completed_at=datetime.fromtimestamp((NS+4_000_000_000)/1_000_000_000, UTC),
        ),
    )
    service = MomentumPaperService.__new__(MomentumPaperService)
    service.settings, service.redis = Settings(market_data_momentum_warning_enabled=True), AsyncMock()
    service._warning = consumer()
    service._warning._stream_epoch = ("new-warning-after-reconnect", 99)
    summary = metric(code="momentum_30s")["payload"]
    record = MomentumTapeRecord(
        event_key="queued-snapshot:detected", logical_id=summary["logical_id"],
        strategy_code="momentum_30s", event_type="DETECTED", symbol="TEST", session_date=DAY,
        observed_at=original.produced_at, payload=summary,
    )
    service._engine = SimpleNamespace(active_symbols=set(),
                                      detect_from_snapshots=lambda *args, **kwargs: ((record,), 0))
    service._condition_snapshot = _condition_snapshot()
    service._clock = lambda: datetime.fromtimestamp(received/1_000_000_000, UTC)
    service._feed_gap_started_ms, service._subscription_cap_skips = None, 0
    service._persist, service._sync_gateway_subscriptions = AsyncMock(), AsyncMock()
    await service._handle_gateway_event(original.model_dump_json())
    packet = json.loads(service.redis.xadd.await_args.args[1]["data"])
    assert packet["snapshot_source_event_id"] == str(original.event_id)
    assert packet["snapshot_source_produced_at"] == original.produced_at.isoformat()
    assert packet["snapshot_last_trade_sip_ns"] == original.payload.snapshots[0].last_trade_timestamp_ns
    assert packet["gateway_instance_id"] is packet["generation"] is None
    assert packet["observer_warning_context"] == dict(gateway_instance_id="new-warning-after-reconnect", generation=99)
    collector = MeasurementCollector(DAY)
    assert collector.record(packet, read_ns=received, stream_id="queued-old-snapshot")
    row, = collector.comparison_rows()
    assert row["gateway_instance_id"] == row["generation"] == UNKNOWN
    assert row["snapshot_snapshot_source_event_id"] == str(original.event_id)
    assert row["pair_status"] == "COUNTERPART_UNKNOWN"
    warning = metric(at=received)
    warning.update(gateway_instance_id="new-warning-after-reconnect", generation=99)
    assert collector.record(warning, read_ns=received, stream_id="new-warning")
    assert len(collector.comparison_rows()) == 2
    assert all(item["pair_status"] == "COUNTERPART_UNKNOWN" for item in collector.comparison_rows())
