"""Bound strategy-internal direct CANCEL paths against an already-terminal target.

Named replay: LASE, paper:schwab_1m, 2026-06-02 13:13:24.666--13:13:40.555 ET.
Target 7815b4e5-96c3-456f-8b01-6011d322e895 accumulated 48 quote-tick drift CANCEL
refusals in 15.889 seconds. The replay below places those durable events on the real drift path and
proves the next broker request is suppressed. Live remains UNEXERCISED until a covered direct path
reaches a second terminal refusal for the same target order id.

Coverage is 4 of 5 strategy-internal direct CANCEL submitters: refresh, drift, abandon, and the
cancel verifier. Webull's cancel_exit_pair remains outside this Schwab dead-target contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    BrokerAccount,
    BrokerOrder,
    BrokerOrderEvent,
    Strategy,
    TradeIntent,
)
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings


TERMINAL_REASON = "Order in state FILLED cannot be canceled"


class _Redis:
    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        del stream, fields, kwargs
        return "1-0"

    async def get(self, key: str):
        del key
        return None

    async def set(self, key: str, value: str, ex: int | None = None):
        del key, value, ex
        return True

    async def aclose(self) -> None:
        return None


class _DirectCancelAdapter:
    def __init__(
        self,
        *,
        cancel_event_type: str = "rejected",
        cancel_reason: str = TERMINAL_REASON,
    ) -> None:
        self.cancel_event_type = cancel_event_type
        self.cancel_reason = cancel_reason
        self.requests = []

    async def submit_order(self, request):
        self.requests.append(request)
        if request.intent_type == "cancel":
            return [
                ExecutionReport(
                    event_type=self.cancel_event_type,  # type: ignore[arg-type]
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
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id=f"replacement-{len(self.requests)}",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
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


def _service(adapter: _DirectCancelAdapter) -> tuple[OmsRiskService, sessionmaker[Session]]:
    sessions = _session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=_Redis(),
        session_factory=sessions,
        broker_adapter=adapter,
    )
    return service, sessions


def _seed_target(
    session: Session,
    *,
    symbol: str = "LASE",
    order_type: str = "market",
    client_order_id: str = "LASE-target-1",
) -> tuple[Strategy, BrokerAccount, TradeIntent, BrokerOrder]:
    strategy = Strategy(
        code="schwab_1m_v2",
        name="v2",
        execution_mode="live",
        metadata_json={},
    )
    account = BrokerAccount(
        name="live:schwab_1m_v2",
        provider="schwab",
        environment="live",
    )
    session.add_all([strategy, account])
    session.flush()
    intent = TradeIntent(
        strategy_id=strategy.id,
        broker_account_id=account.id,
        symbol=symbol,
        side="buy",
        intent_type="open",
        quantity=Decimal("10"),
        reason="WORKING_ORDER_REFRESH",
        status="submitted",
        payload={},
        created_at=datetime(2026, 6, 2, 17, 13, 24, 666000, tzinfo=UTC),
    )
    session.add(intent)
    session.flush()
    payload = {"order_type": order_type, "reference_price": "2.55"}
    if order_type == "limit":
        payload["limit_price"] = "2.55"
    order = BrokerOrder(
        intent_id=intent.id,
        strategy_id=strategy.id,
        broker_account_id=account.id,
        client_order_id=client_order_id,
        broker_order_id=f"broker-{client_order_id}",
        symbol=symbol,
        side="buy",
        order_type=order_type,
        time_in_force="day",
        quantity=Decimal("10"),
        status="accepted",
        payload=payload,
    )
    session.add(order)
    session.commit()
    return strategy, account, intent, order


def _working_report(order: BrokerOrder) -> ExecutionReport:
    return ExecutionReport(
        event_type="accepted",
        client_order_id=order.client_order_id,
        broker_order_id=order.broker_order_id,
        symbol=order.symbol,
        side=order.side,  # type: ignore[arg-type]
        intent_type="open",
        quantity=order.quantity,
        filled_quantity=Decimal("0"),
        metadata=dict(order.payload or {}),
        origin="broker",
    )


def _cancel_requests(adapter: _DirectCancelAdapter) -> list:
    return [request for request in adapter.requests if request.intent_type == "cancel"]


@pytest.mark.asyncio
async def test_lase_48_event_drift_replay_refuses_the_next_broker_request() -> None:
    """LASE was a quote-tick drift episode; replay that path, not the 5 s refresh path."""
    adapter = _DirectCancelAdapter()
    service, sessions = _service(adapter)
    with sessions() as session:
        _strategy, _account, _intent, order = _seed_target(session, order_type="limit")
        first = datetime(2026, 6, 2, 17, 13, 24, 666000, tzinfo=UTC)
        for offset in range(48):
            session.add(
                BrokerOrderEvent(
                    order_id=order.id,
                    event_type="rejected",
                    event_at=first + timedelta(milliseconds=offset * 338),
                    event_source="unknown",
                    payload={"reason": TERMINAL_REASON},
                )
            )
        session.commit()

    await service._run_drift_cancel("LASE", {"ask": 2.70}, 0.01)

    assert len(_cancel_requests(adapter)) == 0


@pytest.mark.asyncio
async def test_abandon_path_persists_first_terminal_reply_and_bounds_the_second() -> None:
    adapter = _DirectCancelAdapter()
    service, sessions = _service(adapter)
    with sessions() as session:
        strategy, account, intent, order = _seed_target(session, symbol="DAIC")
        for _ in range(2):
            await service._cancel_working_order_and_abandon_intent(
                session=session,
                order=order,
                intent=intent,
                strategy=strategy,
                broker_account=account,
                reason_code="INTENT_MAX_AGE",
                reason_detail="test dead target",
            )
            session.commit()

        assert len(_cancel_requests(adapter)) == 1
        assert service.store.count_terminal_cancel_refusals(session, order_id=order.id) == 1


@pytest.mark.asyncio
async def test_drift_path_persists_first_terminal_reply_and_bounds_the_second() -> None:
    adapter = _DirectCancelAdapter()
    service, sessions = _service(adapter)
    with sessions() as session:
        _seed_target(session, symbol="KUST", order_type="limit")

    await service._run_drift_cancel("KUST", {"ask": 2.70}, 0.01)
    await service._run_drift_cancel("KUST", {"ask": 2.70}, 0.01)

    assert len(_cancel_requests(adapter)) == 1
    with sessions() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "KUST"))
        assert order is not None
        assert service.store.count_terminal_cancel_refusals(session, order_id=order.id) == 1


@pytest.mark.asyncio
async def test_nonterminal_refusal_does_not_consume_direct_path_budget() -> None:
    adapter = _DirectCancelAdapter(cancel_reason="temporary upstream refusal")
    service, sessions = _service(adapter)
    with sessions() as session:
        _strategy, account, intent, order = _seed_target(session, symbol="OPEN")
        report = _working_report(order)
        for _ in range(2):
            await service._refresh_working_order(
                session=session,
                order=order,
                intent=intent,
                strategy_code="schwab_1m_v2",
                broker_account_name=account.name,
                report=report,
            )
            session.commit()

        assert len(_cancel_requests(adapter)) == 2
        assert service.store.count_terminal_cancel_refusals(session, order_id=order.id) == 0


@pytest.mark.asyncio
async def test_new_target_order_id_resets_direct_path_budget() -> None:
    adapter = _DirectCancelAdapter()
    service, sessions = _service(adapter)
    with sessions() as session:
        _strategy, account, first_intent, first = _seed_target(session, symbol="RESET")
        first_report = _working_report(first)
        for _ in range(2):
            await service._refresh_working_order(
                session=session,
                order=first,
                intent=first_intent,
                strategy_code="schwab_1m_v2",
                broker_account_name=account.name,
                report=first_report,
            )
            session.commit()

        second_intent = TradeIntent(
            strategy_id=first.strategy_id,
            broker_account_id=first.broker_account_id,
            symbol="RESET",
            side="buy",
            intent_type="open",
            quantity=Decimal("10"),
            reason="WORKING_ORDER_REFRESH",
            status="submitted",
            payload={},
        )
        session.add(second_intent)
        session.flush()
        second = BrokerOrder(
            intent_id=second_intent.id,
            strategy_id=first.strategy_id,
            broker_account_id=first.broker_account_id,
            client_order_id="RESET-target-2",
            broker_order_id="broker-RESET-target-2",
            symbol="RESET",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=Decimal("10"),
            status="accepted",
            payload={"order_type": "market", "reference_price": "2.55"},
        )
        session.add(second)
        session.commit()

        await service._refresh_working_order(
            session=session,
            order=second,
            intent=second_intent,
            strategy_code="schwab_1m_v2",
            broker_account_name=account.name,
            report=_working_report(second),
        )
        session.commit()

        assert len(_cancel_requests(adapter)) == 2


@pytest.mark.asyncio
async def test_backdated_terminal_reply_never_ages_out_of_the_budget() -> None:
    adapter = _DirectCancelAdapter()
    service, sessions = _service(adapter)
    with sessions() as session:
        _strategy, account, intent, order = _seed_target(session, symbol="OLD")
        session.add(
            BrokerOrderEvent(
                order_id=order.id,
                event_type="rejected",
                event_at=datetime(2020, 1, 1, tzinfo=UTC),
                event_source="unknown",
                payload={"reason": TERMINAL_REASON},
            )
        )
        session.commit()
        await service._refresh_working_order(
            session=session,
            order=order,
            intent=intent,
            strategy_code="schwab_1m_v2",
            broker_account_name=account.name,
            report=_working_report(order),
        )
        assert _cancel_requests(adapter) == []


def test_budget_is_per_row_uuid_even_when_two_rows_share_a_client_order_id() -> None:
    adapter = _DirectCancelAdapter()
    service, sessions = _service(adapter)
    with sessions() as session:
        _strategy, _account, _intent, first = _seed_target(
            session, symbol="SAME", client_order_id="shared-coid"
        )
        second_intent = TradeIntent(
            strategy_id=first.strategy_id,
            broker_account_id=first.broker_account_id,
            symbol="SAME",
            side="buy",
            intent_type="open",
            quantity=Decimal("10"),
            reason="TEST",
            status="submitted",
            payload={},
        )
        session.add(second_intent)
        session.flush()
        second = BrokerOrder(
            intent_id=second_intent.id,
            strategy_id=first.strategy_id,
            broker_account_id=first.broker_account_id,
            client_order_id="second-coid",
            broker_order_id="broker-second",
            symbol="SAME",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=Decimal("10"),
            status="accepted",
            payload={},
        )
        session.add(second)
        session.flush()
        session.add(
            BrokerOrderEvent(
                order_id=first.id,
                event_type="rejected",
                event_source="unknown",
                payload={"reason": TERMINAL_REASON},
            )
        )
        session.commit()
        # Simulate an imported/legacy collision without violating the current schema's UNIQUE
        # constraint. A client-order-id keyed mutant now sees the first row's refusal for both;
        # the production UUID-keyed query still separates them.
        second.client_order_id = first.client_order_id
        with session.no_autoflush:
            assert service._direct_cancel_dead_target_bound_reached(
                session, order=first, path="test"
            )
            assert not service._direct_cancel_dead_target_bound_reached(
                session, order=second, path="test"
            )


@pytest.mark.asyncio
async def test_cancel_verifier_resubmit_is_durable_and_bounded() -> None:
    adapter = _DirectCancelAdapter()
    service, sessions = _service(adapter)
    service.settings = Settings(
        redis_stream_prefix="test",
        oms_adapter="simulated",
        oms_cancel_verify_attempts=1,
        oms_cancel_verify_interval_seconds=0,
        oms_cancel_verify_resubmits=2,
    )
    with sessions() as session:
        _strategy, account, _intent, order = _seed_target(session, symbol="VERIFY")
        order_id = order.id
        request = type(
            "Request",
            (),
            {
                "intent_type": "cancel",
                "client_order_id": order.client_order_id,
                "metadata": {"broker_order_id": order.broker_order_id or ""},
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
            },
        )()

    await service._verify_cancel_landed(
        request=request,
        symbol="VERIFY",
        account_name=account.name,
        client_order_id="VERIFY-target-1",
        broker_order_id="broker-VERIFY-target-1",
        target_order_id=order_id,
    )

    assert len(_cancel_requests(adapter)) == 1
    with sessions() as session:
        assert service.store.count_terminal_cancel_refusals(session, order_id=order_id) == 1


def test_broker_report_cannot_blank_committed_fanout_identity() -> None:
    adapter = _DirectCancelAdapter()
    service, sessions = _service(adapter)
    with sessions() as session:
        _strategy, _account, _intent, order = _seed_target(session, symbol="IDENTITY")
        committed = {
            "fanout_segment_id": "17",
            "fanout_slot_id": "committed-slot",
            "fanout_attempt_id": "committed-attempt",
        }
        order.payload = committed
        report = ExecutionReport(
            event_type="rejected",
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            symbol=order.symbol,
            side=order.side,  # type: ignore[arg-type]
            intent_type="cancel",
            quantity=order.quantity,
            reason=TERMINAL_REASON,
            metadata={"fanout_slot_id": "", "fanout_attempt_id": "wrong"},
            origin="broker",
        )
        service._record_direct_cancel_reports(
            session,
            order=order,
            reports=[report],
            existing_metadata=committed,
            internal="TEST",
        )
        session.commit()
        event = session.scalar(
            select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id)
        )
        assert event is not None
        metadata = event.payload["metadata"]
        assert metadata["fanout_slot_id"] == "committed-slot"
        assert metadata["fanout_attempt_id"] == "committed-attempt"


@pytest.mark.asyncio
async def test_confirmed_cancel_still_reaches_refresh_replacement() -> None:
    adapter = _DirectCancelAdapter(cancel_event_type="cancelled", cancel_reason="USER_CANCEL")
    service, sessions = _service(adapter)
    with sessions() as session:
        _strategy, account, intent, order = _seed_target(session, symbol="REPLACE")
        result = await service._refresh_working_order(
            session=session,
            order=order,
            intent=intent,
            strategy_code="schwab_1m_v2",
            broker_account_name=account.name,
            report=_working_report(order),
        )
        session.commit()

        assert result["orders"] == 1
        assert [request.intent_type for request in adapter.requests] == ["cancel", "open"]
