"""`sync_broker_orders` must poll open orders through the ROTATING list (#1027).

2026-09-21 live: open orders were polled newest-`updated_at` first, sequentially, and Webull's
order-detail budget ran out part-way through each pass. The same OLD order drew the 429 on every
sync - NCPL's filled entry went unseen ~786 s, GRML's ~255 s - so the position had no managed row,
no bracket and no exit for that long. #1027 added `OmsStore.list_open_orders_for_sync`; until the
sync loop calls it the rotation is inert. This file pins the call site by BEHAVIOUR.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

ACCOUNT = "live:orb"


class _FakeRedis:
    async def xadd(self, *args, **kwargs) -> str:
        return "1-0"


class _BudgetedStatusAdapter:
    """Answers the first `budget` order-detail reads of each sync pass; the rest are 429s (None)."""

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.passes: list[list[str]] = [[]]
        self.served: list[list[str]] = [[]]

    def next_pass(self) -> None:
        self.passes.append([])
        self.served.append([])

    async def fetch_order_update(self, request):
        self.passes[-1].append(request.client_order_id)
        if len(self.served[-1]) < self.budget:
            self.served[-1].append(request.client_order_id)
        return None  # nothing changed at the broker; a 429 reads the same to the loop


def _service(adapter, *, orders: int) -> OmsRiskService:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    service = OmsRiskService(
        Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=_FakeRedis(),
        session_factory=sf,
        broker_adapter=adapter,
    )
    with sf() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2", name="v2")
        account = service.store.ensure_broker_account(
            session, ACCOUNT, provider="webull", environment="live"
        )
        anchor = datetime(2026, 9, 21, 17, 40, tzinfo=UTC)
        for index in range(orders):
            intent = service.store.create_trade_intent(
                session,
                strategy=strategy,
                broker_account=account,
                event=TradeIntentEvent(
                    source_service="test",
                    payload=TradeIntentPayload(
                        strategy_code="schwab_1m_v2",
                        broker_account_name=ACCOUNT,
                        symbol=f"SYM{index}",
                        side="buy",
                        quantity=Decimal("1"),
                        intent_type="open",
                        reason="ENTRY",
                        metadata={},
                    ),
                ),
            )
            order = service.store.get_or_create_order(
                session,
                intent=intent,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id=f"order-{index}",
                symbol=f"SYM{index}",
                side="buy",
                quantity=Decimal("1"),
                metadata={},
                broker_order_id=f"broker-{index}",
                status="accepted",
            )
            order.updated_at = anchor + timedelta(seconds=index)  # order-0 is the OLDEST
        session.commit()
    return service


@pytest.mark.asyncio
async def test_the_oldest_open_order_is_not_starved_by_a_per_pass_status_budget() -> None:
    # NCPL 2026-09-21: three open orders, a budget that answers two reads per pass. Newest-first
    # and fixed, `order-0` (the oldest - the filled entry) is the one that NEVER gets an answer.
    adapter = _BudgetedStatusAdapter(budget=2)
    service = _service(adapter, orders=3)

    for index in range(3):
        if index:
            adapter.next_pass()
        await service.sync_broker_orders(account_names=[ACCOUNT])

    served = [set(answered) for answered in adapter.served]
    assert all(len(answered) == 2 for answered in served)  # control: the budget really bit
    assert set().union(*served) == {"order-0", "order-1", "order-2"}
    assert sum("order-0" in answered for answered in served) >= 1, (
        "the oldest open order was never served in three passes - the starvation is back"
    )
    # and the head of the pass really moves
    assert len({polled[0] for polled in adapter.passes}) == 3


@pytest.mark.asyncio
async def test_every_open_order_is_still_polled_on_every_pass_when_the_budget_allows() -> None:
    # CONTROL: rotation changes ORDER, never membership.
    adapter = _BudgetedStatusAdapter(budget=99)
    service = _service(adapter, orders=3)

    await service.sync_broker_orders(account_names=[ACCOUNT])
    adapter.next_pass()
    await service.sync_broker_orders(account_names=[ACCOUNT])

    assert [sorted(polled) for polled in adapter.passes] == [
        ["order-0", "order-1", "order-2"]
    ] * 2
