"""A resting entry placed in RTH must be CANCELLED at the broker when the window closes.

⭐⭐ THE INCIDENT — RCEL, 2026-08-07, real money, caught by the operator on his chart.

    15:33 .. 15:55   the bot repriced the resting entry NINE times, cancelling each correctly
    15:55:13         [V2-RESTING-PLACE] RCEL stop=7.8265 limit=7.8657   <- a REAL broker STOP_LIMIT
    16:00:03         [V2-RESTING-EH-DISARM] RCEL reason=window_closed   <- and NO cancel intent
    16:02:52         broker: id=1007522463623 status=WORKING, qty 2, with two
                     AWAITING_PARENT_ORDER children (SELL STOP 7.44, SELL LIMIT 7.98)

The entry window closed the ENTRY and left the ORDER. It was fillable in extended hours — where
Schwab REFUSES the protective STOP leg — so a fill would have opened a real-money position with no
working stop, after the close, unattended.

⛔ THE DEFECT: `_queue_resting_cancel` asked `_resting_session_is_eh()` — the session NOW — on the
premise "in EH nothing is live at the broker". That is true of an order PLACED in EH (a soft-rest
watched in memory) and FALSE of one placed in RTH and disarmed after 16:00. The guard tested the
wrong moment: the only moment that knows whether a broker order exists is the moment of PLACEMENT.

⇒ THE FIX: record `resting_is_broker_order` at placement, branch on it at cancel.
"""
from __future__ import annotations

import logging

import pytest

from project_mai_tai.strategy_core.schwab_1m_v2 import (
    SchwabV2Config,
    SchwabV2Strategy,
    SymbolState,
)

@pytest.fixture
def strat():
    s = SchwabV2Strategy(SchwabV2Config())
    s._eh_resting_enabled = True          # EH resting ON — the config the incident ran under
    s._pending_intents = []
    return s


def _cancels(strat) -> list:
    return [d for d in strat._pending_intents if getattr(d, "intent_type", "") == "cancel"]


def _lines(caplog, marker: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if marker in r.getMessage()]


# ------------------------------------------------------------------ the incident

def test_RCEL_an_RTH_PLACED_order_disarmed_in_EH_IS_CANCELLED(strat, caplog, monkeypatch) -> None:
    """⛔⭐ THE REGRESSION. Placed in RTH, window closes, session is now EH. A broker order exists
    and MUST be cancelled — this is the exact sequence that left RCEL WORKING through the close."""
    st = SymbolState(symbol="RCEL")
    st.resting_active = True
    st.resting_level = 7.8265
    st.resting_is_broker_order = True                       # placed during RTH

    monkeypatch.setattr(strat, "_resting_session_is_eh", lambda now=None: True)  # it is now EH
    with caplog.at_level(logging.INFO):
        strat._queue_resting_cancel(st, reason="window_closed")

    assert len(_cancels(strat)) == 1, (
        "no cancel was queued — the broker order would be left WORKING past the close"
    )
    assert _lines(caplog, "V2-RESTING-CANCEL"), "expected a real cancel line"
    assert not _lines(caplog, "V2-RESTING-EH-DISARM"), (
        "took the soft-rest branch for an order that IS at the broker"
    )
    assert st.resting_active is False and st.resting_is_broker_order is False


def test_a_TRUE_soft_rest_still_emits_NO_cancel(strat, caplog, monkeypatch) -> None:
    """⛔ THE OTHER HALF. An EH-placed soft-rest has nothing at the broker; a cancel draft for a
    non-existent order would be spurious. The fix must not turn one bug into its mirror."""
    st = SymbolState(symbol="PAVS")
    st.resting_active = True
    st.resting_level = 7.0528
    st.resting_is_broker_order = False                      # armed in memory only

    monkeypatch.setattr(strat, "_resting_session_is_eh", lambda now=None: True)
    with caplog.at_level(logging.INFO):
        strat._queue_resting_cancel(st, reason="window_closed")

    assert _cancels(strat) == [], "queued a broker cancel for an order that never existed"
    assert _lines(caplog, "V2-RESTING-EH-DISARM")


def test_an_RTH_order_cancelled_DURING_rth_is_unchanged(strat, caplog, monkeypatch) -> None:
    """Behaviour-identical for the common case — the 9 reprices RCEL did correctly before 16:00."""
    st = SymbolState(symbol="RCEL")
    st.resting_active = True
    st.resting_level = 7.8790
    st.resting_is_broker_order = True

    monkeypatch.setattr(strat, "_resting_session_is_eh", lambda now=None: False)
    with caplog.at_level(logging.INFO):
        strat._queue_resting_cancel(st, reason="liquidity_floor")

    assert len(_cancels(strat)) == 1
    assert _lines(caplog, "V2-RESTING-CANCEL")


def test_the_decision_no_longer_depends_on_the_CURRENT_session(strat, monkeypatch) -> None:
    """⭐ THE INVARIANT, stated directly: for a given placement kind the outcome is the SAME in
    both sessions. If this ever fails, the guard has drifted back to asking the clock."""
    for eh_now in (True, False):
        monkeypatch.setattr(strat, "_resting_session_is_eh", lambda now=None, v=eh_now: v)

        strat._pending_intents = []
        broker = SymbolState(symbol="RCEL")
        broker.resting_active = True
        broker.resting_is_broker_order = True
        strat._queue_resting_cancel(broker, reason="window_closed")
        assert len(_cancels(strat)) == 1, f"broker order not cancelled with eh_now={eh_now}"

        strat._pending_intents = []
        soft = SymbolState(symbol="PAVS")
        soft.resting_active = True
        soft.resting_is_broker_order = False
        strat._queue_resting_cancel(soft, reason="window_closed")
        assert _cancels(strat) == [], f"soft-rest wrongly cancelled with eh_now={eh_now}"


def test_state_that_never_went_through_PLACE_still_cancels(strat, monkeypatch, caplog) -> None:
    """⛔⭐⭐ THE FAIL-SAFE DEFAULT, and the regression that caught it.

    `_queue_resting_place` is not the only way `resting_active` becomes True: a v2 RESTART rebuilds
    SymbolState from the DB seed, and the pre-existing suite constructs state directly. Such a state
    cannot know whether a broker order is live. Defaulting `resting_is_broker_order` to False would
    SKIP the cancel and leave a real order working — the #580 orphan, i.e. THIS VERY BUG
    re-triggered by restart instead of by session.

    ⇒ The default is True: a spurious cancel is harmless and logged; a missed cancel leaves live
    money on the book unattended."""
    st = SymbolState(symbol="APLX")
    assert st.resting_is_broker_order is True, "the fail-safe default was inverted"
    st.resting_active = True
    st.resting_level = 9.03                                  # never went through _queue_resting_place

    monkeypatch.setattr(strat, "_resting_session_is_eh", lambda now=None: True)
    with caplog.at_level(logging.INFO):
        strat._queue_resting_cancel(st, reason="liquidity_floor")

    assert len(_cancels(strat)) == 1, (
        "an unknown-provenance rest was skipped — a live broker order would be left working"
    )


# ------------------------------------------------------------------ the flag is set correctly

def test_placement_marks_an_RTH_rest_as_a_BROKER_order(strat, monkeypatch, caplog) -> None:
    st = SymbolState(symbol="RCEL")
    monkeypatch.setattr(strat, "_resting_session_is_eh", lambda now=None: False)
    with caplog.at_level(logging.INFO):
        strat._queue_resting_place(st, 7.8265)
    assert st.resting_is_broker_order is True
    assert _lines(caplog, "V2-RESTING-PLACE")


def test_placement_marks_an_EH_rest_as_a_SOFT_rest(strat, monkeypatch, caplog) -> None:
    st = SymbolState(symbol="PAVS")
    monkeypatch.setattr(strat, "_resting_session_is_eh", lambda now=None: True)
    with caplog.at_level(logging.INFO):
        strat._queue_resting_place(st, 7.0528)
    assert st.resting_is_broker_order is False
    assert _lines(caplog, "V2-RESTING-EH-ARM")
