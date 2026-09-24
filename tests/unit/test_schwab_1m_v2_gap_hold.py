from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    SchwabV2Strategy,
    TradeIntentDraft,
)

ET = ZoneInfo("America/New_York")
MIDDAY_MS = int(datetime(2026, 9, 23, 12, 0, tzinfo=ET).timestamp() * 1000)


class _Emitter:
    def __init__(self) -> None:
        self.drafts: list[TradeIntentDraft] = []

    async def emit(self, draft: TradeIntentDraft) -> None:
        self.drafts.append(draft)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "strategy_schwab_1m_v2_gap_hold_enabled": True,
        "strategy_schwab_1m_v2_gap_hold_detect_seconds": 90.0,
        "strategy_schwab_1m_v2_atr_flip_enabled": True,
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
        "strategy_schwab_1m_v2_webull_resting_mirror_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def _chart_bar(ts: int, close: float, *, volume: int = 25_000) -> ChartBar:
    return ChartBar(
        symbol="BENF",
        open=close - 0.03,
        high=close + 0.08,
        low=close - 0.06,
        close=close,
        volume=volume,
        timestamp_ms=ts,
    )


def _ohlcv(bar: ChartBar) -> OHLCVBar:
    return OHLCVBar(
        timestamp_ms=bar.timestamp_ms,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


def _draft(intent_type: str) -> TradeIntentDraft:
    return TradeIntentDraft(
        symbol="BENF",
        side="buy" if intent_type == "open" else "sell",
        intent_type=intent_type,
        quantity=Decimal("1"),
        reason="schwab_1m_v2 ATR Flip gap-hold control",
    )


def _begin_hold(strategy: SchwabV2Strategy, *, detected_at_ms: int = MIDDAY_MS) -> None:
    strategy._now_ms = lambda: detected_at_ms
    assert strategy.begin_gap_hold(
        "BENF",
        detected_at_ms=detected_at_ms,
        last_bar_age_s=91.0,
        last_print_age_s=5.0,
    )


def test_flag_off_is_inert() -> None:
    strategy = SchwabV2Strategy(
        Settings(strategy_schwab_1m_v2_gap_hold_enabled=False)
    )
    state = strategy.watchlist_state("BENF")
    state.resting_active = True

    assert not strategy.begin_gap_hold(
        "BENF",
        detected_at_ms=MIDDAY_MS,
        last_bar_age_s=91.0,
        last_print_age_s=5.0,
    )
    assert state.gap_hold_active is False
    assert state.resting_active is True


def _seed_last_bar(bot: SchwabV2BotService, symbol: str, stamp_ms: int) -> None:
    state = bot.strategy.watchlist_state(symbol)
    state.bars.append(
        OHLCVBar(
            timestamp_ms=stamp_ms,
            open=2.5,
            high=2.6,
            low=2.4,
            close=2.5,
            volume=20_000,
        )
    )
    state.resting_active = True
    state.resting_is_broker_order = True
    state.webull_resting_active = True
    state.resting_level = 2.58
    state.resting_trigger = 2.58


@pytest.mark.asyncio
async def test_clock_detect_holds_and_cancels_both_legs_while_quiet_stock_does_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Age is measured from the bar's CLOSE (start + 60 s), never from its START stamp.

    2026-09-24 production: a 1-minute bar is stamped at its start and delivered ~1 s after
    its close, so a HEALTHY stream reads 60-120 s "old" against the start stamp and a 90 s
    threshold tripped every minute at :30 on every watchlist name (GAPHOLD held everything).
    HEALTHY below is that exact state: stamped 91 s ago = closed 31 s ago = one bar late at
    most, prints fresh. It must NOT be held. BENF is a real hole: closed 91 s ago (the next
    bar is 31 s overdue) while the tape still prints.
    """
    bot = SchwabV2BotService(_settings())
    bot._watchlist = {"BENF", "QUIET", "HEALTHY"}
    primary = _Emitter()
    webull = _Emitter()
    bot.intent_emitter = primary  # type: ignore[assignment]
    bot.webull_intent_emitter = webull  # type: ignore[assignment]

    _seed_last_bar(bot, "BENF", MIDDAY_MS - 151_000)  # closed 91 s ago
    _seed_last_bar(bot, "QUIET", MIDDAY_MS - 151_000)  # closed 91 s ago, no prints since
    _seed_last_bar(bot, "HEALTHY", MIDDAY_MS - 91_000)  # closed 31 s ago = normal cadence
    bot._gap_last_print_at_ms["BENF"] = MIDDAY_MS - 5_000
    bot._gap_last_print_at_ms["QUIET"] = MIDDAY_MS - 151_000
    bot._gap_last_print_at_ms["HEALTHY"] = MIDDAY_MS - 5_000
    caplog.set_level(logging.INFO)

    held = await bot._evaluate_gap_holds(datetime.fromtimestamp(MIDDAY_MS / 1000, UTC))

    assert held == ["BENF"]
    assert bot.strategy.gap_hold_active("BENF")
    assert not bot.strategy.gap_hold_active("QUIET")
    assert not bot.strategy.gap_hold_active("HEALTHY")
    assert [draft.symbol for draft in primary.drafts] == ["BENF"]
    assert [draft.intent_type for draft in primary.drafts] == ["cancel"]
    assert [draft.intent_type for draft in webull.drafts] == ["cancel"]
    assert primary.drafts[0].metadata["reason"] == "bar_gap"
    assert webull.drafts[0].metadata["reason"] == "bar_gap"
    detect_lines = [m for m in caplog.messages if "[V2-GAP-DETECT]" in m]
    assert len(detect_lines) == 1 and "BENF" in detect_lines[0]
    assert "last_bar_close_age_s=91.0" in detect_lines[0]
    assert any("[V2-GAP-HOLD] BENF" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_healthy_stream_is_never_held_across_a_full_minute() -> None:
    """Sweep the whole delivery cycle: a bar stamped T, delivered at T+61 s, is the newest
    bar for every clock second until T+121 s. No second of that cycle may detect a gap."""
    bot = SchwabV2BotService(_settings())
    bot._watchlist = {"BENF"}
    bot.intent_emitter = _Emitter()  # type: ignore[assignment]
    bot.webull_intent_emitter = _Emitter()  # type: ignore[assignment]
    _seed_last_bar(bot, "BENF", MIDDAY_MS)
    for age_s in range(61, 122):
        now_ms = MIDDAY_MS + age_s * 1000
        bot._gap_last_print_at_ms["BENF"] = now_ms - 1_000
        held = await bot._evaluate_gap_holds(datetime.fromtimestamp(now_ms / 1000, UTC))
        assert held == [], f"healthy stream held at start-age {age_s}s"
    assert not bot.strategy.gap_hold_active("BENF")


@pytest.mark.asyncio
async def test_first_minutes_of_the_entry_window_are_not_a_gap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """07:00 ET: Schwab sends no bars before 07:00 (the known blind window), so every name's
    newest bar is YESTERDAY's while the tape prints. That is the session's start, not a hole
    inside a series — holding it would cost the first 10+ minutes of the window on every name.
    A symbol with no live bar in the current 04:00 ET session is skipped (once, logged)."""
    bot = SchwabV2BotService(_settings())
    bot._watchlist = {"BENF"}
    bot.intent_emitter = _Emitter()  # type: ignore[assignment]
    bot.webull_intent_emitter = _Emitter()  # type: ignore[assignment]
    yesterday_close = int(datetime(2026, 9, 22, 19, 59, tzinfo=ET).timestamp() * 1000)
    seven_am = int(datetime(2026, 9, 23, 7, 0, 3, tzinfo=ET).timestamp() * 1000)
    _seed_last_bar(bot, "BENF", yesterday_close)
    bot._gap_last_print_at_ms["BENF"] = seven_am - 700
    caplog.set_level(logging.INFO)

    for offset_s in (0, 5, 60):
        now_ms = seven_am + offset_s * 1000
        bot._gap_last_print_at_ms["BENF"] = now_ms - 700
        held = await bot._evaluate_gap_holds(datetime.fromtimestamp(now_ms / 1000, UTC))
        assert held == []
    assert not bot.strategy.gap_hold_active("BENF")
    skip_lines = [m for m in caplog.messages if "[V2-GAP-DETECT-SKIP] BENF" in m]
    assert len(skip_lines) == 1  # throttled: once per symbol per session
    assert "reason=no_live_bar_this_session" in skip_lines[0]

    # The first live bar of the session (07:08 ET, closed 07:09) arms the detector: a
    # hole after it, with the tape printing, is a real gap again.
    first_bar = int(datetime(2026, 9, 23, 7, 8, tzinfo=ET).timestamp() * 1000)
    bot.strategy.watchlist_state("BENF").bars.append(
        OHLCVBar(
            timestamp_ms=first_bar, open=2.5, high=2.6, low=2.4, close=2.5, volume=20_000
        )
    )
    later = first_bar + 60_000 + 91_000  # closed 91 s ago
    bot._gap_last_print_at_ms["BENF"] = later - 1_000
    held = await bot._evaluate_gap_holds(datetime.fromtimestamp(later / 1000, UTC))
    assert held == ["BENF"]


@pytest.mark.asyncio
async def test_hold_blocks_only_opens_and_never_blocks_an_exit() -> None:
    bot = SchwabV2BotService(_settings())
    emitter = _Emitter()
    bot.intent_emitter = emitter  # type: ignore[assignment]
    _begin_hold(bot.strategy)

    assert await bot._maybe_emit(_draft("open")) == "dropped_routing"
    assert await bot._maybe_emit(_draft("close")) == "queued"
    assert [draft.intent_type for draft in emitter.drafts] == ["close"]


def test_resume_reseeds_atr_from_only_ten_contiguous_live_bars(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = SchwabV2Strategy(_settings())
    strategy._now_ms = lambda: MIDDAY_MS + 20 * 60_000
    state = strategy.watchlist_state("BENF")
    state.atr_session_anchor_ms = MIDDAY_MS - 8 * 60 * 60_000
    state.atr_wilders = 99.0
    state.atr_trail = 101.0
    state.atr_state = "short"
    state.atr_prev_bar = OHLCVBar(
        timestamp_ms=MIDDAY_MS - 10 * 60_000,
        open=10.0,
        high=20.0,
        low=1.0,
        close=10.0,
        volume=1,
    )
    _begin_hold(strategy)

    bars = [
        _chart_bar(MIDDAY_MS + index * 60_000, 2.00 + index * 0.03)
        for index in range(10)
    ]
    caplog.set_level(logging.INFO)
    for bar in bars[:9]:
        strategy.on_observed_bar("BENF", bar, observation_phase="live")
    assert state.gap_hold_active is True
    strategy.on_observed_bar("BENF", bars[9], observation_phase="live")

    fresh = SchwabV2Strategy(
        _settings(strategy_schwab_1m_v2_gap_hold_enabled=False)
    )
    fresh_state = fresh.watchlist_state("BENF")
    for bar in bars:
        fresh._update_atr_state(
            fresh_state,
            _ohlcv(bar),
            observation_phase="live",
            state_only=True,
        )

    assert state.gap_hold_active is False
    assert state.gap_hold_contiguous_bars == 10
    assert state.atr_wilders == pytest.approx(fresh_state.atr_wilders)
    assert state.atr_trail == pytest.approx(fresh_state.atr_trail)
    assert state.atr_trail != 101.0
    assert any(
        "[V2-GAP-RESUME] BENF contiguous_bars=10" in message
        for message in caplog.messages
    )


def test_new_gap_restarts_count_and_resume_count_follows_period() -> None:
    strategy = SchwabV2Strategy(_settings())
    strategy._now_ms = lambda: MIDDAY_MS + 30 * 60_000
    _begin_hold(strategy)

    for index in range(7):
        strategy.on_observed_bar(
            "BENF",
            _chart_bar(MIDDAY_MS + index * 60_000, 2.0 + index * 0.02),
            observation_phase="live",
        )
    for index in range(4):
        strategy.on_observed_bar(
            "BENF",
            _chart_bar(MIDDAY_MS + (8 + index) * 60_000, 2.2 + index * 0.02),
            observation_phase="live",
        )
    state = strategy.watchlist_state("BENF")
    assert state.gap_hold_active is True
    assert state.gap_hold_contiguous_bars == 4

    for index in range(4, 10):
        strategy.on_observed_bar(
            "BENF",
            _chart_bar(MIDDAY_MS + (8 + index) * 60_000, 2.2 + index * 0.02),
            observation_phase="live",
        )
    assert state.gap_hold_active is False
    assert state.gap_hold_contiguous_bars == 10

    period_three = SchwabV2Strategy(
        _settings(strategy_schwab_1m_v2_atr_flip_period=3)
    )
    period_three._now_ms = lambda: MIDDAY_MS + 30 * 60_000
    _begin_hold(period_three)
    for index in range(5):
        period_three.on_observed_bar(
            "BENF",
            _chart_bar(MIDDAY_MS + index * 60_000, 3.0 + index * 0.02),
            observation_phase="live",
        )
    assert period_three.gap_hold_active("BENF")
    period_three.on_observed_bar(
        "BENF",
        _chart_bar(MIDDAY_MS + 5 * 60_000, 3.1),
        observation_phase="live",
    )
    assert not period_three.gap_hold_active("BENF")


def test_benf_stored_sparse_bars_never_resume_at_1246_or_1253() -> None:
    """Production strategy_bar_history rows for BENF on 2026-09-23, 12:03-12:53 ET."""

    stored = (
        (1790179380000, 3.09, 3.15, 2.94, 2.94, 1_562_387),
        (1790179440000, 2.94, 2.94, 2.94, 2.94, 34_109),
        (1790180640000, 2.24, 2.39, 2.09, 2.09, 1_027_977),
        (1790181240000, 1.90, 2.04, 1.75, 1.955, 1_074_045),
        (1790181300000, 1.9501, 2.08, 1.95, 2.08, 1_028_937),
        (1790181900000, 2.19, 2.29, 2.17, 2.28, 398_625),
        (1790181960000, 2.2798, 2.35, 2.18, 2.3456, 1_213_907),
        (1790182020000, 2.35, 2.43, 2.31, 2.43, 489_503),
        (1790182320000, 2.50, 2.51, 2.35, 2.4601, 495_769),
        (1790182380000, 2.465, 2.59, 2.33, 2.33, 1_299_846),
    )
    strategy = SchwabV2Strategy(_settings())
    strategy._now_ms = lambda: MIDDAY_MS + 60 * 60_000
    for ts, open_px, high, low, close, volume in stored[:2]:
        strategy.on_observed_bar(
            "BENF",
            ChartBar(
                symbol="BENF",
                open=open_px,
                high=high,
                low=low,
                close=close,
                volume=volume,
                timestamp_ms=ts,
            ),
            observation_phase="live",
        )
    _begin_hold(strategy, detected_at_ms=MIDDAY_MS + 5 * 60_000)
    held_at: dict[int, bool] = {}
    opens_at: dict[int, list[TradeIntentDraft]] = {}
    for ts, open_px, high, low, close, volume in stored[2:]:
        strategy.on_observed_bar(
            "BENF",
            ChartBar(
                symbol="BENF",
                open=open_px,
                high=high,
                low=low,
                close=close,
                volume=volume,
                timestamp_ms=ts,
            ),
            observation_phase="live",
        )
        held_at[int(ts)] = strategy.gap_hold_active("BENF")
        opens_at[int(ts)] = [
            draft
            for draft in strategy.drain_pending_intents()
            if draft.intent_type == "open"
        ]

    assert held_at[1790181960000] is True  # 12:46 ET
    assert held_at[1790182380000] is True  # 12:53 ET
    assert opens_at[1790181960000] == []
    assert opens_at[1790182380000] == []
