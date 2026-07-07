"""Central market-data capture — GLOBAL, bot-agnostic, additive + isolated.

A flag-gated, READ-ONLY consumer of the shared ``mai_tai:market-data`` Redis
stream. The market-data gateway publishes Polygon/Massive trade prints + L1
quotes to that stream regardless of who listens; this consumer only *reads* it
and *writes* parsed rows to central per-type tables (``market_capture_trades``,
``market_capture_quotes``) keyed by symbol+time, so ANY bot — today's or
tomorrow's — can query the raw tick history for backtesting. It is NOT part of
any bot (that was the #335 mistake) and touches NOTHING in the trading path:
no gateway changes, no bot changes, no order flow.

Design notes:
- **Default OFF.** ``market_capture_enabled=False`` -> ``run()`` returns at once.
- **Off-loop batched writes (#350 pattern).** Events buffer and flush via
  ``asyncio.to_thread`` so the consumer loop never stalls at 50-150+ inserts/sec.
- **Timestamp normalization (ORB-bug lesson).** Trade ``timestamp_ns`` is ms on
  the live WS feed; it is normalized to true ns (``market_data.tick_time``)
  before storage — a 1970 timestamp can never be persisted. Quotes carry no
  payload timestamp, so their ``event_ts`` is the event ``produced_at``.
- **Append-only**, no ``raw`` blob, no unique-dedupe constraint — keeps the
  high-volume trade table cheap; backtests de-dupe if a restart replays a tick.
- **Tail-from-now offset** (``$``): a brief gap on restart is acceptable given
  the stream's ~8-minute retention; a consumer group is the future hardening.
- **Extensible:** add an ``elif event_type == ...`` branch + a table to capture
  book/L2 later WITHOUT a schema rewrite (none flows on the stream today).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

from redis.asyncio import Redis
from sqlalchemy import insert
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import MarketCaptureQuote, MarketCaptureTrade
from project_mai_tai.db.session import build_timed_session_factory
from project_mai_tai.events import stream_name
from project_mai_tai.market_data.tick_time import normalize_ts_ns, ns_to_datetime
from project_mai_tai.settings import Settings, get_settings

SERVICE_NAME = "market-capture"
logger = logging.getLogger(SERVICE_NAME)


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _conditions(value: object) -> str | None:
    if not value:
        return None
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value) or None
    return str(value)


def _parse_iso(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class MarketCaptureService:
    def __init__(
        self,
        settings: Settings | None = None,
        redis_client: Redis | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis = redis_client or Redis.from_url(
            self.settings.redis_url, decode_responses=True
        )
        self.session_factory = session_factory  # built lazily when enabled
        self._md_offset: str = "$"  # tail new events only
        self._trades: list[dict] = []
        self._quotes: list[dict] = []
        self._rows_written = 0
        self._dropped = 0

    async def run(self) -> None:
        if not bool(getattr(self.settings, "market_capture_enabled", False)):
            logger.info("[CAPTURE] disabled (market_capture_enabled=false); not starting")
            return
        if self.session_factory is None:
            self.session_factory = build_timed_session_factory(self.settings, service="market_capture", profile="slow")
        stream = stream_name(self.settings.redis_stream_prefix, "market-data")
        batch = int(getattr(self.settings, "market_capture_batch_size", 1000))
        flush_secs = float(getattr(self.settings, "market_capture_flush_secs", 2.0))
        stats_every = int(getattr(self.settings, "market_capture_stats_every", 30))
        loop = asyncio.get_running_loop()
        last_flush = loop.time()
        loop_count = 0
        logger.info(
            "[CAPTURE] starting — global market-data capture (stream=%s batch=%d flush=%.1fs)",
            stream, batch, flush_secs,
        )
        try:
            while True:
                try:
                    resp = await self.redis.xread({stream: self._md_offset}, count=batch, block=1000)
                    for _stream, entries in resp or []:
                        for entry_id, fields in entries:
                            self._md_offset = entry_id
                            self._ingest(fields.get("data"))
                    now = loop.time()
                    buffered = len(self._trades) + len(self._quotes)
                    if buffered >= batch or (buffered and now - last_flush >= flush_secs):
                        await self._flush()
                        last_flush = now
                    loop_count += 1
                    if stats_every and loop_count % stats_every == 0:
                        logger.info(
                            "[CAPTURE] rows_written=%d buffered=%d dropped=%d",
                            self._rows_written, buffered, self._dropped,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A transient redis/DB blip must not kill the capture loop —
                    # log, back off, and resume (the systemd unit also restarts,
                    # but this keeps a single run alive across hiccups).
                    logger.exception("[CAPTURE] loop iteration failed; backing off 2s")
                    await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            await self._flush()
            logger.info("[CAPTURE] cancelled; flushed (rows_written=%d)", self._rows_written)
            raise

    def _ingest(self, raw: object) -> None:
        if not raw:
            return
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return
        payload = obj.get("payload") or {}
        symbol = str(payload.get("symbol", "")).upper()
        if not symbol:
            return
        provider = str(getattr(self.settings, "market_capture_provider_tag", "massive"))
        event_type = obj.get("event_type")
        if event_type == "trade_tick":
            ns = normalize_ts_ns(payload.get("timestamp_ns"))
            event_ts = ns_to_datetime(ns) if ns else _parse_iso(obj.get("produced_at"))
            price = _dec(payload.get("price"))
            if event_ts is None or price is None:
                self._dropped += 1
                return
            self._trades.append({
                "provider": provider,
                "symbol": symbol,
                "event_ts": event_ts,
                "price": price,
                "size": _int(payload.get("size")),
                "exchange": (str(payload["exchange"]) if payload.get("exchange") not in (None, "") else None),
                "conditions": _conditions(payload.get("conditions")),
                "cumulative_volume": _int(payload.get("cumulative_volume")),
            })
        elif event_type == "quote_tick":
            event_ts = _parse_iso(obj.get("produced_at"))
            if event_ts is None:
                self._dropped += 1
                return
            self._quotes.append({
                "provider": provider,
                "symbol": symbol,
                "event_ts": event_ts,
                "bid_price": _dec(payload.get("bid_price")),
                "ask_price": _dec(payload.get("ask_price")),
                "bid_size": _int(payload.get("bid_size")),
                "ask_size": _int(payload.get("ask_size")),
            })
        # else: book/L2 or other types — not on the stream today; add a branch + table later.

    async def _flush(self) -> None:
        trades, quotes = self._trades, self._quotes
        self._trades, self._quotes = [], []
        if not trades and not quotes:
            return
        await asyncio.to_thread(self._write, trades, quotes)

    def _write(self, trades: list[dict], quotes: list[dict]) -> None:
        try:
            with self.session_factory() as session:
                if trades:
                    session.execute(insert(MarketCaptureTrade), trades)
                if quotes:
                    session.execute(insert(MarketCaptureQuote), quotes)
                session.commit()
            self._rows_written += len(trades) + len(quotes)
        except Exception:  # never let a write error kill the loop
            self._dropped += len(trades) + len(quotes)
            logger.exception(
                "[CAPTURE] flush failed (dropped %d trade + %d quote rows)",
                len(trades), len(quotes),
            )


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(MarketCaptureService().run())


if __name__ == "__main__":
    run()
