from __future__ import annotations

from datetime import UTC, datetime

from project_mai_tai.services.strategy_engine_app import StrategyEngineState
from project_mai_tai.settings import Settings


def seed_trending_bars(
    start_price: float = 2.0,
    count: int = 50,
    *,
    start_timestamp: float = 1_700_000_000.0,
    interval_secs: int = 30,
) -> list[dict[str, float | int]]:
    bars = []
    for index in range(count):
        close = start_price + index * 0.01
        bars.append(
            {
                "open": close - 0.01,
                "high": close + 0.02,
                "low": close - 0.02,
                "close": close,
                "volume": 20_000 + index * 50,
                "timestamp": start_timestamp + index * interval_secs,
            }
        )
    return bars


def test_flush_completed_bars_records_last_tick_at_for_due_symbol(monkeypatch) -> None:
    current = datetime(2026, 4, 2, 7, 0, tzinfo=UTC)

    def now_provider() -> datetime:
        return current

    state = StrategyEngineState(
        settings=Settings(
            strategy_polygon_30s_enabled=True,
            strategy_polygon_30s_live_aggregate_bars_enabled=False,
            strategy_polygon_30s_force_tick_built_mode=True,
            strategy_polygon_30s_tick_bar_close_grace_seconds=0.0,
        ),
        now_provider=now_provider,
    )
    bot = state.bots["polygon_30s"]
    bot.set_watchlist(["UGRO"])
    state.seed_bars(
        "polygon_30s",
        "UGRO",
        seed_trending_bars(start_timestamp=current.timestamp() - 50 * 30, interval_secs=30),
    )
    bot.latest_quotes["UGRO"] = {"bid": 2.79, "ask": 2.80}
    bot.definition.trading_config.confirm_bars = 0
    bot.definition.trading_config.min_score = 0
    bot.definition.trading_config.entry_intrabar_enabled = False

    monkeypatch.setattr(
        bot.entry_engine,
        "check_entry",
        lambda ticker, indicators, bar_index, position_tracker: {
            "ticker": ticker,
            "price": float(indicators["price"]),
            "path": "P1_MACD_CROSS",
            "score": 5,
            "score_details": "test",
        },
    )

    tick_timestamp_ns = int(current.timestamp() * 1_000_000_000)
    initial_intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.80,
        size=200,
        timestamp_ns=tick_timestamp_ns,
        strategy_codes=["polygon_30s"],
    )
    assert initial_intents == []

    current = datetime(2026, 4, 2, 7, 0, 31, tzinfo=UTC)
    flushed_intents, completed_count = state.flush_completed_bars()

    assert completed_count >= 1
    open_intents = [intent for intent in flushed_intents if intent.payload.intent_type == "open"]
    assert len(open_intents) == 1
    assert open_intents[0].payload.symbol == "UGRO"
    assert "UGRO" in bot.summary()["last_tick_at"]
