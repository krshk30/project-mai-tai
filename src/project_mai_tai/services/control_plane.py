from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from html import escape
import json
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from redis.asyncio import Redis
from sqlalchemy import case, desc, func, select, text
from sqlalchemy.orm import Session, sessionmaker
import uvicorn

from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    Fill,
    ReconciliationFinding,
    ReconciliationRun,
    Strategy,
    SystemIncident,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.events import (
    HeartbeatEvent,
    MarketDataSubscriptionEvent,
    SnapshotBatchEvent,
    StrategyStateSnapshotEvent,
    stream_name,
)
from project_mai_tai.log import configure_logging
from project_mai_tai.runtime_registry import configured_strategy_registrations
from project_mai_tai.shadow import LegacyShadowClient
from project_mai_tai.settings import Settings, get_settings
from project_mai_tai.strategy_core import (
    FivePillarsConfig,
    MomentumAlertConfig,
    MomentumConfirmedConfig,
    TopGainersConfig,
)


SERVICE_NAME = "control-plane"


def utcnow() -> datetime:
    return datetime.now(UTC)


class ControlPlaneRepository:
    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: sessionmaker[Session],
        redis: Redis,
        legacy_client: LegacyShadowClient | None = None,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.redis = redis
        self.legacy_client = legacy_client
        if self.legacy_client is None and self.settings.legacy_api_base_url:
            self.legacy_client = LegacyShadowClient(
                self.settings.legacy_api_base_url,
                timeout_seconds=self.settings.legacy_api_timeout_seconds,
            )
        self._legacy_cache: dict[str, Any] | None = None
        self._legacy_cache_at: datetime | None = None
        self._legacy_cache_lock = asyncio.Lock()

    async def load_dashboard_data(self) -> dict[str, Any]:
        db_state = self._load_database_state()
        stream_state = await self._load_stream_state()
        legacy_shadow = await self._load_legacy_shadow_data(
            strategy_runtime=stream_state["strategy_runtime"],
            recent_intents=db_state["recent_intents"],
        )
        scanner = self._build_scanner_view(
            market_data=stream_state["market_data"],
            strategy_runtime=stream_state["strategy_runtime"],
            legacy_shadow=legacy_shadow,
        )
        bots = self._build_bot_views(
            strategy_runtime=stream_state["strategy_runtime"],
            legacy_shadow=legacy_shadow,
            recent_intents=db_state["recent_intents"],
            recent_orders=db_state["recent_orders"],
            recent_fills=db_state["recent_fills"],
        )

        overall_status = "healthy"
        if db_state["errors"] or stream_state["errors"]:
            overall_status = "degraded"
        elif any(service["status"] not in {"healthy", "starting"} for service in stream_state["services"]):
            overall_status = "degraded"
        elif (
            db_state["reconciliation"]["latest_run"] is not None
            and db_state["reconciliation"]["latest_run"]["summary"].get("total_findings", 0) > 0
        ):
            overall_status = "degraded"

        return {
            "generated_at": utcnow().isoformat(),
            "status": overall_status,
            "environment": self.settings.environment,
            "domain": "project-mai-tai.live",
            "control_plane_url": self.settings.control_plane_base_url,
            "provider": self.settings.broker_default_provider,
            "oms_adapter": self.settings.oms_adapter,
            "scanner_config": {
                "five_pillars": FivePillarsConfig(
                    min_price=self.settings.market_data_scan_min_price,
                    max_price=self.settings.market_data_scan_max_price,
                ).__dict__,
                "top_gainers": TopGainersConfig(
                    min_price=self.settings.market_data_scan_min_price,
                    max_price=self.settings.market_data_scan_max_price,
                ).__dict__,
                "momentum_alerts": MomentumAlertConfig(
                    min_price=self.settings.market_data_scan_min_price,
                    max_price=self.settings.market_data_scan_max_price,
                ).__dict__,
                "momentum_confirmed": MomentumConfirmedConfig().__dict__,
            },
            "streams": {
                "market_data": stream_name(self.settings.redis_stream_prefix, "market-data"),
                "snapshot_batches": stream_name(self.settings.redis_stream_prefix, "snapshot-batches"),
                "market_data_subscriptions": stream_name(
                    self.settings.redis_stream_prefix,
                    "market-data-subscriptions",
                ),
                "strategy_intents": stream_name(self.settings.redis_stream_prefix, "strategy-intents"),
                "order_events": stream_name(self.settings.redis_stream_prefix, "order-events"),
                "heartbeats": stream_name(self.settings.redis_stream_prefix, "heartbeats"),
            },
            "counts": db_state["counts"],
            "services": stream_state["services"],
            "market_data": stream_state["market_data"],
            "scanner": scanner,
            "bots": bots,
            "recent_intents": db_state["recent_intents"],
            "recent_orders": db_state["recent_orders"],
            "recent_fills": db_state["recent_fills"],
            "virtual_positions": db_state["virtual_positions"],
            "account_positions": db_state["account_positions"],
            "reconciliation": db_state["reconciliation"],
            "strategy_runtime": stream_state["strategy_runtime"],
            "legacy_shadow": legacy_shadow,
            "incidents": db_state["incidents"],
            "errors": db_state["errors"] + stream_state["errors"] + legacy_shadow["errors"],
        }

    def _build_scanner_view(
        self,
        *,
        market_data: dict[str, Any],
        strategy_runtime: dict[str, Any],
        legacy_shadow: dict[str, Any],
    ) -> dict[str, Any]:
        bot_states = strategy_runtime.get("bots", {})
        watchlist = [str(symbol) for symbol in strategy_runtime.get("watchlist", [])]
        top_confirmed: list[dict[str, Any]] = []
        for index, item in enumerate(strategy_runtime.get("top_confirmed", []), start=1):
            ticker = str(item.get("ticker", "")).upper()
            watched_by = [
                strategy_code
                for strategy_code, bot in bot_states.items()
                if ticker and ticker in {str(symbol).upper() for symbol in bot.get("watchlist", [])}
            ]
            bid = float(item.get("bid", 0) or 0)
            ask = float(item.get("ask", 0) or 0)
            spread = float(item.get("spread", 0) or 0)
            if spread <= 0 and bid > 0 and ask > 0:
                spread = round(ask - bid, 4)
            top_confirmed.append(
                {
                    **item,
                    "rank": index,
                    "ticker": ticker,
                    "rank_score": float(item.get("rank_score", 0) or 0),
                    "confirmation_path": str(item.get("confirmation_path", "")),
                    "confirmed_at": str(item.get("confirmed_at", "")),
                    "entry_price": float(item.get("entry_price", 0) or 0),
                    "price": float(item.get("price", 0) or 0),
                    "change_pct": float(item.get("change_pct", 0) or 0),
                    "volume": float(item.get("volume", 0) or 0),
                    "rvol": float(item.get("rvol", 0) or 0),
                    "bid": bid,
                    "ask": ask,
                    "bid_size": int(item.get("bid_size", 0) or 0),
                    "ask_size": int(item.get("ask_size", 0) or 0),
                    "spread": spread,
                    "spread_pct": float(item.get("spread_pct", 0) or 0),
                    "squeeze_count": int(item.get("squeeze_count", 0) or 0),
                    "first_spike_time": str(item.get("first_spike_time", "")),
                    "catalyst": str(item.get("catalyst", "")),
                    "headline": str(item.get("headline", "")),
                    "sentiment": str(item.get("sentiment", "")),
                    "news_url": str(item.get("news_url", "")),
                    "news_date": str(item.get("news_date", "")),
                    "watched_by": watched_by,
                    "is_top5": bool(watched_by),
                }
            )

        legacy_confirmed = [
            str(symbol).upper()
            for symbol in legacy_shadow.get("scanner", {}).get("confirmed_symbols", [])
        ]
        return {
            "status": "active" if top_confirmed else "idle",
            "cycle_count": int(strategy_runtime.get("cycle_count", 0) or 0),
            "watchlist": watchlist,
            "watchlist_count": len(watchlist),
            "top_confirmed_count": len(top_confirmed),
            "top_confirmed": top_confirmed,
            "five_pillars": list(strategy_runtime.get("five_pillars", [])),
            "five_pillars_count": len(strategy_runtime.get("five_pillars", [])),
            "top_gainers": list(strategy_runtime.get("top_gainers", [])),
            "top_gainers_count": len(strategy_runtime.get("top_gainers", [])),
            "recent_alerts": list(strategy_runtime.get("recent_alerts", [])),
            "recent_alerts_count": len(strategy_runtime.get("recent_alerts", [])),
            "top_gainer_changes": list(strategy_runtime.get("top_gainer_changes", [])),
            "alert_warmup": dict(strategy_runtime.get("alert_warmup", {})),
            "active_subscription_symbols": int(market_data.get("active_subscription_symbols", 0) or 0),
            "subscription_symbols": list(market_data.get("subscription_symbols", [])),
            "latest_snapshot_batch": market_data.get("latest_snapshot_batch"),
            "legacy_confirmed_symbols": legacy_confirmed,
            "legacy_confirmed_count": len(legacy_confirmed),
        }

    def _build_bot_views(
        self,
        *,
        strategy_runtime: dict[str, Any],
        legacy_shadow: dict[str, Any],
        recent_intents: list[dict[str, Any]],
        recent_orders: list[dict[str, Any]],
        recent_fills: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        registrations = configured_strategy_registrations(self.settings)
        ordered_codes = [registration.code for registration in registrations]
        registration_map = {registration.code: registration for registration in registrations}
        runtime_bots = strategy_runtime.get("bots", {})
        legacy_bots = legacy_shadow.get("bots", {})

        extra_codes = sorted(
            (set(runtime_bots.keys()) | set(legacy_bots.keys())) - set(ordered_codes)
        )
        all_codes = ordered_codes + extra_codes

        bot_views: list[dict[str, Any]] = []
        for code in all_codes:
            runtime_bot = runtime_bots.get(code, {})
            legacy_bot = legacy_bots.get(code, {})
            registration = registration_map.get(code)

            positions = list(runtime_bot.get("positions", []))
            watchlist = [str(symbol) for symbol in runtime_bot.get("watchlist", [])]
            pending_open = [str(symbol) for symbol in runtime_bot.get("pending_open_symbols", [])]
            pending_close = [str(symbol) for symbol in runtime_bot.get("pending_close_symbols", [])]
            pending_scale = [str(level) for level in runtime_bot.get("pending_scale_levels", [])]

            bot_views.append(
                {
                    "strategy_code": code,
                    "display_name": registration.display_name if registration else code.replace("_", " ").upper(),
                    "account_name": runtime_bot.get("account_name")
                    or (registration.account_name if registration else ""),
                    "execution_mode": registration.execution_mode if registration else "unknown",
                    "provider": self.settings.broker_default_provider if registration else "unknown",
                    "wiring_status": (
                        f'{registration.execution_mode}/{self.settings.broker_default_provider}'
                        if registration
                        else "unknown"
                    ),
                    "legacy_status": str(legacy_bot.get("status", "not_available")),
                    "legacy_present": bool(legacy_bot),
                    "watchlist": watchlist,
                    "watchlist_count": len(watchlist),
                    "positions": positions,
                    "position_count": len(positions),
                    "pending_open_symbols": pending_open,
                    "pending_close_symbols": pending_close,
                    "pending_scale_levels": pending_scale,
                    "pending_count": len(pending_open) + len(pending_close) + len(pending_scale),
                    "daily_pnl": float(runtime_bot.get("daily_pnl", 0) or 0),
                    "closed_today": list(runtime_bot.get("closed_today", [])),
                    "recent_intents": [
                        item for item in recent_intents if item.get("strategy_code") == code
                    ][:3],
                    "recent_orders": [
                        item for item in recent_orders if item.get("strategy_code") == code
                    ][:3],
                    "recent_fills": [
                        item for item in recent_fills if item.get("strategy_code") == code
                    ][:3],
                }
            )
        return bot_views

    async def load_health(self) -> dict[str, Any]:
        overview = await self.load_dashboard_data()
        return {
            "status": overview["status"],
            "service": SERVICE_NAME,
            "timestamp": overview["generated_at"],
            "environment": overview["environment"],
            "database_connected": not any(error.startswith("database:") for error in overview["errors"]),
            "redis_connected": not any(error.startswith("redis:") for error in overview["errors"]),
            "counts": overview["counts"],
            "services": overview["services"],
        }

    def _load_database_state(self) -> dict[str, Any]:
        errors: list[str] = []
        counts = {
            "strategies": 0,
            "broker_accounts": 0,
            "pending_intents": 0,
            "recent_fills": 0,
            "open_virtual_positions": 0,
            "open_account_positions": 0,
            "open_incidents": 0,
            "latest_reconciliation_findings": 0,
        }
        recent_intents: list[dict[str, Any]] = []
        recent_orders: list[dict[str, Any]] = []
        recent_fills: list[dict[str, Any]] = []
        virtual_positions: list[dict[str, Any]] = []
        account_positions: list[dict[str, Any]] = []
        reconciliation = {
            "latest_run": None,
            "findings": [],
        }
        incidents: list[dict[str, Any]] = []

        try:
            with self.session_factory() as session:
                session.execute(text("SELECT 1"))

                strategies = session.scalars(select(Strategy)).all()
                broker_accounts = session.scalars(select(BrokerAccount)).all()
                strategy_lookup = {strategy.id: strategy for strategy in strategies}
                account_lookup = {account.id: account for account in broker_accounts}

                counts["strategies"] = len(strategies)
                counts["broker_accounts"] = len(broker_accounts)
                counts["pending_intents"] = int(
                    session.scalar(
                        select(func.count()).select_from(TradeIntent).where(
                            TradeIntent.status.in_(["pending", "submitted", "accepted"])
                        )
                    )
                    or 0
                )
                counts["recent_fills"] = int(session.scalar(select(func.count()).select_from(Fill)) or 0)
                counts["open_virtual_positions"] = int(
                    session.scalar(
                        select(func.count()).select_from(VirtualPosition).where(VirtualPosition.quantity > 0)
                    )
                    or 0
                )
                counts["open_account_positions"] = int(
                    session.scalar(
                        select(func.count()).select_from(AccountPosition).where(AccountPosition.quantity > 0)
                    )
                    or 0
                )
                counts["open_incidents"] = int(
                    session.scalar(
                        select(func.count()).select_from(SystemIncident).where(SystemIncident.status != "closed")
                    )
                    or 0
                )

                latest_reconciliation_run = session.scalar(
                    select(ReconciliationRun).order_by(desc(ReconciliationRun.started_at))
                )
                if latest_reconciliation_run is not None:
                    latest_finding_rows = session.scalars(
                        select(ReconciliationFinding)
                        .where(ReconciliationFinding.reconciliation_run_id == latest_reconciliation_run.id)
                        .order_by(
                            desc(
                                case(
                                    (ReconciliationFinding.severity == "critical", 2),
                                    (ReconciliationFinding.severity == "warning", 1),
                                    else_=0,
                                )
                            ),
                            desc(ReconciliationFinding.created_at),
                        )
                        .limit(20)
                    ).all()
                    counts["latest_reconciliation_findings"] = len(latest_finding_rows)
                    reconciliation = {
                        "latest_run": {
                            "status": latest_reconciliation_run.status,
                            "started_at": _datetime_str(latest_reconciliation_run.started_at),
                            "completed_at": _datetime_str(latest_reconciliation_run.completed_at),
                            "summary": latest_reconciliation_run.summary,
                        },
                        "findings": [
                            {
                                "severity": finding.severity,
                                "finding_type": finding.finding_type,
                                "symbol": finding.symbol or "",
                                "title": str(finding.payload.get("title", finding.finding_type)),
                                "created_at": _datetime_str(finding.created_at),
                            }
                            for finding in latest_finding_rows
                        ],
                    }

                for intent in session.scalars(
                    select(TradeIntent).order_by(desc(TradeIntent.updated_at)).limit(50)
                ).all():
                    strategy = strategy_lookup.get(intent.strategy_id)
                    account = account_lookup.get(intent.broker_account_id)
                    recent_intents.append(
                        {
                            "strategy_code": strategy.code if strategy else str(intent.strategy_id),
                            "broker_account_name": account.name if account else str(intent.broker_account_id),
                            "symbol": intent.symbol,
                            "side": intent.side,
                            "intent_type": intent.intent_type,
                            "quantity": _decimal_str(intent.quantity),
                            "status": intent.status,
                            "reason": intent.reason,
                            "updated_at": _datetime_str(intent.updated_at),
                        }
                    )

                for order in session.scalars(
                    select(BrokerOrder).order_by(desc(BrokerOrder.updated_at)).limit(50)
                ).all():
                    strategy = strategy_lookup.get(order.strategy_id)
                    account = account_lookup.get(order.broker_account_id)
                    recent_orders.append(
                        {
                            "strategy_code": strategy.code if strategy else str(order.strategy_id),
                            "broker_account_name": account.name if account else str(order.broker_account_id),
                            "symbol": order.symbol,
                            "side": order.side,
                            "quantity": _decimal_str(order.quantity),
                            "status": order.status,
                            "client_order_id": order.client_order_id,
                            "broker_order_id": order.broker_order_id or "",
                            "updated_at": _datetime_str(order.updated_at),
                        }
                    )

                for fill in session.scalars(select(Fill).order_by(desc(Fill.filled_at)).limit(50)).all():
                    strategy = strategy_lookup.get(fill.strategy_id)
                    account = account_lookup.get(fill.broker_account_id)
                    recent_fills.append(
                        {
                            "strategy_code": strategy.code if strategy else str(fill.strategy_id),
                            "broker_account_name": account.name if account else str(fill.broker_account_id),
                            "symbol": fill.symbol,
                            "side": fill.side,
                            "quantity": _decimal_str(fill.quantity),
                            "price": _decimal_str(fill.price),
                            "filled_at": _datetime_str(fill.filled_at),
                        }
                    )

                for position in session.scalars(
                    select(VirtualPosition)
                    .where(VirtualPosition.quantity > 0)
                    .order_by(desc(VirtualPosition.updated_at))
                    .limit(20)
                ).all():
                    strategy = strategy_lookup.get(position.strategy_id)
                    account = account_lookup.get(position.broker_account_id)
                    virtual_positions.append(
                        {
                            "strategy_code": strategy.code if strategy else str(position.strategy_id),
                            "broker_account_name": account.name if account else str(position.broker_account_id),
                            "symbol": position.symbol,
                            "quantity": _decimal_str(position.quantity),
                            "average_price": _decimal_str(position.average_price),
                            "realized_pnl": _decimal_str(position.realized_pnl),
                            "updated_at": _datetime_str(position.updated_at),
                        }
                    )

                for position in session.scalars(
                    select(AccountPosition)
                    .where(AccountPosition.quantity > 0)
                    .order_by(desc(AccountPosition.updated_at))
                    .limit(20)
                ).all():
                    account = account_lookup.get(position.broker_account_id)
                    account_positions.append(
                        {
                            "broker_account_name": account.name if account else str(position.broker_account_id),
                            "symbol": position.symbol,
                            "quantity": _decimal_str(position.quantity),
                            "average_price": _decimal_str(position.average_price),
                            "market_value": _decimal_str(position.market_value),
                            "updated_at": _datetime_str(position.updated_at),
                        }
                    )

                for incident in session.scalars(
                    select(SystemIncident).order_by(desc(SystemIncident.opened_at)).limit(10)
                ).all():
                    incidents.append(
                        {
                            "service_name": incident.service_name or "system",
                            "severity": incident.severity,
                            "title": incident.title,
                            "status": incident.status,
                            "opened_at": _datetime_str(incident.opened_at),
                        }
                    )
        except Exception as exc:
            errors.append(f"database:{exc}")

        return {
            "counts": counts,
            "recent_intents": recent_intents,
            "recent_orders": recent_orders,
            "recent_fills": recent_fills,
            "virtual_positions": virtual_positions,
            "account_positions": account_positions,
            "reconciliation": reconciliation,
            "incidents": incidents,
            "errors": errors,
        }

    async def _load_stream_state(self) -> dict[str, Any]:
        errors: list[str] = []
        services: list[dict[str, Any]] = []
        market_data = {
            "latest_snapshot_batch": None,
            "active_subscription_symbols": 0,
            "subscription_symbols": [],
        }
        strategy_runtime = {
            "watchlist": [],
            "top_confirmed": [],
            "five_pillars": [],
            "top_gainers": [],
            "recent_alerts": [],
            "top_gainer_changes": [],
            "alert_warmup": {},
            "cycle_count": 0,
            "bots": {},
        }

        try:
            heartbeats = await self._read_stream_events("heartbeats", limit=50)
            latest_by_service: dict[str, dict[str, Any]] = {}
            for event in heartbeats:
                payload = HeartbeatEvent.model_validate(event).payload
                if payload.service_name in latest_by_service:
                    continue
                latest_by_service[payload.service_name] = {
                    "service_name": payload.service_name,
                    "instance_name": payload.instance_name,
                    "status": payload.status,
                    "details": payload.details,
                    "observed_at": _datetime_str(HeartbeatEvent.model_validate(event).produced_at),
                }
            services = sorted(latest_by_service.values(), key=lambda item: item["service_name"])
        except Exception as exc:
            errors.append(f"redis:heartbeats:{exc}")

        try:
            snapshot_events = await self._read_stream_events("snapshot-batches", limit=1)
            if snapshot_events:
                event = SnapshotBatchEvent.model_validate(snapshot_events[0])
                market_data["latest_snapshot_batch"] = {
                    "snapshot_count": len(event.payload.snapshots),
                    "reference_count": len(event.payload.reference_data),
                    "completed_at": _datetime_str(event.payload.completed_at),
                }
        except Exception as exc:
            errors.append(f"redis:snapshot-batches:{exc}")

        try:
            subscription_events = await self._read_stream_events("market-data-subscriptions", limit=1)
            if subscription_events:
                event = MarketDataSubscriptionEvent.model_validate(subscription_events[0])
                market_data["active_subscription_symbols"] = len(event.payload.symbols)
                market_data["subscription_symbols"] = event.payload.symbols
        except Exception as exc:
            errors.append(f"redis:market-data-subscriptions:{exc}")

        try:
            strategy_state_events = await self._read_stream_events("strategy-state", limit=1)
            if strategy_state_events:
                event = StrategyStateSnapshotEvent.model_validate(strategy_state_events[0])
                strategy_runtime = {
                    "watchlist": event.payload.watchlist,
                    "top_confirmed": event.payload.top_confirmed,
                    "five_pillars": event.payload.five_pillars,
                    "top_gainers": event.payload.top_gainers,
                    "recent_alerts": event.payload.recent_alerts,
                    "top_gainer_changes": event.payload.top_gainer_changes,
                    "alert_warmup": event.payload.alert_warmup,
                    "cycle_count": event.payload.cycle_count,
                    "bots": {
                        bot.strategy_code: bot.model_dump()
                        for bot in event.payload.bots
                    },
                }
        except Exception as exc:
            errors.append(f"redis:strategy-state:{exc}")

        return {
            "services": services,
            "market_data": market_data,
            "strategy_runtime": strategy_runtime,
            "errors": errors,
        }

    async def _read_stream_events(self, topic: str, *, limit: int) -> list[dict[str, Any]]:
        stream = stream_name(self.settings.redis_stream_prefix, topic)
        entries = await self.redis.xrevrange(stream, count=limit)
        payloads: list[dict[str, Any]] = []
        for _message_id, fields in entries:
            data = fields.get("data")
            if data:
                payloads.append(json.loads(data))
        return payloads

    async def _load_legacy_shadow_data(
        self,
        *,
        strategy_runtime: dict[str, Any],
        recent_intents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.legacy_client is None:
            return {
                "enabled": False,
                "connected": False,
                "fetched_at": None,
                "scanner": {"confirmed_symbols": [], "count": 0},
                "bots": {},
                "divergence": self._empty_legacy_divergence(),
                "errors": [],
            }

        cached = await self._get_cached_legacy_snapshot()
        divergence = self._build_legacy_divergence(
            legacy_snapshot=cached,
            strategy_runtime=strategy_runtime,
            recent_intents=recent_intents,
        )
        return {
            **cached,
            "divergence": divergence,
        }

    async def _get_cached_legacy_snapshot(self) -> dict[str, Any]:
        async with self._legacy_cache_lock:
            cache_age = None
            if self._legacy_cache_at is not None:
                cache_age = (utcnow() - self._legacy_cache_at).total_seconds()
            if self._legacy_cache is not None and cache_age is not None:
                if cache_age < self.settings.legacy_api_cache_ttl_seconds:
                    return self._legacy_cache

            snapshot = await self.legacy_client.fetch_snapshot()
            self._legacy_cache = snapshot
            self._legacy_cache_at = utcnow()
            return snapshot

    def _build_legacy_divergence(
        self,
        *,
        legacy_snapshot: dict[str, Any],
        strategy_runtime: dict[str, Any],
        recent_intents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        new_watchlist = {str(symbol).upper() for symbol in strategy_runtime.get("watchlist", [])}
        legacy_confirmed = {
            str(symbol).upper()
            for symbol in legacy_snapshot.get("scanner", {}).get("confirmed_symbols", [])
        }

        by_strategy: dict[str, dict[str, Any]] = {}
        total_issues = 0
        new_bots = strategy_runtime.get("bots", {})
        recent_intents_by_strategy: dict[str, set[str]] = {}
        for item in recent_intents:
            strategy_code = str(item.get("strategy_code", ""))
            recent_intents_by_strategy.setdefault(strategy_code, set()).add(
                f'{item.get("intent_type", "")}:{item.get("side", "")}:{item.get("symbol", "")}'
            )

        all_strategy_codes = sorted(
            set(legacy_snapshot.get("bots", {}).keys()) | set(new_bots.keys())
        )
        for strategy_code in all_strategy_codes:
            legacy_bot = legacy_snapshot.get("bots", {}).get(strategy_code, {})
            new_bot = new_bots.get(strategy_code, {})
            legacy_watched = {
                str(symbol).upper() for symbol in legacy_bot.get("watched_tickers", [])
            }
            new_watched = {
                str(symbol).upper() for symbol in new_bot.get("watchlist", [])
            }

            legacy_positions = {
                str(item.get("symbol", "")).upper(): float(item.get("quantity", 0) or 0)
                for item in legacy_bot.get("positions", [])
                if item.get("symbol")
            }
            new_positions = {
                str(item.get("ticker", "")).upper(): float(item.get("quantity", 0) or 0)
                for item in new_bot.get("positions", [])
                if item.get("ticker")
            }
            position_mismatches = []
            for symbol in sorted(set(legacy_positions) | set(new_positions)):
                legacy_qty = legacy_positions.get(symbol, 0.0)
                new_qty = new_positions.get(symbol, 0.0)
                if abs(legacy_qty - new_qty) > 0.0001:
                    position_mismatches.append(
                        {
                            "symbol": symbol,
                            "legacy_quantity": legacy_qty,
                            "new_quantity": new_qty,
                        }
                    )

            legacy_actions = {
                self._normalize_legacy_action_key(item)
                for item in legacy_bot.get("recent_actions", [])
                if self._normalize_legacy_action_key(item)
            }
            new_actions = recent_intents_by_strategy.get(strategy_code, set())

            issue_count = (
                len(legacy_watched - new_watched)
                + len(new_watched - legacy_watched)
                + len(position_mismatches)
                + len(legacy_actions - new_actions)
                + len(new_actions - legacy_actions)
            )
            total_issues += issue_count

            by_strategy[strategy_code] = {
                "legacy_status": legacy_bot.get("status", "unknown"),
                "new_present": bool(new_bot),
                "legacy_present": bool(legacy_bot),
                "watched_only_in_legacy": sorted(legacy_watched - new_watched),
                "watched_only_in_new": sorted(new_watched - legacy_watched),
                "position_mismatches": position_mismatches,
                "actions_only_in_legacy": sorted(legacy_actions - new_actions),
                "actions_only_in_new": sorted(new_actions - legacy_actions),
                "issue_count": issue_count,
            }

        confirmed_only_in_legacy = sorted(legacy_confirmed - new_watchlist)
        confirmed_only_in_new = sorted(new_watchlist - legacy_confirmed)
        total_issues += len(confirmed_only_in_legacy) + len(confirmed_only_in_new)

        return {
            "status": "aligned" if total_issues == 0 else "drifted",
            "issue_count": total_issues,
            "confirmed_only_in_legacy": confirmed_only_in_legacy,
            "confirmed_only_in_new": confirmed_only_in_new,
            "strategies": by_strategy,
        }

    def _empty_legacy_divergence(self) -> dict[str, Any]:
        return {
            "status": "disabled",
            "issue_count": 0,
            "confirmed_only_in_legacy": [],
            "confirmed_only_in_new": [],
            "strategies": {},
        }

    def _normalize_legacy_action_key(self, item: dict[str, Any]) -> str:
        symbol = str(item.get("symbol", "")).upper()
        action = str(item.get("action", "")).upper()
        if not symbol or not action:
            return ""
        action_map = {
            "BUY": "open:buy",
            "CLOSE": "close:sell",
            "SCALE": "scale:sell",
        }
        normalized = action_map.get(action)
        if normalized is None:
            return ""
        return f"{normalized}:{symbol}"


def build_app(
    settings: Settings | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    redis_client: Redis | None = None,
    legacy_client: LegacyShadowClient | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    active_session_factory = session_factory or build_session_factory(active_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        redis = redis_client or Redis.from_url(active_settings.redis_url, decode_responses=True)
        app.state.repository = ControlPlaneRepository(
            active_settings,
            session_factory=active_session_factory,
            redis=redis,
            legacy_client=legacy_client,
        )
        yield
        if redis_client is None:
            await redis.aclose()

    app = FastAPI(
        title="Project Mai Tai Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return await app.state.repository.load_health()

    @app.get("/meta")
    async def meta() -> dict[str, Any]:
        return {
            "app_name": active_settings.app_name,
            "domain": "project-mai-tai.live",
            "legacy_api_base_url": active_settings.legacy_api_base_url,
            "oms_adapter": active_settings.oms_adapter,
            "streams": {
                "market_data": stream_name(active_settings.redis_stream_prefix, "market-data"),
                "snapshot_batches": stream_name(active_settings.redis_stream_prefix, "snapshot-batches"),
                "market_data_subscriptions": stream_name(
                    active_settings.redis_stream_prefix,
                    "market-data-subscriptions",
                ),
                "strategy_intents": stream_name(active_settings.redis_stream_prefix, "strategy-intents"),
                "order_events": stream_name(active_settings.redis_stream_prefix, "order-events"),
                "heartbeats": stream_name(active_settings.redis_stream_prefix, "heartbeats"),
            },
        }

    @app.get("/api/overview")
    async def overview() -> dict[str, Any]:
        return await app.state.repository.load_dashboard_data()

    @app.get("/api/scanner")
    async def scanner() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {"scanner": data["scanner"]}

    @app.get("/api/bots")
    async def bots() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {"bots": data["bots"]}

    @app.get("/api/orders")
    async def orders() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {
            "recent_intents": data["recent_intents"],
            "recent_orders": data["recent_orders"],
            "recent_fills": data["recent_fills"],
        }

    @app.get("/api/positions")
    async def positions() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {
            "virtual_positions": data["virtual_positions"],
            "account_positions": data["account_positions"],
        }

    @app.get("/api/reconciliation")
    async def reconciliation() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {
            "reconciliation": data["reconciliation"],
            "incidents": data["incidents"],
        }

    @app.get("/api/shadow")
    async def shadow() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {
            "legacy_shadow": data["legacy_shadow"],
            "strategy_runtime": data["strategy_runtime"],
        }

    @app.get("/scanner/confirmed")
    async def scanner_confirmed() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {
            "stocks": data["scanner"]["top_confirmed"],
            "count": data["scanner"]["top_confirmed_count"],
        }

    @app.get("/scanner/pillars")
    async def scanner_pillars() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {
            "stocks": data["scanner"]["five_pillars"],
            "count": data["scanner"]["five_pillars_count"],
        }

    @app.get("/scanner/gainers")
    async def scanner_gainers() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {
            "stocks": data["scanner"]["top_gainers"],
            "count": data["scanner"]["top_gainers_count"],
        }

    @app.get("/scanner/alerts")
    async def scanner_alerts() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {
            "alerts": data["scanner"]["recent_alerts"],
            "count": data["scanner"]["recent_alerts_count"],
            "warmup": data["scanner"]["alert_warmup"],
        }

    @app.get("/scanner/dashboard", response_class=HTMLResponse)
    async def scanner_dashboard() -> str:
        data = await app.state.repository.load_dashboard_data()
        return _render_scanner_dashboard(data)

    @app.get("/bot")
    async def bot_30s_status() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return _build_bot_api_payload(data, "macd_30s")

    @app.get("/bot1m")
    async def bot_1m_status() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return _build_bot_api_payload(data, "macd_1m")

    @app.get("/tosbot")
    async def tos_bot_status() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return _build_bot_api_payload(data, "tos")

    @app.get("/runnerbot")
    async def runner_bot_status() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return _build_bot_api_payload(data, "runner")

    @app.get("/bot/30s", response_class=HTMLResponse)
    async def bot_30s_page() -> str:
        data = await app.state.repository.load_dashboard_data()
        return _render_bot_detail_page(data, "macd_30s")

    @app.get("/bot/1m", response_class=HTMLResponse)
    async def bot_1m_page() -> str:
        data = await app.state.repository.load_dashboard_data()
        return _render_bot_detail_page(data, "macd_1m")

    @app.get("/bot/tos", response_class=HTMLResponse)
    async def bot_tos_page() -> str:
        data = await app.state.repository.load_dashboard_data()
        return _render_bot_detail_page(data, "tos")

    @app.get("/bot/runner", response_class=HTMLResponse)
    async def bot_runner_page() -> str:
        data = await app.state.repository.load_dashboard_data()
        return _render_bot_detail_page(data, "runner")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        data = await app.state.repository.load_dashboard_data()
        return _render_dashboard(data)

    return app


app = build_app()


def run() -> None:
    settings = get_settings()
    configure_logging(SERVICE_NAME, settings.log_level)
    uvicorn.run(
        "project_mai_tai.services.control_plane:app",
        host=settings.control_plane_host,
        port=settings.control_plane_port,
        reload=False,
    )


def _render_dashboard(data: dict[str, Any]) -> str:
    refresh_seconds = 5
    scanner = data["scanner"]
    bot_views = data["bots"]
    errors_html = "".join(
        f'<div class="alert">{escape(error)}</div>' for error in data["errors"]
    ) or '<div class="ok-banner">No current control-plane read errors.</div>'

    scanner_rows = "".join(
        f"""
        <tr>
          <td>{item["rank"]}</td>
          <td><strong>{escape(item["ticker"])}</strong></td>
          <td>{escape(item["confirmation_path"] or "-")}</td>
          <td>{item["rank_score"]:.0f}</td>
          <td>{item["price"]:.2f}</td>
          <td>{item["change_pct"]:+.1f}%</td>
          <td>{_short_volume(item["volume"])}</td>
          <td>{item["rvol"]:.1f}x</td>
          <td>{item["spread_pct"]:.2f}%</td>
          <td>{item["squeeze_count"]}</td>
          <td>{escape(item["first_spike_time"] or "-")}</td>
          <td>{escape(", ".join(item["watched_by"]) or "-")}</td>
        </tr>
        """
        for item in scanner["top_confirmed"]
    ) or _empty_row(12, "No confirmed candidates yet")

    bot_cards = "".join(
        f"""
        <article class="bot-card">
          <div class="bot-head">
            <div>
              <h3>{escape(bot["display_name"])}</h3>
              <div class="sub">{escape(bot["strategy_code"])} / {escape(bot["account_name"] or "-")}</div>
            </div>
            {_status_badge(bot["wiring_status"])}
          </div>
          <div class="bot-metrics">
            <div><span class="mini-label">Watching</span><strong>{bot["watchlist_count"]}</strong></div>
            <div><span class="mini-label">Positions</span><strong>{bot["position_count"]}</strong></div>
            <div><span class="mini-label">Pending</span><strong>{bot["pending_count"]}</strong></div>
          </div>
          <div class="bot-lines">
            <p><strong>Execution:</strong> {escape(bot["execution_mode"])} via {escape(bot["provider"])}</p>
            <p><strong>Legacy Shadow:</strong> {escape(bot["legacy_status"] if bot["legacy_present"] else "not available")}</p>
            <p><strong>Watchlist:</strong> {escape(", ".join(bot["watchlist"][:8]) or "None")}</p>
            <p><strong>Pending Opens:</strong> {escape(", ".join(bot["pending_open_symbols"][:6]) or "None")}</p>
            <p><strong>Pending Closes:</strong> {escape(", ".join(bot["pending_close_symbols"][:6]) or "None")}</p>
            <p><strong>Pending Scales:</strong> {escape(", ".join(bot["pending_scale_levels"][:6]) or "None")}</p>
            <p><strong>Positions:</strong> {escape(_position_preview(bot["positions"]))}</p>
            <p><strong>Recent Intents:</strong> {escape(_intent_preview(bot["recent_intents"]))}</p>
          </div>
        </article>
        """
        for bot in bot_views
    ) or '<div class="muted-box">No bot runtime snapshots available yet.</div>'

    services_rows = "".join(
        f"""
        <tr>
          <td>{escape(service["service_name"])}</td>
          <td>{_status_badge(service["status"])}</td>
          <td>{escape(service["instance_name"])}</td>
          <td>{escape(service["observed_at"])}</td>
          <td>{escape(", ".join(f"{key}={value}" for key, value in service["details"].items()) or "-")}</td>
        </tr>
        """
        for service in data["services"]
    ) or _empty_row(5, "No service heartbeats yet")

    intents_rows = "".join(
        f"""
        <tr>
          <td>{escape(item["strategy_code"])}</td>
          <td>{escape(item["symbol"])}</td>
          <td>{escape(item["intent_type"])}</td>
          <td>{escape(item["side"])}</td>
          <td>{escape(item["quantity"])}</td>
          <td>{_status_badge(item["status"])}</td>
          <td>{escape(item["updated_at"])}</td>
        </tr>
        """
        for item in data["recent_intents"]
    ) or _empty_row(7, "No trade intents recorded yet")

    orders_rows = "".join(
        f"""
        <tr>
          <td>{escape(item["strategy_code"])}</td>
          <td>{escape(item["symbol"])}</td>
          <td>{escape(item["side"])}</td>
          <td>{escape(item["quantity"])}</td>
          <td>{_status_badge(item["status"])}</td>
          <td><code>{escape(item["client_order_id"])}</code></td>
          <td>{escape(item["updated_at"])}</td>
        </tr>
        """
        for item in data["recent_orders"]
    ) or _empty_row(7, "No broker orders recorded yet")

    fills_rows = "".join(
        f"""
        <tr>
          <td>{escape(item["strategy_code"])}</td>
          <td>{escape(item["symbol"])}</td>
          <td>{escape(item["side"])}</td>
          <td>{escape(item["quantity"])}</td>
          <td>{escape(item["price"])}</td>
          <td>{escape(item["filled_at"])}</td>
        </tr>
        """
        for item in data["recent_fills"]
    ) or _empty_row(6, "No fills recorded yet")

    virtual_positions_rows = "".join(
        f"""
        <tr>
          <td>{escape(item["strategy_code"])}</td>
          <td>{escape(item["broker_account_name"])}</td>
          <td>{escape(item["symbol"])}</td>
          <td>{escape(item["quantity"])}</td>
          <td>{escape(item["average_price"])}</td>
          <td>{escape(item["realized_pnl"])}</td>
          <td>{escape(item["updated_at"])}</td>
        </tr>
        """
        for item in data["virtual_positions"]
    ) or _empty_row(7, "No virtual positions open")

    account_positions_rows = "".join(
        f"""
        <tr>
          <td>{escape(item["broker_account_name"])}</td>
          <td>{escape(item["symbol"])}</td>
          <td>{escape(item["quantity"])}</td>
          <td>{escape(item["average_price"])}</td>
          <td>{escape(item["market_value"] or "-")}</td>
          <td>{escape(item["updated_at"])}</td>
        </tr>
        """
        for item in data["account_positions"]
    ) or _empty_row(6, "No account positions open")

    incidents_rows = "".join(
        f"""
        <tr>
          <td>{escape(item["service_name"])}</td>
          <td>{_status_badge(item["severity"])}</td>
          <td>{escape(item["title"])}</td>
          <td>{_status_badge(item["status"])}</td>
          <td>{escape(item["opened_at"])}</td>
        </tr>
        """
        for item in data["incidents"]
    ) or _empty_row(5, "No incidents logged")

    reconciliation_rows = "".join(
        f"""
        <tr>
          <td>{_status_badge(item["severity"])}</td>
          <td>{escape(item["finding_type"])}</td>
          <td>{escape(item["symbol"] or "-")}</td>
          <td>{escape(item["title"])}</td>
          <td>{escape(item["created_at"])}</td>
        </tr>
        """
        for item in data["reconciliation"]["findings"]
    ) or _empty_row(5, "No reconciliation findings in the latest run")

    shadow_strategy_rows = "".join(
        f"""
        <tr>
          <td>{escape(strategy_code)}</td>
          <td>{_status_badge(details["legacy_status"]) if details["legacy_present"] else '<span style="color:#61758a;">missing</span>'}</td>
          <td>{'YES' if details["new_present"] else 'NO'}</td>
          <td>{escape(", ".join(details["watched_only_in_legacy"][:6]) or "-")}</td>
          <td>{escape(", ".join(details["watched_only_in_new"][:6]) or "-")}</td>
          <td>{len(details["position_mismatches"])}</td>
          <td>{details["issue_count"]}</td>
        </tr>
        """
        for strategy_code, details in data["legacy_shadow"]["divergence"]["strategies"].items()
    ) or _empty_row(7, "No legacy shadow comparison available")

    latest_snapshot = data["market_data"]["latest_snapshot_batch"] or {}
    snapshot_summary = (
        f'{latest_snapshot.get("snapshot_count", 0)} snapshots / '
        f'{latest_snapshot.get("reference_count", 0)} refs'
        if latest_snapshot
        else "No snapshot batches yet"
    )
    subscription_symbols = data["market_data"]["subscription_symbols"][:12]
    subscription_summary = ", ".join(subscription_symbols) or "No dynamic subscriptions yet"
    latest_reconciliation = data["reconciliation"]["latest_run"] or {}
    latest_reconciliation_summary = latest_reconciliation.get("summary", {})
    cutover_confidence = latest_reconciliation_summary.get("cutover_confidence", 0)
    legacy_shadow = data["legacy_shadow"]
    shadow_divergence = legacy_shadow["divergence"]
    shadow_confirmed_legacy = ", ".join(shadow_divergence["confirmed_only_in_legacy"][:10]) or "None"
    shadow_confirmed_new = ", ".join(shadow_divergence["confirmed_only_in_new"][:10]) or "None"

    return f"""
    <html>
      <head>
        <title>Project Mai Tai Control Plane</title>
        <meta http-equiv="refresh" content="{refresh_seconds}">
        <style>
          :root {{
            --ink: #122433;
            --muted: #61758a;
            --line: rgba(18, 36, 51, 0.12);
            --panel: rgba(255, 255, 255, 0.9);
            --bg-top: #f6efe1;
            --bg-bottom: #edf7fb;
            --accent: #0f7f66;
            --warn: #d48000;
            --danger: #c0392b;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            color: var(--ink);
            font-family: Georgia, "Times New Roman", serif;
            background:
              radial-gradient(circle at top right, rgba(15, 127, 102, 0.12), transparent 28%),
              linear-gradient(180deg, var(--bg-top), var(--bg-bottom));
          }}
          .shell {{
            max-width: 1280px;
            margin: 0 auto;
            padding: 24px;
          }}
          .nav {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin: 0 0 20px 0;
          }}
          .nav a {{
            text-decoration: none;
            color: var(--ink);
            border: 1px solid var(--line);
            background: rgba(255,255,255,0.72);
            padding: 10px 14px;
            border-radius: 999px;
            font-size: 13px;
          }}
          .hero, .section, .table-card {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 22px;
            box-shadow: 0 18px 42px rgba(18, 36, 51, 0.08);
          }}
          .hero {{
            padding: 28px;
            margin-bottom: 20px;
          }}
          .eyebrow {{
            color: var(--accent);
            font-size: 13px;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            margin-bottom: 10px;
          }}
          h1, h2 {{
            margin: 0 0 10px 0;
          }}
          p {{
            margin: 0;
            color: var(--muted);
            line-height: 1.45;
          }}
          .cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 14px;
            margin-top: 22px;
          }}
          .card {{
            background: rgba(255, 255, 255, 0.8);
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 16px;
          }}
          .label {{
            font-size: 12px;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: var(--muted);
            margin-bottom: 6px;
          }}
          .value {{
            font-size: 28px;
            font-weight: bold;
          }}
          .grid-2 {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 16px;
            margin-top: 16px;
          }}
          .bot-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 14px;
          }}
          .bot-card {{
            background: rgba(255,255,255,0.78);
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 16px;
          }}
          .bot-head {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 10px;
            margin-bottom: 12px;
          }}
          .bot-head h3 {{
            margin: 0 0 4px 0;
          }}
          .bot-metrics {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 8px;
            margin-bottom: 12px;
          }}
          .bot-metrics > div {{
            background: rgba(18, 36, 51, 0.04);
            border-radius: 12px;
            padding: 10px;
          }}
          .mini-label {{
            display: block;
            color: var(--muted);
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 4px;
          }}
          .bot-lines p {{
            margin: 0 0 8px 0;
            font-size: 14px;
          }}
          .section {{
            padding: 20px;
          }}
          .section-header {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            gap: 12px;
            margin-bottom: 12px;
          }}
          .sub {{
            color: var(--muted);
            font-size: 14px;
          }}
          .table-card {{
            overflow: hidden;
          }}
          table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
          }}
          th, td {{
            padding: 12px 14px;
            border-bottom: 1px solid var(--line);
            text-align: left;
            vertical-align: top;
          }}
          th {{
            background: rgba(18, 36, 51, 0.04);
            color: var(--muted);
            font-size: 12px;
            letter-spacing: 0.06em;
            text-transform: uppercase;
          }}
          tr:last-child td {{
            border-bottom: none;
          }}
          .pill {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: bold;
          }}
          .status-healthy, .status-filled, .status-pass, .status-open {{
            background: rgba(15, 127, 102, 0.12);
            color: var(--accent);
          }}
          .status-starting, .status-accepted, .status-submitted, .status-warning {{
            background: rgba(212, 128, 0, 0.12);
            color: var(--warn);
          }}
          .status-rejected, .status-degraded, .status-error, .status-closed, .status-critical {{
            background: rgba(192, 57, 43, 0.12);
            color: var(--danger);
          }}
          .status-pending, .status-cancelled {{
            background: rgba(18, 36, 51, 0.1);
            color: var(--ink);
          }}
          .muted-box {{
            color: var(--muted);
            font-size: 14px;
            line-height: 1.5;
          }}
          .alert {{
            padding: 10px 12px;
            border-left: 4px solid var(--danger);
            background: rgba(192, 57, 43, 0.08);
            margin-bottom: 8px;
            border-radius: 10px;
          }}
          .ok-banner {{
            padding: 10px 12px;
            border-left: 4px solid var(--accent);
            background: rgba(15, 127, 102, 0.08);
            border-radius: 10px;
          }}
          code {{
            background: rgba(18, 36, 51, 0.08);
            padding: 2px 6px;
            border-radius: 6px;
          }}
        </style>
      </head>
      <body>
        <div class="shell">
          <section class="hero">
            <div class="eyebrow">Project Mai Tai Operator View</div>
            <h1>Parallel Live-Trading Rebuild</h1>
            <p>
              This control plane is reading the new platform's durable OMS state and live stream
              health so you can validate it beside the legacy system before cutover.
            </p>
            <div class="cards">
              <div class="card">
                <div class="label">Platform Status</div>
                <div class="value">{data["status"].upper()}</div>
                <p>{escape(data["environment"])} / {escape(data["provider"])} / {escape(data["oms_adapter"])}</p>
              </div>
              <div class="card">
                <div class="label">Open Virtual Positions</div>
                <div class="value">{data["counts"]["open_virtual_positions"]}</div>
                <p>Strategy-attributed positions inside shared accounts.</p>
              </div>
              <div class="card">
                <div class="label">Pending Intents</div>
                <div class="value">{data["counts"]["pending_intents"]}</div>
                <p>Open, submitted, or accepted intents waiting on broker lifecycle.</p>
              </div>
              <div class="card">
                <div class="label">Latest Snapshot</div>
                <div class="value">{escape(snapshot_summary)}</div>
                <p>{escape(latest_snapshot.get("completed_at", "No snapshot timestamp yet"))}</p>
              </div>
              <div class="card">
                <div class="label">Active Market Symbols</div>
                <div class="value">{data["market_data"]["active_subscription_symbols"]}</div>
                <p>{escape(subscription_summary)}</p>
              </div>
              <div class="card">
                <div class="label">Cutover Confidence</div>
                <div class="value">{cutover_confidence}/100</div>
                <p>{escape(latest_reconciliation.get("completed_at", "No reconciliation run yet"))}</p>
              </div>
              <div class="card">
                <div class="label">Control Plane</div>
                <div class="value"><code>{escape(data["control_plane_url"])}</code></div>
                <p>{escape(data["generated_at"])}</p>
              </div>
            </div>
          </section>

          <nav class="nav">
            <a href="/scanner/dashboard">Scanner Page</a>
            <a href="/bot/30s">30s Bot</a>
            <a href="/bot/1m">1m Bot</a>
            <a href="/bot/tos">TOS Bot</a>
            <a href="/bot/runner">Runner Bot</a>
            <a href="#scanner">Scanner</a>
            <a href="#bots">Bots</a>
            <a href="#shadow">Shadow</a>
            <a href="#reconciliation">Reconciliation</a>
            <a href="#orders">Orders</a>
            <a href="#positions">Positions</a>
          </nav>

          <div class="grid-2" id="scanner">
            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Scanner Pipeline</h2>
                  <div class="sub">Closest equivalent to the legacy scanner dashboard: confirmed names, watchlist flow, and subscription state.</div>
                </div>
              </div>
              <div class="muted-box">
                <p><strong>Scanner Status:</strong> {escape(scanner["status"])}</p>
                <p><strong>Watchlist Count:</strong> {scanner["watchlist_count"]}</p>
                <p><strong>Top Confirmed Count:</strong> {scanner["top_confirmed_count"]}</p>
                <p><strong>Active Subscriptions:</strong> {scanner["active_subscription_symbols"]}</p>
                <p><strong>Watchlist:</strong> {escape(", ".join(scanner["watchlist"][:12]) or "None")}</p>
                <p><strong>Legacy Confirmed:</strong> {escape(", ".join(scanner["legacy_confirmed_symbols"][:12]) or "None")}</p>
              </div>
            </section>

            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Subscriptions</h2>
                  <div class="sub">Symbols currently pushed into the live tick pipeline.</div>
                </div>
              </div>
              <div class="muted-box">
                <p><strong>Latest Snapshot Batch:</strong> {escape(snapshot_summary)}</p>
                <p><strong>Snapshot Completed:</strong> {escape(latest_snapshot.get("completed_at", "No snapshot timestamp yet"))}</p>
                <p><strong>Subscribed Symbols:</strong> {escape(", ".join(scanner["subscription_symbols"][:20]) or "None")}</p>
              </div>
            </section>
          </div>

          <section class="section">
            <div class="section-header">
              <div>
                <h2>Confirmed Candidates</h2>
                <div class="sub">New scanner output promoted to bot-ready candidates, including score, path, and which bots are watching.</div>
              </div>
            </div>
            <div class="table-card">
              <table>
                <thead>
                  <tr><th>Rank</th><th>Ticker</th><th>Path</th><th>Score</th><th>Price</th><th>Change</th><th>Volume</th><th>RVOL</th><th>Spread</th><th>Squeezes</th><th>First Spike</th><th>Watched By</th></tr>
                </thead>
                <tbody>{scanner_rows}</tbody>
              </table>
            </div>
          </section>

          <section class="section" id="bots">
            <div class="section-header">
              <div>
                <h2>Bot Deck</h2>
                <div class="sub">Legacy-style bot visibility for 30s, 1m, TOS, and Runner.</div>
              </div>
            </div>
            <div class="bot-grid">{bot_cards}</div>
          </section>

          <div class="grid-2">
            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Service Health</h2>
                  <div class="sub">Latest heartbeat per service from Redis streams.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Service</th><th>Status</th><th>Instance</th><th>Observed</th><th>Details</th></tr>
                  </thead>
                  <tbody>{services_rows}</tbody>
                </table>
              </div>
            </section>

            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Control Plane Notes</h2>
                  <div class="sub">Fast checks and current read-model diagnostics.</div>
                </div>
              </div>
              <div class="muted-box">
                <p><strong>Domain:</strong> {escape(data["domain"])}</p>
                <p><strong>Redis Prefix:</strong> <code>{escape(data["streams"]["heartbeats"].split(":")[0])}</code></p>
                <p><strong>Broker Accounts:</strong> {data["counts"]["broker_accounts"]}</p>
                <p><strong>Strategies:</strong> {data["counts"]["strategies"]}</p>
                <p><strong>Open Incidents:</strong> {data["counts"]["open_incidents"]}</p>
                <p><strong>Latest Reconciliation Findings:</strong> {data["counts"]["latest_reconciliation_findings"]}</p>
                <p><strong>Latest Reconciliation Status:</strong> {escape(latest_reconciliation.get("status", "not-run"))}</p>
                <p><strong>Refresh:</strong> Every {refresh_seconds}s</p>
              </div>
              <div style="margin-top: 16px;">{errors_html}</div>
            </section>
          </div>

          <div class="grid-2">
            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Legacy Shadow</h2>
                  <div class="sub">Side-by-side divergence against the legacy VPS app.</div>
                </div>
              </div>
              <div class="muted-box">
                <p><strong>Status:</strong> {escape(shadow_divergence["status"])}</p>
                <p><strong>Connected:</strong> {"yes" if legacy_shadow["connected"] else "no"}</p>
                <p><strong>Fetched:</strong> {escape(legacy_shadow.get("fetched_at") or "")}</p>
                <p><strong>Total Shadow Issues:</strong> {shadow_divergence["issue_count"]}</p>
                <p><strong>Confirmed Only In Legacy:</strong> {escape(shadow_confirmed_legacy)}</p>
                <p><strong>Confirmed Only In New:</strong> {escape(shadow_confirmed_new)}</p>
              </div>
            </section>

            <section class="section">
              <div class="section-header">
                <div>
                  <h2>New Strategy State</h2>
                  <div class="sub">Latest in-memory strategy snapshot published by the new engine.</div>
                </div>
              </div>
              <div class="muted-box">
                <p><strong>Watchlist:</strong> {escape(", ".join(data["strategy_runtime"]["watchlist"][:12]) or "None")}</p>
                <p><strong>Top Confirmed Count:</strong> {len(data["strategy_runtime"]["top_confirmed"])}</p>
                <p><strong>Strategy Snapshots:</strong> {len(data["strategy_runtime"]["bots"])}</p>
              </div>
            </section>
          </div>

          <section class="section" id="shadow">
            <div class="section-header">
              <div>
                  <h2>Shadow Divergence</h2>
                <div class="sub">Confirmed-name drift, missing strategy wiring, watched symbol gaps, and position mismatches.</div>
              </div>
            </div>
            <div class="table-card">
              <table>
                <thead>
                  <tr><th>Strategy</th><th>Legacy Status</th><th>New Present</th><th>Watched Only Legacy</th><th>Watched Only New</th><th>Pos Mismatches</th><th>Issues</th></tr>
                </thead>
                <tbody>{shadow_strategy_rows}</tbody>
              </table>
            </div>
          </section>

          <div class="grid-2" id="reconciliation">
            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Reconciliation</h2>
                  <div class="sub">Latest shared-account consistency check across OMS state and attributed positions.</div>
                </div>
              </div>
              <div class="muted-box">
                <p><strong>Status:</strong> {escape(latest_reconciliation.get("status", "not-run"))}</p>
                <p><strong>Started:</strong> {escape(latest_reconciliation.get("started_at", ""))}</p>
                <p><strong>Completed:</strong> {escape(latest_reconciliation.get("completed_at", ""))}</p>
                <p><strong>Total Findings:</strong> {latest_reconciliation_summary.get("total_findings", 0)}</p>
                <p><strong>Critical Findings:</strong> {latest_reconciliation_summary.get("critical_findings", 0)}</p>
                <p><strong>Warning Findings:</strong> {latest_reconciliation_summary.get("warning_findings", 0)}</p>
              </div>
            </section>

            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Latest Findings</h2>
                  <div class="sub">Current blockers to shared-account correctness and safe cutover.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Severity</th><th>Type</th><th>Symbol</th><th>Title</th><th>Detected</th></tr>
                  </thead>
                  <tbody>{reconciliation_rows}</tbody>
                </table>
              </div>
            </section>
          </div>

          <div class="grid-2" id="orders">
            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Recent Intents</h2>
                  <div class="sub">Latest strategy decisions accepted by the event bus.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Strategy</th><th>Symbol</th><th>Type</th><th>Side</th><th>Qty</th><th>Status</th><th>Updated</th></tr>
                  </thead>
                  <tbody>{intents_rows}</tbody>
                </table>
              </div>
            </section>

            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Recent Orders</h2>
                  <div class="sub">Durable OMS order state keyed by client order id.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Strategy</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th><th>Client Id</th><th>Updated</th></tr>
                  </thead>
                  <tbody>{orders_rows}</tbody>
                </table>
              </div>
            </section>
          </div>

          <div class="grid-2" id="positions">
            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Recent Fills</h2>
                  <div class="sub">Execution reports persisted by the OMS layer.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Strategy</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Filled</th></tr>
                  </thead>
                  <tbody>{fills_rows}</tbody>
                </table>
              </div>
            </section>

            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Incidents</h2>
                  <div class="sub">Any control-plane or runtime issues that have been logged.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Service</th><th>Severity</th><th>Title</th><th>Status</th><th>Opened</th></tr>
                  </thead>
                  <tbody>{incidents_rows}</tbody>
                </table>
              </div>
            </section>
          </div>

          <div class="grid-2">
            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Virtual Positions</h2>
                  <div class="sub">Strategy-attributed holdings inside each broker account.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Strategy</th><th>Account</th><th>Symbol</th><th>Qty</th><th>Avg Px</th><th>Realized PnL</th><th>Updated</th></tr>
                  </thead>
                  <tbody>{virtual_positions_rows}</tbody>
                </table>
              </div>
            </section>

            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Account Positions</h2>
                  <div class="sub">Broker-account level holdings for reconciliation and operator checks.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Account</th><th>Symbol</th><th>Qty</th><th>Avg Px</th><th>Market Value</th><th>Updated</th></tr>
                  </thead>
                  <tbody>{account_positions_rows}</tbody>
                </table>
              </div>
            </section>
          </div>
        </div>
      </body>
    </html>
    """


BOT_PAGE_META = {
    "macd_30s": {"title": "30-Second MACD Bot", "emoji": "🤖", "color": "#2979ff", "path": "/bot/30s"},
    "macd_1m": {"title": "1-Minute MACD Bot", "emoji": "⏱️", "color": "#9c27b0", "path": "/bot/1m"},
    "tos": {"title": "TOS Bot", "emoji": "📊", "color": "#ff6f00", "path": "/bot/tos"},
    "runner": {"title": "Runner Bot", "emoji": "🚀", "color": "#e91e63", "path": "/bot/runner"},
}


def _find_bot_view(data: dict[str, Any], strategy_code: str) -> dict[str, Any] | None:
    return next(
        (bot for bot in data["bots"] if bot["strategy_code"] == strategy_code),
        None,
    )


def _build_bot_api_payload(data: dict[str, Any], strategy_code: str) -> dict[str, Any]:
    bot = _find_bot_view(data, strategy_code)
    if bot is None:
        return {"error": "Bot not initialized"}
    return {
        "status": bot["wiring_status"],
        "watched_tickers": bot["watchlist"],
        "positions": bot["positions"],
        "pending_open_symbols": bot["pending_open_symbols"],
        "pending_close_symbols": bot["pending_close_symbols"],
        "pending_scale_levels": bot["pending_scale_levels"],
        "daily_pnl": bot["daily_pnl"],
        "closed_today": bot["closed_today"],
        "recent_intents": bot["recent_intents"],
        "recent_orders": bot["recent_orders"],
        "recent_fills": bot["recent_fills"],
        "trade_log": _build_bot_decision_entries(bot),
    }


def _render_scanner_dashboard(data: dict[str, Any]) -> str:
    scanner = data["scanner"]
    scanner_state = data["strategy_runtime"]
    config = data["scanner_config"]
    latest_snapshot = data["market_data"]["latest_snapshot_batch"] or {}
    services = {service["service_name"]: service for service in data["services"]}
    market_data_service = services.get("market-data-gateway", {})
    subscription_symbols = set(scanner["subscription_symbols"])

    confirmed_rows = _render_scanner_confirmed_rows(scanner_state.get("top_confirmed", []), subscription_symbols)
    pillar_rows = _render_scanner_stock_rows(scanner["five_pillars"], subscription_symbols)
    gainer_rows = _render_scanner_stock_rows(scanner["top_gainers"], subscription_symbols)
    alert_rows = _render_alert_rows(scanner["recent_alerts"])

    warmup = scanner["alert_warmup"]
    websocket_status = market_data_service.get("status", "unknown")
    websocket_label = "⚡ live" if websocket_status == "healthy" else websocket_status
    reconcile_note = (
        "✅ All positions synced"
        if data["counts"]["open_incidents"] == 0
        else f'⚠️ {data["counts"]["open_incidents"]} open incidents'
    )
    top_gainer_change_rows = "".join(
        f"""<tr>
            <td>{escape(str(item.get("type", "")))}</td>
            <td><strong>{escape(str(item.get("ticker", "")))}</strong></td>
            <td>{escape(str(item.get("time", "")))}</td>
            <td>{escape(str(item.get("direction", item.get("rank", "-"))))}</td>
        </tr>"""
        for item in scanner.get("top_gainer_changes", [])[:20]
    ) or '<tr><td colspan="4" style="text-align:center;color:#7b86a4;padding:18px;">No top-gainer rank changes yet</td></tr>'

    watchlist_html = "".join(
        f'<span class="pill-chip">{escape(symbol)}</span>'
        for symbol in scanner["watchlist"][:24]
    ) or '<span style="color:#7b86a4;">No active watchlist symbols</span>'
    subscription_html = "".join(
        f'<span class="pill-chip live">{escape(symbol)}</span>'
        for symbol in scanner["subscription_symbols"][:24]
    ) or '<span style="color:#7b86a4;">No live subscriptions yet</span>'

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Momentum Scanner Dashboard</title>
    <meta http-equiv="refresh" content="10">
    <style>
        :root {{
            --bg: #131a2b;
            --bg-soft: #1a2238;
            --panel: #202b46;
            --panel-alt: #1c2540;
            --line: rgba(121, 146, 193, 0.28);
            --ink: #f0f4ff;
            --muted: #98a6c8;
            --cyan: #59d7ff;
            --green: #5fff8d;
            --lime: #8fff4d;
            --amber: #ffcc5b;
            --pink: #d05bff;
            --red: #ff6b6b;
        }}
        * {{ box-sizing: border-box; }}
        body {{ background:
            radial-gradient(circle at top left, rgba(89,215,255,0.08), transparent 28%),
            linear-gradient(180deg, #0f1525, var(--bg));
            color: var(--ink);
            font-family: 'Consolas','Monaco',monospace;
            margin: 0;
        }}
        .shell {{
            display: grid;
            grid-template-columns: 300px minmax(0, 1fr);
            gap: 16px;
            min-height: 100vh;
            padding: 16px;
        }}
        .sidebar {{
            position: sticky;
            top: 16px;
            align-self: start;
            background: rgba(17, 24, 41, 0.96);
            border: 1px solid var(--line);
            border-radius: 22px;
            padding: 18px;
            box-shadow: 0 24px 60px rgba(0, 0, 0, 0.28);
        }}
        .brand {{
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 20px;
        }}
        .brand-badge {{
            width: 68px;
            height: 68px;
            border-radius: 18px;
            display: grid;
            place-items: center;
            background: linear-gradient(135deg, #7b2cff, #59d7ff);
            color: white;
            font-weight: 700;
            text-align: center;
            font-size: 13px;
            line-height: 1.1;
            letter-spacing: 0.04em;
        }}
        .brand h1 {{ margin: 0; font-size: 24px; color: white; }}
        .brand p {{ margin: 4px 0 0 0; color: var(--muted); font-size: 13px; }}
        .side-section {{ margin-top: 18px; padding-top: 18px; border-top: 1px solid var(--line); }}
        .side-label {{
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--muted);
            margin-bottom: 10px;
        }}
        .metric-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
        }}
        .metric-card {{
            background: var(--panel-alt);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 10px;
        }}
        .metric-card strong {{ display: block; font-size: 22px; color: var(--cyan); }}
        .metric-card span {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
        .stack {{ display: grid; gap: 8px; }}
        .line-item {{
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 10px 12px;
            font-size: 13px;
            color: var(--muted);
        }}
        .line-item strong {{ color: var(--ink); }}
        .nav-grid {{
            display: grid;
            gap: 8px;
        }}
        .nav-grid a {{
            text-decoration: none;
            color: var(--ink);
            background: linear-gradient(180deg, rgba(89,215,255,0.08), rgba(255,255,255,0.02));
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 10px 12px;
            font-size: 13px;
        }}
        .pill-chip {{
            display: inline-flex;
            margin: 0 6px 6px 0;
            padding: 4px 8px;
            border-radius: 999px;
            background: rgba(255,255,255,0.06);
            border: 1px solid var(--line);
            color: var(--ink);
            font-size: 12px;
        }}
        .pill-chip.live {{
            background: rgba(95,255,141,0.12);
            color: var(--green);
        }}
        .workspace {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 16px;
            align-content: start;
        }}
        .panel {{
            background: rgba(24, 32, 54, 0.96);
            border: 1px solid var(--line);
            border-radius: 20px;
            overflow: hidden;
            box-shadow: 0 18px 42px rgba(0, 0, 0, 0.24);
            min-width: 0;
        }}
        .panel.full {{ grid-column: 1 / -1; }}
        .panel-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            padding: 14px 16px;
            background: linear-gradient(180deg, rgba(89,215,255,0.08), rgba(0,0,0,0));
            border-bottom: 1px solid var(--line);
        }}
        .panel-header h2, .panel-header h3 {{
            margin: 0;
            font-size: 16px;
            color: var(--ink);
        }}
        .panel-header .sub {{
            color: var(--muted);
            font-size: 12px;
            margin-top: 3px;
        }}
        .count {{
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 700;
            background: rgba(89,215,255,0.12);
            color: var(--cyan);
        }}
        .count.green {{ background: rgba(95,255,141,0.12); color: var(--green); }}
        .count.pink {{ background: rgba(208,91,255,0.12); color: #f1b3ff; }}
        .count.amber {{ background: rgba(255,204,91,0.12); color: var(--amber); }}
        .panel-copy {{
            padding: 14px 16px 0 16px;
            color: var(--muted);
            font-size: 12px;
        }}
        .table-wrap {{
            max-height: 420px;
            overflow: auto;
        }}
        table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
        th {{
            position: sticky;
            top: 0;
            z-index: 1;
            background: #253556;
            color: #b9c6e4;
            padding: 8px 10px;
            text-align: left;
            border-bottom: 1px solid var(--line);
        }}
        td {{
            padding: 8px 10px;
            border-bottom: 1px solid rgba(121, 146, 193, 0.16);
            color: #edf3ff;
            vertical-align: top;
        }}
        tbody tr:nth-child(odd) {{ background: rgba(255,255,255,0.015); }}
        tbody tr:hover {{ background: rgba(89,215,255,0.06); }}
        .status-positive {{ color: var(--green); }}
        .status-negative {{ color: var(--red); }}
        .mono-note {{
            color: var(--muted);
            font-size: 12px;
            line-height: 1.5;
            padding: 14px 16px 16px 16px;
        }}
        @media (max-width: 1200px) {{
            .shell {{ grid-template-columns: 1fr; }}
            .sidebar {{ position: static; }}
        }}
        @media (max-width: 900px) {{
            .workspace {{ grid-template-columns: 1fr; }}
            .panel.full {{ grid-column: auto; }}
        }}
    </style>
</head>
<body>
    <div class="shell">
        <aside class="sidebar">
            <div class="brand">
                <div class="brand-badge">MAI<br>TAI</div>
                <div>
                    <h1>Scanner Deck</h1>
                    <p>Dedicated scanner workspace for the new platform</p>
                </div>
            </div>

            <div class="metric-grid">
                <div class="metric-card">
                    <span>Confirmed</span>
                    <strong>{len(scanner_state.get("top_confirmed", []))}</strong>
                </div>
                <div class="metric-card">
                    <span>Pillars</span>
                    <strong>{scanner["five_pillars_count"]}</strong>
                </div>
                <div class="metric-card">
                    <span>Gainers</span>
                    <strong>{scanner["top_gainers_count"]}</strong>
                </div>
                <div class="metric-card">
                    <span>Alerts</span>
                    <strong>{scanner["recent_alerts_count"]}</strong>
                </div>
            </div>

            <div class="side-section">
                <div class="side-label">Overview</div>
                <div class="stack">
                    <div class="line-item"><strong>Status:</strong> {escape(scanner["status"])}</div>
                    <div class="line-item"><strong>Cycle:</strong> {scanner["cycle_count"]}</div>
                    <div class="line-item"><strong>Last Scan:</strong> {escape(latest_snapshot.get("completed_at", "N/A"))}</div>
                    <div class="line-item"><strong>Ref Tickers:</strong> {latest_snapshot.get("reference_count", 0):,}</div>
                    <div class="line-item"><strong>WebSocket:</strong> {escape(websocket_label)} ({scanner["active_subscription_symbols"]} subs)</div>
                    <div class="line-item"><strong>Reconcile:</strong> {escape(reconcile_note)}</div>
                </div>
            </div>

            <div class="side-section">
                <div class="side-label">Warmup</div>
                <div class="stack">
                    <div class="line-item"><strong>5m Squeeze:</strong> {"Ready" if warmup.get("squeeze_5min_ready") else "Warming"}</div>
                    <div class="line-item"><strong>10m Squeeze:</strong> {"Ready" if warmup.get("squeeze_10min_ready") else "Warming"}</div>
                    <div class="line-item"><strong>Alert Engine:</strong> {"Ready" if warmup.get("fully_ready") else "History building"}</div>
                </div>
            </div>

            <div class="side-section">
                <div class="side-label">Navigation</div>
                <div class="nav-grid">
                    <a href="/scanner/dashboard">📡 Scanner</a>
                    <a href="/">🧭 Control Plane</a>
                    <a href="/bot/30s">🤖 30s Bot</a>
                    <a href="/bot/1m">⏱️ 1m Bot</a>
                    <a href="/bot/tos">📊 TOS Bot</a>
                    <a href="/bot/runner">🚀 Runner Bot</a>
                </div>
            </div>

            <div class="side-section">
                <div class="side-label">Active Watchlist</div>
                <div>{watchlist_html}</div>
            </div>

            <div class="side-section">
                <div class="side-label">Live Subscriptions</div>
                <div>{subscription_html}</div>
            </div>

            <div class="side-section">
                <div class="side-label">Scanner Rules</div>
                <div class="stack">
                    <div class="line-item"><strong>5 Pillars:</strong> ${config["five_pillars"]["min_price"]:.0f}-${config["five_pillars"]["max_price"]:.0f}, float ≤ {_short_volume(config["five_pillars"]["max_float"])}, volume ≥ {_short_volume(config["five_pillars"]["min_today_volume"])}, rvol ≥ {config["five_pillars"]["min_rvol_5pillars"]}x</div>
                    <div class="line-item"><strong>Top Gainers:</strong> top {config["top_gainers"]["top_gainers_count"]} with rvol ≥ {config["top_gainers"]["min_rvol_top_gainers"]}x</div>
                    <div class="line-item"><strong>Alerts:</strong> squeeze 5m {config["momentum_alerts"]["squeeze_5min_pct"]}% / 10m {config["momentum_alerts"]["squeeze_10min_pct"]}% / spike {config["momentum_alerts"]["volume_spike_mult"]}x</div>
                </div>
            </div>
        </aside>

        <main class="workspace">
            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h2>Momentum Confirmed</h2>
                        <div class="sub">Bot-ready candidates from the new scanner runtime.</div>
                    </div>
                    <span class="count green">{len(scanner_state.get("top_confirmed", []))} names</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>#</th><th>Ticker / Bot</th><th>Path</th><th>Score</th><th>Confirmed</th><th>Entry Price</th><th>Price</th><th>Change%</th><th>Bid</th><th>Ask</th><th>Spread</th><th>Volume</th><th>RVol</th><th>Float</th><th>Squeezes</th><th>1st Spike</th><th>Catalyst</th><th>📰</th><th>🚫</th></tr></thead>
                        <tbody>{confirmed_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel">
                <div class="panel-header">
                    <div>
                        <h3>5 Pillars Scanner</h3>
                        <div class="sub">Qualifying names across the preserved five-pillar filter.</div>
                    </div>
                    <span class="count green">{scanner["five_pillars_count"]}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>#</th><th>Ticker</th><th>First Seen</th><th>Price</th><th>Change%</th><th>Bid</th><th>Ask</th><th>Spread</th><th>Volume</th><th>RVol</th><th>Float</th><th>HOD</th><th>VWAP</th><th>Prev Close</th><th>Age</th></tr></thead>
                        <tbody>{pillar_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel">
                <div class="panel-header">
                    <div>
                        <h3>Top Gainers</h3>
                        <div class="sub">Independent ranker refreshed from snapshot state.</div>
                    </div>
                    <span class="count pink">{scanner["top_gainers_count"]}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>#</th><th>Ticker</th><th>First Seen</th><th>Price</th><th>Change%</th><th>Bid</th><th>Ask</th><th>Spread</th><th>Volume</th><th>RVol</th><th>Float</th><th>HOD</th><th>VWAP</th><th>Prev Close</th><th>Age</th></tr></thead>
                        <tbody>{gainer_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel">
                <div class="panel-header">
                    <div>
                        <h3>Momentum Alerts</h3>
                        <div class="sub">Recent alert tape across spike and squeeze detectors.</div>
                    </div>
                    <span class="count amber">{scanner["recent_alerts_count"]}</span>
                </div>
                <div class="panel-copy">Warmup: {"Ready" if warmup.get("fully_ready") else "History building"} | 5m ready: {"yes" if warmup.get("squeeze_5min_ready") else "no"} | 10m ready: {"yes" if warmup.get("squeeze_10min_ready") else "no"}</div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Type</th><th>Ticker</th><th>Price</th><th>Bid</th><th>Ask</th><th>Volume</th><th>Float</th><th>Details</th><th>Time</th></tr></thead>
                        <tbody>{alert_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel">
                <div class="panel-header">
                    <div>
                        <h3>Top Gainer Changes</h3>
                        <div class="sub">Rank moves, new entrants, and drops in the top-gainer deck.</div>
                    </div>
                    <span class="count">{len(scanner.get("top_gainer_changes", []))}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Type</th><th>Ticker</th><th>Time</th><th>Move</th></tr></thead>
                        <tbody>{top_gainer_change_rows}</tbody>
                    </table>
                </div>
            </section>
        </main>
    </div>
</body>
</html>"""


def _render_bot_detail_page(data: dict[str, Any], strategy_code: str) -> str:
    bot = _find_bot_view(data, strategy_code)
    if bot is None:
        return "<h1>Bot not initialized</h1>"

    meta = BOT_PAGE_META[strategy_code]
    recent_fills = [item for item in data["recent_fills"] if item["strategy_code"] == strategy_code][:50]
    decision_entries = _build_bot_decision_entries(bot)
    position_rows = _build_bot_position_rows(data, bot)
    closed_today = list(bot.get("closed_today", []))
    closed_rows = _build_closed_trade_rows(closed_today)
    trades_rows = _build_bot_fill_rows(recent_fills)
    intent_rows = _build_bot_intent_rows(bot)
    order_rows = _build_bot_order_rows(bot)
    account_rows = _build_bot_account_rows(data, bot)
    decision_lines = _build_bot_decision_lines(decision_entries)
    watchlist_html = _render_chip_cloud(bot["watchlist"], empty_text="No symbols on watch")
    pending_open_html = _render_chip_cloud(bot["pending_open_symbols"], variant="live", empty_text="None queued")
    pending_close_html = _render_chip_cloud(bot["pending_close_symbols"], variant="danger", empty_text="None queued")
    pending_scale_html = _render_chip_cloud(bot["pending_scale_levels"], variant="amber", empty_text="No scale levels armed")
    account_note = (
        "Shared Alpaca paper account with sibling strategy. Account-level quantities can include sibling exposure."
        if strategy_code in {"tos", "runner"}
        else "Dedicated paper account mapped to this bot."
    )
    pnl_color = "#5fff8d" if bot["daily_pnl"] >= 0 else "#ff6b6b"
    recent_fill_count = len(recent_fills)
    current_position = bot["positions"][0] if strategy_code == "runner" and bot["positions"] else None

    runner_status_panel = ""
    if strategy_code == "runner":
        if current_position:
            current_profit_pct = _as_float(current_position.get("current_profit_pct"))
            runner_color = "#5fff8d" if current_profit_pct >= 0 else "#ff6b6b"
            runner_status_panel = f"""
            <section class="panel full accent-panel">
                <div class="panel-header">
                    <div>
                        <h2>Current Runner Ride</h2>
                        <div class="sub">Live runner management, trailing protection, and fade checks.</div>
                    </div>
                    <span class="count accent">{escape(str(current_position.get("ticker", "")))}</span>
                </div>
                <div class="hero-grid">
                    <div class="hero-card"><span>Entry</span><strong>{_fmt_money(_as_float(current_position.get("entry_price")))}</strong><small>{escape(str(current_position.get("entry_time", "")))}</small></div>
                    <div class="hero-card"><span>Current</span><strong>{_fmt_money(_as_float(current_position.get("current_price")))}</strong><small>Qty {escape(str(current_position.get("quantity", 0)))}</small></div>
                    <div class="hero-card"><span>P&L</span><strong style="color:{runner_color}">{current_profit_pct:+.1f}%</strong><small>Peak {_as_float(current_position.get("peak_profit_pct")):.1f}%</small></div>
                    <div class="hero-card"><span>Trail</span><strong>{_as_float(current_position.get("trail_pct")):.0f}%</strong><small>Stop {_fmt_money(_as_float(current_position.get("trail_stop")))}</small></div>
                    <div class="hero-card"><span>Vol Fade</span><strong>{"YES" if current_position.get("volume_faded") else "NO"}</strong><small>Entry Δ {_as_float(current_position.get("entry_change_pct")):+.1f}%</small></div>
                </div>
            </section>"""
        else:
            runner_status_panel = """
            <section class="panel full accent-panel">
                <div class="panel-header">
                    <div>
                        <h2>Current Runner Ride</h2>
                        <div class="sub">The runner engine is waiting for a fresh breakout candidate.</div>
                    </div>
                    <span class="count accent">Idle</span>
                </div>
                <div class="panel-copy">Scanning for runners... Score≥70, Change≥35%, after 7AM, accelerating, then managed with tiered trails and EMA checks.</div>
            </section>"""

    closed_trades_panel = ""
    if strategy_code == "runner":
        closed_trades_panel = f"""
        <section class="panel">
            <div class="panel-header">
                <div>
                    <h3>Closed Trades</h3>
                    <div class="sub">Completed runner rides captured by the strategy runtime.</div>
                </div>
                <span class="count pink">{len(closed_today)}</span>
            </div>
            <div class="table-wrap">
                <table>
                    <thead><tr><th>Ticker</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th><th>Peak</th><th>Duration</th></tr></thead>
                    <tbody>{closed_rows}</tbody>
                </table>
            </div>
        </section>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>{meta["emoji"]} {meta["title"]}</title>
    <meta http-equiv="refresh" content="10">
    <style>
        :root {{
            --bg: #131a2b;
            --panel: #202b46;
            --panel-alt: #1c2540;
            --line: rgba(121, 146, 193, 0.28);
            --ink: #f0f4ff;
            --muted: #98a6c8;
            --cyan: #59d7ff;
            --green: #5fff8d;
            --amber: #ffcc5b;
            --pink: #d05bff;
            --red: #ff6b6b;
            --accent: {meta["color"]};
        }}
        * {{ box-sizing: border-box; }}
        body {{
            background:
                radial-gradient(circle at top left, rgba(89,215,255,0.08), transparent 28%),
                linear-gradient(180deg, #0f1525, var(--bg));
            color: var(--ink);
            font-family: 'Consolas','Monaco',monospace;
            margin: 0;
        }}
        .shell {{
            display: grid;
            grid-template-columns: 300px minmax(0, 1fr);
            gap: 16px;
            min-height: 100vh;
            padding: 16px;
        }}
        .sidebar {{
            position: sticky;
            top: 16px;
            align-self: start;
            background: rgba(17, 24, 41, 0.96);
            border: 1px solid var(--line);
            border-radius: 22px;
            padding: 18px;
            box-shadow: 0 24px 60px rgba(0, 0, 0, 0.28);
        }}
        .brand {{
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 20px;
        }}
        .brand-badge {{
            width: 68px;
            height: 68px;
            border-radius: 18px;
            display: grid;
            place-items: center;
            background: linear-gradient(135deg, var(--accent), #59d7ff);
            color: white;
            font-weight: 700;
            text-align: center;
            font-size: 24px;
            line-height: 1;
        }}
        .brand h1 {{ margin: 0; font-size: 24px; color: white; }}
        .brand p {{ margin: 4px 0 0 0; color: var(--muted); font-size: 13px; }}
        .side-section {{ margin-top: 18px; padding-top: 18px; border-top: 1px solid var(--line); }}
        .side-label {{
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--muted);
            margin-bottom: 10px;
        }}
        .metric-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
        }}
        .metric-card {{
            background: var(--panel-alt);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 10px;
        }}
        .metric-card strong {{ display: block; font-size: 22px; color: var(--cyan); }}
        .metric-card span {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
        .stack {{ display: grid; gap: 8px; }}
        .line-item {{
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 10px 12px;
            font-size: 13px;
            color: var(--muted);
            line-height: 1.45;
        }}
        .line-item strong {{ color: var(--ink); }}
        .nav-grid {{
            display: grid;
            gap: 8px;
        }}
        .nav-grid a {{
            text-decoration: none;
            color: var(--ink);
            background: linear-gradient(180deg, rgba(89,215,255,0.08), rgba(255,255,255,0.02));
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 10px 12px;
            font-size: 13px;
        }}
        .nav-grid a.active {{
            border-color: var(--accent);
            box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 80%, white 20%);
            background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 18%, transparent), rgba(255,255,255,0.02));
        }}
        .pill-chip {{
            display: inline-flex;
            margin: 0 6px 6px 0;
            padding: 4px 8px;
            border-radius: 999px;
            background: rgba(255,255,255,0.06);
            border: 1px solid var(--line);
            color: var(--ink);
            font-size: 12px;
        }}
        .pill-chip.live {{ background: rgba(95,255,141,0.12); color: var(--green); }}
        .pill-chip.danger {{ background: rgba(255,107,107,0.12); color: var(--red); }}
        .pill-chip.amber {{ background: rgba(255,204,91,0.12); color: var(--amber); }}
        .queue-group {{ margin-bottom: 10px; }}
        .queue-group strong {{ display: block; margin-bottom: 6px; color: var(--ink); font-size: 12px; }}
        .workspace {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 16px;
            align-content: start;
        }}
        .panel {{
            background: rgba(24, 32, 54, 0.96);
            border: 1px solid var(--line);
            border-radius: 20px;
            overflow: hidden;
            box-shadow: 0 18px 42px rgba(0, 0, 0, 0.24);
            min-width: 0;
        }}
        .panel.full {{ grid-column: 1 / -1; }}
        .accent-panel {{
            border-color: color-mix(in srgb, var(--accent) 52%, rgba(121,146,193,0.28));
            box-shadow: 0 18px 42px rgba(0, 0, 0, 0.24), 0 0 0 1px color-mix(in srgb, var(--accent) 22%, transparent);
        }}
        .panel-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            padding: 14px 16px;
            background: linear-gradient(180deg, rgba(89,215,255,0.08), rgba(0,0,0,0));
            border-bottom: 1px solid var(--line);
        }}
        .panel-header h2, .panel-header h3 {{
            margin: 0;
            font-size: 16px;
            color: var(--ink);
        }}
        .panel-header .sub {{
            color: var(--muted);
            font-size: 12px;
            margin-top: 3px;
        }}
        .count {{
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 700;
            background: rgba(89,215,255,0.12);
            color: var(--cyan);
        }}
        .count.accent {{
            background: color-mix(in srgb, var(--accent) 18%, transparent);
            color: color-mix(in srgb, var(--accent) 75%, white 25%);
        }}
        .count.pink {{ background: rgba(208,91,255,0.12); color: #f1b3ff; }}
        .panel-copy {{
            padding: 14px 16px;
            color: var(--muted);
            font-size: 12px;
            line-height: 1.5;
        }}
        .hero-grid {{
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 12px;
            padding: 0 16px 16px 16px;
        }}
        .hero-card {{
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 12px;
        }}
        .hero-card span {{
            display: block;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--muted);
            margin-bottom: 6px;
        }}
        .hero-card strong {{ display: block; font-size: 18px; color: var(--ink); }}
        .hero-card small {{ color: var(--muted); font-size: 11px; }}
        .badge-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            padding: 0 16px 16px 16px;
        }}
        .badge {{
            padding: 6px 10px;
            border-radius: 999px;
            font-size: 12px;
            border: 1px solid var(--line);
            background: rgba(255,255,255,0.04);
        }}
        .table-wrap {{
            max-height: 420px;
            overflow: auto;
        }}
        table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
        th {{
            position: sticky;
            top: 0;
            z-index: 1;
            background: #253556;
            color: #b9c6e4;
            padding: 8px 10px;
            text-align: left;
            border-bottom: 1px solid var(--line);
        }}
        td {{
            padding: 8px 10px;
            border-bottom: 1px solid rgba(121, 146, 193, 0.16);
            color: #edf3ff;
            vertical-align: top;
        }}
        tbody tr:nth-child(odd) {{ background: rgba(255,255,255,0.015); }}
        tbody tr:hover {{ background: rgba(89,215,255,0.06); }}
        .log-wrap {{
            padding: 12px 16px 16px 16px;
            display: grid;
            gap: 8px;
        }}
        .log-line {{
            font-size: 12px;
            padding: 8px 10px;
            border-radius: 10px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(121, 146, 193, 0.16);
        }}
        @media (max-width: 1200px) {{
            .shell {{ grid-template-columns: 1fr; }}
            .sidebar {{ position: static; }}
            .hero-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
        }}
        @media (max-width: 900px) {{
            .workspace {{ grid-template-columns: 1fr; }}
            .panel.full {{ grid-column: auto; }}
            .hero-grid {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
    <div class="shell">
        <aside class="sidebar">
            <div class="brand">
                <div class="brand-badge">{meta["emoji"]}</div>
                <div>
                    <h1>{meta["title"]}</h1>
                    <p>Dedicated execution workspace for this bot.</p>
                </div>
            </div>

            <div class="metric-grid">
                <div class="metric-card">
                    <span>Watchlist</span>
                    <strong>{bot["watchlist_count"]}</strong>
                </div>
                <div class="metric-card">
                    <span>Open</span>
                    <strong>{bot["position_count"]}</strong>
                </div>
                <div class="metric-card">
                    <span>Pending</span>
                    <strong>{bot["pending_count"]}</strong>
                </div>
                <div class="metric-card">
                    <span>Fills</span>
                    <strong>{recent_fill_count}</strong>
                </div>
            </div>

            <div class="side-section">
                <div class="side-label">Overview</div>
                <div class="stack">
                    <div class="line-item"><strong>Execution:</strong> {escape(bot["wiring_status"].upper())}</div>
                    <div class="line-item"><strong>Account:</strong> {escape(bot["account_name"])}</div>
                    <div class="line-item"><strong>Legacy Shadow:</strong> {escape(bot["legacy_status"])}</div>
                    <div class="line-item"><strong>P&amp;L Day:</strong> <span style="color:{pnl_color};">${bot["daily_pnl"]:+,.2f}</span></div>
                    <div class="line-item"><strong>Account Model:</strong> {escape(account_note)}</div>
                </div>
            </div>

            <div class="side-section">
                <div class="side-label">Navigation</div>
                {_render_page_nav(strategy_code)}
            </div>

            <div class="side-section">
                <div class="side-label">Watchlist</div>
                <div>{watchlist_html}</div>
            </div>

            <div class="side-section">
                <div class="side-label">Pending Workflow</div>
                <div class="queue-group"><strong>Open Queue</strong><div>{pending_open_html}</div></div>
                <div class="queue-group"><strong>Close Queue</strong><div>{pending_close_html}</div></div>
                <div class="queue-group"><strong>Scale Queue</strong><div>{pending_scale_html}</div></div>
            </div>

            <div class="side-section">
                <div class="side-label">Bot Notes</div>
                <div class="stack">
                    <div class="line-item"><strong>Trade Log:</strong> Recent intents, orders, and fills are shown on the right for quick operator review.</div>
                    <div class="line-item"><strong>Reconcile:</strong> Use account exposure and position status together to catch drift before cutover.</div>
                    <div class="line-item"><strong>Strategy:</strong> {"Score≥70 | Change≥35% | After 7AM | Accelerating | Tiered trail + EMA break" if strategy_code == "runner" else "Preserved entry, exit, and position-tracking logic from the legacy runtime."}</div>
                </div>
            </div>
        </aside>

        <main class="workspace">
            <section class="panel full accent-panel">
                <div class="panel-header">
                    <div>
                        <h2>{meta["title"]}</h2>
                        <div class="sub">Bot deck view for execution readiness, activity, and broker alignment.</div>
                    </div>
                    <span class="count accent">{escape(bot["display_name"])}</span>
                </div>
                <div class="badge-row">
                    <span class="badge">Execution Workspace</span>
                    <span class="badge">Mode {escape(bot["execution_mode"].upper())}</span>
                    <span class="badge">Provider {escape(bot["provider"].upper())}</span>
                    <span class="badge">Legacy Shadow {escape(bot["legacy_status"])}</span>
                </div>
                <div class="hero-grid">
                    <div class="hero-card"><span>Watchlist</span><strong>{bot["watchlist_count"]}</strong><small>Symbols assigned to this bot</small></div>
                    <div class="hero-card"><span>Open Positions</span><strong>{bot["position_count"]}</strong><small>Strategy-owned live positions</small></div>
                    <div class="hero-card"><span>Pending Actions</span><strong>{bot["pending_count"]}</strong><small>Open, close, and scale queues</small></div>
                    <div class="hero-card"><span>Recent Fills</span><strong>{recent_fill_count}</strong><small>Latest fills tracked by OMS</small></div>
                    <div class="hero-card"><span>P&amp;L Day</span><strong style="color:{pnl_color};">${bot["daily_pnl"]:+,.2f}</strong><small>{escape(bot["account_name"])}</small></div>
                </div>
            </section>

            {runner_status_panel}

            <section class="panel">
                <div class="panel-header">
                    <div>
                        <h3>Open Positions</h3>
                        <div class="sub">Runtime, virtual, and account quantities side by side.</div>
                    </div>
                    <span class="count">{bot["position_count"]}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Ticker</th><th style="text-align:right">Bot Qty<br>Time</th><th style="text-align:right">Bot $</th><th style="text-align:right">Virtual Qty<br>Avg</th><th style="text-align:right">Account Qty<br>Current</th><th style="text-align:right">P&amp;L</th><th>Status</th></tr></thead>
                        <tbody>{position_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel">
                <div class="panel-header">
                    <div>
                        <h3>Recent Trades</h3>
                        <div class="sub">Filled executions seen by OMS for this bot.</div>
                    </div>
                    <span class="count">{recent_fill_count}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Filled</th><th>Side</th><th>Ticker</th><th style="text-align:right">Qty</th><th style="text-align:right">Fill $</th><th style="text-align:right">Total $</th></tr></thead>
                        <tbody>{trades_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel">
                <div class="panel-header">
                    <div>
                        <h3>Trade Intents</h3>
                        <div class="sub">Latest strategy intents emitted into OMS.</div>
                    </div>
                    <span class="count">{len(bot["recent_intents"])}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Updated</th><th>Type</th><th>Side</th><th>Ticker</th><th style="text-align:right">Qty</th><th>Reason</th><th>Status</th></tr></thead>
                        <tbody>{intent_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel">
                <div class="panel-header">
                    <div>
                        <h3>Recent Orders</h3>
                        <div class="sub">Broker-order lifecycle tied back to the same bot.</div>
                    </div>
                    <span class="count pink">{len(bot["recent_orders"])}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Updated</th><th>Side</th><th>Ticker</th><th style="text-align:right">Qty</th><th>Status</th><th>Client Order ID</th></tr></thead>
                        <tbody>{order_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel">
                <div class="panel-header">
                    <div>
                        <h3>Account Exposure</h3>
                        <div class="sub">Broker-account positions under this bot's mapped account.</div>
                    </div>
                    <span class="count">{len([item for item in data["account_positions"] if item.get("broker_account_name") == bot["account_name"]])}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Account</th><th>Ticker</th><th style="text-align:right">Qty</th><th style="text-align:right">Avg Px</th><th style="text-align:right">Market Value</th><th>Updated</th></tr></thead>
                        <tbody>{account_rows}</tbody>
                    </table>
                </div>
            </section>

            {closed_trades_panel}

            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>Decision Tape</h3>
                        <div class="sub">Compact log of the bot's most recent intent activity.</div>
                    </div>
                    <span class="count accent">{len(decision_entries)}</span>
                </div>
                <div class="log-wrap">{decision_lines}</div>
            </section>
        </main>
    </div>
</body>
</html>"""


def _render_page_nav(active: str) -> str:
    links: list[str] = []
    for code, meta in BOT_PAGE_META.items():
        links.append(
            f'<a href="{meta["path"]}" class="{"active" if code == active else ""}">{meta["emoji"]} {escape(meta["title"].replace(" Bot", ""))}</a>'
        )
    return (
        '<div class="nav-grid">'
        '<a href="/scanner/dashboard">📡 Scanner</a>'
        + "".join(links)
        + '<a href="/">💚 Control Plane</a>'
        + "</div>"
    )


def _render_chip_cloud(items: list[str], *, variant: str = "", empty_text: str = "None") -> str:
    if not items:
        return f'<span style="color:#7b86a4;">{escape(empty_text)}</span>'
    class_name = f"pill-chip {variant}".strip()
    return "".join(f'<span class="{class_name}">{escape(str(item))}</span>' for item in items)


def _build_bot_fill_rows(recent_fills: list[dict[str, Any]]) -> str:
    if not recent_fills:
        return '<tr><td colspan="6" style="text-align:center;color:#7b86a4;padding:15px;">No trades yet</td></tr>'
    return "".join(
        f"""<tr>
            <td style="font-size:11px">{escape(item["filled_at"])}</td>
            <td style="color:{'#00c853' if item['side'] == 'buy' else '#ff1744'};font-weight:bold">{escape(item["side"].upper())}</td>
            <td><strong>{escape(item["symbol"])}</strong></td>
            <td style="text-align:right">{escape(item["quantity"])}</td>
            <td style="text-align:right">${escape(item["price"])}</td>
            <td style="text-align:right">${_decimal_total(item["quantity"], item["price"])}</td>
        </tr>"""
        for item in recent_fills
    )


def _build_bot_intent_rows(bot: dict[str, Any]) -> str:
    intents = bot["recent_intents"]
    if not intents:
        return '<tr><td colspan="7" style="text-align:center;color:#7b86a4;padding:15px;">No intents yet</td></tr>'
    rows: list[str] = []
    for item in intents:
        status_color = "#5fff8d" if item["status"] in {"filled", "submitted", "accepted"} else "#ffcc5b"
        rows.append(
            f"""<tr>
            <td>{escape(item["updated_at"])}</td>
            <td>{escape(item["intent_type"].upper())}</td>
            <td>{escape(item["side"].upper())}</td>
            <td><strong>{escape(item["symbol"])}</strong></td>
            <td style="text-align:right">{escape(item["quantity"])}</td>
            <td>{escape(item["reason"])}</td>
            <td style="color:{status_color}">{escape(item["status"].upper())}</td>
        </tr>"""
        )
    return "".join(rows)


def _build_bot_order_rows(bot: dict[str, Any]) -> str:
    orders = bot["recent_orders"]
    if not orders:
        return '<tr><td colspan="6" style="text-align:center;color:#7b86a4;padding:15px;">No orders yet</td></tr>'
    rows: list[str] = []
    for item in orders:
        side_color = "#00c853" if item["side"] == "buy" else "#ff1744"
        client_order_id = str(item.get("client_order_id", ""))
        rows.append(
            f"""<tr>
            <td>{escape(item["updated_at"])}</td>
            <td style="color:{side_color};font-weight:bold;">{escape(item["side"].upper())}</td>
            <td><strong>{escape(item["symbol"])}</strong></td>
            <td style="text-align:right">{escape(item["quantity"])}</td>
            <td>{escape(item["status"].upper())}</td>
            <td style="font-size:11px;">{escape(client_order_id[-24:] if len(client_order_id) > 24 else client_order_id)}</td>
        </tr>"""
        )
    return "".join(rows)


def _build_bot_account_rows(data: dict[str, Any], bot: dict[str, Any]) -> str:
    rows = [
        item for item in data["account_positions"] if item.get("broker_account_name") == bot["account_name"]
    ]
    if not rows:
        return '<tr><td colspan="6" style="text-align:center;color:#7b86a4;padding:15px;">No broker-account positions</td></tr>'
    return "".join(
        f"""<tr>
            <td>{escape(str(item.get("broker_account_name", "")))}</td>
            <td><strong>{escape(str(item.get("symbol", "")))}</strong></td>
            <td style="text-align:right">{escape(str(item.get("quantity", "")))}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("average_price")))}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("market_value")))}</td>
            <td>{escape(str(item.get("updated_at", "")))}</td>
        </tr>"""
        for item in rows
    )


def _build_bot_decision_lines(decision_entries: list[dict[str, str]]) -> str:
    if not decision_entries:
        return '<div class="log-line" style="color:#7b86a4;text-align:center;">No decisions yet</div>'
    return "".join(
        f'<div class="log-line" style="color:{entry["color"]};">{escape(entry["text"])}</div>'
        for entry in decision_entries
    )


def _build_bot_position_rows(data: dict[str, Any], bot: dict[str, Any]) -> str:
    strategy_code = bot["strategy_code"]
    account_name = bot["account_name"]
    runtime_positions = {str(item.get("ticker", "")).upper(): item for item in bot["positions"] if item.get("ticker")}
    virtual_positions = {
        str(item.get("symbol", "")).upper(): item
        for item in data["virtual_positions"]
        if item.get("strategy_code") == strategy_code
    }
    account_positions = {
        str(item.get("symbol", "")).upper(): item
        for item in data["account_positions"]
        if item.get("broker_account_name") == account_name
    }

    symbols = sorted(set(runtime_positions) | set(virtual_positions) | set(account_positions))
    if not symbols:
        return '<tr><td colspan="7" style="text-align:center;color:#888;padding:15px;">No open positions</td></tr>'

    rows: list[str] = []
    for symbol in symbols:
        runtime = runtime_positions.get(symbol)
        virtual = virtual_positions.get(symbol)
        account = account_positions.get(symbol)

        runtime_qty = _as_float(runtime.get("quantity")) if runtime else 0.0
        virtual_qty = _as_float(virtual.get("quantity")) if virtual else 0.0
        account_qty = _as_float(account.get("quantity")) if account else 0.0
        runtime_entry = _as_float(runtime.get("entry_price")) if runtime else 0.0
        virtual_avg = _as_float(virtual.get("average_price")) if virtual else 0.0
        account_market_value = _as_float(account.get("market_value")) if account else 0.0
        current_price = 0.0
        if account and account_qty:
            current_price = account_market_value / max(account_qty, 0.0001)
        elif runtime:
            current_price = _as_float(runtime.get("current_price"))

        if runtime and virtual and abs(runtime_qty - virtual_qty) < 0.0001:
            status_html = '<span style="color:#00c853">✅ SYNCED</span>'
        elif runtime and virtual:
            status_html = f'<span style="color:#ff9100">⚠️ VQTY: bot={runtime_qty:g} virt={virtual_qty:g}</span>'
        elif account and not runtime:
            status_html = '<span style="color:#ff1744">⚠️ ACCOUNT-ONLY</span>'
        elif runtime and not account and strategy_code in {"macd_30s", "macd_1m"}:
            status_html = '<span style="color:#ff1744">⚠️ GHOST (not on broker)</span>'
        else:
            status_html = '<span style="color:#888">—</span>'

        pnl_amount = 0.0
        pnl_pct = 0.0
        if current_price > 0 and runtime_entry > 0 and runtime_qty > 0:
            pnl_amount = (current_price - runtime_entry) * runtime_qty
            pnl_pct = ((current_price - runtime_entry) / runtime_entry) * 100
        pnl_color = "#00c853" if pnl_amount >= 0 else "#ff1744"
        time_text = escape(str(runtime.get("entry_time", ""))) if runtime else "—"

        rows.append(
            f"""<tr style="border-bottom:1px solid #222;">
            <td><strong>{escape(symbol)}</strong></td>
            <td style="text-align:right">{_fmt_qty(runtime_qty)}<br><span style="font-size:10px;color:#888;">{time_text}</span></td>
            <td style="text-align:right">{_fmt_money(runtime_entry)}</td>
            <td style="text-align:right">{_fmt_qty(virtual_qty)}<br><span style="font-size:10px;color:#888;">{_fmt_money(virtual_avg)}</span></td>
            <td style="text-align:right">{_fmt_qty(account_qty)}<br><span style="font-size:10px;color:#888;">{_fmt_money(current_price)}</span></td>
            <td style="text-align:right;color:{pnl_color}"><strong>${pnl_amount:+.2f}</strong><br><strong>{pnl_pct:+.1f}%</strong></td>
            <td>{status_html}</td>
        </tr>"""
        )
    return "".join(rows)


def _build_bot_decision_entries(bot: dict[str, Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for item in bot["recent_intents"]:
        entries.append(
            {
                "color": "#00c853" if item["intent_type"] == "open" else "#ffd600" if item["intent_type"] == "scale" else "#ff1744",
                "text": f'{item["updated_at"]} {item["intent_type"].upper()} {item["symbol"]} {item["side"].upper()} qty={item["quantity"]} | {item["reason"]} | {item["status"].upper()}',
            }
        )
    return entries[:50]


def _build_closed_trade_rows(closed_today: list[dict[str, Any]]) -> str:
    if not closed_today:
        return '<tr><td colspan="7" style="text-align:center;color:#888;">No closed trades</td></tr>'
    rows = []
    for item in closed_today:
        pnl = _as_float(item.get("pnl"))
        color = "#00c853" if pnl >= 0 else "#ff1744"
        rows.append(
            f"""<tr>
            <td>{escape(str(item.get("ticker", "")))}</td>
            <td>{_fmt_money(_as_float(item.get("entry_price")))}</td>
            <td>{_fmt_money(_as_float(item.get("exit_price")))}</td>
            <td style="color:{color}">${pnl:+.2f} ({_as_float(item.get("pnl_pct")):+.1f}%)</td>
            <td>{escape(str(item.get("reason", "")))}</td>
            <td>pk:{_as_float(item.get("peak_profit_pct")):.1f}%</td>
            <td>{escape(str(item.get("entry_time", "")))}→{escape(str(item.get("exit_time", "")))}</td>
        </tr>"""
        )
    return "".join(rows)


def _render_scanner_confirmed_rows(rows: list[dict[str, Any]], live_symbols: set[str]) -> str:
    if not rows:
        return '<tr><td colspan="19" style="text-align:center;color:#888;padding:20px;">No confirmed candidates yet</td></tr>'
    rendered = []
    for index, item in enumerate(rows, start=1):
        ticker = str(item.get("ticker", "")).upper()
        live_badge = ' <span style="color:#00ff41;font-size:10px;">⚡LIVE</span>' if ticker in live_symbols else ""
        watched_by = ", ".join(item.get("watched_by", [])) if item.get("watched_by") else "—"
        path = str(item.get("confirmation_path", ""))
        path_badge = (
            '<span style="background:#ff9100;color:#000;font-size:9px;padding:1px 4px;border-radius:3px;">A</span>'
            if "PATH_A" in path
            else '<span style="background:#2979ff;color:#fff;font-size:9px;padding:1px 4px;border-radius:3px;">B</span>'
        )
        top5_badge = (
            ' <span style="background:#ffd600;color:#000;font-size:9px;padding:1px 4px;border-radius:3px;font-weight:bold;">⭐TOP5</span>'
            if item.get("is_top5")
            else ""
        )
        bid = _as_float(item.get("bid"))
        ask = _as_float(item.get("ask"))
        bid_size = int(item.get("bid_size", 0) or 0)
        ask_size = int(item.get("ask_size", 0) or 0)
        spread_cents = _as_float(item.get("spread")) * 100 if item.get("spread") is not None else max(ask - bid, 0) * 100
        spread_color = "#00c853" if spread_cents <= 1 else ("#ffd600" if spread_cents <= 3 else "#ff1744")
        change_pct = _as_float(item.get("change_pct"))
        row_bg = "#0a1a0a" if item.get("is_top5") else "transparent"
        catalyst_html = _render_confirmed_catalyst_cell(item)
        news_link_html = _render_confirmed_news_icon(item)
        blacklist_html = _render_confirmed_blacklist_placeholder(ticker)
        rendered.append(
            f"""<tr style="background:{row_bg};">
            <td style="text-align:center">{index}</td>
            <td><strong>{escape(ticker)}</strong>{live_badge}{top5_badge}<br><span style="font-size:10px;color:#888;">{escape(watched_by)}</span></td>
            <td style="text-align:center">{path_badge}</td>
            <td style="color:#ffd600;font-weight:bold;">{_as_float(item.get("rank_score")):.0f}</td>
            <td style="color:#00ff41;">{escape(str(item.get("confirmed_at", item.get("first_spike_time", ""))))}</td>
            <td>{_fmt_money(_as_float(item.get("entry_price")))}</td>
            <td>{_fmt_money(_as_float(item.get("price")))}</td>
            <td style="color:{'#00c853' if change_pct >= 0 else '#ff1744'}">{change_pct:+.1f}%</td>
            <td>{_fmt_money(bid)} <span style="color:#888;font-size:11px">x{bid_size}</span></td>
            <td>{_fmt_money(ask)} <span style="color:#888;font-size:11px">x{ask_size}</span></td>
            <td style="color:{spread_color}">{spread_cents:.0f}¢</td>
            <td>{_short_volume(item.get("volume"))}</td>
            <td>{_as_float(item.get("rvol")):.1f}x</td>
            <td>{_short_volume(item.get("shares_outstanding"))}</td>
            <td>{int(item.get("squeeze_count", 0) or 0)}</td>
            <td>{escape(str(item.get("first_spike_time", "")))}</td>
            <td style="font-size:12px;min-width:250px;max-width:400px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{catalyst_html}</td>
            <td style="text-align:center">{news_link_html}</td>
            <td style="text-align:center">{blacklist_html}</td>
        </tr>"""
        )
    return "".join(rendered)


def _render_confirmed_catalyst_cell(item: dict[str, Any]) -> str:
    catalyst = str(item.get("catalyst", "") or "").strip()
    headline = str(item.get("headline", "") or "").strip()
    news_url = str(item.get("news_url", "") or "").strip()
    news_date = str(item.get("news_date", "") or "").strip()
    sentiment = str(item.get("sentiment", "") or "").strip().lower()

    if not catalyst and not headline:
        return '<span style="color:#555">No recent news</span>'

    sent_color = {"bullish": "#00c853", "bearish": "#ff1744", "neutral": "#ffd600"}.get(sentiment, "#888")
    sent_bg = {"bullish": "#0a2e0a", "bearish": "#2e0a0a", "neutral": "#2e2e0a"}.get(sentiment, "transparent")
    sent_label = {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}.get(sentiment, "")

    display_headline = headline
    if display_headline.startswith("["):
        bracket_end = display_headline.find("]")
        if bracket_end > 0:
            display_headline = display_headline[bracket_end + 1 :].strip()

    headline_html = escape(display_headline)
    if news_url:
        headline_html = f'<a href="{escape(news_url)}" target="_blank" rel="noreferrer" style="color:#4fc3f7;text-decoration:none;">{headline_html}</a>'

    date_html = f'<span style="color:#888;">{escape(news_date)}</span> ' if news_date else ""
    catalyst_label = f'{escape(catalyst)} ' if catalyst else ""
    return (
        f'<span style="display:block;background:{sent_bg};border-left:3px solid {sent_color};padding-left:8px;">'
        f'{sent_label} {catalyst_label}{date_html}{headline_html}</span>'
    )


def _render_confirmed_news_icon(item: dict[str, Any]) -> str:
    news_url = str(item.get("news_url", "") or "").strip()
    if not news_url:
        return '<span style="color:#61758a;">—</span>'
    return (
        f'<a href="{escape(news_url)}" target="_blank" rel="noreferrer" '
        'style="color:#00e5ff;text-decoration:none;font-size:14px;" title="Open news article">📰</a>'
    )


def _render_confirmed_blacklist_placeholder(ticker: str) -> str:
    if not ticker:
        return '<span style="color:#61758a;">—</span>'
    return (
        '<span style="color:#ff6b6b;font-size:11px;padding:2px 6px;'
        'border:1px solid #ff6b6b;border-radius:3px;opacity:0.55;" '
        f'title="Blacklist workflow is not wired yet for {escape(ticker)}">🚫</span>'
    )


def _render_scanner_stock_rows(rows: list[dict[str, Any]], live_symbols: set[str]) -> str:
    if not rows:
        return '<tr><td colspan="15" style="text-align:center;color:#888;padding:20px;">No stocks qualifying</td></tr>'
    rendered = []
    for index, item in enumerate(rows, start=1):
        ticker = str(item.get("ticker", "")).upper()
        live_badge = ' <span style="color:#00ff41;font-size:10px;">⚡LIVE</span>' if ticker in live_symbols else ""
        rendered.append(
            f"""<tr>
            <td style="text-align:center">{index}</td>
            <td><strong>{escape(ticker)}</strong>{live_badge}</td>
            <td style="color:#00e5ff;font-size:12px;">{escape(str(item.get("first_seen", "")))}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("price")))}</td>
            <td style="text-align:right;color:{'#00c853' if _as_float(item.get('change_pct')) >= 0 else '#ff1744'}">{_as_float(item.get("change_pct")):+.1f}%</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("bid")))}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("ask")))}</td>
            <td style="text-align:right">{_as_float(item.get("spread_pct")):.2f}%</td>
            <td style="text-align:right">{_short_volume(item.get("volume"))}</td>
            <td style="text-align:right">{_as_float(item.get("rvol")):.1f}x</td>
            <td style="text-align:right">{_short_volume(item.get("shares_outstanding"))}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("hod")))}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("vwap")))}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("prev_close")))}</td>
            <td style="text-align:right">{escape(_format_age(item.get("data_age_secs")))}</td>
        </tr>"""
        )
    return "".join(rendered)


def _render_alert_rows(alerts: list[dict[str, Any]]) -> str:
    if not alerts:
        return '<tr><td colspan="9" style="text-align:center;color:#888;padding:20px;">No alerts fired yet</td></tr>'
    rendered = []
    for alert in reversed(alerts[-100:]):
        details = alert.get("details", {})
        if isinstance(details, dict):
            details_str = ", ".join(f"{key}={value}" for key, value in details.items())
        else:
            details_str = str(details)
        rendered.append(
            f"""<tr>
            <td>{escape(str(alert.get("type", "")))}</td>
            <td><strong>{escape(str(alert.get("ticker", "")))}</strong></td>
            <td>{_fmt_money(_as_float(alert.get("price")))}</td>
            <td>{_fmt_money(_as_float(alert.get("bid")))}</td>
            <td>{_fmt_money(_as_float(alert.get("ask")))}</td>
            <td>{_short_volume(alert.get("volume"))}</td>
            <td>{_short_volume(alert.get("float"))}</td>
            <td>{escape(details_str)}</td>
            <td>{escape(str(alert.get("time", "")))}</td>
        </tr>"""
        )
    return "".join(rendered)


def _status_badge(status: str) -> str:
    normalized = status.lower().replace(" ", "_")
    return f'<span class="pill status-{escape(normalized)}">{escape(status.upper())}</span>'


def _empty_row(columns: int, message: str) -> str:
    return f'<tr><td colspan="{columns}" style="color:#61758a;">{escape(message)}</td></tr>'


def _short_volume(value: float | int | None) -> str:
    if value is None:
        return "-"
    numeric = float(value)
    abs_numeric = abs(numeric)
    if abs_numeric >= 1_000_000_000:
        return f"{numeric / 1_000_000_000:.1f}B"
    if abs_numeric >= 1_000_000:
        return f"{numeric / 1_000_000:.1f}M"
    if abs_numeric >= 1_000:
        return f"{numeric / 1_000:.1f}K"
    return f"{numeric:.0f}"


def _position_preview(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return "None"
    previews: list[str] = []
    for item in positions[:3]:
        symbol = str(item.get("ticker") or item.get("symbol") or "").upper()
        quantity = item.get("quantity", 0)
        if not symbol:
            continue
        previews.append(f"{symbol} x {quantity}")
    return ", ".join(previews) if previews else "None"


def _intent_preview(intents: list[dict[str, Any]]) -> str:
    if not intents:
        return "None"
    previews: list[str] = []
    for item in intents[:3]:
        symbol = str(item.get("symbol", "")).upper()
        intent_type = str(item.get("intent_type", "")).lower()
        side = str(item.get("side", "")).lower()
        if not symbol:
            continue
        previews.append(f"{intent_type}/{side} {symbol}")
    return ", ".join(previews) if previews else "None"


def _decimal_str(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.normalize() if value != 0 else Decimal("0"), "f")


def _datetime_str(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_age(seconds: Any) -> str:
    try:
        numeric = int(seconds)
    except (TypeError, ValueError):
        return "?"
    if numeric < 0:
        return "?"
    if numeric < 60:
        return f"{numeric}s"
    minutes, remaining = divmod(numeric, 60)
    if minutes < 60:
        return f"{minutes}m{remaining}s"
    return f"{minutes}m"


def _fmt_money(value: float) -> str:
    if value <= 0:
        return "—"
    return f"${value:.2f}"


def _fmt_qty(value: float) -> str:
    if abs(value) < 0.0001:
        return "—"
    if abs(value - round(value)) < 0.0001:
        return str(int(round(value)))
    return f"{value:.2f}"


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _decimal_total(quantity: str, price: str) -> str:
    return f"{_as_float(quantity) * _as_float(price):,.2f}"
