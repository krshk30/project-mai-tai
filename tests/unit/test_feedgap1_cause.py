"""Retained FEEDGAP1 clocks with controlled paper state, not live coverage proof."""

from dataclasses import replace
from decimal import Decimal
import inspect
import textwrap

import pytest

from project_mai_tai.momentum_paper import warning_consumer as runtime
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import TradePrint
from project_mai_tai.momentum_paper.warning_window import CoverageEvidence
from tests.unit.test_momentum_warning import START, EPOCH, bar, consumer, trade


# Exact SCNI event ID/SIP and DB observation-ms pairs retained 2026-10-14
# 12:13/14 UTC: FEEDGAP1-real-matched-window / real-newest20 JSON evidence.
REAL_CALLBACKS = (
    ("c4826492-bb21-404d-8cb6-28331a690875", 1791979824819, 1791979826826, "0.9363", 300),
    ("f310af71-821d-4a35-a31a-18587b2e93eb", 1791979825119, 1791979826877, "0.9399", 1),
    ("4ca78ed3-f83d-42a4-b3a7-6a6abaddce73", 1791979825712, 1791979826890, "0.9389", 1),
    ("bef73538-a5c8-4a86-b46e-8d0d9c2216b2", 1791979825712, 1791979826915, "0.9388", 10),
    ("09774ce3-1e03-465a-b176-d499ff71927f", 1791979825712, 1791979826928, "0.9388", 2),
)


def active_model():
    model = consumer()
    for item in (bar(), bar(1, high="12")):
        model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    assert len(model.engine.active_events) == 2
    return model


def state(model, *, include_paths=True):
    return (dict(model.windows), dict(model._latest), dict(model.last_warning_ns),
            dict(model._last_raw_ns), dict(model._raw_first_ms), dict(model._raw_coverage),
            model.coverage, model.epoch, model._sequence, model._drops,
            model.engine.active_events if include_paths else None)


def replay_real_callbacks():
    # Four controlled events: this does not reconstruct the real entries/prices.
    model = runtime.MomentumWarningConsumer(
        prior_closes={"SCNI": Decimal("2"), "DLXY": Decimal("2")},
        condition_version="CONTROLLED", coverage_started_ms=1791979218476,
    )
    for offset in (0, 1):
        for index, symbol in enumerate(("SCNI", "DLXY")):
            start = 1791979228000 + offset * 1000
            item = replace(bar(offset, high="12" if offset else "10", symbol=symbol),
                           start_ms=start, end_ms=start + 1000,
                           received_utc_ns=(start + 1000) * 1_000_000,
                           published_utc_ns=(start + 1000) * 1_000_000,
                           sequence=offset * 2 + index + 1)
            model.observe_aggregate(item, observed_ns=item.published_utc_ns, legacy_active=set())
    assert len(model.engine.active_events) == 4
    for symbol in ("SCNI", "DLXY"):
        fill = TradePrint(symbol, 1791979230001, Decimal("12.1"), 100, eligible=True)
        records = model.observe_trade(fill, sip_timestamp_ns=fill.sip_ts_ms * 1_000_000,
                                       observed_ns=fill.sip_ts_ms * 1_000_000)
        assert sum(row.event_type == "FILLED" for row in records) == 2
    before = state(model, include_paths=False)
    records = []
    for event_id, sip_ms, observed_ms, price, size in REAL_CALLBACKS:
        conditions = (12,) if event_id == REAL_CALLBACKS[0][0] else (12, 37)
        item = TradePrint("SCNI", sip_ms, Decimal(price), size,
                          conditions=conditions, eligible=True)
        records.extend(model.observe_trade(item, sip_timestamp_ns=sip_ms * 1_000_000,
                                           observed_ns=observed_ms * 1_000_000))
        assert model.last_status == "raw_future_or_stale"
    return model, before, records


def test_five_real_scni_callbacks_mark_only_two_scni_paths():
    # Keep the qualified case identity; FEEDGAP2 supersedes late-gap marking.
    model, before, records = replay_real_callbacks()
    assert records == []
    assert model.late_raw_ignored_by_symbol == {"SCNI": 5}
    assert state(model, include_paths=False) == before
    assert not any(event.feed_gap for event in model.engine._active.values())
    assert model._raw_coverage == {}  # No complete raw-tape claim is manufactured.
    terminal = model.engine.close_session(1791979831001)
    scni = [row for row in terminal if row.symbol == "SCNI"]
    assert len(scni) == 2
    assert {row.event_type for row in scni} == {"UNANSWERABLE"}
    assert {row.payload["reason"] for row in scni} == {"session_tail_closed_before_complete_path"}
    assert {row.payload["path_complete"] for row in scni} == {False}
    # No complete tape is invented for the controlled DLXY path either.
    assert all(row.payload["reason"] != "feed_gap" for row in terminal if row.symbol == "DLXY")


def restore_global_raw_call(monkeypatch):
    source = textwrap.dedent(inspect.getsource(runtime.MomentumWarningConsumer.observe_trade))
    mutated = source.replace(
        'return self.engine.mark_feed_gap(\n            trade.sip_ts_ms, observed_ns // 1_000_000, symbol=symbol,\n        )',
        'return self.engine.mark_feed_gap(trade.sip_ts_ms, observed_ns // 1_000_000)',
    )
    mutated = mutated.replace('self.last_status = "raw_future_or_stale"\n        return ()',
        'self.last_status = "raw_future_or_stale"\n        return self.engine.mark_feed_gap(trade.sip_ts_ms, observed_ns // 1_000_000)')
    assert mutated != source
    namespace = dict(runtime.__dict__)
    exec(compile(mutated, "<feedgap1-old-stale-branch>", "exec"), namespace)
    monkeypatch.setattr(runtime.MomentumWarningConsumer, "observe_trade", namespace["observe_trade"])


def test_omit_symbol_scope_mutation_marks_dlxy_and_fails_real_control(monkeypatch):
    restore_global_raw_call(monkeypatch)
    _, _, records = replay_real_callbacks()
    assert len(records) == 4
    assert {row.symbol for row in records} == {"SCNI", "DLXY"}
    with pytest.raises(AssertionError):
        test_five_real_scni_callbacks_mark_only_two_scni_paths()


def test_restore_original_990_stale_branch_reproduces_twenty_rows(monkeypatch):
    restore_global_raw_call(monkeypatch)
    gap_source = textwrap.dedent(inspect.getsource(MomentumPaperEngine.mark_feed_gap))
    gap_namespace = dict(inspect.getmodule(MomentumPaperEngine).__dict__)
    exec(compile(gap_source.replace("event.feed_gap or ", ""), "<feedgap1-old-engine>", "exec"),
         gap_namespace)
    monkeypatch.setattr(MomentumPaperEngine, "mark_feed_gap", gap_namespace["mark_feed_gap"])
    _, _, records = replay_real_callbacks()
    assert len(records) == 20
    assert {row.event_type for row in records} == {"FEED_GAP"}
    with pytest.raises(AssertionError):
        test_five_real_scni_callbacks_mark_only_two_scni_paths()


@pytest.mark.parametrize("age_ns", [1_000_000_001, -1, -1_000_000])
def test_stale_and_future_refusal_never_ingest_fill_detect_or_claim_coverage(age_ns, monkeypatch):
    model = active_model()
    before = state(model, include_paths=False)
    def forbidden(*args, **kwargs):
        pytest.fail("refused raw print reached ingestion or bucket detection")
    monkeypatch.setattr(model.engine, "ingest", forbidden)
    monkeypatch.setattr(model, "_bucket_signals", forbidden)
    item = trade(2001, "100")
    stamp = item.sip_ts_ms * 1_000_000
    records = model.observe_trade(item, sip_timestamp_ns=stamp, observed_ns=stamp + age_ns)
    if age_ns > 1_000_000_000:
        assert records == () and model.late_raw_ignored_by_symbol == {"TEST": 1}
        assert not any(event.feed_gap for event in model.engine._active.values())
    else:
        assert len(records) == 2
        assert {row.symbol for row in records} == {"TEST"}
        assert all(event.feed_gap for event in model.engine._active.values())
        assert not model.late_raw_ignored_by_symbol
    assert model.last_status == "raw_future_or_stale"
    assert state(model, include_paths=False) == before


def test_exact_one_second_usable_and_stale_repeat_marks_only_its_symbol_once():
    model = active_model()
    item = trade(2001, "12.1")
    stamp = item.sip_ts_ms * 1_000_000
    records = model.observe_trade(item, sip_timestamp_ns=stamp, observed_ns=stamp + 1_000_000_000)
    assert model.last_status != "raw_future_or_stale"
    assert len([row for row in records if row.event_type == "FILLED"]) == 2
    before = state(model, include_paths=False)
    assert not any(event.feed_gap for event in model.engine._active.values())
    records = model.observe_trade(item, sip_timestamp_ns=stamp, observed_ns=stamp + 1_000_000_001)
    assert records == ()
    assert model.observe_trade(item, sip_timestamp_ns=stamp, observed_ns=stamp + 1_000_000_002) == ()
    assert model.late_raw_ignored_by_symbol == {"TEST": 2}
    assert not any(event.feed_gap for event in model.engine._active.values())
    assert state(model, include_paths=False) == before
    assert model._raw_coverage == {}


@pytest.mark.parametrize("kind", ["sequence", "drops", "epoch", "raw_drops",
                                  "raw_epoch", "raw_missing", "raw_discontinuous", "raw_hole"])
def test_genuine_gap_still_marks_and_finishes_unanswerable(kind):
    model = active_model()
    item = trade(2001, "12.1")
    model.observe_trade(item, sip_timestamp_ns=item.sip_ts_ms * 1_000_000,
                        observed_ns=item.sip_ts_ms * 1_000_000)
    if kind.startswith("raw_"):
        proof = CoverageEvidence(EPOCH, START, START + 2001, 0, True)
        model._raw_coverage["TEST"] = proof
        next_trade = trade(2002, "12.1")
        changed = {"raw_drops": {"dropped_total": 1},
                   "raw_epoch": {"epoch": ("new-gateway", 1)},
                   "raw_discontinuous": {"continuous": False},
                   "raw_hole": {"start_ms": START + 2002}}
        coverage = (None if kind == "raw_missing" else
                    replace(proof, through_ms=START + 2002, **changed[kind]))
        records = model.observe_trade(next_trade,
            sip_timestamp_ns=next_trade.sip_ts_ms * 1_000_000,
            observed_ns=next_trade.sip_ts_ms * 1_000_000,
            raw_coverage=coverage)
    else:
        changes = {"sequence": {"sequence": 4}, "drops": {"dropped_total": 1},
                   "epoch": {"gateway_instance_id": "new-gateway"}}
        item = replace(bar(2), **changes[kind])
        _, records = model.observe_aggregate(item, observed_ns=item.published_utc_ns,
                                             legacy_active=set())
        assert model.windows["TEST"].retained_buckets == 1
    assert len([row for row in records if row.event_type == "FEED_GAP"]) == 2
    stale = trade(4000, "100")
    assert model.observe_trade(stale, sip_timestamp_ns=stale.sip_ts_ms * 1_000_000,
        observed_ns=stale.sip_ts_ms * 1_000_000 + 1_000_000_001) == ()
    assert all(event.feed_gap for event in model.engine._active.values())
    terminal = model.engine.close_session(START + 602_001)
    assert len([row for row in terminal if row.event_type == "UNANSWERABLE"]) == 2
    assert {row.payload["reason"] for row in terminal} == {"feed_gap"}
    assert {row.payload["path_complete"] for row in terminal} == {False}
