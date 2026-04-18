from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from project_mai_tai.strategy_core.entry import EntryEngine
from project_mai_tai.strategy_core.exit import ExitEngine
from project_mai_tai.strategy_core.position_tracker import PositionTracker
from project_mai_tai.strategy_core.trading_config import TradingConfig


def base_indicators(**overrides):
    data = {
        "open": 2.19,
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
        "stoch_k_prev2": 50.0,
        "stoch_d": 57.0,
        "stoch_d_prev": 54.0,
        "ema9": 2.2,
        "ema20": 2.1,
        "vwap": 2.18,
        "extended_vwap": 2.18,
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
        "price_above_extended_vwap": True,
        "price_above_ema9": True,
        "price_above_ema20": True,
        "price_above_both_emas": True,
        "price_cross_above_vwap": False,
        "price_cross_above_extended_vwap": False,
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


def test_entry_engine_blocks_overbought_stoch_entry_cap() -> None:
    config = TradingConfig(confirm_bars=0, min_score=0).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )

    blocked = engine.check_entry("UGRO", base_indicators(stoch_k=90.0), 10)

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "blocked"
    assert "stochK" in decision["reason"]


def test_entry_engine_30s_blocks_price_eight_percent_or_more_above_ema9() -> None:
    config = TradingConfig(confirm_bars=0, min_score=0).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(price=2.377, ema9=2.20),
        10,
    )

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "blocked"
    assert "above EMA9" in decision["reason"]


def test_entry_engine_30s_allows_price_under_ema9_eight_percent_cap() -> None:
    config = TradingConfig(confirm_bars=0, min_score=0).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )

    signal = engine.check_entry(
        "UGRO",
        base_indicators(price=2.375, ema9=2.20),
        10,
    )

    assert signal is not None
    assert signal["path"] == "P1_MACD_CROSS"


def test_entry_engine_p3_uses_trigger_bar_score_for_confirmation() -> None:
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
    signal = engine.check_entry("UGRO", weak_confirmation, 21)

    assert signal is not None
    assert signal["path"] == "P3_MACD_SURGE"
    assert signal["score"] == 5


def test_entry_engine_p3_no_longer_requires_trigger_volume_gate() -> None:
    config = TradingConfig(confirm_bars=0, min_score=0, surge_rate=0.001).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            macd_cross_above=False,
            price_cross_above_vwap=False,
            histogram=0.02,
            macd_delta=0.01,
            macd_delta_accelerating=True,
            volume=100,
        ),
        20,
    )

    assert signal is not None
    assert signal["path"] == "P3_MACD_SURGE"


def test_entry_engine_30s_p3_uses_tv_histogram_floor() -> None:
    config = TradingConfig(confirm_bars=0, min_score=0, surge_rate=0.001).make_30s_variant()
    engine = EntryEngine(
        config,
        name="Some Other Display Name",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(
            macd_cross_above=False,
            price_cross_above_vwap=False,
            histogram=0.009,
            macd_delta=0.01,
            macd_delta_accelerating=True,
        ),
        20,
    )
    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "idle"

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            macd_cross_above=False,
            price_cross_above_vwap=False,
            histogram=0.01,
            macd_delta=0.01,
            macd_delta_accelerating=True,
        ),
        21,
    )

    assert signal is not None
    assert signal["path"] == "P3_MACD_SURGE"


def test_entry_engine_30s_no_longer_blocks_ema9_stretch_preconditions() -> None:
    config = TradingConfig(confirm_bars=0, min_score=0).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )
    signal = engine.check_entry(
        "UGRO",
        base_indicators(price=2.26, ema9=2.22, vwap=2.22),
        13,
    )

    assert signal is not None
    assert signal["path"] == "P1_MACD_CROSS"


def test_entry_engine_30s_still_enters_without_vwap_preconditions() -> None:
    config = TradingConfig(confirm_bars=1, min_score=4).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )

    calm_bar = base_indicators(price=2.20, ema9=2.20, vwap=2.20, volume=20_000, macd_cross_above=False)
    extended_vwap_bar = base_indicators(price=2.26, ema9=2.245, vwap=2.20, volume=20_000, macd_cross_above=False)

    assert engine.check_entry("UGRO", calm_bar, 10) is None
    assert engine.check_entry("UGRO", calm_bar, 11) is None
    assert engine.check_entry("UGRO", extended_vwap_bar, 12) is None

    pending = engine.check_entry(
        "UGRO",
        base_indicators(price=2.27, ema9=2.255, vwap=2.21),
        13,
    )

    assert pending is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "pending"


def test_entry_engine_30s_confirmation_no_longer_blocks_small_vwap_chase() -> None:
    config = TradingConfig(confirm_bars=1, min_score=4).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )
    tracker = PositionTracker(config)

    assert engine.check_entry("UGRO", base_indicators(price=2.20, ema9=2.20, vwap=2.20), 10, tracker) is None

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            macd_cross_above=False,
            price=2.28,
            price_prev=2.20,
            ema9=2.26,
            vwap=2.22,
        ),
        11,
        tracker,
    )

    assert signal is not None
    assert signal["path"] == "P1_MACD_CROSS"


def test_entry_engine_30s_confirmation_no_longer_blocks_extreme_vwap_chase() -> None:
    config = TradingConfig(confirm_bars=1, min_score=4).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )
    tracker = PositionTracker(config)

    assert engine.check_entry("UGRO", base_indicators(price=2.20, ema9=2.20, vwap=2.20), 10, tracker) is None

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            macd_cross_above=False,
            price=2.80,
            price_prev=2.20,
            ema9=2.76,
            vwap=2.22,
        ),
        11,
        tracker,
    )

    assert signal is not None
    assert signal["path"] == "P1_MACD_CROSS"


def test_entry_engine_30s_confirmation_uses_trigger_bar_stoch_cap() -> None:
    config = TradingConfig(confirm_bars=1, min_score=4).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )
    tracker = PositionTracker(config)

    trigger = base_indicators(
        macd_cross_above=False,
        price_cross_above_vwap=False,
        histogram=0.02,
        macd_delta=0.01,
        macd_delta_accelerating=True,
        stoch_k=80.0,
        price=2.30,
        ema9=2.22,
    )
    assert engine.check_entry("UGRO", trigger, 10, tracker) is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "pending"

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            macd_cross_above=False,
            price_cross_above_vwap=False,
            histogram=0.021,
            macd_delta=0.011,
            macd_delta_accelerating=True,
            stoch_k=95.0,
            price=2.32,
            ema9=2.23,
        ),
        11,
        tracker,
    )

    assert signal is not None
    assert signal["path"] == "P3_MACD_SURGE"


def test_entry_engine_30s_confirmation_uses_trigger_bar_ema9_cap() -> None:
    config = TradingConfig(confirm_bars=1, min_score=4).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )
    tracker = PositionTracker(config)

    trigger = base_indicators(
        macd_cross_above=False,
        price_cross_above_vwap=False,
        histogram=0.02,
        macd_delta=0.01,
        macd_delta_accelerating=True,
        price=2.37,
        ema9=2.20,
    )
    assert engine.check_entry("UGRO", trigger, 10, tracker) is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "pending"

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            macd_cross_above=False,
            price_cross_above_vwap=False,
            histogram=0.021,
            macd_delta=0.011,
            macd_delta_accelerating=True,
            price=2.39,
            ema9=2.20,
        ),
        11,
        tracker,
    )

    assert signal is not None
    assert signal["path"] == "P3_MACD_SURGE"


def test_entry_engine_30s_confirmation_allows_pullback_above_breakout_level() -> None:
    config = TradingConfig(confirm_bars=1, min_score=4).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )
    tracker = PositionTracker(config)

    trigger = base_indicators(
        price=2.30,
        ema9=2.20,
        vwap=2.18,
        macd_cross_above=True,
        price_cross_above_vwap=False,
    )
    assert engine.check_entry("UGRO", trigger, 10, tracker) is None

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            macd_cross_above=False,
            price=2.24,
            price_prev=2.30,
            ema9=2.21,
            vwap=2.18,
            low=2.20,
        ),
        11,
        tracker,
    )

    assert signal is not None
    assert signal["path"] == "P1_MACD_CROSS"


def test_entry_engine_30s_no_longer_uses_hard_vwap_block() -> None:
    config = TradingConfig(confirm_bars=0, min_score=0).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )
    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            price=2.81,
            price_prev=2.20,
            ema9=2.77,
            vwap=2.22,
        ),
        11,
    )

    assert signal is not None
    assert signal["path"] == "P1_MACD_CROSS"


def test_entry_engine_1m_variant_is_unaffected_by_30s_preconditions() -> None:
    config = TradingConfig(confirm_bars=1, min_score=4).make_1m_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot 1M",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )
    tracker = PositionTracker(config)

    assert engine.check_entry(
        "UGRO",
        base_indicators(price=2.20, ema9=2.20, vwap=2.20),
        10,
        tracker,
    ) is None

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            macd_cross_above=False,
            price=2.28,
            price_prev=2.20,
            ema9=2.22,
            vwap=2.22,
        ),
        11,
        tracker,
    )

    assert signal is not None
    assert signal["path"] == "P1_MACD_CROSS"


def test_entry_engine_tos_variant_matches_script_macd_cross_entry() -> None:
    config = TradingConfig().make_tos_variant()
    engine = EntryEngine(
        config,
        name="TOS Bot",
        now_provider=lambda: datetime(2026, 4, 2, 13, 0),
    )

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            volume=6_000,
            macd_cross_above=True,
            macd_increasing=True,
            price_above_vwap=True,
            price_cross_above_vwap=False,
            stoch_k=95.0,
        ),
        10,
    )

    assert signal is not None
    assert signal["path"] == "P1_MACD_CROSS"


def test_entry_engine_tos_variant_matches_script_vwap_breakout_entry() -> None:
    config = TradingConfig().make_tos_variant()
    engine = EntryEngine(
        config,
        name="TOS Bot",
        now_provider=lambda: datetime(2026, 4, 2, 13, 0),
    )

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            volume=6_000,
            macd_cross_above=False,
            macd_above_signal=True,
            macd_increasing=True,
            price_above_vwap=True,
            price_cross_above_vwap=True,
        ),
        10,
    )

    assert signal is not None
    assert signal["path"] == "P2_VWAP_BREAKOUT"


def test_entry_engine_tos_variant_requires_vwap_filter_and_has_no_p3() -> None:
    config = TradingConfig().make_tos_variant()
    engine = EntryEngine(
        config,
        name="TOS Bot",
        now_provider=lambda: datetime(2026, 4, 2, 13, 0),
    )

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(
            volume=6_000,
            macd_cross_above=True,
            macd_increasing=True,
            price_above_vwap=False,
            price_cross_above_vwap=False,
        ),
        10,
    )

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "idle"

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(
            volume=6_000,
            macd_cross_above=False,
            macd_above_signal=True,
            macd_increasing=True,
            macd_delta=0.02,
            macd_delta_accelerating=True,
            histogram=0.03,
            price_above_ema9=True,
            price_above_vwap=True,
            price_cross_above_vwap=False,
        ),
        11,
    )

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "idle"


def test_entry_engine_30s_no_longer_blocks_near_high_without_breakout() -> None:
    config = TradingConfig(confirm_bars=0, min_score=0).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )

    setup_bar = base_indicators(price=2.20, high=2.30, ema9=2.18, vwap=2.18, macd_cross_above=False)
    assert engine.check_entry("UGRO", setup_bar, 10) is None

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            price=2.292,
            high=2.295,
            ema9=2.24,
            vwap=2.24,
            macd_cross_above=True,
            price_cross_above_vwap=False,
        ),
        11,
    )

    assert signal is not None
    assert signal["path"] == "P1_MACD_CROSS"


def test_entry_engine_30s_structure_allows_fresh_breakout() -> None:
    config = TradingConfig(confirm_bars=0, min_score=0).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 10, 0),
    )

    setup_bar = base_indicators(price=2.20, high=2.30, ema9=2.18, vwap=2.18, macd_cross_above=False)
    assert engine.check_entry("UGRO", setup_bar, 10) is None

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            price=2.31,
            high=2.31,
            ema9=2.25,
            vwap=2.29,
            macd_cross_above=True,
            price_cross_above_vwap=False,
        ),
        11,
    )

    assert signal is not None
    assert signal["path"] == "P1_MACD_CROSS"


def test_entry_engine_30s_uses_extended_vwap_before_open() -> None:
    config = TradingConfig(confirm_bars=1, min_score=4).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 8, 0),
    )

    seed = base_indicators(
        price=3.80,
        ema9=3.79,
        vwap=2.20,
        extended_vwap=3.78,
        volume=20_000,
        macd_cross_above=False,
        price_above_vwap=True,
        price_above_extended_vwap=True,
    )
    assert engine.check_entry("UGRO", seed, 10) is None
    assert engine.check_entry("UGRO", seed, 11) is None
    assert engine.check_entry("UGRO", seed, 12) is None

    pending = engine.check_entry(
        "UGRO",
        base_indicators(
            price=3.82,
            ema9=3.80,
            vwap=2.21,
            extended_vwap=3.79,
        ),
        13,
    )

    assert pending is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "pending"


def test_entry_engine_30s_uses_regular_vwap_after_open_without_entry_filter_block() -> None:
    config = TradingConfig(confirm_bars=1, min_score=4).make_30s_variant()
    engine = EntryEngine(
        config,
        name="MACD Bot",
        now_provider=lambda: datetime(2026, 3, 28, 9, 45),
    )

    seed = base_indicators(
        price=3.80,
        ema9=3.79,
        vwap=2.20,
        extended_vwap=3.78,
        volume=20_000,
        macd_cross_above=False,
        price_above_vwap=True,
        price_above_extended_vwap=True,
    )
    assert engine.check_entry("UGRO", seed, 10) is None
    assert engine.check_entry("UGRO", seed, 11) is None
    assert engine.check_entry("UGRO", seed, 12) is None

    pending = engine.check_entry(
        "UGRO",
        base_indicators(
            price=3.82,
            ema9=3.80,
            vwap=2.21,
            extended_vwap=3.79,
        ),
        13,
    )

    assert pending is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "pending"


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


def test_position_tracker_pauses_ticker_after_three_consecutive_losses(tmp_path) -> None:
    config = TradingConfig(
        default_quantity=10,
        ticker_loss_pause_streak_limit=3,
        ticker_loss_pause_minutes=30,
    )
    tracker = PositionTracker(config, history_dir=str(tmp_path))

    for _ in range(3):
        tracker.open_position("UGRO", entry_price=2.0)
        tracker.close_position("UGRO", exit_price=1.9, reason="LOSS")

    allowed, reason = tracker.can_open_position("UGRO")
    other_allowed, other_reason = tracker.can_open_position("ANNA")

    assert allowed is False
    assert "UGRO paused" in reason
    assert other_allowed is True
    assert other_reason == ""


def test_position_tracker_resets_loss_pause_after_winning_trade(tmp_path) -> None:
    config = TradingConfig(
        default_quantity=10,
        ticker_loss_pause_streak_limit=3,
        ticker_loss_pause_minutes=30,
    )
    tracker = PositionTracker(config, history_dir=str(tmp_path))

    for _ in range(2):
        tracker.open_position("UGRO", entry_price=2.0)
        tracker.close_position("UGRO", exit_price=1.9, reason="LOSS")
    tracker.open_position("UGRO", entry_price=2.0)
    tracker.close_position("UGRO", exit_price=2.1, reason="WIN")

    allowed, reason = tracker.can_open_position("UGRO")

    assert allowed is True
    assert reason == ""


def test_position_tracker_cold_loss_pause_ignores_loss_that_had_room_first(tmp_path) -> None:
    config = TradingConfig(
        default_quantity=10,
        ticker_loss_pause_streak_limit=3,
        ticker_loss_pause_minutes=30,
        ticker_loss_pause_only_on_cold_losses=True,
        ticker_loss_pause_cold_peak_profit_pct=1.0,
    )
    tracker = PositionTracker(config, history_dir=str(tmp_path))

    for _ in range(2):
        tracker.open_position("UGRO", entry_price=2.0)
        tracker.close_position("UGRO", exit_price=1.9, reason="LOSS")

    position = tracker.open_position("UGRO", entry_price=2.0)
    position.update_price(2.04)
    tracker.close_position("UGRO", exit_price=1.95, reason="LOSS")

    allowed, reason = tracker.can_open_position("UGRO")

    assert allowed is True
    assert reason == ""


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


def test_make_30s_reclaim_variant_tightens_early_profit_floor_locks() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)

    assert config.profit_floor_lock_at_1pct_peak_pct == 0.25
    assert config.profit_floor_lock_at_2pct_peak_pct == 0.75
    assert config.profit_floor_lock_at_3pct_peak_pct == 1.5
    assert config.profit_floor_trail_buffer_over_4pct_pct == 1.5


def test_reclaim_floor_breaches_sooner_after_one_percent_peak_with_tighter_lock(tmp_path) -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    tracker = PositionTracker(config, history_dir=str(tmp_path))
    position = tracker.open_position("UGRO", entry_price=2.0, path="PRETRIGGER_RECLAIM")
    position.update_price(2.03)
    position.update_price(2.004)
    engine = ExitEngine(config)

    floor_exit = engine.check_exit(
        position,
        base_indicators(
            price=2.004,
            macd_cross_below=False,
            stoch_k_below_exit=False,
            stoch_k_falling=False,
        ),
    )

    assert position.peak_profit_pct >= 1.0
    assert position.floor_pct == 0.25
    assert floor_exit is not None
    assert floor_exit["reason"] == "FLOOR_BREACH"


def test_exit_engine_30s_stoch_health_blocks_premature_stoch_exit(tmp_path) -> None:
    config = TradingConfig().make_30s_variant()
    tracker = PositionTracker(config, history_dir=str(tmp_path))
    position = tracker.open_position("UGRO", entry_price=2.0)
    position.update_price(2.01)
    engine = ExitEngine(config)

    exit_signal = engine.check_exit(
        position,
        base_indicators(
            stoch_k=19.0,
            stoch_k_prev=16.0,
            stoch_k_prev2=12.0,
            stoch_d=17.0,
            stoch_k_below_exit=True,
            stoch_k_falling=True,
            macd_cross_below=False,
        ),
    )

    assert exit_signal is None


def test_exit_engine_30s_stoch_exit_still_fires_on_true_rollover(tmp_path) -> None:
    config = TradingConfig().make_30s_variant()
    tracker = PositionTracker(config, history_dir=str(tmp_path))
    position = tracker.open_position("UGRO", entry_price=2.0)
    position.update_price(2.01)
    engine = ExitEngine(config)

    exit_signal = engine.check_exit(
        position,
        base_indicators(
            stoch_k=18.0,
            stoch_k_prev=24.0,
            stoch_k_prev2=29.0,
            stoch_d=21.0,
            stoch_k_below_exit=True,
            stoch_k_falling=True,
            macd_cross_below=False,
        ),
    )

    assert exit_signal is not None
    assert exit_signal["reason"] == "STOCHK_TIER1"


class _ProbeRuntime:
    def __init__(self, has_position: bool, peak_profit_pct: float = 0.0):
        self.positions = self
        self._has_position = has_position
        self._position = (
            SimpleNamespace(peak_profit_pct=peak_profit_pct)
            if has_position
            else None
        )

    def has_position(self, ticker: str) -> bool:
        del ticker
        return self._has_position

    def get_position(self, ticker: str):
        del ticker
        return self._position

    def can_open_position(self, ticker: str | None = None) -> tuple[bool, str]:
        del ticker
        return True, ""


def test_entry_engine_pretrigger_probe_emits_starter_buy() -> None:
    config = TradingConfig().make_30s_pretrigger_variant(quantity=100)
    engine = EntryEngine(
        config,
        name="Probe Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    warmup_bars = [
        base_indicators(
            open=2.18 + (index * 0.01),
            price=2.24 + (index * 0.01),
            high=2.30 + (index * 0.01),
            low=2.12 + (index * 0.01),
            volume=18_000 + (index * 700),
            histogram=0.001 + (index * 0.001),
            histogram_prev=0.0005 + (index * 0.0008),
            macd=0.002 + (index * 0.001),
            signal=0.001 + (index * 0.0008),
            signal_prev=0.0005 + (index * 0.0007),
            macd_prev=0.001 + (index * 0.0009),
            ema9=2.20 + (index * 0.01),
            ema20=2.08 + (index * 0.005),
            vwap=2.18 + (index * 0.009),
            extended_vwap=2.18 + (index * 0.009),
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        )
        for index in range(10)
    ]
    warmup_bars.extend(
        [
            base_indicators(
                open=2.300,
                price=2.312,
                high=2.315,
                low=2.296,
                volume=28_000,
                histogram=0.010,
                histogram_prev=0.008,
                macd=0.016,
                macd_prev=0.015,
                signal=0.0145,
                signal_prev=0.0140,
                ema9=2.300,
                ema20=2.225,
                vwap=2.296,
                extended_vwap=2.296,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.304,
                price=2.316,
                high=2.318,
                low=2.300,
                volume=29_000,
                histogram=0.011,
                histogram_prev=0.010,
                macd=0.017,
                macd_prev=0.016,
                signal=0.0150,
                signal_prev=0.0145,
                ema9=2.304,
                ema20=2.228,
                vwap=2.300,
                extended_vwap=2.300,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.308,
                price=2.314,
                high=2.317,
                low=2.304,
                volume=30_000,
                histogram=0.012,
                histogram_prev=0.011,
                macd=0.018,
                macd_prev=0.017,
                signal=0.0155,
                signal_prev=0.0150,
                ema9=2.308,
                ema20=2.231,
                vwap=2.304,
                extended_vwap=2.304,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.309,
                price=2.311,
                high=2.314,
                low=2.306,
                volume=31_000,
                histogram=0.013,
                histogram_prev=0.012,
                macd=0.019,
                macd_prev=0.018,
                signal=0.0160,
                signal_prev=0.0155,
                ema9=2.310,
                ema20=2.234,
                vwap=2.307,
                extended_vwap=2.307,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
        ]
    )
    for bar in warmup_bars:
        engine._remember_bar("UGRO", bar)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.31,
            price=2.34,
            high=2.35,
            low=2.30,
            volume=42_000,
            histogram=0.018,
            histogram_prev=0.012,
            macd=0.022,
            macd_prev=0.020,
            signal=0.019,
            signal_prev=0.018,
            ema9=2.32,
            ema20=2.24,
            vwap=2.31,
            extended_vwap=2.31,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
        _ProbeRuntime(False),
    )

    assert signal is not None
    assert signal["action"] == "BUY"
    assert signal["path"] == "PRETRIGGER_PROBE"
    assert signal["quantity"] == 25


def test_entry_engine_pretrigger_probe_adds_on_confirm() -> None:
    config = TradingConfig().make_30s_pretrigger_variant(quantity=100)
    engine = EntryEngine(
        config,
        name="Probe Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    for idx in range(1, 15):
        engine._remember_bar(
            "UGRO",
            base_indicators(
                open=2.18 + (idx * 0.005),
                price=2.20 + (idx * 0.006),
                high=2.22 + (idx * 0.006),
                low=2.16 + (idx * 0.005),
                volume=18_000 + (idx * 500),
                histogram=0.001 + (idx * 0.001),
                macd=0.002 + (idx * 0.001),
                signal=0.001 + (idx * 0.0008),
                ema9=2.18 + (idx * 0.005),
                ema20=2.10 + (idx * 0.003),
                vwap=2.17 + (idx * 0.004),
                extended_vwap=2.17 + (idx * 0.004),
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
        )

    engine._probe_state["UGRO"] = {
        "entry_bar": 15,
        "starter_qty": 25,
        "remaining_qty": 75,
        "probe_entry_price": 2.33,
        "resistance_level": 2.30,
        "hold_floor": 2.27,
        "pretrigger_score": 5,
        "pretrigger_score_details": "comp+ press+ loc+ mom+ candle+ vol-",
        "confirmed": False,
        "confirm_reason": "",
    }

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.31,
            price=2.36,
            high=2.37,
            low=2.30,
            volume=40_000,
            histogram=0.020,
            histogram_prev=0.013,
            macd=0.025,
            macd_prev=0.021,
            signal=0.022,
            signal_prev=0.023,
            ema9=2.31,
            ema20=2.20,
            vwap=2.28,
            extended_vwap=2.28,
            macd_cross_above=True,
            macd_above_signal=True,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        16,
        _ProbeRuntime(True),
    )

    assert signal is not None
    assert signal["action"] == "BUY"
    assert signal["path"] == "P1_MACD_CROSS"
    assert signal["quantity"] == 75


def test_entry_engine_pretrigger_probe_exits_on_fail_fast() -> None:
    config = TradingConfig().make_30s_pretrigger_variant(quantity=100)
    engine = EntryEngine(
        config,
        name="Probe Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )
    engine._probe_state["UGRO"] = {
        "entry_bar": 15,
        "starter_qty": 25,
        "remaining_qty": 75,
        "probe_entry_price": 2.33,
        "resistance_level": 2.30,
        "hold_floor": 2.27,
        "pretrigger_score": 5,
        "pretrigger_score_details": "comp+ press+ loc+ mom+ candle+ vol-",
        "confirmed": False,
        "confirm_reason": "",
    }

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.26,
            price=2.25,
            high=2.27,
            low=2.24,
            volume=25_000,
            histogram=-0.002,
            histogram_prev=0.004,
            macd=0.010,
            macd_prev=0.015,
            signal=0.012,
            signal_prev=0.011,
            ema9=2.28,
            ema20=2.20,
            vwap=2.24,
            extended_vwap=2.24,
            macd_cross_above=False,
            macd_above_signal=False,
        ),
        16,
        _ProbeRuntime(True),
    )

    assert signal is not None
    assert signal["action"] == "SELL"
    assert signal["reason"] == "PRETRIGGER_FAIL_FAST"


def test_entry_engine_pretrigger_probe_can_ignore_macd_fail_fast_when_config_disabled() -> None:
    config = TradingConfig().make_30s_pretrigger_variant(quantity=100)
    config.pretrigger_fail_fast_on_macd_below_signal = False
    config.pretrigger_fail_fast_on_price_below_ema9 = False
    engine = EntryEngine(
        config,
        name="Probe Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )
    engine._probe_state["UGRO"] = {
        "entry_bar": 15,
        "starter_qty": 25,
        "remaining_qty": 75,
        "probe_entry_price": 2.33,
        "resistance_level": 2.30,
        "hold_floor": 2.20,
        "pretrigger_score": 5,
        "pretrigger_score_details": "comp+ press+ loc+ mom+ candle+ vol-",
        "confirmed": False,
        "confirm_reason": "",
    }

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.31,
            price=2.29,
            high=2.32,
            low=2.28,
            volume=25_000,
            histogram=-0.002,
            histogram_prev=0.004,
            macd=0.010,
            macd_prev=0.015,
            signal=0.012,
            signal_prev=0.011,
            ema9=2.30,
            ema20=2.20,
            vwap=2.24,
            extended_vwap=2.24,
            macd_cross_above=False,
            macd_above_signal=False,
        ),
        16,
        _ProbeRuntime(True),
    )

    assert signal is None


def test_entry_engine_pretrigger_probe_still_exits_on_hold_floor_breach_when_soft_flags_disabled() -> None:
    config = TradingConfig().make_30s_pretrigger_variant(quantity=100)
    config.pretrigger_fail_fast_on_macd_below_signal = False
    config.pretrigger_fail_fast_on_price_below_ema9 = False
    engine = EntryEngine(
        config,
        name="Probe Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )
    engine._probe_state["UGRO"] = {
        "entry_bar": 15,
        "starter_qty": 25,
        "remaining_qty": 75,
        "probe_entry_price": 2.33,
        "resistance_level": 2.30,
        "hold_floor": 2.27,
        "pretrigger_score": 5,
        "pretrigger_score_details": "comp+ press+ loc+ mom+ candle+ vol-",
        "confirmed": False,
        "confirm_reason": "",
    }

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.28,
            price=2.25,
            high=2.28,
            low=2.24,
            volume=25_000,
            histogram=0.003,
            histogram_prev=0.004,
            macd=0.013,
            macd_prev=0.015,
            signal=0.012,
            signal_prev=0.011,
            ema9=2.30,
            ema20=2.20,
            vwap=2.24,
            extended_vwap=2.24,
            macd_cross_above=False,
            macd_above_signal=True,
        ),
        16,
        _ProbeRuntime(True),
    )

    assert signal is not None
    assert signal["action"] == "SELL"
    assert signal["reason"] == "PRETRIGGER_FAIL_FAST"


def test_entry_engine_pretrigger_probe_requires_compression_and_location() -> None:
    config = TradingConfig().make_30s_pretrigger_variant(quantity=100)
    engine = EntryEngine(
        config,
        name="Probe Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    warmup_bars = [
        base_indicators(
            open=2.18 + (index * 0.01),
            price=2.22 + (index * 0.03),
            high=2.24 + (index * 0.03),
            low=2.16 + (index * 0.01),
            volume=18_000 + (index * 500),
            histogram=0.001 + (index * 0.001),
            histogram_prev=0.0005 + (index * 0.001),
            macd=0.002 + (index * 0.001),
            signal=0.001 + (index * 0.0008),
            signal_prev=0.0005 + (index * 0.0008),
            macd_prev=0.001 + (index * 0.001),
            ema9=2.18 + (index * 0.01),
            ema20=2.10 + (index * 0.004),
            vwap=2.16 + (index * 0.01),
            extended_vwap=2.16 + (index * 0.01),
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        )
        for index in range(14)
    ]
    for idx, bar in enumerate(warmup_bars, start=1):
        assert engine.check_entry("UGRO", bar, idx) is None

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.60,
            price=2.68,
            high=2.70,
            low=2.58,
            volume=45_000,
            histogram=0.018,
            histogram_prev=0.012,
            macd=0.022,
            macd_prev=0.020,
            signal=0.019,
            signal_prev=0.018,
            ema9=2.55,
            ema20=2.32,
            vwap=2.42,
            extended_vwap=2.42,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
        _ProbeRuntime(False),
    )

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "blocked"
    assert decision["reason"] in {"pretrigger compression not ready", "pretrigger location not ready"}


def test_entry_engine_pretrigger_probe_requires_pressure() -> None:
    config = TradingConfig().make_30s_pretrigger_variant(quantity=100)
    engine = EntryEngine(
        config,
        name="Probe Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    warmup_bars = [
        base_indicators(
            open=2.280 + (index * 0.002),
            price=2.290 + (index * 0.002),
            high=2.300 + (index * 0.002),
            low=2.270 + (index * 0.002),
            volume=24_000 + (index * 400),
            histogram=0.008 + (index * 0.001),
            histogram_prev=0.007 + (index * 0.001),
            macd=0.015 + (index * 0.001),
            macd_prev=0.014 + (index * 0.001),
            signal=0.013 + (index * 0.0008),
            signal_prev=0.012 + (index * 0.0008),
            ema9=2.276 + (index * 0.002),
            ema20=2.220 + (index * 0.001),
            vwap=2.272 + (index * 0.002),
            extended_vwap=2.272 + (index * 0.002),
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        )
        for index in range(14)
    ]
    for idx, bar in enumerate(warmup_bars, start=1):
        assert engine.check_entry("UGRO", bar, idx) is None

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.332,
            price=2.356,
            high=2.360,
            low=2.330,
            volume=38_000,
            histogram=0.021,
            histogram_prev=0.017,
            macd=0.027,
            macd_prev=0.024,
            signal=0.023,
            signal_prev=0.021,
            ema9=2.334,
            ema20=2.248,
            vwap=2.330,
            extended_vwap=2.330,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
        _ProbeRuntime(False),
    )

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "blocked"
    assert decision["reason"] == "pretrigger pressure not ready"


def test_entry_engine_pretrigger_probe_blocks_overbought_stoch() -> None:
    config = TradingConfig().make_30s_pretrigger_variant(quantity=100)
    engine = EntryEngine(
        config,
        name="Probe Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    warmup_bars = [
        base_indicators(
            open=2.18 + (index * 0.01),
            price=2.24 + (index * 0.01),
            high=2.30 + (index * 0.01),
            low=2.12 + (index * 0.01),
            volume=18_000 + (index * 700),
            histogram=0.001 + (index * 0.001),
            histogram_prev=0.0005 + (index * 0.0008),
            macd=0.002 + (index * 0.001),
            signal=0.001 + (index * 0.0008),
            signal_prev=0.0005 + (index * 0.0007),
            macd_prev=0.001 + (index * 0.0009),
            ema9=2.20 + (index * 0.01),
            ema20=2.08 + (index * 0.005),
            vwap=2.18 + (index * 0.009),
            extended_vwap=2.18 + (index * 0.009),
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        )
        for index in range(10)
    ]
    warmup_bars.extend(
        [
            base_indicators(
                open=2.300,
                price=2.312,
                high=2.315,
                low=2.296,
                volume=28_000,
                histogram=0.010,
                histogram_prev=0.008,
                macd=0.016,
                macd_prev=0.015,
                signal=0.0145,
                signal_prev=0.0140,
                ema9=2.300,
                ema20=2.225,
                vwap=2.296,
                extended_vwap=2.296,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.304,
                price=2.316,
                high=2.318,
                low=2.300,
                volume=29_000,
                histogram=0.011,
                histogram_prev=0.010,
                macd=0.017,
                macd_prev=0.016,
                signal=0.0150,
                signal_prev=0.0145,
                ema9=2.304,
                ema20=2.228,
                vwap=2.300,
                extended_vwap=2.300,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.308,
                price=2.314,
                high=2.317,
                low=2.304,
                volume=30_000,
                histogram=0.012,
                histogram_prev=0.011,
                macd=0.018,
                macd_prev=0.017,
                signal=0.0155,
                signal_prev=0.0150,
                ema9=2.308,
                ema20=2.231,
                vwap=2.304,
                extended_vwap=2.304,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.309,
                price=2.311,
                high=2.314,
                low=2.306,
                volume=31_000,
                histogram=0.013,
                histogram_prev=0.012,
                macd=0.019,
                macd_prev=0.018,
                signal=0.0160,
                signal_prev=0.0155,
                ema9=2.310,
                ema20=2.234,
                vwap=2.307,
                extended_vwap=2.307,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
        ]
    )
    for bar in warmup_bars:
        engine._remember_bar("UGRO", bar)

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.31,
            price=2.34,
            high=2.35,
            low=2.30,
            volume=42_000,
            histogram=0.018,
            histogram_prev=0.012,
            macd=0.022,
            macd_prev=0.020,
            signal=0.019,
            signal_prev=0.018,
            ema9=2.32,
            ema20=2.24,
            vwap=2.31,
            extended_vwap=2.31,
            stoch_k=91.0,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
        _ProbeRuntime(False),
    )

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "blocked"
    assert "stochK" in decision["reason"]


def test_entry_engine_pretrigger_probe_allows_small_pullback_below_ema9() -> None:
    config = TradingConfig().make_30s_pretrigger_variant(quantity=100)
    engine = EntryEngine(
        config,
        name="Probe Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    warmup_bars = [
        base_indicators(
            open=2.18 + (index * 0.01),
            price=2.24 + (index * 0.01),
            high=2.30 + (index * 0.01),
            low=2.12 + (index * 0.01),
            volume=18_000 + (index * 700),
            histogram=0.001 + (index * 0.001),
            histogram_prev=0.0005 + (index * 0.0008),
            macd=0.002 + (index * 0.001),
            signal=0.001 + (index * 0.0008),
            signal_prev=0.0005 + (index * 0.0007),
            macd_prev=0.001 + (index * 0.0009),
            ema9=2.20 + (index * 0.01),
            ema20=2.08 + (index * 0.005),
            vwap=2.18 + (index * 0.009),
            extended_vwap=2.18 + (index * 0.009),
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        )
        for index in range(10)
    ]
    warmup_bars.extend(
        [
            base_indicators(
                open=2.300,
                price=2.312,
                high=2.315,
                low=2.296,
                volume=32_000,
                histogram=0.010,
                histogram_prev=0.008,
                macd=0.016,
                macd_prev=0.015,
                signal=0.0145,
                signal_prev=0.0140,
                ema9=2.300,
                ema20=2.225,
                vwap=2.296,
                extended_vwap=2.296,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.304,
                price=2.316,
                high=2.318,
                low=2.300,
                volume=33_000,
                histogram=0.011,
                histogram_prev=0.010,
                macd=0.017,
                macd_prev=0.016,
                signal=0.0150,
                signal_prev=0.0145,
                ema9=2.304,
                ema20=2.228,
                vwap=2.300,
                extended_vwap=2.300,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.308,
                price=2.314,
                high=2.317,
                low=2.304,
                volume=34_000,
                histogram=0.012,
                histogram_prev=0.011,
                macd=0.018,
                macd_prev=0.017,
                signal=0.0155,
                signal_prev=0.0150,
                ema9=2.308,
                ema20=2.231,
                vwap=2.304,
                extended_vwap=2.304,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.309,
                price=2.311,
                high=2.314,
                low=2.306,
                volume=35_000,
                histogram=0.013,
                histogram_prev=0.012,
                macd=0.019,
                macd_prev=0.018,
                signal=0.0160,
                signal_prev=0.0155,
                ema9=2.310,
                ema20=2.234,
                vwap=2.307,
                extended_vwap=2.307,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
        ]
    )
    for bar in warmup_bars:
        engine._remember_bar("UGRO", bar)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.309,
            price=2.318,
            high=2.319,
            low=2.309,
            volume=42_000,
            histogram=0.018,
            histogram_prev=0.012,
            macd=0.022,
            macd_prev=0.020,
            signal=0.019,
            signal_prev=0.018,
            ema9=2.320,
            ema20=2.24,
            vwap=2.31,
            extended_vwap=2.31,
            stoch_k=72.0,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
        _ProbeRuntime(False),
    )

    assert signal is not None
    assert signal["path"] == "PRETRIGGER_PROBE"


def test_entry_engine_pretrigger_probe_allows_trimmed_compression_with_single_wick() -> None:
    config = TradingConfig().make_30s_pretrigger_variant(quantity=100)
    engine = EntryEngine(
        config,
        name="Probe Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    for idx in range(10):
        engine._remember_bar(
            "UGRO",
            base_indicators(
                open=2.18 + (idx * 0.01),
                price=2.24 + (idx * 0.01),
                high=2.30 + (idx * 0.01),
                low=2.12 + (idx * 0.01),
                volume=18_000 + (idx * 700),
                histogram=0.001 + (idx * 0.001),
                histogram_prev=0.0005 + (idx * 0.0008),
                macd=0.002 + (idx * 0.001),
                signal=0.001 + (idx * 0.0008),
                signal_prev=0.0005 + (idx * 0.0007),
                macd_prev=0.001 + (idx * 0.0009),
                ema9=2.20 + (idx * 0.01),
                ema20=2.08 + (idx * 0.005),
                vwap=2.18 + (idx * 0.009),
                extended_vwap=2.18 + (idx * 0.009),
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
        )
    for bar in (
        base_indicators(
            open=2.300,
            price=2.312,
            high=2.315,
            low=2.296,
            volume=28_000,
            histogram=0.010,
            histogram_prev=0.008,
            macd=0.016,
            macd_prev=0.015,
            signal=0.0145,
            signal_prev=0.0140,
            ema9=2.300,
            ema20=2.226,
            vwap=2.296,
            extended_vwap=2.296,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        base_indicators(
            open=2.304,
            price=2.316,
            high=2.420,  # single noisy wick
            low=2.300,
            volume=29_000,
            histogram=0.011,
            histogram_prev=0.010,
            macd=0.017,
            macd_prev=0.016,
            signal=0.0150,
            signal_prev=0.0145,
            ema9=2.304,
            ema20=2.229,
            vwap=2.300,
            extended_vwap=2.300,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        base_indicators(
            open=2.308,
            price=2.314,
            high=2.317,
            low=2.304,
            volume=30_000,
            histogram=0.012,
            histogram_prev=0.011,
            macd=0.018,
            macd_prev=0.017,
            signal=0.0155,
            signal_prev=0.0150,
            ema9=2.308,
            ema20=2.232,
            vwap=2.304,
            extended_vwap=2.304,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        base_indicators(
            open=2.309,
            price=2.311,
            high=2.314,
            low=2.306,
            volume=31_000,
            histogram=0.013,
            histogram_prev=0.012,
            macd=0.019,
            macd_prev=0.018,
            signal=0.0160,
            signal_prev=0.0155,
            ema9=2.310,
            ema20=2.235,
            vwap=2.307,
            extended_vwap=2.307,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
    ):
        engine._remember_bar("UGRO", bar)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.309,
            price=2.338,
            high=2.342,
            low=2.307,
            volume=42_000,
            histogram=0.018,
            histogram_prev=0.012,
            macd=0.022,
            macd_prev=0.020,
            signal=0.019,
            signal_prev=0.018,
            ema9=2.320,
            ema20=2.240,
            vwap=2.310,
            extended_vwap=2.310,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
        _ProbeRuntime(False),
    )

    assert signal is not None
    assert signal["path"] == "PRETRIGGER_PROBE"


def test_entry_engine_pretrigger_probe_allows_three_of_four_compression_shelf() -> None:
    config = TradingConfig().make_30s_pretrigger_variant(quantity=100)
    engine = EntryEngine(
        config,
        name="Probe Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    for idx in range(10):
        engine._remember_bar(
            "UGRO",
            base_indicators(
                open=2.18 + (idx * 0.01),
                price=2.24 + (idx * 0.01),
                high=2.30 + (idx * 0.01),
                low=2.12 + (idx * 0.01),
                volume=18_000 + (idx * 700),
                histogram=0.001 + (idx * 0.001),
                histogram_prev=0.0005 + (idx * 0.0008),
                macd=0.002 + (idx * 0.001),
                signal=0.001 + (idx * 0.0008),
                signal_prev=0.0005 + (idx * 0.0007),
                macd_prev=0.001 + (idx * 0.0009),
                ema9=2.20 + (idx * 0.01),
                ema20=2.08 + (idx * 0.005),
                vwap=2.18 + (idx * 0.009),
                extended_vwap=2.18 + (idx * 0.009),
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
        )

    for bar in (
        base_indicators(
            open=2.28,
            price=2.37,
            high=2.39,
            low=2.21,
            volume=26_000,
            histogram=0.010,
            histogram_prev=0.008,
            macd=0.016,
            macd_prev=0.015,
            signal=0.0145,
            signal_prev=0.0140,
            ema9=2.29,
            ema20=2.22,
            vwap=2.26,
            extended_vwap=2.26,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        base_indicators(
            open=2.342,
            price=2.351,
            high=2.354,
            low=2.338,
            volume=28_000,
            histogram=0.011,
            histogram_prev=0.010,
            macd=0.017,
            macd_prev=0.016,
            signal=0.0150,
            signal_prev=0.0145,
            ema9=2.338,
            ema20=2.226,
            vwap=2.304,
            extended_vwap=2.304,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        base_indicators(
            open=2.346,
            price=2.356,
            high=2.358,
            low=2.342,
            volume=29_000,
            histogram=0.012,
            histogram_prev=0.011,
            macd=0.018,
            macd_prev=0.017,
            signal=0.0155,
            signal_prev=0.0150,
            ema9=2.342,
            ema20=2.229,
            vwap=2.308,
            extended_vwap=2.308,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        base_indicators(
            open=2.350,
            price=2.354,
            high=2.357,
            low=2.347,
            volume=30_000,
            histogram=0.013,
            histogram_prev=0.012,
            macd=0.019,
            macd_prev=0.018,
            signal=0.0160,
            signal_prev=0.0155,
            ema9=2.346,
            ema20=2.232,
            vwap=2.312,
            extended_vwap=2.312,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
    ):
        engine._remember_bar("UGRO", bar)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.356,
            price=2.364,
            high=2.365,
            low=2.351,
            volume=41_000,
            histogram=0.018,
            histogram_prev=0.013,
            macd=0.022,
            macd_prev=0.020,
            signal=0.019,
            signal_prev=0.018,
            ema9=2.356,
            ema20=2.238,
            vwap=2.316,
            extended_vwap=2.316,
            stoch_k=74.0,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
        _ProbeRuntime(False),
    )

    assert signal is not None
    assert signal["path"] == "PRETRIGGER_PROBE"


def test_entry_engine_pretrigger_probe_does_not_add_when_confirm_is_extended() -> None:
    config = TradingConfig().make_30s_pretrigger_variant(quantity=100)
    engine = EntryEngine(
        config,
        name="Probe Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    for idx in range(1, 15):
        engine._remember_bar(
            "UGRO",
            base_indicators(
                open=2.18 + (idx * 0.005),
                price=2.20 + (idx * 0.006),
                high=2.22 + (idx * 0.006),
                low=2.16 + (idx * 0.005),
                volume=18_000 + (idx * 500),
                histogram=0.001 + (idx * 0.001),
                macd=0.002 + (idx * 0.001),
                signal=0.001 + (idx * 0.0008),
                ema9=2.18 + (idx * 0.005),
                ema20=2.10 + (idx * 0.003),
                vwap=2.17 + (idx * 0.004),
                extended_vwap=2.17 + (idx * 0.004),
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
        )

    engine._probe_state["UGRO"] = {
        "entry_bar": 15,
        "starter_qty": 25,
        "remaining_qty": 75,
        "probe_entry_price": 2.33,
        "resistance_level": 2.30,
        "hold_floor": 2.27,
        "pretrigger_score": 5,
        "pretrigger_score_details": "comp+ press+ loc+ mom+ candle+ vol-",
        "confirmed": False,
        "confirm_reason": "",
    }

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.44,
            price=2.46,
            high=2.55,
            low=2.43,
            volume=40_000,
            histogram=0.020,
            histogram_prev=0.013,
            macd=0.025,
            macd_prev=0.021,
            signal=0.022,
            signal_prev=0.023,
            ema9=2.35,
            ema20=2.20,
            vwap=2.28,
            extended_vwap=2.28,
            macd_cross_above=True,
            macd_above_signal=True,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        16,
        _ProbeRuntime(True),
    )

    assert signal is None
    assert engine._probe_state["UGRO"]["confirmed"] is False


def _seed_reclaim_pullback_setup(engine: EntryEngine, ticker: str = "UGRO") -> None:
    warmup_bars = [
        base_indicators(
            open=2.40 + (idx * 0.01),
            price=2.45 + (idx * 0.01),
            high=2.47 + (idx * 0.01),
            low=2.38 + (idx * 0.01),
            volume=20_000 + (idx * 500),
            histogram=0.008 + (idx * 0.001),
            histogram_prev=0.007 + (idx * 0.001),
            macd=0.020 + (idx * 0.001),
            macd_prev=0.019 + (idx * 0.001),
            signal=0.017 + (idx * 0.0008),
            signal_prev=0.016 + (idx * 0.0008),
            ema9=2.40 + (idx * 0.008),
            ema20=2.28 + (idx * 0.005),
            vwap=2.38 + (idx * 0.007),
            extended_vwap=2.38 + (idx * 0.007),
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        )
        for idx in range(6)
    ]
    warmup_bars.extend(
        [
            base_indicators(
                open=2.54,
                price=2.58,
                high=2.60,
                low=2.53,
                volume=26_000,
                histogram=0.013,
                histogram_prev=0.012,
                macd=0.026,
                macd_prev=0.025,
                signal=0.021,
                signal_prev=0.020,
                ema9=2.47,
                ema20=2.33,
                vwap=2.43,
                extended_vwap=2.43,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.57,
                price=2.49,
                high=2.58,
                low=2.46,
                volume=20_000,
                histogram=0.007,
                histogram_prev=0.013,
                macd=0.022,
                macd_prev=0.026,
                signal=0.020,
                signal_prev=0.021,
                ema9=2.47,
                ema20=2.34,
                vwap=2.44,
                extended_vwap=2.44,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.49,
                price=2.46,
                high=2.50,
                low=2.44,
                volume=19_000,
                histogram=0.0075,
                histogram_prev=0.007,
                macd=0.022,
                macd_prev=0.022,
                signal=0.020,
                signal_prev=0.020,
                ema9=2.46,
                ema20=2.34,
                vwap=2.44,
                extended_vwap=2.44,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.46,
                price=2.47,
                high=2.48,
                low=2.45,
                volume=20_000,
                histogram=0.008,
                histogram_prev=0.0075,
                macd=0.023,
                macd_prev=0.022,
                signal=0.020,
                signal_prev=0.020,
                ema9=2.46,
                ema20=2.345,
                vwap=2.445,
                extended_vwap=2.445,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.47,
                price=2.48,
                high=2.49,
                low=2.46,
                volume=21_000,
                histogram=0.009,
                histogram_prev=0.008,
                macd=0.024,
                macd_prev=0.023,
                signal=0.021,
                signal_prev=0.020,
                ema9=2.47,
                ema20=2.35,
                vwap=2.45,
                extended_vwap=2.45,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.48,
                price=2.49,
                high=2.50,
                low=2.47,
                volume=22_000,
                histogram=0.010,
                histogram_prev=0.009,
                macd=0.025,
                macd_prev=0.024,
                signal=0.0215,
                signal_prev=0.021,
                ema9=2.475,
                ema20=2.355,
                vwap=2.452,
                extended_vwap=2.452,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.49,
                price=2.495,
                high=2.50,
                low=2.48,
                volume=22_500,
                histogram=0.011,
                histogram_prev=0.010,
                macd=0.026,
                macd_prev=0.025,
                signal=0.022,
                signal_prev=0.0215,
                ema9=2.48,
                ema20=2.36,
                vwap=2.454,
                extended_vwap=2.454,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.495,
                price=2.498,
                high=2.50,
                low=2.49,
                volume=23_000,
                histogram=0.012,
                histogram_prev=0.011,
                macd=0.027,
                macd_prev=0.026,
                signal=0.0225,
                signal_prev=0.022,
                ema9=2.482,
                ema20=2.365,
                vwap=2.455,
                extended_vwap=2.455,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
        ]
    )
    for bar in warmup_bars:
        engine._remember_bar(ticker, bar)


def _seed_retest_breakout_setup(engine: EntryEngine, ticker: str = "UGRO") -> None:
    setup_bars = [
        base_indicators(
            open=2.38 + (idx * 0.01),
            price=2.42 + (idx * 0.01),
            high=2.44 + (idx * 0.01),
            low=2.36 + (idx * 0.01),
            volume=18_000 + (idx * 500),
            histogram=0.006 + (idx * 0.001),
            histogram_prev=0.005 + (idx * 0.001),
            macd=0.015 + (idx * 0.001),
            macd_prev=0.014 + (idx * 0.001),
            signal=0.012 + (idx * 0.0007),
            signal_prev=0.011 + (idx * 0.0007),
            ema9=2.38 + (idx * 0.008),
            ema20=2.28 + (idx * 0.005),
            vwap=2.37 + (idx * 0.007),
            extended_vwap=2.37 + (idx * 0.007),
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        )
        for idx in range(10)
    ]
    setup_bars[7] = base_indicators(
        open=2.50,
        price=2.53,
        high=2.54,
        low=2.49,
        volume=24_000,
        histogram=0.013,
        histogram_prev=0.012,
        macd=0.022,
        macd_prev=0.020,
        signal=0.017,
        signal_prev=0.016,
        ema9=2.46,
        ema20=2.32,
        vwap=2.44,
        extended_vwap=2.44,
        macd_cross_above=False,
        price_cross_above_vwap=False,
        price_above_vwap=True,
        price_above_extended_vwap=True,
    )
    setup_bars.extend(
        [
            base_indicators(
                open=2.48,
                price=2.53,
                high=2.55,
                low=2.47,
                volume=34_000,
                histogram=0.015,
                histogram_prev=0.012,
                macd=0.026,
                macd_prev=0.022,
                signal=0.020,
                signal_prev=0.019,
                ema9=2.47,
                ema20=2.35,
                vwap=2.45,
                extended_vwap=2.45,
                macd_cross_above=False,
                price_cross_above_vwap=True,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.53,
                price=2.58,
                high=2.60,
                low=2.52,
                volume=56_000,
                histogram=0.020,
                histogram_prev=0.015,
                macd=0.032,
                macd_prev=0.026,
                signal=0.022,
                signal_prev=0.020,
                ema9=2.50,
                ema20=2.37,
                vwap=2.47,
                extended_vwap=2.47,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.58,
                price=2.56,
                high=2.59,
                low=2.54,
                volume=28_000,
                histogram=0.018,
                histogram_prev=0.020,
                macd=0.031,
                macd_prev=0.032,
                signal=0.024,
                signal_prev=0.022,
                ema9=2.52,
                ema20=2.39,
                vwap=2.49,
                extended_vwap=2.49,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
            base_indicators(
                open=2.56,
                price=2.55,
                high=2.57,
                low=2.53,
                volume=24_000,
                histogram=0.017,
                histogram_prev=0.018,
                macd=0.030,
                macd_prev=0.031,
                signal=0.024,
                signal_prev=0.024,
                ema9=2.525,
                ema20=2.40,
                vwap=2.50,
                extended_vwap=2.50,
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
        ]
    )
    for bar in setup_bars:
        engine._remember_bar(ticker, bar)


def test_entry_engine_pretrigger_reclaim_emits_starter_buy() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_lookback_bars = 14
    config.pretrigger_reclaim_max_extension_above_ema9_pct = 0.06
    config.pretrigger_reclaim_max_extension_above_vwap_pct = 0.06
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.cooldown_bars = 0
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.52,
            price=2.59,
            high=2.60,
            low=2.50,
            volume=31_000,
            histogram=0.016,
            histogram_prev=0.014,
            macd=0.029,
            macd_prev=0.028,
            signal=0.023,
            signal_prev=0.023,
            ema9=2.485,
            ema20=2.385,
            vwap=2.455,
            extended_vwap=2.455,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
    )

    assert signal is not None
    assert signal["action"] == "BUY"
    assert signal["path"] == "PRETRIGGER_RECLAIM"
    assert signal["quantity"] == 25
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["reason"] == "PRETRIGGER_RECLAIM"


def test_make_30s_reclaim_variant_uses_current_research_baseline() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)

    assert config.pretrigger_reclaim_touch_lookback_bars == 8
    assert config.pretrigger_reclaim_min_pullback_from_high_pct == 0.0025
    assert config.pretrigger_reclaim_max_pullback_from_high_pct == 0.15
    assert config.pretrigger_reclaim_max_retrace_fraction_of_leg == 1.2
    assert config.pretrigger_reclaim_max_extension_above_ema9_pct == 0.02
    assert config.pretrigger_reclaim_max_extension_above_vwap_pct == 0.04
    assert config.pretrigger_reclaim_require_higher_low is False
    assert config.pretrigger_reclaim_require_held_move is False
    assert config.pretrigger_reclaim_require_volume is False
    assert config.pretrigger_reclaim_require_pullback_absorption is False
    assert config.pretrigger_reclaim_require_stoch is False
    assert config.pretrigger_reclaim_confirm_add_min_peak_profit_pct == 1.0
    assert config.profit_floor_lock_at_1pct_peak_pct == 0.25
    assert config.profit_floor_lock_at_2pct_peak_pct == 0.75
    assert config.pretrigger_reclaim_require_reentry_reset is False
    assert config.pretrigger_reclaim_reentry_min_reset_from_high_pct == 0.01
    assert config.pretrigger_reclaim_reentry_touch_lookback_bars == 8
    assert config.pretrigger_reclaim_fail_fast_on_macd_below_signal is False
    assert config.pretrigger_reclaim_fail_fast_on_price_below_ema9 is False
    assert config.pretrigger_failed_break_lookahead_bars == 4
    assert config.ticker_loss_pause_streak_limit == 3
    assert config.ticker_loss_pause_only_on_cold_losses is True
    assert config.ticker_loss_pause_cold_peak_profit_pct == 1.0


def test_entry_engine_pretrigger_reclaim_blocks_reentry_without_fresh_reset() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_max_extension_above_ema9_pct = 0.06
    config.pretrigger_reclaim_max_extension_above_vwap_pct = 0.06
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.pretrigger_reclaim_require_reentry_reset = True
    config.cooldown_bars = 0
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)
    engine._last_exit_bar["UGRO"] = 17
    recent = engine._recent_bars["UGRO"]
    for offset, bar in enumerate(recent[-4:], start=1):
        bar["bar_index"] = float(14 + offset)
        bar["low"] = max(float(bar["low"]), float(bar["ema9"]) * 1.02)
        bar["selected_vwap"] = float(bar["low"]) * 0.95

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.49,
            price=2.55,
            high=2.56,
            low=2.50,
            volume=29_000,
            histogram=0.015,
            histogram_prev=0.010,
            macd=0.029,
            macd_prev=0.025,
            signal=0.021,
            signal_prev=0.020,
            ema9=2.47,
            ema20=2.35,
            vwap=2.44,
            extended_vwap=2.44,
            stoch_k=58.0,
            price_above_vwap=True,
            price_above_extended_vwap=True,
            macd_cross_above=False,
            price_cross_above_vwap=False,
        ),
        19,
    )

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["reason"] == "pretrigger reclaim reentry reset missing fresh EMA9/VWAP touch"


def test_entry_engine_pretrigger_reclaim_allows_reentry_after_fresh_reset() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_max_extension_above_ema9_pct = 0.06
    config.pretrigger_reclaim_max_extension_above_vwap_pct = 0.06
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.pretrigger_reclaim_require_reentry_reset = True
    config.cooldown_bars = 0
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)
    engine._last_exit_bar["UGRO"] = 17
    recent = engine._recent_bars["UGRO"]
    for offset, bar in enumerate(recent[-4:], start=1):
        bar["bar_index"] = float(14 + offset)

    outcome = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.49,
            price=2.55,
            high=2.56,
            low=2.46,
            volume=29_000,
            histogram=0.015,
            histogram_prev=0.010,
            macd=0.029,
            macd_prev=0.025,
            signal=0.021,
            signal_prev=0.020,
            ema9=2.47,
            ema20=2.35,
            vwap=2.44,
            extended_vwap=2.44,
            stoch_k=58.0,
            price_above_vwap=True,
            price_above_extended_vwap=True,
            macd_cross_above=False,
            price_cross_above_vwap=False,
        ),
        19,
    )

    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["reason"] in {"PRETRIGGER_RECLAIM", "PRETRIGGER_RECLAIM_ARMED"}
    if outcome is not None:
        assert outcome["path"] == "PRETRIGGER_RECLAIM"


def test_make_30s_retest_variant_uses_conservative_research_baseline() -> None:
    config = TradingConfig().make_30s_retest_variant(quantity=100)

    assert config.entry_logic_mode == "pretrigger_retest"
    assert config.pretrigger_entry_size_factor == 1.0
    assert config.pretrigger_confirm_entry_size_factor == 0.0
    assert config.pretrigger_fail_fast_on_macd_below_signal is False
    assert config.pretrigger_fail_fast_on_price_below_ema9 is False
    assert config.pretrigger_retest_breakout_window_bars == 6
    assert config.pretrigger_retest_min_breakout_pct == 0.0025
    assert config.pretrigger_retest_breakout_close_tolerance_pct == 0.0015
    assert config.pretrigger_retest_breakout_min_close_pos_pct == 0.60
    assert config.pretrigger_retest_breakout_min_range_expansion == 1.00
    assert config.pretrigger_retest_max_pullback_from_breakout_pct == 0.04
    assert config.pretrigger_retest_level_tolerance_pct == 0.005
    assert config.pretrigger_retest_require_dual_anchor is True
    assert config.pretrigger_retest_arm_break_lookahead_bars == 1


def test_entry_engine_pretrigger_reclaim_arms_then_buys_on_next_bar_break() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_max_extension_above_ema9_pct = 0.06
    config.pretrigger_reclaim_max_extension_above_vwap_pct = 0.06
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)

    armed = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.485,
            price=2.492,
            high=2.499,
            low=2.470,
            volume=31_000,
            histogram=0.015,
            histogram_prev=0.014,
            macd=0.028,
            macd_prev=0.027,
            signal=0.023,
            signal_prev=0.023,
            ema9=2.485,
            ema20=2.385,
            vwap=2.455,
            extended_vwap=2.455,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
    )

    assert armed is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "pending"
    assert decision["reason"] == "PRETRIGGER_RECLAIM_ARMED"

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.494,
            price=2.59,
            high=2.60,
            low=2.49,
            volume=34_000,
            histogram=0.017,
            histogram_prev=0.015,
            macd=0.030,
            macd_prev=0.028,
            signal=0.024,
            signal_prev=0.023,
            ema9=2.490,
            ema20=2.388,
            vwap=2.458,
            extended_vwap=2.458,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        16,
    )

    assert signal is not None
    assert signal["action"] == "BUY"
    assert signal["path"] == "PRETRIGGER_RECLAIM_BREAK"
    assert signal["quantity"] == 25


def test_entry_engine_pretrigger_reclaim_adds_on_follow_through_break() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_max_extension_above_ema9_pct = 0.06
    config.pretrigger_reclaim_max_extension_above_vwap_pct = 0.06
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)
    engine._probe_state["UGRO"] = {
        "entry_bar": 15,
        "starter_qty": 25,
        "remaining_qty": 75,
        "probe_entry_price": 2.59,
        "resistance_level": 2.58,
        "hold_floor": 2.48,
        "effective_atr": 0.06,
        "pretrigger_score": 5,
        "pretrigger_score_details": "reclaim+ pullback+ location+ momentum+ trend+",
        "confirmed": False,
        "confirm_reason": "",
        "starter_high": 2.60,
    }

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.58,
            price=2.63,
            high=2.65,
            low=2.56,
            volume=34_000,
            histogram=0.019,
            histogram_prev=0.016,
            macd=0.029,
            macd_prev=0.028,
            signal=0.027,
            signal_prev=0.027,
            ema9=2.52,
            ema20=2.42,
            vwap=2.49,
            extended_vwap=2.49,
            macd_cross_above=False,
            macd_above_signal=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        16,
        _ProbeRuntime(True, peak_profit_pct=1.2),
    )

    assert signal is not None
    assert signal["action"] == "BUY"
    assert signal["path"] == "R1_BREAK_CONFIRM"
    assert signal["quantity"] == 75
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["reason"] == "PRETRIGGER_ADD_R1_BREAK_CONFIRM"


def test_entry_engine_pretrigger_reclaim_blocks_add_until_starter_has_min_peak_profit() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_max_extension_above_ema9_pct = 0.06
    config.pretrigger_reclaim_max_extension_above_vwap_pct = 0.06
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.pretrigger_reclaim_confirm_add_min_peak_profit_pct = 1.0
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)
    engine._probe_state["UGRO"] = {
        "entry_bar": 15,
        "starter_qty": 25,
        "remaining_qty": 75,
        "probe_entry_price": 2.52,
        "resistance_level": 2.50,
        "hold_floor": 2.46,
        "pretrigger_score": 3,
        "pretrigger_score_details": "pullback+ touch+ loc+ mom+ candle+ vol-",
        "confirmed": False,
        "confirm_reason": "",
        "effective_atr": 0.02,
        "starter_high": 2.53,
    }

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.51,
            price=2.58,
            high=2.59,
            low=2.50,
            volume=32_000,
            histogram=0.015,
            histogram_prev=0.011,
            macd=0.020,
            macd_prev=0.017,
            signal=0.018,
            signal_prev=0.017,
            ema9=2.52,
            ema20=2.42,
            vwap=2.49,
            extended_vwap=2.49,
            macd_cross_above=False,
            macd_above_signal=True,
            stoch_k=58.0,
            stoch_k_prev=54.0,
            stoch_k_prev2=50.0,
        ),
        16,
        _ProbeRuntime(True, peak_profit_pct=0.8),
    )

    assert signal is None
    assert engine.pop_last_decision("UGRO") is None
    assert bool(engine._probe_state["UGRO"]["confirmed"]) is False


def test_entry_engine_pretrigger_reclaim_allows_add_after_starter_reaches_min_peak_profit() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_max_extension_above_ema9_pct = 0.06
    config.pretrigger_reclaim_max_extension_above_vwap_pct = 0.06
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.pretrigger_reclaim_confirm_add_min_peak_profit_pct = 1.0
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)
    engine._probe_state["UGRO"] = {
        "entry_bar": 15,
        "starter_qty": 25,
        "remaining_qty": 75,
        "probe_entry_price": 2.52,
        "resistance_level": 2.50,
        "hold_floor": 2.46,
        "pretrigger_score": 3,
        "pretrigger_score_details": "pullback+ touch+ loc+ mom+ candle+ vol-",
        "confirmed": False,
        "confirm_reason": "",
        "effective_atr": 0.02,
        "starter_high": 2.53,
    }

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.51,
            price=2.58,
            high=2.59,
            low=2.50,
            volume=32_000,
            histogram=0.015,
            histogram_prev=0.011,
            macd=0.020,
            macd_prev=0.017,
            signal=0.018,
            signal_prev=0.017,
            ema9=2.52,
            ema20=2.42,
            vwap=2.49,
            extended_vwap=2.49,
            macd_cross_above=False,
            macd_above_signal=True,
            stoch_k=58.0,
            stoch_k_prev=54.0,
            stoch_k_prev2=50.0,
        ),
        16,
        _ProbeRuntime(True, peak_profit_pct=1.4),
    )

    assert signal is not None
    assert signal["action"] == "BUY"
    assert signal["path"] == "R1_BREAK_CONFIRM"
    assert signal["quantity"] == 75


def test_entry_engine_pretrigger_reclaim_waits_without_follow_through_break() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)
    engine._probe_state["UGRO"] = {
        "entry_bar": 15,
        "starter_qty": 25,
        "remaining_qty": 75,
        "probe_entry_price": 2.59,
        "resistance_level": 2.58,
        "hold_floor": 2.48,
        "effective_atr": 0.06,
        "pretrigger_score": 5,
        "pretrigger_score_details": "reclaim+ pullback+ location+ momentum+ trend+",
        "confirmed": False,
        "confirm_reason": "",
        "starter_high": 2.60,
    }

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.57,
            price=2.59,
            high=2.595,
            low=2.55,
            volume=30_000,
            histogram=0.018,
            histogram_prev=0.017,
            macd=0.028,
            macd_prev=0.027,
            signal=0.027,
            signal_prev=0.027,
            ema9=2.52,
            ema20=2.42,
            vwap=2.49,
            extended_vwap=2.49,
            macd_cross_above=True,
            macd_above_signal=True,
            price_cross_above_vwap=True,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        16,
        _ProbeRuntime(True),
    )

    assert signal is None
    assert engine.pop_last_decision("UGRO") is None
    assert bool(engine._probe_state["UGRO"]["confirmed"]) is False


def test_entry_engine_pretrigger_reclaim_ignores_soft_fail_fast_by_default() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )
    engine._probe_state["UGRO"] = {
        "entry_bar": 15,
        "starter_qty": 25,
        "remaining_qty": 75,
        "probe_entry_price": 2.33,
        "resistance_level": 2.30,
        "hold_floor": 2.20,
        "pretrigger_score": 5,
        "pretrigger_score_details": "reclaim+",
        "confirmed": False,
        "confirm_reason": "",
        "starter_high": 2.34,
    }

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.31,
            price=2.29,
            high=2.32,
            low=2.28,
            volume=25_000,
            histogram=-0.002,
            histogram_prev=0.004,
            macd=0.010,
            macd_prev=0.015,
            signal=0.012,
            signal_prev=0.011,
            ema9=2.30,
            ema20=2.20,
            vwap=2.24,
            extended_vwap=2.24,
            macd_cross_above=False,
            macd_above_signal=False,
        ),
        16,
        _ProbeRuntime(True),
    )

    assert signal is None
    assert "UGRO" in engine._probe_state


def test_entry_engine_pretrigger_reclaim_accepts_leg_retrace_when_pct_pullback_is_shallow() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_max_extension_above_ema9_pct = 0.06
    config.pretrigger_reclaim_max_extension_above_vwap_pct = 0.06
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.pretrigger_reclaim_lookback_bars = 14
    config.pretrigger_reclaim_max_retrace_fraction_of_leg = 1.0
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.52,
            price=2.59,
            high=2.60,
            low=2.49,
            volume=33_000,
            histogram=0.016,
            histogram_prev=0.014,
            macd=0.030,
            macd_prev=0.028,
            signal=0.024,
            signal_prev=0.023,
            ema9=2.49,
            ema20=2.39,
            vwap=2.46,
            extended_vwap=2.46,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
    )

    assert signal is not None
    assert signal["action"] == "BUY"
    assert signal["path"] == "PRETRIGGER_RECLAIM"


def test_entry_engine_pretrigger_retest_arms_on_clean_retest_bar() -> None:
    config = TradingConfig().make_30s_retest_variant(quantity=100)
    engine = EntryEngine(
        config,
        name="Retest Bot",
        now_provider=lambda: datetime(2026, 4, 13, 9, 45),
    )

    _seed_retest_breakout_setup(engine)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.502,
            price=2.534,
            high=2.538,
            low=2.50,
            volume=31_000,
            histogram=0.018,
            histogram_prev=0.017,
            macd=0.031,
            macd_prev=0.030,
            signal=0.025,
            signal_prev=0.024,
            ema9=2.515,
            ema20=2.41,
            vwap=2.505,
            extended_vwap=2.505,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
    )

    assert signal is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "pending"
    assert decision["reason"] == "PRETRIGGER_RETEST_ARMED"


def test_entry_engine_pretrigger_retest_buys_only_on_next_bar_break() -> None:
    config = TradingConfig().make_30s_retest_variant(quantity=100)
    engine = EntryEngine(
        config,
        name="Retest Bot",
        now_provider=lambda: datetime(2026, 4, 13, 9, 45),
    )

    _seed_retest_breakout_setup(engine)

    armed = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.502,
            price=2.534,
            high=2.538,
            low=2.50,
            volume=31_000,
            histogram=0.018,
            histogram_prev=0.017,
            macd=0.031,
            macd_prev=0.030,
            signal=0.025,
            signal_prev=0.024,
            ema9=2.515,
            ema20=2.41,
            vwap=2.505,
            extended_vwap=2.505,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
    )
    assert armed is None
    assert engine.pop_last_decision("UGRO") is not None

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.54,
            price=2.572,
            high=2.576,
            low=2.535,
            volume=36_000,
            histogram=0.021,
            histogram_prev=0.018,
            macd=0.034,
            macd_prev=0.031,
            signal=0.026,
            signal_prev=0.025,
            ema9=2.525,
            ema20=2.42,
            vwap=2.515,
            extended_vwap=2.515,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        16,
    )

    assert signal is not None
    assert signal["action"] == "BUY"
    assert signal["path"] == "PRETRIGGER_RETEST_BREAK"



def test_entry_engine_pretrigger_reclaim_blocks_when_too_extended_from_both_anchors() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    for idx in range(14):
        engine._remember_bar(
            "UGRO",
            base_indicators(
                open=2.74 + (idx * 0.01),
                price=2.82 + (idx * 0.01),
                high=2.84 + (idx * 0.01),
                low=2.72 + (idx * 0.01),
                volume=20_000 + (idx * 500),
                histogram=0.010 + (idx * 0.001),
                histogram_prev=0.009 + (idx * 0.001),
                macd=0.020 + (idx * 0.001),
                macd_prev=0.019 + (idx * 0.001),
                signal=0.017 + (idx * 0.0008),
                signal_prev=0.016 + (idx * 0.0008),
                ema9=2.58 + (idx * 0.01),
                ema20=2.44 + (idx * 0.006),
                vwap=2.54 + (idx * 0.009),
                extended_vwap=2.54 + (idx * 0.009),
                macd_cross_above=False,
                price_cross_above_vwap=False,
                price_above_vwap=True,
                price_above_extended_vwap=True,
            ),
        )
    engine._recent_bars["UGRO"][-3]["low"] = 2.60
    engine._recent_bars["UGRO"][-2]["low"] = 2.59
    engine._recent_bars["UGRO"][-1]["low"] = 2.60

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.70,
            price=2.74,
            high=2.75,
            low=2.69,
            volume=34_000,
            histogram=0.020,
            histogram_prev=0.016,
            macd=0.034,
            macd_prev=0.031,
            signal=0.027,
            signal_prev=0.026,
            ema9=2.60,
            ema20=2.45,
            vwap=2.58,
            extended_vwap=2.58,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
    )

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["reason"] == "pretrigger reclaim too extended from EMA9/VWAP"


def test_entry_engine_pretrigger_reclaim_can_disable_location_gate() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.pretrigger_reclaim_require_location = False
    config.pretrigger_reclaim_score_threshold = 0
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.70,
            price=2.74,
            high=2.75,
            low=2.69,
            volume=34_000,
            histogram=0.020,
            histogram_prev=0.016,
            macd=0.034,
            macd_prev=0.031,
            signal=0.027,
            signal_prev=0.026,
            ema9=2.60,
            ema20=2.45,
            vwap=2.58,
            extended_vwap=2.58,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
    )

    assert signal is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["reason"] != "pretrigger reclaim location not ready"


def test_entry_engine_pretrigger_reclaim_blocks_weak_min_score_without_stoch_support() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.pretrigger_reclaim_require_stoch_for_min_score = True
    config.pretrigger_reclaim_max_extension_above_ema9_pct = 0.06
    config.pretrigger_reclaim_max_extension_above_vwap_pct = 0.06
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.52,
            price=2.59,
            high=2.60,
            low=2.50,
            volume=20_000,
            histogram=0.020,
            histogram_prev=0.016,
            macd=0.034,
            macd_prev=0.031,
            signal=0.027,
            signal_prev=0.026,
            ema9=2.485,
            ema20=2.385,
            vwap=2.455,
            extended_vwap=2.455,
            stoch_k=95.0,
            stoch_k_prev=92.0,
            stoch_k_prev2=88.0,
            stoch_d=93.0,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
    )

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["reason"] == "pretrigger reclaim minimum-score starter requires stoch support"


def test_entry_engine_pretrigger_reclaim_allows_strong_stoch_min_score_starter() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.pretrigger_reclaim_require_stoch_for_min_score = True
    config.pretrigger_reclaim_max_extension_above_ema9_pct = 0.06
    config.pretrigger_reclaim_max_extension_above_vwap_pct = 0.06
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.52,
            price=2.59,
            high=2.60,
            low=2.50,
            volume=20_000,
            histogram=0.020,
            histogram_prev=0.016,
            macd=0.034,
            macd_prev=0.031,
            signal=0.027,
            signal_prev=0.026,
            ema9=2.485,
            ema20=2.385,
            vwap=2.455,
            extended_vwap=2.455,
            stoch_k=65.0,
            stoch_k_prev=60.0,
            stoch_k_prev2=55.0,
            stoch_d=62.0,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
    )

    assert signal is not None
    assert signal["path"] == "PRETRIGGER_RECLAIM"
    assert signal["score"] == 2


def test_entry_engine_pretrigger_reclaim_reports_below_vwap_and_ema9_support() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.44,
            price=2.45,
            high=2.48,
            low=2.44,
            volume=31_000,
            histogram=0.015,
            histogram_prev=0.014,
            macd=0.028,
            macd_prev=0.027,
            signal=0.023,
            signal_prev=0.023,
            ema9=2.48,
            ema20=2.38,
            vwap=2.49,
            extended_vwap=2.49,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=False,
            price_above_extended_vwap=False,
        ),
        15,
    )

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["reason"] == "pretrigger reclaim below VWAP and EMA9 support"


def test_entry_engine_pretrigger_reclaim_allows_same_bar_touch_recovery_location() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.pretrigger_reclaim_allow_touch_recovery_location = True
    config.pretrigger_reclaim_score_threshold = 0
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.445,
            price=2.47,
            high=2.48,
            low=2.44,
            volume=31_000,
            histogram=0.015,
            histogram_prev=0.014,
            macd=0.028,
            macd_prev=0.027,
            signal=0.023,
            signal_prev=0.023,
            ema9=2.48,
            ema20=2.38,
            vwap=2.49,
            extended_vwap=2.49,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=False,
            price_above_extended_vwap=False,
        ),
        15,
    )

    assert signal is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "pending"
    assert decision["reason"] == "PRETRIGGER_RECLAIM_ARMED"


def test_entry_engine_pretrigger_reclaim_allows_single_anchor_location_when_near_other_anchor() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.pretrigger_reclaim_allow_single_anchor_location = True
    config.pretrigger_reclaim_score_threshold = 0
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.468,
            price=2.485,
            high=2.49,
            low=2.46,
            volume=31_000,
            histogram=0.015,
            histogram_prev=0.014,
            macd=0.028,
            macd_prev=0.027,
            signal=0.023,
            signal_prev=0.023,
            ema9=2.48,
            ema20=2.38,
            vwap=2.50,
            extended_vwap=2.50,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=False,
            price_above_extended_vwap=False,
        ),
        15,
    )

    assert signal is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "pending"
    assert decision["reason"] == "PRETRIGGER_RECLAIM_ARMED"


def test_entry_engine_pretrigger_reclaim_blocks_single_anchor_location_without_strong_reclaim_candle() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.pretrigger_reclaim_allow_single_anchor_location = True
    config.pretrigger_reclaim_score_threshold = 0
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.468,
            price=2.477,
            high=2.49,
            low=2.46,
            volume=31_000,
            histogram=0.015,
            histogram_prev=0.014,
            macd=0.028,
            macd_prev=0.027,
            signal=0.023,
            signal_prev=0.023,
            ema9=2.475,
            ema20=2.38,
            vwap=2.495,
            extended_vwap=2.495,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=False,
            price_above_extended_vwap=False,
        ),
        15,
    )

    assert signal is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["reason"] == "pretrigger reclaim single-anchor candle too weak"


def test_entry_engine_pretrigger_reclaim_arms_single_anchor_location_when_dual_anchor_required_for_starter() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.pretrigger_reclaim_allow_single_anchor_location = True
    config.pretrigger_reclaim_require_dual_anchor_for_starter = True
    config.pretrigger_reclaim_score_threshold = 0
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.468,
            price=2.485,
            high=2.49,
            low=2.46,
            volume=31_000,
            histogram=0.015,
            histogram_prev=0.014,
            macd=0.028,
            macd_prev=0.027,
            signal=0.023,
            signal_prev=0.023,
            ema9=2.48,
            ema20=2.38,
            vwap=2.50,
            extended_vwap=2.50,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=False,
            price_above_extended_vwap=False,
        ),
        15,
    )

    assert signal is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["status"] == "pending"
    assert decision["reason"] == "PRETRIGGER_RECLAIM_ARMED"


def test_entry_engine_pretrigger_reclaim_blocks_when_pullback_loses_prespike_support() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_require_higher_low = True
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )
    _seed_reclaim_pullback_setup(engine)

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.47,
            price=2.50,
            high=2.515,
            low=2.46,
            volume=31_000,
            histogram=0.016,
            histogram_prev=0.014,
            macd=0.029,
            macd_prev=0.028,
            signal=0.023,
            signal_prev=0.023,
            ema9=2.485,
            ema20=2.385,
            vwap=2.455,
            extended_vwap=2.455,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
    )

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["reason"].startswith("pretrigger reclaim higher low failed:")


def test_entry_engine_pretrigger_reclaim_blocks_when_pullback_loses_too_much_spike_gain() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = True
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )
    _seed_reclaim_pullback_setup(engine)

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.47,
            price=2.50,
            high=2.515,
            low=2.46,
            volume=31_000,
            histogram=0.016,
            histogram_prev=0.014,
            macd=0.029,
            macd_prev=0.028,
            signal=0.023,
            signal_prev=0.023,
            ema9=2.485,
            ema20=2.385,
            vwap=2.455,
            extended_vwap=2.455,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
    )

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["reason"].startswith("pretrigger reclaim held move failed:")


def test_entry_engine_pretrigger_reclaim_blocks_when_pullback_volume_is_not_absorbed() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = True
    config.pretrigger_reclaim_require_held_move = False
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )
    _seed_reclaim_pullback_setup(engine)

    blocked = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.47,
            price=2.50,
            high=2.515,
            low=2.46,
            volume=31_000,
            histogram=0.016,
            histogram_prev=0.014,
            macd=0.029,
            macd_prev=0.028,
            signal=0.023,
            signal_prev=0.023,
            ema9=2.485,
            ema20=2.385,
            vwap=2.455,
            extended_vwap=2.455,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
    )

    assert blocked is None
    decision = engine.pop_last_decision("UGRO")
    assert decision is not None
    assert decision["reason"].startswith("pretrigger reclaim pullback volume too heavy:")


def test_entry_engine_pretrigger_reclaim_can_disable_momentum_gate() -> None:
    config = TradingConfig().make_30s_reclaim_variant(quantity=100)
    config.pretrigger_reclaim_lookback_bars = 14
    config.pretrigger_reclaim_max_extension_above_ema9_pct = 0.06
    config.pretrigger_reclaim_max_extension_above_vwap_pct = 0.06
    config.pretrigger_reclaim_require_higher_low = False
    config.pretrigger_reclaim_require_pullback_absorption = False
    config.pretrigger_reclaim_require_held_move = False
    config.pretrigger_reclaim_require_momentum = False
    engine = EntryEngine(
        config,
        name="Reclaim Bot",
        now_provider=lambda: datetime(2026, 4, 2, 8, 35),
    )

    _seed_reclaim_pullback_setup(engine)

    signal = engine.check_entry(
        "UGRO",
        base_indicators(
            open=2.52,
            price=2.59,
            high=2.60,
            low=2.50,
            volume=31_000,
            histogram=-0.002,
            histogram_prev=0.001,
            macd=0.020,
            macd_prev=0.021,
            signal=0.023,
            signal_prev=0.023,
            ema9=2.485,
            ema20=2.385,
            vwap=2.455,
            extended_vwap=2.455,
            macd_cross_above=False,
            price_cross_above_vwap=False,
            price_above_vwap=True,
            price_above_extended_vwap=True,
        ),
        15,
    )

    assert signal is not None
    assert signal["path"] == "PRETRIGGER_RECLAIM"
