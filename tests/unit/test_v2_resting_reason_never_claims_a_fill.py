"""A reason string must not assert a FILL outcome for an order that never reached the book.

⛔⭐ THE DEFECT. When an up-flip fires while a resting arm is live, the strategy waits a grace and,
if still flat, cancels with `reason="flip_no_fill"` — "an order was resting and did not fill".

On the EH SOFT-REST path that sentence is FALSE. `_queue_resting_place` sets
`resting_is_broker_order = False` in extended hours because a broker buy-stop-limit cannot trigger
there: the level is armed IN MEMORY and quotes are watched. Nothing is ever sent. So the tape said a
resting order failed to fill when no resting order had ever existed — and the counts read off that
tape inherited the error.

⛔ A WRONG REASON IS WORSE THAN A MISSING ONE: a plausible false reason stops the investigation.
[[feedback_a_wrong_reason_is_worse_than_a_missing_one]]

⚠️ WHAT THIS STILL CANNOT SAY, declared rather than implied. It separates "no broker order existed"
from "one did". It does NOT separate an EH cross whose marketable limit the OMS ABANDONED pre-submit
(`ASK_PAST_BAND` / `NO_FRESH_QUOTE` / `MISSING_SIGNAL`) from one that reached the book and genuinely
went unfilled — the strategy cannot see the OMS abandon. That needs a signal it does not have, and
inventing one here would be a behaviour change, not a log change.
[[feedback_a_watch_that_fails_to_a_false_clean]]

⛔⭐⭐ FIXTURE vs PRODUCTION: `strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled` defaults to
**False** in code and is **true** in production. `_eh_resting_enabled` is set explicitly on every
fixture below; without it these tests would pass by never entering the soft-rest path.
[[feedback_fixture_must_match_production_config]]
"""
from __future__ import annotations

import logging

import pytest

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    SchwabV2Config,
    SchwabV2Strategy,
    SymbolState,
)


@pytest.fixture
def strat():
    s = SchwabV2Strategy(SchwabV2Config())
    s._eh_resting_enabled = True          # PRODUCTION value; code default is False
    s._pending_intents = []
    return s


def _lines(caplog, marker: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if marker in r.getMessage()]


def test_the_fixture_matches_production_not_the_code_default() -> None:
    assert Settings().strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled is False, (
        "code default changed — re-check whether these fixtures still need the explicit flag"
    )


def test_soft_rest_does_not_claim_the_flip_failed_to_fill(strat, caplog) -> None:
    """⛔⭐ THE FIX. Nothing was ever at the broker, so the reason must not assert a fill verdict."""
    st = SymbolState(symbol="PAVS")
    st.resting_active = True
    st.resting_level = 7.0528
    st.resting_is_broker_order = False          # soft-rest: armed in memory only

    with caplog.at_level(logging.INFO):
        strat._queue_resting_cancel(st, reason="flip_no_fill")

    line = _lines(caplog, "V2-RESTING-EH-DISARM")
    assert line, "expected the soft-rest disarm line"
    assert "reason=flip_no_fill_soft_rest" in line[0]
    assert "reason=flip_no_fill " not in line[0], (
        "the unqualified reason claims a resting order failed to fill; none ever existed"
    )


def test_a_REAL_broker_order_keeps_the_unqualified_reason(strat, caplog) -> None:
    """⛔ MUTATION GUARD, the opposite direction. For an order that DID reach the book,
    `flip_no_fill` is TRUE and must survive untouched — renaming it there would destroy the
    distinction this change exists to create."""
    st = SymbolState(symbol="RCEL")
    st.resting_active = True
    st.resting_level = 7.8265
    st.resting_is_broker_order = True            # a real broker STOP_LIMIT

    with caplog.at_level(logging.INFO):
        strat._queue_resting_cancel(st, reason="flip_no_fill")

    line = _lines(caplog, "V2-RESTING-CANCEL")
    assert line, "expected the real-cancel line"
    assert "reason=flip_no_fill" in line[0]
    assert "soft_rest" not in line[0], "a real broker order must keep the unqualified reason"


def test_the_two_paths_are_distinguishable_on_the_tape(strat, caplog) -> None:
    """⭐ The point of the change, asserted as a PAIR. Before it, both paths emitted the same
    `reason=flip_no_fill` and no reader could tell an unfilled broker order from an arm that never
    became one. If these two ever match again, the distinction is gone."""
    soft = SymbolState(symbol="PAVS")
    soft.resting_active, soft.resting_level, soft.resting_is_broker_order = True, 7.05, False
    real = SymbolState(symbol="RCEL")
    real.resting_active, real.resting_level, real.resting_is_broker_order = True, 7.82, True

    with caplog.at_level(logging.INFO):
        strat._queue_resting_cancel(soft, reason="flip_no_fill")
        strat._queue_resting_cancel(real, reason="flip_no_fill")

    reasons = {
        ln.split("reason=")[1].split(" ")[0]
        for ln in _lines(caplog, "V2-RESTING")
    }
    assert reasons == {"flip_no_fill_soft_rest", "flip_no_fill"}, (
        f"the two paths must be distinguishable; got {reasons}"
    )


def test_other_reasons_are_untouched_on_the_soft_rest_path(strat, caplog) -> None:
    """⛔ SCOPED. Only the reason that makes a FILL claim is qualified. `window_closed` says
    nothing about filling and must pass through byte-identical — #666's tape depends on it."""
    st = SymbolState(symbol="PAVS")
    st.resting_active, st.resting_level, st.resting_is_broker_order = True, 7.05, False

    with caplog.at_level(logging.INFO):
        strat._queue_resting_cancel(st, reason="window_closed")

    line = _lines(caplog, "V2-RESTING-EH-DISARM")
    assert "reason=window_closed" in line[0]
    assert "soft_rest" not in line[0]


def test_no_broker_cancel_is_queued_for_a_soft_rest(strat, caplog) -> None:
    """The behaviour half must be unchanged: this is a LOG-ONLY change."""
    st = SymbolState(symbol="PAVS")
    st.resting_active, st.resting_level, st.resting_is_broker_order = True, 7.05, False

    with caplog.at_level(logging.INFO):
        strat._queue_resting_cancel(st, reason="flip_no_fill")

    assert [d for d in strat._pending_intents if getattr(d, "intent_type", "") == "cancel"] == []
    assert st.resting_active is False and st.resting_is_broker_order is False
