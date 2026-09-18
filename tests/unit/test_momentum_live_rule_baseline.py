from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.backtest.momentum_live_rule_baseline import (
    AggregateBar,
    BAND_REJECT_SUSPECT_SESSIONS,
    DETECTOR_SUSPECT_DETECTIONS,
    aggregate_has_candidate,
    capture_session,
    capture_sessions,
    detector_band_decision,
    detector_is_suspect,
    path_control_payload,
    premarket_range_has_candidate,
    prior_close_universe,
    raw_trade_row,
    read_capture,
    replay_capture,
    replay_directory,
    report_payload,
    require_capture_window,
    screen_control_session,
)


def _ms(clock: str) -> int:
    parsed = datetime.fromisoformat(f"2026-09-17T{clock}-04:00")
    return int(parsed.timestamp() * 1000)


def _conditions() -> dict[str, object]:
    rules = {}
    for code, high_low, open_close in (
        (12, False, False),
        (13, False, False),
        (14, True, True),
        (37, False, False),
        (41, True, True),
    ):
        rules[str(code)] = {
            "condition_id": code,
            "name": str(code),
            "updates_high_low": high_low,
            "updates_open_close": open_close,
            "updates_volume": True,
            "raw": {},
        }
    return {"retrieved_at": "2026-09-17T20:00:00+00:00", "rules": rules}


def _row(clock: str, price: str, *, trade_id: str, codes: list[int] | None = None):
    return {
        "ev": "T",
        "sym": "TEST",
        "t": _ms(clock),
        "p": price,
        "s": 100,
        "c": [12] if codes is None else codes,
        "i": trade_id,
        "x": 11,
        "trfi": 501,
        "y": _ms(clock) - 1,
    }


def _payload(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "session_date": "2026-09-17",
        "prior_closes": {"TEST": "1.00"},
        "condition_snapshot": _conditions(),
        "trades": {"TEST": rows},
    }


@pytest.mark.parametrize(("price", "expected"), [("1.1999", False), ("1.20", True)])
def test_aggregate_candidate_threshold_is_decimal_exact(price: str, expected: bool) -> None:
    bars = [
        AggregateBar(_ms("04:11:00"), Decimal("1"), Decimal("1")),
        AggregateBar(_ms("04:11:25"), Decimal(price), Decimal(price)),
    ]
    assert aggregate_has_candidate(bars) is expected


def test_aggregate_candidate_keeps_an_intrasecond_raw_move_in_the_superset() -> None:
    bars = [AggregateBar(_ms("04:11:25"), Decimal("1.20"), Decimal("1.00"))]
    assert aggregate_has_candidate(bars) is True


def test_aggregate_window_includes_exact_boundary_and_excludes_one_second_older() -> None:
    exact = [
        AggregateBar(_ms("04:11:00"), Decimal("1"), Decimal("1")),
        AggregateBar(_ms("04:12:00"), Decimal("1.20"), Decimal("1.20")),
    ]
    too_old = [
        AggregateBar(_ms("04:11:00"), Decimal("1"), Decimal("1")),
        AggregateBar(_ms("04:12:01"), Decimal("1.20"), Decimal("1.20")),
    ]
    assert aggregate_has_candidate(exact) is True
    assert aggregate_has_candidate(too_old) is False


def test_prior_close_floor_disagreement_is_flagged_and_excluded() -> None:
    adjusted = [
        {"T": "KEEP", "c": "1.00", "h": "1.1", "l": "1"},
        {"T": "SPLT", "c": "1.20", "h": "1.3", "l": "1"},
    ]
    unadjusted = [
        {"T": "KEEP", "c": "1.00", "h": "1.1", "l": "1"},
        {"T": "SPLT", "c": "0.60", "h": "0.7", "l": "0.5"},
    ]
    universe, disagreements = prior_close_universe(adjusted, unadjusted)
    assert universe == {"KEEP": Decimal("1.00")}
    assert disagreements == ("SPLT:adjusted=1.20:unadjusted=0.60",)


def test_premarket_range_screen_is_a_necessary_but_order_agnostic_superset() -> None:
    assert (
        premarket_range_has_candidate(
            [
                AggregateBar(_ms("04:20:00"), Decimal("1.05"), Decimal("1.00")),
                AggregateBar(_ms("07:00:00"), Decimal("1.20"), Decimal("1.18")),
            ]
        )
        is True
    )


def test_capture_downloads_raw_trades_for_aggregate_candidates_only() -> None:
    class Client:
        def __init__(self) -> None:
            self.grouped_calls = 0
            self.trade_symbols: list[str] = []

        def get_grouped_daily_aggs(self, day, *, adjusted):
            del day, adjusted
            self.grouped_calls += 1
            return [
                {"T": "KEEP", "c": "1", "h": "1.1", "l": "1"},
                {"T": "DROP", "c": "1", "h": "1.1", "l": "1"},
            ]

        def list_aggs(self, symbol, _multiplier, timespan, *args, **kwargs):
            del args, kwargs
            high = "1.20" if symbol == "KEEP" else "1.19"
            if timespan == "minute":
                return [
                    {"t": _ms("04:11:00"), "h": "1", "l": "1"},
                    {"t": _ms("07:00:00"), "h": high, "l": high},
                ]
            return [
                {"t": _ms("04:11:00"), "h": "1", "l": "1"},
                {"t": _ms("04:11:25"), "h": high, "l": high},
            ]

        def list_trades(self, symbol, **kwargs):
            del kwargs
            self.trade_symbols.append(symbol)
            return []

    client = Client()
    payload = capture_session(
        client,
        datetime(2026, 9, 17).date(),
        condition_payload=_conditions(),
        captured_at=datetime(2026, 9, 17, 20, tzinfo=UTC),
    )
    assert payload["candidate_symbols"] == ["KEEP"]
    assert client.trade_symbols == ["KEEP"]
    assert client.grouped_calls == 2
    assert payload["api_calls"] == {
        "grouped_daily": 2,
        "minute_aggregates": 2,
        "second_aggregates": 1,
        "raw_trades": 1,
    }


def test_real_screen_shape_keeps_premarket_move_when_rth_daily_range_is_tight() -> None:
    class Client:
        def __init__(self) -> None:
            self.grouped_calls = 0

        def get_grouped_daily_aggs(self, day, *, adjusted):
            del day, adjusted
            self.grouped_calls += 1
            if self.grouped_calls <= 2:
                return [{"T": "MOVE", "c": "1", "h": "1.1", "l": "1"}]
            return [{"T": "MOVE", "c": "1.02", "h": "1.05", "l": "1.00"}]

        def list_aggs(self, symbol, _multiplier, timespan, *args, **kwargs):
            del symbol, args, kwargs
            if timespan == "minute":
                return [
                    {"t": _ms("04:11:00"), "h": "1", "l": "1"},
                    {"t": _ms("07:00:00"), "h": "1.20", "l": "1.20"},
                ]
            return [
                {"t": _ms("04:11:00"), "h": "1", "l": "1"},
                {"t": _ms("04:11:30"), "h": "1.20", "l": "1.20"},
            ]

    result = screen_control_session(
        Client(),
        datetime(2026, 9, 17).date(),
        now=datetime(2026, 9, 17, 20, tzinfo=UTC),
    )
    assert result["acceptance"] == "PASS"
    assert result["full_scan_symbols"] == ["MOVE"]
    assert result["premarket_screen_symbols"] == ["MOVE"]
    assert result["old_daily_screen_missing"] == ["MOVE"]


def test_capture_lists_window_splits_and_persists_them_in_each_session(tmp_path: Path) -> None:
    class Client:
        def list_conditions(self, **kwargs):
            del kwargs
            return [
                {
                    "id": 12,
                    "name": "Form T/Extended Hours",
                    "update_rules": {
                        "consolidated": {
                            "updates_high_low": False,
                            "updates_open_close": False,
                            "updates_volume": True,
                        }
                    },
                }
            ]

        def list_splits(self, **kwargs):
            assert kwargs["execution_date_gte"] == datetime(2026, 9, 17).date()
            assert kwargs["execution_date_lte"] == datetime(2026, 9, 17).date()
            return [
                {
                    "ticker": "SPLT",
                    "execution_date": "2026-09-17",
                    "split_from": 1,
                    "split_to": 10,
                    "id": "split-1",
                }
            ]

        def get_grouped_daily_aggs(self, day, *, adjusted):
            del day, adjusted
            return []

    paths = capture_sessions(
        Client(),
        [datetime(2026, 9, 17).date()],
        tmp_path,
        now=datetime(2026, 9, 17, 20, tzinfo=UTC),
    )
    payload = read_capture(paths[0])
    assert payload["split_events"] == [
        {
            "ticker": "SPLT",
            "execution_date": "2026-09-17",
            "split_from": "1",
            "split_to": "10",
            "id": "split-1",
        }
    ]


def test_raw_capture_preserves_the_production_wire_fields() -> None:
    row = raw_trade_row(
        {
            "sip_timestamp": 1_789_555_200_123_000_000,
            "participant_timestamp": 1_789_555_200_122_000_000,
            "price": 2.5,
            "size": 40,
            "conditions": [12, 14],
            "id": "wire-id",
            "exchange": 11,
            "trf_id": 501,
        },
        "ABCD",
    )
    assert row == {
        "ev": "T",
        "sym": "ABCD",
        "t": 1_789_555_200_123,
        "p": "2.5",
        "s": 40,
        "c": [12, 14],
        "i": "wire-id",
        "x": 11,
        "trfi": 501,
        "y": 1_789_555_200_122,
    }


def test_capture_is_forbidden_during_market_hours() -> None:
    eastern = ZoneInfo("America/New_York")
    with pytest.raises(RuntimeError, match="forbidden before 16:00 ET"):
        require_capture_window(datetime(2026, 9, 17, 15, 59, tzinfo=eastern))
    require_capture_window(datetime(2026, 9, 17, 16, 0, tzinfo=eastern))


def test_replay_uses_production_rules_and_allows_fresh_post_exit_reentry() -> None:
    rows = [
        _row("04:11:00.000", "1.00", trade_id="r1"),
        _row("04:11:25.000", "1.20", trade_id="d1"),
        _row("04:11:25.001", "1.21", trade_id="f1"),
        _row("04:11:26.000", "1.28", trade_id="t1"),
        _row("04:11:27.000", "1.27", trade_id="t2"),
        _row("04:11:28.000", "1.00", trade_id="r2"),
        _row("04:11:50.000", "1.20", trade_id="d2"),
        _row("04:11:50.001", "1.21", trade_id="f2"),
        _row("04:11:51.000", "1.02", trade_id="s1"),
    ]
    result = replay_capture(_payload(rows))
    by_strategy = {row.strategy_code: row for row in result.strategy_results}
    assert by_strategy["momentum_30s"].detections == 2
    assert by_strategy["momentum_30s"].fills == 2
    assert by_strategy["momentum_30s"].targets == 1
    assert by_strategy["momentum_30s"].stops == 1
    assert by_strategy["momentum_30s"].path_rows == by_strategy["momentum_30s"].union_prints


def test_path_union_starts_at_the_detecting_print_within_one_millisecond() -> None:
    rows = [
        _row("04:11:00.000", "1.00", trade_id="reference"),
        _row("04:11:25.000", "1.19", trade_id="same-ms-before"),
        _row("04:11:25.000", "1.20", trade_id="detection"),
        _row("04:11:25.001", "1.21", trade_id="fill"),
        _row("04:11:26.000", "1.28", trade_id="target"),
    ]
    result = replay_capture(_payload(rows))
    for strategy in result.strategy_results:
        assert strategy.timestamp_only_union_prints == strategy.path_rows + 1
        assert strategy.same_millisecond_before_detection_prints == 1
        assert strategy.union_prints == strategy.path_rows


def test_path_control_requires_exact_equality_and_classifies_the_old_extra() -> None:
    result = replay_capture(
        _payload(
            [
                _row("04:11:00.000", "1.00", trade_id="reference"),
                _row("04:11:25.000", "1.19", trade_id="same-ms-before"),
                _row("04:11:25.000", "1.20", trade_id="detection"),
                _row("04:11:25.001", "1.21", trade_id="fill"),
            ]
        )
    )
    control = path_control_payload(
        [
            replace(result, session_date=(date(2026, 8, 1) + timedelta(days=index)).isoformat())
            for index in range(30)
        ]
    )
    assert control["acceptance"] == "PASS"
    assert control["prior_expectation"] == {
        "expected_zero_sessions": 29,
        "expected_nonzero_session": "2026-08-05",
        "expected_nonzero_prints": 12,
        "matched": False,
        "observed_zero_sessions": 0,
        "observed_nonzero_sessions": 30,
        "observed_nonzero_prints": 60,
    }
    assert all(
        row["old_control_discrepancy"] == row["same_millisecond_before_detection_prints"] == 1
        for row in control["rows"]
    )
    assert all(row["order_aware_exact"] is True for row in control["rows"])


def test_excluded_print_is_replayed_but_cannot_make_the_reference() -> None:
    rows = [
        _row("04:11:00.000", "0.50", trade_id="bad", codes=[12, 37]),
        _row("04:11:01.000", "1.00", trade_id="r1"),
        _row("04:11:25.000", "1.10", trade_id="too-small"),
    ]
    result = replay_capture(_payload(rows))
    assert result.excluded_prints == 1
    assert all(row.detections == 0 for row in result.strategy_results)


def test_suspect_rate_is_frozen_at_more_than_ten_per_bot_session() -> None:
    assert DETECTOR_SUSPECT_DETECTIONS == 10
    assert BAND_REJECT_SUSPECT_SESSIONS == 3
    assert detector_is_suspect(10) is False
    assert detector_is_suspect(11) is True
    quiet = replay_capture(_payload([]))
    report = report_payload([quiet] * 30)
    assert report["criterion"] == {
        "detector_suspect_when": "detections > 10 for either bot in one session",
        "threshold": 10,
        "reject_band_when_suspect_sessions_gt": 3,
        "replacement": "nearest-rank P95, minimum 10",
        "frozen_before_capture": True,
        "seen_session_excluded": "2026-09-17",
    }


def test_band_is_rejected_only_after_more_than_three_suspect_sessions() -> None:
    accepted = detector_band_decision([11, 11, 11, *([0] * 27)])
    rejected = detector_band_decision([11, 11, 11, 11, *([0] * 26)])
    assert accepted["verdict"] == "ACCEPT_BAND"
    assert rejected == {
        "verdict": "REJECT_BAND",
        "suspect_sessions": 4,
        "sessions": 30,
        "replacement_nearest_rank_p95": 11,
    }


def test_replay_refuses_an_incomplete_thirty_session_capture(tmp_path: Path) -> None:
    (tmp_path / "momentum-live-rule-2026-09-17.json.gz").touch()
    with pytest.raises(RuntimeError, match="expected 30.*found 1"):
        replay_directory(tmp_path, expected_sessions=30)
