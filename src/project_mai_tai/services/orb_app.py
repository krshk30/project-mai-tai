"""ORB (P6 "OPEN") isolated bot — scaffold + gateway data (3a) + entry brain (3b) + heartbeat (3c).

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
    IsolatedBotStateEvent,
    MarketDataSubscriptionEvent,
    MarketDataSubscriptionPayload,
    StrategyBotStatePayload,
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


@dataclass
class _SymbolState:
    or_bars: list[OrbBar] = field(default_factory=list)
    or_evaluated: bool = False
    opening_range: OpeningRange | None = None
    traded: bool = False
    entry_price: float | None = None
    last_bar_at: str = ""
    # intrabar-reclaim mode only: start (ms UTC) of the current uninterrupted hold
    # above OR_high; reset to None whenever a tick prints back below OR_high.
    reclaim_cross_ms: int | None = None
    # ms UTC of the reclaim-confirm (entry emit) — fill-instrumentation timestamp.
    reclaim_emit_ms: int | None = None
    # running-high mode only: highest 1-min bar-high seen since 09:25 (the breakout level).
    running_high: float | None = None


class OrbService:
    # Class-level defaults so instances built via __new__ (some unit tests) read the
    # legacy path; __init__ overrides them from settings.
    _reclaim_mode: bool = False
    _reclaim_hold_ms: int = 25_000
    _running_high_mode: bool = False
    # Market-data consume-loop throughput (mirrors strategy-engine #175/#179). The open
    # burst spans the WHOLE scanner universe and exceeded 700 ticks/s on 2026-06-30; a
    # single count=500 xread per 1s loop fell ~3x behind (effective ~196/s), surfacing the
    # 09:30 bar + its entry ~1:47 late. Drain-to-budget + non-blocking follow-up passes +
    # universe-DB-read off the hot path keep ORB caught up through the open.
    _MARKET_DATA_XREAD_COUNT: int = 1000
    _MARKET_DATA_DRAIN_BUDGET: int = 20_000
    _UNIVERSE_REFRESH_SECS: float = 5.0
    _HEARTBEAT_SECS: float = 5.0

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
        # Flag-gated intrabar-reclaim live test: cap-off + reclaim@OR_high + N% trail.
        # Default False -> every reclaim branch is skipped and ORB is byte-identical.
        self._reclaim_mode = bool(getattr(self.settings, "orb_intrabar_reclaim_enabled", False))
        self._reclaim_hold_ms = int(getattr(self.settings, "orb_reclaim_hold_secs", 25)) * 1000
        # Running-high breakout mode (operator-validated). Mutually exclusive with reclaim:
        # only active when reclaim is OFF. Default False -> byte-identical to existing paths.
        self._running_high_mode = bool(
            getattr(self.settings, "orb_running_high_enabled", False)
        ) and not self._reclaim_mode
        self._rh_gap_cap_pct = float(getattr(self.settings, "orb_running_high_gap_cap_pct", 1.5))
        self._rh_window_min = int(getattr(self.settings, "orb_running_high_window_minutes", 30))
        # OMS-quote-priced entry (Piece 1). When True, the bot OMITS limit_price/reference_price
        # from open intents (fail-closed: a stale signal-time price is structurally unshippable)
        # and hands the OMS the bound (orb_intended_break_level) + gap_cap + price_source so the
        # OMS re-prices off its live quote at placement. Default False -> byte-identical emit.
        self._oms_quote_priced = bool(
            getattr(self.settings, "orb_oms_quote_priced_entry_enabled", False)
        )
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
        # Timers so the universe DB read + heartbeat run on wall-clock cadence, NOT every
        # tick-drain iteration (which now spins fast while catching up at the open).
        self._last_universe_refresh_at: datetime | None = None
        self._last_heartbeat_at: datetime | None = None
        # ET session date — when it rolls, per-symbol state + aggregators are reset so the
        # next session starts clean (running_high re-seeds from 09:25, traded flags clear,
        # aggregators rebuild with the new session anchor). Without this, a bot left running
        # across midnight carries the prior day's state into the new session.
        self._session_date = datetime.now(_ET).date()

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
                self._maybe_roll_session()
                now = datetime.now(UTC)
                # Universe = a DashboardSnapshot DB read; keep it OFF the per-tick hot path
                # (it changes rarely: 09:25 freeze + promotions). Refresh on a timer.
                if (
                    self._last_universe_refresh_at is None
                    or (now - self._last_universe_refresh_at).total_seconds() >= self._UNIVERSE_REFRESH_SECS
                ):
                    await self._sync_gateway_subscription(self._refresh_universe())
                    self._last_universe_refresh_at = now
                processed = await self._drain_market_data()
                await self._publish_pending_intents()
                if (
                    self._last_heartbeat_at is None
                    or (now - self._last_heartbeat_at).total_seconds() >= self._HEARTBEAT_SECS
                ):
                    await self._publish_heartbeat()
                    self._last_heartbeat_at = now
                # Backlogged (drain hit the budget) -> loop immediately to catch up. Caught
                # up -> the drain's first-pass BLOCK already paced us; sleep only when there
                # was genuinely nothing to do (no symbols yet / empty stream).
                if processed == 0:
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("[ORB] cancelled; shutting down")
            raise

    def _maybe_roll_session(self) -> None:
        """Reset per-symbol state + aggregators when the ET date rolls, so each session
        starts clean (no prior-day running_high / traded flag / stale-symbol carryover).
        No-op within the same session; only fires on a date change."""
        today = datetime.now(_ET).date()
        if today == self._session_date:
            return
        prior = self._session_date
        self._session_date = today
        self._states.clear()
        self._aggregators.clear()
        logger.info("[ORB] day-roll reset %s -> %s: cleared per-symbol state + aggregators", prior, today)

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
        ts_ns = _normalize_trade_ts_ns(payload.get("timestamp_ns"))
        ts = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC) if ts_ns else datetime.now(UTC)
        agg = self._aggregators.get(symbol)
        if agg is None:
            # Running-high mode seeds the reference from 09:25 (pre-09:30) bars, so anchor
            # the aggregator at 09:25; all other modes anchor at the 09:30 session open.
            anchor = self._observe_open_utc() if self._running_high_mode else self._session_open_utc()
            agg = OrbTickAggregator(session_open=anchor)
            self._aggregators[symbol] = agg
        bar = agg.add_tick(ts, price, size)
        if bar is not None:
            self._on_bar(symbol, bar)
        if self._reclaim_mode:
            self._check_reclaim(symbol, price, ts)

    @staticmethod
    def _session_open_utc() -> datetime:
        now_et = datetime.now(_ET)
        return now_et.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(UTC)

    @staticmethod
    def _observe_open_utc() -> datetime:
        """09:25 ET — the running-high observation anchor (reference builds from here)."""
        now_et = datetime.now(_ET)
        return now_et.replace(hour=9, minute=25, second=0, microsecond=0).astimezone(UTC)

    def _on_bar_running_high(self, symbol: str, bar: OrbBar) -> None:
        """Running-high breakout entry (operator-validated). Reference = highest 1-min
        bar-high since 09:25; enter when a bar breaks it within 09:30..open+window, at the
        breakout level, only if the fill is within gap_cap% of the broken high. v1 = one
        entry per symbol (re-entry is a follow-up). Exit = OMS trail orb_reclaim_trail_pct."""
        observe_open = self._observe_open_utc()
        if bar.timestamp < observe_open:
            return
        open_utc = self._session_open_utc()
        cutoff = open_utc + timedelta(minutes=self._rh_window_min)
        st = self._states.setdefault(symbol, _SymbolState())
        st.last_bar_at = bar.timestamp.isoformat()
        if st.running_high is None:
            st.running_high = bar.high          # first observed bar seeds the reference
            return
        if (
            not st.traded
            and symbol in self._universe
            and open_utc <= bar.timestamp <= cutoff
            and bar.high > st.running_high
        ):
            level = st.running_high
            fill = level if bar.open <= level else bar.open   # gap-up fills at the open
            if fill <= level * (1.0 + self._rh_gap_cap_pct / 100.0):
                st.traded = True               # one entry per symbol (v1)
                st.entry_price = fill
                self._pending_intents.append((symbol, fill))
                logger.info(
                    "[ORB-RH-ENTRY] %s entry=%.4f broke_high=%.4f gap=%.2f%%",
                    symbol, fill, level, (fill / level - 1.0) * 100.0,
                )
        st.running_high = max(st.running_high, bar.high)

    # ----- the entry brain: OR build -> breakout -> arm-on-window-open -> open intent -----
    def _on_bar(self, symbol: str, bar: OrbBar) -> None:
        if self._running_high_mode:
            self._on_bar_running_high(symbol, bar)
            return
        open_utc = self._session_open_utc()
        if bar.timestamp < open_utc:
            return  # pre-open bar — not part of the opening range
        or_end = open_utc + timedelta(minutes=self._cfg.or_minutes)
        cutoff = open_utc + timedelta(minutes=self._cfg.cutoff_minutes)
        st = self._states.setdefault(symbol, _SymbolState())
        st.last_bar_at = bar.timestamp.isoformat()
        if bar.timestamp < or_end:
            st.or_bars.append(bar)  # building the opening range (09:30-09:34)
            # Reclaim mode arms as soon as the OR's bars are all in, so the tick-level
            # reclaim can fire from 09:35 — and with NO width cap (cap-off).
            if (
                self._reclaim_mode
                and not st.or_evaluated
                and len(st.or_bars) >= self._cfg.or_minutes
                and symbol in self._universe
            ):
                st.or_evaluated = True
                st.opening_range = self._build_or_no_cap(st.or_bars)
            return
        if not st.or_evaluated:
            st.or_evaluated = True
            # ARM only pre-09:25-universe names; build_opening_range returns None on
            # insufficient coverage or width > cap (skip-this-symbol).
            if symbol in self._universe:
                st.opening_range = (
                    self._build_or_no_cap(st.or_bars)
                    if self._reclaim_mode
                    else build_opening_range(st.or_bars, self._cfg)
                )
        if self._reclaim_mode:
            return  # entry is tick-driven (_check_reclaim); no bar-close breakout
        if st.opening_range is None or st.traded or bar.timestamp > cutoff:
            return
        if bar_confirms_breakout(st.opening_range, bar, self._cfg):
            entry = entry_fill_price(st.opening_range, bar, self._mode)
            st.traded = True  # one trade per symbol per session
            st.entry_price = entry
            self._pending_intents.append((symbol, entry))
            logger.info(
                "[ORB-BREAKOUT] %s entry=%.4f OR_high=%.4f mode=%s",
                symbol, entry, st.opening_range.high, self._mode.value,
            )

    def _build_or_no_cap(self, or_bars: list[OrbBar]) -> OpeningRange | None:
        """Opening range with the 2-12% width band REMOVED (cap-off). Only gate is
        in-time coverage (>= or_minutes bars), so high-volatility wide-range names
        still arm — the whole point of the cap-off test."""
        if len(or_bars) < self._cfg.or_minutes:
            return None
        high = max(b.high for b in or_bars)
        low = min(b.low for b in or_bars)
        avg_volume = sum(b.volume for b in or_bars) / len(or_bars)
        return OpeningRange(high=high, low=low, avg_volume=avg_volume)

    def _check_reclaim(self, symbol: str, price: float, ts: datetime) -> None:
        """Intrabar reclaim entry (cap-off mode). Once the OR is armed, a tick at/above
        OR_high starts a hold timer; if price stays >= OR_high for orb_reclaim_hold_secs,
        emit ONE open intent as a resting LIMIT at OR_high. A tick back below OR_high
        resets the timer (a pullback before the reclaim is fine — the sustained reclaim
        is the confirmation). Entries only in (OR-end, cutoff]."""
        st = self._states.get(symbol)
        if st is None or st.opening_range is None or st.traded:
            return
        open_utc = self._session_open_utc()
        or_end = open_utc + timedelta(minutes=self._cfg.or_minutes)
        cutoff = open_utc + timedelta(minutes=self._cfg.cutoff_minutes)
        if ts < or_end or ts > cutoff:
            return
        or_high = st.opening_range.high
        ts_ms = int(ts.timestamp() * 1000)
        if price >= or_high:
            if st.reclaim_cross_ms is None:
                st.reclaim_cross_ms = ts_ms
                logger.info("[ORB-RECLAIM-CROSS] %s price=%.4f OR_high=%.4f", symbol, price, or_high)
            elif ts_ms - st.reclaim_cross_ms >= self._reclaim_hold_ms:
                st.traded = True  # one trade per symbol per session
                st.entry_price = or_high
                st.reclaim_emit_ms = ts_ms  # fill-instrumentation: confirm time
                self._pending_intents.append((symbol, or_high))
                logger.info(
                    "[ORB-RECLAIM-ENTRY] %s intended=%.4f held=%.0fs",
                    symbol, or_high, self._reclaim_hold_ms / 1000,
                )
        else:
            st.reclaim_cross_ms = None  # hold broke — wait for the next reclaim

    def _build_open_intent(self, symbol: str, entry_price: float) -> TradeIntentEvent:
        if self._running_high_mode:
            pct = str(self.settings.orb_reclaim_trail_pct)   # 3% trail (shared setting)
            qty = int(self.settings.orb_reclaim_quantity)     # qty 5 (shared setting)
            metadata = {
                "stop_guard_enabled": "true",
                "stop_loss_pct": pct,
                "trail_pct": pct,                 # OMS trailing stop (#340)
                "stop_guard_quote_max_age_ms": "2000",
                "stop_guard_initial_panic_buffer_pct": "1.5",
                "orb_entry": "true",
                "execution_mode": "running_high_breakout",
                "order_type": "limit",            # resting limit at the breakout level
                "orb_intended_break_level": f"{entry_price:.4f}",
            }
            if self._oms_quote_priced:
                # Fail-closed: omit limit_price/reference_price so a stale signal-time price
                # cannot be shipped even if the OMS path is bypassed (the adapter falls back
                # to reference_price, so it too must be absent). The OMS re-prices off its live
                # quote at placement, bounded by orb_intended_break_level + gap_cap.
                metadata["price_source"] = "ask"
                metadata["orb_gap_cap_pct"] = f"{self._rh_gap_cap_pct}"
            else:
                # Byte-identical legacy: ship the signal-time break level as the limit.
                metadata["limit_price"] = f"{entry_price:.4f}"
                metadata["reference_price"] = f"{entry_price:.4f}"
        elif self._reclaim_mode:
            st = self._states.get(symbol)
            emit_ms = st.reclaim_emit_ms if st is not None else None
            pct = str(self.settings.orb_reclaim_trail_pct)
            qty = int(self.settings.orb_reclaim_quantity)
            metadata = {
                "stop_guard_enabled": "true",
                "stop_loss_pct": pct,   # initial stop = trail% below entry
                "trail_pct": pct,       # ratchet — drives the OMS trailing stop (#340)
                "stop_guard_quote_max_age_ms": "2000",
                "stop_guard_initial_panic_buffer_pct": "1.5",
                "orb_entry": "true",
                "execution_mode": "intrabar_reclaim",
                # RESTING LIMIT at OR_high — the entry mechanism under test.
                "order_type": "limit",
                "limit_price": f"{entry_price:.4f}",
                "reference_price": f"{entry_price:.4f}",
                # fill instrumentation: intended price + reclaim-confirm time, so
                # slippage (actual fill - OR_high) and time-to-fill are recoverable.
                "orb_intended_or_high": f"{entry_price:.4f}",
                "orb_reclaim_emit_ms": str(emit_ms) if emit_ms is not None else "",
            }
        else:
            pct = str(self.settings.orb_trail_pct)
            qty = int(self.settings.orb_quantity)
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
                quantity=Decimal(str(qty)),
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
            trail = (
                self.settings.orb_reclaim_trail_pct
                if self._reclaim_mode
                else self.settings.orb_trail_pct
            )
            logger.info(
                "[ORB-OPEN] %s entry=%.4f trail_pct=%s mode=%s",
                symbol, entry_price, trail,
                "intrabar_reclaim" if self._reclaim_mode else self._mode.value,
            )

    # ----- observability: isolated heartbeat (dashboard renders ORB from this stream) -----
    def _build_heartbeat_payload(self) -> StrategyBotStatePayload:
        decisions: list[dict] = []
        bar_counts: dict[str, int] = {}
        last_tick: dict[str, str] = {}
        positions: list[dict] = []
        for sym, st in sorted(self._states.items()):
            bar_counts[sym] = len(st.or_bars)
            if st.last_bar_at:
                last_tick[sym] = st.last_bar_at
            if st.traded:
                status = "entered"
            elif self._running_high_mode:
                status = "watching" if st.running_high is not None else "building_or"
            elif not st.or_evaluated:
                status = "building_or"
            elif st.opening_range is None:
                status = "skipped"  # not in pre-09:25 universe / width-capped / no coverage
            else:
                status = "armed"
            row: dict = {"ticker": sym, "status": status}
            if st.opening_range is not None:
                row["or_high"] = st.opening_range.high
                row["or_low"] = st.opening_range.low
                row["or_width_pct"] = round(st.opening_range.width_pct, 2)
            decisions.append(row)
            if st.traded and st.entry_price is not None:
                trail = (
                    float(self.settings.orb_reclaim_trail_pct)
                    if self._reclaim_mode
                    else float(self.settings.orb_trail_pct)
                )
                positions.append({
                    "symbol": sym,
                    "entry_price": st.entry_price,
                    "stop_loss_pct": trail,
                    "trail_pct": trail,
                    # the OMS owns the live trailing stop (8% legacy / 3% reclaim test)
                    "exit_owner": f"oms_trail{int(trail)}",
                })
        return StrategyBotStatePayload(
            strategy_code=SERVICE_NAME,
            account_name=str(self.settings.orb_broker_account_name),
            watchlist=sorted(self._universe),
            data_health={"status": "healthy", "universe_size": len(self._universe)},
            recent_decisions=decisions,
            positions=positions,
            bar_counts=bar_counts,
            last_tick_at=last_tick,
        )

    async def _publish_heartbeat(self) -> None:
        event = IsolatedBotStateEvent(
            source_service=SERVICE_NAME, payload=self._build_heartbeat_payload()
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "strategy-state-isolated"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_strategy_state_isolated_stream_maxlen,
            approximate=True,
        )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await OrbService().run()


def run() -> None:
    asyncio.run(main())
