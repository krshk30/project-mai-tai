"""Batched, append-only writer for Schwab LEVELONE ticks (schwab_1m_v2).

Pure observer/tee. Buffers `SchwabTick`s from the streamer's `on_tick` callback
and flushes them to `market_trade_ticks` / `market_quote_ticks` in batches, OFF
the event loop (`asyncio.to_thread`), with `ON CONFLICT DO NOTHING`. It NEVER
applies backpressure to the streamer: on overflow it drops oldest and counts it.
Capture is best-effort evidence, not execution-critical, and shares nothing with
the strategy / bar feed.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import MarketQuoteTick, MarketTradeTick
from project_mai_tai.market_data.schwab_v2_streamer import SchwabTick
from project_mai_tai.settings import Settings

logger = logging.getLogger(__name__)
PROVIDER = "schwab"
_MAX_FLUSH = 2000  # cap rows per to_thread roundtrip


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class SchwabV2TickWriter:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session] | None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._buf: deque[SchwabTick] = deque()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self.ticks_written = 0
        self.dropped = 0
        self._flush_interval = max(
            0.25, float(getattr(settings, "strategy_schwab_1m_v2_tick_flush_interval_secs", 2.0))
        )
        self._batch = max(
            1, int(getattr(settings, "strategy_schwab_1m_v2_tick_flush_batch_size", 500))
        )
        self._max_buffer = max(
            self._batch, int(getattr(settings, "strategy_schwab_1m_v2_tick_max_buffer", 50_000))
        )

    async def on_tick(self, tick: SchwabTick) -> None:
        """Streamer callback — O(1), never blocks the receive loop."""
        if len(self._buf) >= self._max_buffer:
            self._buf.popleft()
            self.dropped += 1
        self._buf.append(tick)
        if len(self._buf) >= self._batch:
            self._wake.set()

    async def run(self) -> None:
        if self.session_factory is None:
            logger.warning("schwab_v2 tick writer idle: session_factory unavailable")
            await self._stop.wait()
            return
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._flush_interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            await self._flush()
        await self._flush()  # final drain on shutdown

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def stats(self) -> dict[str, int]:
        return {
            "buffered": len(self._buf),
            "ticks_written": self.ticks_written,
            "dropped": self.dropped,
        }

    async def _flush(self) -> None:
        if not self._buf:
            return
        n = min(len(self._buf), _MAX_FLUSH)
        batch = [self._buf.popleft() for _ in range(n)]
        try:
            self.ticks_written += await asyncio.to_thread(self._write_batch, batch)
        except Exception:
            logger.exception(
                "schwab_v2 tick writer flush failed (%d ticks lost this batch)", len(batch)
            )

    def _write_batch(self, batch: list[SchwabTick]) -> int:
        trades: list[dict] = []
        quotes: list[dict] = []
        for t in batch:
            ev = datetime.fromtimestamp(t.event_ts_ms / 1000.0, UTC)
            common = {
                "provider": PROVIDER, "service": t.service, "symbol": t.symbol,
                "event_ts": ev, "cumulative_volume": t.cumulative_volume,
                "raw": t.raw, "raw_hash": t.raw_hash,
            }
            if t.kind == "trade":
                trades.append({**common, "price": _dec(t.price), "size": t.size})
            else:
                quotes.append({
                    **common,
                    "bid_price": _dec(t.bid_price), "ask_price": _dec(t.ask_price),
                    "last_price": _dec(t.last_price), "bid_size": t.bid_size,
                    "ask_size": t.ask_size, "last_size": t.last_size,
                })
        written = 0
        assert self.session_factory is not None
        with self.session_factory() as session:
            if trades:
                written += self._insert(session, MarketTradeTick, trades,
                                        "uq_market_trade_ticks_dedupe")
            if quotes:
                written += self._insert(session, MarketQuoteTick, quotes,
                                        "uq_market_quote_ticks_dedupe")
            session.commit()
        return written

    @staticmethod
    def _insert(session: Session, model: type, rows: list[dict], constraint: str) -> int:
        stmt = pg_insert(model).values(rows).on_conflict_do_nothing(constraint=constraint)
        result = session.execute(stmt)
        rc = result.rowcount
        return rc if isinstance(rc, int) and rc >= 0 else len(rows)


__all__ = ["SchwabV2TickWriter"]
