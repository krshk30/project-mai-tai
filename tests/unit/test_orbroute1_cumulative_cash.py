"""Cumulative broker VWAP is not the price of the next execution slice."""

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerOrder, Fill, VirtualPosition
from project_mai_tai.oms.orb_country import PROTECTION, reconcile_one, record_protection_reports
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from tests.unit.test_orbroute1_oms_chain import seed_filled_webull, setup as setup


def prepared(setup, monkeypatch):
    entry_id, now = seed_filled_webull(setup, monkeypatch)
    service, factory, broker = setup
    asyncio.run(reconcile_one(service, entry_id))
    with factory() as session:
        state = session.get(BrokerOrder, entry_id).payload[PROTECTION]
        coid = WebullBrokerAdapter._combo_leg_coid(state["base"], "T")
    return service, factory, broker, entry_id, now, coid


@pytest.mark.parametrize("terminal,quantity,average,cash,pnl", [
    ("filled", "10", "6.6", "66", "10.70"),
    ("cancelled", "7", "6.3", "44.1", "5.39"),
])
def test_actual_child_cumulative_vwap_cash_repeat_and_terminal_partial(
    setup, monkeypatch, terminal, quantity, average, cash, pnl,
):
    service, factory, broker, entry_id, now, coid = prepared(setup, monkeypatch)
    first = ExecutionReport("partially_filled", coid, broker_order_id="target-native",
        broker_fill_id="target-cumulative-4", symbol="ABC", side="sell", intent_type="close",
        quantity=Decimal(10), filled_quantity=Decimal(4), fill_price=Decimal(6),
        metadata={"broker_first_execution_time_status": "UNKNOWN"}, reported_at=now, origin="broker")
    final = replace(first, event_type=terminal, broker_fill_id=f"target-cumulative-{quantity}",
        filled_quantity=Decimal(quantity), fill_price=Decimal(average),
        metadata={**first.metadata, "orbroute1_fresh_read": "true", **(
            {"webull_terminal_with_partial_fill": terminal} if terminal == "cancelled" else {})})
    for report in (first, first, final, final):
        asyncio.run(record_protection_reports(service, entry_id, [report]))
        with factory() as session:
            recorded = session.scalars(select(Fill).where(Fill.side == "sell")).all()
            assert sum((f.quantity * f.price for f in recorded), Decimal(0)) == (
                report.filled_quantity * report.fill_price)
    # Rehydrate a fresh OMS object against durable rows, not an in-memory delta.
    from project_mai_tai.oms.service import OmsRiskService
    restarted = OmsRiskService(settings=service.settings, redis_client=service.redis,
                              session_factory=factory, broker_adapter=broker)
    asyncio.run(record_protection_reports(restarted, entry_id, [final]))
    with factory() as session:
        fills = session.scalars(select(Fill).where(Fill.side == "sell").order_by(Fill.quantity)).all()
        assert len(fills) == 2
        assert sum((f.quantity for f in fills), Decimal(0)) == Decimal(quantity)
        assert sum((f.quantity * f.price for f in fills), Decimal(0)) == Decimal(cash)
        vp = session.scalar(select(VirtualPosition))
        assert vp.quantity == 10 - Decimal(quantity)
        assert vp.realized_pnl == Decimal(pnl)
        for fill in fills:
            assert fill.payload["metadata"]["broker_first_execution_time_status"] == "UNKNOWN"
            assert fill.payload["metadata"]["orbroute1_broker_cumulative_average"] in {"6", average}
            assert fill.filled_at.replace(tzinfo=now.tzinfo) == now
        cached = session.get(BrokerOrder, entry_id).payload[PROTECTION]["terminal_children"][coid]
        assert Decimal(cached["fill_price"]) == Decimal(average)
        assert Decimal(cached["filled_quantity"]) == Decimal(quantity)
    assert len(broker.sent) == 1


def test_original_cumulative_average_as_incremental_price_mutation_is_caught(setup, monkeypatch):
    service, _factory, _broker = setup
    original = service._record_order_reports

    async def wrong_price(**kwargs):
        kwargs["reports"] = [replace(report, fill_price=Decimal(
            report.metadata["orbroute1_broker_cumulative_average"])) for report in kwargs["reports"]]
        return await original(**kwargs)

    monkeypatch.setattr(service, "_record_order_reports", wrong_price)
    with pytest.raises(AssertionError):
        test_actual_child_cumulative_vwap_cash_repeat_and_terminal_partial(
            setup, monkeypatch, "filled", "10", "6.6", "66", "10.70")


def test_changed_cash_without_new_quantity_is_not_silently_rewritten(setup, monkeypatch):
    service, factory, _broker, entry_id, now, coid = prepared(setup, monkeypatch)
    report = ExecutionReport("partially_filled", coid, broker_order_id="target-native",
        broker_fill_id="partial-four", symbol="ABC", side="sell", intent_type="close",
        quantity=Decimal(10), filled_quantity=Decimal(4), fill_price=Decimal(6),
        reported_at=now, origin="broker")
    asyncio.run(record_protection_reports(service, entry_id, [report]))
    with pytest.raises(ValueError, match="cumulative_cash_inconsistent"):
        asyncio.run(record_protection_reports(service, entry_id, [replace(report, fill_price=Decimal(7))]))
    with factory() as session:
        sells = session.scalars(select(Fill).where(Fill.side == "sell")).all()
        assert len(sells) == 1
        assert sells[0].quantity * sells[0].price == 24


def test_recurring_derived_price_repeats_with_only_numeric_storage_roundoff(setup, monkeypatch):
    service, factory, _broker, entry_id, now, coid = prepared(setup, monkeypatch)
    first = ExecutionReport("partially_filled", coid, broker_order_id="target-native",
        broker_fill_id="partial-four", symbol="ABC", side="sell", intent_type="close",
        quantity=Decimal(10), filled_quantity=Decimal(4), fill_price=Decimal(6),
        reported_at=now, origin="broker")
    second = replace(first, broker_fill_id="partial-seven", filled_quantity=Decimal(7), fill_price=Decimal("6.5"))
    final = replace(first, event_type="filled", broker_fill_id="full-ten",
                    filled_quantity=Decimal(10), fill_price=Decimal("6.6"))
    for report in (first, second, second, final, final):
        asyncio.run(record_protection_reports(service, entry_id, [report]))
        with factory() as session:
            fills = session.scalars(select(Fill).where(Fill.side == "sell")).all()
            qty = sum((f.quantity for f in fills), Decimal(0))
            cash = sum((f.quantity * f.price for f in fills), Decimal(0))
            assert qty == report.filled_quantity
            assert abs(cash - qty * report.fill_price) <= qty * Decimal("0.000000005")
    with factory() as session:
        assert len(session.scalars(select(Fill).where(Fill.side == "sell")).all()) == 3
        assert session.scalar(select(VirtualPosition)).quantity == 0


def test_foreign_child_cumulative_report_never_books(setup, monkeypatch):
    service, factory, broker, entry_id, now, _coid = prepared(setup, monkeypatch)
    foreign = ExecutionReport("filled", "foreign-child", broker_order_id="foreign-native",
        broker_fill_id="foreign-execution", symbol="ABC", side="sell", intent_type="close",
        quantity=Decimal(10), filled_quantity=Decimal(10), fill_price=Decimal("6.6"),
        reported_at=now, origin="broker")
    with pytest.raises(ValueError, match="child_identity_mismatch"):
        asyncio.run(record_protection_reports(service, entry_id, [foreign]))
    with factory() as session:
        assert session.scalars(select(Fill).where(Fill.side == "sell")).all() == []
        assert session.scalar(select(VirtualPosition)).quantity == 10
    assert len(broker.sent) == 1
