"""Shared ORB universe and market-data feed. The retired paper unit cannot run here."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.events import MarketDataSubscriptionEvent, MarketDataSubscriptionPayload, stream_name
from project_mai_tai.fanout_outcome_consumer import session_anchor
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_intrabar import OrbBar, OrbConfig
from project_mai_tai.strategy_core.orb_tick_aggregator import OrbTickAggregator

SERVICE_NAME = "orb"
logger = logging.getLogger(SERVICE_NAME)
_ET = ZoneInfo("America/New_York")


def _normalize_trade_ts_ns(value: int | float | str | None) -> int | None:
    """Coerce a gateway ``trade_tick.timestamp_ns`` to true nanoseconds.

    The market-data gateway labels the field ``timestamp_ns`` but the magnitude
    varies by source: Massive/Polygon trade ticks carry MILLISECONDS (13-digit,
    e.g. ``1782135713372``) while Schwab-sourced ticks carry real nanoseconds
    (19-digit). The strategy-engine already normalizes by magnitude
    (``StrategyEngineService._normalize_tick_timestamp_ns``); ORB must do the same.
    Without this, a millisecond value run through ``ms / 1e9`` lands at ~1970
    (``1782135713372 / 1e9`` ≈ 1782 s), and the session-anchored
    ``OrbTickAggregator`` drops every tick (bucket < session_open, minute never
    rolls) — so no opening-range bar ever forms and ORB silently never trades.
    Mirror the strategy-engine magnitude ladder so any source resolves to ns.
    """
    if not value:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v >= 1_000_000_000_000_000_000:  # already nanoseconds (>= ~2001 in ns)
        return v
    if v >= 1_000_000_000_000_000:  # microseconds
        return v * 1_000
    if v >= 1_000_000_000_000:  # milliseconds (the Massive/Polygon case)
        return v * 1_000_000
    if v >= 1_000_000_000:  # seconds
        return v * 1_000_000_000
    return None


class OrbService:
    """Feed-only base of OrbSchwabService; no orders, modelled fills or paper writer."""

    _MARKET_DATA_XREAD_COUNT = 1000
    _MARKET_DATA_DRAIN_BUDGET = 20_000
    _UNIVERSE_REFRESH_SECS = 5.0
    _running_high_mode = True
    _fixed_resting_mode = False
    _reclaim_mode = False

    def __init__(self, settings: Settings | None = None, redis_client: Redis | None = None,
                 session_factory: sessionmaker[Session] | None = None) -> None:
        self.settings = settings or Settings(_env_file=None)
        self.redis = redis_client or Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.session_factory = session_factory
        self._aggregators: dict[str, OrbTickAggregator] = {}
        self._last_gateway_symbols: list[str] = []
        self._gateway_subscription_announced = False
        self._gateway_subscription_uncertain = False
        self._md_offset = "$"
        self._states: dict = {}
        self._universe: set[str] = set()
        self._cfg = OrbConfig(universe_lead_minutes=int(self.settings.orb_universe_lead_minutes))
        self._last_universe_refresh_at: datetime | None = None
        now = datetime.now(UTC)
        self._session_date = now.astimezone(_ET).date()
        self._scanner_session_start = session_anchor(now)

    def _handle_market_data(self, fields: dict) -> None:
        raw = fields.get("data")
        if not raw:
            return
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(obj, dict) or obj.get("event_type") != "trade_tick":
            return
        payload = obj.get("payload") or {}
        if not isinstance(payload, dict):
            return
        symbol = str(payload.get("symbol", "")).upper()
        if not symbol or symbol not in self._last_gateway_symbols:
            return
        try:
            price = float(payload["price"])
            size = float(payload.get("size", 0) or 0)
        except (KeyError, TypeError, ValueError):
            return
        ts_ns = _normalize_trade_ts_ns(payload.get("timestamp_ns"))
        ts = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC) if ts_ns else datetime.now(UTC)
        agg = self._aggregators.get(symbol)
        if agg is None:
            anchor = self._observe_open_utc() if self._running_high_mode else self._session_open_utc()
            agg = OrbTickAggregator(session_open=anchor)
            self._aggregators[symbol] = agg
        bar = agg.add_tick(ts, price, size)
        if bar is not None:
            self._on_bar(symbol, bar, observed_at=ts, observed_price=price)

    def _on_bar(self, symbol: str, bar: OrbBar, *, observed_at: datetime | None = None,
                observed_price: float | None = None) -> None:
        raise NotImplementedError("The live ORB subclass owns bar decisions")

    def _refresh_universe(self) -> list[str]:
        self._universe = {s.upper() for s in self._pre_open_universe()}
        return sorted(self._universe)

    def _pre_open_universe(self, now: datetime | None = None) -> list[str]:
        """Confirmed scanner names whose confirmation landed at/before 09:25 ET (read
        from the persisted ``scanner_confirmed_last_nonempty`` snapshot). Names that
        confirm DURING 09:25-10:00 are OUT OF SCOPE by design (operator decision
        2026-06-18) — no clean opening range. Empty => ORB sits the day out."""
        if self.session_factory is None:
            return []
        try:
            with self.session_factory() as session:
                snap = session.scalar(
                    select(DashboardSnapshot).where(
                        DashboardSnapshot.snapshot_type == "scanner_confirmed_last_nonempty"
                    )
                )
        except Exception:
            logger.exception("[ORB] failed reading confirmed-candidate snapshot")
            return []
        if snap is None or not isinstance(snap.payload, dict):
            return []
        # Freshness is scanner-session scoped, not calendar-day scoped. Between
        # midnight and 04:00 ET both UTC and ET dates can make a prior-session
        # snapshot look current; the producer's 04:00 marker is authoritative.
        scanner_session_raw = str(snap.payload.get("scanner_session_start_utc", ""))
        try:
            scanner_session_start = datetime.fromisoformat(scanner_session_raw)
            if scanner_session_start.tzinfo is None:
                scanner_session_start = scanner_session_start.replace(tzinfo=UTC)
        except ValueError:
            return []
        current = now or datetime.now(UTC)
        if scanner_session_start.astimezone(UTC) != session_anchor(current):
            return []

        # Keep the timestamp check as an independent corrupt/stale payload guard.
        persisted = str(snap.payload.get("persisted_at", ""))
        if not persisted.startswith(current.date().isoformat()):
            return []
        cutoff = (datetime(2000, 1, 1, 9, 30) - timedelta(minutes=self._cfg.universe_lead_minutes)).time()
        out: list[str] = []
        for cand in snap.payload.get("all_confirmed_candidates") or []:
            ticker = str(cand.get("ticker", "")).upper()
            confirmed = self._parse_et_time(str(cand.get("confirmed_at", "")))
            if ticker and confirmed is not None and confirmed <= cutoff:
                out.append(ticker)
        return out

    @staticmethod
    def _parse_et_time(value: str) -> time | None:
        s = value.replace(" ET", "").strip()
        if not s:
            return None
        for fmt in ("%I:%M:%S %p", "%I:%M %p"):
            try:
                return datetime.strptime(s, fmt).time()
            except ValueError:
                continue
        return None

    async def _sync_gateway_subscription(self, symbols: list[str]) -> None:
        desired = sorted({str(s).upper() for s in symbols if str(s).strip()})
        first_announcement = (
            self.settings.market_data_subscription_startup_enabled
            and not self._gateway_subscription_announced
        )
        if (
            desired == self._last_gateway_symbols and not first_announcement
            and not getattr(self, "_gateway_subscription_uncertain", False)
        ):
            return  # debounce — publish only on change
        event = MarketDataSubscriptionEvent(
            source_service=SERVICE_NAME,
            payload=MarketDataSubscriptionPayload(
                consumer_name=SERVICE_NAME, mode="replace", symbols=desired
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
        logger.info("[ORB-GATEWAY-SUBSCRIBE] consumer=%s symbols=%d", SERVICE_NAME, len(desired))

    async def _drain_market_data(self) -> int:
        """Drain the market-data stream to a budget (mirrors strategy-engine #175/#179).

        The first xread BLOCKs briefly to wait for ticks; subsequent passes are
        NON-blocking and stop the instant the stream is empty (a pass returns fewer than
        ``count``). Bounded by ``_MARKET_DATA_DRAIN_BUDGET`` so one hot symbol can't starve
        the loop. Returns the number of ticks processed so the caller can loop immediately
        (no sleep) while still backlogged — keeping ORB caught up through the open burst
        instead of falling minutes behind (the 2026-06-30 ~1:47 CELZ entry lag)."""
        if not self._last_gateway_symbols:
            return 0
        stream = stream_name(self.settings.redis_stream_prefix, "market-data")
        processed = 0
        block: int | None = 500  # first pass waits for ticks; follow-up passes don't
        while processed < self._MARKET_DATA_DRAIN_BUDGET:
            response = await self.redis.xread(
                {stream: self._md_offset}, count=self._MARKET_DATA_XREAD_COUNT, block=block
            )
            block = None
            if not response:
                break
            batch = 0
            for _stream, entries in response:
                for entry_id, fields in entries:
                    self._md_offset = entry_id
                    self._handle_market_data(fields)
                    batch += 1
            processed += batch
            if batch < self._MARKET_DATA_XREAD_COUNT:
                break  # drained the stream this pass — caught up
        return processed

    @staticmethod
    def _event_time(obj: dict) -> datetime:
        raw = str(obj.get("produced_at") or "").strip()
        if raw:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
            except ValueError:
                pass
        return datetime.now(UTC)

    @staticmethod
    def _session_open_utc() -> datetime:
        now_et = datetime.now(_ET)
        return now_et.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(UTC)

    @staticmethod
    def _observe_open_utc() -> datetime:
        """09:25 ET — the running-high observation anchor (reference builds from here)."""
        now_et = datetime.now(_ET)
        return now_et.replace(hour=9, minute=25, second=0, microsecond=0).astimezone(UTC)


async def main() -> None:
    raise RuntimeError("project-mai-tai-orb is retired; run orb-schwab only")


def run() -> None:
    asyncio.run(main())
