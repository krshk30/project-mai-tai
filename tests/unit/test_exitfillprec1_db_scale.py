"""Controlled JBDI quantities/prices; SQLite preimages emulate PG Numeric(18,8)."""


from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, Fill, OmsManagedPosition, SystemIncident, TradeIntent,
)
from tests.unit.test_oms_resolved_oco_fill_required import ACCOUNT, _service_with_weto

SYMBOL = "JBDI"
ENTRY_COID = "schwab_1m_v2-JBDI-open-CONTROLLED"
PROTECT_BASE = "schwab_1m_v2-JBDI-protect-CONTROLLED"
CHILD_ID = "EXITFILLPREC1-CONTROLLED-CHILD"


def _jbdi_preimage(stored_price, *, stored_qty="41"):
    service, sessions, row_id = _service_with_weto()
    with sessions.begin() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        entry = session.get(BrokerOrder, row.entry_order_id)
        intent = session.get(TradeIntent, entry.intent_id)
        row.symbol = entry.symbol = intent.symbol = SYMBOL
        row.original_quantity = row.current_quantity = 41
        entry.quantity = intent.quantity = Decimal("41")
        entry.client_order_id = row.entry_client_order_id = ENTRY_COID
        entry.payload = {**entry.payload, "webull_protect_base_client_order_id": PROTECT_BASE}
        row.entry_price = Decimal("7.31")
        detail = {"symbol": SYMBOL, "entry_broker_order_id": entry.broker_order_id,
                  "exit_base_client_order_id": PROTECT_BASE, "quantity": Decimal("41"),
                  "price": Decimal(stored_price), "broker_order_id": CHILD_ID,
                  "filled_at": datetime(2026, 10, 13, 16, 34, tzinfo=UTC)}
        service.store.apply_fill_to_positions(
            session, strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            symbol=SYMBOL, side="buy", quantity=Decimal("41"), price=Decimal("7.31"),
            reported_at=datetime(2026, 10, 13, 16, 32, 6, tzinfo=UTC),
        )
        assert service._persist_oco_exit_fill(session, ACCOUNT, SYMBOL, entry, detail)
        # Position reconciliation is separate from child booking; model the reported flat preimage.
        service.store.apply_fill_to_positions(
            session, strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            symbol=SYMBOL, side="sell", quantity=Decimal("41"), price=Decimal(stored_price),
            reported_at=detail["filled_at"],
        )
        fill = session.scalar(select(Fill).where(Fill.broker_fill_id == f"{CHILD_ID}:41"))
        fill.quantity = Decimal(stored_qty)
        assert service.store.get_virtual_position(
            session, strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            symbol=SYMBOL,
        ).quantity == 0
        for source in ("oco_exit_fill_unrecorded", "not_this_episode"):
            session.add(SystemIncident(
                service_name="oms-risk", severity="critical", status="open",
                title=f"EXITFILLPREC1 controlled {source}",
                payload={"source": source, "managed_row_id": row_id},
            ))
    return service, sessions, row_id, detail


@pytest.mark.parametrize("reported,stored", [
    ("6.7180487805", "6.71804878"),
    ("6.7180487850", "6.71804879"),
    ("6.7180487899", "6.71804879"),
    ("6.71", "6.71000000"),
])
@pytest.mark.asyncio
async def test_jbdi_41_already_durable_fill_closes_at_pg_price_scale(reported, stored):
    service, sessions, row_id, detail = _jbdi_preimage(stored)
    detail["price"] = Decimal(reported)
    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, detail=detail, expected_row_id=row_id,
    ) is True
    with sessions() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        fills = session.scalars(select(Fill).where(Fill.side == "sell", Fill.symbol == SYMBOL)).all()
        incidents = session.scalars(select(SystemIncident)).all()
        assert row.status == "closed" and row.current_quantity == 0
        assert len(fills) == 1 and fills[0].price == Decimal(stored)
        assert fills[0].quantity == 41 and fills[0].broker_fill_id == f"{CHILD_ID}:41"
        assert {i.payload["source"]: i.status for i in incidents} == {
            "oco_exit_fill_unrecorded": "closed", "not_this_episode": "open",
        }


@pytest.mark.asyncio
async def test_jbdi_wrong_stored_quantity_still_refuses():
    service, sessions, row_id, detail = _jbdi_preimage("6.71804878", stored_qty="40")
    detail["price"] = Decimal("6.7180487805")
    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, detail=detail, expected_row_id=row_id,
    ) is False
    with sessions() as session:
        assert session.get(OmsManagedPosition, UUID(row_id)).status == "open"
        assert session.scalar(select(Fill).where(Fill.side == "sell")).quantity == 40


@pytest.mark.parametrize("mutation", ["foreign_account", "foreign_fill_id", "different_8dp_price"])
@pytest.mark.asyncio
async def test_price_normalization_never_accepts_foreign_or_different_fill(mutation, monkeypatch):
    service, sessions, row_id, detail = _jbdi_preimage("6.71804878")
    with sessions.begin() as session:
        fill = session.scalar(select(Fill).where(Fill.side == "sell"))
        if mutation == "foreign_account":
            account = BrokerAccount(name="test:foreign", provider="webull", environment="test")
            session.add(account)
            session.flush()
            fill.broker_account_id = account.id
        elif mutation == "foreign_fill_id":
            fill.broker_fill_id = "FOREIGN-CHILD:41"
        else:
            fill.price = Decimal("6.71804877")
    monkeypatch.setattr(service, "_persist_oco_exit_fill", lambda *_args: False)
    detail["price"] = Decimal("6.7180487805")
    assert await service._close_resolved_oco_managed_row(
        ACCOUNT, SYMBOL, detail=detail, expected_row_id=row_id,
    ) is False
    with sessions() as session:
        assert session.get(OmsManagedPosition, UUID(row_id)).status == "open"
        assert len(session.scalars(select(Fill).where(Fill.side == "sell")).all()) == 1


def test_confirmed_flat_retry_recognizes_same_pg_rounded_fill():
    service, sessions, row_id, detail = _jbdi_preimage("6.71804878")
    detail["price"] = Decimal("6.7180487805")
    with sessions.begin() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        entry = session.get(BrokerOrder, row.entry_order_id)
        for incident in session.scalars(select(SystemIncident)):
            if incident.payload.get("source") == "oco_exit_fill_unrecorded":
                session.delete(incident)
        assert service._close_confirmed_flat_episode(
            session, ACCOUNT, SYMBOL, expected_row_id=row_id, entry=entry, detail=detail,
        ) is True
    with sessions() as session:
        relevant = session.scalars(select(SystemIncident)).all()
        assert not any(i.payload.get("source") == "oco_exit_fill_unrecorded" for i in relevant)


def test_fill_storage_contract_remains_numeric_18_8():
    assert Fill.__table__.c.price.type.precision == 18
    assert Fill.__table__.c.price.type.scale == 8
