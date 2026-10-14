"""Controlled call-site distinctions, not proof of the live flood's origin."""

from dataclasses import replace
from decimal import Decimal
import inspect
import textwrap

import pytest

from project_mai_tai.momentum_paper.warning_window import CoverageEvidence
from project_mai_tai.momentum_paper import engine as engine_runtime
from project_mai_tai.momentum_paper import warning_consumer as consumer_runtime
from project_mai_tai.momentum_paper.models import TradePrint
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from tests.unit.test_momentum_warning import START, EPOCH, bar, consumer, trade


def active_consumer():
    model = consumer()
    for item in (bar(), bar(1, high="12")):
        model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    assert len(model.engine.active_events) == 2
    assert model.windows["TEST"].retained_buckets == 2
    return model


def window_state(model):
    return (dict(model.windows), dict(model._latest), dict(model.last_warning_ns),
            model._stream_epoch, model._sequence, model._drops, model.coverage)


def test_stale_duplicate_precedes_duplicate_filter_emits_once_and_preserves_windows():
    model = active_consumer()
    item = trade(2001, "12.1")
    sip_ns = item.sip_ts_ms * 1_000_000
    model.observe_trade(item, sip_timestamp_ns=sip_ns, observed_ns=sip_ns)
    before = window_state(model)
    assert not any(event.feed_gap for event in model.engine._active.values())
    assert model.observe_trade(item, sip_timestamp_ns=sip_ns, observed_ns=sip_ns + 1_000_000_000) == ()
    assert model.last_status == "raw_duplicate_or_out_of_order"
    records = model.observe_trade(item, sip_timestamp_ns=sip_ns, observed_ns=sip_ns + 1_000_000_001)
    assert model.last_status == "raw_future_or_stale"
    assert records == ()
    assert model._last_raw_ns == {"TEST": sip_ns}
    for _ in range(5):
        assert model.observe_trade(item, sip_timestamp_ns=sip_ns, observed_ns=sip_ns + 1_000_000_001) == ()
    assert window_state(model) == before
    assert not any(event.feed_gap for event in model.engine._active.values())
    assert model.late_raw_ignored_by_symbol == {"TEST": 6}


@pytest.mark.parametrize("age_ns,refused", [(1_000_000_000, False), (1_000_000_001, True)])
def test_raw_freshness_exact_one_second_and_one_ns_over(age_ns, refused):
    model = active_consumer()
    item = trade(2001, "12.1")
    sip_ns = item.sip_ts_ms * 1_000_000
    before = window_state(model)
    records = model.observe_trade(item, sip_timestamp_ns=sip_ns, observed_ns=sip_ns + age_ns)
    assert (model.last_status == "raw_future_or_stale") is refused
    assert not any(record.event_type == "FEED_GAP" for record in records)
    assert ("TEST" in model._last_raw_ns) is not refused
    assert not any(event.feed_gap for event in model.engine._active.values())
    assert model.late_raw_ignored_by_symbol == ({"TEST": 1} if refused else {})
    assert window_state(model) == before


@pytest.mark.parametrize("future_ns", [1, 1_000_000])
def test_future_raw_print_is_refused_without_clearing_windows(future_ns):
    model = active_consumer()
    item = trade(2001, "12.1")
    sip_ns = item.sip_ts_ms * 1_000_000
    before = window_state(model)
    records = model.observe_trade(item, sip_timestamp_ns=sip_ns, observed_ns=sip_ns - future_ns)
    assert model.last_status == "raw_future_or_stale"
    assert model._last_raw_ns == {}
    assert len(records) == 2 and all(record.event_type == "FEED_GAP" for record in records)
    assert all(record.payload["started_ms"] == item.sip_ts_ms for record in records)
    assert all(record.payload["ended_ms"] == (sip_ns - future_ns) // 1_000_000 for record in records)
    assert window_state(model) == before


@pytest.mark.parametrize("kind", ["sequence", "drops", "epoch"])
def test_real_aggregate_discontinuity_clears_windows_and_records_fixed_start_gap(kind):
    model = active_consumer()
    previous = model.windows["TEST"]
    proof = CoverageEvidence(EPOCH, START - 60_000, START + 2000, 0, True)
    model.set_coverage(proof)
    model._raw_first_ms["TEST"] = START
    model._raw_coverage["TEST"] = proof
    item = bar(2)
    changes = {"sequence": {"sequence": 4}, "drops": {"dropped_total": 1},
               "epoch": {"gateway_instance_id": "replacement-gateway"}}
    item = replace(item, **changes[kind])
    _, records = model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    gaps = [record for record in records if record.event_type == "FEED_GAP"]
    assert len(gaps) == 2
    assert all(record.payload == {"started_ms": model.engine.coverage_started_ms,
                                  "ended_ms": START + 3000} for record in gaps)
    assert model.windows["TEST"] is not previous
    assert model.windows["TEST"].retained_buckets == 1
    assert model._raw_first_ms == model._raw_coverage == {}
    assert model.last_warning_ns == {} and model.coverage is None
    assert model.epoch == item.epoch and model._sequence == item.sequence
    next_item = replace(bar(3), sequence=item.sequence + 1,
                        gateway_instance_id=item.gateway_instance_id,
                        generation=item.generation, dropped_total=item.dropped_total)
    _, next_records = model.observe_aggregate(next_item, observed_ns=next_item.published_utc_ns, legacy_active=set())
    assert not any(record.event_type == "FEED_GAP" for record in next_records)
    assert model.windows["TEST"].retained_buckets == 2


# Exact retained integer-ms pairs/event fields from the bounded SCNI sample.
# Distinct event IDs sharing SIP ms are NOT classified as duplicate trades.
RECORDED_SCNI_CALLS = (
    ("c4826492-bb21-404d-8cb6-28331a690875", 1791979824819, 1791979826826, "0.9363", 300, (12,)),
    ("f310af71-821d-4a35-a31a-18587b2e93eb", 1791979825119, 1791979826877, "0.9399", 1, (12, 37)),
    ("4ca78ed3-f83d-42a4-b3a7-6a6abaddce73", 1791979825712, 1791979826890, "0.9389", 1, (12, 37)),
    ("bef73538-a5c8-4a86-b46e-8d0d9c2216b2", 1791979825712, 1791979826915, "0.9388", 10, (12, 37)),
    ("09774ce3-1e03-465a-b176-d499ff71927f", 1791979825712, 1791979826928, "0.9388", 2, (12, 37)),
)


def test_recorded_five_scni_stale_callbacks_twenty_old_rows_two_scoped_windows_intact(monkeypatch):
    def replay(*, legacy=False):
        # CONTROLLED four active paths; only the five raw callbacks/times are real.
        seed_ms = RECORDED_SCNI_CALLS[0][1] - 30_000
        model = MomentumWarningConsumer(prior_closes={"SCNI": Decimal("1"), "DLXY": Decimal("1")},
            condition_version="CONTROLLED-feedgap1", coverage_started_ms=seed_ms)
        for seq, (offset, symbol, high) in enumerate(
            ((0, "SCNI", "10"), (0, "DLXY", "10"),
             (1, "SCNI", "12"), (1, "DLXY", "12")), 1
        ):
            start = seed_ms + offset * 1000
            item = replace(bar(high=high), symbol=symbol, sequence=seq,
                           start_ms=start, end_ms=start + 1000,
                           received_utc_ns=(start + 1000) * 1_000_000,
                           published_utc_ns=(start + 1000) * 1_000_000)
            model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
        model.complete_read(f"{seed_ms + 2000}-0", observed_ns=(seed_ms + 2000) * 1_000_000)
        assert len(model.engine.active_events) == 4
        for symbol in ("SCNI", "DLXY"):
            model.engine.ingest(TradePrint(symbol, seed_ms + 2001, Decimal("12.1"), 100,
                                           trade_id=f"CONTROLLED-fill-{symbol}"), allow_detection=False)
        assert all(event.fill is not None for event in model.engine._active.values())
        before = window_state(model)
        bucket_counts = {name: window.retained_buckets for name, window in model.windows.items()}
        rows = []
        for event_id, sip_ms, ended_ms, price, size, conditions in RECORDED_SCNI_CALLS:
            # Both times have only stored-ms precision; conversion is not new ns evidence.
            item = TradePrint("SCNI", sip_ms, Decimal(price), size, trade_id=event_id,
                              conditions=conditions, eligible=False,
                              exclusion_reason="CONTROLLED-unused-on-stale-branch")
            result = model.observe_trade(item, sip_timestamp_ns=sip_ms * 1_000_000,
                                         observed_ns=ended_ms * 1_000_000)
            assert model.last_status == "raw_future_or_stale" and model._last_raw_ns == {}
            assert all(record.event_type == "FEED_GAP" and record.payload == {
                "started_ms": sip_ms, "ended_ms": ended_ms} for record in result)
            rows.extend(result)
            assert window_state(model) == before
            assert {name: window.retained_buckets for name, window in model.windows.items()} == bucket_counts
        assert {(event.symbol, event.strategy_code) for event in model.engine._active.values()} == {
            (symbol, strategy) for symbol in ("SCNI", "DLXY")
            for strategy in ("momentum_30s_warn", "momentum_60s_warn")}
        assert all(event.feed_gap is legacy for event in model.engine._active.values())
        assert model.late_raw_ignored_by_symbol == {"SCNI": 5}
        return rows

    assert [end - sip for _, sip, end, *_ in RECORDED_SCNI_CALLS] == [2007, 1758, 1178, 1203, 1216]
    assert len({row[0] for row in RECORDED_SCNI_CALLS}) == 5
    rows = replay()
    assert rows == []
    source = textwrap.dedent(inspect.getsource(engine_runtime.MomentumPaperEngine.mark_feed_gap))
    scoped_guard = "        if symbol is not None and event.symbol != symbol:\n            continue\n"
    assert scoped_guard in source
    namespace = dict(engine_runtime.__dict__)
    old_source = source.replace("event.feed_gap or ", "").replace(scoped_guard, "")
    exec(compile(old_source, "<feedgap1-old-engine-replay>", "exec"), namespace)
    with monkeypatch.context() as mutation:
        mutation.setattr(engine_runtime.MomentumPaperEngine, "mark_feed_gap", namespace["mark_feed_gap"])
        consumer_source = textwrap.dedent(inspect.getsource(MomentumWarningConsumer.observe_trade))
        old_consumer_source = consumer_source.replace(
            'self.last_status = "raw_future_or_stale"\n        return ()',
            'self.last_status = "raw_future_or_stale"\n        return self.engine.mark_feed_gap(trade.sip_ts_ms, observed_ns // 1_000_000, symbol=symbol)',
        )
        assert old_consumer_source != consumer_source
        consumer_namespace = dict(consumer_runtime.__dict__)
        exec(compile(old_consumer_source, "<feedgap1-old-late-replay>", "exec"), consumer_namespace)
        mutation.setattr(MomentumWarningConsumer, "observe_trade", consumer_namespace["observe_trade"])
        old_rows = replay(legacy=True)
    assert len(old_rows) == 20
    assert len({row.logical_id for row in old_rows}) == 4
