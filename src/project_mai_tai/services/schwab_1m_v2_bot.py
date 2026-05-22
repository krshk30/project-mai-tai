"""Service entrypoint for the isolated `schwab_1m_v2` bot.

Sixth service. Runs as its own systemd unit. Subscribes to the existing
`mai_tai:strategy-state` Redis stream to pick up the scanner's confirmed
symbol set, polls Schwab REST for 1m bars + quotes, evaluates the strategy
(placeholder), emits intents to `mai_tai:strategy-intents` for OMS to
consume.

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

from redis.asyncio import Redis

from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
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


class SchwabV2BotService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.redis: Redis | None = None
        self.strategy = SchwabV2Strategy(self.settings)
        self.rest_client: SchwabV2RestClient | None = None
        self.intent_emitter: SchwabV2IntentEmitter | None = None
        self._stop_event = asyncio.Event()
        self._strategy_state_stream = stream_name(
            self.settings.redis_stream_prefix, "strategy-state"
        )
        self._strategy_state_last_id = "$"

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

        tasks = [
            asyncio.create_task(self._heartbeat_loop()),
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

    async def _scanner_consumer_loop(self) -> None:
        """Tail mai_tai:strategy-state and feed confirmed symbols into the
        REST client's watchlist. Falls back gracefully if no events arrive.
        """
        assert self.redis is not None
        assert self.rest_client is not None
        max_watchlist = max(
            1, int(self.settings.strategy_schwab_1m_v2_max_watchlist_size)
        )
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
                    raw = data.get("data") if isinstance(data, dict) else None
                    if not isinstance(raw, str):
                        continue
                    try:
                        event = StrategyStateSnapshotEvent.model_validate_json(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    symbols = self._extract_confirmed_symbols(event)
                    if not symbols:
                        continue
                    selected = sorted(symbols)[:max_watchlist]
                    self.rest_client.set_desired_symbols(set(selected))
                    logger.debug(
                        "schwab_1m_v2 watchlist updated count=%d sample=%s",
                        len(selected),
                        ",".join(selected[:5]),
                    )

    @staticmethod
    def _extract_confirmed_symbols(event: StrategyStateSnapshotEvent) -> set[str]:
        payload = event.payload
        candidates: list[dict | str] = []
        candidates.extend(payload.all_confirmed)
        candidates.extend(payload.top_confirmed)
        # `watchlist` is already a list[str]; mix it in as a safety net.
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
        try:
            draft = self.strategy.on_bar(symbol, bar)
        except Exception:
            logger.exception("schwab_1m_v2 on_bar failed for %s", symbol)
            return
        await self._maybe_emit(draft)

    async def _handle_quote(self, symbol: str, quote: Quote) -> None:
        try:
            draft = self.strategy.on_quote(symbol, quote)
        except Exception:
            logger.exception("schwab_1m_v2 on_quote failed for %s", symbol)
            return
        await self._maybe_emit(draft)

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
