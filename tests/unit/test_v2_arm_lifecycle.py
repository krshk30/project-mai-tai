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
from datetime import datetime
from zoneinfo import ZoneInfo

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

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
def test_entry_window_close_releases_every_arm_but_keeps_held_quantity() -> None:
    strat = _strategy()
    _arm(strat, "AAA")
    held = _arm(strat, "HELD", qty=10, held=10)

    census = strat.release_entry_state_at_window_close()

    assert census.evaluated == 2
    assert census.arms_released == ("AAA", "HELD")
    assert census.held_positions == ("HELD",)
    assert not strat.watchlist_state("AAA").cw_armed
    assert not held.cw_armed, "a held position needs EXIT management, not permission to BUY again"
    assert (held.position_qty, held.position_qty_held) == (10, 10), (
        "the close sweep erased the position it is required to keep exit-managed"
    )


def test_partial_fill_cancels_the_remainder_and_preserves_the_filled_quantity() -> None:
    strat = _strategy()
    st = _arm(strat, "PARTIAL", qty=2, held=1, resting=True)
    st.resting_is_broker_order = True
    st.resting_level = 4.25

    census = strat.release_entry_state_at_window_close()
    cancels = [d for d in strat.drain_pending_intents() if d.intent_type == "cancel"]

    assert census.cancel_requested == ("PARTIAL",)
    assert len(cancels) == 1
    assert (st.position_qty, st.position_qty_held) == (2, 1)
    assert not st.cw_armed and not st.resting_active


def test_cancel_keeps_the_original_fanout_identity_until_the_draft_is_built() -> None:
    """The close must cancel first and clear second; reversing those lines launders the cancel
    into a newly minted segment and breaks the identity chain #797 was built to expose."""
    strat = _strategy()
    st = _arm(strat, "MIRROR", resting=True)
    st.resting_is_broker_order = True
    st.webull_resting_active = True
    st.resting_level = 3.20
    st.fanout_segment_id = 1_787_000_123_000

    census = strat.release_entry_state_at_window_close()
    webull_cancel = strat.drain_webull_direct_intents()[0]

    assert census.cancel_requested == ("MIRROR",)
    assert webull_cancel.intent_type == "cancel"
    assert webull_cancel.metadata["fanout_segment_id"] == "1787000123000"
    assert st.fanout_segment_id == 0


def test_software_only_rest_is_released_without_claiming_a_broker_cancel() -> None:
    strat = _strategy()
    st = _arm(strat, "SOFT", resting=True)
    st.resting_is_broker_order = False
    st.resting_level = 2.0

    census = strat.release_entry_state_at_window_close()

    assert census.released == ("SOFT",)
    assert census.cancel_requested == (), "local state clearing was misreported as a broker request"
    assert strat.drain_pending_intents() == []
    assert strat.drain_webull_direct_intents() == []


def test_webull_only_rest_is_cancelled_even_without_a_schwab_broker_order() -> None:
    """The Schwab software-rest branch used to return before reaching the independent Webull
    mirror. A venue-specific working order must not be hidden by the other venue's state."""
    strat = _strategy()
    st = _arm(strat, "WEBULL", resting=True)
    st.resting_is_broker_order = False
    st.webull_resting_active = True
    st.resting_level = 2.0
    st.fanout_segment_id = 1_787_000_456_000

    census = strat.release_entry_state_at_window_close()

    assert census.cancel_requested == ("WEBULL",)
    assert strat.drain_pending_intents() == []
    webull = strat.drain_webull_direct_intents()
    assert len(webull) == 1 and webull[0].intent_type == "cancel"
    assert webull[0].metadata["fanout_segment_id"] == "1787000456000"


def test_close_sweep_reports_a_clean_population_without_inventing_transitions(caplog) -> None:
    strat = _strategy()
    strat.watchlist_state("CLEAN")
    with caplog.at_level(logging.INFO):
        census = strat.release_entry_state_at_window_close()
    assert census.evaluated == 1 and census.released == ()
    assert "V2-CW-DISARM" not in caplog.text


def test_close_clock_has_both_polarities_and_uses_the_0400_session_anchor() -> None:
    strat = _strategy()
    assert not strat._entry_window_closed_for_session(datetime(2026, 8, 26, 15, 59, tzinfo=ET))
    assert strat._entry_window_closed_for_session(datetime(2026, 8, 26, 16, 0, tzinfo=ET))
    assert strat._entry_window_closed_for_session(datetime(2026, 8, 27, 3, 59, tzinfo=ET))
    assert not strat._entry_window_closed_for_session(datetime(2026, 8, 27, 4, 0, tzinfo=ET))


def _bar(ts: datetime) -> OHLCVBar:
    return OHLCVBar(
        timestamp_ms=int(ts.timestamp() * 1000),
        open=5.0,
        high=5.2,
        low=4.9,
        close=5.1,
        volume=10_000,
    )


def test_a_real_BUY_flip_arms_before_close_and_is_blocked_after_close(monkeypatch, caplog) -> None:
    """Both halves: the guard can fire, and it can stay quiet without disabling the real tracker."""
    before = _strategy()
    before_state = before.watchlist_state("BEFORE")
    before_at = datetime(2026, 8, 26, 15, 59, tzinfo=ET)
    before_state.bars.append(_bar(before_at))
    monkeypatch.setattr(before, "_now_ms", lambda: int(before_at.timestamp() * 1000))
    monkeypatch.setattr(
        before,
        "_update_atr_state",
        lambda *_: {"flip": "BUY", "flip_level": 4.8, "trail": 4.8},
    )
    with caplog.at_level(logging.INFO):
        before._evaluate_completed_bar(before_state, is_new_bar=True)
    assert before_state.cw_armed is True, "the control never reached the real BUY-arm path"
    assert "V2-POST-CLOSE-ENTRY-BLOCKED" not in caplog.text

    caplog.clear()
    after = _strategy()
    after_state = after.watchlist_state("AFTER")
    after_at = datetime(2026, 8, 26, 16, 1, tzinfo=ET)
    after_state.bars.append(_bar(after_at))
    monkeypatch.setattr(after, "_now_ms", lambda: int(after_at.timestamp() * 1000))
    monkeypatch.setattr(
        after,
        "_update_atr_state",
        lambda *_: {"flip": "BUY", "flip_level": 4.8, "trail": 4.8},
    )
    with caplog.at_level(logging.INFO):
        assert after._evaluate_completed_bar(after_state, is_new_bar=True) is None
    assert after_state.cw_armed is False
    assert "[V2-POST-CLOSE-ENTRY-BLOCKED] AFTER evaluated=1 blocked=1" in caplog.text


def test_a_held_position_still_emits_its_SELL_exit_after_close(monkeypatch) -> None:
    strat = _strategy()
    state = _arm(strat, "HELD", qty=1, held=1)
    after_at = datetime(2026, 8, 26, 16, 2, tzinfo=ET)
    state.bars.append(_bar(after_at))
    monkeypatch.setattr(strat, "_now_ms", lambda: int(after_at.timestamp() * 1000))
    monkeypatch.setattr(
        strat,
        "_update_atr_state",
        lambda *_: {"flip": "SELL", "flip_level": 5.2, "trail": 5.2},
    )

    draft = strat._evaluate_completed_bar(state, is_new_bar=True)

    assert draft is not None and draft.intent_type == "close" and draft.side == "sell"
    assert state.cw_armed is False
    assert state.position_qty_held == 1


def test_bot_keeps_listening_to_all_symbols_but_keeps_zero_entry_arms(caplog) -> None:
    """The live symptom was five listened symbols but only DAIC/YYGH armed. Listening is a
    subscription state, not entry permission: the close sweep must leave all five subscriptions
    present while reducing the arm population to zero."""
    from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService

    bot = SchwabV2BotService(_strategy().settings, session_factory=None)
    bot._watchlist = {"CRE", "DAIC", "WSHP", "XPON", "YYGH"}
    for symbol in bot._watchlist:
        bot.strategy.watchlist_state(symbol)
    _arm(bot.strategy, "DAIC")
    _arm(bot.strategy, "YYGH")
    close = datetime(2026, 8, 26, 16, 0, tzinfo=ET)

    with caplog.at_level(logging.INFO):
        census = bot._release_entry_state_at_window_close(close)

    assert census is not None and census.evaluated == 5 and len(census.arms_released) == 2
    assert bot.strategy.cw_armed_segments() == []
    assert bot._subscription_symbols() == bot._watchlist
    assert "evaluated=5 released=2 arms_released=2" in caplog.text
    assert "armed_after_close=0" in caplog.text

    # 01:00 ET still belongs to the previous 04:00-anchored session. It must not consume the next
    # day's close key and it must not emit a second census for the same close.
    assert bot._release_entry_state_at_window_close(
        datetime(2026, 8, 27, 1, 0, tzinfo=ET)
    ) is None


def test_position_poll_emits_the_close_cancel_without_waiting_for_another_bar(monkeypatch) -> None:
    import asyncio

    from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService

    class _Sink:
        def __init__(self) -> None:
            self.drafts = []

        async def emit(self, draft) -> None:
            self.drafts.append(draft)

    bot = SchwabV2BotService(_strategy().settings, session_factory=None)
    bot._watchlist = {"REST"}
    st = _arm(bot.strategy, "REST", resting=True)
    st.resting_is_broker_order = True
    st.resting_level = 2.50
    sink = _Sink()
    bot.intent_emitter = sink
    close = datetime(2026, 8, 26, 16, 0, tzinfo=ET)
    real_release = bot._release_entry_state_at_window_close
    monkeypatch.setattr(bot, "_release_entry_state_at_window_close", lambda: real_release(close))
    monkeypatch.setattr(bot, "_fetch_position_maps", lambda: ({"REST": 1}, {}))
    monkeypatch.setattr(bot, "_fetch_managed_symbols", lambda: set())
    monkeypatch.setattr(bot, "_roll_stale_session_state", lambda *_: None)

    asyncio.run(bot._position_poll_pass())

    assert len(sink.drafts) == 1 and sink.drafts[0].intent_type == "cancel"
    assert not st.resting_active and not st.cw_armed


def test_unknown_position_read_still_blocks_entry_and_retains_known_held_state(
    monkeypatch, caplog
) -> None:
    """COULD_NOT_TELL is fail-closed for ENTRY and fail-preserving for EXIT. A failed poll must not
    turn the last known held quantity into zero, but it also must not leave permission to buy."""
    import asyncio

    from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService

    bot = SchwabV2BotService(_strategy().settings, session_factory=None)
    bot._watchlist = {"UNKNOWN"}
    st = _arm(bot.strategy, "UNKNOWN", qty=1, held=1)
    close = datetime(2026, 8, 26, 16, 0, tzinfo=ET)
    real_release = bot._release_entry_state_at_window_close
    monkeypatch.setattr(bot, "_release_entry_state_at_window_close", lambda: real_release(close))
    monkeypatch.setattr(bot, "_fetch_position_maps", lambda: None)

    with caplog.at_level(logging.WARNING):
        asyncio.run(bot._position_poll_pass())

    assert not st.cw_armed
    assert (st.position_qty, st.position_qty_held) == (1, 1), (
        "an unreadable broker/DB poll was silently converted into flat and weakened exit protection"
    )
    assert "result=COULD_NOT_TELL entry_permission=BLOCKED known_position_state=RETAINED" in caplog.text


def test_scanner_protection_uses_last_known_position_when_the_read_is_unknown(monkeypatch) -> None:
    from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService

    bot = SchwabV2BotService(_strategy().settings, session_factory=None)
    bot.strategy.update_position("HELD", 2, held_qty=2)
    monkeypatch.setattr(bot, "_fetch_position_maps", lambda: None)

    assert bot._fetch_open_positions() == {"HELD": 2}


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
