from dataclasses import replace
import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal as D
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import Base
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import (
    MOMENTUM_STRATEGIES,
    MOMENTUM_WARNING_STRATEGIES,
    MomentumTapeRecord,
    TradePrint,
)
from project_mai_tai.momentum_paper.store import MomentumPaperStore
from project_mai_tai.momentum_paper.store import session_record
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.momentum_paper.warning_window import (
    AggregateObservation,
    CoverageEvidence,
    ObservedRawPaperPath,
    WarningCandidates,
    WarningEvaluation,
    WarningWindow,
    WindowRange,
)
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_momentum_paper_service import _condition_snapshot, _store


START = 1791793200000  # 2026-10-12 04:20 ET, controlled observation clocks.
EPOCH = ("gateway-fixture", 1)


def bar(offset=0, *, low="10", high="10", symbol="TEST", sequence=None):
    start = START + offset * 1000
    return AggregateObservation(
        symbol, start, start + 1000, D(low), D(high), D(low), D(high), 100,
        (start + 1000) * 1_000_000, (start + 1000) * 1_000_000,
        EPOCH[0], EPOCH[1], offset + 1 if sequence is None else sequence, 0,
    )


def evaluation(now, *, symbol="TEST", warning=False, known=True, high="10.9"):
    return WarningEvaluation(symbol, START + now, tuple(
        WindowRange(seconds, D("10"), D(high), known, False) for seconds in (30, 60)
    ), (30, 60) if warning else ())


def trade(offset, price="10", *, eligible=True):
    return TradePrint("TEST", START + offset, D(price), 100, eligible=eligible)


def consumer():
    return MomentumWarningConsumer(prior_closes={"TEST": D("10")},
                                   condition_version="fixture", coverage_started_ms=START - 60_000)


def test_v1_parser_preserves_real_ns_and_never_creates_low_print():
    original = bar()
    payload = {**original.__dict__, "version": 1, "event_type": "aggregate"}
    payload.update({key: str(payload[key]) for key in ("open", "high", "low", "close")})
    assert AggregateObservation.from_payload(json.loads(json.dumps(payload))) == original
    assert not hasattr(original, "sip_ts_ms")
    assert not hasattr(original, "low_at")
    for key, value in (("version", 2), ("start_ms", "1"), ("sequence", True)):
        with pytest.raises(ValueError):
            AggregateObservation.from_payload({**payload, key: value})


@pytest.mark.parametrize("high,warning", [("10.9999", False), ("11", True)])
def test_exact_decimal_warning_threshold(high, warning):
    window = WarningWindow("TEST")
    current = bar(high=high)
    window.observe(current, observed_ns=current.published_utc_ns)
    assert bool(window.evaluate(observed_ms=START + 1000).warning_windows) is warning


def test_partial_boundary_low_is_not_whole_bucket_evidence():
    window = WarningWindow("TEST")
    for offset, low, high in ((0, "5", "10"), (1, "5", "10"), (30, "10", "11")):
        item = bar(offset, low=low, high=high)
        window.observe(item, observed_ns=item.published_utc_ns)
    proof = CoverageEvidence(EPOCH, START, START + 31_500, 0, True)
    result = window.evaluate(observed_ms=START + 31_500, coverage=proof)
    short = result.windows[0]
    assert short.low == D("10")
    assert short.boundary_ambiguous and not short.fully_known
    assert not result.fully_known_cool


def test_window_bound_and_missing_coverage_is_unknown_not_quiet():
    window = WarningWindow("TEST")
    for offset in range(1000):
        item = bar(offset)
        window.observe(item, observed_ns=item.published_utc_ns)
    assert window.retained_buckets <= 60
    assert not window.evaluate(observed_ms=START + 1_000_000).fully_known_cool


def test_drop_epoch_reset_clear_window_and_duplicate_does_not_warn():
    window = WarningWindow("TEST")
    first = bar(low="5", high="5")
    assert window.observe(first, observed_ns=first.published_utc_ns)
    assert not window.observe(first, observed_ns=first.published_utc_ns)
    current = replace(bar(1), dropped_total=1)
    window.observe(current, observed_ns=current.published_utc_ns)
    assert not window.evaluate(observed_ms=START + 2000).warning_windows
    assert window.retained_buckets == 1
    with pytest.raises(ValueError, match="regressed"):
        window.observe(bar(2), observed_ns=bar(2).published_utc_ns)


def test_candidate_cap_counts_paths_and_reuses_owned_symbol():
    candidates = WarningCandidates()
    active = {f"X{index}" for index in range(16)}
    assert candidates.observe(evaluation(0, warning=True), active_path_symbols=active).reason == "warning_cap_full"
    reused = candidates.observe(evaluation(0, symbol="X0", warning=True), active_path_symbols=active)
    assert reused.status == "ADMITTED"
    assert candidates.desired_symbols(active) == active


def test_lower_gateway_owner_cap_preserves_legacy_capacity():
    candidates = WarningCandidates(owner_cap=1)
    assert candidates.observe(evaluation(0, warning=True), active_path_symbols={"OLD"}).status == "ADMITTED"
    assert candidates.observe(evaluation(1, symbol="NEW", warning=True), active_path_symbols={"OLD"}).reason == "warning_owner_cap_full"
    assert candidates.desired_symbols({"OLD"}) == {"TEST", "OLD"}


def test_end_of_evaluation_clock_fences_predecision_fill_and_late_proposal():
    model = consumer()
    model.observe_aggregate(bar(), observed_ns=bar().published_utc_ns, legacy_active=set())
    current = bar(1, high="12")
    _, records = model.observe_aggregate(current, observed_ns=current.published_utc_ns, legacy_active=set())
    sealed = model.engine.seal_warning_decisions(records, observed_decision_ns=(START+2005)*1_000_000)
    assert len(sealed) == 2
    queued = model.engine.ingest(trade(2001, "12.1"), allow_detection=False)
    assert not any(row.event_type == "FILLED" for row in queued)
    model2 = consumer()
    model2.observe_aggregate(bar(), observed_ns=bar().published_utc_ns, legacy_active=set())
    _, late = model2.observe_aggregate(current, observed_ns=current.published_utc_ns, legacy_active=set())
    assert not model2.engine.seal_warning_decisions(late, observed_decision_ns=(START+3001)*1_000_000)
    assert not model2.engine.active_events


def test_sliding_retention_and_two_known_cool_windows_are_both_required():
    candidates = WarningCandidates()
    candidates.observe(evaluation(0, warning=True), active_path_symbols=set())
    candidates.observe(evaluation(59_000, warning=True), active_path_symbols=set())
    for now in range(60_000, 120_000, 1000):
        assert candidates.observe(evaluation(now), active_path_symbols=set()).status == "RETAINED"
    assert candidates.observe(evaluation(120_000), active_path_symbols=set()).status == "DROPPED"
    assert candidates.observe(evaluation(120_001, warning=True), active_path_symbols=set()).reason == "drop_cooldown"
    assert candidates.observe(evaluation(180_000, warning=True), active_path_symbols=set()).status == "ADMITTED"


@pytest.mark.parametrize("break_type", ["unknown", "sample_gap", "one_window_hot"])
def test_unproved_cooling_never_expires_candidate(break_type):
    candidates = WarningCandidates()
    candidates.observe(evaluation(0, warning=True), active_path_symbols=set())
    for now in range(1000, 62_000, 1000):
        current = evaluation(now)
        if now == 31_000:
            if break_type == "sample_gap":
                continue
            if break_type == "unknown":
                current = evaluation(now, known=False)
            else:
                current = replace(current, windows=(current.windows[0], WindowRange(60, D("10"), D("11"), True, False)))
        assert candidates.observe(current, active_path_symbols=set()).status == "RETAINED"


def test_active_path_never_evicted_even_with_full_quiet_proof():
    candidates = WarningCandidates()
    candidates.observe(evaluation(0, warning=True), active_path_symbols=set())
    for now in range(1000, 100_000, 1000):
        assert candidates.observe(evaluation(now), active_path_symbols={"TEST"}).reason == "active_paper_path"
    assert "TEST" in candidates.desired_symbols({"TEST"})


def test_bucket_signal_is_separate_and_causal_next_print_target_is_native():
    model = consumer()
    original_default = dict(MOMENTUM_STRATEGIES)
    _, first = model.observe_aggregate(bar(), observed_ns=bar().published_utc_ns, legacy_active=set())
    assert not first
    current = bar(1, high="12")
    outcome, detected = model.observe_aggregate(current, observed_ns=current.published_utc_ns, legacy_active=set())
    assert outcome.status == "ADMITTED"
    assert {record.strategy_code for record in detected} == set(MOMENTUM_WARNING_STRATEGIES)
    payload = detected[0].payload
    assert payload["reference"]["exact_low_sip_timestamp"] is None
    assert "sip_ts_ms" not in payload["detect"]
    assert not model.engine._eligible_history
    same = model.observe_trade(trade(2000, "12"), sip_timestamp_ns=(START + 2000)*1_000_000,
                               observed_ns=(START + 2000)*1_000_000)
    assert not any(r.event_type == "FILLED" for r in same)
    fill = model.observe_trade(trade(2001, "12.1"), sip_timestamp_ns=(START + 2001)*1_000_000,
                               observed_ns=(START + 2001)*1_000_000)
    assert sum(r.event_type == "FILLED" for r in fill) == 2
    hit = model.observe_trade(trade(3000, "12.8"), sip_timestamp_ns=(START + 3000)*1_000_000,
                              observed_ns=(START + 3000)*1_000_000)
    assert not any(r.event_type == "EXITED" for r in hit)
    exits = model.engine.advance_clock(START + 4000)
    assert all(r.payload["exit_reason"] == "TARGET" for r in exits)
    assert all(r.payload["exit"]["sip_ts_ms"] == START + 3000 for r in exits)
    assert dict(MOMENTUM_STRATEGIES) == original_default and len(original_default) == 2


@pytest.mark.parametrize("price,reason", [("10", "STOP"), ("12.1", "TIME")])
def test_warning_stop_and_600_second_time_exit(price, reason):
    model = consumer()
    model.observe_aggregate(bar(), observed_ns=bar().published_utc_ns, legacy_active=set())
    current = bar(1, high="12")
    model.observe_aggregate(current, observed_ns=current.published_utc_ns, legacy_active=set())
    model.observe_trade(trade(2001, "12.1"), sip_timestamp_ns=(START+2001)*1_000_000,
                        observed_ns=(START+2001)*1_000_000)
    at = 3000 if reason == "STOP" else 602_002
    records = model.observe_trade(trade(at, price), sip_timestamp_ns=(START+at)*1_000_000,
                                  observed_ns=(START+at)*1_000_000)
    assert sum(r.event_type == "EXITED" and r.payload["exit_reason"] == reason for r in records) == 2


def test_raw_only_requires_explicit_complete_coverage_and_never_cold_seeds():
    path = ObservedRawPaperPath(symbol="TEST", prior_close=D("10"), condition_version="fixture")
    first = path.observe(trade(0), sip_timestamp_ns=START*1_000_000, observed_ns=START*1_000_000,
                         coverage=CoverageEvidence(EPOCH, START, START, 0, True))
    assert first.reason == "raw_coverage_incomplete"
    for offset in (30_000, 59_999):
        result = path.observe(trade(offset), sip_timestamp_ns=(START+offset)*1_000_000,
                              observed_ns=(START+offset)*1_000_000,
                              coverage=CoverageEvidence(EPOCH, START, START+offset, 0, True))
        assert result.status == "OBSERVED"
    signal = path.observe(trade(60_000, "12"), sip_timestamp_ns=(START+60_000)*1_000_000,
                          observed_ns=(START+60_000)*1_000_000,
                          coverage=CoverageEvidence(EPOCH, START, START+60_000, 0, True))
    assert signal.status == "DETECTED"
    assert {r.strategy_code for r in signal.records} == set(MOMENTUM_WARNING_STRATEGIES)


def test_raw_signal_never_fills_queued_print_before_observed_decision():
    path = ObservedRawPaperPath(symbol="TEST", prior_close=D("10"), condition_version="fixture")
    for offset, price, delay in ((0, "10", 0), (60_000, "12", 100_000_000)):
        path.observe(trade(offset, price), sip_timestamp_ns=(START+offset)*1_000_000,
                     observed_ns=(START+offset)*1_000_000+delay,
                     coverage=CoverageEvidence(EPOCH, START, START+offset, 0, True))
    queued = path.observe(trade(60_001, "12.1"), sip_timestamp_ns=(START+60_001)*1_000_000,
                          observed_ns=(START+60_101)*1_000_000,
                          coverage=CoverageEvidence(EPOCH, START, START+60_001, 0, True))
    assert queued.reason == "raw_predecision_print"
    assert not any(r.event_type == "FILLED" for r in queued.records)


def test_store_and_seed_isolate_warning_terminal_and_incomplete_records():
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    store = MomentumPaperStore(sessionmaker(database))
    records = []
    for code in (*MOMENTUM_STRATEGIES, *MOMENTUM_WARNING_STRATEGIES):
        records.append(MomentumTapeRecord(
            f"{code}:FINAL", f"{code}:one", code, "FINAL", date(2026, 10, 12), "TEST",
            datetime(2026, 10, 12, 8, tzinfo=UTC),
            {"strategy_code": code, "exit": {"sip_ts_ms": START + 123}},
        ))
        records.append(replace(records[-1], event_key=f"{code}:DETECTED", logical_id=f"{code}:two",
                               event_type="DETECTED"))
    store.append_many(records)
    legacy_rows = store.load_session(date(2026, 10, 12))
    warning_rows = store.load_session(date(2026, 10, 12), strategy_codes=MOMENTUM_WARNING_STRATEGIES)
    assert {r.strategy_code for r in legacy_rows} == set(MOMENTUM_STRATEGIES)
    assert {r.strategy_code for r in warning_rows} == set(MOMENTUM_WARNING_STRATEGIES)
    assert len(store.load_completed_since(date(2026, 10, 12))) == 2
    legacy = MomentumPaperEngine(prior_closes={"TEST": D("10")}, condition_version="fixture",
                                 coverage_started_ms=START)
    legacy.seed_reentry_boundaries(records)
    warning = consumer().engine
    warning.seed_reentry_boundaries(records)
    assert {key[0] for key in legacy._reference_after_ms} == set(MOMENTUM_STRATEGIES)
    assert {key[0] for key in warning._reference_after_ms} == set(MOMENTUM_WARNING_STRATEGIES)
    assert store.incomplete_logical_ids(legacy_rows) == {f"{code}:two" for code in MOMENTUM_STRATEGIES}


def test_filtered_symbols_do_not_create_false_global_stream_gaps():
    model = consumer()
    for item in (bar(), bar(1, symbol="FILTERED", sequence=2), bar(2, high="11", sequence=3)):
        model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    assert model.windows["TEST"].retained_buckets == 2
    assert "TEST" in model.desired_symbols(set())


def test_raw_reference_cannot_use_bucket_after_signal_sip_time():
    model = consumer()
    model.candidates.observe(evaluation(0, warning=True), active_path_symbols=set())
    original = bar(low="5", high="5")
    model.observe_aggregate(original, observed_ns=original.published_utc_ns, legacy_active=set())
    # This delayed signal preceded the completed low bucket: no fabricated reference.
    records = model.observe_trade(trade(500, "6"), sip_timestamp_ns=(START+500)*1_000_000,
                                  observed_ns=(START+1000)*1_000_000)
    assert not any(record.event_type == "DETECTED" for record in records)


@pytest.mark.asyncio
async def test_flagged_service_restores_separate_warning_boundaries_only():
    store = _store()
    now = datetime.fromtimestamp(START/1000, tz=UTC)
    store.append_many([session_record(
        session_date=now.date(), observed_at=now, prior_close_date=date(2026, 10, 9),
        prior_closes={"TEST": "10"}, condition_snapshot=_condition_snapshot().payload(),
    )])
    for code in (*MOMENTUM_STRATEGIES, *MOMENTUM_WARNING_STRATEGIES):
        store.append_many([MomentumTapeRecord(
            f"{code}:FINAL", f"{code}:one", code, "FINAL", now.date(), "TEST", now,
            {"strategy_code": code, "exit": {"sip_ts_ms": START+123}},
        )])
    service = MomentumPaperService(
        Settings().model_copy(update={"market_data_momentum_warning_enabled": True}),
        store=store, redis_client=AsyncMock(), clock=lambda: now,
    )
    await service._prepare_session(now.date())
    assert service._warning is not None
    assert {key[0] for key in service._engine._reference_after_ms} == set(MOMENTUM_STRATEGIES)
    assert {key[0] for key in service._warning.engine._reference_after_ms} == set(MOMENTUM_WARNING_STRATEGIES)
    assert {row["strategy_code"] for row in service._completed_from_store} == set(MOMENTUM_STRATEGIES)


@pytest.mark.asyncio
async def test_warning_service_persists_namespaced_events_and_existing_owner_request(monkeypatch):
    redis = AsyncMock()
    store = _store()
    service = MomentumPaperService(Settings(), store=store, redis_client=redis)
    service._engine = MomentumPaperEngine(prior_closes={"TEST": D("10")},
                                          condition_version="fixture", coverage_started_ms=START)
    service._warning = consumer()
    for item in (bar(), bar(1, high="12")):
        monkeypatch.setattr("project_mai_tai.services.momentum_paper_app.wall_time.time_ns",
                            lambda: item.published_utc_ns)
        payload = {**item.__dict__, "version": 1, "event_type": "aggregate"}
        await service._handle_warning_event(json.dumps(payload, default=str))
    assert service._subscribed_symbols == set()
    assert service._warning_subscribed_symbols == {"TEST"}
    assert store.load_session(date(2026, 10, 12)) == []
    warning = store.load_session(date(2026, 10, 12), strategy_codes=MOMENTUM_WARNING_STRATEGIES)
    assert {row.strategy_code for row in warning} == set(MOMENTUM_WARNING_STRATEGIES)
    calls = [call.args for call in redis.xadd.await_args_list]
    assert any("market-data-subscriptions" in call[0] for call in calls)
    assert any("momentum-warning-metrics" in call[0] for call in calls)
    owner_requests = [json.loads(call[1]["data"])["payload"] for call in calls
                      if "market-data-subscriptions" in call[0]]
    assert any(row["consumer_name"] == "momentum-warning" and row["symbols"] == ["TEST"]
               for row in owner_requests)
    assert not any(row["consumer_name"] == "momentum-paper" and row["symbols"]
                   for row in owner_requests)


def test_pre411_warning_prefeed_does_not_open_paper_events():
    model = consumer()
    before = START - 15*60_000
    for index, high in ((0, "10"), (1, "12")):
        item = replace(bar(index, high=high), start_ms=before+index*1000,
                       end_ms=before+(index+1)*1000,
                       received_utc_ns=(before+(index+1)*1000)*1_000_000,
                       published_utc_ns=(before+(index+1)*1000)*1_000_000)
        outcome, records = model.observe_aggregate(item, observed_ns=item.published_utc_ns,
                                                    legacy_active=set())
        assert not records
    assert outcome.status == "ADMITTED"


@pytest.mark.asyncio
async def test_warning_stream_failure_does_not_invalidate_legacy_snapshot_feed(monkeypatch):
    redis = AsyncMock()
    redis.xread.side_effect = [RuntimeError("controlled warning-only read failure"), asyncio.CancelledError()]
    service = MomentumPaperService(Settings(), store=_store(), redis_client=redis)
    service._warning = consumer()
    service._connected = True
    monkeypatch.setattr("project_mai_tai.services.momentum_paper_app.asyncio.sleep", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await service._warning_loop()
    assert service._connected
    assert service._feed_gap_started_ms is None
    assert service._warning.coverage is None
