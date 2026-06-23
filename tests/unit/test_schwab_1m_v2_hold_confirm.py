"""Intrabar hold-confirmation (ATR variant-B) entry-gate tests.

After an INTRABAR trail-touch, the strategy watches the next N seconds of LEVELONE
quotes and emits the entry only if the move HOLDS (net_delta >= bps); a reverting
wick-touch is SKIPPED. A thin window (< min_ticks) falls back to ENTER (matches the
offline backtest's BAR_CLOSE_FALLBACK). Default OFF = byte-inert.

These pin the decision matrix: inert-when-off, confirm, skip, thin-fallback,
heartbeat resolution, and flip-invalidation. See
docs/intrabar-hold-confirmation-design.md.
"""
from __future__ import annotations

from datetime import UTC, datetime

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy


def _strat(*, hold_on: bool, **overrides) -> SchwabV2Strategy:
    s = SchwabV2Strategy(Settings())
    s._atr_enabled = True            # ATR path live (variant B by default)
    s._hold_confirm_enabled = hold_on
    s._hold_confirm_n_secs = 20
    s._hold_confirm_bps = 5.0
    s._hold_confirm_min_ticks = 5
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _warm_to_short(strat: SchwabV2Strategy, *, n_warm: int = 150):
    """Drive declining RED bars (verbatim from the ATR-flip test construction) so the
    segment settles SHORT with a resting trail and no touch fired. Returns (T, now_ms)."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    for i in range(n_warm):
        close = 12.0 - 0.02 * i
        ts = now_ms - (n_warm - i) * 60_000
        # (symbol, open, high, low, close, volume, ts); high stays below the trail.
        strat.on_bar("TEST", ChartBar("TEST", close + 0.05, close + 0.04, close - 0.06, close, 10_000, ts))
    st = strat.watchlist_state("TEST")
    assert st.atr_prev_state == "short", "warmup must end short"
    assert not st.atr_fired_in_short_seg, "no touch should have fired in warmup"
    return float(st.atr_prev_trail), now_ms


def _q(px: float, t_ms: int) -> Quote:
    return Quote("TEST", px - 0.01, px + 0.01, px, t_ms, 0)


# --------------------------------------------------------------------------- (1)

def test_hold_off_on_quote_is_inert() -> None:
    """Flag OFF: a quote that crosses the trail never arms a pending hold and never
    emits from on_quote — byte-identical to the original no-op."""
    strat = _strat(hold_on=False)
    T, now_ms = _warm_to_short(strat)
    assert strat.on_quote("TEST", _q(T + 0.02, now_ms)) is None
    assert strat.watchlist_state("TEST").atr_hold_pending is None


# --------------------------------------------------------------------------- (2)

def test_hold_confirms_when_move_holds() -> None:
    """Touch, then the window holds at +10 bps (> 5 bps) with >= min_ticks -> ENTER."""
    strat = _strat(hold_on=True)
    T, now_ms = _warm_to_short(strat)
    # touch instant
    assert strat.on_quote("TEST", _q(T, now_ms)) is None
    assert strat.watchlist_state("TEST").atr_hold_pending is not None
    # accumulate ticks inside the window (still holding)
    for k in range(1, 5):
        assert strat.on_quote("TEST", _q(T * 1.001, now_ms + k * 1000)) is None
    # resolving quote at the deadline, endpoint +10 bps
    draft = strat.on_quote("TEST", _q(T * 1.001, now_ms + 20_000))
    assert draft is not None
    assert "ATR Flip B [hold:confirm]" in draft.reason
    assert draft.metadata["hold_mode"] == "confirm"
    assert draft.side == "buy" and draft.intent_type == "open"
    assert draft.metadata["reference_price"] == f"{T:.4f}"   # entry == touch_price
    assert strat.watchlist_state("TEST").atr_hold_pending is None


# --------------------------------------------------------------------------- (3)

def test_hold_skips_when_move_reverts() -> None:
    """Touch, then the window reverts to -10 bps (< 5 bps) -> SKIP (no emit), and the
    segment stays claimed so the bar-close path won't re-enter it."""
    strat = _strat(hold_on=True)
    T, now_ms = _warm_to_short(strat)
    assert strat.on_quote("TEST", _q(T, now_ms)) is None
    for k in range(1, 5):
        assert strat.on_quote("TEST", _q(T * 0.999, now_ms + k * 1000)) is None
    draft = strat.on_quote("TEST", _q(T * 0.999, now_ms + 20_000))
    assert draft is None                                      # screened false-flip
    st = strat.watchlist_state("TEST")
    assert st.atr_hold_pending is None
    assert st.atr_fired_in_short_seg is True                  # not re-armable this segment


# --------------------------------------------------------------------------- (4)

def test_thin_window_falls_back_to_enter() -> None:
    """Touch + fewer than min_ticks quotes -> coverage guard -> ENTER (fallback)."""
    strat = _strat(hold_on=True)
    T, now_ms = _warm_to_short(strat)
    assert strat.on_quote("TEST", _q(T, now_ms)) is None      # 1 tick
    draft = strat.on_quote("TEST", _q(T - 0.50, now_ms + 20_000))  # 2 ticks < 5, past deadline
    assert draft is not None
    assert draft.metadata["hold_mode"] == "fallback_thin"
    assert "hold:fallback_thin" in draft.reason


# --------------------------------------------------------------------------- (5)

def test_heartbeat_resolves_pending_on_bar() -> None:
    """A hold whose window elapsed with no triggering quote is settled on the next
    completed bar (here: thin -> fallback enter)."""
    strat = _strat(hold_on=True)
    T, now_ms = _warm_to_short(strat)
    # touch dated in the past so its deadline is already behind wall-clock
    assert strat.on_quote("TEST", _q(T, now_ms - 60_000)) is None
    assert strat.watchlist_state("TEST").atr_hold_pending is not None
    # a fresh RED bar (no touch, no flip) -> heartbeat resolves the stale hold
    bar = ChartBar("TEST", T - 0.40, T - 0.40, T - 0.60, T - 0.50, 10_000, now_ms)
    draft = strat.on_bar("TEST", bar)
    assert draft is not None
    assert draft.metadata["hold_mode"] == "fallback_thin"
    assert strat.watchlist_state("TEST").atr_hold_pending is None


# --------------------------------------------------------------------------- (6)

def test_pending_hold_dropped_when_segment_flips() -> None:
    """A pending hold whose short segment flips long before resolving is DROPPED
    (the setup is invalidated) — no entry."""
    strat = _strat(hold_on=True)
    T, now_ms = _warm_to_short(strat)
    assert strat.on_quote("TEST", _q(T, now_ms)) is None
    assert strat.watchlist_state("TEST").atr_hold_pending is not None
    # a GREEN bar closing above the trail flips the segment long
    flip_bar = ChartBar("TEST", T + 0.10, T + 1.00, T - 0.10, T + 1.00, 10_000, now_ms)
    draft = strat.on_bar("TEST", flip_bar)
    assert draft is None
    assert strat.watchlist_state("TEST").atr_hold_pending is None
