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

from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Strategy
from project_mai_tai.db.session import build_timed_session_factory
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
from project_mai_tai.strategy_core.orb_tick_entry import OrbTickEntry
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
    # 2026-06-30 phantom-fix: entry state reflects CONFIRMED FILLS, not emits.
    #   attempts   = entry tries this window (cap 2 = original + reclaim, then suppressed),
    #   pending    = an emit is in flight awaiting the OMS fill/abandon outcome,
    #   traded     = holding a confirmed fill,
    #   entry_price= the REAL fill price, set on the fill event (NOT on emit).
    # An OMS-abandoned try leaves NO phantom + re-enters (until the cap), instead of the
    # old traded=True-on-emit that suppressed re-entry + showed a phantom vs a flat broker.
    # Reconciled by _reconcile_orders off broker_order_events (DB); held_qty mirrors traded.
    attempts: int = 0
    pending: bool = False
    traded: bool = False
    entry_price: float | None = None
    # held_qty tracks the REAL position from broker fills (buy adds, sell reduces). traded
    # mirrors held_qty>0 so it clears on a flat exit -> a re-break can reclaim (the CANF case).
    held_qty: float = 0.0
    # when the current pending emit went out — used to time out a stuck pending if the OMS
    # abandons pre-order (e.g. ASK_PAST_GAP_CAP) and emits NO terminal broker_order_events row.
    pending_since: datetime | None = None
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
    _tick_entry_mode: bool = False          # ORB tick-driven entry V1 (default off = byte-identical)
    _tick_gap_cap_pct: float = 1.5
    _tick_window_min: int = 30
    _tick_atr_gate_pct: float = 4.3
    _tick_gate_after_secs: float = 0.0
    # Market-data consume-loop throughput (mirrors strategy-engine #175/#179). The open
    # burst spans the WHOLE scanner universe and exceeded 700 ticks/s on 2026-06-30; a
    # single count=500 xread per 1s loop fell ~3x behind (effective ~196/s), surfacing the
    # 09:30 bar + its entry ~1:47 late. Drain-to-budget + non-blocking follow-up passes +
    # universe-DB-read off the hot path keep ORB caught up through the open.
    _MARKET_DATA_XREAD_COUNT: int = 1000
    _MARKET_DATA_DRAIN_BUDGET: int = 20_000
    _UNIVERSE_REFRESH_SECS: float = 5.0
    _HEARTBEAT_SECS: float = 5.0
    # Max entry tries per symbol per 09:30-10:00 window (original break + one reclaim),
    # then suppressed — whether they filled or abandoned. The same fill-counted state the
    # future bracket's 2-entry cap will key on.
    _ENTRY_ATTEMPT_CAP: int = 2
    # Clear a stuck pending if no terminal broker_order_events row arrives within this window.
    # Some OMS abandons (ASK_PAST_GAP_CAP pre-order) emit NO DB row, so without this a
    # pending emit would block re-entry forever. Set above oms_intent_max_age_seconds (30).
    _PENDING_ABANDON_TIMEOUT_SECS: float = 45.0

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
        # Order reconcile reads broker_order_events (DB) — the redis order-events stream is
        # unfed in prod. Cursor starts at boot so we only process events from now forward.
        self._oe_cursor: datetime = datetime.now(UTC)
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
        # ORB tick-driven entry V1 (docs/orb-tick-exit-design.md). Enter on the break TICK via the
        # shared OrbTickEntry leaf, gated to high-ATR names, 2% OMS trail, high-ATR up-sized. Takes
        # precedence over the bar-close paths in add_tick when enabled. Default False -> byte-identical
        # (no engine created, no tick path). Mutually exclusive with reclaim.
        self._tick_entry_mode = bool(
            getattr(self.settings, "orb_tick_entry_enabled", False)
        ) and not self._reclaim_mode
        self._tick_gap_cap_pct = float(getattr(self.settings, "orb_tick_entry_gap_cap_pct", 1.5))
        self._tick_window_min = int(getattr(self.settings, "orb_tick_entry_window_minutes", 30))
        self._tick_atr_gate_pct = float(getattr(self.settings, "orb_tick_entry_atr_gate_pct", 4.3))
        self._tick_gate_after_secs = float(getattr(self.settings, "orb_tick_entry_gate_after_minutes", 0.0)) * 60.0
        self._tick_engines: dict[str, OrbTickEntry] = {}
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
            self.session_factory = build_timed_session_factory(self.settings, service="orb", profile="fast")
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
                await self._reconcile_orders()  # DB reconcile: fills/exits/abandons (phantom-fix)
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
        self._oe_cursor = datetime.now(UTC)  # don't replay yesterday's order events into fresh state
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
            anchor = (
                self._observe_open_utc()
                if (self._running_high_mode or self._tick_entry_mode)
                else self._session_open_utc()
            )
            agg = OrbTickAggregator(session_open=anchor)
            self._aggregators[symbol] = agg
        bar = agg.add_tick(ts, price, size)
        if self._tick_entry_mode:
            self._check_tick_entry(symbol, price, ts, bar)   # tick-driven entry (bar feeds the ATR gate)
        elif bar is not None:
            self._on_bar(symbol, bar)
        if self._reclaim_mode:
            self._check_reclaim(symbol, price, ts)

    # ----- entry-state gate + fill/abandon reconciliation (2026-06-30 phantom-fix) -----
    def _can_enter(self, st: _SymbolState) -> bool:
        """May we emit an entry for this symbol now? No emit while one is in flight
        (``pending``) or while holding a confirmed fill (``traded``), and only up to the
        per-symbol per-window attempt cap (original + reclaim)."""
        return not st.pending and not st.traded and st.attempts < self._ENTRY_ATTEMPT_CAP

    async def _reconcile_orders(self) -> None:
        """Reconcile ORB's per-symbol entry state against CONFIRMED broker outcomes recorded
        in ``broker_order_events`` (the DB path — the redis order-events stream is unfed in
        prod, so #388's stream consumer was DOA). Tracks held qty across BUY (open) and SELL
        (close) fills so ``traded`` mirrors a REAL position and CLEARS on a flat exit, which is
        what re-enables a reclaim after a filled-then-exited entry (the CANF case). Abandons
        clear ``pending`` without a fill. ``attempts`` is NEVER touched here — only ORB emits
        burn attempts — so OMS quote-drift-cancel churn cannot exhaust the cap. Sparse; polled
        off the hot path (sync DB read, mirrors the universe-snapshot read)."""
        if self.session_factory is None:
            return
        try:
            rows = self._fetch_order_events_since(self._oe_cursor)
        except Exception:
            logger.exception("[ORB] order-event reconcile query failed")
            rows = []
        for event_at, event_type, symbol, side, quantity, payload in rows:
            self._oe_cursor = event_at
            self._apply_order_event(
                symbol=str(symbol or "").upper(),
                side=str(side or ""),
                event_type=str(event_type or ""),
                quantity=float(quantity or 0.0),
                payload=payload if isinstance(payload, dict) else {},
            )
        self._expire_stale_pending()

    def _fetch_order_events_since(self, cursor: datetime) -> list:
        """ORB order-events after ``cursor``, joined to broker_orders for symbol/side/qty."""
        with self.session_factory() as session:
            return session.execute(
                select(
                    BrokerOrderEvent.event_at,
                    BrokerOrderEvent.event_type,
                    BrokerOrder.symbol,
                    BrokerOrder.side,
                    BrokerOrder.quantity,
                    BrokerOrderEvent.payload,
                )
                .join(BrokerOrder, BrokerOrder.id == BrokerOrderEvent.order_id)
                .join(Strategy, Strategy.id == BrokerOrder.strategy_id)
                .where(Strategy.code == SERVICE_NAME)
                .where(BrokerOrderEvent.event_at > cursor)
                .order_by(BrokerOrderEvent.event_at)
            ).all()

    def _apply_order_event(
        self, *, symbol: str, side: str, event_type: str, quantity: float, payload: dict
    ) -> None:
        st = self._states.get(symbol)
        if st is None:
            return  # symbol not tracked this session (e.g. cleared post day-roll)
        is_open = side.lower() == "buy"   # ORB is long-only: buy=entry/open, sell=exit/close
        filled = event_type in ("filled", "partially_filled")
        abandoned = event_type in ("rejected", "cancelled")
        if is_open and filled:
            st.held_qty += quantity
            st.traded = True                     # confirmed fill -> holding (OMS owns the exit)
            st.pending = False
            st.pending_since = None
            fill_px = self._fill_price_from_payload(payload)
            if fill_px is not None:
                st.entry_price = fill_px
            logger.info("[ORB-ENTRY-FILLED] %s qty=%.0f held=%.0f entry=%s attempt=%d/%d",
                        symbol, quantity, st.held_qty, st.entry_price,
                        st.attempts, self._ENTRY_ATTEMPT_CAP)
        elif is_open and abandoned:
            # NOT a fill -> clear the (never-held) pending; re-enterable until the cap.
            st.pending = False
            st.pending_since = None
            reason = (payload.get("metadata") or {}).get("abandon_reason_code") or payload.get("reason") or event_type
            logger.info("[ORB-ENTRY-RESET] %s reason=%s attempt=%d/%d -> %s",
                        symbol, reason, st.attempts, self._ENTRY_ATTEMPT_CAP,
                        "re-enterable" if st.attempts < self._ENTRY_ATTEMPT_CAP else "suppressed(cap)")
        elif (not is_open) and filled:
            # exit fill -> reduce held; when flat, clear traded so a re-break can RECLAIM.
            st.held_qty = max(0.0, st.held_qty - quantity)
            if st.held_qty <= 1e-9:
                st.traded = False
                st.entry_price = None
                logger.info("[ORB-POSITION-FLAT] %s exited; re-enterable=%s attempt=%d/%d",
                            symbol, st.attempts < self._ENTRY_ATTEMPT_CAP,
                            st.attempts, self._ENTRY_ATTEMPT_CAP)
        # 'accepted' / a rejected EXIT / anything else -> no entry-state change (conservative)

    def _expire_stale_pending(self) -> None:
        """Clear a pending emit that never got a terminal broker_order_events row (some OMS
        abandons, e.g. ASK_PAST_GAP_CAP pre-order, emit none). Without this, that symbol would
        block re-entry for the rest of the window. Re-enterable until the attempt cap."""
        now = datetime.now(UTC)
        for sym, st in self._states.items():
            if (
                st.pending
                and st.pending_since is not None
                and (now - st.pending_since).total_seconds() > self._PENDING_ABANDON_TIMEOUT_SECS
            ):
                st.pending = False
                st.pending_since = None
                logger.info("[ORB-ENTRY-RESET] %s reason=pending_timeout attempt=%d/%d -> %s",
                            sym, st.attempts, self._ENTRY_ATTEMPT_CAP,
                            "re-enterable" if st.attempts < self._ENTRY_ATTEMPT_CAP else "suppressed(cap)")

    @staticmethod
    def _fill_price_from_payload(payload: dict) -> float | None:
        """Best-effort entry price for display (the OMS owns the real position/exit)."""
        meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        for src in (payload, meta):
            for key in ("fill_price", "limit_price", "reference_price"):
                v = src.get(key)
                if v not in (None, ""):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
        return None

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
            self._can_enter(st)
            and symbol in self._universe
            and open_utc <= bar.timestamp <= cutoff
            and bar.high > st.running_high
        ):
            level = st.running_high
            fill = level if bar.open <= level else bar.open   # gap-up fills at the open
            if fill <= level * (1.0 + self._rh_gap_cap_pct / 100.0):
                st.pending = True              # emit in flight; confirmed on the fill event
                st.attempts += 1
                self._pending_intents.append((symbol, fill))
                logger.info(
                    "[ORB-RH-ENTRY] %s entry=%.4f broke_high=%.4f gap=%.2f%% attempt=%d/%d",
                    symbol, fill, level, (fill / level - 1.0) * 100.0,
                    st.attempts, self._ENTRY_ATTEMPT_CAP,
                )
        st.running_high = max(st.running_high, bar.high)

    def _check_tick_entry(self, symbol: str, price: float, ts: datetime, completed_bar: OrbBar | None) -> None:
        """Tick-driven ORB entry V1 (docs/orb-tick-exit-design.md). The shared OrbTickEntry leaf keeps a
        CONTINUOUS running-high and returns the broken level on a break TICK inside (09:30, cutoff] that
        passes the causal high-ATR gate (period-5 ATR% over the ORB-window bars so far). On a gated
        break we emit ONE quote-priced open intent (2% OMS trail, high-ATR up-sized); the OMS re-prices
        off its live ask bounded by orb_intended_break_level + gap_cap. Exit = the OMS 2% trailing stop.
        During a hold `_can_enter` gates re-entry, while the engine keeps advancing the running-high so a
        re-entry needs a genuinely higher high (mirrors the validated backtest)."""
        eng = self._tick_engines.get(symbol)
        if eng is None:
            open_utc = self._session_open_utc()
            eng = OrbTickEntry(
                observe_open=self._observe_open_utc(),
                session_open=open_utc,
                cutoff=open_utc + timedelta(minutes=self._tick_window_min),
                atr_gate_pct=self._tick_atr_gate_pct,
                gate_after_secs=self._tick_gate_after_secs,
            )
            self._tick_engines[symbol] = eng
        if completed_bar is not None:
            eng.observe_bar(completed_bar)          # causal ATR gate sees only bars closed before this tick
        level = eng.observe_tick(ts, price)
        if level is None:
            return                                  # no break, or the high-ATR gate rejected it
        st = self._states.setdefault(symbol, _SymbolState())
        st.last_bar_at = ts.isoformat()
        if not self._can_enter(st) or symbol not in self._universe:
            return
        # coarse gap pre-check on the crossing tick (OMS does the real gap check off the live ask)
        if price > level * (1.0 + self._tick_gap_cap_pct / 100.0):
            return
        st.pending = True                           # emit in flight; confirmed on the fill event
        st.attempts += 1
        self._pending_intents.append((symbol, level))
        logger.info(
            "[ORB-TICK-ENTRY] %s broke_high=%.4f at=%.4f gate_atr_ge=%.2f%% attempt=%d/%d",
            symbol, level, price, self._tick_atr_gate_pct, st.attempts, self._ENTRY_ATTEMPT_CAP,
        )

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
        if st.opening_range is None or not self._can_enter(st) or bar.timestamp > cutoff:
            return
        if bar_confirms_breakout(st.opening_range, bar, self._cfg):
            entry = entry_fill_price(st.opening_range, bar, self._mode)
            st.pending = True  # emit in flight; confirmed on the fill event
            st.attempts += 1
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
                st.pending = True  # emit in flight; confirmed on the fill event
                st.attempts += 1
                st.reclaim_emit_ms = ts_ms  # fill-instrumentation: confirm time
                self._pending_intents.append((symbol, or_high))
                logger.info(
                    "[ORB-RECLAIM-ENTRY] %s intended=%.4f held=%.0fs",
                    symbol, or_high, self._reclaim_hold_ms / 1000,
                )
        else:
            st.reclaim_cross_ms = None  # hold broke — wait for the next reclaim

    def _build_open_intent(self, symbol: str, entry_price: float) -> TradeIntentEvent:
        if self._tick_entry_mode:
            # Tick-driven V1: 2% OMS trail, high-ATR up-sized, quote-priced fail-closed (the OMS
            # re-prices off its live ask bounded by orb_intended_break_level + gap_cap). entry_price
            # is the broken running-high level (the intended break). docs/orb-tick-exit-design.md.
            pct = str(self.settings.orb_tick_entry_trail_pct)
            qty = int(self.settings.orb_tick_entry_quantity)
            metadata = {
                "stop_guard_enabled": "true",
                "stop_loss_pct": pct,
                "trail_pct": pct,                 # OMS 2% trailing stop (#340)
                "stop_guard_quote_max_age_ms": "2000",
                "stop_guard_initial_panic_buffer_pct": "1.5",
                "orb_entry": "true",
                "execution_mode": "tick_entry_breakout",
                "order_type": "limit",
                "orb_intended_break_level": f"{entry_price:.4f}",
                "price_source": "ask",
                "orb_gap_cap_pct": f"{self._tick_gap_cap_pct}",
            }
        elif self._running_high_mode:
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
            st = self._states.get(symbol)
            if st is not None:
                st.pending_since = datetime.now(UTC)  # start the stuck-pending timeout clock
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
