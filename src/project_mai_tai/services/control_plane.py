from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
from io import StringIO
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from redis.asyncio import Redis
from sqlalchemy import case, desc, func, select, text
from sqlalchemy.orm import Session, sessionmaker
import uvicorn

from project_mai_tai.db.models import (
    AccountPosition,
    AiTradeReview,
    BrokerAccount,
    BrokerOrder,
    BrokerOrderEvent,
    DashboardSnapshot,
    Fill,
    ReconciliationFinding,
    ReconciliationRun,
    ScannerBlacklistEntry,
    Strategy,
    StrategyBarHistory,
    SystemIncident,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.events import (
    HeartbeatEvent,
    ManualStopUpdateEvent,
    ManualStopUpdatePayload,
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
from project_mai_tai.trade_episodes import collect_completed_trade_cycles
from project_mai_tai.trade_episodes import display_order_path
from project_mai_tai.trade_episodes import looks_like_broker_payload_text


SERVICE_NAME = "control-plane"
EASTERN_TZ = ZoneInfo("America/New_York")
SCHWAB_OAUTH_REDIRECT_URI = "https://hook.project-mai-tai.live/auth/callback"


def utcnow() -> datetime:
    return datetime.now(UTC)


def current_eastern_day_start_utc(now: datetime | None = None) -> datetime:
    return current_scanner_session_start_utc(now)


def current_eastern_day_end_utc(now: datetime | None = None) -> datetime:
    return current_eastern_day_start_utc(now) + timedelta(days=1)


def _parse_review_filter_date(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return parsed.replace(tzinfo=EASTERN_TZ).astimezone(UTC)


def _default_review_filter_dates(now: datetime | None = None) -> tuple[str, str, datetime, datetime]:
    reference = (now or utcnow()).astimezone(EASTERN_TZ)
    date_text = reference.strftime("%Y-%m-%d")
    start = current_eastern_day_start_utc(now)
    end = current_eastern_day_end_utc(now)
    return date_text, date_text, start, end


def _schwab_authorize_url(settings: Settings) -> str:
    client_id = (settings.schwab_client_id or "").strip()
    if not client_id:
        raise RuntimeError("Schwab client ID is not configured on the VPS")
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": SCHWAB_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": "api",
        }
    )
    return f"{settings.schwab_base_url.rstrip('/')}/v1/oauth/authorize?{query}"


def _exchange_schwab_authorization_code(settings: Settings, code: str) -> dict[str, Any]:
    client_id = (settings.schwab_client_id or "").strip()
    client_secret = (settings.schwab_client_secret or "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("Schwab OAuth credentials are not configured on the VPS")
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": SCHWAB_OAUTH_REDIRECT_URI,
        }
    ).encode("utf-8")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    request = Request(
        settings.schwab_token_url,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=settings.schwab_request_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Schwab auth-code exchange failed: {detail or exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Schwab auth-code exchange returned an invalid payload")
    access_token = str(payload.get("access_token", "")).strip()
    refresh_token = str(payload.get("refresh_token", "")).strip()
    if not access_token or not refresh_token:
        raise RuntimeError(f"Schwab auth-code exchange returned no tokens: {payload}")
    return payload


def _persist_schwab_token_store(settings: Settings, payload: dict[str, Any]) -> None:
    token_store_path = (settings.schwab_token_store_path or "").strip()
    if not token_store_path:
        raise RuntimeError("MAI_TAI_SCHWAB_TOKEN_STORE_PATH is not configured on the VPS")
    expires_in_raw = payload.get("expires_in")
    expires_in = int(expires_in_raw) if expires_in_raw not in {None, ""} else 1800
    document = {
        "access_token": str(payload.get("access_token", "")).strip(),
        "refresh_token": str(payload.get("refresh_token", "")).strip(),
        "expires_at": (datetime.now(UTC) + timedelta(seconds=max(expires_in - 30, 0))).isoformat(),
        "token_type": payload.get("token_type"),
        "scope": payload.get("scope"),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    path = Path(token_store_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def _within_current_eastern_day(timestamp: datetime | None, now: datetime | None = None) -> bool:
    if timestamp is None:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    day_start = current_eastern_day_start_utc(now)
    day_end = current_eastern_day_end_utc(now)
    timestamp_utc = timestamp.astimezone(UTC)
    return day_start <= timestamp_utc < day_end


async def _publish_manual_stop_update(
    redis: Redis,
    settings: Settings,
    *,
    scope: str,
    action: str,
    symbol: str,
    strategy_code: str | None = None,
) -> None:
    if not hasattr(redis, "xadd"):
        return
    stream = stream_name(settings.redis_stream_prefix, "runtime-controls")
    event = ManualStopUpdateEvent(
        source_service=SERVICE_NAME,
        payload=ManualStopUpdatePayload(
            scope=scope, action=action, symbol=str(symbol).upper(), strategy_code=strategy_code
        ),
    )
    await redis.xadd(stream, {"data": event.model_dump_json()})


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
        self._overview_cache: dict[str, Any] | None = None
        self._overview_cache_at: datetime | None = None
        self._overview_cache_lock = asyncio.Lock()

    @staticmethod
    def _snapshot_matches_current_scanner_session(
        snapshot: DashboardSnapshot | None,
        *,
        session_start: datetime,
        require_session_marker: bool = False,
    ) -> bool:
        if snapshot is None or snapshot.created_at is None:
            return False
        payload = snapshot.payload if isinstance(snapshot.payload, dict) else {}
        marker_raw = payload.get("scanner_session_start_utc")
        if isinstance(marker_raw, str) and marker_raw.strip():
            try:
                marker_dt = datetime.fromisoformat(marker_raw)
            except ValueError:
                return False
            if marker_dt.tzinfo is None:
                marker_dt = marker_dt.replace(tzinfo=UTC)
            return marker_dt.astimezone(UTC) == session_start
        if require_session_marker:
            return False
        return snapshot.created_at.astimezone(UTC) >= session_start

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

    def _serialize_trade_coach_review(self, review: AiTradeReview) -> dict[str, Any]:
        payload = review.payload if isinstance(review.payload, dict) else {}
        trade_snapshot = payload.get("trade_snapshot", {})
        if not isinstance(trade_snapshot, dict):
            trade_snapshot = {}
        return {
            "strategy_code": review.strategy_code,
            "broker_account_name": review.broker_account_name,
            "symbol": review.symbol,
            "review_type": review.review_type,
            "cycle_key": review.cycle_key,
            "verdict": review.verdict,
            "action": review.action,
            "confidence": _decimal_str(review.confidence),
            "summary": review.summary,
            "schema_version": str(payload.get("schema_version", "") or ""),
            "coaching_focus": str(payload.get("coaching_focus", "") or ""),
            "execution_timing": str(payload.get("execution_timing", "") or ""),
            "setup_quality": _as_float(payload.get("setup_quality")),
            "execution_quality": _as_float(payload.get("execution_quality")),
            "outcome_quality": _as_float(payload.get("outcome_quality")),
            "should_have_traded": bool(payload.get("should_have_traded", False)),
            "should_review_manually": bool(payload.get("should_review_manually", False)),
            "key_reasons": list(payload.get("key_reasons", []) or []),
            "rule_hits": list(payload.get("rule_hits", []) or []),
            "rule_violations": list(payload.get("rule_violations", []) or []),
            "next_time": list(payload.get("next_time", []) or []),
            "path": str(trade_snapshot.get("path", "") or ""),
            "entry_time": str(trade_snapshot.get("entry_time", "") or ""),
            "exit_time": str(trade_snapshot.get("exit_time", "") or ""),
            "entry_price": str(trade_snapshot.get("entry_price", "") or ""),
            "exit_price": str(trade_snapshot.get("exit_price", "") or ""),
            "pnl": _as_float(trade_snapshot.get("pnl")),
            "pnl_pct": _as_float(trade_snapshot.get("pnl_pct")),
            "exit_summary": str(trade_snapshot.get("exit_summary", "") or ""),
            "created_at": _datetime_str(review.created_at),
        }

    def load_trade_coach_review_history(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        reviews: list[dict[str, Any]] = []
        with self.session_factory() as session:
            query = select(AiTradeReview).order_by(desc(AiTradeReview.created_at))
            if start is not None:
                query = query.where(AiTradeReview.created_at >= start)
            if end is not None:
                query = query.where(AiTradeReview.created_at < end)
            query = query.limit(limit)
            for review in session.scalars(query).all():
                serialized = self._serialize_trade_coach_review(review)
                if self._is_ui_hidden_symbol(
                    serialized.get("broker_account_name"),
                    serialized.get("symbol"),
                ):
                    continue
                reviews.append(serialized)
        return reviews

    def load_trade_coach_regime_profiles(
        self,
        reviews: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        grouped_reviews: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
        for review in reviews:
            cycle_key = str(review.get("cycle_key", "") or "").strip()
            strategy_code = str(review.get("strategy_code", "") or "").strip()
            symbol = str(review.get("symbol", "") or "").strip().upper()
            if not cycle_key or not strategy_code or not symbol:
                continue
            entry_time = _parse_et_timestamp(str(review.get("entry_time", "") or ""))
            exit_time = _parse_et_timestamp(str(review.get("exit_time", "") or ""))
            if entry_time.year <= 1 or exit_time.year <= 1:
                continue
            interval_secs = _trade_coach_review_interval_secs(review)
            grouped_reviews.setdefault((strategy_code, symbol, interval_secs), []).append(
                {
                    "cycle_key": cycle_key,
                    "entry_time": entry_time.astimezone(UTC),
                    "exit_time": exit_time.astimezone(UTC),
                    "review": review,
                }
            )

        profiles: dict[str, dict[str, Any]] = {}
        if not grouped_reviews:
            return profiles

        with self.session_factory() as session:
            for (strategy_code, symbol, interval_secs), windows in grouped_reviews.items():
                query_start = min(item["entry_time"] for item in windows) - timedelta(minutes=5)
                query_end = max(item["exit_time"] for item in windows) + timedelta(seconds=max(interval_secs, 30))
                bars = list(
                    session.scalars(
                        select(StrategyBarHistory)
                        .where(StrategyBarHistory.strategy_code == strategy_code)
                        .where(StrategyBarHistory.symbol == symbol)
                        .where(StrategyBarHistory.interval_secs == interval_secs)
                        .where(StrategyBarHistory.bar_time >= query_start)
                        .where(StrategyBarHistory.bar_time <= query_end)
                        .order_by(StrategyBarHistory.bar_time.asc())
                    ).all()
                )
                if not bars:
                    continue
                for window in windows:
                    profile = _build_trade_coach_regime_profile(
                        window["review"],
                        bars,
                        entry_time=window["entry_time"],
                        exit_time=window["exit_time"],
                        interval_secs=interval_secs,
                    )
                    if profile:
                        profiles[str(window["cycle_key"])] = profile

        return profiles

    def load_live_trade_coach_regime_profiles(
        self,
        *,
        strategy_code: str,
        symbols: list[str],
        interval_secs: int,
        bars_per_symbol: int = 14,
    ) -> dict[str, dict[str, Any]]:
        normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
        if not normalized_symbols:
            return {}

        profiles: dict[str, dict[str, Any]] = {}
        with self.session_factory() as session:
            for symbol in normalized_symbols:
                bars_desc = list(
                    session.scalars(
                        select(StrategyBarHistory)
                        .where(StrategyBarHistory.strategy_code == strategy_code)
                        .where(StrategyBarHistory.symbol == symbol)
                        .where(StrategyBarHistory.interval_secs == interval_secs)
                        .order_by(desc(StrategyBarHistory.bar_time))
                        .limit(max(4, bars_per_symbol))
                    ).all()
                )
                if not bars_desc:
                    continue
                profile = _build_live_trade_coach_regime_profile(
                    symbol=symbol,
                    bars=list(reversed(bars_desc)),
                    interval_secs=interval_secs,
                )
                if profile:
                    profiles[symbol] = profile
        return profiles

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

    def set_bot_manual_stop_symbol(self, strategy_code: str, symbol: str) -> bool:
        normalized_code = str(strategy_code).strip()
        normalized_symbol = symbol.strip().upper()
        if not normalized_code or not normalized_symbol:
            return False
        session_marker = current_scanner_session_start_utc().isoformat()
        session_start = current_scanner_session_start_utc()

        with self.session_factory() as session:
            snapshot = session.scalar(
                select(DashboardSnapshot)
                .where(DashboardSnapshot.snapshot_type == "bot_manual_stop_symbols")
                .order_by(desc(DashboardSnapshot.created_at))
            )
            payload = (
                dict(snapshot.payload)
                if self._snapshot_matches_current_scanner_session(
                    snapshot,
                    session_start=session_start,
                    require_session_marker=True,
                )
                and isinstance(snapshot.payload, dict)
                else {}
            )
            bots_payload = payload.get("bots", {})
            if not isinstance(bots_payload, dict):
                bots_payload = {}
            symbols = {
                str(item).upper()
                for item in bots_payload.get(normalized_code, [])
                if str(item).strip()
            }
            symbols.add(normalized_symbol)
            bots_payload[normalized_code] = sorted(symbols)
            payload["bots"] = bots_payload
            payload["scanner_session_start_utc"] = session_marker
            session.execute(
                text("DELETE FROM dashboard_snapshots WHERE snapshot_type = 'bot_manual_stop_symbols'")
            )
            session.add(
                DashboardSnapshot(
                    snapshot_type="bot_manual_stop_symbols",
                    payload=payload,
                )
            )
            session.commit()
        return True

    def set_global_manual_stop_symbol(self, symbol: str) -> bool:
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            return False
        session_marker = current_scanner_session_start_utc().isoformat()
        session_start = current_scanner_session_start_utc()

        with self.session_factory() as session:
            snapshot = session.scalar(
                select(DashboardSnapshot)
                .where(DashboardSnapshot.snapshot_type == "global_manual_stop_symbols")
                .order_by(desc(DashboardSnapshot.created_at))
            )
            payload = (
                dict(snapshot.payload)
                if self._snapshot_matches_current_scanner_session(
                    snapshot,
                    session_start=session_start,
                    require_session_marker=True,
                )
                and isinstance(snapshot.payload, dict)
                else {}
            )
            symbols = {
                str(item).upper() for item in payload.get("symbols", []) if str(item).strip()
            }
            symbols.add(normalized_symbol)
            payload["symbols"] = sorted(symbols)
            payload["scanner_session_start_utc"] = session_marker
            session.execute(
                text("DELETE FROM dashboard_snapshots WHERE snapshot_type = 'global_manual_stop_symbols'")
            )
            session.add(
                DashboardSnapshot(
                    snapshot_type="global_manual_stop_symbols",
                    payload=payload,
                )
            )
            session.commit()
        return True

    def remove_global_manual_stop_symbol(self, symbol: str) -> bool:
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            return False
        session_marker = current_scanner_session_start_utc().isoformat()
        session_start = current_scanner_session_start_utc()

        with self.session_factory() as session:
            snapshot = session.scalar(
                select(DashboardSnapshot)
                .where(DashboardSnapshot.snapshot_type == "global_manual_stop_symbols")
                .order_by(desc(DashboardSnapshot.created_at))
            )
            payload = (
                dict(snapshot.payload)
                if self._snapshot_matches_current_scanner_session(
                    snapshot,
                    session_start=session_start,
                    require_session_marker=True,
                )
                and isinstance(snapshot.payload, dict)
                else {}
            )
            symbols = {
                str(item).upper() for item in payload.get("symbols", []) if str(item).strip()
            }
            if normalized_symbol not in symbols:
                return False
            symbols.discard(normalized_symbol)
            payload["symbols"] = sorted(symbols)
            payload["scanner_session_start_utc"] = session_marker
            session.execute(
                text("DELETE FROM dashboard_snapshots WHERE snapshot_type = 'global_manual_stop_symbols'")
            )
            session.add(
                DashboardSnapshot(
                    snapshot_type="global_manual_stop_symbols",
                    payload=payload,
                )
            )
            session.commit()
        return True

    def remove_bot_manual_stop_symbol(self, strategy_code: str, symbol: str) -> bool:
        normalized_code = str(strategy_code).strip()
        normalized_symbol = symbol.strip().upper()
        if not normalized_code or not normalized_symbol:
            return False
        session_marker = current_scanner_session_start_utc().isoformat()
        session_start = current_scanner_session_start_utc()

        with self.session_factory() as session:
            snapshot = session.scalar(
                select(DashboardSnapshot)
                .where(DashboardSnapshot.snapshot_type == "bot_manual_stop_symbols")
                .order_by(desc(DashboardSnapshot.created_at))
            )
            payload = (
                dict(snapshot.payload)
                if self._snapshot_matches_current_scanner_session(
                    snapshot,
                    session_start=session_start,
                    require_session_marker=True,
                )
                and isinstance(snapshot.payload, dict)
                else {}
            )
            bots_payload = payload.get("bots", {})
            if not isinstance(bots_payload, dict):
                return False
            symbols = {
                str(item).upper()
                for item in bots_payload.get(normalized_code, [])
                if str(item).strip()
            }
            if normalized_symbol not in symbols:
                return False
            symbols.discard(normalized_symbol)
            if symbols:
                bots_payload[normalized_code] = sorted(symbols)
            else:
                bots_payload.pop(normalized_code, None)
            payload["bots"] = bots_payload
            payload["scanner_session_start_utc"] = session_marker
            session.execute(
                text("DELETE FROM dashboard_snapshots WHERE snapshot_type = 'bot_manual_stop_symbols'")
            )
            session.add(
                DashboardSnapshot(
                    snapshot_type="bot_manual_stop_symbols",
                    payload=payload,
                )
            )
            session.commit()
        return True

    async def load_dashboard_data(self) -> dict[str, Any]:
        cache_ttl_seconds = 2.0
        async with self._overview_cache_lock:
            cache_age = None
            if self._overview_cache_at is not None:
                cache_age = (utcnow() - self._overview_cache_at).total_seconds()
            if self._overview_cache is not None and cache_age is not None and cache_age < cache_ttl_seconds:
                return self._overview_cache

            data = await self._load_dashboard_data_uncached()
            self._overview_cache = data
            self._overview_cache_at = utcnow()
            return data

    async def invalidate_overview_cache(self) -> None:
        async with self._overview_cache_lock:
            self._overview_cache = None
            self._overview_cache_at = None

    async def _load_dashboard_data_uncached(self) -> dict[str, Any]:
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
            recent_trade_coach_reviews=db_state["recent_trade_coach_reviews"],
            recent_bar_decisions=db_state["recent_bar_decisions"],
            open_orders=db_state["open_orders"],
            persisted_snapshots=db_state["dashboard_snapshots"],
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
            "recent_trade_coach_reviews": db_state["recent_trade_coach_reviews"],
            "recent_bar_decisions": db_state["recent_bar_decisions"],
            "virtual_positions": db_state["virtual_positions"],
            "account_positions": db_state["account_positions"],
            "reconciliation": db_state["reconciliation"],
            "strategy_runtime": normalized_strategy_runtime,
            "legacy_shadow": legacy_shadow,
            "incidents": db_state["incidents"],
            "scanner_blacklist": db_state["scanner_blacklist"],
            "errors": db_state["errors"] + stream_state["errors"] + legacy_shadow["errors"],
        }

    async def load_bot_dashboard_data(self) -> dict[str, Any]:
        db_state = self._load_database_state(lightweight=True)
        stream_state = await self._load_stream_state()
        normalized_strategy_runtime = self._normalize_strategy_runtime(stream_state["strategy_runtime"])
        legacy_shadow = self._empty_legacy_shadow_data()
        bots = self._build_bot_views(
            strategy_runtime=normalized_strategy_runtime,
            legacy_shadow=legacy_shadow,
            recent_intents=db_state["recent_intents"],
            recent_orders=db_state["recent_orders"],
            recent_fills=db_state["recent_fills"],
            recent_trade_coach_reviews=db_state["recent_trade_coach_reviews"],
            recent_bar_decisions=db_state["recent_bar_decisions"],
            open_orders=db_state["open_orders"],
            persisted_snapshots=db_state["dashboard_snapshots"],
        )

        overall_status = "healthy"
        if db_state["errors"] or stream_state["errors"]:
            overall_status = "degraded"
        elif any(
            service.get("effective_status", service["status"]) not in {"healthy", "starting"}
            for service in stream_state["services"]
        ):
            overall_status = "degraded"

        return {
            "generated_at": _datetime_str(utcnow()),
            "status": overall_status,
            "environment": self.settings.environment,
            "provider": self.settings.broker_provider_label,
            "oms_adapter": self.settings.oms_adapter_label,
            "services": stream_state["services"],
            "market_data": stream_state["market_data"],
            "bots": bots,
            "recent_intents": db_state["recent_intents"],
            "recent_orders": db_state["recent_orders"],
            "recent_fills": db_state["recent_fills"],
            "recent_trade_coach_reviews": db_state["recent_trade_coach_reviews"],
            "recent_bar_decisions": db_state["recent_bar_decisions"],
            "virtual_positions": db_state["virtual_positions"],
            "account_positions": db_state["account_positions"],
            "strategy_runtime": normalized_strategy_runtime,
            "legacy_shadow": legacy_shadow,
            "errors": db_state["errors"] + stream_state["errors"],
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
        global_manual_stop_snapshot = (
            persisted_snapshots.get("global_manual_stop_symbols", {})
            if isinstance(persisted_snapshots, dict)
            else {}
        )
        global_manual_stop_symbols = {
            str(symbol).upper()
            for symbol in (
                global_manual_stop_snapshot.get("symbols", [])
                if isinstance(global_manual_stop_snapshot, dict)
                else []
            )
            if str(symbol).strip()
        }
        bot_states = strategy_runtime.get("bots", {})
        live_market_rows = self._build_live_market_lookup(strategy_runtime)
        watchlist = [
            str(symbol)
            for symbol in strategy_runtime.get("watchlist", [])
            if str(symbol).upper() not in blacklisted_symbols
            and str(symbol).upper() not in global_manual_stop_symbols
        ]
        top_confirmed_tickers = {
            str(item.get("ticker", "")).upper()
            for item in strategy_runtime.get("top_confirmed", [])
            if isinstance(item, dict)
            and str(item.get("ticker", "")).upper() not in blacklisted_symbols
            and str(item.get("ticker", "")).upper() not in global_manual_stop_symbols
        }
        all_confirmed = [
            self._normalize_confirmed_row(
                index=index,
                item=item,
                bot_states=bot_states,
                live_market_row=live_market_rows.get(str(item.get("ticker", "")).upper()),
                top_confirmed_tickers=top_confirmed_tickers,
            )
            for index, item in enumerate(strategy_runtime.get("all_confirmed", []), start=1)
            if str(item.get("ticker", "")).upper() not in blacklisted_symbols
            and str(item.get("ticker", "")).upper() not in global_manual_stop_symbols
        ]
        top_confirmed = [
            self._normalize_confirmed_row(
                index=index,
                item=item,
                bot_states=bot_states,
                live_market_row=live_market_rows.get(str(item.get("ticker", "")).upper()),
                top_confirmed_tickers=top_confirmed_tickers,
            )
            for index, item in enumerate(strategy_runtime.get("top_confirmed", []), start=1)
            if str(item.get("ticker", "")).upper() not in blacklisted_symbols
            and str(item.get("ticker", "")).upper() not in global_manual_stop_symbols
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
            restored_session_start = str(snapshot_payload.get("scanner_session_start_utc", "") or "")
            restored_at_is_current = False
            restored_session_is_current = False
            if restored_session_start:
                try:
                    restored_session_dt = datetime.fromisoformat(restored_session_start)
                except ValueError:
                    restored_session_dt = None
                if restored_session_dt is not None:
                    if restored_session_dt.tzinfo is None:
                        restored_session_dt = restored_session_dt.replace(tzinfo=UTC)
                    restored_session_is_current = (
                        restored_session_dt.astimezone(UTC) == current_scanner_session_start_utc()
                    )
            if restored_at and restored_session_is_current:
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
                restored_top_confirmed_tickers = {
                    str(item.get("ticker", "")).upper()
                    for item in restored_top_rows
                    if isinstance(item, dict)
                    and str(item.get("ticker", "")).upper() not in blacklisted_symbols
                    and str(item.get("ticker", "")).upper() not in global_manual_stop_symbols
                }
                all_confirmed = [
                    self._normalize_confirmed_row(
                        index=index,
                        item=item,
                        bot_states=bot_states,
                        live_market_row=live_market_rows.get(str(item.get("ticker", "")).upper()),
                        top_confirmed_tickers=restored_top_confirmed_tickers,
                    )
                    for index, item in enumerate(restored_rows, start=1)
                    if isinstance(item, dict)
                    and str(item.get("ticker", "")).upper() not in blacklisted_symbols
                    and str(item.get("ticker", "")).upper() not in global_manual_stop_symbols
                ]
            if isinstance(restored_top_rows, list) and restored_top_rows and restored_at_is_current:
                top_confirmed = [
                    self._normalize_confirmed_row(
                        index=index,
                        item=item,
                        bot_states=bot_states,
                        live_market_row=live_market_rows.get(str(item.get("ticker", "")).upper()),
                        top_confirmed_tickers=restored_top_confirmed_tickers,
                    )
                    for index, item in enumerate(restored_top_rows, start=1)
                    if isinstance(item, dict)
                    and str(item.get("ticker", "")).upper() not in blacklisted_symbols
                    and str(item.get("ticker", "")).upper() not in global_manual_stop_symbols
                ]
                watchlist = [
                    str(symbol)
                    for symbol in restored_watchlist
                    if str(symbol).upper() not in blacklisted_symbols
                    and str(symbol).upper() not in global_manual_stop_symbols
                ]
                if top_confirmed:
                    top_confirmed_source = "restored"
                    top_confirmed_snapshot_at = restored_at

        all_confirmed_by_ticker = {
            str(item.get("ticker", "")).upper(): item
            for item in all_confirmed
            if str(item.get("ticker", "")).strip()
        }
        bot_handoff: list[dict[str, Any]] = []
        for handoff_rank, symbol in enumerate(watchlist, start=1):
            ticker = str(symbol).upper()
            item = dict(all_confirmed_by_ticker.get(ticker, {}))
            watched_by = [
                strategy_code
                for strategy_code, bot in bot_states.items()
                if ticker and ticker in {str(candidate).upper() for candidate in bot.get("watchlist", [])}
            ]
            item.setdefault("ticker", ticker)
            item.setdefault("confirmation_path", "")
            item.setdefault("rank_score", 0.0)
            item.setdefault("confirmed_at", "")
            item.setdefault("price", 0.0)
            item.setdefault("change_pct", 0.0)
            item.setdefault("volume", 0.0)
            item.setdefault("rvol", 0.0)
            item["handoff_rank"] = handoff_rank
            item["watched_by"] = watched_by
            item["is_handed_to_bot"] = True
            item["is_top5"] = ticker in {str(row.get("ticker", "")).upper() for row in top_confirmed}
            bot_handoff.append(item)

        legacy_confirmed = [
            str(symbol).upper()
            for symbol in legacy_shadow.get("scanner", {}).get("confirmed_symbols", [])
        ]
        alert_snapshot = (
            persisted_snapshots.get("scanner_alert_engine_state", {})
            if isinstance(persisted_snapshots, dict)
            else {}
        )
        today_alerts = [
            item
            for item in (
                alert_snapshot.get("today_alerts", [])
                if isinstance(alert_snapshot, dict)
                else []
            )
            if isinstance(item, dict)
            and str(item.get("ticker", "")).upper() not in blacklisted_symbols
            and str(item.get("ticker", "")).upper() not in global_manual_stop_symbols
        ]
        alert_diagnostics = [
            item
            for item in (
                alert_snapshot.get("recent_rejections", [])
                if isinstance(alert_snapshot, dict)
                else []
            )
            if isinstance(item, dict)
            and str(item.get("ticker", "")).upper() not in blacklisted_symbols
            and str(item.get("ticker", "")).upper() not in global_manual_stop_symbols
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
            "bot_handoff_count": len(bot_handoff),
            "bot_handoff": bot_handoff,
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
            "today_alerts": today_alerts,
            "today_alerts_count": len(today_alerts),
            "alert_diagnostics": alert_diagnostics,
            "alert_diagnostics_count": len(alert_diagnostics),
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
            "retention_states": list(strategy_runtime.get("retention_states", [])),
            "legacy_confirmed_symbols": legacy_confirmed,
            "legacy_confirmed_count": len(legacy_confirmed),
            "blacklist": blacklist_entries,
            "blacklist_symbols": sorted(blacklisted_symbols),
            "blacklist_count": len(blacklist_entries),
            "global_manual_stop_symbols": sorted(global_manual_stop_symbols),
            "global_manual_stop_count": len(global_manual_stop_symbols),
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
        top_confirmed_tickers: set[str] | None = None,
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
            "is_top5": ticker in (top_confirmed_tickers or set()),
            "is_handed_to_bot": bool(watched_by),
        }

    def _build_bot_views(
        self,
        *,
        strategy_runtime: dict[str, Any],
        legacy_shadow: dict[str, Any],
        recent_intents: list[dict[str, Any]],
        recent_orders: list[dict[str, Any]],
        recent_fills: list[dict[str, Any]],
        recent_trade_coach_reviews: list[dict[str, Any]],
        recent_bar_decisions: list[dict[str, Any]],
        open_orders: list[dict[str, Any]],
        persisted_snapshots: dict[str, Any],
    ) -> list[dict[str, Any]]:
        registrations = configured_strategy_registrations(self.settings)
        ordered_codes = [registration.code for registration in registrations]
        registration_map = {registration.code: registration for registration in registrations}
        runtime_bots = strategy_runtime.get("bots", {})
        legacy_bots = legacy_shadow.get("bots", {})
        manual_stop_snapshot = (
            persisted_snapshots.get("bot_manual_stop_symbols", {})
            if isinstance(persisted_snapshots, dict)
            else {}
        )
        manual_stop_payload = (
            manual_stop_snapshot.get("bots", {})
            if isinstance(manual_stop_snapshot, dict)
            else {}
        )
        if not isinstance(manual_stop_payload, dict):
            manual_stop_payload = {}

        bot_views: list[dict[str, Any]] = []
        for code in ordered_codes:
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
            manual_stop_symbols = sorted(
                {
                    str(symbol).upper()
                    for symbol in (
                        runtime_bot.get("manual_stop_symbols")
                        or manual_stop_payload.get(code, [])
                        or []
                    )
                    if str(symbol).strip()
                    and not self._is_ui_hidden_symbol(account_name, symbol)
                }
            )
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
            live_decision_symbols = {
                str(symbol).upper()
                for symbol in watchlist + pending_open + pending_close
                if str(symbol).strip()
            }
            live_decision_symbols.update(
                str(item).split(":", 1)[0].upper()
                for item in pending_scale
                if str(item).strip()
            )
            live_decision_symbols.update(
                str(item.get("ticker") or item.get("symbol") or "").upper()
                for item in positions
                if str(item.get("ticker") or item.get("symbol") or "").strip()
            )
            recent_decisions = [
                self._decision_display_row(item)
                for item in list(runtime_bot.get("recent_decisions", []))
                if not self._is_ui_hidden_symbol(account_name, item.get("ticker") or item.get("symbol"))
                and str(item.get("ticker") or item.get("symbol") or "").upper() in live_decision_symbols
            ]
            if not recent_decisions:
                recent_decisions = [
                    self._decision_display_row(item)
                    for item in recent_bar_decisions
                    if item.get("strategy_code") == code
                    and not self._is_ui_hidden_symbol(account_name, item.get("symbol"))
                    and str(item.get("symbol") or "").upper() in live_decision_symbols
                ]
            recent_decisions = _dedupe_decision_events(recent_decisions)
            indicator_snapshots = [
                item
                for item in list(runtime_bot.get("indicator_snapshots", []))
                if not self._is_ui_hidden_symbol(account_name, item.get("ticker") or item.get("symbol"))
            ]
            bar_counts = {
                str(symbol).upper(): int(count or 0)
                for symbol, count in dict(runtime_bot.get("bar_counts", {}) or {}).items()
                if not self._is_ui_hidden_symbol(account_name, symbol)
            }
            last_tick_at = {
                str(symbol).upper(): str(observed_at or "")
                for symbol, observed_at in dict(runtime_bot.get("last_tick_at", {}) or {}).items()
                if not self._is_ui_hidden_symbol(account_name, symbol)
            }
            retention_states = [
                item
                for item in list(runtime_bot.get("retention_states", []))
                if not self._is_ui_hidden_symbol(account_name, item.get("ticker") or item.get("symbol"))
            ]
            data_health = dict(runtime_bot.get("data_health", {}) or {})
            halted_symbols = [
                str(symbol).upper()
                for symbol in list(data_health.get("halted_symbols", []) or [])
                if str(symbol).strip() and not self._is_ui_hidden_symbol(account_name, symbol)
            ]
            warning_symbols = [
                str(symbol).upper()
                for symbol in list(data_health.get("warning_symbols", []) or [])
                if str(symbol).strip() and not self._is_ui_hidden_symbol(account_name, symbol)
            ]
            raw_reasons = dict(data_health.get("reasons", {}) or {})
            raw_warning_reasons = dict(data_health.get("warning_reasons", {}) or {})
            raw_since = dict(data_health.get("since", {}) or {})
            raw_warning_since = dict(data_health.get("warning_since", {}) or {})
            data_health = {
                **data_health,
                "status": str(data_health.get("status", "healthy") or "healthy"),
                "halted_symbols": halted_symbols,
                "warning_symbols": warning_symbols,
                "reasons": {
                    str(symbol).upper(): str(raw_reasons.get(symbol) or raw_reasons.get(str(symbol).upper()) or "")
                    for symbol in halted_symbols
                },
                "warning_reasons": {
                    str(symbol).upper(): str(
                        raw_warning_reasons.get(symbol)
                        or raw_warning_reasons.get(str(symbol).upper())
                        or ""
                    )
                    for symbol in warning_symbols
                },
                "since": {
                    str(symbol).upper(): str(raw_since.get(symbol) or raw_since.get(str(symbol).upper()) or "")
                    for symbol in halted_symbols
                },
                "warning_since": {
                    str(symbol).upper(): str(
                        raw_warning_since.get(symbol)
                        or raw_warning_since.get(str(symbol).upper())
                        or ""
                    )
                    for symbol in warning_symbols
                },
            }
            recent_decisions = self._live_decision_placeholder_rows(
                live_symbols=live_decision_symbols,
                recent_decisions=recent_decisions,
                bar_counts=bar_counts,
                last_tick_at=last_tick_at,
                data_health=data_health,
                provider=(
                    self.settings.provider_for_strategy(code)
                    if registration
                    else str(runtime_bot.get("provider", "") or "")
                ),
            ) + recent_decisions
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
                    "data_health": data_health,
                    "manual_stop_symbols": manual_stop_symbols,
                    "retention_states": retention_states,
                    "positions": positions,
                    "position_count": len(positions),
                    "pending_open_symbols": pending_open,
                    "pending_close_symbols": pending_close,
                    "pending_scale_levels": pending_scale,
                    "pending_count": len(pending_open) + len(pending_close) + len(pending_scale),
                    "daily_pnl": float(runtime_bot.get("daily_pnl", 0) or 0),
                    "closed_today": list(runtime_bot.get("closed_today", [])),
                    "recent_decisions": recent_decisions[:50],
                    "indicator_snapshots": indicator_snapshots,
                    "bar_counts": bar_counts,
                    "last_tick_at": last_tick_at,
                    "tos_parity": tos_parity,
                    "recent_intents": [
                        item
                        for item in recent_intents
                        if item.get("strategy_code") == code
                        and not self._is_ui_hidden_symbol(account_name, item.get("symbol"))
                    ][:50],
                    "recent_orders": [
                        item
                        for item in recent_orders
                        if item.get("strategy_code") == code
                        and not self._is_ui_hidden_symbol(account_name, item.get("symbol"))
                    ][:50],
                    "recent_fills": [
                        item
                        for item in recent_fills
                        if item.get("strategy_code") == code
                        and not self._is_ui_hidden_symbol(account_name, item.get("symbol"))
                    ][:100],
                    "recent_trade_coach_reviews": [
                        item
                        for item in recent_trade_coach_reviews
                        if item.get("strategy_code") == code
                        and item.get("broker_account_name") == account_name
                        and not self._is_ui_hidden_symbol(account_name, item.get("symbol"))
                    ][:25],
                }
            )
        return bot_views

    @staticmethod
    def _decision_display_row(item: dict[str, Any]) -> dict[str, Any]:
        row = dict(item)
        status = str(row.get("status", "")).lower()
        reason = str(row.get("reason", "")).lower()
        if status == "idle" and reason == "no entry path matched":
            row["status"] = "evaluated"
            row["reason"] = "entry evaluated; no setup matched this bar"
        return row

    def _live_decision_placeholder_rows(
        self,
        *,
        live_symbols: set[str],
        recent_decisions: list[dict[str, Any]],
        bar_counts: dict[str, int],
        last_tick_at: dict[str, str],
        data_health: dict[str, Any],
        provider: str,
    ) -> list[dict[str, Any]]:
        seen_symbols = {
            str(item.get("symbol") or item.get("ticker") or "").upper()
            for item in recent_decisions
            if str(item.get("symbol") or item.get("ticker") or "").strip()
        }
        halted_symbols = {
            str(symbol).upper()
            for symbol in list(data_health.get("halted_symbols", []) or [])
            if str(symbol).strip()
        }
        warning_symbols = {
            str(symbol).upper()
            for symbol in list(data_health.get("warning_symbols", []) or [])
            if str(symbol).strip()
        }
        halt_reasons = {
            str(symbol).upper(): str(reason or "")
            for symbol, reason in dict(data_health.get("reasons", {}) or {}).items()
            if str(symbol).strip()
        }
        warning_reasons = {
            str(symbol).upper(): str(reason or "")
            for symbol, reason in dict(data_health.get("warning_reasons", {}) or {}).items()
            if str(symbol).strip()
        }
        market_data_source = self._market_data_source_label(provider)

        placeholders: list[dict[str, Any]] = []
        for symbol in sorted(live_symbols):
            if symbol in seen_symbols:
                continue

            last_tick_label = str(last_tick_at.get(symbol, "") or "")
            bar_count = int(bar_counts.get(symbol, 0) or 0)
            status = "pending"
            if symbol in halted_symbols:
                status = "critical"
                reason = halt_reasons.get(symbol) or f"{market_data_source} market data halt active"
            elif symbol in warning_symbols:
                status = "warning"
                reason = warning_reasons.get(symbol) or f"{market_data_source} ticks are temporarily quiet on this flat symbol"
            elif last_tick_label and bar_count > 0:
                wait_age_seconds = _seconds_since_eastern_label(last_tick_label)
                if wait_age_seconds is not None and wait_age_seconds >= 90:
                    status = "critical"
                    reason = (
                        "live in bot; no completed 30s trade bar for "
                        f"{_format_age(wait_age_seconds)} after the last live "
                        f"{market_data_source} tick - verify tape/bar flow now"
                    )
                elif wait_age_seconds is not None and wait_age_seconds >= 45:
                    reason = (
                        "live in bot; still waiting "
                        f"{_format_age(wait_age_seconds)} for the next completed 30s "
                        f"trade bar after the last live {market_data_source} tick"
                    )
                elif wait_age_seconds is not None:
                    reason = (
                        "live in bot; waiting for next completed 30s trade bar to "
                        f"evaluate ({_format_age(wait_age_seconds)} since last "
                        f"{market_data_source} tick)"
                    )
                else:
                    reason = "live in bot; waiting for next completed 30s trade bar to evaluate"
            elif last_tick_label:
                reason = (
                    f"live in bot; receiving {market_data_source} ticks, "
                    "waiting for first completed 30s trade bar"
                )
            elif bar_count > 0:
                reason = (
                    f"live in bot; historical warmup loaded, waiting for fresh "
                    f"{market_data_source} ticks"
                )
            else:
                reason = f"live in bot; waiting for {market_data_source} market data"

            placeholders.append(
                {
                    "symbol": symbol,
                    "status": status,
                    "reason": reason,
                    "path": "",
                    "score": "",
                    "price": "",
                    "last_bar_at": last_tick_label,
                }
            )

        return placeholders

    @staticmethod
    def _market_data_source_label(provider: str) -> str:
        normalized = str(provider or "").strip().lower()
        if normalized == "schwab":
            return "Schwab"
        return "Polygon"

    def _build_tos_parity_view(
        self,
        *,
        strategy_code: str,
        indicator_snapshots: list[dict[str, Any]],
        watchlist: list[str],
    ) -> dict[str, Any]:
        enabled = strategy_code in {"macd_1m", "schwab_1m", "tos"}
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
        stream_state = await self._load_stream_state()
        database_error: str | None = None
        try:
            with self.session_factory() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:
            database_error = f"database:{exc}"

        errors = list(stream_state["errors"])
        if database_error:
            errors.append(database_error)

        overall_status = "healthy"
        if errors:
            overall_status = "degraded"
        elif any(
            service.get("effective_status", service.get("status")) not in {"healthy", "starting"}
            for service in stream_state["services"]
        ):
            overall_status = "degraded"

        return {
            "status": overall_status,
            "service": SERVICE_NAME,
            "timestamp": _datetime_str(utcnow()),
            "environment": self.settings.environment,
            "database_connected": database_error is None,
            "redis_connected": not any(error.startswith("redis:") for error in errors),
            "counts": {},
            "services": stream_state["services"],
            "errors": errors,
        }

    def _load_database_state(self, *, lightweight: bool = False) -> dict[str, Any]:
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
        recent_trade_coach_reviews: list[dict[str, Any]] = []
        recent_bar_decisions: list[dict[str, Any]] = []
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

                session_start = current_eastern_day_start_utc(now)
                session_end = current_eastern_day_end_utc(now)

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
                counts["recent_fills"] = int(
                    session.scalar(
                        select(func.count()).select_from(Fill).where(
                            Fill.filled_at >= session_start,
                            Fill.filled_at < session_end,
                        )
                    )
                    or 0
                )
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

                if not lightweight:
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
                    select(TradeIntent)
                    .where(TradeIntent.updated_at >= session_start, TradeIntent.updated_at < session_end)
                    .order_by(desc(TradeIntent.updated_at))
                    .limit(1000)
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

                latest_order_event_by_order: dict[Any, BrokerOrderEvent] = {}
                for entry in session.scalars(
                    select(BrokerOrderEvent)
                    .where(BrokerOrderEvent.event_at >= session_start, BrokerOrderEvent.event_at < session_end)
                    .order_by(desc(BrokerOrderEvent.event_at))
                    .limit(2000)
                ).all():
                    latest_order_event_by_order.setdefault(entry.order_id, entry)

                for order in session.scalars(
                    select(BrokerOrder)
                    .where(BrokerOrder.updated_at >= session_start, BrokerOrder.updated_at < session_end)
                    .order_by(desc(BrokerOrder.updated_at))
                    .limit(1000)
                ).all():
                    strategy = strategy_lookup.get(order.strategy_id)
                    account = account_lookup.get(order.broker_account_id)
                    intent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
                    latest_event = latest_order_event_by_order.get(order.id)
                    latest_event_payload = (
                        latest_event.payload
                        if latest_event is not None and isinstance(latest_event.payload, dict)
                        else {}
                    )
                    intent_payload = intent.payload if intent is not None and isinstance(intent.payload, dict) else {}
                    intent_metadata = (
                        intent_payload.get("metadata", {})
                        if isinstance(intent_payload.get("metadata", {}), dict)
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
                            "price": _decimal_str(latest_event_payload.get("fill_price")),
                            "status": order.status,
                            "reason": str(latest_event_payload.get("reason") or (intent.reason if intent else "")),
                            "path": str(intent_metadata.get("path") or ""),
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
                    select(BrokerOrder)
                    .where(
                        BrokerOrder.status.in_(["pending", "submitted", "accepted", "partially_filled"]),
                        BrokerOrder.updated_at >= session_start,
                        BrokerOrder.updated_at < session_end,
                    )
                    .order_by(desc(BrokerOrder.updated_at))
                    .limit(500)
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

                for fill in session.scalars(
                    select(Fill)
                    .where(Fill.filled_at >= session_start, Fill.filled_at < session_end)
                    .order_by(desc(Fill.filled_at))
                    .limit(1000)
                ).all():
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

                for review in session.scalars(
                    select(AiTradeReview)
                    .where(
                        AiTradeReview.created_at >= session_start,
                        AiTradeReview.created_at < session_end,
                    )
                    .order_by(desc(AiTradeReview.created_at))
                    .limit(250)
                ).all():
                    recent_trade_coach_reviews.append(self._serialize_trade_coach_review(review))

                for bar in session.scalars(
                    select(StrategyBarHistory)
                    .where(
                        StrategyBarHistory.bar_time >= session_start,
                        StrategyBarHistory.bar_time < session_end,
                        StrategyBarHistory.decision_status != "",
                    )
                    .order_by(desc(StrategyBarHistory.bar_time))
                    .limit(2000)
                ).all():
                    recent_bar_decisions.append(
                        {
                            "strategy_code": str(bar.strategy_code),
                            "symbol": str(bar.symbol or "").upper(),
                            "status": str(bar.decision_status or ""),
                            "reason": str(bar.decision_reason or ""),
                            "path": str(bar.decision_path or ""),
                            "score": str(bar.decision_score or ""),
                            "score_details": str(bar.decision_score_details or ""),
                            "price": _decimal_str(bar.close_price),
                            "last_bar_at": _datetime_str(bar.bar_time),
                        }
                    )
                if not recent_bar_decisions:
                    bar_rows = list(
                        session.execute(
                        text(
                            """
                            SELECT
                                strategy_code,
                                symbol,
                                decision_status,
                                decision_reason,
                                decision_path,
                                decision_score,
                                decision_score_details,
                                close_price,
                                bar_time
                            FROM strategy_bar_history
                            WHERE bar_time >= :session_start
                              AND bar_time < :session_end
                              AND decision_status <> ''
                            ORDER BY bar_time DESC
                            LIMIT 2000
                            """
                        ),
                        {
                            "session_start": session_start,
                            "session_end": session_end,
                        },
                    ).mappings()
                    )
                    if not bar_rows:
                        bar_rows = list(
                            session.execute(
                                text(
                                    """
                                    SELECT
                                        strategy_code,
                                        symbol,
                                        decision_status,
                                        decision_reason,
                                        decision_path,
                                        decision_score,
                                        decision_score_details,
                                        close_price,
                                        bar_time
                                    FROM strategy_bar_history
                                    WHERE bar_time >= :session_start
                                      AND bar_time < :session_end
                                      AND decision_status <> ''
                                    ORDER BY bar_time DESC
                                    LIMIT 2000
                                    """
                                ),
                                {
                                    "session_start": session_start,
                                    "session_end": session_end,
                                },
                            ).mappings()
                        )
                    for row in bar_rows:
                        recent_bar_decisions.append(
                            {
                                "strategy_code": str(row.get("strategy_code") or ""),
                                "symbol": str(row.get("symbol") or "").upper(),
                                "status": str(row.get("decision_status") or ""),
                                "reason": str(row.get("decision_reason") or ""),
                                "path": str(row.get("decision_path") or ""),
                                "score": str(row.get("decision_score") or ""),
                                "score_details": str(row.get("decision_score_details") or ""),
                                "price": _decimal_str(row.get("close_price")),
                                "last_bar_at": _datetime_str(row.get("bar_time")),
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
                    scanner_session_start = current_scanner_session_start_utc(now)
                    payload = confirmed_snapshot.payload if isinstance(confirmed_snapshot.payload, dict) else {}
                    marker_raw = payload.get("scanner_session_start_utc")
                    marker_matches = False
                    if isinstance(marker_raw, str):
                        try:
                            marker_dt = datetime.fromisoformat(marker_raw)
                        except ValueError:
                            marker_dt = None
                        if marker_dt is not None:
                            if marker_dt.tzinfo is None:
                                marker_dt = marker_dt.replace(tzinfo=UTC)
                            marker_matches = marker_dt.astimezone(UTC) == scanner_session_start
                    if marker_matches:
                        dashboard_snapshots["scanner_confirmed_last_nonempty"] = {
                            **payload,
                            "created_at": _datetime_str(confirmed_snapshot.created_at),
                        }
                alert_snapshot = session.scalar(
                    select(DashboardSnapshot)
                    .where(DashboardSnapshot.snapshot_type == "scanner_alert_engine_state")
                    .order_by(desc(DashboardSnapshot.created_at))
                )
                if self._snapshot_matches_current_scanner_session(
                    alert_snapshot,
                    session_start=current_scanner_session_start_utc(now),
                    require_session_marker=True,
                ):
                    dashboard_snapshots["scanner_alert_engine_state"] = {
                        **alert_snapshot.payload,
                        "created_at": _datetime_str(alert_snapshot.created_at),
                    }
                session_start = current_scanner_session_start_utc(now)
                manual_stop_snapshot = session.scalar(
                    select(DashboardSnapshot)
                    .where(DashboardSnapshot.snapshot_type == "bot_manual_stop_symbols")
                    .order_by(desc(DashboardSnapshot.created_at))
                )
                if self._snapshot_matches_current_scanner_session(
                    manual_stop_snapshot,
                    session_start=session_start,
                    require_session_marker=True,
                ):
                    dashboard_snapshots["bot_manual_stop_symbols"] = {
                        **manual_stop_snapshot.payload,
                        "created_at": _datetime_str(manual_stop_snapshot.created_at),
                    }
                global_manual_stop_snapshot = session.scalar(
                    select(DashboardSnapshot)
                    .where(DashboardSnapshot.snapshot_type == "global_manual_stop_symbols")
                    .order_by(desc(DashboardSnapshot.created_at))
                )
                if self._snapshot_matches_current_scanner_session(
                    global_manual_stop_snapshot,
                    session_start=session_start,
                    require_session_marker=True,
                ):
                    dashboard_snapshots["global_manual_stop_symbols"] = {
                        **global_manual_stop_snapshot.payload,
                        "created_at": _datetime_str(global_manual_stop_snapshot.created_at),
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
        recent_trade_coach_reviews = self._filter_symbol_rows(recent_trade_coach_reviews)
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
            "recent_trade_coach_reviews": recent_trade_coach_reviews,
            "recent_bar_decisions": recent_bar_decisions,
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
            "schwab_prewarm_symbols": [],
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
                    "schwab_prewarm_symbols": event.payload.schwab_prewarm_symbols,
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
            return self._empty_legacy_shadow_data()

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

    def _empty_legacy_shadow_data(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "connected": False,
            "fetched_at": None,
            "scanner": {"confirmed_symbols": [], "count": 0},
            "bots": {},
            "divergence": self._empty_legacy_divergence(),
            "errors": [],
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

    @app.get("/auth/schwab/start")
    async def schwab_auth_start() -> RedirectResponse:
        return RedirectResponse(url=_schwab_authorize_url(active_settings), status_code=303)

    @app.get("/auth/callback", response_class=HTMLResponse)
    async def schwab_auth_callback(code: str | None = None, error: str | None = None) -> HTMLResponse:
        if error:
            return HTMLResponse(
                content=(
                    "<html><body><h1>Schwab OAuth Failed</h1>"
                    f"<p>{escape(error)}</p>"
                    "<p>You can close this window and retry the authorization flow.</p>"
                    "</body></html>"
                ),
                status_code=400,
            )
        if not code:
            return HTMLResponse(
                content=(
                    "<html><body><h1>Schwab OAuth Failed</h1>"
                    "<p>Missing authorization code.</p>"
                    "<p>You can close this window and retry the authorization flow.</p>"
                    "</body></html>"
                ),
                status_code=400,
            )
        try:
            payload = await asyncio.to_thread(_exchange_schwab_authorization_code, active_settings, code)
            await asyncio.to_thread(_persist_schwab_token_store, active_settings, payload)
            await app.state.repository.invalidate_overview_cache()
        except Exception as exc:
            return HTMLResponse(
                content=(
                    "<html><body><h1>Schwab OAuth Failed</h1>"
                    f"<p>{escape(str(exc))}</p>"
                    "<p>You can close this window and retry the authorization flow.</p>"
                    "</body></html>"
                ),
                status_code=500,
            )
        return HTMLResponse(
            content=(
                "<html><body><h1>Schwab OAuth Updated</h1>"
                "<p>The Schwab token store on the VPS was refreshed successfully.</p>"
                "<p>You can close this window and return to Mai Tai.</p>"
                "</body></html>"
            ),
            status_code=200,
        )

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
        data = await app.state.repository.load_bot_dashboard_data()
        return {
            "bots": [
                {
                    **bot,
                    "recent_decisions": recent_decisions,
                    "trade_log": _build_bot_decision_entries(recent_decisions),
                    "account_summary": _build_bot_account_summary(data, bot),
                    "listening_status": _build_bot_listening_status(data, bot, recent_decisions),
                }
                for bot in data["bots"]
                for recent_decisions in [_resolved_bot_recent_decisions(data, bot)]
            ]
        }

    @app.get("/api/coach-reviews")
    async def coach_reviews_api(
        strategy_code: str | None = None,
        verdict: str | None = None,
        coaching_focus: str | None = None,
        symbol: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        data = await app.state.repository.load_bot_dashboard_data()
        default_start_text, default_end_text, default_start, default_end = _default_review_filter_dates()
        range_start = _parse_review_filter_date(start_date) or default_start
        range_end_start = _parse_review_filter_date(end_date) or default_end
        range_end = range_end_start + timedelta(days=1) if (end_date or "").strip() else default_end
        review_history = app.state.repository.load_trade_coach_review_history()
        regime_profiles = app.state.repository.load_trade_coach_regime_profiles(review_history)
        all_reviews = _apply_trade_coach_regime_profiles(
            _enrich_trade_coach_reviews(
                review_history,
                data.get("bots", []),
            ),
            regime_profiles,
        )
        filtered_reviews = _filter_trade_coach_reviews(
            all_reviews,
            strategy_code=strategy_code,
            verdict=verdict,
            coaching_focus=coaching_focus,
            symbol=symbol,
            start=range_start,
            end=range_end,
        )
        pattern_signals = _trade_coach_pattern_signals(filtered_reviews)
        path_patterns = _trade_coach_pattern_scoreboard(filtered_reviews, mode="path")
        regime_patterns = _trade_coach_pattern_scoreboard(filtered_reviews, mode="regime")
        operator_guidance = _trade_coach_operator_guidance(pattern_signals)
        return {
            "count": len(filtered_reviews),
            "returned_count": min(len(filtered_reviews), 100),
            "filters": {
                "strategy_code": (strategy_code or "").strip(),
                "verdict": (verdict or "").strip().lower(),
                "coaching_focus": (coaching_focus or "").strip().lower(),
                "symbol": (symbol or "").strip().upper(),
                "start_date": (start_date or default_start_text).strip(),
                "end_date": (end_date or default_end_text).strip(),
            },
            "summary": _trade_coach_review_summary(filtered_reviews),
            "review_queue": _build_trade_coach_review_queue(filtered_reviews)[:10],
            "pattern_signals": pattern_signals[:8],
            "path_patterns": path_patterns[:8],
            "regime_patterns": regime_patterns[:8],
            "operator_guidance": operator_guidance,
            "available_filters": {
                "strategies": [
                    {
                        "strategy_code": str(bot.get("strategy_code", "") or ""),
                        "display_name": str(bot.get("display_name", "") or ""),
                    }
                    for bot in data.get("bots", [])
                ],
                "verdicts": sorted(
                    {
                        str(item.get("verdict", "") or "").strip().lower()
                        for item in all_reviews
                        if str(item.get("verdict", "") or "").strip()
                    }
                ),
                "coaching_focuses": sorted(
                    {
                        str(item.get("coaching_focus", "") or "").strip().lower()
                        for item in all_reviews
                        if str(item.get("coaching_focus", "") or "").strip()
                    }
                ),
            },
            "reviews": filtered_reviews[:100],
        }

    @app.get("/api/coach-review")
    async def coach_review_api(cycle_key: str) -> dict[str, Any]:
        data = await app.state.repository.load_bot_dashboard_data()
        review_history = app.state.repository.load_trade_coach_review_history()
        regime_profiles = app.state.repository.load_trade_coach_regime_profiles(review_history)
        all_reviews = _apply_trade_coach_regime_profiles(
            _enrich_trade_coach_reviews(
                review_history,
                data.get("bots", []),
            ),
            regime_profiles,
        )
        review = _find_trade_coach_review(all_reviews, cycle_key)
        if review is None:
            return {"found": False, "cycle_key": cycle_key}
        same_path_reviews = _trade_coach_related_reviews(review, all_reviews, mode="path")
        same_symbol_reviews = _trade_coach_related_reviews(review, all_reviews, mode="symbol")
        similar_regime_reviews = _trade_coach_similar_regime_reviews(review, all_reviews)
        return {
            "found": True,
            "review": review,
            "priority": _trade_coach_review_priority(review),
            "regime_profile": dict(review.get("regime_profile", {}) or {}),
            "same_path_summary": _trade_coach_history_summary(same_path_reviews),
            "same_symbol_summary": _trade_coach_history_summary(same_symbol_reviews),
            "similar_regime_summary": _trade_coach_similarity_summary(similar_regime_reviews),
            "recent_same_path_reviews": same_path_reviews,
            "recent_same_symbol_reviews": same_symbol_reviews,
            "recent_similar_regime_reviews": similar_regime_reviews,
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
        await app.state.repository.invalidate_overview_cache()
        return RedirectResponse(url=redirect_to, status_code=303)

    @app.get("/scanner/blacklist/remove")
    async def scanner_blacklist_remove(
        symbol: str,
        redirect_to: str = "/scanner/dashboard",
    ) -> RedirectResponse:
        app.state.repository.remove_scanner_blacklist_symbol(symbol)
        await app.state.repository.invalidate_overview_cache()
        return RedirectResponse(url=redirect_to, status_code=303)

    @app.get("/bot/symbol/stop")
    async def bot_symbol_stop(
        strategy_code: str,
        symbol: str,
        redirect_to: str = "/bot/30s",
    ) -> RedirectResponse:
        app.state.repository.set_bot_manual_stop_symbol(strategy_code, symbol)
        await app.state.repository.invalidate_overview_cache()
        await _publish_manual_stop_update(
            app.state.repository.redis,
            active_settings,
            scope="bot",
            action="stop",
            strategy_code=strategy_code,
            symbol=symbol,
        )
        return RedirectResponse(url=redirect_to, status_code=303)

    @app.get("/bot/symbol/resume")
    async def bot_symbol_resume(
        strategy_code: str,
        symbol: str,
        redirect_to: str = "/bot/30s",
    ) -> RedirectResponse:
        app.state.repository.remove_bot_manual_stop_symbol(strategy_code, symbol)
        await app.state.repository.invalidate_overview_cache()
        await _publish_manual_stop_update(
            app.state.repository.redis,
            active_settings,
            scope="bot",
            action="resume",
            strategy_code=strategy_code,
            symbol=symbol,
        )
        return RedirectResponse(url=redirect_to, status_code=303)

    @app.get("/scanner/symbol/stop")
    async def scanner_symbol_stop(
        symbol: str,
        redirect_to: str = "/scanner/dashboard",
    ) -> RedirectResponse:
        app.state.repository.set_global_manual_stop_symbol(symbol)
        await app.state.repository.invalidate_overview_cache()
        await _publish_manual_stop_update(
            app.state.repository.redis,
            active_settings,
            scope="global",
            action="stop",
            symbol=symbol,
        )
        return RedirectResponse(url=redirect_to, status_code=303)

    @app.get("/scanner/symbol/resume")
    async def scanner_symbol_resume(
        symbol: str,
        redirect_to: str = "/scanner/dashboard",
    ) -> RedirectResponse:
        app.state.repository.remove_global_manual_stop_symbol(symbol)
        await app.state.repository.invalidate_overview_cache()
        await _publish_manual_stop_update(
            app.state.repository.redis,
            active_settings,
            scope="global",
            action="resume",
            symbol=symbol,
        )
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
            "today_alerts_count": data["scanner"]["today_alerts_count"],
            "diagnostics": data["scanner"]["alert_diagnostics"],
            "warmup": data["scanner"]["alert_warmup"],
        }

    @app.get("/scanner/alerts/export.csv")
    async def scanner_alerts_export_csv() -> Response:
        data = await app.state.repository.load_dashboard_data()
        output = StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "time",
                "type",
                "ticker",
                "price",
                "bid",
                "ask",
                "volume",
                "float",
                "details_json",
            ],
        )
        writer.writeheader()
        for alert in data["scanner"].get("today_alerts", []):
            details = alert.get("details", {})
            writer.writerow(
                {
                    "time": str(alert.get("time", "") or ""),
                    "type": str(alert.get("type", "") or ""),
                    "ticker": str(alert.get("ticker", "") or "").upper(),
                    "price": _as_float(alert.get("price")),
                    "bid": _as_float(alert.get("bid")),
                    "ask": _as_float(alert.get("ask")),
                    "volume": int(alert.get("volume", 0) or 0),
                    "float": int(alert.get("float", 0) or 0),
                    "details_json": json.dumps(details if isinstance(details, dict) else details),
                }
            )
        filename = f"mai-tai-alerts-{utcnow().astimezone(EASTERN_TZ).strftime('%Y-%m-%d')}.csv"
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename=\"{filename}\"'},
        )

    @app.get("/scanner/dashboard", response_class=HTMLResponse)
    async def scanner_dashboard() -> str:
        data = await app.state.repository.load_dashboard_data()
        return _render_scanner_dashboard(data)

    @app.get("/bot")
    async def bot_30s_status() -> dict[str, Any]:
        data = await app.state.repository.load_bot_dashboard_data()
        return _build_bot_api_payload(data, "macd_30s")

    @app.get("/botwebull")
    async def bot_webull_status() -> dict[str, Any]:
        data = await app.state.repository.load_bot_dashboard_data()
        return _build_bot_api_payload(data, "webull_30s")

    @app.get("/bot1m")
    async def bot_1m_status() -> dict[str, Any]:
        data = await app.state.repository.load_bot_dashboard_data()
        return _build_bot_api_payload(data, "macd_1m")

    @app.get("/botschwab1m")
    async def bot_schwab_1m_status() -> dict[str, Any]:
        data = await app.state.repository.load_bot_dashboard_data()
        return _build_bot_api_payload(data, "schwab_1m")

    @app.get("/botprobe")
    async def bot_probe_status() -> dict[str, Any]:
        data = await app.state.repository.load_bot_dashboard_data()
        return _build_bot_api_payload(data, "macd_30s_probe")

    @app.get("/botreclaim")
    async def bot_reclaim_status() -> dict[str, Any]:
        data = await app.state.repository.load_bot_dashboard_data()
        return _build_bot_api_payload(data, "macd_30s_reclaim")

    @app.get("/tosbot")
    async def tos_bot_status() -> dict[str, Any]:
        data = await app.state.repository.load_bot_dashboard_data()
        return _build_bot_api_payload(data, "tos")

    @app.get("/runnerbot")
    async def runner_bot_status() -> dict[str, Any]:
        data = await app.state.repository.load_bot_dashboard_data()
        return _build_bot_api_payload(data, "runner")

    async def _render_bot_page_with_trade_coach(strategy_code: str) -> str:
        data = await app.state.repository.load_bot_dashboard_data()
        bot = _find_bot_view(data, strategy_code)
        if bot is None:
            return _render_bot_detail_page(data, strategy_code)

        review_history = app.state.repository.load_trade_coach_review_history()
        regime_profiles = app.state.repository.load_trade_coach_regime_profiles(review_history)
        all_reviews = _apply_trade_coach_regime_profiles(
            _enrich_trade_coach_reviews(review_history, list(data.get("bots", []))),
            regime_profiles,
        )
        live_regime_profiles = app.state.repository.load_live_trade_coach_regime_profiles(
            strategy_code=strategy_code,
            symbols=_trade_coach_live_advisory_symbols(
                bot,
                _resolved_bot_recent_decisions(data, bot),
            ),
            interval_secs=int(bot.get("interval_secs", 30) or 30),
        )
        advisories = _build_trade_coach_live_advisories(
            bot=bot,
            recent_decisions=_resolved_bot_recent_decisions(data, bot),
            all_reviews=all_reviews,
            live_regime_profiles=live_regime_profiles,
        )
        return _render_bot_detail_page(
            data,
            strategy_code,
            trade_coach_live_advisories=advisories,
        )

    @app.get("/bot/30s", response_class=HTMLResponse)
    async def bot_30s_page() -> str:
        return await _render_bot_page_with_trade_coach("macd_30s")

    @app.get("/bot/30s-webull", response_class=HTMLResponse)
    async def bot_webull_30s_page() -> str:
        return await _render_bot_page_with_trade_coach("webull_30s")

    @app.get("/bot/30s-probe", response_class=HTMLResponse)
    async def bot_30s_probe_page() -> str:
        return await _render_bot_page_with_trade_coach("macd_30s_probe")

    @app.get("/bot/30s-reclaim", response_class=HTMLResponse)
    async def bot_30s_reclaim_page() -> str:
        return await _render_bot_page_with_trade_coach("macd_30s_reclaim")

    @app.get("/bot/1m", response_class=HTMLResponse)
    async def bot_1m_page() -> str:
        return await _render_bot_page_with_trade_coach("macd_1m")

    @app.get("/bot/1m-schwab", response_class=HTMLResponse)
    async def bot_schwab_1m_page() -> str:
        return await _render_bot_page_with_trade_coach("schwab_1m")

    @app.get("/bot/tos", response_class=HTMLResponse)
    async def bot_tos_page() -> str:
        return await _render_bot_page_with_trade_coach("tos")

    @app.get("/bot/runner", response_class=HTMLResponse)
    async def bot_runner_page() -> str:
        return await _render_bot_page_with_trade_coach("runner")

    @app.get("/coach/reviews", response_class=HTMLResponse)
    async def coach_reviews_page(
        strategy_code: str | None = None,
        verdict: str | None = None,
        coaching_focus: str | None = None,
        symbol: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        data = await app.state.repository.load_bot_dashboard_data()
        default_start_text, default_end_text, default_start, default_end = _default_review_filter_dates()
        range_start = _parse_review_filter_date(start_date) or default_start
        range_end_start = _parse_review_filter_date(end_date) or default_end
        range_end = range_end_start + timedelta(days=1) if (end_date or "").strip() else default_end
        review_history = app.state.repository.load_trade_coach_review_history()
        regime_profiles = app.state.repository.load_trade_coach_regime_profiles(review_history)
        return _render_trade_coach_review_center(
            data,
            review_history=review_history,
            regime_profiles=regime_profiles,
            strategy_code=strategy_code,
            verdict=verdict,
            coaching_focus=coaching_focus,
            symbol=symbol,
            start_date=(start_date or default_start_text).strip(),
            end_date=(end_date or default_end_text).strip(),
        )

    @app.get("/coach/review", response_class=HTMLResponse)
    async def coach_review_detail_page(cycle_key: str) -> str:
        data = await app.state.repository.load_bot_dashboard_data()
        review_history = app.state.repository.load_trade_coach_review_history()
        return _render_trade_coach_review_detail(
            data,
            review_history=review_history,
            cycle_key=cycle_key,
            regime_profiles=app.state.repository.load_trade_coach_regime_profiles(review_history),
        )

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
    bot_nav_html = _build_bot_nav_html([str(bot["strategy_code"]) for bot in bot_views])
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
        for item in scanner["top_confirmed"]
    ) or _empty_row(12, "No confirmed candidates yet")
    handoff_rows = "".join(
        f"""
        <tr>
          <td>{item["handoff_rank"]}</td>
          <td><strong>{escape(item["ticker"])}</strong></td>
          <td>{escape(", ".join(item["watched_by"]) or "-")}</td>
          <td>{"yes" if item.get("is_top5") else "no"}</td>
          <td>{item["rank_score"]:.0f}</td>
          <td>{item["price"]:.2f}</td>
          <td>{item["change_pct"]:+.1f}%</td>
          <td>{escape(item["confirmation_path"] or "-")}</td>
        </tr>
        """
        for item in scanner["bot_handoff"]
    ) or _empty_row(8, "No symbols currently handed to bots")

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
            <p><strong>Data Health:</strong> {escape(str(bot.get("data_health", {}).get("status", "healthy")).upper())}</p>
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
                <div class="label">Ranked Scanner</div>
                <div class="value">{scanner["top_confirmed_count"]}</div>
                <p>Score stays visible here only</p>
              </div>
              <div class="card">
                <div class="label">Handed To Bots</div>
                <div class="value">{scanner["bot_handoff_count"]}</div>
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

          <nav class="nav">{bot_nav_html}</nav>

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
                    <p><strong>Ranked Scanner Count:</strong> {scanner["top_confirmed_count"]}</p>
                    <p><strong>Handed To Bots:</strong> {scanner["bot_handoff_count"]}</p>
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
                    <h2>Ranked Scanner View</h2>
                    <div class="sub">Momentum-confirmed names ranked for visibility only. Score does not gate bot handoff.</div>
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

              <section class="section">
                <div class="section-header">
                  <div>
                    <h2>Handed To Bots</h2>
                    <div class="sub">Current symbols flowing from the momentum scanner into active bot watchlists.</div>
                  </div>
                </div>
                <div class="table-card">
                  <table>
                    <thead>
                      <tr><th>#</th><th>Ticker</th><th>Feed To</th><th>Ranked View</th><th>Score</th><th>Price</th><th>Change</th><th>Path</th></tr>
                    </thead>
                    <tbody>{handoff_rows}</tbody>
                  </table>
                </div>
              </section>
            </div>
          </details>

          <section class="section" id="bots">
            <div class="section-header">
              <div>
                <h2>Bot Deck</h2>
                <div class="sub">Active strategy runtimes configured in this environment.</div>
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
        "title": "Schwab 30 Sec Bot",
        "nav_title": "Schwab 30s",
        "badge": "30",
        "color": "#2979ff",
        "path": "/bot/30s",
    },
    "webull_30s": {
        "title": "Webull 30 Sec Bot",
        "nav_title": "Webull 30s",
        "badge": "WB",
        "color": "#ff8f00",
        "path": "/bot/30s-webull",
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
    "schwab_1m": {
        "title": "Schwab 1 Min Bot",
        "nav_title": "Schwab 1m",
        "badge": "1M",
        "color": "#1e88e5",
        "path": "/bot/1m-schwab",
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


def _visible_bot_page_meta(available_codes: list[str] | None = None) -> list[tuple[str, dict[str, str]]]:
    ordered_codes = available_codes or list(BOT_PAGE_META.keys())
    return [(code, BOT_PAGE_META[code]) for code in ordered_codes if code in BOT_PAGE_META]


def _build_bot_nav_html(available_codes: list[str]) -> str:
    links = ["<a href=\"/scanner/dashboard\">Scanner Page</a>"]
    for code, meta in _visible_bot_page_meta(available_codes):
        links.append(f'<a href="{meta["path"]}">{escape(str(meta.get("nav_title", meta["title"])).replace("Mai Tai ", ""))}</a>')
    links.append('<a href="/coach/reviews">Trade Coach</a>')
    links.extend(
        [
            '<a href="#scanner">Scanner</a>',
            '<a href="#bots">Bots</a>',
            '<a href="#reconciliation">Reconciliation</a>',
            '<a href="#orders">Orders</a>',
            '<a href="#positions">Positions</a>',
        ]
    )
    return "".join(links)


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


def _resolved_bot_recent_decisions(data: dict[str, Any], bot: dict[str, Any]) -> list[dict[str, Any]]:
    runtime_items = [
        dict(item)
        for item in bot.get("recent_decisions", [])
        if isinstance(item, dict)
    ]
    if runtime_items:
        return runtime_items[:50]

    strategy_code = str(bot.get("strategy_code", "") or "")
    fallback_items = [
        dict(item)
        for item in data.get("recent_bar_decisions", [])
        if isinstance(item, dict) and str(item.get("strategy_code", "") or "") == strategy_code
    ]
    return _dedupe_decision_events(fallback_items)[:50]


def _service_by_name(data: dict[str, Any], service_name: str) -> dict[str, Any] | None:
    return next(
        (service for service in data.get("services", []) if service.get("service_name") == service_name),
        None,
    )


def _parse_eastern_label(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %I:%M:%S %p ET").replace(tzinfo=EASTERN_TZ)
    except ValueError:
        return None


def _seconds_since_eastern_label(value: str | None) -> float | None:
    parsed = _parse_eastern_label(value)
    if parsed is None:
        return None
    return (utcnow().astimezone(EASTERN_TZ) - parsed).total_seconds()


def _is_regular_session_now() -> bool:
    now_et = utcnow().astimezone(EASTERN_TZ)
    minutes = now_et.hour * 60 + now_et.minute
    return 7 * 60 <= minutes < 18 * 60


def _build_bot_listening_status(
    data: dict[str, Any],
    bot: dict[str, Any],
    recent_decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    market_data_source = ControlPlaneRepository._market_data_source_label(str(bot.get("provider", "") or ""))
    strategy_service = _service_by_name(data, "strategy-engine") or {}
    market_data = data.get("market_data", {})
    latest_snapshot = market_data.get("latest_snapshot_batch") or {}
    latest_market_data_at = str(latest_snapshot.get("completed_at") or "")
    if not latest_market_data_at:
        latest_market_data_at = _datetime_str(market_data.get("latest_subscription_observed_at_raw"))
    latest_decision_at = str(recent_decisions[0].get("last_bar_at") or "") if recent_decisions else ""
    latest_bot_tick_at = max((str(value or "") for value in dict(bot.get("last_tick_at", {}) or {}).values()), default="")
    indicator_snapshots = list(bot.get("indicator_snapshots", []) or [])
    latest_indicator_at = max(
        (str(snapshot.get("last_bar_at") or "") for snapshot in indicator_snapshots if str(snapshot.get("last_bar_at") or "").strip()),
        default="",
    )
    latest_heartbeat_at = str(strategy_service.get("observed_at") or "")

    decision_age_seconds = _seconds_since_eastern_label(latest_decision_at)
    bot_tick_age_seconds = _seconds_since_eastern_label(latest_bot_tick_at)
    indicator_age_seconds = _seconds_since_eastern_label(latest_indicator_at)
    market_data_age_seconds = _seconds_since_eastern_label(latest_market_data_at)
    heartbeat_age_seconds = _seconds_since_eastern_label(latest_heartbeat_at)

    service_status = str(
        strategy_service.get("effective_status", strategy_service.get("status", "unknown")) or "unknown"
    ).lower()
    service_raw_status = str(strategy_service.get("status", "unknown") or "unknown").lower()
    watchlist_count = len(bot.get("watchlist", []))
    position_count = len(bot.get("positions", []))
    active_session = _is_regular_session_now()
    data_health = dict(bot.get("data_health", {}) or {})
    data_health_status = str(data_health.get("status", "healthy") or "healthy").lower()
    halted_symbols = [str(symbol).upper() for symbol in list(data_health.get("halted_symbols", []) or [])]
    warning_symbols = [str(symbol).upper() for symbol in list(data_health.get("warning_symbols", []) or [])]
    raw_reasons = dict(data_health.get("reasons", {}) or {})
    raw_warning_reasons = dict(data_health.get("warning_reasons", {}) or {})
    unique_reasons = [
        str(reason).strip()
        for reason in dict.fromkeys(raw_reasons.values())
        if str(reason).strip()
    ]
    representative_reason = unique_reasons[0] if len(unique_reasons) == 1 else ""
    unique_warning_reasons = [
        str(reason).strip()
        for reason in dict.fromkeys(raw_warning_reasons.values())
        if str(reason).strip()
    ]
    representative_warning_reason = (
        unique_warning_reasons[0] if len(unique_warning_reasons) == 1 else ""
    )

    state = "LISTENING"
    detail = "Bot is actively evaluating bars."
    color = "#5fff8d"
    has_fresh_bot_activity = (
        (decision_age_seconds is not None and decision_age_seconds <= 120)
        or (bot_tick_age_seconds is not None and bot_tick_age_seconds <= 90)
        or (indicator_age_seconds is not None and indicator_age_seconds <= 120)
    )

    if data_health_status in {"critical", "error"}:
        state = "DATA HALT"
        if halted_symbols:
            if representative_reason:
                detail = representative_reason
            else:
                detail = (
                    f"{market_data_source} stream stale/disconnected; entries are blocked and any open positions are eligible for emergency close."
                    if position_count > 0
                    else f"{market_data_source} stream stale/disconnected; entries are blocked, but there are no open positions to emergency close."
                )
        else:
            detail = f"{market_data_source} data health is degraded."
        color = "#ff6b6b"
    elif data_health_status == "degraded":
        if halted_symbols:
            state = "DEGRADED"
            if representative_reason:
                detail = representative_reason
            else:
                detail = (
                    f"{market_data_source} data is quiet on some flat symbols; entries on those names stay blocked until ticks recover."
                )
            color = "#ffcc5b"
        elif warning_symbols:
            state = "LISTENING"
            if representative_warning_reason:
                detail = representative_warning_reason
            else:
                detail = (
                    f"{market_data_source} ticks are quiet on some flat symbols, but the overall stream is still live."
                )
            color = "#5fff8d"
        else:
            state = "DEGRADED"
            detail = f"{market_data_source} data health is degraded."
            color = "#ffcc5b"
    elif service_status in {"stopping", "stopped", "inactive"} or service_raw_status in {"stopping", "stopped", "inactive"}:
        state = "STOPPED"
        detail = "Strategy engine is not running."
        color = "#ff6b6b"
    elif active_session and market_data_age_seconds is not None and market_data_age_seconds > 90:
        state = "STALE"
        detail = "Market data feed looks stale."
        color = "#ffcc5b"
    elif active_session and watchlist_count == 0 and position_count == 0:
        state = "NO ACTIVE SYMBOLS"
        detail = "Bot is up, but there are no active symbols to evaluate."
        color = "#98a6c8"
    elif active_session and decision_age_seconds is not None and decision_age_seconds > 120 and watchlist_count > 0:
        if indicator_age_seconds is not None and indicator_age_seconds <= 120:
            detail = "Bars are updating; Decision Tape is lagging behind."
        else:
            state = "STALE"
            detail = "Bot has symbols, but no fresh decision rows are being recorded."
            color = "#ffcc5b"
    elif not recent_decisions and active_session and watchlist_count > 0:
        state = "STALE"
        detail = "No decision rows are available for an active session."
        color = "#ffcc5b"
    elif active_session and heartbeat_age_seconds is not None and heartbeat_age_seconds > 90:
        if has_fresh_bot_activity:
            detail = "Bot activity is fresh; strategy heartbeat is lagging."
        else:
            state = "STALE"
            detail = "Strategy heartbeat is stale."
            color = "#ffcc5b"

    return {
        "state": state,
        "detail": detail,
        "color": color,
        "latest_decision_at": latest_decision_at,
        "latest_bot_tick_at": latest_bot_tick_at,
        "latest_market_data_at": latest_market_data_at,
        "latest_heartbeat_at": latest_heartbeat_at,
        "watchlist_count": watchlist_count,
        "position_count": position_count,
        "tracked_bar_count": sum(int(value or 0) for value in dict(bot.get("bar_counts", {}) or {}).values()),
        "data_health": data_health,
    }


def _build_bot_api_payload(data: dict[str, Any], strategy_code: str) -> dict[str, Any]:
    bot = _find_bot_view(data, strategy_code)
    if bot is None:
        return {"error": "Bot not initialized"}
    recent_decisions = _resolved_bot_recent_decisions(data, bot)
    listening_status = _build_bot_listening_status(data, bot, recent_decisions)
    return {
        "status": bot["wiring_status"],
        "watched_tickers": bot["watchlist"],
        "positions": bot["positions"],
        "pending_open_symbols": bot["pending_open_symbols"],
        "pending_close_symbols": bot["pending_close_symbols"],
        "pending_scale_levels": bot["pending_scale_levels"],
        "daily_pnl": bot["daily_pnl"],
        "closed_today": bot["closed_today"],
        "recent_decisions": recent_decisions,
        "recent_intents": bot["recent_intents"],
        "recent_orders": bot["recent_orders"],
        "recent_fills": bot["recent_fills"],
        "indicator_snapshots": bot["indicator_snapshots"],
        "bar_counts": bot.get("bar_counts", {}),
        "last_tick_at": bot.get("last_tick_at", {}),
        "data_health": bot.get("data_health", {}),
        "tos_parity": bot["tos_parity"],
        "account_summary": _build_bot_account_summary(data, bot),
        "trade_log": _build_bot_decision_entries(recent_decisions),
        "listening_status": listening_status,
    }


def _render_scanner_dashboard(data: dict[str, Any]) -> str:
    scanner = data["scanner"]
    bot_views = data["bots"]
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
        set(scanner.get("global_manual_stop_symbols", [])),
    )
    pillar_rows = _render_scanner_stock_rows(scanner["five_pillars"][:20], subscription_symbols)
    gainer_rows = _render_scanner_stock_rows(scanner["top_gainers"][:20], subscription_symbols)
    alert_rows = _render_alert_rows(scanner["recent_alerts"])
    confirmed_sub = (
        "Full confirmed universe for the current session. TOP5 marks the ranked scanner slice; "
        "BOT marks symbols currently handed to active bot watchlists."
    )
    scanner_nav_links = [
        '<a href="/scanner/dashboard" class="active">Mai Tai Scanner</a>',
        '<a href="/coach/reviews">Trade Coach</a>',
        '<a href="/">Mai Tai Control Plane</a>',
    ]
    for code, meta in _visible_bot_page_meta([str(bot["strategy_code"]) for bot in bot_views]):
        scanner_nav_links.append(
            f'<a href="{meta["path"]}">{escape(str(meta.get("nav_title", meta["title"])))}</a>'
        )
    scanner_nav_html = "".join(scanner_nav_links)

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
                    <span>Handed</span>
                    <strong>{scanner["bot_handoff_count"]}</strong>
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
                <div class="metric-card">
                    <span>Global Stops</span>
                    <strong>{scanner["global_manual_stop_count"]}</strong>
                </div>
            </div>

            <div class="side-section">
                <div class="side-label">Navigation</div>
                <div class="nav-strip">{scanner_nav_html}</div>
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
                    <div class="line-item"><strong>Handed To Bots:</strong> {scanner["bot_handoff_count"]}</div>
                    <div class="line-item"><strong>Ref Tickers:</strong> {latest_snapshot.get("reference_count", 0):,}</div>
                    <div class="line-item"><strong>WebSocket:</strong> {escape(websocket_label)} ({displayed_subscription_count} subs)</div>
                    <div class="line-item"><strong>Feed Note:</strong> {escape(feed_status_note or "No feed note")}</div>
                    <div class="line-item"><strong>Reconcile:</strong> {escape(reconcile_note)}</div>
                </div>
            </div>

            <div class="side-section">
                <div class="side-label">Global Manual Stops</div>
                <div class="stack">{_render_scanner_manual_stop_entries(scanner.get("global_manual_stop_symbols", []))}</div>
            </div>

            <div class="side-section">
                <div class="side-label">Scanner Blacklist</div>
                <div class="stack">{_render_scanner_blacklist_entries(scanner["blacklist"])}</div>
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
                        <thead><tr><th>#</th><th>Ticker / Bot</th><th>Score</th><th>Confirmed</th><th>Entry Price</th><th>Price</th><th>Change%</th><th>Volume</th><th>RVol</th><th>Squeezes</th><th>1st Spike</th><th>Catalyst</th><th>Control</th></tr></thead>
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
                        <div class="sub">Latest alert tape with simple color-coded momentum events. Export includes the full current-day alert ledger.</div>
                    </div>
                    <div style="display:flex;gap:12px;align-items:center;">
                        <a href="/scanner/alerts/export.csv" style="color:#4fc3f7;text-decoration:none;font-size:12px;border:1px solid #4fc3f7;padding:6px 10px;border-radius:8px;">Export Today CSV ({scanner["today_alerts_count"]})</a>
                        <span class="count amber">{scanner["recent_alerts_count"]}</span>
                    </div>
                </div>
                <div class="panel-copy">Warmup: {"Ready" if warmup.get("fully_ready") else "History building"} | 5m ready: {"yes" if warmup.get("squeeze_5min_ready") else "no"} | 10m ready: {"yes" if warmup.get("squeeze_10min_ready") else "no"}</div>
                <div class="table-wrap table-wrap-alerts">
                    <table>
                        <thead><tr><th>Time</th><th>Type</th><th>Ticker</th><th style="text-align:right">Price</th><th style="text-align:right">Volume</th><th>Details</th></tr></thead>
                        <tbody>{alert_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>Recent Alert Rejections</h3>
                        <div class="sub">Near-candidates seen by the alert engine that did not fire, with the current blocking reasons attached.</div>
                    </div>
                    <span class="count amber">{scanner["alert_diagnostics_count"]}</span>
                </div>
                <div class="table-wrap table-wrap-alerts">
                    <table>
                        <thead><tr><th>Time</th><th>Ticker</th><th style="text-align:right">Price</th><th style="text-align:right">Volume</th><th>Reasons</th><th>Metrics</th></tr></thead>
                        <tbody>{_render_alert_diagnostic_rows(scanner.get("alert_diagnostics", []))}</tbody>
                    </table>
                </div>
            </section>
        </main>
    </div>
</body>
</html>"""


def _render_bot_detail_page(
    data: dict[str, Any],
    strategy_code: str,
    *,
    trade_coach_live_advisories: list[dict[str, Any]] | None = None,
) -> str:
    bot = _find_bot_view(data, strategy_code)
    if bot is None:
        return "<h1>Bot not initialized</h1>"

    meta = BOT_PAGE_META[strategy_code]
    refresh_seconds = 30
    recent_decisions = _resolved_bot_recent_decisions(data, bot)
    listening_status = _build_bot_listening_status(data, bot, recent_decisions)
    recent_fills = [item for item in data["recent_fills"] if item["strategy_code"] == strategy_code]
    recent_orders = [item for item in data["recent_orders"] if item["strategy_code"] == strategy_code]
    recent_trade_coach_reviews = list(bot.get("recent_trade_coach_reviews", []))
    live_trade_coach_advisories = list(trade_coach_live_advisories or [])
    position_rows = _build_bot_position_rows(data, bot)
    completed_rows, completed_count, completed_pnl = _build_completed_position_rows(bot, recent_orders, recent_fills)
    trade_coach_rows, trade_coach_count = _build_trade_coach_review_rows(recent_trade_coach_reviews)
    live_trade_coach_rows, live_trade_coach_count = _build_trade_coach_live_advisory_rows(
        live_trade_coach_advisories
    )
    live_trade_coach_summary_cards = _build_trade_coach_live_advisory_summary_cards(
        live_trade_coach_advisories,
        reviewed_count=trade_coach_count,
    )
    live_trade_coach_spotlights = _build_trade_coach_live_advisory_spotlight_cards(
        live_trade_coach_advisories
    )
    order_rows, order_count = _build_order_history_rows(recent_orders, recent_fills)
    tos_parity = bot.get("tos_parity", {})
    tos_parity_rows = _build_tos_parity_rows(tos_parity)
    decision_rows = _build_bot_decision_rows(recent_decisions)
    failed_rows, failed_count = _build_failed_action_rows(bot)
    pnl_color = "#5fff8d" if completed_pnl >= 0 else "#ff6b6b"
    recent_fill_count = len(recent_fills)
    retention_rows = list(bot.get("retention_states", []))
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
    manual_stop_symbols = [
        str(symbol).upper()
        for symbol in bot.get("manual_stop_symbols", [])
        if str(symbol).strip()
    ]
    for symbol in bot["watchlist"]:
        normalized = str(symbol).upper()
        if normalized and normalized not in active_symbols and normalized not in manual_stop_symbols:
            active_symbols.append(normalized)
    open_symbols = {
        str(item.get("ticker") or item.get("symbol") or "").upper()
        for item in bot["positions"]
        if item.get("ticker") or item.get("symbol")
    }
    pending_symbols = {str(symbol).upper() for symbol in bot["pending_open_symbols"] + bot["pending_close_symbols"]}
    live_symbol_html = _build_bot_symbol_action_html(
        strategy_code,
        symbols=active_symbols,
        manual_stop_symbols=set(manual_stop_symbols),
        open_symbols=open_symbols,
        pending_symbols=pending_symbols,
        redirect_to=meta["path"],
        empty_text="No live symbols in this bot",
    )
    manual_stop_html = _build_bot_manual_stop_html(
        strategy_code,
        manual_stop_symbols,
        redirect_to=meta["path"],
    )
    retention_html = _build_retention_status_html(retention_rows, tracked_symbols=set(active_symbols))
    data_health = dict(bot.get("data_health", {}) or {})
    data_health_status = str(data_health.get("status", "healthy") or "healthy").lower()
    halted_symbols = [str(symbol).upper() for symbol in list(data_health.get("halted_symbols", []) or [])]
    warning_symbols = [str(symbol).upper() for symbol in list(data_health.get("warning_symbols", []) or [])]
    data_health_reasons = dict(data_health.get("reasons", {}) or {})
    data_warning_reasons = dict(data_health.get("warning_reasons", {}) or {})
    data_health_since = dict(data_health.get("since", {}) or {})
    data_warning_since = dict(data_health.get("warning_since", {}) or {})
    market_data_source = ControlPlaneRepository._market_data_source_label(str(bot.get("provider", "") or ""))
    data_health_card_label = f"{market_data_source} Data Health"
    data_health_panel_title = (
        f"{market_data_source} Data Warning"
        if data_health_status == "degraded"
        else f"{market_data_source} Data Halt"
    )
    quote_source_label = f"{market_data_source} quotes"
    data_health_detail = f"{market_data_source} data path healthy."
    data_health_color = (
        "#ff6b6b"
        if data_health_status in {"critical", "error"}
        else "#ffcc5b" if data_health_status == "degraded" else "#5fff8d"
    )
    if halted_symbols:
        reason_parts = [
            f"{symbol}: {data_health_reasons.get(symbol, f'{market_data_source} stream stale/disconnected')}"
            for symbol in halted_symbols
        ]
        data_health_detail = " | ".join(reason_parts)
    elif warning_symbols:
        reason_parts = [
            f"{symbol}: {data_warning_reasons.get(symbol, f'{market_data_source} ticks temporarily quiet on this flat symbol')}"
            for symbol in warning_symbols
        ]
        data_health_detail = " | ".join(reason_parts)
    current_position = bot["positions"][0] if strategy_code == "runner" and bot["positions"] else None
    available_codes = [str(item["strategy_code"]) for item in data.get("bots", [])]
    production_preview = int(bot.get("interval_secs") or 0) == 30 and strategy_code in {
        "macd_30s",
        "webull_30s",
        "probe_30s",
        "reclaim_30s",
    }
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

    completed_positions_panel = f"""
            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>Completed Positions</h3>
                    <div class="sub">Completed trade cycles for this bot, including positions that finished by scale-out.</div>
                </div>
                <span class="count pink">{completed_count}</span>
            </div>
            <div class="table-wrap">
                <table>
                    <thead><tr><th>Ticker</th><th>Path</th><th style="text-align:right">Qty</th><th>Entry Time</th><th style="text-align:right">Entry</th><th>Exit Time</th><th style="text-align:right">Exit</th><th>P&amp;L</th><th>Exit Summary</th></tr></thead>
                    <tbody>{completed_rows}</tbody>
                </table>
            </div>
        </section>"""

    live_trade_coach_panel = f"""
            <section class="panel full accent-panel coach-advisory-panel">
                <div class="panel-header">
                    <div>
                        <h3>Trade Coach Live Advisory</h3>
                        <div class="sub">Advisory-only caution for live symbols, grounded in reviewed history and similar regimes. This does not gate trading.</div>
                    </div>
                    <span class="count accent">Live preview · {live_trade_coach_count}</span>
                </div>
                <div class="panel-copy">{
                    "Production preview for the live 30-second coaching experience. It shows what the operator would see in real time, but it never changes trading behavior."
                    if production_preview
                    else "Read-only live caution layer for this bot. The coach can surface context here without changing any execution behavior."
                }</div>
                {live_trade_coach_summary_cards}
                <div class="panel-header coach-subheader">
                    <div>
                        <h3>Top Live Cautions</h3>
                        <div class="sub">The strongest symbol-level cautions the coach can see right now from path, regime, and symbol memory.</div>
                    </div>
                    <span class="count">{min(live_trade_coach_count, 3)}</span>
                </div>
                <div class="panel-copy coach-subcopy">This is the pre-trade view only. It is intentionally informational, so we can see the end-state workflow without introducing any trading controls.</div>
                {live_trade_coach_spotlights}
                <div class="panel-header coach-subheader">
                    <div>
                        <h3>Live Symbol Matrix</h3>
                        <div class="sub">Every visible symbol with current context, matched trade memory, caution score, and what to watch next.</div>
                    </div>
                    <span class="count">{live_trade_coach_count}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Symbol</th><th>Live Context</th><th>History Match</th><th>Caution</th><th>What To Watch</th></tr></thead>
                        <tbody>{live_trade_coach_rows}</tbody>
                    </table>
                </div>
                <div class="panel-copy">Use this as a pre-trade caution layer only. The review center remains the source of truth for the full post-trade breakdown and operator follow-up.</div>
            </section>"""

    trade_coach_panel = f"""
            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>Trade Coach Reviews</h3>
                        <div class="sub">Post-trade AI reviews for completed flat-to-flat cycles on this bot.</div>
                    </div>
                    <span class="count accent">{trade_coach_count}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Reviewed</th><th>Ticker</th><th>Trade Facts</th><th>Coach Verdict</th><th>Should Trade</th><th>Coach Notes</th></tr></thead>
                        <tbody>{trade_coach_rows}</tbody>
                    </table>
                </div>
            </section>"""

    listening_panel = f"""
            <section class="panel full accent-panel">
                <div class="panel-header">
                    <div>
                        <h2>Listening Status</h2>
                        <div class="sub">Explicit signal for whether this bot is actively listening and evaluating bars.</div>
                    </div>
                    <span class="count accent" style="color:{listening_status["color"]}">{escape(listening_status["state"])}</span>
                </div>
                <div class="hero-grid">
                    <div class="hero-card"><span>State</span><strong style="color:{listening_status["color"]}">{escape(listening_status["state"])}</strong><small>{escape(listening_status["detail"])}</small></div>
                    <div class="hero-card"><span>Last Decision</span><strong>{escape(listening_status["latest_decision_at"] or "-")}</strong><small>{len(recent_decisions)} rows visible</small></div>
                    <div class="hero-card"><span>Last Bot Tick</span><strong>{escape(listening_status["latest_bot_tick_at"] or "-")}</strong><small>Latest tick that reached this bot</small></div>
                    <div class="hero-card"><span>Last Market Data</span><strong>{escape(listening_status["latest_market_data_at"] or "-")}</strong><small>Snapshot / subscription freshness</small></div>
                    <div class="hero-card"><span>Last Strategy Heartbeat</span><strong>{escape(listening_status["latest_heartbeat_at"] or "-")}</strong><small>strategy-engine heartbeat</small></div>
                    <div class="hero-card"><span>{escape(data_health_card_label)}</span><strong style="color:{data_health_color}">{escape(data_health_status.upper())}</strong><small>{escape(", ".join(halted_symbols or warning_symbols) or "no halted symbols")}</small></div>
                    <div class="hero-card"><span>Tracked Symbols</span><strong>{listening_status["watchlist_count"]}</strong><small>Open positions: {listening_status["position_count"]} · Bars cached: {listening_status["tracked_bar_count"]}</small></div>
                </div>
            </section>"""

    data_health_panel = ""
    if data_health_status != "healthy":
        affected_symbols = halted_symbols or warning_symbols
        halted_since = ", ".join(
            f"{symbol} since {(data_health_since if halted_symbols else data_warning_since).get(symbol, '-')}"
            for symbol in affected_symbols
        )
        if data_health_status == "degraded":
            data_health_sub = (
                "Some flat symbols have temporarily quiet Schwab ticks. The overall stream remains up and synthetic bar continuation can still run."
            )
        else:
            data_health_sub = (
                f"Trading is blocked for halted symbols; any open positions are eligible for emergency close using {quote_source_label} only."
                if listening_status["position_count"] > 0
                else "Trading is blocked for halted symbols; there are no open positions currently exposed to the emergency-close path."
            )
        data_health_panel = f"""
            <section class="panel full {"critical-panel" if data_health_status in {"critical", "error"} else "accent-panel"}">
                <div class="panel-header">
                    <div>
                        <h2>{escape(data_health_panel_title)}</h2>
                        <div class="sub">{escape(data_health_sub)}</div>
                    </div>
                    <span class="count {"danger" if data_health_status in {"critical", "error"} else "accent"}">{escape(data_health_status.upper())}</span>
                </div>
                <div class="panel-copy"><strong>Symbols:</strong> {escape(", ".join(affected_symbols) or "-")}<br><strong>Since:</strong> {escape(halted_since or "-")}<br><strong>Reason:</strong> {escape(data_health_detail)}</div>
            </section>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>{meta["title"]}</title>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="{refresh_seconds}">
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
        .critical-panel {{
            border-color: rgba(255,107,107,0.74);
            box-shadow: 0 18px 42px rgba(0, 0, 0, 0.24), 0 0 0 1px rgba(255,107,107,0.30);
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
        .count.danger {{ background: rgba(255,107,107,0.14); color: var(--red); }}
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
        .coach-advisory-panel {{
            background:
                radial-gradient(circle at top right, color-mix(in srgb, var(--accent) 16%, transparent), transparent 28%),
                rgba(24, 32, 54, 0.96);
        }}
        .coach-advisory-hero-grid {{
            padding-top: 2px;
        }}
        .coach-hero-card {{
            background: linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.02));
        }}
        .coach-subheader {{
            margin: 0 16px;
            border: 1px solid rgba(121, 146, 193, 0.16);
            border-radius: 16px;
            padding-left: 0;
            padding-right: 0;
            background: rgba(255,255,255,0.02);
        }}
        .coach-subcopy {{
            padding-top: 10px;
            padding-bottom: 10px;
        }}
        .coach-spotlight-grid {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 12px;
            padding: 0 16px 16px 16px;
        }}
        .coach-spotlight-card {{
            background: linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.02));
            border: 1px solid rgba(121, 146, 193, 0.24);
            border-radius: 16px;
            padding: 14px;
            display: grid;
            gap: 12px;
            min-width: 0;
        }}
        .coach-spotlight-empty {{
            grid-column: 1 / -1;
        }}
        .coach-spotlight-topline {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
            color: var(--muted);
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }}
        .coach-spotlight-card h4 {{
            margin: 0;
            font-size: 15px;
            line-height: 1.45;
        }}
        .coach-spotlight-card p {{
            margin: 0;
            color: var(--muted);
            font-size: 12px;
            line-height: 1.5;
        }}
        .coach-spotlight-facts {{
            display: grid;
            gap: 8px;
        }}
        .coach-spotlight-facts div {{
            display: grid;
            gap: 4px;
        }}
        .coach-spotlight-facts strong {{
            color: var(--ink);
            font-size: 12px;
        }}
        .coach-spotlight-facts span {{
            color: var(--muted);
            font-size: 11px;
            line-height: 1.4;
        }}
        .coach-chip-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }}
        .coach-spotlight-high {{
            border-color: rgba(255,107,107,0.42);
            box-shadow: inset 0 0 0 1px rgba(255,107,107,0.12);
        }}
        .coach-spotlight-medium {{
            border-color: rgba(255,204,91,0.38);
            box-shadow: inset 0 0 0 1px rgba(255,204,91,0.10);
        }}
        .coach-spotlight-low {{
            border-color: rgba(95,255,141,0.24);
        }}
        .coach-details {{
            border-top: 1px solid rgba(121, 146, 193, 0.16);
            padding-top: 10px;
        }}
        .coach-details summary {{
            cursor: pointer;
            color: var(--cyan);
            font-size: 12px;
        }}
        .coach-details-copy {{
            margin-top: 8px;
            color: var(--muted);
            font-size: 11px;
            line-height: 1.5;
        }}
        .coach-inline-link {{
            display: inline-flex;
            margin: 4px 8px 0 0;
            color: var(--cyan);
            text-decoration: none;
            font-size: 11px;
        }}
        .coach-inline-empty {{
            color: var(--muted);
            font-size: 11px;
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
            .coach-spotlight-grid {{ grid-template-columns: 1fr; }}
        }}
        @media (max-width: 900px) {{
            .workspace {{ grid-template-columns: 1fr; }}
            .panel.full {{ grid-column: auto; }}
            .hero-grid {{ grid-template-columns: 1fr; }}
            .summary-grid {{ grid-template-columns: 1fr; }}
            .coach-subheader {{ margin: 0 12px; }}
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
                    <p>Execution Workspace for this bot.</p>
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
                <div class="side-label">Manual Stops</div>
                <div>{manual_stop_html}</div>
            </div>

            <div class="side-section">
                <div class="side-label">Feed States</div>
                <div>{retention_html}</div>
            </div>

            <div class="side-section">
                <div class="side-label">Overview</div>
                <div class="metric-grid">
                    <div class="metric-card">
                        <span>Daily P&amp;L</span>
                        <strong style="color:{pnl_color};">${completed_pnl:+,.2f}</strong>
                    </div>
                    <div class="metric-card">
                        <span>Open</span>
                        <strong>{bot["position_count"]}</strong>
                    </div>
                    <div class="metric-card">
                        <span>Closed</span>
                        <strong>{completed_count}</strong>
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
                <div class="panel-copy">{_render_page_nav(strategy_code, available_codes)}</div>
            </section>

            {listening_panel}
            {data_health_panel}
            {runner_status_panel}

            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>Open Positions</h3>
                        <div class="sub">Live bot, virtual, and broker quantities side by side.</div>
                    </div>
                    <span class="count">{bot["position_count"]}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Ticker</th><th style="text-align:right">Bot Qty<br>Entry Time</th><th style="text-align:right">Entry Price</th><th style="text-align:right">Virtual Qty<br>Avg Price</th><th style="text-align:right">Broker Qty<br>Current Price</th><th style="text-align:right">Open P&amp;L</th><th>Sync Status</th></tr></thead>
                        <tbody>{position_rows}</tbody>
                    </table>
                </div>
            </section>

            {completed_positions_panel}
            {live_trade_coach_panel}
            {trade_coach_panel}

            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>Order History</h3>
                        <div class="sub">Entry, scale, and exit orders with fill price, status, and reason.</div>
                    </div>
                    <span class="count accent">{order_count}</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Time</th><th>Ticker</th><th>Action</th><th>Side</th><th style="text-align:right">Qty</th><th style="text-align:right">Price</th><th>Status</th><th>Reason</th></tr></thead>
                        <tbody>{order_rows}</tbody>
                    </table>
                </div>
            </section>

            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>Decision Tape</h3>
                        <div class="sub">Recent entry checks and block reasons from the strategy runtime.</div>
                    </div>
                    <span class="count accent">{len(recent_decisions)}</span>
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
                        <div class="sub">Recent failed order events in a simple tape format.</div>
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

            <section class="panel full">
                <div class="panel-header">
                    <div>
                        <h3>TOS Parity</h3>
                        <div class="sub">{escape(str(tos_parity.get("summary", "TOS parity is only tracked for the 1-minute and TOS runtimes.")))}</div>
                    </div>
                    <span class="count accent">{len(tos_parity.get("snapshots", []))}</span>
                </div>
                <div class="panel-copy">{escape(", ".join(tos_parity.get("settings", [])) or "No parity settings loaded.")}</div>
                <div class="table-wrap">
                    <table>
                        <thead><tr><th>Ticker</th><th>Last Bar</th><th style="text-align:right">Close</th><th style="text-align:right">EMA9</th><th style="text-align:right">EMA20</th><th style="text-align:right">MACD</th><th style="text-align:right">Signal</th><th style="text-align:right">Hist</th><th style="text-align:right">VWAP</th><th>Flags</th></tr></thead>
                        <tbody>{tos_parity_rows}</tbody>
                    </table>
                </div>
            </section>
        </main>
    </div>
</body>
</html>"""


def _render_page_nav(active: str, available_codes: list[str]) -> str:
    links: list[str] = []
    for code, meta in _visible_bot_page_meta(available_codes):
        links.append(
            f'<a href="{meta["path"]}" class="{"active" if code == active else ""}">{escape(str(meta.get("nav_title", meta["title"])).replace(" Bot", ""))}</a>'
        )
    return (
        '<div class="nav-strip">'
        '<a href="/scanner/dashboard">Mai Tai Scanner</a>'
        + "".join(links)
        + '<a href="/coach/reviews">Trade Coach</a>'
        + '<a href="/">Mai Tai Control Plane</a>'
        + "</div>"
    )


def _render_trade_coach_review_nav(available_codes: list[str]) -> str:
    links = ['<a href="/scanner/dashboard">Mai Tai Scanner</a>']
    for _, meta in _visible_bot_page_meta(available_codes):
        links.append(
            f'<a href="{meta["path"]}">{escape(str(meta.get("nav_title", meta["title"])).replace(" Bot", ""))}</a>'
        )
    links.append('<a href="/coach/reviews" class="active">Trade Coach</a>')
    links.append('<a href="/">Mai Tai Control Plane</a>')
    return '<div class="nav-strip">' + "".join(links) + "</div>"


def _render_chip_cloud(items: list[str], *, variant: str = "", empty_text: str = "None") -> str:
    if not items:
        return f'<span style="color:#7b86a4;">{escape(empty_text)}</span>'
    class_name = f"pill-chip {variant}".strip()
    return "".join(f'<span class="{class_name}">{escape(str(item))}</span>' for item in items)


def _build_bot_symbol_action_html(
    strategy_code: str,
    *,
    symbols: list[str],
    manual_stop_symbols: set[str],
    open_symbols: set[str],
    pending_symbols: set[str],
    redirect_to: str,
    empty_text: str,
) -> str:
    if not symbols:
        return f'<span style="color:#7b86a4;">{escape(empty_text)}</span>'
    rendered: list[str] = []
    for symbol in symbols:
        variant = "live" if symbol in open_symbols else "amber" if symbol in pending_symbols else ""
        action = "resume" if symbol in manual_stop_symbols else "stop"
        label = "Resume" if action == "resume" else "Stop"
        color = "#5fff8d" if action == "resume" else "#ff6b6b"
        url = (
            f"/bot/symbol/{action}?strategy_code={quote(strategy_code)}"
            f"&symbol={quote(symbol)}&redirect_to={quote(redirect_to, safe='/')}"
        )
        rendered.append(
            '<div class="line-item" style="padding:8px 10px;">'
            f'<span class="pill-chip {variant}">{escape(symbol)}</span> '
            f'<a href="{escape(url)}" '
            f'style="color:{color};font-size:11px;padding:2px 6px;border:1px solid {color};'
            'border-radius:999px;text-decoration:none;display:inline-block;">'
            f"{escape(label)}</a></div>"
        )
    return "".join(rendered)


def _build_bot_manual_stop_html(
    strategy_code: str,
    manual_stop_symbols: list[str],
    *,
    redirect_to: str,
) -> str:
    if not manual_stop_symbols:
        return '<div class="line-item" style="color:#7b86a4;">No symbols manually stopped.</div>'
    rendered: list[str] = []
    for symbol in sorted({str(item).upper() for item in manual_stop_symbols if str(item).strip()}):
        url = (
            f"/bot/symbol/resume?strategy_code={quote(strategy_code)}"
            f"&symbol={quote(symbol)}&redirect_to={quote(redirect_to, safe='/')}"
        )
        rendered.append(
            '<div class="line-item" style="padding:8px 10px;">'
            f'<span class="pill-chip danger">{escape(symbol)}</span> '
            f'<a href="{escape(url)}" '
            'style="color:#5fff8d;font-size:11px;padding:2px 6px;border:1px solid #5fff8d;'
            'border-radius:999px;text-decoration:none;display:inline-block;">Resume</a></div>'
        )
    return "".join(rendered)


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


def _build_order_history_rows(recent_orders: list[dict[str, Any]], recent_fills: list[dict[str, Any]]) -> tuple[str, int]:
    orders = list(recent_orders)
    if not orders:
        return (
            '<tr><td colspan="8" style="text-align:center;color:#7b86a4;padding:15px;">No orders yet</td></tr>',
            0,
        )
    fills_by_symbol_side: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in sorted(recent_fills, key=lambda row: _parse_et_timestamp(str(row.get("filled_at", "") or ""))):
        symbol = str(item.get("symbol", "")).upper()
        side = str(item.get("side", "")).lower()
        if symbol and side:
            fills_by_symbol_side.setdefault((symbol, side), []).append(item)
    row_models: list[dict[str, str]] = []
    for item in sorted(orders, key=lambda row: _parse_et_timestamp(str(row.get("updated_at", "") or ""))):
        side_color = "#00c853" if item["side"] == "buy" else "#ff1744"
        intent_type = str(item.get("intent_type", "") or "").lower()
        status = str(item.get("status", "") or "")
        symbol = str(item.get("symbol", "")).upper()
        side = str(item.get("side", "")).lower()
        fill_price = "-"
        if status.lower() == "filled":
            fill_queue = fills_by_symbol_side.get((symbol, side), [])
            if fill_queue:
                fill_price = _fmt_money(_as_float(fill_queue.pop(0).get("price")))
        row_models.append(
            {
                "updated_at": str(item["updated_at"]),
                "symbol": symbol,
                "intent_type": intent_type.upper() or "-",
                "side": str(item["side"].upper()),
                "side_color": side_color,
                "quantity": str(item["quantity"]),
                "fill_price": fill_price,
                "status": str(item["status"].upper()),
                "reason": _display_order_reason(item),
            }
        )
    rows: list[str] = []
    for row in sorted(row_models, key=lambda item: _parse_et_timestamp(item["updated_at"]), reverse=True):
        rows.append(
            f"""<tr>
            <td>{escape(row["updated_at"])}</td>
            <td><strong>{escape(row["symbol"])}</strong></td>
            <td>{escape(row["intent_type"])}</td>
            <td style="color:{row["side_color"]};font-weight:bold;">{escape(row["side"])}</td>
            <td style="text-align:right">{escape(row["quantity"])}</td>
            <td style="text-align:right">{row["fill_price"]}</td>
            <td>{escape(row["status"])}</td>
            <td>{escape(row["reason"])}</td>
        </tr>"""
        )
    return "".join(rows), len(orders)


def _trade_coach_verdict_color(verdict: str) -> str:
    normalized = str(verdict or "").strip().lower()
    if normalized == "good":
        return "#00c853"
    if normalized == "bad":
        return "#ff5252"
    if normalized == "mixed":
        return "#ffcc5b"
    return "#7b86a4"


def _trade_coach_tag_list(items: list[Any]) -> str:
    values = [str(item).strip() for item in items if str(item).strip()]
    if not values:
        return "-"
    return " | ".join(values)


def _trade_coach_trade_window_display(review: dict[str, Any]) -> tuple[str, str]:
    entry_text = str(review.get("entry_time", "") or "").strip()
    exit_text = str(review.get("exit_time", "") or "").strip()
    if not entry_text and not exit_text:
        reviewed_text = str(review.get("created_at", "") or "").strip()
        return reviewed_text or "-", "reviewed"

    entry_dt = _parse_et_timestamp(entry_text)
    exit_dt = _parse_et_timestamp(exit_text)
    valid_entry = entry_dt.year > 1
    valid_exit = exit_dt.year > 1
    if valid_entry and valid_exit:
        if entry_dt.date() == exit_dt.date():
            return (
                entry_dt.strftime("%Y-%m-%d"),
                f"{entry_dt.strftime('%I:%M:%S %p ET')} -> {exit_dt.strftime('%I:%M:%S %p ET')}",
            )
        return (
            entry_dt.strftime("%Y-%m-%d %I:%M:%S %p ET"),
            exit_dt.strftime("%Y-%m-%d %I:%M:%S %p ET"),
        )
    if valid_exit:
        return exit_dt.strftime("%Y-%m-%d"), f"close {exit_dt.strftime('%I:%M:%S %p ET')}"
    if valid_entry:
        return entry_dt.strftime("%Y-%m-%d"), f"open {entry_dt.strftime('%I:%M:%S %p ET')}"
    return (entry_text or exit_text or "-", exit_text if entry_text and exit_text else "trade window")


def _trade_coach_trade_timestamp(review: dict[str, Any]) -> datetime:
    exit_dt = _parse_et_timestamp(str(review.get("exit_time", "") or ""))
    if exit_dt.year > 1:
        return exit_dt.astimezone(UTC)
    entry_dt = _parse_et_timestamp(str(review.get("entry_time", "") or ""))
    if entry_dt.year > 1:
        return entry_dt.astimezone(UTC)
    created_dt = _parse_et_timestamp(str(review.get("created_at", "") or ""))
    if created_dt.year > 1:
        return created_dt.astimezone(UTC)
    return datetime.min.replace(tzinfo=UTC)


def _enrich_trade_coach_reviews(
    reviews: list[dict[str, Any]],
    bots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    by_strategy: dict[str, dict[str, Any]] = {}
    for bot in bots:
        strategy = str(bot.get("strategy_code", "") or "")
        account_name = str(bot.get("account_name", "") or "")
        if strategy:
            by_strategy[strategy] = bot
        if strategy and account_name:
            by_pair[(strategy, account_name)] = bot

    enriched: list[dict[str, Any]] = []
    for review in reviews:
        item = dict(review)
        strategy = str(item.get("strategy_code", "") or "")
        account_name = str(item.get("broker_account_name", "") or "")
        bot = by_pair.get((strategy, account_name)) or by_strategy.get(strategy) or {}
        item["display_name"] = str(
            bot.get("display_name", "")
            or item.get("display_name")
            or strategy
            or "-"
        )
        item["account_display_name"] = str(
            bot.get("account_display_name", "")
            or item.get("account_display_name")
            or account_name
            or "-"
        )
        enriched.append(item)
    return enriched


def _trade_coach_review_interval_secs(review: dict[str, Any]) -> int:
    strategy_code = str(review.get("strategy_code", "") or "").strip().lower()
    if "1m" in strategy_code or strategy_code == "tos":
        return 60
    return 30


def _trade_coach_price_band(value: float) -> str:
    if value < 1:
        return "sub-$1"
    if value < 2:
        return "$1-$2"
    if value < 5:
        return "$2-$5"
    if value < 10:
        return "$5-$10"
    return "$10+"


def _trade_coach_volume_band(value: float) -> str:
    if value < 100_000:
        return "thin"
    if value < 500_000:
        return "light"
    if value < 1_500_000:
        return "active"
    return "heavy"


def _trade_coach_volatility_band(value: float) -> str:
    if value < 1.0:
        return "calm"
    if value < 3.0:
        return "active"
    if value < 6.0:
        return "hot"
    return "explosive"


def _trade_coach_momentum_band(value: float) -> str:
    if value < 0:
        return "fading"
    if value < 5:
        return "steady"
    if value < 15:
        return "strong"
    return "squeeze"


def _build_trade_coach_regime_profile(
    review: dict[str, Any],
    bars: list[StrategyBarHistory],
    *,
    entry_time: datetime,
    exit_time: datetime,
    interval_secs: int,
) -> dict[str, Any]:
    if not bars:
        return {}
    normalized_bars: list[StrategyBarHistory] = []
    for bar in bars:
        if bar.bar_time.tzinfo is None:
            bar.bar_time = bar.bar_time.replace(tzinfo=UTC)
        normalized_bars.append(bar)
    pre_start = entry_time - timedelta(minutes=5)
    trade_end = exit_time + timedelta(seconds=max(interval_secs, 30))
    pre_bars = [bar for bar in normalized_bars if pre_start <= bar.bar_time < entry_time][-10:]
    trade_bars = [bar for bar in normalized_bars if entry_time <= bar.bar_time <= trade_end]
    active_bars = trade_bars or pre_bars[-3:]
    if not active_bars:
        return {}

    entry_price = _as_float(review.get("entry_price"))
    if entry_price <= 0:
        reference_bar = trade_bars[0] if trade_bars else pre_bars[-1]
        entry_price = _as_float(reference_bar.open_price or reference_bar.close_price)

    lead_bars = pre_bars if pre_bars else active_bars
    avg_pre_entry_volume = (
        sum(max(int(bar.volume or 0), 0) for bar in lead_bars) / len(lead_bars) if lead_bars else 0.0
    )
    avg_range_pct = (
        sum(
            ((_as_float(bar.high_price) - _as_float(bar.low_price)) / max(_as_float(bar.open_price), 0.01)) * 100.0
            for bar in active_bars
        )
        / len(active_bars)
        if active_bars
        else 0.0
    )
    earliest_bar = lead_bars[0] if lead_bars else active_bars[0]
    first_reference_price = max(_as_float(earliest_bar.open_price), 0.01)
    latest_pretrade_bar = trade_bars[0] if trade_bars else active_bars[-1]
    pre_entry_change_pct = ((_as_float(latest_pretrade_bar.close_price) - first_reference_price) / first_reference_price) * 100.0
    high_water = max(_as_float(bar.high_price) for bar in active_bars)
    low_water = min(_as_float(bar.low_price) for bar in active_bars)
    trade_range_pct = ((high_water - low_water) / max(entry_price, 0.01)) * 100.0
    avg_trade_count = (
        sum(max(int(bar.trade_count or 0), 0) for bar in active_bars) / len(active_bars) if active_bars else 0.0
    )
    duration_secs = max(int((exit_time - entry_time).total_seconds()), 0)

    price_band = _trade_coach_price_band(entry_price)
    volume_band = _trade_coach_volume_band(avg_pre_entry_volume)
    volatility_band = _trade_coach_volatility_band(avg_range_pct)
    momentum_band = _trade_coach_momentum_band(pre_entry_change_pct)
    return {
        "label": f"{price_band} price | {volume_band} volume | {volatility_band} volatility | {momentum_band} momentum",
        "price_band": price_band,
        "volume_band": volume_band,
        "volatility_band": volatility_band,
        "momentum_band": momentum_band,
        "entry_price": round(entry_price, 4),
        "avg_pre_entry_volume": round(avg_pre_entry_volume, 0),
        "avg_range_pct": round(avg_range_pct, 2),
        "pre_entry_change_pct": round(pre_entry_change_pct, 2),
        "trade_range_pct": round(trade_range_pct, 2),
        "avg_trade_count": round(avg_trade_count, 0),
        "duration_secs": duration_secs,
        "bar_count": len(active_bars),
    }


def _build_live_trade_coach_regime_profile(
    *,
    symbol: str,
    bars: list[StrategyBarHistory],
    interval_secs: int,
) -> dict[str, Any]:
    if not bars:
        return {}
    normalized_bars: list[StrategyBarHistory] = []
    for bar in bars:
        if bar.bar_time.tzinfo is None:
            bar.bar_time = bar.bar_time.replace(tzinfo=UTC)
        normalized_bars.append(bar)
    active_bars = normalized_bars[-min(len(normalized_bars), 6) :]
    if not active_bars:
        return {}
    lead_bars = normalized_bars[: -len(active_bars)][-10:] if len(normalized_bars) > len(active_bars) else []
    reference_bar = active_bars[0]
    entry_price = max(_as_float(reference_bar.open_price or reference_bar.close_price), 0.01)
    lead_source = lead_bars or active_bars
    avg_pre_entry_volume = (
        sum(max(_as_float(bar.volume), 0.0) for bar in lead_source) / max(len(lead_source), 1)
    )
    avg_range_pct = (
        sum(
            max(
                (_as_float(bar.high_price) - _as_float(bar.low_price))
                / max(_as_float(bar.close_price or bar.open_price), 0.01)
                * 100.0,
                0.0,
            )
            for bar in active_bars
        )
        / max(len(active_bars), 1)
    )
    first_close = max(_as_float(active_bars[0].close_price or active_bars[0].open_price), 0.01)
    last_close = max(_as_float(active_bars[-1].close_price or active_bars[-1].open_price), 0.01)
    pre_entry_change_pct = ((last_close - first_close) / first_close) * 100.0
    trade_range_pct = (
        (max(_as_float(bar.high_price) for bar in active_bars) - min(_as_float(bar.low_price) for bar in active_bars))
        / entry_price
    ) * 100.0
    avg_trade_count = (
        sum(max(_as_float(bar.trade_count), 0.0) for bar in active_bars) / max(len(active_bars), 1)
    )
    duration_secs = max(len(active_bars), 1) * max(interval_secs, 1)
    price_band = _trade_coach_price_band(entry_price)
    volume_band = _trade_coach_volume_band(avg_pre_entry_volume)
    volatility_band = _trade_coach_volatility_band(avg_range_pct)
    momentum_band = _trade_coach_momentum_band(pre_entry_change_pct)
    return {
        "symbol": symbol,
        "label": f"{price_band} price | {volume_band} volume | {volatility_band} volatility | {momentum_band} momentum",
        "price_band": price_band,
        "volume_band": volume_band,
        "volatility_band": volatility_band,
        "momentum_band": momentum_band,
        "entry_price": round(entry_price, 4),
        "avg_pre_entry_volume": round(avg_pre_entry_volume, 0),
        "avg_range_pct": round(avg_range_pct, 2),
        "pre_entry_change_pct": round(pre_entry_change_pct, 2),
        "trade_range_pct": round(trade_range_pct, 2),
        "avg_trade_count": round(avg_trade_count, 0),
        "duration_secs": duration_secs,
        "bar_count": len(active_bars),
    }


def _apply_trade_coach_regime_profiles(
    reviews: list[dict[str, Any]],
    regime_profiles: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not regime_profiles:
        return list(reviews)
    enriched: list[dict[str, Any]] = []
    for item in reviews:
        review = dict(item)
        cycle_key = str(review.get("cycle_key", "") or "")
        review["regime_profile"] = dict(regime_profiles.get(cycle_key, {}) or {})
        enriched.append(review)
    return enriched


def _filter_trade_coach_reviews(
    reviews: list[dict[str, Any]],
    *,
    strategy_code: str | None = None,
    verdict: str | None = None,
    coaching_focus: str | None = None,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    strategy_filter = str(strategy_code or "").strip()
    verdict_filter = str(verdict or "").strip().lower()
    focus_filter = str(coaching_focus or "").strip().lower()
    symbol_filter = str(symbol or "").strip().upper()

    filtered: list[dict[str, Any]] = []
    for item in reviews:
        if strategy_filter and str(item.get("strategy_code", "") or "") != strategy_filter:
            continue
        if verdict_filter and str(item.get("verdict", "") or "").strip().lower() != verdict_filter:
            continue
        if focus_filter and str(item.get("coaching_focus", "") or "").strip().lower() != focus_filter:
            continue
        if symbol_filter and str(item.get("symbol", "") or "").strip().upper() != symbol_filter:
            continue
        trade_timestamp = _trade_coach_trade_timestamp(item)
        if start is not None and trade_timestamp < start:
            continue
        if end is not None and trade_timestamp >= end:
            continue
        filtered.append(item)

    return sorted(
        filtered,
        key=_trade_coach_trade_timestamp,
        reverse=True,
    )


def _trade_coach_live_advisory_symbols(
    bot: dict[str, Any],
    recent_decisions: list[dict[str, Any]],
) -> list[str]:
    ordered: list[str] = []

    def add(symbol: object) -> None:
        normalized = str(symbol or "").strip().upper()
        if normalized and normalized not in ordered:
            ordered.append(normalized)

    for item in bot.get("positions", []):
        add(item.get("ticker") or item.get("symbol"))
    for symbol in bot.get("pending_open_symbols", []):
        add(symbol)
    for symbol in bot.get("pending_close_symbols", []):
        add(symbol)
    for item in recent_decisions:
        add(item.get("symbol"))
    for symbol in bot.get("watchlist", []):
        add(symbol)
    return ordered[:10]


def _trade_coach_current_path_for_symbol(
    bot: dict[str, Any],
    symbol: str,
    recent_decisions: list[dict[str, Any]],
) -> str:
    normalized_symbol = str(symbol or "").strip().upper()
    for item in recent_decisions:
        if str(item.get("symbol", "") or "").strip().upper() != normalized_symbol:
            continue
        path = str(item.get("path", "") or "").strip().upper()
        if path:
            return path
    for item in bot.get("positions", []):
        if str(item.get("ticker") or item.get("symbol") or "").strip().upper() != normalized_symbol:
            continue
        path = str(item.get("path", "") or "").strip().upper()
        if path:
            return path
    for item in bot.get("recent_orders", []):
        if str(item.get("symbol", "") or "").strip().upper() != normalized_symbol:
            continue
        path = str(item.get("path", "") or "").strip().upper()
        if path:
            return path
    return ""


def _trade_coach_live_advisory_for_symbol(
    *,
    bot: dict[str, Any],
    symbol: str,
    recent_decisions: list[dict[str, Any]],
    bot_reviews: list[dict[str, Any]],
    live_regime_profiles: dict[str, dict[str, Any]],
    path_patterns: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    latest_decision = next(
        (
            item
            for item in recent_decisions
            if str(item.get("symbol", "") or "").strip().upper() == normalized_symbol
        ),
        {},
    )
    current_path = _trade_coach_current_path_for_symbol(bot, normalized_symbol, recent_decisions)
    same_symbol_reviews = [
        item
        for item in bot_reviews
        if str(item.get("symbol", "") or "").strip().upper() == normalized_symbol
    ]
    same_symbol_summary = _trade_coach_history_summary(same_symbol_reviews)
    live_regime = dict(live_regime_profiles.get(normalized_symbol, {}) or {})
    target_review = {
        "strategy_code": bot.get("strategy_code", ""),
        "broker_account_name": bot.get("account_name", ""),
        "symbol": normalized_symbol,
        "path": current_path,
        "regime_profile": live_regime,
    }
    same_path_reviews = (
        _trade_coach_related_reviews(target_review, bot_reviews, mode="path", limit=4)
        if current_path
        else []
    )
    same_path_summary = _trade_coach_history_summary(same_path_reviews)
    similar_regime_reviews = (
        _trade_coach_similar_regime_reviews(target_review, bot_reviews, limit=4) if live_regime else []
    )
    similar_regime_summary = _trade_coach_similarity_summary(similar_regime_reviews)
    path_signal = dict(path_patterns.get(current_path, {}) or {}) if current_path else {}

    score = float(path_signal.get("caution_score", 0.0))
    reasons: list[str] = []
    if path_signal:
        reasons.append(
            f"path {current_path} has {path_signal.get('caution_label', 'low')} caution "
            f"({int(path_signal.get('count', 0))} reviewed trades, avg {float(path_signal.get('avg_pnl_pct', 0.0)):+.1f}%)"
        )
    if similar_regime_summary["count"] >= 2:
        avg_similarity = float(similar_regime_summary["avg_similarity_score"])
        avg_regime_pnl = float(similar_regime_summary["avg_pnl_pct"])
        if avg_regime_pnl < 0:
            score += min(abs(avg_regime_pnl) * 8.0, 24.0)
        if similar_regime_summary["bad"]:
            score += min(similar_regime_summary["bad"] * 12.0, 24.0)
        if similar_regime_summary["mixed"] >= max(similar_regime_summary["good"], 1):
            score += 8.0
        reasons.append(
            f"similar regime {similar_regime_summary['count']} reviews, avg similarity {avg_similarity:.0f}, avg {avg_regime_pnl:+.1f}%"
        )
    if same_symbol_summary["count"] >= 2 and float(same_symbol_summary["avg_pnl_pct"]) < 0:
        score += min(abs(float(same_symbol_summary["avg_pnl_pct"])) * 5.0, 15.0)
        reasons.append(
            f"{normalized_symbol} history avg {float(same_symbol_summary['avg_pnl_pct']):+.1f}% across {same_symbol_summary['count']} reviews"
        )

    caution_label = "high" if score >= 55 else "medium" if score >= 28 else "low"
    severity_caption = (
        "Coach memory sees repeated weakness in similar reviewed trades."
        if caution_label == "high"
        else "Coach memory is mixed enough that the setup needs extra confirmation."
        if caution_label == "medium"
        else "Coach memory is mostly neutral here, so treat this as context rather than a green light."
    )
    if caution_label == "high":
        message = (
            f"{current_path or normalized_symbol} has struggled in reviewed history like this; require tighter confirmation before trusting it."
        )
        action = "Wait for cleaner confirmation, smaller size, or faster exits."
    elif caution_label == "medium":
        message = (
            f"Reviewed history is mixed for {current_path or normalized_symbol}; verify tape quality before treating it as a clean go."
        )
        action = "Check spread, follow-through, and first pullback quality before committing."
    else:
        message = (
            f"No strong caution signal is standing out for {normalized_symbol} from reviewed history yet."
        )
        action = "Use the live setup rules normally and keep validating it against fresh examples."

    reference_reviews: list[dict[str, Any]] = []
    seen_cycle_keys: set[str] = set()

    def add_reference(review: dict[str, Any], bucket: str) -> None:
        cycle_key = str(review.get("cycle_key", "") or "").strip()
        if not cycle_key or cycle_key in seen_cycle_keys:
            return
        seen_cycle_keys.add(cycle_key)
        reference_reviews.append(
            {
                "cycle_key": cycle_key,
                "bucket": bucket,
                "symbol": str(review.get("symbol", "") or "-"),
                "path": str(review.get("path", "") or "-"),
                "verdict": str(review.get("verdict", "") or "-"),
                "created_at": str(review.get("created_at", "") or "-"),
            }
        )

    for review in similar_regime_reviews[:2]:
        add_reference(review, "regime match")
    for review in same_path_reviews[:2]:
        add_reference(review, "path match")
    for review in same_symbol_reviews[:2]:
        add_reference(review, "same symbol")

    live_status = str(latest_decision.get("status", "") or "watching").strip().lower() or "watching"
    live_reason = str(latest_decision.get("reason", "") or "live in bot; waiting for a clearer setup").strip()
    live_timestamp = str(latest_decision.get("last_bar_at") or bot.get("last_tick_at", {}).get(normalized_symbol) or "").strip()
    return {
        "symbol": normalized_symbol,
        "current_path": current_path or "-",
        "live_status": live_status,
        "live_reason": live_reason or "-",
        "live_timestamp": live_timestamp or "-",
        "same_symbol_summary": same_symbol_summary,
        "same_path_summary": same_path_summary,
        "similar_regime_summary": similar_regime_summary,
        "regime_profile": live_regime,
        "path_signal": path_signal,
        "caution_score": round(score, 1),
        "caution_label": caution_label,
        "severity_caption": severity_caption,
        "message": message,
        "action": action,
        "reasons": reasons[:3],
        "reference_reviews": reference_reviews[:3],
    }


def _build_trade_coach_live_advisories(
    *,
    bot: dict[str, Any],
    recent_decisions: list[dict[str, Any]],
    all_reviews: list[dict[str, Any]],
    live_regime_profiles: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    strategy_code = str(bot.get("strategy_code", "") or "")
    account_name = str(bot.get("account_name", "") or "")
    bot_reviews = [
        item
        for item in all_reviews
        if str(item.get("strategy_code", "") or "") == strategy_code
        and str(item.get("broker_account_name", "") or "") == account_name
    ]
    if not bot_reviews:
        return []
    path_patterns = {
        str(item.get("pattern_key", "") or ""): item
        for item in _trade_coach_pattern_scoreboard(bot_reviews, mode="path", limit=50)
        if str(item.get("pattern_key", "") or "").strip()
    }
    advisories = [
        _trade_coach_live_advisory_for_symbol(
            bot=bot,
            symbol=symbol,
            recent_decisions=recent_decisions,
            bot_reviews=bot_reviews,
            live_regime_profiles=live_regime_profiles,
            path_patterns=path_patterns,
        )
        for symbol in _trade_coach_live_advisory_symbols(bot, recent_decisions)
    ]
    advisories = [
        item
        for item in advisories
        if item["current_path"] != "-"
        or item["same_symbol_summary"]["count"] > 0
        or item["similar_regime_summary"]["count"] > 0
    ]
    return sorted(
        advisories,
        key=lambda item: (
            2 if item["caution_label"] == "high" else 1 if item["caution_label"] == "medium" else 0,
            float(item.get("caution_score", 0.0)),
            _parse_et_timestamp(str(item.get("live_timestamp", "") or "")),
        ),
        reverse=True,
    )[:8]


def _trade_coach_review_summary(reviews: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "good": sum(1 for item in reviews if str(item.get("verdict", "") or "").strip().lower() == "good"),
        "mixed": sum(1 for item in reviews if str(item.get("verdict", "") or "").strip().lower() == "mixed"),
        "bad": sum(1 for item in reviews if str(item.get("verdict", "") or "").strip().lower() == "bad"),
        "manual_review": sum(1 for item in reviews if bool(item.get("should_review_manually"))),
        "should_skip": sum(1 for item in reviews if not bool(item.get("should_have_traded"))),
    }


def _find_trade_coach_review(reviews: list[dict[str, Any]], cycle_key: str) -> dict[str, Any] | None:
    target = str(cycle_key or "").strip()
    if not target:
        return None
    return next(
        (
            item
            for item in reviews
            if str(item.get("cycle_key", "") or "").strip() == target
        ),
        None,
    )


def _trade_coach_review_priority(review: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    score = 0
    verdict = str(review.get("verdict", "") or "").strip().lower()
    if verdict == "bad":
        score += 90
        reasons.append("coach marked this trade bad")
    elif verdict == "mixed":
        score += 55
        reasons.append("coach marked this trade mixed")
    if bool(review.get("should_review_manually")):
        score += 80
        reasons.append("coach explicitly requested manual review")
    if not bool(review.get("should_have_traded", True)):
        score += 75
        reasons.append("coach says this trade should have been skipped")

    for field_name, label, threshold, weight in (
        ("setup_quality", "setup quality is weak", 0.75, 24),
        ("execution_quality", "execution quality is weak", 0.78, 28),
        ("outcome_quality", "outcome quality is weak", 0.65, 18),
    ):
        value = _as_float(review.get(field_name))
        if value and value < threshold:
            score += weight
            reasons.append(f"{label} ({value:.2f})")

    if list(review.get("rule_violations", []) or []):
        score += 32
        reasons.append("rule violations were recorded")

    pnl_pct = _as_float(review.get("pnl_pct"))
    if pnl_pct < 0:
        score += 12
        reasons.append(f"closed red at {pnl_pct:+.1f}%")

    return {
        "score": score,
        "reasons": reasons or ["healthy review with no urgent follow-up"],
        "label": "high" if score >= 90 else "medium" if score >= 45 else "low",
    }


def _build_trade_coach_review_queue(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queued: list[dict[str, Any]] = []
    for item in reviews:
        priority = _trade_coach_review_priority(item)
        if priority["score"] <= 0:
            continue
        queued.append(
            {
                **item,
                "priority_score": int(priority["score"]),
                "priority_label": str(priority["label"]),
                "priority_reasons": list(priority["reasons"]),
            }
        )
    return sorted(
        queued,
        key=lambda row: (
            int(row.get("priority_score", 0)),
            _trade_coach_trade_timestamp(row),
        ),
        reverse=True,
    )


def _trade_coach_history_summary(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    pnl_values = [_as_float(item.get("pnl_pct")) for item in reviews]
    return {
        "count": len(reviews),
        "good": sum(1 for item in reviews if str(item.get("verdict", "") or "").strip().lower() == "good"),
        "mixed": sum(1 for item in reviews if str(item.get("verdict", "") or "").strip().lower() == "mixed"),
        "bad": sum(1 for item in reviews if str(item.get("verdict", "") or "").strip().lower() == "bad"),
        "avg_pnl_pct": (sum(pnl_values) / len(pnl_values)) if pnl_values else 0.0,
    }


def _trade_coach_similarity_summary(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _trade_coach_history_summary(reviews)
    similarity_scores = [int(item.get("regime_similarity_score", 0) or 0) for item in reviews]
    summary["avg_similarity_score"] = (
        sum(similarity_scores) / len(similarity_scores) if similarity_scores else 0.0
    )
    return summary


def _trade_coach_pattern_key(review: dict[str, Any], *, mode: str) -> str:
    if mode == "path":
        return str(review.get("path", "") or "").strip() or "Unlabeled Path"
    regime = dict(review.get("regime_profile", {}) or {})
    return str(regime.get("label", "") or "").strip() or "Unknown Regime"


def _trade_coach_pattern_type_label(mode: str) -> str:
    return "path" if mode == "path" else "regime"


def _trade_coach_pattern_scoreboard(
    reviews: list[dict[str, Any]],
    *,
    mode: str,
    limit: int = 8,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in reviews:
        key = _trade_coach_pattern_key(item, mode=mode)
        if not key or key == "Unknown Regime":
            if mode == "regime":
                continue
        grouped.setdefault(key, []).append(item)

    rows: list[dict[str, Any]] = []
    for key, items in grouped.items():
        summary = _trade_coach_history_summary(items)
        avg_setup = sum(_as_float(entry.get("setup_quality")) for entry in items) / len(items)
        avg_execution = sum(_as_float(entry.get("execution_quality")) for entry in items) / len(items)
        avg_outcome = sum(_as_float(entry.get("outcome_quality")) for entry in items) / len(items)
        manual_review_count = sum(1 for entry in items if bool(entry.get("should_review_manually")))
        should_skip_count = sum(1 for entry in items if not bool(entry.get("should_have_traded", True)))
        score = 0.0
        if len(items) >= 2 and summary["avg_pnl_pct"] < 0:
            score += min(abs(summary["avg_pnl_pct"]) * 8, 28)
        if should_skip_count:
            score += min(should_skip_count * 25, 40)
        if manual_review_count:
            score += min(manual_review_count * 10, 24)
        if summary["bad"]:
            score += min(summary["bad"] * 18, 36)
        if summary["mixed"] and summary["mixed"] >= max(summary["good"], 1):
            score += 12
        elif summary["mixed"]:
            score += min(summary["mixed"] * 5, 10)
        if avg_outcome < 0.55:
            score += 18
        if avg_execution < 0.72:
            score += 12
        if avg_setup < 0.75:
            score += 10
        caution_label = "high" if score >= 55 else "medium" if score >= 30 else "low"
        rows.append(
            {
                "pattern_type": _trade_coach_pattern_type_label(mode),
                "pattern_key": key,
                "count": len(items),
                "avg_pnl_pct": summary["avg_pnl_pct"],
                "good": summary["good"],
                "mixed": summary["mixed"],
                "bad": summary["bad"],
                "manual_review_count": manual_review_count,
                "should_skip_count": should_skip_count,
                "avg_setup_quality": avg_setup,
                "avg_execution_quality": avg_execution,
                "avg_outcome_quality": avg_outcome,
                "caution_score": round(score, 1),
                "caution_label": caution_label,
            }
        )

    return sorted(
        rows,
        key=lambda row: (
            float(row.get("caution_score", 0.0)),
            int(row.get("count", 0)),
        ),
        reverse=True,
    )[:limit]


def _trade_coach_pattern_signals(reviews: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    path_patterns = _trade_coach_pattern_scoreboard(reviews, mode="path", limit=max(limit, 12))
    regime_patterns = _trade_coach_pattern_scoreboard(reviews, mode="regime", limit=max(limit, 12))
    signals: list[dict[str, Any]] = []
    for item in path_patterns + regime_patterns:
        reasons: list[str] = []
        avg_pnl_pct = float(item.get("avg_pnl_pct", 0.0))
        if avg_pnl_pct < 0:
            reasons.append(f"avg P&L {avg_pnl_pct:+.1f}%")
        if int(item.get("should_skip_count", 0)) > 0:
            reasons.append(f"{int(item.get('should_skip_count', 0))} coach skip flags")
        if int(item.get("manual_review_count", 0)) > 0:
            reasons.append(f"{int(item.get('manual_review_count', 0))} manual reviews")
        if float(item.get("avg_outcome_quality", 0.0)) < 0.55:
            reasons.append(f"outcome quality {float(item.get('avg_outcome_quality', 0.0)):.2f}")
        if float(item.get("avg_execution_quality", 0.0)) < 0.72:
            reasons.append(f"execution quality {float(item.get('avg_execution_quality', 0.0)):.2f}")
        message = (
            "Recent reviewed trades in this pattern have been weak; treat new entries with tighter confirmation."
            if str(item.get("caution_label", "")) == "high"
            else "Pattern is mixed lately; review context before trusting it."
            if str(item.get("caution_label", "")) == "medium"
            else "Pattern is holding up better than the weak groups in this filter window."
        )
        signals.append(
            {
                **item,
                "reasons": reasons or ["pattern remains stable in this filter window"],
                "message": message,
            }
        )
    sorted_signals = sorted(
        signals,
        key=lambda row: (
            2 if str(row.get("caution_label", "")) == "high" else 1 if str(row.get("caution_label", "")) == "medium" else 0,
            float(row.get("caution_score", 0.0)),
            int(row.get("count", 0)),
        ),
        reverse=True,
    )
    caution_signals = [row for row in sorted_signals if str(row.get("caution_label", "")) != "low"]
    return caution_signals[:limit] if caution_signals else sorted_signals[:limit]


def _trade_coach_operator_guidance(pattern_signals: list[dict[str, Any]], *, limit: int = 4) -> list[dict[str, Any]]:
    guidance: list[dict[str, Any]] = []
    for item in pattern_signals:
        caution = str(item.get("caution_label", "") or "low")
        pattern_type = str(item.get("pattern_type", "") or "pattern")
        pattern_key = str(item.get("pattern_key", "") or "unknown")
        reasons = list(item.get("reasons", []) or [])
        avg_pnl_pct = float(item.get("avg_pnl_pct", 0.0))
        if caution == "high":
            title = f"Be selective with {pattern_type} {pattern_key}"
            action = "Require tighter confirmation, smaller size, or faster exits until this group improves."
        elif caution == "medium":
            title = f"Watch {pattern_type} {pattern_key}"
            action = "Review tape quality and recent examples before treating this pattern as fully trusted."
        else:
            title = f"{pattern_type.title()} {pattern_key} is relatively stable"
            action = "No special caution signal yet, but keep validating against fresh trades."
        guidance.append(
            {
                "title": title,
                "pattern_type": pattern_type,
                "pattern_key": pattern_key,
                "caution_label": caution,
                "summary": f"{int(item.get('count', 0))} reviewed trades, avg P&L {avg_pnl_pct:+.1f}%",
                "action": action,
                "reasons": reasons[:3],
            }
        )
    return guidance[:limit]


def _trade_coach_related_reviews(
    review: dict[str, Any],
    all_reviews: list[dict[str, Any]],
    *,
    mode: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    cycle_key = str(review.get("cycle_key", "") or "")
    strategy_code = str(review.get("strategy_code", "") or "")
    account_name = str(review.get("broker_account_name", "") or "")
    symbol = str(review.get("symbol", "") or "")
    path = str(review.get("path", "") or "")
    related: list[dict[str, Any]] = []
    for item in all_reviews:
        if str(item.get("cycle_key", "") or "") == cycle_key:
            continue
        if str(item.get("strategy_code", "") or "") != strategy_code:
            continue
        if str(item.get("broker_account_name", "") or "") != account_name:
            continue
        if mode == "path" and path and str(item.get("path", "") or "") != path:
            continue
        if mode == "symbol" and symbol and str(item.get("symbol", "") or "") != symbol:
            continue
        related.append(item)
    return sorted(
        related,
        key=lambda row: _parse_et_timestamp(str(row.get("created_at", "") or "")),
        reverse=True,
    )[:limit]


def _trade_coach_regime_similarity(
    target_review: dict[str, Any],
    candidate_review: dict[str, Any],
) -> dict[str, Any]:
    target = dict(target_review.get("regime_profile", {}) or {})
    candidate = dict(candidate_review.get("regime_profile", {}) or {})
    if not target or not candidate:
        return {"score": 0, "label": "low", "reasons": []}

    score = 0
    reasons: list[str] = []
    for key, weight, label in (
        ("price_band", 24, "price band"),
        ("volume_band", 22, "volume regime"),
        ("volatility_band", 22, "volatility regime"),
        ("momentum_band", 18, "pre-entry momentum"),
    ):
        value = str(target.get(key, "") or "")
        candidate_value = str(candidate.get(key, "") or "")
        if value and value == candidate_value:
            score += weight
            reasons.append(f"same {label}")

    if str(target_review.get("path", "") or "") and str(target_review.get("path", "") or "") == str(candidate_review.get("path", "") or ""):
        score += 10
        reasons.append("same path")

    target_entry_price = max(_as_float(target.get("entry_price")), 0.01)
    candidate_entry_price = max(_as_float(candidate.get("entry_price")), 0.01)
    price_gap_pct = abs(candidate_entry_price - target_entry_price) / target_entry_price * 100.0
    if price_gap_pct <= 10:
        score += 10
        reasons.append("entry price within 10%")
    elif price_gap_pct <= 25:
        score += 6
        reasons.append("entry price within 25%")

    target_volume = max(_as_float(target.get("avg_pre_entry_volume")), 0.0)
    candidate_volume = max(_as_float(candidate.get("avg_pre_entry_volume")), 0.0)
    if target_volume > 0 and candidate_volume > 0:
        volume_ratio = max(target_volume, candidate_volume) / max(min(target_volume, candidate_volume), 1.0)
        if volume_ratio <= 1.5:
            score += 8
            reasons.append("similar pre-entry volume")
        elif volume_ratio <= 2.5:
            score += 4

    target_range = _as_float(target.get("avg_range_pct"))
    candidate_range = _as_float(candidate.get("avg_range_pct"))
    range_gap = abs(target_range - candidate_range)
    if range_gap <= 1.0:
        score += 8
        reasons.append("similar bar range")
    elif range_gap <= 2.5:
        score += 4

    label = "high" if score >= 72 else "medium" if score >= 48 else "low"
    return {"score": score, "label": label, "reasons": reasons[:4]}


def _trade_coach_similar_regime_reviews(
    review: dict[str, Any],
    all_reviews: list[dict[str, Any]],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    cycle_key = str(review.get("cycle_key", "") or "")
    strategy_code = str(review.get("strategy_code", "") or "")
    account_name = str(review.get("broker_account_name", "") or "")
    related: list[dict[str, Any]] = []
    for item in all_reviews:
        if str(item.get("cycle_key", "") or "") == cycle_key:
            continue
        if str(item.get("strategy_code", "") or "") != strategy_code:
            continue
        if str(item.get("broker_account_name", "") or "") != account_name:
            continue
        similarity = _trade_coach_regime_similarity(review, item)
        if int(similarity["score"]) < 48:
            continue
        related.append(
            {
                **item,
                "regime_similarity_score": int(similarity["score"]),
                "regime_similarity_label": str(similarity["label"]),
                "regime_similarity_reasons": list(similarity["reasons"]),
            }
        )
    return sorted(
        related,
        key=lambda row: (
            int(row.get("regime_similarity_score", 0)),
            _parse_et_timestamp(str(row.get("created_at", "") or "")),
        ),
        reverse=True,
    )[:limit]


def _build_trade_coach_related_rows(
    reviews: list[dict[str, Any]],
    *,
    include_similarity: bool = False,
    include_profile: bool = False,
) -> str:
    if not reviews:
        colspan = 5 + (1 if include_similarity else 0) + (1 if include_profile else 0)
        return f'<tr><td colspan="{colspan}" style="text-align:center;color:#888;">No similar reviewed trades yet</td></tr>'
    rendered: list[str] = []
    for item in reviews:
        review_url = f'/coach/review?cycle_key={quote(str(item.get("cycle_key", "") or ""))}'
        similarity_html = ""
        if include_similarity:
            similarity_html = (
                f'<td>{int(item.get("regime_similarity_score", 0))}'
                f' <span style="color:#98a6c8;">({escape(str(item.get("regime_similarity_label", "") or "-"))})</span></td>'
            )
        profile_html = ""
        if include_profile:
            regime = dict(item.get("regime_profile", {}) or {})
            profile_html = f'<td>{escape(str(regime.get("label", "") or "-"))}</td>'
        rendered.append(
            f"""<tr>
            <td style="white-space:nowrap;">{escape(str(item.get("created_at", "")) or "-")}</td>
            <td><strong>{escape(str(item.get("symbol", "")) or "-")}</strong></td>
            <td>{escape(str(item.get("path", "")) or "-")}</td>
            <td style="color:{_trade_coach_verdict_color(str(item.get("verdict", "")))};">{escape(str(item.get("verdict", "")) or "-")}</td>
            {similarity_html}
            {profile_html}
            <td><a href="{escape(review_url)}" style="color:#59d7ff;text-decoration:none;">Open</a></td>
        </tr>"""
        )
    return "".join(rendered)


def _build_trade_coach_review_rows(recent_reviews: list[dict[str, Any]]) -> tuple[str, int]:
    if not recent_reviews:
        return (
            '<tr><td colspan="6" style="text-align:center;color:#888;">No trade coach reviews yet</td></tr>',
            0,
        )

    rendered_rows: list[str] = []
    for item in sorted(
        recent_reviews,
        key=lambda row: _parse_et_timestamp(str(row.get("created_at", "") or "")),
        reverse=True,
    )[:25]:
        verdict = str(item.get("verdict", "") or "-").lower()
        action = str(item.get("action", "") or "-").lower()
        summary = str(item.get("summary", "") or "").strip() or "-"
        path = str(item.get("path", "") or "-")
        pnl_pct = _as_float(item.get("pnl_pct"))
        execution_timing = str(item.get("execution_timing", "") or "-").replace("_", " ")
        setup_quality = _as_float(item.get("setup_quality"))
        execution_quality = _as_float(item.get("execution_quality"))
        outcome_quality = _as_float(item.get("outcome_quality"))
        confidence = _as_float(item.get("confidence"))
        should_have_traded = "yes" if bool(item.get("should_have_traded")) else "no"
        should_review_manually = bool(item.get("should_review_manually"))
        coaching_focus = str(item.get("coaching_focus", "") or "-").replace("_", " ")
        key_reasons = _trade_coach_tag_list(list(item.get("key_reasons", []) or []))
        rule_violations = _trade_coach_tag_list(list(item.get("rule_violations", []) or []))
        next_time = _trade_coach_tag_list(list(item.get("next_time", []) or []))
        trade_day, trade_window = _trade_coach_trade_window_display(item)
        rendered_rows.append(
            f"""<tr>
            <td style="white-space:nowrap;"><strong>{escape(trade_day)}</strong><br><span style="color:#98a6c8;">{escape(trade_window)}</span></td>
            <td><strong>{escape(str(item.get("symbol", "")) or "-")}</strong></td>
            <td style="white-space:nowrap;"><strong>{escape(path)}</strong><br><span style="color:#98a6c8;">P&amp;L {pnl_pct:+.1f}% · timing {escape(execution_timing)} · setup {setup_quality:.2f} · exec {execution_quality:.2f} · outcome {outcome_quality:.2f}</span></td>
            <td style="color:{_trade_coach_verdict_color(verdict)};font-weight:bold;text-transform:uppercase;">{escape(verdict or "-")}<br><span style="color:#98a6c8;font-weight:normal;">{escape(action or "-")} · {confidence:.2f} · focus {escape(coaching_focus)}</span>{'<br><span style="color:#ffcc5b;font-weight:normal;">manual review</span>' if should_review_manually else ''}</td>
            <td style="text-transform:uppercase;">{escape(should_have_traded)}</td>
            <td style="font-size:11px;max-width:620px;"><div>{escape(summary)}</div><div style="margin-top:4px;color:#98a6c8;"><strong>Why:</strong> {escape(key_reasons)}</div><div style="margin-top:4px;color:#98a6c8;"><strong>Violations:</strong> {escape(rule_violations)}</div><div style="margin-top:4px;color:#98a6c8;"><strong>Next:</strong> {escape(next_time)}</div></td>
        </tr>"""
        )
    return "".join(rendered_rows), len(recent_reviews)


def _build_trade_coach_review_rows(
    recent_reviews: list[dict[str, Any]],
    *,
    include_context: bool = False,
) -> tuple[str, int]:
    column_count = 7 if include_context else 6
    if not recent_reviews:
        return (
            f'<tr><td colspan="{column_count}" style="text-align:center;color:#888;">No trade coach reviews yet</td></tr>',
            0,
        )

    rendered_rows: list[str] = []
    for item in sorted(
        recent_reviews,
        key=lambda row: _parse_et_timestamp(str(row.get("created_at", "") or "")),
        reverse=True,
    )[:25]:
        verdict = str(item.get("verdict", "") or "-").lower()
        action = str(item.get("action", "") or "-").lower()
        summary = str(item.get("summary", "") or "").strip() or "-"
        path = str(item.get("path", "") or "-")
        pnl_pct = _as_float(item.get("pnl_pct"))
        execution_timing = str(item.get("execution_timing", "") or "-").replace("_", " ")
        setup_quality = _as_float(item.get("setup_quality"))
        execution_quality = _as_float(item.get("execution_quality"))
        outcome_quality = _as_float(item.get("outcome_quality"))
        confidence = _as_float(item.get("confidence"))
        should_have_traded = "yes" if bool(item.get("should_have_traded")) else "no"
        should_review_manually = bool(item.get("should_review_manually"))
        coaching_focus = str(item.get("coaching_focus", "") or "-").replace("_", " ")
        key_reasons = _trade_coach_tag_list(list(item.get("key_reasons", []) or []))
        rule_violations = _trade_coach_tag_list(list(item.get("rule_violations", []) or []))
        next_time = _trade_coach_tag_list(list(item.get("next_time", []) or []))
        review_url = f'/coach/review?cycle_key={quote(str(item.get("cycle_key", "") or ""))}'
        trade_day, trade_window = _trade_coach_trade_window_display(item)
        context_cell = ""
        if include_context:
            context_cell = (
                f'<td style="white-space:nowrap;"><strong>{escape(str(item.get("display_name", item.get("strategy_code", "")) or "-"))}</strong>'
                f'<br><span style="color:#98a6c8;">{escape(str(item.get("account_display_name", item.get("broker_account_name", "")) or "-"))}</span></td>'
            )
        rendered_rows.append(
            f"""<tr>
            <td style="white-space:nowrap;"><strong>{escape(trade_day)}</strong><br><span style="color:#98a6c8;">{escape(trade_window)}</span></td>
            {context_cell}
            <td><strong>{escape(str(item.get("symbol", "")) or "-")}</strong><br><a href="{escape(review_url)}" style="color:#59d7ff;text-decoration:none;">open review</a></td>
            <td style="white-space:nowrap;"><strong>{escape(path)}</strong><br><span style="color:#98a6c8;">P&amp;L {pnl_pct:+.1f}% &middot; timing {escape(execution_timing)} &middot; setup {setup_quality:.2f} &middot; exec {execution_quality:.2f} &middot; outcome {outcome_quality:.2f}</span></td>
            <td style="color:{_trade_coach_verdict_color(verdict)};font-weight:bold;text-transform:uppercase;">{escape(verdict or "-")}<br><span style="color:#98a6c8;font-weight:normal;">{escape(action or "-")} &middot; {confidence:.2f} &middot; focus {escape(coaching_focus)}</span>{'<br><span style="color:#ffcc5b;font-weight:normal;">manual review</span>' if should_review_manually else ''}</td>
            <td style="text-transform:uppercase;">{escape(should_have_traded)}</td>
            <td style="font-size:11px;max-width:620px;"><div>{escape(summary)}</div><div style="margin-top:4px;color:#98a6c8;"><strong>Why:</strong> {escape(key_reasons)}</div><div style="margin-top:4px;color:#98a6c8;"><strong>Violations:</strong> {escape(rule_violations)}</div><div style="margin-top:4px;color:#98a6c8;"><strong>Next:</strong> {escape(next_time)}</div><div style="margin-top:6px;"><a href="{escape(review_url)}" style="color:#59d7ff;text-decoration:none;">Open full review</a></div></td>
        </tr>"""
        )
    return "".join(rendered_rows), len(recent_reviews)


def _build_trade_coach_queue_rows(queue_reviews: list[dict[str, Any]]) -> tuple[str, int]:
    if not queue_reviews:
        return (
            '<tr><td colspan="6" style="text-align:center;color:#888;">No priority reviews right now</td></tr>',
            0,
        )

    rendered_rows: list[str] = []
    for item in queue_reviews[:10]:
        review_url = f'/coach/review?cycle_key={quote(str(item.get("cycle_key", "") or ""))}'
        trade_day, trade_window = _trade_coach_trade_window_display(item)
        rendered_rows.append(
            f"""<tr>
            <td style="text-transform:uppercase;color:{_trade_coach_verdict_color(str(item.get("verdict", "")))};"><strong>{escape(str(item.get("priority_label", "low")))} ({int(item.get("priority_score", 0))})</strong></td>
            <td style="white-space:nowrap;"><strong>{escape(trade_day)}</strong><br><span style="color:#98a6c8;">{escape(trade_window)}</span></td>
            <td><strong>{escape(str(item.get("display_name", item.get("strategy_code", "")) or "-"))}</strong><br><span style="color:#98a6c8;">{escape(str(item.get("account_display_name", item.get("broker_account_name", "")) or "-"))}</span></td>
            <td><strong>{escape(str(item.get("symbol", "")) or "-")}</strong><br><span style="color:#98a6c8;">{escape(str(item.get("path", "") or "-"))}</span></td>
            <td style="font-size:11px;color:#98a6c8;">{escape(_trade_coach_tag_list(list(item.get("priority_reasons", []) or [])))}</td>
            <td><a href="{escape(review_url)}" style="color:#59d7ff;text-decoration:none;">Open review</a></td>
        </tr>"""
        )
    return "".join(rendered_rows), len(queue_reviews)


def _build_trade_coach_pattern_signal_rows(pattern_signals: list[dict[str, Any]]) -> tuple[str, int]:
    if not pattern_signals:
        return (
            '<tr><td colspan="5" style="text-align:center;color:#888;">No pattern signals in this filter window</td></tr>',
            0,
        )

    rendered_rows: list[str] = []
    for item in pattern_signals[:8]:
        rendered_rows.append(
            f"""<tr>
            <td style="text-transform:uppercase;color:{_trade_coach_verdict_color(str(item.get("caution_label", "")))};"><strong>{escape(str(item.get("caution_label", "low")))} ({float(item.get("caution_score", 0.0)):.0f})</strong></td>
            <td><strong>{escape(str(item.get("pattern_type", "") or "-").upper())}</strong><br><span style="color:#98a6c8;">{escape(str(item.get("pattern_key", "") or "-"))}</span></td>
            <td>{int(item.get("count", 0))}<br><span style="color:#98a6c8;">avg P&amp;L {float(item.get("avg_pnl_pct", 0.0)):+.1f}%</span></td>
            <td style="font-size:11px;color:#98a6c8;">G {int(item.get("good", 0))} &middot; M {int(item.get("mixed", 0))} &middot; B {int(item.get("bad", 0))}<br>manual {int(item.get("manual_review_count", 0))} &middot; skip {int(item.get("should_skip_count", 0))}</td>
            <td style="font-size:11px;"><div>{escape(str(item.get("message", "") or "-"))}</div><div style="margin-top:4px;color:#98a6c8;"><strong>Why:</strong> {escape(_trade_coach_tag_list(list(item.get("reasons", []) or [])))}</div></td>
        </tr>"""
        )
    return "".join(rendered_rows), len(pattern_signals)


def _build_trade_coach_pattern_rows(patterns: list[dict[str, Any]]) -> tuple[str, int]:
    if not patterns:
        return (
            '<tr><td colspan="6" style="text-align:center;color:#888;">No pattern summaries yet</td></tr>',
            0,
        )

    rendered_rows: list[str] = []
    for item in patterns[:8]:
        rendered_rows.append(
            f"""<tr>
            <td><strong>{escape(str(item.get("pattern_key", "") or "-"))}</strong><br><span style="color:#98a6c8;">{escape(str(item.get("pattern_type", "") or "-").upper())}</span></td>
            <td>{int(item.get("count", 0))}</td>
            <td>G {int(item.get("good", 0))} &middot; M {int(item.get("mixed", 0))} &middot; B {int(item.get("bad", 0))}</td>
            <td>{float(item.get("avg_pnl_pct", 0.0)):+.1f}%<br><span style="color:#98a6c8;">outcome {float(item.get("avg_outcome_quality", 0.0)):.2f}</span></td>
            <td>setup {float(item.get("avg_setup_quality", 0.0)):.2f}<br><span style="color:#98a6c8;">exec {float(item.get("avg_execution_quality", 0.0)):.2f}</span></td>
            <td style="text-transform:uppercase;color:{_trade_coach_verdict_color(str(item.get("caution_label", "")))};"><strong>{escape(str(item.get("caution_label", "low")))}</strong><br><span style="color:#98a6c8;">score {float(item.get("caution_score", 0.0)):.0f}</span></td>
        </tr>"""
        )
    return "".join(rendered_rows), len(patterns)


def _build_trade_coach_guidance_rows(operator_guidance: list[dict[str, Any]]) -> tuple[str, int]:
    if not operator_guidance:
        return (
            '<tr><td colspan="4" style="text-align:center;color:#888;">No operator guidance in this filter window</td></tr>',
            0,
        )

    rendered_rows: list[str] = []
    for item in operator_guidance[:4]:
        rendered_rows.append(
            f"""<tr>
            <td style="text-transform:uppercase;color:{_trade_coach_verdict_color(str(item.get("caution_label", "")))};"><strong>{escape(str(item.get("caution_label", "low")))}</strong></td>
            <td><strong>{escape(str(item.get("title", "") or "-"))}</strong><br><span style="color:#98a6c8;">{escape(str(item.get("summary", "") or "-"))}</span></td>
            <td style="font-size:11px;color:#98a6c8;">{escape(_trade_coach_tag_list(list(item.get("reasons", []) or [])))}</td>
            <td style="font-size:11px;">{escape(str(item.get("action", "") or "-"))}</td>
        </tr>"""
        )
    return "".join(rendered_rows), len(operator_guidance)


def _build_trade_coach_live_advisory_summary_cards(
    advisories: list[dict[str, Any]],
    *,
    reviewed_count: int,
) -> str:
    high_count = sum(1 for item in advisories if str(item.get("caution_label", "") or "").lower() == "high")
    medium_count = sum(1 for item in advisories if str(item.get("caution_label", "") or "").lower() == "medium")
    low_count = sum(1 for item in advisories if str(item.get("caution_label", "") or "").lower() == "low")
    strongest = advisories[0] if advisories else {}
    strongest_symbol = str(strongest.get("symbol", "") or "-")
    strongest_path = str(strongest.get("current_path", "") or "-")
    strongest_score = float(strongest.get("caution_score", 0.0))
    strongest_label = str(strongest.get("caution_label", "low") or "low").upper()
    avg_regime_matches = (
        sum(int(dict(item.get("similar_regime_summary", {}) or {}).get("count", 0)) for item in advisories) / len(advisories)
        if advisories
        else 0.0
    )
    return f"""
                <div class="hero-grid coach-advisory-hero-grid">
                    <div class="hero-card coach-hero-card">
                        <span>Mode</span>
                        <strong>READ-ONLY</strong>
                        <small>Production preview only. No gates, no order changes, no OMS influence.</small>
                    </div>
                    <div class="hero-card coach-hero-card">
                        <span>Live Symbols</span>
                        <strong>{len(advisories)}</strong>
                        <small>Symbols with current 30-second context and coach memory attached.</small>
                    </div>
                    <div class="hero-card coach-hero-card">
                        <span>Caution Mix</span>
                        <strong>{high_count} high / {medium_count} med</strong>
                        <small>{low_count} low caution symbols remain visible for context.</small>
                    </div>
                    <div class="hero-card coach-hero-card">
                        <span>Reviewed History</span>
                        <strong>{reviewed_count}</strong>
                        <small>Completed 30-second trades already scored by the coach for this bot.</small>
                    </div>
                    <div class="hero-card coach-hero-card">
                        <span>Strongest Live Signal</span>
                        <strong>{escape(strongest_symbol)}</strong>
                        <small>{escape(strongest_path)} · {strongest_label} caution ({strongest_score:.0f}) · avg {avg_regime_matches:.1f} similar-regime matches</small>
                    </div>
                </div>"""


def _build_trade_coach_live_reference_links(references: list[dict[str, Any]]) -> str:
    if not references:
        return '<span class="coach-inline-empty">No matched reviewed trades yet.</span>'

    rendered_links: list[str] = []
    for item in references:
        review_url = f'/coach/review?cycle_key={quote(str(item.get("cycle_key", "") or ""))}'
        bucket = str(item.get("bucket", "") or "match").strip().lower()
        label = f"{bucket}: {str(item.get('symbol', '') or '-')} / {str(item.get('path', '') or '-')}"
        rendered_links.append(
            f'<a class="coach-inline-link" href="{escape(review_url)}">{escape(label)}</a>'
        )
    return "".join(rendered_links)


def _build_trade_coach_live_advisory_spotlight_cards(advisories: list[dict[str, Any]]) -> str:
    if not advisories:
        return """
                <div class="coach-spotlight-grid">
                    <article class="coach-spotlight-card coach-spotlight-empty">
                        <div class="coach-spotlight-topline">No live caution matches yet</div>
                        <h4>Waiting for a stronger 30-second setup footprint</h4>
                        <p>The coach will start surfacing symbols here once we have enough live context to compare against reviewed trade memory.</p>
                    </article>
                </div>"""

    rendered_cards: list[str] = []
    for item in advisories[:3]:
        regime_profile = dict(item.get("regime_profile", {}) or {})
        same_symbol_summary = dict(item.get("same_symbol_summary", {}) or {})
        same_path_summary = dict(item.get("same_path_summary", {}) or {})
        similar_regime_summary = dict(item.get("similar_regime_summary", {}) or {})
        references = list(item.get("reference_reviews", []) or [])
        reasons = list(item.get("reasons", []) or [])
        reason_html = "".join(
            f'<span class="pill-chip {("warning" if index == 0 else "accent")}">{escape(str(reason))}</span>'
            for index, reason in enumerate(reasons[:3])
        ) or '<span class="pill-chip">No elevated caution reasons yet.</span>'
        reference_html = _build_trade_coach_live_reference_links(references)
        caution_label = str(item.get("caution_label", "low") or "low").lower()
        caution_style = _trade_coach_verdict_color(caution_label)
        rendered_cards.append(
            f"""
                    <article class="coach-spotlight-card coach-spotlight-{escape(caution_label)}">
                        <div class="coach-spotlight-topline">
                            <span>{escape(str(item.get("symbol", "") or "-"))} · {escape(str(item.get("current_path", "") or "-"))}</span>
                            <span class="count" style="background:rgba(255,255,255,0.05);color:{caution_style};border:1px solid color-mix(in srgb, {caution_style} 40%, rgba(121,146,193,0.24));">{escape(caution_label.upper())} {float(item.get("caution_score", 0.0)):.0f}</span>
                        </div>
                        <h4>{escape(str(item.get("message", "") or "-"))}</h4>
                        <p>{escape(str(item.get("severity_caption", "") or "-"))}</p>
                        <div class="coach-spotlight-facts">
                            <div><strong>Live context</strong><span>{escape(str(item.get("live_status", "") or "-").upper())} · {escape(str(item.get("live_timestamp", "") or "-"))}</span></div>
                            <div><strong>Why now</strong><span>{escape(str(item.get("live_reason", "") or "-"))}</span></div>
                            <div><strong>Regime profile</strong><span>{escape(str(regime_profile.get("label", "") or "-"))}</span></div>
                            <div><strong>Path memory</strong><span>{int(same_path_summary.get("count", 0))} same-path reviews · avg {float(same_path_summary.get("avg_pnl_pct", 0.0)):+.1f}%</span></div>
                            <div><strong>Memory</strong><span>{int(similar_regime_summary.get("count", 0))} similar regime · {int(same_symbol_summary.get("count", 0))} same-symbol</span></div>
                        </div>
                        <div class="coach-chip-row">{reason_html}</div>
                        <details class="coach-details">
                            <summary>Why this surfaced</summary>
                            <div class="coach-details-copy"><strong>What to watch:</strong> {escape(str(item.get("action", "") or "-"))}</div>
                            <div class="coach-details-copy"><strong>Path memory:</strong> {int(same_path_summary.get("count", 0))} reviewed trades · avg {float(same_path_summary.get("avg_pnl_pct", 0.0)):+.1f}%</div>
                            <div class="coach-details-copy"><strong>Similar regime memory:</strong> {int(similar_regime_summary.get("count", 0))} reviewed trades · avg {float(similar_regime_summary.get("avg_pnl_pct", 0.0)):+.1f}%</div>
                            <div class="coach-details-copy"><strong>Matched reviews:</strong> {reference_html}</div>
                        </details>
                    </article>"""
        )
    return f'<div class="coach-spotlight-grid">{"".join(rendered_cards)}</div>'


def _build_trade_coach_live_advisory_rows(advisories: list[dict[str, Any]]) -> tuple[str, int]:
    if not advisories:
        return (
            '<tr><td colspan="5" style="text-align:center;color:#888;">No live coach advisory matches yet for this bot.</td></tr>',
            0,
        )

    rendered_rows: list[str] = []
    for item in advisories:
        same_symbol_summary = dict(item.get("same_symbol_summary", {}) or {})
        same_path_summary = dict(item.get("same_path_summary", {}) or {})
        similar_regime_summary = dict(item.get("similar_regime_summary", {}) or {})
        regime_profile = dict(item.get("regime_profile", {}) or {})
        path_signal = dict(item.get("path_signal", {}) or {})
        reference_html = _build_trade_coach_live_reference_links(list(item.get("reference_reviews", []) or []))
        rendered_rows.append(
            f"""<tr>
            <td><strong>{escape(str(item.get("symbol", "")) or "-")}</strong><br><span style="color:#98a6c8;">{escape(str(item.get("current_path", "")) or "-")}</span></td>
            <td style="white-space:nowrap;"><strong>{escape(str(item.get("live_status", "")) or "-").upper()}</strong><br><span style="color:#98a6c8;">{escape(str(item.get("live_timestamp", "")) or "-")}</span><div style="margin-top:4px;font-size:11px;color:#98a6c8;max-width:220px;">{escape(str(item.get("live_reason", "")) or "-")}</div></td>
            <td style="font-size:11px;max-width:300px;"><div><strong>Regime:</strong> {escape(str(regime_profile.get("label", "")) or "-")}</div><div style="margin-top:4px;color:#98a6c8;"><strong>Path history:</strong> {int(same_path_summary.get("count", 0))} reviews &middot; avg {float(same_path_summary.get("avg_pnl_pct", 0.0)):+.1f}%</div><div style="margin-top:4px;color:#98a6c8;"><strong>Symbol history:</strong> {int(same_symbol_summary.get("count", 0))} reviews &middot; avg {float(same_symbol_summary.get("avg_pnl_pct", 0.0)):+.1f}%</div><div style="margin-top:4px;color:#98a6c8;"><strong>Similar regime:</strong> {int(similar_regime_summary.get("count", 0))} reviews &middot; avg {float(similar_regime_summary.get("avg_pnl_pct", 0.0)):+.1f}%</div><div style="margin-top:4px;color:#98a6c8;"><strong>Path signal:</strong> {escape(str(path_signal.get("caution_label", "-")))} {f'&middot; score {float(path_signal.get("caution_score", 0.0)):.0f}' if path_signal else ''}</div></td>
            <td style="text-transform:uppercase;color:{_trade_coach_verdict_color(str(item.get("caution_label", "")))};"><strong>{escape(str(item.get("caution_label", "low")))} ({float(item.get("caution_score", 0.0)):.0f})</strong><br><span style="color:#98a6c8;font-weight:normal;">{escape(str(item.get("severity_caption", "") or "-"))}</span></td>
            <td style="font-size:11px;max-width:420px;"><div>{escape(str(item.get("message", "") or "-"))}</div><div style="margin-top:4px;color:#98a6c8;"><strong>What to watch:</strong> {escape(str(item.get("action", "") or "-"))}</div><div style="margin-top:4px;color:#98a6c8;"><strong>Matched reviews:</strong> {reference_html}</div></td>
        </tr>"""
        )
    return "".join(rendered_rows), len(advisories)


def _render_trade_coach_review_detail(
    data: dict[str, Any],
    *,
    review_history: list[dict[str, Any]],
    cycle_key: str,
    regime_profiles: dict[str, dict[str, Any]] | None = None,
) -> str:
    all_reviews = _apply_trade_coach_regime_profiles(
        _enrich_trade_coach_reviews(
            review_history,
            list(data.get("bots", [])),
        ),
        regime_profiles or {},
    )
    review = _find_trade_coach_review(all_reviews, cycle_key)
    available_codes = [str(item.get("strategy_code", "") or "") for item in data.get("bots", [])]
    nav_html = _render_trade_coach_review_nav(available_codes)
    if review is None:
        return f"""<!DOCTYPE html>
<html><head><title>Trade Coach Review Not Found</title><meta charset="utf-8"></head>
<body style="background:#131a2b;color:#f0f4ff;font-family:Consolas,Monaco,monospace;padding:24px;">
<h1>Trade Coach Review Not Found</h1>
<p>The requested cycle key was not found in the current coach review window.</p>
<div>{nav_html}</div>
</body></html>"""

    priority = _trade_coach_review_priority(review)
    same_path_reviews = _trade_coach_related_reviews(review, all_reviews, mode="path")
    same_symbol_reviews = _trade_coach_related_reviews(review, all_reviews, mode="symbol")
    similar_regime_reviews = _trade_coach_similar_regime_reviews(review, all_reviews)
    same_path_summary = _trade_coach_history_summary(same_path_reviews)
    same_symbol_summary = _trade_coach_history_summary(same_symbol_reviews)
    similar_regime_summary = _trade_coach_similarity_summary(similar_regime_reviews)
    same_path_rows = _build_trade_coach_related_rows(same_path_reviews)
    same_symbol_rows = _build_trade_coach_related_rows(same_symbol_reviews)
    similar_regime_rows = _build_trade_coach_related_rows(
        similar_regime_reviews,
        include_similarity=True,
        include_profile=True,
    )
    verdict = str(review.get("verdict", "") or "-").lower()
    action = str(review.get("action", "") or "-").lower()
    focus = str(review.get("coaching_focus", "") or "-").replace("_", " ")
    entry_time = str(review.get("entry_time", "") or "-")
    exit_time = str(review.get("exit_time", "") or "-")
    exit_summary = str(review.get("exit_summary", "") or "-")
    regime_profile = dict(review.get("regime_profile", {}) or {})
    pnl = _as_float(review.get("pnl"))
    pnl_pct = _as_float(review.get("pnl_pct"))
    key_reasons = _render_chip_cloud(list(review.get("key_reasons", []) or []), variant="accent", empty_text="None")
    rule_hits = _render_chip_cloud(list(review.get("rule_hits", []) or []), variant="", empty_text="None")
    rule_violations = _render_chip_cloud(list(review.get("rule_violations", []) or []), variant="warning", empty_text="None")
    next_time = _render_chip_cloud(list(review.get("next_time", []) or []), variant="accent", empty_text="None")
    back_url = "/coach/reviews"
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Trade Coach Review Detail</title>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="30">
    <style>
        :root {{
            --bg: #131a2b; --panel: #202b46; --panel-alt: #1c2540; --line: rgba(121,146,193,0.28);
            --ink: #f0f4ff; --muted: #98a6c8; --accent: #59d7ff; --green: #5fff8d; --amber: #ffcc5b; --red: #ff6b6b;
        }}
        * {{ box-sizing:border-box; }}
        body {{ margin:0; background:radial-gradient(circle at top left, rgba(89,215,255,0.08), transparent 28%), linear-gradient(180deg, #0f1525, var(--bg)); color:var(--ink); font-family:'Consolas','Monaco',monospace; }}
        .shell {{ padding:18px; display:grid; gap:16px; }}
        .panel {{ background:linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02)); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 18px 44px rgba(0,0,0,0.26); }}
        .hero-grid, .fact-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; }}
        .hero-card {{ background:var(--panel-alt); border:1px solid var(--line); border-radius:14px; padding:14px; }}
        .hero-card span {{ display:block; color:var(--muted); font-size:12px; margin-bottom:8px; }}
        .hero-card strong {{ font-size:22px; }}
        .nav-strip {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }}
        .nav-strip a, .back-link {{ text-decoration:none; color:var(--ink); background:linear-gradient(180deg, rgba(89,215,255,0.08), rgba(255,255,255,0.02)); border:1px solid var(--line); border-radius:999px; padding:8px 12px; font-size:12px; }}
        .nav-strip a.active {{ border-color:var(--accent); box-shadow:inset 0 0 0 1px rgba(89,215,255,0.35); }}
        .panel-header h2, .panel-header h3 {{ margin:0; }}
        .sub {{ color:var(--muted); font-size:12px; margin-top:6px; }}
        .facts {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:12px; }}
        .fact {{ background:var(--panel-alt); border:1px solid var(--line); border-radius:14px; padding:14px; }}
        .fact .label {{ color:var(--muted); font-size:12px; margin-bottom:6px; }}
        .fact .value {{ font-size:14px; word-break:break-word; }}
        .section-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:16px; }}
        .pill-chip {{ display:inline-flex; align-items:center; margin:4px 6px 0 0; padding:4px 10px; border-radius:999px; border:1px solid var(--line); background:rgba(255,255,255,0.04); font-size:12px; }}
        .pill-chip.accent {{ border-color:rgba(89,215,255,0.36); color:#aeeeff; }}
        .pill-chip.warning {{ border-color:rgba(255,204,91,0.45); color:#ffe3a3; }}
    </style>
</head>
<body>
    <div class="shell">
        <section class="panel">
            <a class="back-link" href="{back_url}">Back to review center</a>
            <div style="margin-top:14px;">
                <h1 style="margin:0;font-size:28px;">{escape(str(review.get("symbol", "")) or "-")} Review Detail</h1>
                <div class="sub">{escape(str(review.get("display_name", review.get("strategy_code", "")) or "-"))} · {escape(str(review.get("account_display_name", review.get("broker_account_name", "")) or "-"))}</div>
                {nav_html}
            </div>
        </section>
        <section class="panel">
            <div class="hero-grid">
                <div class="hero-card"><span>Verdict</span><strong style="color:{_trade_coach_verdict_color(verdict)};">{escape(verdict.upper())}</strong></div>
                <div class="hero-card"><span>Action</span><strong>{escape(action.upper())}</strong></div>
                <div class="hero-card"><span>Focus</span><strong>{escape(focus)}</strong></div>
                <div class="hero-card"><span>Confidence</span><strong>{_as_float(review.get("confidence")):.2f}</strong></div>
                <div class="hero-card"><span>Priority</span><strong>{escape(str(priority["label"]).upper())} ({int(priority["score"])})</strong></div>
                <div class="hero-card"><span>Reviewed</span><strong style="font-size:16px;">{escape(str(review.get("created_at", "")) or "-")}</strong></div>
            </div>
        </section>
        <section class="panel">
            <div class="panel-header"><h2>Trade Facts</h2></div>
            <div class="facts">
                <div class="fact"><div class="label">Path</div><div class="value">{escape(str(review.get("path", "")) or "-")}</div></div>
                <div class="fact"><div class="label">Entry Time</div><div class="value">{escape(entry_time)}</div></div>
                <div class="fact"><div class="label">Exit Time</div><div class="value">{escape(exit_time)}</div></div>
                <div class="fact"><div class="label">Entry Price</div><div class="value">{escape(str(review.get("entry_price", "")) or "-")}</div></div>
                <div class="fact"><div class="label">Exit Price</div><div class="value">{escape(str(review.get("exit_price", "")) or "-")}</div></div>
                <div class="fact"><div class="label">P&amp;L</div><div class="value">{pnl:+.2f} / {pnl_pct:+.1f}%</div></div>
                <div class="fact"><div class="label">Exit Summary</div><div class="value">{escape(exit_summary)}</div></div>
                <div class="fact"><div class="label">Cycle Key</div><div class="value">{escape(str(review.get("cycle_key", "")) or "-")}</div></div>
            </div>
        </section>
        <section class="panel">
            <div class="panel-header"><h2>Coach Summary</h2></div>
            <div class="sub">{escape(str(review.get("summary", "")) or "-")}</div>
            <div class="section-grid" style="margin-top:14px;">
                <div class="fact"><div class="label">Priority Reasons</div><div class="value">{_render_chip_cloud(list(priority["reasons"]), variant="warning", empty_text="None")}</div></div>
                <div class="fact"><div class="label">Key Reasons</div><div class="value">{key_reasons}</div></div>
                <div class="fact"><div class="label">Rule Hits</div><div class="value">{rule_hits}</div></div>
                <div class="fact"><div class="label">Rule Violations</div><div class="value">{rule_violations}</div></div>
                <div class="fact"><div class="label">Next Time</div><div class="value">{next_time}</div></div>
                <div class="fact"><div class="label">Quality Scores</div><div class="value">setup {_as_float(review.get("setup_quality")):.2f} · execution {_as_float(review.get("execution_quality")):.2f} · outcome {_as_float(review.get("outcome_quality")):.2f}</div></div>
            </div>
        </section>
        <section class="panel">
            <div class="panel-header"><h2>Pattern Memory</h2></div>
            <div class="sub">This is the bridge toward live usefulness: compare this trade against same-path history, same-symbol history, and similar historical regimes built from price, volume, volatility, and momentum context.</div>
            <div class="hero-grid" style="margin-top:14px;">
                <div class="hero-card"><span>Same Path Count</span><strong>{same_path_summary["count"]}</strong><small style="display:block;color:#98a6c8;margin-top:8px;">avg P&amp;L {same_path_summary["avg_pnl_pct"]:+.1f}%</small></div>
                <div class="hero-card"><span>Same Path Mix</span><strong style="font-size:16px;">G {same_path_summary["good"]} · M {same_path_summary["mixed"]} · B {same_path_summary["bad"]}</strong><small style="display:block;color:#98a6c8;margin-top:8px;">Path {escape(str(review.get("path", "")) or "-")}</small></div>
                <div class="hero-card"><span>Same Symbol Count</span><strong>{same_symbol_summary["count"]}</strong><small style="display:block;color:#98a6c8;margin-top:8px;">avg P&amp;L {same_symbol_summary["avg_pnl_pct"]:+.1f}%</small></div>
                <div class="hero-card"><span>Same Symbol Mix</span><strong style="font-size:16px;">G {same_symbol_summary["good"]} · M {same_symbol_summary["mixed"]} · B {same_symbol_summary["bad"]}</strong><small style="display:block;color:#98a6c8;margin-top:8px;">Symbol {escape(str(review.get("symbol", "")) or "-")}</small></div>
                <div class="hero-card"><span>Regime Profile</span><strong style="font-size:16px;">{escape(str(regime_profile.get("label", "")) or "-")}</strong><small style="display:block;color:#98a6c8;margin-top:8px;">price {escape(str(regime_profile.get("price_band", "")) or "-")} &middot; volume {escape(str(regime_profile.get("volume_band", "")) or "-")}</small></div>
                <div class="hero-card"><span>Similar Regime Count</span><strong>{similar_regime_summary["count"]}</strong><small style="display:block;color:#98a6c8;margin-top:8px;">avg similarity {similar_regime_summary["avg_similarity_score"]:.0f} &middot; avg P&amp;L {similar_regime_summary["avg_pnl_pct"]:+.1f}%</small></div>
            </div>
            <div class="section-grid" style="margin-top:14px;">
                <div class="fact">
                    <div class="label">Recent Same-Path Reviews</div>
                    <div class="value">
                        <div style="overflow-x:auto;border:1px solid var(--line);border-radius:12px;">
                            <table style="width:100%;border-collapse:collapse;min-width:420px;">
                                <thead><tr><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Reviewed</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Ticker</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Path</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Verdict</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Open</th></tr></thead>
                                <tbody>{same_path_rows}</tbody>
                            </table>
                        </div>
                    </div>
                </div>
                <div class="fact">
                    <div class="label">Recent Similar-Regime Reviews</div>
                    <div class="value">
                        <div style="overflow-x:auto;border:1px solid var(--line);border-radius:12px;">
                            <table style="width:100%;border-collapse:collapse;min-width:640px;">
                                <thead><tr><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Reviewed</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Ticker</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Path</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Verdict</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Similarity</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Profile</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Open</th></tr></thead>
                                <tbody>{similar_regime_rows}</tbody>
                            </table>
                        </div>
                    </div>
                </div>
                <div class="fact">
                    <div class="label">Recent Same-Symbol Reviews</div>
                    <div class="value">
                        <div style="overflow-x:auto;border:1px solid var(--line);border-radius:12px;">
                            <table style="width:100%;border-collapse:collapse;min-width:420px;">
                                <thead><tr><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Reviewed</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Ticker</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Path</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Verdict</th><th style="padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);">Open</th></tr></thead>
                                <tbody>{same_symbol_rows}</tbody>
                            </table>
                        </div>
                    </div>
                </div>
                <div class="fact">
                    <div class="label">Regime Metrics</div>
                    <div class="value">
                        <div class="pill-chip accent">Pre-entry volume {int(regime_profile.get("avg_pre_entry_volume", 0) or 0):,}</div>
                        <div class="pill-chip accent">Avg bar range {float(regime_profile.get("avg_range_pct", 0.0) or 0.0):.2f}%</div>
                        <div class="pill-chip accent">Pre-entry change {float(regime_profile.get("pre_entry_change_pct", 0.0) or 0.0):+.2f}%</div>
                        <div class="pill-chip accent">Trade range {float(regime_profile.get("trade_range_pct", 0.0) or 0.0):.2f}%</div>
                        <div class="pill-chip accent">Duration {int(regime_profile.get("duration_secs", 0) or 0)}s</div>
                        <div class="pill-chip accent">Bars sampled {int(regime_profile.get("bar_count", 0) or 0)}</div>
                    </div>
                </div>
            </div>
        </section>
    </div>
</body>
</html>"""


def _render_trade_coach_review_center(
    data: dict[str, Any],
    *,
    review_history: list[dict[str, Any]],
    regime_profiles: dict[str, dict[str, Any]] | None = None,
    strategy_code: str | None = None,
    verdict: str | None = None,
    coaching_focus: str | None = None,
    symbol: str | None = None,
    start_date: str = "",
    end_date: str = "",
) -> str:
    all_reviews = _apply_trade_coach_regime_profiles(
        _enrich_trade_coach_reviews(
            review_history,
            list(data.get("bots", [])),
        ),
        regime_profiles or {},
    )
    filtered_reviews = _filter_trade_coach_reviews(
        all_reviews,
        strategy_code=strategy_code,
        verdict=verdict,
        coaching_focus=coaching_focus,
        symbol=symbol,
        start=_parse_review_filter_date(start_date),
        end=(
            (_parse_review_filter_date(end_date) + timedelta(days=1))
            if str(end_date or "").strip()
            else None
        ),
    )
    review_rows, visible_count = _build_trade_coach_review_rows(filtered_reviews, include_context=True)
    summary = _trade_coach_review_summary(filtered_reviews)
    queue_reviews = _build_trade_coach_review_queue(filtered_reviews)
    queue_rows, queue_count = _build_trade_coach_queue_rows(queue_reviews)
    pattern_signals = _trade_coach_pattern_signals(filtered_reviews)
    signal_rows, signal_count = _build_trade_coach_pattern_signal_rows(pattern_signals)
    path_patterns = _trade_coach_pattern_scoreboard(filtered_reviews, mode="path")
    regime_patterns = _trade_coach_pattern_scoreboard(filtered_reviews, mode="regime")
    operator_guidance = _trade_coach_operator_guidance(pattern_signals)
    guidance_rows, guidance_count = _build_trade_coach_guidance_rows(operator_guidance)
    path_pattern_rows, path_pattern_count = _build_trade_coach_pattern_rows(path_patterns)
    regime_pattern_rows, regime_pattern_count = _build_trade_coach_pattern_rows(regime_patterns)
    available_codes = [str(item.get("strategy_code", "") or "") for item in data.get("bots", [])]
    nav_html = _render_trade_coach_review_nav(available_codes)
    selected_strategy = str(strategy_code or "").strip()
    selected_verdict = str(verdict or "").strip().lower()
    selected_focus = str(coaching_focus or "").strip().lower()
    selected_symbol = str(symbol or "").strip().upper()
    selected_start_date = str(start_date or "").strip()
    selected_end_date = str(end_date or "").strip()
    strategy_options = "".join(
        f'<option value="{escape(str(bot.get("strategy_code", "") or ""))}"{" selected" if str(bot.get("strategy_code", "") or "") == selected_strategy else ""}>{escape(str(bot.get("display_name", "") or bot.get("strategy_code", "") or "-"))}</option>'
        for bot in data.get("bots", [])
    )
    verdict_options = "".join(
        f'<option value="{escape(value)}"{" selected" if value == selected_verdict else ""}>{escape(value.upper())}</option>'
        for value in sorted(
            {
                str(item.get("verdict", "") or "").strip().lower()
                for item in all_reviews
                if str(item.get("verdict", "") or "").strip()
            }
        )
    )
    focus_options = "".join(
        f'<option value="{escape(value)}"{" selected" if value == selected_focus else ""}>{escape(value.replace("_", " "))}</option>'
        for value in sorted(
            {
                str(item.get("coaching_focus", "") or "").strip().lower()
                for item in all_reviews
                if str(item.get("coaching_focus", "") or "").strip()
            }
        )
    )
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Trade Coach Reviews</title>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="30">
    <style>
        :root {{
            --bg: #131a2b;
            --panel: #202b46;
            --panel-alt: #1c2540;
            --line: rgba(121, 146, 193, 0.28);
            --ink: #f0f4ff;
            --muted: #98a6c8;
            --accent: #59d7ff;
            --green: #5fff8d;
            --amber: #ffcc5b;
            --red: #ff6b6b;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            background:
                radial-gradient(circle at top left, rgba(89,215,255,0.08), transparent 28%),
                linear-gradient(180deg, #0f1525, var(--bg));
            color: var(--ink);
            font-family: 'Consolas','Monaco',monospace;
        }}
        .shell {{ padding: 18px; display: grid; gap: 16px; }}
        .panel {{
            background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02));
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 18px;
            box-shadow: 0 18px 44px rgba(0,0,0,0.26);
        }}
        .hero {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 18px;
            flex-wrap: wrap;
        }}
        .hero h1 {{ margin: 0; font-size: 28px; }}
        .hero p {{ margin: 8px 0 0; color: var(--muted); max-width: 840px; line-height: 1.5; }}
        .nav-strip {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 12px;
        }}
        .nav-strip a {{
            text-decoration: none;
            color: var(--ink);
            background: linear-gradient(180deg, rgba(89,215,255,0.08), rgba(255,255,255,0.02));
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 8px 12px;
            font-size: 12px;
        }}
        .nav-strip a.active {{
            border-color: var(--accent);
            box-shadow: inset 0 0 0 1px rgba(89,215,255,0.35);
        }}
        .hero-cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 12px;
            min-width: min(100%, 620px);
            flex: 1;
        }}
        .hero-card {{
            background: var(--panel-alt);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 14px;
        }}
        .hero-card span {{
            display: block;
            color: var(--muted);
            font-size: 12px;
            margin-bottom: 8px;
        }}
        .hero-card strong {{ font-size: 24px; }}
        .filters {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            align-items: end;
        }}
        label {{
            display: grid;
            gap: 6px;
            color: var(--muted);
            font-size: 12px;
        }}
        select, input, button {{
            width: 100%;
            border-radius: 10px;
            border: 1px solid var(--line);
            background: #11192c;
            color: var(--ink);
            padding: 10px 12px;
            font: inherit;
        }}
        .filter-actions {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
        }}
        .filter-actions button, .button-link {{
            width: auto;
            min-height: 40px;
            cursor: pointer;
            background: linear-gradient(180deg, rgba(89,215,255,0.16), rgba(255,255,255,0.02));
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }}
        .button-link {{
            padding: 0 14px;
        }}
        .panel-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            margin-bottom: 12px;
        }}
        .panel-header h2, .panel-header h3 {{ margin: 0; font-size: 16px; }}
        .panel-header .sub {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
        .count {{
            border-radius: 999px;
            border: 1px solid var(--line);
            padding: 6px 10px;
            color: var(--accent);
            font-weight: bold;
        }}
        .table-wrap {{
            overflow-x: auto;
            border: 1px solid var(--line);
            border-radius: 14px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            min-width: 1080px;
        }}
        thead {{
            background: rgba(255,255,255,0.04);
        }}
        th, td {{
            padding: 12px 14px;
            border-bottom: 1px solid var(--line);
            text-align: left;
            vertical-align: top;
            font-size: 12px;
        }}
        tbody tr:hover {{ background: rgba(255,255,255,0.03); }}
        .good {{ color: var(--green); }}
        .mixed {{ color: var(--amber); }}
        .bad {{ color: var(--red); }}
        @media (max-width: 720px) {{
            .shell {{ padding: 12px; }}
            .hero h1 {{ font-size: 22px; }}
        }}
    </style>
</head>
<body>
    <div class="shell">
        <section class="panel">
            <div class="hero">
                <div>
                    <h1>Trade Coach Review Center</h1>
                    <p>Aggregated post-trade AI reviews across Mai Tai bots. Use this page to scan verdicts, isolate a path or symbol, and decide which trades deserve deeper manual review.</p>
                    {nav_html}
                </div>
                <div class="hero-cards">
                    <div class="hero-card"><span>Visible Reviews</span><strong>{visible_count}</strong></div>
                    <div class="hero-card"><span>Good</span><strong class="good">{summary["good"]}</strong></div>
                    <div class="hero-card"><span>Mixed</span><strong class="mixed">{summary["mixed"]}</strong></div>
                    <div class="hero-card"><span>Bad</span><strong class="bad">{summary["bad"]}</strong></div>
                    <div class="hero-card"><span>Manual Review</span><strong>{summary["manual_review"]}</strong></div>
                    <div class="hero-card"><span>Should Skip</span><strong>{summary["should_skip"]}</strong></div>
                    <div class="hero-card"><span>Priority Queue</span><strong>{queue_count}</strong></div>
                    <div class="hero-card"><span>Pattern Signals</span><strong>{signal_count}</strong></div>
                </div>
            </div>
        </section>

        <section class="panel">
            <div class="panel-header">
                <div>
                    <h2>Review Filters</h2>
                    <div class="sub">Filter the persisted coach ledger without changing the live bot pages. Date range defaults to today, but you can widen it to prior history here.</div>
                </div>
            </div>
            <form method="get" action="/coach/reviews" class="filters">
                <label>
                    Start Date
                    <input type="date" name="start_date" value="{escape(selected_start_date)}" />
                </label>
                <label>
                    End Date
                    <input type="date" name="end_date" value="{escape(selected_end_date)}" />
                </label>
                <label>
                    Strategy
                    <select name="strategy_code">
                        <option value="">All bots</option>
                        {strategy_options}
                    </select>
                </label>
                <label>
                    Verdict
                    <select name="verdict">
                        <option value="">All verdicts</option>
                        {verdict_options}
                    </select>
                </label>
                <label>
                    Focus
                    <select name="coaching_focus">
                        <option value="">All focuses</option>
                        {focus_options}
                    </select>
                </label>
                <label>
                    Symbol
                    <input type="text" name="symbol" value="{escape(selected_symbol)}" placeholder="USEG" />
                </label>
                <div class="filter-actions">
                    <button type="submit">Apply Filters</button>
                    <a class="button-link" href="/coach/reviews">Clear</a>
                    <a class="button-link" href="/api/coach-reviews">JSON API</a>
                </div>
            </form>
        </section>

        <section class="panel">
            <div class="panel-header">
                <div>
                    <h3>Operator Guidance</h3>
                    <div class="sub">Direct takeaways from the current caution signals. This is the first step toward the eventual "we have seen this before, be careful" workflow.</div>
                </div>
                <span class="count">{guidance_count}</span>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Level</th>
                            <th>Guidance</th>
                            <th>Why</th>
                            <th>What To Do</th>
                        </tr>
                    </thead>
                    <tbody>{guidance_rows}</tbody>
                </table>
            </div>
        </section>

        <section class="panel">
            <div class="panel-header">
                <div>
                    <h3>Pattern Signals</h3>
                    <div class="sub">This is the first operator-facing bridge toward live caution logic: recent path and regime groups that have been weak, mixed, or coach-flagged in the selected history window.</div>
                </div>
                <span class="count">{signal_count}</span>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Signal</th>
                            <th>Pattern</th>
                            <th>Trades</th>
                            <th>Mix</th>
                            <th>Coach Take</th>
                        </tr>
                    </thead>
                    <tbody>{signal_rows}</tbody>
                </table>
            </div>
        </section>

        <section class="panel">
            <div class="panel-header">
                <div>
                    <h3>Path Scoreboard</h3>
                    <div class="sub">How reviewed paths have behaved in this filter window. This helps us see whether a setup type has been paying, fading, or needing tighter confirmation.</div>
                </div>
                <span class="count">{path_pattern_count}</span>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Path</th>
                            <th>Trades</th>
                            <th>Verdict Mix</th>
                            <th>Avg Result</th>
                            <th>Avg Quality</th>
                            <th>Caution</th>
                        </tr>
                    </thead>
                    <tbody>{path_pattern_rows}</tbody>
                </table>
            </div>
        </section>

        <section class="panel">
            <div class="panel-header">
                <div>
                    <h3>Regime Scoreboard</h3>
                    <div class="sub">Trade regimes grouped by price, volume, volatility, and momentum context. This is where we start seeing whether a broader market pattern has been strong or fragile lately.</div>
                </div>
                <span class="count">{regime_pattern_count}</span>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Regime</th>
                            <th>Trades</th>
                            <th>Verdict Mix</th>
                            <th>Avg Result</th>
                            <th>Avg Quality</th>
                            <th>Caution</th>
                        </tr>
                    </thead>
                    <tbody>{regime_pattern_rows}</tbody>
                </table>
            </div>
        </section>

        <section class="panel">
            <div class="panel-header">
                <div>
                    <h3>Priority Review Queue</h3>
                    <div class="sub">Trades that deserve the next operator look based on verdict severity, manual-review flags, weak quality scores, or red closes.</div>
                </div>
                <span class="count">{queue_count}</span>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Priority</th>
                            <th>Trade Window</th>
                            <th>Bot</th>
                            <th>Ticker</th>
                            <th>Why Surfaced</th>
                            <th>Open</th>
                        </tr>
                    </thead>
                    <tbody>{queue_rows}</tbody>
                </table>
            </div>
        </section>

        <section class="panel">
            <div class="panel-header">
                <div>
                    <h3>Recent Coach Reviews</h3>
                    <div class="sub">Shows up to the latest 100 filtered reviews. Bot pages still show per-bot context, but this page is the fastest place to scan quality patterns.</div>
                </div>
                <span class="count">{visible_count}</span>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Trade Window</th>
                            <th>Bot</th>
                            <th>Ticker</th>
                            <th>Trade Facts</th>
                            <th>Coach Verdict</th>
                            <th>Should Trade</th>
                            <th>Coach Notes</th>
                        </tr>
                    </thead>
                    <tbody>{review_rows}</tbody>
                </table>
            </div>
        </section>
    </div>
</body>
</html>"""


def _build_completed_position_rows(
    bot: dict[str, Any],
    recent_orders: list[dict[str, Any]],
    recent_fills: list[dict[str, Any]],
) -> tuple[str, int, float]:
    completed_rows = _collect_completed_position_rows(bot, recent_orders, recent_fills)
    if not completed_rows:
        return (
            '<tr><td colspan="9" style="text-align:center;color:#888;">No completed positions</td></tr>',
            0,
            0.0,
        )

    completed_pnl = sum(_as_float(row.get("pnl")) for row in completed_rows)
    rendered_rows: list[str] = []
    for row in sorted(
        completed_rows,
        key=lambda item: _parse_et_timestamp(str(item.get("sort_time", "") or "")),
        reverse=True,
    )[:25]:
        pnl = _as_float(row.get("pnl"))
        color = "#00c853" if pnl >= 0 else "#ff1744"
        rendered_rows.append(
            f"""<tr>
            <td><strong>{escape(str(row.get("ticker", "")) or "-")}</strong></td>
            <td style="white-space:nowrap;">{escape(str(row.get("path", "")) or "-")}</td>
            <td style="text-align:right">{escape(str(row.get("quantity", "")) or "-")}</td>
            <td style="white-space:nowrap;">{escape(str(row.get("entry_time", "")) or "-")}</td>
            <td style="text-align:right">{escape(str(row.get("entry_price", "")) or "-")}</td>
            <td style="white-space:nowrap;">{escape(str(row.get("exit_time", "")) or "-")}</td>
            <td style="text-align:right">{escape(str(row.get("exit_price", "")) or "-")}</td>
            <td style="color:{color};white-space:nowrap;">${pnl:+.2f} ({_as_float(row.get("pnl_pct")):+.1f}%)</td>
            <td title="{escape(str(row.get("summary", "")) or "-")}" style="font-size:11px;white-space:nowrap;max-width:320px;overflow:hidden;text-overflow:ellipsis;">{escape(str(row.get("summary", "")) or "-")}</td>
        </tr>"""
    )
    return "".join(rendered_rows), len(completed_rows), completed_pnl


def _collect_completed_position_rows(
    bot: dict[str, Any],
    recent_orders: list[dict[str, Any]],
    recent_fills: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    strategy_code = str(bot.get("strategy_code", "") or "")
    account_name = str(bot.get("account_name", "") or "")
    cycles = collect_completed_trade_cycles(
        strategy_code=strategy_code,
        broker_account_name=account_name,
        recent_orders=recent_orders,
        recent_fills=recent_fills,
        closed_today=bot.get("closed_today", []),
    )
    return [
        {
            "ticker": cycle.symbol,
            "path": cycle.path,
            "quantity": _fmt_qty(cycle.quantity),
            "entry_time": cycle.entry_time,
            "entry_price": _fmt_money(cycle.entry_price),
            "exit_time": cycle.exit_time,
            "exit_price": _fmt_money(cycle.exit_price),
            "pnl": cycle.pnl,
            "pnl_pct": cycle.pnl_pct,
            "summary": cycle.summary,
            "sort_time": cycle.sort_time,
            "cycle_key": cycle.cycle_key,
        }
        for cycle in cycles
    ]


def _dedupe_decision_events(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str, str]] = set()
    for item in items:
        signature = (
            str(item.get("last_bar_at", "") or ""),
            str(item.get("symbol", "") or item.get("ticker", "") or "").upper(),
            str(item.get("status", "") or "").upper(),
            str(item.get("reason", "") or ""),
            str(item.get("path", "") or ""),
            str(item.get("score", "") if item.get("score", "") not in (None, "") else ""),
            str(item.get("price", "") if item.get("price", "") not in (None, "") else ""),
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(item)
    return deduped


def _display_order_reason(item: dict[str, Any]) -> str:
    reason = str(item.get("reason", "") or "").strip()
    if reason and not looks_like_broker_payload_text(reason):
        return reason
    intent_type = str(item.get("intent_type", "") or "").strip().upper()
    if intent_type == "OPEN":
        path = display_order_path(item)
        return f"ENTRY_{path}" if path and path != "-" else "ENTRY"
    if intent_type:
        return intent_type
    return "-"


def _build_retention_status_html(retention_rows: list[dict[str, Any]], *, tracked_symbols: set[str]) -> str:
    groups: dict[str, list[str]] = {"active": [], "cooldown": [], "resume_probe": [], "dropped": []}
    for item in retention_rows:
        ticker = str(item.get("ticker", "") or "").upper()
        state = str(item.get("state", "") or "").lower()
        if not ticker or ticker not in tracked_symbols or state not in groups:
            continue
        groups[state].append(ticker)

    labels = {
        "active": ("Feed Live", "#00c853"),
        "cooldown": ("Cooldown", "#ffcc5b"),
        "resume_probe": ("Resume / Reclaim", "#59d7ff"),
        "dropped": ("Dropped", "#ff6b6b"),
    }
    sections: list[str] = []
    for state in ("active", "cooldown", "resume_probe", "dropped"):
        symbols = groups[state]
        if not symbols:
            continue
        label, color = labels[state]
        chips = "".join(
            f'<span class="pill-chip" style="border-color:{color};color:{color};">{escape(symbol)}</span>'
            for symbol in symbols
        )
        sections.append(
            f'<div class="line-item"><strong style="color:{color};">{escape(label)}:</strong> {chips}</div>'
        )
    if not sections:
        return '<div class="line-item" style="color:#7b86a4;">No tracked symbols are currently in feed-retention state.</div>'
    return "".join(sections)


def _parse_et_timestamp(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=EASTERN_TZ)
    try:
        return datetime.strptime(value, "%Y-%m-%d %I:%M:%S %p ET").replace(tzinfo=EASTERN_TZ)
    except ValueError:
        try:
            parsed_time = datetime.strptime(value, "%I:%M:%S %p ET")
            current_et = utcnow().astimezone(EASTERN_TZ)
            return current_et.replace(
                hour=parsed_time.hour,
                minute=parsed_time.minute,
                second=parsed_time.second,
                microsecond=0,
            )
        except ValueError:
            return datetime.min.replace(tzinfo=EASTERN_TZ)


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
    strategy_account_rows = [
        item
        for item in account_rows
        if str(item.get("symbol", "")).upper() in strategy_symbols
    ]
    gross_market_value = sum(_as_float(item.get("market_value")) for item in strategy_account_rows)
    latest_updated_at = max((str(item.get("updated_at", "")) for item in account_rows), default="")
    return {
        "account_position_count": len(strategy_account_rows),
        "strategy_symbol_count": len(strategy_symbols),
        "non_strategy_symbol_count": len(other_symbols),
        "non_strategy_symbols": other_symbols,
        "gross_market_value": gross_market_value,
        "latest_updated_at": latest_updated_at,
    }


def _build_bot_account_rows(data: dict[str, Any], bot: dict[str, Any]) -> str:
    strategy_symbols = {
        str(item.get("symbol", "")).upper()
        for item in data["virtual_positions"]
        if item.get("strategy_code") == bot["strategy_code"] and item.get("symbol")
    }
    rows = [
        item for item in data["account_positions"] if item.get("broker_account_name") == bot["account_name"]
        and str(item.get("symbol", "")).upper() in strategy_symbols
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
        and (
            str(item.get("symbol", "")).upper() in runtime_positions
            or str(item.get("symbol", "")).upper() in virtual_positions
        )
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
        return '<tr><td colspan="7" style="text-align:center;color:#888;padding:28px 15px;">No open positions</td></tr>'

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


def _build_bot_decision_entries(decision_items: list[dict[str, Any]]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for item in decision_items:
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
    return entries[:50]


def _build_bot_decision_rows(decision_items: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for item in decision_items[:50]:
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
        return '<tr><td colspan="7" style="text-align:center;color:#888;">No recent decision-tape events in current runtime memory</td></tr>'
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

    failures.sort(key=lambda item: item.get("updated", ""), reverse=True)
    if not failures:
        return '<tr><td colspan="8" style="text-align:center;color:#888;">No recent failed orders</td></tr>', 0

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
    global_manual_stop_symbols: set[str],
) -> str:
    if not rows:
        return '<tr><td colspan="13" style="text-align:center;color:#888;padding:20px;">No confirmed candidates yet</td></tr>'
    rendered = []
    for index, item in enumerate(rows, start=1):
        ticker = str(item.get("ticker", "")).upper()
        live_badge = ' <span style="color:#00ff41;font-size:10px;">LIVE</span>' if ticker in live_symbols else ""
        top5_badge = (
            ' <span style="background:#ffd600;color:#000;font-size:9px;padding:1px 4px;border-radius:3px;font-weight:bold;">TOP5</span>'
            if item.get("is_top5")
            else ""
        )
        bot_badge = (
            ' <span style="background:#00c853;color:#001b07;font-size:9px;padding:1px 4px;border-radius:3px;font-weight:bold;">BOT</span>'
            if item.get("is_handed_to_bot")
            else ""
        )
        change_pct = _as_float(item.get("change_pct"))
        row_bg = "#0a1a0a" if item.get("is_handed_to_bot") else "transparent"
        news_icon_html = _render_confirmed_news_icon(item)
        catalyst_html = _render_confirmed_catalyst_cell(item)
        control_html = _render_confirmed_manual_stop_action(
            ticker,
            manual_stop_symbols=global_manual_stop_symbols,
        )
        rendered.append(
            f"""<tr style="background:{row_bg};">
            <td style="text-align:center">{index}</td>
            <td><strong>{escape(ticker)}</strong>{live_badge}{top5_badge}{bot_badge}</td>
            <td style="color:#ffd600;font-weight:bold;">{_as_float(item.get("rank_score")):.0f}</td>
            <td style="color:#00ff41;">{escape(str(item.get("confirmed_at", item.get("first_spike_time", ""))))}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("entry_price")))}</td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("price")))}</td>
            <td style="text-align:right;color:{'#00c853' if change_pct >= 0 else '#ff1744'}">{change_pct:+.1f}%</td>
            <td style="text-align:right">{_short_volume(item.get("volume"))}</td>
            <td style="text-align:right">{_as_float(item.get("rvol")):.1f}x</td>
            <td style="text-align:right">{int(item.get("squeeze_count", 0) or 0)}</td>
            <td>{escape(str(item.get("first_spike_time", "")))}</td>
            <td style="font-size:12px;min-width:180px;max-width:320px;white-space:normal;overflow-wrap:anywhere;"><div style="display:flex;gap:8px;align-items:flex-start;"><div style="padding-top:2px;">{news_icon_html}</div><div style="flex:1;">{catalyst_html}</div></div></td>
            <td>{control_html}</td>
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
        return '<span style="color:#61758a;font-size:14px;" title="No news article">🚫</span>'
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


def _render_confirmed_manual_stop_action(ticker: str, *, manual_stop_symbols: set[str]) -> str:
    if not ticker:
        return '<span style="color:#61758a;">—</span>'

    action = "resume" if ticker in manual_stop_symbols else "stop"
    label = "Resume" if action == "resume" else "Stop"
    color = "#5fff8d" if action == "resume" else "#ff6b6b"
    url = (
        f'/scanner/symbol/{action}?symbol={quote(ticker)}&redirect_to='
        f'{quote("/scanner/dashboard", safe="/")}'
    )
    return (
        f'<a href="{escape(url)}" '
        f'style="color:{color};font-size:11px;padding:2px 6px;border:1px solid {color};'
        'border-radius:3px;text-decoration:none;display:inline-block;" '
        f'title="{escape(label)} {escape(ticker)} for all bots">{escape(label)}</a>'
    )


def _render_scanner_manual_stop_entries(symbols: list[str]) -> str:
    normalized = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
    if not normalized:
        return '<div class="line-item">No global manual stops.</div>'

    rendered: list[str] = []
    for symbol in normalized:
        resume_url = (
            f'/scanner/symbol/resume?symbol={quote(symbol)}&redirect_to='
            f'{quote("/scanner/dashboard", safe="/")}'
        )
        rendered.append(
            f"""<div class="line-item">
            <strong>{escape(symbol)}</strong><br>
            <span style="font-size:11px;color:#98a6c8;">Blocked from handoff to all bots</span><br>
            <a href="{escape(resume_url)}" style="color:#5fff8d;font-size:11px;text-decoration:none;">resume</a>
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


def _render_alert_diagnostic_rows(diagnostics: list[dict[str, Any]]) -> str:
    if not diagnostics:
        return '<tr><td colspan="6" style="text-align:center;color:#888;padding:20px;">No blocked alert candidates recorded yet</td></tr>'
    rendered = []
    for item in reversed(diagnostics[-25:]):
        reasons = [
            str(reason)
            for reason in item.get("reasons", [])
            if str(reason).strip()
        ]
        reasons_html = escape(", ".join(reasons[:4]) if reasons else "candidate_seen_but_no_alert_fired")
        metrics: list[str] = []
        squeeze_5 = item.get("squeeze_5min_pct")
        squeeze_10 = item.get("squeeze_10min_pct")
        if squeeze_5 is not None:
            metrics.append(f"5m={float(squeeze_5):+.1f}%")
        if squeeze_10 is not None:
            metrics.append(f"10m={float(squeeze_10):+.1f}%")
        vol_5min = int(item.get("vol_5min", 0) or 0)
        expected_5min = int(item.get("expected_5min", 0) or 0)
        if vol_5min > 0 or expected_5min > 0:
            metrics.append(f"5m vol {_short_volume(vol_5min)}/{_short_volume(expected_5min)}")
        if item.get("volume_gate_open"):
            metrics.append("gate=open")
        metrics_html = escape(" | ".join(metrics) if metrics else "-")
        rendered.append(
            f"""<tr>
            <td>{escape(str(item.get("time", "")))}</td>
            <td><strong>{escape(str(item.get("ticker", "")))}</strong></td>
            <td style="text-align:right">{_fmt_money(_as_float(item.get("price")))}</td>
            <td style="text-align:right">{_short_volume(item.get("volume"))}</td>
            <td>{reasons_html}</td>
            <td>{metrics_html}</td>
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
