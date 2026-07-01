"""ORB fill-counted entry-state + reclaim reconcile (2026-07-01 rewrite).

#388 reconciled off a redis order-events stream that the OMS never publishes (dead
>=20d) → DOA: a filled-then-exited entry kept ``traded=True`` forever (no path cleared
it on exit), so a re-break could never reclaim (the CANF case). This rewrite reconciles
off ``broker_order_events`` (the DB path — proven live) and tracks held qty across BUY
(open) and SELL (close) fills, so ``traded`` mirrors a REAL position and clears on a flat
exit → the re-break reclaims. attempts is only ever burned by an ORB emit, never by the
reconcile, so OMS quote-drift-cancel churn can't exhaust the cap.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from project_mai_tai.services.orb_app import OrbService, _SymbolState
from project_mai_tai.settings import Settings


def _svc() -> OrbService:
    return OrbService(settings=Settings(orb_running_high_enabled=True), redis_client=MagicMock())


def _apply(svc, symbol, side, event_type, qty=5.0, payload=None) -> None:
    svc._apply_order_event(
        symbol=symbol, side=side, event_type=event_type, quantity=qty, payload=payload or {}
    )


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


def test_open_fill_marks_traded_and_held() -> None:
    svc = _svc()
    st = svc._states["FOO"] = _SymbolState()
    st.pending, st.attempts = True, 1
    _apply(svc, "FOO", "buy", "filled", 5.0, {"metadata": {"limit_price": "2.55"}})
    assert st.pending is False
    assert st.traded is True
    assert st.held_qty == 5.0
    assert st.entry_price == 2.55


def test_filled_then_exited_clears_traded_and_is_reenterable() -> None:
    """THE CANF CASE — the whole point of the fix: a filled entry that later exits must
    clear traded so a re-break can reclaim. #388 could never do this."""
    svc = _svc()
    st = svc._states["CANF"] = _SymbolState()
    st.pending, st.attempts = True, 1
    _apply(svc, "CANF", "buy", "filled", 5.0, {"metadata": {"limit_price": "4.87"}})
    assert st.traded is True and st.held_qty == 5.0
    assert svc._can_enter(st) is False               # holding -> not yet
    _apply(svc, "CANF", "sell", "filled", 5.0)       # OMS exit fills -> flat
    assert st.held_qty == 0.0
    assert st.traded is False                         # CLEARED on flat
    assert st.entry_price is None
    assert svc._can_enter(st) is True                 # RE-ENTERABLE -> re-break reclaims


def test_two_attempt_cap_holds_reclaim_then_stops() -> None:
    """Re-break takes attempt 2/2, then suppressed — even though flat (traded cleared)."""
    svc = _svc()
    st = svc._states["FOO"] = _SymbolState()
    st.pending, st.attempts = True, 1                 # emit #1
    _apply(svc, "FOO", "buy", "filled", 5.0)
    _apply(svc, "FOO", "sell", "filled", 5.0)         # exited
    assert svc._can_enter(st) is True                 # reclaim allowed (1 < 2)
    st.pending, st.attempts = True, 2                 # emit #2 (the reclaim)
    _apply(svc, "FOO", "buy", "filled", 5.0)
    _apply(svc, "FOO", "sell", "filled", 5.0)         # exited again
    assert st.attempts == 2
    assert st.traded is False                         # flat...
    assert svc._can_enter(st) is False                # ...but cap hit -> no third try this window


def test_oms_cancel_churn_does_not_burn_attempts() -> None:
    """CANF had ~15 quote-drift-cancels for one intent. The reconcile must not treat that
    as burning entry attempts (attempts is only incremented on an ORB emit)."""
    svc = _svc()
    st = svc._states["CANF"] = _SymbolState()
    st.pending, st.attempts = True, 1
    for _ in range(15):
        _apply(svc, "CANF", "buy", "cancelled", 5.0)
    assert st.attempts == 1                           # NEVER touched by the reconcile
    assert st.pending is False                        # cleared (idempotent)
    assert svc._can_enter(st) is True                 # still one try left


def test_abandon_reject_resets_no_phantom() -> None:
    svc = _svc()
    st = svc._states["TC"] = _SymbolState()
    st.pending, st.attempts = True, 1
    _apply(svc, "TC", "buy", "rejected", 5.0, {"metadata": {"abandon_reason_code": "ASK_PAST_GAP_CAP"}})
    assert st.pending is False
    assert st.traded is False and st.held_qty == 0.0  # NOT a fill -> no phantom
    assert svc._can_enter(st) is True                 # re-enterable


def test_pending_timeout_clears_stuck_pending() -> None:
    """ASK_PAST_GAP_CAP pre-order abandons emit NO broker_order_events row, so pending would
    stick forever without the timeout guard."""
    svc = _svc()
    st = svc._states["TC"] = _SymbolState()
    st.pending, st.attempts = True, 1
    st.pending_since = datetime.now(UTC) - timedelta(seconds=60)   # stale (> 45s)
    svc._expire_stale_pending()
    assert st.pending is False
    assert svc._can_enter(st) is True


def test_pending_not_cleared_before_timeout() -> None:
    svc = _svc()
    st = svc._states["TC"] = _SymbolState()
    st.pending, st.attempts = True, 1
    st.pending_since = datetime.now(UTC) - timedelta(seconds=10)   # fresh (< 45s)
    svc._expire_stale_pending()
    assert st.pending is True                          # not yet


def test_partial_exit_keeps_traded_until_flat() -> None:
    svc = _svc()
    st = svc._states["FOO"] = _SymbolState()
    st.pending, st.attempts = True, 1
    _apply(svc, "FOO", "buy", "filled", 5.0)
    _apply(svc, "FOO", "sell", "filled", 2.0)          # partial out
    assert st.held_qty == 3.0 and st.traded is True    # still holding
    _apply(svc, "FOO", "sell", "filled", 3.0)          # rest out
    assert st.held_qty == 0.0 and st.traded is False


def test_rejected_exit_leaves_position_conservative() -> None:
    """JEM today: exit SELL was rejected. Conservative — stay traded (still held), no reclaim,
    no phantom. Never errs toward re-entering a name we may still hold."""
    svc = _svc()
    st = svc._states["JEM"] = _SymbolState()
    st.pending, st.attempts = True, 1
    _apply(svc, "JEM", "buy", "filled", 5.0)
    _apply(svc, "JEM", "sell", "rejected", 5.0)
    assert st.held_qty == 5.0 and st.traded is True
    assert svc._can_enter(st) is False


def test_accepted_is_noop() -> None:
    svc = _svc()
    st = svc._states["FOO"] = _SymbolState()
    st.pending, st.attempts = True, 1
    _apply(svc, "FOO", "buy", "accepted", 5.0)         # submission ack only
    assert st.pending is True and st.traded is False


def test_unknown_symbol_is_noop() -> None:
    svc = _svc()
    svc._states.clear()
    _apply(svc, "ZZZ", "buy", "filled", 5.0)           # no state -> no crash
    assert "ZZZ" not in svc._states


def test_fill_price_from_payload_precedence() -> None:
    assert OrbService._fill_price_from_payload({"fill_price": "1.2"}) == 1.2
    assert OrbService._fill_price_from_payload({"metadata": {"limit_price": "3.4"}}) == 3.4
    assert OrbService._fill_price_from_payload({"metadata": {"reference_price": "5.6"}}) == 5.6
    assert OrbService._fill_price_from_payload({}) is None


def test_reconcile_applies_rows_and_advances_cursor() -> None:
    """End-to-end via a mocked fetch: the CANF fill→exit flows through _reconcile_orders,
    cursor advances to the last event, and the symbol becomes re-enterable."""
    svc = _svc()
    st = svc._states["CANF"] = _SymbolState()
    st.pending, st.attempts = True, 1
    t1 = datetime(2026, 7, 1, 13, 31, 26, tzinfo=UTC)
    t2 = datetime(2026, 7, 1, 13, 31, 41, tzinfo=UTC)
    rows = [
        (t1, "filled", "CANF", "buy", 5.0, {"metadata": {"limit_price": "4.87"}}),
        (t2, "filled", "CANF", "sell", 5.0, {}),
    ]
    svc.session_factory = MagicMock()                  # non-None so the reconcile proceeds
    svc._oe_cursor = datetime(2026, 7, 1, 13, 0, 0, tzinfo=UTC)  # before the events
    svc._fetch_order_events_since = lambda cursor: [r for r in rows if r[0] > cursor]
    asyncio.run(svc._reconcile_orders())
    assert st.held_qty == 0.0 and st.traded is False   # filled then exited -> flat
    assert svc._oe_cursor == t2                          # cursor advanced to last event
    assert svc._can_enter(st) is True                   # re-enterable
