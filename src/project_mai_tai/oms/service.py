from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.broker_adapters.alpaca import AlpacaPaperBrokerAdapter
from project_mai_tai.broker_adapters.protocols import BrokerAdapter, ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.db.models import TradeIntent
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
SESSION_TZ = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(UTC)


class OmsRiskService:
    NO_POSITION_REASONS = ("cannot be sold short", "insufficient qty", "no broker position available to sell")
    NOT_TRADABLE_REASONS = ("is not tradable",)

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
                    sync_summary = await self.sync_broker_state()
                except Exception:
                    self.logger.exception("failed syncing broker state")
                else:
                    self.logger.debug("broker state sync complete: %s", sync_summary)
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
                provider=self.settings.resolved_broker_provider,
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

            if event.payload.intent_type == "cancel":
                published_events = await self._process_cancel_intent(
                    session=session,
                    strategy_id=strategy.id,
                    broker_account_id=broker_account.id,
                    intent=intent,
                    event=event,
                )
                session.commit()
                for order_event in published_events:
                    await self._publish_order_event(order_event)
                return published_events

            blocked_reason = await self._get_session_symbol_block_reason(
                account_name=event.payload.broker_account_name,
                symbol=event.payload.symbol,
            )
            if blocked_reason and event.payload.intent_type in {"open", "scale"}:
                self.store.mark_intent_status(intent, "rejected")
                order_event = self._build_rejected_event(
                    event,
                    intent.id,
                    reason=blocked_reason,
                )
                session.commit()
                await self._publish_order_event(order_event)
                return [order_event]

            request_quantity = event.payload.quantity
            if event.payload.intent_type in {"close", "scale"} and event.payload.side == "sell":
                duplicate_exit = self.store.find_open_exit_order(
                    session,
                    strategy_id=strategy.id,
                    broker_account_id=broker_account.id,
                    symbol=event.payload.symbol,
                )
                if duplicate_exit is not None:
                    self.store.mark_intent_status(intent, "rejected")
                    order_event = self._build_rejected_event(
                        event,
                        intent.id,
                        reason="duplicate_exit_in_flight",
                    )
                    session.commit()
                    await self._publish_order_event(order_event)
                    return [order_event]

                virtual_position = self.store.get_virtual_position(
                    session,
                    strategy_id=strategy.id,
                    broker_account_id=broker_account.id,
                    symbol=event.payload.symbol,
                )
                strategy_available_quantity = (
                    virtual_position.quantity
                    if virtual_position is not None and virtual_position.quantity > 0
                    else Decimal("0")
                )
                if strategy_available_quantity <= 0:
                    self.store.mark_intent_status(intent, "rejected")
                    order_event = self._build_rejected_event(
                        event,
                        intent.id,
                        reason="no strategy position available to sell",
                    )
                    session.commit()
                    await self._publish_order_event(order_event)
                    return [order_event]

                account_position = self.store.get_account_position(
                    session,
                    broker_account_id=broker_account.id,
                    symbol=event.payload.symbol,
                )
                available_quantity = (
                    account_position.quantity
                    if account_position is not None and account_position.quantity > 0
                    else Decimal("0")
                )
                if available_quantity <= 0:
                    self.store.mark_intent_status(intent, "rejected")
                    order_event = self._build_rejected_event(
                        event,
                        intent.id,
                        reason="no broker position available to sell",
                    )
                    session.commit()
                    await self._publish_order_event(order_event)
                    return [order_event]

                reserved_exit_quantity = self.store.get_open_exit_reserved_quantity(
                    session,
                    broker_account_id=broker_account.id,
                    symbol=event.payload.symbol,
                )
                remaining_account_quantity = max(Decimal("0"), available_quantity - reserved_exit_quantity)
                if remaining_account_quantity <= 0:
                    self.store.mark_intent_status(intent, "rejected")
                    order_event = self._build_rejected_event(
                        event,
                        intent.id,
                        reason="broker quantity already reserved for pending exits",
                    )
                    session.commit()
                    await self._publish_order_event(order_event)
                    return [order_event]

                request_quantity = min(
                    event.payload.quantity,
                    strategy_available_quantity,
                    remaining_account_quantity,
                )
                intent.quantity = request_quantity

            client_order_id = self._build_client_order_id(event)
            request = OrderRequest(
                client_order_id=client_order_id,
                broker_account_name=event.payload.broker_account_name,
                strategy_code=event.payload.strategy_code,
                symbol=event.payload.symbol,
                side=event.payload.side,
                intent_type=event.payload.intent_type,
                quantity=request_quantity,
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
                    quantity=request_quantity,
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
                if report.event_type == "rejected" and self._is_not_tradable_reason(report.reason):
                    await self._set_session_symbol_block(
                        account_name=event.payload.broker_account_name,
                        symbol=event.payload.symbol,
                        reason="broker_symbol_not_tradable_for_session",
                    )

                published_events.append(
                    self._build_order_event(
                        intent_event=event,
                        intent_db_id=intent.id,
                        order_db_id=order.id,
                        report=report,
                        quantity=request_quantity,
                    )
                )

            session.commit()

        for order_event in published_events:
            await self._publish_order_event(order_event)

        await self.sync_broker_state(account_names=[event.payload.broker_account_name])
        return published_events

    async def _process_cancel_intent(
        self,
        *,
        session: Session,
        strategy_id: UUID,
        broker_account_id: UUID,
        intent,
        event: TradeIntentEvent,
    ) -> list[OrderEventEvent]:
        metadata = dict(event.payload.metadata)
        target_order = self.store.find_open_order_for_cancel(
            session,
            strategy_id=strategy_id,
            broker_account_id=broker_account_id,
            symbol=event.payload.symbol,
            metadata=metadata,
        )
        if target_order is None:
            self.store.mark_intent_status(intent, "rejected")
            return [
                self._build_rejected_event(
                    event,
                    intent.id,
                    reason="cancel_target_not_found",
                )
            ]

        metadata.setdefault("target_client_order_id", target_order.client_order_id)
        if target_order.broker_order_id:
            metadata.setdefault("broker_order_id", target_order.broker_order_id)

        request = OrderRequest(
            client_order_id=target_order.client_order_id,
            broker_account_name=event.payload.broker_account_name,
            strategy_code=event.payload.strategy_code,
            symbol=target_order.symbol,
            side=target_order.side,  # type: ignore[arg-type]
            intent_type="cancel",
            quantity=target_order.quantity,
            reason=event.payload.reason,
            metadata=metadata,
        )
        reports = await self.broker_adapter.submit_order(request)
        published_events: list[OrderEventEvent] = []

        for report in reports:
            order = self.store.update_order_from_report(
                target_order,
                report=report,
                metadata=dict(report.metadata),
                preserve_status=report.event_type == "rejected",
            )
            payload = {
                "client_order_id": report.client_order_id,
                "broker_order_id": report.broker_order_id,
                "broker_fill_id": report.broker_fill_id,
                "metadata": dict(report.metadata),
                "reason": report.reason,
            }
            self.store.append_order_event(session, order=order, report=report, payload=payload)
            self.store.mark_intent_status(intent, report.event_type)
            published_events.append(
                self._build_order_event(
                    intent_event=event,
                intent_db_id=intent.id,
                order_db_id=order.id,
                report=report,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                )
            )

        return published_events

    async def sync_broker_state(self, *, account_names: list[str] | None = None) -> dict[str, int]:
        order_summary = await self.sync_broker_orders(account_names=account_names)
        position_summary = await self.sync_broker_positions(account_names=account_names)
        return {
            "accounts": position_summary["accounts"],
            "positions": position_summary["positions"],
            "orders": order_summary["orders"],
            "terminal_orders": order_summary["terminal_orders"],
        }

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

            self.store.clear_virtual_positions_without_account_backing(
                session,
                broker_account_ids=[account.id for account in broker_accounts],
            )

            session.commit()

        return {
            "accounts": synced_accounts,
            "positions": synced_positions,
        }

    async def sync_broker_orders(self, *, account_names: list[str] | None = None) -> dict[str, int]:
        with self.session_factory() as session:
            if account_names is None:
                broker_accounts = self.store.list_active_broker_accounts(session)
            else:
                broker_accounts = self.store.list_named_broker_accounts(session, account_names)

            account_lookup = {account.id: account for account in broker_accounts}
            open_orders = self.store.list_open_orders(
                session,
                broker_account_ids=list(account_lookup.keys()),
            )

            synced_orders = 0
            terminal_orders = 0
            for order in open_orders:
                account = account_lookup.get(order.broker_account_id)
                if account is None or not order.broker_order_id:
                    continue

                intent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
                if intent is None:
                    continue

                request = OrderRequest(
                    client_order_id=order.client_order_id,
                    broker_account_name=account.name,
                    strategy_code="",
                    symbol=order.symbol,
                    side=order.side,  # type: ignore[arg-type]
                    intent_type=intent.intent_type,  # type: ignore[arg-type]
                    quantity=order.quantity,
                    reason=intent.reason,
                    metadata={**{str(k): str(v) for k, v in (order.payload or {}).items()}, "broker_order_id": order.broker_order_id},
                    order_type=order.order_type,
                    time_in_force=order.time_in_force,
                )
                report = await self.broker_adapter.fetch_order_update(request)
                if report is None:
                    continue

                status_changed = report.event_type != order.status
                fill_progressed = report.filled_quantity > 0
                if not status_changed and not fill_progressed:
                    continue

                synced_orders += 1
                previous_status = order.status
                self.store.update_order_from_report(
                    order,
                    report=report,
                    metadata=dict(report.metadata),
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
                    strategy_id=order.strategy_id,
                    broker_account_id=order.broker_account_id,
                    report=report,
                    payload=payload,
                )
                if fill is not None:
                    self.store.apply_fill_to_positions(
                        session,
                        strategy_id=order.strategy_id,
                        broker_account_id=order.broker_account_id,
                        symbol=order.symbol,
                        side=order.side,
                        quantity=fill.quantity,
                        price=fill.price,
                        reported_at=fill.filled_at,
                    )

                intent_status = report.event_type
                if report.event_type == "accepted":
                    intent_status = "submitted"
                self.store.mark_intent_status(intent, intent_status)
                if previous_status in self.store.OPEN_ORDER_STATUSES and report.event_type in {"filled", "cancelled", "rejected"}:
                    terminal_orders += 1

            session.commit()

        return {
            "orders": synced_orders,
            "terminal_orders": terminal_orders,
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
        if self.settings.oms_adapter == "schwab":
            return SchwabBrokerAdapter(self.settings)
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
        if event.payload.intent_type == "cancel":
            if event.payload.quantity < 0:
                return False, "cancel quantity cannot be negative"
        elif event.payload.quantity <= 0:
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
        client_order_id: str | None = None,
        symbol: str | None = None,
        side: str | None = None,
        quantity: Decimal | None = None,
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
                client_order_id=client_order_id if client_order_id is not None else report.client_order_id,
                broker_order_id=report.broker_order_id,
                broker_fill_id=report.broker_fill_id,
                symbol=symbol if symbol is not None else intent_event.payload.symbol,
                side=(side or intent_event.payload.side),  # type: ignore[arg-type]
                intent_type=intent_event.payload.intent_type,
                status=report.event_type,
                quantity=quantity if quantity is not None else intent_event.payload.quantity,
                filled_quantity=report.filled_quantity,
                fill_price=report.fill_price,
                reason=report.reason or intent_event.payload.reason,
                metadata=dict(report.metadata),
            ),
        )

    def _build_rejected_event(
        self,
        intent_event: TradeIntentEvent,
        intent_db_id: UUID,
        *,
        reason: str = "risk_rejected",
    ) -> OrderEventEvent:
        client_order_id = (
            intent_event.payload.metadata.get("target_client_order_id")
            or self._build_client_order_id(intent_event)
        )
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
                reason=reason,
                metadata=dict(intent_event.payload.metadata),
            ),
        )

    def _session_symbol_block_key(self, *, account_name: str, symbol: str, session_date: str | None = None) -> str:
        day = session_date or datetime.now(SESSION_TZ).date().isoformat()
        safe_account = account_name.replace(":", "_")
        return f"{self.settings.redis_stream_prefix}:symbol-block:{day}:{safe_account}:{symbol.upper()}"

    def _seconds_until_session_end(self) -> int:
        now = datetime.now(SESSION_TZ)
        tomorrow = (now + timedelta(days=1)).date()
        next_midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=SESSION_TZ)
        return max(60, int((next_midnight - now).total_seconds()))

    async def _get_session_symbol_block_reason(self, *, account_name: str, symbol: str) -> str | None:
        getter = getattr(self.redis, "get", None)
        if getter is None:
            return None
        value = await getter(self._session_symbol_block_key(account_name=account_name, symbol=symbol))
        return str(value) if value else None

    async def _set_session_symbol_block(self, *, account_name: str, symbol: str, reason: str) -> None:
        setter = getattr(self.redis, "set", None)
        if setter is None:
            return
        await setter(
            self._session_symbol_block_key(account_name=account_name, symbol=symbol),
            reason,
            ex=self._seconds_until_session_end(),
        )

    def _is_not_tradable_reason(self, reason: str | None) -> bool:
        if not reason:
            return False
        lowered = reason.lower()
        return any(fragment in lowered for fragment in self.NOT_TRADABLE_REASONS)
