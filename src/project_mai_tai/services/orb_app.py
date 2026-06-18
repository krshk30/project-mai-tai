"""ORB (P6 "OPEN") isolated bot — slice 3a scaffold + gateway-consumer data layer.

Runs as its OWN process/event loop (escapes the shared strategy-engine 1 Hz-loop
contention by construction) and consumes the EXISTING market-data gateway as a
registered consumer (no new Schwab streamer session, no credential collision).

This slice wires the scaffold + data layer ONLY: register the gateway consumer,
drain trade ticks for the universe, aggregate them into 1-min bars, and hand each
completed bar to ``_on_bar`` — a STUB here. Slice 3b implements the pre-09:25
universe read + the ORB entry logic (OR build / breakout / arm-on-window-open via
the ``orb_intrabar`` leaf / the ``trail_pct=8`` open intent).

Default OFF: with ``orb_enabled=False`` ``run()`` returns immediately — the service
registers no consumer, drains nothing, emits nothing (byte-identical to today).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from project_mai_tai.events import (
    MarketDataSubscriptionEvent,
    MarketDataSubscriptionPayload,
    stream_name,
)
from project_mai_tai.settings import Settings, get_settings
from project_mai_tai.strategy_core.orb_intrabar import OrbBar
from project_mai_tai.strategy_core.orb_tick_aggregator import OrbTickAggregator

SERVICE_NAME = "orb"
logger = logging.getLogger(SERVICE_NAME)
_ET = ZoneInfo("America/New_York")


class OrbService:
    def __init__(self, settings: Settings | None = None, redis_client: Redis | None = None) -> None:
        self.settings = settings or get_settings()
        self.redis = redis_client or Redis.from_url(self.settings.redis_url, decode_responses=True)
        self._aggregators: dict[str, OrbTickAggregator] = {}
        self._last_gateway_symbols: list[str] = []
        self._md_offset: str = "$"  # tail new ticks only
        self._bar_count = 0

    # ----- lifecycle -----
    async def run(self) -> None:
        if not bool(getattr(self.settings, "orb_enabled", False)):
            logger.info("[ORB] disabled (orb_enabled=false); not starting")
            return
        logger.info("[ORB] starting — isolated bot, market-data gateway consumer")
        try:
            while True:
                await self._sync_gateway_subscription(self._pre_open_universe())
                await self._drain_market_data()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("[ORB] cancelled; shutting down")
            raise

    # ----- universe (3b fills this in) -----
    def _pre_open_universe(self) -> list[str]:
        """STUB for slice 3a. Slice 3b returns the pre-09:25 confirmed universe
        (names with ``last_confirmed_at <= 09:25``). Empty => no symbols armed."""
        return []

    # ----- gateway consumer registration (mirrors the v2 / strategy-engine pattern) -----
    async def _sync_gateway_subscription(self, symbols: list[str]) -> None:
        desired = sorted({str(s).upper() for s in symbols if str(s).strip()})
        if desired == self._last_gateway_symbols:
            return  # debounce — publish only on change
        self._last_gateway_symbols = desired
        event = MarketDataSubscriptionEvent(
            source_service=SERVICE_NAME,
            payload=MarketDataSubscriptionPayload(
                consumer_name=SERVICE_NAME, mode="replace", symbols=desired
            ),
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "market-data-subscriptions"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_market_data_subscription_stream_maxlen,
            approximate=True,
        )
        logger.info("[ORB-GATEWAY-SUBSCRIBE] consumer=%s symbols=%d", SERVICE_NAME, len(desired))

    # ----- market-data drain -> aggregate -> bar -----
    async def _drain_market_data(self) -> None:
        if not self._last_gateway_symbols:
            return
        response = await self.redis.xread(
            {stream_name(self.settings.redis_stream_prefix, "market-data"): self._md_offset},
            count=500,
            block=500,
        )
        for _stream, entries in response or []:
            for entry_id, fields in entries:
                self._md_offset = entry_id
                self._handle_market_data(fields)

    def _handle_market_data(self, fields: dict) -> None:
        raw = fields.get("data")
        if not raw:
            return
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return
        if obj.get("event_type") != "trade_tick":
            return  # quotes/bars not used by the ORB entry path (quotes drive the OMS exit)
        payload = obj.get("payload") or {}
        symbol = str(payload.get("symbol", "")).upper()
        if not symbol or symbol not in self._last_gateway_symbols:
            return
        try:
            price = float(payload["price"])
            size = float(payload.get("size", 0) or 0)
        except (KeyError, TypeError, ValueError):
            return
        ts_ns = payload.get("timestamp_ns")
        ts = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC) if ts_ns else datetime.now(UTC)
        agg = self._aggregators.get(symbol)
        if agg is None:
            agg = OrbTickAggregator(session_open=self._session_open_utc())
            self._aggregators[symbol] = agg
        bar = agg.add_tick(ts, price, size)
        if bar is not None:
            self._on_bar(symbol, bar)

    @staticmethod
    def _session_open_utc() -> datetime:
        now_et = datetime.now(_ET)
        return now_et.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(UTC)

    # ----- per-bar hook (3b implements the ORB entry logic) -----
    def _on_bar(self, symbol: str, bar: OrbBar) -> None:
        """STUB for slice 3a — just counts/logs. Slice 3b builds the 5-min OR,
        applies the universe + breakout filters, arms-on-window-open and emits the
        open intent with stop_guard_enabled/stop_loss_pct=8/trail_pct=8."""
        self._bar_count += 1
        logger.debug(
            "[ORB-BAR] %s %s o=%.4f h=%.4f l=%.4f c=%.4f v=%.0f vwap=%.4f ema9=%.4f",
            symbol, bar.timestamp.isoformat(), bar.open, bar.high, bar.low, bar.close,
            bar.volume, bar.vwap or 0.0, bar.ema9 or 0.0,
        )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await OrbService().run()


def run() -> None:
    asyncio.run(main())
