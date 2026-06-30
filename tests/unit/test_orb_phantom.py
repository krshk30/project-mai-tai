"""ORB phantom-position / fill-counted entry-state fix (2026-06-30).

Before: ORB set traded=True on intent EMIT, so an OMS-abandoned entry (e.g. CELZ
ASK_PAST_GAP_CAP) left a phantom position + suppressed re-entry for the session.
After: state reflects CONFIRMED FILLS (reconciled off the order-events stream),
re-enterable up to the attempt cap (original + reclaim), then suppressed.
"""
import json
from unittest.mock import MagicMock

from project_mai_tai.services.orb_app import OrbService, _SymbolState
from project_mai_tai.settings import Settings


def _svc() -> OrbService:
    return OrbService(settings=Settings(orb_running_high_enabled=True), redis_client=MagicMock())


def _oe(symbol, status, *, strategy="orb", intent_type="open", fill_price=None, abandon=None):
    payload = {
        "strategy_code": strategy, "intent_type": intent_type, "symbol": symbol,
        "status": status, "side": "buy", "reason": "",
        "metadata": {"abandon_reason_code": abandon} if abandon else {},
    }
    if fill_price is not None:
        payload["fill_price"] = str(fill_price)
    return {"data": json.dumps({"payload": payload})}


def test_can_enter_gate() -> None:
    svc = _svc()
    st = _SymbolState()
    assert svc._can_enter(st) is True            # fresh
    st.pending = True
    assert svc._can_enter(st) is False           # in flight
    st.pending = False
    st.traded = True
    assert svc._can_enter(st) is False           # holding a fill
    st.traded = False
    st.attempts = 2
    assert svc._can_enter(st) is False           # at cap
    st.attempts = 1
    assert svc._can_enter(st) is True            # one try left


def test_fill_marks_traded_with_real_price() -> None:
    svc = _svc()
    st = svc._states["FOO"] = _SymbolState()
    st.pending = True
    st.attempts = 1
    svc._handle_order_event(_oe("FOO", "filled", fill_price="2.55"))
    assert st.pending is False
    assert st.traded is True            # confirmed fill -> holding
    assert st.entry_price == 2.55       # the REAL fill price


def test_abandon_resets_and_leaves_no_phantom() -> None:
    svc = _svc()
    st = svc._states["FOO"] = _SymbolState()
    st.pending = True
    st.attempts = 1
    svc._handle_order_event(_oe("FOO", "rejected", abandon="ASK_PAST_GAP_CAP"))
    assert st.pending is False
    assert st.traded is False           # NOT a fill -> no phantom position
    assert st.entry_price is None       # phantom price cleared
    assert svc._can_enter(st) is True   # re-enterable (1 < cap 2)


def test_attempt_cap_suppresses_after_two_tries() -> None:
    svc = _svc()
    st = svc._states["FOO"] = _SymbolState()
    st.pending, st.attempts = True, 1
    svc._handle_order_event(_oe("FOO", "cancelled"))     # original abandoned
    assert svc._can_enter(st) is True                    # reclaim still allowed
    st.pending, st.attempts = True, 2
    svc._handle_order_event(_oe("FOO", "cancelled"))     # reclaim abandoned
    assert svc._can_enter(st) is False                   # cap hit -> suppressed for the session


def test_order_event_ignores_other_strategy_and_non_open() -> None:
    svc = _svc()
    st = svc._states["FOO"] = _SymbolState()
    st.pending, st.attempts = True, 1
    svc._handle_order_event(_oe("FOO", "filled", strategy="schwab_1m_v2"))  # not ORB's
    assert st.traded is False and st.pending is True
    svc._handle_order_event(_oe("FOO", "filled", intent_type="close"))       # an exit, not the open
    assert st.traded is False and st.pending is True


def test_order_event_unknown_symbol_is_noop() -> None:
    svc = _svc()
    svc._states.clear()
    svc._handle_order_event(_oe("ZZZ", "filled", fill_price="1.0"))  # no state -> no crash
    assert "ZZZ" not in svc._states
