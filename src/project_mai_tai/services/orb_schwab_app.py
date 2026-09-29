"""Default-off live ORB producer, separate from the broker-disconnected paper bot."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from project_mai_tai.db.session import build_timed_session_factory
from project_mai_tai.events import (
    MarketDataSubscriptionEvent,
    MarketDataSubscriptionPayload,
    stream_name,
)
from project_mai_tai.fanout_outcome_consumer import session_anchor
from project_mai_tai.orb_schwab_macd import schwab_completed_bar_macd_gate
from project_mai_tai.orb_schwab_order_route import (
    build_orb_schwab_cancel_intent,
    build_orb_schwab_open_intent,
    build_orb_schwab_reprice_intent,
    publish_orb_schwab_intent,
)
from project_mai_tai.services.orb_app import OrbService
from project_mai_tai.strategy_core.orb_intrabar import OrbBar
from project_mai_tai.strategy_core.orb_schwab_open import OrbSchwabOpeningOrder

_SERVICE = "orb-schwab"
_ET = ZoneInfo("America/New_York")
logger = logging.getLogger(_SERVICE)


class OrbSchwabService(OrbService):
    """Reuse ORB's confirmed-universe and tick aggregation, never its paper orders."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._running_high_mode = True
        self._fixed_resting_mode = False
        self._reclaim_mode = False
        self._opening_orders: dict[str, OrbSchwabOpeningOrder] = {}
        self._closed_bars: list[tuple[str, OrbBar, datetime]] = []

    def _maybe_roll_session(self, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        today = current.astimezone(_ET).date()
        anchor = session_anchor(current)
        if today == self._session_date and anchor == self._scanner_session_start:
            return
        self._session_date = today
        self._scanner_session_start = anchor
        self._opening_orders.clear()
        self._aggregators.clear()
        self._states.clear()

    async def _sync_gateway_subscription(self, symbols: list[str]) -> None:
        desired = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
        if desired == self._last_gateway_symbols:
            return
        event = MarketDataSubscriptionEvent(
            source_service=_SERVICE,
            payload=MarketDataSubscriptionPayload(
                consumer_name=_SERVICE, mode="replace", symbols=desired
            ),
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "market-data-subscriptions"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_market_data_subscription_stream_maxlen,
            approximate=True,
        )
        self._last_gateway_symbols = desired

    def _handle_market_data(self, fields: dict) -> None:
        raw = fields.get("data")
        if not raw:
            return
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            return
        if event.get("event_type") == "quote_tick":
            symbol = str((event.get("payload") or {}).get("symbol", "")).upper()
            if symbol not in self._last_gateway_symbols:
                return
            aggregator = self._aggregators.get(symbol)
            if aggregator is not None:
                observed_at = self._event_time(event)
                bar = aggregator.flush_before(observed_at)
                if bar is not None:
                    self._on_bar(symbol, bar, observed_at=observed_at)
            return
        super()._handle_market_data(fields)

    def _on_bar(
        self,
        symbol: str,
        bar: OrbBar,
        *,
        observed_at: datetime | None = None,
        observed_price: float | None = None,
    ) -> None:
        if symbol in self._universe:
            self._closed_bars.append((symbol, bar, observed_at or datetime.now(UTC)))

    @staticmethod
    def _processing_time() -> datetime:
        return datetime.now(UTC)

    async def _process_closed_bars(self) -> None:
        while self._closed_bars:
            symbol, bar, _observed_at = self._closed_bars.pop(0)
            opening = self._session_open_utc()
            first = opening - timedelta(minutes=5)
            if not first <= bar.timestamp < opening:
                continue
            order = self._opening_orders.setdefault(symbol, OrbSchwabOpeningOrder(opening))
            if bar.timestamp < first + timedelta(minutes=2):
                order.bars.setdefault(bar.timestamp, bar)
                continue
            allowed, reason, _histogram = await asyncio.to_thread(
                schwab_completed_bar_macd_gate,
                self.session_factory,
                symbol,
                self._processing_time(),
            )
            action = order.on_closed_bar(
                bar,
                observed_at=self._processing_time(),
                macd_allowed=allowed,
                macd_reason=reason,
            )
            if action is None:
                continue
            if action.kind == "place":
                assert action.level is not None
                event = build_orb_schwab_open_intent(self.settings, symbol, action.level)
            elif action.kind == "cancel":
                event = build_orb_schwab_cancel_intent(self.settings, symbol)
            else:
                assert action.level is not None
                event = build_orb_schwab_reprice_intent(self.settings, symbol, action.level)
            await publish_orb_schwab_intent(
                self.redis, self.settings, event, datetime.now(UTC)
            )

    async def run(self) -> None:
        if not self.settings.orb_live_schwab_orders_enabled or not self.settings.orb_enabled:
            logger.info("[ORB-SCHWAB] disabled")
            return
        if self.session_factory is None:
            self.session_factory = build_timed_session_factory(
                self.settings, service="orb-schwab", profile="fast"
            )
        try:
            while True:
                self._maybe_roll_session()
                now = datetime.now(UTC)
                if (
                    self._last_universe_refresh_at is None
                    or (now - self._last_universe_refresh_at).total_seconds()
                    >= self._UNIVERSE_REFRESH_SECS
                ):
                    self._refresh_universe()
                    await self._sync_gateway_subscription(sorted(self._universe))
                    self._last_universe_refresh_at = now
                processed = await self._drain_market_data()
                await self._process_closed_bars()
                if processed == 0:
                    await asyncio.sleep(1)
        finally:
            await self._sync_gateway_subscription([])


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    await OrbSchwabService().run()


def run() -> None:
    asyncio.run(main())
