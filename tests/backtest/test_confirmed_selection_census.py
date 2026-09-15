from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.backtest.atr_oracle import Bar
from project_mai_tai.backtest.confirmed_selection_census import (
    CANDIDATE_BELOW_HIGH_PCT,
    CANDIDATE_HIGH_AGE_MINUTES,
    ConfirmEvent,
    EvidenceUnknown,
    FlipObservation,
    LiveEntry,
    SymbolDayInput,
    _assert_after_close,
    _bar_at,
    _confirmed_keys,
    _count_five_pct_swings,
    _criterion,
    _features,
    _live_entries_for_flip,
    _lower_high_streak,
    _opening_confirms,
    _outcome,
    evaluate_symbol_day,
    evaluate_candidate,
    run_census,
    summarize_feature,
)

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 15)


def _at(hour: int, minute: int = 0, *, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=ET)


def _bar(
    at: datetime,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    volume: int = 100,
) -> Bar:
    return Bar(
        ts=int(at.timestamp() * 1000),
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=volume,
    )


def _observation(index: int, *, hit: bool, age: float, below: float) -> FlipObservation:
    at = _at(7) + timedelta(minutes=index)
    return FlipObservation(
        day=DAY + timedelta(days=index),
        symbol=f"S{index:03d}",
        flip_at=at,
        flip_close=10.0,
        sell_at=at + timedelta(minutes=10),
        hit_plus5=hit,
        mfe_pct=6.0 if hit else 2.0,
        mae_pct=-1.0,
        feature_anchor_et="07:00",
        pre0700_available=False,
        minutes_since_high=age,
        below_session_high_pct=below,
        lower_high_streak_30m=0,
        kaufman_efficiency_60=0.5,
        prior_120m_range_pct=10.0,
        prior_120m_swing_count=1,
        prior_120m_bar_count=120,
        volume_trend_30_over_60=1.0,
        change_since_confirm_pct=0.0,
        confirm_change_pct=50.0,
        confirm_path="PATH_C_EXTREME_MOVER",
        live_entry_ids=(),
    )


def test_fade_only_symbol_days_are_not_in_the_confirmed_population() -> None:
    confirmed = (DAY, "YES")
    fade_only = (DAY, "NO")
    events = {
        confirmed: [ConfirmEvent("CONFIRM", _at(8), 10.0, 50.0, "PATH_C_EXTREME_MOVER")],
        fade_only: [ConfirmEvent("FADE", _at(9))],
    }

    assert _confirmed_keys(events) == [confirmed]


def test_repeated_confirm_snapshots_do_not_mint_new_memberships() -> None:
    events = [
        ConfirmEvent("CONFIRM", _at(7), 10.0, 40.0, "PATH_C_EXTREME_MOVER"),
        ConfirmEvent("CONFIRM", _at(7, 1), 10.1, 41.0, "PATH_C_EXTREME_MOVER"),
        ConfirmEvent("FADE", _at(8)),
        ConfirmEvent("RETENTION_DROP", _at(8)),
        ConfirmEvent("CONFIRM", _at(9), 9.0, 31.0, "PATH_C_EXTREME_MOVER"),
    ]

    assert [(event.at.strftime("%H:%M"), event.price) for event in _opening_confirms(events)] == [
        ("07:00", 10.0),
        ("09:00", 9.0),
    ]


def test_every_feature_is_frozen_before_the_flip_bar() -> None:
    bars = tuple(
        _bar(_at(7) + timedelta(minutes=index), 10.0, high=12.0 if index == 0 else 10.0)
        for index in range(61)
    )
    item = SymbolDayInput(
        DAY,
        "TEST",
        (ConfirmEvent("CONFIRM", _at(7), 10.0, 50.0, "PATH_C_EXTREME_MOVER"),),
        bars,
    )
    ordinary_flip = _bar(_at(8, 1), 10.0, high=10.0, volume=1)
    dangerous_flip = _bar(_at(8, 1), 99.0, high=1000.0, volume=999_999_999)
    ordinary = _features(replace(item, schwab_bars=(*bars, ordinary_flip)), ordinary_flip)
    mutated_flip = _features(replace(item, schwab_bars=(*bars, dangerous_flip)), dangerous_flip)

    assert ordinary == mutated_flip
    assert ordinary["kaufman_efficiency_60"] == 0.0
    assert ordinary["prior_120m_bar_count"] == 61
    assert ordinary["below_session_high_pct"] == pytest.approx(100 / 6)


def test_pre0700_history_changes_the_session_high_only_when_capture_is_available() -> None:
    schwab = tuple(_bar(_at(7) + timedelta(minutes=index), 10.0) for index in range(61))
    pre = (_bar(_at(6), 20.0),)
    flip = _bar(_at(8, 1), 10.0)
    base = SymbolDayInput(DAY, "TEST", (), schwab, pre0700_bars=pre)

    unavailable = _features(base, flip)
    available = _features(replace(base, pre0700_available=True), flip)

    assert unavailable["feature_anchor_et"] == "07:00"
    assert unavailable["below_session_high_pct"] == 0.0
    assert available["feature_anchor_et"] == "04:00"
    assert available["below_session_high_pct"] == 50.0
    assert available["minutes_since_high"] == 121.0


def test_volume_trend_is_unknown_when_the_window_mixes_massive_and_schwab_units() -> None:
    pre = tuple(_bar(_at(6, 30) + timedelta(minutes=index), 10.0) for index in range(30))
    schwab = tuple(_bar(_at(7) + timedelta(minutes=index), 10.0) for index in range(90))
    item = SymbolDayInput(
        DAY,
        "TEST",
        (),
        schwab,
        pre0700_bars=pre,
        pre0700_available=True,
    )

    mixed = _features(item, _bar(_at(7, 30), 10.0))
    schwab_only = _features(item, _bar(_at(8, 30), 10.0))

    assert mixed["volume_trend_30_over_60"] is None
    assert schwab_only["volume_trend_30_over_60"] == 1.0


def test_lower_high_streak_uses_completed_consecutive_thirty_minute_blocks() -> None:
    bars = [
        _bar(_at(7), 9.0, high=10.0),
        _bar(_at(7, 30), 8.0, high=9.0),
        _bar(_at(8), 7.0, high=8.0),
    ]

    assert _lower_high_streak(bars, _at(8, 30), _at(7)) == 2
    assert _lower_high_streak(bars[:1], _at(7, 30), _at(7)) is None


def test_five_percent_swing_count_is_directional_and_non_overlapping() -> None:
    assert _count_five_pct_swings([100.0, 106.0, 100.0, 105.0]) == 3
    assert _count_five_pct_swings([100.0]) is None


def test_outcome_starts_after_flip_bar_and_stops_at_the_next_sell() -> None:
    bars = [
        _bar(_at(8), 100.0, high=200.0),
        _bar(_at(8, 1), 100.0, high=104.0, low=98.0),
        _bar(_at(8, 2), 100.0, high=104.0, low=97.0),
        _bar(_at(8, 3), 100.0, high=120.0, low=90.0),
    ]
    rows = [{"flip": "BUY"}, {"flip": None}, {"flip": "SELL"}, {"flip": None}]

    sell_at, hit, mfe, mae = _outcome(bars, rows, 0)

    assert sell_at == _at(8, 2)
    assert hit is False
    assert mfe == pytest.approx(4.0)
    assert mae == pytest.approx(-3.0)


def test_deciles_keep_equal_feature_values_together_and_report_the_gradient() -> None:
    low = [
        replace(_observation(index, hit=False, age=10.0, below=1.0), prior_120m_range_pct=1.0)
        for index in range(10)
    ]
    high = [
        replace(_observation(index + 10, hit=True, age=10.0, below=1.0), prior_120m_range_pct=2.0)
        for index in range(10)
    ]

    summary = summarize_feature([*low, *high], "prior_120m_range_pct")

    assert [(row.flips, row.hits, row.value_min) for row in summary.deciles] == [
        (10, 0, 1.0),
        (10, 10, 2.0),
    ]
    assert summary.monotonic is True
    assert summary.direction == "increasing"
    assert summary.survives_drop_one_symbol is True
    assert summary.survives_drop_one_day is True
    assert summary.retained is True


def test_flat_decile_rates_are_not_retained_as_a_gradient() -> None:
    rows = [
        replace(
            _observation(index, hit=False, age=10.0, below=1.0), prior_120m_range_pct=float(index)
        )
        for index in range(20)
    ]

    summary = summarize_feature(rows, "prior_120m_range_pct")

    assert summary.monotonic is False
    assert summary.direction == "flat"
    assert summary.retained is False


def test_feature_gradient_cannot_be_carried_by_one_name_or_one_day() -> None:
    low = [
        replace(_observation(index, hit=False, age=10.0, below=1.0), prior_120m_range_pct=1.0)
        for index in range(60)
    ]
    ordinary_high = [
        replace(
            _observation(index + 100, hit=False, age=10.0, below=1.0),
            prior_120m_range_pct=2.0,
        )
        for index in range(60)
    ]
    runner = [
        replace(
            _observation(index + 200, hit=True, age=10.0, below=1.0),
            symbol="RUNNER",
            day=DAY,
            prior_120m_range_pct=2.0,
        )
        for index in range(10)
    ]

    summary = summarize_feature([*low, *ordinary_high, *runner], "prior_120m_range_pct")

    assert summary.monotonic is True
    assert summary.direction == "increasing"
    assert summary.survives_drop_one_symbol is False
    assert summary.survives_drop_one_day is False
    assert summary.retained is False
    assert summary.max_day_flips == 11


def test_candidate_requires_both_conditions_and_survives_drop_one_checks() -> None:
    blocked = [_observation(index, hit=False, age=91.0, below=5.1) for index in range(61)]
    kept = [_observation(index + 1000, hit=True, age=90.0, below=5.0) for index in range(61)]

    result = evaluate_candidate([*blocked, *kept])

    assert result.blocked == 61
    assert result.kept == 61
    assert result.count_floor_met is True
    assert result.rate_separation_met is True
    assert result.survives_drop_one_symbol is True
    assert result.survives_drop_one_day is True
    assert result.pass_criterion is True


def test_candidate_does_not_block_when_only_one_condition_crosses_the_boundary() -> None:
    rows = [
        _observation(0, hit=False, age=91.0, below=5.1),
        _observation(1, hit=False, age=91.0, below=4.0),
        _observation(2, hit=False, age=80.0, below=6.0),
    ]

    result = evaluate_candidate(rows)

    assert result.blocked == 1
    assert result.kept == 2


def test_candidate_fails_when_one_runner_and_one_day_carry_the_separation() -> None:
    blocked = [_observation(index, hit=False, age=91.0, below=5.1) for index in range(61)]
    kept_misses = [_observation(index + 100, hit=False, age=90.0, below=5.0) for index in range(60)]
    runner_hits = [
        replace(
            _observation(index + 200, hit=True, age=90.0, below=5.0),
            symbol="RUNNER",
            day=DAY,
        )
        for index in range(61)
    ]

    result = evaluate_candidate([*blocked, *kept_misses, *runner_hits])

    assert result.count_floor_met is True
    assert result.rate_separation_met is True
    assert result.survives_drop_one_symbol is False
    assert result.survives_drop_one_day is False
    assert result.pass_criterion is False


def test_candidate_does_not_pass_on_a_small_after_the_fact_example() -> None:
    rows = [
        _observation(
            0, hit=False, age=CANDIDATE_HIGH_AGE_MINUTES + 1, below=CANDIDATE_BELOW_HIGH_PCT + 1
        ),
        _observation(1, hit=True, age=CANDIDATE_HIGH_AGE_MINUTES, below=CANDIDATE_BELOW_HIGH_PCT),
    ]

    result = evaluate_candidate(rows)

    assert result.rate_separation_met is True
    assert result.count_floor_met is False
    assert result.pass_criterion is False


def test_candidate_requires_blocked_rate_below_half_the_kept_rate() -> None:
    blocked = [_observation(index, hit=index < 18, age=91.0, below=5.1) for index in range(60)]
    kept = [_observation(index + 100, hit=index < 30, age=90.0, below=5.0) for index in range(60)]
    rows = [*blocked, *kept]

    result = evaluate_candidate(rows)

    assert result.blocked_hit_rate_pct == pytest.approx(30.0)
    assert result.kept_hit_rate_pct == pytest.approx(50.0)
    assert result.rate_separation_met is False
    assert result.pass_criterion is False
    assert _criterion(rows) is False


def test_live_entry_matching_covers_the_flip_minute_and_immediately_following_minute() -> None:
    entries = [
        LiveEntry("inside", _at(9, 30) + timedelta(seconds=59), ("live:orb",)),
        LiveEntry("next", _at(9, 31), ("live:orb",)),
        LiveEntry("outside", _at(9, 32), ("live:orb",)),
    ]

    assert _live_entries_for_flip(entries, _at(9, 30)) == ("inside", "next")


def test_run_census_reports_confirmed_and_bar_measurable_denominators_separately() -> None:
    class Source:
        def load(self, start: date, end: date) -> list[SymbolDayInput]:
            assert (start, end) == (DAY, DAY)
            confirm = (ConfirmEvent("CONFIRM", _at(8), 10.0, 50.0, "PATH_C_EXTREME_MOVER"),)
            return [
                SymbolDayInput(DAY, "HAS", confirm, (_bar(_at(7), 10.0),)),
                SymbolDayInput(DAY, "AFTER", confirm, (_bar(_at(20), 10.0),)),
                SymbolDayInput(DAY, "NONE", confirm, ()),
            ]

    report = run_census(Source(), DAY, DAY)

    assert report.confirmed_symbol_days == 3
    assert report.daily_bar_present_symbol_days == 2
    assert report.bar_measurable_symbol_days == 1
    assert report.no_live_bar_symbol_days == 1
    assert report.no_analysis_window_bar_symbol_days == 2
    assert report.confirm_memberships == 3


def test_real_sugp_mysz_tape_preserves_the_observed_selection_shapes() -> None:
    raw = json.loads(
        (Path(__file__).parent / "fixtures" / "SUGP_MYSZ_20260915_selection.json").read_text(
            encoding="utf-8"
        )
    )
    items = []
    for item in raw["items"]:
        items.append(
            SymbolDayInput(
                day=date.fromisoformat(item["day"]),
                symbol=item["symbol"],
                scanner_events=tuple(
                    ConfirmEvent(
                        event["event_type"],
                        datetime.fromisoformat(event["at"]),
                        event["price"],
                        event["change_pct"],
                        event["confirm_path"],
                    )
                    for event in item["scanner_events"]
                ),
                schwab_bars=tuple(Bar(**bar) for bar in item["schwab_bars"]),
                pre0700_bars=tuple(Bar(**bar) for bar in item["pre0700_bars"]),
                pre0700_available=item["pre0700_available"],
                live_entries=tuple(
                    LiveEntry(
                        entry["logical_id"],
                        datetime.fromisoformat(entry["filled_at"]),
                        tuple(entry["accounts"]),
                    )
                    for entry in item["live_entries"]
                ),
            )
        )

    class Source:
        def load(self, start: date, end: date) -> list[SymbolDayInput]:
            assert (start, end) == (DAY, DAY)
            return items

    report = run_census(Source(), DAY, DAY)
    focus = {(row.symbol, row.flip_at.strftime("%H:%M")): row for row in report.observations}

    assert report.confirmed_symbol_days == 2
    assert report.bar_measurable_symbol_days == 2
    assert focus[("SUGP", "09:44")].hit_plus5 is False
    assert focus[("SUGP", "09:44")].live_entry_ids == ()
    assert focus[("MYSZ", "09:34")].hit_plus5 is False
    assert focus[("MYSZ", "09:34")].mfe_pct == pytest.approx(0.998003992)
    assert focus[("MYSZ", "09:34")].minutes_since_high == 327.0
    assert focus[("MYSZ", "11:22")].hit_plus5 is True
    assert focus[("MYSZ", "11:22")].mfe_pct == pytest.approx(12.311015119)


def test_canonical_oracle_is_anchored_at_0400_but_only_0700_flips_are_units(monkeypatch) -> None:
    bars = (_bar(_at(4), 10.0), _bar(_at(7), 10.0))
    seen: dict[str, object] = {}

    def fake_oracle(actual, *, seed, period, factor):
        seen["bars"] = tuple(_bar_at(row) for row in actual)
        seen["settings"] = (seed, period, factor)
        return [{"flip": "BUY"} for _ in actual]

    from project_mai_tai.backtest import confirmed_selection_census as module

    monkeypatch.setattr(module, "compute_atr_trail", fake_oracle)
    item = SymbolDayInput(DAY, "TEST", (), bars)

    observations = evaluate_symbol_day(item)

    assert seen == {
        "bars": (_at(4), _at(7)),
        "settings": ("sma5", 5, 3.5),
    }
    assert [row.flip_at for row in observations] == [_at(7)]


def test_current_day_query_refuses_to_run_during_the_live_window() -> None:
    with pytest.raises(EvidenceUnknown, match="after-close only"):
        _assert_after_close(DAY, datetime(2026, 9, 15, 14, tzinfo=UTC))

    _assert_after_close(DAY, datetime(2026, 9, 15, 21, tzinfo=UTC))
