"""P2.11 — every `cw_armed` clear must EMIT A DISARM LINE.

⭐⭐ WHY. `_apply_session_anchor_reset` cleared `cw_armed` with no log line, so a replay-armed
segment left a DANGLING ARM — an `[V2-CW-ARM]` with no `[V2-CW-DISARM]` ever. Consequences:

  * every log-derived segment count is contaminated
  * the v2-restart pre-flight read it wrong in BOTH directions — over-reporting PAVS/ZCMD
    (dangling arms that were actually clear) and under-reporting FUSE/HYFM/AXTL (armed on a prior
    day, so no ARM line exists in today's files at all)
  * the divergence grew 7 -> 8 -> 9 symbols across three days

⛔ NO TRADE HAS EVER BEEN LOST TO IT. It is a TRUST defect, not a trading one — and it accumulates,
which is the combination that makes deferring it wrong. Deferred three times for being small.

⛔⭐ THE TRAP THIS FIX MUST NOT FALL INTO: log the TRANSITION, not the ASSIGNMENT. `cw_armed = False`
when already False is a no-op. Logging unconditionally would emit a DISARM for every never-armed
symbol on every 04:00 reset and INVENT events — corrupting the same counts in the opposite
direction. These tests pin both halves.
"""
from __future__ import annotations

import logging

import pytest

from project_mai_tai.strategy_core.schwab_1m_v2 import SymbolState


def _disarms(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "V2-CW-DISARM" in r.getMessage()]


@pytest.fixture
def strat():
    """The real strategy object, built the way the bot builds it."""
    from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy, SchwabV2Config
    return SchwabV2Strategy(SchwabV2Config())


# ------------------------------------------------------------- the session anchor reset

def test_anchor_reset_EMITS_a_disarm_when_the_symbol_WAS_armed(strat, caplog) -> None:
    """The dangling-ARM source. This is the line whose absence corrupted every segment count."""
    st = SymbolState(symbol="PAVS")
    st.cw_armed = True
    with caplog.at_level(logging.INFO):
        strat._apply_session_anchor_reset(st, 1786003200000)
    msgs = _disarms(caplog)
    assert len(msgs) == 1, f"expected exactly one disarm, got {msgs}"
    assert "PAVS" in msgs[0] and "session_anchor_reset" in msgs[0]
    assert st.cw_armed is False


def test_anchor_reset_is_SILENT_when_the_symbol_was_NOT_armed(strat, caplog) -> None:
    """⛔⭐ THE OTHER HALF, AND THE EASY ONE TO GET WRONG. The 04:00 reset runs over EVERY watchlist
    symbol. Logging unconditionally would invent a DISARM for every never-armed name, every day —
    corrupting the counts this fix exists to repair, in the opposite direction."""
    st = SymbolState(symbol="WYHG")
    st.cw_armed = False
    with caplog.at_level(logging.INFO):
        strat._apply_session_anchor_reset(st, 1786003200000)
    assert _disarms(caplog) == []


def test_the_reset_still_clears_everything_it_did_before(strat) -> None:
    """⛔ Behaviour-identical apart from the log line. The reset has two drivers (bar-driven and
    time-driven) that MUST stay identical; a drift here is silent."""
    st = SymbolState(symbol="CLRO")
    st.cw_armed = True
    st.cw_bars_waited = 3
    st.cw_three_bar_high = 9.9
    st.cw_trigger = 10.1
    st.cw_flip_level = 9.5
    st.cw_entries_this_flip = 2
    st.cw_segment_high = 11.0
    strat._apply_session_anchor_reset(st, 1786003200000)
    assert st.cw_armed is False
    assert st.cw_bars_waited == 0
    assert st.cw_three_bar_high == 0.0
    assert st.cw_trigger == 0.0
    assert st.cw_flip_level == 0.0
    assert st.cw_entries_this_flip == 0
    assert st.cw_segment_high == 0.0
    assert st.atr_session_anchor_ms == 1786003200000


# ------------------------------------------------------------- pairing is the point

def test_every_ARM_can_be_PAIRED_with_a_DISARM_across_a_reset(strat, caplog) -> None:
    """⭐ THE ACTUAL GOAL. Not 'a line exists' but 'the log is pairable' — which is what every
    segment-derived count, and the pre-flight gate, depend on."""
    with caplog.at_level(logging.INFO):
        for sym in ("GTE", "INLF", "ZYBT"):
            st = SymbolState(symbol=sym)
            st.cw_armed = True
            strat._apply_session_anchor_reset(st, 1786003200000)
    msgs = _disarms(caplog)
    assert len(msgs) == 3
    for sym in ("GTE", "INLF", "ZYBT"):
        assert any(sym in m for m in msgs), f"{sym} armed but never disarmed — a DANGLING ARM"


def test_disarm_reasons_are_DISTINCT_per_cause(strat, caplog) -> None:
    """A single generic reason would make the line present but useless: 'why did this segment end'
    is the question every count actually asks."""
    st = SymbolState(symbol="AZI")
    st.cw_armed = True
    with caplog.at_level(logging.INFO):
        strat._apply_session_anchor_reset(st, 1786003200000)
    assert "reason=session_anchor_reset" in _disarms(caplog)[0]
