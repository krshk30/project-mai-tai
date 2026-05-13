from __future__ import annotations

import asyncio
import json
import logging
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.broker_adapters.alpaca import AlpacaPaperBrokerAdapter
from project_mai_tai.broker_adapters.protocols import BrokerAdapter, ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Strategy, TradeIntent
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    OrderEventEvent,
    OrderEventPayload,
    QuoteTickEvent,
    TradeIntentEvent,
    TradeIntentPayload,
    TradeTickEvent,
    stream_name,
)
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.runtime_registry import configured_broker_account_registrations, strategy_registration_map
from project_mai_tai.runtime_seed import seed_runtime_metadata
from project_mai_tai.services.runtime import _install_signal_handlers
from project_mai_tai.settings import Settings, get_settings
from project_mai_tai.strategy_core.time_utils import session_day_eastern_str

logger = logging.getLogger(__name__)

SERVICE_NAME = "oms-risk"
SESSION_TZ = ZoneInfo("America/New_York")
SCHWAB_INELIGIBLE_REASON_SUBSTRINGS = ("must be placed with a broker",)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _format_limit_price(value: float | str | Decimal | None) -> str | None:
    if value is None:
        return None
    try:
        return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")
    except Exception:
        return None


def _panic_limit_price(value: float | str | Decimal | None, buffer_pct: float) -> str | None:
    if value is None:
        return None
    try:
        price = Decimal(str(value))
        if price <= 0:
            return None
        buffered = price * (Decimal("1") - (Decimal(str(buffer_pct)) / Decimal("100")))
        return format(max(buffered, Decimal("0.01")).quantize(Decimal("0.01")), "f")
    except Exception:
        return None


def _extended_hours_session(now: datetime | None = None) -> str | None:
    current = (now or utcnow()).astimezone(SESSION_TZ)
    regular_open = current.replace(hour=9, minute=30, second=0, microsecond=0)
    regular_close = current.replace(hour=16, minute=0, second=0, microsecond=0)
    if regular_open <= current < regular_close:
        return None
    return "AM" if current < regular_open else "PM"


def _is_regular_market_session(now: datetime | None = None) -> bool:
    return _extended_hours_session(now) is None


def _metadata_marks_extended_hours(metadata: dict[str, object]) -> bool:
    session = str(metadata.get("session", "") or "").strip().upper()
    if session in {"AM", "PM"}:
        return True
    return str(metadata.get("extended_hours", "")).strip().lower() == "true"


@dataclass
class ArmedHardStop:
    strategy_code: str
    broker_account_name: str
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    stop_loss_pct: float
    stop_price: Decimal
    quote_max_age_ms: int
    initial_panic_buffer_pct: float
    close_in_flight: bool = False
    last_trigger_attempt_at: datetime | None = None


class OmsRiskService:
    NO_POSITION_REASONS = ("cannot be sold short", "insufficient qty", "no broker position available to sell")
    NOT_TRADABLE_REASONS = ("is not tradable",)
    NATIVE_STOP_GUARD_REASON = "HARD_STOP_NATIVE_BACKUP"

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
            stream_name(self.settings.redis_stream_prefix, "market-data"): "$",
        }
        self._armed_hard_stops: dict[tuple[str, str, str], ArmedHardStop] = {}
        self._latest_quotes_by_symbol: dict[str, dict[str, object]] = {}
        self._latest_trades_by_symbol: dict[str, dict[str, object]] = {}

    async def run(self) -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        heartbeat_interval_secs = max(1, self.settings.service_heartbeat_interval_seconds)
        last_heartbeat = asyncio.get_running_loop().time()
        last_broker_sync = 0.0

        seed_summary = self.seed_runtime_metadata()
        self.logger.info(
            "seeded runtime metadata: %s strategies, %s broker accounts",
            seed_summary["strategies"],
            seed_summary["broker_accounts"],
        )
        await self._publish_heartbeat(
            "starting",
            {
                "adapter": self.settings.oms_adapter_label,
                "providers": ",".join(self.settings.active_broker_providers),
            },
        )
        while not stop_event.is_set():
            loop_now = asyncio.get_running_loop().time()
            broker_sync_interval_secs = await self._broker_sync_interval_seconds()
            read_timeout_secs = min(
                heartbeat_interval_secs,
                max(0.1, broker_sync_interval_secs - (loop_now - last_broker_sync)),
            )
            try:
                messages = await self.redis.xread(
                    self._stream_offsets,
                    block=max(100, int(read_timeout_secs * 1000)),
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
                heartbeat_details = {
                    "adapter": self.settings.oms_adapter_label,
                    "providers": ",".join(self.settings.active_broker_providers),
                }
                await self._publish_heartbeat("healthy", heartbeat_details)
                last_heartbeat = now

        await self._publish_heartbeat(
            "stopping",
            {
                "adapter": self.settings.oms_adapter_label,
                "providers": ",".join(self.settings.active_broker_providers),
            },
        )
        await self.redis.aclose()

    async def _handle_stream_message(self, fields: dict[str, str]) -> None:
        data = fields.get("data")
        if not data:
            return

        payload = json.loads(data)
        event_type = str(payload.get("event_type", "")).strip().lower()
        if event_type == "trade_intent":
            event = TradeIntentEvent.model_validate(payload)
            await self.process_trade_intent(event)
            return
        if not self._armed_hard_stops:
            return
        if event_type == "quote_tick":
            event = QuoteTickEvent.model_validate(payload)
            await self._handle_quote_tick_event(event)
            return
        if event_type == "trade_tick":
            event = TradeTickEvent.model_validate(payload)
            await self._handle_trade_tick_event(event)

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
                provider=self.settings.provider_for_account(event.payload.broker_account_name),
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

            if (
                broker_account.provider == "schwab"
                and event.payload.intent_type == "open"
                and self._has_cached_schwab_ineligible_symbol(
                    session=session,
                    broker_account_id=broker_account.id,
                    symbol=event.payload.symbol,
                )
            ):
                self.store.mark_intent_status(intent, "rejected")
                order_event = self._build_rejected_event(
                    event,
                    intent.id,
                    reason="schwab_ineligible_cached",
                )
                session.commit()
                await self._publish_order_event(order_event)
                return [order_event]

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

            pre_submit_events: list[OrderEventEvent] = []
            request_quantity = event.payload.quantity
            if event.payload.intent_type in {"close", "scale"} and event.payload.side == "sell":
                if not self._is_native_stop_guard_metadata(event.payload.metadata):
                    pre_submit_events.extend(
                        await self._cancel_native_stop_guard_before_sell(
                            session=session,
                            strategy=strategy,
                            broker_account=broker_account,
                            symbol=event.payload.symbol,
                        )
                    )
                duplicate_exit = self.store.find_open_exit_order(
                    session,
                    strategy_id=strategy.id,
                    broker_account_id=broker_account.id,
                    symbol=event.payload.symbol,
                    include_native_stop_guard=False,
                )
                if duplicate_exit is not None:
                    self.store.mark_intent_status(intent, "rejected")
                    order_event = self._build_rejected_event(
                        event,
                        intent.id,
                        reason="duplicate_exit_in_flight",
                    )
                    session.commit()
                    for prior_event in pre_submit_events:
                        await self._publish_order_event(prior_event)
                    await self._publish_order_event(order_event)
                    return [*pre_submit_events, order_event]

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
                    available_quantity = await self._refresh_broker_position_quantity(
                        session=session,
                        broker_account_id=broker_account.id,
                        broker_account_name=broker_account.name,
                        symbol=event.payload.symbol,
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
                    include_native_stop_guard=False,
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
                    for prior_event in pre_submit_events:
                        await self._publish_order_event(prior_event)
                    await self._publish_order_event(order_event)
                    return [*pre_submit_events, order_event]

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
                order_type=str(event.payload.metadata.get("order_type", "market")),
                time_in_force=str(event.payload.metadata.get("time_in_force", "day")),
            )
            reports = await self.broker_adapter.submit_order(request)
            published_events = [*pre_submit_events]
            published_events.extend(await self._record_order_reports(
                session=session,
                intent=intent,
                strategy_id=strategy.id,
                broker_account_id=broker_account.id,
                intent_event=event,
                request=request,
                reports=reports,
            ))
            stop_reject_reason = self._stop_reject_reason(request=request, reports=reports)
            if stop_reject_reason:
                published_events.extend(
                    await self._process_stop_reject_market_fallback(
                        session=session,
                        strategy=strategy,
                        broker_account=broker_account,
                        original_event=event,
                        original_request=request,
                        rejection_reason=stop_reject_reason,
                    )
                )

            if (
                request.side == "sell"
                and request.intent_type in {"close", "scale"}
                and not self._is_native_stop_guard_metadata(request.metadata)
                and str(request.metadata.get("stop_guard", "")).strip().lower() != "true"
                and not any(
                    item.payload.status in {"accepted", "submitted", "partially_filled", "filled"}
                    for item in published_events
                )
            ):
                published_events.extend(
                    await self._rearm_native_stop_from_registry(
                        session=session,
                        strategy_id=strategy.id,
                        broker_account_id=broker_account.id,
                        strategy_code=event.payload.strategy_code,
                        broker_account_name=event.payload.broker_account_name,
                        symbol=event.payload.symbol,
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
            order_type=target_order.order_type,
            time_in_force=target_order.time_in_force,
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

    def _record_internal_risk_pass(
        self,
        session: Session,
        *,
        intent: TradeIntent,
        strategy: Strategy,
        broker_account: BrokerAccount,
        metadata: dict[str, str],
        reason: str,
    ) -> None:
        self.store.record_risk_check(
            session,
            intent=intent,
            strategy_id=strategy.id,
            broker_account_id=broker_account.id,
            outcome="pass",
            reason=reason,
            payload={"metadata": dict(metadata)},
        )

    async def _cancel_native_stop_guard_before_sell(
        self,
        *,
        session: Session,
        strategy: Strategy,
        broker_account: BrokerAccount,
        symbol: str,
    ) -> list[OrderEventEvent]:
        native_order = self.store.find_open_native_stop_guard_order(
            session,
            strategy_id=strategy.id,
            broker_account_id=broker_account.id,
            symbol=symbol,
        )
        if native_order is None:
            return []

        cancel_event = TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=strategy.code,
                broker_account_name=broker_account.name,
                symbol=symbol,
                side="sell",
                quantity=native_order.quantity,
                intent_type="cancel",
                reason="NATIVE_STOP_GUARD_CANCEL",
                metadata={
                    "native_stop_guard_manage": "true",
                    "target_client_order_id": native_order.client_order_id,
                    "broker_order_id": native_order.broker_order_id or "",
                },
            ),
        )
        cancel_intent = self.store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=broker_account,
            event=cancel_event,
        )
        self._record_internal_risk_pass(
            session,
            intent=cancel_intent,
            strategy=strategy,
            broker_account=broker_account,
            metadata=dict(cancel_event.payload.metadata),
            reason="native_stop_guard_internal_cancel",
        )
        return await self._process_cancel_intent(
            session=session,
            strategy_id=strategy.id,
            broker_account_id=broker_account.id,
            intent=cancel_intent,
            event=cancel_event,
        )

    async def _arm_or_rearm_native_stop_guard(
        self,
        *,
        session: Session,
        strategy: Strategy,
        broker_account: BrokerAccount,
        stop: ArmedHardStop,
    ) -> list[OrderEventEvent]:
        if not _is_regular_market_session():
            return []
        if stop.quantity <= 0 or stop.stop_price <= 0:
            return []

        published_events: list[OrderEventEvent] = []
        existing = self.store.find_open_native_stop_guard_order(
            session,
            strategy_id=strategy.id,
            broker_account_id=broker_account.id,
            symbol=stop.symbol,
        )
        if existing is not None:
            published_events.extend(
                await self._cancel_native_stop_guard_before_sell(
                    session=session,
                    strategy=strategy,
                    broker_account=broker_account,
                    symbol=stop.symbol,
                )
            )

        stop_event = TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=strategy.code,
                broker_account_name=broker_account.name,
                symbol=stop.symbol,
                side="sell",
                quantity=stop.quantity,
                intent_type="close",
                reason=self.NATIVE_STOP_GUARD_REASON,
                metadata={
                    "native_stop_guard": "true",
                    "order_type": "STOP",
                    "time_in_force": "day",
                    "stop_price": _format_limit_price(stop.stop_price) or str(stop.stop_price),
                    "stop_loss_pct": str(stop.stop_loss_pct),
                },
            ),
        )
        stop_intent = self.store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=broker_account,
            event=stop_event,
        )
        self._record_internal_risk_pass(
            session,
            intent=stop_intent,
            strategy=strategy,
            broker_account=broker_account,
            metadata=dict(stop_event.payload.metadata),
            reason="native_stop_guard_internal_arm",
        )
        request = OrderRequest(
            client_order_id=self._build_client_order_id(stop_event),
            broker_account_name=broker_account.name,
            strategy_code=strategy.code,
            symbol=stop.symbol,
            side="sell",
            intent_type="close",
            quantity=stop.quantity,
            reason=self.NATIVE_STOP_GUARD_REASON,
            metadata=dict(stop_event.payload.metadata),
            order_type="STOP",
            time_in_force="day",
        )
        reports = await self.broker_adapter.submit_order(request)
        published_events.extend(
            await self._record_order_reports(
                session=session,
                intent=stop_intent,
                strategy_id=strategy.id,
                broker_account_id=broker_account.id,
                intent_event=stop_event,
                request=request,
                reports=reports,
            )
        )
        return published_events

    async def _manage_native_stop_after_fill(
        self,
        *,
        session: Session,
        strategy_id: UUID,
        broker_account_id: UUID,
        strategy_code: str,
        broker_account_name: str,
        symbol: str,
        side: str,
        intent_type: str,
        metadata: dict[str, str],
    ) -> list[OrderEventEvent]:
        if not _is_regular_market_session():
            return []
        if self._is_native_stop_guard_metadata(metadata):
            return []
        if str(metadata.get("stop_guard", "")).strip().lower() == "true":
            return []

        strategy = session.get(Strategy, strategy_id)
        broker_account = session.get(BrokerAccount, broker_account_id)
        if strategy is None or broker_account is None:
            return []

        if str(side).lower() == "buy" and str(intent_type).lower() == "open":
            if str(metadata.get("stop_guard_enabled", "")).lower() != "true":
                return []
        elif str(side).lower() == "sell" and str(intent_type).lower() in {"close", "scale"}:
            pass
        else:
            return []

        stop = self._armed_hard_stops.get(
            self._hard_stop_key(strategy_code, broker_account_name, symbol),
        )
        if stop is None or stop.quantity <= 0:
            return []
        return await self._arm_or_rearm_native_stop_guard(
            session=session,
            strategy=strategy,
            broker_account=broker_account,
            stop=stop,
        )

    async def _rearm_native_stop_from_registry(
        self,
        *,
        session: Session,
        strategy_id: UUID,
        broker_account_id: UUID,
        strategy_code: str,
        broker_account_name: str,
        symbol: str,
    ) -> list[OrderEventEvent]:
        if not _is_regular_market_session():
            return []
        strategy = session.get(Strategy, strategy_id)
        broker_account = session.get(BrokerAccount, broker_account_id)
        if strategy is None or broker_account is None:
            return []
        stop = self._armed_hard_stops.get(
            self._hard_stop_key(strategy_code, broker_account_name, symbol),
        )
        if stop is None or stop.quantity <= 0:
            return []
        return await self._arm_or_rearm_native_stop_guard(
            session=session,
            strategy=strategy,
            broker_account=broker_account,
            stop=stop,
        )

    async def _has_active_native_stop_guard_order(
        self,
        *,
        strategy_code: str,
        broker_account_name: str,
        symbol: str,
    ) -> bool:
        with self.session_factory() as session:
            strategy = session.scalar(select(Strategy).where(Strategy.code == strategy_code))
            broker_account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == broker_account_name))
            if strategy is None or broker_account is None:
                return False
            native_order = self.store.find_open_native_stop_guard_order(
                session,
                strategy_id=strategy.id,
                broker_account_id=broker_account.id,
                symbol=symbol,
            )
            return native_order is not None

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
            strategy_lookup = {
                strategy.id: strategy
                for strategy in session.scalars(select(Strategy)).all()
            }
            open_orders = self.store.list_open_orders(
                session,
                broker_account_ids=list(account_lookup.keys()),
            )

            synced_orders = 0
            terminal_orders = 0
            published_events: list[OrderEventEvent] = []
            for order in open_orders:
                account = account_lookup.get(order.broker_account_id)
                if account is None or not order.broker_order_id:
                    continue

                intent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
                if intent is None:
                    continue
                strategy = strategy_lookup.get(order.strategy_id)

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
                    order_type=str((order.payload or {}).get("order_type", order.order_type)),
                    time_in_force=str((order.payload or {}).get("time_in_force", order.time_in_force)),
                )
                report = await self.broker_adapter.fetch_order_update(request)
                if report is None:
                    continue

                previous_status = order.status
                payload = {
                    "client_order_id": report.client_order_id,
                    "broker_order_id": report.broker_order_id,
                    "broker_fill_id": report.broker_fill_id,
                    "metadata": dict(report.metadata),
                    "reason": report.reason,
                }
                fill = self.store.record_fill_if_needed(
                    session,
                    order=order,
                    strategy_id=order.strategy_id,
                    broker_account_id=order.broker_account_id,
                    report=report,
                    payload=payload,
                )
                status_changed = report.event_type != previous_status
                should_refresh = (
                    report.event_type in self.store.OPEN_ORDER_STATUSES
                    and self._should_refresh_working_order(order)
                )

                if status_changed or fill is not None:
                    synced_orders += 1
                    self.store.update_order_from_report(
                        order,
                        report=report,
                        metadata=dict(report.metadata),
                    )
                    self.store.append_order_event(session, order=order, report=report, payload=payload)
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
                        self._update_hard_stop_registry_from_fill(
                            strategy_code=strategy.code if strategy is not None else "",
                            broker_account_name=account.name,
                            symbol=order.symbol,
                            side=order.side,
                            intent_type=intent.intent_type,
                            quantity=fill.quantity,
                            price=fill.price,
                            metadata={str(k): str(v) for k, v in (order.payload or {}).items()},
                        )
                        published_events.extend(
                            await self._manage_native_stop_after_fill(
                                session=session,
                                strategy_id=order.strategy_id,
                                broker_account_id=order.broker_account_id,
                                strategy_code=strategy.code if strategy is not None else "",
                                broker_account_name=account.name,
                                symbol=order.symbol,
                                side=order.side,
                                intent_type=intent.intent_type,
                                metadata={str(k): str(v) for k, v in (order.payload or {}).items()},
                            )
                        )

                    intent_status = report.event_type
                    if report.event_type == "accepted":
                        intent_status = "submitted"
                    self.store.mark_intent_status(intent, intent_status)
                    self._update_hard_stop_registry_from_order_status(
                        strategy_code=strategy.code if strategy is not None else "",
                        broker_account_name=account.name,
                        symbol=order.symbol,
                        metadata={str(k): str(v) for k, v in (order.payload or {}).items()},
                        status=report.event_type,
                        reason=report.reason,
                    )
                    if previous_status in self.store.OPEN_ORDER_STATUSES and report.event_type in {"filled", "cancelled", "rejected"}:
                        terminal_orders += 1
                    if (
                        report.event_type in {"cancelled", "rejected"}
                        and not self._is_native_stop_guard_metadata(order.payload or {})
                        and str((order.payload or {}).get("stop_guard", "")).strip().lower() != "true"
                        and str(order.side).lower() == "sell"
                        and str(intent.intent_type).lower() in {"close", "scale"}
                    ):
                        published_events.extend(
                            await self._rearm_native_stop_from_registry(
                                session=session,
                                strategy_id=order.strategy_id,
                                broker_account_id=order.broker_account_id,
                                strategy_code=strategy.code if strategy is not None else "",
                                broker_account_name=account.name,
                                symbol=order.symbol,
                            )
                        )
                    published_events.append(
                        self._build_order_event(
                            intent_event=TradeIntentEvent(
                                source_service=SERVICE_NAME,
                                payload=TradeIntentPayload(
                                    strategy_code=strategy.code if strategy is not None else "",
                                    broker_account_name=account.name,
                                    symbol=order.symbol,
                                    side=order.side,  # type: ignore[arg-type]
                                    quantity=order.quantity,
                                    intent_type=intent.intent_type,  # type: ignore[arg-type]
                                    reason=intent.reason,
                                    metadata={**{str(k): str(v) for k, v in (order.payload or {}).items()}},
                                ),
                            ),
                            intent_db_id=intent.id,
                            order_db_id=order.id,
                            report=report,
                            client_order_id=order.client_order_id,
                            symbol=order.symbol,
                            side=order.side,
                            quantity=order.quantity,
                        )
                    )

                if should_refresh:
                    refresh_result = await self._refresh_working_order(
                        session=session,
                        order=order,
                        intent=intent,
                        strategy_code=strategy.code if strategy is not None else "",
                        broker_account_name=account.name,
                        report=report,
                    )
                    synced_orders += refresh_result["orders"]
                    terminal_orders += refresh_result["terminal_orders"]
                    published_events.extend(refresh_result["published_events"])
                elif not status_changed and fill is None:
                    continue

            session.commit()

        for order_event in published_events:
            await self._publish_order_event(order_event)

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

    async def _broker_sync_interval_seconds(self) -> float:
        default_interval = max(1.0, float(self.settings.oms_broker_sync_interval_seconds))
        if await self._has_active_stop_guard_orders():
            return min(
                default_interval,
                max(0.1, float(self.settings.oms_stop_guard_refresh_stage_1_seconds)),
            )
        return default_interval

    async def _has_active_stop_guard_orders(self) -> bool:
        with self.session_factory() as session:
            broker_accounts = self.store.list_active_broker_accounts(session)
            open_orders = self.store.list_open_orders(
                session,
                broker_account_ids=[account.id for account in broker_accounts],
            )
            return any(self._is_stop_guard_order(order) for order in open_orders)

    @staticmethod
    def _hard_stop_key(strategy_code: str, broker_account_name: str, symbol: str) -> tuple[str, str, str]:
        return (str(strategy_code), str(broker_account_name), str(symbol).upper())

    @staticmethod
    def _is_native_stop_guard_metadata(metadata: dict[str, object] | None) -> bool:
        payload = metadata or {}
        return str(payload.get("native_stop_guard", "")).strip().lower() == "true"

    def _is_native_stop_guard_order(self, order: BrokerOrder) -> bool:
        return self._is_native_stop_guard_metadata(order.payload or {})

    async def _handle_quote_tick_event(self, event: QuoteTickEvent) -> None:
        symbol = str(event.payload.symbol).upper()
        self._latest_quotes_by_symbol[symbol] = {
            "bid": float(event.payload.bid_price),
            "ask": float(event.payload.ask_price),
            "received_at": utcnow(),
        }
        await self._evaluate_hard_stop_market_event(symbol)

    async def _handle_trade_tick_event(self, event: TradeTickEvent) -> None:
        symbol = str(event.payload.symbol).upper()
        self._latest_trades_by_symbol[symbol] = {
            "price": float(event.payload.price),
            "received_at": utcnow(),
        }
        await self._evaluate_hard_stop_market_event(symbol)

    async def _evaluate_hard_stop_market_event(self, symbol: str) -> None:
        normalized_symbol = str(symbol).upper()
        matching_stops = [
            stop
            for stop in self._armed_hard_stops.values()
            if stop.symbol == normalized_symbol and stop.quantity > 0
        ]
        if not matching_stops:
            return
        for stop in matching_stops:
            if stop.close_in_flight:
                continue
            if self._is_hard_stop_trigger_throttled(stop):
                continue
            trigger_price, trigger_source = self._resolve_hard_stop_trigger_price(stop)
            if trigger_price is None or trigger_source is None:
                continue
            if Decimal(str(trigger_price)) > stop.stop_price:
                continue
            await self._trigger_hard_stop(stop, trigger_price=Decimal(str(trigger_price)), trigger_source=trigger_source)

    def _resolve_hard_stop_trigger_price(self, stop: ArmedHardStop) -> tuple[float | None, str | None]:
        max_age_ms = max(0, stop.quote_max_age_ms)
        fresh_bid: float | None = None
        quote = self._latest_quotes_by_symbol.get(stop.symbol)
        if quote is not None:
            received_at = quote.get("received_at")
            bid = quote.get("bid")
            if isinstance(received_at, datetime) and bid is not None:
                age_ms = (utcnow() - received_at).total_seconds() * 1000
                if age_ms <= max_age_ms:
                    fresh_bid = float(bid)
        fresh_last: float | None = None
        trade = self._latest_trades_by_symbol.get(stop.symbol)
        if trade is not None and trade.get("price") is not None:
            received_at = trade.get("received_at")
            if isinstance(received_at, datetime):
                age_ms = (utcnow() - received_at).total_seconds() * 1000
                if age_ms <= max_age_ms:
                    fresh_last = float(trade["price"])
        if fresh_bid is not None and Decimal(str(fresh_bid)) <= stop.stop_price:
            return fresh_bid, "bid"
        if fresh_last is not None and Decimal(str(fresh_last)) <= stop.stop_price:
            return fresh_last, "last"
        if fresh_bid is not None:
            return fresh_bid, "bid"
        if fresh_last is not None:
            return fresh_last, "last"
        return None, None

    def _is_hard_stop_trigger_throttled(self, stop: ArmedHardStop) -> bool:
        if stop.last_trigger_attempt_at is None:
            return False
        return (utcnow() - stop.last_trigger_attempt_at).total_seconds() < 0.25

    async def _trigger_hard_stop(
        self,
        stop: ArmedHardStop,
        *,
        trigger_price: Decimal,
        trigger_source: str,
    ) -> None:
        if _is_regular_market_session() and await self._has_active_native_stop_guard_order(
            strategy_code=stop.strategy_code,
            broker_account_name=stop.broker_account_name,
            symbol=stop.symbol,
        ):
            stop.last_trigger_attempt_at = utcnow()
            return
        stop.last_trigger_attempt_at = utcnow()
        stop.close_in_flight = True
        event = TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=stop.strategy_code,
                broker_account_name=stop.broker_account_name,
                symbol=stop.symbol,
                side="sell",
                quantity=stop.quantity,
                intent_type="close",
                reason="HARD_STOP",
                metadata=self._build_hard_stop_metadata(
                    stop=stop,
                    trigger_price=trigger_price,
                    trigger_source=trigger_source,
                ),
            ),
        )
        order_events = await self.process_trade_intent(event)
        if any(item.payload.status in {"accepted", "submitted", "partially_filled", "filled"} for item in order_events):
            if any(item.payload.status == "filled" for item in order_events):
                self._armed_hard_stops.pop(
                    self._hard_stop_key(stop.strategy_code, stop.broker_account_name, stop.symbol),
                    None,
                )
            return
        stop.close_in_flight = False
        if any(item.payload.reason in self.NO_POSITION_REASONS for item in order_events):
            self._armed_hard_stops.pop(
                self._hard_stop_key(stop.strategy_code, stop.broker_account_name, stop.symbol),
                None,
            )

    def _build_hard_stop_metadata(
        self,
        *,
        stop: ArmedHardStop,
        trigger_price: Decimal,
        trigger_source: str,
    ) -> dict[str, str]:
        metadata = {
            "stop_guard": "true",
            "stop_loss_pct": str(stop.stop_loss_pct),
            "stop_price": _format_limit_price(stop.stop_price) or str(stop.stop_price),
            "stop_trigger_price": _format_limit_price(trigger_price) or str(trigger_price),
            "stop_trigger_source": str(trigger_source),
            "panic_buffer_pct": str(stop.initial_panic_buffer_pct),
            "reference_price": _format_limit_price(trigger_price) or str(trigger_price),
        }
        routed_price = _panic_limit_price(trigger_price, stop.initial_panic_buffer_pct)
        if routed_price is None:
            return metadata
        metadata.update(
            {
                "order_type": "limit",
                "time_in_force": "day",
                "limit_price": routed_price,
                "reference_price": routed_price,
                "price_source": "bid" if trigger_source == "bid" else "last",
            }
        )
        session = _extended_hours_session()
        if session is not None:
            metadata.update(
                {
                    "session": session,
                    "extended_hours": "true",
                }
            )
        return metadata

    def _update_hard_stop_registry_from_fill(
        self,
        *,
        strategy_code: str,
        broker_account_name: str,
        symbol: str,
        side: str,
        intent_type: str,
        quantity: Decimal,
        price: Decimal,
        metadata: dict[str, object],
    ) -> None:
        normalized_symbol = str(symbol).upper()
        key = self._hard_stop_key(strategy_code, broker_account_name, normalized_symbol)
        if str(side).lower() == "buy" and str(intent_type).lower() == "open":
            if str(metadata.get("stop_guard_enabled", "")).lower() != "true":
                return
            try:
                stop_loss_pct = float(metadata.get("stop_loss_pct", 0) or 0)
            except (TypeError, ValueError):
                return
            if stop_loss_pct <= 0 or quantity <= 0 or price <= 0:
                return
            try:
                quote_max_age_ms = int(metadata.get("stop_guard_quote_max_age_ms", 2000) or 2000)
            except (TypeError, ValueError):
                quote_max_age_ms = 2000
            try:
                initial_panic_buffer_pct = float(metadata.get("stop_guard_initial_panic_buffer_pct", 1.5) or 1.5)
            except (TypeError, ValueError):
                initial_panic_buffer_pct = 1.5
            if _metadata_marks_extended_hours(metadata):
                quote_max_age_ms = min(
                    max(0, quote_max_age_ms),
                    max(0, int(self.settings.oms_after_hours_stop_guard_quote_max_age_ms)),
                )
                initial_panic_buffer_pct = max(
                    float(initial_panic_buffer_pct),
                    float(self.settings.oms_after_hours_stop_guard_initial_panic_buffer_pct),
                )
            existing = self._armed_hard_stops.get(key)
            if existing is None:
                entry_price = price
                total_quantity = quantity
            else:
                total_quantity = existing.quantity + quantity
                weighted_cost = existing.entry_price * existing.quantity + price * quantity
                entry_price = weighted_cost / total_quantity if total_quantity > 0 else price
            stop_price = entry_price * (Decimal("1") - (Decimal(str(stop_loss_pct)) / Decimal("100")))
            self._armed_hard_stops[key] = ArmedHardStop(
                strategy_code=strategy_code,
                broker_account_name=broker_account_name,
                symbol=normalized_symbol,
                quantity=total_quantity,
                entry_price=entry_price,
                stop_loss_pct=stop_loss_pct,
                stop_price=stop_price,
                quote_max_age_ms=max(0, quote_max_age_ms),
                initial_panic_buffer_pct=initial_panic_buffer_pct,
                close_in_flight=False,
                last_trigger_attempt_at=None,
            )
            return

        existing = self._armed_hard_stops.get(key)
        if existing is None or quantity <= 0:
            return
        if str(side).lower() == "sell":
            remaining_quantity = max(Decimal("0"), existing.quantity - quantity)
            if remaining_quantity <= 0:
                self._armed_hard_stops.pop(key, None)
                return
            existing.quantity = remaining_quantity

    def _update_hard_stop_registry_from_order_status(
        self,
        *,
        strategy_code: str,
        broker_account_name: str,
        symbol: str,
        metadata: dict[str, object],
        status: str,
        reason: str,
    ) -> None:
        key = self._hard_stop_key(strategy_code, broker_account_name, symbol)
        stop = self._armed_hard_stops.get(key)
        if stop is None:
            return
        if str(metadata.get("stop_guard", "")).lower() != "true":
            return
        normalized_status = str(status).lower()
        normalized_reason = str(reason).lower()
        if normalized_status in {"accepted", "submitted", "partially_filled"}:
            stop.close_in_flight = True
            return
        if normalized_status == "filled":
            self._armed_hard_stops.pop(key, None)
            return
        if normalized_status in {"cancelled", "rejected"}:
            if any(token in normalized_reason for token in self.NO_POSITION_REASONS):
                self._armed_hard_stops.pop(key, None)
                return
            stop.close_in_flight = "duplicate_exit_in_flight" in normalized_reason or (
                "broker quantity already reserved for pending exits" in normalized_reason
            )

    def _build_broker_adapter(self) -> BrokerAdapter:
        registrations = configured_broker_account_registrations(self.settings)
        provider_by_account = {registration.name: registration.provider for registration in registrations}
        unique_providers = {provider for provider in provider_by_account.values() if provider}
        if not unique_providers:
            unique_providers = {self.settings.resolved_broker_provider}

        if len(unique_providers) == 1:
            return self._build_provider_adapter(next(iter(unique_providers)))

        return RoutingBrokerAdapter(
            default_provider=self.settings.resolved_broker_provider,
            provider_by_account=provider_by_account,
            factories_by_provider={
                provider: (lambda provider=provider: self._build_provider_adapter(provider))
                for provider in unique_providers | {self.settings.resolved_broker_provider}
            },
        )

    def _build_provider_adapter(self, provider: str) -> BrokerAdapter:
        normalized = str(provider).strip().lower()
        if self.settings.oms_adapter == "simulated":
            return SimulatedBrokerAdapter()
        if normalized == "simulated":
            return SimulatedBrokerAdapter()
        if normalized == "alpaca":
            return AlpacaPaperBrokerAdapter(self.settings)
        if normalized == "schwab":
            return SchwabBrokerAdapter(self.settings)
        if normalized == "webull":
            return WebullBrokerAdapter(self.settings)
        raise RuntimeError(f"Unsupported broker provider: {provider}")

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
        symbol = str(event.payload.symbol).strip().upper()
        if symbol and symbol in self.settings.protected_symbol_set:
            return False, f"protected_symbol:{symbol}"
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

    @staticmethod
    def _current_session_day(now: datetime | None = None) -> str:
        return session_day_eastern_str(now or utcnow())

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

    async def _refresh_broker_position_quantity(
        self,
        *,
        session: Session,
        broker_account_id: UUID,
        broker_account_name: str,
        symbol: str,
    ) -> Decimal:
        try:
            snapshots = await self.broker_adapter.list_account_positions(broker_account_name)
        except Exception as exc:
            self.logger.warning(
                "failed broker position refresh before exit recheck for %s %s: %s",
                broker_account_name,
                symbol,
                exc,
            )
            return Decimal("0")

        self.store.sync_account_positions(
            session,
            broker_account_id=broker_account_id,
            snapshots=snapshots,
        )
        refreshed_position = self.store.get_account_position(
            session,
            broker_account_id=broker_account_id,
            symbol=symbol,
        )
        if refreshed_position is None or refreshed_position.quantity <= 0:
            return Decimal("0")
        return refreshed_position.quantity

    async def _record_order_reports(
        self,
        *,
        session: Session,
        intent,
        strategy_id: UUID,
        broker_account_id: UUID,
        intent_event: TradeIntentEvent,
        request: OrderRequest,
        reports: list[ExecutionReport],
    ) -> list[OrderEventEvent]:
        published_events: list[OrderEventEvent] = []
        for report in reports:
            order = self.store.get_or_create_order(
                session,
                intent=intent,
                strategy_id=strategy_id,
                broker_account_id=broker_account_id,
                client_order_id=request.client_order_id,
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                metadata=dict(request.metadata),
                broker_order_id=report.broker_order_id,
                status=report.event_type,
                order_type=request.order_type,
                time_in_force=request.time_in_force,
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
                strategy_id=strategy_id,
                broker_account_id=broker_account_id,
                report=report,
                payload=payload,
            )
            if fill is not None:
                self.store.apply_fill_to_positions(
                    session,
                    strategy_id=strategy_id,
                    broker_account_id=broker_account_id,
                    symbol=request.symbol,
                    side=request.side,
                    quantity=fill.quantity,
                    price=fill.price,
                    reported_at=fill.filled_at,
                )
                self._update_hard_stop_registry_from_fill(
                    strategy_code=intent_event.payload.strategy_code,
                    broker_account_name=intent_event.payload.broker_account_name,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=fill.quantity,
                    price=fill.price,
                    metadata=dict(request.metadata),
                )
                published_events.extend(
                    await self._manage_native_stop_after_fill(
                        session=session,
                        strategy_id=strategy_id,
                        broker_account_id=broker_account_id,
                        strategy_code=intent_event.payload.strategy_code,
                        broker_account_name=intent_event.payload.broker_account_name,
                        symbol=request.symbol,
                        side=request.side,
                        intent_type=request.intent_type,
                        metadata=dict(request.metadata),
                    )
                )

            intent_status = report.event_type
            if report.event_type == "accepted":
                intent_status = "submitted"
            self.store.mark_intent_status(intent, intent_status)
            self._update_hard_stop_registry_from_order_status(
                strategy_code=intent_event.payload.strategy_code,
                broker_account_name=intent_event.payload.broker_account_name,
                symbol=request.symbol,
                metadata=dict(request.metadata),
                status=report.event_type,
                reason=report.reason,
            )
            if report.event_type == "rejected" and self._is_schwab_ineligible_reason(report.reason):
                self.store.record_schwab_ineligible_entry(
                    session,
                    broker_account_id=broker_account_id,
                    symbol=request.symbol,
                    session_date=self._current_session_day(report.reported_at),
                    reason_text=report.reason or "",
                    first_seen_at=report.reported_at,
                )
            if report.event_type == "rejected" and self._is_not_tradable_reason(report.reason):
                await self._set_session_symbol_block(
                    account_name=intent_event.payload.broker_account_name,
                    symbol=intent_event.payload.symbol,
                    reason="broker_symbol_not_tradable_for_session",
                )

            published_events.append(
                self._build_order_event(
                    intent_event=intent_event,
                    intent_db_id=intent.id,
                    order_db_id=order.id,
                    report=report,
                    client_order_id=request.client_order_id,
                    symbol=request.symbol,
                    side=request.side,
                    quantity=request.quantity,
                )
            )
        return published_events

    def _should_refresh_working_order(self, order: BrokerOrder) -> bool:
        refresh_after = self._refresh_after_seconds(order)
        last_activity = order.updated_at or order.submitted_at
        if last_activity is None:
            return True
        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=UTC)
        return (utcnow() - last_activity).total_seconds() >= refresh_after

    def _refresh_after_seconds(self, order: BrokerOrder) -> float:
        if self._is_stop_guard_order(order):
            stage = self._stop_guard_refresh_stage(order.payload or {})
            if stage <= 0:
                return max(0.1, float(self.settings.oms_stop_guard_refresh_stage_1_seconds))
            if stage == 1:
                return max(0.1, float(self.settings.oms_stop_guard_refresh_stage_2_seconds))
            return max(0.1, float(self.settings.oms_stop_guard_refresh_stage_3_seconds))
        return max(1.0, float(self.settings.oms_working_order_refresh_seconds))

    @staticmethod
    def _is_stop_guard_order(order: BrokerOrder) -> bool:
        payload = order.payload or {}
        return str(payload.get("stop_guard", "")).strip().lower() == "true"

    @staticmethod
    def _stop_guard_refresh_stage(metadata: dict[str, object]) -> int:
        try:
            return max(0, int(str(metadata.get("stop_guard_refresh_stage", "0"))))
        except (TypeError, ValueError):
            return 0

    def _next_stop_guard_refresh_stage(self, metadata: dict[str, object]) -> int:
        return min(2, self._stop_guard_refresh_stage(metadata) + 1)

    def _stop_guard_buffer_pct_for_stage(self, stage: int, metadata: dict[str, object]) -> float:
        if str(metadata.get("catastrophic_stop_guard", "")).strip().lower() == "true":
            return float(self.settings.oms_after_hours_stop_guard_catastrophic_panic_buffer_pct)
        if stage <= 0:
            try:
                return float(metadata.get("panic_buffer_pct", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        if stage == 1:
            return float(self.settings.oms_stop_guard_refresh_stage_1_buffer_pct)
        return float(self.settings.oms_stop_guard_refresh_stage_2_buffer_pct)

    def _stop_guard_catastrophic_refresh_metadata(
        self,
        *,
        metadata: dict[str, str],
        quote: dict[str, float | None],
    ) -> dict[str, str] | None:
        if not _metadata_marks_extended_hours(metadata):
            return None
        try:
            stop_price = Decimal(str(metadata.get("stop_price", "")).strip())
        except Exception:
            return None
        if stop_price <= 0:
            return None
        bid_price = quote.get("bid_price")
        last_price = quote.get("last_price")
        current_price = bid_price if bid_price is not None and bid_price > 0 else last_price
        if current_price is None or current_price <= 0:
            return None
        try:
            catastrophic_gap_pct = float(self.settings.oms_after_hours_stop_guard_catastrophic_gap_pct)
        except (TypeError, ValueError):
            catastrophic_gap_pct = 0.0
        if catastrophic_gap_pct <= 0:
            return None
        catastrophic_trigger = stop_price * (
            Decimal("1") - (Decimal(str(catastrophic_gap_pct)) / Decimal("100"))
        )
        if Decimal(str(current_price)) > catastrophic_trigger:
            return None
        panic_buffer_pct = float(self.settings.oms_after_hours_stop_guard_catastrophic_panic_buffer_pct)
        refreshed_price = _panic_limit_price(current_price, panic_buffer_pct)
        if refreshed_price is None:
            return None
        metadata["limit_price"] = refreshed_price
        metadata["reference_price"] = refreshed_price
        metadata["price_source"] = "bid" if bid_price is not None and bid_price > 0 else "last"
        metadata["panic_buffer_pct"] = str(panic_buffer_pct)
        metadata["catastrophic_stop_guard"] = "true"
        metadata["stop_guard_refresh_stage"] = "2"
        metadata["watchdog_refresh_reason"] = "catastrophic_gap"
        return metadata

    async def _refresh_working_order(
        self,
        *,
        session: Session,
        order: BrokerOrder,
        intent: TradeIntent,
        strategy_code: str,
        broker_account_name: str,
        report: ExecutionReport,
    ) -> dict[str, object]:
        remaining_quantity = max(Decimal("0"), order.quantity - report.filled_quantity)
        if remaining_quantity <= 0:
            return {"orders": 0, "terminal_orders": 0, "published_events": []}

        refreshed_metadata = await self._build_refreshed_order_metadata(
            broker_account_name=broker_account_name,
            order=order,
        )
        if refreshed_metadata is None:
            return {"orders": 0, "terminal_orders": 0, "published_events": []}

        existing_metadata = {str(k): str(v) for k, v in (order.payload or {}).items()}
        cancel_request = OrderRequest(
            client_order_id=order.client_order_id,
            broker_account_name=broker_account_name,
            strategy_code=strategy_code,
            symbol=order.symbol,
            side=order.side,  # type: ignore[arg-type]
            intent_type="cancel",
            quantity=remaining_quantity,
            reason="WORKING_ORDER_REFRESH",
            metadata={
                **existing_metadata,
                "broker_order_id": order.broker_order_id or "",
                "target_client_order_id": order.client_order_id,
                "watchdog_refresh": "true",
            },
            order_type=order.order_type,
            time_in_force=order.time_in_force,
        )
        cancel_reports = await self.broker_adapter.submit_order(cancel_request)
        cancelled_report = next((item for item in cancel_reports if item.event_type == "cancelled"), None)
        if cancelled_report is None:
            return {"orders": 0, "terminal_orders": 0, "published_events": []}

        cancel_metadata = {
            **existing_metadata,
            **{str(k): str(v) for k, v in cancelled_report.metadata.items()},
            "watchdog_refresh": "true",
        }
        self.store.update_order_from_report(
            order,
            report=cancelled_report,
            metadata=cancel_metadata,
        )
        self.store.append_order_event(
            session,
            order=order,
            report=cancelled_report,
            payload={
                "client_order_id": cancelled_report.client_order_id,
                "broker_order_id": cancelled_report.broker_order_id,
                "broker_fill_id": cancelled_report.broker_fill_id,
                "metadata": dict(cancelled_report.metadata),
                "reason": cancelled_report.reason,
                "internal": "watchdog_refresh",
            },
        )

        replacement_request = OrderRequest(
            client_order_id=self._replacement_client_order_id(order.client_order_id),
            broker_account_name=broker_account_name,
            strategy_code=strategy_code,
            symbol=order.symbol,
            side=order.side,  # type: ignore[arg-type]
            intent_type=intent.intent_type,  # type: ignore[arg-type]
            quantity=remaining_quantity,
            reason=intent.reason,
            metadata=refreshed_metadata,
            order_type=str(refreshed_metadata.get("order_type", order.order_type)),
            time_in_force=str(refreshed_metadata.get("time_in_force", order.time_in_force)),
        )
        replacement_reports = await self.broker_adapter.submit_order(replacement_request)
        replacement_event = TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=strategy_code,
                broker_account_name=broker_account_name,
                symbol=order.symbol,
                side=order.side,  # type: ignore[arg-type]
                quantity=remaining_quantity,
                intent_type=intent.intent_type,  # type: ignore[arg-type]
                reason=intent.reason,
                metadata=dict(refreshed_metadata),
            ),
        )
        published_events = await self._record_order_reports(
            session=session,
            intent=intent,
            strategy_id=order.strategy_id,
            broker_account_id=order.broker_account_id,
            intent_event=replacement_event,
            request=replacement_request,
            reports=replacement_reports,
        )
        return {
            "orders": len(replacement_reports),
            "terminal_orders": 1,
            "published_events": published_events,
        }

    async def _build_refreshed_order_metadata(
        self,
        *,
        broker_account_name: str,
        order: BrokerOrder,
    ) -> dict[str, str] | None:
        metadata = {str(k): str(v) for k, v in (order.payload or {}).items()}
        metadata["watchdog_refresh"] = "true"
        metadata["watchdog_replaces_client_order_id"] = order.client_order_id
        metadata["watchdog_replaced_at"] = utcnow().isoformat()

        order_type = str(metadata.get("order_type", order.order_type or "market")).lower()
        if order_type != "limit":
            return metadata

        quote = await self._fetch_quote_for_order(
            broker_account_name=broker_account_name,
            symbol=order.symbol,
        )
        if not quote:
            return None
        if self._is_stop_guard_order(order):
            catastrophic_metadata = self._stop_guard_catastrophic_refresh_metadata(
                metadata=metadata,
                quote=quote,
            )
            if catastrophic_metadata is not None:
                return catastrophic_metadata
            next_stage = self._next_stop_guard_refresh_stage(metadata)
            panic_buffer_pct = self._stop_guard_buffer_pct_for_stage(next_stage, metadata)
            bid_price = quote.get("bid_price")
            last_price = quote.get("last_price")
            refreshed_price = _panic_limit_price(
                bid_price if bid_price is not None and bid_price > 0 else last_price,
                panic_buffer_pct,
            )
            if refreshed_price is None:
                return None
            metadata["limit_price"] = refreshed_price
            metadata["reference_price"] = refreshed_price
            metadata["price_source"] = "bid" if bid_price is not None and bid_price > 0 else "last"
            metadata["panic_buffer_pct"] = str(panic_buffer_pct)
            metadata["stop_guard_refresh_stage"] = str(next_stage)
            return metadata
        price_source = str(
            metadata.get("price_source")
            or ("ask" if str(order.side).lower() == "buy" else "bid")
        ).lower()
        quote_field = "ask_price" if price_source == "ask" else "bid_price"
        refreshed_price = quote.get(quote_field) or quote.get("last_price")
        if refreshed_price is None:
            return None
        price_text = format(Decimal(str(refreshed_price)).quantize(Decimal("0.01")), "f")
        metadata["limit_price"] = price_text
        metadata["reference_price"] = price_text
        return metadata

    async def _fetch_quote_for_order(
        self,
        *,
        broker_account_name: str,
        symbol: str,
    ) -> dict[str, float | None]:
        fetcher = getattr(self.broker_adapter, "fetch_quotes", None)
        if callable(fetcher):
            quotes = await fetcher([symbol])
            return dict(quotes.get(symbol.upper(), {}))
        if isinstance(self.broker_adapter, RoutingBrokerAdapter):
            adapter = self.broker_adapter._adapter_for_account(broker_account_name)
            fetcher = getattr(adapter, "fetch_quotes", None)
            if callable(fetcher):
                quotes = await fetcher([symbol])
                return dict(quotes.get(symbol.upper(), {}))
        return {}

    @staticmethod
    def _replacement_client_order_id(client_order_id: str) -> str:
        base = str(client_order_id).strip()[:110]
        return f"{base}-r{uuid4().hex[:8]}"

    def _stop_reject_reason(
        self,
        *,
        request: OrderRequest,
        reports: list[ExecutionReport],
    ) -> str | None:
        if str(request.metadata.get("stop_reject_fallback", "")).lower() == "true":
            return None
        if request.intent_type not in {"open", "scale"}:
            return None
        for report in reports:
            if report.event_type == "rejected" and self._is_stop_rejection_reason(report.reason):
                return report.reason or "stop_rejected"
        return None

    def _has_cached_schwab_ineligible_symbol(
        self,
        *,
        session: Session,
        broker_account_id: UUID,
        symbol: str,
    ) -> bool:
        return (
            self.store.get_schwab_ineligible_entry(
                session,
                broker_account_id=broker_account_id,
                symbol=symbol,
                session_date=self._current_session_day(),
            )
            is not None
        )

    @staticmethod
    def _is_schwab_ineligible_reason(reason: str | None) -> bool:
        normalized = str(reason or "").strip().lower()
        return any(fragment in normalized for fragment in SCHWAB_INELIGIBLE_REASON_SUBSTRINGS)

    async def _process_stop_reject_market_fallback(
        self,
        *,
        session: Session,
        strategy,
        broker_account,
        original_event: TradeIntentEvent,
        original_request: OrderRequest,
        rejection_reason: str,
    ) -> list[OrderEventEvent]:
        available_quantity = await self._refresh_broker_position_quantity(
            session=session,
            broker_account_id=broker_account.id,
            broker_account_name=broker_account.name,
            symbol=original_event.payload.symbol,
        )
        if available_quantity <= 0:
            return []

        fallback_metadata = {
            **{str(k): str(v) for k, v in original_request.metadata.items()},
            "fallback_for_client_order_id": original_request.client_order_id,
            "fallback_rejection_reason": rejection_reason,
            "stop_reject_fallback": "true",
            "order_type": "market",
        }
        fallback_event = TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=original_event.payload.strategy_code,
                broker_account_name=original_event.payload.broker_account_name,
                symbol=original_event.payload.symbol,
                side="sell",
                quantity=available_quantity,
                intent_type="close",
                reason="STOP_REJECTED_FALLBACK",
                metadata=fallback_metadata,
            ),
        )
        fallback_intent = self.store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=broker_account,
            event=fallback_event,
        )
        self.store.record_risk_check(
            session,
            intent=fallback_intent,
            strategy_id=strategy.id,
            broker_account_id=broker_account.id,
            outcome="pass",
            reason="stop_rejected_fallback",
            payload={"metadata": dict(fallback_metadata)},
        )
        fallback_request = OrderRequest(
            client_order_id=self._build_client_order_id(fallback_event),
            broker_account_name=broker_account.name,
            strategy_code=original_event.payload.strategy_code,
            symbol=original_event.payload.symbol,
            side="sell",
            intent_type="close",
            quantity=available_quantity,
            reason="STOP_REJECTED_FALLBACK",
            metadata=fallback_metadata,
        )
        fallback_reports = await self.broker_adapter.submit_order(fallback_request)
        return await self._record_order_reports(
            session=session,
            intent=fallback_intent,
            strategy_id=strategy.id,
            broker_account_id=broker_account.id,
            intent_event=fallback_event,
            request=fallback_request,
            reports=fallback_reports,
        )

    def _is_not_tradable_reason(self, reason: str | None) -> bool:
        if not reason:
            return False
        lowered = reason.lower()
        return any(fragment in lowered for fragment in self.NOT_TRADABLE_REASONS)

    def _is_stop_rejection_reason(self, reason: str | None) -> bool:
        if not reason:
            return False
        lowered = reason.lower()
        return "stop" in lowered and ("reject" in lowered or "below" in lowered or "at/below" in lowered)
