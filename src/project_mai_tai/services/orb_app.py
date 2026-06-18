"""ORB (P6 "OPEN") isolated bot — scaffold + gateway data layer (3a) + entry brain (3b).

Runs as its OWN process/event loop (escapes the shared strategy-engine 1 Hz-loop
contention by construction) and consumes the EXISTING market-data gateway as a
registered consumer (no new Schwab streamer session, no credential collision).

Loop: read the pre-09:25 confirmed universe (the binding rule) → register those
symbols as a gateway consumer → drain their trade ticks → aggregate to 1-min bars →
per symbol, build the 5-min OR, apply the breakout filter (orb_intrabar leaf),
arm-on-window-open, and emit one open intent with stop_guard_enabled / stop_loss_pct
/ trail_pct (the OMS TRAIL-8% ratchet, #340, then drives the exit).

Default OFF: with ``orb_enabled=False`` ``run()`` returns immediately — no DB read,
no consumer, no drain, no intent (byte-identical to today).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.events import (
    MarketDataSubscriptionEvent,
    MarketDataSubscriptionPayload,
    TradeIntentEvent,
    TradeIntentPayload,
    stream_name,
)
from project_mai_tai.settings import Settings, get_settings
from project_mai_tai.strategy_core.orb_intrabar import (
    ExecutionMode,
    OpeningRange,
    OrbBar,
    OrbConfig,
    bar_confirms_breakout,
    build_opening_range,
    entry_fill_price,
)
from project_mai_tai.strategy_core.orb_tick_aggregator import OrbTickAggregator

SERVICE_NAME = "orb"
logger = logging.getLogger(SERVICE_NAME)
_ET = ZoneInfo("America/New_York")


@dataclass
class _SymbolState:
    or_bars: list[OrbBar] = field(default_factory=list)
    or_evaluated: bool = False
    opening_range: OpeningRange | None = None
    traded: bool = False


class OrbService:
    def __init__(
        self,
        settings: Settings | None = None,
        redis_client: Redis | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis = redis_client or Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.session_factory = session_factory  # built lazily when enabled (no DB connect when off)
        self._aggregators: dict[str, OrbTickAggregator] = {}
        self._last_gateway_symbols: list[str] = []
        self._md_offset: str = "$"  # tail new ticks only
        self._states: dict[str, _SymbolState] = {}
        self._universe: set[str] = set()
        self._pending_intents: list[tuple[str, float]] = []
        self._cfg = OrbConfig(
            or_minutes=int(self.settings.orb_or_minutes),
            vol_mult=float(self.settings.orb_vol_mult),
            width_max_pct=float(self.settings.orb_width_max_pct),
            width_min_pct=float(self.settings.orb_width_min_pct),
            cutoff_minutes=int(self.settings.orb_cutoff_minutes),
            trail_pct=float(self.settings.orb_trail_pct),
            universe_lead_minutes=int(self.settings.orb_universe_lead_minutes),
        )
        self._mode = ExecutionMode(str(self.settings.orb_execution_mode))

    # ----- lifecycle -----
    async def run(self) -> None:
        if not bool(getattr(self.settings, "orb_enabled", False)):
            logger.info("[ORB] disabled (orb_enabled=false); not starting")
            return
        if self.session_factory is None:
            self.session_factory = build_session_factory(self.settings)
        logger.info("[ORB] starting — isolated bot, market-data gateway consumer")
        try:
            while True:
                await self._sync_gateway_subscription(self._refresh_universe())
                await self._drain_market_data()
                await self._publish_pending_intents()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("[ORB] cancelled; shutting down")
            raise

    # ----- universe: pre-09:25 confirmed names (the binding rule) -----
    def _refresh_universe(self) -> list[str]:
        self._universe = {s.upper() for s in self._pre_open_universe()}
        return sorted(self._universe)

    def _pre_open_universe(self) -> list[str]:
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
        # freshness — only today's snapshot (avoid trading a stale prior session)
        persisted = str(snap.payload.get("persisted_at", ""))
        if not persisted.startswith(datetime.now(UTC).date().isoformat()):
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

    # ----- the entry brain: OR build -> breakout -> arm-on-window-open -> open intent -----
    def _on_bar(self, symbol: str, bar: OrbBar) -> None:
        open_utc = self._session_open_utc()
        if bar.timestamp < open_utc:
            return  # pre-open bar — not part of the opening range
        or_end = open_utc + timedelta(minutes=self._cfg.or_minutes)
        cutoff = open_utc + timedelta(minutes=self._cfg.cutoff_minutes)
        st = self._states.setdefault(symbol, _SymbolState())
        if bar.timestamp < or_end:
            st.or_bars.append(bar)  # building the opening range (09:30-09:34)
            return
        if not st.or_evaluated:
            st.or_evaluated = True
            # ARM only pre-09:25-universe names; build_opening_range returns None on
            # insufficient coverage or width > cap (skip-this-symbol).
            if symbol in self._universe:
                st.opening_range = build_opening_range(st.or_bars, self._cfg)
        if st.opening_range is None or st.traded or bar.timestamp > cutoff:
            return
        if bar_confirms_breakout(st.opening_range, bar, self._cfg):
            entry = entry_fill_price(st.opening_range, bar, self._mode)
            st.traded = True  # one trade per symbol per session
            self._pending_intents.append((symbol, entry))
            logger.info(
                "[ORB-BREAKOUT] %s entry=%.4f OR_high=%.4f mode=%s",
                symbol, entry, st.opening_range.high, self._mode.value,
            )

    def _build_open_intent(self, symbol: str, entry_price: float) -> TradeIntentEvent:
        pct = str(self.settings.orb_trail_pct)
        metadata = {
            "stop_guard_enabled": "true",
            "stop_loss_pct": pct,   # initial stop = trail% below entry
            "trail_pct": pct,       # ratchet — drives the OMS TRAIL-8% trailing stop (#340)
            "stop_guard_quote_max_age_ms": "2000",
            "stop_guard_initial_panic_buffer_pct": "1.5",
            "orb_entry": "true",
            "execution_mode": self._mode.value,
        }
        return TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=SERVICE_NAME,
                broker_account_name=str(self.settings.orb_broker_account_name),
                symbol=symbol,
                side="buy",
                quantity=Decimal(str(int(self.settings.orb_quantity))),
                intent_type="open",
                reason="ORB_OPEN",
                metadata=metadata,
            ),
        )

    async def _publish_pending_intents(self) -> None:
        if not self._pending_intents:
            return
        pending, self._pending_intents = self._pending_intents, []
        for symbol, entry_price in pending:
            event = self._build_open_intent(symbol, entry_price)
            await self.redis.xadd(
                stream_name(self.settings.redis_stream_prefix, "strategy-intents"),
                {"data": event.model_dump_json()},
                maxlen=self.settings.redis_strategy_intent_stream_maxlen,
                approximate=True,
            )
            logger.info("[ORB-OPEN] %s entry=%.4f trail_pct=%s", symbol, entry_price, self.settings.orb_trail_pct)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await OrbService().run()


def run() -> None:
    asyncio.run(main())
