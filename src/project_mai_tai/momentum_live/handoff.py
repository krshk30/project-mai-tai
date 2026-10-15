from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
import json
import logging
import time
from uuid import UUID

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload, stream_name
from project_mai_tai.momentum_live.admission import EpisodeLedger, Refusal, STRATEGY_CODE, Signal

logger = logging.getLogger("momentum-live")


class LiveHandoff:
    """Pure bounded offer; one independent worker owns Redis emission."""

    def __init__(self, redis, settings, *, route_for=None, country_lookup=None, reload_country=None):
        self.redis = redis
        self.settings = settings
        self.queue: asyncio.Queue[tuple[str, Signal]] = asyncio.Queue(maxsize=64)
        self.ledger = EpisodeLedger.restore([])
        self.ledger.ready = False
        self.counts: Counter[str] = Counter()
        if route_for is None:
            from project_mai_tai.momentum_live.runtime import unknown_sec_route
            route_for = unknown_sec_route
        self.route_for = route_for
        self.country_lookup = country_lookup
        self.reload_country = reload_country
        self.lookup_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self.lookup_seen: set[str] = set()
        self.country_prepared_day = None
        self.country_warning_offer = None

    def offer_warning(self, symbol: str) -> bool:
        if self.country_warning_offer is not None:
            return self.country_warning_offer(symbol)
        if not self.settings.momentum_live_enabled or self.country_lookup is None or symbol in self.lookup_seen:
            return False
        if len(self.lookup_seen) >= 4096:
            self.counts["country_lookup_seen_full"] += 1
            return False
        try:
            self.lookup_queue.put_nowait(symbol)
        except asyncio.QueueFull:
            self.counts["country_lookup_queue_full"] += 1
            return False
        self.lookup_seen.add(symbol)
        return True

    async def prepare_country_day(self, day) -> None:
        if self.country_prepared_day == day:
            return
        if self.reload_country is not None:
            await self.reload_country(day)
        self.lookup_seen.clear()
        while not self.lookup_queue.empty():
            self.lookup_queue.get_nowait()
            self.lookup_queue.task_done()
        self.country_prepared_day = day

    async def _lookup(self) -> None:
        while True:
            symbol = await self.lookup_queue.get()
            try:
                if self.country_lookup is not None:
                    await self.country_lookup(symbol)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[MOMLIVE1-COUNTRY] symbol=%s lookup=UNKNOWN", symbol)
            finally:
                self.lookup_queue.task_done()

    def offer(self, record, *, day_volume: int | None, now_ns: int) -> str:
        if not self.settings.momentum_live_enabled:
            return "disabled"
        if record.event_type != "DETECTED" or record.strategy_code not in {"momentum_30s_warn", "momentum_60s_warn"}:
            return "not_live_signal"
        try:
            detect = record.payload["detect"]
            reference = record.payload["reference"]
            end = int(detect.get("end_ms", detect.get("sip_ts_ms")))
            start = int(reference.get("end_ms", reference.get("sip_ts_ms")))
            observed = int(record.payload["observed_decision_ns"])
            if not 0 <= now_ns-observed <= 2_000_000_000:
                raise Refusal("sealed_signal_future_or_stale")
            decision = self.route_for(record.symbol, datetime.fromtimestamp(observed/1e9, UTC))
            if decision.broker is None:
                raise Refusal("entry_window_closed")
            evidence = {"status": getattr(decision, "status", "UNKNOWN"),
                "is_us": getattr(decision, "is_us", None), "cik": getattr(decision, "cik", None),
                "as_of": getattr(decision, "as_of", None).isoformat() if getattr(decision, "as_of", None) else None}
            signal = Signal(str(record.session_date), record.symbol, record.logical_id,
                            start, end, Decimal(str(detect["price"])), observed, day_volume,
                            str(decision.broker), str(decision.reason), json.dumps(evidence, sort_keys=True))
            seen = (signal.session_date, signal.symbol, signal.source_id) in self.ledger._sources
            episode, fresh = self.ledger.claim(signal, now_ns=now_ns)
            if not fresh:
                if not seen:
                    try:
                        self.queue.put_nowait((episode.episode_id, signal))
                    except asyncio.QueueFull:
                        self.counts["coalesced_update_full"] += 1
                self.counts["duplicate_move"] += 1
                return "duplicate_move"
            self.queue.put_nowait((episode.episode_id, signal))
        except asyncio.QueueFull:
            self.ledger.reconcile(episode.episode_id, cumulative_filled=Decimal(0),
                                  cumulative_exited=Decimal(0), now_ns=now_ns, terminal_entry=True)
            reason = "handoff_full"
        except (Refusal, KeyError, TypeError, ValueError, ArithmeticError) as exc:
            reason = str(exc) if isinstance(exc, Refusal) else "signal_payload_invalid"
        else:
            self.counts["offered"] += 1
            return "offered"
        self.counts[reason] += 1
        logger.info("[MOMLIVE1-REFUSED] symbol=%s reason=%s", record.symbol, reason)
        return reason

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._emit()), asyncio.create_task(self._feedback()), asyncio.create_task(self._lookup())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _feedback(self) -> None:
        while True:
            try:
                rows = await asyncio.wait_for(self.redis.xrevrange(
                    stream_name(self.settings.redis_stream_prefix, "momentum-live-state"), count=1), timeout=1)
                if rows:
                    payload = json.loads(rows[0][1]["data"])
                    self.observe_state(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ledger.ready = False
                logger.warning("[MOMLIVE1-HANDOFF] ownership_feedback=UNKNOWN")
            await asyncio.sleep(1)

    async def _emit(self) -> None:
        while True:
            episode_id, signal = await self.queue.get()
            try:
                event = TradeIntentEvent(event_id=UUID(episode_id), source_service="momentum-paper",
                    produced_at=datetime.fromtimestamp(signal.observed_ns/1e9, UTC),
                    payload=TradeIntentPayload(strategy_code=STRATEGY_CODE,
                        broker_account_name=self.settings.strategy_schwab_1m_v2_account_name,
                        symbol=signal.symbol, side="buy", quantity=Decimal(1), intent_type="open",
                        reason="MOMLIVE1_DETECTED", metadata={
                            "momlive_signal": json.dumps({"session_date": signal.session_date,
                                "symbol": signal.symbol, "source_id": signal.source_id,
                                "interval_start_ms": signal.interval_start_ms,
                                "interval_end_ms": signal.interval_end_ms,
                                "signal_price": str(signal.signal_price), "observed_ns": signal.observed_ns,
                                "day_volume": signal.day_volume, "country_route": signal.country_route,
                                "country_category": signal.country_category,
                                "country_evidence_json": signal.country_evidence_json}),
                            "episode_id": episode_id, "one_shot": "true"}))
                await asyncio.wait_for(self.redis.xadd(
                    stream_name(self.settings.redis_stream_prefix, "strategy-intents"),
                    {"data": event.model_dump_json()},
                    maxlen=self.settings.redis_strategy_intent_stream_maxlen, approximate=True), timeout=1)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Unknown delivery retains the claimed episode; never emit a fresh identity/retry.
                self.counts["handoff_ack_unknown"] += 1
                logger.exception("[MOMLIVE1-HANDOFF] episode=%s delivery=UNKNOWN retry=none", episode_id)
            finally:
                self.queue.task_done()

    def observe_state(self, payload: dict) -> None:
        if (payload.get("strategy_code") != STRATEGY_CODE or payload.get("source") != "oms"
                or payload.get("version") != 1 or payload.get("ready") is not True
                or not 0 <= time.time_ns() - int(payload.get("observed_ns", 0)) <= 10_000_000_000):
            raise Refusal("feedback_owner_invalid")
        incoming = {row["episode_id"]: row for row in payload["episodes"]}
        for row in self.ledger.snapshot():
            if row["episode_id"] not in incoming:
                incoming[row["episode_id"]] = row
        self.ledger = EpisodeLedger.restore(list(incoming.values()))
