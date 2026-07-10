"""PR #2 dual-broker bake-off — mirror every primary Schwab v2 OPEN to a SECOND
(Webull) account (`_maybe_mirror_v2_open`).

Load-bearing properties proven here:
- flag OFF -> byte-identical: a v2 buy-open produces exactly ONE submit / one leg
  (primary only); the mirror method returns at its no-op guard;
- flag ON -> a v2 primary buy-open produces TWO submits (schwab + webull) and a
  managed row on EACH account for the symbol;
- a Webull reject is recorded + swallowed and NEVER affects the intact primary leg.

Harness mirrors tests/unit/test_orb_oms_quote_priced_entry.py: a real OmsRiskService
over an in-memory SQLite session_factory, with an injectable broker adapter so the
webull leg can be made to reject deterministically.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder, OmsManagedPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

PRIMARY = "paper:schwab_1m_v2"
WEBULL = "live:v2_webull"


# --------------------------------------------------------------------------- helpers

class _FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    async def xadd(self, stream, fields, **kwargs):
        del kwargs
        self.entries.append((stream, json.loads(fields["data"])))
        return "1-0"

    async def get(self, key):
        return None

    async def set(self, key, value, ex=None):
        del ex
        return True

    async def xread(self, offsets, block=0, count=0):
        del offsets, block, count
        return []

    async def aclose(self):
        return None


class _MirrorAdapter:
    """Simulated-style adapter that records every submit and can be told to reject one
    account's submits (to simulate a Webull reject for a Schwab-ineligible foreign name)."""

    def __init__(self, *, reject_account: str | None = None) -> None:
        self._sim = SimulatedBrokerAdapter()
        self._reject_account = reject_account
        self.submits: list[tuple[str, str]] = []  # (broker_account_name, symbol)

    async def submit_order(self, request: OrderRequest) -> list[ExecutionReport]:
        self.submits.append((request.broker_account_name, request.symbol))
        if self._reject_account is not None and request.broker_account_name == self._reject_account:
            return [
                ExecutionReport(
                    event_type="rejected",
                    client_order_id=request.client_order_id,
                    broker_order_id=None,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=request.quantity,
                    reason="webull rejected: insufficient buying power",
                    metadata=dict(request.metadata),
                )
            ]
        return await self._sim.submit_order(request)

    async def fetch_order_update(self, request: OrderRequest):
        return await self._sim.fetch_order_update(request)

    async def list_account_positions(self, broker_account_name: str):
        return await self._sim.list_account_positions(broker_account_name)


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _oms(*, adapter: _MirrorAdapter, mirror_on: bool) -> OmsRiskService:
    return OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_v2_exit_management_enabled=True,
            strategy_schwab_1m_v2_account_name=PRIMARY,
            strategy_schwab_1m_v2_webull_account_name=WEBULL,
            strategy_schwab_1m_v2_webull_mirror_enabled=mirror_on,
        ),
        redis_client=_FakeRedis(),
        session_factory=_session_factory(),
        broker_adapter=adapter,
    )


def _v2_open(*, symbol: str = "VSME", qty: str = "10", account: str = PRIMARY) -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="schwab_1m_v2",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name=account,
            symbol=symbol,
            side="buy",
            quantity=Decimal(qty),
            intent_type="open",
            reason="ENTRY_ATR_FLIP",
            # reference_price -> the simulated adapter fills; path -> managed-row entry path.
            metadata={"path": "ATR Flip", "reference_price": "2.50"},
        ),
    )


def _orders(service: OmsRiskService) -> list[BrokerOrder]:
    with service.session_factory() as session:
        return list(session.scalars(select(BrokerOrder)).all())


def _managed(service: OmsRiskService) -> list[OmsManagedPosition]:
    with service.session_factory() as session:
        return list(session.scalars(select(OmsManagedPosition)).all())


# --------------------------------------------------------------------------- tests

@pytest.mark.asyncio
async def test_mirror_off_no_second_leg():
    """Flag OFF -> byte-identical: exactly ONE submit / one leg / one managed row (primary)."""
    adapter = _MirrorAdapter()
    service = _oms(adapter=adapter, mirror_on=False)
    events = await service.process_trade_intent(_v2_open())

    assert [e.payload.status for e in events] == ["accepted", "filled"]
    assert adapter.submits == [(PRIMARY, "VSME")]  # a single submit, primary only

    orders = _orders(service)
    assert len(orders) == 1
    assert {o.symbol for o in orders} == {"VSME"}

    managed = _managed(service)
    assert len(managed) == 1
    assert managed[0].broker_account_name == PRIMARY


@pytest.mark.asyncio
async def test_mirror_on_creates_webull_leg():
    """Flag ON -> TWO submits (schwab + webull) and a managed row on EACH account."""
    adapter = _MirrorAdapter()
    service = _oms(adapter=adapter, mirror_on=True)
    events = await service.process_trade_intent(_v2_open())

    # The RETURN value is still only the primary leg (mirror is an independent post-step).
    assert [e.payload.status for e in events] == ["accepted", "filled"]

    # Both accounts submitted the same symbol.
    assert (PRIMARY, "VSME") in adapter.submits
    assert (WEBULL, "VSME") in adapter.submits
    assert len(adapter.submits) == 2

    # Two BrokerOrders with DISTINCT client_order_ids (no collision).
    orders = _orders(service)
    assert len(orders) == 2
    assert len({o.client_order_id for o in orders}) == 2

    # A managed row on EACH account for the symbol.
    managed = _managed(service)
    accounts = {m.broker_account_name for m in managed}
    assert accounts == {PRIMARY, WEBULL}
    webull_leg = [m for m in managed if m.broker_account_name == WEBULL]
    assert len(webull_leg) == 1
    assert webull_leg[0].symbol == "VSME" and webull_leg[0].status == "open"


@pytest.mark.asyncio
async def test_mirror_webull_reject_does_not_affect_primary():
    """Flag ON, the webull adapter rejects -> the primary leg is fully intact, the mirror
    reject is recorded + swallowed, no crash, no primary managed-row disturbance."""
    adapter = _MirrorAdapter(reject_account=WEBULL)
    service = _oms(adapter=adapter, mirror_on=True)

    events = await service.process_trade_intent(_v2_open())  # must not raise

    # Primary leg unaffected.
    assert [e.payload.status for e in events] == ["accepted", "filled"]
    assert (PRIMARY, "VSME") in adapter.submits
    assert (WEBULL, "VSME") in adapter.submits

    # The primary managed row exists and is open; the webull leg (rejected) has NO managed row.
    managed = _managed(service)
    assert [m.broker_account_name for m in managed] == [PRIMARY]
    assert managed[0].status == "open"

    # Both orders persisted: primary filled, webull rejected.
    orders = {o.broker_account_id: o for o in _orders(service)}
    assert len(orders) == 2
    statuses = sorted(o.status for o in orders.values())
    assert statuses == ["filled", "rejected"]
