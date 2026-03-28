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
            top_confirmed.append(
                {
                    "rank": index,
                    "ticker": ticker,
                    "rank_score": float(item.get("rank_score", 0) or 0),
                    "confirmation_path": str(item.get("confirmation_path", "")),
                    "price": float(item.get("price", 0) or 0),
                    "change_pct": float(item.get("change_pct", 0) or 0),
                    "volume": float(item.get("volume", 0) or 0),
                    "rvol": float(item.get("rvol", 0) or 0),
                    "spread_pct": float(item.get("spread_pct", 0) or 0),
                    "squeeze_count": int(item.get("squeeze_count", 0) or 0),
                    "first_spike_time": str(item.get("first_spike_time", "")),
                    "watched_by": watched_by,
                }
            )

        legacy_confirmed = [
            str(symbol).upper()
            for symbol in legacy_shadow.get("scanner", {}).get("confirmed_symbols", [])
        ]
        return {
            "status": "active" if top_confirmed else "idle",
            "watchlist": watchlist,
            "watchlist_count": len(watchlist),
            "top_confirmed_count": len(top_confirmed),
            "top_confirmed": top_confirmed,
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
                    select(TradeIntent).order_by(desc(TradeIntent.updated_at)).limit(10)
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
                    select(BrokerOrder).order_by(desc(BrokerOrder.updated_at)).limit(10)
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

                for fill in session.scalars(select(Fill).order_by(desc(Fill.filled_at)).limit(10)).all():
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
