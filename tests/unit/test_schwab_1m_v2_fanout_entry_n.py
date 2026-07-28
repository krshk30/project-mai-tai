"""`cw_entry_n` must agree between a fan-out leg and the Schwab primary it was fired beside.

⭐ WHY THIS MATTERS. `cw_entry_n` + `cw_arm_bar_ts` are the ONLY reliable first-entry-vs-reclaim
split (grouping by `cw_flip_level` silently merges distinct segments -- it repeats whenever the ATR
trail has not moved). Reclaim was turned back ON 2026-07-27 and is being judged on live fills, so a
mislabelled `cw_entry_n` does not just dirty a column: it moves trades between the two buckets whose
difference is the entire question (July live: reclaims win 38% / median -4.98%, firsts 58% / +1.93%).

⛔ THE BUG (shipped in #570). `_build_webull_fanout_draft` derived the tag itself as
`cw_entries_this_flip + 1`. But the three fan-out sites sit on OPPOSITE SIDES of the
`cw_entries_this_flip += 1` that the reactive path performs:

    reactive     increments FIRST, then builds the leg  -> "+1" double-counts
    eh_resting   no increment (the resting order has not filled) -> "+1" correct
    rth_resting  no increment                                     -> "+1" correct

So every REACTIVE fan-out leg was tagged one HIGHER than its own Schwab primary -- a first entry
labelled as a reclaim, on exactly the path reclaims use.

THE FIX: `entry_n` is supplied by the caller and never derived inside the helper.
"""
from __future__ import annotations

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy


def _strat():
    return SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
    ))


def _draft(strat, state, *, source, entry_n):
    return strat._build_webull_fanout_draft(
        state, entry_px=5.0, session_is_eh=False, source=source, entry_n=entry_n,
    )


def test_reactive_leg_matches_its_primary_after_the_increment() -> None:
    """THE REGRESSION. On the reactive path the counter is ALREADY incremented, so the leg must
    carry the counter as-is. Deriving `+1` inside the helper tagged this '2'."""
    strat = _strat()
    st = strat.watchlist_state("EGG")
    st.cw_entries_this_flip = 1                 # the reactive path just incremented for THIS entry
    d = _draft(strat, st, source="reactive", entry_n=st.cw_entries_this_flip)
    assert d.metadata["cw_entry_n"] == "1", (
        "the fan-out leg disagrees with its own Schwab primary -- a FIRST entry tagged as a reclaim"
    )


def test_resting_legs_are_the_next_entry_because_nothing_incremented_yet() -> None:
    """Both resting paths fire BEFORE any increment, so the leg is entry n+1."""
    strat = _strat()
    st = strat.watchlist_state("EGG")
    st.cw_entries_this_flip = 0                 # nothing has entered in this segment yet
    for source in ("eh_resting", "rth_resting"):
        d = _draft(strat, st, source=source, entry_n=st.cw_entries_this_flip + 1)
        assert d.metadata["cw_entry_n"] == "1", source


def test_a_real_reclaim_is_still_tagged_2_on_every_path() -> None:
    """The fix must not flatten everything to '1' -- a genuine second entry must still read 2."""
    strat = _strat()
    st = strat.watchlist_state("EGG")

    st.cw_entries_this_flip = 2                                  # reactive, post-increment
    assert _draft(strat, st, source="reactive",
                  entry_n=st.cw_entries_this_flip).metadata["cw_entry_n"] == "2"

    st.cw_entries_this_flip = 1                                  # resting, pre-increment
    assert _draft(strat, st, source="rth_resting",
                  entry_n=st.cw_entries_this_flip + 1).metadata["cw_entry_n"] == "2"


def test_entry_n_is_caller_supplied_not_derived() -> None:
    """PINS THE DESIGN. The helper must not reach for `cw_entries_this_flip` itself; if it did,
    these two calls -- same state, different callers -- could not differ."""
    strat = _strat()
    st = strat.watchlist_state("EGG")
    st.cw_entries_this_flip = 1
    assert _draft(strat, st, source="reactive", entry_n=1).metadata["cw_entry_n"] == "1"
    assert _draft(strat, st, source="rth_resting", entry_n=2).metadata["cw_entry_n"] == "2"


def test_arm_bar_ts_travels_with_it() -> None:
    """`cw_entry_n` is only meaningful paired with the segment id."""
    strat = _strat()
    st = strat.watchlist_state("EGG")
    st.cw_arm_bar_ts = 1785257880000
    d = _draft(strat, st, source="reactive", entry_n=1)
    assert d.metadata["cw_arm_bar_ts"] == "1785257880000"
