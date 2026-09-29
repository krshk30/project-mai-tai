"""Default-off live ORB producer, separate from the broker-disconnected paper bot."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from project_mai_tai.db.session import build_timed_session_factory
from project_mai_tai.events import (
    MarketDataSubscriptionEvent,
    MarketDataSubscriptionPayload,
    stream_name,
)
from project_mai_tai.fanout_outcome_consumer import session_anchor
from project_mai_tai.orb_schwab_macd import schwab_completed_bar_macd_gate
from project_mai_tai.orb_schwab_exits import (
    ATR_SOURCE, completed_bar_evidence, open_entries, save_context, schwab_completed_atr_bars,
)
from project_mai_tai.orb_schwab_order_route import (
    build_orb_schwab_cancel_intent,
    build_orb_schwab_exit_intent,
    build_orb_schwab_open_intent,
    build_orb_schwab_reprice_intent,
    publish_orb_schwab_intent,
)
from project_mai_tai.services.orb_app import OrbService, _normalize_trade_ts_ns
from project_mai_tai.strategy_core.orb_intrabar import OrbBar
from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata
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
        if self.settings.orb_schwab_observe_enabled and self.settings.orb_live_schwab_orders_enabled:
            raise ValueError("ORB observation and live sending cannot be enabled together")
        self._observe_only = self.settings.orb_schwab_observe_enabled
        self._observe_status_at: datetime | None = None
        self._observe_macd_minute: dict[str, datetime] = {}
        self._observe_plan_at: dict[str, datetime] = {}
        self._observe_crosses: set[tuple[str, str, str]] = set()
        self._observe_seen_bars: dict[str, set[datetime]] = {}
        if (self._observe_only or self.settings.orb_live_schwab_orders_enabled) and (
            self.settings.orb_paper_atr_entry_gate_enabled or self.settings.orb_paper_four_red_delay_enabled
        ):
            raise ValueError("Optional paper entry gates need a separately reviewed native-order design")
        self._exit_held_symbols: set[str] = set()
        self._exit_last_poll: datetime | None = None
        self._exit_atr_cache: dict[str, tuple[datetime, list, str]] = {}
        self._exit_last_publish: dict[str, datetime] = {}
        self._observe_exit_entries: dict[str, dict] = {}
        self._observe_exit_records: set[tuple[str, str | None, str, str]] = set()

    def _maybe_roll_session(self, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        today = current.astimezone(_ET).date()
        anchor = session_anchor(current)
        if today == self._session_date and anchor == self._scanner_session_start:
            return
        self._session_date = today
        self._scanner_session_start = anchor
        self._opening_orders.clear()
        self._closed_bars.clear()
        self._aggregators.clear()
        self._states.clear()
        self._observe_macd_minute.clear()
        self._observe_plan_at.clear()
        self._observe_crosses.clear()
        self._observe_seen_bars.clear()
        self._observe_status_at = None
        self._exit_atr_cache.clear()
        self._exit_last_publish.clear()
        self._exit_last_poll = None
        self._observe_exit_entries.clear()
        self._observe_exit_records.clear()

    async def _sync_gateway_subscription(self, symbols: list[str]) -> None:
        desired = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
        if self._observe_only:
            # Observe only feeds other consumers already requested. No warm-up or
            # gateway owner changes are caused by the rehearsal.
            self._last_gateway_symbols = desired
            return
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
        if not isinstance(event, dict):
            return
        if not isinstance(event.get("payload"), dict):
            return
        if self._observe_only:
            self._observe_trade_cross(event)
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

    def _record_observation(self, kind: str, **fields: object) -> None:
        logger.info(
            "[ORB-SCHWAB-OBSERVE] %s",
            json.dumps(
                {"kind": kind, "at": self._processing_time().isoformat(),
                 "broker_orders_sent": 0, "fill_status": "UNMEASURED", **fields},
                sort_keys=True,
            ),
        )

    def _observe_trade_cross(self, event: dict) -> None:
        if event.get("event_type") != "trade_tick":
            return
        payload = event.get("payload") or {}
        symbol = str(payload.get("symbol", "")).upper()
        order = self._opening_orders.get(symbol)
        planned_at = self._observe_plan_at.get(symbol)
        if order is None or not order.placed or order.cancelled or planned_at is None:
            return
        try:
            price = Decimal(str(payload["price"]))
            timestamp_ns = _normalize_trade_ts_ns(payload.get("timestamp_ns"))
            if not timestamp_ns or not price.is_finite() or price <= 0:
                return
            traded_at = datetime.fromtimestamp(timestamp_ns / 1e9, tz=UTC)
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError, OSError):
            return
        opening = self._session_open_utc()
        now = self._processing_time()
        if not (
            opening <= traded_at < opening + timedelta(minutes=30)
            and traded_at >= planned_at
            and timedelta(0) <= now - traded_at <= timedelta(seconds=5)
        ):
            return
        plan = build_orb_schwab_bracket_metadata(order.last_requested_level)
        if price < Decimal(plan["stop_price"]):
            return
        relation = "above_cap" if price > Decimal(plan["limit_price"]) else "within_cap"
        key = (symbol, plan["stop_price"], relation)
        if key in self._observe_crosses:
            return
        self._observe_crosses.add(key)
        if relation == "within_cap" and symbol not in self._observe_exit_entries:
            self._observe_exit_entries[symbol] = {
                "entry_id": f"conditional:{symbol}", "symbol": symbol,
                "fill_id": f"NOT_A_FILL:{symbol}", "fill_at": traded_at, "context": {},
            }
        self._record_observation(
            "price_cross_only", symbol=symbol, trade_at=traded_at.isoformat(),
            trade_price=str(price), trigger=plan["stop_price"], cap=plan["limit_price"],
            relation=relation, execution="NOT_TESTED", size=payload.get("size"),
        )

    async def _observe_working_plans(self, now: datetime) -> None:
        if not self._observe_only:
            return
        opening = self._session_open_utc()
        if now >= opening:
            minute = now.replace(second=0, microsecond=0)
            for symbol, order in self._opening_orders.items():
                if not order.placed or order.cancelled:
                    continue
                if self._observe_macd_minute.get(symbol) == minute:
                    continue
                self._observe_macd_minute[symbol] = minute
                if now >= opening + timedelta(minutes=30):
                    allowed, reason, histogram = False, "entry_window_ended", None
                else:
                    allowed, reason, histogram = await asyncio.to_thread(
                        schwab_completed_bar_macd_gate, self.session_factory, symbol, now
                    )
                if not allowed:
                    order.cancelled = True
                self._record_observation(
                    "working_plan_check", symbol=symbol, macd_allowed=allowed,
                    macd_histogram=histogram, reason=reason,
                    proposed_action="keep_if_unfilled" if allowed else "cancel_if_still_unfilled",
                )
        if self._observe_status_at is None or now - self._observe_status_at >= timedelta(minutes=1):
            self._observe_status_at = now
            expected = [opening - timedelta(minutes=5 - index) for index in range(5)]
            self._record_observation(
                "coverage", candidates=sorted(self._universe),
                proposed_orders=sum(order.placed for order in self._opening_orders.values()),
                price_cross_records=len(self._observe_crosses),
                missing_closed_range_minutes={
                    symbol: [
                        minute.isoformat() for minute in expected
                        if minute + timedelta(minutes=1) <= now
                        and minute not in self._observe_seen_bars.get(symbol, set())
                    ]
                    for symbol in sorted(self._universe)
                },
                source="existing_gateway_feeds_only", broker_checks="NOT_EXERCISED",
            )

    async def _process_strategy_exits(self, now: datetime) -> None:
        if not (self._observe_only or self.settings.orb_live_schwab_orders_enabled):
            return
        local = now.astimezone(_ET)
        if local.weekday() >= 5 or not (9, 30) <= (local.hour, local.minute) < (16, 0):
            return
        if self._exit_last_poll is not None and now - self._exit_last_poll < timedelta(seconds=1):
            return
        self._exit_last_poll = now
        if self._observe_only:
            entries = list(self._observe_exit_entries.values())
        else:
            try:
                entries = await asyncio.to_thread(
                    open_entries, self.session_factory, self.settings.strategy_schwab_1m_v2_account_name
                )
            except Exception:
                logger.exception("[ORB-SCHWAB-EXIT-UNKNOWN] owned-fill read unavailable; subscriptions retained")
                return
            self._exit_held_symbols = {entry["symbol"] for entry in entries}
            await self._sync_gateway_subscription(sorted(self._universe | self._exit_held_symbols))
        for entry in entries:
            symbol = entry["symbol"]
            if entry.get("close_claimed"):
                continue
            prior = entry["context"]
            if not entry["fill_id"] or entry["fill_at"] is None:
                context = {"body": None, "atr_status": "broker_fill_time_unknown"}
                await asyncio.to_thread(save_context, self.session_factory, entry["entry_id"], context)
                logger.warning("[ORB-SCHWAB-EXIT-UNKNOWN] symbol=%s broker fill/time unconfirmed", symbol)
                continue
            if prior.get("reason"):
                context = prior
            else:
                cached = self._exit_atr_cache.get(symbol)
                if cached is None or cached[0].replace(second=0, microsecond=0) != now.replace(second=0, microsecond=0) or (
                    cached[2] != "complete" and now - cached[0] >= timedelta(seconds=2)
                ):
                    bars, status = await asyncio.to_thread(schwab_completed_atr_bars, self.session_factory, symbol, now)
                    cached = self._exit_atr_cache[symbol] = (now, bars, status)
                context = completed_bar_evidence(entry["fill_id"], entry["fill_at"], now,
                                        atr_bars=cached[1], atr_status=cached[2], prior=prior)
            if self._observe_only:
                entry["context"] = context
                key = (symbol, context.get("reason"), context.get("atr_status"),
                       "known" if context.get("body") else "unknown")
                if key not in self._observe_exit_records:
                    self._observe_exit_records.add(key)
                    self._record_observation(
                        "conditional_exit", symbol=symbol, reason=context.get("reason"),
                        decision_at=context.get("decision_at"), body=context.get("body"),
                        atr_status=context.get("atr_status"), atr_source=ATR_SOURCE,
                        assumption="ONLY_IF_THE_OBSERVED_CROSS_HAD_FILLED; NOT_A_BROKER_FILL",
                    )
                continue
            await asyncio.to_thread(save_context, self.session_factory, entry["entry_id"], context)
            if not context.get("reason"):
                continue
            last = self._exit_last_publish.get(entry["entry_id"])
            if last is not None and now - last < timedelta(seconds=5):
                continue
            event = build_orb_schwab_exit_intent(self.settings, symbol, entry["entry_id"], entry["fill_id"], context["reason"])
            await publish_orb_schwab_intent(self.redis, self.settings, event, self._processing_time())
            self._exit_last_publish[entry["entry_id"]] = now
            logger.info("[ORB-SCHWAB-EXIT-REQUEST] symbol=%s reason=%s entry=%s fill=%s",
                        symbol, context["reason"], entry["entry_id"], entry["fill_id"])

    async def _process_closed_bars(self) -> None:
        while self._closed_bars:
            symbol, bar, _observed_at = self._closed_bars.pop(0)
            opening = self._session_open_utc()
            first = opening - timedelta(minutes=5)
            if not first <= bar.timestamp < opening:
                continue
            if self._observe_only:
                self._observe_seen_bars.setdefault(symbol, set()).add(bar.timestamp)
            order = self._opening_orders.setdefault(symbol, OrbSchwabOpeningOrder(opening))
            if bar.timestamp < first + timedelta(minutes=2):
                order.bars.setdefault(bar.timestamp, bar)
                if self._observe_only:
                    self._record_observation(
                        "range_bar", symbol=symbol, bar_at=bar.timestamp.isoformat(),
                        high=bar.high, qualified_high=bar.breakout_high,
                    )
                continue
            allowed, reason, histogram = await asyncio.to_thread(
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
            if self._observe_only:
                prices = (
                    build_orb_schwab_bracket_metadata(action.level)
                    if action is not None and action.level is not None else None
                )
                if action is not None and action.kind in {"place", "reprice"}:
                    self._observe_plan_at[symbol] = self._processing_time()
                self._record_observation(
                    "decision", symbol=symbol, bar_at=bar.timestamp.isoformat(),
                    high=bar.high, qualified_high=bar.breakout_high,
                    range_bars_seen=len(order.bars), range_bars_required=int(
                        (bar.timestamp - first).total_seconds() / 60
                    ) + 1,
                    macd_allowed=allowed, macd_reason=reason, macd_histogram=histogram,
                    proposed_action=action.kind if action is not None else "none",
                    decision_reason=action.reason if action is not None else "no_action",
                    prices=prices, quantity=2, execution="NOT_TESTED",
                )
                continue
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
        if not self.settings.orb_enabled or not (
            self.settings.orb_live_schwab_orders_enabled or self._observe_only
        ):
            logger.info("[ORB-SCHWAB] disabled")
            return
        if self.session_factory is None:
            self.session_factory = build_timed_session_factory(
                self.settings, service="orb-schwab", profile="fast"
            )
        logger.info(
            "[ORB-SCHWAB] mode=%s live_sending=%s",
            "OBSERVE_ONLY" if self._observe_only else "LIVE",
            self.settings.orb_live_schwab_orders_enabled,
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
                    await self._sync_gateway_subscription(sorted(self._universe | self._exit_held_symbols))
                    self._last_universe_refresh_at = now
                processed = await self._drain_market_data()
                await self._process_closed_bars()
                await self._observe_working_plans(self._processing_time())
                await self._process_strategy_exits(self._processing_time())
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
