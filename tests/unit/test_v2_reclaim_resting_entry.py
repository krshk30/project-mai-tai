"""RESTED RECLAIM ENTRY - rest at `cw_segment_high` instead of chasing the break with a MARKET.

WHY (execution only). The reactive path chases a price it already knew: the level is computed at bar
close, the bot waits for a print above it, then buys AFTER the print. Measured 21 days on
`live:schwab_1m_v2`, same universe and window:

    reactive MARKET      n=71  SD 57.0 bps  worst adverse +351.7 bps
    resting STOP_LIMIT   n=67  SD 25.6 bps  worst adverse  +60.2 bps

#674's price cap is a STOPGAP on this path - it bounds the damage, it does not stop the chasing.

THE ACCEPTANCE CRITERION IS NOT "THE GUARD PASSES". It is: an order placed by the reclaim slot is
always repriced by the reclaim slot and always cancelled by the reclaim slot - never orphaned
because a cancel fired against the other slot's latch. That is the #580 / EGG-POLA surface: a live
buy order at the broker that nothing repriced and nothing cancelled, twice, both hand-cancelled.

THE STRUCTURAL REASON THIS IS SAFE: there is only ever ONE resting order per symbol. `_cw_v2_quote`
has always stood the reactive path down while `resting_active`, so the two entry types are already
mutually exclusive. `resting_slot` selects a REPRICE LEVEL and never gates a cancel - every cancel
goes through the one `_queue_resting_cancel`. Hence ZERO new clear-without-cancel sites, which is
the property these tests pin.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    Quote,
    SchwabV2Strategy,
    SymbolState,
)

_ET = ZoneInfo("America/New_York")
RTH = int(datetime(2026, 8, 10, 11, 0, tzinfo=_ET).timestamp() * 1000)


def _strat(**over):
    kw = {
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
    }
    kw.update(over)
    s = SchwabV2Strategy(Settings(**kw))
    s._reactive_entry_enabled = True
    s._entries_held = False
    s._pending_intents = []
    s._eh_resting_enabled = False
    return s


def _armed(sym="TEST", *, seg_high=10.0, flip=9.0):
    st = SymbolState(symbol=sym)
    st.cw_armed = True
    st.cw_bars_waited = 2
    st.cw_segment_high = seg_high
    st.cw_flip_level = flip
    st.position_qty = 0
    st.bars.append(OHLCVBar(timestamp_ms=RTH, open=9.5, high=seg_high, low=9.1,
                            close=9.8, volume=500_000))
    return st


def _rth(strat, monkeypatch, in_window=True, eh=False, bar_age_ms=1_000):
    monkeypatch.setattr(strat, "_resting_in_window", lambda now=None: in_window)
    monkeypatch.setattr(strat, "_resting_session_is_eh", lambda now=None: eh)
    monkeypatch.setattr(strat, "_liquidity_floor_ok", lambda st: True)
    # ⛔⭐⭐ THE CLOCK MUST SIT NEXT TO THE BAR, OR EVERY TEST HERE IS A STALE-BAR TEST.
    # `_armed()` stamps its bar at a fixed 2026-08-10 instant while `_now_ms()` returned the
    # real wall clock, so the fixture's bar was ELEVEN DAYS old. That passed only because this
    # path had no bar-freshness gate — the fixture was quietly exercising the exact replay
    # scenario that produced the live USDE churn, and asserting it should ARM.
    # Freezing the clock a second after the bar makes these tests mean what their names say.
    # ⇒ `bar_age_ms` is the seam: raise it to test the stale-bar refusal deliberately.
    monkeypatch.setattr(strat, "_now_ms", lambda: RTH + bar_age_ms)


def _places(strat):
    return [d for d in strat._pending_intents if getattr(d, "intent_type", "") == "open"]


def _cancels(strat):
    return [d for d in strat._pending_intents if getattr(d, "intent_type", "") == "cancel"]


def _lines(caplog, marker):
    return [r.getMessage() for r in caplog.records if marker in r.getMessage()]


# -- arming -------------------------------------------------------------------------------

def test_reclaim_rests_at_the_segment_high(monkeypatch, caplog) -> None:
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed(seg_high=10.0)
    with caplog.at_level(logging.INFO):
        strat._cw_v2_reclaim_resting_track(st)
    assert st.resting_active is True
    assert st.resting_slot == "reclaim"
    assert st.resting_level == pytest.approx(10.0), "must rest AT the known level, not chase it"
    assert st.last_resting_placed_slot == "reclaim"
    line = _lines(caplog, "V2-RESTING-PLACE")
    assert line and "slot=reclaim" in line[0], (
        "the placement line must carry the slot so the two paths are separable on the tape"
    )


def test_it_stands_down_while_the_FIRST_slot_owns_the_order(monkeypatch) -> None:
    """ONE RESTING ORDER PER SYMBOL. `_resting_entry_already_open` refuses a second live buy per
    symbol; this is why it is never reached."""
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed()
    st.resting_active, st.resting_slot, st.resting_level = True, "first", 9.4
    strat._cw_v2_reclaim_resting_track(st)
    assert _places(strat) == [] and _cancels(strat) == []
    assert st.resting_slot == "first" and st.resting_level == 9.4


# -- SLOT SEPARATION: the #580 / EGG-POLA surface ------------------------------------------

def test_the_FIRST_entry_manager_never_touches_a_RECLAIM_order(monkeypatch) -> None:
    """THE CENTRAL TEST. The first-entry manager reprices against the ATR trail, which moves the
    OTHER WAY from `cw_segment_high`. If it touched a reclaim order it would reprice it off a
    different mechanism - and a cancel issued by the wrong owner is the cross-slot orphan. It must
    RETURN, not cancel: cancelling would also be the wrong owner acting."""
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed()
    st.resting_active, st.resting_slot, st.resting_level = True, "reclaim", 10.0
    st.resting_is_broker_order = True
    st.atr_state = "short"
    sig = {"trail": 8.0, "state": "short", "state_age": 5, "flip": None,
           "touch": False, "touch_price": None, "loss": 0.5}

    strat._cw_v2_resting_track(st, sig)

    assert _cancels(strat) == [], "the first-entry manager must not cancel the reclaim's order"
    assert _places(strat) == [], "nor re-place it against the trail"
    assert st.resting_active is True and st.resting_slot == "reclaim"
    assert st.resting_level == pytest.approx(10.0), "the reclaim's level must be untouched"


def test_the_RECLAIM_manager_never_touches_a_FIRST_order(monkeypatch) -> None:
    """The mirror. Neither manager may act on the other's order."""
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed()
    st.resting_active, st.resting_slot, st.resting_level = True, "first", 9.4
    st.resting_is_broker_order = True
    strat._cw_v2_reclaim_resting_track(st)
    assert _cancels(strat) == [] and _places(strat) == []
    assert st.resting_level == pytest.approx(9.4)


@pytest.mark.parametrize("setup", ["out_of_window", "disarmed", "slot_taken", "in_position", "eh"])
def test_every_reclaim_teardown_goes_through_the_cancel_path(monkeypatch, setup) -> None:
    """ZERO NEW CLEAR-WITHOUT-CANCEL SITES - the acceptance criterion.

    Each way the reclaim's order can stop being wanted must queue a BROKER CANCEL, never merely
    clear the latch. Clearing without cancelling is exactly EGG/POLA: the order stays live at the
    broker and no path can ever reprice or cancel it again."""
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed()
    st.resting_active, st.resting_slot, st.resting_level = True, "reclaim", 10.0
    st.resting_is_broker_order = True

    if setup == "out_of_window":
        monkeypatch.setattr(strat, "_resting_in_window", lambda now=None: False)
    elif setup == "disarmed":
        st.cw_armed = False
    elif setup == "slot_taken":
        st.cw_reclaim_taken = True
    elif setup == "in_position":
        # ⛔ BOTH, deliberately. "in_position" must mean a REAL FILL. Setting only the union
        # `position_qty` used to be enough because the gate read the union — which is precisely
        # the ambiguity being removed: our own in-flight resting intent also raises it, and that
        # is what made this gate cancel its own order 253 times.
        st.position_qty = st.position_qty_held = 2
    elif setup == "eh":
        monkeypatch.setattr(strat, "_resting_session_is_eh", lambda now=None: True)

    strat._cw_v2_reclaim_resting_track(st)

    assert len(_cancels(strat)) == 1, (
        f"{setup}: abandoned WITHOUT a broker cancel - this is the #580 orphan"
    )
    assert st.resting_active is False


def test_a_soft_rest_teardown_queues_no_broker_cancel(monkeypatch) -> None:
    """THE OPPOSITE DIRECTION. If nothing was ever sent to the broker, a cancel draft would be
    spurious. `resting_is_broker_order` decides - recorded at placement (#666)."""
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed()
    st.resting_active, st.resting_slot, st.resting_level = True, "reclaim", 10.0
    st.resting_is_broker_order = False
    st.cw_armed = False
    strat._cw_v2_reclaim_resting_track(st)
    assert _cancels(strat) == []
    assert st.resting_active is False


# -- claim on FILL, by placement slot ------------------------------------------------------

def test_a_reclaim_fill_claims_the_RECLAIM_slot_not_the_resting_one() -> None:
    """THE INFERENCE THIS CHANGE BREAKS. The old rule was "the reactive path claims
    cw_reclaim_taken at EMIT, so a fill with the reclaim slot free must be the resting one." A
    RESTED reclaim claims nothing until it fills, so that premise is gone - the slot that PLACED
    the order decides."""
    strat = _strat()
    st = strat.watchlist_state("TEST")
    st.last_resting_placed_slot = "reclaim"
    strat.update_position("TEST", 2, held_qty=2)
    assert st.cw_reclaim_taken is True
    assert st.cw_resting_taken is False, "a reclaim fill must not consume the resting slot"


def test_a_first_entry_fill_still_claims_the_RESTING_slot() -> None:
    strat = _strat()
    st = strat.watchlist_state("TEST")
    st.last_resting_placed_slot = "first"
    strat.update_position("TEST", 2, held_qty=2)
    assert st.cw_resting_taken is True and st.cw_reclaim_taken is False


def test_placement_is_not_a_claim(monkeypatch) -> None:
    """SETTLED: claim on FILL, never on placement. An order that never fills has cost nothing and
    must forfeit nothing - otherwise a place-then-cancel spends the reclaim on a trade that never
    existed, which is strictly worse than today."""
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed()
    strat._cw_v2_reclaim_resting_track(st)
    assert st.resting_active is True
    assert st.cw_reclaim_taken is False, "placement must not consume the slot"


# -- reused guards -------------------------------------------------------------------------

def test_an_already_crossed_level_is_not_rested(monkeypatch) -> None:
    """#527 reused verbatim: a buy stop must sit ABOVE the ask. `cw_segment_high` sits at the recent
    high BY DEFINITION, so this path meets the condition far more often than the trail does."""
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed(seg_high=10.0)

    class _Q:
        ask_price = 10.50

    st.last_quote = _Q()
    strat._cw_v2_reclaim_resting_track(st)
    assert _places(strat) == [] and st.resting_active is False


def test_fail_open_when_there_is_no_quote(monkeypatch) -> None:
    """Fail-open is INHERITED UNCHANGED. Making it fail-closed is a separate change with its own
    evidence - a rider on an entry-path change is how an unrelated behaviour ships unnoticed."""
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed(seg_high=10.0)
    st.last_quote = None
    strat._cw_v2_reclaim_resting_track(st)
    assert st.resting_active is True, "no quote must not block the placement (fail-open, as #527)"


def test_extended_hours_stands_down(monkeypatch) -> None:
    """REGRESSION GUARD. In EH the reactive entry is ALREADY price-committed (marketable band-capped
    EH-LIMIT). Resting here competed with that deployed path and turned a working EH fill into an
    ASK_PAST_BAND abandon."""
    strat = _strat()
    _rth(strat, monkeypatch, eh=True)
    st = _armed()
    strat._cw_v2_reclaim_resting_track(st)
    assert _places(strat) == [] and st.resting_active is False


def test_reprice_only_on_a_meaningful_move(monkeypatch) -> None:
    """STABLE-REST reused: a level that ratchets every bar must not cancel/replace every bar."""
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed(seg_high=10.0)
    strat._cw_v2_reclaim_resting_track(st)
    strat._pending_intents = []
    st.cw_segment_high = 10.001
    strat._cw_v2_reclaim_resting_track(st)
    assert _cancels(strat) == [], "a trivial ratchet must leave the order out there"
    st.cw_segment_high = 12.0
    strat._cw_v2_reclaim_resting_track(st)
    assert len(_cancels(strat)) == 1


# -- LIVE-BAR guard (the 2026-08-21 churn) ------------------------------------------------

def test_a_REPLAYED_bar_must_not_arm_a_reclaim_rest(monkeypatch, caplog) -> None:
    """★ THE CHURN. This path was the only one of four without the #528 LIVE-BAR guard, so a
    warmup / seed bar replay drove it: each replayed bar armed a fresh segment, placed a
    resting order, and cancelled it on the next replayed bar.

    Live 2026-08-21, USDE — ELEVEN place/cancel pairs inside NINETEEN MILLISECONDS
    (13:44:54.636 -> .655) off bars stamped 37 and 32 minutes apart, walking the level
    4.46 -> 5.51. No tape moves like that in 19ms; that was history being replayed. Every
    cycle sent a real order to Schwab and a real mirrored leg to Webull.
    """
    strat = _strat()
    # the bar is a full hour older than the clock -> a replayed bar, not a live one
    _rth(strat, monkeypatch, bar_age_ms=3_600_000)
    st = _armed(seg_high=10.0)
    with caplog.at_level(logging.INFO):
        strat._cw_v2_reclaim_resting_track(st)

    assert st.resting_active is False, "a replayed bar armed a resting order"
    assert _places(strat) == [], "a replayed bar emitted a real order"
    assert _cancels(strat) == [], "and then a cancel for it"
    assert not _lines(caplog, "V2-RESTING-PLACE")


def test_a_LIVE_bar_still_arms(monkeypatch) -> None:
    """★ THE CONTROL, and it is the load-bearing half. Without it the guard above passes just
    as well on a build where the reclaim entry never arms at all — the fix would have silently
    deleted the feature and every assertion would still be green."""
    strat = _strat()
    _rth(strat, monkeypatch, bar_age_ms=1_000)
    st = _armed(seg_high=10.0)
    strat._cw_v2_reclaim_resting_track(st)

    assert st.resting_active is True, "the live-bar path stopped arming — the guard is too tight"
    assert st.resting_slot == "reclaim"
    assert len(_places(strat)) == 1


def test_the_guard_sits_at_ARM_not_at_reprice(monkeypatch) -> None:
    """★ Arm-time only, exactly like the first-entry path. An order that armed legitimately must
    keep being managed even if the feed then stalls — otherwise this reopens the #580 orphan: a
    live buy order at the broker that nothing reprices and nothing cancels.

    ⛔ A replay cannot reach here, because it can no longer set `resting_active` in the first
    place; the chain is cut at its source rather than at every link.
    """
    strat = _strat()
    _rth(strat, monkeypatch, bar_age_ms=3_600_000)   # stale feed
    st = _armed(seg_high=10.0)
    st.resting_active = True                          # ...but an order is ALREADY working
    st.resting_slot = "reclaim"
    st.resting_level = 10.0
    st.cw_segment_high = 12.0                         # and the level has moved a long way

    strat._cw_v2_reclaim_resting_track(st)
    assert len(_cancels(strat)) == 1, (
        "a working order stopped being repriced on a stale feed — that is the #580 orphan"
    )


def test_all_four_resting_paths_carry_the_live_bar_guard() -> None:
    """★ §181a. The guard was missing from exactly one of four paths for a month, and nothing
    said so. This asserts the set, so a fifth path cannot be added without one."""
    import inspect

    from project_mai_tai.strategy_core import schwab_1m_v2 as mod

    for fn in (
        mod.SchwabV2Strategy._cw_v2_resting_track,
        mod.SchwabV2Strategy._cw_v2_reclaim_resting_track,
        mod.SchwabV2Strategy._eh_resting_cross_check,
        mod.SchwabV2Strategy._fanout_rth_resting_cross,
    ):
        src = inspect.getsource(fn)
        assert "_resting_max_bar_age_ms" in src, (
            f"{fn.__name__} places or fires a resting order off an ungated bar"
        )


# -- slot_consumed: HELD, not the union (the 253-cancel oscillation) -----------------------

def test_our_own_in_flight_resting_intent_must_not_consume_the_slot(monkeypatch) -> None:
    """★ THE OSCILLATION. `position_qty` is the UNION and counts in-flight open intents; a resting
    buy-stop's intent stays `submitted` for its entire life because it only resolves when price
    triggers it. So placing the leg set the gate, the gate cancelled the leg, the intent went
    away, the gate cleared, and the next bar placed it again — at a CONSTANT price.

    Measured 08-14..08-21: `slot_consumed` is 253 of 773 mirror cancels. IPST 2026-08-17 re-sent
    31 legs over 3648 seconds inside a 0.6% level range; four other segments did it at 0.0%.
    """
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed(seg_high=10.0)
    st.resting_active, st.resting_slot, st.resting_level = True, "reclaim", 10.0
    st.resting_is_broker_order = True
    st.position_qty = 2          # our OWN resting intent, in flight
    st.position_qty_held = 0     # ...and nothing actually filled

    strat._cw_v2_reclaim_resting_track(st)

    assert _cancels(strat) == [], (
        "the reclaim slot cancelled its own order because its own in-flight intent looked "
        "like a position — that is the 253-cancel oscillation"
    )
    assert st.resting_active is True, "the order must stay live and keep being managed"


def test_a_REAL_fill_still_consumes_the_slot(monkeypatch) -> None:
    """★ THE CONTROL, and it is load-bearing. Without it the test above passes just as well on a
    build where the slot is NEVER consumed — which would leave a resting buy working while we are
    already in the position, i.e. the opposite defect."""
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed(seg_high=10.0)
    st.resting_active, st.resting_slot, st.resting_level = True, "reclaim", 10.0
    st.resting_is_broker_order = True
    st.position_qty = 2
    st.position_qty_held = 2     # a REAL fill

    strat._cw_v2_reclaim_resting_track(st)

    cancels = _cancels(strat)
    assert len(cancels) == 1, "a real fill must still consume the slot and cancel the order"
    assert cancels[0].metadata.get("reason") == "slot_consumed"


def test_cw_reclaim_taken_still_consumes_the_slot(monkeypatch) -> None:
    """★ The other half of the gate is untouched — one reclaim per cross, still enforced."""
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed(seg_high=10.0)
    st.resting_active, st.resting_slot, st.resting_level = True, "reclaim", 10.0
    st.resting_is_broker_order = True
    st.position_qty = st.position_qty_held = 0
    st.cw_reclaim_taken = True

    strat._cw_v2_reclaim_resting_track(st)
    assert len(_cancels(strat)) == 1


# -- THE DOUBLE-POSITION CASE, pinned ------------------------------------------------------

def test_the_reactive_MARKET_stands_down_while_a_reclaim_order_rests(monkeypatch) -> None:
    """★★ THE RISK THIS CHANGE WAS QUESTIONED ON, pinned directly.

    The concern about moving off the union is that it "would let a market buy fire while a
    stop-limit rests, i.e. a double position". The guard against that is NOT this gate — it is
    `resting_active` in `_cw_v2_quote`: *"Reactive entry off, OR a resting buy-stop-limit is
    already live for this symbol."* This asserts it holds with our own intent in flight, which is
    exactly the state the change creates.
    """
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed(seg_high=10.0)
    st.resting_active, st.resting_slot, st.resting_level = True, "reclaim", 10.0
    st.position_qty = 2          # our own resting intent in flight
    st.position_qty_held = 0

    quote = Quote(symbol="TEST", bid_price=10.4, ask_price=10.5, last_price=10.45,
                  quote_time_ms=RTH + 1_000)
    assert strat._cw_v2_quote(st, quote) is None, (
        "a reactive MARKET buy was emitted while a resting stop-limit was live — double position"
    )


def test_the_spurious_cancel_was_itself_the_hole_in_that_guard(monkeypatch) -> None:
    """★★ THE INVERSION, and the reason this change is SAFER rather than riskier.

    Every spurious `slot_consumed` cancel set `resting_active = False`. With that flag down the
    reactive MARKET path is no longer stood down — so the union was not the conservative choice,
    it was punching a hole in the real double-position guard 253 times over the window.

    This pins the causal chain: order cancelled -> resting_active False -> reactive path OPEN.
    """
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed(seg_high=10.0)
    st.resting_active, st.resting_slot, st.resting_level = True, "reclaim", 10.0
    st.resting_is_broker_order = True

    quote = Quote(symbol="TEST", bid_price=10.4, ask_price=10.5, last_price=10.45,
                  quote_time_ms=RTH + 1_000)
    assert strat._cw_v2_quote(st, quote) is None, "precondition: the stand-down is active"

    # now do what the old gate did on every bar: cancel the order for `slot_consumed`
    strat._queue_resting_cancel(st, reason="slot_consumed")
    assert st.resting_active is False

    # ...and the reactive stand-down is gone. THIS is the window the oscillation opened.
    assert strat._cw_v2_quote(st, quote) is not None or not strat._reactive_entry_enabled, (
        "with resting_active cleared the reactive path must be reachable again — if it is not, "
        "this test no longer demonstrates the hole and needs rewriting, not deleting"
    )


def test_the_gate_reads_held_not_the_union() -> None:
    """★ §181a. Behaviour above can be satisfied by several shapes; this pins the one the
    reasoning rests on, so a future 'tidy' back to the union is loud."""
    import inspect

    from project_mai_tai.strategy_core import schwab_1m_v2 as mod

    src = inspect.getsource(mod.SchwabV2Strategy._cw_v2_reclaim_resting_track)
    assert "state.position_qty_held != 0 or state.cw_reclaim_taken" in src, (
        "the reclaim slot gate is back on the union, which counts our own in-flight intent"
    )


# -- #761 live acceptance: opportunity and success are separate ---------------------------

RECLAIM_SLOT_CHECKED = "[V2-RECLAIM-SLOT-CHECKED]"
UNION_ONLY_PASSED = "[V2-RECLAIM-UNION-ONLY-PASSED]"


def test_union_only_reclaim_emits_opportunity_and_success(monkeypatch, caplog) -> None:
    """The known-positive: the exact #761 state emits one denominator and one success.

    Their placement on opposite sides of the slot-consumption gate is load-bearing.  Restoring
    the old `position_qty != 0` predicate leaves CHECKED visible but returns before PASSED, so B28
    reports a failed acceptance rather than an unmeasured feature.
    """
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed(seg_high=10.0)
    st.resting_active, st.resting_slot, st.resting_level = True, "reclaim", 10.0
    st.resting_is_broker_order = True
    st.position_qty = 2
    st.position_qty_held = 0
    st.cw_reclaim_taken = False

    with caplog.at_level(logging.INFO):
        strat._cw_v2_reclaim_resting_track(st)

    assert len(_lines(caplog, RECLAIM_SLOT_CHECKED)) == 1, "the #761 denominator is missing"
    assert len(_lines(caplog, UNION_ONLY_PASSED)) == 1, "the #761 success marker is missing"
    assert _cancels(strat) == [], "the union-only state was still treated as a consumed slot"
    assert st.resting_active is True, "the reclaim order must remain managed"


def test_union_only_markers_are_silent_when_feature_is_off(monkeypatch, caplog) -> None:
    """Flag-off is not an opportunity.  Both observables must stay inside the feature check."""
    strat = _strat()
    strat._reactive_entry_enabled = False
    _rth(strat, monkeypatch)
    st = _armed(seg_high=10.0)
    st.resting_active, st.resting_slot, st.resting_level = True, "reclaim", 10.0
    st.position_qty = 2
    st.position_qty_held = 0
    st.cw_reclaim_taken = False

    with caplog.at_level(logging.INFO):
        strat._cw_v2_reclaim_resting_track(st)

    assert _lines(caplog, RECLAIM_SLOT_CHECKED) == []
    assert _lines(caplog, UNION_ONLY_PASSED) == []


def test_union_only_markers_are_silent_when_site_did_not_run(monkeypatch, caplog) -> None:
    """Feature-on alone is not a denominator: no active reclaim rest means no #761 opportunity."""
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed(seg_high=10.0)
    st.position_qty = 0
    st.position_qty_held = 0

    with caplog.at_level(logging.INFO):
        strat._cw_v2_reclaim_resting_track(st)

    assert _lines(caplog, RECLAIM_SLOT_CHECKED) == []
    assert _lines(caplog, UNION_ONLY_PASSED) == []
    assert len(_places(strat)) == 1, "control must reach the tracker and arm a reclaim rest"


def test_a_real_fill_is_not_in_the_union_only_denominator(monkeypatch, caplog) -> None:
    """The denominator is the defect population, not every reclaim-slot evaluation."""
    strat = _strat()
    _rth(strat, monkeypatch)
    st = _armed(seg_high=10.0)
    st.resting_active, st.resting_slot, st.resting_level = True, "reclaim", 10.0
    st.resting_is_broker_order = True
    st.position_qty = 2
    st.position_qty_held = 2

    with caplog.at_level(logging.INFO):
        strat._cw_v2_reclaim_resting_track(st)

    assert len(_lines(caplog, RECLAIM_SLOT_CHECKED)) == 1, (
        "a real fill is still an evaluation of the reclaim-slot gate and must count in its "
        "denominator"
    )
    assert _lines(caplog, UNION_ONLY_PASSED) == []
    assert len(_cancels(strat)) == 1, "a genuine fill must still consume the reclaim slot"
