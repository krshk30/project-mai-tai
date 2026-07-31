"""`_find_oco_entry_order` must resolve to an entry that CAN have OCO children (2026-07-31).

⭐⭐ WHY. The OCO exit poll asks "has this symbol's bracket exit filled?" by first resolving the
bracket ENTRY order, then looking for child legs hanging off its client_order_id. The lookup ordered
by `updated_at DESC` with **no status filter**, so the newest *cancelled* buy could win — and a
cancelled entry never had a position, so it has no OCO children and never will. The poll then asks
about a bracket that cannot exist, gets nothing, and the managed row stays open forever.

Live 2026-07-31, AXTU on live:schwab_1m_v2:

    15:15:47  entry-1 FILLED   (coid ...28a67ee60b14)  -> its OCO exit filled 15:26:52 @3.60
    15:31:16  a buy CANCELLED  (coid ...5c583838cf31)  <- newest by updated_at from here on
    16:03:05  entry-2 FILLED   (coid ...7a66d16fbbfb)  -> its OCO exit filled 16:17:07 @3.83

Entry-1's exit went unrecorded; so did entry-2's. Both were recovered from Schwab history hours
later and backfilled. Schwab's fill -> order-history propagation lags minutes, so the window in
which the correct filled entry was still the newest row was small.

⛔ HONESTY: this is a REAL bug on its own merits — an OCO entry lookup resolving to a cancelled
order is wrong regardless — but it is NOT yet proven to be the sole cause of the AXTU/AXTX misses.
An earlier version of this theory was dismissed after comparing the wrong instants on FCUV.
Confirmation is in vivo via `[OMS-OCO-EXIT-MISS]`: pre-fix its `entry_coid` should point at a
cancelled order on an old row; post-fix, misses on old rows should stop.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import Base, BrokerOrder
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore

ACCT = "live:schwab_1m_v2"
SYMBOL = "AXTU"
T0 = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    store = OmsStore()
    with sessionmaker(bind=engine)() as s:
        strategy = store.ensure_strategy(s, "schwab_1m_v2", name="Schwab 1m v2")
        account = store.ensure_broker_account(
            s, ACCT, provider="schwab", environment="live"
        )
        s.flush()
        s.info["strategy_id"] = strategy.id
        s.info["account_id"] = account.id
        yield s


def _svc() -> OmsRiskService:
    return OmsRiskService.__new__(OmsRiskService)   # the method only needs the passed session


def _buy(session, *, coid: str, status: str, minutes: int, symbol: str = SYMBOL):
    o = BrokerOrder(
        intent_id=None,
        strategy_id=session.info["strategy_id"],
        broker_account_id=session.info["account_id"],
        client_order_id=coid,
        broker_order_id=coid.replace("-", "")[:18],
        symbol=symbol,
        side="buy",
        order_type="limit",
        time_in_force="day",
        quantity=Decimal("2"),
        status=status,
        submitted_at=T0 + timedelta(minutes=minutes),
        updated_at=T0 + timedelta(minutes=minutes),
    )
    session.add(o)
    session.flush()
    return o


def test_the_live_axtu_shape_resolves_to_the_FILLED_entry(session) -> None:
    """THE REGRESSION. A cancelled buy newer than the filled entry must NOT win the lookup."""
    _buy(session, coid="filled-entry-1", status="filled", minutes=15)
    _buy(session, coid="cancelled-newer", status="cancelled", minutes=31)
    got = _svc()._find_oco_entry_order(session, ACCT, SYMBOL)
    assert got is not None
    assert got.client_order_id == "filled-entry-1", (
        "resolved to a cancelled order — it has no OCO children, so the exit can never be found"
    )


def test_newest_FILLED_entry_still_wins(session) -> None:
    """The fix must not break re-entries: among filled entries the most recent is still correct."""
    _buy(session, coid="filled-entry-1", status="filled", minutes=15)
    _buy(session, coid="cancelled-noise", status="cancelled", minutes=31)
    _buy(session, coid="filled-entry-2", status="filled", minutes=63)
    got = _svc()._find_oco_entry_order(session, ACCT, SYMBOL)
    assert got.client_order_id == "filled-entry-2"


@pytest.mark.parametrize("dead", ["cancelled", "canceled", "rejected", "expired"])
def test_no_dead_status_can_ever_win(session, dead: str) -> None:
    """Every non-fill terminal is equally unable to carry OCO children."""
    _buy(session, coid="filled-entry", status="filled", minutes=10)
    _buy(session, coid=f"{dead}-newer", status=dead, minutes=40)
    assert _svc()._find_oco_entry_order(session, ACCT, SYMBOL).client_order_id == "filled-entry"


def test_returns_none_when_there_is_no_filled_entry(session) -> None:
    """No filled entry => no position => no OCO children. None is the honest answer, and the
    caller already degrades to 'no exit recorded' rather than raising."""
    _buy(session, coid="cancelled-only", status="cancelled", minutes=20)
    assert _svc()._find_oco_entry_order(session, ACCT, SYMBOL) is None


def test_scoped_to_symbol_and_account(session) -> None:
    """Ownership scoping is unchanged — a different symbol must never satisfy the lookup."""
    _buy(session, coid="other-symbol", status="filled", minutes=50, symbol="FCUV")
    assert _svc()._find_oco_entry_order(session, ACCT, SYMBOL) is None


def test_missing_session_still_returns_none(session) -> None:
    """Documented tolerance: some callers drive the close path without a session."""
    assert _svc()._find_oco_entry_order(None, ACCT, SYMBOL) is None
