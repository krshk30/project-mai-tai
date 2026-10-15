"""FEEDGAP1: one gap receipt per event, without weakening path semantics."""

import inspect
import textwrap
from decimal import Decimal

import pytest

from project_mai_tai.momentum_paper import engine as runtime
from tests.unit.test_momentum_paper_engine import _engine, _detect_both, _ms, _trade


def test_repeated_overlapping_gaps_emit_once_for_each_event():
    engine = _engine()
    _detect_both(engine)
    engine.ingest(_trade("04:11:26.000", "1.20"))
    first = engine.mark_feed_gap(_ms("04:12:00.000"), _ms("04:12:01.000"))
    assert len(first) == 2
    assert {record.event_type for record in first} == {"FEED_GAP"}
    assert len({record.logical_id for record in first}) == 2
    assert all(record.payload == {"started_ms": _ms("04:12:00.000"),
                                  "ended_ms": _ms("04:12:01.000")} for record in first)
    for _ in range(20):
        assert engine.mark_feed_gap(_ms("04:12:00.000"), _ms("04:12:01.000")) == ()
        assert engine.mark_feed_gap(_ms("04:13:00.000"), _ms("04:13:01.000")) == ()
    assert all(event.feed_gap for event in engine._active.values())
    terminal = engine.ingest(_trade("04:21:26.001", "1.20"))
    unanswerable = [record for record in terminal if record.event_type == "UNANSWERABLE"]
    assert len(unanswerable) == 2
    assert {record.payload["reason"] for record in unanswerable} == {"feed_gap"}
    assert {record.payload["path_complete"] for record in unanswerable} == {False}


def test_nonoverlapping_gaps_never_mark_or_consume_first_receipt():
    engine = _engine()
    _detect_both(engine)
    engine.ingest(_trade("04:11:26.000", "1.20"))
    assert engine.mark_feed_gap(_ms("04:00:00.000"), _ms("04:00:01.000")) == ()
    assert engine.mark_feed_gap(_ms("04:30:00.000"), _ms("04:30:01.000")) == ()
    assert not any(event.feed_gap for event in engine._active.values())
    assert len(engine.mark_feed_gap(_ms("04:12:00.000"), _ms("04:12:01.000"))) == 2


def test_gap_after_target_still_marks_path_unanswerable_once():
    engine = _engine()
    _detect_both(engine)
    engine.ingest(_trade("04:11:26.000", "1.30"))
    engine.ingest(_trade("04:11:27.000", "1.37"))
    engine.advance_clock(_ms("04:11:28.000"))
    assert len(engine.mark_feed_gap(_ms("04:12:00.000"), _ms("04:12:05.000"))) == 2
    assert engine.mark_feed_gap(_ms("04:12:00.000"), _ms("04:12:05.000")) == ()
    terminal = engine.advance_clock(_ms("04:21:26.001"))
    assert [record.event_type for record in terminal].count("UNANSWERABLE") == 2
    assert {record.payload["reason"] for record in terminal} == {"feed_gap"}
    assert {record.payload["path_complete"] for record in terminal} == {False}


def test_restore_duplicate_gap_emission_mutation_fails_repeated_control(monkeypatch):
    test_repeated_overlapping_gaps_emit_once_for_each_event()
    source = textwrap.dedent(inspect.getsource(runtime.MomentumPaperEngine.mark_feed_gap))
    assert "event.feed_gap or " in source
    namespace = dict(runtime.__dict__)
    exec(compile(source.replace("event.feed_gap or ", ""), "<feedgap1-controlled-mutation>", "exec"), namespace)
    monkeypatch.setattr(runtime.MomentumPaperEngine, "mark_feed_gap", namespace["mark_feed_gap"])
    with pytest.raises(AssertionError):
        test_repeated_overlapping_gaps_emit_once_for_each_event()


def two_symbol_target_paths():
    engine = runtime.MomentumPaperEngine(
        prior_closes={"SCNI": Decimal("1"), "DLXY": Decimal("1")},
        condition_version="CONTROLLED-feedgap1", coverage_started_ms=_ms("04:00:00.000"),
    )
    for symbol in ("SCNI", "DLXY"):
        _detect_both(engine, symbol=symbol)
        engine.ingest(_trade("04:11:26.000", "1.30", symbol=symbol))
        engine.ingest(_trade("04:11:27.000", "1.37", symbol=symbol))
    engine.advance_clock(_ms("04:11:28.000"))
    assert len(engine._active) == 4
    return engine


def test_symbol_scoped_gap_marks_only_affected_symbol_unanswerable():
    engine = two_symbol_target_paths()
    rows = engine.mark_feed_gap(_ms("04:12:00.000"), _ms("04:12:01.000"), symbol="SCNI")
    assert len(rows) == 2 and {row.symbol for row in rows} == {"SCNI"}
    assert engine.mark_feed_gap(_ms("04:12:01.000"), _ms("04:12:02.000"), symbol="SCNI") == ()
    assert all(event.feed_gap is (event.symbol == "SCNI") for event in engine._active.values())
    terminal = engine.advance_clock(_ms("04:21:26.001"))
    affected = [row for row in terminal if row.symbol == "SCNI"]
    unaffected = [row for row in terminal if row.symbol == "DLXY"]
    assert len(affected) == len(unaffected) == 2
    assert all(row.event_type == "UNANSWERABLE" and row.payload["reason"] == "feed_gap" for row in affected)
    assert all(row.event_type == "FINAL" and row.payload["path_complete"] for row in unaffected)


def test_default_global_gap_remains_global_after_scoped_gap():
    engine = two_symbol_target_paths()
    assert engine.mark_feed_gap(_ms("04:12:00.000"), _ms("04:12:01.000"), symbol="ABSENT") == ()
    assert not any(event.feed_gap for event in engine._active.values())
    assert len(engine.mark_feed_gap(_ms("04:12:00.000"), _ms("04:12:01.000"), symbol="SCNI")) == 2
    global_rows = engine.mark_feed_gap(_ms("04:12:01.000"), _ms("04:12:02.000"))
    assert len(global_rows) == 2 and {row.symbol for row in global_rows} == {"DLXY"}
    assert all(event.feed_gap for event in engine._active.values())
    assert engine.mark_feed_gap(_ms("04:12:02.000"), _ms("04:12:03.000")) == ()
