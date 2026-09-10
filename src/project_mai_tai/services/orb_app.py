"""Broker-disconnected ORB live-paper observer.

Runs as its OWN process/event loop (escapes the shared strategy-engine 1 Hz-loop
contention by construction) and consumes the EXISTING market-data gateway as a
registered consumer (no new Schwab streamer session, no credential collision).

Loop: read the pre-09:25 confirmed universe (the binding rule) → register those
symbols as a gateway consumer → drain trade and quote ticks → aggregate trade ticks
to 1-min bars → model one resting order at the 09:25-09:29 high → observe an intrabar
fill or finalize the fixed 09:25-09:30 high → maintain a durable paper position →
model the settled exits at executable bid → report same-day P&L on the dashboard.

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
    ORB_PAPER_ATR_BAR_EVENT_TYPE,
    ORB_PAPER_ENTRY_GATE_EVENT_TYPE,
    ORB_PAPER_EVENT_TYPE,
    ORB_PAPER_EXIT_EVENT_TYPE,
    ORB_PAPER_LEVEL_FINALIZED_EVENT_TYPE,
    ORB_PAPER_ORDER_ADJUSTED_EVENT_TYPE,
    ORB_PAPER_ORDER_PLACED_EVENT_TYPE,
    ORB_PAPER_ORDER_UNANSWERABLE_EVENT_TYPE,
    OrbPaperDecision,
    OrbPaperStore,
)
from project_mai_tai.orb_paper_lifecycle import (
    PAPER_ATR_FACTOR,
    PAPER_ATR_PERIOD,
    PAPER_LIFECYCLE_VERSION,
    OrbPaperPosition,
    compute_paper_atr_trail,
    forming_bar_body_pct,
    paper_atr_session_key,
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
    # ⛔⭐⭐ OPERATOR RULING 2026-09-09 — LEFT, not PULLED. This records that adjustment timing was
    # unanswerable; it does NOT gate the fill. It replaced `decision_blocked`, which did gate it.
    # #915 deliberately implemented neither default and blocked further modeled fills while the
    # question was open. The ruling: "place early so something is always working — pulling it
    # removes the very thing you placed early for, and you'd miss the trade for a timing detail."
    # The accepted cost is a fill at the 09:29 level instead of the final one: slightly cheaper,
    # slightly earlier — the trade already accepted by choosing to place at 09:29.
    adjustment_unanswerable: bool = False
    entry_gate_armed: bool = True
    entry_gate_reason: str = "RULES_DISABLED"
    entry_gate_changed_at: datetime | None = None
    entry_gate_armed_at: datetime | None = None
    fresh_cross_ready: bool = True
    above_level: bool = False


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
    atr_bars: list[OrbBar] = field(default_factory=list)
    atr_state: str | None = None
    atr_trail: float | None = None
    atr_flip_evaluations: int = 0
    opening_red_count: int | None = None
    entry_gate_last_bar_at: datetime | None = None
    entry_gate_evaluations: int = 0
    entry_gate_arms: int = 0
    entry_gate_pulls: int = 0
    entry_gate_withholds: int = 0
    entry_gate_atr_blocked: int = 0
    entry_gate_atr_unanswerable: int = 0
    entry_gate_red_delayed: int = 0
    entry_break_evaluations: int = 0
    entry_breaks_held: int = 0


@dataclass(frozen=True)
class _PendingPaperEntry:
    symbol: str
    entry_price: float
    observed_at: datetime
    attempt: int
    event_type: str = ORB_PAPER_EVENT_TYPE
    detail: dict[str, object] = field(default_factory=dict)
    counts_as_entry: bool = True
    event_key: str | None = None


class OrbService:
    # Class-level defaults so instances built via __new__ (some unit tests) read the
    # legacy path; __init__ overrides them from settings.
    _reclaim_mode: bool = False
    _reclaim_hold_ms: int = 25_000
    _running_high_mode: bool = False
    _resting_entry: bool = False
    _fixed_resting_mode: bool = False
    _paper_lifecycle_enabled: bool = False
    _paper_atr_entry_gate_enabled: bool = False
    _paper_four_red_delay_enabled: bool = False
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
        self._paper_positions: dict[str, OrbPaperPosition] = {}
        self._paper_closed_today: list[dict[str, object]] = []
        self._paper_recent_decisions: list[dict[str, object]] = []
        self._paper_exit_quote_evaluations = 0
        self._paper_exit_counts: dict[str, int] = {}
        self._paper_atr_bars_restored = 0
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
        self._paper_lifecycle_enabled = bool(
            getattr(self.settings, "orb_paper_lifecycle_enabled", False)
        )
        self._paper_target_pct = float(getattr(self.settings, "orb_paper_target_pct", 5.0))
        self._paper_stop_pct = float(getattr(self.settings, "orb_paper_stop_pct", 8.0))
        self._paper_min_break_body_pct = float(
            getattr(self.settings, "orb_paper_min_break_body_pct", 45.0)
        )
        self._paper_atr_exit_enabled = bool(
            getattr(self.settings, "orb_paper_atr_exit_enabled", True)
        )
        self._paper_atr_entry_gate_enabled = bool(
            getattr(self.settings, "orb_paper_atr_entry_gate_enabled", False)
        )
        self._paper_four_red_delay_enabled = bool(
            getattr(self.settings, "orb_paper_four_red_delay_enabled", False)
        )
        if self._paper_lifecycle_enabled and not self._fixed_resting_mode:
            raise RuntimeError(
                "ORB paper lifecycle requires the fixed resting entry model; refusing partial simulation"
            )
        if self._paper_lifecycle_enabled and (
            self._paper_target_pct <= 0
            or self._paper_stop_pct <= 0
            or not 0 < self._paper_min_break_body_pct <= 100
        ):
            raise RuntimeError("ORB paper lifecycle rule values are invalid; refusing to start")
        if (
            self._paper_atr_entry_gate_enabled or self._paper_four_red_delay_enabled
        ) and not self._fixed_resting_mode:
            raise RuntimeError(
                "ORB paper entry gates require the fixed resting entry model; refusing partial observation"
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
        self._restore_paper_lifecycle()
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
                    self._refresh_universe()
                    await self._sync_gateway_subscription(self._paper_market_symbols())
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
        self._paper_closed_today = []
        self._paper_recent_decisions = []
        self._restore_paper_lifecycle()
        logger.info("[ORB] day-roll reset %s -> %s: cleared per-symbol state + aggregators", prior, today)

    # ----- universe: pre-09:25 confirmed names (the binding rule) -----
    def _refresh_universe(self) -> list[str]:
        self._universe = {s.upper() for s in self._pre_open_universe()}
        return sorted(self._universe)

    def _paper_market_symbols(self) -> list[str]:
        """An open paper position keeps its feed even after the entry universe clears."""
        positions = getattr(self, "_paper_positions", {})
        return sorted(self._universe | set(positions))

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
        if agg is not None:
            bar = agg.flush_before(observed_at)
            if bar is not None:
                self._on_bar(symbol, bar, observed_at=observed_at, observed_price=ask)
            self._finalize_fixed_resting_without_0930_bar(symbol, observed_at=observed_at)
            self._evaluate_fixed_entry_gates(
                symbol,
                evaluated_at=observed_at,
                observed_price=ask,
                bar_at=None,
                record_unchanged=False,
            )
        self._evaluate_paper_position_quote(symbol, bid=bid, observed_at=observed_at)

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
            self._evaluate_fixed_entry_gates(
                symbol,
                evaluated_at=ts,
                observed_price=price,
                bar_at=None,
                record_unchanged=False,
            )
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
            "entry_gate_armed": order.entry_gate_armed,
            "entry_gate_reason": order.entry_gate_reason,
            "entry_gate_changed_at": (
                order.entry_gate_changed_at.isoformat()
                if order.entry_gate_changed_at
                else None
            ),
            "entry_gate_fresh_cross_ready": order.fresh_cross_ready,
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
        event_key: str | None = None,
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
                event_key=event_key,
            )
        )

    def _paper_entry_gates_enabled(self) -> bool:
        return self._paper_atr_entry_gate_enabled or self._paper_four_red_delay_enabled

    def _fixed_entry_gate_decision(
        self,
        st: _SymbolState,
        *,
        evaluated_at: datetime,
    ) -> tuple[bool, str, str, dict[str, object]]:
        red_delay_until = self._session_open_utc() + timedelta(minutes=1)
        red_delay_applies = bool(
            self._paper_four_red_delay_enabled
            and st.opening_red_count is not None
            and st.opening_red_count >= 4
        )
        red_delay_active = red_delay_applies and evaluated_at < red_delay_until

        atr_state = str(st.atr_state or "").lower()
        atr_unanswerable = self._paper_atr_entry_gate_enabled and not atr_state
        atr_purple = self._paper_atr_entry_gate_enabled and atr_state not in {"", "long"}

        allowed = not (red_delay_active or atr_unanswerable or atr_purple)
        if atr_unanswerable:
            check_kind = "live"
            reason = "LIVE_CHECK_ATR_STATE_UNANSWERABLE_ORDER_WITHHELD"
        elif atr_purple:
            check_kind = "live"
            reason = "LIVE_CHECK_ATR_PURPLE_ORDER_PULLED"
        elif red_delay_active:
            check_kind = "day_gate"
            reason = "DAY_GATE_FOUR_OF_FIVE_RED_FIRST_MINUTE_DELAY"
        else:
            active_kinds = [
                kind
                for kind, enabled in (
                    ("day_gate", self._paper_four_red_delay_enabled),
                    ("live", self._paper_atr_entry_gate_enabled),
                )
                if enabled
            ]
            check_kind = "+".join(active_kinds)
            reason = f"{'_AND_'.join(kind.upper() for kind in active_kinds)}_CHECKS_PASS"
        checks_evaluated = [
            kind
            for kind, enabled in (
                ("day_gate", self._paper_four_red_delay_enabled),
                ("live", self._paper_atr_entry_gate_enabled),
            )
            if enabled
        ]
        return allowed, reason, check_kind, {
            "checks_evaluated": checks_evaluated,
            "atr_entry_gate": (
                "ENABLED" if self._paper_atr_entry_gate_enabled else "DISABLED"
            ),
            "atr_state": atr_state.upper() if atr_state else "UNANSWERABLE",
            "four_red_delay": (
                "ACTIVE"
                if red_delay_active
                else "COMPLETE"
                if red_delay_applies
                else "NOT_APPLICABLE"
                if self._paper_four_red_delay_enabled
                else "DISABLED"
            ),
            "opening_red_count": st.opening_red_count,
            "red_delay_until": red_delay_until.isoformat(),
        }

    def _evaluate_fixed_entry_gates(
        self,
        symbol: str,
        *,
        evaluated_at: datetime,
        observed_price: float | None,
        bar_at: datetime | None,
        initial: bool = False,
        allow_arm: bool = True,
        record_unchanged: bool = True,
    ) -> None:
        if not self._paper_entry_gates_enabled():
            return
        st = self._states.get(symbol)
        order = st.resting_order if st is not None else None
        if st is None or order is None or order.filled_at is not None:
            return
        if bar_at is not None and st.entry_gate_last_bar_at == bar_at:
            return

        allowed, reason, check_kind, gate_detail = self._fixed_entry_gate_decision(
            st,
            evaluated_at=evaluated_at,
        )
        if allowed and not order.entry_gate_armed and not allow_arm:
            return
        changed = initial or allowed != order.entry_gate_armed
        if not changed and not record_unchanged:
            return

        prior_armed = order.entry_gate_armed
        if initial:
            action = "ARM" if allowed else "WITHHOLD"
        elif allowed and not prior_armed:
            action = "ARM"
        elif not allowed and prior_armed:
            action = "PULL"
        else:
            action = "HOLD_ARMED" if allowed else "HOLD_PULLED"

        order.entry_gate_armed = allowed
        order.entry_gate_reason = reason
        if changed:
            order.entry_gate_changed_at = evaluated_at
        if allowed:
            if initial:
                order.entry_gate_armed_at = order.placed_at
                order.fresh_cross_ready = True
            elif not prior_armed:
                order.entry_gate_armed_at = evaluated_at
                # Re-arming above the level is not a retroactive fill. A later
                # trade must first return to/below the level, then cross it.
                order.fresh_cross_ready = bool(
                    observed_price is not None and observed_price <= order.current_level
                )
                order.above_level = bool(
                    observed_price is not None and observed_price > order.current_level
                )
        else:
            order.entry_gate_armed_at = None
            order.fresh_cross_ready = False
            order.above_level = bool(
                observed_price is not None and observed_price > order.current_level
            )

        st.entry_gate_evaluations += 1
        if bar_at is not None:
            st.entry_gate_last_bar_at = bar_at
        if action == "ARM":
            st.entry_gate_arms += 1
        elif action == "PULL":
            st.entry_gate_pulls += 1
        elif action == "WITHHOLD":
            st.entry_gate_withholds += 1
        if self._paper_atr_entry_gate_enabled:
            if not st.atr_state:
                st.entry_gate_atr_unanswerable += 1
            elif str(st.atr_state).lower() != "long":
                st.entry_gate_atr_blocked += 1

        event_type = (
            ORB_PAPER_ORDER_PLACED_EVENT_TYPE
            if initial and allowed
            else ORB_PAPER_ENTRY_GATE_EVENT_TYPE
        )
        detail = self._fixed_resting_detail(
            order,
            check_kind=check_kind,
            level_derivation="MAX_1M_TRADE_HIGH_09:25_THROUGH_09:29_ET",
            status=("MODELED_RESTING" if allowed else "MODELED_ORDER_PULLED"),
            reason=reason,
            quote_at=st.latest_quote_at,
            bid=st.latest_bid,
            ask=st.latest_ask,
            decision_observed_at=evaluated_at,
        )
        detail.update(gate_detail)
        detail.update(
            {
                "entry_gate_action": action,
                "entry_gate_evaluation": st.entry_gate_evaluations,
                "fresh_cross_required_after_rearm": not order.fresh_cross_ready,
            }
        )
        self._queue_fixed_resting_event(
            symbol,
            price=order.current_level,
            observed_at=evaluated_at,
            event_type=event_type,
            detail=detail,
        )
        logger.info(
            "[ORB-PAPER-ENTRY-GATE] %s action=%s reason=%s atr=%s red=%s "
            "evaluated=%d armed=%s fresh_cross_ready=%s check=%s checks=%s",
            symbol,
            action,
            reason,
            gate_detail["atr_state"],
            st.opening_red_count,
            st.entry_gate_evaluations,
            order.entry_gate_armed,
            order.fresh_cross_ready,
            check_kind,
            ",".join(gate_detail["checks_evaluated"]),
        )
        if initial and allowed:
            logger.info(
                "[ORB-PAPER-ORDER-PLACED] %s level=%.4f placed_at=%s "
                "derivation=09:25-09:29 check=day_gate entry_gates=pass",
                symbol,
                order.current_level,
                order.placed_at.isoformat(),
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
            st.opening_red_count = sum(
                item.close < item.open for item in by_minute.values()
            )
            if self._paper_four_red_delay_enabled and st.opening_red_count >= 4:
                st.entry_gate_red_delayed = 1
            if self._paper_entry_gates_enabled():
                self._evaluate_fixed_entry_gates(
                    symbol,
                    evaluated_at=placed_at,
                    observed_price=observed_price,
                    bar_at=bar.timestamp,
                    initial=True,
                )
            else:
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
        if not order.entry_gate_armed:
            order.current_level = order.final_level
            order.adjustment_outcome = "LEVEL_FINALIZED_WHILE_ENTRY_GATE_PULLED"
            self._queue_fixed_resting_event(
                symbol,
                price=order.current_level,
                observed_at=finalized_at,
                event_type=ORB_PAPER_LEVEL_FINALIZED_EVENT_TYPE,
                detail=self._fixed_resting_detail(
                    order,
                    check_kind="live",
                    level_derivation="MAX_1M_TRADE_HIGH_09:25_THROUGH_09:30_ET_INCLUSIVE",
                    status="FINALIZED_WHILE_PULLED",
                    reason="ENTRY_GATE_PULLED_ORDER_LEVEL_UPDATED_BEFORE_REARM",
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
        # ⛔ LEFT: flag it for the record and the heartbeat, but leave the order WORKING at
        # `order.current_level` — still the 09:29 level, deliberately not raised to final_level.
        order.adjustment_unanswerable = True
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
            "observed_price=%s quote_at=%s denominator=adjustment_opportunities "
            "ruling=LEFT order_still_working_at_old_level=true",
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
            or st.pending
            or ts < order.placed_at
        ):
            return
        cutoff = self._session_open_utc() + timedelta(minutes=self._rh_window_min)
        if ts > cutoff:
            return
        if price <= order.current_level:
            order.above_level = False
            if order.entry_gate_armed:
                order.fresh_cross_ready = True
            return
        if order.above_level:
            return
        order.above_level = True
        st.entry_break_evaluations += 1
        if not order.entry_gate_armed or not order.fresh_cross_ready or (
            order.entry_gate_armed_at is not None and ts <= order.entry_gate_armed_at
        ):
            st.entry_breaks_held += 1
            order.fresh_cross_ready = False
            if self._paper_entry_gates_enabled():
                detail = self._fixed_resting_detail(
                    order,
                    check_kind="live",
                    level_derivation=(
                        "MAX_1M_TRADE_HIGH_09:25_THROUGH_09:30_ET_INCLUSIVE"
                        if order.finalized_at is not None
                        else "MAX_1M_TRADE_HIGH_09:25_THROUGH_09:29_ET"
                    ),
                    status="MODELED_BREAK_HELD",
                    reason="BREAK_HELD_BY_ENTRY_GATE_OR_FRESH_CROSS_REQUIREMENT",
                    quote_at=st.latest_quote_at,
                    bid=st.latest_bid,
                    ask=st.latest_ask,
                    decision_observed_at=ts,
                )
                detail.update(
                    {
                        "entry_gate_evaluations": st.entry_gate_evaluations,
                        "entry_break_evaluations": st.entry_break_evaluations,
                        "entry_breaks_held": st.entry_breaks_held,
                        "trade_price": price,
                    }
                )
                self._queue_fixed_resting_event(
                    symbol,
                    price=order.current_level,
                    observed_at=ts,
                    event_type=ORB_PAPER_ENTRY_GATE_EVENT_TYPE,
                    detail=detail,
                )
            return
        order.filled_at = ts
        order.fill_price = order.current_level
        st.pending = True
        entry_key = self._paper_event_key(
            symbol=symbol,
            observed_at=ts,
            event_type=ORB_PAPER_EVENT_TYPE,
            attempt=1,
        )
        forming = self._aggregators.get(symbol)
        forming_bar = forming.current_bar() if forming is not None else None
        body_pct = (
            forming_bar_body_pct(
                open_price=forming_bar.open,
                high=forming_bar.high,
                low=forming_bar.low,
                close=forming_bar.close,
            )
            if forming_bar is not None
            else None
        )
        detail = self._fixed_resting_detail(
            order,
            check_kind="live",
            level_derivation=(
                "MAX_1M_TRADE_HIGH_09:25_THROUGH_09:30_ET_INCLUSIVE"
                if order.adjusted_at is not None
                else "MAX_1M_TRADE_HIGH_09:25_THROUGH_09:29_ET"
            ),
            status="RECORDED_NOT_A_BROKER_FILL",
            reason=(
                "INTRABAR_BREAK_OF_RETAINED_09:29_LEVEL_AFTER_UNANSWERABLE_TIMING"
                if order.adjustment_unanswerable
                else "INTRABAR_BREAK_OF_MODELED_RESTING_LEVEL"
            ),
            quote_at=st.latest_quote_at,
            bid=st.latest_bid,
            ask=st.latest_ask,
            decision_observed_at=ts,
        )
        if self._paper_lifecycle_enabled:
            detail.update(
                {
                    "paper_lifecycle_version": PAPER_LIFECYCLE_VERSION,
                    "paper_position_status": "OPEN",
                    "target_pct": self._paper_target_pct,
                    "stop_pct": self._paper_stop_pct,
                    "target_price": order.current_level * (1.0 + self._paper_target_pct / 100.0),
                    "hard_stop_price": order.current_level * (1.0 - self._paper_stop_pct / 100.0),
                    "break_bar_body_pct_at_fill": body_pct,
                    "break_bar_min_body_pct": self._paper_min_break_body_pct,
                    "atr_exit": "BAR_CLOSE_5_3.5_WILDERS",
                    "atr_source": "ORB_GATEWAY_TRADE_TICKS",
                    "atr_bars_at_entry": [
                        self._paper_bar_payload(bar) for bar in st.atr_bars
                    ],
                    "clock_exit": "DISABLED",
                }
            )
            if forming_bar is None:
                detail.update(
                    {
                        "paper_position_status": "UNANSWERABLE",
                        "reason": "BREAK_BAR_EVIDENCE_MISSING_AT_MODELED_FILL",
                        "lifecycle_unanswerable_reason": (
                            "BREAK_BAR_EVIDENCE_MISSING_AT_MODELED_FILL"
                        ),
                    }
                )
        self._queue_fixed_resting_event(
            symbol,
            price=order.current_level,
            observed_at=ts,
            event_type=ORB_PAPER_EVENT_TYPE,
            detail=detail,
            counts_as_entry=True,
            event_key=entry_key,
        )
        if self._paper_lifecycle_enabled and forming_bar is not None:
            self._paper_positions[symbol] = OrbPaperPosition(
                entry_event_key=entry_key,
                symbol=symbol,
                entry_time=ts,
                entry_price=order.current_level,
                quantity=float(self.settings.orb_reclaim_quantity),
                mode="fixed_opening_high_resting",
                target_pct=self._paper_target_pct,
                stop_pct=self._paper_stop_pct,
                break_body_pct=body_pct,
                body_exit_pending=(
                    body_pct is not None and body_pct < self._paper_min_break_body_pct
                ),
            )
        elif self._paper_lifecycle_enabled:
            logger.error(
                "[ORB-PAPER-LIFECYCLE-UNANSWERABLE] %s reason=break-bar-evidence-missing "
                "modeled_fill=%.4f denominator=modeled_fills",
                symbol,
                order.current_level,
            )
        logger.info(
            "[ORB-PAPER-RESTING-FILL] %s modeled_fill=%.4f trade=%.4f at=%s "
            "adjusted=%s unanswerable_left=%s check=live assumption=resting-level",
            symbol,
            order.current_level,
            price,
            ts.isoformat(),
            order.adjusted_at is not None,
            order.adjustment_unanswerable,
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
        self._update_paper_atr(symbol, bar, observed_at=observed_at)
        if self._fixed_resting_mode:
            gate_evaluated_at = observed_at or self._modeled_minute_end(bar)
            self._evaluate_fixed_entry_gates(
                symbol,
                evaluated_at=gate_evaluated_at,
                observed_price=observed_price,
                bar_at=bar.timestamp,
                allow_arm=False,
                record_unchanged=False,
            )
            self._on_bar_fixed_resting(
                symbol,
                bar,
                observed_at=observed_at,
                observed_price=observed_price,
            )
            self._evaluate_fixed_entry_gates(
                symbol,
                evaluated_at=gate_evaluated_at,
                observed_price=observed_price,
                bar_at=bar.timestamp,
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

    def _update_paper_atr(
        self,
        symbol: str,
        bar: OrbBar,
        *,
        observed_at: datetime | None,
    ) -> None:
        """Evaluate settled ATR entry and exit rules on completed minutes."""
        entry_gate_needed = self._paper_atr_entry_gate_enabled
        exit_rule_needed = self._paper_lifecycle_enabled and self._paper_atr_exit_enabled
        if not entry_gate_needed and not exit_rule_needed:
            return
        st = self._states.setdefault(symbol, _SymbolState())
        if st.atr_bars and paper_atr_session_key(st.atr_bars[-1].timestamp) != (
            paper_atr_session_key(bar.timestamp)
        ):
            st.atr_bars = []
            st.atr_state = None
            st.atr_trail = None
        if any(item.timestamp == bar.timestamp for item in st.atr_bars):
            return
        st.atr_bars.append(bar)
        rows = compute_paper_atr_trail(
            st.atr_bars,
            period=PAPER_ATR_PERIOD,
            factor=PAPER_ATR_FACTOR,
        )
        latest = rows[-1]
        st.atr_state = str(latest["state"]) if latest["state"] else None
        st.atr_trail = float(latest["trail"]) if latest["trail"] is not None else None
        if latest["state"] is not None:
            st.atr_flip_evaluations += 1
        if not exit_rule_needed:
            return
        position = self._paper_positions.get(symbol)
        if position is not None and bar.timestamp >= position.entry_time.replace(second=0, microsecond=0):
            self._queue_fixed_resting_event(
                symbol,
                price=bar.close,
                observed_at=observed_at or self._modeled_minute_end(bar),
                event_type=ORB_PAPER_ATR_BAR_EVENT_TYPE,
                detail={
                    "paper_lifecycle_version": PAPER_LIFECYCLE_VERSION,
                    "reason": "ATR_BAR_EVIDENCE",
                    "entry_event_key": position.entry_event_key,
                    "atr_source": "ORB_GATEWAY_TRADE_TICKS",
                    "atr_period": PAPER_ATR_PERIOD,
                    "atr_factor": PAPER_ATR_FACTOR,
                    "atr_average": "WILDERS",
                    "atr_state": st.atr_state,
                    "atr_trail": st.atr_trail,
                    "atr_flip": latest["flip"],
                    "bar": self._paper_bar_payload(bar),
                },
            )
        if latest["flip"] != "SELL":
            return
        decision_at = self._modeled_minute_end(bar)
        if position is None or position.exit_pending or position.entry_time > decision_at:
            return
        position.atr_exit_pending = True
        position.atr_decision_at = decision_at
        position.atr_trail = st.atr_trail
        quote_at = st.latest_quote_at
        if st.latest_bid is not None and quote_at is not None and quote_at >= decision_at:
            self._evaluate_paper_position_quote(
                symbol,
                bid=st.latest_bid,
                observed_at=quote_at,
            )

    @staticmethod
    def _paper_bar_payload(bar: OrbBar) -> dict[str, object]:
        return {
            "timestamp": bar.timestamp.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }

    @staticmethod
    def _paper_bar_from_payload(value: object) -> OrbBar | None:
        if not isinstance(value, dict):
            return None
        try:
            timestamp = datetime.fromisoformat(str(value["timestamp"]).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return OrbBar(
                timestamp=timestamp.astimezone(UTC),
                open=float(value["open"]),
                high=float(value["high"]),
                low=float(value["low"]),
                close=float(value["close"]),
                volume=float(value["volume"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _evaluate_paper_position_quote(
        self,
        symbol: str,
        *,
        bid: float,
        observed_at: datetime,
    ) -> None:
        if not self._paper_lifecycle_enabled:
            return
        position = self._paper_positions.get(symbol)
        if position is None or position.exit_pending or observed_at < position.entry_time:
            return
        position.observe_bid(bid, observed_at)
        self._paper_exit_quote_evaluations += 1
        if position.body_exit_pending:
            self._queue_paper_exit(
                position,
                exit_price=bid,
                observed_at=observed_at,
                decision_at=position.entry_time,
                reason="BREAK_BAR_BODY_UNDER_45_PCT",
                check_kind="post-fill",
            )
        elif position.atr_exit_pending:
            self._queue_paper_exit(
                position,
                exit_price=bid,
                observed_at=observed_at,
                decision_at=position.atr_decision_at or observed_at,
                reason="ATR_TURNED_PURPLE_AT_BAR_CLOSE",
                check_kind="bar-close",
            )
        elif bid >= position.target_price:
            self._queue_paper_exit(
                position,
                exit_price=bid,
                observed_at=observed_at,
                decision_at=observed_at,
                reason="TARGET_PLUS_5_PCT_TOUCH",
                check_kind="live",
            )
        elif bid <= position.stop_price:
            self._queue_paper_exit(
                position,
                exit_price=bid,
                observed_at=observed_at,
                decision_at=observed_at,
                reason="HARD_STOP_MINUS_8_PCT_TOUCH",
                check_kind="live",
            )

    def _queue_paper_exit(
        self,
        position: OrbPaperPosition,
        *,
        exit_price: float,
        observed_at: datetime,
        decision_at: datetime,
        reason: str,
        check_kind: str,
    ) -> None:
        position.exit_pending = True
        pnl = round((exit_price - position.entry_price) * position.quantity, 8)
        pnl_pct = round((exit_price / position.entry_price - 1.0) * 100.0, 8)
        detail: dict[str, object] = {
            "paper_lifecycle_version": PAPER_LIFECYCLE_VERSION,
            "paper_position_status": "CLOSED",
            "entry_event_key": position.entry_event_key,
            "entry_time": position.entry_time.isoformat(),
            "exit_time": observed_at.isoformat(),
            "exit_decision_at": decision_at.isoformat(),
            "exit_price": exit_price,
            "exit_reason": reason,
            "reason": reason,
            "exit_summary": reason.replace("_", " ").title(),
            "check_kind": check_kind,
            "price_basis": "EXECUTABLE_BID",
            "target_pct": position.target_pct,
            "stop_pct": position.stop_pct,
            "target_price": position.target_price,
            "hard_stop_price": position.stop_price,
            "break_bar_body_pct_at_fill": position.break_body_pct,
            "break_bar_min_body_pct": self._paper_min_break_body_pct,
            "atr_period": PAPER_ATR_PERIOD,
            "atr_factor": PAPER_ATR_FACTOR,
            "atr_average": "WILDERS",
            "atr_trail": position.atr_trail,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "peak_profit_pct": position.peak_profit_pct,
            "classification": "SIMULATED_NO_REALISED_CONTROL_NOT_SIZE_QUALIFIED",
        }
        self._pending_paper_entries.append(
            _PendingPaperEntry(
                symbol=position.symbol,
                entry_price=position.entry_price,
                observed_at=observed_at,
                attempt=1,
                event_type=ORB_PAPER_EXIT_EVENT_TYPE,
                detail=detail,
                counts_as_entry=False,
            )
        )
        logger.info(
            "[ORB-PAPER-EXIT] %s entry=%.4f exit=%.4f qty=%s pnl=%+.4f "
            "reason=%s decision_at=%s observed_at=%s basis=executable-bid",
            position.symbol,
            position.entry_price,
            exit_price,
            position.quantity,
            pnl,
            reason,
            decision_at.isoformat(),
            observed_at.isoformat(),
        )

    @staticmethod
    def _paper_entry_row(position: OrbPaperPosition) -> dict[str, object]:
        return {
            "event_key": position.entry_event_key,
            "ticker": position.symbol,
            "symbol": position.symbol,
            "status": "paper_position_opened",
            "reason": "MODELED_RESTING_FILL",
            "entry_price": position.entry_price,
            "quantity": position.quantity,
            "entry_time": position.entry_time.isoformat(),
            "last_bar_at": position.entry_time.isoformat(),
            "target_price": position.target_price,
            "hard_stop_price": position.stop_price,
        }

    @staticmethod
    def _closed_row(decision: OrbPaperDecision) -> dict[str, object]:
        detail = decision.detail
        return {
            "event_key": decision.event_key,
            "ticker": decision.symbol,
            "symbol": decision.symbol,
            "entry_price": float(decision.entry_price),
            "exit_price": float(detail["exit_price"]),
            "quantity": float(decision.quantity),
            "pnl": float(detail["pnl"]),
            "pnl_pct": float(detail["pnl_pct"]),
            "reason": str(detail["exit_reason"]),
            "exit_summary": str(detail.get("exit_summary") or detail["exit_reason"]),
            "entry_time": str(detail["entry_time"]),
            "exit_time": str(detail["exit_time"]),
            "peak_profit_pct": float(detail.get("peak_profit_pct") or 0.0),
        }

    @classmethod
    def _paper_exit_row(cls, decision: OrbPaperDecision) -> dict[str, object]:
        row = cls._closed_row(decision)
        row.update(
            {
                "status": "paper_trade_closed",
                "last_bar_at": row["exit_time"],
            }
        )
        return row

    def _remember_paper_decision(self, row: dict[str, object]) -> None:
        key = str(row.get("event_key") or "")
        self._paper_recent_decisions = [
            item for item in self._paper_recent_decisions if str(item.get("event_key") or "") != key
        ]
        self._paper_recent_decisions.insert(0, row)
        del self._paper_recent_decisions[50:]

    def _complete_paper_exit(self, decision: OrbPaperDecision) -> None:
        position = self._paper_positions.get(decision.symbol)
        entry_key = str(decision.detail.get("entry_event_key") or "")
        if position is None or position.entry_event_key != entry_key:
            return
        self._paper_positions.pop(decision.symbol, None)
        row = self._closed_row(decision)
        self._paper_closed_today = [
            item for item in self._paper_closed_today if item.get("event_key") != decision.event_key
        ]
        self._paper_closed_today.insert(0, row)
        reason = str(decision.detail.get("exit_reason") or "UNKNOWN")
        self._paper_exit_counts[reason] = self._paper_exit_counts.get(reason, 0) + 1
        self._remember_paper_decision(self._paper_exit_row(decision))

    def _restore_paper_lifecycle(self) -> None:
        """Fail closed on startup rather than forgetting an open modeled position."""
        if not self._paper_lifecycle_enabled:
            return
        if self.paper_store is None:
            raise RuntimeError("ORB paper lifecycle cannot restore without its durable store")
        positions: dict[str, OrbPaperPosition] = {}
        closed_today: list[dict[str, object]] = []
        recent: list[dict[str, object]] = []
        exit_counts: dict[str, int] = {}
        for decision in self.paper_store.load_lifecycle():
            detail = decision.detail
            if int(detail.get("paper_lifecycle_version") or 0) != PAPER_LIFECYCLE_VERSION:
                continue
            observed_at = decision.observed_at
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=UTC)
            if decision.event_type == ORB_PAPER_EVENT_TYPE:
                if detail.get("paper_position_status") != "OPEN":
                    if decision.session_date == self._session_date:
                        recent.insert(
                            0,
                            {
                                "event_key": decision.event_key,
                                "ticker": decision.symbol,
                                "symbol": decision.symbol,
                                "status": "paper_trade_unanswerable",
                                "reason": detail.get("lifecycle_unanswerable_reason"),
                                "entry_price": float(decision.entry_price),
                                "entry_time": observed_at.isoformat(),
                                "last_bar_at": observed_at.isoformat(),
                            },
                        )
                    continue
                position = OrbPaperPosition(
                    entry_event_key=decision.event_key,
                    symbol=decision.symbol,
                    entry_time=observed_at,
                    entry_price=float(decision.entry_price),
                    quantity=float(decision.quantity),
                    mode=decision.mode,
                    target_pct=float(detail.get("target_pct") or self._paper_target_pct),
                    stop_pct=float(detail.get("stop_pct") or self._paper_stop_pct),
                    break_body_pct=(
                        float(detail["break_bar_body_pct_at_fill"])
                        if detail.get("break_bar_body_pct_at_fill") is not None
                        else None
                    ),
                    body_exit_pending=(
                        detail.get("break_bar_body_pct_at_fill") is not None
                        and float(detail["break_bar_body_pct_at_fill"])
                        < self._paper_min_break_body_pct
                    ),
                )
                positions[decision.symbol] = position
                state = self._states.setdefault(decision.symbol, _SymbolState())
                state.atr_bars = []
                for raw_bar in detail.get("atr_bars_at_entry") or []:
                    restored_bar = self._paper_bar_from_payload(raw_bar)
                    if restored_bar is None:
                        continue
                    if state.atr_bars and paper_atr_session_key(
                        state.atr_bars[-1].timestamp
                    ) != paper_atr_session_key(restored_bar.timestamp):
                        state.atr_bars = []
                    if all(item.timestamp != restored_bar.timestamp for item in state.atr_bars):
                        state.atr_bars.append(restored_bar)
                if decision.session_date == self._session_date:
                    recent.insert(0, self._paper_entry_row(position))
            elif decision.event_type == ORB_PAPER_ATR_BAR_EVENT_TYPE:
                entry_key = str(detail.get("entry_event_key") or "")
                position = positions.get(decision.symbol)
                restored_bar = self._paper_bar_from_payload(detail.get("bar"))
                if (
                    position is None
                    or position.entry_event_key != entry_key
                    or restored_bar is None
                ):
                    continue
                state = self._states.setdefault(decision.symbol, _SymbolState())
                if state.atr_bars and paper_atr_session_key(
                    state.atr_bars[-1].timestamp
                ) != paper_atr_session_key(restored_bar.timestamp):
                    state.atr_bars = []
                if all(item.timestamp != restored_bar.timestamp for item in state.atr_bars):
                    state.atr_bars.append(restored_bar)
                state.atr_state = str(detail.get("atr_state") or "") or None
                state.atr_trail = (
                    float(detail["atr_trail"]) if detail.get("atr_trail") is not None else None
                )
                if state.atr_state is not None:
                    state.atr_flip_evaluations += 1
                if detail.get("atr_flip") == "SELL":
                    position.atr_exit_pending = True
                    position.atr_decision_at = self._modeled_minute_end(restored_bar)
                    position.atr_trail = state.atr_trail
            elif decision.event_type == ORB_PAPER_EXIT_EVENT_TYPE:
                entry_key = str(detail.get("entry_event_key") or "")
                position = positions.get(decision.symbol)
                if position is not None and position.entry_event_key == entry_key:
                    positions.pop(decision.symbol, None)
                if decision.session_date == self._session_date:
                    closed_today.insert(0, self._closed_row(decision))
                    recent.insert(0, self._paper_exit_row(decision))
                    reason = str(detail.get("exit_reason") or "UNKNOWN")
                    exit_counts[reason] = exit_counts.get(reason, 0) + 1
        self._states = {
            symbol: state for symbol, state in self._states.items() if symbol in positions
        }
        self._paper_positions = positions
        self._paper_closed_today = closed_today[:100]
        self._paper_recent_decisions = recent[:50]
        self._paper_exit_counts = exit_counts
        restored_atr_bars = sum(len(state.atr_bars) for state in self._states.values())
        self._paper_atr_bars_restored = restored_atr_bars
        replay_points = [
            max(bar.timestamp for bar in state.atr_bars)
            for symbol, state in self._states.items()
            if symbol in positions and state.atr_bars
        ]
        if replay_points:
            # One Redis stream offset serves every symbol. Resume from the oldest
            # open position's next unpersisted minute; duplicate bars for fresher
            # symbols are ignored by timestamp, while using the newest point here
            # could skip evidence for the older position entirely.
            replay_from_ms = int((min(replay_points).timestamp() + 60.0) * 1000) - 1
            self._md_offset = f"{replay_from_ms}-0"
        logger.info(
            "[ORB-PAPER-RESTORE] open=%d closed_today=%d atr_bars=%d lifecycle_version=%d",
            len(positions),
            len(closed_today),
            restored_atr_bars,
            PAPER_LIFECYCLE_VERSION,
        )

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
        event_key: str | None = None,
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
        event_key = event_key or self._paper_event_key(
            symbol=symbol,
            observed_at=observed_at,
            event_type=event_type,
            attempt=decision_attempt,
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
    def _paper_event_key(
        *, symbol: str, observed_at: datetime, event_type: str, attempt: int
    ) -> str:
        return (
            f"orb-paper:{observed_at.astimezone(_ET).date().isoformat()}:{symbol}:"
            f"{event_type}:{attempt}:{int(observed_at.timestamp() * 1_000_000)}"
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
                    event_key=item.event_key,
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
            if item.counts_as_entry and self._paper_lifecycle_enabled:
                position = self._paper_positions.get(symbol)
                if position is not None and position.entry_event_key == decision.event_key:
                    self._remember_paper_decision(self._paper_entry_row(position))
                    if (
                        position.body_exit_pending
                        and st is not None
                        and st.latest_bid is not None
                        and st.latest_quote_at is not None
                        and st.latest_quote_at >= position.entry_time
                    ):
                        self._evaluate_paper_position_quote(
                            symbol,
                            bid=st.latest_bid,
                            observed_at=st.latest_quote_at,
                        )
                elif decision.detail.get("paper_position_status") == "UNANSWERABLE":
                    self._remember_paper_decision(
                        {
                            "event_key": decision.event_key,
                            "ticker": decision.symbol,
                            "symbol": decision.symbol,
                            "status": "paper_trade_unanswerable",
                            "reason": decision.detail.get("lifecycle_unanswerable_reason"),
                            "entry_price": float(decision.entry_price),
                            "entry_time": decision.observed_at.isoformat(),
                            "last_bar_at": decision.observed_at.isoformat(),
                        }
                    )
            if item.event_type == ORB_PAPER_ENTRY_GATE_EVENT_TYPE:
                self._remember_paper_decision(
                    {
                        "event_key": decision.event_key,
                        "ticker": decision.symbol,
                        "symbol": decision.symbol,
                        "status": str(
                            decision.detail.get("entry_gate_action")
                            or "ENTRY_GATE_EVALUATED"
                        ).lower(),
                        "reason": decision.detail.get("reason"),
                        "entry_price": float(decision.entry_price),
                        "entry_time": decision.observed_at.isoformat(),
                        "last_bar_at": decision.observed_at.isoformat(),
                        "check_kind": decision.detail.get("check_kind"),
                        "entry_gate_armed": decision.detail.get("entry_gate_armed"),
                        "entry_gate_evaluation": decision.detail.get(
                            "entry_gate_evaluation"
                        ),
                    }
                )
            if item.event_type == ORB_PAPER_EXIT_EVENT_TYPE and self._paper_lifecycle_enabled:
                self._complete_paper_exit(decision)
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
        paper_positions = getattr(self, "_paper_positions", {})
        lifecycle_enabled = bool(getattr(self, "_paper_lifecycle_enabled", False))
        for sym, st in sorted(self._states.items()):
            bar_counts[sym] = len(st.or_bars)
            if st.last_bar_at:
                last_tick[sym] = st.last_bar_at
            if sym in paper_positions:
                status = "paper_position_open"
            elif st.paper_entries:
                status = "paper_entry_recorded"
            elif self._fixed_resting_mode and st.resting_order is not None:
                if not st.resting_order.entry_gate_armed:
                    status = "entry_gate_withheld"
                elif st.resting_order.adjustment_unanswerable:
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
            row: dict = {"ticker": sym, "symbol": sym, "status": status}
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
                        "entry_gate_armed": order.entry_gate_armed,
                        "entry_gate_reason": order.entry_gate_reason,
                        "entry_gate_changed_at": (
                            order.entry_gate_changed_at.isoformat()
                            if order.entry_gate_changed_at
                            else None
                        ),
                        "fresh_cross_ready": order.fresh_cross_ready,
                        "opening_red_count_0925_0929": st.opening_red_count,
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
        # ⛔⭐ THE RULING'S OWN DENOMINATOR. LEFT and PULLED differ on exactly one population:
        # orders that were unanswerable AND then filled at the retained 09:29 level. Under PULLED
        # every one of these would have been a missed trade. Counting it is what makes the ruling
        # reviewable later instead of permanent by default.
        left_fills_at_retained_level = sum(
            1
            for st in self._states.values()
            if st.resting_order is not None
            and st.resting_order.adjustment_unanswerable
            and st.resting_order.filled_at is not None
        )
        position_rows = [
            {
                "ticker": position.symbol,
                "symbol": position.symbol,
                "quantity": position.quantity,
                "entry_price": position.entry_price,
                "current_price": (
                    position.current_bid
                    if position.current_bid is not None
                    else position.entry_price
                ),
                "entry_time": position.entry_time.isoformat(),
                "target_price": position.target_price,
                "hard_stop_price": position.stop_price,
                "break_bar_body_pct_at_fill": position.break_body_pct,
                "atr_exit_pending": position.atr_exit_pending,
                "price_basis": "EXECUTABLE_BID",
            }
            for position in sorted(paper_positions.values(), key=lambda item: item.entry_time)
        ]
        closed_today = list(getattr(self, "_paper_closed_today", []))
        recent_paper = list(getattr(self, "_paper_recent_decisions", []))
        if lifecycle_enabled or self._paper_entry_gates_enabled():
            decisions = recent_paper + decisions
        atr_denominator = sum(st.atr_flip_evaluations for st in self._states.values())
        entry_gate_evaluations = sum(
            st.entry_gate_evaluations for st in self._states.values()
        )
        entry_gate_arms = sum(st.entry_gate_arms for st in self._states.values())
        entry_gate_pulls = sum(st.entry_gate_pulls for st in self._states.values())
        entry_gate_withholds = sum(
            st.entry_gate_withholds for st in self._states.values()
        )
        entry_gate_atr_blocked = sum(
            st.entry_gate_atr_blocked for st in self._states.values()
        )
        entry_gate_atr_unanswerable = sum(
            st.entry_gate_atr_unanswerable for st in self._states.values()
        )
        entry_gate_red_delayed = sum(
            st.entry_gate_red_delayed for st in self._states.values()
        )
        entry_break_evaluations = sum(
            st.entry_break_evaluations for st in self._states.values()
        )
        entry_breaks_held = sum(st.entry_breaks_held for st in self._states.values())
        exit_quote_denominator = int(getattr(self, "_paper_exit_quote_evaluations", 0))
        return StrategyBotStatePayload(
            strategy_code=SERVICE_NAME,
            account_name=ORB_PAPER_ACCOUNT_NAME,
            watchlist=self._paper_market_symbols(),
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
                "paper_lifecycle": {
                    "status": "ACTIVE" if lifecycle_enabled else "DISABLED",
                    "open_positions": len(position_rows),
                    "closed_today": len(closed_today),
                    "exit_quote_evaluations": exit_quote_denominator,
                    "exit_quote_evaluations_basis": "SINCE_PROCESS_START",
                    "exit_counts": dict(getattr(self, "_paper_exit_counts", {})),
                    "exit_counts_basis": "CURRENT_ET_DAY_DURABLE_TAPE",
                    "rules": {
                        "target": "+5% touch on executable bid",
                        "hard_stop": "-8% touch on executable bid",
                        "break_bar_body": "under 45% at fill; exit on first post-fill executable bid",
                        "atr": "5, 3.5, Wilders; purple turn on completed minute",
                        "clock": "disabled; an open trade is never force-closed by time",
                    },
                    "atr_completed_bar_evaluations": atr_denominator,
                    "atr_source": "ORB_GATEWAY_TRADE_TICKS",
                    "atr_bars_restored": int(getattr(self, "_paper_atr_bars_restored", 0)),
                    "atr_restart_recovery": (
                        "DURABLE_ENTRY_SNAPSHOT_PLUS_COMPLETED_BARS_AND_STREAM_REPLAY"
                    ),
                    "denominator": exit_quote_denominator,
                    "denominator_basis": "EXECUTABLE_BID_EVALUATIONS_SINCE_PROCESS_START",
                },
                "paper_entry_gates": {
                    "status": (
                        "ACTIVE" if self._paper_entry_gates_enabled() else "DISABLED"
                    ),
                    "atr_live_gate_enabled": self._paper_atr_entry_gate_enabled,
                    "four_red_first_minute_delay_enabled": (
                        self._paper_four_red_delay_enabled
                    ),
                    "rules": {
                        "atr": (
                            "completed-bar cyan/long arms; purple/short or unknown pulls; "
                            "later cyan/long may re-arm"
                        ),
                        "four_red": (
                            "four or more red bars among 09:25-09:29 delay entry only "
                            "until 09:31 ET"
                        ),
                        "rearm": (
                            "no retroactive fill; price must return to/below the fixed level "
                            "then cross above"
                        ),
                    },
                    "evaluations": entry_gate_evaluations,
                    "arms": entry_gate_arms,
                    "pulls": entry_gate_pulls,
                    "initial_withholds": entry_gate_withholds,
                    "atr_blocked": entry_gate_atr_blocked,
                    "atr_unanswerable": entry_gate_atr_unanswerable,
                    "red_delayed_name_days": entry_gate_red_delayed,
                    "break_crossings_evaluated": entry_break_evaluations,
                    "break_crossings_held": entry_breaks_held,
                    "denominator": entry_gate_evaluations,
                    "denominator_basis": "MODELED_ENTRY_GATE_DECISIONS_SINCE_PROCESS_START",
                },
                "resting_adjustment_timing": {
                    "status": "MEASURED" if adjustment_denominator else "UNEXERCISED",
                    "filled_before_adjustment": filled_before_adjustment,
                    "modeled_adjustments_landed": adjustments_landed,
                    "unanswerable": adjustment_unanswerable,
                    "left_fills_at_retained_level": left_fills_at_retained_level,
                    "ruling": "LEFT (operator 2026-09-09)",
                    "denominator": adjustment_denominator,
                },
            },
            recent_decisions=decisions[:50],
            positions=position_rows,
            daily_pnl=sum(float(item.get("pnl") or 0.0) for item in closed_today),
            closed_today=closed_today,
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
                    "paper_lifecycle": (
                        "entry+position+exit+pnl"
                        if self._paper_lifecycle_enabled
                        else "entry-observation-only"
                    ),
                    "paper_atr_entry_gate": (
                        "enabled" if self._paper_atr_entry_gate_enabled else "disabled"
                    ),
                    "paper_four_red_delay": (
                        "enabled" if self._paper_four_red_delay_enabled else "disabled"
                    ),
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
