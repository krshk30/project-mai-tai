from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI
from pydantic import BaseModel, Field
from redis.asyncio import Redis
import uvicorn

from project_mai_tai.events import HeartbeatEvent, HeartbeatPayload, StrategyStateSnapshotEvent, stream_name
from project_mai_tai.log import configure_logging
from project_mai_tai.services.runtime import _install_signal_handlers
from project_mai_tai.services.strategy_engine_app import current_scanner_session_start_utc
from project_mai_tai.services.tradingview_notifications import (
    TradingViewAlertNotifier,
    build_tradingview_alert_notifier,
)
from project_mai_tai.services.tradingview_playwright import (
    PlaywrightTradingViewAlertOperator,
    describe_message_template,
)
from project_mai_tai.settings import Settings, get_settings


SERVICE_NAME = "tradingview-alerts"


def utcnow() -> datetime:
    return datetime.now(UTC)


logger = logging.getLogger(__name__)


class AlertSyncRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    source: str = "manual"


class AlertSymbolRequest(BaseModel):
    symbol: str
    source: str = "manual"


@dataclass(frozen=True)
class AlertSyncPlan:
    desired_symbols: list[str]
    symbols_to_add: list[str]
    symbols_to_remove: list[str]
    unchanged_symbols: list[str]


def normalize_symbols(symbols: list[str] | set[str] | tuple[str, ...]) -> list[str]:
    normalized = {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}
    return sorted(normalized)


def build_alert_sync_plan(*, desired_symbols: list[str], current_symbols: list[str]) -> AlertSyncPlan:
    desired = set(normalize_symbols(desired_symbols))
    current = set(normalize_symbols(current_symbols))
    return AlertSyncPlan(
        desired_symbols=sorted(desired),
        symbols_to_add=sorted(desired - current),
        symbols_to_remove=sorted(current - desired),
        unchanged_symbols=sorted(current & desired),
    )


@dataclass
class StoredAlertState:
    session_start_utc: str | None = None
    managed_symbols: list[str] = field(default_factory=list)
    desired_symbols: list[str] = field(default_factory=list)
    requested_symbols: list[str] = field(default_factory=list)
    protected_symbols: list[str] = field(default_factory=list)
    last_source: str = "bootstrap"
    last_synced_at: str | None = None
    last_error: str | None = None
    last_strategy_event_id: str | None = None
    last_relogin_notification_at: str | None = None
    provider: str = "log_only"


class TradingViewAlertStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> StoredAlertState:
        if not self.path.exists():
            return StoredAlertState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("failed to load tradingview alert state from %s", self.path)
            return StoredAlertState()
        if not isinstance(payload, dict):
            return StoredAlertState()
        return StoredAlertState(
            session_start_utc=str(payload["session_start_utc"]) if payload.get("session_start_utc") else None,
            managed_symbols=normalize_symbols(payload.get("managed_symbols", [])),
            desired_symbols=normalize_symbols(payload.get("desired_symbols", [])),
            requested_symbols=normalize_symbols(payload.get("requested_symbols", [])),
            protected_symbols=normalize_symbols(payload.get("protected_symbols", [])),
            last_source=str(payload.get("last_source", "bootstrap")),
            last_synced_at=str(payload["last_synced_at"]) if payload.get("last_synced_at") else None,
            last_error=str(payload["last_error"]) if payload.get("last_error") else None,
            last_strategy_event_id=(
                str(payload["last_strategy_event_id"]) if payload.get("last_strategy_event_id") else None
            ),
            last_relogin_notification_at=(
                str(payload["last_relogin_notification_at"])
                if payload.get("last_relogin_notification_at")
                else None
            ),
            provider=str(payload.get("provider", "log_only")),
        )

    def save(self, state: StoredAlertState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True), encoding="utf-8")


class TradingViewAlertOperator(Protocol):
    async def add_alert(self, symbol: str) -> None: ...

    async def remove_alert(self, symbol: str) -> None: ...

    async def status(self) -> dict[str, object]: ...

    async def close(self) -> None: ...


class LoggingTradingViewAlertOperator:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def add_alert(self, symbol: str) -> None:
        logger.info(
            "TradingView add requested | symbol=%s operator=%s chart_url=%s",
            symbol,
            self.settings.tradingview_alerts_operator,
            self.settings.tradingview_alerts_chart_url,
        )

    async def remove_alert(self, symbol: str) -> None:
        logger.info(
            "TradingView remove requested | symbol=%s operator=%s chart_url=%s",
            symbol,
            self.settings.tradingview_alerts_operator,
            self.settings.tradingview_alerts_chart_url,
        )

    async def status(self) -> dict[str, object]:
        return {
            "operator": "log_only",
            "ready": bool(self.settings.tradingview_alerts_enabled),
            "auth_required": False,
            "auth_reason": None,
            "note": "Browser automation is not wired yet; requests are logged and persisted.",
            "message_template": describe_message_template(self.settings),
        }

    async def close(self) -> None:
        return None


def build_tradingview_alert_operator(settings: Settings) -> TradingViewAlertOperator:
    operator_name = settings.tradingview_alerts_operator.strip().lower()
    if operator_name == "playwright":
        return PlaywrightTradingViewAlertOperator(settings)
    return LoggingTradingViewAlertOperator(settings)


class TradingViewAlertService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        redis: Redis | None = None,
        state_store: TradingViewAlertStateStore | None = None,
        operator: TradingViewAlertOperator | None = None,
        notifier: TradingViewAlertNotifier | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.logger = configure_logging(SERVICE_NAME, self.settings.log_level)
        self.redis = redis or Redis.from_url(self.settings.redis_url, decode_responses=True)
        self._owns_redis = redis is None
        self.state_store = state_store or TradingViewAlertStateStore(self.settings.tradingview_alerts_state_path)
        self.operator = operator or build_tradingview_alert_operator(self.settings)
        self.notifier = notifier or build_tradingview_alert_notifier(self.settings)
        self._state = self.state_store.load()
        self._active_session_start_utc = current_scanner_session_start_utc().isoformat()
        self._stream = stream_name(self.settings.redis_stream_prefix, "strategy-state")
        self._stream_offsets = {self._stream: "$"}
        self._sync_lock = asyncio.Lock()
        self._last_plan = build_alert_sync_plan(
            desired_symbols=self._state.desired_symbols,
            current_symbols=self._state.managed_symbols,
        )
        self._last_heartbeat_at: str | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def state(self) -> StoredAlertState:
        return self._state

    async def start(self) -> None:
        if self._worker_task is not None:
            return
        try:
            await self._bootstrap_from_latest_strategy_state()
        except Exception:
            self.logger.exception("failed to bootstrap TradingView alerts during startup")
        self._stop_event = asyncio.Event()
        _install_signal_handlers(self._stop_event)
        self._worker_task = asyncio.create_task(self._run_worker(), name="tradingview-alert-worker")

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._worker_task is not None:
            await self._worker_task
            self._worker_task = None
        await self.operator.close()
        if self._owns_redis:
            await self.redis.aclose()

    async def _run_worker(self) -> None:
        assert self._stop_event is not None
        heartbeat_interval_secs = max(1, self.settings.service_heartbeat_interval_seconds)
        while not self._stop_event.is_set():
            try:
                messages = await self.redis.xread(
                    self._stream_offsets,
                    block=heartbeat_interval_secs * 1000,
                    count=50,
                )
            except Exception:
                self.logger.exception("strategy-state xread failed")
                await asyncio.sleep(1)
                continue

            if not messages:
                await self._publish_heartbeat("healthy")
                continue

            for stream, entries in messages:
                for message_id, fields in entries:
                    self._stream_offsets[stream] = message_id
                    raw_event = fields.get("data")
                    if raw_event is None:
                        continue
                    try:
                        event = StrategyStateSnapshotEvent.model_validate_json(raw_event)
                    except Exception:
                        self.logger.exception("failed to parse strategy-state event")
                        continue
                    if not self.settings.tradingview_alerts_auto_sync_enabled:
                        continue
                    try:
                        await self.sync_watchlist(
                            [str(symbol) for symbol in event.payload.watchlist],
                            source=f"strategy-state:{event.source_service}",
                            strategy_event_id=str(event.event_id),
                            protected_symbols=self._protected_symbols_from_strategy_event(event),
                        )
                    except Exception:
                        self.logger.exception(
                            "failed to sync TradingView alerts from strategy-state event | event_id=%s source=%s",
                            event.event_id,
                            event.source_service,
                        )

    async def _bootstrap_from_latest_strategy_state(self) -> None:
        if not self.settings.tradingview_alerts_auto_sync_enabled:
            return
        try:
            entries = await self.redis.xrevrange(self._stream, count=1)
        except Exception:
            self.logger.exception("failed to bootstrap tradingview alerts from latest strategy-state")
            return
        if not entries:
            return

        message_id, fields = entries[0]
        self._stream_offsets[self._stream] = message_id
        raw_event = fields.get("data")
        if raw_event is None:
            return
        try:
            event = StrategyStateSnapshotEvent.model_validate_json(raw_event)
        except Exception:
            self.logger.exception("failed to parse bootstrap strategy-state event")
            return

        desired_symbols = [str(symbol) for symbol in event.payload.watchlist]
        self.logger.info(
            "bootstrapping TradingView alerts from latest strategy-state | symbols=%s source=%s event_id=%s",
            len(desired_symbols),
            event.source_service,
            event.event_id,
        )
        await self.sync_watchlist(
            desired_symbols,
            source=f"strategy-state-bootstrap:{event.source_service}",
            strategy_event_id=str(event.event_id),
            protected_symbols=self._protected_symbols_from_strategy_event(event),
        )

    async def sync_watchlist(
        self,
        symbols: list[str],
        *,
        source: str,
        strategy_event_id: str | None = None,
        protected_symbols: list[str] | None = None,
    ) -> AlertSyncPlan:
        requested = normalize_symbols(symbols)
        protected = normalize_symbols(protected_symbols or [])
        sticky = self._sticky_managed_symbols_for_source(source)
        desired = normalize_symbols([*requested, *protected, *sticky])
        async with self._sync_lock:
            plan = build_alert_sync_plan(
                desired_symbols=desired,
                current_symbols=self._state.managed_symbols,
            )
            self.logger.info(
                "syncing TradingView alerts | source=%s requested=%s protected=%s add=%s remove=%s unchanged=%s",
                source,
                len(requested),
                len(protected),
                len(plan.symbols_to_add),
                len(plan.symbols_to_remove),
                len(plan.unchanged_symbols),
            )
            try:
                for symbol in plan.symbols_to_add:
                    await self.operator.add_alert(symbol)
                for symbol in plan.symbols_to_remove:
                    await self.operator.remove_alert(symbol)
            except Exception as exc:
                self._state.session_start_utc = self._active_session_start_utc
                self._state.desired_symbols = desired
                self._state.requested_symbols = requested
                self._state.protected_symbols = protected
                self._state.last_source = source
                self._state.last_error = str(exc)
                self._state.last_strategy_event_id = strategy_event_id
                self.state_store.save(self._state)
                await self._maybe_notify_relogin_required()
                raise

            self._state.session_start_utc = self._active_session_start_utc
            self._state.managed_symbols = list(plan.desired_symbols)
            self._state.desired_symbols = list(plan.desired_symbols)
            self._state.requested_symbols = list(requested)
            self._state.protected_symbols = list(protected)
            self._state.last_source = source
            self._state.last_error = None
            self._state.last_synced_at = utcnow().isoformat()
            self._state.last_strategy_event_id = strategy_event_id
            self._state.provider = self.settings.tradingview_alerts_operator
            self.state_store.save(self._state)
            self._last_plan = plan
            return plan

    async def add_symbol(self, symbol: str, *, source: str) -> AlertSyncPlan:
        desired = set(self._state.desired_symbols)
        desired.add(str(symbol).strip().upper())
        return await self.sync_watchlist(sorted(desired), source=source)

    async def remove_symbol(self, symbol: str, *, source: str) -> AlertSyncPlan:
        target = str(symbol).strip().upper()
        desired = {item for item in self._state.desired_symbols if item != target}
        return await self.sync_watchlist(sorted(desired), source=source)

    async def status_payload(self) -> dict[str, object]:
        operator_status = await self.operator.status()
        return {
            "service": SERVICE_NAME,
            "enabled": self.settings.tradingview_alerts_enabled,
            "auto_sync_enabled": self.settings.tradingview_alerts_auto_sync_enabled,
            "provider": self.settings.tradingview_alerts_operator,
            "session_start_utc": self._state.session_start_utc,
            "managed_symbols": list(self._state.managed_symbols),
            "desired_symbols": list(self._state.desired_symbols),
            "requested_symbols": list(self._state.requested_symbols),
            "protected_symbols": list(self._state.protected_symbols),
            "last_source": self._state.last_source,
            "last_synced_at": self._state.last_synced_at,
            "last_error": self._state.last_error,
            "last_strategy_event_id": self._state.last_strategy_event_id,
            "last_relogin_notification_at": self._state.last_relogin_notification_at,
            "last_plan": {
                "desired_symbols": list(self._last_plan.desired_symbols),
                "symbols_to_add": list(self._last_plan.symbols_to_add),
                "symbols_to_remove": list(self._last_plan.symbols_to_remove),
                "unchanged_symbols": list(self._last_plan.unchanged_symbols),
            },
            "state_path": str(self.state_store.path),
            "operator_status": operator_status,
            "notifier_status": await self.notifier.status(),
            "last_heartbeat_at": self._last_heartbeat_at,
        }

    def _protected_symbols_from_strategy_event(self, event: StrategyStateSnapshotEvent) -> list[str]:
        protected: set[str] = set()
        for bot in event.payload.bots:
            protected.update(str(symbol).strip().upper() for symbol in bot.pending_open_symbols if str(symbol).strip())
            protected.update(str(symbol).strip().upper() for symbol in bot.pending_close_symbols if str(symbol).strip())
            for level in bot.pending_scale_levels:
                text = str(level).strip().upper()
                if not text:
                    continue
                if ":" in text:
                    symbol, _suffix = text.split(":", 1)
                    if symbol:
                        protected.add(symbol)
                    continue
                protected.add(text)
            for position in bot.positions:
                if not isinstance(position, dict):
                    continue
                ticker = str(position.get("ticker", "")).strip().upper()
                if ticker:
                    protected.add(ticker)
        return sorted(protected)

    def _sticky_managed_symbols_for_source(self, source: str) -> list[str]:
        normalized_source = str(source).strip().lower()
        if not normalized_source.startswith("strategy-state"):
            return []
        if self._state.session_start_utc != self._active_session_start_utc:
            return []
        return list(self._state.managed_symbols)

    async def _publish_heartbeat(self, status: str) -> None:
        event = HeartbeatEvent(
            source_service=SERVICE_NAME,
            payload=HeartbeatPayload(
                service_name=SERVICE_NAME,
                instance_name="local",
                status=status,
                details={
                    "managed_symbols": str(len(self._state.managed_symbols)),
                    "provider": self.settings.tradingview_alerts_operator,
                },
            ),
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "heartbeats"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_heartbeat_stream_maxlen,
            approximate=True,
        )
        self._last_heartbeat_at = event.produced_at.isoformat()

    async def _maybe_notify_relogin_required(self) -> None:
        operator_status = await self.operator.status()
        if not operator_status.get("auth_required"):
            return
        now = utcnow()
        last_notified_at = self._parse_iso_datetime(self._state.last_relogin_notification_at)
        cooldown_seconds = max(1, self.settings.tradingview_alerts_notification_cooldown_minutes) * 60
        if last_notified_at is not None and (now - last_notified_at).total_seconds() < cooldown_seconds:
            return
        reason = str(operator_status.get("auth_reason") or self._state.last_error or "TradingView login required")
        try:
            await self.notifier.send_relogin_required(reason=reason, operator_status=operator_status)
        except Exception:
            self.logger.exception("failed to send TradingView relogin notification")
            return
        self._state.last_relogin_notification_at = now.isoformat()
        self.state_store.save(self._state)

    def _parse_iso_datetime(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None


def build_app(
    *,
    settings: Settings | None = None,
    service: TradingViewAlertService | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    active_service = service or TradingViewAlertService(settings=active_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await active_service.start()
        try:
            yield
        finally:
            await active_service.stop()

    app = FastAPI(title="TradingView Alert Service", lifespan=lifespan)
    app.state.alert_service = active_service

    @app.get("/health")
    async def health() -> dict[str, object]:
        return await active_service.status_payload()

    @app.get("/alerts/status")
    async def alerts_status() -> dict[str, object]:
        return await active_service.status_payload()

    @app.post("/alerts/sync")
    async def alerts_sync(request: AlertSyncRequest) -> dict[str, object]:
        plan = await active_service.sync_watchlist(request.symbols, source=request.source)
        return {"success": True, **asdict(plan)}

    @app.post("/alerts/add")
    async def alerts_add(request: AlertSymbolRequest) -> dict[str, object]:
        plan = await active_service.add_symbol(request.symbol, source=request.source)
        return {"success": True, **asdict(plan)}

    @app.post("/alerts/remove")
    async def alerts_remove(request: AlertSymbolRequest) -> dict[str, object]:
        plan = await active_service.remove_symbol(request.symbol, source=request.source)
        return {"success": True, **asdict(plan)}

    return app


def run() -> None:
    settings = get_settings()
    app = build_app(settings=settings)
    uvicorn.run(
        app,
        host=settings.tradingview_alerts_host,
        port=settings.tradingview_alerts_port,
        log_level=settings.log_level.lower(),
    )
