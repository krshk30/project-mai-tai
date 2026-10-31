"""NUWEREF1 background-only, once-per-symbol/day reference recovery."""

from __future__ import annotations

import asyncio
from contextvars import Context
from datetime import date, datetime
import json
import logging
import math
from threading import Thread
from typing import Callable, Mapping, Sequence

from project_mai_tai.strategy_core.models import MarketSnapshot, ReferenceData
from project_mai_tai.strategy_core.snapshot_utils import get_current_price, get_current_volume
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ, is_fillable_et_session
from project_mai_tai.symbol_case import raw_symbol_is_uppercase


def complete_reference(reference: ReferenceData | None) -> bool:
    shares = getattr(reference, "shares_outstanding", None)
    average = getattr(reference, "avg_daily_volume", None)
    return (
        isinstance(shares, int) and not isinstance(shares, bool) and shares > 0
        and isinstance(average, (float, int)) and not isinstance(average, bool)
        and math.isfinite(average) and average > 0
    )


class ScannerReferenceRecovery:
    def __init__(self, *, redis=None, prefix: str = "mai_tai", fetcher=None,
                 min_price: float, max_price: float, min_volume: int,
                 logger: logging.Logger | None = None):
        self.redis = redis
        self.prefix = prefix
        self.fetcher: Callable[[str, date], ReferenceData | None] | None = fetcher
        self.min_price = min_price
        self.max_price = max_price
        self.min_volume = min_volume
        self.logger = logger or logging.getLogger(__name__)
        self._day: date | None = None
        self._attempted: set[tuple[date, str]] = set()
        self._ready: dict[tuple[date, str], ReferenceData] = {}
        self._logged: set[tuple[date, str]] = set()
        self._tasks: set[asyncio.Task] = set()
        self._serial = asyncio.Semaphore(1)
        self._closing = False
        self._shutdown_logged = False

    async def _fetch_readonly(self, symbol, day):
        """Only reference SDK reads may outlive process shutdown (C12043)."""
        loop = asyncio.get_running_loop()
        result = loop.create_future()

        def deliver(value, failed):
            if not result.done():
                if failed:
                    result.set_exception(RuntimeError("readonly_reference_fetch_failed"))
                else:
                    result.set_result(value)

        def read():
            try:
                value, failed = self.fetcher(symbol, day), False
            except Exception:
                value, failed = None, True
            try:
                loop.call_soon_threadsafe(deliver, value, failed)
            except RuntimeError:
                # The process loop has closed; never publish a late result.
                pass

        Thread(target=read, name="nuweref1-readonly", daemon=True).start()
        return await result

    def observe(self, snapshots: Sequence[MarketSnapshot],
                known: Mapping[str, ReferenceData], now: datetime) -> dict[str, ReferenceData]:
        """Schedule only; no await, SDK call or Redis operation on this path."""
        if now.tzinfo is None:
            return {}
        now = now.astimezone(EASTERN_TZ)
        day = now.date()
        if self._day != day:
            self._day = day
            self._attempted = {key for key in self._attempted if key[0] == day}
            self._ready = {key: value for key, value in self._ready.items() if key[0] == day}
            self._logged = {key for key in self._logged if key[0] == day}
        recovered = {}
        for snapshot in snapshots:
            symbol = snapshot.ticker
            if complete_reference(known.get(symbol)):
                continue
            key = (day, symbol)
            if key in self._ready:
                recovered[symbol] = self._ready[key]
                continue
            price = get_current_price(snapshot)
            if (self._closing or key in self._attempted or not raw_symbol_is_uppercase(symbol)
                    or not is_fillable_et_session(now, 4, 16)
                    or price is None or not self.min_price <= price <= self.max_price
                    or get_current_volume(snapshot) < self.min_volume):
                continue
            self._attempted.add(key)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._failure(key, "event_loop_unavailable")
                continue
            # Reference I/O is not a child of a bot's keepwatch/history operation.
            task = loop.create_task(self._recover(key), name=f"nuweref1-{symbol}", context=Context())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return recovered

    def ready_references(self, symbols: Sequence[str], day: date) -> dict[str, ReferenceData]:
        return {symbol: self._ready[(day, symbol)] for symbol in symbols if (day, symbol) in self._ready}

    def _failure(self, key, reason):
        if key not in self._logged:
            self._logged.add(key)
            self.logger.warning("[NUWEREF1-UNSCANNED] symbol=%s day=%s reason=%s", key[1], key[0], reason)

    async def _recover(self, key):
        async with self._serial:
            if self._closing or self._day != key[0]:
                return
            storage_key = f"{self.prefix}:nuweref1:{key[0].isoformat()}:{key[1]}"
            try:
                if self.redis is None or self.fetcher is None:
                    self._failure(key, "recovery_port_unavailable")
                    return
                claimed = await self.redis.set(storage_key, '{"status":"pending"}', nx=True, ex=172800)
                if not claimed:
                    raw = await self.redis.get(storage_key)
                    stored = json.loads(raw) if raw is not None else {}
                    if stored.get("status") == "ready":
                        if stored.get("symbol") != key[1] or stored.get("day") != key[0].isoformat():
                            self._failure(key, "stored_reference_identity_unknown")
                            return
                        reference = ReferenceData(stored["shares_outstanding"], stored["avg_daily_volume"])
                        if complete_reference(reference) and self._day == key[0]:
                            self._ready[key] = reference
                    # Pending/failed/expired-after-claim is never permission to retry.
                    return
                reference = await self._fetch_readonly(key[1], key[0])
                if self._closing:
                    return
                if not complete_reference(reference):
                    await self.redis.set(storage_key, '{"status":"failed"}', xx=True, ex=172800)
                    self._failure(key, "reference_incomplete")
                    return
                payload = json.dumps({"status": "ready", "symbol": key[1], "day": key[0].isoformat(),
                                      "shares_outstanding": reference.shares_outstanding,
                                      "avg_daily_volume": reference.avg_daily_volume}, allow_nan=False)
                saved = await self.redis.set(storage_key, payload, xx=True, ex=172800)
                if not saved:
                    self._failure(key, "result_store_unknown")
                    return
                if self._day == key[0] and not self._closing:
                    self._ready[key] = reference
            except asyncio.CancelledError:
                raise
            except Exception:
                # The durable pending claim also prevents retries on ambiguous errors.
                self._failure(key, "fetch_or_store_failed")

    async def drain(self):
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def close(self):
        self._closing = True
        try:
            await asyncio.wait_for(self.drain(), timeout=3.0)
        except TimeoutError:
            if not self._shutdown_logged:
                self._shutdown_logged = True
                self.logger.warning("[NUWEREF1-SHUTDOWN-TIMEOUT] readonly_reference_work_abandoned")
        except asyncio.CancelledError:
            for task in tuple(self._tasks):
                task.cancel()
            raise
