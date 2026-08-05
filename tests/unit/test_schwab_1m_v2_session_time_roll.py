"""Time-driven 04:00-ET session roll — the bar-driven reset's blind spot.

⛔⭐ THE DEFECT. `_update_atr_state` resets session state when a bar's anchor differs from the
stored one. That test is only ever EVALUATED when a bar arrives. A symbol that leaves the
watchlist stops receiving bars, so its reset never fires: on 2026-08-05 FUSE, HYFM and AXTL sat
`cw_armed=True` for ~33 hours (armed 08-03) while the watchlist read ["BJDX","GTE","YXT"].
Every WATCHLISTED symbol self-cleared correctly. Only the silent ones rotted.

⛔ THE TEST THAT MATTERS is `test_a_stale_armed_symbol_rolls_with_NO_bar_arriving`. A restart
also clears these symbols, so "cw_armed_segments is empty after the deploy" proves NOTHING —
it would read clean whether or not this code works. The only honest assertion is that state
clears with no bar and no restart.

Asserts on STATE, never on log narration.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    SchwabV2Strategy,
    session_start_ts_ms,
)

_ET = ZoneInfo("America/New_York")

# 2026-08-05 09:00 ET — inside the session whose anchor is 2026-08-05 04:00 ET.
NOW = datetime(2026, 8, 5, 9, 0, tzinfo=_ET)
NOW_MS = int(NOW.timestamp() * 1000)
TODAY_ANCHOR = session_start_ts_ms(NOW_MS)
# 2026-08-03 22:54 ET — FUSE's real arm bar, two sessions back.
STALE_MS = int(datetime(2026, 8, 3, 22, 54, tzinfo=_ET).timestamp() * 1000)
STALE_ANCHOR = session_start_ts_ms(STALE_MS)


def _strat(**overrides) -> SchwabV2Strategy:
    kwargs = {
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
    }
    kwargs.update(overrides)
    return SchwabV2Strategy(Settings(**kwargs))


def _armed_stale(strat: SchwabV2Strategy, symbol: str = "FUSE"):
    """A symbol armed in an EARLIER session that has since gone silent."""
    st = strat.watchlist_state(symbol)
    st.cw_armed = True
    st.cw_arm_bar_ts = STALE_MS
    st.cw_trigger = 1.32
    st.cw_segment_high = 1.32
    st.cw_entries_this_flip = 3
    st.atr_session_anchor_ms = STALE_ANCHOR
    st.atr_trail = 1.24
    st.atr_state = "short"
    st.resting_active = False
    return st


def _never_protected(_symbol: str, _state) -> bool:
    return False


# ─────────────────────────── the defect ───────────────────────────


def test_a_stale_armed_symbol_rolls_with_NO_bar_arriving() -> None:
    """THE acceptance test. No bar, no restart — the arm must still clear."""
    strat = _strat()
    st = _armed_stale(strat)
    assert st.cw_armed is True  # precondition: this is the 33-hour state

    rolled = strat.roll_stale_session_state(NOW_MS, is_protected=_never_protected)

    assert rolled == ["FUSE"]
    assert st.cw_armed is False
    # `cw_arm_bar_ts` is deliberately NOT cleared — the bar-driven reset does not clear it either,
    # and `cw_armed_segments()` filters on `cw_armed`, so a disarmed segment cannot surface.
    # Kept identical rather than "improved", so the two drivers stay provably the same.
    assert st.cw_arm_bar_ts == STALE_MS
    assert st.cw_entries_this_flip == 0
    assert st.cw_trigger == 0.0
    assert st.cw_segment_high == 0.0
    assert st.atr_session_anchor_ms == TODAY_ANCHOR
    assert st.atr_trail is None and st.atr_state is None


def test_a_symbol_already_in_THIS_session_is_untouched() -> None:
    """A live, watchlisted symbol must not be disturbed — this is the RTH path."""
    strat = _strat()
    st = _armed_stale(strat, "BJDX")
    st.cw_arm_bar_ts = NOW_MS - 60_000
    st.atr_session_anchor_ms = TODAY_ANCHOR  # it HAS seen a bar this session

    rolled = strat.roll_stale_session_state(NOW_MS, is_protected=_never_protected)

    assert rolled == []
    assert st.cw_armed is True
    assert st.cw_trigger == 1.32
    assert st.atr_trail == 1.24


def test_a_never_seeded_symbol_is_untouched() -> None:
    """anchor_ms == 0 means no bar EVER. Rolling it would fabricate a session it never had."""
    strat = _strat()
    st = _armed_stale(strat, "NEW")
    st.atr_session_anchor_ms = 0

    assert strat.roll_stale_session_state(NOW_MS, is_protected=_never_protected) == []
    assert st.cw_armed is True


def test_protection_is_honoured() -> None:
    strat = _strat()
    st = _armed_stale(strat, "HELD")

    rolled = strat.roll_stale_session_state(
        NOW_MS, is_protected=lambda symbol, _st: symbol == "HELD"
    )

    assert rolled == []
    assert st.cw_armed is True
    assert st.atr_session_anchor_ms == STALE_ANCHOR


# ─────────────── the extraction did not change the bar path ───────────────


def test_bar_driven_reset_still_clears_at_the_anchor() -> None:
    """Regression on the refactor: `_apply_session_anchor_reset` was extracted OUT of
    `_update_atr_state`. The bar-driven path must behave exactly as before."""
    strat = _strat()
    st = _armed_stale(strat, "AAA")

    bar = OHLCVBar(
        timestamp_ms=NOW_MS, open=1.0, high=1.1, low=0.9, close=1.05, volume=25_000
    )
    strat._update_atr_state(st, bar)

    assert st.cw_armed is False
    assert st.cw_entries_this_flip == 0
    assert st.atr_session_anchor_ms == TODAY_ANCHOR


def test_both_drivers_produce_the_same_reset() -> None:
    """Field-by-field: the two paths must not drift."""
    strat = _strat()
    by_time = _armed_stale(strat, "TIME")
    by_bar = _armed_stale(strat, "BAR")

    strat.roll_stale_session_state(NOW_MS, is_protected=_never_protected)
    strat._update_atr_state(
        by_bar,
        OHLCVBar(timestamp_ms=NOW_MS, open=1.0, high=1.1, low=0.9, close=1.05, volume=25_000),
    )

    for field in (
        "cw_armed", "cw_bars_waited", "cw_trigger", "cw_flip_level",
        "cw_entries_this_flip", "cw_segment_high", "cw_v2_emit_claimed",
        "resting_active", "resting_level", "atr_session_anchor_ms",
    ):
        assert getattr(by_time, field) == getattr(by_bar, field), field


# ─────────────────────────── the bot's carve-out ───────────────────────────


def _bot(**overrides) -> SchwabV2BotService:
    kwargs = {
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_session_time_roll_enabled": True,
    }
    kwargs.update(overrides)
    return SchwabV2BotService(Settings(**kwargs), session_factory=None)


def test_flag_off_is_byte_identical() -> None:
    bot = _bot(strategy_schwab_1m_v2_session_time_roll_enabled=False)
    st = _armed_stale(bot.strategy)

    bot._roll_stale_session_state({}, {})

    assert st.cw_armed is True
    assert st.atr_session_anchor_ms == STALE_ANCHOR


def test_a_symbol_with_an_open_position_survives_the_roll() -> None:
    bot = _bot()
    st = _armed_stale(bot.strategy, "HELD")

    bot._roll_stale_session_state({"HELD": 2}, {"HELD": 2})

    assert st.cw_armed is True, "#580: a held position must keep its state"


def test_a_working_RESTING_ORDER_survives_the_roll() -> None:
    """⛔ #580 orphan. The reset clears `resting_active`; doing that while the buy-stop is
    WORKING AT THE BROKER loses the latch, and a lost latch never reprices again.
    `_protected_symbols()` does NOT cover this — it answers a watchlist question and sees
    positions only — which is why the sweep carries its own wider predicate."""
    bot = _bot()
    st = _armed_stale(bot.strategy, "RESTING")
    st.resting_active = True
    st.resting_level = 1.25

    bot._roll_stale_session_state({}, {})

    assert st.resting_active is True
    assert st.resting_level == 1.25
    assert st.cw_armed is True


def test_a_symbol_mid_warmup_is_skipped() -> None:
    """Warmup replays historical bars whose anchors are legitimately older. Rolling underneath
    the replay would reset the trail mid-series."""
    bot = _bot()
    st = _armed_stale(bot.strategy, "WARMING")
    bot._watchlist = {"WARMING"}
    bot._rest_warmup_done = set()  # not yet warmed

    bot._roll_stale_session_state({}, {})

    assert st.cw_armed is True


def test_a_warmed_watchlisted_symbol_that_went_silent_DOES_roll() -> None:
    """The halted/illiquid case: still watchlisted, warmup complete, but no bars this session."""
    bot = _bot()
    st = _armed_stale(bot.strategy, "HALTED")
    bot._watchlist = {"HALTED"}
    bot._rest_warmup_done = {"HALTED"}

    bot._roll_stale_session_state({}, {})

    assert st.cw_armed is False


def test_operator_protected_symbols_are_never_touched() -> None:
    bot = _bot(protected_symbols="CYN")
    st = _armed_stale(bot.strategy, "CYN")

    bot._roll_stale_session_state({}, {})

    assert st.cw_armed is True
