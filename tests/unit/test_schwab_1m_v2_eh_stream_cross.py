"""PRE-MARKET stream cross for the EH soft rest (operator 2026-09-28, CLRO 07:24 ET).

The EH soft rest only saw REST quote polls (every 5 s). CLRO's tape printed 5.59 at 07:24:58.121
and 5.5689 at 07:24:59.992 against a 5.5590 trigger; v2 received the 5.59 update at 07:24:58.491,
but no poll landed on either spike, so the cross never fired and the flip was lost.

The fix offers each STREAMED trade print to the SAME `_eh_resting_cross_check`. These tests replay
CLRO's real prices and pin that every existing guard still holds, and that nothing outside
pre-market is touched (operator: no regular-market change).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.market_data.schwab_v2_streamer import SchwabTick, SchwabV2Streamer
from project_mai_tai.services import schwab_1m_v2_bot as bot_module
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

_ET = ZoneInfo("America/New_York")


def _ms(h: int, m: int, s: int = 0, us: int = 0) -> int:
    return int(datetime(2026, 9, 28, h, m, s, us, tzinfo=_ET).timestamp() * 1000)


# CLRO 2026-09-28, from market_quote_ticks / market_trade_ticks and the v2 log.
LINE = 5.531361    # [V2-ATR-PROBE] trail=5.531361; [V2-RESTING-EH-ARM] 07:20:02 logs line=5.5314 trigger=5.5590
BAR_0723 = _ms(7, 23)               # last completed bar before the flip bar (logged 07:24:02)
ARMED_AT = _ms(7, 20, 2)
PRINT_1_TS = _ms(7, 24, 58, 121_000)    # 5.59   (field 35 trade time)
PRINT_1_RCV = _ms(7, 24, 58, 491_000)   # received_at
PRINT_2_TS = _ms(7, 24, 59, 992_000)    # 5.5689
PRINT_2_RCV = _ms(7, 25, 0, 498_000)


def _settings(**overrides) -> Settings:
    kwargs = {
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct": 0.5,   # live /proc value
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _armed_clro(strat: SchwabV2Strategy, *, bar_ts: int = BAR_0723):
    """Arm CLRO's soft rest exactly as production did (EH, line 5.5314, trigger 5.5590)."""
    strat._resting_session_is_eh = lambda now=None: True
    strat._resting_in_window = lambda now=None: True
    strat._now_ms = lambda: ARMED_AT
    st = strat.watchlist_state("CLRO")
    # The 07:19 bar the rest actually armed from ([V2-RESTING-EH-ARM] 07:20:02, state_age=6).
    st.bars.append(OHLCVBar(timestamp_ms=_ms(7, 19), open=5.24, high=5.34, low=5.24,
                            close=5.33, volume=17_480))
    strat._cw_v2_resting_track(
        st,
        {"touch": False, "touch_price": None, "flip": None, "flip_level": None,
         "trail": LINE, "loss": 0.30, "state": "short", "state_age": 6},
    )
    assert strat.drain_pending_intents() == []       # soft rest: nothing sent to the broker
    assert st.resting_active is True and st.resting_is_broker_order is False
    assert st.resting_trigger == pytest.approx(5.5590, abs=1e-4)
    # The last completed bar when the spike printed (07:23, logged 07:24:02).
    st.bars.append(OHLCVBar(timestamp_ms=bar_ts, open=5.30, high=5.362, low=5.30,
                            close=5.31, volume=7_846))
    return st


def _at(strat: SchwabV2Strategy, now_ms: int) -> None:
    strat._now_ms = lambda: now_ms


# ------------------------------------------------------------------ the CLRO miss, replayed
def test_clro_rest_polls_either_side_of_the_spike_never_cross() -> None:
    """The defect as it happened: polls landing either side of the spike stay below the trigger."""
    strat = SchwabV2Strategy(_settings())
    _armed_clro(strat)
    _at(strat, _ms(7, 24, 55, 482_000))
    assert strat.on_quote("CLRO", Quote("CLRO", 5.44, 5.47, 5.46, _ms(7, 24, 55), 0)) is None
    _at(strat, PRINT_2_RCV)
    assert strat.on_quote("CLRO", Quote("CLRO", 5.53, 5.57, 5.5362, _ms(7, 24, 59), 0)) is None


def test_clro_second_print_with_recorded_ask_below_trigger_does_not_cross() -> None:
    """Operator 10-05: the recorded 5.55 ask, not the old 5.57 proxy, rules out this print."""
    strat = SchwabV2Strategy(_settings(strategy_schwab_1m_v2_pm_print_ask_confirm_enabled=True))
    st = _armed_clro(strat)
    _at(strat, PRINT_1_RCV)
    assert strat.on_stream_trade("CLRO", 5.59, PRINT_1_TS, ask_price=5.59) is None
    assert st.resting_flip_ms == 0
    _at(strat, PRINT_2_RCV)
    assert strat.on_stream_trade("CLRO", 5.5689, PRINT_2_TS, ask_price=5.55) is None
    assert st.resting_flip_ms == 0
    assert st.position_qty == 0 and st.position_qty_held == 0  # draft is not a fill


def test_second_spike_and_later_rest_quote_do_not_emit_again() -> None:
    strat = SchwabV2Strategy(_settings())
    _armed_clro(strat)
    _at(strat, PRINT_1_RCV)
    assert strat.on_stream_trade("CLRO", 5.59, PRINT_1_TS, ask_price=5.59) is None
    _at(strat, PRINT_2_RCV)
    assert strat.on_stream_trade("CLRO", 5.5689, PRINT_2_TS, ask_price=5.57) is not None
    assert strat.on_stream_trade("CLRO", 5.5689, PRINT_2_TS, ask_price=5.57) is None
    assert strat.on_quote("CLRO", Quote("CLRO", 5.55, 5.59, 5.59, PRINT_2_RCV, 0)) is None


def test_rest_cross_first_blocks_the_stream_print() -> None:
    """Whichever source sees the cross first wins; the other can never double the entry."""
    strat = SchwabV2Strategy(_settings())
    _armed_clro(strat)
    _at(strat, PRINT_1_RCV)
    assert strat.on_quote("CLRO", Quote("CLRO", 5.55, 5.59, 5.59, PRINT_1_RCV, 0)) is not None
    assert strat.on_stream_trade("CLRO", 5.59, PRINT_1_TS, ask_price=5.57) is None


def test_print_below_trigger_does_not_emit() -> None:
    strat = SchwabV2Strategy(_settings())
    st = _armed_clro(strat)
    _at(strat, PRINT_1_RCV)
    assert strat.on_stream_trade("CLRO", 5.5589, PRINT_1_TS, ask_price=5.57) is None
    assert st.resting_flip_ms == 0


def test_missing_or_out_of_band_ask_does_not_take_the_latch() -> None:
    strat = SchwabV2Strategy(_settings())
    st = _armed_clro(strat)
    _at(strat, PRINT_1_RCV)
    assert strat.on_stream_trade("CLRO", 5.59, PRINT_1_TS, ask_price=None) is None
    assert strat.on_stream_trade("CLRO", 5.59, PRINT_1_TS, ask_price=5.59) is None
    assert st.resting_flip_ms == 0


def test_ask_exactly_at_cap_is_eligible() -> None:
    strat = SchwabV2Strategy(_settings())
    st = _armed_clro(strat)
    _at(strat, PRINT_2_RCV)
    cap = st.resting_trigger * (1 + strat._resting_band_pct_value() / 100)
    assert strat.on_stream_trade("CLRO", 5.5689, PRINT_2_TS, ask_price=cap) is not None
    assert st.position_qty == 0


# ------------------------------------------------------------------ scope: pre-market only
@pytest.mark.parametrize(
    ("now_ms", "emits"),
    [
        (_ms(9, 29, 59, 900_000), True),     # last pre-market instant
        (_ms(9, 30), False),                 # regular session: never
        (_ms(11, 0), False),
        (_ms(16, 5), False),                 # post-market: never
    ],
)
def test_pre_market_only_boundary(now_ms: int, emits: bool) -> None:
    """Even with the EH session seam forced True, the stream path refuses at/after 09:30 ET."""
    strat = SchwabV2Strategy(_settings())
    _armed_clro(strat, bar_ts=now_ms - 90_000)            # keep the bar-age guard satisfied
    _at(strat, now_ms)
    got = strat.on_stream_trade("CLRO", 5.59, now_ms - 300, ask_price=5.57)
    assert (got is not None) is emits


def test_stream_path_never_reaches_rth_fanout_or_quote_entry(monkeypatch) -> None:
    strat = SchwabV2Strategy(_settings())
    st = _armed_clro(strat)
    before = st.last_quote

    def _boom(*_a, **_k):
        raise AssertionError("the stream path must not reach a regular-market entry path")

    monkeypatch.setattr(strat, "_fanout_rth_resting_cross", _boom)
    monkeypatch.setattr(strat, "_cw_v2_quote", _boom)
    _at(strat, PRINT_1_RCV)
    assert strat.on_stream_trade("CLRO", 5.59, PRINT_1_TS, ask_price=5.57) is not None
    assert st.last_quote is before                        # REST quote cache untouched


# ------------------------------------------------------------------ every existing guard still holds
@pytest.mark.parametrize(("age_ms", "emits"), [(3000, True), (3001, False), (-3001, False)])
def test_print_age_threshold_is_pinned(age_ms: int, emits: bool) -> None:
    strat = SchwabV2Strategy(_settings())
    _armed_clro(strat)
    _at(strat, PRINT_1_RCV)
    got = strat.on_stream_trade("CLRO", 5.59, PRINT_1_RCV - age_ms, ask_price=5.57)
    assert (got is not None) is emits


def test_stale_bar_still_blocks_the_cross() -> None:
    """The REST path's bar-age bound (180 s from the last bar's stamp) is not bypassed."""
    strat = SchwabV2Strategy(_settings())
    _armed_clro(strat, bar_ts=PRINT_1_RCV - 181_000)
    _at(strat, PRINT_1_RCV)
    assert strat.on_stream_trade("CLRO", 5.59, PRINT_1_TS, ask_price=5.57) is None


def test_gap_hold_blocks_the_stream_print() -> None:
    strat = SchwabV2Strategy(_settings(strategy_schwab_1m_v2_gap_hold_enabled=True))
    st = _armed_clro(strat)
    st.gap_hold_active = True
    _at(strat, PRINT_1_RCV)
    assert strat.on_stream_trade("CLRO", 5.59, PRINT_1_TS, ask_price=5.57) is None


def test_boot_hold_blocks_the_stream_print() -> None:
    strat = SchwabV2Strategy(_settings())
    _armed_clro(strat)
    strat._entries_held = True
    _at(strat, PRINT_1_RCV)
    assert strat.on_stream_trade("CLRO", 5.59, PRINT_1_TS, ask_price=5.57) is None


def test_no_armed_rest_and_unknown_symbol_are_ignored() -> None:
    strat = SchwabV2Strategy(_settings())
    strat.watchlist_state("CLRO")                         # watched, nothing armed
    _at(strat, PRINT_1_RCV)
    assert strat.on_stream_trade("CLRO", 5.59, PRINT_1_TS, ask_price=5.57) is None
    assert strat.on_stream_trade("ZZZZ", 5.59, PRINT_1_TS, ask_price=5.57) is None
    assert "ZZZZ" not in strat._symbol_states             # never creates a state


@pytest.mark.parametrize(
    "overrides",
    [
        {"strategy_schwab_1m_v2_eh_resting_stream_cross_enabled": False},
        {"strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled": False},
    ],
)
def test_flag_off_is_inert(overrides) -> None:
    strat = SchwabV2Strategy(_settings(**overrides))
    st = strat.watchlist_state("CLRO")
    st.resting_active = True
    st.resting_level = LINE
    st.resting_trigger = 5.5590
    st.bars.append(OHLCVBar(timestamp_ms=BAR_0723, open=5.3, high=5.36, low=5.3,
                            close=5.31, volume=1))
    strat._resting_session_is_eh = lambda now=None: True
    _at(strat, PRINT_1_RCV)
    assert strat.on_stream_trade("CLRO", 5.59, PRINT_1_TS, ask_price=5.57) is None


# ------------------------------------------------------------------ bot wiring
def _bot(monkeypatch, **overrides):
    bot = SchwabV2BotService(_settings(**overrides))
    written: list[SchwabTick] = []
    emitted: list[object] = []

    class _Writer:
        async def on_tick(self, tick):
            written.append(tick)

    async def _emit(draft, emitter=None):
        emitted.append(draft)
        return "emitted"

    async def _no_legs(*, legs=None):
        return None

    bot.tick_writer = _Writer()
    monkeypatch.setattr(bot, "_maybe_emit", _emit)
    monkeypatch.setattr(bot, "_emit_webull_fanout_legs", _no_legs)
    return bot, written, emitted


def _trade(price: float, ts: int, *, ask: float = 5.57) -> SchwabTick:
    return SchwabTick("trade", "LEVELONE_EQUITIES", "CLRO", ts,
                      {"3": price, "35": ts, "2": ask}, f"t{ts}", price=price)


async def _send_levelone(bot: SchwabV2BotService, content: dict, received_ms: int) -> None:
    for tick in SchwabV2Streamer._extract_level_one_ticks(content, received_ms):
        await bot._handle_stream_tick(tick)


@pytest.mark.asyncio
async def test_levelone_recycled_last_without_field_35_never_enters(monkeypatch) -> None:
    """Exercise the real parser: a quote update can synthesize a trade at message time."""
    bot, written, emitted = _bot(monkeypatch)
    st = _armed_clro(bot.strategy)
    _at(bot.strategy, PRINT_2_RCV)
    ticks = SchwabV2Streamer._extract_level_one_ticks(
        {"0": "CLRO", "1": 5.54, "2": 5.57, "3": 5.5689}, PRINT_2_RCV
    )
    assert [tick.kind for tick in ticks] == ["trade", "quote"]
    for tick in ticks:
        await bot._handle_stream_tick(tick)
    await asyncio.sleep(0)
    assert len(written) == 2
    assert emitted == [] and st.resting_flip_ms == 0


@pytest.mark.asyncio
async def test_recycled_last_is_refused_even_with_a_fresh_remembered_ask(monkeypatch) -> None:
    """Field 35 is required even when every other entry precondition is satisfied."""
    bot, _written, emitted = _bot(monkeypatch)
    st = _armed_clro(bot.strategy)
    _at(bot.strategy, PRINT_2_RCV)
    await _send_levelone(bot, {"0": "CLRO", "2": 5.55}, PRINT_2_RCV - 100)
    assert bot._eh_stream_ask_by_symbol["CLRO"][0] == 5.55
    # A LEVELONE quote delta can carry recycled field 3 without a new trade time.
    await _send_levelone(bot, {"0": "CLRO", "1": 5.52, "3": 5.5689}, PRINT_2_RCV)
    await asyncio.sleep(0)
    assert emitted == [] and st.resting_flip_ms == 0


@pytest.mark.asyncio
async def test_trade_delta_uses_fresh_remembered_ask_once(monkeypatch) -> None:
    bot, _written, emitted = _bot(monkeypatch)
    st = _armed_clro(bot.strategy)
    _at(bot.strategy, PRINT_2_RCV)
    await _send_levelone(bot, {"0": "CLRO", "2": 5.55}, PRINT_2_RCV - 100)
    # Production-shaped trade: field 2 is absent because the ask did not change.
    trade = {"0": "CLRO", "1": 5.52, "3": 5.5689, "35": PRINT_2_TS}
    await _send_levelone(bot, trade, PRINT_2_RCV)
    await _send_levelone(bot, trade, PRINT_2_RCV)
    await asyncio.sleep(0)
    assert len(emitted) == 1 and st.resting_flip_ms == PRINT_2_RCV
    assert st.position_qty == 0 and st.position_qty_held == 0


@pytest.mark.asyncio
async def test_remembered_ask_is_fresh_through_ten_seconds(monkeypatch) -> None:
    bot, _written, emitted = _bot(monkeypatch)
    _armed_clro(bot.strategy)
    _at(bot.strategy, PRINT_2_RCV - 10_000)
    await _send_levelone(bot, {"0": "CLRO", "2": 5.55}, PRINT_2_RCV - 10_000)
    _at(bot.strategy, PRINT_2_RCV)
    await _send_levelone(bot, {"0": "CLRO", "3": 5.5689, "35": PRINT_2_TS}, PRINT_2_RCV)
    await asyncio.sleep(0)
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_stale_remembered_ask_logs_skip_without_latch(monkeypatch, caplog) -> None:
    bot, _written, emitted = _bot(monkeypatch)
    st = _armed_clro(bot.strategy)
    _at(bot.strategy, PRINT_2_RCV - 10_001)
    await _send_levelone(bot, {"0": "CLRO", "2": 5.55}, PRINT_2_RCV - 10_001)
    _at(bot.strategy, PRINT_2_RCV)
    with caplog.at_level(logging.INFO):
        await _send_levelone(
            bot, {"0": "CLRO", "3": 5.5689, "35": PRINT_2_TS}, PRINT_2_RCV
        )
    assert emitted == [] and st.resting_flip_ms == 0
    assert "reason=no_fresh_ask" in caplog.text and "latch_taken=0" in caplog.text


@pytest.mark.asyncio
async def test_remembered_ask_past_cap_does_not_burn_latch(monkeypatch, caplog) -> None:
    bot, _written, emitted = _bot(monkeypatch)
    st = _armed_clro(bot.strategy)
    _at(bot.strategy, PRINT_2_RCV)
    await _send_levelone(bot, {"0": "CLRO", "2": 5.59}, PRINT_2_RCV - 100)
    with caplog.at_level(logging.INFO):
        await _send_levelone(
            bot, {"0": "CLRO", "3": 5.5689, "35": PRINT_2_TS}, PRINT_2_RCV
        )
    assert emitted == [] and st.resting_flip_ms == 0
    assert "reason=ask_past_band" in caplog.text and "latch_taken=0" in caplog.text


@pytest.mark.asyncio
async def test_timesale_print_never_reaches_the_rest(monkeypatch) -> None:
    bot, _written, emitted = _bot(monkeypatch)
    st = _armed_clro(bot.strategy)
    _at(bot.strategy, PRINT_2_RCV)
    tick = SchwabV2Streamer._extract_timesale_tick(
        {"0": "CLRO", "1": PRINT_2_TS, "2": 5.5689}, PRINT_2_RCV
    )
    assert tick is not None
    await bot._handle_stream_tick(tick)
    await asyncio.sleep(0)
    assert emitted == [] and st.resting_flip_ms == 0


@pytest.mark.asyncio
async def test_timesale_service_is_excluded_even_if_payload_has_field_35(monkeypatch) -> None:
    """The service identity itself, not only LEVELONE's field shape, gates entry."""
    bot, _written, emitted = _bot(monkeypatch)
    st = _armed_clro(bot.strategy)
    _at(bot.strategy, PRINT_2_RCV)
    bot._eh_stream_ask_by_symbol["CLRO"] = (5.55, PRINT_2_RCV)
    tick = SchwabTick(
        "trade", "TIMESALE_EQUITY", "CLRO", PRINT_2_TS,
        {"3": 5.5689, "35": PRINT_2_TS}, "cross-service", price=5.5689,
    )
    await bot._handle_stream_tick(tick)
    await asyncio.sleep(0)
    assert emitted == [] and st.resting_flip_ms == 0


@pytest.mark.asyncio
async def test_stream_disconnect_clears_remembered_asks(monkeypatch, caplog) -> None:
    bot, _written, emitted = _bot(monkeypatch)
    st = _armed_clro(bot.strategy)
    _at(bot.strategy, PRINT_2_RCV)
    await _send_levelone(bot, {"0": "CLRO", "2": 5.55}, PRINT_2_RCV - 100)
    assert "CLRO" in bot._eh_stream_ask_by_symbol
    await bot._clear_eh_stream_asks_on_disconnect()
    assert bot._eh_stream_ask_by_symbol == {}
    with caplog.at_level(logging.INFO):
        await _send_levelone(bot, {"0": "CLRO", "3": 5.5689, "35": PRINT_2_TS}, PRINT_2_RCV)
    assert emitted == [] and st.resting_flip_ms == 0
    assert "reason=no_fresh_ask" in caplog.text


def test_watchlist_departure_and_readd_clear_remembered_ask(monkeypatch) -> None:
    bot, _written, _emitted = _bot(monkeypatch)
    bot._watchlist = {"CLRO"}
    bot._eh_stream_ask_by_symbol["CLRO"] = (5.55, PRINT_2_RCV)
    symbols = ["OTHER"]
    monkeypatch.setattr(bot_module.StrategyStateSnapshotEvent, "model_validate_json", lambda _: object())
    monkeypatch.setattr(bot, "_strategy_state_event_is_current", lambda _: True)
    monkeypatch.setattr(bot, "_extract_confirmed_symbols", lambda _: symbols)
    monkeypatch.setattr(bot, "_protected_symbols", lambda: set())
    monkeypatch.setattr(bot, "_schwab_ineligible_symbols", lambda: set())
    monkeypatch.setattr(bot, "_try_complete_boot_state_restoration", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_push_desired_symbols", lambda: None)
    monkeypatch.setattr(bot.strategy, "release_and_drop_symbol", lambda _: None)
    bot._apply_strategy_state_event({"data": "{}"}, max_watchlist=2)
    assert "CLRO" not in bot._eh_stream_ask_by_symbol
    # A still-subscribed held symbol can receive a quote while off-watchlist.
    bot._eh_stream_ask_by_symbol["CLRO"] = (5.55, PRINT_2_RCV)
    symbols[:] = ["CLRO"]
    bot._apply_strategy_state_event({"data": "{}"}, max_watchlist=2)
    assert "CLRO" not in bot._eh_stream_ask_by_symbol


@pytest.mark.asyncio
async def test_bot_routes_the_clro_print_and_still_captures_it(monkeypatch) -> None:
    bot, written, emitted = _bot(monkeypatch)
    _armed_clro(bot.strategy)
    _at(bot.strategy, PRINT_1_RCV)
    await _send_levelone(bot, {"0": "CLRO", "1": 5.55, "2": 5.59, "3": 5.59,
                               "4": 100, "5": 500, "35": PRINT_1_TS}, PRINT_1_RCV)
    assert emitted == [] and bot.strategy._symbol_states["CLRO"].resting_flip_ms == 0
    assert len(written) == 2                              # trade + quote capture unchanged
    _at(bot.strategy, PRINT_2_RCV)
    await _send_levelone(bot, {"0": "CLRO", "1": 5.52, "2": 5.55, "3": 5.5689,
                               "4": 5400, "9": 2, "35": PRINT_2_TS}, PRINT_2_RCV)
    await asyncio.sleep(0)
    assert len(emitted) == 1 and emitted[0].metadata["eh_cross_source"] == "stream_trade"
    assert len(written) == 4


@pytest.mark.asyncio
async def test_stream_cross_does_not_drain_an_unrelated_queued_webull_leg(monkeypatch) -> None:
    bot, written, emitted = _bot(monkeypatch)
    _armed_clro(bot.strategy)
    _at(bot.strategy, PRINT_2_RCV)
    other_leg = object()
    bot.strategy._pending_webull_fanout_intents.append(other_leg)
    await bot._handle_stream_tick(_trade(5.5689, PRINT_2_TS))
    await asyncio.sleep(0)
    assert bot.strategy._pending_webull_fanout_intents == [other_leg]
    assert len(written) == 1 and len(emitted) == 1


@pytest.mark.asyncio
async def test_blocked_emit_cannot_hold_the_next_stream_bar_or_duplicate(monkeypatch) -> None:
    bot, written, emitted = _bot(monkeypatch, strategy_schwab_1m_v2_tick_capture_enabled=True)
    st = _armed_clro(bot.strategy)
    _at(bot.strategy, PRINT_2_RCV)
    started = asyncio.Event()
    release = asyncio.Event()
    bars = []

    async def _blocked_emit(draft, emitter=None):
        started.set()
        await release.wait()
        emitted.append(draft)
        return "queued"

    async def _bar(symbol, bar):
        bars.append((symbol, bar.timestamp_ms))

    monkeypatch.setattr(bot, "_maybe_emit", _blocked_emit)
    streamer = SchwabV2Streamer(bot.settings, on_chart_bar=_bar, on_tick=bot._stream_tick_callback())
    payload = json.dumps({"data": [
        {"service": "LEVELONE_EQUITIES", "timestamp": PRINT_2_RCV, "content": [
            {"0": "CLRO", "2": 5.57, "3": 5.5689, "35": PRINT_2_TS},
            {"0": "CLRO", "2": 5.57, "3": 5.5689, "35": PRINT_2_TS},
        ]},
        {"service": "CHART_EQUITY", "content": [
            {"0": "CLRO", "2": 5.3, "3": 5.6, "4": 5.3, "5": 5.5,
             "6": 8000, "7": _ms(7, 24)},
        ]},
    ]})
    try:
        await asyncio.wait_for(streamer._handle_message(payload), timeout=0.5)
        await asyncio.wait_for(started.wait(), timeout=0.5)
        assert bars == [("CLRO", _ms(7, 24))]
        assert len(written) == 4  # two raw records, each has a trade and a quote
        assert st.resting_flip_ms == PRINT_2_RCV
        assert emitted == []  # submission is blocked; no fill can be assumed
    finally:
        release.set()
        await asyncio.sleep(0)
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_queued_premarket_cross_expires_before_rth_emit(monkeypatch) -> None:
    bot, written, emitted = _bot(monkeypatch)
    _armed_clro(bot.strategy, bar_ts=_ms(9, 29))
    _at(bot.strategy, _ms(9, 29, 59, 900_000))
    await bot._handle_stream_tick(_trade(5.5689, _ms(9, 29, 59, 800_000)))
    _at(bot.strategy, _ms(9, 30))
    await asyncio.sleep(0)
    assert len(written) == 1
    assert emitted == []


@pytest.mark.asyncio
async def test_bot_quote_ticks_never_reach_the_cross(monkeypatch) -> None:
    bot, written, emitted = _bot(monkeypatch)
    calls: list[tuple] = []
    monkeypatch.setattr(bot.strategy, "on_stream_trade", lambda *a: calls.append(a))
    await bot._handle_stream_tick(
        SchwabTick("quote", "LEVELONE_EQUITIES", "CLRO", PRINT_1_TS, {}, "q",
                   bid_price=5.55, ask_price=5.59, last_price=5.59)
    )
    assert calls == [] and emitted == [] and len(written) == 1


@pytest.mark.asyncio
async def test_bot_strategy_error_never_stops_capture(monkeypatch) -> None:
    bot, written, emitted = _bot(monkeypatch)

    def _raise(*_a):
        bot.strategy._pending_webull_fanout_intents.append(object())
        raise RuntimeError("boom")

    monkeypatch.setattr(bot.strategy, "on_stream_trade", _raise)
    await bot._handle_stream_tick(_trade(5.59, PRINT_1_TS))
    assert emitted == [] and len(written) == 1
    assert bot.strategy._pending_webull_fanout_intents == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"strategy_schwab_1m_v2_eh_resting_stream_cross_enabled": False},
        {"strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled": False},
    ],
)
async def test_bot_flag_off_does_not_call_the_strategy(monkeypatch, overrides) -> None:
    bot, written, emitted = _bot(monkeypatch, **overrides)
    calls: list[tuple] = []
    monkeypatch.setattr(bot.strategy, "on_stream_trade", lambda *a: calls.append(a))
    await bot._handle_stream_tick(_trade(5.59, PRINT_1_TS))
    assert calls == [] and len(written) == 1


@pytest.mark.parametrize(
    ("overrides", "routed"),
    [
        ({}, True),                                                          # production: GAPHOLD off here, cross on
        ({"strategy_schwab_1m_v2_eh_resting_stream_cross_enabled": False}, False),
        ({"strategy_schwab_1m_v2_eh_resting_stream_cross_enabled": False,
          "strategy_schwab_1m_v2_gap_hold_enabled": True}, True),
    ],
)
def test_streamer_callback_routes_ticks_when_the_cross_is_on(monkeypatch, overrides, routed) -> None:
    bot, _written, _emitted = _bot(
        monkeypatch, strategy_schwab_1m_v2_tick_capture_enabled=True, **overrides
    )
    cb = bot._stream_tick_callback()
    assert (cb == bot._handle_stream_tick) is routed
    if not routed:
        assert cb == bot.tick_writer.on_tick


def test_capture_off_does_not_start_timesale_or_the_stream_cross(monkeypatch) -> None:
    bot, _written, _emitted = _bot(
        monkeypatch,
        strategy_schwab_1m_v2_tick_capture_enabled=False,
        strategy_schwab_1m_v2_timesale_capture_enabled=True,
    )
    bot.tick_writer = None
    cb = bot._stream_tick_callback()
    assert cb is None
    streamer = SchwabV2Streamer(bot.settings, on_chart_bar=None, on_tick=cb)
    assert streamer._tick_capture is False
    assert streamer._timesale_capture is False
