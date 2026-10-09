"""Default-off live ORB producer, separate from the broker-disconnected paper bot."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from project_mai_tai.db.session import build_timed_session_factory
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    MarketDataSubscriptionEvent,
    MarketDataSubscriptionPayload,
    stream_name,
)
from project_mai_tai.fanout_outcome_consumer import session_anchor
from project_mai_tai.log import configure_logging
from project_mai_tai.orb_live_decisions import LiveDecisionTape, decision_session_factory
from project_mai_tai.orb_schwab_atr_entry import schwab_atr_entry_gate
from project_mai_tai.orb_schwab_fill import OrbSchwabMacdFill, fill_window, warn_refusal
from project_mai_tai.orb_schwab_macd import (
    BAR_WAIT, MacdVerdict, last_closed_bar_close, schwab_completed_bar_macd_gate,
)
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
from project_mai_tai.settings import get_settings
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
        self._observe_macd_checked_at: dict[str, datetime] = {}
        self._observe_pending_close: dict[str, datetime] = {}
        self._pending_macd_checked_at: dict[tuple[str, datetime], datetime] = {}
        self._first_macd_processing_at: dict[tuple[str, datetime], datetime] = {}
        self._macd_deferred_bars: set[tuple[str, datetime]] = set()
        self._observe_plan_at: dict[str, datetime] = {}
        self._observe_crosses: set[tuple[str, str, str]] = set()
        self._observe_seen_bars: dict[str, set[datetime]] = {}
        self._exit_held_symbols: set[str] = set()
        self._exit_last_poll: datetime | None = None
        self._exit_atr_cache: dict[str, tuple[datetime, list, str]] = {}
        self._exit_last_publish: dict[str, datetime] = {}
        self._observe_exit_entries: dict[str, dict] = {}
        self._observe_exit_records: set[tuple[str, str | None, str, str]] = set()
        self._live_last_bar_at: datetime | None = None
        self._live_last_decision_at: datetime | None = None
        self._live_healthy_since: datetime | None = None
        self._live_decision_tape = LiveDecisionTape()
        self._macd_fill = (
            OrbSchwabMacdFill(self.settings, clock=self._processing_time)
            if self.settings.orb_schwab_macd_fill_enabled else None
        )
        self._macd_fill_tasks: dict[tuple[str, datetime, datetime], asyncio.Task] = {}

    async def _completed_macd_gate(self, symbol, now):
        original = await asyncio.to_thread(
            schwab_completed_bar_macd_gate, self.session_factory, symbol, now
        )
        if self._macd_fill is None:
            return original
        start, cutoff, deadline = fill_window(now)
        key = symbol, start, cutoff
        for prior_key, prior_task in list(self._macd_fill_tasks.items()):
            if prior_key != key and prior_task.done():
                if not prior_task.cancelled():
                    prior_task.exception()
                self._macd_fill_tasks.pop(prior_key)
        if original[1] != "insufficient_schwab_history":
            return original
        remaining = (deadline - self._processing_time()).total_seconds()
        if remaining <= 0:
            warn_refusal(symbol, "fill_deadline")
            return MacdVerdict.BAR_NOT_YET, "fill_deadline", None
        task = self._macd_fill_tasks.get(key)
        if task is None:
            if any(k[:2] == key[:2] and not t.done() for k, t in self._macd_fill_tasks.items()):
                warn_refusal(symbol, "fill_worker_pending")
                return MacdVerdict.BAR_NOT_YET, "fill_worker_pending", None
            task = asyncio.create_task(asyncio.to_thread(
                schwab_completed_bar_macd_gate, self.session_factory, symbol, now, fill=self._macd_fill
            ))
            self._macd_fill_tasks[key] = task
        try:
            # Keep physical work retained after timeout/cancellation; no duplicate retry.
            result = await asyncio.wait_for(asyncio.shield(task), timeout=min(2.0, remaining))
        except TimeoutError:
            warn_refusal(symbol, "decision_worker_timeout")
            return MacdVerdict.BAR_NOT_YET, "decision_worker_timeout", None
        if task.done():
            self._macd_fill_tasks.pop(key, None)
        return result

    def _live_phase(self, now: datetime) -> str:
        opening = self._session_open_utc()
        if now < opening - timedelta(minutes=5):
            return "waiting_for_open"
        if now < opening - timedelta(minutes=3):
            return "opening_range"
        if now < opening + timedelta(minutes=30):
            return "entry_window"
        return "session_complete"

    async def _publish_live_heartbeat(self) -> None:
        now = self._processing_time()
        since = self._live_healthy_since or now
        event = HeartbeatEvent(
            source_service=_SERVICE,
            produced_at=now,
            payload=HeartbeatPayload(
                service_name=_SERVICE, instance_name=_SERVICE, status="healthy",
                details={
                    "mode": "OBSERVE_ONLY" if self._observe_only else "LIVE",
                    "phase": self._live_phase(now),
                    # HeartbeatPayload's existing wire contract is dict[str, str].
                    "universe": json.dumps(sorted(self._universe)),
                    "subscribed": json.dumps(
                        [] if self._observe_only or not self._gateway_subscription_announced
                        else self._last_gateway_symbols
                    ),
                    "last_bar_at": self._live_last_bar_at.isoformat() if self._live_last_bar_at else "",
                    "last_decision_at": self._live_last_decision_at.isoformat() if self._live_last_decision_at else "",
                    "healthy_since": since.isoformat(),
                },
            ),
        )
        await asyncio.wait_for(self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "heartbeats"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_heartbeat_stream_maxlen, approximate=True,
        ), timeout=2)
        self._live_healthy_since = since

    async def _live_heartbeat_loop(self) -> None:
        # Separate task: Redis latency never serializes bar processing or exits.
        while True:
            try:
                await self._publish_live_heartbeat()
            except Exception as exc:
                self._live_healthy_since = None
                logger.warning("[ORB-SCHWAB-HEARTBEAT] publish failed: %s", type(exc).__name__)
            await asyncio.sleep(15)

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
        self._observe_macd_checked_at.clear()
        self._observe_pending_close.clear()
        self._pending_macd_checked_at.clear()
        self._first_macd_processing_at.clear()
        self._macd_deferred_bars.clear()
        self._observe_plan_at.clear()
        self._observe_crosses.clear()
        self._observe_seen_bars.clear()
        self._observe_status_at = None
        self._exit_atr_cache.clear()
        self._exit_last_publish.clear()
        self._exit_last_poll = None
        self._observe_exit_entries.clear()
        self._observe_exit_records.clear()
        self._live_last_bar_at = None
        self._live_last_decision_at = None

    async def _sync_gateway_subscription(self, symbols: list[str]) -> None:
        desired = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
        first_announcement = (
            self.settings.market_data_subscription_startup_enabled
            and not self._gateway_subscription_announced
        )
        if self._observe_only:
            # Observation never claims symbols. COLDSTART1 registers only its
            # empty owner, while retaining the local filter for observed ticks.
            self._last_gateway_symbols = desired
            if not first_announcement and not getattr(self, "_gateway_subscription_uncertain", False):
                return
        elif (
            desired == self._last_gateway_symbols and not first_announcement
            and not getattr(self, "_gateway_subscription_uncertain", False)
        ):
            return
        event = MarketDataSubscriptionEvent(
            source_service=_SERVICE,
            payload=MarketDataSubscriptionPayload(
                consumer_name=_SERVICE, mode="replace", symbols=[] if self._observe_only else desired
            ),
        )
        self._gateway_subscription_uncertain = True
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "market-data-subscriptions"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_market_data_subscription_stream_maxlen,
            approximate=True,
        )
        self._last_gateway_symbols = desired
        self._gateway_subscription_announced = True
        self._gateway_subscription_uncertain = False

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
            if self._live_last_bar_at is None or bar.timestamp > self._live_last_bar_at:
                self._live_last_bar_at = bar.timestamp
            opening = self._session_open_utc()
            if opening - timedelta(minutes=3) <= bar.timestamp < opening:
                self._first_macd_processing_at.setdefault(
                    (symbol, bar.timestamp), self._processing_time()
                )
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

    def _record_live_decision(self, symbol, bar, now, verdict, histogram, action, reason, atr) -> None:
        if self._observe_only:
            return
        try:
            self._live_decision_tape.offer({
                "strategy_code": "orb_schwab", "symbol": symbol,
                "bar_at": bar.timestamp.isoformat(), "evaluated_at": now.isoformat(),
                "last_bar_at": bar.timestamp.astimezone(_ET).strftime("%Y-%m-%d %I:%M:%S %p ET"),
                "status": action if action != "none" else "skipped",
                "action": action, "reason": reason, "path": "orb_live",
                "macd_verdict": verdict.value, "macd_histogram": histogram,
                "atr_verdict": atr, "price": str(bar.close),
                "score": "" if histogram is None else str(histogram),
                "score_details": json.dumps({"macd": verdict.value, "atr": atr}, sort_keys=True),
            })
        except Exception as exc:
            logger.warning("[ORB-LIVE-TAPE] enqueue_failed symbol=%s error=%s",
                           symbol, type(exc).__name__)

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
                prior_check = self._observe_macd_checked_at.get(symbol)
                if prior_check is not None and now - prior_check < timedelta(seconds=1):
                    continue
                self._observe_macd_checked_at[symbol] = now
                if now >= opening + timedelta(minutes=30):
                    verdict, reason, histogram = MacdVerdict.NEGATIVE, "entry_window_ended", None
                else:
                    verdict, reason, histogram = await self._completed_macd_gate(symbol, now)
                if verdict == MacdVerdict.BAR_NOT_YET:
                    deadline = self._observe_pending_close.setdefault(
                        symbol, last_closed_bar_close(now)
                    ) + BAR_WAIT
                    if now < deadline:
                        self._record_observation(
                            "working_plan_check", symbol=symbol, macd_allowed=None,
                            macd_histogram=None, reason=reason, proposed_action="wait_for_schwab_bar",
                        )
                        continue
                else:
                    self._observe_pending_close.pop(symbol, None)
                self._observe_macd_minute[symbol] = minute
                if verdict != MacdVerdict.ALLOWED:
                    order.cancelled = True
                    if verdict == MacdVerdict.BAR_NOT_YET:
                        reason = "bar_missing"
                self._record_observation(
                    "working_plan_check", symbol=symbol, macd_allowed=verdict == MacdVerdict.ALLOWED,
                    macd_histogram=histogram, reason=reason,
                    proposed_action=("keep_if_unfilled" if verdict == MacdVerdict.ALLOWED
                                     else "cancel_if_still_unfilled"),
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
                    (cached[2] != "complete" or prior.get("atr_missing_minutes") or prior.get("body") is None)
                    and now - cached[0] >= timedelta(seconds=2)
                ):
                    bars, status = await asyncio.to_thread(schwab_completed_atr_bars, self.session_factory, symbol, now)
                    cached = self._exit_atr_cache[symbol] = (now, bars, status)
                context = completed_bar_evidence(entry["fill_id"], entry["fill_at"], now,
                                        atr_bars=cached[1], atr_status=cached[2], prior=prior)
            if self._observe_only:
                entry["context"] = context
                key = (symbol, context.get("reason"), context.get("atr_status"),
                       context.get("body_status"))
                if key not in self._observe_exit_records:
                    self._observe_exit_records.add(key)
                    self._record_observation(
                        "conditional_exit", symbol=symbol, reason=context.get("reason"),
                        decision_at=context.get("decision_at"), body=context.get("body"),
                        body_status=context.get("body_status"),
                        atr_missing_minutes=context.get("atr_missing_minutes", []),
                        atr_overdue_minutes=context.get("atr_overdue_minutes", []),
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
        pending: list[tuple[str, OrbBar, datetime]] = []
        deferred_symbols: set[str] = set()
        while self._closed_bars:
            symbol, bar, observed_at = self._closed_bars.pop(0)
            opening = self._session_open_utc()
            first = opening - timedelta(minutes=5)
            if not first <= bar.timestamp < opening:
                continue
            if symbol in deferred_symbols:
                pending.append((symbol, bar, observed_at))
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
            now = self._processing_time()
            key = symbol, bar.timestamp
            self._first_macd_processing_at.setdefault(key, now)
            prior_check = self._pending_macd_checked_at.get(key)
            if prior_check is not None and now - prior_check < timedelta(seconds=1):
                pending.append((symbol, bar, observed_at))
                deferred_symbols.add(symbol)
                continue
            self._pending_macd_checked_at[key] = now
            verdict, reason, histogram = await self._completed_macd_gate(symbol, now)
            if self._macd_fill is not None:
                # Recheck after the worker: a successful response cannot buy late.
                now = self._processing_time()
            atr_evidence = "not_evaluated" if self.settings.orb_schwab_atr_entry_gate_enabled else "disabled"
            deadline = bar.timestamp + timedelta(minutes=1) + BAR_WAIT
            if (not order.placed and not order.cancelled
                    and self.settings.orb_schwab_atr_entry_gate_enabled
                    and verdict == MacdVerdict.ALLOWED):
                atr = await asyncio.to_thread(schwab_atr_entry_gate, self.session_factory, symbol, now)
                atr_evidence = atr.verdict
                logger.info("[ORB-SCHWAB-ATR-ENTRY] symbol=%s enabled=True evidence=%s",
                            symbol, json.dumps(atr.evidence(), sort_keys=True))
                if self._observe_only:
                    self._record_observation("atr_entry_gate", symbol=symbol, **atr.evidence())
                if atr.verdict == "unknown" and now < deadline:
                    self._macd_deferred_bars.add(key)
                    pending.append((symbol, bar, observed_at))
                    deferred_symbols.add(symbol)
                    continue
                if atr.verdict != "allowed":
                    self._live_last_decision_at = now
                    order.cancelled = True
                    self._pending_macd_checked_at.pop(key, None)
                    self._first_macd_processing_at.pop(key, None)
                    self._macd_deferred_bars.discard(key)
                    self._record_live_decision(symbol, bar, now, verdict, histogram,
                                               "none", f"atr_{atr.reason}", atr_evidence)
                    if self._observe_only:
                        self._record_observation("decision", symbol=symbol,
                                                 bar_at=bar.timestamp.isoformat(), proposed_action="none",
                                                 decision_reason=f"atr_{atr.reason}")
                    continue
            if verdict == MacdVerdict.BAR_NOT_YET and now < deadline:
                self._macd_deferred_bars.add(key)
                pending.append((symbol, bar, observed_at))
                deferred_symbols.add(symbol)
                continue
            self._pending_macd_checked_at.pop(key, None)
            self._live_last_decision_at = now
            first_processing_at = self._first_macd_processing_at.pop(key)
            was_deferred = key in self._macd_deferred_bars
            self._macd_deferred_bars.discard(key)
            if verdict == MacdVerdict.BAR_NOT_YET:
                order.cancelled = True
                logger.warning(
                    "[ORB-SCHWAB-BAR-MISSING] symbol=%s bar=%s reason=%s",
                    symbol, bar.timestamp.isoformat(), reason,
                )
                if not self._observe_only:
                    logger.info(
                        "[ORB-SCHWAB-DECISION] symbol=%s bar=%s macd=%s histogram=%s action=%s reason=bar_missing",
                        symbol, bar.timestamp.isoformat(), verdict.value, histogram,
                        "cancel" if order.placed else "none",
                    )
                    self._record_live_decision(symbol, bar, now, verdict, histogram,
                                               "cancel" if order.placed else "none",
                                               "bar_missing", atr_evidence)
                if self._observe_only:
                    self._record_observation(
                        "decision", symbol=symbol, bar_at=bar.timestamp.isoformat(),
                        proposed_action="none" if not order.placed else "cancel",
                        decision_reason="bar_missing", macd_allowed=None,
                    )
                elif order.placed:
                    event = build_orb_schwab_cancel_intent(self.settings, symbol)
                    await publish_orb_schwab_intent(self.redis, self.settings, event, now)
                continue
            if self._macd_fill is not None:
                now = self._processing_time()
            if not order.placed and now >= opening - timedelta(seconds=30):
                if self._macd_fill is not None:
                    warn_refusal(symbol, "entry_deadline")
                order.cancelled = True
                decision_reason = (
                    "macd_negative_no_entry"
                    if verdict == MacdVerdict.NEGATIVE else "late_macd_no_entry"
                )
                logger.warning(
                    "[ORB-SCHWAB-ENTRY-CUTOFF] symbol=%s bar=%s reason=%s",
                    symbol, bar.timestamp.isoformat(), decision_reason,
                )
                if not self._observe_only:
                    logger.info(
                        "[ORB-SCHWAB-DECISION] symbol=%s bar=%s macd=%s histogram=%s action=none reason=%s",
                        symbol, bar.timestamp.isoformat(), verdict.value, histogram, decision_reason,
                    )
                    self._record_live_decision(symbol, bar, now, verdict, histogram,
                                               "none", decision_reason, atr_evidence)
                if self._observe_only:
                    self._record_observation(
                        "decision", symbol=symbol, bar_at=bar.timestamp.isoformat(),
                        proposed_action="none", decision_reason=decision_reason,
                    )
                continue
            action = order.on_closed_bar(
                bar,
                observed_at=first_processing_at,
                macd_allowed=verdict == MacdVerdict.ALLOWED,
                macd_reason=reason,
            )
            if not self._observe_only:
                logger.info(
                    "[ORB-SCHWAB-DECISION] symbol=%s bar=%s macd=%s histogram=%s action=%s reason=%s",
                    symbol, bar.timestamp.isoformat(), verdict.value, histogram,
                    action.kind if action is not None else "none",
                    action.reason if action is not None else "no_action",
                )
                self._record_live_decision(symbol, bar, now, verdict, histogram,
                                           action.kind if action is not None else "none",
                                           action.reason if action is not None else "no_action",
                                           atr_evidence)
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
                    macd_allowed=verdict == MacdVerdict.ALLOWED,
                    macd_reason=reason, macd_histogram=histogram,
                    proposed_action=action.kind if action is not None else "none",
                    decision_reason=action.reason if action is not None else "no_action",
                    prices=prices, quantity=2, execution="NOT_TESTED",
                )
                continue
            if action is None:
                continue
            if action.kind == "place":
                assert action.level is not None
                event = build_orb_schwab_open_intent(
                    self.settings, symbol, action.level,
                    deferred_macd_bar_close=(bar.timestamp + timedelta(minutes=1)) if was_deferred else None,
                )
            elif action.kind == "cancel":
                event = build_orb_schwab_cancel_intent(self.settings, symbol)
            else:
                assert action.level is not None
                event = build_orb_schwab_reprice_intent(self.settings, symbol, action.level)
            await publish_orb_schwab_intent(
                self.redis, self.settings, event, datetime.now(UTC)
            )
        self._closed_bars = pending

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
            "[ORB-SCHWAB] mode=%s live_sending=%s atr_entry_gate_enabled=%s",
            "OBSERVE_ONLY" if self._observe_only else "LIVE",
            self.settings.orb_live_schwab_orders_enabled,
            self.settings.orb_schwab_atr_entry_gate_enabled,
        )
        if self.settings.market_data_subscription_startup_enabled and not self._observe_only:
            # Hydrate ownership before any replacement, including outside RTH.
            # A failed read must escape BEFORE the finally block can release it.
            entries = await asyncio.to_thread(
                open_entries, self.session_factory, self.settings.strategy_schwab_1m_v2_account_name
            )
            self._exit_held_symbols = {entry["symbol"] for entry in entries}
        heartbeat_task = asyncio.create_task(self._live_heartbeat_loop(), name="orb-schwab-heartbeat")
        self._live_decision_tape.session_factory = decision_session_factory(self.settings)
        tape_task = asyncio.create_task(self._live_decision_tape.run(), name="orb-live-decision-tape")
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
            tape_task.cancel()
            with suppress(asyncio.CancelledError):
                await tape_task
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            # A failed/ambiguous first XADD is not permission to clear retained
            # coverage. Only a successfully announced instance may release it.
            if not self.settings.market_data_subscription_startup_enabled or self._gateway_subscription_announced:
                await self._sync_gateway_subscription([])


async def main() -> None:
    settings = get_settings()
    configure_logging(_SERVICE, settings.log_level)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    await OrbSchwabService().run()


def run() -> None:
    asyncio.run(main())
