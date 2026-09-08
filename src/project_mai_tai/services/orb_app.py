"""Broker-disconnected ORB live-paper observer.

Runs as its OWN process/event loop (escapes the shared strategy-engine 1 Hz-loop
contention by construction) and consumes the EXISTING market-data gateway as a
registered consumer (no new Schwab streamer session, no credential collision).

Loop: read the pre-09:25 confirmed universe (the binding rule) → register those
symbols as a gateway consumer → drain trade and quote ticks → aggregate trade ticks
to 1-min bars → model one resting order at the 09:25-09:29 high → observe an intrabar
fill or finalize the fixed 09:25-09:30 high → append every decision to the evidence tape.

The service cannot construct a trade intent or import broker routing. The OMS also
refuses any forged ORB intent before persistence, giving the paper boundary two
independent enforcement points.

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
from project_mai_tai.db.session import build_timed_session_factory
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    IsolatedBotStateEvent,
    MarketDataSubscriptionEvent,
    MarketDataSubscriptionPayload,
    StrategyBotStatePayload,
    stream_name,
)
from project_mai_tai.orb_paper_store import (
    ORB_PAPER_ACCOUNT_NAME,
    ORB_PAPER_EVENT_TYPE,
    ORB_PAPER_LEVEL_FINALIZED_EVENT_TYPE,
    ORB_PAPER_ORDER_ADJUSTED_EVENT_TYPE,
    ORB_PAPER_ORDER_PLACED_EVENT_TYPE,
    ORB_PAPER_ORDER_UNANSWERABLE_EVENT_TYPE,
    OrbPaperDecision,
    OrbPaperStore,
)
from project_mai_tai.settings import Settings
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
class _ModeledRestingOrder:
    """One paper-only resting order; no object here can address a broker."""

    order_id: str
    placed_at: datetime
    initial_level: float
    current_level: float
    source_minutes: tuple[str, ...]
    final_level: float | None = None
    finalized_at: datetime | None = None
    adjusted_at: datetime | None = None
    adjustment_outcome: str = "NOT_EVALUATED"
    filled_at: datetime | None = None
    fill_price: float | None = None
    decision_blocked: bool = False


@dataclass
class _SymbolState:
    or_bars: list[OrbBar] = field(default_factory=list)
    or_evaluated: bool = False
    opening_range: OpeningRange | None = None
    # Entry observations retain the strategy's existing two-attempt cap. ``pending`` is
    # true only while the durable paper row is being written; it is never a broker order.
    attempts: int = 0
    pending: bool = False
    paper_entries: int = 0
    last_paper_entry_price: float | None = None
    last_bar_at: str = ""
    # intrabar-reclaim mode only: start (ms UTC) of the current uninterrupted hold
    # above OR_high; reset to None whenever a tick prints back below OR_high.
    reclaim_cross_ms: int | None = None
    # ms UTC of the reclaim confirmation, retained as decision provenance.
    reclaim_emit_ms: int | None = None
    # running-high mode only: highest 1-min bar-high seen since 09:25 (the breakout level).
    running_high: float | None = None
    # Fixed-resting paper mode: 09:25-09:30 bars and the single modeled order.
    resting_order: _ModeledRestingOrder | None = None
    latest_bid: float | None = None
    latest_ask: float | None = None
    latest_quote_at: datetime | None = None
    adjustment_opportunities: int = 0
    adjustment_unanswerable: int = 0


@dataclass(frozen=True)
class _PendingPaperEntry:
    symbol: str
    entry_price: float
    observed_at: datetime
    attempt: int
    event_type: str = ORB_PAPER_EVENT_TYPE
    detail: dict[str, object] = field(default_factory=dict)
    counts_as_entry: bool = True


class OrbService:
    # Class-level defaults so instances built via __new__ (some unit tests) read the
    # legacy path; __init__ overrides them from settings.
    _reclaim_mode: bool = False
    _reclaim_hold_ms: int = 25_000
    _running_high_mode: bool = False
    _resting_entry: bool = False
    _fixed_resting_mode: bool = False
    _mode: ExecutionMode = ExecutionMode.BAR_CLOSE
    # Market-data consume-loop throughput (mirrors strategy-engine #175/#179). The open
    # burst spans the WHOLE scanner universe and exceeded 700 ticks/s on 2026-06-30; a
    # single count=500 xread per 1s loop fell ~3x behind (effective ~196/s), surfacing the
    # 09:30 bar + its entry ~1:47 late. Drain-to-budget + non-blocking follow-up passes +
    # universe-DB-read off the hot path keep ORB caught up through the open.
    _MARKET_DATA_XREAD_COUNT: int = 1000
    _MARKET_DATA_DRAIN_BUDGET: int = 20_000
    _UNIVERSE_REFRESH_SECS: float = 5.0
    _HEARTBEAT_SECS: float = 5.0
    # Max entry observations per symbol per 09:30-10:00 window (original break + one reclaim).
    _ENTRY_ATTEMPT_CAP: int = 2
    def __init__(
        self,
        settings: Settings | None = None,
        redis_client: Redis | None = None,
        session_factory: sessionmaker[Session] | None = None,
        paper_store: OrbPaperStore | None = None,
    ) -> None:
        # Do not read a checkout-local .env. Production supplies the broker-free
        # systemd environment generated specifically for this process.
        self.settings = settings or Settings(_env_file=None)
        self.redis = redis_client or Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.session_factory = session_factory  # built lazily when enabled (no DB connect when off)
        self._aggregators: dict[str, OrbTickAggregator] = {}
        self._last_gateway_symbols: list[str] = []
        self._md_offset: str = "$"  # tail new ticks only
        self._states: dict[str, _SymbolState] = {}
        self._universe: set[str] = set()
        self._pending_paper_entries: list[_PendingPaperEntry] = []
        self.paper_store = paper_store or (
            OrbPaperStore(session_factory) if session_factory is not None else None
        )
        # Flag-gated intrabar-reclaim live test: cap-off + reclaim@OR_high + N% trail.
        # Default False -> every reclaim branch is skipped and ORB is byte-identical.
        self._reclaim_mode = bool(getattr(self.settings, "orb_intrabar_reclaim_enabled", False))
        self._reclaim_hold_ms = int(getattr(self.settings, "orb_reclaim_hold_secs", 25)) * 1000
        # The isolated paper observer uses running-high + resting together for the fixed
        # 09:25-09:30 model. Running-high alone retains the prior dynamic reference for rollback.
        self._running_high_mode = bool(
            getattr(self.settings, "orb_running_high_enabled", False)
        ) and not self._reclaim_mode
        self._rh_gap_cap_pct = float(getattr(self.settings, "orb_running_high_gap_cap_pct", 1.5))
        self._rh_window_min = int(getattr(self.settings, "orb_running_high_window_minutes", 30))
        # Preserve the selected historical pricing policy on the paper evidence row. It is
        # descriptive only: this service cannot publish an order or invoke an adapter.
        self._oms_quote_priced = bool(
            getattr(self.settings, "orb_oms_quote_priced_entry_enabled", False)
        )
        # Select the fixed-level, intrabar paper model. The isolated env forces this on;
        # it still cannot publish an order or invoke an adapter.
        self._resting_entry = bool(getattr(self.settings, "orb_resting_entry_enabled", False))
        self._fixed_resting_mode = self._running_high_mode and self._resting_entry
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
        # next session starts clean (running_high re-seeds from 09:25, decision state clears,
        # aggregators rebuild with the new session anchor). Without this, a bot left running
        # across midnight carries the prior day's state into the new session.
        self._session_date = datetime.now(_ET).date()

    # ----- lifecycle -----
    async def run(self) -> None:
        if not bool(getattr(self.settings, "orb_enabled", False)):
            logger.info("[ORB] disabled (orb_enabled=false); not starting")
            return
        if self.session_factory is None:
            self.session_factory = build_timed_session_factory(self.settings, service="orb", profile="fast")
        if self.paper_store is None:
            self.paper_store = OrbPaperStore(self.session_factory)
        logger.info("[ORB] starting — broker-disconnected paper observer, market-data gateway consumer")
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
                await self._record_pending_paper_entries()
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

    def _handle_quote_tick(self, obj: dict) -> None:
        payload = obj.get("payload") or {}
        symbol = str(payload.get("symbol", "")).upper()
        if not symbol or symbol not in self._last_gateway_symbols:
            return
        try:
            bid = float(payload["bid_price"])
            ask = float(payload["ask_price"])
        except (KeyError, TypeError, ValueError):
            return
        observed_at = self._event_time(obj)
        st = self._states.setdefault(symbol, _SymbolState())
        st.latest_bid = bid
        st.latest_ask = ask
        st.latest_quote_at = observed_at

        # A quote in the next minute is enough to close the current trade bar. This
        # lets the modeled placement/adjustment happen at the minute boundary rather
        # than waiting for the next trade print.
        agg = self._aggregators.get(symbol)
        if agg is None:
            return
        bar = agg.flush_before(observed_at)
        if bar is not None:
            self._on_bar(symbol, bar, observed_at=observed_at, observed_price=ask)
        self._finalize_fixed_resting_without_0930_bar(symbol, observed_at=observed_at)

    def _handle_market_data(self, fields: dict) -> None:
        raw = fields.get("data")
        if not raw:
            return
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return
        event_type = obj.get("event_type")
        if event_type == "quote_tick":
            if self._fixed_resting_mode:
                self._handle_quote_tick(obj)
            return
        if event_type != "trade_tick":
            return
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
            self._on_bar(symbol, bar, observed_at=ts, observed_price=price)
        if self._fixed_resting_mode:
            self._finalize_fixed_resting_without_0930_bar(symbol, observed_at=ts)
            self._check_fixed_resting_fill(symbol, price, ts)
        if self._reclaim_mode:
            self._check_reclaim(symbol, price, ts)

    # ----- entry-state gate -----
    def _can_enter(self, st: _SymbolState) -> bool:
        """May we record another paper entry decision for this symbol?"""
        return not st.pending and st.attempts < self._ENTRY_ATTEMPT_CAP

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
        breakout level, only if the observed price is within gap_cap% of the broken high. v1 = one
        entry per symbol (re-entry is a follow-up)."""
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
            self._can_enter(st)
            and symbol in self._universe
            and open_utc <= bar.timestamp <= cutoff
            and bar.high > st.running_high
        ):
            level = st.running_high
            fill = level if bar.open <= level else bar.open   # gap-up fills at the open
            if fill <= level * (1.0 + self._rh_gap_cap_pct / 100.0):
                st.pending = True  # durable paper write in flight
                st.attempts += 1
                self._pending_paper_entries.append(
                    _PendingPaperEntry(symbol, fill, datetime.now(UTC), st.attempts)
                )
                logger.info(
                    "[ORB-RH-ENTRY] %s entry=%.4f broke_high=%.4f gap=%.2f%% attempt=%d/%d",
                    symbol, fill, level, (fill / level - 1.0) * 100.0,
                    st.attempts, self._ENTRY_ATTEMPT_CAP,
                )
        st.running_high = max(st.running_high, bar.high)

    @staticmethod
    def _modeled_minute_end(bar: OrbBar) -> datetime:
        return bar.timestamp.replace(second=59, microsecond=0)

    @staticmethod
    def _fixed_resting_detail(
        order: _ModeledRestingOrder,
        *,
        check_kind: str,
        level_derivation: str,
        status: str,
        reason: str,
        quote_at: datetime | None = None,
        bid: float | None = None,
        ask: float | None = None,
        decision_observed_at: datetime | None = None,
    ) -> dict[str, object]:
        return {
            "reason": reason,
            "status": status,
            "check_kind": check_kind,
            "level_derivation": level_derivation,
            "level_window_et": "09:25-09:30 inclusive",
            "level_source_minutes": list(order.source_minutes),
            "modeled_order_id": order.order_id,
            "initial_order_level": f"{order.initial_level:.4f}",
            "final_opening_high": (
                f"{order.final_level:.4f}" if order.final_level is not None else None
            ),
            "current_order_level": f"{order.current_level:.4f}",
            "order_placed_at": order.placed_at.isoformat(),
            "adjusted": order.adjusted_at is not None,
            "adjusted_at": order.adjusted_at.isoformat() if order.adjusted_at else None,
            "adjustment_outcome": order.adjustment_outcome,
            "fill_assumption": (
                "MODELED_AT_RESTING_LEVEL_ON_FIRST_INTRABAR_TRADE_ABOVE_LEVEL; "
                "NO_BROKER_ORDER; SPREAD_NOT_CHARGED"
            ),
            "decision_observed_at": (
                decision_observed_at.isoformat() if decision_observed_at else None
            ),
            "quote_at": quote_at.isoformat() if quote_at else None,
            "bid": bid,
            "ask": ask,
        }

    def _queue_fixed_resting_event(
        self,
        symbol: str,
        *,
        price: float,
        observed_at: datetime,
        event_type: str,
        detail: dict[str, object],
        counts_as_entry: bool = False,
    ) -> None:
        self._pending_paper_entries.append(
            _PendingPaperEntry(
                symbol=symbol,
                entry_price=price,
                observed_at=observed_at,
                attempt=1,
                event_type=event_type,
                detail=detail,
                counts_as_entry=counts_as_entry,
            )
        )

    def _on_bar_fixed_resting(
        self,
        symbol: str,
        bar: OrbBar,
        *,
        observed_at: datetime | None,
        observed_price: float | None,
    ) -> None:
        """Model one fixed-level resting order without constructing an executable order."""
        observe_open = self._observe_open_utc()
        session_open = self._session_open_utc()
        if bar.timestamp < observe_open or bar.timestamp > session_open:
            return
        st = self._states.setdefault(symbol, _SymbolState())
        st.last_bar_at = bar.timestamp.isoformat()
        if all(existing.timestamp != bar.timestamp for existing in st.or_bars):
            st.or_bars.append(bar)

        initial_end = session_open - timedelta(minutes=1)
        if (
            bar.timestamp == initial_end
            and st.resting_order is None
            and not st.or_evaluated
            and symbol in self._universe
        ):
            st.or_evaluated = True
            expected = {observe_open + timedelta(minutes=offset) for offset in range(5)}
            by_minute = {item.timestamp: item for item in st.or_bars if item.timestamp in expected}
            placed_at = self._modeled_minute_end(bar)
            available_level = max((item.high for item in by_minute.values()), default=bar.high)
            source_minutes = tuple(sorted(item.astimezone(_ET).strftime("%H:%M") for item in by_minute))
            if set(by_minute) != expected:
                missing = sorted(
                    item.astimezone(_ET).strftime("%H:%M") for item in expected - set(by_minute)
                )
                self._queue_fixed_resting_event(
                    symbol,
                    price=available_level,
                    observed_at=placed_at,
                    event_type=ORB_PAPER_ORDER_UNANSWERABLE_EVENT_TYPE,
                    detail={
                        "reason": "FIXED_LEVEL_SOURCE_COVERAGE_INCOMPLETE",
                        "status": "UNANSWERABLE",
                        "check_kind": "day_gate",
                        "level_derivation": "MAX_1M_TRADE_HIGH_09:25_THROUGH_09:29_ET",
                        "level_window_et": "09:25-09:30 inclusive",
                        "level_source_minutes": list(source_minutes),
                        "missing_minutes": missing,
                        "order_placed_at": None,
                        "adjusted": False,
                        "fill_assumption": "NOT_APPLICABLE_NO_MODELED_ORDER",
                    },
                )
                logger.warning(
                    "[ORB-PAPER-ORDER-UNANSWERABLE] %s missing_minutes=%s check=day_gate",
                    symbol,
                    ",".join(missing),
                )
                return
            order = _ModeledRestingOrder(
                order_id=f"orb-paper-order:{placed_at.astimezone(_ET).date().isoformat()}:{symbol}",
                placed_at=placed_at,
                initial_level=available_level,
                current_level=available_level,
                source_minutes=source_minutes,
            )
            st.resting_order = order
            st.running_high = available_level
            st.attempts = 1
            self._queue_fixed_resting_event(
                symbol,
                price=available_level,
                observed_at=placed_at,
                event_type=ORB_PAPER_ORDER_PLACED_EVENT_TYPE,
                detail=self._fixed_resting_detail(
                    order,
                    check_kind="day_gate",
                    level_derivation="MAX_1M_TRADE_HIGH_09:25_THROUGH_09:29_ET",
                    status="MODELED_RESTING",
                    reason="FIXED_RESTING_ORDER_PLACED",
                    decision_observed_at=observed_at,
                ),
            )
            logger.info(
                "[ORB-PAPER-ORDER-PLACED] %s level=%.4f placed_at=%s "
                "derivation=09:25-09:29 check=day_gate",
                symbol,
                available_level,
                placed_at.isoformat(),
            )
            return

        if bar.timestamp != session_open or st.resting_order is None:
            return
        order = st.resting_order
        finalized_at = self._modeled_minute_end(bar)
        order.finalized_at = finalized_at
        order.final_level = max(order.initial_level, bar.high)
        st.running_high = order.final_level
        level_rose = order.final_level > order.initial_level
        if level_rose:
            st.adjustment_opportunities += 1

        common = {
            "quote_at": st.latest_quote_at,
            "bid": st.latest_bid,
            "ask": st.latest_ask,
        }
        if order.filled_at is not None:
            order.adjustment_outcome = "NOT_APPLICABLE_FILLED_BEFORE_09:30_CLOSE"
            self._queue_fixed_resting_event(
                symbol,
                price=order.final_level,
                observed_at=finalized_at,
                event_type=ORB_PAPER_LEVEL_FINALIZED_EVENT_TYPE,
                detail=self._fixed_resting_detail(
                    order,
                    check_kind="post-fill",
                    level_derivation="MAX_1M_TRADE_HIGH_09:25_THROUGH_09:30_ET_INCLUSIVE",
                    status="FINALIZED_NO_ADJUSTMENT",
                    reason="ORDER_ALREADY_FILLED_AT_09:29_LEVEL",
                    decision_observed_at=observed_at,
                    **common,
                ),
            )
            return
        if not level_rose:
            order.adjustment_outcome = "NOT_NEEDED_09:30_HIGH_DID_NOT_RISE"
            self._queue_fixed_resting_event(
                symbol,
                price=order.current_level,
                observed_at=finalized_at,
                event_type=ORB_PAPER_LEVEL_FINALIZED_EVENT_TYPE,
                detail=self._fixed_resting_detail(
                    order,
                    check_kind="live",
                    level_derivation="MAX_1M_TRADE_HIGH_09:25_THROUGH_09:30_ET_INCLUSIVE",
                    status="FINALIZED_NO_ADJUSTMENT",
                    reason="09:30_HIGH_DID_NOT_RAISE_LEVEL",
                    decision_observed_at=observed_at,
                    **common,
                ),
            )
            return

        first_post_close_quote = st.latest_quote_at is not None and st.latest_quote_at > finalized_at
        market_below_new_level = (
            observed_price is not None
            and observed_price < order.final_level
            and st.latest_ask is not None
            and st.latest_ask < order.final_level
        )
        if first_post_close_quote and market_below_new_level:
            order.current_level = order.final_level
            order.adjusted_at = finalized_at
            order.adjustment_outcome = "MODELED_ADJUSTMENT_LANDED"
            self._queue_fixed_resting_event(
                symbol,
                price=order.current_level,
                observed_at=finalized_at,
                event_type=ORB_PAPER_ORDER_ADJUSTED_EVENT_TYPE,
                detail=self._fixed_resting_detail(
                    order,
                    check_kind="live",
                    level_derivation="MAX_1M_TRADE_HIGH_09:25_THROUGH_09:30_ET_INCLUSIVE",
                    status="MODELED_ADJUSTED",
                    reason="09:30_HIGH_RAISED_LEVEL",
                    decision_observed_at=observed_at,
                    **common,
                ),
            )
            logger.info(
                "[ORB-PAPER-ORDER-ADJUSTED] %s old=%.4f new=%.4f adjusted_at=%s check=live",
                symbol,
                order.initial_level,
                order.current_level,
                finalized_at.isoformat(),
            )
            return

        order.adjustment_outcome = "UNANSWERABLE_ADJUSTMENT_TIMING"
        order.decision_blocked = True
        st.adjustment_unanswerable += 1
        self._queue_fixed_resting_event(
            symbol,
            price=order.current_level,
            observed_at=observed_at or finalized_at,
            event_type=ORB_PAPER_ORDER_UNANSWERABLE_EVENT_TYPE,
            detail=self._fixed_resting_detail(
                order,
                check_kind="live",
                level_derivation="MAX_1M_TRADE_HIGH_09:25_THROUGH_09:30_ET_INCLUSIVE",
                status="UNANSWERABLE",
                reason="HIGHER_09:30_HIGH_BUT_ADJUSTMENT_NOT_PROVEN_IN_TIME",
                decision_observed_at=observed_at,
                **common,
            ),
        )
        logger.warning(
            "[ORB-PAPER-ADJUSTMENT-UNANSWERABLE] %s old=%.4f new=%.4f "
            "observed_price=%s quote_at=%s denominator=adjustment_opportunities",
            symbol,
            order.initial_level,
            order.final_level,
            observed_price,
            st.latest_quote_at.isoformat() if st.latest_quote_at else "missing",
        )

    def _finalize_fixed_resting_without_0930_bar(
        self,
        symbol: str,
        *,
        observed_at: datetime,
    ) -> None:
        """Close the level window when 09:30 had no trade bar to contribute."""
        st = self._states.get(symbol)
        order = st.resting_order if st is not None else None
        finalized_at = self._session_open_utc() + timedelta(seconds=59)
        if (
            st is None
            or order is None
            or order.final_level is not None
            or observed_at <= finalized_at
        ):
            return
        order.final_level = order.initial_level
        order.finalized_at = finalized_at
        order.adjustment_outcome = "NOT_NEEDED_NO_09:30_TRADE_HIGH"
        self._queue_fixed_resting_event(
            symbol,
            price=order.current_level,
            observed_at=finalized_at,
            event_type=ORB_PAPER_LEVEL_FINALIZED_EVENT_TYPE,
            detail=self._fixed_resting_detail(
                order,
                check_kind="live",
                level_derivation="MAX_1M_TRADE_HIGH_09:25_THROUGH_09:30_ET_INCLUSIVE",
                status="FINALIZED_NO_ADJUSTMENT",
                reason="NO_09:30_TRADE_BAR_LEVEL_UNCHANGED",
                quote_at=st.latest_quote_at,
                bid=st.latest_bid,
                ask=st.latest_ask,
                decision_observed_at=observed_at,
            ),
        )
        logger.info(
            "[ORB-PAPER-LEVEL-FINALIZED] %s level=%.4f reason=no-09:30-trade-bar check=live",
            symbol,
            order.current_level,
        )

    def _check_fixed_resting_fill(self, symbol: str, price: float, ts: datetime) -> None:
        st = self._states.get(symbol)
        order = st.resting_order if st is not None else None
        if (
            st is None
            or order is None
            or order.filled_at is not None
            or order.decision_blocked
            or st.pending
            or ts < order.placed_at
        ):
            return
        cutoff = self._session_open_utc() + timedelta(minutes=self._rh_window_min)
        if ts > cutoff or price <= order.current_level:
            return
        order.filled_at = ts
        order.fill_price = order.current_level
        st.pending = True
        self._queue_fixed_resting_event(
            symbol,
            price=order.current_level,
            observed_at=ts,
            event_type=ORB_PAPER_EVENT_TYPE,
            detail=self._fixed_resting_detail(
                order,
                check_kind="live",
                level_derivation=(
                    "MAX_1M_TRADE_HIGH_09:25_THROUGH_09:30_ET_INCLUSIVE"
                    if order.adjusted_at is not None
                    else "MAX_1M_TRADE_HIGH_09:25_THROUGH_09:29_ET"
                ),
                status="RECORDED_NOT_A_BROKER_FILL",
                reason="INTRABAR_BREAK_OF_MODELED_RESTING_LEVEL",
                quote_at=st.latest_quote_at,
                bid=st.latest_bid,
                ask=st.latest_ask,
                decision_observed_at=ts,
            ),
            counts_as_entry=True,
        )
        logger.info(
            "[ORB-PAPER-RESTING-FILL] %s modeled_fill=%.4f trade=%.4f at=%s "
            "adjusted=%s check=live assumption=resting-level",
            symbol,
            order.current_level,
            price,
            ts.isoformat(),
            order.adjusted_at is not None,
        )

    # ----- the entry brain: OR build -> breakout -> arm-on-window-open -> paper decision -----
    def _on_bar(
        self,
        symbol: str,
        bar: OrbBar,
        *,
        observed_at: datetime | None = None,
        observed_price: float | None = None,
    ) -> None:
        if self._fixed_resting_mode:
            self._on_bar_fixed_resting(
                symbol,
                bar,
                observed_at=observed_at,
                observed_price=observed_price,
            )
            return
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
        if st.opening_range is None or not self._can_enter(st) or bar.timestamp > cutoff:
            return
        if bar_confirms_breakout(st.opening_range, bar, self._cfg):
            entry = entry_fill_price(st.opening_range, bar, self._mode)
            st.pending = True  # durable paper write in flight
            st.attempts += 1
            self._pending_paper_entries.append(
                _PendingPaperEntry(symbol, entry, datetime.now(UTC), st.attempts)
            )
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
        record ONE paper decision at OR_high. A tick back below OR_high
        resets the timer (a pullback before the reclaim is fine — the sustained reclaim
        is the confirmation). Entries only in (OR-end, cutoff]."""
        st = self._states.get(symbol)
        if st is None or st.opening_range is None or not self._can_enter(st):
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
                st.pending = True  # durable paper write in flight
                st.attempts += 1
                st.reclaim_emit_ms = ts_ms  # decision provenance: confirmation time
                self._pending_paper_entries.append(
                    _PendingPaperEntry(symbol, or_high, ts, st.attempts)
                )
                logger.info(
                    "[ORB-RECLAIM-ENTRY] %s intended=%.4f held=%.0fs",
                    symbol, or_high, self._reclaim_hold_ms / 1000,
                )
        else:
            st.reclaim_cross_ms = None  # hold broke — wait for the next reclaim

    def _active_trail_pct(self) -> float:
        """Trailing-stop % the CURRENT entry mode arms — MUST mirror the pct
        ``_build_paper_entry_decision`` records (running-high + intrabar-reclaim use
        ``orb_reclaim_trail_pct``; the classic-OR path uses ``orb_trail_pct``). Single
        source for the ``[ORB-OPEN]`` log + heartbeat so the DISPLAY can never drift from
        the observed strategy decision. (Pre-2026-07-14 bug: the display keyed on
        ``_reclaim_mode`` only, so a running-high entry SHOWED ``orb_trail_pct`` while the
        recorded decision used ``orb_reclaim_trail_pct`` — an 8%-vs-3% mislabel.)"""
        if self._running_high_mode or self._reclaim_mode:
            return float(self.settings.orb_reclaim_trail_pct)
        return float(self.settings.orb_trail_pct)

    def _build_paper_entry_decision(
        self,
        symbol: str,
        entry_price: float,
        *,
        observed_at: datetime,
        attempt: int | None = None,
        event_type: str = ORB_PAPER_EVENT_TYPE,
        detail: dict[str, object] | None = None,
    ) -> OrbPaperDecision:
        if self._fixed_resting_mode:
            qty = int(self.settings.orb_reclaim_quantity)
            metadata = {
                "orb_entry": "true",
                "execution_mode": "fixed_opening_high_resting",
                "order_type": "MODELED_RESTING_ORDER",
                "broker_route": "none",
            }
        elif self._running_high_mode:
            pct = str(self.settings.orb_reclaim_trail_pct)   # 3% trail (shared setting)
            qty = int(self.settings.orb_reclaim_quantity)     # qty 5 (shared setting)
            metadata = {
                "stop_guard_enabled": "true",
                "stop_loss_pct": pct,
                "trail_pct": pct,
                "stop_guard_quote_max_age_ms": "2000",
                "stop_guard_initial_panic_buffer_pct": "1.5",
                "orb_entry": "true",
                "execution_mode": "running_high_breakout",
                "order_type": "limit",            # resting limit at the breakout level
                "orb_intended_break_level": f"{entry_price:.4f}",
            }
            if self._resting_entry:
                # Preserve the historical stop-limit parameters as decision evidence only.
                limit_cap = entry_price * (1.0 + self._rh_gap_cap_pct / 100.0)
                metadata["order_type"] = "STOP_LIMIT"
                metadata["stop_price"] = f"{entry_price:.4f}"
                metadata["limit_price"] = f"{limit_cap:.4f}"
                metadata["reference_price"] = f"{entry_price:.4f}"
            elif self._oms_quote_priced:
                # Retain the selected pricing policy as evidence without invoking it.
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
                "trail_pct": pct,
                "stop_guard_quote_max_age_ms": "2000",
                "stop_guard_initial_panic_buffer_pct": "1.5",
                "orb_entry": "true",
                "execution_mode": "intrabar_reclaim",
                # RESTING LIMIT at OR_high — the entry mechanism under test.
                "order_type": "limit",
                "limit_price": f"{entry_price:.4f}",
                "reference_price": f"{entry_price:.4f}",
                # Preserve intended price and confirmation time as decision provenance.
                "orb_intended_or_high": f"{entry_price:.4f}",
                "orb_reclaim_emit_ms": str(emit_ms) if emit_ms is not None else "",
            }
        else:
            pct = str(self.settings.orb_trail_pct)
            qty = int(self.settings.orb_quantity)
            metadata = {
                "stop_guard_enabled": "true",
                "stop_loss_pct": pct,   # initial stop = trail% below entry
                "trail_pct": pct,
                "stop_guard_quote_max_age_ms": "2000",
                "stop_guard_initial_panic_buffer_pct": "1.5",
                "orb_entry": "true",
                "execution_mode": self._mode.value,
            }
        st = self._states.get(symbol)
        decision_attempt = attempt if attempt is not None else (st.attempts if st is not None else 0)
        event_key = (
            f"orb-paper:{observed_at.astimezone(_ET).date().isoformat()}:{symbol}:"
            f"{event_type}:{decision_attempt}:{int(observed_at.timestamp() * 1_000_000)}"
        )
        decision_detail: dict[str, object] = {
            "reason": "ORB_OPEN",
            "classification": "SIMULATED_NO_REALISED_CONTROL_NOT_SIZE_QUALIFIED",
            "metadata": metadata,
        }
        if detail:
            decision_detail.update(detail)
        return OrbPaperDecision(
            event_key=event_key,
            session_date=observed_at.astimezone(_ET).date(),
            symbol=symbol,
            observed_at=observed_at,
            entry_price=Decimal(str(entry_price)),
            quantity=Decimal(str(qty)),
            attempt=decision_attempt,
            mode=(
                "fixed_opening_high_resting"
                if self._fixed_resting_mode
                else "running_high_breakout"
                if self._running_high_mode
                else "intrabar_reclaim"
                if self._reclaim_mode
                else self._mode.value
            ),
            detail=decision_detail,
            event_type=event_type,
        )

    @staticmethod
    def _require_paper_decision(decision: object) -> OrbPaperDecision:
        """Service-side refusal: only the non-order paper type may leave the entry brain."""
        if not isinstance(decision, OrbPaperDecision):
            logger.error(
                "[ORB-PAPER-REFUSED] service blocked non-paper output type=%s",
                type(decision).__name__,
            )
            raise RuntimeError("ORB is broker-disconnected; only OrbPaperDecision is accepted")
        return decision

    async def _record_pending_paper_entries(self) -> None:
        if not self._pending_paper_entries:
            return
        if self.paper_store is None:
            raise RuntimeError("ORB paper store is unavailable; refusing to discard a decision")
        pending, self._pending_paper_entries = self._pending_paper_entries, []
        for index, item in enumerate(pending):
            symbol = item.symbol
            entry_price = item.entry_price
            st = self._states.get(symbol)
            decision = self._require_paper_decision(
                self._build_paper_entry_decision(
                    symbol,
                    entry_price,
                    observed_at=item.observed_at,
                    attempt=item.attempt,
                    event_type=item.event_type,
                    detail=item.detail,
                )
            )
            try:
                self.paper_store.append(decision)
            except Exception:
                self._pending_paper_entries = pending[index:] + self._pending_paper_entries
                logger.exception(
                    "[ORB-PAPER-WRITE-FAILED] %s decision retained; no broker fallback",
                    symbol,
                )
                raise
            if st is not None and item.counts_as_entry:
                st.pending = False
                st.paper_entries += 1
                st.last_paper_entry_price = entry_price
            if item.counts_as_entry:
                if not self._fixed_resting_mode:
                    trail = self._active_trail_pct()
                    logger.info(
                        "[ORB-OPEN] %s entry=%.4f trail_pct=%s mode=%s",
                        symbol,
                        entry_price,
                        trail,
                        "intrabar_reclaim" if self._reclaim_mode else self._mode.value,
                    )
                logger.info(
                    "[ORB-PAPER-ENTRY] %s entry=%.4f attempt=%d event_key=%s "
                    "status=RECORDED_NOT_A_FILL",
                    symbol,
                    entry_price,
                    decision.attempt,
                    decision.event_key,
                )

    # ----- observability: service health plus isolated dashboard state -----
    def _build_heartbeat_payload(self) -> StrategyBotStatePayload:
        decisions: list[dict] = []
        bar_counts: dict[str, int] = {}
        last_tick: dict[str, str] = {}
        for sym, st in sorted(self._states.items()):
            bar_counts[sym] = len(st.or_bars)
            if st.last_bar_at:
                last_tick[sym] = st.last_bar_at
            if st.paper_entries:
                status = "paper_entry_recorded"
            elif self._fixed_resting_mode and st.resting_order is not None:
                if st.resting_order.decision_blocked:
                    status = "adjustment_unanswerable"
                elif st.resting_order.adjusted_at is not None:
                    status = "resting_adjusted"
                else:
                    status = "resting_at_initial_level"
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
            if st.resting_order is not None:
                order = st.resting_order
                row.update(
                    {
                        "initial_order_level": order.initial_level,
                        "final_opening_high": order.final_level,
                        "current_order_level": order.current_level,
                        "order_placed_at": order.placed_at.isoformat(),
                        "adjustment_outcome": order.adjustment_outcome,
                        "adjusted_at": order.adjusted_at.isoformat() if order.adjusted_at else None,
                        "modeled_fill_at": order.filled_at.isoformat() if order.filled_at else None,
                    }
                )
            decisions.append(row)
        adjustment_denominator = sum(st.adjustment_opportunities for st in self._states.values())
        adjustment_unanswerable = sum(st.adjustment_unanswerable for st in self._states.values())
        adjustments_landed = sum(
            1
            for st in self._states.values()
            if st.resting_order is not None
            and st.resting_order.adjustment_outcome == "MODELED_ADJUSTMENT_LANDED"
        )
        filled_before_adjustment = sum(
            1
            for st in self._states.values()
            if st.resting_order is not None
            and st.resting_order.adjustment_outcome
            == "NOT_APPLICABLE_FILLED_BEFORE_09:30_CLOSE"
            and st.resting_order.final_level is not None
            and st.resting_order.final_level > st.resting_order.initial_level
        )
        return StrategyBotStatePayload(
            strategy_code=SERVICE_NAME,
            account_name=ORB_PAPER_ACCOUNT_NAME,
            watchlist=sorted(self._universe),
            data_health={
                "status": "healthy",
                "universe_size": len(self._universe),
                "execution_mode": "paper",
                "broker_route": "none",
                "entry_model": (
                    "fixed_opening_high_resting"
                    if self._fixed_resting_mode
                    else "running_high_breakout"
                    if self._running_high_mode
                    else self._mode.value
                ),
                "resting_adjustment_timing": {
                    "status": "MEASURED" if adjustment_denominator else "UNEXERCISED",
                    "filled_before_adjustment": filled_before_adjustment,
                    "modeled_adjustments_landed": adjustments_landed,
                    "unanswerable": adjustment_unanswerable,
                    "denominator": adjustment_denominator,
                },
            },
            recent_decisions=decisions,
            positions=[],
            bar_counts=bar_counts,
            last_tick_at=last_tick,
        )

    async def _publish_heartbeat(self) -> None:
        heartbeat = HeartbeatEvent(
            source_service=SERVICE_NAME,
            payload=HeartbeatPayload(
                service_name=SERVICE_NAME,
                instance_name=SERVICE_NAME,
                status="healthy",
                details={
                    "execution_mode": "paper",
                    "broker_route": "none",
                    "universe_size": str(len(self._universe)),
                    "entry_model": (
                        "fixed_opening_high_resting"
                        if self._fixed_resting_mode
                        else "running_high_breakout"
                        if self._running_high_mode
                        else self._mode.value
                    ),
                },
            ),
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "heartbeats"),
            {"data": heartbeat.model_dump_json()},
            maxlen=self.settings.redis_heartbeat_stream_maxlen,
            approximate=True,
        )

        state = IsolatedBotStateEvent(
            source_service=SERVICE_NAME, payload=self._build_heartbeat_payload()
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "strategy-state-isolated"),
            {"data": state.model_dump_json()},
            maxlen=self.settings.redis_strategy_state_isolated_stream_maxlen,
            approximate=True,
        )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await OrbService().run()


def run() -> None:
    asyncio.run(main())
