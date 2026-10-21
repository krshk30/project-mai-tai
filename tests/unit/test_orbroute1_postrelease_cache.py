"""No ORB SELL may use Webull's cached pre-release holdings as fresh backing."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.oms import orb_schwab_eod as eod
from project_mai_tai.oms.orb_country import reconcile_one
from tests.unit.test_orbroute1_oms_chain import seed_filled_webull, setup as setup


@pytest.mark.parametrize("positions_case,expected_sells", [
    ("cached_only", 0), ("missing_receipt", 0), ("invalid_receipt", 0),
    ("bool_receipt", 0), ("negative_receipt", 0), ("at_release", 0),
    ("fresh_zero", 0), ("fresh_ten", 1), ("cache_then_fresh", 1),
])
def test_exit_requires_post_release_wire_receipt_not_cached_total(setup, monkeypatch, positions_case, expected_sells):
    entry_id, filled_at = seed_filled_webull(setup, monkeypatch)
    service, factory, broker = setup
    asyncio.run(reconcile_one(service, entry_id))
    seconds, age, post_reads = [0.0], [600.0], []

    async def advance(delay):
        seconds[0] += delay

    monkeypatch.setattr(eod, "monotonic", lambda: seconds[0])
    monkeypatch.setattr(eod, "sleep", advance)
    original_positions = list(broker.positions)

    async def positions(_account):
        if not broker.cancels:
            age[0] = 600.0
            return list(original_positions)
        post_reads.append(seconds[0])
        fresh = positions_case.startswith("fresh_") or (
            positions_case == "cache_then_fresh" and len(post_reads) >= 2)
        age[0] = 0.0 if fresh else seconds[0] if positions_case == "at_release" else 600.0
        if fresh:
            seconds[0] += .001
        return [] if positions_case == "fresh_zero" else list(original_positions)

    broker.list_account_positions = positions
    values = {"missing_receipt": None, "invalid_receipt": float("inf"),
              "bool_receipt": True, "negative_receipt": -1.0}
    broker.positions_wire_read_age_seconds = lambda _account: values.get(positions_case, age[0])
    original_detail = broker.fetch_order_update

    async def detail(request):
        if request.side == "buy":
            return ExecutionReport("filled", request.client_order_id,
                broker_order_id="entry-native", broker_fill_id="entry-execution", symbol="ABC",
                side="buy", intent_type="open", quantity=Decimal(10), filled_quantity=Decimal(10),
                fill_price=Decimal("5.53"), reported_at=filled_at, origin="broker")
        return await original_detail(request)

    broker.fetch_order_update = detail
    for suffix in ("T", "S"):
        broker.children[suffix] = ExecutionReport("cancelled", "exact-child-rebound",
            broker_order_id=f"native-{suffix}", symbol="ABC", side="sell", intent_type="close",
            quantity=Decimal(10), filled_quantity=Decimal(0), reported_at=filled_at, origin="broker")
    with factory.begin() as session:
        entry = session.get(BrokerOrder, entry_id)
        entry.payload = {**entry.payload, "orb_schwab_eod_close": {"phase": "checking"}}
        target = eod._Target(entry.id, entry.strategy_id, entry.broker_account_id, "live:orb",
            "ABC", "entry-native", entry.client_order_id, Decimal(10), Decimal(10), True)
    def clock():
        return filled_at.replace(hour=19, minute=55) + timedelta(seconds=seconds[0])
    try:
        asyncio.run(eod._attempt(service, target, uuid4(), clock))
    except ValueError:
        pass
    sells = [r for r in broker.sent if r.intent_type == "close"
             and r.metadata.get("webull_exit_only_pair") != "true"]
    assert len(sells) == expected_sells
    assert len(broker.cancels) == 1
    assert seconds[0] <= 30.25
    if positions_case == "cache_then_fresh":
        assert len(post_reads) >= 2
    if positions_case in {"cached_only", "at_release"}:
        with factory() as session:
            assert not session.scalars(select(BrokerOrder).where(BrokerOrder.side == "sell")).all()


def test_pre_release_cache_mutation_is_caught(setup, monkeypatch):
    async def stale_allowed(service, target, _released_at, _clock, *, quantity_receipt=None,
                            allow_broker_below_own=False):
        return await eod._broker_quantity(service, target, quantity_receipt=quantity_receipt,
                                          allow_broker_below_own=allow_broker_below_own)
    monkeypatch.setattr(eod, "_post_release_quantity", stale_allowed)
    with pytest.raises(AssertionError):
        test_exit_requires_post_release_wire_receipt_not_cached_total(setup, monkeypatch, "cached_only", 0)
