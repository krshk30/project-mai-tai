from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.backtest.momentum_live_rule_baseline import (
    AggregateBar,
    DETECTOR_SUSPECT_DETECTIONS,
    aggregate_has_candidate,
    broad_candidates,
    capture_session,
    detector_is_suspect,
    raw_trade_row,
    replay_capture,
    replay_directory,
    report_payload,
    require_capture_window,
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


def test_broad_filter_is_a_safe_superset_with_prior_close_floor() -> None:
    prior = [
        {"T": "KEEP", "c": "1.00", "h": "1.1", "l": "1"},
        {"T": "CHEAP", "c": "0.99", "h": "1", "l": "0.9"},
    ]
    current = [
        {"T": "KEEP", "c": "1.1", "h": "1.20", "l": "1.00"},
        {"T": "CHEAP", "c": "1.2", "h": "1.30", "l": "1.00"},
    ]
    assert broad_candidates(prior, current) == {"KEEP": Decimal("1.00")}


def test_capture_downloads_raw_trades_for_aggregate_candidates_only() -> None:
    class Client:
        def __init__(self) -> None:
            self.grouped_calls = 0
            self.trade_symbols: list[str] = []

        def get_grouped_daily_aggs(self, day, *, adjusted):
            del day, adjusted
            self.grouped_calls += 1
            if self.grouped_calls == 1:
                return [
                    {"T": "KEEP", "c": "1", "h": "1.1", "l": "1"},
                    {"T": "DROP", "c": "1", "h": "1.1", "l": "1"},
                ]
            return [
                {"T": "KEEP", "c": "1.1", "h": "1.3", "l": "1"},
                {"T": "DROP", "c": "1.1", "h": "1.3", "l": "1"},
            ]

        def list_aggs(self, symbol, *args, **kwargs):
            del args, kwargs
            high = "1.20" if symbol == "KEEP" else "1.19"
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
    assert detector_is_suspect(10) is False
    assert detector_is_suspect(11) is True
    quiet = replay_capture(_payload([]))
    report = report_payload([quiet])
    assert report["criterion"] == {
        "detector_suspect_when": "detections > 10 for either bot in one session",
        "threshold": 10,
        "frozen_before_capture": True,
    }


def test_replay_refuses_an_incomplete_thirty_session_capture(tmp_path: Path) -> None:
    (tmp_path / "momentum-live-rule-2026-09-17.json.gz").touch()
    with pytest.raises(RuntimeError, match="expected 30.*found 1"):
        replay_directory(tmp_path, expected_sessions=30)
