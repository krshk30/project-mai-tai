"""PAPERLAG1 bookkeeping and bounded telemetry, without changing admission gates."""
from dataclasses import replace
from copy import deepcopy
import inspect
import json
import logging
import textwrap
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.momentum_paper import warning_consumer as runtime
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_momentum_warning import START, bar, consumer, trade


def observe(model, item, age_ns=0):
    return model.observe_aggregate(item, observed_ns=item.published_utc_ns + age_ns,
                                   legacy_active=set())


def test_stale_then_fresh_advances_without_global_gap_or_stale_decision(caplog):
    caplog.set_level(logging.INFO, logger=runtime.__name__)
    model = consumer()
    observe(model, bar())
    stale = bar(1, high="100")
    outcome, records = observe(model, stale, 1_000_000_001)
    assert outcome.status == "REFUSED" and records == ()
    assert model._sequence == 2 and not model.engine.active_events
    assert model.coverage is None
    assert model.transport.evidence(observed_ns=stale.published_utc_ns + 1_000_000_001) is None
    model.complete_read(f"{START + 3000}-0", observed_ns=stale.published_utc_ns + 1_000_000_001)
    assert model.coverage is None
    fresh = bar(2)
    outcome, records = observe(model, fresh, 1000)
    assert not records and model._sequence == 3
    model.complete_read(f"{START + 3001}-0", observed_ns=fresh.published_utc_ns + 1000)
    assert model.coverage is not None
    assert model.coverage.start_ms >= fresh.received_utc_ns // 1_000_000
    assert "FEEDGAP2-AGGREGATE-GAP" not in caplog.text
    assert model.transport_stale_by_symbol == {"TEST": 1}


@pytest.mark.parametrize("offset", [0, 1, 2])
def test_stale_newer_clears_only_affected_symbol_older_revision_keeps_it(offset):
    model = consumer()
    model.engine.prior_closes["OTHER"] = model.engine.prior_closes["TEST"]
    observe(model, bar(1, sequence=1))
    observe(model, bar(1, symbol="OTHER", sequence=2))
    original = model.windows["TEST"]
    other = model.windows["OTHER"]
    item = bar(offset, sequence=3, high="100")
    outcome, records = observe(model, item, 1_000_000_001)
    assert outcome.status == "REFUSED" and not records
    assert model.windows["OTHER"] is other
    if offset > 1:
        assert "TEST" not in model.windows and "TEST" not in model._latest
    else:
        assert model.windows["TEST"] is original
        assert model._latest["TEST"].start_ms == START + 1000
    assert not model.engine.active_events


@pytest.mark.parametrize("kind", ["sequence", "drops", "epoch"])
@pytest.mark.parametrize("stale", [False, True])
def test_real_gap_drop_epoch_remain_global_even_if_bar_is_stale(kind, stale, caplog):
    caplog.set_level(logging.INFO, logger=runtime.__name__)
    model = consumer()
    observe(model, bar())
    observe(model, bar(1, high="12"))
    assert len(model.engine.active_events) == 2
    item = bar(2)
    if kind == "sequence":
        item = replace(item, sequence=4)
    elif kind == "drops":
        item = replace(item, dropped_total=1)
    else:
        item = replace(item, gateway_instance_id="new-epoch")
    _, records = observe(model, item, 1_000_000_001 if stale else 0)
    assert len(records) == 2 and {r.event_type for r in records} == {"FEED_GAP"}
    assert all(event.feed_gap for event in model.engine._active.values())
    assert "FEEDGAP2-AGGREGATE-GAP" in caplog.text


@pytest.mark.parametrize("stale", [False, True])
def test_drop_regression_refused_and_duplicate_does_not_recount(stale):
    model = consumer()
    first = replace(bar(), dropped_total=2)
    observe(model, first)
    duplicate = replace(first, high=first.high * 10)
    outcome, records = observe(model, duplicate, 1_000_000_001 if stale else 0)
    assert outcome.reason == "duplicate_or_reordered_aggregate" and not records
    assert not model.transport_stale_by_symbol
    with pytest.raises(ValueError, match="warning_drop_counter_regressed"):
        observe(model, replace(bar(1), dropped_total=1), 1_000_000_001 if stale else 0)
    assert model._sequence == 1 and model._drops == 2


def test_future_clock_refusal_preserves_sequence_and_has_no_stale_counter():
    model = consumer()
    observe(model, bar())
    outcome, records = observe(model, bar(1, high="100"), -1)
    assert outcome.reason == "aggregate_clock_or_stale" and not records
    assert model._sequence == 1 and model.coverage is None
    assert not model.transport_stale_by_symbol
    assert not model.engine.active_events


def test_stale_duplicate_is_ignored_without_incrementing_stale_counter():
    model = consumer()
    item = bar()
    observe(model, item, 1_000_000_001)
    assert model.transport_stale_by_symbol == {"TEST": 1}
    outcome, records = observe(model, item, 1_000_000_001)
    assert outcome.reason == "duplicate_or_reordered_aggregate" and not records
    assert model.transport_stale_by_symbol == {"TEST": 1}
    assert model._sequence == item.sequence


def test_stale_aggregate_cannot_move_existing_filled_paths(monkeypatch):
    model = consumer()
    observe(model, bar())
    observe(model, bar(1, high="12"))
    item = trade(2001, "12.1")
    records = model.observe_trade(item, sip_timestamp_ns=item.sip_ts_ms * 1_000_000,
                                   observed_ns=item.sip_ts_ms * 1_000_000)
    assert len([r for r in records if r.event_type == "FILLED"]) == 2
    before = deepcopy(model.engine._active)
    def forbidden(*args, **kwargs):
        pytest.fail("stale aggregate reached detection/fill/exit ingestion")
    monkeypatch.setattr(model.engine, "ingest", forbidden)
    monkeypatch.setattr(model, "_bucket_signals", forbidden)
    outcome, records = observe(model, bar(2, low="1", high="100"), 1_000_000_001)
    assert outcome.status == "REFUSED" and not records
    assert model.engine._active == before


@pytest.mark.parametrize("age_ns,stale", [(1_000_000_000, False), (1_000_000_001, True)])
def test_receive_age_exact_one_second_boundary(age_ns, stale):
    model = consumer()
    outcome, _ = observe(model, bar(), age_ns)
    assert (outcome.reason == "aggregate_clock_or_stale") == stale
    assert model._sequence == 1
    assert sum(model.transport_stale_by_symbol.values()) == int(stale)


def test_transport_counts_top5_reset_and_state_bound():
    model = runtime.MomentumWarningConsumer(prior_closes={}, condition_version="fixture",
        coverage_started_ms=START, max_window_symbols=16)
    for i in range(17):
        observe(model, bar(i, symbol=f"S{i:02}", sequence=i+1), 1_000_000_001)
    assert len(model.transport_stale_by_symbol) == 16
    metrics = model.take_transport_stale_metrics()
    assert metrics["transport_stale_total"] == 17
    assert metrics["transport_stale_untracked"] == 1
    assert not metrics["transport_stale_top5_complete"]
    assert metrics["transport_stale_by_symbol"] == {f"S{i:02}": 1 for i in range(5)}
    assert model.take_transport_stale_metrics()["transport_stale_total"] == 0
    assert not model.transport_stale_by_symbol


def test_old_order_mutation_fails_stale_fresh_control(monkeypatch, caplog):
    source = textwrap.dedent(inspect.getsource(runtime.MomentumWarningConsumer.observe_aggregate))
    mutated = source.replace("if future_clock:", "if future_clock or receive_stale:", 1)
    assert mutated != source
    namespace = dict(runtime.__dict__)
    exec(compile(mutated, "<paperlag1-old-order>", "exec"), namespace)
    monkeypatch.setattr(runtime.MomentumWarningConsumer, "observe_aggregate", namespace["observe_aggregate"])
    with pytest.raises(AssertionError):
        test_stale_then_fresh_advances_without_global_gap_or_stale_decision(caplog)


@pytest.mark.asyncio
async def test_stale_metrics_once_per_symbol_per_sample_not_other_decisions(monkeypatch):
    service = MomentumPaperService(Settings(), redis_client=AsyncMock())
    service._warning = consumer()
    service._engine = consumer().engine
    service._persist = AsyncMock()
    service._warning_metrics = AsyncMock()
    service._sync_gateway_subscriptions = AsyncMock()

    async def handle(item, age_ns):
        monkeypatch.setattr("project_mai_tai.services.momentum_paper_app.wall_time.time_ns",
                            lambda: item.published_utc_ns + age_ns)
        await service._handle_warning_event(json.dumps(
            {**item.__dict__, "version": 1, "event_type": "aggregate"}, default=str))

    await handle(bar(), 1_000_000_001)
    await handle(bar(1), 1_000_000_001)
    await handle(bar(2, symbol="OTHER"), 1_000_000_001)
    assert service._warning_metrics.await_count == 2
    metrics = service._warning.take_transport_stale_metrics()
    assert metrics["transport_stale_total"] == 3
    assert metrics["transport_stale_by_symbol"] == {"TEST": 2, "OTHER": 1}
    await handle(bar(3), 1_000_000_001)
    assert service._warning_metrics.await_count == 3
    # Future-clock refusals are not stale coalescing candidates or counter entries.
    await handle(bar(4), -1)
    await handle(bar(4), -1)
    assert service._warning_metrics.await_count == 5
    assert service._warning._sequence == 4
    assert service._warning._transport_stale_total == 1
    await handle(bar(4), 0)
    await handle(bar(5, high="12"), 0)
    assert service._warning_metrics.await_count == 6
    stale_source = bar(6)
    stale_source = replace(stale_source,
        received_utc_ns=stale_source.received_utc_ns + 8_000_000_000,
        published_utc_ns=stale_source.published_utc_ns + 8_000_000_000)
    await handle(stale_source, 0)
    assert service._warning_metrics.await_count == 7
    assert service._warning_metrics.await_args.kwargs["reason"] == "aggregate_source_end_stale"
    assert len(service._warning.engine.active_events) == 2


@pytest.mark.asyncio
async def test_untracked_symbols_do_not_make_coalescing_state_unbounded(monkeypatch):
    service = MomentumPaperService(Settings(), redis_client=AsyncMock())
    service._warning = runtime.MomentumWarningConsumer(prior_closes={}, condition_version="fixture",
        coverage_started_ms=START, max_window_symbols=16)
    service._engine = consumer().engine
    service._persist = AsyncMock()
    service._warning_metrics = AsyncMock()
    for i in range(20):
        item = bar(i, symbol=f"S{i}", sequence=i+1)
        monkeypatch.setattr("project_mai_tai.services.momentum_paper_app.wall_time.time_ns",
                            lambda: item.published_utc_ns + 1_000_000_001)
        await service._handle_warning_event(json.dumps(
            {**item.__dict__, "version": 1, "event_type": "aggregate"}, default=str))
    assert service._warning_metrics.await_count == 16
    assert len(service._warning.transport_stale_by_symbol) == 16
    assert service._warning.take_transport_stale_metrics()["transport_stale_untracked"] == 4
