"""Isolated, broker-disconnected Momentum 30 / Momentum 60 paper service."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import json
import logging
from typing import Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.session import build_timed_session_factory
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    IsolatedBotStateEvent,
    StrategyBotStatePayload,
    stream_name,
)
from project_mai_tai.log import configure_logging
from project_mai_tai.momentum_paper.conditions import (
    ConditionSnapshot,
    build_condition_snapshot,
    condition_snapshot_from_payload,
)
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import (
    MOMENTUM_ACCOUNT_NAME,
    MOMENTUM_STRATEGIES,
    MomentumTapeRecord,
    TradePrint,
)
from project_mai_tai.momentum_paper.report import grade_strategy
from project_mai_tai.momentum_paper.store import (
    MomentumPaperStore,
    session_closed_record,
    session_record,
)
from project_mai_tai.settings import Settings, get_settings
from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS


SERVICE_NAME = "momentum-paper"
logger = logging.getLogger(SERVICE_NAME)
_ET = ZoneInfo("America/New_York")
_PREPARE_AT = time(3, 55)
_STREAM_AT = time(4, 0)
_TAIL_AT = time(9, 30)
_CLOSE_AT = time(9, 40, 1)
_HEARTBEAT_SECONDS = 15
_PATH_FLUSH_SECONDS = 0.25
_PATH_FLUSH_ROWS = 500


def detector_health_status(detections: int) -> str:
    del detections
    return "UNCALIBRATED"


def previous_trading_day(day: date) -> date:
    candidate = day - timedelta(days=1)
    while candidate.weekday() >= 5 or candidate in US_MARKET_HOLIDAYS:
        candidate -= timedelta(days=1)
    return candidate


def _raw_value(row: object, *names: str) -> object:
    for name in names:
        if isinstance(row, Mapping) and name in row:
            return row[name]
        value = getattr(row, name, None)
        if value is not None:
            return value
    return None


def _condition_codes(value: object) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(int(item) for item in value)
    return (int(value),)


def decode_raw_messages(messages: Iterable[object] | str | bytes) -> tuple[object, ...]:
    if isinstance(messages, bytes):
        messages = messages.decode("utf-8")
    if isinstance(messages, str):
        decoded = json.loads(messages)
        if isinstance(decoded, list):
            return tuple(decoded)
        return (decoded,)
    return tuple(messages)


def normalize_raw_trade(row: object, *, conditions: ConditionSnapshot) -> TradePrint | None:
    event_type = str(_raw_value(row, "ev", "event_type") or "")
    if event_type != "T":
        return None
    symbol = str(_raw_value(row, "sym", "symbol") or "").upper()
    price_raw = _raw_value(row, "p", "price")
    sip_raw = _raw_value(row, "t", "sip_timestamp", "timestamp")
    if not symbol or price_raw is None or sip_raw is None:
        return None
    try:
        price = Decimal(str(price_raw))
        sip_ts_ms = int(sip_raw)
        size = int(_raw_value(row, "s", "size") or 0)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if price <= 0 or sip_ts_ms <= 0:
        return None
    codes = _condition_codes(_raw_value(row, "c", "conditions"))
    eligible, reason = conditions.classify(codes)
    participant = _raw_value(row, "pt", "y", "participant_timestamp")
    exchange = _raw_value(row, "x", "exchange")
    trf_id = _raw_value(row, "trfi", "trf_id")
    return TradePrint(
        symbol=symbol,
        sip_ts_ms=sip_ts_ms,
        participant_ts_ms=int(participant) if participant is not None else None,
        price=price,
        size=size,
        trade_id=str(_raw_value(row, "i", "id", "trade_id") or ""),
        exchange=int(exchange) if exchange is not None else None,
        trf_id=int(trf_id) if trf_id is not None else None,
        conditions=codes,
        eligible=eligible,
        exclusion_reason=reason,
    )


class MomentumPaperService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        redis_client: Redis | None = None,
        session_factory: sessionmaker[Session] | None = None,
        store: MomentumPaperStore | None = None,
        rest_client_factory: Callable[[], object] | None = None,
        websocket_client_factory: Callable[[], object] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis = redis_client or Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.session_factory = session_factory
        self.store = store
        self._rest_client_factory = rest_client_factory or self._build_rest_client
        self._websocket_client_factory = websocket_client_factory or self._build_websocket_client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._session_date: date | None = None
        self._condition_snapshot: ConditionSnapshot | None = None
        self._engine: MomentumPaperEngine | None = None
        self._websocket: object | None = None
        self._websocket_task: asyncio.Task[None] | None = None
        self._connected = False
        self._tail_mode = False
        self._last_heartbeat: datetime | None = None
        self._completed_from_store: list[dict[str, object]] = []
        self._historical_completed: list[dict[str, object]] = []
        self._complete_sessions = 0
        self._session_closed = False
        self._feed_gap_started_ms: int | None = None
        self._path_buffer: list[MomentumTapeRecord] = []
        self._last_path_flush_at = self._clock()

    async def run(self) -> None:
        if not bool(getattr(self.settings, "momentum_paper_enabled", False)):
            logger.info("[MOMENTUM-PAPER] disabled; no feed or persistence opened")
            return
        if not self.settings.massive_api_key:
            raise RuntimeError("momentum paper requires MAI_TAI_MASSIVE_API_KEY")
        if self.session_factory is None:
            self.session_factory = build_timed_session_factory(
                self.settings, service=SERVICE_NAME, profile="fast"
            )
        if self.store is None:
            self.store = MomentumPaperStore(self.session_factory)
        logger.info("[MOMENTUM-PAPER] starting isolated paper observer; broker_route=none")
        try:
            while True:
                await self._tick()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            await self._stop_stream()
            if self._engine is not None:
                await self._persist(self._engine.close_session(self._now_ms()))
            raise

    async def _tick(self) -> None:
        now = self._clock()
        et = now.astimezone(_ET)
        if et.weekday() >= 5 or et.date() in US_MARKET_HOLIDAYS:
            await self._stop_stream()
            return
        if self._session_date != et.date():
            await self._stop_stream()
            self._session_date = et.date()
            self._condition_snapshot = None
            self._engine = None
            self._tail_mode = False
            self._completed_from_store = []
            self._historical_completed = []
            self._complete_sessions = 0
            self._session_closed = False
        if _PREPARE_AT <= et.time() < _CLOSE_AT and self._engine is None:
            await self._prepare_session(et.date())
        if self._engine is None:
            return
        await self._persist(self._engine.advance_clock(self._now_ms()))
        await self._flush_path_buffer_if_due()
        if _STREAM_AT <= et.time() < _CLOSE_AT and self._websocket_task is None:
            await self._start_stream()
        if et.time() >= _TAIL_AT and not self._tail_mode and self._websocket is not None:
            self._enter_tail_mode()
        if et.time() >= _CLOSE_AT and not self._session_closed:
            await self._persist(self._engine.close_session(self._now_ms()))
            await self._stop_stream()
            closed = session_closed_record(
                session_date=et.date(),
                observed_at=now,
                excluded_prints=self._engine.session_excluded_prints,
            )
            await self._persist([closed])
            self._session_closed = True
        if (
            self._last_heartbeat is None
            or (now - self._last_heartbeat).total_seconds() >= _HEARTBEAT_SECONDS
        ):
            await self._publish_state()
            self._last_heartbeat = now

    async def _prepare_session(self, session_date: date) -> None:
        assert self.store is not None
        existing = await asyncio.to_thread(self.store.load_session, session_date)
        ready = self.store.latest_session_ready(existing)
        if ready is None:
            prior_day = previous_trading_day(session_date)
            snapshot, prior_closes = await asyncio.to_thread(
                self._fetch_reference_inputs, prior_day
            )
            ready = session_record(
                session_date=session_date,
                observed_at=self._clock(),
                prior_close_date=prior_day,
                prior_closes={symbol: str(price) for symbol, price in prior_closes.items()},
                condition_snapshot=snapshot.payload(),
            )
            await asyncio.to_thread(self.store.append_many, [ready])
            existing.append(ready)
        payload = ready.payload
        raw_closes = payload.get("prior_closes")
        raw_conditions = payload.get("condition_snapshot")
        if not isinstance(raw_closes, Mapping) or not isinstance(raw_conditions, Mapping):
            raise RuntimeError("persisted Momentum session evidence is malformed")
        self._condition_snapshot = condition_snapshot_from_payload(raw_conditions)
        self._engine = MomentumPaperEngine(
            prior_closes={str(symbol): Decimal(str(price)) for symbol, price in raw_closes.items()},
            condition_version=self._condition_snapshot.version,
            coverage_started_ms=self._now_ms(),
        )
        self._engine.seed_reentry_boundaries(existing)
        incomplete = self.store.incomplete_logical_ids(existing)
        if incomplete:
            interrupted = self._interrupted_records(existing, incomplete)
            await asyncio.to_thread(self.store.append_many, interrupted)
            existing.extend(interrupted)
            logger.warning(
                "[MOMENTUM-PAPER-RECOVERY] marked %d interrupted forward paths UNANSWERABLE",
                len(interrupted),
            )
        self._completed_from_store = [
            dict(row.payload)
            for row in existing
            if row.event_type in {"FINAL", "NO_FILL", "UNANSWERABLE"}
        ]
        self._session_closed = any(row.event_type == "SESSION_CLOSED" for row in existing)
        closed_dates = await asyncio.to_thread(self.store.load_closed_session_dates, limit=20)
        self._complete_sessions = len(closed_dates)
        if closed_dates:
            historical = await asyncio.to_thread(self.store.load_completed_since, min(closed_dates))
            self._historical_completed = [
                row
                for row in historical
                if str(row.get("session_date")) != session_date.isoformat()
            ]
        logger.info(
            "[MOMENTUM-PAPER-SESSION] ready date=%s prior_close_date=%s symbols=%d "
            "condition_version=%s restored_terminal=%d",
            session_date,
            payload.get("prior_close_date"),
            len(raw_closes),
            self._condition_snapshot.version,
            len(self._completed_from_store),
        )

    def _fetch_reference_inputs(
        self, prior_day: date
    ) -> tuple[ConditionSnapshot, dict[str, Decimal]]:
        client = self._rest_client_factory()
        retrieved_at = self._clock()
        condition_rows = list(
            client.list_conditions(
                asset_class="stocks",
                data_type="trade",
                limit=1000,
                sort="id",
                order="asc",
            )
        )
        snapshot = build_condition_snapshot(condition_rows, retrieved_at=retrieved_at)
        grouped = client.get_grouped_daily_aggs(prior_day, adjusted=True)
        closes: dict[str, Decimal] = {}
        for row in grouped:
            symbol = str(_raw_value(row, "ticker", "T") or "").upper()
            close = _raw_value(row, "close", "c")
            if not symbol or close is None:
                continue
            try:
                value = Decimal(str(close))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if value > 0:
                closes[symbol] = value
        if not closes:
            raise RuntimeError(
                f"Massive returned no grouped daily closes for {prior_day}; refusing session"
            )
        return snapshot, closes

    async def _start_stream(self) -> None:
        if self._websocket is not None or self._websocket_task is not None:
            await self._stop_stream()
        websocket = self._websocket_client_factory()
        self._websocket = websocket
        websocket.subscribe("T.*")
        self._tail_mode = False
        self._websocket_task = asyncio.create_task(self._connect(websocket))
        logger.info("[MOMENTUM-PAPER-FEED] subscribed=T.* mode=global")

    async def _connect(self, websocket: object) -> None:
        try:
            await websocket.connect(self._handle_messages)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[MOMENTUM-PAPER-FEED] disconnected; current paths fail closed")
        finally:
            unexpected_disconnect = self._websocket is websocket
            self._connected = False
            if unexpected_disconnect and self._feed_gap_started_ms is None:
                self._feed_gap_started_ms = self._now_ms()
                logger.warning("[MOMENTUM-PAPER-FEED] stream ended; current paths fail closed")
            close = getattr(websocket, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            if self._websocket is websocket:
                self._websocket = None
            if self._websocket_task is asyncio.current_task():
                self._websocket_task = None

    async def _handle_messages(self, messages: Iterable[object] | str | bytes) -> None:
        self._connected = True
        if self._feed_gap_started_ms is not None and self._engine is not None:
            await self._persist(
                self._engine.mark_feed_gap(self._feed_gap_started_ms, self._now_ms())
            )
            self._feed_gap_started_ms = None
        if self._engine is None or self._condition_snapshot is None:
            return
        for row in decode_raw_messages(messages):
            trade = normalize_raw_trade(row, conditions=self._condition_snapshot)
            if trade is None:
                continue
            await self._persist(self._engine.ingest(trade))

    def _enter_tail_mode(self) -> None:
        assert self._websocket is not None
        assert self._engine is not None
        symbols = sorted(
            {str(row["symbol"]) for row in self._engine.active_events if str(row.get("symbol", ""))}
        )
        self._websocket.unsubscribe("T.*")
        if symbols:
            self._websocket.subscribe(*[f"T.{symbol}" for symbol in symbols])
        self._tail_mode = True
        logger.info(
            "[MOMENTUM-PAPER-FEED] mode=symbol_tail symbols=%s",
            ",".join(symbols) or "none",
        )

    async def _stop_stream(self) -> None:
        websocket = self._websocket
        task = self._websocket_task
        self._websocket = None
        self._websocket_task = None
        self._connected = False
        if websocket is not None:
            result = websocket.close()
            if asyncio.iscoroutine(result):
                await result
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._flush_path_buffer(force=True)

    async def _persist(self, records: Iterable[MomentumTapeRecord]) -> None:
        rows = tuple(records)
        if not rows:
            return
        path_rows = [row for row in rows if row.event_type == "PATH_PRINT"]
        durable_transitions = [row for row in rows if row.event_type != "PATH_PRINT"]
        self._path_buffer.extend(path_rows)
        if not durable_transitions:
            await self._flush_path_buffer_if_due()
            return
        assert self.store is not None
        batch = [*self._path_buffer, *durable_transitions]
        self._path_buffer.clear()
        await asyncio.to_thread(self.store.append_many, batch)
        self._last_path_flush_at = self._clock()

    async def _flush_path_buffer_if_due(self) -> None:
        if not self._path_buffer:
            return
        age = (self._clock() - self._last_path_flush_at).total_seconds()
        if len(self._path_buffer) >= _PATH_FLUSH_ROWS or age >= _PATH_FLUSH_SECONDS:
            await self._flush_path_buffer(force=True)

    async def _flush_path_buffer(self, *, force: bool) -> None:
        if not self._path_buffer or not force:
            return
        assert self.store is not None
        batch = list(self._path_buffer)
        self._path_buffer.clear()
        await asyncio.to_thread(self.store.append_many, batch)
        self._last_path_flush_at = self._clock()

    async def _publish_state(self) -> None:
        if self._engine is None:
            return
        all_completed = (
            self._historical_completed
            + self._completed_from_store
            + list(self._engine.completed_events)
        )
        heartbeat = HeartbeatEvent(
            source_service=SERVICE_NAME,
            payload=HeartbeatPayload(
                service_name=SERVICE_NAME,
                instance_name=SERVICE_NAME,
                status="healthy" if self._connected else "degraded",
                details={
                    "execution_mode": "paper",
                    "broker_route": "none",
                    "streamer_connected": str(self._connected).lower(),
                    "subscription_mode": "symbol_tail" if self._tail_mode else "T.*",
                    "active_paths": str(len(self._engine.active_events)),
                    "excluded_prints": str(self._engine.session_excluded_prints),
                },
            ),
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "heartbeats"),
            {"data": heartbeat.model_dump_json()},
            maxlen=self.settings.redis_heartbeat_stream_maxlen,
            approximate=True,
        )
        for strategy_code in MOMENTUM_STRATEGIES:
            state = IsolatedBotStateEvent(
                source_service=SERVICE_NAME,
                payload=self._build_bot_state(strategy_code, all_completed),
            )
            await self.redis.xadd(
                stream_name(self.settings.redis_stream_prefix, "strategy-state-isolated"),
                {"data": state.model_dump_json()},
                maxlen=self.settings.redis_strategy_state_isolated_stream_maxlen,
                approximate=True,
            )

    def _build_bot_state(
        self, strategy_code: str, completed: list[dict[str, object]]
    ) -> StrategyBotStatePayload:
        assert self._engine is not None
        active = [
            row for row in self._engine.active_events if row["strategy_code"] == strategy_code
        ]
        terminal = [row for row in completed if row.get("strategy_code") == strategy_code]
        today = self._session_date.isoformat() if self._session_date is not None else ""
        today_terminal = [row for row in terminal if str(row.get("session_date")) == today]
        counts = Counter(str(row.get("status", "UNKNOWN")) for row in today_terminal)
        detections = len(active) + len(today_terminal)
        detector_status = detector_health_status(detections)
        recent = sorted(
            today_terminal + active,
            key=lambda row: int(dict(row.get("detect") or {}).get("sip_ts_ms", 0) or 0),
            reverse=True,
        )[:20]
        decisions = [self._decision_row(row) for row in recent]
        open_positions = [row for row in active if row.get("fill") and not row.get("exit")]
        closed = [row for row in today_terminal if row.get("status") == "FINAL"]
        daily_pnl = sum(Decimal(str(row.get("pnl") or "0")) for row in closed)
        grade = grade_strategy(terminal, complete_sessions=self._complete_sessions)
        return StrategyBotStatePayload(
            strategy_code=strategy_code,
            account_name=MOMENTUM_ACCOUNT_NAME,
            watchlist=sorted({str(row["symbol"]) for row in active}),
            positions=[self._position_row(row) for row in open_positions],
            pending_open_symbols=sorted(
                str(row["symbol"]) for row in active if row.get("fill") is None
            ),
            daily_pnl=float(daily_pnl),
            closed_today=[self._closed_row(row) for row in closed],
            recent_decisions=decisions,
            last_tick_at={
                str(row["symbol"]): datetime.fromtimestamp(
                    int(dict(row["detect"])["sip_ts_ms"]) / 1000, tz=UTC
                ).isoformat()
                for row in recent
            },
            data_health={
                "status": "healthy" if self._connected else "waiting",
                "execution_mode": "paper",
                "broker_route": "none",
                "streamer_connected": str(self._connected).lower(),
                "momentum_paper": {
                    "detector_status": detector_status,
                    "events": detections,
                    "filled": sum(bool(row.get("fill")) for row in terminal + active),
                    "open": len(active),
                    "closed": len(closed),
                    "no_fill": counts["NO_FILL"],
                    "unanswerable": counts["UNANSWERABLE"],
                    "excluded_prints": self._engine.session_excluded_prints,
                    "tape_key_collisions": self._engine.tape_key_collisions,
                    "gross_pnl": str(daily_pnl),
                    "grade": {
                        "verdict": grade.verdict,
                        "sessions": grade.sessions,
                        "filled_gradable": grade.filled_gradable,
                    },
                },
            },
        )

    @staticmethod
    def _decision_row(row: dict[str, object]) -> dict[str, object]:
        detected = dict(row.get("detect") or {})
        return {
            "ticker": row.get("symbol"),
            "symbol": row.get("symbol"),
            "status": row.get("status"),
            "reason": row.get("reason") or row.get("exit_reason") or "paper observation",
            "last_tick_at": datetime.fromtimestamp(
                int(detected.get("sip_ts_ms", 0)) / 1000, tz=UTC
            ).isoformat(),
            "price": detected.get("price"),
            "fill_price": dict(row.get("fill") or {}).get("price"),
            "exit_price": dict(row.get("exit") or {}).get("price"),
            "pnl": row.get("pnl"),
            "pnl_pct": row.get("pnl_pct"),
        }

    @staticmethod
    def _position_row(row: dict[str, object]) -> dict[str, object]:
        fill = dict(row.get("fill") or {})
        return {
            "ticker": row["symbol"],
            "symbol": row["symbol"],
            "quantity": row.get("quantity", 0),
            "entry_price": fill.get("price"),
            "current_price": fill.get("price"),
            "entry_time": datetime.fromtimestamp(
                int(fill.get("sip_ts_ms", 0)) / 1000, tz=UTC
            ).isoformat(),
        }

    @staticmethod
    def _closed_row(row: dict[str, object]) -> dict[str, object]:
        fill = dict(row.get("fill") or {})
        exit_row = dict(row.get("exit") or {})
        return {
            "ticker": row["symbol"],
            "symbol": row["symbol"],
            "quantity": row.get("quantity", 0),
            "entry_price": fill.get("price"),
            "entry_time": datetime.fromtimestamp(
                int(fill.get("sip_ts_ms", 0)) / 1000, tz=UTC
            ).isoformat(),
            "exit_price": exit_row.get("price"),
            "exit_time": datetime.fromtimestamp(
                int(exit_row.get("sip_ts_ms", 0)) / 1000, tz=UTC
            ).isoformat(),
            "pnl": row.get("pnl"),
            "pnl_pct": row.get("pnl_pct"),
            "exit_reason": row.get("exit_reason"),
        }

    def _interrupted_records(
        self, rows: list[MomentumTapeRecord], logical_ids: set[str]
    ) -> list[MomentumTapeRecord]:
        detected = {
            row.logical_id: row
            for row in rows
            if row.event_type == "DETECTED" and row.logical_id in logical_ids
        }
        now = self._clock()
        return [
            MomentumTapeRecord(
                event_key=f"{logical_id}:restart:{int(now.timestamp() * 1000)}:UNANSWERABLE",
                logical_id=logical_id,
                strategy_code=row.strategy_code,
                event_type="UNANSWERABLE",
                session_date=row.session_date,
                symbol=row.symbol,
                observed_at=now,
                payload={
                    **dict(row.payload),
                    "status": "UNANSWERABLE",
                    "reason": "service_restart_interrupted_forward_path",
                    "path_complete": False,
                    "terminal_boundary_sip_ts_ms": int(
                        dict(row.payload.get("path_range") or {}).get("end_sip_ts_ms", 0) or 0
                    ),
                },
            )
            for logical_id, row in sorted(detected.items())
        ]

    def _build_rest_client(self) -> object:
        from massive import RESTClient

        return RESTClient(api_key=self.settings.massive_api_key)

    def _build_websocket_client(self) -> object:
        from massive import WebSocketClient

        return WebSocketClient(
            api_key=self.settings.massive_api_key,
            raw=True,
            subscriptions=[],
            max_reconnects=0,
        )

    def _now_ms(self) -> int:
        return int(self._clock().timestamp() * 1000)


async def prove_entitlement(settings: Settings, *, timeout_seconds: int) -> None:
    from massive import WebSocketClient

    seen = asyncio.Event()
    evidence: list[str] = []
    client = WebSocketClient(
        api_key=settings.massive_api_key,
        raw=True,
        subscriptions=[],
        max_reconnects=0,
    )
    client.subscribe("T.*")

    async def handler(messages: Iterable[object] | str | bytes) -> None:
        for row in decode_raw_messages(messages):
            rendered = json.dumps(row, default=str, sort_keys=True)
            if str(_raw_value(row, "ev", "event_type") or "") == "T" or (
                "subscribed" in rendered.lower() and "t.*" in rendered.lower()
            ):
                evidence.append(rendered[:500])
                seen.set()

    task = asyncio.create_task(client.connect(handler))
    try:
        await asyncio.wait_for(seen.wait(), timeout=timeout_seconds)
    finally:
        result = client.close()
        if asyncio.iscoroutine(result):
            await result
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    print(f"[MOMENTUM-ENTITLEMENT] PASS evidence={evidence[0]}")


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entitlement-check", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=15)
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.entitlement_check:
        if not settings.massive_api_key:
            raise RuntimeError("MAI_TAI_MASSIVE_API_KEY is required")
        await prove_entitlement(settings, timeout_seconds=max(1, args.timeout_seconds))
        return
    await MomentumPaperService(settings).run()


def run() -> None:
    settings = get_settings()
    configure_logging(SERVICE_NAME, settings.log_level)
    asyncio.run(main())
