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


def _sig(*, trail=9.5, state="short", state_age=3):
    return {"touch": False, "touch_price": None, "flip": None, "flip_level": None,
            "trail": trail, "loss": 0.5, "state": state, "state_age": state_age}


def _tick(strat, state, *, trail, ts=IN_WIN, st="short", in_window=True, now_ms=1_000_000, state_age=3):
    """One bar through the resting manager; returns the drafts it queued this bar. The RTH window is
    injected via `in_window` (wall-clock, not the bar ts). `now_ms` drives the silence-on-fill grace
    (also wall-clock, injectable). `state_age` = bars the ATR has held its state (established-short gate;
    default 3 = established, so it does not gate the other tests)."""
    strat._resting_in_window = lambda now=None: in_window
    strat._now_ms = lambda: now_ms
    state.bars.append(OHLCVBar(timestamp_ms=ts, open=trail + 1, high=trail + 1.2,
                               low=trail - 0.2, close=trail + 0.9, volume=10_000))
    strat._cw_v2_resting_track(state, _sig(trail=trail, state=st, state_age=state_age))
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


# --------------------------------------------------------------------------- STABLE-REST cadence
def test_reprice_only_on_a_large_move() -> None:
    """⭐ STABLE-REST (the NVVE lesson). The order stays OUT THERE through small wiggles; it re-places
    only on a >= 1% trail move (cancel one bar, re-place the next -- no overlap). The old code cancelled
    on every 0.2% wiggle so no order was ever resting when price crossed."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    assert _tick(strat, st, trail=9.50)[0].intent_type == "open"      # bar 1: place at 9.50
    assert _tick(strat, st, trail=9.47) == []                        # 0.32% wiggle -> HOLD, no draft
    out = _tick(strat, st, trail=9.40)                               # 1.05% move -> reprice: CANCEL
    assert len(out) == 1 and out[0].intent_type == "cancel"
    assert st.resting_active is False
    out2 = _tick(strat, st, trail=9.40)                             # next bar: re-place at 9.40
    assert len(out2) == 1 and out2[0].intent_type == "open"
    assert out2[0].metadata["stop_price"] == "9.4000"


def test_reprice_threshold_is_tunable() -> None:
    """Pin the threshold VALUE: at 2%, a 1.05% move must NOT reprice (it holds)."""
    strat = _strat(strategy_schwab_1m_v2_cw_v2_resting_entry_reprice_pct=2.0)
    st = strat.watchlist_state("TEST")
    _tick(strat, st, trail=9.50)                                      # place
    assert _tick(strat, st, trail=9.40) == []                        # 1.05% < 2% -> HOLD


def test_small_trail_move_does_not_replace() -> None:
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _tick(strat, st, trail=9.50)                                      # place
    assert _tick(strat, st, trail=9.495) == []                       # 0.05% move -> leave it, no intent


def test_stop_leq_ask_guard_skips_the_place() -> None:
    """⭐ STOP<=ASK guard. A buy-stop must sit ABOVE the ask; on a fast up-tick the ask can already be
    at/above the trail (the flip is happening) -> Schwab firm-rejects "stop must be above the current
    ask". Skip the place; re-arm once the trail is back above the market."""
    from project_mai_tai.market_data.schwab_v2_rest_client import Quote
    strat = _strat()
    st = strat.watchlist_state("TEST")
    st.last_quote = Quote("TEST", 9.55, 9.60, 9.58, IN_WIN, 0)       # ask 9.60 >= trail 9.50 -> SKIP
    assert _tick(strat, st, trail=9.50) == []
    assert st.resting_active is False
    st.last_quote = Quote("TEST", 9.05, 9.10, 9.08, IN_WIN, 0)       # ask 9.10 < trail 9.50 -> place
    out = _tick(strat, st, trail=9.50)
    assert len(out) == 1 and out[0].intent_type == "open"


def test_stop_leq_ask_guard_fails_open_without_a_quote() -> None:
    """No fresh quote -> the guard fails open (the broker stays the backstop); nothing regresses."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    st.last_quote = None
    assert _tick(strat, st, trail=9.50)[0].intent_type == "open"


def test_does_not_place_on_a_stale_replayed_bar() -> None:
    """⭐ LIVE-BAR gate (the SKYQ lesson). Never rest on a warmup-replayed / stale bar -- only on the
    CURRENT live purple line. On a mid-session CONFIRM the bot replays hours of old bars; without this
    we rested off ~3h-old levels the instant SKYQ confirmed."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    stale_now = IN_WIN + 10 * 60 * 1000                              # wall-clock 10 min after the bar ts
    assert _tick(strat, st, trail=9.50, now_ms=stale_now) == []      # bar is stale -> NO place
    assert st.resting_active is False
    out = _tick(strat, st, trail=9.50, now_ms=IN_WIN + 1000)         # bar 1s old -> live -> place
    assert len(out) == 1 and out[0].intent_type == "open"


def test_does_not_rest_until_short_is_established() -> None:
    """⭐ ESTABLISHED-SHORT gate (the SKYQ lesson). Don't rest on a fresh 1-bar short in a whipsaw --
    only once the ATR has been short for >= min_short_bars (3) consecutive bars (a settled downtrend)."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    assert _tick(strat, st, trail=9.50, state_age=1) == []           # just flipped short -> too fresh
    assert _tick(strat, st, trail=9.50, state_age=2) == []           # 2 bars -> still not established
    out = _tick(strat, st, trail=9.50, state_age=3)                  # 3 bars -> established -> place
    assert len(out) == 1 and out[0].intent_type == "open"


def test_min_short_bars_is_tunable() -> None:
    """Pin the threshold VALUE: at 5, a 3-bar-old short must NOT place yet."""
    strat = _strat(strategy_schwab_1m_v2_cw_v2_resting_entry_min_short_bars=5)
    st = strat.watchlist_state("TEST")
    assert _tick(strat, st, trail=9.50, state_age=3) == []           # 3 < 5 -> not established, HOLD
    assert _tick(strat, st, trail=9.50, state_age=5)[0].intent_type == "open"


# --------------------------------------------------------------- HOLD-THROUGH-FLIP + SILENCE-ON-FILL
def test_holds_the_order_through_the_up_flip() -> None:
    """⭐ HOLD-THROUGH-FLIP. The up-flip IS the fill, so state->long must NOT cancel the resting order
    (the old code did -- a race vs its own fill). It starts a settle grace and leaves the order live."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _tick(strat, st, trail=9.50, now_ms=1000)                        # place while short
    out = _tick(strat, st, trail=9.50, st="long", now_ms=2000)      # flip to long -> HOLD (no draft)
    assert out == []                                                 # NOT cancelled
    assert st.resting_active is True                                 # still resting at the broker
    assert st.resting_flip_ms == 2000                               # settle grace started


def test_silence_on_fill_position_appears_stops_everything() -> None:
    """⭐ SILENCE-ON-FILL. Once the fill lands (position_qty != 0) the flag + grace clear and nothing
    is emitted -- the OTOCO exit owns the position. This is what kills the ~30-reject NVVE spam."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _tick(strat, st, trail=9.50, now_ms=1000)                        # place
    _tick(strat, st, trail=9.50, st="long", now_ms=2000)            # flip -> grace
    st.position_qty = 2                                              # the resting order FILLED
    out = _tick(strat, st, trail=9.50, st="long", now_ms=3000)
    assert out == []                                                 # no cancel, no place
    assert st.resting_active is False and st.resting_flip_ms == 0


def test_no_reemit_during_the_fill_settle_grace() -> None:
    """During the grace the strategy stays SILENT even if the ATR whipsaws back to short -- it must not
    spam new brackets into the position-sync lag (the NVVE churn-after-fill)."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _tick(strat, st, trail=9.50, now_ms=1000)                        # place
    _tick(strat, st, trail=9.50, st="long", now_ms=2000)            # flip -> grace @2000
    assert _tick(strat, st, trail=9.30, st="short", now_ms=12000) == []   # 10s in (grace 30s) -> HOLD
    assert st.resting_active is True                                 # original order still out there


def test_grace_expiry_with_no_fill_cancels_then_rearms() -> None:
    """If the flip did NOT fill us, after the 30s grace we retire the stale order and re-arm on the
    next short segment."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _tick(strat, st, trail=9.50, now_ms=1000)                        # place
    _tick(strat, st, trail=9.50, st="long", now_ms=2000)            # flip -> grace @2000
    out = _tick(strat, st, trail=9.50, st="long", now_ms=33000)     # 31s later, still flat -> cancel
    assert len(out) == 1 and out[0].intent_type == "cancel"
    assert st.resting_active is False and st.resting_flip_ms == 0
    out2 = _tick(strat, st, trail=9.30, st="short", now_ms=34000)  # back to short -> re-arm
    assert len(out2) == 1 and out2[0].intent_type == "open"


def test_cancels_when_out_of_window() -> None:
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _tick(strat, st, trail=9.50)                                      # place (in window)
    out = _tick(strat, st, trail=9.50, in_window=False)              # wall-clock now out of window -> cancel
    assert len(out) == 1 and out[0].intent_type == "cancel"


def test_does_not_place_out_of_window() -> None:
    strat = _strat()
    st = strat.watchlist_state("TEST")
    assert _tick(strat, st, trail=9.50, in_window=False) == []       # never rest pre-market / post-16:00


def test_window_is_wall_clock_not_bar_ts() -> None:
    """⭐ THE FIX. The gate keyed off the last BAR's timestamp, so a stale/replayed in-window bar
    (a quiet symbol's prior-session 15:59 close, or a warmup replay of an old session) fired the
    resting entry pre-market -- the 04:00/07:00 ET STOP_LIMIT rejects on 07-23. It now reads the
    WALL CLOCK, so an IN-WINDOW bar at an OUT-OF-WINDOW clock does NOT place."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    # real gate: True across the RTH session, False outside -- incl. 04:00, the actual bug time
    f = strat._resting_in_window
    assert f(datetime(2026, 7, 23, 4, 0, tzinfo=_ET)) is False       # the 04:00 ET bug moment
    assert f(datetime(2026, 7, 23, 9, 29, tzinfo=_ET)) is False      # before the open
    assert f(datetime(2026, 7, 23, 9, 30, tzinfo=_ET)) is True       # runs from 09:30 (unlike reactive)
    assert f(datetime(2026, 7, 23, 9, 45, tzinfo=_ET)) is True       # 09:45 open window -> in
    assert f(datetime(2026, 7, 23, 15, 59, tzinfo=_ET)) is True      # last minute
    assert f(datetime(2026, 7, 23, 16, 0, tzinfo=_ET)) is False      # 16:00 exclusive
    # and the track obeys the clock, not the (in-window) bar ts
    st.bars.append(OHLCVBar(timestamp_ms=IN_WIN, open=10, high=10.2, low=9.8, close=10.1, volume=10_000))
    strat._resting_in_window = lambda now=None: False                # simulate an out-of-window wall clock
    strat._cw_v2_resting_track(st, _sig(trail=9.5))
    assert strat.drain_pending_intents() == []                       # in-window bar, out-of-window clock -> no place


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
