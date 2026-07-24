"""CW-v2 EH RESTING flip-entry (P-B2) — software emulation of the resting buy-stop-limit in extended
hours, where a broker stop trigger is dead on BOTH brokers (docs/premarket-eod-exit-design.md).

When the flag is ON: the resting window opens to 07:30; in EH the bar-track arms a SOFTWARE rest (no
broker STOP_LIMIT draft) and `on_quote` emits a MARKETABLE EH-LIMIT buy on the ATR up-cross (price
reaching the resting level). RTH is byte-identical (a broker buy-stop-limit rests as today). Flag-OFF is
byte-identical (window stays 09:30, cross-check inert).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

_ET = ZoneInfo("America/New_York")
RTH_WIN = int(datetime(2026, 7, 10, 11, 0, tzinfo=_ET).timestamp() * 1000)     # 11:00 ET (RTH)
PRE_WIN = int(datetime(2026, 7, 10, 8, 0, tzinfo=_ET).timestamp() * 1000)      # 08:00 ET (pre-market EH)


def _strat(eh=True, resting=True, **overrides):
    kwargs = {
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": resting,
        "strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled": eh,
    }
    kwargs.update(overrides)
    return SchwabV2Strategy(Settings(**kwargs))


def _sig(*, trail=9.5, state="short", state_age=3):
    return {"touch": False, "touch_price": None, "flip": None, "flip_level": None,
            "trail": trail, "loss": 0.5, "state": state, "state_age": state_age}


def _arm(strat, state, *, trail, ts=PRE_WIN, now_ms, eh=True):
    """Run one bar through the resting manager in EH so it arms a SOFTWARE rest (no broker draft).
    Returns the drafts queued this bar (should be [] in EH — nothing goes to the broker)."""
    strat._resting_session_is_eh = lambda now=None: eh
    strat._resting_in_window = lambda now=None: True
    strat._now_ms = lambda: now_ms
    state.bars.append(OHLCVBar(timestamp_ms=ts, open=trail + 1, high=trail + 1.2,
                               low=trail - 0.2, close=trail + 0.9, volume=10_000))
    strat._cw_v2_resting_track(state, _sig(trail=trail))
    return strat.drain_pending_intents()


# --------------------------------------------------------------------------- window opens to 07:00
def test_window_opens_to_0700_when_flag_on() -> None:
    strat = _strat()
    f = strat._resting_in_window
    assert f(datetime(2026, 7, 10, 6, 59, tzinfo=_ET)) is False    # just before 07:00
    assert f(datetime(2026, 7, 10, 7, 0, tzinfo=_ET)) is True      # ⭐ opens at 07:00
    assert f(datetime(2026, 7, 10, 8, 0, tzinfo=_ET)) is True      # pre-market EH
    assert f(datetime(2026, 7, 10, 9, 30, tzinfo=_ET)) is True     # RTH open
    assert f(datetime(2026, 7, 10, 15, 59, tzinfo=_ET)) is True    # last RTH minute
    assert f(datetime(2026, 7, 10, 16, 0, tzinfo=_ET)) is False    # 16:00 exclusive


def test_window_stays_0930_when_flag_off() -> None:
    """Flag OFF -> byte-identical: the window stays 09:30 (07:30 does NOT open)."""
    strat = _strat(eh=False)
    f = strat._resting_in_window
    assert f(datetime(2026, 7, 10, 7, 0, tzinfo=_ET)) is False     # closed pre-market
    assert f(datetime(2026, 7, 10, 8, 0, tzinfo=_ET)) is False
    assert f(datetime(2026, 7, 10, 9, 29, tzinfo=_ET)) is False
    assert f(datetime(2026, 7, 10, 9, 30, tzinfo=_ET)) is True     # unchanged 09:30 start


def test_0700_window_threshold_is_pinned() -> None:
    """Threshold mutation guard: 07:00 must be the exact boundary — 06:59 out, 07:00 in."""
    strat = _strat()
    assert strat._resting_in_window(datetime(2026, 7, 10, 6, 59, tzinfo=_ET)) is False
    assert strat._resting_in_window(datetime(2026, 7, 10, 7, 0, tzinfo=_ET)) is True


# --------------------------------------------------------------------------- EH arms in memory (no broker draft)
def test_eh_arms_software_rest_without_a_broker_draft() -> None:
    """⭐ In EH the bar-track arms the level IN MEMORY (resting_active/level) but queues NO broker order —
    a broker buy-stop-limit can't trigger in extended hours."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    out = _arm(strat, st, trail=9.5, now_ms=PRE_WIN + 1000)
    assert out == []                                              # NOTHING sent to the broker
    assert st.resting_active is True and st.resting_level == 9.5  # armed in memory


def test_rth_still_places_a_broker_stop_limit() -> None:
    """RTH is byte-identical: the bar-track still queues the broker buy-stop-limit draft."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    out = _arm(strat, st, trail=9.5, ts=RTH_WIN, now_ms=RTH_WIN + 1000, eh=False)
    assert len(out) == 1
    assert out[0].metadata["order_type"] == "STOP_LIMIT"
    assert out[0].metadata["stop_price"] == "9.5000"
    assert out[0].metadata["limit_price"] == "9.5475"            # line*(1+0.5%)


# --------------------------------------------------------------------------- the cross emits a marketable EH-LIMIT
def test_cross_emits_marketable_eh_limit_buy() -> None:
    """⭐ The ATR up-cross (a live print reaching the resting level) emits a MARKETABLE EH-LIMIT open —
    NOT a broker STOP_LIMIT — tagged eh_resting so the OMS band-caps it."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _arm(strat, st, trail=9.5, now_ms=PRE_WIN + 1000)            # software-rest at 9.5
    strat._now_ms = lambda: PRE_WIN + 2000
    d = strat.on_quote("TEST", Quote("TEST", 9.50, 9.52, 9.51, PRE_WIN + 2000, 0))  # last 9.51 >= 9.5
    assert d is not None
    assert d.intent_type == "open" and d.side == "buy"
    md = d.metadata
    assert md["order_type"] == "limit"                           # marketable EH-LIMIT
    assert md["eh_resting"] == "true" and md["resting_entry"] == "true"
    assert md["resting_level"] == "9.5000" and md["entry_price"] == "9.5000"
    assert md["resting_band_pct"] == "0.5"
    assert "ATR Flip" in d.reason                                # keeps the ATR-only belt
    assert st.resting_flip_ms == PRE_WIN + 2000                  # entered the settle grace (emit-once)


def test_no_emit_below_the_level() -> None:
    """A print BELOW the resting level is not a cross -> no emit, no grace."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _arm(strat, st, trail=9.5, now_ms=PRE_WIN + 1000)
    strat._now_ms = lambda: PRE_WIN + 2000
    assert strat.on_quote("TEST", Quote("TEST", 9.40, 9.42, 9.41, PRE_WIN + 2000, 0)) is None
    assert st.resting_flip_ms == 0                               # not triggered


def test_cross_emits_exactly_once_per_trigger() -> None:
    """A burst of quotes above the level emits ONCE (the settle grace suppresses the rest) — mirrors the
    RTH broker fill (once triggered, stop re-arming)."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _arm(strat, st, trail=9.5, now_ms=PRE_WIN + 1000)
    strat._now_ms = lambda: PRE_WIN + 2000
    first = strat.on_quote("TEST", Quote("TEST", 9.55, 9.57, 9.56, PRE_WIN + 2000, 0))
    assert first is not None
    strat._now_ms = lambda: PRE_WIN + 2500
    second = strat.on_quote("TEST", Quote("TEST", 9.60, 9.62, 9.61, PRE_WIN + 2500, 0))
    assert second is None                                        # grace suppresses the re-emit


# --------------------------------------------------------------------------- live-bar guard
def test_cross_blocked_on_a_stale_replayed_bar() -> None:
    """⭐ LIVE-BAR guard (#528 mirror): never emit off a warmup-replayed / stale bar. A bar older than
    the max age -> no emit even though the price crosses."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    # arm cleanly on a live bar
    _arm(strat, st, trail=9.5, now_ms=PRE_WIN + 1000)
    # now the wall clock is 10 min past the (only) bar -> the feed is stale
    stale_now = PRE_WIN + 10 * 60 * 1000
    strat._now_ms = lambda: stale_now
    assert strat.on_quote("TEST", Quote("TEST", 9.55, 9.57, 9.56, stale_now, 0)) is None
    assert st.resting_flip_ms == 0                               # never fired on the stale bar


# --------------------------------------------------------------------------- session / flag gating
def test_no_eh_cross_in_rth() -> None:
    """In RTH the cross-check is inert (the broker stop owns the cross); on_quote falls to the reactive
    path, which returns None here (no reactive arm)."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    st.resting_active = True
    st.resting_level = 9.5
    st.bars.append(OHLCVBar(timestamp_ms=RTH_WIN, open=9.4, high=9.6, low=9.3, close=9.55, volume=10_000))
    strat._resting_session_is_eh = lambda now=None: False        # RTH
    strat._now_ms = lambda: RTH_WIN + 1000
    assert strat.on_quote("TEST", Quote("TEST", 9.55, 9.57, 9.56, RTH_WIN + 1000, 0)) is None
    assert st.resting_flip_ms == 0


def test_flag_off_is_fully_inert() -> None:
    """Flag OFF -> the EH cross-check never fires; even with an armed rest and a crossing print, nothing
    is emitted by this path (byte-identical: reactive owns on_quote)."""
    strat = _strat(eh=False)
    st = strat.watchlist_state("TEST")
    st.resting_active = True
    st.resting_level = 9.5
    st.bars.append(OHLCVBar(timestamp_ms=PRE_WIN, open=9.4, high=9.6, low=9.3, close=9.55, volume=10_000))
    strat._resting_session_is_eh = lambda now=None: True
    strat._now_ms = lambda: PRE_WIN + 1000
    # reactive stands down while resting_active (interlock), so on_quote returns None — the point is the
    # EH cross-check did NOT emit.
    assert strat.on_quote("TEST", Quote("TEST", 9.55, 9.57, 9.56, PRE_WIN + 1000, 0)) is None
    assert st.resting_flip_ms == 0


def test_no_emit_while_in_a_position() -> None:
    """Already long (a fill landed) -> the exit ladder owns it; the cross-check is silent."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _arm(strat, st, trail=9.5, now_ms=PRE_WIN + 1000)
    st.position_qty = 2
    strat._now_ms = lambda: PRE_WIN + 2000
    assert strat.on_quote("TEST", Quote("TEST", 9.55, 9.57, 9.56, PRE_WIN + 2000, 0)) is None


# --------------------------------------------------------------------------- grace -> disarm in memory (no broker cancel)
def test_grace_expiry_disarms_in_memory_without_a_broker_cancel() -> None:
    """After the cross emits, if no fill lands the settle grace expires and the bar-track DISARMS the
    software rest IN MEMORY — no broker cancel draft (nothing is live at the broker in EH)."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _arm(strat, st, trail=9.5, now_ms=PRE_WIN + 1000)
    strat._now_ms = lambda: PRE_WIN + 2000
    strat.on_quote("TEST", Quote("TEST", 9.55, 9.57, 9.56, PRE_WIN + 2000, 0))   # cross -> grace @ +2000
    assert st.resting_flip_ms == PRE_WIN + 2000
    # 31s later, still flat -> grace expires -> disarm (in memory), NO broker cancel draft
    out = _arm(strat, st, trail=9.5, ts=PRE_WIN + 31_000, now_ms=PRE_WIN + 33_000)
    assert out == []                                            # no broker draft
    assert st.resting_active is False and st.resting_flip_ms == 0
