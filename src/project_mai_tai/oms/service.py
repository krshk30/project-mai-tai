from __future__ import annotations

import asyncio
import json
import logging
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
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
from project_mai_tai.db.session import build_oms_session_factory
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Strategy, StrategyBarHistory, TradeIntent
from project_mai_tai.exit_logic.config import TradingConfig
from project_mai_tai.exit_logic.engine import ExitEngine
from project_mai_tai.exit_logic.position import Position
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
from project_mai_tai.log import configure_logging
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
    # Trailing-stop ratchet (ORB TRAIL-8%). Default 0.0 => fixed stop, byte-identical
    # to prior behavior. When >0 the stop ratchets up trail_pct% below the
    # high-water-mark and never down.
    trail_pct: float = 0.0
    high_water_mark: Decimal | None = None


@dataclass(frozen=True)
class _V2ManagedSnapshot:
    """Plain-data read of an open v2 managed row, taken INSIDE an off-loop DB unit
    so neither the ORM row nor its Session ever crosses the worker-thread boundary
    (the `_run_db` contract). Field names mirror `OmsManagedPosition` so
    `_hydrate_v2_position` works unchanged on this snapshot (duck-typed)."""

    symbol: str
    entry_price: float
    current_quantity: int
    entry_time: str
    entry_path: str
    peak_profit_pct: float
    tier: int
    floor_pct: float | None
    floor_price: float | None
    scales_done: list
    scale_pnl: float
    dedup_active: bool


@dataclass(frozen=True)
class _DriftCancelCandidate:
    """Plain-data snapshot of a working order whose quote has drifted past its
    limit. Collected inside an off-loop read unit; the broker cancel runs on-loop;
    the DB write-back re-fetches order/intent by id in a second off-loop unit. No
    ORM object crosses a thread."""

    order_id: UUID
    intent_id: UUID
    client_order_id: str
    broker_account_name: str
    strategy_code: str
    symbol: str
    side: str
    quantity: Decimal
    order_type: str
    time_in_force: str
    existing_metadata: dict
    broker_order_id: str
    limit_price: str
    intent_created_at: datetime | None
    drift: float


class OmsRiskService:
    NO_POSITION_REASONS = ("cannot be sold short", "insufficient qty", "no broker position available to sell")
    # F2 default so instances created without __init__ (test helpers) safely skip the
    # armed-stop persistence hot-path logic; __init__ overrides from settings in production.
    _armed_stop_persistence_enabled: bool = False
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
        self.session_factory = session_factory or build_oms_session_factory(self.settings)
        self.broker_adapter = broker_adapter or self._build_broker_adapter()
        self.store = store or OmsStore()
        self.strategy_registrations = strategy_registration_map(self.settings)
        self.instance_name = socket.gethostname()
        # Configure root logging so the OMS emits INFO (fills, [HARD-STOP ARMED/
        # TRIGGERED/CLEARED], exits) — not just default-WARNING. Without this the
        # entrypoint never configured logging, so every INFO line (incl. real-money
        # stop arm/trigger) was silently dropped.
        self.logger = configure_logging(SERVICE_NAME, self.settings.log_level)
        # Track-2 intrabar fix: intents and market-data ticks are consumed on SEPARATE
        # loops/tasks so a slow broker-sync REST on the control loop can never starve
        # quote-driven exit evaluation. Each stream tracks its own offset.
        self._intent_offsets = {
            stream_name(self.settings.redis_stream_prefix, "strategy-intents"): "$",
        }
        self._market_offsets = {
            stream_name(self.settings.redis_stream_prefix, "market-data"): "$",
        }
        self._armed_hard_stops: dict[tuple[str, str, str], ArmedHardStop] = {}
        # F2 (restart-while-holding): the in-memory registry above is process-memory only.
        # `_armed_stop_dirty` tracks keys whose durable `oms_armed_stops` mirror row is
        # stale; they are flushed off-loop after the stop-eval / the braid so a restart
        # can rehydrate protection (ORB was NAKED across restarts before this).
        self._armed_stop_dirty: set[tuple[str, str, str]] = set()
        self._armed_stop_persistence_enabled: bool = bool(
            getattr(self.settings, "oms_armed_stop_persistence_enabled", True)
        )
        self._boot_protection_alerts: int = 0
        self._latest_quotes_by_symbol: dict[str, dict[str, object]] = {}
        self._latest_trades_by_symbol: dict[str, dict[str, object]] = {}
        # Track-2 Phase-2 Slice-3: OMS-managed v2 exit ladder. `_managed_v2_symbols`
        # is the hot-path guard — a quote only opens a session/evaluates when its
        # symbol has an OPEN v2 managed row. Populated by the slice-1 fill hook
        # (gated) + rehydrated at startup; empty when the flag is OFF (inert).
        self._managed_v2_symbols: set[str] = set()
        self._v2_exit_config: TradingConfig = TradingConfig().make_v2_variant()
        self._v2_exit_engine: ExitEngine = ExitEngine(self._v2_exit_config)

    async def _run_db(self, fn, *, commit: bool = True):
        """Run a PURE-SYNC unit of DB work on a worker thread, off the event loop.

        SPOF cure (requirement 1): even a timeout-bounded stall (Fix 1) hangs a
        throwaway worker thread — never the shared asyncio loop — so the tick
        consumer, heartbeat, and other tasks keep running. The session is opened,
        used, committed, and closed ENTIRELY inside the thread (`fn` receives it
        and must not let it escape), so SQLAlchemy's per-thread-session contract
        holds and no session ever crosses threads. Broker `await`s must stay
        OUTSIDE `fn` (they belong on the loop; the adapters already offload their
        own REST). On exception the context manager rolls back and the error
        propagates to the caller (bounded by Fix 1), where each hot handler
        already log-skip-continues (Fix 4)."""
        def _unit():
            with self.session_factory() as session:
                result = fn(session)
                if commit:
                    session.commit()
                return result

        return await asyncio.to_thread(_unit)

    async def run(self) -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)

        seed_summary = self.seed_runtime_metadata()
        self.logger.info(
            "seeded runtime metadata: %s strategies, %s broker accounts",
            seed_summary["strategies"],
            seed_summary["broker_accounts"],
        )
        self._rehydrate_managed_v2_symbols()  # slice-3: re-arm quote eval for open v2 rows
        await self._rehydrate_armed_hard_stops()  # F2: rebuild the ORB stop registry from the durable mirror
        await self._publish_heartbeat(
            "starting",
            {
                "adapter": self.settings.oms_adapter_label,
                "providers": ",".join(self.settings.active_broker_providers),
            },
        )
        # F2 (protected-before-serving): confirm every OMS-owned held position is protected
        # + broker-backed BEFORE the tick consumer starts — never serve ticks with an
        # OMS-owned position unprotected. OMS-owned only (manual holdings untouched).
        await self._reconcile_protection_before_serving()
        # Track-2 intrabar fix: a DEDICATED tick consumer evaluates quote-driven exits
        # within milliseconds of the live tick. It is decoupled from the control loop so
        # the periodic broker-sync REST (and intent processing) can NEVER starve it — the
        # root cause of the 2026-06-17 LNAI scale that fired ~70s late at 4.345 instead of
        # ~4.45. The consumer coalesces each read burst last-quote-wins per symbol and the
        # eval rejects event-time-stale quotes, so the call is always made on the FRESHEST
        # price, never a backlogged one.
        tick_task = asyncio.create_task(self._run_tick_consumer(stop_event))
        try:
            await self._run_control_loop(stop_event)
        finally:
            stop_event.set()
            tick_task.cancel()
            try:
                await tick_task
            except asyncio.CancelledError:
                pass

        await self._publish_heartbeat(
            "stopping",
            {
                "adapter": self.settings.oms_adapter_label,
                "providers": ",".join(self.settings.active_broker_providers),
            },
        )
        await self.redis.aclose()

    async def _run_control_loop(self, stop_event: asyncio.Event) -> None:
        """Intents + periodic broker-sync + heartbeat. Reads ONLY the strategy-intents
        stream; market-data ticks are handled by `_run_tick_consumer` on its own task so
        a slow broker-sync here cannot delay an exit decision."""
        heartbeat_interval_secs = max(1, self.settings.service_heartbeat_interval_seconds)
        last_heartbeat = asyncio.get_running_loop().time()
        last_broker_sync = 0.0
        while not stop_event.is_set():
            loop_now = asyncio.get_running_loop().time()
            try:
                broker_sync_interval_secs = await self._broker_sync_interval_seconds()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Best-effort cadence optimizer — a DB stall/timeout here must
                # never break the loop or skip the heartbeat below (which is what
                # keeps the watchdog informed during a DB outage). Fall back to the
                # default interval. (Un-wrapped, this was a fatal control-loop gap.)
                self.logger.warning("broker-sync interval check failed; using default cadence")
                broker_sync_interval_secs = max(1.0, float(self.settings.oms_broker_sync_interval_seconds))
            read_timeout_secs = min(
                heartbeat_interval_secs,
                max(0.1, broker_sync_interval_secs - (loop_now - last_broker_sync)),
            )
            try:
                messages = await self.redis.xread(
                    self._intent_offsets,
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
                        self._intent_offsets[stream] = message_id
                        try:
                            await self._handle_stream_message(fields)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # A bad intent (or a Fix-1 timeout-exception raised
                            # during intent processing) must skip-continue, NOT
                            # propagate to run() and exit the whole service — the
                            # fatal control-loop gap the SPOF audit identified.
                            self.logger.exception("failed handling strategy intent message")

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
                try:
                    await self._publish_heartbeat("healthy", heartbeat_details)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The heartbeat is the watchdog's liveness signal — a transient
                    # publish error must not exit the loop. Advance the stamp anyway
                    # so we don't tight-loop; the next interval retries.
                    self.logger.exception("failed publishing heartbeat")
                last_heartbeat = now

    async def _run_tick_consumer(self, stop_event: asyncio.Event) -> None:
        """Dedicated market-data consumer — the tick-by-tick guarantee. Reads the
        market-data stream on its own task (never interleaved with broker-sync REST) and
        coalesces each read burst LAST-QUOTE-WINS per symbol, so a tick storm cannot build
        a serial backlog: the exit ladder always decides on the freshest quote within ms
        of arrival. Trades are dispatched in arrival order (armed-hard-stop fidelity)."""
        while not stop_event.is_set():
            try:
                messages = await self.redis.xread(
                    self._market_offsets,
                    block=200,
                    count=500,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("failed reading market-data stream")
                await asyncio.sleep(1)
                continue
            if not messages:
                continue
            payloads: list[dict] = []
            for stream, entries in messages:
                for message_id, fields in entries:
                    self._market_offsets[stream] = message_id
                    data = fields.get("data")
                    if not data:
                        continue
                    try:
                        payloads.append(json.loads(data))
                    except Exception:
                        continue
            for event in self._coalesce_ticks(payloads):
                try:
                    if isinstance(event, TradeTickEvent):
                        await self._handle_trade_tick_event(event)
                    else:
                        await self._handle_quote_tick_event(event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("failed handling market-data tick")

    @staticmethod
    def _coalesce_ticks(payloads: list[dict]) -> list[object]:
        """Collapse a read burst to the work actually worth doing: the NEWEST quote per
        symbol (the profit/floor ladder only cares about the current price, so acting on
        stale intermediate quotes just adds latency), while every trade tick is preserved
        in arrival order (armed-hard-stop fidelity). Returns validated event objects in
        dispatch order; the per-symbol quote slot is emitted at its first-seen position
        but carries the last-seen payload — last-quote-wins."""
        latest_quote_by_symbol: dict[str, dict] = {}
        order: list[tuple[str, object]] = []  # ("quote", symbol) | ("trade", payload)
        for payload in payloads:
            event_type = str(payload.get("event_type", "")).strip().lower()
            symbol = str((payload.get("payload") or {}).get("symbol", "")).upper()
            if not symbol:
                continue
            if event_type == "quote_tick":
                if symbol not in latest_quote_by_symbol:
                    order.append(("quote", symbol))
                latest_quote_by_symbol[symbol] = payload
            elif event_type == "trade_tick":
                order.append(("trade", payload))
        events: list[object] = []
        for kind, item in order:
            try:
                if kind == "trade":
                    events.append(TradeTickEvent.model_validate(item))
                else:
                    events.append(QuoteTickEvent.model_validate(latest_quote_by_symbol[item]))
            except Exception:
                continue
        return events

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
        # Quote/trade ticks: must reach the handler even without armed hard
        # stops so the Tier 1 quote-drift cancel can fire on working open
        # limit orders (which by definition have not filled yet, so no
        # armed hard stop). The handler itself short-circuits the
        # hard-stop evaluation when there are no armed stops.
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
                if (
                    event.payload.intent_type == "close"
                    and str(event.payload.metadata.get("stop_guard", "")).strip().lower() == "true"
                ):
                    pre_submit_events.extend(
                        await self._cancel_open_exit_orders_before_hard_stop(
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

            # Piece 1: ORB entry priced off the OMS live quote at placement (flag-gated;
            # no-op when off / non-ORB). Mutates event.payload.metadata's limit in place,
            # or returns a rejected event (abandon) which short-circuits before any submit.
            orb_abandon_event = self._apply_orb_quote_priced_entry(
                session=session, event=event, intent=intent
            )
            if orb_abandon_event is not None:
                session.commit()
                for prior_event in pre_submit_events:
                    await self._publish_order_event(prior_event)
                await self._publish_order_event(orb_abandon_event)
                return [*pre_submit_events, orb_abandon_event]

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

        # F2: mirror any armed-stop changes made by _record_order_reports (arm on a
        # buy-open fill, decrement/clear on a sell fill) to the durable table, off-loop.
        await self._flush_dirty_armed_stops()
        await self._reconcile_after_intent(event.payload.broker_account_name)
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

    async def _cancel_open_exit_orders_before_hard_stop(
        self,
        *,
        session: Session,
        strategy: Strategy,
        broker_account: BrokerAccount,
        symbol: str,
    ) -> list[OrderEventEvent]:
        published_events: list[OrderEventEvent] = []
        seen_client_order_ids: set[str] = set()

        while True:
            open_exit = self.store.find_open_exit_order(
                session,
                strategy_id=strategy.id,
                broker_account_id=broker_account.id,
                symbol=symbol,
                include_native_stop_guard=False,
            )
            if open_exit is None:
                break

            if open_exit.client_order_id in seen_client_order_ids:
                break
            seen_client_order_ids.add(open_exit.client_order_id)

            cancel_event = TradeIntentEvent(
                source_service=SERVICE_NAME,
                payload=TradeIntentPayload(
                    strategy_code=strategy.code,
                    broker_account_name=broker_account.name,
                    symbol=symbol,
                    side="sell",
                    quantity=open_exit.quantity,
                    intent_type="cancel",
                    reason="HARD_STOP_PREEMPT_PENDING_EXIT",
                    metadata={
                        "hard_stop_preempt": "true",
                        "target_client_order_id": open_exit.client_order_id,
                        "broker_order_id": open_exit.broker_order_id or "",
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
                reason="hard_stop_preempt_pending_exit",
            )
            published_events.extend(
                await self._process_cancel_intent(
                    session=session,
                    strategy_id=strategy.id,
                    broker_account_id=broker_account.id,
                    intent=cancel_intent,
                    event=cancel_event,
                )
            )

        return published_events

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

    def _apply_managed_position_after_fill(
        self,
        *,
        session: Session,
        strategy_code: str,
        broker_account_name: str,
        symbol: str,
        side: str,
        intent_type: str,
        quantity: Decimal,
        price: Decimal,
        metadata: dict[str, str],
    ) -> None:
        """Track-2 Phase-2 Slice-1: maintain the OMS-owned `oms_managed_positions`
        ladder state from v2's own fills. SOLE WRITER — only this OMS path writes
        the table. Slice 1 does NOT emit exits; it only records/closes state.
        Gated OFF by default (`oms_v2_exit_management_enabled`) → fully dormant.
        """
        if not bool(getattr(self.settings, "oms_v2_exit_management_enabled", False)):
            return
        if strategy_code != "schwab_1m_v2":
            return
        s = str(side).lower()
        it = str(intent_type).lower()
        if s == "buy" and it == "open":
            existing = self.store.get_open_managed_position(
                session, broker_account_name=broker_account_name, symbol=symbol
            )
            if existing is not None:
                return  # idempotent: already managing this symbol
            entry_path = str(metadata.get("path", "")).strip()
            self.store.create_managed_position(
                session,
                strategy_code=strategy_code,
                broker_account_name=broker_account_name,
                symbol=symbol,
                entry_price=price,
                quantity=int(quantity),
                entry_path=entry_path,
                config_name="make_v2_variant",
            )
            self._managed_v2_symbols.add(symbol)  # slice-3: arm quote-path eval
            logger.info(
                "[OMS-V2-MANAGED-OPEN] sym=%s acct=%s qty=%s entry=%s path=%s",
                symbol, broker_account_name, int(quantity), price, entry_path,
            )
        elif s == "sell":
            # #6: when close-on-fill is ON, the managed-exit row is closed HERE, on the
            # CONFIRMED fill (current_quantity decrement + close-at-0) — NOT on submit in
            # the quote eval. So managed-exit sell fills fall through to the shared
            # decrement/close below (the same path external flattens already use).
            # Legacy (flag OFF): slice-3 closed the row on submit → skip to avoid
            # double-handling (rollback lever).
            if str(metadata.get("oms_v2_managed_exit", "")).strip().lower() == "true":
                if not bool(getattr(self.settings, "oms_v2_exit_close_on_fill_enabled", True)):
                    return
            # External flatten (operator-initiated): keep the row honest.
            row = self.store.get_open_managed_position(
                session, broker_account_name=broker_account_name, symbol=symbol
            )
            if row is None:
                return
            row.current_quantity = max(0, int(row.current_quantity) - int(quantity))
            if row.current_quantity <= 0:
                self.store.close_managed_position(session, row)
                self._managed_v2_symbols.discard(symbol)  # slice-3: disarm eval
                logger.info("[OMS-V2-MANAGED-CLOSE] sym=%s acct=%s flat", symbol, broker_account_name)
            else:
                session.flush()

    # ---- Track-2 Phase-2 Slice-3: OMS-managed v2 exit ladder (quote-driven) ----

    def _rehydrate_managed_v2_symbols(self) -> None:
        """At startup, repopulate the hot-path guard from open managed rows so a
        restart keeps protecting positions opened before it. Inert when OFF."""
        if not bool(getattr(self.settings, "oms_v2_exit_management_enabled", False)):
            return
        acct = self.settings.strategy_schwab_1m_v2_account_name
        try:
            with self.session_factory() as session:
                self._managed_v2_symbols = set(
                    self.store.list_open_managed_symbols(session, broker_account_name=acct)
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("v2 managed-symbol rehydrate failed: %s", exc)
            return
        if self._managed_v2_symbols:
            self.logger.info(
                "[OMS-V2-MANAGED-REHYDRATE] armed %d symbol(s): %s",
                len(self._managed_v2_symbols), ",".join(sorted(self._managed_v2_symbols)),
            )

    # ------------------------------------------------------------------ #
    # F2: durable armed-stop mirror — persist / rehydrate / boot reconcile
    # ------------------------------------------------------------------ #
    @staticmethod
    def _armed_stop_row_kwargs(stop: ArmedHardStop) -> dict:
        """Persistable fields of an ArmedHardStop (transient throttle state excluded)."""
        return {
            "quantity": stop.quantity,
            "entry_price": stop.entry_price,
            "stop_loss_pct": float(stop.stop_loss_pct),
            "stop_price": stop.stop_price,
            "quote_max_age_ms": int(stop.quote_max_age_ms),
            "initial_panic_buffer_pct": float(stop.initial_panic_buffer_pct),
            "trail_pct": float(stop.trail_pct),
            "high_water_mark": stop.high_water_mark,
            "close_in_flight": bool(stop.close_in_flight),
        }

    def _persist_armed_stop_snapshot(self, session: Session, snapshot: list) -> None:
        """Off-loop WRITE unit: upsert present keys, delete absent ones — mirroring the
        in-memory registry state captured on-loop before the thread hop."""
        for (strategy_code, broker_account_name, symbol), kwargs in snapshot:
            if kwargs is None:
                self.store.delete_armed_stop(
                    session, strategy_code=strategy_code,
                    broker_account_name=broker_account_name, symbol=symbol,
                )
            else:
                self.store.upsert_armed_stop(
                    session, strategy_code=strategy_code,
                    broker_account_name=broker_account_name, symbol=symbol, **kwargs,
                )

    async def _flush_dirty_armed_stops(self) -> None:
        """Persist dirtied armed-stop keys to the durable mirror OFF the loop (best-effort).
        The in-memory stop is authoritative for live triggering; the mirror exists only for
        restart-recovery, so a failed/slow flush never affects protection (and the boot
        reconcile is the safety net). Snapshots on-loop so no dict is read from the thread."""
        if not self._armed_stop_persistence_enabled or not self._armed_stop_dirty:
            return
        keys = list(self._armed_stop_dirty)
        self._armed_stop_dirty.clear()
        snapshot: list = []
        for key in keys:
            stop = self._armed_hard_stops.get(key)
            snapshot.append((key, self._armed_stop_row_kwargs(stop) if stop is not None else None))
        try:
            await self._run_db(
                lambda session: self._persist_armed_stop_snapshot(session, snapshot), commit=True
            )
        except Exception as exc:  # noqa: BLE001 — mirror is best-effort; reconcile is the net
            self.logger.warning("armed-stop mirror flush failed (best-effort): %s", exc)
            self._armed_stop_dirty.update(keys)  # retry on the next flush

    @staticmethod
    def _armed_stop_row_to_dict(row) -> dict:
        """Convert a durable OmsArmedStop ORM row to primitives INSIDE the worker thread
        (so no ORM object escapes the `_run_db` unit)."""
        return {
            "strategy_code": str(row.strategy_code),
            "broker_account_name": str(row.broker_account_name),
            "symbol": str(row.symbol).upper(),
            "quantity": Decimal(str(row.quantity)),
            "entry_price": Decimal(str(row.entry_price)),
            "stop_loss_pct": float(row.stop_loss_pct),
            "stop_price": Decimal(str(row.stop_price)),
            "quote_max_age_ms": int(row.quote_max_age_ms),
            "initial_panic_buffer_pct": float(row.initial_panic_buffer_pct),
            "trail_pct": float(row.trail_pct),
            "high_water_mark": (Decimal(str(row.high_water_mark)) if row.high_water_mark is not None else None),
            "close_in_flight": bool(row.close_in_flight),
        }

    async def _rehydrate_armed_hard_stops(self) -> None:
        """Boot: rebuild the in-memory `_armed_hard_stops` registry from the durable mirror
        so an ORB position stays PROTECTED across a restart (the pre-F2 naked gap). Off-loop
        read; the dict assignment (registry mutation) stays on-loop."""
        if not self._armed_stop_persistence_enabled:
            return
        try:
            rows = await self._run_db(
                lambda session: [
                    self._armed_stop_row_to_dict(r) for r in self.store.list_armed_stops(session)
                ],
                commit=False,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("armed-stop rehydrate read failed: %s", exc)
            return
        for d in rows:
            key = self._hard_stop_key(d["strategy_code"], d["broker_account_name"], d["symbol"])
            self._armed_hard_stops[key] = ArmedHardStop(
                strategy_code=d["strategy_code"], broker_account_name=d["broker_account_name"],
                symbol=d["symbol"], quantity=d["quantity"], entry_price=d["entry_price"],
                stop_loss_pct=d["stop_loss_pct"], stop_price=d["stop_price"],
                quote_max_age_ms=d["quote_max_age_ms"], initial_panic_buffer_pct=d["initial_panic_buffer_pct"],
                close_in_flight=d["close_in_flight"], last_trigger_attempt_at=None,
                trail_pct=d["trail_pct"], high_water_mark=d["high_water_mark"],
            )
        if rows:
            self.logger.info(
                "[OMS-ARMED-STOP-REHYDRATE] restored %d armed stop(s): %s",
                len(rows), ",".join(sorted(str(d["symbol"]) for d in rows)),
            )

    def _read_owned_positions_with_broker_qty(self, session: Session) -> list:
        """Off-loop READ unit: OMS-owned open positions (per-strategy virtual ledger) with
        their current broker-truth quantity. OMS-owned by construction — a manual holding
        has no virtual_positions row, so it is never returned (scoping invariant)."""
        out: list = []
        for sc, ban, sym, qty in self.store.list_owned_open_positions(session):
            if qty <= 0:
                continue
            broker_qty = self.store.get_account_position_qty_by_name(
                session, broker_account_name=ban, symbol=sym
            )
            out.append((sc, ban, str(sym).upper(), Decimal(str(qty)), Decimal(str(broker_qty))))
        return out

    async def _reconcile_protection_before_serving(self) -> None:
        """PROTECTED-BEFORE-SERVING: before the tick consumer starts, confirm every
        OMS-OWNED open position is protected (a rehydrated stop / managed row) AND backed at
        the broker. OMS-owned ONLY (manual holdings are invisible — no virtual_positions row,
        never touched: the scoping invariant). Loud-logs the INVERSE mismatch only: an OMS
        record present but the position missing/short at the broker, or an owned position
        with no rehydrated protection. Never arms/sells/flags a holding it did not place."""
        if not self._armed_stop_persistence_enabled:
            return
        try:
            await self.sync_broker_positions()  # refresh account_positions (off-loop, #391)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("boot reconcile: broker position sync failed: %s", exc)
        try:
            owned = await self._run_db(self._read_owned_positions_with_broker_qty, commit=False)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("boot reconcile: owned-position read failed: %s", exc)
            return
        alerts = 0
        for sc, ban, sym, owned_qty, broker_qty in owned:
            key = self._hard_stop_key(sc, ban, sym)
            protected = key in self._armed_hard_stops or sym in self._managed_v2_symbols
            if not protected:
                alerts += 1
                self.logger.error(
                    "[OMS-BOOT-PROTECTION-ALERT] NAKED OMS-owned position %s %s qty=%s has NO "
                    "rehydrated stop after restart — investigate",
                    ban, sym, owned_qty,
                )
            if broker_qty < owned_qty:
                alerts += 1
                self.logger.error(
                    "[OMS-BOOT-PROTECTION-ALERT] VANISHED OMS-owned position %s %s expected "
                    "qty=%s but broker shows %s — investigate",
                    ban, sym, owned_qty, broker_qty,
                )
        self._boot_protection_alerts = alerts
        if alerts == 0:
            self.logger.info(
                "[OMS-BOOT-PROTECTION] all %d OMS-owned position(s) protected + broker-backed",
                len(owned),
            )

    def _hydrate_v2_position(self, row) -> Position:
        """Rebuild an exit_logic.Position from a managed row, restoring the
        accumulated ladder state (peak/tier/floor/scales) so the ratchet CONTINUES
        — never resets — across quotes. Floor params from make_v2_variant."""
        cfg = self._v2_exit_config
        p = Position(
            ticker=row.symbol,
            entry_price=float(row.entry_price),
            quantity=int(row.current_quantity),
            entry_time=str(row.entry_time),
            path=row.entry_path or "",
            scale_profile="NORMAL",
            floor_lock_at_1pct_peak_pct=cfg.profit_floor_lock_at_1pct_peak_pct,
            floor_lock_at_2pct_peak_pct=cfg.profit_floor_lock_at_2pct_peak_pct,
            floor_lock_at_3pct_peak_pct=cfg.profit_floor_lock_at_3pct_peak_pct,
            floor_trail_buffer_over_4pct_pct=cfg.profit_floor_trail_buffer_over_4pct_pct,
        )
        p.peak_profit_pct = float(row.peak_profit_pct or 0.0)
        p.tier = int(row.tier or 1)
        p.floor_pct = float(row.floor_pct) if row.floor_pct is not None else -999.0
        p.floor_price = float(row.floor_price) if row.floor_price is not None else 0.0
        p.scales_done = list(row.scales_done or [])
        p.scale_pnl = float(row.scale_pnl or 0.0)
        return p

    def _v2_scale_level_price(self, entry_price: float, level: str) -> float:
        cfg = self._v2_exit_config
        pct = {
            "PCT2": cfg.scale_normal2_pct,
            "FAST4": cfg.scale_fast4_pct,
            "PCT4_AFTER2": cfg.scale_4after2_pct,
        }.get(str(level))
        return entry_price if pct is None else entry_price * (1.0 + float(pct) / 100.0)

    async def _evaluate_v2_managed_exit(self, symbol: str) -> None:
        """Run the v2 exit ladder for one symbol on the latest quote. DECISION uses
        the live bid; FILL reference_price is the leg LEVEL (decision B — stop/floor/
        scale level) so live-paper agrees with the re-score by construction. Precedence
        hard>floor>scale, one action per quote. Sole-writer of the managed row; the
        quote->Position state-update is co-located here (deferred from slice 1).

        PR-A off-load: the per-tick READ and the no-exit / dedup price-state WRITE are
        the high-frequency freeze driver — they carry no broker await and no in-memory
        dict mutation, so they run OFF the event loop via ``_run_db``. Decisions
        (hydrate/ratchet) and the ``_managed_v2_symbols`` guard mutation stay on-loop.
        The RARE exit-emit (``_emit_v2_exit_on_loop``) keeps its on-loop session — it
        reaches the shared, dict-mutating, broker-awaiting ``_record_order_reports``
        (owned by PR-D); it is bounded to ~5s by #391 Fix-1 and fires only on an exit."""
        if not bool(getattr(self.settings, "oms_v2_exit_management_enabled", False)):
            return
        quote = self._latest_quotes_by_symbol.get(symbol)
        if not quote:
            return
        received_at = quote.get("received_at")
        if isinstance(received_at, datetime):
            age_ms = (utcnow() - received_at).total_seconds() * 1000.0
            if age_ms > float(getattr(self.settings, "oms_v2_exit_quote_max_age_ms", 5000)):
                return  # stale quote — never act on a gap
        bid = float(quote.get("bid") or 0.0)
        if bid <= 0:
            return
        acct = self.settings.strategy_schwab_1m_v2_account_name
        # #6 (CLRO desync fix): mark closed on the confirmed FILL, not on submit. Default on.
        close_on_fill = bool(getattr(self.settings, "oms_v2_exit_close_on_fill_enabled", True))
        try:
            # Phase 1 — READ (off-loop): snapshot the open row + dedup state. Neither the
            # ORM row nor its Session escapes the worker thread. None => no open row.
            snapshot = await self._run_db(
                lambda session: self._read_v2_managed_snapshot(session, acct, symbol, close_on_fill),
                commit=False,
            )
            if snapshot is None:
                self._managed_v2_symbols.discard(symbol)  # dict mutation stays on-loop
                return

            # Phase 2 — DECIDE (on-loop, pure): hydrate + ratchet off the snapshot.
            entry_price = snapshot.entry_price
            position = self._hydrate_v2_position(snapshot)
            position.update_price(bid)

            # #6 dedup guard: an exit order already works for this symbol -> keep the
            # position open + monitored + broker-consistent; refresh ladder PRICE-state
            # only (write_quantity=False, held qty stays fill-gated) and do NOT re-emit.
            if snapshot.dedup_active:
                await self._run_db(
                    lambda session: self._persist_v2_price_state(
                        session, acct, symbol, position, write_quantity=False
                    ),
                    commit=True,
                )
                return

            hard = self._v2_exit_engine.check_hard_stop(position, bid)
            intrabar = None if hard is not None else self._v2_exit_engine.check_intrabar_exit(position)

            if hard is not None:
                ref = entry_price * (1.0 - float(self._v2_exit_config.stop_loss_pct) / 100.0)
                await self._emit_v2_exit_on_loop(
                    acct, symbol, position, entry_price, kind="HARD",
                    reference_price=ref, reason="oms_v2_managed_exit:HARD_STOP",
                    bid=bid, close_on_fill=close_on_fill,
                )
            elif intrabar is not None and intrabar.get("action") == "CLOSE":
                ref = float(position.floor_price) or bid
                await self._emit_v2_exit_on_loop(
                    acct, symbol, position, entry_price, kind="FLOOR",
                    reference_price=ref, reason="oms_v2_managed_exit:FLOOR_BREACH",
                    bid=bid, close_on_fill=close_on_fill,
                )
            elif intrabar is not None and intrabar.get("action") == "SCALE" and int(intrabar.get("sell_qty") or 0) > 0:
                sell_qty = int(intrabar["sell_qty"])
                level = str(intrabar.get("level") or "")
                ref = self._v2_scale_level_price(entry_price, level)
                await self._emit_v2_exit_on_loop(
                    acct, symbol, position, entry_price, kind="SCALE",
                    reference_price=ref, reason=f"oms_v2_managed_exit:SCALE_{level}",
                    bid=bid, close_on_fill=close_on_fill, sell_qty=sell_qty, level=level,
                )
            else:
                # no exit this quote — co-located quote->Position state update (off-loop write)
                await self._run_db(
                    lambda session: self._persist_v2_price_state(
                        session, acct, symbol, position, write_quantity=True
                    ),
                    commit=True,
                )
        except Exception as exc:  # noqa: BLE001 — the quote path must never die
            self.logger.warning("v2 managed-exit eval failed for %s: %s", symbol, exc)
            return

    def _read_v2_managed_snapshot(
        self, session: Session, acct: str, symbol: str, close_on_fill: bool
    ) -> _V2ManagedSnapshot | None:
        """Off-loop READ unit: snapshot the open managed row + whether an exit order is
        already working (dedup). Returns None when there is no open row. Pure DB read —
        no ORM object leaves this function (the `_run_db` contract)."""
        row = self.store.get_open_managed_position(
            session, broker_account_name=acct, symbol=symbol
        )
        if row is None:
            return None
        dedup_active = False
        if close_on_fill:
            broker_account = session.scalar(
                select(BrokerAccount).where(BrokerAccount.name == row.broker_account_name)
            )
            if broker_account is not None and self.store.get_open_exit_reserved_quantity(
                session,
                broker_account_id=broker_account.id,
                symbol=symbol,
                include_native_stop_guard=False,
            ) > 0:
                dedup_active = True
        return _V2ManagedSnapshot(
            symbol=row.symbol,
            entry_price=float(row.entry_price),
            current_quantity=int(row.current_quantity),
            entry_time=str(row.entry_time),
            entry_path=row.entry_path or "",
            peak_profit_pct=float(row.peak_profit_pct or 0.0),
            tier=int(row.tier or 1),
            floor_pct=(float(row.floor_pct) if row.floor_pct is not None else None),
            floor_price=(float(row.floor_price) if row.floor_price is not None else None),
            scales_done=list(row.scales_done or []),
            scale_pnl=float(row.scale_pnl or 0.0),
            dedup_active=dedup_active,
        )

    def _persist_v2_price_state(
        self, session: Session, acct: str, symbol: str, position: Position, *, write_quantity: bool
    ) -> None:
        """Off-loop WRITE unit: persist ladder state for the still-open managed row.
        Re-fetches the row in this fresh session (no ORM crosses threads); no-op if the
        row has since closed (safe under the single-loop-thread model)."""
        row = self.store.get_open_managed_position(
            session, broker_account_name=acct, symbol=symbol
        )
        if row is None:
            return
        self.store.update_managed_position_from_position(
            session, row, position, write_quantity=write_quantity
        )

    async def _emit_v2_exit_on_loop(
        self,
        acct: str,
        symbol: str,
        position: Position,
        entry_price: float,
        *,
        kind: str,
        reference_price: float,
        reason: str,
        bid: float,
        close_on_fill: bool,
        sell_qty: int | None = None,
        level: str | None = None,
    ) -> None:
        """The RARE v2 exit-emit, kept ON-LOOP (single session, one commit) exactly as
        before PR-A: it reaches the shared ``_record_order_reports``, which mutates
        ``_armed_hard_stops`` and awaits a broker submit, so it must not run in a worker
        thread. Bounded to ~5s by #391 Fix-1; fires only when an exit actually triggers.
        Behaviour of the per-kind write/close/scale + publish is byte-identical to the
        pre-split inline branches."""
        events: list = []
        try:
            with self.session_factory() as session:
                row = self.store.get_open_managed_position(
                    session, broker_account_name=acct, symbol=symbol
                )
                if row is None:
                    self._managed_v2_symbols.discard(symbol)
                    return
                if kind == "SCALE":
                    events = await self._emit_v2_managed_sell(
                        session, row, intent_type="scale", quantity=int(sell_qty or 0),
                        reference_price=reference_price, reason=reason, bid=bid,
                    )
                    position.apply_scale(str(level or ""), int(sell_qty or 0), exit_price=reference_price)
                    # #6: fill-gate the scale quantity (write_quantity=False) — the scale fill
                    # decrements current_quantity; on submit persist only the ladder state.
                    self.store.update_managed_position_from_position(
                        session, row, position, write_quantity=not close_on_fill
                    )
                else:  # HARD / FLOOR — full close
                    events = await self._emit_v2_managed_sell(
                        session, row, intent_type="close", quantity=int(position.quantity),
                        reference_price=reference_price, reason=reason, bid=bid,
                    )
                    if close_on_fill:
                        # #6: do NOT close on submit — the confirmed fill closes the row.
                        # Persist price-state only; keep the position monitored/protected.
                        self.store.update_managed_position_from_position(
                            session, row, position, write_quantity=False
                        )
                    else:
                        self.store.close_managed_position(session, row)
                        self._managed_v2_symbols.discard(symbol)
                session.commit()
        except Exception as exc:  # noqa: BLE001 — the quote path must never die
            self.logger.warning("v2 managed-exit emit failed for %s: %s", symbol, exc)
            return
        for ev in events:
            await self._publish_order_event(ev)

    async def _emit_v2_managed_sell(
        self,
        session: Session,
        row,
        *,
        intent_type: str,
        quantity: int,
        reference_price: float,
        reason: str,
        bid: float | None = None,
    ) -> list:
        """THE SINGLE place a v2 managed-exit SELL is built. The order's
        broker_account_name is ALWAYS the managed row's account — the safe-by-
        construction invariant that pins routing to the simulated adapter
        (paper-isolation; proven by test_v2_exit_paper_isolation).

        Extended-hours routing (2026-07-05): in RTH the order stays MARKET/NORMAL
        (byte-identical). In extended hours a MARKET order cannot fill, so route a
        LIMIT with session=AM|PM off the live ``bid``: protective legs (hard-stop /
        floor, intent_type="close") price a MARKETABLE buffered limit so they
        reliably cross the spread; scale partials price AT the bid (patient). The
        leg-level ``reference_price`` is left unchanged so the [OMS-V2-MANAGED-EXIT]
        log and the live-paper re-score stay identical; ``limit_price`` drives the
        live order (adapter prefers limit_price, falls back to reference_price)."""
        strategy = session.scalar(select(Strategy).where(Strategy.code == row.strategy_code))
        broker_account = session.scalar(
            select(BrokerAccount).where(BrokerAccount.name == row.broker_account_name)
        )
        if strategy is None or broker_account is None:
            self.logger.warning(
                "[OMS-V2-MANAGED-EXIT] missing strategy/account %s/%s — no exit emitted",
                row.strategy_code, row.broker_account_name,
            )
            return []
        metadata = {
            "oms_v2_managed_exit": "true",
            "reference_price": f"{float(reference_price):.4f}",
            "order_type": "market",
            "time_in_force": "day",
        }
        order_type = "market"
        session_code = _extended_hours_session()
        if session_code is not None and bid and bid > 0:
            if intent_type == "scale":
                routed = _format_limit_price(bid)  # profit-taking: at the bid, zero buffer
            else:  # "close" = hard-stop / floor: buffered marketable limit that must fill
                buffer_pct = float(
                    getattr(self.settings, "oms_v2_exit_eh_protective_limit_buffer_pct", 0.5)
                )
                routed = _panic_limit_price(bid, buffer_pct)
            if routed is not None:
                order_type = "limit"
                metadata.update(
                    {
                        "order_type": "limit",
                        "limit_price": routed,
                        "price_source": "bid",
                        "session": session_code,
                        "extended_hours": "true",
                    }
                )
        event = TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=row.strategy_code,
                broker_account_name=row.broker_account_name,  # <-- THE INVARIANT
                symbol=row.symbol,
                side="sell",
                quantity=Decimal(str(quantity)),
                intent_type=intent_type,
                reason=reason,
                metadata=dict(metadata),
            ),
        )
        intent = self.store.create_trade_intent(
            session, strategy=strategy, broker_account=broker_account, event=event
        )
        self._record_internal_risk_pass(
            session, intent=intent, strategy=strategy, broker_account=broker_account,
            metadata=dict(metadata), reason="oms_v2_managed_exit",
        )
        request = OrderRequest(
            client_order_id=self._build_client_order_id(event),
            broker_account_name=row.broker_account_name,  # <-- THE INVARIANT
            strategy_code=row.strategy_code,
            symbol=row.symbol,
            side="sell",
            intent_type=intent_type,
            quantity=Decimal(str(quantity)),
            reason=reason,
            metadata=dict(metadata),
            order_type=order_type,
            time_in_force="day",
        )
        reports = await self.broker_adapter.submit_order(request)
        events = await self._record_order_reports(
            session=session, intent=intent, strategy_id=strategy.id,
            broker_account_id=broker_account.id, intent_event=event,
            request=request, reports=reports,
        )
        self.logger.info(
            "[OMS-V2-MANAGED-EXIT] %s sym=%s acct=%s qty=%s ref=%.4f",
            reason, row.symbol, row.broker_account_name, quantity, float(reference_price),
        )
        return events

    async def _has_active_native_stop_guard_order(
        self,
        *,
        strategy_code: str,
        broker_account_name: str,
        symbol: str,
    ) -> bool:
        def _unit(session) -> bool:
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

        # Off-loop (Fix 2): this pre-close dedup check sits on the hard-stop path
        # (A1). A stall here must never freeze the loop; and per Fix 3 the caller
        # treats a raised timeout as "proceed to fire the stop".
        return await self._run_db(_unit, commit=False)

    async def _reconcile_after_intent(self, broker_account_name: str) -> None:
        """Best-effort post-intent broker→DB reconcile (Fix 3b).

        By the time this runs the order has ALREADY been submitted and committed,
        and the broker is the source of truth. A stall/failure here must NOT unwind
        the submitted (possibly protective) order or propagate to the caller — the
        next periodic ``sync_broker_orders`` back-fills the DB from the broker. So a
        hung reconcile degrades bookkeeping only, it never blocks or unwinds a stop."""
        try:
            await self.sync_broker_state(account_names=[broker_account_name])
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.warning(
                "post-intent broker-state reconcile failed for %s (order already "
                "submitted; next periodic sync reconciles)",
                broker_account_name,
            )

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
        # SPOF fix (Fix 2): this is the method BOTH 2026-07-01/02 zombies hung in
        # — `sync_account_positions -> session.flush()` ran on the event loop and
        # hung on a stalled connection. Split into phases so the DB work runs OFF
        # the loop via `_run_db` while the broker REST `await`s stay ON the loop
        # (the adapter already offloads them). Behavior is identical when healthy:
        # same accounts, same per-account sync, same virtual-clear, same commit
        # boundary (nothing is committed unless all broker fetches succeed).
        # Phase 1 (DB, off-loop): resolve target accounts as (id, name) tuples.
        def _load_accounts(session) -> list[tuple[UUID, str]]:
            if account_names is None:
                accounts = self.store.list_active_broker_accounts(session)
            else:
                accounts = self.store.list_named_broker_accounts(session, account_names)
            return [(account.id, account.name) for account in accounts]

        accounts = await self._run_db(_load_accounts, commit=False)

        # Phase 2 (broker REST, on-loop): fetch each account's live positions.
        fetched: list[tuple[UUID, list]] = []
        for account_id, account_name in accounts:
            snapshots = await self.broker_adapter.list_account_positions(account_name)
            fetched.append((account_id, snapshots))

        # Phase 3 (DB writes, off-loop): persist snapshots + clear unbacked
        # virtuals, committed inside the worker thread (the flush that froze the
        # loop now cannot).
        account_ids = [account_id for account_id, _ in accounts]

        def _persist(session) -> int:
            synced_positions = 0
            for account_id, snapshots in fetched:
                synced_positions += self.store.sync_account_positions(
                    session,
                    broker_account_id=account_id,
                    snapshots=snapshots,
                )
            self.store.clear_virtual_positions_without_account_backing(
                session,
                broker_account_ids=account_ids,
            )
            return synced_positions

        synced_positions = await self._run_db(_persist)

        return {
            "accounts": len(accounts),
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
                # Poll if we can identify the order at the broker by EITHER id. Webull's
                # place response returns only a client_order_id (broker_order_id arrives
                # later via order-detail), so gating on broker_order_id alone meant Webull
                # fills were never polled -> the fill went undetected and the hard stop
                # never armed (naked position). fetch_order_update keys on client_order_id,
                # so client_order_id is sufficient; Alpaca/Schwab always have a
                # broker_order_id by this point, so this is behaviour-identical for them.
                if account is None or not (order.broker_order_id or order.client_order_id):
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
                    metadata={**{str(k): str(v) for k, v in (order.payload or {}).items()}, "broker_order_id": order.broker_order_id or ""},
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
                        self._apply_managed_position_after_fill(
                            session=session,
                            strategy_code=strategy.code if strategy is not None else "",
                            broker_account_name=account.name,
                            symbol=order.symbol,
                            side=order.side,
                            intent_type=intent.intent_type,
                            quantity=fill.quantity,
                            price=fill.price,
                            metadata={str(k): str(v) for k, v in (order.payload or {}).items()},
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
                    # Tier 2 + Tier 3: before paying for another cancel-and-replace
                    # cycle, decide whether the intent itself should be abandoned.
                    # The 2026-05-18 incident had 414 retries on a single intent;
                    # these guards stop that.
                    abandon_code: str | None = None
                    abandon_detail: str | None = None
                    if (
                        str(intent.intent_type).lower() == "open"
                        and not self._is_stop_guard_order(order)
                    ):
                        if self._intent_too_old(intent):
                            abandon_code = "INTENT_MAX_AGE"
                            abandon_detail = (
                                f"intent age {self._intent_age_secs(intent):.1f}s "
                                f"exceeds max {self._intent_max_age_secs()}s"
                            )
                        else:
                            invalid_reason = self._intent_setup_invalid_reason(
                                session,
                                intent=intent,
                                strategy=strategy,
                            )
                            if invalid_reason:
                                abandon_code = "SETUP_INVALID"
                                abandon_detail = invalid_reason
                    if abandon_code is not None:
                        await self._cancel_working_order_and_abandon_intent(
                            session=session,
                            order=order,
                            intent=intent,
                            strategy=strategy,
                            broker_account=account,
                            reason_code=abandon_code,
                            reason_detail=abandon_detail or abandon_code,
                        )
                        synced_orders += 1
                        terminal_orders += 1
                    else:
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

            self._terminalize_orphaned_active_intents(
                session,
                broker_account_ids=list(account_lookup.keys()),
            )
            session.commit()

        for order_event in published_events:
            await self._publish_order_event(order_event)

        # F2: mirror armed-stop changes made during the per-order sync (arm/decrement/
        # clear/rearm) to the durable table, off-loop after the session closed.
        await self._flush_dirty_armed_stops()
        return {
            "orders": synced_orders,
            "terminal_orders": terminal_orders,
        }

    def _terminalize_orphaned_active_intents(
        self,
        session: Session,
        *,
        broker_account_ids: list[UUID],
    ) -> int:
        """Repair active intents whose broker orders have already gone terminal."""
        if not broker_account_ids:
            return 0

        active_statuses = set(self.store.OPEN_ORDER_STATUSES)
        repaired = 0
        active_intents = session.scalars(
            select(TradeIntent)
            .where(TradeIntent.broker_account_id.in_(broker_account_ids))
            .where(TradeIntent.status.in_(("pending", "submitted", "accepted")))
        ).all()

        for intent in active_intents:
            related_orders = session.scalars(
                select(BrokerOrder).where(BrokerOrder.intent_id == intent.id)
            ).all()
            if related_orders:
                statuses = {str(order.status).lower() for order in related_orders}
                if statuses & active_statuses:
                    continue
                terminal_status = self._terminal_intent_status_from_order_statuses(statuses)
                if terminal_status is None:
                    continue
                self.store.mark_intent_status(intent, terminal_status)
                repaired += 1
                continue

            if str(intent.intent_type).lower() != "cancel":
                continue
            target_order = self._target_order_for_cancel_intent(session, intent)
            if target_order is None or str(target_order.status).lower() in active_statuses:
                continue
            self.store.mark_intent_status(intent, str(target_order.status).lower())
            repaired += 1

        if repaired:
            self.logger.info("[OMS-INTENT-REPAIR] terminalized %s orphaned active intents", repaired)
        return repaired

    @staticmethod
    def _terminal_intent_status_from_order_statuses(statuses: set[str]) -> str | None:
        if not statuses:
            return None
        if "filled" in statuses:
            return "filled"
        if "partially_filled" in statuses:
            return None
        if "cancelled" in statuses:
            return "cancelled"
        if "rejected" in statuses:
            return "rejected"
        return None

    def _target_order_for_cancel_intent(self, session: Session, intent: TradeIntent) -> BrokerOrder | None:
        payload = intent.payload or {}
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        if not isinstance(metadata, dict):
            return None
        target_client_order_id = str(metadata.get("target_client_order_id") or "").strip()
        broker_order_id = str(metadata.get("broker_order_id") or "").strip()
        if target_client_order_id:
            order = session.scalar(
                select(BrokerOrder).where(BrokerOrder.client_order_id == target_client_order_id)
            )
            if order is not None:
                return order
        if broker_order_id:
            return session.scalar(select(BrokerOrder).where(BrokerOrder.broker_order_id == broker_order_id))
        return None

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
        def _unit(session) -> bool:
            broker_accounts = self.store.list_active_broker_accounts(session)
            open_orders = self.store.list_open_orders(
                session,
                broker_account_ids=[account.id for account in broker_accounts],
            )
            return any(self._is_stop_guard_order(order) for order in open_orders)

        return await self._run_db(_unit, commit=False)

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
        # Event-time, not processing-time: stamp with the producer's `produced_at` so the
        # downstream staleness guard measures TRUE price age. (Same host as market-data →
        # no clock skew.) Previously this was utcnow() at processing time, which made the
        # guard blind to consumption lag and let the LNAI exit act on a 70s-old quote.
        self._latest_quotes_by_symbol[symbol] = {
            "bid": float(event.payload.bid_price),
            "ask": float(event.payload.ask_price),
            "received_at": self._event_time(event),
        }
        if self._armed_hard_stops:
            await self._evaluate_hard_stop_market_event(symbol)
        await self._cancel_drifted_working_orders(symbol)
        # Slice-3: run the v2 exit ladder on this quote, but ONLY for symbols with an
        # open v2 managed row (the in-memory guard keeps the hot path free of DB hits
        # for everything else; empty set when the flag is OFF → no-op).
        if symbol in self._managed_v2_symbols:
            await self._evaluate_v2_managed_exit(symbol)

    async def _handle_trade_tick_event(self, event: TradeTickEvent) -> None:
        symbol = str(event.payload.symbol).upper()
        self._latest_trades_by_symbol[symbol] = {
            "price": float(event.payload.price),
            "received_at": self._event_time(event),
        }
        await self._evaluate_hard_stop_market_event(symbol)

    @staticmethod
    def _event_time(event: object) -> datetime:
        """Producer publish time for staleness measurement, falling back to now() if the
        envelope lacks a usable timestamp (so a missing field can never wedge the path)."""
        produced_at = getattr(event, "produced_at", None)
        if isinstance(produced_at, datetime):
            return produced_at
        return utcnow()

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
            self._ratchet_trailing_stop(stop)  # raise the trailing stop on favorable moves (inert when trail_pct=0)
            if self._is_hard_stop_trigger_throttled(stop):
                continue
            trigger_price, trigger_source = self._resolve_hard_stop_trigger_price(stop)
            if trigger_price is None or trigger_source is None:
                continue
            if Decimal(str(trigger_price)) > stop.stop_price:
                continue
            await self._trigger_hard_stop(stop, trigger_price=Decimal(str(trigger_price)), trigger_source=trigger_source)
        # F2: persist any ratcheted/cleared stops OFF the loop, AFTER every trigger decision
        # above — so the mirror stays fresh for restart-recovery without ever delaying a
        # stop (the in-memory stop is authoritative). No-op when nothing dirtied.
        await self._flush_dirty_armed_stops()

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

    @staticmethod
    def _ratcheted_trailing_stop(
        stop_price: Decimal, high_water_mark: Decimal, observed_price: Decimal, trail_pct: float
    ) -> tuple[Decimal, Decimal]:
        """Pure ratchet math. Returns (new_stop_price, new_high_water_mark); the
        stop only ever rises. ``trail_pct <= 0`` is inert (returns inputs)."""
        if trail_pct <= 0 or observed_price <= high_water_mark:
            return stop_price, high_water_mark
        candidate = observed_price * (Decimal("1") - Decimal(str(trail_pct)) / Decimal("100"))
        return (candidate if candidate > stop_price else stop_price), observed_price

    def _ratchet_trailing_stop(self, stop: ArmedHardStop) -> None:
        """Raise a trailing stop toward ``trail_pct`` below the high-water-mark of the
        freshest BID. No-op for fixed stops (trail_pct=0).

        BID-ONLY (deliberate): the breach trigger fires on the bid, so the ratchet
        must track the bid too. Tracking the *last* trade instead would, on a
        wide-spread thin microcap (spread > trail_pct), ratchet the stop up off a
        high last print and then immediately trigger on a much-lower bid — running
        the trail tighter than the backtested TRAIL-8% width (the TRAIL-3%-overfit
        failure mode already ruled out). Keeping ratchet and trigger on the same
        reference preserves the robust 8% room that made TRAIL-8% win."""
        if stop.trail_pct <= 0:
            return
        quote = self._latest_quotes_by_symbol.get(stop.symbol)
        if quote is None or quote.get("bid") is None:
            return
        received_at = quote.get("received_at")
        if not isinstance(received_at, datetime):
            return
        if (utcnow() - received_at).total_seconds() * 1000 > max(0, stop.quote_max_age_ms):
            return
        hwm = stop.high_water_mark if stop.high_water_mark is not None else stop.entry_price
        prev_stop_price, prev_hwm = stop.stop_price, stop.high_water_mark
        stop.stop_price, stop.high_water_mark = self._ratcheted_trailing_stop(
            stop.stop_price, hwm, Decimal(str(quote["bid"])), stop.trail_pct
        )
        # F2: persist the ratcheted level (full fidelity) only when it actually moved.
        if self._armed_stop_persistence_enabled and (
            stop.stop_price != prev_stop_price or stop.high_water_mark != prev_hwm
        ):
            self._armed_stop_dirty.add(
                self._hard_stop_key(stop.strategy_code, stop.broker_account_name, stop.symbol)
            )

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
        if _is_regular_market_session():
            try:
                has_native_guard = await self._has_active_native_stop_guard_order(
                    strategy_code=stop.strategy_code,
                    broker_account_name=stop.broker_account_name,
                    symbol=stop.symbol,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Fix 3: the pre-close native-guard dedup check is an OPTIMIZATION,
                # not a safety gate. If it stalls/times out (DB hung), PROCEED to
                # fire the protective close — a DB stall must NEVER abort real-money
                # stop protection. Worst case is a duplicate close the periodic sync
                # reconciles, which is strictly safer than a missed stop.
                self.logger.warning(
                    "[HARD-STOP] native-guard pre-check failed (DB stall?) for %s %s — "
                    "proceeding to submit the protective close",
                    stop.strategy_code,
                    stop.symbol,
                )
                has_native_guard = False
            if has_native_guard:
                stop.last_trigger_attempt_at = utcnow()
                return
        stop.last_trigger_attempt_at = utcnow()
        stop.close_in_flight = True
        self.logger.info(
            "[HARD-STOP TRIGGERED] %s %s qty=%s stop=%.4f trigger=%.4f source=%s -> submitting close",
            stop.strategy_code,
            stop.symbol,
            stop.quantity,
            float(stop.stop_price),
            float(trigger_price),
            trigger_source,
        )
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
                _popkey = self._hard_stop_key(stop.strategy_code, stop.broker_account_name, stop.symbol)
                self._armed_hard_stops.pop(_popkey, None)
                if self._armed_stop_persistence_enabled:
                    self._armed_stop_dirty.add(_popkey)  # F2: flush deletes the mirror row
            return
        stop.close_in_flight = False
        if any(item.payload.reason in self.NO_POSITION_REASONS for item in order_events):
            _popkey = self._hard_stop_key(stop.strategy_code, stop.broker_account_name, stop.symbol)
            self._armed_hard_stops.pop(_popkey, None)
            if self._armed_stop_persistence_enabled:
                self._armed_stop_dirty.add(_popkey)  # F2: flush deletes the mirror row

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
        # F2: this call may mutate the registry for `key` — mark it for durable mirroring.
        # Over-marking (a no-op path) is harmless: the flush reflects the ACTUAL dict state
        # (upsert if present, delete if absent). The dict remains the source of truth.
        if self._armed_stop_persistence_enabled:
            self._armed_stop_dirty.add(key)
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
            # Trailing-stop ratchet (ORB TRAIL-8%). Absent metadata => trail_pct 0.0
            # => fixed stop, byte-identical to prior behavior. On a scale-in we
            # preserve the existing ratchet (don't reset the HWM or lower the stop).
            try:
                trail_pct = float(metadata.get("trail_pct", 0) or 0)
            except (TypeError, ValueError):
                trail_pct = 0.0
            if existing is not None and trail_pct <= 0:
                trail_pct = float(existing.trail_pct)
            if trail_pct > 0:
                prior_hwm = (
                    existing.high_water_mark
                    if existing is not None and existing.high_water_mark is not None
                    else entry_price
                )
                high_water_mark: Decimal | None = max(entry_price, prior_hwm)
                if existing is not None and existing.stop_price > stop_price:
                    stop_price = existing.stop_price
            else:
                high_water_mark = None
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
                trail_pct=trail_pct,
                high_water_mark=high_water_mark,
            )
            self.logger.info(
                "[HARD-STOP ARMED] %s %s qty=%s entry=%.4f stop=%.4f stop_loss_pct=%s trail_pct=%s",
                strategy_code,
                normalized_symbol,
                total_quantity,
                float(entry_price),
                float(stop_price),
                stop_loss_pct,
                trail_pct,
            )
            return

        existing = self._armed_hard_stops.get(key)
        if existing is None or quantity <= 0:
            return
        if str(side).lower() == "sell":
            remaining_quantity = max(Decimal("0"), existing.quantity - quantity)
            if remaining_quantity <= 0:
                self._armed_hard_stops.pop(key, None)
                self.logger.info(
                    "[HARD-STOP CLEARED] %s %s (position flat)",
                    strategy_code,
                    normalized_symbol,
                )
                return
            existing.quantity = remaining_quantity
            self.logger.info(
                "[HARD-STOP DECREMENT] %s %s remaining_qty=%s",
                strategy_code,
                normalized_symbol,
                remaining_quantity,
            )

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
        if self._armed_stop_persistence_enabled:
            self._armed_stop_dirty.add(key)  # F2: mirror the resulting state (see _from_fill)
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

    def _orb_quote_priced_entry_applies(self, event: TradeIntentEvent) -> bool:
        """Piece 1 gate: only the flag-on ORB entry buy with the quote-priced contract
        (order_type=limit + price_source=ask). Everything else is a no-op -> byte-identical."""
        md = event.payload.metadata
        return (
            bool(getattr(self.settings, "orb_oms_quote_priced_entry_enabled", False))
            and event.payload.strategy_code == "orb"
            and event.payload.intent_type == "open"
            and event.payload.side == "buy"
            and str(md.get("order_type", "")).lower() == "limit"
            and str(md.get("price_source", "")).lower() == "ask"
        )

    def _fresh_ask(self, symbol: str, max_age_ms: int) -> float | None:
        """The live ask from the OMS quote book (Polygon NBBO) if fresh enough, else None.
        NOTE (standing): no Webull quote entitlement -> ORB prices/stops off Polygon
        consolidated NBBO while executing on Webull; first suspect if thin-name fills look off."""
        quote = self._latest_quotes_by_symbol.get(symbol)
        if not quote:
            return None
        received_at = quote.get("received_at")
        ask = quote.get("ask")
        if ask in (None, 0) or not isinstance(received_at, datetime):
            return None
        if (utcnow() - received_at).total_seconds() * 1000.0 > max(0, max_age_ms):
            return None
        ask_f = float(ask)
        return ask_f if ask_f > 0 else None

    def _abandon_orb_entry(
        self,
        *,
        event: TradeIntentEvent,
        intent: TradeIntent,
        reason_code: str,
        reason_detail: str,
    ) -> OrderEventEvent:
        """Pre-submission abandon for the quote-priced ORB entry (no broker order exists yet).
        Stamps the reason onto the intent metadata for later winners-missed vs fakeouts-dodged
        analysis, marks the intent rejected, logs [OMS-ABANDON-INTENT], and returns the event."""
        md = event.payload.metadata
        md["abandon_intent"] = "true"
        md["abandon_reason_code"] = reason_code
        md["abandon_reason_detail"] = reason_detail
        md["oms_quote_priced"] = "abandoned"
        self.store.mark_intent_status(intent, "rejected")
        self.logger.info(
            "[OMS-ABANDON-INTENT] code=%s symbol=%s strategy=%s side=%s reason=%s",
            reason_code,
            event.payload.symbol,
            event.payload.strategy_code,
            event.payload.side,
            reason_detail,
        )
        return self._build_rejected_event(event, intent.id, reason=reason_code)

    def _apply_orb_quote_priced_entry(
        self,
        *,
        session: Session,
        event: TradeIntentEvent,
        intent: TradeIntent,
    ) -> OrderEventEvent | None:
        """Piece 1: price the ORB entry limit off the OMS's own live quote at placement.

        Returns None to PROCEED (after mutating the limit in event.payload.metadata), or a
        rejected OrderEventEvent to ABANDON (short-circuit before any broker submit). No-op
        (returns None, no mutation) when the flag is off or the intent is not a quote-priced
        ORB entry -> byte-identical. ``session`` is unused today but kept for symmetry with
        the other pre-submit helpers and future per-symbol lookups.
        """
        del session  # reserved; abandon marks intent in the caller's open session
        if not self._orb_quote_priced_entry_applies(event):
            return None
        md = event.payload.metadata
        symbol = str(event.payload.symbol).upper()
        # Bound base is mandatory (fail-closed): without it we cannot bound the chase.
        try:
            break_level = float(md["orb_intended_break_level"])
        except (KeyError, TypeError, ValueError):
            return self._abandon_orb_entry(
                event=event, intent=intent, reason_code="MISSING_BOUND",
                reason_detail="orb_intended_break_level absent/invalid; cannot bound quote-priced entry",
            )
        if break_level <= 0:
            return self._abandon_orb_entry(
                event=event, intent=intent, reason_code="MISSING_BOUND",
                reason_detail=f"orb_intended_break_level non-positive ({break_level})",
            )
        try:
            gap_cap_pct = float(md.get("orb_gap_cap_pct", 0.0))
        except (TypeError, ValueError):
            gap_cap_pct = 0.0
        bound = break_level * (1.0 + gap_cap_pct / 100.0)
        max_age_ms = int(getattr(self.settings, "orb_oms_quote_priced_max_age_ms", 2000))
        ask = self._fresh_ask(symbol, max_age_ms)
        if ask is None:
            return self._abandon_orb_entry(
                event=event, intent=intent, reason_code="NO_FRESH_QUOTE",
                reason_detail=f"no fresh ask within {max_age_ms}ms for {symbol}",
            )
        if ask > bound:
            return self._abandon_orb_entry(
                event=event, intent=intent, reason_code="ASK_PAST_GAP_CAP",
                reason_detail=(
                    f"ask {ask:.4f} past gap-cap bound {bound:.4f} "
                    f"(break {break_level:.4f} +{gap_cap_pct}%)"
                ),
            )
        # ask <= bound: marketable buy limit at ask + 1 tick, never exceeding the bound (Q3).
        tick = Decimal("0.01") if ask >= 1.0 else Decimal("0.0001")
        limit = min(Decimal(str(ask)) + tick, Decimal(str(bound)))
        # ROUND_DOWN so tick-alignment can never push the limit back above the gap-cap bound.
        limit_s = format(limit.quantize(tick, rounding=ROUND_DOWN), "f")
        md["limit_price"] = limit_s
        md["reference_price"] = limit_s
        md["oms_quote_priced"] = "true"
        md["oms_quote_ask"] = f"{ask:.4f}"
        md["oms_quote_bound"] = f"{bound:.4f}"
        self.logger.info(
            "[OMS-ORB-QUOTE-PRICED] symbol=%s ask=%.4f break=%.4f bound=%.4f limit=%s",
            symbol, ask, break_level, bound, limit_s,
        )
        return None

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
                reject_reason=report.reason if report.event_type == "rejected" else None,
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
                self._apply_managed_position_after_fill(
                    session=session,
                    strategy_code=intent_event.payload.strategy_code,
                    broker_account_name=intent_event.payload.broker_account_name,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=fill.quantity,
                    price=fill.price,
                    metadata=dict(request.metadata),
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

    # ----- Stuck-intent cancellation (2026-05-18 incident) ----------------
    # AUUD/QNCX/SBFM pre-market intents at 09:27 ET kept retrying for 4.5
    # hours and 400+ attempts each. Three guards stop that:
    #   Tier 1 (quote-driven): _cancel_drifted_working_orders cancels a
    #          working limit on the very next quote tick when the ask
    #          (buy) / bid (sell) has moved past the limit by more than
    #          the configured tolerance. Fires within ms of the quote
    #          update; no retry.
    #   Tier 2 (age cap): _intent_too_old marks an intent as abandoned
    #          once it has been open longer than
    #          oms_intent_max_age_seconds (default 30s). Belt-and-braces
    #          for stocks that stop quoting entirely.
    #   Tier 3 (setup revalidation): _intent_setup_invalid_reason checks
    #          strategy_bar_history for the latest bar of the intent's
    #          symbol+strategy; if the bar is no longer status=signal
    #          with the same path, the intent is abandoned. Prevents
    #          buying on a setup that has expired since the original
    #          intent fired.

    def _intent_max_age_secs(self) -> int:
        return max(0, int(getattr(self.settings, "oms_intent_max_age_seconds", 0) or 0))

    def _quote_drift_tolerance_dollars(self) -> float:
        return max(
            0.0,
            float(getattr(self.settings, "oms_quote_drift_cancel_tolerance_cents", 0.0) or 0.0),
        ) / 100.0

    @staticmethod
    def _normalize_intent_created_at(intent: TradeIntent) -> datetime | None:
        created = intent.created_at
        if created is None:
            return None
        return created if created.tzinfo is not None else created.replace(tzinfo=UTC)

    def _intent_age_secs(self, intent: TradeIntent) -> float:
        created = self._normalize_intent_created_at(intent)
        if created is None:
            return 0.0
        return max(0.0, (utcnow() - created).total_seconds())

    def _intent_too_old(self, intent: TradeIntent) -> bool:
        max_age = self._intent_max_age_secs()
        if max_age <= 0:
            return False
        return self._intent_age_secs(intent) > max_age

    def _intent_path(self, intent: TradeIntent) -> str:
        payload = intent.payload if isinstance(intent.payload, dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return str(metadata.get("path", "")).strip()

    def _intent_setup_invalid_reason(
        self,
        session: Session,
        *,
        intent: TradeIntent,
        strategy: Strategy | None,
    ) -> str | None:
        if not bool(getattr(self.settings, "oms_intent_setup_revalidation_enabled", True)):
            return None
        if str(intent.intent_type).lower() != "open":
            return None
        if strategy is None:
            return None
        intent_path = self._intent_path(intent)
        if not intent_path:
            return None
        record = session.scalar(
            select(StrategyBarHistory)
            .where(
                StrategyBarHistory.strategy_code == strategy.code,
                StrategyBarHistory.symbol == intent.symbol,
            )
            .order_by(StrategyBarHistory.bar_time.desc())
            .limit(1)
        )
        if record is None:
            return None
        decision_status = str(record.decision_status or "").strip()
        decision_path = str(record.decision_path or "").strip()
        if not decision_status:
            # FAIL OPEN: the strategy records no decision tape. The isolated
            # schwab_1m_v2 bot persists OHLCV bars but never writes
            # decision_status/decision_path, so this revalidation can only judge
            # tape-writing strategies (the momentum bots it was built for). For a
            # tape-less strategy every bar reads as 'idle' != 'signal', which made
            # this guard ABANDON every v2 ATR-Flip intent that did not fill
            # instantly — i.e. all after-hours fills (thin liquidity -> the order
            # reaches the cancel-and-replace cycle -> SETUP_INVALID). We cannot
            # revalidate what isn't recorded, so do NOT abandon a good order.
            return None
        if decision_status == "signal" and decision_path == intent_path:
            return None
        bar_et = record.bar_time.astimezone(SESSION_TZ).strftime("%H:%M:%S") if record.bar_time else "?"
        return (
            f"latest bar {bar_et} ET status={decision_status or 'idle'} "
            f"path={decision_path or 'none'} != intent path={intent_path}"
        )

    def _quote_drift_dollars_against(
        self,
        order: BrokerOrder,
        quote: dict[str, object],
    ) -> float | None:
        if self._is_stop_guard_order(order):
            return None
        payload = order.payload or {}
        if str(payload.get("order_type", "")).strip().lower() != "limit":
            return None
        try:
            limit_price = float(str(payload.get("limit_price", "")).strip())
        except (TypeError, ValueError):
            return None
        if limit_price <= 0:
            return None
        side = str(order.side).lower()
        if side == "buy":
            ask = quote.get("ask")
            if not isinstance(ask, (int, float)) or ask <= 0:
                return None
            return float(ask) - limit_price
        if side == "sell":
            bid = quote.get("bid")
            if not isinstance(bid, (int, float)) or bid <= 0:
                return None
            return limit_price - float(bid)
        return None

    async def _cancel_working_order_and_abandon_intent(
        self,
        *,
        session: Session,
        order: BrokerOrder,
        intent: TradeIntent,
        strategy: Strategy | None,
        broker_account: BrokerAccount,
        reason_code: str,
        reason_detail: str,
    ) -> list[OrderEventEvent]:
        existing_metadata = {str(k): str(v) for k, v in (order.payload or {}).items()}
        cancel_request = OrderRequest(
            client_order_id=order.client_order_id,
            broker_account_name=broker_account.name,
            strategy_code=strategy.code if strategy is not None else "",
            symbol=order.symbol,
            side=order.side,  # type: ignore[arg-type]
            intent_type="cancel",
            quantity=order.quantity,
            reason=reason_code,
            metadata={
                **existing_metadata,
                "broker_order_id": order.broker_order_id or "",
                "target_client_order_id": order.client_order_id,
                "abandon_intent": "true",
                "abandon_reason_code": reason_code,
                "abandon_reason_detail": reason_detail,
            },
            order_type=order.order_type,
            time_in_force=order.time_in_force,
        )
        cancel_reports = await self.broker_adapter.submit_order(cancel_request)
        cancelled_report = next(
            (item for item in cancel_reports if item.event_type == "cancelled"),
            None,
        )
        if cancelled_report is not None:
            cancel_metadata = {
                **existing_metadata,
                **{str(k): str(v) for k, v in cancelled_report.metadata.items()},
                "abandon_intent": "true",
                "abandon_reason_code": reason_code,
                "abandon_reason_detail": reason_detail,
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
                    "internal": reason_code,
                },
            )
        self.store.mark_intent_status(intent, "cancelled")
        self.logger.info(
            "[OMS-ABANDON-INTENT] code=%s symbol=%s strategy=%s side=%s "
            "intent_age_s=%.1f limit=%s reason=%s",
            reason_code,
            order.symbol,
            strategy.code if strategy is not None else "?",
            order.side,
            self._intent_age_secs(intent),
            str((order.payload or {}).get("limit_price", "")),
            reason_detail,
        )
        return []

    async def _cancel_drifted_working_orders(self, symbol: str) -> None:
        """Tier 1: cancel working limit orders the instant the quote drifts past the limit.

        PR-A off-load: the candidate READ and the cancel WRITE-BACK both run OFF the
        event loop via ``_run_db`` — this path mutates no in-memory dict, so it splits
        cleanly (unlike the v2 exit-emit). Only the per-order broker cancel await stays
        on-loop. Broker-agnostic: covers ORB (Webull) and v2 (Schwab) working limits."""
        tolerance_dollars = self._quote_drift_tolerance_dollars()
        if tolerance_dollars <= 0:
            return
        quote = self._latest_quotes_by_symbol.get(symbol.upper())
        if not quote:
            return
        try:
            await self._run_drift_cancel(symbol.upper(), quote, tolerance_dollars)
        except Exception as exc:  # noqa: BLE001 — the quote path must never die; a stall here
            # must NEVER skip the downstream v2 hard-stop eval that runs later in the same
            # quote handler (loop-hardening; the happy path is unchanged).
            self.logger.warning("quote-drift cancel failed for %s: %s", symbol, exc)

    async def _run_drift_cancel(self, symbol: str, quote: dict, tolerance_dollars: float) -> None:
        """The drift-cancel phases (off-loop read -> on-loop broker cancels -> off-loop
        write-back), split out so ``_cancel_drifted_working_orders`` can wrap them in the
        never-die guard. ``symbol`` arrives already upper-cased."""
        # Phase 1 — READ (off-loop): drift-eligible candidates as plain snapshots.
        candidates = await self._run_db(
            lambda session: self._collect_drift_cancel_candidates(
                session, symbol, quote, tolerance_dollars
            ),
            commit=False,
        )
        if not candidates:
            return
        # Phase 2 — BROKER (on-loop): submit each cancel, collect the reports.
        results: list[tuple[_DriftCancelCandidate, ExecutionReport | None, str]] = []
        for candidate in candidates:
            reason_detail = (
                f"quote drift {candidate.drift * 100:.1f}c past limit "
                f"(tolerance {tolerance_dollars * 100:.1f}c); ask/bid moved away"
            )
            cancel_request = OrderRequest(
                client_order_id=candidate.client_order_id,
                broker_account_name=candidate.broker_account_name,
                strategy_code=candidate.strategy_code,
                symbol=candidate.symbol,
                side=candidate.side,  # type: ignore[arg-type]
                intent_type="cancel",
                quantity=candidate.quantity,
                reason="QUOTE_DRIFT_CANCEL",
                metadata={
                    **candidate.existing_metadata,
                    "broker_order_id": candidate.broker_order_id,
                    "target_client_order_id": candidate.client_order_id,
                    "abandon_intent": "true",
                    "abandon_reason_code": "QUOTE_DRIFT_CANCEL",
                    "abandon_reason_detail": reason_detail,
                },
                order_type=candidate.order_type,
                time_in_force=candidate.time_in_force,
            )
            cancel_reports = await self.broker_adapter.submit_order(cancel_request)
            cancelled_report = next(
                (item for item in cancel_reports if item.event_type == "cancelled"), None
            )
            results.append((candidate, cancelled_report, reason_detail))
        # Phase 3 — WRITE-BACK (off-loop): record cancels + always abandon the intents.
        await self._run_db(
            lambda session: self._apply_drift_cancel_writes(session, results), commit=True
        )
        # Logging on-loop — parity with the prior [OMS-ABANDON-INTENT] line (always emitted).
        for candidate, _report, reason_detail in results:
            self.logger.info(
                "[OMS-ABANDON-INTENT] code=%s symbol=%s strategy=%s side=%s "
                "intent_age_s=%.1f limit=%s reason=%s",
                "QUOTE_DRIFT_CANCEL",
                candidate.symbol,
                candidate.strategy_code or "?",
                candidate.side,
                self._drift_candidate_intent_age_secs(candidate),
                candidate.limit_price,
                reason_detail,
            )

    def _collect_drift_cancel_candidates(
        self, session: Session, symbol: str, quote: dict, tolerance_dollars: float
    ) -> list[_DriftCancelCandidate]:
        """Off-loop READ unit: working orders for `symbol` whose quote has drifted past
        the limit beyond tolerance, as plain snapshots (no ORM crosses the thread).
        Mirrors the prior in-line filter exactly: open-intent only; stop-guard / non-limit
        orders are excluded by ``_quote_drift_dollars_against`` returning None."""
        orders = session.scalars(
            select(BrokerOrder)
            .where(BrokerOrder.status.in_(self.store.OPEN_ORDER_STATUSES))
            .where(BrokerOrder.symbol == symbol)
        ).all()
        if not orders:
            return []
        account_lookup = {
            account.id: account for account in self.store.list_active_broker_accounts(session)
        }
        strategy_lookup = {
            strategy.id: strategy for strategy in session.scalars(select(Strategy)).all()
        }
        candidates: list[_DriftCancelCandidate] = []
        for order in orders:
            if order.intent_id is None:
                continue
            drift = self._quote_drift_dollars_against(order, quote)
            if drift is None or drift <= tolerance_dollars:
                continue
            intent = session.get(TradeIntent, order.intent_id)
            if intent is None:
                continue
            if str(intent.intent_type).lower() != "open":
                continue  # don't auto-cancel close/scale chases here
            account = account_lookup.get(order.broker_account_id)
            if account is None:
                continue
            strategy = strategy_lookup.get(order.strategy_id)
            candidates.append(
                _DriftCancelCandidate(
                    order_id=order.id,
                    intent_id=order.intent_id,
                    client_order_id=order.client_order_id,
                    broker_account_name=account.name,
                    strategy_code=(strategy.code if strategy is not None else ""),
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    order_type=order.order_type,
                    time_in_force=order.time_in_force,
                    existing_metadata={str(k): str(v) for k, v in (order.payload or {}).items()},
                    broker_order_id=order.broker_order_id or "",
                    limit_price=str((order.payload or {}).get("limit_price", "")),
                    intent_created_at=intent.created_at,
                    drift=drift,
                )
            )
        return candidates

    def _apply_drift_cancel_writes(
        self,
        session: Session,
        results: list[tuple[_DriftCancelCandidate, ExecutionReport | None, str]],
    ) -> None:
        """Off-loop WRITE unit: for each drift-cancel candidate, record the broker cancel
        report (when one was returned) and ALWAYS abandon the intent — byte-for-byte the
        DB writes the prior ``_cancel_working_order_and_abandon_intent`` performed, minus
        its (now on-loop) broker await and logging. Re-fetches order/intent by id."""
        for candidate, cancelled_report, reason_detail in results:
            order = session.get(BrokerOrder, candidate.order_id)
            intent = session.get(TradeIntent, candidate.intent_id)
            if intent is None:
                continue
            if cancelled_report is not None and order is not None:
                cancel_metadata = {
                    **candidate.existing_metadata,
                    **{str(k): str(v) for k, v in cancelled_report.metadata.items()},
                    "abandon_intent": "true",
                    "abandon_reason_code": "QUOTE_DRIFT_CANCEL",
                    "abandon_reason_detail": reason_detail,
                }
                self.store.update_order_from_report(
                    order, report=cancelled_report, metadata=cancel_metadata
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
                        "internal": "QUOTE_DRIFT_CANCEL",
                    },
                )
            self.store.mark_intent_status(intent, "cancelled")

    def _drift_candidate_intent_age_secs(self, candidate: _DriftCancelCandidate) -> float:
        created = candidate.intent_created_at
        if created is None:
            return 0.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return max(0.0, (utcnow() - created).total_seconds())

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
        is_stop_guard_close = (
            request.intent_type == "close"
            and str(request.metadata.get("stop_guard", "")).strip().lower() == "true"
        )
        if request.intent_type not in {"open", "scale"} and not is_stop_guard_close:
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
