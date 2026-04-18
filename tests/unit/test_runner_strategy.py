from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from project_mai_tai.services.strategy_engine_app import StrategyEngineState
from project_mai_tai.strategy_core.runner import RunnerConfig, RunnerPosition, order_routing_metadata


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
        client_order_id="runner-UGRO-open-1",
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


def test_runner_apply_execution_fill_uses_incremental_quantity_for_cumulative_reports() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    runner = state.bots["runner"]

    runner.apply_execution_fill(
        client_order_id="runner-MASK-open-1",
        symbol="MASK",
        intent_type="open",
        status="partially_filled",
        side="buy",
        quantity=Decimal("19"),
        price=Decimal("1.82"),
    )
    runner.apply_execution_fill(
        client_order_id="runner-MASK-open-1",
        symbol="MASK",
        intent_type="open",
        status="filled",
        side="buy",
        quantity=Decimal("100"),
        price=Decimal("1.82"),
    )

    summary = runner.summary()
    assert len(summary["positions"]) == 1
    assert summary["positions"][0]["quantity"] == 100


def test_runner_clears_ghost_position_on_no_strategy_position_reject() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    runner = state.bots["runner"]
    runner.apply_execution_fill(
        client_order_id="runner-MASK-open-1",
        symbol="MASK",
        intent_type="open",
        status="filled",
        side="buy",
        quantity=Decimal("100"),
        price=Decimal("1.82"),
    )
    runner._pending_close_symbols.add("MASK")

    runner.apply_order_status(
        symbol="MASK",
        intent_type="close",
        status="rejected",
        reason="no strategy position available to sell",
    )

    assert runner.summary()["positions"] == []


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


def test_runner_rolls_daily_pnl_and_closed_trades_at_new_session_after_eight_pm_et(monkeypatch) -> None:
    active_day = {"value": "2026-03-30"}
    monkeypatch.setattr(
        "project_mai_tai.strategy_core.runner.session_day_eastern_str",
        lambda *_args, **_kwargs: active_day["value"],
    )

    state = StrategyEngineState(now_provider=fixed_now)
    runner = state.bots["runner"]
    runner._daily_pnl = 25.0
    runner._closed_today = [{"ticker": "MASK"}]
    runner._entered_today = {"MASK"}

    active_day["value"] = "2026-03-31"

    summary = runner.summary()

    assert summary["daily_pnl"] == 0.0
    assert summary["closed_today"] == []
    assert summary["entered_today"] == []


def test_runner_does_not_reenter_same_symbol_after_first_filled_trade() -> None:
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

    first_intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.80,
        size=500,
        timestamp_ns=1_700_001_800_000_000_000,
    )

    assert first_intents
    runner.apply_execution_fill(
        client_order_id="runner-UGRO-open-1",
        symbol="UGRO",
        intent_type="open",
        status="filled",
        side="buy",
        quantity=Decimal("100"),
        price=Decimal("2.80"),
    )
    runner.apply_execution_fill(
        client_order_id="runner-UGRO-close-1",
        symbol="UGRO",
        intent_type="close",
        status="filled",
        side="sell",
        quantity=Decimal("100"),
        price=Decimal("2.70"),
    )

    second_intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.85,
        size=500,
        timestamp_ns=1_700_001_860_000_000_000,
    )

    assert second_intents == []
    assert runner.summary()["entered_today"] == ["UGRO"]


def test_runner_can_hold_multiple_symbols_at_once() -> None:
    state = StrategyEngineState(now_provider=fixed_now)
    runner = state.bots["runner"]
    ugro = {
        "ticker": "UGRO",
        "rank_score": 78.0,
        "change_pct": 40.0,
        "prev_close": 2.0,
        "confirmed_at": "09:45:00 AM ET",
        "bid": 2.79,
        "ask": 2.80,
        "confirmation_path": "PATH_B_2SQ",
    }
    mask = {
        "ticker": "MASK",
        "rank_score": 82.0,
        "change_pct": 36.0,
        "prev_close": 1.5,
        "confirmed_at": "09:46:00 AM ET",
        "bid": 2.09,
        "ask": 2.10,
        "confirmation_path": "PATH_B_2SQ",
    }

    runner.set_watchlist(["UGRO", "MASK"])
    runner.update_candidates([ugro, mask])
    state.seed_bars("runner", "UGRO", seed_runner_bars(start_price=2.0))
    state.seed_bars("runner", "MASK", seed_runner_bars(start_price=1.6))

    ugro_intents = state.handle_trade_tick(
        symbol="UGRO",
        price=2.80,
        size=500,
        timestamp_ns=1_700_001_800_000_000_000,
    )
    mask_intents = state.handle_trade_tick(
        symbol="MASK",
        price=2.10,
        size=500,
        timestamp_ns=1_700_001_860_000_000_000,
    )

    assert ugro_intents
    assert mask_intents
    runner.apply_execution_fill(
        client_order_id="runner-UGRO-open-1",
        symbol="UGRO",
        intent_type="open",
        status="filled",
        side="buy",
        quantity=Decimal("100"),
        price=Decimal("2.80"),
    )
    runner.apply_execution_fill(
        client_order_id="runner-MASK-open-1",
        symbol="MASK",
        intent_type="open",
        status="filled",
        side="buy",
        quantity=Decimal("100"),
        price=Decimal("2.10"),
    )

    summary = runner.summary()
    tickers = sorted(position["ticker"] for position in summary["positions"])

    assert tickers == ["MASK", "UGRO"]


def test_runner_order_routing_metadata_uses_extended_hours_limit_in_premarket() -> None:
    metadata = order_routing_metadata(
        price="2.80",
        side="buy",
        now=datetime(2026, 3, 31, 11, 0, tzinfo=UTC),
    )

    assert metadata["order_type"] == "limit"
    assert metadata["extended_hours"] == "true"
    assert metadata["limit_price"] == "2.80"
    assert metadata["price_source"] == "ask"


def test_runner_uses_quote_anchored_limit_prices_in_extended_hours() -> None:
    state = StrategyEngineState(now_provider=lambda: datetime(2026, 3, 31, 11, 0, tzinfo=UTC))
    runner = state.bots["runner"]
    runner.update_market_snapshots(
        [
            type(
                "Snapshot",
                (),
                {
                    "ticker": "UGRO",
                    "last_quote": type("Quote", (), {"bid_price": 2.79, "ask_price": 2.80})(),
                },
            )()
        ]
    )
    runner._positions["UGRO"] = RunnerPosition("UGRO", entry_price=2.0, quantity=100)

    open_intent = runner._emit_open_intent({"ticker": "UGRO", "rank_score": 78.0, "change_pct": 40.0}, 2.75)
    close_intent = runner._emit_close_intent(symbol="UGRO", reason="TEST")

    assert open_intent.payload.metadata["limit_price"] == "2.80"
    assert open_intent.payload.metadata["price_source"] == "ask"
    assert close_intent.payload.metadata["limit_price"] == "2.79"
    assert close_intent.payload.metadata["price_source"] == "bid"


def test_runner_blocks_close_retries_after_duplicate_exit_reject() -> None:
    state = StrategyEngineState(now_provider=lambda: datetime(2026, 3, 31, 14, 0, tzinfo=UTC))
    runner = state.bots["runner"]
    runner._positions["UGRO"] = RunnerPosition("UGRO", entry_price=2.0, quantity=100)
    runner._pending_close_symbols.add("UGRO")

    runner.apply_order_status(
        symbol="UGRO",
        intent_type="close",
        status="rejected",
        reason="duplicate_exit_in_flight",
    )

    assert "UGRO" not in runner._pending_close_symbols
    assert runner._is_close_retry_blocked("UGRO") is True
