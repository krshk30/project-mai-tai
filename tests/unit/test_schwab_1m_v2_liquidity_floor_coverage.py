"""The liquidity floor must guard the LIVE entry paths, not just the dead ones (live 2026-07-28).

⭐ WHAT WAS WRONG. `strategy_schwab_1m_v2_atr_flip_vol_floor` (5000) is described in settings as
"the ONLY filter", and it was applied in exactly two places: `_maybe_atr_emit` and `_cw_entry`.
Both are the A/B + break paths that the resting flip-entry replaced. The three paths that actually
trade today -- REACTIVE, RESTING, and the Webull FAN-OUT -- each bought with no liquidity check at
all. A floor that guards only dead code is not a floor.

⭐ THE LIVE CASE. CNET, 2026-07-28:
    19:52:02  [V2-RESTING-PLACE] CNET stop=1.4034     <- driving bar volume = 4011
    19:57:06  [V2-FANOUT-RTH-RESTING] CNET px=1.4300 -> parallel Webull leg
    result: bought 1.43, stopped out 1.36 = -4.9%
4011 is BELOW the 5000 floor that was supposed to be protecting us. The gate existed; the path
never reached it. (Operator confirmed the floor stays at 5000 -- coverage was the bug, not the
number.)

⛔ THE ARM-ONLY RULE. On the resting path the floor gates the initial ARM only, never a reprice or
a cancel. An order already working must keep being managed even if the tape thins, or we recreate
the #580 orphan: a live buy-stop at the broker that nobody reprices.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

_ET = ZoneInfo("America/New_York")
IN_WIN = int(datetime(2026, 7, 10, 11, 0, tzinfo=_ET).timestamp() * 1000)

CNET_THIN = 4011      # the real driving-bar volume when CNET was armed
FLOOR = 5000          # strategy_schwab_1m_v2_atr_flip_vol_floor default


def _strat(**over):
    kw = {
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
    }
    kw.update(over)
    return SchwabV2Strategy(Settings(**kw))


def _bar(strat, state, *, trail, volume):
    state.bars.append(OHLCVBar(timestamp_ms=IN_WIN, open=trail + 1, high=trail + 1.2,
                               low=trail - 0.2, close=trail + 0.9, volume=volume))


def _q():
    return Quote(symbol="CNET", bid_price=1.43, ask_price=1.44, last_price=1.43,
                 quote_time_ms=IN_WIN + 1000)


def _sig(trail):
    return {"touch": False, "touch_price": None, "flip": None, "flip_level": None,
            "trail": trail, "loss": 0.5, "state": "short", "state_age": 3}


def _rest_tick(strat, state, *, trail, volume):
    strat._resting_in_window = lambda now=None: True
    strat._now_ms = lambda: 1_000_000
    _bar(strat, state, trail=trail, volume=volume)
    strat._cw_v2_resting_track(state, _sig(trail))
    return strat.drain_pending_intents()


# ------------------------------------------------------------------ the floor default
def test_the_floor_value_is_pinned_at_5000() -> None:
    """PINS THE VALUE. Operator kept 5000 after reviewing CNET; a silent drift changes what trades."""
    assert Settings().strategy_schwab_1m_v2_atr_flip_vol_floor == FLOOR


def test_helper_reads_the_last_completed_bar() -> None:
    strat = _strat()
    st = strat.watchlist_state("CNET")
    assert strat._liquidity_floor_ok(st) is True, "no bars yet -> must not block"
    _bar(strat, st, trail=1.40, volume=CNET_THIN)
    assert strat._liquidity_floor_ok(st) is False
    _bar(strat, st, trail=1.40, volume=10_339)
    assert strat._liquidity_floor_ok(st) is True


# ------------------------------------------------------------------ RESTING path
def test_resting_does_not_arm_on_a_thin_bar() -> None:
    """THE CNET REGRESSION."""
    strat = _strat()
    st = strat.watchlist_state("CNET")
    assert _rest_tick(strat, st, trail=1.4034, volume=CNET_THIN) == []
    assert st.resting_active is False, "armed a resting buy-stop on a 4011-volume bar"


def test_resting_still_arms_on_a_liquid_bar() -> None:
    """The floor must not block everything -- the #552 failure mode."""
    strat = _strat()
    st = strat.watchlist_state("CNET")
    assert _rest_tick(strat, st, trail=1.4034, volume=10_339)
    assert st.resting_active is True


def test_a_working_order_is_still_managed_when_the_tape_thins() -> None:
    """⛔ ARM-ONLY. Once resting, a thin bar must NOT strand the order -- that is the #580 orphan."""
    strat = _strat()
    st = strat.watchlist_state("CNET")
    _rest_tick(strat, st, trail=1.4034, volume=10_339)
    assert st.resting_active is True
    drafts = _rest_tick(strat, st, trail=1.2000, volume=CNET_THIN)   # trail drops, tape thins
    assert drafts, "the order was abandoned on a thin bar instead of being repriced/cancelled"


# ------------------------------------------------------------------ FAN-OUT leg
def test_fanout_leg_is_gated_on_its_own() -> None:
    """The Webull leg fires from its OWN software price-cross detector, so gating the Schwab
    primary does not cover it. CNET fired here at px=1.4300 on a thin tape."""
    strat = _strat(strategy_schwab_1m_v2_dual_broker_fanout_enabled=True)
    st = strat.watchlist_state("CNET")
    st.resting_active = True
    st.resting_level = 1.4034
    st.fanout_webull_claimed = False
    strat._now_ms = lambda: IN_WIN + 1000
    strat._resting_session_is_eh = lambda now=None: False   # wall-clock; pin to RTH
    _bar(strat, st, trail=1.4034, volume=CNET_THIN)

    strat._fanout_rth_resting_cross(st, _q())
    assert strat.drain_webull_fanout_intents() == []
    assert st.fanout_webull_claimed is False


def test_fanout_leg_still_fires_on_a_liquid_bar() -> None:
    strat = _strat(strategy_schwab_1m_v2_dual_broker_fanout_enabled=True)
    st = strat.watchlist_state("CNET")
    st.resting_active = True
    st.resting_level = 1.4034
    st.fanout_webull_claimed = False
    strat._now_ms = lambda: IN_WIN + 1000
    strat._resting_session_is_eh = lambda now=None: False   # wall-clock; pin to RTH
    _bar(strat, st, trail=1.4034, volume=10_339)

    strat._fanout_rth_resting_cross(st, _q())
    assert strat.drain_webull_fanout_intents(), "the floor blocked a legitimate liquid fan-out leg"


# ------------------------------------------------------------------ ORB (the other strategy)
# Operator: the floor applies to "any buy from both strategies". ORB had only a RELATIVE gate
# (`vol_mult * opening_range.avg_volume`) -- 1.5x a tiny average is still tiny, so a thin name
# cleared it trivially. ORB is live (`MAI_TAI_ORB_ENABLED=true`).
from project_mai_tai.strategy_core.orb_intrabar import (  # noqa: E402
    OpeningRange,
    OrbBar,
    OrbConfig,
    bar_confirms_breakout,
)


def _orb_bar(volume: float) -> OrbBar:
    return OrbBar(timestamp=datetime(2026, 7, 28, 9, 40, tzinfo=_ET), open=2.01, high=2.12,
                  low=2.00, close=2.10, volume=volume, vwap=2.00, ema9=2.00)


def test_orb_absolute_floor_blocks_a_relative_spike_on_a_thin_tape() -> None:
    """THE GAP. avg_volume=100 -> the 1.5x relative gate is satisfied by 150 shares."""
    thin = OpeningRange(high=2.00, low=1.90, avg_volume=100.0)
    assert bar_confirms_breakout(thin, _orb_bar(4_000), OrbConfig()) is False


def test_orb_still_takes_a_genuinely_liquid_breakout() -> None:
    thin = OpeningRange(high=2.00, low=1.90, avg_volume=100.0)
    assert bar_confirms_breakout(thin, _orb_bar(20_000), OrbConfig()) is True


def test_orb_floor_value_is_pinned_and_matches_v2() -> None:
    assert OrbConfig().vol_floor == FLOOR


def test_orb_relative_gate_still_applies_on_top() -> None:
    """The absolute floor ADDS to the relative one; it must not replace it. A 6k bar clears the
    floor but not 1.5x a 10k average."""
    liquid = OpeningRange(high=2.00, low=1.90, avg_volume=10_000.0)
    assert bar_confirms_breakout(liquid, _orb_bar(6_000), OrbConfig()) is False
