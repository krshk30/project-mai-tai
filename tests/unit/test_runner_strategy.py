from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from project_mai_tai.services.strategy_engine_app import StrategyEngineState


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
