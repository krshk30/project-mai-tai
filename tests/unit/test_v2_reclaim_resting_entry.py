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


def _rth(strat, monkeypatch, in_window=True, eh=False):
    monkeypatch.setattr(strat, "_resting_in_window", lambda now=None: in_window)
    monkeypatch.setattr(strat, "_resting_session_is_eh", lambda now=None: eh)
    monkeypatch.setattr(strat, "_liquidity_floor_ok", lambda st: True)


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
        st.position_qty = 2
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
