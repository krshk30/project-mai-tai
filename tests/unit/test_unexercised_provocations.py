"""One harness, three rows: SLOT2, DB2, RECOV1.

⛔ WHAT THIS IS NOT. It is NOT evidence that any of these has been exercised in production, and it
closes NO live acceptance status. A unit test cannot move a row from UNEXERCISED — that requires the
condition to occur on the live system. The first version of this docstring said all three "had never
once been exercised", which conflated a test with a reading and is the exact rule this board keeps:
the instrument is not the reading.

⭐ WHAT IS ACTUALLY NEW, measured rather than asserted (2026-09-08):
  RECOV1  MATERIALLY NEW. Nothing previously routed through the deployed
          `_v2_close_reconcile_flat` with a real OmsManagedPosition row; the prior coverage was a
          transcription of the predicate, which proves only that a rule was copied correctly.
  DB2     DUPLICATE. #858's tests/unit/test_v2_fanout_zero_hold_mirror_scope.py already covers it:
          removing the venue-evidence veto turns test_expired_hold_vetoes_release_of_a_filled_claim
          RED without this file. Kept as a parallel control, not claimed as new.
  SLOT2   PARTIAL. #880 covers slot consumption on fill. Removing the guard at
          strategy_core/schwab_1m_v2.py:3161 left the whole unit suite's v2 tests green, so that
          specific branch appears uncovered — but the mechanism is not new and the claim here is
          only about that branch.

Each row is driven against the DEPLOYED function. Stubs are held to the transport boundary — a
broker read, a DB write, a queued order — so the decision under test is always the real one, and
every test carries a control that could have failed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from project_mai_tai.db.models import OmsManagedPosition
from project_mai_tai.oms.service import OmsRiskService, _PositionRead
from project_mai_tai.strategy_core import schwab_1m_v2 as v2
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy, SymbolState

ACCT = "live:schwab_1m_v2"
SYMBOL = "CHPT"
KEY = (ACCT, SYMBOL)
NOW_MS = 1_788_000_000_000


# --------------------------------------------------------------------------- SLOT2

def _resting_strategy(placed: list) -> SchwabV2Strategy:
    s = object.__new__(SchwabV2Strategy)
    s._resting_entry_enabled = True
    s._cw_v2_enabled = True
    s._entries_held = False
    s._resting_min_short_bars = 2
    s._resting_max_bar_age_ms = 300_000
    s._eh_resting_enabled = False
    s._now_ms = lambda: NOW_MS
    s._resting_in_window = lambda: True
    s._liquidity_floor_ok = lambda _state: True
    s._resting_session_is_eh = lambda: False
    s._queue_resting_place = lambda st, trail: placed.append((st.symbol, trail))
    s._queue_resting_cancel = lambda st, reason=None: None
    s._persist_fanout_claim_transition = lambda *a, **k: None
    s._restored_fanout_segment_ids = {}
    return s


def _armed_state(*, resting_taken: bool) -> SymbolState:
    st = SymbolState(symbol=SYMBOL)
    st.resting_active = False
    st.position_qty_held = 0
    st.resting_flip_ms = 0
    st.atr_state = "short"
    st.atr_trail = 5.0
    st.atr_state_age = 9
    st.last_quote = None
    st.bars = []
    st.fanout_segment_id = 7717
    st.cw_armed = True
    st.cw_resting_taken = resting_taken
    return st


_SIGNAL = {"state": "short", "trail": 5.0, "state_age": 9}


def test_slot2_refuses_a_second_resting_entry_inside_one_segment():
    placed: list = []
    strategy = _resting_strategy(placed)
    state = _armed_state(resting_taken=True)

    strategy._cw_v2_resting_track(state, _SIGNAL)

    assert placed == [], "a second resting entry was placed inside a segment whose slot was consumed"
    assert state.cw_resting_suppressed_segment_id == 7717


def test_slot2_control_a_free_slot_is_placed():
    """CONTROL. Without this the test above proves only that the method can decline."""
    placed: list = []
    strategy = _resting_strategy(placed)

    strategy._cw_v2_resting_track(_armed_state(resting_taken=False), _SIGNAL)

    assert placed == [(SYMBOL, 5.0)]


def test_slot2_an_intervening_flip_re_allows_the_same_entry():
    """⛔ THE CONTROL THAT MATTERS. The slot must reset on a real segment end, not stay latched.

    The reset is driven through the DEPLOYED `_release_arm` -- the same writes the SELL-flip
    "segment over" branch makes -- rather than by assigning the flag, which would only re-assert
    that the branch reads it.
    """
    placed: list = []
    strategy = _resting_strategy(placed)
    state = _armed_state(resting_taken=True)

    strategy._cw_v2_resting_track(state, _SIGNAL)
    assert placed == [], "precondition: the second entry must be suppressed first"

    assert strategy._release_arm(state, reason="atr_flip_to_short") is True
    assert state.cw_resting_taken is False

    state.cw_armed = True
    state.fanout_segment_id = 8811
    strategy._cw_v2_resting_track(state, _SIGNAL)

    assert placed == [(SYMBOL, 5.0)], "the flip did not re-allow the entry; the slot blocks rather than discriminates"


# --------------------------------------------------------------------------- DB2

def _claim_strategy(released: list) -> SchwabV2Strategy:
    s = object.__new__(SchwabV2Strategy)
    s._cw_v2_enabled = True
    s._now_ms = lambda: NOW_MS
    s._symbol_states = {}
    s._persist_fanout_claim_transition = lambda *a, **k: None
    s._restored_fanout_segment_ids = {}
    s._atr_rearm_enabled = False
    s._entries_held = False
    s._cw_v2_reclaim_gap_bars = 0
    s._resting_entry_enabled = False
    real = SchwabV2Strategy._release_fanout_webull_claim
    s._release_fanout_webull_claim = lambda state, *, reason, persist=True: (
        released.append(reason),
        real(s, state, reason=reason, persist=False),
    )[1]
    return s


def _claim_state(*, outcome: str, resting_active: bool) -> SymbolState:
    st = SymbolState(symbol=SYMBOL)
    st.position_qty = 0
    st.position_qty_held = 0
    st.fanout_webull_claimed = True
    st.fanout_claim_outcome = outcome
    st.webull_resting_active = resting_active
    st.fanout_zero_hold_started_ms = NOW_MS - v2.FANOUT_POSITIVE_ZERO_HOLD_MS - 1_000
    st.fanout_claim_slot_id = None
    return st


@pytest.mark.parametrize(
    ("outcome", "resting_active"),
    [("filled", False), ("held", True)],
)
def test_db2_a_webull_fill_does_not_erase_its_own_claim(outcome, resting_active):
    """A FILLED claim is a live position at ITS venue, and a working mirrored rest makes the
    Schwab-scoped union read zero as its NORMAL state. Neither may release the claim."""
    released: list = []
    strategy = _claim_strategy(released)
    state = _claim_state(outcome=outcome, resting_active=resting_active)
    strategy._symbol_states[SYMBOL] = state

    strategy.update_position(SYMBOL, 0, held_qty=0)

    assert state.fanout_webull_claimed is True, "the Webull claim was erased by a Schwab-scoped zero"
    assert released == []


def test_db2_control_a_claim_with_no_venue_evidence_does_release():
    """CONTROL. The hold must still expire when nothing at the Webull venue contradicts the zero --
    otherwise the veto above is indistinguishable from a claim that can never be released."""
    released: list = []
    strategy = _claim_strategy(released)
    state = _claim_state(outcome="held", resting_active=False)
    strategy._symbol_states[SYMBOL] = state

    strategy.update_position(SYMBOL, 0, held_qty=0)

    assert state.fanout_webull_claimed is False
    assert released == ["schwab_union_zero_positive_evidence_hold_expired"]


# --------------------------------------------------------------------------- RECOV1

def _managed_row(*, quantity: int = 100, entry_price: str = "11.50") -> OmsManagedPosition:
    """A REAL ORM row. RECOV1 was previously carried on a transcribed predicate and a namespace."""
    return OmsManagedPosition(
        id=uuid4(), strategy_code="schwab_1m_v2", broker_account_name=ACCT, symbol=SYMBOL,
        entry_price=Decimal(entry_price), original_quantity=100, current_quantity=quantity,
        entry_path="resting", entry_time=datetime.now(UTC), peak_profit_pct=Decimal("0"),
        current_profit_pct=Decimal("0"), tier=1, scale_pnl=Decimal("0"),
        config_name="make_v2_variant", status="open",
    )


def _oms(*, latched: bool, read: _PositionRead, spawned: list) -> OmsRiskService:
    import logging

    o = object.__new__(OmsRiskService)
    o.logger = logging.getLogger("recov1-harness")
    o._exit_reservation_released = {KEY} if latched else set()
    o._v2_exit_close_failures = {KEY: 99}
    o._V2_EXIT_RECONCILE_AFTER_FAILURES = 3
    o._webull_protect_base = {}
    # ⛔ INSTANCE-OWNED, NEVER THE CLASS ATTRIBUTE. `_v2_exit_stood_down` is declared at
    # oms/service.py:521 as a CLASS-level set and re-assigned per instance in __init__.
    # object.__new__ skips __init__, so an unassigned fixture mutates the one shared set: the
    # UNKNOWN case reaches the abandonment threshold and writes into it, and a later HELD case
    # happens to clear it. That made the suite order-dependent and unlike production.
    o._v2_exit_stood_down = set()

    async def _state(_a, _s):
        return read

    async def _is_flat(_a, _s, *, established_at=None, state=None):
        return read is _PositionRead.FLAT_CONFIRMED

    o._broker_symbol_position_state = _state          # transport boundary
    o._broker_symbol_is_flat = _is_flat               # transport boundary
    o._spawn_webull_protection = lambda **kw: spawned.append(kw)
    return o


def test_recov1_reprotects_only_on_a_latched_and_positively_held_read():
    spawned: list = []
    service = _oms(latched=True, read=_PositionRead.HELD, spawned=spawned)

    asyncio.run(service._v2_close_reconcile_flat(None, ACCT, SYMBOL, _managed_row()))

    assert spawned == [{
        "broker_account_name": ACCT, "symbol": SYMBOL,
        "quantity": 100, "entry_price": 11.5, "strategy_code": "schwab_1m_v2",
    }]
    assert service._exit_reservation_released == set(), "the latch must clear so the next exit can release"


def test_recov1_control_an_inconclusive_read_never_reattaches():
    """⛔ Re-attaching on an UNKNOWN read would place a protective pair against shares we may no
    longer own -- the ERNA unpaired-sell shape. The latch must SURVIVE so a later HELD read can act."""
    spawned: list = []
    service = _oms(latched=True, read=_PositionRead.UNKNOWN, spawned=spawned)

    asyncio.run(service._v2_close_reconcile_flat(None, ACCT, SYMBOL, _managed_row()))

    assert spawned == []
    assert service._exit_reservation_released == {KEY}


def test_recov1_control_a_held_read_without_the_latch_does_nothing():
    spawned: list = []
    service = _oms(latched=False, read=_PositionRead.HELD, spawned=spawned)

    asyncio.run(service._v2_close_reconcile_flat(None, ACCT, SYMBOL, _managed_row()))

    assert spawned == []


@pytest.mark.parametrize(("quantity", "entry_price"), [(0, "11.50"), (100, "0")])
def test_recov1_an_unpriceable_row_skips_rather_than_guessing(quantity, entry_price):
    """A row that cannot price a protective pair must SKIP and say the position may be uncovered --
    never spawn protection at a guessed size or price."""
    spawned: list = []
    service = _oms(latched=True, read=_PositionRead.HELD, spawned=spawned)

    service._reprotect_after_failed_release(ACCT, SYMBOL, _managed_row(quantity=quantity, entry_price=entry_price))

    assert spawned == []


def test_the_recov1_fixture_never_touches_the_class_level_stand_down_set():
    """⛔ ISOLATION ASSERTION. `_v2_exit_stood_down` is a CLASS attribute (oms/service.py:521)
    re-assigned per instance in __init__. A fixture built with object.__new__ and no assignment
    mutates the single shared set, so one test's UNKNOWN read leaks into the next test's state.
    """
    before = set(OmsRiskService._v2_exit_stood_down)

    first = _oms(latched=True, read=_PositionRead.UNKNOWN, spawned=[])
    second = _oms(latched=True, read=_PositionRead.HELD, spawned=[])

    assert first._v2_exit_stood_down is not OmsRiskService._v2_exit_stood_down
    assert second._v2_exit_stood_down is not first._v2_exit_stood_down

    asyncio.run(first._v2_close_reconcile_flat(None, ACCT, SYMBOL, _managed_row()))

    assert second._v2_exit_stood_down == set(), "one fixture's stand-down leaked into another"
    assert set(OmsRiskService._v2_exit_stood_down) == before, "the CLASS attribute was mutated"
