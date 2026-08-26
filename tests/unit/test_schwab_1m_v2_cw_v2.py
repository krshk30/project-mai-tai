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


def _bar(high: float, *, vol: int = 25_000, low: float | None = None, ts: int = 0) -> OHLCVBar:
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


def _resting_fill(strat, state, qty: int = 10) -> None:
    """Simulate the RESTING entry filling, which is what consumes the resting slot.

    ⭐ 2026-08-03: `position_qty_held` is FILLS-ONLY, so a 0 -> >0 transition there is a real
    execution. This is the slot the old code never consumed -- the defect that let the reclaim path
    see two free slots and fire two reclaims."""
    strat.update_position(state.symbol, qty, held_qty=qty)


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


def test_cw_v2_no_longer_skips_the_0930_1000_window():
    """Inverted 2026-07-30: the ORB window is removed, so 09:45 enters exactly like 11:00 does.
    The old assertion (in-window => None) is the behaviour that cost APLX/SNDG."""
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    assert strat._cw_v2_quote(state, _quote(12.5, ts=ORB_MS)) is not None   # 09:45 ET now fires
    assert state.cw_entries_this_flip == 1


def test_cw_v2_sell_flip_cancels():
    strat = _strat()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    strat._cw_v2_track(state, _sig(flip="SELL"))
    assert state.cw_armed is False
    assert strat._cw_v2_quote(state, _quote(12.5)) is None


# --------------------------------------------------------------- reclaim (max 2)

def test_cw_v2_reclaim_two_then_capped():
    """RETARGETED 2026-08-03 — the cap is COMPOSITION, not a count.

    Was: two reactive entries then capped at 2. That pinned the SCALAR cap, which permits
    reclaim+reclaim — the composition the operator calls "very bad". The reactive path now owns the
    RECLAIM slot and may fire at most once per cross."""
    strat = _strat_reclaim()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)

    assert strat._cw_v2_quote(state, _quote(12.5)) is not None
    assert state.cw_reclaim_taken is True
    # claimed -> a 2nd break before the fill is blocked
    assert strat._cw_v2_quote(state, _quote(12.6)) is None

    state.position_qty = 10
    strat.update_position("TEST", 0)
    assert state.cw_v2_emit_claimed is False

    strat._cw_v2_track(state, _sig())
    _feed_bar(strat, state, _bar(15.0, ts=4), _sig())
    # ⛔ NO SECOND RECLAIM, however high the break. This is the rule the scalar cap could not express.
    assert strat._cw_v2_quote(state, _quote(15.5)) is None
    assert state.cw_reclaim_taken is True


# --------------------------------------------------------------- reclaim = new segment high (2026-07-13 fix)

def _release(state):
    """Simulate the position opening then fully closing -> reclaim claim released, flat."""
    state.position_qty = 0
    state.cw_v2_emit_claimed = False


def test_cw_v2_reclaim_requires_new_segment_high():
    """The reclaim must break a genuine NEW high across ALL bars since the flip — never re-cross
    the flip+2 3-bar trigger. The 2026-07-13 SOBR over-trading fix, still guarded."""
    strat = _strat_reclaim()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)                       # 3-bar trigger 12.0, segment_high 12.0
    _resting_fill(strat, state)
    strat.update_position("TEST", 0)
    _feed_bar(strat, state, _bar(15.0, ts=4), _sig())
    assert state.cw_segment_high == 15.0
    # re-crossing the OLD 3-bar trigger (13.0 > 12.0) but below the new segment high must NOT enter
    assert strat._cw_v2_quote(state, _quote(13.0)) is None
    assert state.cw_reclaim_taken is False
    # only a break of the NEW segment high reclaims
    assert strat._cw_v2_quote(state, _quote(15.5)) is not None
    assert state.cw_reclaim_taken is True


def test_cw_v2_cap_two_per_flip_segment():
    """RETARGETED — the legal composition is resting + reclaim, and nothing else.

    A resting FILL consumes the resting slot; the reactive path may then take the reclaim once. A
    further break is refused."""
    strat = _strat_reclaim()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)

    _resting_fill(strat, state)                       # resting slot consumed by a real fill
    assert state.cw_resting_taken is True
    strat.update_position("TEST", 0)                  # it exits; the slot STAYS consumed
    assert state.cw_resting_taken is True

    _feed_bar(strat, state, _bar(15.0, ts=4), _sig())
    assert strat._cw_v2_quote(state, _quote(15.5)) is not None    # the reclaim
    assert state.cw_reclaim_taken is True

    _release(state)
    _feed_bar(strat, state, _bar(18.0, ts=5), _sig())
    assert strat._cw_v2_quote(state, _quote(18.5)) is None        # no third entry


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
    """The 1-bar reclaim gap still holds — measured from the RESTING fill's exit."""
    strat = _strat_reclaim(strategy_schwab_1m_v2_cw_v2_reclaim_gap_bars=1)
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    _resting_fill(strat, state)
    strat.update_position("TEST", 0)                  # exit -> release + start the gap clock
    assert state.cw_v2_emit_claimed is False
    assert state.cw_v2_bars_since_exit == 0
    assert strat._cw_v2_quote(state, _quote(12.6)) is None        # same bar: blocked by the gap
    assert state.cw_reclaim_taken is False
    strat._cw_v2_track(state, _sig())                 # a NEW bar
    assert state.cw_v2_bars_since_exit == 1
    assert strat._cw_v2_quote(state, _quote(12.5)) is not None
    assert state.cw_reclaim_taken is True


def test_cw_v2_reclaim_gap0_allows_same_bar_byte_identical():
    """gap=0 -> a same-bar reclaim is allowed, unchanged."""
    strat = _strat_reclaim()  # gap defaults to 0
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    _resting_fill(strat, state)
    strat.update_position("TEST", 0)
    assert strat._cw_v2_quote(state, _quote(12.6)) is not None
    assert state.cw_reclaim_taken is True


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


# --------------------------------------------------------------- 09:30-10:00 window REMOVED
# The ORB suppression was removed 2026-07-30 (operator decision). It reserved the window for
# `project-mai-tai-orb`, DISABLED since 2026-07-23, and -- worse -- it PAUSED the setup instead of
# cancelling it, so every armed symbol was released at one clock edge at 10:00 and bought a stale
# trigger. On the incident day APLX entered +23.7% and SNDG +18.9% past their flip levels.

def test_a_break_inside_0930_1000_now_ENTERS_at_its_own_time():
    """THE REGRESSION: restoring the ORB gate turns this red."""
    strat = _strat()
    st = strat.watchlist_state("SOBR")
    _arm_to_watch(strat, st)
    assert strat._cw_v2_quote(st, _quote(12.5, ts=ORB_MS)) is not None


def test_the_window_is_no_longer_a_special_case_at_all():
    """Pins the PROPERTY, not one timestamp: an in-window break and an out-of-window break are
    treated identically. A partial revert that re-gated only one path would go red here."""
    in_window = _strat()
    out_window = _strat()
    a = in_window.watchlist_state("SOBR")
    b = out_window.watchlist_state("SOBR")
    _arm_to_watch(in_window, a)
    _arm_to_watch(out_window, b)
    got_in = in_window._cw_v2_quote(a, _quote(12.5, ts=ORB_MS))
    got_out = out_window._cw_v2_quote(b, _quote(12.5, ts=NON_ORB_MS))
    assert (got_in is None) == (got_out is None)
    assert got_in is not None


def test_the_orb_window_predicate_is_gone_not_merely_unused():
    """A dormant predicate is an invitation for a future session to re-wire it. Three guards in
    this file have already been found living in replaced code -- do not leave a fourth."""
    assert not hasattr(SchwabV2Strategy, "_cw_in_orb_window")


def test_stale_trigger_behaviour_is_restored():
    """RETARGETED 2026-08-03 — the SOBR chase is now CLOSED, as a side effect of the cap fix.

    This test used to DOCUMENT a known live bug: the first entry rode the FROZEN flip+2 trigger, so
    a quote at 12.9 entered while the real segment high was 15.8 (SOBR 07-15). It was left live
    "pending a narrower fix".

    Making the reactive path the RECLAIM slot is that fix: reactive now breaks the SEGMENT HIGH, so
    a stale-trigger chase can no longer enter. Kept and inverted rather than deleted — a deleted
    test invites someone to restore the chase to make a scalar cap pass again."""
    strat = _strat()
    st = strat.watchlist_state("SOBR")
    _arm_to_watch(strat, st)                       # frozen trigger 12.0
    _feed_bar(strat, st, _bar(15.8, low=12.4, ts=4), _sig())
    strat._cw_v2_track(st, _sig())
    assert st.cw_segment_high == 15.8
    assert strat._cw_v2_quote(st, _quote(12.9)) is None     # the chase no longer enters
    assert strat._cw_v2_quote(st, _quote(15.9)) is not None  # a genuine new high still does


# --------------------------------------------------- resting liquidity RE-check (2026-07-30)
# The arm-time floor is a STALE check. Live that day, ALL FOUR below-floor entries were resting
# orders that passed the floor at placement and filled minutes later into a dried-up tape:
#   APLX placed 13:09 off a 12,530-share bar -> filled 13:19 into a 100-share bar (125x thinner).
# The reactive path is unaffected (market order, checked at emit = fill).

def _in_window(strat):
    """⛔ FREEZE THE RESTING WINDOW. `_resting_in_window()` reads the WALL CLOCK (09:30-16:00 ET),
    so a test that leaves it live passes every afternoon and fails every evening. That is exactly
    what happened: this pair went green locally and in CI at ~15:00 ET, then failed the 19:44 ET
    run with `reason=window_closed` — a red main branch for everyone, caused by the clock."""
    strat._resting_in_window = lambda *a, **k: True
    return strat


def test_a_resting_order_is_CANCELLED_when_liquidity_dries_up():
    """THE REGRESSION: while resting, a below-floor bar must cancel the order."""
    strat = _in_window(_strat(strategy_schwab_1m_v2_atr_flip_vol_floor=10_000,
                   strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True))
    st = strat.watchlist_state("APLX")
    st.resting_active = True
    st.resting_level = 9.03
    st.bars.append(_bar(9.05, vol=100, ts=99))          # 100 shares — far below the 10k floor
    strat._pending_intents.clear()
    strat._cw_v2_resting_track(st, _sig(state="short", trail=9.03))
    assert st.resting_active is False, "a thin tape must not leave the order working"
    assert any(
        d.intent_type == "cancel" for d in strat._pending_intents
    ), "it must CANCEL, never silently skip — a skip is the #580 orphan"


def test_a_resting_order_SURVIVES_while_liquidity_holds():
    """⛔ The guard must not churn a healthy order — that would re-create the reprice storm."""
    strat = _in_window(_strat(strategy_schwab_1m_v2_atr_flip_vol_floor=10_000,
                   strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True))
    st = strat.watchlist_state("APLX")
    st.resting_active = True
    st.resting_level = 9.03
    st.bars.append(_bar(9.05, vol=50_000, ts=99))
    strat._pending_intents.clear()
    strat._cw_v2_resting_track(st, _sig(state="short", trail=9.03))
    assert st.resting_active is True
    assert not any(d.intent_type == "cancel" for d in strat._pending_intents)


# =============================================================================================
# ACCEPTANCE CRITERIA — the composition cap (operator-confirmed 2026-08-03)
# Exactly one RESTING and one RECLAIM per cross. Never two reclaims ("very bad"), never two
# restings. Four live breaches that day: HYFM x3, FUSE x1.
# =============================================================================================


def test_LIVE_BREACH_the_arm_must_not_wipe_the_fill_that_caused_it():
    """⭐⭐ THE BUG, pinned. The resting buy sits AT the ATR line and fills INTRABAR; the arm
    confirms the SAME cross at the BAR CLOSE, 21s-706s later, and used to run
    `cw_entries_this_flip = 0` — wiping the entry that caused the cross. Counting restarted at zero
    and two MORE were allowed: three entries on one cross, four times live on 2026-08-03."""
    strat = _strat_reclaim()
    state = strat.watchlist_state("HYFM")

    _resting_fill(strat, state)                 # 12:09:41 — fills BEFORE the arm
    assert state.cw_resting_taken is True
    _arm_to_watch(strat, state)                 # 12:10:02 — the arm confirms the SAME cross
    assert state.cw_resting_taken is True, "the arm wiped the fill that caused it (the live bug)"

    strat.update_position("HYFM", 0)
    _feed_bar(strat, state, _bar(15.0, ts=4), _sig())
    assert strat._cw_v2_quote(state, _quote(15.5)) is not None     # the one legal reclaim
    _release(state)
    _feed_bar(strat, state, _bar(18.0, ts=5), _sig())
    assert strat._cw_v2_quote(state, _quote(18.5)) is None         # THE THIRD ENTRY IS REFUSED


def test_BOUNDARY_a_genuine_second_cross_starts_clean():
    """⛔ The fix must not over-correct into blocking legitimate re-arms. A cross ENDS at the
    DISARM, and the next one gets its own full resting+reclaim."""
    strat = _strat_reclaim()
    state = strat.watchlist_state("TEST")
    _arm_to_watch(strat, state)
    _resting_fill(strat, state)
    strat.update_position("TEST", 0)
    _feed_bar(strat, state, _bar(15.0, ts=4), _sig())
    assert strat._cw_v2_quote(state, _quote(15.5)) is not None
    assert state.cw_resting_taken and state.cw_reclaim_taken       # this cross is used up
    _release(state)

    _feed_bar(strat, state, _bar(9.0, ts=5), _sig(flip="SELL"))    # the cross ENDS
    assert state.cw_resting_taken is False and state.cw_reclaim_taken is False

    _arm_to_watch(strat, state)                                    # a genuine NEW cross
    assert strat._cw_v2_quote(state, _quote(12.5)) is not None     # gets its own entry


def test_PRIMARY_fill_consumes_its_slot_regardless_of_quantity():
    """The primary Schwab fill consumes its venue-local slot even when its quantity is one.

    `update_position` is fed only by the bot's primary-account position map; the Webull fan-out
    account does not reach this method.  The account-scope boundary is pinned separately in
    `test_schwab_1m_v2_reportable_state.py`.
    """
    strat = _strat_reclaim()
    state = strat.watchlist_state("UPC")
    _arm_to_watch(strat, state)
    _resting_fill(strat, state, qty=1)          # primary sizing may be one; quantity is not identity
    assert state.cw_resting_taken is True
    strat.update_position("UPC", 0)
    _feed_bar(strat, state, _bar(15.0, ts=4), _sig())
    assert strat._cw_v2_quote(state, _quote(15.5)) is not None     # its one reclaim
    _release(state)
    _feed_bar(strat, state, _bar(18.0, ts=5), _sig())
    assert strat._cw_v2_quote(state, _quote(18.5)) is None         # still bounded


def test_AN_EXITED_ENTRY_STILL_CONSUMES_ITS_SLOT():
    """Operator-confirmed: an exit does NOT refill the slot. FUSE 17:03 exited its first entry on
    its own bracket before entries 2 and 3 fired — that is still a breach."""
    strat = _strat_reclaim()
    state = strat.watchlist_state("FUSE")
    _arm_to_watch(strat, state)
    _resting_fill(strat, state)
    strat.update_position("FUSE", 0)            # it exits cleanly
    assert state.cw_resting_taken is True, "an exit must not refill the resting slot"
