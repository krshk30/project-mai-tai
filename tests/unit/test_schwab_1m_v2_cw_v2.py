"""CW v2 (intrabar break + rule-7 above-line + reclaim) — operator-validated rule refinements.

Drives the new bar-path state machine (`_cw_v2_track`) and the intrabar entry (`_cw_v2_quote`) in
isolation with synthetic ATR signals + quotes. Flag-off tests guard byte-identical behavior of the
shipped bar-close CW when the sub-flag is disabled.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy
from project_mai_tai.market_data.schwab_v2_rest_client import Quote

_ET = ZoneInfo("America/New_York")
NON_ORB_MS = int(datetime(2026, 7, 10, 11, 0, tzinfo=_ET).timestamp() * 1000)   # 11:00 ET
ORB_MS = int(datetime(2026, 7, 10, 9, 45, tzinfo=_ET).timestamp() * 1000)       # 09:45 ET


def _strat(**overrides):
    kwargs = {
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
    }
    kwargs.update(overrides)
    return SchwabV2Strategy(Settings(**kwargs))


def _strat_reclaim(**overrides):
    """Reclaim is OFF by default from 2026-07-15 (operator rule) but the code path is retained.
    The reclaim tests below opt in explicitly so they keep guarding that retained path."""
    kwargs = {"strategy_schwab_1m_v2_cw_v2_reclaim_enabled": True}
    kwargs.update(overrides)
    return _strat(**kwargs)


def _bar(high: float, *, vol: int = 10_000, low: float | None = None, ts: int = 0) -> OHLCVBar:
    return OHLCVBar(timestamp_ms=ts, open=high - 0.1, high=high,
                    low=high - 0.2 if low is None else low, close=high - 0.05, volume=vol)


def _sig(flip=None, *, flip_level=None, trail=9.5, loss=0.5, state="long", age=1) -> dict:
    return {"touch": False, "touch_price": None, "flip": flip, "flip_level": flip_level,
            "trail": trail, "loss": loss, "state": state, "state_age": age}


def _quote(px: float, *, ts: int = NON_ORB_MS) -> Quote:
    return Quote("TEST", px - 0.01, px + 0.01, px, ts, 0)


def _feed_bar(strat, state, bar, sig):
    """Simulate one new bar reaching the CW-v2 tracker."""
    state.bars.append(bar)
    strat._cw_v2_track(state, sig)


def _arm_to_watch(strat, state):
    """BUY flip (flip bar high 12.0, flip_level 9.5) + 2 bars (highs 10.0, 11.0) ->
    trigger = max(12.0, 10.0, 11.0) = 12.0, INCLUDING the flip/spike bar."""
    _feed_bar(strat, state, _bar(12.0, ts=1), _sig(flip="BUY", flip_level=9.5))
    _feed_bar(strat, state, _bar(10.0, ts=2), _sig())
    _feed_bar(strat, state, _bar(11.0, ts=3), _sig())
    assert state.cw_trigger == 12.0          # flip bar's 12.0 is included (rule 5)
    assert state.cw_flip_level == 9.5
    assert state.cw_bars_waited == 2 and state.cw_armed is True
    strat._cw_v2_track(state, _sig())         # bar+3: watch phase, resets forming-bar low


# --------------------------------------------------------------- flag / neutrality

def test_cw_v2_flag_defaults_off():
    assert Settings().strategy_schwab_1m_v2_cw_v2_enabled is False
    assert SchwabV2Strategy(Settings())._cw_v2_enabled is False
    # cw on but v2 off -> v2 inert
    s = SchwabV2Strategy(Settings(strategy_schwab_1m_v2_confirmed_window_enabled=True))
    assert s._cw_v2_enabled is False
    st = s.watchlist_state("TEST")
    st.bars.append(_bar(12.0, ts=1))
    s._cw_v2_track(st, _sig(flip="BUY", flip_level=9.5))  # no-op
    assert st.cw_trigger == 0.0
    assert s._cw_v2_quote(st, _quote(99.0)) is None


def test_cw_v2_requires_both_flags():
    # sub-flag on but CW off -> still inert (v2 requires CW)
    s = SchwabV2Strategy(Settings(strategy_schwab_1m_v2_cw_v2_enabled=True))
    assert s._cw_v2_enabled is False


# --------------------------------------------------------------- trigger / entry

def test_cw_v2_trigger_includes_flip_bar():
    strat = _strat()
    _arm_to_watch(strat, strat.watchlist_state("TEST"))  # asserts trigger == 12.0 inside


def test_cw_v2_intrabar_break_enters():
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    # below-trigger quotes don't enter; the first quote above 12.0 with a full bar above 9.5 does.
    assert strat._cw_v2_quote(state, _quote(11.5)) is None
    draft = strat._cw_v2_quote(state, _quote(12.5))
    assert draft is not None
    assert draft.side == "buy" and draft.intent_type == "open"
    assert draft.metadata["atr_variant"] == "CW-v2"
    assert draft.quantity == Decimal("10")
    assert state.cw_entries_this_flip == 1 and state.cw_v2_emit_claimed is True


def test_cw_v2_rule7_blocks_bar_that_dipped_below_flip_level():
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    # forming bar dips to 9.0 (below the 9.5 flip level) BEFORE the break to 12.5 -> blocked.
    assert strat._cw_v2_quote(state, _quote(9.0)) is None    # sets low-so-far = 9.0
    assert strat._cw_v2_quote(state, _quote(12.5)) is None    # break, but low-so-far 9.0 <= 9.5
    assert state.cw_entries_this_flip == 0


def test_cw_v2_orb_window_skips_entry():
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    assert strat._cw_v2_quote(state, _quote(12.5, ts=ORB_MS)) is None   # 09:45 ET
    assert state.cw_entries_this_flip == 0
    assert strat._cw_v2_quote(state, _quote(12.5, ts=NON_ORB_MS)) is not None  # 11:00 ET fires


def test_cw_v2_sell_flip_cancels():
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    strat._cw_v2_track(state, _sig(flip="SELL"))
    assert state.cw_armed is False
    assert strat._cw_v2_quote(state, _quote(12.5)) is None


# --------------------------------------------------------------- reclaim (max 2)

def test_cw_v2_reclaim_two_then_capped():
    strat = _strat_reclaim()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)

    # entry #1
    assert strat._cw_v2_quote(state, _quote(12.5)) is not None
    assert state.cw_entries_this_flip == 1
    # claimed -> a 2nd break before the fill is blocked
    assert strat._cw_v2_quote(state, _quote(12.6)) is None

    # fill then exit -> update_position True->False releases the claim (no cooldown for reclaim)
    state.position_qty = 10
    assert strat._cw_v2_quote(state, _quote(12.7)) is None   # in position -> flat gate
    strat.update_position("TEST", 0)
    assert state.cw_v2_emit_claimed is False

    strat._cw_v2_track(state, _sig())    # next bar: reset forming-bar low
    # entry #2 (reclaim)
    assert strat._cw_v2_quote(state, _quote(12.5)) is not None
    assert state.cw_entries_this_flip == 2

    # cap: after a 2nd exit, a further break is blocked (2 per flip)
    state.position_qty = 10
    strat.update_position("TEST", 0)
    strat._cw_v2_track(state, _sig())
    assert strat._cw_v2_quote(state, _quote(12.5)) is None
    assert state.cw_entries_this_flip == 2

    # a fresh BUY flip re-arms the counter
    _feed_bar(strat, state, _bar(20.0, ts=99), _sig(flip="BUY", flip_level=15.0))
    assert state.cw_entries_this_flip == 0 and state.cw_trigger == 20.0


# --------------------------------------------------------------- reclaim = new segment high (2026-07-13 fix)

def _release(state):
    """Simulate the position opening then fully closing -> reclaim claim released, flat."""
    state.position_qty = 0
    state.cw_v2_emit_claimed = False


def test_cw_v2_reclaim_requires_new_segment_high():
    """The reclaim (2nd entry) must break a genuine NEW high across ALL bars since the flip —
    NOT re-cross the flip+2 3-bar trigger. This is the 2026-07-13 SOBR over-trading fix."""
    strat = _strat_reclaim()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)                       # 3-bar trigger 12.0, segment_high 12.0
    assert strat._cw_v2_quote(state, _quote(12.5)) is not None      # 1st entry breaks 12.0
    assert state.cw_entries_this_flip == 1
    _release(state)
    # the name runs on -> the segment high advances to 15.0 over the next bars
    _feed_bar(strat, state, _bar(15.0, ts=4), _sig())
    assert state.cw_segment_high == 15.0
    # a quote that re-crosses the OLD 3-bar trigger (13.0 > 12.0) but is BELOW the new segment high
    # must NOT reclaim (the bug: it used to enter here on a mere bounce).
    assert strat._cw_v2_quote(state, _quote(13.0)) is None
    assert state.cw_entries_this_flip == 1
    # only a break of the NEW segment high (>15.0) reclaims.
    assert strat._cw_v2_quote(state, _quote(15.5)) is not None
    assert state.cw_entries_this_flip == 2


def test_cw_v2_cap_two_per_flip_segment():
    """Hard cap: no 3rd entry in the same BUY-flip segment, even on a further new high."""
    strat = _strat_reclaim()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    strat._cw_v2_quote(state, _quote(12.5))           # n=1
    _release(state)
    _feed_bar(strat, state, _bar(15.0, ts=4), _sig())
    strat._cw_v2_quote(state, _quote(15.5))           # n=2 (reclaim)
    assert state.cw_entries_this_flip == 2
    _release(state)
    _feed_bar(strat, state, _bar(18.0, ts=5), _sig())  # segment high advances again
    assert strat._cw_v2_quote(state, _quote(18.5)) is None   # capped at 2 -> no 3rd
    assert state.cw_entries_this_flip == 2


def test_cw_v2_segment_high_advances_every_bar_incl_no_signal():
    """The reclaim lookback grows on EVERY bar since the flip, even a bar with no ATR signal."""
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)                       # segment_high 12.0
    _feed_bar(strat, state, _bar(13.5, ts=4), _sig())
    assert state.cw_segment_high == 13.5
    _feed_bar(strat, state, _bar(14.2, ts=5), None)   # NO atr signal this bar
    assert state.cw_segment_high == 14.2


def test_cw_v2_new_buy_flip_reseeds_segment_high_and_cap():
    """A fresh BUY flip starts a NEW segment: reclaim counter resets and the segment high re-seeds
    to the flip bar (so the prior segment's high does not carry over)."""
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    strat._cw_v2_quote(state, _quote(12.5))           # n=1 in segment A
    _feed_bar(strat, state, _bar(20.0, ts=6), _sig(flip="SELL"))   # segment A ends
    assert state.cw_armed is False
    _feed_bar(strat, state, _bar(8.0, ts=7), _sig(flip="BUY", flip_level=6.0))  # new segment B
    assert state.cw_entries_this_flip == 0            # cap reset
    assert state.cw_segment_high == 8.0               # re-seeded to the new flip bar (not 20.0)


# --- reclaim 1-bar gap (2026-07-14; backtest: same-bar reclaim bleeds) ---


def test_cw_v2_reclaim_gap1_blocks_same_bar_then_allows_next_bar():
    strat = _strat_reclaim(strategy_schwab_1m_v2_cw_v2_reclaim_gap_bars=1)
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    assert strat._cw_v2_quote(state, _quote(12.5)) is not None      # entry #1
    assert state.cw_entries_this_flip == 1
    state.position_qty = 10
    strat.update_position("TEST", 0)                                # exit -> release + reset counter
    assert state.cw_v2_emit_claimed is False
    assert state.cw_v2_bars_since_exit == 0
    # SAME bar: reclaim blocked by the 1-bar gap
    assert strat._cw_v2_quote(state, _quote(12.6)) is None
    assert state.cw_entries_this_flip == 1
    # a NEW bar arrives -> bars_since_exit=1 -> reclaim now allowed
    strat._cw_v2_track(state, _sig())
    assert state.cw_v2_bars_since_exit == 1
    assert strat._cw_v2_quote(state, _quote(12.5)) is not None
    assert state.cw_entries_this_flip == 2


def test_cw_v2_reclaim_gap0_allows_same_bar_byte_identical():
    strat = _strat_reclaim()  # gap defaults to 0
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    assert strat._cw_v2_quote(state, _quote(12.5)) is not None
    state.position_qty = 10
    strat.update_position("TEST", 0)
    # same-bar reclaim allowed (no gap) — unchanged from before
    assert strat._cw_v2_quote(state, _quote(12.6)) is not None
    assert state.cw_entries_this_flip == 2


# --- reclaim master switch (2026-07-15 operator rule: reclaim OFF, code retained) ---


def test_cw_v2_reclaim_flag_defaults_off():
    assert Settings().strategy_schwab_1m_v2_cw_v2_reclaim_enabled is False
    strat = _strat()
    assert strat._cw_v2_reclaim_enabled is False
    assert strat._cw_v2_max_entries_per_flip == 1


def test_cw_v2_reclaim_flag_on_restores_two_per_flip():
    strat = _strat_reclaim()
    assert strat._cw_v2_reclaim_enabled is True
    assert strat._cw_v2_max_entries_per_flip == 2


def test_cw_v2_reclaim_off_allows_one_entry_per_flip_segment():
    """THE operator rule: one entry per BUY-flip. The 2nd break — even a genuine NEW segment
    high, which the reclaim path would have taken — must not enter."""
    strat = _strat()                                  # reclaim OFF (default)
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    assert strat._cw_v2_quote(state, _quote(12.5)) is not None      # entry #1
    assert state.cw_entries_this_flip == 1
    _release(state)                                   # flat again, claim released
    _feed_bar(strat, state, _bar(15.0, ts=4), _sig())  # segment high advances to 15.0
    assert strat._cw_v2_quote(state, _quote(15.5)) is None          # NO reclaim
    assert state.cw_entries_this_flip == 1


def test_cw_v2_reclaim_off_still_re_arms_on_a_fresh_buy_flip():
    """Reclaim-off caps entries per SEGMENT, not per day — a new BUY flip is a new segment
    and must still be enterable (otherwise the name goes dead after one trade)."""
    strat = _strat()                                  # reclaim OFF
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    assert strat._cw_v2_quote(state, _quote(12.5)) is not None      # entry #1, segment A
    _release(state)
    _feed_bar(strat, state, _bar(20.0, ts=6), _sig(flip="SELL"))    # segment A ends
    # new segment B: BUY flip + 2 bars -> armed again, counter reset
    _feed_bar(strat, state, _bar(8.0, ts=7), _sig(flip="BUY", flip_level=6.0))
    assert state.cw_entries_this_flip == 0
    _feed_bar(strat, state, _bar(7.5, ts=8), _sig())
    _feed_bar(strat, state, _bar(7.8, ts=9), _sig())
    strat._cw_v2_track(state, _sig())                 # watch phase
    assert strat._cw_v2_quote(state, _quote(8.5)) is not None       # entry in segment B
    assert state.cw_entries_this_flip == 1


def test_cw_v2_reclaim_off_leaves_gap_setting_inert():
    """The reclaim-gap knob is retained but unreachable while reclaim is off (no 2nd entry)."""
    strat = _strat(strategy_schwab_1m_v2_cw_v2_reclaim_gap_bars=1)
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    assert strat._cw_v2_quote(state, _quote(12.5)) is not None
    _release(state)
    strat._cw_v2_track(state, _sig())                 # a new bar passes the gap
    _feed_bar(strat, state, _bar(15.0, ts=4), _sig())
    assert strat._cw_v2_quote(state, _quote(15.5)) is None          # still capped at 1
    assert state.cw_entries_this_flip == 1


# --------------------------------------------------------------- stale-trigger / gated chase
# 2026-07-15 SOBR (real money): the 1st entry used the FROZEN flip+2 trigger, which never advances.
# The gates (ORB window / entry window / position held) only DEFER -- they never disarm -- so a setup
# broken while gated fired the instant the gate lifted, at ANY price above a level the stock had left
# behind. Trigger 1.69 (~09:30) -> ran to 2.63 (+56%) during the 09:30-10:00 ORB blackout -> bought
# 2.24 at 10:00:07 (+32.5% past trigger) into a fading move -> -5.25% stop in 46s, peak 0.00%.
# Fix: break the HIGHEST bar since the flip (cw_segment_high), for the 1st entry too.

def test_sobr_replay_gated_break_does_not_chase_the_completed_move():
    """THE REGRESSION ANCHOR. Break happens while the ORB window gates us; price runs far above the
    frozen trigger; when the gate lifts we must NOT buy the top."""
    strat = _strat()
    st = strat.watchlist_state("SOBR")
    _arm_to_watch(strat, st)                       # trigger = 12.0

    # the break happens DURING the ORB blackout -> gated, no entry
    assert strat._cw_v2_quote(st, _quote(12.5, ts=ORB_MS)) is None

    # the stock runs hard while we are gated; closed bars carry the new highs
    _feed_bar(strat, st, _bar(15.0, low=12.4, ts=4), _sig())
    _feed_bar(strat, st, _bar(15.8, low=14.9, ts=5), _sig())
    assert st.cw_segment_high == 15.8              # the highest bar since the flip
    assert st.cw_trigger == 12.0                   # frozen (retained for telemetry)

    # gate lifts. price is 12.9 -- above the STALE trigger but WELL below the real high.
    # pre-fix this entered at 12.9 (+7.5% past trigger, into a fading move). It must not.
    assert strat._cw_v2_quote(st, _quote(12.9)) is None
    assert st.cw_entries_this_flip == 0


def test_break_of_the_new_segment_high_still_enters():
    """The fix must not make us un-tradeable: breaking the ACTUAL highest bar is a real entry."""
    strat = _strat()
    st = strat.watchlist_state("SOBR")
    _arm_to_watch(strat, st)
    _feed_bar(strat, st, _bar(15.0, low=12.4, ts=4), _sig())
    strat._cw_v2_track(st, _sig())                 # new forming bar -> reset low-so-far
    draft = strat._cw_v2_quote(st, _quote(15.2))   # breaks the 15.0 segment high
    assert draft is not None and draft.side == "buy"
    assert st.cw_entries_this_flip == 1


def test_prompt_break_is_byte_identical_to_the_frozen_trigger():
    """At arming, segment_high == cw_trigger (same 3 bars), so a break within ms of the cross behaves
    exactly as before the fix -- the normal path is untouched (cf. KUST: px 1.4950 vs trig 1.4900)."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    _arm_to_watch(strat, st)
    assert st.cw_segment_high == st.cw_trigger == 12.0   # the invariant the fix relies on
    draft = strat._cw_v2_quote(st, _quote(12.01))
    assert draft is not None and draft.side == "buy"


def test_orb_window_block_is_no_longer_silent():
    """2026-07-15: the ORB block was `return None` with no log, so the SOBR chase was invisible until
    the operator spotted it on a chart. It must announce itself -- once per flip, not per quote."""
    strat = _strat()
    st = strat.watchlist_state("SOBR")
    _arm_to_watch(strat, st)
    assert st.cw_orb_block_logged is False
    strat._cw_v2_quote(st, _quote(12.5, ts=ORB_MS))
    assert st.cw_orb_block_logged is True          # logged
    strat._cw_v2_quote(st, _quote(12.6, ts=ORB_MS))
    assert st.cw_orb_block_logged is True          # still one-shot (this runs on EVERY quote)
    # a fresh BUY flip re-arms the one-shot
    _feed_bar(strat, st, _bar(20.0, ts=9), _sig(flip="BUY", flip_level=18.0))
    assert st.cw_orb_block_logged is False
