from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.broker_adapters.alpaca import AlpacaPaperBrokerAdapter
from project_mai_tai.broker_adapters.protocols import BrokerAdapter, ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    OrderEventEvent,
    OrderEventPayload,
    TradeIntentEvent,
    stream_name,
)
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.runtime_registry import strategy_registration_map
from project_mai_tai.runtime_seed import seed_runtime_metadata
from project_mai_tai.services.runtime import _install_signal_handlers
from project_mai_tai.settings import Settings, get_settings

logger = logging.getLogger(__name__)

SERVICE_NAME = "oms-risk"


def utcnow() -> datetime:
    return datetime.now(UTC)


class OmsRiskService:
    def __init__(
        self,
        settings: Settings | None = None,
        redis_client: Redis | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
        broker_adapter: BrokerAdapter | None = None,
        store: OmsStore | None = None,
    ):
        self.settings = settings or get_settings()
        self.redis = redis_client or Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.session_factory = session_factory or build_session_factory(self.settings)
        self.broker_adapter = broker_adapter or self._build_broker_adapter()
        self.store = store or OmsStore()
        self.strategy_registrations = strategy_registration_map(self.settings)
        self.instance_name = socket.gethostname()
        self.logger = logging.getLogger(SERVICE_NAME)
        self._stream_offsets = {
            stream_name(self.settings.redis_stream_prefix, "strategy-intents"): "$",
        }

    async def run(self) -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        heartbeat_interval_secs = max(1, self.settings.service_heartbeat_interval_seconds)
        broker_sync_interval_secs = max(1, self.settings.oms_broker_sync_interval_seconds)
        last_heartbeat = asyncio.get_running_loop().time()
        last_broker_sync = 0.0

        seed_summary = self.seed_runtime_metadata()
        self.logger.info(
            "seeded runtime metadata: %s strategies, %s broker accounts",
            seed_summary["strategies"],
            seed_summary["broker_accounts"],
        )
        await self._publish_heartbeat("starting", {"adapter": self.settings.oms_adapter})
        while not stop_event.is_set():
            loop_now = asyncio.get_running_loop().time()
            read_timeout_secs = min(
                heartbeat_interval_secs,
                max(1, broker_sync_interval_secs - int(loop_now - last_broker_sync)),
            )
            try:
                messages = await self.redis.xread(
                    self._stream_offsets,
                    block=read_timeout_secs * 1000,
                    count=50,
                )
            except Exception:
                self.logger.exception("failed reading strategy intent stream")
                await asyncio.sleep(1)
                continue

            if messages:
                for stream, entries in messages:
                    for message_id, fields in entries:
                        self._stream_offsets[stream] = message_id
                        await self._handle_stream_message(fields)

            now = asyncio.get_running_loop().time()
            if now - last_broker_sync >= broker_sync_interval_secs:
                try:
                    sync_summary = await self.sync_broker_positions()
                except Exception:
                    self.logger.exception("failed syncing broker positions")
                else:
                    self.logger.debug("broker sync complete: %s", sync_summary)
                last_broker_sync = now
            if now - last_heartbeat >= heartbeat_interval_secs:
                heartbeat_details = {"adapter": self.settings.oms_adapter}
                await self._publish_heartbeat("healthy", heartbeat_details)
                last_heartbeat = now

        await self._publish_heartbeat("stopping", {"adapter": self.settings.oms_adapter})
        await self.redis.aclose()

    async def _handle_stream_message(self, fields: dict[str, str]) -> None:
        data = fields.get("data")
        if not data:
            return

        event = TradeIntentEvent.model_validate(json.loads(data))
        await self.process_trade_intent(event)

    async def process_trade_intent(self, event: TradeIntentEvent) -> list[OrderEventEvent]:
        with self.session_factory() as session:
            registration = self.strategy_registrations.get(event.payload.strategy_code)
            strategy = self.store.ensure_strategy(
                session,
                event.payload.strategy_code,
                name=(registration.display_name if registration else event.payload.strategy_code.replace("_", " ").upper()),
                execution_mode=registration.execution_mode if registration else "paper",
                metadata_json=(
                    dict(registration.metadata)
                    if registration
                    else {"account_name": event.payload.broker_account_name}
                ),
            )
            broker_account = self.store.ensure_broker_account(
                session,
                event.payload.broker_account_name,
                provider=self.settings.broker_default_provider,
                environment=self.settings.environment,
            )
            intent = self.store.create_trade_intent(
                session,
                strategy=strategy,
                broker_account=broker_account,
                event=event,
            )

            passed, risk_reason = self._evaluate_risk(event)
            outcome = "pass" if passed else "reject"
            self.store.record_risk_check(
                session,
                intent=intent,
                strategy_id=strategy.id,
                broker_account_id=broker_account.id,
                outcome=outcome,
                reason=risk_reason,
                payload={"metadata": dict(event.payload.metadata)},
            )

            if not passed:
                self.store.mark_intent_status(intent, "rejected")
                order_event = self._build_rejected_event(event, intent.id)
                session.commit()
                await self._publish_order_event(order_event)
                return [order_event]

            client_order_id = self._build_client_order_id(event)
            request = OrderRequest(
                client_order_id=client_order_id,
                broker_account_name=event.payload.broker_account_name,
                strategy_code=event.payload.strategy_code,
                symbol=event.payload.symbol,
                side=event.payload.side,
                intent_type=event.payload.intent_type,
                quantity=event.payload.quantity,
                reason=event.payload.reason,
                metadata=dict(event.payload.metadata),
            )
            reports = await self.broker_adapter.submit_order(request)
            published_events: list[OrderEventEvent] = []

            for report in reports:
                order = self.store.get_or_create_order(
                    session,
                    intent=intent,
                    strategy_id=strategy.id,
                    broker_account_id=broker_account.id,
                    client_order_id=client_order_id,
                    symbol=event.payload.symbol,
                    side=event.payload.side,
                    quantity=event.payload.quantity,
                    metadata=dict(event.payload.metadata),
                    broker_order_id=report.broker_order_id,
                    status=report.event_type,
                )
                payload = {
                    "client_order_id": report.client_order_id,
                    "broker_order_id": report.broker_order_id,
                    "broker_fill_id": report.broker_fill_id,
                    "metadata": dict(report.metadata),
                    "reason": report.reason,
                }
                self.store.append_order_event(session, order=order, report=report, payload=payload)
                fill = self.store.record_fill_if_needed(
                    session,
                    order=order,
                    strategy_id=strategy.id,
                    broker_account_id=broker_account.id,
                    report=report,
                    payload=payload,
                )
                if fill is not None:
                    self.store.apply_fill_to_positions(
                        session,
                        strategy_id=strategy.id,
                        broker_account_id=broker_account.id,
                        symbol=event.payload.symbol,
                        side=event.payload.side,
                        quantity=fill.quantity,
                        price=fill.price,
                        reported_at=fill.filled_at,
                    )

                intent_status = report.event_type
                if report.event_type == "accepted":
                    intent_status = "submitted"
                self.store.mark_intent_status(intent, intent_status)

                published_events.append(
                    self._build_order_event(
                        intent_event=event,
                        intent_db_id=intent.id,
                        order_db_id=order.id,
                        report=report,
                    )
                )

            session.commit()

        for order_event in published_events:
            await self._publish_order_event(order_event)

        await self.sync_broker_positions(account_names=[event.payload.broker_account_name])
        return published_events

    async def sync_broker_positions(self, *, account_names: list[str] | None = None) -> dict[str, int]:
        with self.session_factory() as session:
            if account_names is None:
                broker_accounts = self.store.list_active_broker_accounts(session)
            else:
                broker_accounts = self.store.list_named_broker_accounts(session, account_names)

            synced_accounts = 0
            synced_positions = 0
            for broker_account in broker_accounts:
                snapshots = await self.broker_adapter.list_account_positions(broker_account.name)
                synced_positions += self.store.sync_account_positions(
                    session,
                    broker_account_id=broker_account.id,
                    snapshots=snapshots,
                )
                synced_accounts += 1

            session.commit()

        return {
            "accounts": synced_accounts,
            "positions": synced_positions,
        }

    async def _publish_order_event(self, event: OrderEventEvent) -> None:
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "order-events"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_order_event_stream_maxlen,
            approximate=True,
        )

    async def _publish_heartbeat(self, status: str, details: dict[str, str]) -> None:
        event = HeartbeatEvent(
            source_service=SERVICE_NAME,
            payload=HeartbeatPayload(
                service_name=SERVICE_NAME,
                instance_name=self.instance_name,
                status=status,
                details=details,
            ),
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "heartbeats"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_heartbeat_stream_maxlen,
            approximate=True,
        )

    def _build_broker_adapter(self) -> BrokerAdapter:
        if self.settings.oms_adapter == "simulated":
            return SimulatedBrokerAdapter()
        if self.settings.oms_adapter == "alpaca_paper":
            return AlpacaPaperBrokerAdapter(self.settings)
        raise RuntimeError(f"Unsupported OMS adapter: {self.settings.oms_adapter}")

    def seed_runtime_metadata(self) -> dict[str, int]:
        summary = seed_runtime_metadata(
            self.settings,
            session_factory=self.session_factory,
            store=self.store,
        )
        return {
            "strategies": summary.strategies,
            "broker_accounts": summary.broker_accounts,
        }

    def _evaluate_risk(self, event: TradeIntentEvent) -> tuple[bool, str]:
        if event.payload.quantity <= 0:
            return False, "quantity must be positive"
        if event.payload.intent_type not in {"open", "scale", "close", "cancel"}:
            return False, f"unsupported intent_type={event.payload.intent_type}"
        if event.payload.side not in {"buy", "sell"}:
            return False, f"unsupported side={event.payload.side}"
        return True, "ok"

    def _build_client_order_id(self, event: TradeIntentEvent) -> str:
        intent_id = event.event_id.hex[:12]
        return f"{event.payload.strategy_code}-{event.payload.symbol}-{event.payload.intent_type}-{intent_id}"

    def _build_order_event(
        self,
        *,
        intent_event: TradeIntentEvent,
        intent_db_id: UUID,
        order_db_id: UUID,
        report: ExecutionReport,
    ) -> OrderEventEvent:
        return OrderEventEvent(
            source_service=SERVICE_NAME,
            correlation_id=intent_event.event_id,
            payload=OrderEventPayload(
                intent_event_id=intent_event.event_id,
                intent_db_id=intent_db_id,
                order_db_id=order_db_id,
                strategy_code=intent_event.payload.strategy_code,
                broker_account_name=intent_event.payload.broker_account_name,
                client_order_id=report.client_order_id,
                broker_order_id=report.broker_order_id,
                broker_fill_id=report.broker_fill_id,
                symbol=intent_event.payload.symbol,
                side=intent_event.payload.side,
                intent_type=intent_event.payload.intent_type,
                status=report.event_type,
                quantity=intent_event.payload.quantity,
                filled_quantity=report.filled_quantity,
                fill_price=report.fill_price,
                reason=report.reason or intent_event.payload.reason,
                metadata=dict(report.metadata),
            ),
        )

    def _build_rejected_event(self, intent_event: TradeIntentEvent, intent_db_id: UUID) -> OrderEventEvent:
        client_order_id = self._build_client_order_id(intent_event)
        return OrderEventEvent(
            source_service=SERVICE_NAME,
            correlation_id=intent_event.event_id,
            payload=OrderEventPayload(
                intent_event_id=intent_event.event_id,
                intent_db_id=intent_db_id,
                order_db_id=None,
                strategy_code=intent_event.payload.strategy_code,
                broker_account_name=intent_event.payload.broker_account_name,
                client_order_id=client_order_id,
                broker_order_id=None,
                broker_fill_id=None,
                symbol=intent_event.payload.symbol,
                side=intent_event.payload.side,
                intent_type=intent_event.payload.intent_type,
                status="rejected",
                quantity=intent_event.payload.quantity,
                filled_quantity=Decimal("0"),
                fill_price=None,
                reason="risk_rejected",
                metadata=dict(intent_event.payload.metadata),
            ),
        )
