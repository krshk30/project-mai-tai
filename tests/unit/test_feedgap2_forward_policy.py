"""Forward-only late-print policy and consumer-instance observation receipts."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import inspect
import json
import logging
import textwrap
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.momentum_paper import warning_consumer as runtime
from project_mai_tai.momentum_paper.models import TradePrint
from project_mai_tai.momentum_paper.warning_window import CoverageEvidence
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_feedgap1_cause import REAL_CALLBACKS
from tests.unit.test_momentum_warning import START, EPOCH, bar, consumer, trade


def active_model():
    model = consumer()
    for item in (bar(), bar(1, high="12")):
        model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    assert len(model.engine.active_events) == 2
    return model


def observe(model, item, *, age_ns):
    stamp = item.sip_ts_ms * 1_000_000
    return model.observe_trade(item, sip_timestamp_ns=stamp, observed_ns=stamp + age_ns)


def assert_late_no_evidence():
    model = active_model()
    before = model.engine.active_events
    records = observe(model, trade(2001, "100"), age_ns=1_000_000_001)
    assert records == ()
    assert model.last_status == "raw_future_or_stale"
    assert model.engine.active_events == before
    assert model._last_raw_ns == model._raw_first_ms == model._raw_coverage == {}
    assert model.late_raw_ignored_by_symbol == {"TEST": 1}
    return model


def test_late_print_counts_once_without_fill_exit_path_or_gap_ingest(monkeypatch):
    model = assert_late_no_evidence()
    def forbidden(*args, **kwargs):
        pytest.fail("late callback reached paper evidence or gap marking")
    monkeypatch.setattr(model.engine, "ingest", forbidden)
    monkeypatch.setattr(model.engine, "mark_feed_gap", forbidden)
    monkeypatch.setattr(model, "_bucket_signals", forbidden)
    assert observe(model, trade(2002, "100"), age_ns=2_000_000_000) == ()
    assert model.late_raw_ignored_by_symbol == {"TEST": 2}


def test_late_after_fill_cannot_exit_or_poison_a_path():
    model = active_model()
    observe(model, trade(2001, "12.1"), age_ns=0)
    before = model.engine.active_events
    assert all(row["fill"] is not None and row["exit"] is None for row in before)
    assert observe(model, trade(2002, "100"), age_ns=1_000_000_001) == ()
    assert model.engine.active_events == before
    assert not any(event.feed_gap for event in model.engine._active.values())


def test_five_retained_scni_callbacks_count_five_without_claiming_full_day_or_grade(monkeypatch):
    model = consumer()
    model.candidates.desired_symbols = lambda active: {"SCNI"}  # Controlled ownership.
    def forbidden(*args, **kwargs):
        pytest.fail("retained late callback entered evidence or gap marking")
    monkeypatch.setattr(model.engine, "ingest", forbidden)
    monkeypatch.setattr(model.engine, "mark_feed_gap", forbidden)
    for _, sip_ms, observed_ms, price, size in REAL_CALLBACKS:
        item = TradePrint("SCNI", sip_ms, Decimal(price), size, eligible=True)
        assert observe(model, item, age_ns=(observed_ms - sip_ms) * 1_000_000) == ()
    sample = model.late_raw_ignored_metrics()
    assert sample["late_raw_ignored_total"] == 5
    assert sample["late_raw_ignored_by_symbol"] == {"SCNI": 5}
    assert not model.engine.completed_events  # No historical grade rewrite/replay claim.


def test_exact_one_second_is_usable_but_one_ns_over_is_counted():
    model = active_model()
    rows = observe(model, trade(2001, "12.1"), age_ns=1_000_000_000)
    assert sum(row.event_type == "FILLED" for row in rows) == 2
    assert not model.late_raw_ignored_by_symbol
    assert observe(model, trade(2001, "12.1"), age_ns=1_000_000_001) == ()
    assert model.late_raw_ignored_by_symbol == {"TEST": 1}
    assert not any(event.feed_gap for event in model.engine._active.values())


@pytest.mark.parametrize("future_ns", [1, 1_000_000])
def test_future_refusal_remains_scoped_gap_and_never_late_count(future_ns):
    model = active_model()
    rows = observe(model, trade(2001, "100"), age_ns=-future_ns)
    assert model.last_status == "raw_future_or_stale"
    assert len(rows) == 2 and {row.event_type for row in rows} == {"FEED_GAP"}
    assert not model.late_raw_ignored_by_symbol
    assert all(event.feed_gap for event in model.engine._active.values())
    assert all(row["fill"] is None for row in model.engine.active_events)
    assert model._last_raw_ns == {}


def test_only_owned_eligible_symbol_callbacks_count_not_unknown_or_bad_timestamp():
    model = consumer()
    assert observe(model, trade(2001), age_ns=2_000_000_000) == ()
    assert model.last_status == "raw_symbol_not_warning_owned"
    assert not model.late_raw_ignored_by_symbol
    model.candidates.desired_symbols = lambda active: {"TEST"}
    with pytest.raises(ValueError, match="raw_timestamp_mismatch"):
        model.observe_trade(trade(2001), sip_timestamp_ns=(START + 2002) * 1_000_000,
                             observed_ns=(START + 4000) * 1_000_000)
    assert not model.late_raw_ignored_by_symbol


def test_top_five_and_total_are_cumulative_with_consumer_start_and_epoch_scope():
    model = consumer()
    symbols = ("ALFA", "BRAVO", "CHARL", "DELTA", "ECHO", "FOXT")
    model.candidates.desired_symbols = lambda active: set(symbols)
    for count, symbol in enumerate(symbols, 1):
        for _ in range(count):
            assert observe(model, replace(trade(2001), symbol=symbol), age_ns=2_000_000_000) == ()
    sample = model.late_raw_ignored_metrics()
    assert sample["late_raw_ignored_total"] == 21
    assert sample["late_raw_ignored_by_symbol"] == {
        "FOXT": 6, "ECHO": 5, "DELTA": 4, "CHARL": 3, "BRAVO": 2}
    assert sample["late_raw_ignored_scope"] == "CONSUMER_INSTANCE_CUMULATIVE_NOT_FULL_DAY"
    assert sample["late_raw_ignored_since_ms"] == model.engine.coverage_started_ms
    assert sample["late_raw_ignored_current_epoch"] is None
    assert model.late_raw_ignored_metrics() == sample
    assert consumer().late_raw_ignored_metrics()["late_raw_ignored_total"] == 0


@pytest.mark.parametrize("kind", ["sequence", "drops", "epoch"])
def test_genuine_aggregate_gap_preserved_logs_exact_old_new_values_once(kind, caplog):
    model = active_model()
    observe(model, trade(2001, "12.1"), age_ns=0)
    observe(model, trade(2002), age_ns=2_000_000_000)
    changes = {"sequence": {"sequence": 4}, "drops": {"dropped_total": 1},
               "epoch": {"gateway_instance_id": "replacement\nidentity"}}
    item = replace(bar(2), **changes[kind])
    with caplog.at_level(logging.INFO, logger=runtime.__name__):
        _, rows = model.observe_aggregate(item, observed_ns=item.published_utc_ns,
                                           legacy_active=set())
        model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    assert sum(row.event_type == "FEED_GAP" for row in rows) == 2
    assert model.windows["TEST"].retained_buckets == 1
    logs = [r.getMessage() for r in caplog.records if "FEEDGAP2-AGGREGATE-GAP" in r.getMessage()]
    assert len(logs) == 1 and "\n" not in logs[0]
    for text in ("callsite=warning_consumer.observe_aggregate", "old_sequence=2",
                 f"new_sequence={item.sequence}", f"old_epoch={EPOCH!r}",
                 f"new_epoch={item.epoch!r}", "old_dropped_total=0",
                 f"new_dropped_total={item.dropped_total}", "scope=global"):
        assert text in logs[0]
    sample = model.late_raw_ignored_metrics()
    assert sample["late_raw_ignored_total"] == 1
    assert sample["late_raw_ignored_current_epoch"] == item.epoch
    assert sample["late_raw_ignored_epoch_semantics"] == "CURRENT_EPOCH_NOT_COUNTER_RESET"
    terminal = model.engine.close_session(START + 602_001)
    assert {row.event_type for row in terminal} == {"UNANSWERABLE"}
    assert {row.payload["reason"] for row in terminal} == {"feed_gap"}


def test_genuine_explicit_raw_coverage_gap_remains_gap_after_late_ignore():
    model = active_model()
    observe(model, trade(2001), age_ns=2_000_000_000)
    model._raw_coverage["TEST"] = CoverageEvidence(EPOCH, START, START + 2000, 0, True)
    rows = observe(model, trade(2002, "12.1"), age_ns=0)
    assert sum(row.event_type == "FEED_GAP" for row in rows) == 2
    assert all(event.feed_gap for event in model.engine._active.values())


def test_restore_late_gap_marking_mutation_fails_positive_control(monkeypatch):
    assert_late_no_evidence()
    source = textwrap.dedent(inspect.getsource(runtime.MomentumWarningConsumer.observe_trade))
    mutated = source.replace('self.last_status = "raw_future_or_stale"\n        return ()',
        'self.last_status = "raw_future_or_stale"\n        return self.engine.mark_feed_gap(trade.sip_ts_ms, observed_ns // 1_000_000, symbol=symbol)')
    assert mutated != source
    namespace = dict(runtime.__dict__)
    exec(compile(mutated, "<feedgap2-restore-late-gap>", "exec"), namespace)
    monkeypatch.setattr(runtime.MomentumWarningConsumer, "observe_trade", namespace["observe_trade"])
    with pytest.raises(AssertionError):
        assert_late_no_evidence()


def test_remove_late_counter_mutation_fails_positive_control(monkeypatch):
    source = textwrap.dedent(inspect.getsource(runtime.MomentumWarningConsumer.observe_trade))
    mutated = source.replace("self.late_raw_ignored_by_symbol[symbol] += 1", "pass")
    assert mutated != source
    namespace = dict(runtime.__dict__)
    exec(compile(mutated, "<feedgap2-remove-late-counter>", "exec"), namespace)
    monkeypatch.setattr(runtime.MomentumWarningConsumer, "observe_trade", namespace["observe_trade"])
    with pytest.raises(AssertionError):
        assert_late_no_evidence()


def test_remove_future_gap_mutation_fails_future_twin(monkeypatch):
    source = textwrap.dedent(inspect.getsource(runtime.MomentumWarningConsumer.observe_trade))
    mutated = source.replace(
        'return self.engine.mark_feed_gap(\n            trade.sip_ts_ms, observed_ns // 1_000_000, symbol=symbol,\n        )',
        'return ()',
    )
    assert mutated != source
    namespace = dict(runtime.__dict__)
    exec(compile(mutated, "<feedgap2-remove-future-gap>", "exec"), namespace)
    monkeypatch.setattr(runtime.MomentumWarningConsumer, "observe_trade", namespace["observe_trade"])
    with pytest.raises(AssertionError):
        test_future_refusal_remains_scoped_gap_and_never_late_count(1)


def test_remove_global_gap_mutation_fails_genuine_gap_twin(monkeypatch, caplog):
    source = textwrap.dedent(inspect.getsource(runtime.MomentumWarningConsumer.observe_aggregate))
    mutated = source.replace(
        'records = self.engine.mark_feed_gap(self.engine.coverage_started_ms, now_ms)',
        'records = ()',
    )
    assert mutated != source
    namespace = dict(runtime.__dict__)
    exec(compile(mutated, "<feedgap2-remove-global-gap>", "exec"), namespace)
    monkeypatch.setattr(runtime.MomentumWarningConsumer, "observe_aggregate", namespace["observe_aggregate"])
    with pytest.raises(AssertionError):
        test_genuine_aggregate_gap_preserved_logs_exact_old_new_values_once("sequence", caplog)


@pytest.mark.asyncio
async def test_existing_ten_second_sample_exports_cumulative_counts_without_extra_io(monkeypatch):
    redis = AsyncMock()
    service = MomentumPaperService(Settings(_env_file=None, market_data_subscription_startup_enabled=False),
                                   redis_client=redis)
    model = consumer()
    model.candidates.desired_symbols = lambda active: {"TEST"}
    observe(model, trade(2001), age_ns=2_000_000_000)
    service._warning = model
    service._engine = consumer().engine
    service._persist = AsyncMock()
    service._sync_gateway_subscriptions = AsyncMock()
    now_ns = (START + 4001) * 1_000_000
    service._clock = lambda: datetime.fromtimestamp(now_ns / 1e9, tz=UTC)
    service._session_date = service._clock().date()
    service._last_heartbeat = service._clock()
    service._last_warning_sample_ns = now_ns
    service._gateway_task = object()  # Never start a feed or provider.
    monkeypatch.setattr("project_mai_tai.services.momentum_paper_app.wall_time.time_ns", lambda: now_ns)
    try:
        now_ns += 9_000_000_000
        await service._tick()
        redis.xadd.assert_not_awaited()
        now_ns += 1_000_000_000
        await service._tick()
        assert redis.xadd.await_count == 1
        sample = json.loads(redis.xadd.await_args.args[1]["data"])
        assert sample["event_type"] == "consumer_sample"
        assert sample["late_raw_ignored_total"] == 1
        assert sample["late_raw_ignored_by_symbol"] == {"TEST": 1}
        assert sample["late_raw_ignored_since_ms"] == model.engine.coverage_started_ms
        service._last_heartbeat = service._clock()
        now_ns += 10_000_000_000
        await service._tick()
        assert redis.xadd.await_count == 2
        assert json.loads(redis.xadd.await_args.args[1]["data"])["late_raw_ignored_total"] == 1
    finally:
        service._gateway_task = None
