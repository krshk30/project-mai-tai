"""MOMAGE1 sample counters: exact ages/partition, bounded storage, no new bar IO."""

from dataclasses import replace
from datetime import UTC, datetime
import inspect
import json
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.warning_source_age import AggregateSourceAgeStats
from project_mai_tai.momentum_paper.warning_window import WarningWindow
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_momage1_source_age import delayed_bar, fresh_consumer


@pytest.mark.parametrize("age_ms,status", [(7601, "ADMITTED"), (7602, "REFUSED")])
def test_f_exact_bound_admitted_and_one_ms_above_refused(age_ms, status):
    item, observed = delayed_bar(age_ms=age_ms)
    model = fresh_consumer()
    outcome, records = model.observe_aggregate(item, observed_ns=observed, legacy_active=set())
    assert outcome.status == status
    assert bool(model.engine.seal_warning_decisions(records, observed_decision_ns=observed)) == (status == "ADMITTED")
    sample = model.source_age_stats.take_sample(observed_ns=observed)
    assert sample["source_age_admitted_bars"] == (status == "ADMITTED")
    assert sample["source_age_refused_bars"] == (status == "REFUSED")
    assert sample["source_end_age_max_ms"] == sample["source_end_age_p99_ms"] == age_ms


def test_single_named_source_predicate_at_warning_detection_and_seal():
    assert "aggregate_source_age_reason(" in inspect.getsource(WarningWindow.evaluate)
    assert "aggregate_source_age_reason(" in inspect.getsource(MomentumPaperEngine.detect_from_bucket)
    assert "aggregate_source_age_reason(" in inspect.getsource(MomentumPaperEngine.seal_warning_decisions)
    for function in (WarningWindow.evaluate, MomentumPaperEngine.detect_from_bucket,
                     MomentumPaperEngine.seal_warning_decisions):
        assert "7601" not in inspect.getsource(function)


def test_counter_partition_duplicates_are_age_pass_not_paper_entries():
    item, observed = delayed_bar(high="10", close="10")
    model = fresh_consumer()
    first, _ = model.observe_aggregate(item, observed_ns=observed, legacy_active=set())
    duplicate, _ = model.observe_aggregate(item, observed_ns=observed, legacy_active=set())
    assert first.reason == "no_warning_candidate" and duplicate.reason == "duplicate_or_reordered_aggregate"
    stale, at = delayed_bar(age_ms=7602)
    model.observe_aggregate(stale, observed_ns=at, legacy_active=set())
    # PAPERLAG1 checks duplicates first; use an in-order row for the age partition.
    bad_transport = replace(stale, sequence=model._sequence + 1,
                            received_utc_ns=at - 1_000_000_001)
    refused, _ = model.observe_aggregate(bad_transport, observed_ns=at, legacy_active=set())
    assert refused.reason == "aggregate_clock_or_stale"
    sample = model.source_age_stats.take_sample(observed_ns=at)
    assert sample["source_age_parsed_bars"] == 4
    assert sample["source_age_admitted_bars"] == 2
    assert sample["source_age_refused_bars"] == 1
    assert sample["source_age_transport_refused_bars"] == 1
    assert sample["source_age_admission_semantics"] == "AGE_GATE_PASS_NOT_PAPER_ENTRY"
    assert not model.engine.active_events


def test_exact_nearest_rank_ms_max_and_bounded_overflow_unknown():
    stats = AggregateSourceAgeStats()
    end = 1791964800000
    for value in range(100):
        stats.observe(end_ms=end, observed_ns=(end + value) * 1_000_000, refusal_reason=None)
    sample = stats.take_sample(observed_ns=(end + 100) * 1_000_000)
    assert sample["source_end_age_p99_ms"] == 98 and sample["source_end_age_max_ms"] == 99
    assert sample["source_end_age_p99_status"] == "EXACT_INTEGER_MS_NEAREST_RANK"
    assert len(stats.histogram) == stats.HISTOGRAM_MAX_MS + 1
    stats.observe(end_ms=end, observed_ns=(end + 100_000) * 1_000_000,
                  refusal_reason="aggregate_source_end_stale")
    sample = stats.take_sample(observed_ns=(end + 100_001) * 1_000_000)
    assert sample["source_end_age_max_ms"] == 100_000
    assert sample["source_end_age_p99_ms"] is None
    assert sample["source_end_age_p99_status"] == "UNKNOWN_ABOVE_HISTOGRAM_BOUND"
    assert sample["source_end_age_overflow_samples"] == 1


def test_empty_and_future_samples_never_claim_zero_healthy_age():
    stats = AggregateSourceAgeStats()
    sample = stats.take_sample(observed_ns=1)
    assert sample["source_end_age_max_ms"] is sample["source_end_age_p99_ms"] is None
    assert sample["source_age_observability"] == "EMPTY_NO_HEALTH_CLAIM"
    stats.observe(end_ms=100, observed_ns=99_000_000, refusal_reason="aggregate_bucket_incomplete")
    sample = stats.take_sample(observed_ns=100_000_000)
    assert sample["source_age_incomplete_bars"] == 1
    assert sample["source_end_age_nonnegative_samples"] == 0
    assert sample["source_end_age_p99_ms"] is None


@pytest.mark.asyncio
async def test_reset_export_only_existing_ten_second_consumer_sample(monkeypatch):
    item, now_ns = delayed_bar(high="10", close="10")
    redis = AsyncMock()
    service = MomentumPaperService(Settings(_env_file=None, market_data_subscription_startup_enabled=False),
                                   redis_client=redis)
    service._warning = fresh_consumer()
    service._engine = fresh_consumer().engine
    service._persist = AsyncMock()
    service._clock = lambda: datetime.fromtimestamp(now_ns / 1e9, tz=UTC)
    service._session_date = service._clock().date()
    service._last_heartbeat = service._clock()
    service._last_warning_sample_ns = now_ns
    service._gateway_task = object()  # No socket, provider or reader started.
    monkeypatch.setattr("project_mai_tai.services.momentum_paper_app.wall_time.time_ns", lambda: now_ns)
    try:
        await service._handle_warning_event(json.dumps(
            {**item.__dict__, "version": 1, "event_type": "aggregate"}, default=str))
        redis.xadd.assert_not_awaited()  # Ignored/no-warning bar adds no metrics IO.
        start_ns = now_ns
        now_ns += 9_000_000_000
        await service._tick()
        redis.xadd.assert_not_awaited()
        now_ns += 1_000_000_000
        await service._tick()
        assert redis.xadd.await_count == 1
        sample = json.loads(redis.xadd.await_args.args[1]["data"])
        assert sample["event_type"] == "consumer_sample"
        assert sample["source_age_window_start_ns"] == start_ns
        assert sample["source_age_window_end_ns"] == now_ns
        assert sample["source_age_parsed_bars"] == sample["source_age_admitted_bars"] == 1
        assert sample["source_end_age_p99_ms"] == sample["source_end_age_max_ms"] == 2000
        await service._tick()
        assert redis.xadd.await_count == 1
        now_ns += 10_000_000_000
        service._last_heartbeat = service._clock()
        await service._tick()
        sample = json.loads(redis.xadd.await_args.args[1]["data"])
        assert sample["event_type"] == "consumer_sample"
        assert sample["source_age_parsed_bars"] == 0
        assert sample["source_age_window_start_ns"] == now_ns - 10_000_000_000
        assert sample["source_age_window_end_ns"] == now_ns
        assert sample["source_end_age_max_ms"] is sample["source_end_age_p99_ms"] is None
        assert sample["source_age_observability"] == "EMPTY_NO_HEALTH_CLAIM"
    finally:
        service._gateway_task = None
