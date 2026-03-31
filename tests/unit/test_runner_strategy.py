from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from project_mai_tai.services.strategy_engine_app import StrategyEngineState
from project_mai_tai.strategy_core.runner import RunnerConfig, RunnerPosition


def fixed_now() -> datetime:
    return datetime(2026, 3, 28, 10, 0)


def seed_runner_bars(
    *,
    start_price: float = 2.0,
    count: int = 12,
    start_timestamp: float = 1_700_000_000.0,
) -> list[dict[str, float | int]]:
    bars = []
    for index in range(count):
        close = start_price + index * 0.04
        bars.append(
            {
                "open": close - 0.02,
                "high": close + 0.03,
                "low": close - 0.03,
                "close": close,
                "volume": 40_000 + index * 2_000,
                "timestamp": start_timestamp + index * 60,
            }
        )
    return bars


def test_runner_generates_open_intent_for_eligible_candidate() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    runner = state.bots["runner"]
    candidate = {
        "ticker": "UGRO",
        "rank_score": 78.0,
        "change_pct": 40.0,
        "prev_close": 2.0,
        "confirmed_at": "09:45:00 AM ET",
        "bid": 2.79,
        "ask": 2.80,
        "confirmation_path": "PATH_B_2SQ",
    }

    runner.set_watchlist(["UGRO"])
    runner.update_candidates([candidate])
    state.seed_bars("runner", "UGRO", seed_runner_bars())

    intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.80,
        size=500,
        timestamp_ns=1_700_001_800_000_000_000,
    )

    assert intents
    assert intents[0].payload.strategy_code == "runner"
    assert intents[0].payload.intent_type == "open"
    assert intents[0].payload.reason == "ENTRY_RUNNER_MOMENTUM"
    assert intents[0].payload.metadata["rank_score"] == "78.0"


def test_runner_trailing_stop_emits_close_intent() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    runner = state.bots["runner"]
    runner.apply_execution_fill(
        symbol="UGRO",
        intent_type="open",
        status="filled",
        side="buy",
        quantity=Decimal("100"),
        price=Decimal("2.00"),
        path=None,
    )

    no_close = state.handle_trade_tick(
        symbol="UGRO",
        price=3.00,
        size=500,
        timestamp_ns=1_700_001_900_000_000_000,
    )
    close_intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.65,
        size=500,
        timestamp_ns=1_700_001_905_000_000_000,
    )

    assert no_close == []
    assert close_intents
    assert close_intents[0].payload.strategy_code == "runner"
    assert close_intents[0].payload.intent_type == "close"
    assert close_intents[0].payload.reason == "TRAIL_STOP_10%"


def test_runner_requires_same_min_change_without_news_discount() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    runner = state.bots["runner"]
    candidate = {
        "ticker": "UGRO",
        "rank_score": 78.0,
        "change_pct": 25.0,
        "prev_close": 2.0,
        "confirmed_at": "09:45:00 AM ET",
        "bid": 2.49,
        "ask": 2.50,
        "confirmation_path": "PATH_A_NEWS",
    }

    runner.set_watchlist(["UGRO"])
    runner.update_candidates([candidate])
    state.seed_bars("runner", "UGRO", seed_runner_bars())

    intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.50,
        size=500,
        timestamp_ns=1_700_001_800_000_000_000,
    )

    assert intents == []


def test_runner_trail_pct_stays_flat_at_ten_percent() -> None:
    position = RunnerPosition("UGRO", entry_price=2.0, quantity=100)
    config = RunnerConfig()

    position.peak_profit_pct = 120.0
    position.volume_faded = True

    assert position.get_trail_pct(config) == 10.0


def test_runner_rolls_daily_pnl_and_closed_trades_at_new_et_day(monkeypatch) -> None:
    active_day = {"value": "2026-03-30"}
    monkeypatch.setattr(
        "project_mai_tai.strategy_core.runner.today_eastern_str",
        lambda: active_day["value"],
    )

    state = StrategyEngineState(now_provider=fixed_now)
    runner = state.bots["runner"]
    runner._daily_pnl = 25.0
    runner._closed_today = [{"ticker": "MASK"}]

    active_day["value"] = "2026-03-31"

    summary = runner.summary()

    assert summary["daily_pnl"] == 0.0
    assert summary["closed_today"] == []
