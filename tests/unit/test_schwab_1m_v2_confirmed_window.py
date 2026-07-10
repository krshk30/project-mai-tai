"""Confirmed-window entry (ATR variant "CW") — PR #1 of the confirmed-window ruleset.

Drives ``SchwabV2Strategy._cw_entry`` with synthetic per-bar ATR signals to pin the
wait-3-bar-break state machine in isolation: arm on a BUY flip, wait 3 bars tracking the
highest high, enter the first later bar whose HIGH breaks it, cancel on a SELL flip, and
honor the liquidity floor at the break bar. The flag-default-off test guards byte-identical
behavior when the feature is disabled (the branch in _maybe_atr_emit is then unreachable).
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    SchwabV2Strategy,
)


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _strat(**overrides):
    kwargs = {"strategy_schwab_1m_v2_confirmed_window_enabled": True}
    kwargs.update(overrides)
    return SchwabV2Strategy(Settings(**kwargs))


def _bar(high: float, *, vol: int = 10_000, ts: int = 0) -> OHLCVBar:
    return OHLCVBar(
        timestamp_ms=ts,
        open=high - 0.1,
        high=high,
        low=high - 0.2,
        close=high - 0.05,
        volume=vol,
    )


def _sig(flip=None, *, trail=9.5, loss=0.5, state="long", age=1) -> dict:
    return {
        "touch": False,
        "touch_price": None,
        "flip": flip,
        "trail": trail,
        "loss": loss,
        "state": state,
        "state_age": age,
    }


def _run_to_watch(strat, state):
    """BUY flip + 3 wait bars (highs 10.0, 11.0, 10.5 -> 3-bar high = 11.0)."""
    assert strat._cw_entry(state, _bar(10.0, ts=1), _sig(flip="BUY")) is None
    assert state.cw_armed is True and state.cw_bars_waited == 0
    assert strat._cw_entry(state, _bar(10.0, ts=2), _sig()) is None
    assert strat._cw_entry(state, _bar(11.0, ts=3), _sig()) is None
    assert strat._cw_entry(state, _bar(10.5, ts=4), _sig()) is None
    assert state.cw_bars_waited == 3
    assert state.cw_three_bar_high == 11.0


def test_cw_flag_defaults_off():
    assert Settings().strategy_schwab_1m_v2_confirmed_window_enabled is False
    assert SchwabV2Strategy(Settings())._cw_enabled is False
    assert _strat()._cw_enabled is True


def test_cw_no_emit_during_wait():
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _run_to_watch(strat, state)  # every call above already asserted None


def test_cw_break_emits_at_three_bar_high():
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _run_to_watch(strat, state)
    # A non-break bar in the watch phase does not fire and stays armed.
    assert strat._cw_entry(state, _bar(10.8, ts=5), _sig()) is None
    assert state.cw_armed is True
    # First bar breaking the 3-bar high (11.0) with volume enters at the trigger.
    draft = strat._cw_entry(state, _bar(11.5, ts=6, vol=10_000), _sig(trail=9.7))
    assert draft is not None
    assert draft.side == "buy" and draft.intent_type == "open"
    assert draft.quantity == Decimal("10")
    assert draft.metadata["atr_variant"] == "CW"
    assert draft.metadata["reference_price"] == "11.0000"
    assert draft.metadata["entry_price"] == "11.0000"
    assert draft.metadata["cw_three_bar_high"] == "11.0000"
    assert draft.metadata["path"] == "ATR Flip"
    # Entered -> disarmed (one entry per armed setup).
    assert state.cw_armed is False
    assert state.last_entry_price == 11.0


def test_cw_sell_flip_cancels_setup():
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _run_to_watch(strat, state)
    # SELL flip before the break invalidates the setup.
    assert strat._cw_entry(state, _bar(10.9, ts=5), _sig(flip="SELL", state="short")) is None
    assert state.cw_armed is False
    # A subsequent break must NOT enter (nothing armed).
    assert strat._cw_entry(state, _bar(12.0, ts=6, vol=10_000), _sig()) is None


def test_cw_thin_break_bar_skipped_then_liquid_break_enters():
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _run_to_watch(strat, state)
    # Breaks the 3-bar high but on a sub-floor bar (vol <= 5000): skip, stay armed.
    assert strat._cw_entry(state, _bar(11.6, ts=5, vol=100), _sig()) is None
    assert state.cw_armed is True
    # Next liquid break enters at the trigger.
    draft = strat._cw_entry(state, _bar(11.7, ts=6, vol=10_000), _sig())
    assert draft is not None
    assert draft.metadata["reference_price"] == "11.0000"


def test_cw_no_break_never_emits():
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _run_to_watch(strat, state)
    for i, h in enumerate((10.8, 10.9, 10.5, 10.99), start=5):
        assert strat._cw_entry(state, _bar(h, ts=i, vol=10_000), _sig()) is None
    assert state.cw_armed is True


def test_cw_new_buy_flip_rearms_and_resets_trigger():
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _run_to_watch(strat, state)  # 3-bar high = 11.0
    # A fresh BUY flip re-arms and resets the wait/trigger.
    assert strat._cw_entry(state, _bar(20.0, ts=5), _sig(flip="BUY")) is None
    assert state.cw_armed is True
    assert state.cw_bars_waited == 0
    assert state.cw_three_bar_high == 0.0


# --------------------- PR #3: bar-close flip exit signal (_maybe_cw_flip_close) ------

def _hold(strat, qty=10):
    state = strat.watchlist_state("TEST")
    state.position_qty = qty
    state.bars.append(_bar(10.0, ts=_now_ms()))  # a FRESH held bar
    return state


def test_cw_flip_close_fires_when_holding_on_sell_flip():
    strat = _strat()
    state = _hold(strat, qty=10)
    draft = strat._maybe_cw_flip_close(state, _sig(flip="SELL", state="short"))
    assert draft is not None
    assert draft.side == "sell" and draft.intent_type == "close"
    assert draft.quantity == Decimal("10")
    assert draft.metadata["cw_flip"] == "true"
    assert draft.metadata["atr_variant"] == "CW"


def test_cw_flip_close_none_when_flat():
    strat = _strat()
    state = _hold(strat, qty=0)  # flat
    assert strat._maybe_cw_flip_close(state, _sig(flip="SELL", state="short")) is None


def test_cw_flip_close_none_without_sell_flip():
    strat = _strat()
    state = _hold(strat, qty=10)
    assert strat._maybe_cw_flip_close(state, _sig(flip=None)) is None
    assert strat._maybe_cw_flip_close(state, _sig(flip="BUY")) is None


def test_cw_flip_close_none_when_flag_off():
    strat = SchwabV2Strategy(Settings())  # CW disabled
    state = strat.watchlist_state("TEST")
    state.position_qty = 10
    state.bars.append(_bar(10.0, ts=_now_ms()))
    assert strat._maybe_cw_flip_close(state, _sig(flip="SELL", state="short")) is None


def test_cw_flip_close_none_on_stale_bar():
    strat = _strat()
    state = strat.watchlist_state("TEST")
    state.position_qty = 10
    state.bars.append(_bar(10.0, ts=_now_ms() - 600_000))  # 10 min old -> stale
    assert strat._maybe_cw_flip_close(state, _sig(flip="SELL", state="short")) is None


# ------- PR #4: CW is exclusive with the on_quote hold-confirm TOUCH entry -----------

def _quote(last: float, ms: int = 0):
    return SimpleNamespace(
        last_price=last, bid_price=last - 0.01, ask_price=last + 0.01,
        quote_time_ms=ms or _now_ms(),
    )


def _arm_touch_state(strat, prev_trail: float = 10.0):
    """Set up the intrabar-touch preconditions on_quote arms a hold from."""
    state = strat.watchlist_state("TEST")
    state.atr_prev_state = "short"
    state.atr_prev_trail = prev_trail
    state.position_qty = 0
    state.cooldown_bars_remaining = 0
    state.atr_fired_in_short_seg = False
    return state


def test_cw_disables_on_quote_hold_confirm_entry():
    # Both flags on (mirrors the live env: hold_confirm=true). CW must suppress the
    # quote-path touch entry so it can't fire alongside the CW bar-path wait-3 entry.
    strat = SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_hold_confirm_enabled=True,
        strategy_schwab_1m_v2_atr_flip_enabled=True,
    ))
    state = _arm_touch_state(strat, 10.0)
    draft = strat.on_quote("TEST", _quote(10.5))  # crosses the resting short trail
    assert draft is None
    assert state.atr_hold_pending is None          # CW suppressed the arm


def test_hold_confirm_still_arms_when_cw_off():
    # Byte-identical off: with CW off, the hold-confirm path still arms as before.
    strat = SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_confirmed_window_enabled=False,
        strategy_schwab_1m_v2_hold_confirm_enabled=True,
        strategy_schwab_1m_v2_atr_flip_enabled=True,
    ))
    state = _arm_touch_state(strat, 10.0)
    strat.on_quote("TEST", _quote(10.5))
    assert state.atr_hold_pending is not None       # unchanged behavior
