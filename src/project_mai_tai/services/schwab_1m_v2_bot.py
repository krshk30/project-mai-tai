"""Service entrypoint for the isolated `schwab_1m_v2` bot.

Sixth service. Runs as its own systemd unit. Subscribes to the existing
`mai_tai:strategy-state` Redis stream to pick up the scanner's confirmed
symbol set, polls Schwab REST for 1m bars + quotes, evaluates the strategy
(placeholder), persists completed bars to `strategy_bar_history`, publishes
its own state to `mai_tai:strategy-state-isolated` so the dashboard renders
the bot like any other, and emits intents to `mai_tai:strategy-intents` for
OMS to consume.

NO imports from `services/strategy_engine_app.py`, `services/strategy_engine.py`,
`market_data/schwab_streamer.py`, `strategy_core/schwab_native_30s.py`, etc.

Idle (no intents, no REST traffic) when:
- v2 enable flag is off (default), OR
- the Schwab token store is empty / unreadable

This lets the service ship + boot before the operator wires credentials
or flips the enable flag.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import StrategyBarHistory
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    IsolatedBotStateEvent,
    StrategyBotStatePayload,
    StrategyStateSnapshotEvent,
    stream_name,
)
from project_mai_tai.market_data.schwab_v2_rest_client import (
    ChartBar,
    Quote,
    SchwabV2RestClient,
)
from project_mai_tai.settings import Settings, get_settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    SERVICE_NAME,
    STRATEGY_CODE,
    SchwabV2IntentEmitter,
    SchwabV2Strategy,
)

logger = logging.getLogger(__name__)

INTERVAL_SECS = 60
STATE_PUBLISH_INTERVAL_SECONDS = 5
EASTERN_TZ = ZoneInfo("America/New_York")


def _format_eastern(dt: datetime) -> str:
    """Format a datetime as `"YYYY-MM-DD HH:MM:SS AM/PM ET"`, matching the
    existing strategy-engine's `_datetime_str` so the dashboard's max()-based
    derivation of `latest_bot_tick_at` produces a value that's consistent in
    sort order and display with the other bots.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(EASTERN_TZ).strftime("%Y-%m-%d %I:%M:%S %p ET")


class SchwabV2BotService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis: Redis | None = None
        self.strategy = SchwabV2Strategy(self.settings)
        self.rest_client: SchwabV2RestClient | None = None
        self.intent_emitter: SchwabV2IntentEmitter | None = None
        self.session_factory: sessionmaker[Session] | None = session_factory
        self._stop_event = asyncio.Event()
        self._strategy_state_stream = stream_name(
            self.settings.redis_stream_prefix, "strategy-state"
        )
        self._isolated_state_stream = stream_name(
            self.settings.redis_stream_prefix, "strategy-state-isolated"
        )
        self._strategy_state_last_id = "$"
        self._watchlist: set[str] = set()
        self._bar_counts: dict[str, int] = {}
        self._last_tick_at: dict[str, str] = {}
        self._last_bar_at: dict[str, str] = {}
        self._data_health: dict[str, object] = {
            "status": "starting",
            "halted_symbols": [],
            "warning_symbols": [],
        }

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "strategy_schwab_1m_v2_enabled", False))

    async def run(self) -> None:
        logging.basicConfig(
            level=self.settings.log_level.upper(),
            format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        )
        logger.info("schwab_1m_v2 bot starting (enabled=%s)", self.enabled)

        if not self.enabled:
            logger.warning(
                "schwab_1m_v2 disabled: set MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENABLED=true "
                "to activate. Service will heartbeat as degraded and idle."
            )

        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        if self.session_factory is None:
            try:
                self.session_factory = build_session_factory(self.settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "schwab_1m_v2 session_factory unavailable, bar persistence "
                    "disabled: %s",
                    exc,
                )
        self.intent_emitter = SchwabV2IntentEmitter(
            self.settings,
            self.redis,
            broker_account_name=self.settings.strategy_schwab_1m_v2_account_name,
        )
        self.rest_client = SchwabV2RestClient(
            self.settings,
            on_chart_bar=self._handle_bar,
            on_quote=self._handle_quote,
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                # Windows event loops don't support add_signal_handler;
                # the SIGTERM path on Linux is the production case.
                pass

        await self._publish_heartbeat("starting")
        self._data_health["status"] = "healthy" if self.enabled else "degraded"

        tasks = [
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._state_publish_loop()),
        ]
        if self.enabled:
            tasks.append(asyncio.create_task(self.rest_client.run()))
            tasks.append(asyncio.create_task(self._scanner_consumer_loop()))

        try:
            await self._stop_event.wait()
        finally:
            await self._publish_heartbeat("stopping")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.rest_client is not None:
                await self.rest_client.stop()
            if self.redis is not None:
                await self.redis.aclose()

    async def _publish_heartbeat(self, status: str) -> None:
        if self.redis is None:
            return
        event = HeartbeatEvent(
            source_service=SERVICE_NAME,
            payload=HeartbeatPayload(
                service_name=SERVICE_NAME,
                instance_name=SERVICE_NAME,
                status=status,  # type: ignore[arg-type]
                details={
                    "enabled": str(self.enabled).lower(),
                    "strategy_code": STRATEGY_CODE,
                    "rest_configured": str(
                        bool(self.rest_client and self.rest_client.configured)
                    ).lower(),
                    "watchlist_size": str(len(self._watchlist)),
                    "bars_processed": str(sum(self._bar_counts.values())),
                },
            ),
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "heartbeats"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_heartbeat_stream_maxlen,
            approximate=True,
        )

    async def _heartbeat_loop(self) -> None:
        interval = max(5, int(self.settings.service_heartbeat_interval_seconds))
        while not self._stop_event.is_set():
            try:
                status = "healthy" if self.enabled else "degraded"
                await self._publish_heartbeat(status)
            except Exception as exc:  # noqa: BLE001
                logger.warning("schwab_1m_v2 heartbeat failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _state_publish_loop(self) -> None:
        """Publish StrategyBotStatePayload to strategy-state-isolated stream
        so the dashboard renders the v2 bot like any other.
        """
        while not self._stop_event.is_set():
            try:
                await self._publish_bot_state()
            except Exception as exc:  # noqa: BLE001
                logger.warning("schwab_1m_v2 bot-state publish failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=STATE_PUBLISH_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                continue

    async def _publish_bot_state(self) -> None:
        if self.redis is None:
            return
        payload = StrategyBotStatePayload(
            strategy_code=STRATEGY_CODE,
            account_name=self.settings.strategy_schwab_1m_v2_account_name,
            watchlist=sorted(self._watchlist),
            prewarm_symbols=[],
            data_health=dict(self._data_health),
            retention_states=[],
            positions=[],
            pending_open_symbols=[],
            pending_close_symbols=[],
            pending_scale_levels=[],
            daily_pnl=0.0,
            closed_today=[],
            recent_decisions=[],
            indicator_snapshots=[],
            bar_counts=dict(self._bar_counts),
            last_tick_at=dict(self._last_tick_at),
        )
        event = IsolatedBotStateEvent(source_service=SERVICE_NAME, payload=payload)
        await self.redis.xadd(
            self._isolated_state_stream,
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_strategy_state_isolated_stream_maxlen,
            approximate=True,
        )

    async def _scanner_consumer_loop(self) -> None:
        """Seed from the latest existing strategy-state snapshot, then tail
        for new ones. The seed step is critical on cold-start because
        strategy-engine publishes its snapshot only on bar / intent events,
        which can be minutes apart in pre-market; without the seed, the v2
        bot's watchlist stays empty until the next downstream event fires.
        """
        assert self.redis is not None
        assert self.rest_client is not None
        max_watchlist = max(
            1, int(self.settings.strategy_schwab_1m_v2_max_watchlist_size)
        )

        # Step 1: seed from the latest snapshot already in the stream.
        try:
            seed = await self.redis.xrevrange(
                self._strategy_state_stream, count=1
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("schwab_1m_v2 scanner seed xrevrange failed: %s", exc)
            seed = []
        for entry_id, data in seed:
            self._strategy_state_last_id = entry_id
            self._apply_strategy_state_event(data, max_watchlist=max_watchlist)

        # Step 2: tail for new snapshots.
        while not self._stop_event.is_set():
            try:
                response = await self.redis.xread(
                    streams={self._strategy_state_stream: self._strategy_state_last_id},
                    count=10,
                    block=5_000,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("schwab_1m_v2 scanner xread failed: %s", exc)
                await asyncio.sleep(2.0)
                continue
            if not response:
                continue
            for _stream_key, entries in response:
                for entry_id, data in entries:
                    self._strategy_state_last_id = entry_id
                    self._apply_strategy_state_event(data, max_watchlist=max_watchlist)

    def _apply_strategy_state_event(
        self, data: object, *, max_watchlist: int
    ) -> None:
        raw = data.get("data") if isinstance(data, dict) else None
        if not isinstance(raw, str):
            return
        try:
            event = StrategyStateSnapshotEvent.model_validate_json(raw)
        except Exception:  # noqa: BLE001
            return
        symbols = self._extract_confirmed_symbols(event)
        if not symbols:
            return
        selected = set(sorted(symbols)[:max_watchlist])
        if selected == self._watchlist:
            return
        self._watchlist = selected
        if self.rest_client is not None:
            self.rest_client.set_desired_symbols(selected)
        logger.info(
            "schwab_1m_v2 watchlist updated count=%d sample=%s",
            len(selected),
            ",".join(sorted(selected)[:5]),
        )

    @staticmethod
    def _extract_confirmed_symbols(event: StrategyStateSnapshotEvent) -> set[str]:
        payload = event.payload
        candidates: list[dict | str] = []
        candidates.extend(payload.all_confirmed)
        candidates.extend(payload.top_confirmed)
        symbols: set[str] = set()
        for item in candidates:
            if isinstance(item, dict):
                sym = str(item.get("symbol", "")).strip().upper()
                if sym:
                    symbols.add(sym)
            elif isinstance(item, str):
                cleaned = item.strip().upper()
                if cleaned:
                    symbols.add(cleaned)
        for sym in payload.watchlist:
            cleaned = str(sym).strip().upper()
            if cleaned:
                symbols.add(cleaned)
        return symbols

    async def _handle_bar(self, symbol: str, bar: ChartBar) -> None:
        now_et = _format_eastern(datetime.now(UTC))
        self._last_tick_at[symbol] = now_et
        self._last_bar_at[symbol] = now_et
        self._bar_counts[symbol] = self._bar_counts.get(symbol, 0) + 1

        await asyncio.to_thread(self._persist_bar, symbol, bar)

        try:
            draft = self.strategy.on_bar(symbol, bar)
        except Exception:
            logger.exception("schwab_1m_v2 on_bar failed for %s", symbol)
            return
        await self._maybe_emit(draft)

    async def _handle_quote(self, symbol: str, quote: Quote) -> None:
        self._last_tick_at[symbol] = _format_eastern(datetime.now(UTC))
        try:
            draft = self.strategy.on_quote(symbol, quote)
        except Exception:
            logger.exception("schwab_1m_v2 on_quote failed for %s", symbol)
            return
        await self._maybe_emit(draft)

    def _persist_bar(self, symbol: str, bar: ChartBar) -> None:
        """Upsert (strategy_code, symbol, interval_secs, bar_time) into
        strategy_bar_history. Mirrors the shape the existing strategy-engine
        writes so the dashboard's decision-tape query treats v2 bars
        identically. decision_status stays '' until the strategy emits
        signals.
        """
        if self.session_factory is None:
            return

        volume = int(bar.volume or 0)
        # No trade_count from Schwab Price History; synthesize so the
        # vol=0+tc=0 placeholder filter behaves correctly downstream.
        trade_count = 1 if volume > 0 else 0
        if volume == 0 and trade_count == 0:
            return

        bar_time = datetime.fromtimestamp(bar.timestamp_ms / 1000.0, UTC)

        try:
            with self.session_factory() as session:
                record = session.scalar(
                    select(StrategyBarHistory).where(
                        StrategyBarHistory.strategy_code == STRATEGY_CODE,
                        StrategyBarHistory.symbol == symbol,
                        StrategyBarHistory.interval_secs == INTERVAL_SECS,
                        StrategyBarHistory.bar_time == bar_time,
                    )
                )
                if record is None:
                    record = StrategyBarHistory(
                        strategy_code=STRATEGY_CODE,
                        symbol=symbol,
                        interval_secs=INTERVAL_SECS,
                        bar_time=bar_time,
                        open_price=Decimal(str(bar.open)),
                        high_price=Decimal(str(bar.high)),
                        low_price=Decimal(str(bar.low)),
                        close_price=Decimal(str(bar.close)),
                        volume=volume,
                        trade_count=trade_count,
                    )
                    session.add(record)
                else:
                    record.open_price = Decimal(str(bar.open))
                    record.high_price = Decimal(str(bar.high))
                    record.low_price = Decimal(str(bar.low))
                    record.close_price = Decimal(str(bar.close))
                    record.volume = volume
                    record.trade_count = trade_count
                session.commit()
        except Exception:
            logger.exception(
                "schwab_1m_v2 failed to persist bar history for %s @ %s",
                symbol,
                bar_time,
            )

    async def _maybe_emit(self, draft) -> None:  # type: ignore[no-untyped-def]
        if draft is None:
            return
        if self.intent_emitter is None:
            logger.warning("schwab_1m_v2 intent dropped — emitter not initialized")
            return
        try:
            await self.intent_emitter.emit(draft)
        except Exception:
            logger.exception("schwab_1m_v2 emit failed")


async def main() -> None:
    service = SchwabV2BotService()
    await service.run()


def run() -> None:
    asyncio.run(main())


# Re-exports for tests / introspection
__all__ = ["SchwabV2BotService", "SERVICE_NAME", "STRATEGY_CODE", "main", "run"]
