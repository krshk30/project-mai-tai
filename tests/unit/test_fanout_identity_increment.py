from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, Fill, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.fanout_identity import fanout_slot_for_source, fanout_slot_id
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy

from test_oms_risk_service import (  # noqa: E402 - same-directory fixture module
    FakeRedis,
    FakeWorkingOrderRefreshBrokerAdapter,
    _noop_sync_broker_state,
    build_test_session_factory,
)


SEGMENT = 1_777_000_000_000
RESTING_SLOT_ID = "0a4d2cdf-adbd-597f-9253-1fdc4cab9f5a"
RECLAIM_SLOT_ID = "cd9aeb05-0207-5dba-a112-ad0992bcf6c2"


def _strategy() -> SchwabV2Strategy:
    return SchwabV2Strategy(
        Settings(
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        )
    )


def _metadata(symbol: str = "XPON") -> dict[str, str]:
    assert symbol == "XPON" or symbol in {"EXYN", "PMI"}
    slot_id = {
        "XPON": RESTING_SLOT_ID,
        "EXYN": "af98af05-dec2-5467-8358-c78413560ba0",
        "PMI": "4e818a10-6a09-529a-ab0a-3626caf868e7",
    }[symbol]
    return {
        "fanout_leg": "webull",
        "fanout_source": "rth_resting_mirror",
        "fanout_segment_id": str(SEGMENT),
        "fanout_slot": "resting",
        "fanout_slot_id": slot_id,
        "order_type": "limit",
        "limit_price": "3.21",
        "reference_price": "3.20",
        "price_source": "ask",
    }


def _event(symbol: str = "XPON") -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="schwab-1m-v2",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name="paper:s82-webull",
            symbol=symbol,
            side="buy",
            quantity=Decimal("1"),
            intent_type="open",
            reason="schwab_1m_v2 ATR Flip fan-out webull (rth_resting_mirror)",
            metadata=_metadata(symbol),
        ),
    )


def test_source_to_slot_contract_and_determinism() -> None:
    assert fanout_slot_for_source("rth_resting") == "resting"
    assert fanout_slot_for_source("rth_resting_mirror") == "resting"
    assert fanout_slot_for_source("eh_resting") == "resting"
    assert fanout_slot_for_source("reactive") == "reclaim"
    first = fanout_slot_id(
        strategy_code="schwab_1m_v2", symbol="xpon", segment_id=SEGMENT, slot="resting"
    )
    second = fanout_slot_id(
        strategy_code="SCHWAB_1M_V2", symbol="XPON", segment_id=str(SEGMENT), slot="RESTING"
    )
    reclaim = fanout_slot_id(
        strategy_code="schwab_1m_v2", symbol="XPON", segment_id=SEGMENT, slot="reclaim"
    )
    assert first == RESTING_SLOT_ID
    assert second == RESTING_SLOT_ID
    assert reclaim == RECLAIM_SLOT_ID
    assert first != reclaim
    with pytest.raises(ValueError, match="unknown fan-out source"):
        fanout_slot_for_source("invented")


@pytest.mark.parametrize(
    ("source", "expected_slot", "expected_slot_id"),
    [
        ("rth_resting", "resting", RESTING_SLOT_ID),
        ("rth_resting_mirror", "resting", RESTING_SLOT_ID),
        ("eh_resting", "resting", RESTING_SLOT_ID),
        ("reactive", "reclaim", RECLAIM_SLOT_ID),
    ],
)
def test_every_open_fanout_source_binds_the_expected_slot(
    source: str, expected_slot: str, expected_slot_id: str
) -> None:
    strategy = _strategy()
    strategy._now_ms = lambda: SEGMENT
    state = strategy.watchlist_state("XPON")

    if source == "rth_resting_mirror":
        strategy._webull_resting_mirror_enabled = True
        strategy._resting_session_is_eh = lambda *_args, **_kwargs: False
        strategy._queue_resting_place(state, 3.20, slot="first")
        draft = strategy.drain_webull_direct_intents()[0]
    else:
        draft = strategy._build_webull_fanout_draft(
            state,
            entry_px=3.20,
            session_is_eh=source == "eh_resting",
            source=source,
            entry_n=1,
        )

    assert draft.metadata["fanout_slot"] == expected_slot
    assert draft.metadata["fanout_slot_id"] == expected_slot_id


def test_mirror_cancel_keeps_the_same_segment_and_slot_identity() -> None:
    strategy = _strategy()
    strategy._webull_resting_mirror_enabled = True
    strategy._resting_session_is_eh = lambda *_args, **_kwargs: False
    strategy._now_ms = lambda: SEGMENT
    state = strategy.watchlist_state("XPON")

    strategy._queue_resting_place(state, 3.20, slot="first")
    placed = strategy.drain_webull_direct_intents()[0]
    strategy._queue_resting_cancel(state, reason="control")
    cancelled = strategy.drain_webull_direct_intents()[0]

    assert cancelled.intent_type == "cancel"
    for key in ("fanout_segment_id", "fanout_slot", "fanout_slot_id"):
        assert cancelled.metadata[key] == placed.metadata[key]


class _MetadataDroppingFillAdapter:
    async def submit_order(self, request):
        common = dict(
            client_order_id=request.client_order_id,
            broker_order_id="broker-1",
            symbol=request.symbol,
            side=request.side,
            intent_type=request.intent_type,
            quantity=request.quantity,
            reason=request.reason,
            metadata={},  # the SDK omitted every request field
        )
        return [
            ExecutionReport(event_type="accepted", **common),
            ExecutionReport(
                event_type="filled",
                broker_fill_id="fill-1",
                filled_quantity=request.quantity,
                fill_price=Decimal("3.20"),
                **common,
            ),
        ]

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []

    async def fetch_order_update(self, request):
        del request
        return None


class _MetadataDroppingPollFillAdapter:
    async def submit_order(self, request):
        return [
            ExecutionReport(
                event_type="accepted",
                client_order_id=request.client_order_id,
                broker_order_id="broker-polled",
                symbol=request.symbol,
                side=request.side,
                intent_type=request.intent_type,
                quantity=request.quantity,
                reason=request.reason,
                metadata={},  # submission response omitted the request metadata
            )
        ]

    async def fetch_order_update(self, request):
        return ExecutionReport(
            event_type="filled",
            client_order_id=request.client_order_id,
            broker_order_id="broker-polled",
            broker_fill_id="fill-polled",
            symbol=request.symbol,
            side=request.side,
            intent_type=request.intent_type,
            quantity=request.quantity,
            filled_quantity=request.quantity,
            fill_price=Decimal("3.20"),
            reason=request.reason,
            metadata={},  # later venue detail also omitted the request metadata
        )

    async def list_account_positions(self, broker_account_name: str):
        del broker_account_name
        return []


@pytest.mark.asyncio
async def test_attempt_identity_reaches_intent_order_event_and_fill_when_sdk_drops_metadata() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=_MetadataDroppingFillAdapter(),
    )
    event = _event()

    await service.process_trade_intent(event)

    with session_factory() as session:
        intent = session.scalar(select(TradeIntent))
        order = session.scalar(select(BrokerOrder))
        events = session.scalars(select(BrokerOrderEvent)).all()
        fill = session.scalar(select(Fill))
        assert intent is not None and order is not None and fill is not None
        attempt_id = order.client_order_id
        expected = {
            **_metadata(),
            "fanout_attempt_id": attempt_id,
        }
        assert intent.payload["metadata"]["fanout_attempt_id"] == attempt_id
        for key in (
            "fanout_segment_id", "fanout_slot", "fanout_slot_id", "fanout_attempt_id"
        ):
            assert order.payload[key] == expected[key]
            assert fill.payload["metadata"][key] == expected[key]
            assert all(item.payload["metadata"][key] == expected[key] for item in events)
        assert "fanout_predecessor_attempt_id" not in order.payload

    emitted = [item for stream, item in redis.entries if stream == "test:order-events"]
    assert emitted
    assert all(
        item["payload"]["metadata"]["fanout_attempt_id"] == attempt_id for item in emitted
    )


@pytest.mark.asyncio
async def test_polled_fill_cannot_erase_identity_when_sdk_drops_metadata() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=_MetadataDroppingPollFillAdapter(),
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

    await service.process_trade_intent(_event("PMI"))
    await service.sync_broker_orders(account_names=["paper:s82-webull"])

    with session_factory() as session:
        intent = session.scalar(select(TradeIntent))
        order = session.scalar(select(BrokerOrder))
        fill = session.scalar(select(Fill))
        events = session.scalars(select(BrokerOrderEvent)).all()
        assert intent is not None and order is not None and fill is not None
        attempt_id = order.client_order_id
        for key in (
            "fanout_segment_id",
            "fanout_slot",
            "fanout_slot_id",
            "fanout_attempt_id",
        ):
            expected = intent.payload["metadata"][key]
            assert order.payload[key] == expected
            assert fill.payload["metadata"][key] == expected
            assert all(item.payload["metadata"][key] == expected for item in events)
        assert order.payload["fanout_attempt_id"] == attempt_id


@pytest.mark.asyncio
async def test_cancel_outcome_keeps_target_attempt_identity_instead_of_minting_one() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    service = OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated"),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=FakeWorkingOrderRefreshBrokerAdapter(),
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]

    opened = _event("XPON")
    await service.process_trade_intent(opened)
    with session_factory() as session:
        target = session.scalar(select(BrokerOrder))
        assert target is not None
        target_attempt_id = target.payload["fanout_attempt_id"]

    cancelled = TradeIntentEvent(
        source_service="schwab-1m-v2",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name="paper:s82-webull",
            symbol="XPON",
            side="buy",
            quantity=Decimal("1"),
            intent_type="cancel",
            reason="schwab_1m_v2 resting-entry cancel (webull mirror)",
            metadata={**_metadata(), "resting_entry_cancel": "true"},
        ),
    )
    await service.process_trade_intent(cancelled)

    with session_factory() as session:
        target = session.scalar(select(BrokerOrder))
        events = session.scalars(
            select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == target.id)
        ).all()
        intents = session.scalars(select(TradeIntent).order_by(TradeIntent.created_at)).all()
        assert target.payload["fanout_attempt_id"] == target_attempt_id
        assert events[-1].payload["metadata"]["fanout_attempt_id"] == target_attempt_id
        assert "fanout_attempt_id" not in intents[-1].payload["metadata"]


@pytest.mark.asyncio
async def test_watchdog_replacement_keeps_slot_and_names_exact_prior_attempt() -> None:
    redis = FakeRedis()
    session_factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_working_order_refresh_seconds=5,
        ),
        redis_client=redis,
        session_factory=session_factory,
        broker_adapter=adapter,
    )
    service.sync_broker_state = _noop_sync_broker_state  # type: ignore[method-assign]
    await service.process_trade_intent(_event("EXYN"))

    with session_factory() as session:
        root = session.scalar(select(BrokerOrder))
        assert root is not None
        root.updated_at = datetime.now(UTC) - timedelta(seconds=10)
        root.submitted_at = root.updated_at
        session.commit()

    await service.sync_broker_orders(account_names=["paper:s82-webull"])

    with session_factory() as session:
        orders = session.scalars(select(BrokerOrder).where(BrokerOrder.symbol == "EXYN")).all()
        assert len(orders) == 2
        root = next(item for item in orders if not item.payload.get("fanout_predecessor_attempt_id"))
        replacement = next(item for item in orders if item is not root)
        assert root.payload["fanout_attempt_id"] == root.client_order_id
        assert replacement.payload["fanout_slot_id"] == root.payload["fanout_slot_id"]
        assert replacement.payload["fanout_attempt_id"] == replacement.client_order_id
        assert replacement.payload["fanout_predecessor_attempt_id"] == root.client_order_id
