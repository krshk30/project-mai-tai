import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D
import json
import logging
from unittest.mock import AsyncMock

from massive.websocket.models import EquityAgg
import pytest

from project_mai_tai.market_data.massive_provider import MassiveTradeStream
from project_mai_tai.market_data.publisher import MarketDataPublisher
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.momentum_paper.warning_window import (
    AggregateObservation,
    ObservedATransport,
    ObservedRawPaperPath,
    WarningWindow,
)
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_momentum_paper_service import _store
from tests.unit.test_momentum_warning import EPOCH, START, bar, consumer, trade


@pytest.mark.asyncio
@pytest.mark.parametrize("start", [1610144868000, 1610144868001])
async def test_actual_g1_factory_publisher_to_g2_preserves_exclusive_bounds(start, monkeypatch):
    # Official sample shape; controlled local clocks, not recorded provider arrivals.
    received = (start + 1000)*1_000_000
    monkeypatch.setattr("project_mai_tai.market_data.massive_provider.time.time_ns", lambda: received)
    message = EquityAgg.from_dict(dict(ev="A", sym="SPCE", v=200,
                                      o=25.39, h=25.39, l=25.39, c=25.39,
                                      s=start, e=start+1000))
    record = MassiveTradeStream._normalize_warning_aggregate(message, "SPCE")
    assert record is not None
    redis = AsyncMock()
    publisher = MarketDataPublisher(redis, "fixture", "market-data-gateway")
    await publisher.publish_momentum_warning_aggregate(
        record, sequence=1, dropped_total=0, gateway_instance_id=EPOCH[0], generation=1,
    )
    payload = json.loads(redis.xadd.await_args.args[1]["data"])
    parsed = AggregateObservation.from_payload(payload)
    assert (record.start_ms, record.end_ms) == (parsed.start_ms, parsed.end_ms) == (start, start+1000)
    assert payload["provider_kind"] == "massive_ws_a"
    assert not hasattr(parsed, "sip_ts_ms")
    assert not hasattr(parsed, "low_at")
    window = WarningWindow("SPCE")
    window.observe(parsed, observed_ns=received)
    assert not window.whole_buckets(observed_ms=start+999, seconds=30)
    assert window.whole_buckets(observed_ms=start+1000, seconds=30) == (parsed,)


@pytest.mark.parametrize("delta", [0, 1, 500, 998, 999, 1001])
def test_g1_and_g2_both_reject_unsupported_partial_and_999_intervals(delta):
    message = EquityAgg.from_dict(dict(ev="A", sym="TEST", v=100,
                                      o=10, h=10, l=10, c=10, s=START, e=START+delta))
    assert MassiveTradeStream._normalize_warning_aggregate(message, "TEST") is None
    with pytest.raises(ValueError, match="metadata"):
        replace(bar(), end_ms=START+delta)


@pytest.mark.parametrize("open_price,close,detects", [
    ("10", "8", False), ("8", "10", True), ("10", "10", True),
])
def test_falling_rising_and_recovered_close_have_causal_entry_semantics(open_price, close, detects):
    raw = MomentumPaperEngine(prior_closes={"TEST": D("10")}, condition_version="fixture",
                              coverage_started_ms=START-60_000)
    raw_records = []
    path = [(1, open_price), (500, "8"), (990, close)]
    for offset, price in path:
        raw_records.extend(raw.ingest(trade(offset, price)))
    assert bool([row for row in raw_records if row.event_type == "DETECTED"]) is detects
    model = consumer()
    item = replace(bar(low="8", high="10"), open=D(open_price), close=D(close))
    outcome, records = model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    assert outcome.status == "ADMITTED"  # A high-range false warning is allowed; no false BUY.
    detected = [row for row in records if row.event_type == "DETECTED"]
    assert bool(detected) is detects
    if detects:
        assert len(detected) == 2
        for row in detected:
            assert row.payload["detect"]["price"] == close
            assert row.payload["detect"]["signal_field"] == "close"
            assert row.payload["detect"]["price_provenance"] == "qualifying_bucket_last_close"
            assert row.payload["detect"]["start_ms"] == START
            assert row.payload["detect"]["end_ms"] == START+1000
            assert row.payload["detect"]["observed_decision_ns"] == item.published_utc_ns
            assert row.payload["detect"]["exact_signal_sip_timestamp"] is None
            assert row.payload["reference"]["exact_low_sip_timestamp"] is None
        fills = model.observe_trade(trade(1001, "10.1"),
                                    sip_timestamp_ns=(START+1001)*1_000_000,
                                    observed_ns=(START+1001)*1_000_000)
        assert sum(row.event_type == "FILLED" for row in fills) == 2
        assert all(row.payload["fill"]["price"] == "10.1" for row in fills if row.event_type == "FILLED")
        assert all(row.payload["fill"]["sip_ts_ms"] == START+1001
                   for row in fills if row.event_type == "FILLED")
    else:
        assert not model.engine.active_events
    print(f"CAUSAL_CLOSE O={open_price}/H=10/L=8/C={close} detects={len(detected)} "
          f"source=qualifying_bucket_last_close bounds=[{START},{START+1000}) "
          f"decision_ns={item.published_utc_ns} exact_low_SIP=None "
          f"fill={str(START+1001)+'@10.1' if detects else 'NONE'} controlled_arrivals=True")


def test_actual_trade_trigger_keeps_trade_price_not_aggregate_close():
    model = consumer()
    item = bar(high="11")
    model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    records = model.observe_trade(trade(1001, "12"),
                                  sip_timestamp_ns=(START+1001)*1_000_000,
                                  observed_ns=(START+1001)*1_000_000)
    detected = [row for row in records if row.event_type == "DETECTED"]
    assert len(detected) == 2
    assert all(row.payload["detect"]["price"] == "12" for row in detected)
    assert all(row.payload["reference_provenance"]["price"] == "10" for row in detected)


def test_late_older_close_cannot_use_a_subsequent_bucket_low():
    model = consumer()
    for item in (bar(), bar(1, low="8", high="8")):
        model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    corrected_older = replace(bar(), sequence=3, received_utc_ns=bar(1).published_utc_ns,
                              published_utc_ns=bar(1).published_utc_ns)
    outcome, records = model.observe_aggregate(corrected_older,
                                               observed_ns=corrected_older.published_utc_ns,
                                               legacy_active=set())
    assert outcome.status == "ADMITTED"  # False high-based warning allowed, older-close BUY is not.
    assert not any(row.event_type == "DETECTED" for row in records)
    assert not model.engine.active_events


def test_future_sip_trade_cannot_enter_reference_history_or_create_entry():
    model = consumer()
    initial = bar(high="11")
    model.observe_aggregate(initial, observed_ns=initial.published_utc_ns, legacy_active=set())
    assert not model.observe_trade(trade(3000, "1"), sip_timestamp_ns=(START+3000)*1_000_000,
                                    observed_ns=(START+2000)*1_000_000)
    assert model.last_status == "raw_future_or_stale"
    assert not model.engine._eligible_history
    current = bar(1, high="11.9")
    _, records = model.observe_aggregate(current, observed_ns=current.published_utc_ns, legacy_active=set())
    assert not any(row.event_type == "DETECTED" for row in records)


def test_old_close_never_borrows_raw_trade_from_after_its_bucket():
    model = consumer()
    initial = bar(high="11")
    model.observe_aggregate(initial, observed_ns=initial.published_utc_ns, legacy_active=set())
    model.observe_trade(trade(1500, "8"), sip_timestamp_ns=(START+1500)*1_000_000,
                        observed_ns=(START+1500)*1_000_000)
    assert model.engine._eligible_history["TEST"][0].price == D("8")
    older_close = replace(bar(), sequence=2, received_utc_ns=(START+1800)*1_000_000,
                          published_utc_ns=(START+1800)*1_000_000)
    _, records = model.observe_aggregate(older_close, observed_ns=older_close.published_utc_ns,
                                         legacy_active=set())
    assert not any(row.event_type == "DETECTED" for row in records)
    assert not model.engine.active_events


def test_future_bucket_low_is_excluded_until_its_exclusive_end():
    window = WarningWindow("TEST")
    current = bar(high="11")
    future = replace(bar(1, low="1", high="1"),
                     received_utc_ns=current.published_utc_ns,
                     published_utc_ns=current.published_utc_ns)
    window.observe(current, observed_ns=current.published_utc_ns)
    window.observe(future, observed_ns=current.published_utc_ns)
    assert window.whole_buckets(observed_ms=START+1000, seconds=30) == (current,)
    assert window.evaluate(observed_ms=START+1000).windows[0].low == D("10")
    assert future not in window.whole_buckets(observed_ms=START+1999, seconds=30)


def test_transport_requires_checkpoint_and_does_not_advance_on_empty_read():
    transport = ObservedATransport()
    first = bar()
    transport.observe(first, observed_ns=first.published_utc_ns)
    assert transport.evidence(observed_ns=first.published_utc_ns) is None
    transport.checkpoint("1-0", observed_ns=first.published_utc_ns)
    initial = transport.evidence(observed_ns=first.published_utc_ns)
    assert initial.lineage == "observed_transport"
    transport.checkpoint("1-0", observed_ns=first.published_utc_ns+1_000_000_000)
    assert transport.evidence(observed_ns=first.published_utc_ns+1_000_000_000).through_ms == initial.through_ms
    transport.checkpoint("1-0", observed_ns=first.published_utc_ns+3_000_000_000)
    assert transport.evidence(observed_ns=first.published_utc_ns+3_000_000_000) is None


def test_fresh_record_after_stale_progress_starts_new_transport_span():
    transport = ObservedATransport()
    first = bar()
    transport.observe(first, observed_ns=first.published_utc_ns)
    transport.checkpoint("1-0", observed_ns=first.published_utc_ns)
    later = replace(bar(10), sequence=2)
    transport.observe(later, observed_ns=later.published_utc_ns)
    transport.checkpoint("2-0", observed_ns=later.published_utc_ns)
    evidence = transport.evidence(observed_ns=later.published_utc_ns)
    assert evidence.start_ms == START+11_000
    assert evidence.lineage == "observed_transport"


@pytest.mark.parametrize("fault", ["sequence", "drop", "epoch", "read_failure", "stale", "cursor"])
def test_transport_faults_reset_prefix_or_fail_closed(fault):
    transport = ObservedATransport()
    first = bar()
    transport.observe(first, observed_ns=first.published_utc_ns)
    transport.checkpoint("2-0", observed_ns=first.published_utc_ns)
    current = bar(1)
    if fault == "sequence":
        current = replace(current, sequence=4)
    elif fault == "drop":
        current = replace(current, dropped_total=1)
    elif fault == "epoch":
        current = replace(current, generation=2)
    elif fault == "read_failure":
        transport.invalidate("xread_failure")
        assert transport.evidence(observed_ns=current.published_utc_ns) is None
        return
    elif fault == "stale":
        assert transport.evidence(observed_ns=current.published_utc_ns+2_000_000_000) is None
        return
    elif fault == "cursor":
        transport.checkpoint("1-0", observed_ns=current.published_utc_ns)
        assert transport.evidence(observed_ns=current.published_utc_ns) is None
        return
    transport.observe(current, observed_ns=current.published_utc_ns)
    assert transport.evidence(observed_ns=current.published_utc_ns) is None
    transport.checkpoint("3-0", observed_ns=current.published_utc_ns)
    assert transport.evidence(observed_ns=current.published_utc_ns).start_ms == START+2000


def _quiet_prefix(model, *, legacy_active=frozenset(), fault_at=None):
    outcomes = []
    for offset in range(122):
        item = bar(offset, high="11" if offset == 0 else "10")
        model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set(legacy_active))
        if offset == fault_at:
            model.invalidate_transport("controlled_xread_failure")
        else:
            model.complete_read(f"{START+(offset+1)*1000}-0", observed_ns=item.published_utc_ns)
        outcomes.extend(model.expire_quiet(observed_ms=START+(offset+1)*1000,
                                           legacy_active=set(legacy_active)))
    return outcomes


def test_positive_observed_transport_releases_quiet_candidate_after_full_proof():
    model = consumer()
    outcomes = _quiet_prefix(model)
    dropped = [row for row in outcomes if row.status == "DROPPED"]
    assert len(dropped) == 1 and dropped[0].reason == "both_windows_cool_60s"
    assert model.desired_symbols(set()) == set()
    assert model.coverage.lineage == "observed_transport"
    print("OBSERVED_TRANSPORT quiet_release_after=121s provider_ACK=None raw_complete=False")


@pytest.mark.parametrize("protected", ["active_path", "read_fault"])
def test_quiet_candidate_is_not_evicted_during_active_path_or_broken_proof(protected):
    model = consumer()
    outcomes = _quiet_prefix(model, legacy_active={"TEST"} if protected == "active_path" else set(),
                             fault_at=90 if protected == "read_fault" else None)
    assert not any(row.status == "DROPPED" for row in outcomes)
    assert "TEST" in model.desired_symbols(set())


def test_actual_filled_warning_path_is_not_evicted_by_quiet_transport():
    model = consumer()
    first = bar(high="12")
    model.observe_aggregate(first, observed_ns=first.published_utc_ns, legacy_active=set())
    filled = model.observe_trade(trade(1001, "12.1"),
                                 sip_timestamp_ns=(START+1001)*1_000_000,
                                 observed_ns=(START+1001)*1_000_000)
    assert sum(row.event_type == "FILLED" for row in filled) == 2
    assert not any(row.status == "DROPPED" for row in _quiet_prefix(model))
    assert len(model.engine.active_events) == 2
    assert all(row["fill"]["price"] == "12.1" for row in model.engine.active_events)


def test_empty_window_bounds_hold_can_exhaust_cap_without_unauthorized_idle_release():
    symbols = {f"S{index}" for index in range(16)}
    model = MomentumWarningConsumer(
        prior_closes={symbol: D("10") for symbol in symbols | {"OTHER", "NEW"}},
        condition_version="fixture", coverage_started_ms=START-60_000,
    )
    for index, symbol in enumerate(sorted(symbols)):
        item = replace(bar(high="11"), symbol=symbol, sequence=index+1)
        model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    for offset in range(1, 201):
        item = replace(bar(offset), symbol="OTHER", sequence=16+offset)
        model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
        model.complete_read(f"{offset+1}-0", observed_ns=item.published_utc_ns)
        model.expire_quiet(observed_ms=START+(offset+1)*1000, legacy_active=set())
    assert model.coverage.lineage == "observed_transport"
    assert model.desired_symbols(set()) == symbols
    assert set(model.cooling_status.values()) == {"empty_window_bounds"}
    assert not model.engine.active_events
    item = replace(bar(201, high="11"), symbol="NEW", sequence=217)
    refusal, _ = model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    assert refusal.reason == "warning_cap_full"
    print("DORMANT_CAP controlled=16held next=warning_cap_full remaining_gap=empty_window_bounds")


def test_observed_a_transport_cannot_be_promoted_to_complete_raw_history():
    transport = ObservedATransport()
    path = ObservedRawPaperPath(symbol="TEST", prior_close=D("10"), condition_version="fixture")
    for offset, price in ((0, "10"), (30_000, "10"), (60_000, "12")):
        item = bar(offset//1000)
        transport.observe(item, observed_ns=item.published_utc_ns)
        transport.checkpoint(f"{offset+1}-0", observed_ns=item.published_utc_ns)
        # Deliberately generous span still cannot turn A lineage into raw evidence.
        evidence = replace(transport.evidence(observed_ns=item.published_utc_ns),
                           start_ms=START, through_ms=START+offset)
        result = path.observe(trade(offset, price), sip_timestamp_ns=(START+offset)*1_000_000,
                              observed_ns=(START+offset)*1_000_000, coverage=evidence)
        assert result.status != "DETECTED"
        assert result.reason == "raw_coverage_incomplete"


@pytest.mark.asyncio
async def test_reader_commits_processed_xread_prefix_and_refuses_partial_without_error_flood(monkeypatch, caplog):
    redis = AsyncMock()
    service = MomentumPaperService(Settings(), store=_store(), redis_client=redis)
    service._engine = MomentumPaperEngine(prior_closes={"TEST": D("10")},
                                          condition_version="fixture", coverage_started_ms=START)
    service._warning = consumer()
    now_ns = (START+1000)*1_000_000
    monkeypatch.setattr("project_mai_tai.services.momentum_paper_app.wall_time.time_ns", lambda: now_ns)
    item = bar()
    valid = {**item.__dict__, "version": 1, "event_type": "aggregate"}
    invalid = {**valid, "end_ms": START+999}
    stream = f"{service.settings.redis_stream_prefix}:momentum-warning-bars"
    records = [("1-0", {"data": json.dumps(valid, default=str)})]
    redis.xread.side_effect = [[(stream, records)], asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await service._warning_loop()
    assert service._warning.coverage.lineage == "observed_transport"
    for _ in range(10):
        await service._handle_warning_event(json.dumps(invalid, default=str))
    assert service._warning_invalid_messages == 10
    assert service._warning.coverage is None
    warnings = [row for row in caplog.records if row.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert not any(row.levelno >= logging.ERROR for row in caplog.records)


@pytest.mark.asyncio
async def test_service_cooling_uses_checkpoints_and_emits_warning_owner_release(monkeypatch):
    redis = AsyncMock()
    service = MomentumPaperService(Settings(), store=_store(), redis_client=redis)
    service._engine = MomentumPaperEngine(prior_closes={"TEST": D("10")},
                                          condition_version="fixture", coverage_started_ms=START)
    service._warning = consumer()
    service._session_date = datetime.fromtimestamp(START/1000, tz=UTC).date()
    # No loop/socket is started; the tick treats this inert reader token as already running.
    service._gateway_task = object()
    try:
        for offset in range(122):
            item = bar(offset, high="11" if offset == 0 else "10")
            now_ns = item.published_utc_ns
            monkeypatch.setattr("project_mai_tai.services.momentum_paper_app.wall_time.time_ns", lambda: now_ns)
            service._clock = lambda: datetime.fromtimestamp(now_ns/1_000_000_000, tz=UTC)
            await service._handle_warning_event(json.dumps(
                {**item.__dict__, "version": 1, "event_type": "aggregate"}, default=str,
            ))
            service._warning.complete_read(f"{START+(offset+1)*1000}-0", observed_ns=now_ns)
            await service._tick()
    finally:
        service._gateway_task = None
    requests = [json.loads(call.args[1]["data"])["payload"] for call in redis.xadd.await_args_list
                if call.args[0].endswith(":market-data-subscriptions")]
    warning_requests = [row["symbols"] for row in requests if row["consumer_name"] == "momentum-warning"]
    assert warning_requests == [["TEST"], []]
    assert service._warning_subscribed_symbols == set()
    assert service._warning.coverage.lineage == "observed_transport"
    assert all(row["symbols"] == [] for row in requests if row["consumer_name"] == "momentum-paper")
