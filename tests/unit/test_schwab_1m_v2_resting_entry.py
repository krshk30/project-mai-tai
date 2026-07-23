"""CW-v2 RESTING flip-entry manager — place / no-overlap replace / cancel of a resting buy-stop-limit
that tracks the ATR short trail (docs/v2-resting-flip-entry-design.md).

The load-bearing safety invariant is NO-OVERLAP: at most ONE intent per bar (never a cancel AND a
place together) => never two live buy orders => no double-fill/oversell. Flag-off = inert.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

_ET = ZoneInfo("America/New_York")
IN_WIN = int(datetime(2026, 7, 10, 11, 0, tzinfo=_ET).timestamp() * 1000)     # 11:00 ET (RTH)
OPEN_WIN = int(datetime(2026, 7, 10, 9, 45, tzinfo=_ET).timestamp() * 1000)   # 09:45 ET (in the 09:30-10:00 open)
PRE_WIN = int(datetime(2026, 7, 10, 8, 0, tzinfo=_ET).timestamp() * 1000)     # 08:00 ET (pre-market, OUT of window)


def _strat(resting=True, **overrides):
    kwargs = {
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": resting,
    }
    kwargs.update(overrides)
    return SchwabV2Strategy(Settings(**kwargs))


def _sig(*, trail=9.5, state="short"):
    return {"touch": False, "touch_price": None, "flip": None, "flip_level": None,
            "trail": trail, "loss": 0.5, "state": state, "state_age": 3}


def _tick(strat, state, *, trail, ts=IN_WIN, st="short"):
    """One bar through the resting manager; returns the drafts it queued this bar."""
    state.bars.append(OHLCVBar(timestamp_ms=ts, open=trail + 1, high=trail + 1.2,
                               low=trail - 0.2, close=trail + 0.9, volume=10_000))
    strat._cw_v2_resting_track(state, _sig(trail=trail, state=st))
    return strat.drain_pending_intents()


# --------------------------------------------------------------------------- inert when off
def test_resting_off_is_inert() -> None:
    strat = _strat(resting=False)
    st = strat.watchlist_state("TEST")
    assert _tick(strat, st, trail=9.5) == []          # no intent queued
    assert st.resting_active is False


# --------------------------------------------------------------------------- place
def test_places_a_stop_limit_at_the_short_trail() -> None:
    strat = _strat()
    st = strat.watchlist_state("TEST")
    out = _tick(strat, st, trail=9.5)
    assert len(out) == 1
    d = out[0]
    assert d.intent_type == "open" and d.side == "buy"
    md = d.metadata
    assert md["order_type"] == "STOP_LIMIT"
    assert md["resting_entry"] == "true"
    assert md["stop_price"] == "9.5000"               # the ATR line = trigger
    assert md["limit_price"] == "9.5475"              # line * (1 + 0.5% band) = fill cap
    assert md["reference_price"] == "9.5000" and md["entry_price"] == "9.5000"  # target/stop off the line
    assert "ATR Flip" in d.reason                     # keeps the ATR-only belt
    assert st.resting_active is True and st.resting_level == 9.5


def test_band_is_tunable() -> None:
    strat = _strat(strategy_schwab_1m_v2_cw_v2_resting_entry_band_pct=2.0)
    md = _tick(strat, strat.watchlist_state("TEST"), trail=10.0)[0].metadata
    assert md["stop_price"] == "10.0000" and md["limit_price"] == "10.2000"   # 2% band


# --------------------------------------------------------------------------- no-overlap replace
def test_ratchet_cancels_this_bar_then_replaces_next_bar() -> None:
    """A short trail ratchets DOWN; a >=0.2% move cancels THIS bar and re-places NEXT bar -- never a
    cancel AND a place in the same bar (the no-overlap safety invariant)."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    assert _tick(strat, st, trail=9.50)[0].intent_type == "open"      # bar 1: place
    assert st.resting_active is True

    # bar 2: trail ratchets down 0.32% (9.50 -> 9.47) -> CANCEL only (no place)
    out2 = _tick(strat, st, trail=9.47)
    assert len(out2) == 1 and out2[0].intent_type == "cancel"
    assert st.resting_active is False                                 # cleared -> next bar re-places

    # bar 3: still short at the new level -> PLACE at 9.47 (no overlap: place is a SEPARATE bar)
    out3 = _tick(strat, st, trail=9.47)
    assert len(out3) == 1 and out3[0].intent_type == "open"
    assert out3[0].metadata["stop_price"] == "9.4700"
    assert st.resting_active is True and st.resting_level == 9.47


def test_small_trail_move_does_not_replace() -> None:
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _tick(strat, st, trail=9.50)                                      # place
    assert _tick(strat, st, trail=9.495) == []                       # 0.05% move -> leave it, no intent


# --------------------------------------------------------------------------- cancel on setup loss
def test_cancels_when_no_longer_short() -> None:
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _tick(strat, st, trail=9.50)                                      # place while short
    out = _tick(strat, st, trail=9.50, st="long")                    # flipped long -> cancel
    assert len(out) == 1 and out[0].intent_type == "cancel"
    assert st.resting_active is False


def test_cancels_when_out_of_window() -> None:
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _tick(strat, st, trail=9.50)                                      # place (in window)
    out = _tick(strat, st, trail=9.50, ts=PRE_WIN)                   # 09:45 ORB window -> cancel
    assert len(out) == 1 and out[0].intent_type == "cancel"


def test_does_not_place_out_of_window() -> None:
    strat = _strat()
    st = strat.watchlist_state("TEST")
    assert _tick(strat, st, trail=9.50, ts=PRE_WIN) == []            # never rest pre-market / post-16:00


def test_places_in_the_open_window() -> None:
    """The resting entry RUNS from 09:30 (unlike the reactive entry, which skips 09:30-10:00) --
    faithful to the 9-day study + the band-limit handles the volatile open. Pins the window change."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    out = _tick(strat, st, trail=9.50, ts=OPEN_WIN)                  # 09:45 is now IN window
    assert len(out) == 1 and out[0].intent_type == "open"


# --------------------------------------------------------------------------- fill closes the loop
def test_clears_on_fill_without_cancelling() -> None:
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _tick(strat, st, trail=9.50)                                      # place
    st.position_qty = 2                                               # the resting order FILLED
    out = _tick(strat, st, trail=9.50)
    assert out == []                                                  # no cancel (the OTOCO exit owns it)
    assert st.resting_active is False


# --------------------------------------------------------------------------- reactive interlock
def test_reactive_stands_down_while_a_resting_order_is_live() -> None:
    from project_mai_tai.market_data.schwab_v2_rest_client import Quote
    strat = _strat()
    st = strat.watchlist_state("TEST")
    st.resting_active = True
    st.cw_armed = True
    st.cw_bars_waited = 2
    st.cw_trigger = 5.0
    st.cw_flip_level = 4.0
    # a break that WOULD fire the reactive entry, but a resting order is live -> stand down (no double)
    assert strat._cw_v2_quote(st, Quote("TEST", 5.99, 6.01, 6.00, IN_WIN, 0)) is None


def test_reactive_flag_off_silences_reactive_entry() -> None:
    from project_mai_tai.market_data.schwab_v2_rest_client import Quote
    strat = _strat(resting=False, strategy_schwab_1m_v2_cw_v2_reactive_entry_enabled=False)
    st = strat.watchlist_state("TEST")
    st.cw_armed = True
    st.cw_bars_waited = 2
    st.cw_trigger = 5.0
    st.cw_flip_level = 4.0
    assert strat._cw_v2_quote(st, Quote("TEST", 5.99, 6.01, 6.00, IN_WIN, 0)) is None
