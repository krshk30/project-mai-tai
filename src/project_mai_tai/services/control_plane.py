from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
import json
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from redis.asyncio import Redis
from sqlalchemy import case, desc, func, select, text
from sqlalchemy.orm import Session, sessionmaker
import uvicorn

from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    BrokerOrderEvent,
    DashboardSnapshot,
    Fill,
    ReconciliationFinding,
    ReconciliationRun,
    ScannerBlacklistEntry,
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
from project_mai_tai.services.strategy_engine_app import current_scanner_session_start_utc
from project_mai_tai.shadow import LegacyShadowClient
from project_mai_tai.settings import Settings, get_settings
from project_mai_tai.strategy_core import (
    FivePillarsConfig,
    MomentumAlertConfig,
    MomentumConfirmedConfig,
    TopGainersConfig,
)


SERVICE_NAME = "control-plane"
EASTERN_TZ = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(UTC)


def current_eastern_day_start_utc(now: datetime | None = None) -> datetime:
    current = now or utcnow()
    current_et = current.astimezone(EASTERN_TZ)
    day_start_et = current_et.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start_et.astimezone(UTC)


def current_eastern_day_end_utc(now: datetime | None = None) -> datetime:
    return current_eastern_day_start_utc(now) + timedelta(days=1)


def _within_current_eastern_day(timestamp: datetime | None, now: datetime | None = None) -> bool:
    if timestamp is None:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    day_start = current_eastern_day_start_utc(now)
    day_end = current_eastern_day_end_utc(now)
    timestamp_utc = timestamp.astimezone(UTC)
    return day_start <= timestamp_utc < day_end


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

    def _is_ui_hidden_symbol(self, account_name: str | None, symbol: str | None) -> bool:
        normalized_account = str(account_name or "").strip()
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_account or not normalized_symbol:
            return False
        return (
            normalized_account,
            normalized_symbol,
        ) in self.settings.reconciliation_ignored_position_mismatch_pairs

    def _filter_symbol_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        account_key: str = "broker_account_name",
        symbol_key: str = "symbol",
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in rows
            if not self._is_ui_hidden_symbol(item.get(account_key), item.get(symbol_key))
        ]

    def _is_ui_hidden_symbol_any_account(self, symbol: str | None) -> bool:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return False
        return any(
            ignored_symbol == normalized_symbol
            for _, ignored_symbol in self.settings.reconciliation_ignored_position_mismatch_pairs
        )

    def _incident_symbol(self, title: str | None, payload: dict[str, Any] | None) -> str:
        data = payload if isinstance(payload, dict) else {}
        direct_symbol = str(data.get("symbol") or data.get("ticker") or "").strip().upper()
        if direct_symbol:
            return direct_symbol
        title_text = str(title or "").strip()
        if " for " not in title_text:
            return ""
        candidate = title_text.rsplit(" for ", 1)[1].strip().upper()
        return candidate if candidate.isalnum() else ""

    def _display_account_name(self, account_name: str) -> str:
        display_name = getattr(self.settings, "display_account_name", None)
        if callable(display_name):
            return str(display_name(account_name))
        return account_name

    async def _reconnect_redis(self) -> None:
        previous = self.redis
        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        try:
            await previous.aclose()
        except Exception:
            pass

    def add_scanner_blacklist_symbol(self, symbol: str, *, reason: str = "manual") -> bool:
        normalized = symbol.strip().upper()
        if not normalized:
            return False

        with self.session_factory() as session:
            entry = session.scalar(
                select(ScannerBlacklistEntry).where(ScannerBlacklistEntry.symbol == normalized)
            )
            if entry is None:
                session.add(
                    ScannerBlacklistEntry(
                        symbol=normalized,
                        reason=reason,
                        source="control-plane",
                    )
                )
            else:
                entry.reason = reason
                entry.source = "control-plane"
            session.commit()
        return True

    def remove_scanner_blacklist_symbol(self, symbol: str) -> bool:
        normalized = symbol.strip().upper()
        if not normalized:
            return False

        with self.session_factory() as session:
            entry = session.scalar(
                select(ScannerBlacklistEntry).where(ScannerBlacklistEntry.symbol == normalized)
            )
            if entry is None:
                return False
            session.delete(entry)
            session.commit()
        return True

    async def load_dashboard_data(self) -> dict[str, Any]:
        db_state = self._load_database_state()
        stream_state = await self._load_stream_state()
        normalized_strategy_runtime = self._normalize_strategy_runtime(stream_state["strategy_runtime"])
        legacy_shadow = await self._load_legacy_shadow_data(
            strategy_runtime=normalized_strategy_runtime,
            recent_intents=db_state["recent_intents"],
        )
        scanner = self._build_scanner_view(
            market_data=stream_state["market_data"],
            strategy_runtime=normalized_strategy_runtime,
            legacy_shadow=legacy_shadow,
            persisted_snapshots=db_state["dashboard_snapshots"],
            blacklist_entries=db_state["scanner_blacklist"],
        )
        bots = self._build_bot_views(
            strategy_runtime=normalized_strategy_runtime,
            legacy_shadow=legacy_shadow,
            recent_intents=db_state["recent_intents"],
            recent_orders=db_state["recent_orders"],
            recent_fills=db_state["recent_fills"],
            open_orders=db_state["open_orders"],
        )

        overall_status = "healthy"
        if db_state["errors"] or stream_state["errors"]:
            overall_status = "degraded"
        elif any(
            service.get("effective_status", service["status"]) not in {"healthy", "starting"}
            for service in stream_state["services"]
        ):
            overall_status = "degraded"
        elif (
            db_state["reconciliation"]["latest_run"] is not None
            and db_state["reconciliation"]["latest_run"]["summary"].get("total_findings", 0) > 0
        ):
            overall_status = "degraded"

        return {
            "generated_at": _datetime_str(utcnow()),
            "status": overall_status,
            "environment": self.settings.environment,
            "domain": "project-mai-tai.live",
            "control_plane_url": self.settings.control_plane_base_url,
            "provider": self.settings.broker_provider_label,
            "oms_adapter": self.settings.oms_adapter_label,
            "active_broker_providers": self.settings.active_broker_providers,
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
            "strategy_runtime": normalized_strategy_runtime,
            "legacy_shadow": legacy_shadow,
            "incidents": db_state["incidents"],
            "scanner_blacklist": db_state["scanner_blacklist"],
            "errors": db_state["errors"] + stream_state["errors"] + legacy_shadow["errors"],
        }

    def _normalize_strategy_runtime(self, strategy_runtime: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(strategy_runtime)
        raw_bots = strategy_runtime.get("bots", {})
        if not isinstance(raw_bots, dict):
            return normalized

        normalized_bots: dict[str, Any] = {}
        for code, bot in raw_bots.items():
            if not isinstance(bot, dict):
                normalized_bots[code] = bot
                continue

            normalized_bot = dict(bot)
            normalized_bot["recent_decisions"] = [
                {
                    **dict(item),
                    "last_bar_at": _datetime_str(item.get("last_bar_at")),
                }
                for item in bot.get("recent_decisions", [])
                if isinstance(item, dict)
            ]
            normalized_bot["indicator_snapshots"] = [
                {
                    **dict(item),
                    "last_bar_at": _datetime_str(item.get("last_bar_at")),
                }
                for item in bot.get("indicator_snapshots", [])
                if isinstance(item, dict)
            ]
            normalized_bots[code] = normalized_bot

        normalized["bots"] = normalized_bots
        return normalized

    def _build_scanner_view(
        self,
        *,
        market_data: dict[str, Any],
        strategy_runtime: dict[str, Any],
        legacy_shadow: dict[str, Any],
        persisted_snapshots: dict[str, Any],
        blacklist_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        blacklisted_symbols = {
            str(entry.get("symbol", "")).upper()
            for entry in blacklist_entries
            if entry.get("symbol")
        }
        bot_states = strategy_runtime.get("bots", {})
        live_market_rows = self._build_live_market_lookup(strategy_runtime)
        watchlist = [
            str(symbol)
            for symbol in strategy_runtime.get("watchlist", [])
            if str(symbol).upper() not in blacklisted_symbols
        ]
        all_confirmed = [
            self._normalize_confirmed_row(
                index=index,
                item=item,
                bot_states=bot_states,
                live_market_row=live_market_rows.get(str(item.get("ticker", "")).upper()),
            )
            for index, item in enumerate(strategy_runtime.get("all_confirmed", []), start=1)
            if str(item.get("ticker", "")).upper() not in blacklisted_symbols
        ]
        top_confirmed = [
            self._normalize_confirmed_row(
                index=index,
                item=item,
                bot_states=bot_states,
                live_market_row=live_market_rows.get(str(item.get("ticker", "")).upper()),
            )
            for index, item in enumerate(strategy_runtime.get("top_confirmed", []), start=1)
            if str(item.get("ticker", "")).upper() not in blacklisted_symbols
        ]
        top_confirmed_source = "live" if top_confirmed else "idle"
        top_confirmed_snapshot_at = ""

        if not all_confirmed:
            snapshot = persisted_snapshots.get("scanner_confirmed_last_nonempty", {})
            snapshot_payload = snapshot if isinstance(snapshot, dict) else {}
            restored_rows = snapshot_payload.get("all_confirmed_candidates", [])
            restored_top_rows = snapshot_payload.get("top_confirmed", [])
            restored_watchlist = snapshot_payload.get("watchlist", [])
            restored_at = str(snapshot_payload.get("persisted_at", "") or "")
            restored_at_is_current = False
            if restored_at:
                try:
                    restored_at_dt = datetime.fromisoformat(restored_at)
                except ValueError:
                    restored_at_dt = None
                if restored_at_dt is not None:
                    if restored_at_dt.tzinfo is None:
                        restored_at_dt = restored_at_dt.replace(tzinfo=UTC)
                    restored_at_is_current = (
                        restored_at_dt.astimezone(UTC) >= current_scanner_session_start_utc()
                    )
            if isinstance(restored_rows, list) and restored_rows and restored_at_is_current:
                all_confirmed = [
                    self._normalize_confirmed_row(
                        index=index,
                        item=item,
                        bot_states=bot_states,
                        live_market_row=live_market_rows.get(str(item.get("ticker", "")).upper()),
                    )
                    for index, item in enumerate(restored_rows, start=1)
                    if isinstance(item, dict)
                    and str(item.get("ticker", "")).upper() not in blacklisted_symbols
                ]
            if isinstance(restored_top_rows, list) and restored_top_rows and restored_at_is_current:
                top_confirmed = [
                    self._normalize_confirmed_row(
                        index=index,
                        item=item,
                        bot_states=bot_states,
                        live_market_row=live_market_rows.get(str(item.get("ticker", "")).upper()),
                    )
                    for index, item in enumerate(restored_top_rows, start=1)
                    if isinstance(item, dict)
                    and str(item.get("ticker", "")).upper() not in blacklisted_symbols
                ]
                watchlist = [
                    str(symbol)
                    for symbol in restored_watchlist
                    if str(symbol).upper() not in blacklisted_symbols
                ]
                if top_confirmed:
                    top_confirmed_source = "restored"
                    top_confirmed_snapshot_at = restored_at

        legacy_confirmed = [
            str(symbol).upper()
            for symbol in legacy_shadow.get("scanner", {}).get("confirmed_symbols", [])
        ]
        return {
            "status": "active" if all_confirmed else "idle",
            "cycle_count": int(strategy_runtime.get("cycle_count", 0) or 0),
            "watchlist": watchlist,
            "watchlist_count": len(watchlist),
            "all_confirmed_count": len(all_confirmed),
            "all_confirmed": all_confirmed,
            "top_confirmed_count": len(top_confirmed),
            "top_confirmed": top_confirmed,
            "top_confirmed_source": top_confirmed_source,
            "top_confirmed_snapshot_at": top_confirmed_snapshot_at,
            "five_pillars": [
                item
                for item in strategy_runtime.get("five_pillars", [])
                if str(item.get("ticker", "")).upper() not in blacklisted_symbols
            ],
            "five_pillars_count": len(
                [
                    item
                    for item in strategy_runtime.get("five_pillars", [])
                    if str(item.get("ticker", "")).upper() not in blacklisted_symbols
                ]
            ),
            "top_gainers": [
                item
                for item in strategy_runtime.get("top_gainers", [])
                if str(item.get("ticker", "")).upper() not in blacklisted_symbols
            ],
            "top_gainers_count": len(
                [
                    item
                    for item in strategy_runtime.get("top_gainers", [])
                    if str(item.get("ticker", "")).upper() not in blacklisted_symbols
                ]
            ),
            "recent_alerts": [
                item
                for item in strategy_runtime.get("recent_alerts", [])
                if str(item.get("ticker", "")).upper() not in blacklisted_symbols
            ],
            "recent_alerts_count": len(
                [
                    item
                    for item in strategy_runtime.get("recent_alerts", [])
                    if str(item.get("ticker", "")).upper() not in blacklisted_symbols
                ]
            ),
            "top_gainer_changes": [
                item
                for item in strategy_runtime.get("top_gainer_changes", [])
                if str(item.get("ticker", "")).upper() not in blacklisted_symbols
            ],
            "alert_warmup": dict(strategy_runtime.get("alert_warmup", {})),
            "active_subscription_symbols": int(market_data.get("active_subscription_symbols", 0) or 0),
            "heartbeat_active_symbols": int(market_data.get("heartbeat_active_symbols", 0) or 0),
            "subscription_symbols": list(market_data.get("subscription_symbols", [])),
            "latest_snapshot_batch": market_data.get("latest_snapshot_batch"),
            "feed_status": str(market_data.get("feed_status", "unknown") or "unknown"),
            "feed_status_note": str(market_data.get("feed_status_note", "") or ""),
            "legacy_confirmed_symbols": legacy_confirmed,
            "legacy_confirmed_count": len(legacy_confirmed),
            "blacklist": blacklist_entries,
            "blacklist_symbols": sorted(blacklisted_symbols),
            "blacklist_count": len(blacklist_entries),
        }

    def _build_live_market_lookup(self, strategy_runtime: dict[str, Any]) -> dict[str, dict[str, Any]]:
        lookup: dict[str, dict[str, Any]] = {}
        for collection_name in ("five_pillars", "top_gainers"):
            for item in strategy_runtime.get(collection_name, []):
                ticker = str(item.get("ticker", "")).upper()
                if not ticker:
                    continue
                current = lookup.get(ticker)
                current_age = int(current.get("data_age_secs", 10**9)) if current else 10**9
                item_age = int(item.get("data_age_secs", 10**9) or 10**9)
                if current is None or item_age <= current_age:
                    lookup[ticker] = item
        return lookup

    def _normalize_confirmed_row(
        self,
        *,
        index: int,
        item: dict[str, Any],
        bot_states: dict[str, Any],
        live_market_row: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ticker = str(item.get("ticker", "")).upper()
        watched_by = [
            strategy_code
            for strategy_code, bot in bot_states.items()
            if ticker and ticker in {str(symbol).upper() for symbol in bot.get("watchlist", [])}
        ]
        merged_item = dict(item)
        if live_market_row:
            for field in (
                "price",
                "change_pct",
                "volume",
                "rvol",
                "shares_outstanding",
                "bid",
                "ask",
                "bid_size",
                "ask_size",
                "spread",
                "spread_pct",
                "hod",
                "vwap",
                "prev_close",
                "avg_daily_volume",
                "data_age_secs",
            ):
                if live_market_row.get(field) is not None:
                    merged_item[field] = live_market_row.get(field)
        bid = float(merged_item.get("bid", 0) or 0)
        ask = float(merged_item.get("ask", 0) or 0)
        spread = float(merged_item.get("spread", 0) or 0)
        if spread <= 0 and bid > 0 and ask > 0:
            spread = round(ask - bid, 4)

        return {
            **merged_item,
            "rank": index,
            "ticker": ticker,
            "rank_score": float(merged_item.get("rank_score", 0) or 0),
            "confirmation_path": str(merged_item.get("confirmation_path", "")),
            "confirmed_at": str(merged_item.get("confirmed_at", "")),
            "entry_price": float(merged_item.get("entry_price", 0) or 0),
            "price": float(merged_item.get("price", 0) or 0),
            "change_pct": float(merged_item.get("change_pct", 0) or 0),
            "volume": float(merged_item.get("volume", 0) or 0),
            "rvol": float(merged_item.get("rvol", 0) or 0),
            "bid": bid,
            "ask": ask,
            "bid_size": int(merged_item.get("bid_size", 0) or 0),
            "ask_size": int(merged_item.get("ask_size", 0) or 0),
            "spread": spread,
            "spread_pct": float(merged_item.get("spread_pct", 0) or 0),
            "squeeze_count": int(merged_item.get("squeeze_count", 0) or 0),
            "first_spike_time": str(merged_item.get("first_spike_time", "")),
            "catalyst": str(merged_item.get("catalyst", "")),
            "catalyst_type": str(merged_item.get("catalyst_type") or merged_item.get("catalyst") or ""),
            "headline": str(merged_item.get("headline", "")),
            "sentiment": str(merged_item.get("sentiment", "")),
            "direction": str(merged_item.get("direction") or merged_item.get("sentiment") or ""),
            "news_fetch_status": str(merged_item.get("news_fetch_status", "")),
            "catalyst_status": str(merged_item.get("catalyst_status", "")),
            "news_url": str(merged_item.get("news_url", "")),
            "news_date": str(merged_item.get("news_date", "")),
            "news_window_start": str(merged_item.get("news_window_start", "")),
            "catalyst_reason": str(merged_item.get("catalyst_reason", "")),
            "catalyst_confidence": float(merged_item.get("catalyst_confidence", 0) or 0),
            "article_count": int(merged_item.get("article_count", 0) or 0),
            "real_catalyst_article_count": int(merged_item.get("real_catalyst_article_count", 0) or 0),
            "freshness_minutes": (
                int(merged_item.get("freshness_minutes", 0))
                if merged_item.get("freshness_minutes") is not None
                else None
            ),
            "is_generic_roundup": bool(merged_item.get("is_generic_roundup", False)),
            "has_real_catalyst": bool(merged_item.get("has_real_catalyst", False)),
            "path_a_eligible": bool(merged_item.get("path_a_eligible", False)),
            "ai_shadow_status": str(merged_item.get("ai_shadow_status", "")),
            "ai_shadow_provider": str(merged_item.get("ai_shadow_provider", "")),
            "ai_shadow_model": str(merged_item.get("ai_shadow_model", "")),
            "ai_shadow_direction": str(merged_item.get("ai_shadow_direction", "")),
            "ai_shadow_category": str(merged_item.get("ai_shadow_category", "")),
            "ai_shadow_confidence": float(merged_item.get("ai_shadow_confidence", 0) or 0),
            "ai_shadow_has_real_catalyst": bool(merged_item.get("ai_shadow_has_real_catalyst", False)),
            "ai_shadow_is_generic_roundup": bool(merged_item.get("ai_shadow_is_generic_roundup", False)),
            "ai_shadow_is_company_specific": bool(merged_item.get("ai_shadow_is_company_specific", False)),
            "ai_shadow_path_a_eligible": bool(merged_item.get("ai_shadow_path_a_eligible", False)),
            "ai_shadow_reason": str(merged_item.get("ai_shadow_reason", "")),
            "ai_shadow_headline_basis": str(merged_item.get("ai_shadow_headline_basis", "")),
            "ai_shadow_positive_phrases": list(merged_item.get("ai_shadow_positive_phrases", []) or []),
            "watched_by": watched_by,
            "is_top5": bool(watched_by),
        }

    def _build_bot_views(
        self,
        *,
        strategy_runtime: dict[str, Any],
        legacy_shadow: dict[str, Any],
        recent_intents: list[dict[str, Any]],
        recent_orders: list[dict[str, Any]],
        recent_fills: list[dict[str, Any]],
        open_orders: list[dict[str, Any]],
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
            account_name = str(
                runtime_bot.get("account_name")
                or (registration.account_name if registration else "")
                or ""
            )

            positions = [
                item
                for item in list(runtime_bot.get("positions", []))
                if not self._is_ui_hidden_symbol(account_name, item.get("ticker") or item.get("symbol"))
            ]
            watchlist = [
                str(symbol)
                for symbol in runtime_bot.get("watchlist", [])
                if not self._is_ui_hidden_symbol(account_name, symbol)
            ]
            matching_open_orders = [
                item
                for item in open_orders
                if item.get("strategy_code") == code
                and item.get("broker_account_name") == account_name
            ]
            pending_open = sorted(
                {
                    str(item.get("symbol", "")).upper()
                    for item in matching_open_orders
                    if str(item.get("intent_type", "")).lower() == "open"
                    and str(item.get("side", "")).lower() == "buy"
                    and str(item.get("symbol", "")).strip()
                    and not self._is_ui_hidden_symbol(account_name, item.get("symbol"))
                }
            )
            pending_close = sorted(
                {
                    str(item.get("symbol", "")).upper()
                    for item in matching_open_orders
                    if str(item.get("intent_type", "")).lower() == "close"
                    and str(item.get("side", "")).lower() == "sell"
                    and str(item.get("symbol", "")).strip()
                    and not self._is_ui_hidden_symbol(account_name, item.get("symbol"))
                }
            )
            pending_scale = sorted(
                {
                    f'{str(item.get("symbol", "")).upper()}:{str(item.get("level", "")).upper()}'
                    for item in matching_open_orders
                    if str(item.get("intent_type", "")).lower() == "scale"
                    and str(item.get("side", "")).lower() == "sell"
                    and str(item.get("symbol", "")).strip()
                    and str(item.get("level", "")).strip()
                    and not self._is_ui_hidden_symbol(account_name, item.get("symbol"))
                }
            )
            recent_decisions = [
                item
                for item in list(runtime_bot.get("recent_decisions", []))
                if not self._is_ui_hidden_symbol(account_name, item.get("ticker") or item.get("symbol"))
            ]
            indicator_snapshots = [
                item
                for item in list(runtime_bot.get("indicator_snapshots", []))
                if not self._is_ui_hidden_symbol(account_name, item.get("ticker") or item.get("symbol"))
            ]
            tos_parity = self._build_tos_parity_view(
                strategy_code=code,
                indicator_snapshots=indicator_snapshots,
                watchlist=watchlist,
            )

            bot_views.append(
                {
                    "strategy_code": code,
                    "display_name": registration.display_name if registration else code.replace("_", " ").upper(),
                    "account_name": account_name,
                    "account_display_name": self._display_account_name(account_name),
                    "interval_secs": int(
                        runtime_bot.get("interval_secs")
                        or (registration.interval_secs if registration else 0)
                        or 0
                    ),
                    "runtime_kind": (
                        registration.runtime_kind
                        if registration
                        else str(runtime_bot.get("runtime_kind", "unknown") or "unknown")
                    ),
                    "execution_mode": registration.execution_mode if registration else "unknown",
                    "provider": (
                        self.settings.provider_for_strategy(code)
                        if registration
                        else "unknown"
                    ),
                    "wiring_status": (
                        f'{registration.execution_mode}/{self.settings.provider_for_strategy(code)}'
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
                    "recent_decisions": recent_decisions[:12],
                    "indicator_snapshots": indicator_snapshots,
                    "tos_parity": tos_parity,
                    "recent_intents": [
                        item
                        for item in recent_intents
                        if item.get("strategy_code") == code
                        and not self._is_ui_hidden_symbol(account_name, item.get("symbol"))
                    ][:3],
                    "recent_orders": [
                        item
                        for item in recent_orders
                        if item.get("strategy_code") == code
                        and not self._is_ui_hidden_symbol(account_name, item.get("symbol"))
                    ][:3],
                    "recent_fills": [
                        item
                        for item in recent_fills
                        if item.get("strategy_code") == code
                        and not self._is_ui_hidden_symbol(account_name, item.get("symbol"))
                    ][:3],
                }
            )
        return bot_views

    def _build_tos_parity_view(
        self,
        *,
        strategy_code: str,
        indicator_snapshots: list[dict[str, Any]],
        watchlist: list[str],
    ) -> dict[str, Any]:
        enabled = strategy_code in {"macd_1m", "tos"}
        if not enabled:
            return {
                "enabled": False,
                "status": "not_applicable",
                "comparison_target": "thinkorswim_1m",
                "summary": "TOS parity is only tracked for the 1-minute and TOS runtimes.",
                "snapshots": [],
                "settings": [],
            }

        normalized_snapshots = [
            {
                **item,
                "symbol": str(item.get("symbol", "")).upper(),
                "last_bar_at": _datetime_str(item.get("last_bar_at")),
                "close": float(item.get("close", 0) or 0),
                "ema9": float(item.get("ema9", 0) or 0),
                "ema20": float(item.get("ema20", 0) or 0),
                "macd": float(item.get("macd", 0) or 0),
                "signal": float(item.get("signal", 0) or 0),
                "histogram": float(item.get("histogram", 0) or 0),
                "vwap": float(item.get("vwap", 0) or 0),
                "bar_count": int(item.get("bar_count", 0) or 0),
                "macd_above_signal": bool(item.get("macd_above_signal", False)),
                "price_above_vwap": bool(item.get("price_above_vwap", False)),
                "price_above_ema9": bool(item.get("price_above_ema9", False)),
                "price_above_ema20": bool(item.get("price_above_ema20", False)),
            }
            for item in indicator_snapshots
        ]
        normalized_snapshots.sort(key=lambda item: str(item["last_bar_at"]), reverse=True)
        status = "ready" if normalized_snapshots else "warming" if watchlist else "idle"
        settings = [
            "Aggregation 1m",
            "EMA lengths 9 / 20",
            "MACD 12 / 26 / 9",
            "Average type exponential",
            "VWAP intraday reset",
            "Compare on closed bars only",
        ]
        summary = (
            f'{len(normalized_snapshots)} local 1m snapshots ready for side-by-side TOS checks.'
            if normalized_snapshots
            else "Waiting for closed 1m bars before parity comparison is meaningful."
            if watchlist
            else "No active 1m symbols yet."
        )
        return {
            "enabled": True,
            "status": status,
            "comparison_target": "thinkorswim_1m",
            "summary": summary,
            "snapshots": normalized_snapshots[:6],
            "settings": settings,
        }

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
            "blacklisted_symbols": 0,
        }
        recent_intents: list[dict[str, Any]] = []
        recent_orders: list[dict[str, Any]] = []
        recent_fills: list[dict[str, Any]] = []
        open_orders: list[dict[str, Any]] = []
        virtual_positions: list[dict[str, Any]] = []
        account_positions: list[dict[str, Any]] = []
        reconciliation = {
            "latest_run": None,
            "findings": [],
        }
        incidents: list[dict[str, Any]] = []
        dashboard_snapshots: dict[str, dict[str, Any]] = {}
        scanner_blacklist: list[dict[str, Any]] = []
        now = utcnow()

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
                            if not self._is_ui_hidden_symbol_any_account(finding.symbol)
                        ],
                    }
                    counts["latest_reconciliation_findings"] = len(reconciliation["findings"])

                for intent in session.scalars(
                    select(TradeIntent).order_by(desc(TradeIntent.updated_at)).limit(50)
                ).all():
                    if not _within_current_eastern_day(intent.updated_at, now):
                        continue
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

                latest_order_event_by_order: dict[Any, BrokerOrderEvent] = {}
                for entry in session.scalars(
                    select(BrokerOrderEvent).order_by(desc(BrokerOrderEvent.event_at)).limit(200)
                ).all():
                    latest_order_event_by_order.setdefault(entry.order_id, entry)

                for order in session.scalars(
                    select(BrokerOrder).order_by(desc(BrokerOrder.updated_at)).limit(50)
                ).all():
                    if not _within_current_eastern_day(order.updated_at, now):
                        continue
                    strategy = strategy_lookup.get(order.strategy_id)
                    account = account_lookup.get(order.broker_account_id)
                    intent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
                    latest_event = latest_order_event_by_order.get(order.id)
                    latest_event_payload = (
                        latest_event.payload
                        if latest_event is not None and isinstance(latest_event.payload, dict)
                        else {}
                    )
                    recent_orders.append(
                        {
                            "strategy_code": strategy.code if strategy else str(order.strategy_id),
                            "broker_account_name": account.name if account else str(order.broker_account_id),
                            "symbol": order.symbol,
                            "side": order.side,
                            "intent_type": intent.intent_type if intent is not None else "",
                            "quantity": _decimal_str(order.quantity),
                            "status": order.status,
                            "reason": str(latest_event_payload.get("reason") or (intent.reason if intent else "")),
                            "client_order_id": order.client_order_id,
                            "broker_order_id": order.broker_order_id or "",
                            "order_type": order.order_type,
                            "time_in_force": order.time_in_force,
                            "extended_hours": bool(
                                str((order.payload or {}).get("extended_hours", "")).lower() == "true"
                            ),
                            "updated_at": _datetime_str(order.updated_at),
                        }
                    )

                for order in session.scalars(
                    select(BrokerOrder).where(
                        BrokerOrder.status.in_(["pending", "submitted", "accepted", "partially_filled"])
                    )
                ).all():
                    strategy = strategy_lookup.get(order.strategy_id)
                    account = account_lookup.get(order.broker_account_id)
                    intent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
                    payload = order.payload if isinstance(order.payload, dict) else {}
                    open_orders.append(
                        {
                            "strategy_code": strategy.code if strategy else str(order.strategy_id),
                            "broker_account_name": account.name if account else str(order.broker_account_id),
                            "symbol": order.symbol,
                            "side": order.side,
                            "intent_type": intent.intent_type if intent is not None else "",
                            "status": order.status,
                            "client_order_id": order.client_order_id,
                            "level": str(payload.get("level", "") or ""),
                        }
                    )

                for fill in session.scalars(select(Fill).order_by(desc(Fill.filled_at)).limit(50)).all():
                    if not _within_current_eastern_day(fill.filled_at, now):
                        continue
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
                    payload = incident.payload if isinstance(incident.payload, dict) else {}
                    incident_account_name = str(
                        payload.get("broker_account_name") or payload.get("account_name") or ""
                    ).strip()
                    incident_symbol = self._incident_symbol(incident.title, payload)
                    if incident_account_name:
                        if self._is_ui_hidden_symbol(incident_account_name, incident_symbol):
                            continue
                    elif self._is_ui_hidden_symbol_any_account(incident_symbol):
                        continue
                    incidents.append(
                        {
                            "service_name": incident.service_name or "system",
                            "severity": incident.severity,
                            "title": incident.title,
                            "status": incident.status,
                            "opened_at": _datetime_str(incident.opened_at),
                        }
                    )

                confirmed_snapshot = session.scalar(
                    select(DashboardSnapshot)
                    .where(DashboardSnapshot.snapshot_type == "scanner_confirmed_last_nonempty")
                    .order_by(desc(DashboardSnapshot.created_at))
                )
                if confirmed_snapshot is not None:
                    dashboard_snapshots["scanner_confirmed_last_nonempty"] = {
                        **confirmed_snapshot.payload,
                        "created_at": _datetime_str(confirmed_snapshot.created_at),
                    }

                blacklist_entries = session.scalars(
                    select(ScannerBlacklistEntry).order_by(ScannerBlacklistEntry.symbol)
                ).all()
                counts["blacklisted_symbols"] = len(blacklist_entries)
                scanner_blacklist = [
                    {
                        "symbol": entry.symbol,
                        "reason": entry.reason,
                        "source": entry.source,
                        "created_at": _datetime_str(entry.created_at),
                        "updated_at": _datetime_str(entry.updated_at),
                    }
                    for entry in blacklist_entries
                ]
        except Exception as exc:
            errors.append(f"database:{exc}")

        recent_intents = self._filter_symbol_rows(recent_intents)
        recent_orders = self._filter_symbol_rows(recent_orders)
        recent_fills = self._filter_symbol_rows(recent_fills)
        open_orders = self._filter_symbol_rows(open_orders)
        virtual_positions = self._filter_symbol_rows(virtual_positions)
        account_positions = self._filter_symbol_rows(account_positions)
        counts["open_virtual_positions"] = len(virtual_positions)
        counts["open_account_positions"] = len(account_positions)

        return {
            "counts": counts,
            "recent_intents": recent_intents,
            "recent_orders": recent_orders,
            "recent_fills": recent_fills,
            "open_orders": open_orders,
            "virtual_positions": virtual_positions,
            "account_positions": account_positions,
            "reconciliation": reconciliation,
            "incidents": incidents,
            "dashboard_snapshots": dashboard_snapshots,
            "scanner_blacklist": scanner_blacklist,
            "errors": errors,
        }

    async def _load_stream_state(self) -> dict[str, Any]:
        errors: list[str] = []
        services: list[dict[str, Any]] = []
        market_data = {
            "latest_snapshot_batch": None,
            "active_subscription_symbols": 0,
            "subscription_symbols": [],
            "heartbeat_active_symbols": 0,
            "feed_status": "unknown",
            "feed_status_note": "",
        }
        strategy_runtime = {
            "all_confirmed": [],
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
                heartbeat = HeartbeatEvent.model_validate(event)
                payload = heartbeat.payload
                if payload.service_name in latest_by_service:
                    continue
                latest_by_service[payload.service_name] = {
                    "service_name": payload.service_name,
                    "instance_name": payload.instance_name,
                    "status": payload.status,
                    "raw_status": payload.status,
                    "effective_status": payload.status,
                    "details": payload.details,
                    "observed_at": _datetime_str(heartbeat.produced_at),
                    "observed_at_raw": heartbeat.produced_at,
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
                market_data["latest_snapshot_completed_at_raw"] = event.payload.completed_at
        except Exception as exc:
            errors.append(f"redis:snapshot-batches:{exc}")

        try:
            subscription_events = await self._read_stream_events("market-data-subscriptions", limit=1)
            if subscription_events:
                event = MarketDataSubscriptionEvent.model_validate(subscription_events[0])
                market_data["active_subscription_symbols"] = len(event.payload.symbols)
                market_data["subscription_symbols"] = event.payload.symbols
                market_data["latest_subscription_observed_at_raw"] = event.produced_at
        except Exception as exc:
            errors.append(f"redis:market-data-subscriptions:{exc}")

        try:
            strategy_state_events = await self._read_stream_events("strategy-state", limit=1)
            if strategy_state_events:
                event = StrategyStateSnapshotEvent.model_validate(strategy_state_events[0])
                strategy_runtime = {
                    "all_confirmed": event.payload.all_confirmed,
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

        self._apply_market_data_feed_status(services=services, market_data=market_data)

        return {
            "services": services,
            "market_data": market_data,
            "strategy_runtime": strategy_runtime,
            "errors": errors,
        }

    def _apply_market_data_feed_status(
        self,
        *,
        services: list[dict[str, Any]],
        market_data: dict[str, Any],
    ) -> None:
        market_data_service = next(
            (service for service in services if service.get("service_name") == "market-data-gateway"),
            None,
        )
        if market_data_service is None:
            return

        heartbeat_active_symbols = _safe_int(
            market_data_service.get("details", {}).get("active_symbols", 0)
        )
        snapshot_recent = _is_recent_datetime(
            market_data.get("latest_snapshot_completed_at_raw"),
            max_age_seconds=20,
        )
        subscription_recent = _is_recent_datetime(
            market_data.get("latest_subscription_observed_at_raw"),
            max_age_seconds=30,
        )
        displayed_subscription_count = max(
            _safe_int(market_data.get("active_subscription_symbols", 0)),
            heartbeat_active_symbols,
        )
        raw_status = str(market_data_service.get("status", "unknown") or "unknown")
        effective_status = raw_status
        status_note = ""

        if raw_status in {"starting", "stopping"} and (snapshot_recent or subscription_recent):
            effective_status = "healthy"
            status_note = "Fresh snapshot/subscription activity is still flowing while the heartbeat catches up."
        elif raw_status == "healthy" and not snapshot_recent and displayed_subscription_count == 0:
            status_note = "Heartbeat is healthy, but no fresh snapshot batch has been observed yet."

        market_data_service["raw_status"] = raw_status
        market_data_service["effective_status"] = effective_status
        if status_note:
            market_data_service["status_note"] = status_note

        market_data["heartbeat_active_symbols"] = heartbeat_active_symbols
        if snapshot_recent or subscription_recent or effective_status == "healthy":
            market_data["feed_status"] = "live"
        else:
            market_data["feed_status"] = effective_status
        market_data["feed_status_note"] = status_note

    async def _read_stream_events(self, topic: str, *, limit: int) -> list[dict[str, Any]]:
        stream = stream_name(self.settings.redis_stream_prefix, topic)
        try:
            entries = await self.redis.xrevrange(stream, count=limit)
        except Exception:
            await self._reconnect_redis()
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
        new_confirmed = {
            str(item.get("ticker", "")).upper()
            for item in strategy_runtime.get("all_confirmed", [])
            if item.get("ticker")
        }
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

        confirmed_only_in_legacy = sorted(legacy_confirmed - new_confirmed)
        confirmed_only_in_new = sorted(new_confirmed - legacy_confirmed)
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
            "provider": active_settings.broker_provider_label,
            "active_broker_providers": active_settings.active_broker_providers,
            "oms_adapter": active_settings.oms_adapter_label,
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
        return {
            "bots": [
                {
                    **bot,
                    "account_summary": _build_bot_account_summary(data, bot),
                }
                for bot in data["bots"]
            ]
        }

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

    @app.get("/api/blacklist")
    async def blacklist() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {
            "blacklist": data["scanner"]["blacklist"],
            "count": data["scanner"]["blacklist_count"],
        }

    @app.get("/scanner/blacklist/add")
    async def scanner_blacklist_add(
        symbol: str,
        reason: str = "manual_scanner_blacklist",
        redirect_to: str = "/scanner/dashboard",
    ) -> RedirectResponse:
        app.state.repository.add_scanner_blacklist_symbol(symbol, reason=reason)
        return RedirectResponse(url=redirect_to, status_code=303)

    @app.get("/scanner/blacklist/remove")
    async def scanner_blacklist_remove(
        symbol: str,
        redirect_to: str = "/scanner/dashboard",
    ) -> RedirectResponse:
        app.state.repository.remove_scanner_blacklist_symbol(symbol)
        return RedirectResponse(url=redirect_to, status_code=303)

    @app.get("/scanner/confirmed")
    async def scanner_confirmed() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {
            "stocks": data["scanner"]["all_confirmed"],
            "count": data["scanner"]["all_confirmed_count"],
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

    @app.get("/botprobe")
    async def bot_probe_status() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return _build_bot_api_payload(data, "macd_30s_probe")

    @app.get("/botreclaim")
    async def bot_reclaim_status() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return _build_bot_api_payload(data, "macd_30s_reclaim")

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

    @app.get("/bot/30s-probe", response_class=HTMLResponse)
    async def bot_30s_probe_page() -> str:
        data = await app.state.repository.load_dashboard_data()
        return _render_bot_detail_page(data, "macd_30s_probe")

    @app.get("/bot/30s-reclaim", response_class=HTMLResponse)
    async def bot_30s_reclaim_page() -> str:
        data = await app.state.repository.load_dashboard_data()
        return _render_bot_detail_page(data, "macd_30s_reclaim")

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
    active_service_count = len(data["services"])
    healthy_service_count = sum(1 for service in data["services"] if service["status"] == "healthy")
    starting_service_count = sum(1 for service in data["services"] if service["status"] == "starting")
    degraded_service_count = active_service_count - healthy_service_count - starting_service_count
    live_bot_count = sum(1 for bot in bot_views if bot["watchlist_count"] or bot["position_count"] or bot["pending_count"])
    service_chip_html = "".join(
        f"""
        <span class="service-chip">
          <span class="status-dot status-{escape(service["status"].lower().replace(" ", "_"))}"></span>
          <span>{escape(service["service_name"])}</span>
          <strong>{escape(service["status"])}</strong>
        </span>
        """
        for service in data["services"]
    ) or '<span class="service-chip"><span class="status-dot status-warning"></span><span>No service heartbeats</span></span>'
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
        for item in scanner["all_confirmed"]
    ) or _empty_row(12, "No confirmed candidates yet")

    bot_cards = "".join(
        f"""
        <article class="bot-card">
          <div class="bot-head">
            <div>
              <h3>{escape(bot["display_name"])}</h3>
              <div class="sub">{escape(bot["strategy_code"])} / {escape(bot["account_display_name"] or "-")}</div>
            </div>
            {_status_badge(bot["wiring_status"])}
          </div>
          <div class="bot-metrics">
            <div><span class="mini-label">Eligible Feed</span><strong>{bot["watchlist_count"]}</strong></div>
            <div><span class="mini-label">Positions</span><strong>{bot["position_count"]}</strong></div>
            <div><span class="mini-label">Pending</span><strong>{bot["pending_count"]}</strong></div>
          </div>
          <div class="bot-lines">
            <p><strong>Execution:</strong> {escape(bot["execution_mode"])} via {escape(bot["provider"])}</p>
            <p><strong>Account:</strong> {escape(bot["account_display_name"] or "-")}</p>
            <p><strong>Live Symbols:</strong> {escape(", ".join(bot["watchlist"][:5]) or "None")}</p>
            <p><strong>Positions:</strong> {escape(_position_preview(bot["positions"]))}</p>
            <p><strong>Recent Intents:</strong> {escape(_intent_preview(bot["recent_intents"]))}</p>
          </div>
        </article>
        """
        for bot in bot_views
    ) or '<div class="muted-box">No bot runtime snapshots available yet.</div>'

    parity_bots = [bot for bot in bot_views if bot.get("tos_parity", {}).get("enabled")]
    parity_rows = "".join(
        f"""
        <tr>
          <td><strong>{escape(bot["display_name"])}</strong></td>
          <td>{_status_badge(bot["tos_parity"]["status"])}</td>
          <td>{escape(bot["tos_parity"]["comparison_target"])}</td>
          <td>{len(bot["tos_parity"]["snapshots"])}</td>
          <td>{escape(", ".join(item["symbol"] for item in bot["tos_parity"]["snapshots"][:4]) or "None")}</td>
          <td>{escape(bot["tos_parity"]["snapshots"][0]["last_bar_at"] if bot["tos_parity"]["snapshots"] else "Awaiting closed 1m bar")}</td>
          <td>{escape(bot["tos_parity"]["summary"])}</td>
        </tr>
        """
        for bot in parity_bots
    ) or _empty_row(7, "No TOS parity-ready bot snapshots yet")

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
          <td>{escape(item.get("intent_type", ""))}</td>
          <td>{escape(item["symbol"])}</td>
          <td>{escape(item["side"])}</td>
          <td>{escape(item["quantity"])}</td>
          <td>{_status_badge(item["status"])}</td>
          <td>{escape(item.get("reason", ""))}</td>
          <td><code>{escape(item["client_order_id"])}</code></td>
          <td>{escape(item["updated_at"])}</td>
        </tr>
        """
        for item in data["recent_orders"]
    ) or _empty_row(9, "No broker orders recorded yet")

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
    latest_fill = data["recent_fills"][0] if data["recent_fills"] else None
    latest_fill_summary = (
        f'{latest_fill["strategy_code"]} {latest_fill["side"]} {latest_fill["symbol"]} @ {latest_fill["price"]}'
        if latest_fill
        else "No fills yet"
    )
    health_summary = (
        f"{healthy_service_count}/{active_service_count} healthy"
        + (f" · {starting_service_count} starting" if starting_service_count else "")
        + (f" · {degraded_service_count} attention" if degraded_service_count else "")
    )
    ops_summary = (
        f'{data["counts"]["open_incidents"]} incidents · '
        f'{data["counts"]["latest_reconciliation_findings"]} findings · '
        f'refresh {refresh_seconds}s'
    )
    shadow_summary = (
        f'{shadow_divergence["issue_count"]} shadow issues · '
        f'{len(shadow_divergence["confirmed_only_in_legacy"])} legacy-only confirmed · '
        f'{len(shadow_divergence["confirmed_only_in_new"])} new-only confirmed'
    )
    orderflow_summary = (
        f'{len(data["recent_intents"])} intents · '
        f'{len(data["recent_orders"])} orders · '
        f'{len(data["recent_fills"])} fills'
    )
    position_summary = (
        f'{len(data["virtual_positions"])} virtual · '
        f'{len(data["account_positions"])} broker-level · '
        f'{len(data["incidents"])} incidents'
    )
    health_basis_summary = (
        f"{healthy_service_count}/{active_service_count} services healthy · "
        f"db {'connected' if not any(error.startswith('database:') for error in data['errors']) else 'attention'} · "
        f"redis {'connected' if not any(error.startswith('redis:') for error in data['errors']) else 'attention'} · "
        f"{data['counts']['open_incidents']} incidents"
    )

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
            padding: 22px 24px;
            margin-bottom: 16px;
          }}
          .hero-head {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 16px;
            flex-wrap: wrap;
          }}
          .hero-copy {{
            max-width: 560px;
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
          .ops-strip {{
            display: grid;
            gap: 10px;
            margin: 0;
            padding: 12px 16px;
            background: rgba(255,255,255,0.76);
            border: 1px solid var(--line);
            border-radius: 18px;
            box-shadow: 0 12px 28px rgba(18, 36, 51, 0.06);
            min-width: min(100%, 560px);
          }}
          .ops-strip-top {{
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            gap: 10px;
            align-items: center;
          }}
          .ops-strip-title {{
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 14px;
            color: var(--muted);
          }}
          .ops-strip-title strong {{
            color: var(--ink);
            font-size: 15px;
          }}
          .service-strip {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
          }}
          .service-chip {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 10px;
            border-radius: 999px;
            border: 1px solid var(--line);
            background: rgba(255,255,255,0.78);
            font-size: 12px;
            color: var(--muted);
          }}
          .service-chip strong {{
            color: var(--ink);
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
          }}
          .status-dot {{
            width: 10px;
            height: 10px;
            border-radius: 999px;
            display: inline-block;
            flex: 0 0 auto;
          }}
          .fold-panel {{
            margin-top: 16px;
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 22px;
            box-shadow: 0 18px 42px rgba(18, 36, 51, 0.08);
            overflow: hidden;
          }}
          .fold-panel summary {{
            list-style: none;
            cursor: pointer;
            padding: 16px 20px;
          }}
          .fold-panel summary::-webkit-details-marker {{
            display: none;
          }}
          .fold-summary {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
          }}
          .fold-title {{
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 18px;
          }}
          .fold-title small {{
            color: var(--muted);
            font-size: 13px;
            font-weight: normal;
          }}
          .fold-meta {{
            color: var(--muted);
            font-size: 13px;
            text-align: right;
          }}
          .fold-content {{
            padding: 0 20px 20px 20px;
          }}
          .details-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 16px;
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
          .status-healthy, .status-filled, .status-pass, .status-open, .status-ready {{
            background: rgba(15, 127, 102, 0.12);
            color: var(--accent);
          }}
          .status-dot.status-healthy, .status-dot.status-filled, .status-dot.status-pass, .status-dot.status-open, .status-dot.status-ready {{
            background: var(--accent);
            color: transparent;
          }}
          .status-starting, .status-accepted, .status-submitted, .status-warning, .status-warming {{
            background: rgba(212, 128, 0, 0.12);
            color: var(--warn);
          }}
          .status-dot.status-starting, .status-dot.status-accepted, .status-dot.status-submitted, .status-dot.status-warning, .status-dot.status-warming {{
            background: var(--warn);
            color: transparent;
          }}
          .status-rejected, .status-degraded, .status-error, .status-closed, .status-critical {{
            background: rgba(192, 57, 43, 0.12);
            color: var(--danger);
          }}
          .status-dot.status-rejected, .status-dot.status-degraded, .status-dot.status-error, .status-dot.status-closed, .status-dot.status-critical {{
            background: var(--danger);
            color: transparent;
          }}
          .status-pending, .status-cancelled, .status-idle {{
            background: rgba(18, 36, 51, 0.1);
            color: var(--ink);
          }}
          .status-dot.status-pending, .status-dot.status-cancelled, .status-dot.status-idle {{
            background: var(--ink);
            color: transparent;
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
            <div class="hero-head">
              <div class="hero-copy">
                <div class="eyebrow">Mai Tai</div>
                <h1>Mai Tai Project</h1>
                <p>Paper trading control plane.</p>
              </div>
              <section class="ops-strip">
                <div class="ops-strip-top">
                  <div class="ops-strip-title">
                    <span class="status-dot status-{escape(data["status"].lower().replace(" ", "_"))}"></span>
                    <strong>Mai Tai System Dock</strong>
                    <span>{escape(health_summary)}</span>
                  </div>
                  <div class="fold-meta">{escape(ops_summary)} · {escape(data["generated_at"])}</div>
                </div>
                <div class="service-strip">{service_chip_html}</div>
              </section>
            </div>
            <div class="cards">
              <div class="card">
                <div class="label">Health</div>
                <div class="value">{data["status"].upper()}</div>
                <p>{escape(health_basis_summary)}</p>
              </div>
              <div class="card">
                <div class="label">Confirmed</div>
                <div class="value">{scanner["top_confirmed_count"]}</div>
                <p>{escape(scanner["status"])} scanner state</p>
              </div>
              <div class="card">
                <div class="label">Watchlist</div>
                <div class="value">{scanner["watchlist_count"]}</div>
                <p>{escape(", ".join(scanner["watchlist"][:4]) or "No active symbols")}</p>
              </div>
              <div class="card">
                <div class="label">Live Symbols</div>
                <div class="value">{data["market_data"]["active_subscription_symbols"]}</div>
                <p>{escape(subscription_summary)}</p>
              </div>
              <div class="card">
                <div class="label">Pending Intents</div>
                <div class="value">{data["counts"]["pending_intents"]}</div>
                <p>{orderflow_summary}</p>
              </div>
              <div class="card">
                <div class="label">Open Positions</div>
                <div class="value">{data["counts"]["open_virtual_positions"]}</div>
                <p>{position_summary}</p>
              </div>
              <div class="card">
                <div class="label">Latest Fill</div>
                <div class="value">{len(data["recent_fills"])}</div>
                <p>{escape(latest_fill_summary)}</p>
              </div>
              <div class="card">
                <div class="label">Cutover Confidence</div>
                <div class="value">{cutover_confidence}/100</div>
                <p>{escape(latest_reconciliation.get("completed_at", "No reconciliation run yet"))}</p>
              </div>
              <div class="card">
                <div class="label">Bots With Activity</div>
                <div class="value">{live_bot_count}</div>
                <p>of {len(bot_views)} strategy runtimes</p>
              </div>
            </div>
          </section>

          <nav class="nav">
            <a href="/scanner/dashboard">Scanner Page</a>
            <a href="/bot/30s">30s Bot</a>
            <a href="/bot/30s-probe">30s Probe</a>
            <a href="/bot/30s-reclaim">30s Reclaim</a>
            <a href="/bot/1m">1m Bot</a>
            <a href="/bot/tos">TOS Bot</a>
            <a href="/bot/runner">Runner Bot</a>
            <a href="#scanner">Scanner</a>
            <a href="#bots">Bots</a>
            <a href="#reconciliation">Reconciliation</a>
            <a href="#orders">Orders</a>
            <a href="#positions">Positions</a>
          </nav>

          <details class="fold-panel" open>
            <summary>
              <div class="fold-summary">
                <div class="fold-title">📈 Overview <small>scanner flow, subscriptions, and top confirmed names</small></div>
                <div class="fold-meta">{scanner["top_confirmed_count"]} confirmed · {scanner["watchlist_count"]} watchlist · {scanner["active_subscription_symbols"]} live symbols</div>
              </div>
            </summary>
            <div class="fold-content">
              <div class="details-grid">
                <section class="section">
                  <div class="section-header">
                    <div>
                      <h2>Overview</h2>
                    <div class="sub">Critical scanner state and the live symbol set.</div>
                    </div>
                  </div>
                  <div class="muted-box">
                    <p><strong>Scanner Status:</strong> {escape(scanner["status"])}</p>
                    <p><strong>Top Confirmed Count:</strong> {scanner["top_confirmed_count"]}</p>
                    <p><strong>Active Subscriptions:</strong> {scanner["active_subscription_symbols"]}</p>
                    <p><strong>Latest Snapshot:</strong> {escape(snapshot_summary)}</p>
                    <p><strong>Latest Fill:</strong> {escape(latest_fill_summary)}</p>
                  </div>
                </section>

                <section class="section">
                  <div class="section-header">
                    <div>
                      <h2>Live Symbols</h2>
                      <div class="sub">Symbols currently pushed into the live tick pipeline.</div>
                    </div>
                  </div>
                  <div class="muted-box">
                    <p><strong>Snapshot Completed:</strong> {escape(latest_snapshot.get("completed_at", "No snapshot timestamp yet"))}</p>
                    <p><strong>Subscribed Symbols:</strong> {escape(", ".join(scanner["subscription_symbols"][:20]) or "None")}</p>
                  </div>
                </section>
              </div>

              <section class="section">
                <div class="section-header">
                  <div>
                    <h2>Confirmed Candidates</h2>
                    <div class="sub">Top confirmed names promoted into the shared bot feed.</div>
                  </div>
                </div>
                <div class="table-card">
                  <table>
                    <thead>
                      <tr><th>Rank</th><th>Ticker</th><th>Path</th><th>Score</th><th>Price</th><th>Change</th><th>Volume</th><th>RVOL</th><th>Spread</th><th>Squeezes</th><th>First Spike</th><th>Feed To</th></tr>
                    </thead>
                    <tbody>{scanner_rows}</tbody>
                  </table>
                </div>
              </section>
            </div>
          </details>

          <section class="section" id="bots">
            <div class="section-header">
              <div>
                <h2>Bot Deck</h2>
                <div class="sub">Legacy-style bot visibility for 30s, 1m, TOS, and Runner.</div>
              </div>
            </div>
            <div class="bot-grid">{bot_cards}</div>
          </section>

          <section class="section">
            <div class="section-header">
              <div>
                <h2>TOS Parity</h2>
                <div class="sub">Closed 1m indicator values published by Mai Tai for side-by-side comparison with thinkorswim charts.</div>
              </div>
            </div>
            <div class="table-card">
              <table>
                <thead>
                  <tr><th>Bot</th><th>Status</th><th>Target</th><th>Snapshots</th><th>Symbols</th><th>Latest Bar</th><th>Summary</th></tr>
                </thead>
                <tbody>{parity_rows}</tbody>
              </table>
            </div>
          </section>

          <details class="fold-panel">
            <summary>
              <div class="fold-summary">
                <div class="fold-title">🩺 System & Health <small>services and runtime diagnostics</small></div>
                <div class="fold-meta">{escape(health_summary)} · {escape(ops_summary)}</div>
              </div>
            </summary>
            <div class="fold-content">
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
                <div style="margin-top: 16px;">{errors_html}</div>
              </section>
            </div>
          </details>

          <details class="fold-panel" id="reconciliation">
            <summary>
              <div class="fold-summary">
                <div class="fold-title">🔎 Reconciliation <small>shared-account integrity and cutover safety</small></div>
                <div class="fold-meta">{latest_reconciliation_summary.get("total_findings", 0)} findings · {latest_reconciliation_summary.get("critical_findings", 0)} critical · confidence {cutover_confidence}/100</div>
              </div>
            </summary>
            <div class="fold-content">
              <div class="details-grid">
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
            </div>
          </details>

          <details class="fold-panel" id="orders">
            <summary>
              <div class="fold-summary">
                <div class="fold-title">🧾 Orders & Fills <small>intent, order, and fill flow</small></div>
                <div class="fold-meta">{escape(orderflow_summary)} · latest fill {escape(latest_fill_summary)}</div>
              </div>
            </summary>
            <div class="fold-content">
              <div class="details-grid">
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
                        <tr><th>Strategy</th><th>Type</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th><th>Reason</th><th>Client Id</th><th>Updated</th></tr>
                      </thead>
                      <tbody>{orders_rows}</tbody>
                    </table>
                  </div>
                </section>

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
              </div>
            </div>
          </details>

          <details class="fold-panel" id="positions">
            <summary>
              <div class="fold-summary">
                <div class="fold-title">📦 Positions & Incidents <small>virtual positions, broker positions, and logged issues</small></div>
                <div class="fold-meta">{escape(position_summary)}</div>
              </div>
            </summary>
            <div class="fold-content">
              <div class="details-grid">
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
            </div>
          </details>
        </div>
      </body>
    </html>
    """


BOT_PAGE_META = {
    "macd_30s": {
        "title": "Mai Tai 30-Second MACD Bot",
        "nav_title": "Mai Tai 30s Core",
        "badge": "30",
        "color": "#2979ff",
        "path": "/bot/30s",
    },
    "macd_30s_probe": {
        "title": "Mai Tai 30-Second Probe Bot",
        "nav_title": "Mai Tai 30s Probe",
        "badge": "P",
        "color": "#00897b",
        "path": "/bot/30s-probe",
    },
    "macd_30s_reclaim": {
        "title": "Mai Tai 30-Second Reclaim Bot",
        "nav_title": "Mai Tai 30s Reclaim",
        "badge": "R",
        "color": "#c62828",
        "path": "/bot/30s-reclaim",
    },
    "macd_1m": {
        "title": "Mai Tai 1-Minute MACD Bot",
        "nav_title": "Mai Tai 1m",
        "badge": "1M",
        "color": "#9c27b0",
        "path": "/bot/1m",
    },
    "tos": {
        "title": "Mai Tai TOS Bot",
        "nav_title": "Mai Tai TOS",
        "badge": "TOS",
        "color": "#ff6f00",
        "path": "/bot/tos",
    },
    "runner": {
        "title": "Mai Tai Runner Bot",
        "nav_title": "Mai Tai Runner",
        "badge": "RUN",
        "color": "#e91e63",
        "path": "/bot/runner",
    },
}


def _format_interval_label(interval_secs: object) -> str:
    value = int(interval_secs or 0)
    if value == 30:
        return "30s"
    if value == 60:
        return "1m"
    if value <= 0:
        return "-"
    return f"{value}s"


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
        "recent_decisions": bot["recent_decisions"],
        "recent_intents": bot["recent_intents"],
        "recent_orders": bot["recent_orders"],
        "recent_fills": bot["recent_fills"],
        "indicator_snapshots": bot["indicator_snapshots"],
        "tos_parity": bot["tos_parity"],
        "account_summary": _build_bot_account_summary(data, bot),
        "trade_log": _build_bot_decision_entries(bot),
    }


def _render_scanner_dashboard(data: dict[str, Any]) -> str:
    scanner = data["scanner"]
    config = data["scanner_config"]
    latest_snapshot = data["market_data"]["latest_snapshot_batch"] or {}
    services = {service["service_name"]: service for service in data["services"]}
    market_data_service = services.get("market-data-gateway", {})
    subscription_symbols = set(scanner["subscription_symbols"])
    heartbeat_active_symbols = max(
        _safe_int(scanner.get("heartbeat_active_symbols", 0)),
        _safe_int(market_data_service.get("details", {}).get("active_symbols", 0)),
    )
    displayed_subscription_count = max(int(scanner["active_subscription_symbols"] or 0), heartbeat_active_symbols)
    latest_snapshot_recent = _is_recent_eastern_label(latest_snapshot.get("completed_at"), max_age_seconds=20)

    confirmed_rows = _render_scanner_confirmed_rows(
        scanner["all_confirmed"][:20],
        subscription_symbols,
        set(scanner.get("blacklist_symbols", [])),
    )
    pillar_rows = _render_scanner_stock_rows(scanner["five_pillars"][:20], subscription_symbols)
    gainer_rows = _render_scanner_stock_rows(scanner["top_gainers"][:20], subscription_symbols)
    alert_rows = _render_alert_rows(scanner["recent_alerts"])
    confirmed_sub = "Full confirmed universe for the current session. TOP5 and bot badges mark the active ranked subset."

    warmup = scanner["alert_warmup"]
    websocket_status = market_data_service.get("status", "unknown")
    websocket_label = "⚡ live" if websocket_status == "healthy" else websocket_status
    if latest_snapshot_recent and websocket_status != "healthy":
        websocket_label = "LIVE"
    effective_websocket_status = str(
        market_data_service.get("effective_status", market_data_service.get("status", "unknown"))
    )
    raw_websocket_status = str(
        market_data_service.get("raw_status", market_data_service.get("status", "unknown"))
    )
    websocket_status = effective_websocket_status
    websocket_label = "LIVE" if scanner.get("feed_status") == "live" else websocket_status
    if raw_websocket_status == "healthy" and websocket_status == "healthy":
        websocket_label = "LIVE"
    feed_status_note = str(scanner.get("feed_status_note", "") or market_data_service.get("status_note", "") or "")
    if raw_websocket_status != websocket_status:
        prefix = f"Heartbeat raw status {raw_websocket_status}."
        feed_status_note = f"{prefix} {feed_status_note}".strip()
    reconcile_note = (
        "All positions synced"
        if data["counts"]["open_incidents"] == 0
        else f'{data["counts"]["open_incidents"]} open incidents'
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
    ) or (
        f'<span style="color:#7b86a4;">Heartbeat reports {displayed_subscription_count} live symbols; subscription stream details unavailable.</span>'
        if displayed_subscription_count > 0
        else '<span style="color:#7b86a4;">No live subscriptions yet</span>'
    )
    blacklist_html = _render_scanner_blacklist_entries(scanner.get("blacklist", []))

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Momentum Scanner Dashboard</title>
    <meta charset="utf-8">
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
        .nav-strip {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .nav-strip a {{
            text-decoration: none;
            color: var(--ink);
            background: linear-gradient(180deg, rgba(89,215,255,0.08), rgba(255,255,255,0.02));
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 6px 10px;
            font-size: 12px;
            line-height: 1;
        }}
        .nav-strip a.active {{
            border-color: #59d7ff;
            box-shadow: inset 0 0 0 1px rgba(89,215,255,0.5);
            background: linear-gradient(180deg, rgba(89,215,255,0.18), rgba(255,255,255,0.02));
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
            overflow: auto;
            max-width: 100%;
        }}
        .table-wrap-confirmed {{
            max-height: 520px;
        }}
        .table-wrap-list {{
            max-height: 360px;
        }}
        .table-wrap-alerts {{
            max-height: 420px;
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
                    <h1>Mai Tai Scanner Deck</h1>
                    <p>Dedicated scanner workspace for the new platform</p>
                </div>
            </div>

            <div class="metric-grid">
                <div class="metric-card">
                    <span>Confirmed</span>
                    <strong>{scanner["all_confirmed_count"]}</strong>
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
                <div class="metric-card">
                    <span>Blacklisted</span>
                    <strong>{scanner["blacklist_count"]}</strong>
                </div>
            </div>

            <div class="side-section">
                <div class="side-label">Navigation</div>
                <div class="nav-strip">
                    <a href="/scanner/dashboard" class="active">Mai Tai Scanner</a>
                    <a href="/">Mai Tai Control Plane</a>
                    <a href="/bot/30s">Mai Tai 30s Core</a>
                    <a href="/bot/30s-probe">Mai Tai 30s Probe</a>
                    <a href="/bot/30s-reclaim">Mai Tai 30s Reclaim</a>
                    <a href="/bot/1m">Mai Tai 1m</a>
                    <a href="/bot/tos">Mai Tai TOS</a>
                    <a href="/bot/runner">Mai Tai Runner</a>
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
                <div class="side-label">Overview</div>
                <div class="stack">
                    <div class="line-item"><strong>Status:</strong> {escape(scanner["status"])}</div>
                    <div class="line-item"><strong>Ref Tickers:</strong> {latest_snapshot.get("reference_count", 0):,}</div>
                    <div class="line-item"><strong>WebSocket:</strong> {escape(websocket_label)} ({displayed_subscription_count} subs)</div>
                    <div class="line-item"><strong>Reconcile:</strong> {escape(reconcile_note)}</div>
                </div>
            </div>
        </aside>

        <main class="workspace">
            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h2>Momentum Confirmed</h2>
                        <div class="sub">{confirmed_sub}</div>
                    </div>
                    <span class="count green">{scanner["all_confirmed_count"]} names</span>
                </div>
                <div class="table-wrap table-wrap-confirmed">
                    <table>
                        <thead><tr><th>#</th><th>Ticker / Bot</th><th>Score</th><th>Confirmed</th><th>Entry Price</th><th>Price</th><th>Change%</th><th>Volume</th><th>RVol</th><th>Squeezes</th><th>1st Spike</th><th>Catalyst</th></tr></thead>
                        <tbody>{confirmed_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>5 Pillars Scanner</h3>
                        <div class="sub">Qualifying names across the preserved five-pillar filter.</div>
                    </div>
                    <span class="count green">{scanner["five_pillars_count"]}</span>
                </div>
                <div class="table-wrap table-wrap-list">
                    <table>
                        <thead><tr><th>#</th><th>Ticker</th><th>First Seen</th><th>Price</th><th>Change%</th><th>Spread</th><th>Volume</th><th>RVol</th><th>Age</th></tr></thead>
                        <tbody>{pillar_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>Top Gainers</h3>
                        <div class="sub">Independent ranker refreshed from snapshot state.</div>
                    </div>
                    <span class="count pink">{scanner["top_gainers_count"]}</span>
                </div>
                <div class="table-wrap table-wrap-list">
                    <table>
                        <thead><tr><th>#</th><th>Ticker</th><th>First Seen</th><th>Price</th><th>Change%</th><th>Spread</th><th>Volume</th><th>RVol</th><th>Age</th></tr></thead>
                        <tbody>{gainer_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>Top Gainer Changes</h3>
                        <div class="sub">Latest rank and direction changes flowing through the top-gainer feed.</div>
                    </div>
                    <span class="count amber">{len(scanner.get("top_gainer_changes", []))}</span>
                </div>
                <div class="table-wrap table-wrap-list">
                    <table>
                        <thead><tr><th>Type</th><th>Ticker</th><th>Time</th><th>Direction</th></tr></thead>
                        <tbody>{top_gainer_change_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>Momentum Alerts</h3>
                        <div class="sub">Latest alert tape with simple color-coded momentum events.</div>
                    </div>
                    <span class="count amber">{scanner["recent_alerts_count"]}</span>
                </div>
                <div class="panel-copy">Warmup: {"Ready" if warmup.get("fully_ready") else "History building"} | 5m ready: {"yes" if warmup.get("squeeze_5min_ready") else "no"} | 10m ready: {"yes" if warmup.get("squeeze_10min_ready") else "no"}</div>
                <div class="table-wrap table-wrap-alerts">
                    <table>
                        <thead><tr><th>Time</th><th>Type</th><th>Ticker</th><th style="text-align:right">Price</th><th style="text-align:right">Volume</th><th>Details</th></tr></thead>
                        <tbody>{alert_rows}</tbody>
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
    position_rows = _build_bot_position_rows(data, bot)
    closed_today = sorted(
        list(bot.get("closed_today", [])),
        key=lambda item: str(item.get("exit_time", "") or item.get("closed_at", "") or item.get("entry_time", "")),
        reverse=True,
    )
    closed_rows = _build_closed_trade_rows_v2(closed_today)
    trade_summary_rows, trade_summary_count = _build_trade_summary_rows(bot)
    decision_rows = _build_bot_decision_rows(bot)
    failed_rows, failed_count = _build_failed_action_rows(bot)
    pnl_color = "#5fff8d" if bot["daily_pnl"] >= 0 else "#ff6b6b"
    recent_fill_count = len(recent_fills)
    active_symbols: list[str] = []
    for item in bot["positions"]:
        symbol = str(item.get("ticker") or item.get("symbol") or "").upper()
        if symbol and symbol not in active_symbols:
            active_symbols.append(symbol)
    for symbol in bot["pending_open_symbols"]:
        normalized = str(symbol).upper()
        if normalized and normalized not in active_symbols:
            active_symbols.append(normalized)
    for symbol in bot["pending_close_symbols"]:
        normalized = str(symbol).upper()
        if normalized and normalized not in active_symbols:
            active_symbols.append(normalized)
    for symbol in bot["watchlist"][:10]:
        normalized = str(symbol).upper()
        if normalized and normalized not in active_symbols:
            active_symbols.append(normalized)
    open_symbols = {
        str(item.get("ticker") or item.get("symbol") or "").upper()
        for item in bot["positions"]
        if item.get("ticker") or item.get("symbol")
    }
    pending_symbols = {str(symbol).upper() for symbol in bot["pending_open_symbols"] + bot["pending_close_symbols"]}
    live_symbol_html = "".join(
        f'<span class="pill-chip {"live" if symbol in open_symbols else "amber" if symbol in pending_symbols else ""}">{escape(symbol)}</span>'
        for symbol in active_symbols
    ) or '<span style="color:#7b86a4;">No live symbols in this bot</span>'
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

    closed_trades_panel = f"""
        <section class="panel full">
            <div class="panel-header">
                <div>
                    <h3>Closed Trades</h3>
                    <div class="sub">Completed trades with entry, exit, realized P&amp;L, and close reason.</div>
                </div>
                <span class="count pink">{len(closed_today)}</span>
            </div>
            <div class="table-wrap">
                <table>
                    <thead><tr><th>Ticker</th><th>Path</th><th style="text-align:right">Qty</th><th>Entry Time</th><th style="text-align:right">Entry</th><th>Exit Time</th><th style="text-align:right">Exit</th><th>P&amp;L</th><th>Reason</th></tr></thead>
                    <tbody>{closed_rows}</tbody>
                </table>
            </div>
        </section>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>{meta["title"]}</title>
    <meta charset="utf-8">
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
        .nav-strip {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .nav-strip a {{
            text-decoration: none;
            color: var(--ink);
            background: linear-gradient(180deg, rgba(89,215,255,0.08), rgba(255,255,255,0.02));
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 6px 10px;
            font-size: 12px;
            line-height: 1;
        }}
        .nav-strip a.active {{
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
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
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
            .summary-grid {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
    <div class="shell">
        <aside class="sidebar">
            <div class="brand">
                <div class="brand-badge">{meta["badge"]}</div>
                <div>
                    <h1>{meta["title"]}</h1>
                    <p>Dedicated execution workspace for this bot.</p>
                </div>
            </div>

            <div class="side-section">
                <div class="stack">
                    <div class="line-item"><strong>Status:</strong> {escape(bot["wiring_status"].upper())}</div>
                    <div class="line-item"><strong>Account:</strong> {escape(bot["account_display_name"])}</div>
                    <div class="line-item"><strong>Mode:</strong> {escape(bot["execution_mode"].upper())}</div>
                    <div class="line-item"><strong>Provider:</strong> {escape(bot["provider"].upper())}</div>
                    <div class="line-item"><strong>Interval:</strong> {_format_interval_label(bot.get("interval_secs"))}</div>
                </div>
            </div>

            <div class="side-section">
                <div class="side-label">Live Symbols</div>
                <div>{live_symbol_html}</div>
            </div>

            <div class="side-section">
                <div class="side-label">Overview</div>
                <div class="metric-grid">
                    <div class="metric-card">
                        <span>Daily P&amp;L</span>
                        <strong style="color:{pnl_color};">${bot["daily_pnl"]:+,.2f}</strong>
                    </div>
                    <div class="metric-card">
                        <span>Open</span>
                        <strong>{bot["position_count"]}</strong>
                    </div>
                    <div class="metric-card">
                        <span>Closed</span>
                        <strong>{len(closed_today)}</strong>
                    </div>
                    <div class="metric-card">
                        <span>Pending</span>
                        <strong>{bot["pending_count"]}</strong>
                    </div>
                    <div class="metric-card">
                        <span>Trades</span>
                        <strong>{recent_fill_count}</strong>
                    </div>
                </div>
            </div>
        </aside>

        <main class="workspace">
            <section class="panel full accent-panel">
                <div class="panel-header">
                    <div>
                        <h2>Bot Navigation</h2>
                        <div class="sub">Quick switch between scanner, control plane, and bot pages.</div>
                    </div>
                    <span class="count accent">{escape(bot["display_name"])}</span>
                </div>
                <div class="panel-copy">{_render_page_nav(strategy_code)}</div>
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

            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>Trade Summary</h3>
                        <div class="sub">One row per reclaim trade attempt, paired from entry through exit when available.</div>
                    </div>
                    <span class="count accent">{trade_summary_count}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Ticker</th><th>Entry Time</th><th>Entry</th><th style="text-align:right">Qty</th><th>Exit Time</th><th>Exit</th><th>Status</th></tr></thead>
                        <tbody>{trade_summary_rows}</tbody>
                    </table>
                </div>
            </section>

            {closed_trades_panel}

            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>Bot Decisions</h3>
                        <div class="sub">Recent entry checks and block reasons from the strategy runtime.</div>
                    </div>
                    <span class="count accent">{min(len(bot.get("recent_decisions", [])), 50)}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Bar Time</th><th>Ticker</th><th>Status</th><th>Reason</th><th>Path</th><th style="text-align:right">Score</th><th style="text-align:right">Price</th></tr></thead>
                        <tbody>{decision_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>Failed Actions</h3>
                        <div class="sub">Recent failed intent and order events in a simple tape format.</div>
                    </div>
                    <span class="count pink">{failed_count}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Time</th><th>Ticker</th><th>Stage</th><th>Action</th><th style="text-align:right">Qty</th><th>Status</th><th>Reason</th><th>Note</th></tr></thead>
                        <tbody>{failed_rows}</tbody>
                    </table>
                </div>
            </section>
        </main>
    </div>
</body>
</html>"""


def _render_page_nav(active: str) -> str:
    links: list[str] = []
    for code, meta in BOT_PAGE_META.items():
        links.append(
            f'<a href="{meta["path"]}" class="{"active" if code == active else ""}">{escape(str(meta.get("nav_title", meta["title"])).replace(" Bot", ""))}</a>'
        )
    return (
        '<div class="nav-strip">'
        '<a href="/scanner/dashboard">Mai Tai Scanner</a>'
        + "".join(links)
        + '<a href="/">Mai Tai Control Plane</a>'
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


def _build_trade_summary_rows(bot: dict[str, Any]) -> tuple[str, int]:
    fills_by_symbol_side: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in reversed(bot.get("recent_fills", [])):
        symbol = str(item.get("symbol", "")).upper()
        side = str(item.get("side", "")).lower()
        if symbol and side:
            fills_by_symbol_side.setdefault((symbol, side), []).append(item)

    def _consume_fill(symbol: str, side: str, *, only_when_filled: bool) -> dict[str, Any] | None:
        if only_when_filled:
            queue = fills_by_symbol_side.get((symbol, side), [])
            if queue:
                return queue.pop(0)
        return None

    rows: list[dict[str, str]] = []
    open_rows_by_symbol: dict[str, list[dict[str, str]]] = {}

    for item in reversed(bot.get("recent_intents", [])):
        intent_type = str(item.get("intent_type", "")).lower()
        if intent_type not in {"open", "close"}:
            continue

        symbol = str(item.get("symbol", "")).upper()
        side = str(item.get("side", "")).lower()
        status = str(item.get("status", "")).lower()
        quantity = str(item.get("quantity", "") or "-")
        reason = str(item.get("reason", "") or "-")
        updated_at = str(item.get("updated_at", "") or "-")
        fill = _consume_fill(symbol, side, only_when_filled=status == "filled")
        fill_price = _fmt_money(_as_float(fill.get("price"))) if fill else "-"
        fill_time = str(fill.get("filled_at", "") or updated_at) if fill else updated_at

        if intent_type == "open":
            if status == "filled":
                status_label = "Open Position"
                status_color = "#5fff8d"
            elif status in {"submitted", "accepted", "pending", "partially_filled"}:
                status_label = "Open Order Pending"
                status_color = "#ffcc5b"
            elif status == "cancelled":
                status_label = "Cancelled"
                status_color = "#ff8c42"
            elif status == "rejected":
                status_label = "Rejected"
                status_color = "#ff6b6b"
            else:
                status_label = status.replace("_", " ").title() or "Open"
                status_color = "#98a6c8"

            row = {
                "symbol": symbol or "-",
                "entry_time": fill_time,
                "entry_detail": f"{fill_price}<br><span style=\"font-size:11px;color:#98a6c8;\">{escape(reason)}</span>",
                "quantity": escape(quantity),
                "exit_time": "-",
                "exit_detail": '<span style="color:#7b86a4;">Waiting</span>',
                "status_label": status_label,
                "status_color": status_color,
                "sort_time": fill_time,
            }
            rows.append(row)
            if status in {"filled", "submitted", "accepted", "pending", "partially_filled"}:
                open_rows_by_symbol.setdefault(symbol, []).append(row)
            continue

        close_status = status.replace("_", " ").title() or "Close"
        close_color = "#98a6c8"
        if status == "filled":
            close_status = "Closed"
            close_color = "#59d7ff"
        elif status in {"submitted", "accepted", "pending", "partially_filled"}:
            close_status = "Close Pending"
            close_color = "#ffcc5b"
        elif status == "cancelled":
            close_status = "Close Cancelled"
            close_color = "#ff8c42"
        elif status == "rejected":
            close_status = "Close Rejected"
            close_color = "#ff6b6b"

        open_queue = open_rows_by_symbol.get(symbol, [])
        target_row = next((candidate for candidate in open_queue if candidate["exit_time"] == "-"), None)
        if target_row is None:
            target_row = {
                "symbol": symbol or "-",
                "entry_time": "-",
                "entry_detail": '<span style="color:#7b86a4;">Entry not found on page</span>',
                "quantity": escape(quantity),
                "exit_time": "-",
                "exit_detail": '<span style="color:#7b86a4;">Waiting</span>',
                "status_label": close_status,
                "status_color": close_color,
                "sort_time": fill_time,
            }
            rows.append(target_row)

        target_row["exit_time"] = fill_time
        target_row["exit_detail"] = (
            f"{fill_price}<br><span style=\"font-size:11px;color:#98a6c8;\">{escape(reason)}</span>"
        )
        target_row["status_label"] = close_status
        target_row["status_color"] = close_color
        target_row["sort_time"] = fill_time

    if not rows:
        return (
            '<tr><td colspan="7" style="text-align:center;color:#7b86a4;padding:15px;">'
            "No trade attempts yet</td></tr>",
            0,
        )

    rendered = "".join(
        f"""<tr>
            <td><strong>{escape(row["symbol"])}</strong></td>
            <td style="font-size:11px">{escape(row["entry_time"])}</td>
            <td>{row["entry_detail"]}</td>
            <td style="text-align:right">{row["quantity"]}</td>
            <td style="font-size:11px">{escape(row["exit_time"])}</td>
            <td>{row["exit_detail"]}</td>
            <td><span style="color:{row["status_color"]};font-weight:700;">{escape(row["status_label"])}</span></td>
        </tr>"""
        for row in sorted(rows, key=lambda item: str(item.get("entry_time", "")), reverse=True)[:25]
    )
    return rendered, len(rows)


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


def _build_tos_parity_rows(parity: dict[str, Any]) -> str:
    snapshots = parity.get("snapshots", [])
    if not snapshots:
        return '<tr><td colspan="10" style="text-align:center;color:#7b86a4;padding:15px;">No closed 1m bars published yet</td></tr>'

    rows: list[str] = []
    for item in snapshots:
        close_price = _as_float(item.get("close"))
        ema9_delta = close_price - _as_float(item.get("ema9"))
        ema20_delta = close_price - _as_float(item.get("ema20"))
        macd_delta = _as_float(item.get("macd")) - _as_float(item.get("signal"))
        vwap_delta = close_price - _as_float(item.get("vwap"))
        flags = []
        flags.append("MACD>Signal" if item.get("macd_above_signal") else "MACD<Signal")
        if item.get("price_above_vwap"):
            flags.append("Above VWAP")
        if item.get("price_above_ema9"):
            flags.append("Above EMA9")
        if item.get("price_above_ema20"):
            flags.append("Above EMA20")
        rows.append(
            f"""<tr>
            <td><strong>{escape(item["symbol"])}</strong></td>
            <td>{escape(item["last_bar_at"])}</td>
            <td style="text-align:right">{item["close"]:.4f}</td>
            <td style="text-align:right">{item["ema9"]:.4f}<br>{_render_tos_tolerance_hint(ema9_delta, label="Δ", tight=0.02, watch=0.05)}</td>
            <td style="text-align:right">{item["ema20"]:.4f}<br>{_render_tos_tolerance_hint(ema20_delta, label="Δ", tight=0.03, watch=0.08)}</td>
            <td style="text-align:right">{item["macd"]:.5f}<br>{_render_tos_tolerance_hint(macd_delta, label="gap", tight=0.003, watch=0.010)}</td>
            <td style="text-align:right">{item["signal"]:.5f}</td>
            <td style="text-align:right">{item["histogram"]:.5f}</td>
            <td style="text-align:right">{item["vwap"]:.4f}<br>{_render_tos_tolerance_hint(vwap_delta, label="Δ", tight=0.02, watch=0.05)}</td>
            <td>{escape(', '.join(flags) or '-')}</td>
        </tr>"""
        )
    return "".join(rows)


def _render_tos_tolerance_hint(delta: float, *, label: str, tight: float, watch: float) -> str:
    abs_delta = abs(delta)
    if abs_delta <= tight:
        color = "#ffcc5b"
        state = "tight"
    elif abs_delta <= watch:
        color = "#59d7ff"
        state = "watch"
    else:
        color = "#5fff8d" if delta >= 0 else "#ff6b6b"
        state = "clear"
    return (
        f'<span style="font-size:10px;color:{color};">'
        f'{escape(label)} {_format_signed_decimal(delta)} {escape(state)}</span>'
    )


def _build_bot_account_summary(data: dict[str, Any], bot: dict[str, Any]) -> dict[str, Any]:
    account_rows = [
        item for item in data["account_positions"] if item.get("broker_account_name") == bot["account_name"]
    ]
    virtual_rows = [
        item for item in data["virtual_positions"] if item.get("strategy_code") == bot["strategy_code"]
    ]
    strategy_symbols = {str(item.get("symbol", "")).upper() for item in virtual_rows if item.get("symbol")}
    account_symbols = {str(item.get("symbol", "")).upper() for item in account_rows if item.get("symbol")}
    other_symbols = sorted(account_symbols - strategy_symbols)
    gross_market_value = sum(_as_float(item.get("market_value")) for item in account_rows)
    latest_updated_at = max((str(item.get("updated_at", "")) for item in account_rows), default="")
    return {
        "account_position_count": len(account_rows),
        "strategy_symbol_count": len(strategy_symbols),
        "non_strategy_symbol_count": len(other_symbols),
        "non_strategy_symbols": other_symbols,
        "gross_market_value": gross_market_value,
        "latest_updated_at": latest_updated_at,
    }


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

    def _symbol_sort_key(symbol: str) -> str:
        runtime = runtime_positions.get(symbol) or {}
        virtual = virtual_positions.get(symbol) or {}
        account = account_positions.get(symbol) or {}
        return str(
            runtime.get("entry_time")
            or virtual.get("updated_at")
            or account.get("updated_at")
            or ""
        )

    symbols = sorted(
        set(runtime_positions) | set(virtual_positions) | set(account_positions),
        key=_symbol_sort_key,
        reverse=True,
    )
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
        elif runtime and not account and bot.get("runtime_kind") == "macd":
            status_html = '<span style="color:#ff1744">⚠️ GHOST (not on broker)</span>'
        else:
            status_html = '<span style="color:#888">-</span>'

        pnl_amount = 0.0
        pnl_pct = 0.0
        if current_price > 0 and runtime_entry > 0 and runtime_qty > 0:
            pnl_amount = (current_price - runtime_entry) * runtime_qty
            pnl_pct = ((current_price - runtime_entry) / runtime_entry) * 100
        pnl_color = "#00c853" if pnl_amount >= 0 else "#ff1744"
        time_text = escape(str(runtime.get("entry_time", ""))) if runtime else "-"

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
    for item in bot.get("recent_decisions", []):
        status = str(item.get("status", "info"))
        color = "#7b86a4"
        if status == "signal":
            color = "#00c853"
        elif status == "blocked":
            color = "#ff9100"
        elif status == "pending":
            color = "#40c4ff"
        text = (
            f'{item.get("last_bar_at", "")} BAR {item.get("symbol", "")}'
            f' | {status.upper()} | {item.get("reason", "")}'
        )
        if item.get("path"):
            text += f' | {item["path"]}'
        if item.get("score"):
            text += f' | score={item["score"]}'
        entries.append({"color": color, "text": text})
    for item in bot["recent_intents"]:
        entries.append(
            {
                "color": "#00c853" if item["intent_type"] == "open" else "#ffd600" if item["intent_type"] == "scale" else "#ff1744",
                "text": f'{item["updated_at"]} {item["intent_type"].upper()} {item["symbol"]} {item["side"].upper()} qty={item["quantity"]} | {item["reason"]} | {item["status"].upper()}',
            }
        )
    return entries[:50]


def _build_bot_decision_rows(bot: dict[str, Any]) -> str:
    rows: list[str] = []
    for item in bot.get("recent_decisions", [])[:50]:
        status = str(item.get("status", "") or "").lower()
        if status == "signal":
            status_color = "#00c853"
        elif status == "blocked":
            status_color = "#ff9100"
        elif status == "pending":
            status_color = "#40c4ff"
        else:
            status_color = "#98a6c8"
        score = item.get("score")
        score_text = "-" if score in (None, "") else escape(str(score))
        price = item.get("price")
        price_text = "-" if price in (None, "") else _fmt_money(_as_float(price))
        rows.append(
            f"""<tr>
            <td>{escape(str(item.get("last_bar_at", "")) or "-")}</td>
            <td><strong>{escape(str(item.get("symbol", "")) or "-")}</strong></td>
            <td style="color:{status_color};font-weight:bold;">{escape(status.upper() or "INFO")}</td>
            <td>{escape(str(item.get("reason", "")) or "-")}</td>
            <td>{escape(str(item.get("path", "")) or "-")}</td>
            <td style="text-align:right">{score_text}</td>
            <td style="text-align:right">{price_text}</td>
        </tr>"""
        )
    if not rows:
        return '<tr><td colspan="7" style="text-align:center;color:#888;">No bot decisions yet</td></tr>'
    return "".join(rows)


def _build_failed_action_rows(bot: dict[str, Any]) -> tuple[str, int]:
    failed_statuses = {"rejected", "canceled", "cancelled", "failed", "expired", "error"}
    failures: list[dict[str, str]] = []

    for item in bot.get("recent_orders", []):
        status = str(item.get("status", "") or "").lower()
        if status not in failed_statuses:
            continue
        failures.append(
            {
                "updated": str(item.get("updated_at", "") or ""),
                "stage": "order",
                "ticker": str(item.get("symbol", "") or ""),
                "side": str(item.get("side", "") or ""),
                "intent_type": str(item.get("intent_type", "") or ""),
                "qty": str(item.get("quantity", "") or ""),
                "status": status,
                "reason": _failure_reason_label(status, item.get("reason")),
                "note": _failure_order_note(item),
            }
        )

    for item in bot.get("recent_intents", []):
        status = str(item.get("status", "") or "").lower()
        if status not in failed_statuses:
            continue
        failures.append(
            {
                "updated": str(item.get("updated_at", "") or ""),
                "stage": "intent",
                "ticker": str(item.get("symbol", "") or ""),
                "side": str(item.get("side", "") or ""),
                "intent_type": str(item.get("intent_type", "") or ""),
                "qty": str(item.get("quantity", "") or ""),
                "status": status,
                "reason": _failure_reason_label(status, item.get("reason")),
                "note": "strategy intent",
            }
        )

    failures.sort(key=lambda item: item.get("updated", ""), reverse=True)
    if not failures:
        return '<tr><td colspan="8" style="text-align:center;color:#888;">No failed actions</td></tr>', 0

    rows: list[str] = []
    for item in failures[:50]:
        status = item["status"].upper()
        status_color = "#ff6b6b" if status in {"REJECTED", "FAILED", "ERROR"} else "#ffcc5b"
        action = _failure_action_label(item.get("intent_type", ""), item.get("side", ""))
        rows.append(
            f"""<tr>
            <td>{escape(item["updated"] or "-")}</td>
            <td><strong>{escape(item["ticker"] or "-")}</strong></td>
            <td>{escape(str(item.get("stage", "")).upper() or "-")}</td>
            <td>{escape(action)}</td>
            <td style="text-align:right">{escape(item["qty"] or "-")}</td>
            <td style="color:{status_color};font-weight:bold;">{escape(status)}</td>
            <td>{escape(item["reason"] or "-")}</td>
            <td>{escape(str(item.get("note", "")) or "-")}</td>
        </tr>"""
        )
    return "".join(rows), len(failures)


def _failure_action_label(intent_type: str, side: str) -> str:
    intent = (intent_type or "").strip().lower()
    side_text = (side or "").strip().lower()
    intent_label = intent.replace("_", " ").title() if intent else "Order"
    side_label = side_text.title() if side_text else ""
    return f"{intent_label} {side_label}".strip() or "-"


def _failure_reason_label(status: str, raw_reason: Any) -> str:
    reason_text = str(raw_reason or "").strip()
    normalized_status = (status or "").lower()
    if reason_text and not reason_text.startswith("{"):
        return reason_text
    if normalized_status in {"canceled", "cancelled"}:
        return "Broker cancel"
    if normalized_status == "rejected":
        return "Broker reject"
    if normalized_status == "expired":
        return "Order expired"
    if normalized_status in {"failed", "error"}:
        return "Broker error"
    return "Execution issue"


def _failure_order_note(item: dict[str, Any]) -> str:
    bits: list[str] = []
    order_type = str(item.get("order_type", "") or "").strip()
    tif = str(item.get("time_in_force", "") or "").strip()
    if order_type:
        bits.append(order_type.lower())
    if tif:
        bits.append(tif.lower())
    if item.get("extended_hours"):
        bits.append("extended hours")
    client_order_id = str(item.get("client_order_id", "") or "").strip()
    if not bits and client_order_id:
        bits.append("broker order")
    return ", ".join(bits) if bits else "broker order"


def _build_closed_trade_rows_v2(closed_today: list[dict[str, Any]]) -> str:
    if not closed_today:
        return '<tr><td colspan="9" style="text-align:center;color:#888;">No closed trades</td></tr>'

    rows: list[str] = []
    for item in sorted(
        closed_today,
        key=lambda item: str(item.get("exit_time", "") or item.get("closed_at", "") or item.get("entry_time", "")),
        reverse=True,
    ):
        pnl = _as_float(item.get("pnl"))
        color = "#00c853" if pnl >= 0 else "#ff1744"
        qty = item.get("quantity", item.get("qty", ""))
        path = str(item.get("path", "") or item.get("entry_path", "") or "-")
        reason = str(item.get("reason", "") or item.get("exit_reason", "") or "-")
        rows.append(
            f"""<tr>
            <td><strong>{escape(str(item.get("ticker", "")) or "-")}</strong></td>
            <td>{escape(path)}</td>
            <td style="text-align:right">{escape(str(qty) or "-")}</td>
            <td>{escape(str(item.get("entry_time", "")) or "-")}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("entry_price")))}</td>
            <td>{escape(str(item.get("exit_time", "")) or "-")}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("exit_price")))}</td>
            <td style="color:{color}">${pnl:+.2f} ({_as_float(item.get("pnl_pct")):+.1f}%)</td>
            <td>{escape(reason)}</td>
        </tr>"""
        )
    return "".join(rows)


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


def _render_scanner_confirmed_rows(
    rows: list[dict[str, Any]],
    live_symbols: set[str],
    blacklisted_symbols: set[str],
) -> str:
    if not rows:
        return '<tr><td colspan="12" style="text-align:center;color:#888;padding:20px;">No confirmed candidates yet</td></tr>'
    rendered = []
    for index, item in enumerate(rows, start=1):
        ticker = str(item.get("ticker", "")).upper()
        live_badge = ' <span style="color:#00ff41;font-size:10px;">LIVE</span>' if ticker in live_symbols else ""
        top5_badge = (
            ' <span style="background:#ffd600;color:#000;font-size:9px;padding:1px 4px;border-radius:3px;font-weight:bold;">TOP5</span>'
            if item.get("is_top5")
            else ""
        )
        change_pct = _as_float(item.get("change_pct"))
        row_bg = "#0a1a0a" if item.get("is_top5") else "transparent"
        catalyst_html = _render_confirmed_catalyst_cell(item)
        rendered.append(
            f"""<tr style="background:{row_bg};">
            <td style="text-align:center">{index}</td>
            <td><strong>{escape(ticker)}</strong>{live_badge}{top5_badge}</td>
            <td style="color:#ffd600;font-weight:bold;">{_as_float(item.get("rank_score")):.0f}</td>
            <td style="color:#00ff41;">{escape(str(item.get("confirmed_at", item.get("first_spike_time", ""))))}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("entry_price")))}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("price")))}</td>
            <td style="text-align:right;color:{'#00c853' if change_pct >= 0 else '#ff1744'}">{change_pct:+.1f}%</td>
            <td style="text-align:right">{_short_volume(item.get("volume"))}</td>
            <td style="text-align:right">{_as_float(item.get("rvol")):.1f}x</td>
            <td style="text-align:right">{int(item.get("squeeze_count", 0) or 0)}</td>
            <td>{escape(str(item.get("first_spike_time", "")))}</td>
            <td style="font-size:12px;min-width:180px;max-width:320px;white-space:normal;overflow-wrap:anywhere;">{catalyst_html}</td>
        </tr>"""
        )
    return "".join(rendered)


def _render_confirmed_catalyst_cell_legacy(item: dict[str, Any]) -> str:
    catalyst = str(item.get("catalyst_type") or item.get("catalyst") or "").strip()
    headline = str(item.get("headline", "") or "").strip()
    news_url = str(item.get("news_url", "") or "").strip()
    news_date = str(item.get("news_date", "") or "").strip()
    sentiment = str(item.get("direction") or item.get("sentiment") or "").strip().lower()
    news_fetch_status = str(item.get("news_fetch_status", "") or "").strip().lower()
    catalyst_status = str(item.get("catalyst_status", "") or "").strip().lower()
    confidence = _as_float(item.get("catalyst_confidence"))
    article_count = int(item.get("article_count", 0) or 0)
    real_article_count = int(item.get("real_catalyst_article_count", 0) or 0)
    freshness_minutes = item.get("freshness_minutes")
    is_generic_roundup = bool(item.get("is_generic_roundup", False))
    has_real_catalyst = bool(item.get("has_real_catalyst", False))
    reason = str(item.get("catalyst_reason", "") or "").strip()
    news_window_start = str(item.get("news_window_start", "") or "").strip()
    path_a_eligible = bool(item.get("path_a_eligible", False))
    ai_shadow_status = str(item.get("ai_shadow_status", "") or "").strip().lower()
    ai_shadow_provider = str(item.get("ai_shadow_provider", "") or "").strip()
    ai_shadow_model = str(item.get("ai_shadow_model", "") or "").strip()
    ai_shadow_direction = str(item.get("ai_shadow_direction", "") or "").strip().lower()
    ai_shadow_category = str(item.get("ai_shadow_category", "") or "").strip()
    ai_shadow_confidence = _as_float(item.get("ai_shadow_confidence"))
    ai_shadow_path_a_eligible = bool(item.get("ai_shadow_path_a_eligible", False))
    ai_shadow_reason = str(item.get("ai_shadow_reason", "") or "").strip()
    ai_shadow_headline_basis = str(item.get("ai_shadow_headline_basis", "") or "").strip()
    ai_shadow_positive_phrases = [
        str(phrase).strip()
        for phrase in (item.get("ai_shadow_positive_phrases", []) or [])
        if str(phrase).strip()
    ]

    if not catalyst and not headline and article_count <= 0:
        if news_fetch_status == "error":
            empty_reason = reason or "News provider request failed; Mai Tai will retry shortly."
        elif news_fetch_status == "disabled":
            empty_reason = reason or "News provider is unavailable, so Path A cannot evaluate this symbol."
        elif catalyst_status == "no_articles":
            empty_reason = reason or "No company-specific news has been returned yet in the current catalyst window."
        else:
            empty_reason = reason or "No qualifying news since last market close"
        return f'<span style="color:#8da2b7">{escape(empty_reason)}</span>'

    sent_color = {"bullish": "#00c853", "bearish": "#ff1744", "neutral": "#ffd600"}.get(sentiment, "#8e9bb3")
    sent_bg = {"bullish": "#0a2e0a", "bearish": "#2e0a0a", "neutral": "#2e2e0a"}.get(sentiment, "#162033")
    sent_label = {"bullish": "BULL", "bearish": "BEAR", "neutral": "NEUTRAL"}.get(sentiment, "NEWS")

    if is_generic_roundup:
        sent_color = "#8e9bb3"
        sent_bg = "#1a2435"
        sent_label = "NEWS"
    elif not has_real_catalyst:
        sent_color = "#90a4ae"
        sent_bg = "#18222f"
        sent_label = "INFO"

    display_headline = headline
    if display_headline.startswith("["):
        bracket_end = display_headline.find("]")
        if bracket_end > 0:
            display_headline = display_headline[bracket_end + 1 :].strip()

    if not display_headline:
        display_headline = reason or "Recent catalyst window"

    headline_html = escape(display_headline)
    if news_url:
        headline_html = (
            f'<a href="{escape(news_url)}" target="_blank" rel="noreferrer" '
            f'style="color:#4fc3f7;text-decoration:none;">{headline_html}</a>'
        )

    meta_bits: list[str] = []
    if catalyst:
        meta_bits.append(catalyst)
    if confidence > 0:
        meta_bits.append(f"{confidence:.0%} conf")
    if article_count > 0:
        meta_bits.append(f"{article_count} art")
    if real_article_count > 0 and real_article_count != article_count:
        meta_bits.append(f"{real_article_count} real")
    if freshness_minutes is not None:
        meta_bits.append(f"{int(freshness_minutes)}m old")
    if news_date:
        meta_bits.append(news_date)
    if path_a_eligible:
        meta_bits.append("PATH A ready")
    elif news_fetch_status == "error":
        meta_bits.append("provider retry")
    elif news_fetch_status == "disabled":
        meta_bits.append("provider unavailable")
    elif is_generic_roundup:
        meta_bits.append("roundup only")
    elif catalyst_status == "no_articles":
        meta_bits.append("no articles yet")
    elif article_count > 0 and not has_real_catalyst:
        meta_bits.append("informational")

    meta_html = " · ".join(escape(bit) for bit in meta_bits)
    footer_text = reason or (f"Window: {news_window_start} onward" if news_window_start else "")
    footer_html = (
        f'<div style="color:#8da2b7;font-size:10px;line-height:1.35;margin-top:3px;">{escape(footer_text)}</div>'
        if footer_text
        else ""
    )
    return (
        f'<span style="display:block;background:{sent_bg};border-left:3px solid {sent_color};padding:4px 0 4px 8px;'
        f'border-radius:4px;">'
        f'<div>{sent_label} <strong style="color:{sent_color};">{escape(catalyst or "NEWS")}</strong></div>'
        f'<div style="color:#cfe1ff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{headline_html}</div>'
        f'<div style="color:#8da2b7;font-size:10px;line-height:1.3;">{meta_html}</div>'
        f"{footer_html}</span>"
    )


def _render_confirmed_catalyst_cell(item: dict[str, Any]) -> str:
    catalyst = str(item.get("catalyst_type") or item.get("catalyst") or "").strip()
    headline = str(item.get("headline", "") or "").strip()
    news_url = str(item.get("news_url", "") or "").strip()
    news_date = str(item.get("news_date", "") or "").strip()
    sentiment = str(item.get("direction") or item.get("sentiment") or "").strip().lower()
    news_fetch_status = str(item.get("news_fetch_status", "") or "").strip().lower()
    catalyst_status = str(item.get("catalyst_status", "") or "").strip().lower()
    confidence = _as_float(item.get("catalyst_confidence"))
    article_count = int(item.get("article_count", 0) or 0)
    real_article_count = int(item.get("real_catalyst_article_count", 0) or 0)
    freshness_minutes = item.get("freshness_minutes")
    is_generic_roundup = bool(item.get("is_generic_roundup", False))
    has_real_catalyst = bool(item.get("has_real_catalyst", False))
    reason = str(item.get("catalyst_reason", "") or "").strip()
    news_window_start = str(item.get("news_window_start", "") or "").strip()
    path_a_eligible = bool(item.get("path_a_eligible", False))
    ai_shadow_status = str(item.get("ai_shadow_status", "") or "").strip().lower()
    ai_shadow_provider = str(item.get("ai_shadow_provider", "") or "").strip()
    ai_shadow_model = str(item.get("ai_shadow_model", "") or "").strip()
    ai_shadow_direction = str(item.get("ai_shadow_direction", "") or "").strip().lower()
    ai_shadow_category = str(item.get("ai_shadow_category", "") or "").strip()
    ai_shadow_confidence = _as_float(item.get("ai_shadow_confidence"))
    ai_shadow_path_a_eligible = bool(item.get("ai_shadow_path_a_eligible", False))
    ai_shadow_reason = str(item.get("ai_shadow_reason", "") or "").strip()
    ai_shadow_headline_basis = str(item.get("ai_shadow_headline_basis", "") or "").strip()
    ai_shadow_positive_phrases = [
        str(phrase).strip()
        for phrase in (item.get("ai_shadow_positive_phrases", []) or [])
        if str(phrase).strip()
    ]

    if not catalyst and not headline and article_count <= 0:
        if news_fetch_status == "error":
            empty_reason = reason or "News provider request failed; Mai Tai will retry shortly."
        elif news_fetch_status == "disabled":
            empty_reason = reason or "News provider is unavailable, so Path A cannot evaluate this symbol."
        elif catalyst_status == "no_articles":
            empty_reason = reason or "No company-specific news has been returned yet in the current catalyst window."
        else:
            empty_reason = reason or "No qualifying news since last market close"
        return f'<span style="color:#8da2b7">{escape(empty_reason)}</span>'

    sent_color = {"bullish": "#00c853", "bearish": "#ff1744", "neutral": "#ffd600"}.get(sentiment, "#8e9bb3")
    sent_bg = {"bullish": "#0a2e0a", "bearish": "#2e0a0a", "neutral": "#2e2e0a"}.get(sentiment, "#162033")
    sent_label = {"bullish": "BULL", "bearish": "BEAR", "neutral": "NEUTRAL"}.get(sentiment, "NEWS")

    if is_generic_roundup:
        sent_color = "#8e9bb3"
        sent_bg = "#1a2435"
        sent_label = "NEWS"
    elif not has_real_catalyst:
        sent_color = "#90a4ae"
        sent_bg = "#18222f"
        sent_label = "INFO"

    display_headline = headline
    if display_headline.startswith("["):
        bracket_end = display_headline.find("]")
        if bracket_end > 0:
            display_headline = display_headline[bracket_end + 1 :].strip()

    if not display_headline:
        display_headline = reason or "Recent catalyst window"

    headline_html = escape(display_headline)
    if news_url:
        headline_html = (
            f'<a href="{escape(news_url)}" target="_blank" rel="noreferrer" '
            f'style="color:#4fc3f7;text-decoration:none;">{headline_html}</a>'
        )

    meta_bits: list[str] = []
    if catalyst:
        meta_bits.append(catalyst)
    if confidence > 0:
        meta_bits.append(f"{confidence:.0%} conf")
    if article_count > 0:
        meta_bits.append(f"{article_count} art")
    if real_article_count > 0 and real_article_count != article_count:
        meta_bits.append(f"{real_article_count} real")
    if freshness_minutes is not None:
        meta_bits.append(f"{int(freshness_minutes)}m old")
    if news_date:
        meta_bits.append(news_date)
    if path_a_eligible:
        meta_bits.append("PATH A ready")
    elif news_fetch_status == "error":
        meta_bits.append("provider retry")
    elif news_fetch_status == "disabled":
        meta_bits.append("provider unavailable")
    elif is_generic_roundup:
        meta_bits.append("roundup only")
    elif catalyst_status == "no_articles":
        meta_bits.append("no articles yet")
    elif article_count > 0 and not has_real_catalyst:
        meta_bits.append("informational")

    meta_html = " &middot; ".join(escape(bit) for bit in meta_bits)
    footer_parts: list[str] = []
    footer_text = reason or (f"Window: {news_window_start} onward" if news_window_start else "")
    if footer_text:
        footer_parts.append(
            f'<div style="color:#8da2b7;font-size:10px;line-height:1.35;margin-top:3px;">{escape(footer_text)}</div>'
        )

    if ai_shadow_status and ai_shadow_status != "disabled":
        ai_bits: list[str] = [f"AI shadow: {ai_shadow_direction or 'neutral'}"]
        if ai_shadow_category:
            ai_bits.append(ai_shadow_category)
        if ai_shadow_confidence > 0:
            ai_bits.append(f"{ai_shadow_confidence:.0%}")
        if ai_shadow_path_a_eligible:
            ai_bits.append("PATH A ready")
        if ai_shadow_provider:
            provider_label = ai_shadow_provider
            if ai_shadow_model:
                provider_label = f"{provider_label}/{ai_shadow_model}"
            ai_bits.append(provider_label)
        footer_parts.append(
            '<div style="color:#9ec1ff;font-size:10px;line-height:1.35;margin-top:3px;">'
            + " &middot; ".join(escape(bit) for bit in ai_bits)
            + "</div>"
        )
        ai_detail = ai_shadow_reason or ai_shadow_headline_basis
        if ai_shadow_positive_phrases:
            ai_detail = f"{ai_detail} Positive phrases: {', '.join(ai_shadow_positive_phrases[:3])}.".strip()
        if ai_detail:
            footer_parts.append(
                f'<div style="color:#7f98ba;font-size:10px;line-height:1.35;margin-top:2px;">{escape(ai_detail)}</div>'
            )
    elif ai_shadow_status == "disabled":
        footer_parts.append(
            '<div style="color:#64748b;font-size:10px;line-height:1.35;margin-top:3px;">AI shadow: disabled</div>'
        )

    footer_html = "".join(footer_parts)
    return (
        f'<span style="display:block;background:{sent_bg};border-left:3px solid {sent_color};padding:4px 0 4px 8px;'
        f'border-radius:4px;">'
        f'<div>{sent_label} <strong style="color:{sent_color};">{escape(catalyst or "NEWS")}</strong></div>'
        f'<div style="color:#cfe1ff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{headline_html}</div>'
        f'<div style="color:#8da2b7;font-size:10px;line-height:1.3;">{meta_html}</div>'
        f"{footer_html}</span>"
    )


def _render_confirmed_news_icon(item: dict[str, Any]) -> str:
    news_url = str(item.get("news_url", "") or "").strip()
    if not news_url:
        return '<span style="color:#61758a;">—</span>'
    return (
        f'<a href="{escape(news_url)}" target="_blank" rel="noreferrer" '
        'style="color:#00e5ff;text-decoration:none;font-size:14px;" title="Open news article">📰</a>'
    )


def _render_confirmed_blacklist_action(ticker: str, *, blacklisted_symbols: set[str]) -> str:
    if not ticker:
        return '<span style="color:#61758a;">—</span>'

    action = "remove" if ticker in blacklisted_symbols else "add"
    label = "Unblock" if action == "remove" else "Block"
    color = "#ffcc5b" if action == "remove" else "#ff6b6b"
    url = f'/scanner/blacklist/{action}?symbol={quote(ticker)}&redirect_to={quote("/scanner/dashboard", safe="/")}'
    return (
        f'<a href="{escape(url)}" '
        f'style="color:{color};font-size:11px;padding:2px 6px;border:1px solid {color};'
        'border-radius:3px;text-decoration:none;display:inline-block;" '
        f'title="{escape(label)} {escape(ticker)} from the scanner deck">{escape(label)}</a>'
    )


def _render_scanner_blacklist_entries(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return '<div class="line-item">No blocked symbols.</div>'

    rendered: list[str] = []
    for entry in entries[:20]:
        symbol = str(entry.get("symbol", "")).upper()
        reason = str(entry.get("reason", "") or "manual")
        remove_url = (
            f'/scanner/blacklist/remove?symbol={quote(symbol)}&redirect_to='
            f'{quote("/scanner/dashboard", safe="/")}'
        )
        rendered.append(
            f"""<div class="line-item">
            <strong>{escape(symbol)}</strong><br>
            <span style="font-size:11px;color:#98a6c8;">{escape(reason)}</span><br>
            <a href="{escape(remove_url)}" style="color:#ffcc5b;font-size:11px;text-decoration:none;">remove</a>
        </div>"""
        )
    return "".join(rendered)


def _render_scanner_stock_rows(rows: list[dict[str, Any]], live_symbols: set[str]) -> str:
    if not rows:
        return '<tr><td colspan="9" style="text-align:center;color:#888;padding:20px;">No stocks qualifying</td></tr>'
    rendered = []
    for index, item in enumerate(rows, start=1):
        ticker = str(item.get("ticker", "")).upper()
        live_badge = ' <span style="color:#00ff41;font-size:10px;">LIVE</span>' if ticker in live_symbols else ""
        rendered.append(
            f"""<tr>
            <td style="text-align:center">{index}</td>
            <td><strong>{escape(ticker)}</strong>{live_badge}</td>
            <td style="color:#00e5ff;font-size:12px;">{escape(str(item.get("first_seen", "")))}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("price")))}</td>
            <td style="text-align:right;color:{'#00c853' if _as_float(item.get('change_pct')) >= 0 else '#ff1744'}">{_as_float(item.get("change_pct")):+.1f}%</td>
            <td style="text-align:right">{_as_float(item.get("spread_pct")):.2f}%</td>
            <td style="text-align:right">{_short_volume(item.get("volume"))}</td>
            <td style="text-align:right">{_as_float(item.get("rvol")):.1f}x</td>
            <td style="text-align:right">{escape(_format_age(item.get("data_age_secs")))}</td>
        </tr>"""
        )
    return "".join(rendered)


def _render_alert_rows(alerts: list[dict[str, Any]]) -> str:
    if not alerts:
        return '<tr><td colspan="6" style="text-align:center;color:#888;padding:20px;">No alerts fired yet</td></tr>'
    rendered = []
    for alert in reversed(alerts[-50:]):
        details = alert.get("details", {})
        if isinstance(details, dict):
            detail_pairs = list(details.items())[:4]
            details_str = ", ".join(f"{key}={value}" for key, value in detail_pairs)
        else:
            details_str = str(details)
        alert_type = str(alert.get("type", "") or "")
        if "SPIKE" in alert_type:
            type_color = "#ffcc5b"
            type_bg = "rgba(255,204,91,0.12)"
        elif "SQUEEZE" in alert_type:
            type_color = "#59d7ff"
            type_bg = "rgba(89,215,255,0.12)"
        else:
            type_color = "#98a6c8"
            type_bg = "rgba(255,255,255,0.06)"
        rendered.append(
            f"""<tr>
            <td>{escape(str(alert.get("time", "")))}</td>
            <td><span style="display:inline-flex;padding:4px 8px;border-radius:999px;background:{type_bg};color:{type_color};font-weight:bold;">{escape(alert_type)}</span></td>
            <td><strong>{escape(str(alert.get("ticker", "")))}</strong></td>
            <td style="text-align:right">{_fmt_money(_as_float(alert.get("price")))}</td>
            <td style="text-align:right">{_short_volume(alert.get("volume"))}</td>
            <td>{escape(details_str)}</td>
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


def _datetime_str(value: datetime | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(EASTERN_TZ).strftime("%Y-%m-%d %I:%M:%S %p ET")


def _is_recent_eastern_label(value: str | None, *, max_age_seconds: int) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %I:%M:%S %p ET").replace(tzinfo=EASTERN_TZ)
    except ValueError:
        return False
    age = (utcnow().astimezone(EASTERN_TZ) - parsed).total_seconds()
    return 0 <= age <= max_age_seconds


def _is_recent_datetime(value: Any, *, max_age_seconds: int) -> bool:
    if not isinstance(value, datetime):
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    age = (utcnow() - value.astimezone(UTC)).total_seconds()
    return 0 <= age <= max_age_seconds


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
        return "-"
    return f"${value:.2f}"


def _fmt_qty(value: float) -> str:
    if abs(value) < 0.0001:
        return "-"
    if abs(value - round(value)) < 0.0001:
        return str(int(round(value)))
    return f"{value:.2f}"


def _format_signed_decimal(value: float) -> str:
    return f"{value:+.4f}" if abs(value) >= 0.01 else f"{value:+.5f}"


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _decimal_total(quantity: str, price: str) -> str:
    return f"{_as_float(quantity) * _as_float(price):,.2f}"
