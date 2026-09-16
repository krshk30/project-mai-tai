from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import TradePrint
from project_mai_tai.momentum_paper.report import grade_strategy


ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 16)


def _ms(clock: str, *, day: date = DAY) -> int:
    parsed = datetime.combine(day, datetime.strptime(clock, "%H:%M:%S.%f").time(), tzinfo=ET)
    return int(parsed.astimezone(UTC).timestamp() * 1000)


def _trade(
    clock: str,
    price: str,
    *,
    symbol: str = "TEST",
    eligible: bool = True,
    participant_clock: str | None = None,
    conditions: tuple[int, ...] = (),
) -> TradePrint:
    return TradePrint(
        symbol=symbol,
        sip_ts_ms=_ms(clock),
        participant_ts_ms=_ms(participant_clock) if participant_clock else None,
        price=Decimal(price),
        size=100,
        trade_id=f"{symbol}-{clock}-{price}",
        conditions=conditions,
        eligible=eligible,
        exclusion_reason="ineligible_fixture" if not eligible else "",
    )


def _engine(*, prior_close: str = "1", symbol: str = "TEST") -> MomentumPaperEngine:
    return MomentumPaperEngine(
        prior_closes={symbol: Decimal(prior_close)},
        condition_version="fixture-v1",
        coverage_started_ms=_ms("04:00:00.000"),
    )


def _detected(records) -> set[str]:
    return {record.strategy_code for record in records if record.event_type == "DETECTED"}


def _detect_both(engine: MomentumPaperEngine, *, symbol: str = "TEST") -> None:
    engine.ingest(_trade("04:11:00.000", "1.00", symbol=symbol))
    records = engine.ingest(_trade("04:11:25.000", "1.30", symbol=symbol))
    assert _detected(records) == {"momentum_30s", "momentum_60s"}


def test_45_second_move_fires_only_momentum_60() -> None:
    engine = _engine()
    engine.ingest(_trade("04:11:00.000", "1.00"))

    records = engine.ingest(_trade("04:11:45.000", "1.30"))

    assert _detected(records) == {"momentum_60s"}


def test_25_second_move_fires_both_strategies() -> None:
    engine = _engine()
    _detect_both(engine)


@pytest.mark.parametrize(
    ("clock", "accepted"),
    [
        ("04:10:59.000", False),
        ("04:11:00.000", True),
        ("09:29:59.000", True),
        ("09:30:00.000", False),
    ],
)
def test_detection_window_boundaries_are_frozen(clock: str, accepted: bool) -> None:
    engine = _engine()
    candidate_ms = _ms(clock)
    engine.coverage_started_ms = candidate_ms - 61_000
    engine.ingest(
        TradePrint(
            symbol="TEST",
            sip_ts_ms=candidate_ms - 20_000,
            price=Decimal("1"),
            size=10,
        )
    )

    records = engine.ingest(
        TradePrint(
            symbol="TEST",
            sip_ts_ms=candidate_ms,
            price=Decimal("1.30"),
            size=10,
        )
    )

    assert bool(_detected(records)) is accepted


@pytest.mark.parametrize(("prior_close", "accepted"), [("0.99", False), ("1.00", True)])
def test_prior_close_floor_is_inclusive(prior_close: str, accepted: bool) -> None:
    engine = _engine(prior_close=prior_close)
    engine.ingest(_trade("04:11:00.000", "1"))

    records = engine.ingest(_trade("04:11:25.000", "1.30"))

    assert bool(_detected(records)) is accepted


@pytest.mark.parametrize("symbol", ["ABC.D", "ABCDEF"])
def test_symbol_shape_excludes_dots_and_names_longer_than_five(symbol: str) -> None:
    engine = _engine(symbol=symbol)
    engine.ingest(_trade("04:11:00.000", "1", symbol=symbol))

    records = engine.ingest(_trade("04:11:25.000", "1.30", symbol=symbol))

    assert _detected(records) == set()


def test_warrant_like_name_is_retained_but_labeled() -> None:
    engine = _engine(symbol="ABCW")
    engine.ingest(_trade("04:11:00.000", "1", symbol="ABCW"))

    records = engine.ingest(_trade("04:11:25.000", "1.30", symbol="ABCW"))

    detected = [record for record in records if record.event_type == "DETECTED"]
    assert len(detected) == 2
    assert {record.payload["warrant_like"] for record in detected} == {True}


def test_fill_uses_first_eligible_print_strictly_after_detection() -> None:
    engine = _engine()
    _detect_both(engine)

    same_time = engine.ingest(_trade("04:11:25.000", "1.31"))
    later = engine.ingest(_trade("04:11:25.001", "1.32"))

    assert not any(record.event_type == "FILLED" for record in same_time)
    fills = [record for record in later if record.event_type == "FILLED"]
    assert len(fills) == 2
    assert {record.payload["fill"]["price"] for record in fills} == {"1.32"}
    assert {record.payload["quantity"] for record in fills} == {378}


def test_cancelled_plus_40_percent_print_cannot_trigger_fill_or_exit() -> None:
    trigger_engine = _engine()
    trigger_engine.ingest(_trade("04:11:00.000", "1"))
    bad_trigger = trigger_engine.ingest(
        _trade("04:11:25.000", "1.40", eligible=False, conditions=(99,))
    )
    assert _detected(bad_trigger) == set()

    engine = _engine()
    _detect_both(engine)
    bad_fill = engine.ingest(_trade("04:11:26.000", "1.31", eligible=False))
    assert not any(record.event_type == "FILLED" for record in bad_fill)
    engine.ingest(_trade("04:11:27.000", "1.31"))
    bad_exit = engine.ingest(_trade("04:11:28.000", "1.90", eligible=False))
    assert not any(record.event_type == "EXITED" for record in bad_exit)
    assert engine.session_excluded_prints == 2


def test_sip_timestamp_drives_windows_when_participant_timestamp_disagrees() -> None:
    engine = _engine()
    engine.ingest(_trade("04:11:00.000", "1", participant_clock="09:40:00.000"))

    records = engine.ingest(_trade("04:11:25.000", "1.30", participant_clock="04:00:00.000"))

    assert _detected(records) == {"momentum_30s", "momentum_60s"}


def test_same_exchange_second_stop_beats_target() -> None:
    engine = _engine()
    _detect_both(engine)
    engine.ingest(_trade("04:11:26.000", "1.30"))
    target = engine.ingest(_trade("04:11:27.100", "1.37"))
    stop = engine.ingest(_trade("04:11:27.900", "1.10"))

    assert not any(record.event_type == "EXITED" for record in target)
    exits = [record for record in stop if record.event_type == "EXITED"]
    assert len(exits) == 2
    assert {record.payload["exit_reason"] for record in exits} == {"STOP"}


def test_time_exit_requires_first_eligible_print_strictly_after_600_seconds() -> None:
    engine = _engine()
    _detect_both(engine)
    engine.ingest(_trade("04:11:26.000", "1.30"))
    at_deadline = engine.ingest(_trade("04:21:26.000", "1.31"))
    # Even a post-deadline price above the target exits by TIME: the clock wins.
    after_deadline = engine.ingest(_trade("04:21:26.001", "1.50"))

    assert not any(record.event_type == "EXITED" for record in at_deadline)
    exits = [record for record in after_deadline if record.event_type == "EXITED"]
    assert len(exits) == 2
    assert {record.payload["exit_reason"] for record in exits} == {"TIME"}
    finals = [record for record in after_deadline if record.event_type == "FINAL"]
    assert len(finals) == 2
    assert {record.payload["path_complete"] for record in finals} == {True}


def test_path_starts_at_detection_and_survives_an_early_exit() -> None:
    engine = _engine()
    _detect_both(engine)
    engine.ingest(_trade("04:11:26.000", "1.30"))
    engine.ingest(_trade("04:11:27.100", "1.37"))
    engine.advance_clock(_ms("04:11:28.000"))

    assert {row["status"] for row in engine.active_events} == {"TARGET"}
    assert {row["path_print_count"] for row in engine.active_events} == {3}

    path_record = engine.ingest(_trade("04:12:00.000", "1.34"))
    path_rows = [record for record in path_record if record.event_type == "PATH_PRINT"]
    assert len(path_rows) == 2
    assert {record.payload["dt_ms"] for record in path_rows} == {35_000}
    assert {row["path_print_count"] for row in engine.active_events} == {4}
    assert {dict(row["path_start"])["dt_ms"] for row in engine.active_events} == {0}


def test_repeat_requires_more_than_300_seconds() -> None:
    engine = _engine()
    engine.ingest(_trade("04:11:00.000", "1"))
    first = engine.ingest(_trade("04:11:25.000", "1.30"))
    assert _detected(first) == {"momentum_30s", "momentum_60s"}

    engine.ingest(_trade("04:16:00.000", "1"))
    blocked = engine.ingest(_trade("04:16:25.000", "1.30"))
    assert _detected(blocked) == set()

    engine.ingest(_trade("04:16:01.000", "1"))
    allowed = engine.ingest(_trade("04:16:26.000", "1.30"))
    assert _detected(allowed) == {"momentum_30s", "momentum_60s"}


def test_missing_next_print_becomes_no_fill() -> None:
    engine = _engine()
    _detect_both(engine)

    records = engine.advance_clock(_ms("04:11:35.001"))

    no_fills = [record for record in records if record.event_type == "NO_FILL"]
    assert len(no_fills) == 2
    assert engine.active_events == ()


def test_092959_event_finishes_using_symbol_tail_after_global_subscription_ends() -> None:
    engine = _engine()
    engine.coverage_started_ms = _ms("09:28:00.000")
    engine.ingest(_trade("09:29:40.000", "1"))
    detected = engine.ingest(_trade("09:29:59.000", "1.30"))
    assert _detected(detected) == {"momentum_30s", "momentum_60s"}
    engine.ingest(_trade("09:30:00.000", "1.31"))
    engine.ingest(_trade("09:30:01.000", "1.38"))
    engine.advance_clock(_ms("09:30:02.000"))

    records = engine.advance_clock(_ms("09:40:00.001"))

    finals = [record for record in records if record.event_type == "FINAL"]
    assert len(finals) == 2
    assert {record.payload["exit_reason"] for record in finals} == {"TARGET"}


def test_feed_gap_fails_closed_even_after_a_target() -> None:
    engine = _engine()
    _detect_both(engine)
    engine.ingest(_trade("04:11:26.000", "1.30"))
    engine.ingest(_trade("04:11:27.000", "1.37"))
    engine.advance_clock(_ms("04:11:28.000"))
    engine.mark_feed_gap(_ms("04:12:00.000"), _ms("04:12:05.000"))

    records = engine.advance_clock(_ms("04:21:26.001"))

    assert [record.event_type for record in records].count("UNANSWERABLE") == 2
    assert {record.payload["reason"] for record in records} == {"feed_gap"}
    assert {record.payload["path_complete"] for record in records} == {False}


def test_grade_requires_full_sample_and_drop_one_robustness() -> None:
    rows: list[dict[str, object]] = []
    for index in range(40):
        rows.append(
            {
                "status": "FINAL",
                "exit_reason": "TARGET" if index < 36 else "STOP",
                "pnl_pct": "5" if index < 36 else "-15",
                "session_date": f"2026-08-{index % 20 + 1:02d}",
                "symbol": f"S{index % 8}",
            }
        )

    grade = grade_strategy(rows, complete_sessions=20)

    assert grade.sample_met is True
    assert grade.win_rate_pct == Decimal("90")
    assert grade.average_pnl_pct == Decimal("3")
    assert grade.verdict == "PASS"
    assert all(int(row["count"]) > 0 for row in grade.reduced_samples)
