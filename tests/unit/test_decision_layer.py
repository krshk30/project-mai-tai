from __future__ import annotations

from datetime import datetime

from project_mai_tai.strategy_core.entry import EntryEngine
from project_mai_tai.strategy_core.exit import ExitEngine
from project_mai_tai.strategy_core.position_tracker import PositionTracker
from project_mai_tai.strategy_core.trading_config import TradingConfig


def base_indicators(**overrides):
    data = {
        "price": 2.25,
        "price_prev": 2.2,
        "high": 2.3,
        "low": 2.18,
        "volume": 20_000,
        "macd": 0.02,
        "macd_prev": 0.01,
        "signal": 0.015,
        "signal_prev": 0.014,
        "histogram": 0.005,
        "histogram_prev": 0.004,
        "stoch_k": 60.0,
        "stoch_k_prev": 55.0,
        "ema9": 2.2,
        "ema20": 2.1,
        "vwap": 2.18,
        "macd_above_signal": True,
        "macd_cross_above": True,
        "macd_cross_below": False,
        "macd_increasing": True,
        "macd_delta": 0.01,
        "macd_delta_prev": 0.005,
        "macd_delta_accelerating": True,
        "histogram_growing": True,
        "stoch_k_rising": True,
        "stoch_k_below_exit": False,
        "stoch_k_falling": False,
        "price_above_vwap": True,
        "price_above_ema9": True,
        "price_above_ema20": True,
        "price_above_both_emas": True,
        "price_cross_above_vwap": False,
        "macd_was_below_3bars": True,
    }
    data.update(overrides)
    return data


def test_entry_engine_confirms_buy_signal() -> None:
    config = TradingConfig(confirm_bars=1, min_score=4)
    engine = EntryEngine(
        config,
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )
    tracker = PositionTracker(config)

    assert engine.check_entry("UGRO", base_indicators(), 10, tracker) is None

    signal = engine.check_entry(
        "UGRO",
        base_indicators(macd_cross_above=False, price=2.3, price_prev=2.25),
        11,
        tracker,
    )
    assert signal is not None
    assert signal["action"] == "BUY"
    assert signal["path"] == "P1_MACD_CROSS"
    assert signal["score"] >= 4


def test_entry_engine_p3_requires_stricter_score() -> None:
    config = TradingConfig(confirm_bars=1, min_score=4, surge_rate=0.001)
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )

    p3_trigger = base_indicators(
        macd_cross_above=False,
        price_cross_above_vwap=False,
        histogram=0.02,
        macd_delta=0.01,
        macd_delta_accelerating=True,
        volume=7_000,
    )
    assert engine.check_entry("UGRO", p3_trigger, 20) is None

    weak_confirmation = base_indicators(
        macd_cross_above=False,
        price_cross_above_vwap=False,
        histogram=0.02,
        macd_delta=0.01,
        macd_delta_accelerating=True,
        volume=4_000,
        histogram_growing=False,
        stoch_k_rising=False,
    )
    assert engine.check_entry("UGRO", weak_confirmation, 21) is None


def test_position_tracker_scale_and_close_includes_scale_pnl(tmp_path) -> None:
    config = TradingConfig(default_quantity=100)
    tracker = PositionTracker(config, history_dir=str(tmp_path))
    position = tracker.open_position("UGRO", entry_price=2.0, path="P1_MACD_CROSS")

    position.update_price(2.08)
    scale = position.get_scale_action(config)
    assert scale is not None
    assert scale["level"] == "FAST4"

    position.apply_scale(scale["level"], scale["sell_qty"], exit_price=2.08)
    close = tracker.close_position("UGRO", exit_price=2.04, reason="MANUAL")
    assert close is not None
    assert close["scale_pnl"] > 0
    assert close["pnl"] > 0


def test_exit_engine_floor_and_hard_stop(tmp_path) -> None:
    config = TradingConfig(stop_loss_pct=1.5)
    tracker = PositionTracker(config, history_dir=str(tmp_path))
    position = tracker.open_position("UGRO", entry_price=2.0)

    position.update_price(2.08)
    position.update_price(2.02)
    engine = ExitEngine(config)

    floor_exit = engine.check_exit(position, base_indicators(macd_cross_below=False))
    assert floor_exit is not None
    assert floor_exit["reason"] == "FLOOR_BREACH"

    position = tracker.open_position("ANNA", entry_price=2.0)
    position.update_price(1.96)
    hard_stop = engine.check_hard_stop(position, 1.96)
    assert hard_stop is not None
    assert hard_stop["reason"] == "HARD_STOP"
