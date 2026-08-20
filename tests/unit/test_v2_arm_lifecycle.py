"""B19 / B20 — an arm must END, and ending it must be a TRANSITION nobody has to infer.

⛔⭐⭐ ARMED IS NOT A POSITION. It is bar-driven state, so it only ever ended when a later BAR drove
the state machine to a SELL flip. Two situations produce no such bar, and both left the arm frozen:

  **B19** — the symbol LEAVES the watchlist. We stop watching, no bars arrive, nothing drives the
  flip. `cw_armed` stays True forever and `cw_armed_segments()` — which the restart gate reads —
  keeps reporting a segment nobody is watching. That is how stopping a symbol made the restart gate
  red until the next restart.

  **B20** — the 16:00 ET entry-window close. Arming is bar-driven and bars flow to 20:00, so a
  symbol arming after 16:00 is NORMAL; the arm simply cannot lead anywhere any more, because the
  window that gives it meaning has shut. Carried overnight it only misreports.

⛔ The pre-existing `drop_symbol` (a bare `pop`) is NOT the fix and these tests say so: popping makes
the arm VANISH with no transition and no log line. Silent deletion and silent freezing are the same
defect from opposite ends — in neither case can a reader tell what happened to the segment.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy

ET = ZoneInfo("America/New_York")


def _strategy() -> SchwabV2Strategy:
    return SchwabV2Strategy(
        Settings(
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
        )
    )


def _arm(strat, symbol: str, *, qty: int = 0, held: int = 0, resting: bool = False):
    st = strat.watchlist_state(symbol)
    st.cw_armed = True
    st.cw_arm_bar_ts = 1_787_000_000_000
    st.cw_entries_this_flip = 1
    st.position_qty = qty
    st.position_qty_held = held
    st.resting_active = resting
    return st


# ------------------------------------------------------------------ B19
def test_leaving_the_watchlist_DISARMS_rather_than_freezes(caplog) -> None:
    strat = _strategy()
    _arm(strat, "AAA")
    with caplog.at_level(logging.INFO):
        released = strat.release_and_drop_symbol("AAA")
    assert released is True
    assert "AAA" not in strat._symbol_states, "the state should also be dropped"
    msgs = [r.getMessage() for r in caplog.records if "V2-CW-DISARM" in r.getMessage()]
    assert msgs and "reason=watchlist-removed" in msgs[0]


def test_the_SILENT_POP_helper_is_GONE_not_merely_unused() -> None:
    """⛔⭐⭐ `drop_symbol` popped the state and said NOTHING — no transition, no log line. A reader
    could not tell an ended segment from one that merely stopped being observed, which is the same
    blindness as freezing, approached from the other side.

    It had ZERO callers, so it was dead code offering a silent alternative to the correct path.
    Removed rather than left in place: a wrong mechanism that is merely unused is an invitation to
    use it. If it comes back, this fails."""
    assert not hasattr(SchwabV2Strategy, "drop_symbol"), (
        "the silent-pop helper is back — a segment can end without a transition again"
    )


def test_releasing_a_symbol_that_was_never_armed_is_not_an_event(caplog) -> None:
    """⛔ LOG THE TRANSITION, NOT THE ASSIGNMENT — the house rule one line above `cw_armed=False`."""
    strat = _strategy()
    strat.watchlist_state("AAA")
    with caplog.at_level(logging.INFO):
        assert strat.release_and_drop_symbol("AAA") is False
    assert not [r for r in caplog.records if "V2-CW-DISARM" in r.getMessage()]


def test_releasing_an_unknown_symbol_is_harmless() -> None:
    assert _strategy().release_and_drop_symbol("NOPE") is False


def test_the_release_clears_the_SEGMENT_SLOTS_too() -> None:
    """The release must make the same writes the SELL-flip 'segment over' branch makes. A segment
    that ends with its slots still consumed is a segment that ended halfway."""
    strat = _strategy()
    st = _arm(strat, "AAA")
    st.cw_resting_taken = True
    st.cw_reclaim_taken = True
    strat._release_arm(st, "test")
    assert st.cw_armed is False
    assert st.cw_arm_bar_ts == 0
    assert st.cw_entries_this_flip == 0
    assert st.cw_resting_taken is False and st.cw_reclaim_taken is False


# ------------------------------------------------------------------ B20
def test_entry_window_close_releases_a_FLAT_armed_symbol() -> None:
    strat = _strategy()
    _arm(strat, "AAA")
    assert strat.release_arms_at_entry_window_close() == ["AAA"]
    assert strat.watchlist_state("AAA").cw_armed is False


def test_a_HELD_symbol_keeps_its_arm() -> None:
    """⛔⭐⭐ THE SAFETY CONDITION. We may only release what we do not hold."""
    strat = _strategy()
    _arm(strat, "HELD", qty=10, held=10)
    assert strat.release_arms_at_entry_window_close() == []
    assert strat.watchlist_state("HELD").cw_armed is True


def test_the_UNION_qty_alone_is_enough_to_keep_the_arm() -> None:
    """⛔ Conservative on purpose: an in-flight open intent (union > 0, fills 0) still counts as
    'we may be in this'. Releasing on fills-only would drop the arm mid-entry."""
    strat = _strategy()
    _arm(strat, "INFLIGHT", qty=10, held=0)
    assert strat.release_arms_at_entry_window_close() == []


def test_a_protected_symbol_keeps_its_arm() -> None:
    """The predicate is the session roll's, passed in — held / resting-active / operator-protected
    / mid-warmup. Re-deriving those rules here would be a second copy that drifts."""
    strat = _strategy()
    _arm(strat, "AAA")
    _arm(strat, "BBB")
    out = strat.release_arms_at_entry_window_close(is_protected=lambda sym, st: sym == "AAA")
    assert out == ["BBB"]
    assert strat.watchlist_state("AAA").cw_armed is True


def test_an_unarmed_symbol_is_not_reported_as_released() -> None:
    strat = _strategy()
    strat.watchlist_state("FLAT")
    assert strat.release_arms_at_entry_window_close() == []


# ------------------------------------------------------------------ the safety claim, pinned
def test_NO_AFTER_HOURS_EXIT_PATH_READS_cw_armed() -> None:
    """⛔⭐⭐ THE CONDITION THAT MAKES B20 SAFE — pinned in the test suite, not just argued in a
    docstring.

    B20 is only safe because releasing the arm cannot disarm an EXIT. Verified before building:

      * the software exit ladder arms off `OmsService._cw_floor_armed`, not `state.cw_armed`;
      * `_maybe_cw_flip_close` — the bar-close ATR exit that has NO RTH gate, and therefore the one
        exit genuinely live past 16:00 — gates on `_cw_enabled`, `position_qty > 0` and
        `flip == "SELL"`.

    If a future exit path starts reading `cw_armed`, THIS is the test that should fail first, and
    B20 must be re-argued before it ships again.
    """
    import inspect

    from project_mai_tai.exit_logic import cw_exit

    src = inspect.getsource(SchwabV2Strategy._maybe_cw_flip_close)
    assert "cw_armed" not in src, (
        "_maybe_cw_flip_close now reads cw_armed — B20 releases that flag after 16:00 and would "
        "disarm this exit for a position held past the bell"
    )
    assert "cw_armed" not in inspect.getsource(cw_exit), (
        "the shared exit ladder now reads cw_armed — B20's safety argument no longer holds"
    )


# ------------------------------------------------------------------ B19 at the BOT boundary
def test_the_BOT_actually_releases_a_departing_symbol(caplog) -> None:
    """⛔⭐⭐ THE WIRING, NOT JUST THE HELPER — and the gap that let a real bug through.

    The strategy-level tests above all passed while `departed_symbols` was computed from
    `self._watchlist` AFTER that attribute had already been reassigned to the new selection, which
    makes the set EMPTY and the release a no-op on every symbol, forever. A mutant reproducing
    exactly that survived the first mutation pass.

    ⭐ The lesson is the ordinary one: a helper proven in isolation proves nothing about the call
    site. This drives the real watchlist path end to end.
    """
    from project_mai_tai.events import (
        StrategyStateSnapshotEvent,
        StrategyStateSnapshotPayload,
    )
    from project_mai_tai.settings import Settings as _S
    from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService

    bot = SchwabV2BotService(
        _S(
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
        ),
        session_factory=None,
    )
    bot._watchlist = {"AAA", "GOING"}
    for sym in ("AAA", "GOING"):
        st = bot.strategy.watchlist_state(sym)
        st.cw_armed = True
        st.cw_arm_bar_ts = 1_787_000_000_000

    event = StrategyStateSnapshotEvent(
        source_service="strategy-engine",
        payload=StrategyStateSnapshotPayload(watchlist=["AAA"]),
    )
    with caplog.at_level(logging.INFO):
        bot._apply_strategy_state_event({"data": event.model_dump_json()}, max_watchlist=25)

    assert bot._watchlist == {"AAA"}
    msgs = [r.getMessage() for r in caplog.records if "V2-CW-DISARM" in r.getMessage()]
    assert any("GOING" in m and "watchlist-removed" in m for m in msgs), (
        "the departing symbol was never disarmed — check that departed_symbols is captured "
        "BEFORE self._watchlist is reassigned"
    )
    assert bot.strategy.watchlist_state("AAA").cw_armed is True, "a STAYING symbol must keep its arm"
