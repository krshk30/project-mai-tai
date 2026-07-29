"""The exit-capture must NEVER claim a trade we did not place.

⛔⭐ THE OPERATOR'S CORE RULE: "Mai Tai is not supposed to interfere with manual trades — it listens
to what is posted." On 2026-07-29 we did not interfere with the ORDER, but we CLAIMED it:

    operator's hand-placed TOS sell : 07:49:05 ET, 1000 shares @4.68
    our own AMIX v2 entry           : 07:51:26 ET, 2 shares
    -> booked as schwab_1m_v2-AMIX-open-5da65614d5aa-ocoexit-72643358

`fetch_oco_exit_fill` matched on SYMBOL ALONE and walked EVERY order in the account. The scoping
invariant ("act only on positions we placed") had been enforced on the ACTING path and never on the
RECORDING path.

Ownership is now structural: locate OUR entry by `entry_broker_order_id`, walk ONLY its
`childOrderStrategies`, and FAIL CLOSED without that id. The time/quantity checks are belts.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.settings import Settings

OUR_ENTRY_OID = "1007372643379"
MANUAL_OID = "1007372643358"
NOW = datetime.now(UTC)


def _order(oid, sym, instruction, status, qty, price, closed, children=()):
    return {
        "orderId": oid, "status": status,
        "closeTime": closed.strftime("%Y-%m-%dT%H:%M:%S+0000"),
        "orderLegCollection": [{"instruction": instruction, "instrument": {"symbol": sym}}],
        "orderActivityCollection": [{"executionLegs": [{"price": price, "quantity": qty}]}],
        "childOrderStrategies": list(children),
    }


def _adapter(orders):
    a = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    a.settings = Settings()

    class _Acct:
        account_hash = "HASH"
    a.accounts_by_name = {"live:schwab_1m_v2": _Acct()}

    async def _req(_method, _path):
        return 200, {}, orders
    a._authorized_request_json = _req
    return a


def _call(adapter, **kw):
    return asyncio.run(adapter.fetch_oco_exit_fill("live:schwab_1m_v2", "AMIX", "base", **kw))


# ---------------------------------------------------------------- THE REGRESSION
def test_a_manual_sell_on_the_same_symbol_is_NOT_claimed() -> None:
    """THE LIVE CASE. A hand-placed 1000-share sell sits in the account alongside our bracket."""
    manual = _order(MANUAL_OID, "AMIX", "SELL", "FILLED", 1000, 4.68, NOW - timedelta(minutes=20))
    ours = _order(OUR_ENTRY_OID, "AMIX", "BUY", "FILLED", 2, 4.7585, NOW - timedelta(minutes=18))
    got = _call(_adapter([manual, ours]),
                entry_broker_order_id=OUR_ENTRY_OID,
                entry_filled_at=NOW - timedelta(minutes=18),
                entry_quantity=Decimal("2"))
    assert got is None, f"CLAIMED the operator's manual trade: {got}"


def test_our_own_bracket_exit_is_still_captured() -> None:
    """The other direction — a real OCO child of OUR entry must still be booked."""
    child = _order("999", "AMIX", "SELL", "FILLED", 2, 4.92, NOW - timedelta(minutes=5))
    ours = _order(OUR_ENTRY_OID, "AMIX", "BUY", "FILLED", 2, 4.8287,
                  NOW - timedelta(minutes=10), children=[child])
    got = _call(_adapter([ours]),
                entry_broker_order_id=OUR_ENTRY_OID,
                entry_filled_at=NOW - timedelta(minutes=10),
                entry_quantity=Decimal("2"))
    assert got is not None and float(got["price"]) == 4.92
    assert got["broker_order_id"] == "999"


# ---------------------------------------------------------------- fail-closed
def test_no_ownership_proof_books_NOTHING() -> None:
    """⛔ Without the entry's broker id we cannot prove any sell is ours, so we book nothing.
    An unrecorded exit is a reporting gap; claiming someone else's trade is not acceptable."""
    manual = _order(MANUAL_OID, "AMIX", "SELL", "FILLED", 1000, 4.68, NOW - timedelta(minutes=20))
    assert _call(_adapter([manual]), entry_broker_order_id="") is None


def test_entry_absent_from_the_window_books_NOTHING() -> None:
    manual = _order(MANUAL_OID, "AMIX", "SELL", "FILLED", 2, 4.68, NOW - timedelta(minutes=20))
    assert _call(_adapter([manual]), entry_broker_order_id="NOT-IN-LIST") is None


# ---------------------------------------------------------------- the belts
def test_belt_an_exit_cannot_precede_its_entry() -> None:
    """Even INSIDE our own subtree, a fill older than the entry is not that entry's exit."""
    child = _order("999", "AMIX", "SELL", "FILLED", 2, 4.50, NOW - timedelta(minutes=30))
    ours = _order(OUR_ENTRY_OID, "AMIX", "BUY", "FILLED", 2, 4.75,
                  NOW - timedelta(minutes=10), children=[child])
    got = _call(_adapter([ours]), entry_broker_order_id=OUR_ENTRY_OID,
                entry_filled_at=NOW - timedelta(minutes=10), entry_quantity=Decimal("2"))
    assert got is None


def test_belt_an_exit_cannot_exceed_the_position() -> None:
    child = _order("999", "AMIX", "SELL", "FILLED", 1000, 4.68, NOW - timedelta(minutes=5))
    ours = _order(OUR_ENTRY_OID, "AMIX", "BUY", "FILLED", 2, 4.75,
                  NOW - timedelta(minutes=10), children=[child])
    got = _call(_adapter([ours]), entry_broker_order_id=OUR_ENTRY_OID,
                entry_filled_at=NOW - timedelta(minutes=10), entry_quantity=Decimal("2"))
    assert got is None


def test_the_zero_price_cancelled_sibling_is_still_skipped() -> None:
    """Pre-existing invariant must survive: a CANCELED sibling carries a $0 execution."""
    dead = _order("998", "AMIX", "SELL", "CANCELED", 2, 0.0, NOW - timedelta(minutes=5))
    live = _order("999", "AMIX", "SELL", "FILLED", 2, 4.92, NOW - timedelta(minutes=5))
    ours = _order(OUR_ENTRY_OID, "AMIX", "BUY", "FILLED", 2, 4.8287,
                  NOW - timedelta(minutes=10), children=[dead, live])
    got = _call(_adapter([ours]), entry_broker_order_id=OUR_ENTRY_OID,
                entry_filled_at=NOW - timedelta(minutes=10), entry_quantity=Decimal("2"))
    assert got is not None and float(got["price"]) == 4.92


# ==================================================================================================
# ⛔⭐ THE ISOLATING TEST. The three guards (subtree scoping · not-before-entry · not-bigger-than-
# position) OVERLAP, so the obvious cases are caught by more than one and a broken guard hides behind
# its neighbours — my first draft of this file passed with the symbol-only walk RESTORED.
#
# This case defeats BOTH belts on purpose: a manual sell with the SAME quantity as ours, filled AFTER
# our entry. Only real ownership scoping can reject it.
# ==================================================================================================

def test_a_manual_sell_that_LOOKS_exactly_like_ours_is_still_not_claimed() -> None:
    """Same symbol, same size, later than our entry — indistinguishable except by OWNERSHIP.

    ⛔ If this fails, the subtree scoping is gone and only the belts remain, which means any manual
    trade sized like ours gets claimed. The belts cannot save this case by construction."""
    manual = _order(MANUAL_OID, "AMIX", "SELL", "FILLED", 2, 4.68, NOW - timedelta(minutes=5))
    ours = _order(OUR_ENTRY_OID, "AMIX", "BUY", "FILLED", 2, 4.7585,
                  NOW - timedelta(minutes=10), children=[])   # our bracket has NO filled child yet
    got = _call(_adapter([manual, ours]),
                entry_broker_order_id=OUR_ENTRY_OID,
                entry_filled_at=NOW - timedelta(minutes=10),
                entry_quantity=Decimal("2"))
    assert got is None, (
        f"CLAIMED a manual trade that merely resembles ours ({got}) — ownership scoping is broken; "
        "the quantity/time belts cannot distinguish this case"
    )


def test_scoping_alone_is_what_rejects_it_belts_disabled() -> None:
    """Same as above with the belts NOT supplied at all, so ONLY the subtree walk is in play."""
    manual = _order(MANUAL_OID, "AMIX", "SELL", "FILLED", 2, 4.68, NOW - timedelta(minutes=5))
    ours = _order(OUR_ENTRY_OID, "AMIX", "BUY", "FILLED", 2, 4.7585, NOW - timedelta(minutes=10))
    got = _call(_adapter([manual, ours]), entry_broker_order_id=OUR_ENTRY_OID)
    assert got is None, f"symbol-only matching is back: {got}"
