"""Per-target bound for CANCEL requests against an already-terminal order.

Production replay control: LASE, paper:schwab_1m, 2026-06-02 13:13:24.666--13:13:40.555
ET.  The same target order received 48 broker refusals saying ``Order in state FILLED cannot be
canceled``.  The first reply was authoritative; the next 47 requests could not learn anything.
"""

from __future__ import annotations

from decimal import Decimal
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.settings import Settings


class _Redis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, object]]] = []

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        del kwargs
        self.entries.append((stream, json.loads(fields["data"])))
        return "1-0"

    async def get(self, key: str):
        del key
        return None

    async def set(self, key: str, value: str, ex: int | None = None):
        del key, value, ex
        return True

    async def aclose(self) -> None:
        return None


class _LaseDeadTargetAdapter:
    """Replay the broker half of LASE's 48-request episode."""

    def __init__(self, *, cancel_reason: str = "Order in state FILLED cannot be canceled") -> None:
        self.cancel_reason = cancel_reason
        self.requests = []

    async def submit_order(self, request):
        self.requests.append(request)
        if request.intent_type == "open":
            return [
                ExecutionReport(
                    event_type="accepted",
                    client_order_id=request.client_order_id,
                    broker_order_id=f"broker-{len(self.requests)}",
                    symbol=request.symbol,
                    side=request.side,
                    intent_type="open",
                    quantity=request.quantity,
                    reason=request.reason,
                    metadata=dict(request.metadata),
                    origin="broker",
                )
            ]
        return [
            ExecutionReport(
                event_type="rejected",
                client_order_id=request.client_order_id,
                broker_order_id=request.metadata.get("broker_order_id") or "broker-LASE",
                symbol=request.symbol,
                side=request.side,
                intent_type="cancel",
                quantity=request.quantity,
                reason=self.cancel_reason,
                metadata=dict(request.metadata),
                origin="broker",
            )
        ]

    async def fetch_order_update(self, request):
        del request
        return None

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _intent(symbol: str, intent_type: str, *, target: str = "") -> TradeIntentEvent:
    metadata = {"reference_price": "2.55"} if intent_type == "open" else {}
    if target:
        metadata["target_client_order_id"] = target
    return TradeIntentEvent(
        source_service="strategy-engine",
        payload=TradeIntentPayload(
            strategy_code="macd_30s",
            broker_account_name="paper:macd_30s",
            symbol=symbol,
            side="buy",
            quantity=Decimal("10") if intent_type == "open" else Decimal("0"),
            intent_type=intent_type,  # type: ignore[arg-type]
            reason="ENTRY_P1_MACD_CROSS" if intent_type == "open" else "WORKING_ORDER_REFRESH",
            metadata=metadata,
        ),
    )


def _service(adapter: _LaseDeadTargetAdapter):
    sessions = _session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=_Redis(),
        session_factory=sessions,
        broker_adapter=adapter,
    )
    return service, sessions


@pytest.mark.asyncio
async def test_lase_48_request_episode_trips_after_one_terminal_broker_reply(monkeypatch) -> None:
    """The named 48-attempt episode becomes 1 broker call + 47 bounded local refusals."""

    adapter = _LaseDeadTargetAdapter()
    service, sessions = _service(adapter)
    warnings: list[str] = []
    monkeypatch.setattr(
        service.logger,
        "warning",
        lambda template, *args, **kwargs: warnings.append(template % args),
    )
    opened = await service.process_trade_intent(_intent("LASE", "open"))
    target = opened[0].payload.client_order_id

    outcomes = []
    for _ in range(48):
        outcomes.extend(
            await service.process_trade_intent(_intent("LASE", "cancel", target=target))
        )

    broker_cancels = [request for request in adapter.requests if request.intent_type == "cancel"]
    assert len(broker_cancels) == 1
    assert outcomes[0].payload.reason == "Order in state FILLED cannot be canceled"
    assert [event.payload.reason for event in outcomes[1:]] == [
        "cancel_dead_target_retry_bound"
    ] * 47
    bound_lines = [line for line in warnings if "[OMS-CANCEL-DEAD-TARGET-BOUND]" in line]
    assert len(bound_lines) == 47
    assert all("bound=1" in line for line in bound_lines)
    assert all("reset=new_target_order_id" in line for line in bound_lines)

    with sessions() as session:
        target_order = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "LASE"))
        assert target_order is not None
        target_events = session.scalars(
            select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == target_order.id)
        ).all()
        # Open ACK + the one broker terminal refusal. Local suppressions do not counterfeit 47
        # additional broker events.
        assert [event.event_type for event in target_events] == ["accepted", "rejected"]
        assert len(
            session.scalars(
                select(TradeIntent).where(TradeIntent.intent_type == "cancel")
            ).all()
        ) == 48


@pytest.mark.asyncio
async def test_control_removing_the_bound_replays_all_48_broker_requests(monkeypatch) -> None:
    """Mutation control: disable the evidence read and the historical storm returns in full."""

    adapter = _LaseDeadTargetAdapter()
    service, _ = _service(adapter)
    monkeypatch.setattr(
        service.store,
        "count_terminal_cancel_refusals",
        lambda session, *, order_id: 0,
    )
    opened = await service.process_trade_intent(_intent("LASE", "open"))
    target = opened[0].payload.client_order_id

    for _ in range(48):
        await service.process_trade_intent(_intent("LASE", "cancel", target=target))

    assert len([request for request in adapter.requests if request.intent_type == "cancel"]) == 48


@pytest.mark.asyncio
async def test_new_target_order_id_resets_the_one_report_budget() -> None:
    """The reset is a different target id, never time, HELD, or position state."""

    adapter = _LaseDeadTargetAdapter()
    service, sessions = _service(adapter)
    first = (await service.process_trade_intent(_intent("LASE", "open")))[0]
    await service.process_trade_intent(
        _intent("LASE", "cancel", target=first.payload.client_order_id)
    )
    bounded = await service.process_trade_intent(
        _intent("LASE", "cancel", target=first.payload.client_order_id)
    )
    assert bounded[0].payload.reason == "cancel_dead_target_retry_bound"

    # Retire the old target exactly as broker sync eventually does, then create a replacement.
    with sessions() as session:
        old = session.scalar(
            select(BrokerOrder).where(BrokerOrder.client_order_id == first.payload.client_order_id)
        )
        assert old is not None
        old.status = "filled"
        session.commit()

    second = (await service.process_trade_intent(_intent("LASE", "open")))[0]
    assert second.payload.client_order_id != first.payload.client_order_id
    allowed = await service.process_trade_intent(
        _intent("LASE", "cancel", target=second.payload.client_order_id)
    )
    assert allowed[0].payload.reason == "Order in state FILLED cannot be canceled"
    assert len([request for request in adapter.requests if request.intent_type == "cancel"]) == 2


@pytest.mark.asyncio
async def test_nonterminal_rejection_does_not_consume_dead_target_budget() -> None:
    adapter = _LaseDeadTargetAdapter(cancel_reason="temporary upstream refusal")
    service, _ = _service(adapter)
    opened = await service.process_trade_intent(_intent("LASE", "open"))
    target = opened[0].payload.client_order_id

    first = await service.process_trade_intent(_intent("LASE", "cancel", target=target))
    second = await service.process_trade_intent(_intent("LASE", "cancel", target=target))

    assert first[0].payload.reason == "temporary upstream refusal"
    assert second[0].payload.reason == "temporary upstream refusal"
    assert len([request for request in adapter.requests if request.intent_type == "cancel"]) == 2


def test_terminal_reason_classifier_is_narrow_and_covers_both_live_strings() -> None:
    assert OmsRiskService._CANCEL_DEAD_TARGET_BROKER_REPORT_BOUND == 1
    assert OmsStore.TERMINAL_CANCEL_REFUSAL_REASONS == {
        "ORDER IN STATE CANCELED CANNOT BE CANCELED",
        "ORDER IN STATE FILLED CANNOT BE CANCELED",
    }
