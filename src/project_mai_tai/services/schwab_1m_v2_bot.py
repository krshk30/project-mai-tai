"""Service entrypoint for the isolated `schwab_1m_v2` bot.

Sixth service. Runs as its own systemd unit. Subscribes to the existing
`mai_tai:strategy-state` Redis stream to pick up the scanner's confirmed
symbol set, polls Schwab REST for 1m bars + quotes, evaluates the strategy
(placeholder), persists completed bars to `strategy_bar_history`, publishes
its own state to `mai_tai:strategy-state-isolated` so the dashboard renders
the bot like any other, and emits intents to `mai_tai:strategy-intents` for
OMS to consume.

NO imports from `services/strategy_engine_app.py`, `services/strategy_engine.py`,
`market_data/schwab_streamer.py`, `strategy_core/schwab_native_30s.py`, etc.

Idle (no intents, no REST traffic) when:
- v2 enable flag is off (default), OR
- the Schwab token store is empty / unreadable

This lets the service ship + boot before the operator wires credentials
or flips the enable flag.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import (
    BrokerAccount,
    Strategy,
    StrategyBarHistory,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    IsolatedBotStateEvent,
    MarketDataSubscriptionEvent,
    MarketDataSubscriptionPayload,
    StrategyBotStatePayload,
    StrategyStateSnapshotEvent,
    stream_name,
)
from project_mai_tai.market_data.schwab_v2_loop_health import (
    LoopHealthTracker,
    run_resilient_loop,
    sleep_or_stop,
)
from project_mai_tai.market_data.schwab_v2_rest_client import (
    ChartBar,
    Quote,
    SchwabV2RestClient,
)
from project_mai_tai.market_data.schwab_v2_streamer import SchwabV2Streamer
from project_mai_tai.market_data.schwab_v2_tick_writer import SchwabV2TickWriter
from project_mai_tai.settings import Settings, get_settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    SERVICE_NAME,
    STRATEGY_CODE,
    SchwabV2IntentEmitter,
    SchwabV2Strategy,
)

logger = logging.getLogger(__name__)

INTERVAL_SECS = 60
STATE_PUBLISH_INTERVAL_SECONDS = 5
POSITION_POLL_INTERVAL_SECONDS = 5
# Max bar age (seconds) for DB-persistence. Older bars are warmup feeds
# that prior service instances already persisted; redoing them on every
# restart would block the bar loop for ~10s per symbol on cold-start.
PERSIST_BAR_AGE_LIMIT_SECONDS = 300
# Bar age (seconds) at which a REST-fed bar signals "REST warmup has
# caught up to live for this symbol." The REST warmup batch returns
# bars oldest-first; the tail of the batch is within ~5 min of wall
# clock and crossing that threshold marks the symbol as ready for
# direct strategy ingestion (no longer for streamer subscription —
# the streamer now subscribes immediately on scanner-state arrival).
# 300s matches PERSIST_BAR_AGE_LIMIT_SECONDS so we only mark warmed
# once the same bar would qualify for DB persist.
REST_WARMUP_FRESH_THRESHOLD_SECS = 300.0
# Cap on the per-symbol streamer-pending buffer used while REST warmup
# is in flight. Streamer pushes at most one CHART_EQUITY bar per
# symbol per minute, so 500 covers >8h of pre-warmup buffering — far
# beyond any realistic warmup duration. The cap exists only to bound
# memory if warmup never completes (e.g. weekend test where REST
# returns no fresh candles); on overflow the oldest pending bar is
# dropped.
STREAMER_PENDING_BARS_MAX_PER_SYMBOL = 500
INFLIGHT_INTENT_STATUSES_TERMINAL = ("filled", "rejected", "cancelled")
EASTERN_TZ = ZoneInfo("America/New_York")

# --- Data-flow watchdog thresholds ---
# Whole-watchlist "no bar processed" window that counts as a data stall.
# A 60s bar bot during active trading produces a fresh bar for SOME
# watchlist symbol well within a minute; 180s (3 missed cycles) is a
# robust stall signal that tolerates a quiet symbol or two.
DATA_STALL_THRESHOLD_SECS = 180.0
# Fresh quote activity within this window means the market is actively
# trading. Quotes are the holiday-safe discriminator between "our bar
# pipeline is broken" and "market is closed/holiday so no data is
# expected" — on a closed day quotes go stale too. (Quotes poll ~5s.)
QUOTE_LIVE_THRESHOLD_SECS = 90.0
# Grace period after startup before the stall watchdog can fire, so the
# REST warmup batch has time to land the first bars.
WATCHDOG_STARTUP_GRACE_SECS = 150.0

# US equity market FULL-closure holidays (NYSE/Nasdaq), as ET local dates.
# `_market_session` returns "closed" on these so a holiday weekday isn't
# misread as "regular" — otherwise the watchdog would flag a holiday RTH
# with no bars as a stall. Observed dates (the weekday the market is
# actually shut) are listed, not the nominal date.
#
# MAINTENANCE: hardcoded because the repo has no market-calendar utility and
# it's ~10 dates/year. Covers 2026-2027. **Extend this set when the year
# rolls over** (add the next year before ~December) or holiday RTH days will
# silently misclassify as "regular" again and quietly reintroduce the
# false-stall bug. Half-days (day after Thanksgiving, Christmas Eve on a
# weekday; 13:00 ET early close) are intentionally NOT listed — see the
# decision documented in `_market_session`.
_US_MARKET_HOLIDAYS: frozenset[date] = frozenset(
    {
        # --- 2026 ---
        date(2026, 1, 1),    # New Year's Day
        date(2026, 1, 19),   # MLK Jr. Day
        date(2026, 2, 16),   # Presidents' Day
        date(2026, 4, 3),    # Good Friday
        date(2026, 5, 25),   # Memorial Day
        date(2026, 6, 19),   # Juneteenth
        date(2026, 7, 3),    # Independence Day (observed; Jul 4 is a Saturday)
        date(2026, 9, 7),    # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
        # --- 2027 ---
        date(2027, 1, 1),    # New Year's Day
        date(2027, 1, 18),   # MLK Jr. Day
        date(2027, 2, 15),   # Presidents' Day
        date(2027, 3, 26),   # Good Friday
        date(2027, 5, 31),   # Memorial Day
        date(2027, 6, 18),   # Juneteenth (observed; Jun 19 is a Saturday)
        date(2027, 7, 5),    # Independence Day (observed; Jul 4 is a Sunday)
        date(2027, 9, 6),    # Labor Day
        date(2027, 11, 25),  # Thanksgiving
        date(2027, 12, 24),  # Christmas (observed; Dec 25 is a Saturday)
    }
)


def _format_eastern(dt: datetime) -> str:
    """Format a datetime as `"YYYY-MM-DD HH:MM:SS AM/PM ET"`, matching the
    existing strategy-engine's `_datetime_str` so the dashboard's max()-based
    derivation of `latest_bot_tick_at` produces a value that's consistent in
    sort order and display with the other bots.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(EASTERN_TZ).strftime("%Y-%m-%d %I:%M:%S %p ET")


def _current_scanner_session_start_utc(now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    current_et = current.astimezone(EASTERN_TZ)
    session_start_et = current_et.replace(hour=4, minute=0, second=0, microsecond=0)
    if current_et < session_start_et:
        session_start_et -= timedelta(days=1)
    return session_start_et.astimezone(UTC)


class SchwabV2BotService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis: Redis | None = None
        self.strategy = SchwabV2Strategy(self.settings)
        self.rest_client: SchwabV2RestClient | None = None
        self.streamer: SchwabV2Streamer | None = None
        self.intent_emitter: SchwabV2IntentEmitter | None = None
        self.session_factory: sessionmaker[Session] | None = session_factory
        self._stop_event = asyncio.Event()
        self._strategy_state_stream = stream_name(
            self.settings.redis_stream_prefix, "strategy-state"
        )
        self._isolated_state_stream = stream_name(
            self.settings.redis_stream_prefix, "strategy-state-isolated"
        )
        self._strategy_state_last_id = "$"
        self._watchlist: set[str] = set()
        # Track-2 Phase-2 Slice-2: last symbol list published to the gateway as a
        # subscription consumer (debounce). None = nothing published yet.
        self._last_gateway_symbols: list[str] | None = None
        self._bar_counts: dict[str, int] = {}
        self._last_tick_at: dict[str, str] = {}
        self._last_bar_at: dict[str, str] = {}
        # Set of symbols whose REST warmup batch has caught up to within
        # REST_WARMUP_FRESH_THRESHOLD_SECS of wall clock. The streamer
        # subscribes to the full watchlist immediately on scanner-state
        # arrival (see `_apply_strategy_state_event`); this set instead
        # gates strategy INGESTION of streamer bars. Streamer bars for
        # symbols not yet warmed are buffered in `_streamer_pending` and
        # replayed in timestamp order when warmup completes, so the
        # strategy's append-only deque never sees an out-of-order bar.
        self._rest_warmup_done: set[str] = set()
        # Per-symbol queue of streamer bars received before this symbol's
        # REST warmup completed. Drained in `_handle_bar_from_rest`
        # when the symbol crosses into `_rest_warmup_done`, replaying
        # in timestamp order only those bars strictly newer than the
        # latest bar already in `state.bars`.
        self._streamer_pending: dict[str, list[ChartBar]] = {}
        # C3 routing counters — exposed via heartbeat for observability.
        # `rest_bars_gated` increments on REST bars suppressed because
        # streamer is healthy and already has the bucket. `rest_bars_gap_fill`
        # increments on REST bars that pass through while streamer is
        # connected (genuine gap fills where streamer missed a bucket).
        self._rest_bars_gated: int = 0
        self._rest_bars_gap_fill: int = 0
        # --- Data-flow watchdog state ---
        # Wall-clock (ms) of process start, last bar processed (any symbol),
        # and last quote per symbol. The watchdog compares bar-flow against
        # quote-liveness + market session to decide whether a bar stall is
        # a genuine RTH pipeline fault (degraded + WARN) or expected
        # off-hours REST dryness (degraded + INFO). `_last_data_flow` is the
        # previous classification, for throttled transition logging.
        self._started_at_ms: int = int(datetime.now(UTC).timestamp() * 1000)
        self._last_bar_processed_at_ms: int = 0
        self._last_quote_at_ms: dict[str, int] = {}
        self._last_data_flow: str | None = None
        self._data_health: dict[str, object] = {
            "status": "starting",
            "halted_symbols": [],
            "warning_symbols": [],
        }
        # --- SPOF Workstream A (v2): loop-resilience state ---
        # Shared with the REST client so bar/quote-loop failures surface in this
        # service's heartbeat. See docs/schwab-1m-v2-loop-resilience-design.md.
        self._loop_health = LoopHealthTracker(
            persistent_failure_threshold=int(
                getattr(self.settings, "strategy_schwab_1m_v2_loop_persistent_failure_threshold", 3)
            ),
            logger=logger,
        )
        self._loop_backoff_secs = max(
            0.0,
            float(getattr(self.settings, "strategy_schwab_1m_v2_loop_error_backoff_seconds", 1.0)),
        )
        self._loop_fault_injection_remaining = max(
            0,
            int(getattr(self.settings, "strategy_schwab_1m_v2_loop_fault_injection_count", 0) or 0),
        )
        # name -> task, populated in run(); watched by _task_liveness_loop so a
        # task that ends unexpectedly (v2's silent-death risk) is surfaced loudly.
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "strategy_schwab_1m_v2_enabled", False))

    @property
    def streamer_enabled(self) -> bool:
        """Streamer subsumes the REST bar-poll path for live bars. REST keeps
        running concurrently for cold-start warmup + reconnect gap-fill —
        both feed `_handle_bar`, which is idempotent at strategy + persist
        layers via the strategy's same-bucket update semantics and the
        UPSERT in `_persist_bar`.
        """
        return self.enabled and bool(
            getattr(self.settings, "strategy_schwab_1m_v2_streamer_enabled", False)
        )

    async def run(self) -> None:
        logging.basicConfig(
            level=self.settings.log_level.upper(),
            format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        )
        logger.info("schwab_1m_v2 bot starting (enabled=%s)", self.enabled)

        if not self.enabled:
            logger.warning(
                "schwab_1m_v2 disabled: set MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENABLED=true "
                "to activate. Service will heartbeat as degraded and idle."
            )

        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        if self.session_factory is None:
            try:
                self.session_factory = build_session_factory(self.settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "schwab_1m_v2 session_factory unavailable, bar persistence "
                    "disabled: %s",
                    exc,
                )
        self.intent_emitter = SchwabV2IntentEmitter(
            self.settings,
            self.redis,
            broker_account_name=self.settings.strategy_schwab_1m_v2_account_name,
        )
        self.rest_client = SchwabV2RestClient(
            self.settings,
            on_chart_bar=self._handle_bar_from_rest,
            on_quote=self._handle_quote,
            loop_health=self._loop_health,
        )
        # Tick capture (LEVELONE) — pure observer, default OFF. Built before the
        # streamer so its on_tick can be wired in. Needs a session_factory; build
        # one eagerly if the bar-persist path hasn't lazily created it yet.
        self.tick_writer: SchwabV2TickWriter | None = None
        if bool(getattr(self.settings, "strategy_schwab_1m_v2_tick_capture_enabled", False)):
            if self.session_factory is None:
                try:
                    self.session_factory = build_session_factory(self.settings)
                except Exception:
                    logger.exception(
                        "schwab_1m_v2 tick capture: session_factory unavailable; "
                        "ticks will not persist"
                    )
            self.tick_writer = SchwabV2TickWriter(self.settings, self.session_factory)
        self.streamer = SchwabV2Streamer(
            self.settings,
            on_chart_bar=self._handle_bar_from_streamer,
            on_tick=self.tick_writer.on_tick if self.tick_writer is not None else None,
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                # Windows event loops don't support add_signal_handler;
                # the SIGTERM path on Linux is the production case.
                pass

        await self._publish_heartbeat("starting")
        self._data_health["status"] = "healthy" if self.enabled else "degraded"

        # Named tasks (SPOF Workstream A v2): each loop is individually backstopped
        # by run_resilient_loop, and _task_liveness_loop watches this set so a task
        # that ends unexpectedly is surfaced loudly (v2's silent-death risk).
        self._tasks = {
            "heartbeat": asyncio.create_task(self._heartbeat_loop()),
            "state_publish": asyncio.create_task(self._state_publish_loop()),
        }
        if self.enabled:
            self._tasks["rest_client"] = asyncio.create_task(self.rest_client.run())
            self._tasks["scanner"] = asyncio.create_task(self._scanner_consumer_loop())
            self._tasks["position_poll"] = asyncio.create_task(self._position_poll_loop())
            if self.streamer_enabled:
                self._tasks["streamer"] = asyncio.create_task(self.streamer.run())
                logger.info(
                    "[V2-WS-INIT] schwab_v2 streamer enabled, REST polling "
                    "continues for cold-start warmup + reconnect gap-fill"
                )
            if self.tick_writer is not None:
                self._tasks["tick_writer"] = asyncio.create_task(self.tick_writer.run())
                logger.info(
                    "[V2-TICK-INIT] schwab_v2 LEVELONE tick capture enabled "
                    "(observer-only; flush_interval=%ss batch=%s)",
                    self.settings.strategy_schwab_1m_v2_tick_flush_interval_secs,
                    self.settings.strategy_schwab_1m_v2_tick_flush_batch_size,
                )
        # Liveness supervisor is started last and never watches itself.
        self._tasks["liveness"] = asyncio.create_task(self._task_liveness_loop())

        try:
            await self._stop_event.wait()
        finally:
            await self._publish_heartbeat("stopping")
            for task in self._tasks.values():
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            if self.streamer is not None:
                await self.streamer.stop()
            if self.tick_writer is not None:
                await self.tick_writer.stop()
            if self.rest_client is not None:
                await self.rest_client.stop()
            if self.redis is not None:
                await self.redis.aclose()

    def _market_session(self, now: datetime) -> str:
        """US-equity session in ET: 'premarket' | 'regular' | 'afterhours'
        | 'closed'. Weekends AND full-closure holidays (see
        `_US_MARKET_HOLIDAYS`) classify as 'closed' directly.

        Half-day sessions (day after Thanksgiving, Christmas Eve on a
        weekday — 13:00 ET early close) are intentionally NOT special-cased:
        they're treated as normal regular hours, and the quote-liveness gate
        in `_evaluate_data_flow` handles the early close (quotes go stale
        after 13:00 ET, so the watchdog lands on 'idle_market_quiet' rather
        than a false stall). Deliberate simplification, not an oversight.
        """
        et = now.astimezone(EASTERN_TZ)
        if et.weekday() >= 5:
            return "closed"
        if et.date() in _US_MARKET_HOLIDAYS:
            return "closed"
        minutes = et.hour * 60 + et.minute
        if 4 * 60 <= minutes < 9 * 60 + 30:
            return "premarket"
        if 9 * 60 + 30 <= minutes < 16 * 60:
            return "regular"
        if 16 * 60 <= minutes < 20 * 60:
            return "afterhours"
        return "closed"

    def _evaluate_data_flow(self, now_ms: int) -> tuple[str, dict[str, str]]:
        """Derive heartbeat status + watchdog detail from bar/quote flow.

        Core insight: quotes flow whenever the market is actually trading
        (holiday-safe), while pricehistory REST bars can be dry — notably
        pre/after-hours, where Schwab pricehistory does not serve same-day
        intraday minutes. So 'quotes live but bars stalled' is the real
        starvation signature, graded by session:

        - regular hours -> data_flow='stalled_rth' (REST served same-day
          bars on a normal RTH day, so a stall is a genuine pipeline fault;
          surfaced via WARN log).
        - pre/after-hrs -> data_flow='stalled_offhours_rest_dry' (EXPECTED:
          pricehistory is dry off-hours; the real fix is the CHART_EQUITY
          streamer; surfaced via INFO log).

        Both map to heartbeat status 'degraded' (not a literal 'unhealthy':
        HeartbeatPayload.status is a shared Literal the control-plane parses
        strictly, so a v2-only deploy emitting a new value would make older
        consumers drop the heartbeat). 'degraded' is the strongest safe
        status; the data_flow detail carries the RTH-vs-offhours severity.
        """
        now = datetime.fromtimestamp(now_ms / 1000.0, UTC)
        session = self._market_session(now)
        secs_since_bar = (
            (now_ms - self._last_bar_processed_at_ms) / 1000.0
            if self._last_bar_processed_at_ms
            else None
        )
        last_quote_ms = max(self._last_quote_at_ms.values(), default=0)
        secs_since_quote = (
            (now_ms - last_quote_ms) / 1000.0 if last_quote_ms else None
        )
        quotes_live = (
            secs_since_quote is not None
            and secs_since_quote <= QUOTE_LIVE_THRESHOLD_SECS
        )
        bars_flowing = (
            secs_since_bar is not None
            and secs_since_bar <= DATA_STALL_THRESHOLD_SECS
        )
        uptime_secs = (now_ms - self._started_at_ms) / 1000.0

        if not self.enabled:
            status, flow = "degraded", "disabled"
        elif not self._watchlist:
            status, flow = "healthy", "idle_no_watchlist"
        elif bars_flowing:
            status, flow = "healthy", "flowing"
        elif uptime_secs < WATCHDOG_STARTUP_GRACE_SECS:
            status, flow = "healthy", "warming_up"
        elif not quotes_live:
            # Market not actively trading (closed / holiday / thin) — no
            # bars expected; not a pipeline fault.
            status, flow = "healthy", "idle_market_quiet"
        elif session == "regular":
            status, flow = "degraded", "stalled_rth"
        else:
            status, flow = "degraded", "stalled_offhours_rest_dry"

        detail = {
            "market_session": session,
            "data_flow": flow,
            "secs_since_last_bar": (
                f"{secs_since_bar:.0f}" if secs_since_bar is not None else "none"
            ),
            "secs_since_last_quote": (
                f"{secs_since_quote:.0f}" if secs_since_quote is not None else "none"
            ),
            "quotes_live": str(quotes_live).lower(),
            "rest_empty_streak_max": str(
                self.rest_client.max_consecutive_empty() if self.rest_client else 0
            ),
        }
        return status, detail

    def _log_data_flow_transition(self, detail: dict[str, str]) -> None:
        """Throttled logging on data-flow state change. WARN for RTH stalls
        (actionable pipeline fault), INFO for expected off-hours dryness and
        recovery."""
        flow = detail.get("data_flow", "")
        if flow == self._last_data_flow:
            return
        prev = self._last_data_flow
        self._last_data_flow = flow
        if flow == "stalled_rth":
            logger.warning(
                "[V2-DATA-STALL] quotes live but NO bars processed in %ss during "
                "regular hours — REST pricehistory pipeline is starved "
                "(rest_empty_streak_max=%s, watchlist=%d). Genuine fault: "
                "investigate the REST source.",
                detail.get("secs_since_last_bar"),
                detail.get("rest_empty_streak_max"),
                len(self._watchlist),
            )
        elif flow == "stalled_offhours_rest_dry":
            logger.info(
                "[V2-DATA-DRY] no REST bars in %ss (session=%s) — EXPECTED: "
                "Schwab pricehistory does not serve same-day pre/after-hours "
                "minutes. Warmup seeds from the last session; live pre-market "
                "bars require the CHART_EQUITY streamer.",
                detail.get("secs_since_last_bar"),
                detail.get("market_session"),
            )
        elif flow == "flowing" and prev in {
            "stalled_rth",
            "stalled_offhours_rest_dry",
        }:
            logger.info(
                "[V2-DATA-RECOVERED] bar flow resumed (session=%s)",
                detail.get("market_session"),
            )

    async def _publish_heartbeat(self, status: str | None = None) -> None:
        """Publish a heartbeat. When `status` is None (the periodic path),
        the data-flow watchdog derives it; explicit values are used as-is
        for lifecycle events ('starting' / 'stopping'). Either way the
        watchdog detail fields are attached and transitions are logged.
        """
        if self.redis is None:
            return
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        watchdog_status, watchdog_detail = self._evaluate_data_flow(now_ms)
        effective_status = status or watchdog_status
        self._log_data_flow_transition(watchdog_detail)
        if status is None:
            # Keep the dashboard bot-page health (data_health) in sync with
            # the derived heartbeat status.
            self._data_health["status"] = watchdog_status
        details = {
            "enabled": str(self.enabled).lower(),
            "strategy_code": STRATEGY_CODE,
            "rest_configured": str(
                bool(self.rest_client and self.rest_client.configured)
            ).lower(),
            "streamer_enabled": str(self.streamer_enabled).lower(),
            "streamer_connected": str(
                bool(self.streamer and self.streamer.connected)
            ).lower(),
            "watchlist_size": str(len(self._watchlist)),
            "warmed_size": str(len(self._rest_warmup_done)),
            "bars_processed": str(sum(self._bar_counts.values())),
            "rest_bars_gated_total": str(self._rest_bars_gated),
            "rest_bars_gap_fill_total": str(self._rest_bars_gap_fill),
            "tick_capture": str(self.tick_writer is not None).lower(),
            **(
                {
                    "ticks_written": str(self.tick_writer.stats()["ticks_written"]),
                    "ticks_buffered": str(self.tick_writer.stats()["buffered"]),
                    "ticks_dropped": str(self.tick_writer.stats()["dropped"]),
                }
                if self.tick_writer is not None
                else {}
            ),
            **watchdog_detail,
            # SPOF Workstream A (v2): dedicated loop-resilience health, alongside
            # data_flow — NOT folded into the shared status Literal.
            **self._loop_health.details(),
        }
        event = HeartbeatEvent(
            source_service=SERVICE_NAME,
            payload=HeartbeatPayload(
                service_name=SERVICE_NAME,
                instance_name=SERVICE_NAME,
                status=effective_status,  # type: ignore[arg-type]
                details=details,
            ),
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "heartbeats"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_heartbeat_stream_maxlen,
            approximate=True,
        )

    async def _heartbeat_loop(self) -> None:
        interval = max(5, int(self.settings.service_heartbeat_interval_seconds))
        await run_resilient_loop(
            stop_event=self._stop_event,
            tracker=self._loop_health,
            name="heartbeat",
            # status=None -> data-flow watchdog derives healthy/degraded.
            iteration=self._publish_heartbeat,
            backoff_secs=self._loop_backoff_secs,
            logger=logger,
            idle=lambda: sleep_or_stop(self._stop_event, interval),
        )

    async def _state_publish_loop(self) -> None:
        """Publish StrategyBotStatePayload to strategy-state-isolated stream
        so the dashboard renders the v2 bot like any other.
        """
        await run_resilient_loop(
            stop_event=self._stop_event,
            tracker=self._loop_health,
            name="state_publish",
            iteration=self._publish_bot_state,
            backoff_secs=self._loop_backoff_secs,
            logger=logger,
            idle=lambda: sleep_or_stop(self._stop_event, STATE_PUBLISH_INTERVAL_SECONDS),
        )

    async def _task_liveness_loop(self) -> None:
        """SPOF Workstream A (v2): watch the other tasks. run() does NOT await the
        tasks (it waits on _stop_event), so a task that ends unexpectedly would be
        silent while this service keeps heartbeating — v2's signature risk. Detect
        it and surface loudly via loop_health=degraded-persistent + [V2-TASK-DIED]
        (the per-task backstop should make this impossible; this is belt-and-
        suspenders for a death the backstop doesn't see — e.g. a BaseException)."""
        interval = max(
            1.0,
            float(getattr(self.settings, "strategy_schwab_1m_v2_task_liveness_check_interval_seconds", 15.0) or 15.0),
        )
        while not self._stop_event.is_set():
            await sleep_or_stop(self._stop_event, interval)
            if self._stop_event.is_set():
                break
            for name, task in self._tasks.items():
                if name == "liveness" or not task.done():
                    continue
                exc: BaseException | None = None
                if not task.cancelled():
                    try:
                        exc = task.exception()
                    except Exception:  # pragma: no cover - defensive
                        exc = None
                self._loop_health.mark_task_died(name, exc=exc)

    async def _publish_bot_state(self) -> None:
        if self.redis is None:
            return
        payload = StrategyBotStatePayload(
            strategy_code=STRATEGY_CODE,
            account_name=self.settings.strategy_schwab_1m_v2_account_name,
            watchlist=sorted(self._watchlist),
            prewarm_symbols=[],
            data_health=dict(self._data_health),
            retention_states=[],
            positions=[],
            pending_open_symbols=[],
            pending_close_symbols=[],
            pending_scale_levels=[],
            daily_pnl=0.0,
            closed_today=[],
            recent_decisions=[],
            indicator_snapshots=[],
            bar_counts=dict(self._bar_counts),
            last_tick_at=dict(self._last_tick_at),
        )
        event = IsolatedBotStateEvent(source_service=SERVICE_NAME, payload=payload)
        await self.redis.xadd(
            self._isolated_state_stream,
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_strategy_state_isolated_stream_maxlen,
            approximate=True,
        )

    async def _position_poll_loop(self) -> None:
        """Poll virtual_positions + in-flight trade_intents for v2's broker
        account every 5s; feed results into the strategy's per-symbol state.

        The strategy's update_position() detects the True→False transition
        (OMS closed our position) and arms the cooldown, so we never
        re-enter on the same bar an exit fired on.

        In-flight intents (status NOT IN filled/rejected/cancelled) also
        count as "in position" — covers the gap between intent emission
        and virtual_positions row creation, preventing duplicate opens.
        """
        await run_resilient_loop(
            stop_event=self._stop_event,
            tracker=self._loop_health,
            name="position_poll",
            iteration=self._position_poll_pass,
            backoff_secs=self._loop_backoff_secs,
            logger=logger,
            idle=lambda: sleep_or_stop(self._stop_event, POSITION_POLL_INTERVAL_SECONDS),
        )

    async def _position_poll_pass(self) -> None:
        positions = await asyncio.to_thread(self._fetch_open_positions)
        if positions is None:
            return
        tracked = set(self._watchlist) | set(self.strategy._symbol_states.keys())
        for symbol in tracked:
            qty = positions.get(symbol, 0)
            self.strategy.update_position(symbol, qty)

    def _fetch_open_positions(self) -> dict[str, int]:
        """SQL: virtual_positions(qty>0) ∪ in-flight trade_intents(open)
        for the v2 broker account, keyed by symbol. Quantity is the max
        across sources (a conservative "do we own this" signal).
        """
        if self.session_factory is None:
            return {}
        account_name = self.settings.strategy_schwab_1m_v2_account_name
        positions: dict[str, int] = {}
        try:
            with self.session_factory() as session:
                broker = session.scalar(
                    select(BrokerAccount).where(BrokerAccount.name == account_name)
                )
                if broker is None:
                    return positions
                # Virtual positions = mai-tai's authoritative view of what
                # we own (synchronized by OMS on fills).
                for vp in session.scalars(
                    select(VirtualPosition).where(
                        VirtualPosition.broker_account_id == broker.id,
                        VirtualPosition.quantity > 0,
                    )
                ).all():
                    symbol = str(vp.symbol or "").upper()
                    if symbol:
                        positions[symbol] = max(
                            positions.get(symbol, 0), int(vp.quantity)
                        )
                # In-flight open intents — block re-entry until OMS resolves
                # the prior intent (filled / rejected / cancelled).
                strategy = session.scalar(
                    select(Strategy).where(Strategy.code == "schwab_1m_v2")
                )
                if strategy is not None:
                    for ti in session.scalars(
                        select(TradeIntent).where(
                            TradeIntent.strategy_id == strategy.id,
                            TradeIntent.intent_type == "open",
                            TradeIntent.status.notin_(
                                INFLIGHT_INTENT_STATUSES_TERMINAL
                            ),
                        )
                    ).all():
                        symbol = str(ti.symbol or "").upper()
                        if symbol:
                            qty = int(ti.quantity or 0) or 1
                            positions[symbol] = max(positions.get(symbol, 0), qty)
        except Exception:
            logger.exception("schwab_1m_v2 _fetch_open_positions failed")
        return positions

    async def _scanner_consumer_loop(self) -> None:
        """Seed from the latest existing strategy-state snapshot, then tail
        for new ones. The seed step is critical on cold-start because
        strategy-engine publishes its snapshot only on bar / intent events,
        which can be minutes apart in pre-market; without the seed, the v2
        bot's watchlist stays empty until the next downstream event fires.
        """
        assert self.redis is not None
        assert self.rest_client is not None
        max_watchlist = max(
            1, int(self.settings.strategy_schwab_1m_v2_max_watchlist_size)
        )

        # Step 1: seed from the latest snapshot already in the stream.
        try:
            seed = await self.redis.xrevrange(
                self._strategy_state_stream, count=1
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("schwab_1m_v2 scanner seed xrevrange failed: %s", exc)
            seed = []
        for entry_id, data in seed:
            self._strategy_state_last_id = entry_id
            self._apply_strategy_state_event(data, max_watchlist=max_watchlist)
        await self._sync_gateway_subscription()  # slice-2: register v2 symbols (gated/inert)

        # Step 2: tail for new snapshots (backstopped — xread + apply contained).
        await run_resilient_loop(
            stop_event=self._stop_event,
            tracker=self._loop_health,
            name="scanner",
            iteration=lambda: self._scanner_tail_pass(max_watchlist),
            backoff_secs=self._loop_backoff_secs,
            logger=logger,
        )

    async def _scanner_tail_pass(self, max_watchlist: int) -> None:
        assert self.redis is not None
        response = await self.redis.xread(
            streams={self._strategy_state_stream: self._strategy_state_last_id},
            count=10,
            block=5_000,
        )
        if not response:
            return
        for _stream_key, entries in response:
            for entry_id, data in entries:
                self._strategy_state_last_id = entry_id
                self._apply_strategy_state_event(data, max_watchlist=max_watchlist)
        await self._sync_gateway_subscription()  # slice-2: keep v2 gateway subs current (gated/inert)

    async def _sync_gateway_subscription(self) -> None:
        """Track-2 Phase-2 Slice-2: register v2's watchlist as a market-data
        gateway subscription CONSUMER (`consumer_name=SERVICE_NAME`), so the
        gateway streams quotes for v2's symbols and the OMS quote cache covers
        them — a GUARANTEE the in-practice overlap doesn't give (the gateway
        otherwise subscribes only the momentum bots' retained symbols, which can
        diverge from v2's broader scanner pool). Mirrors the strategy-engine's
        `_sync_market_data_subscriptions` (mode=replace, debounced).

        Gated OFF by default (`oms_v2_exit_management_enabled`) → INERT: v2
        publishes nothing, registers no consumer, streams no extra symbols —
        identical to today (the OMS doesn't use v2 quotes until slice 3 anyway).
        """
        # Register when the dedicated coverage flag OR the exit flag is on. Decoupled
        # so coverage can be deployed + verified live before exits arm; the OR ensures
        # exits can never run without the OMS feed covering v2's symbols.
        register = bool(getattr(self.settings, "strategy_schwab_1m_v2_gateway_register_enabled", False))
        exits = bool(getattr(self.settings, "oms_v2_exit_management_enabled", False))
        if not (register or exits):
            return
        if self.redis is None:
            return
        desired = sorted(self._watchlist)
        if desired == self._last_gateway_symbols:
            return  # debounce — only publish on change
        self._last_gateway_symbols = desired
        event = MarketDataSubscriptionEvent(
            source_service=SERVICE_NAME,
            payload=MarketDataSubscriptionPayload(
                consumer_name=SERVICE_NAME,
                mode="replace",
                symbols=desired,
            ),
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "market-data-subscriptions"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_market_data_subscription_stream_maxlen,
            approximate=True,
        )
        logger.info(
            "[V2-GATEWAY-SUBSCRIBE] consumer=%s symbols=%d", SERVICE_NAME, len(desired)
        )

    def _apply_strategy_state_event(
        self, data: object, *, max_watchlist: int
    ) -> None:
        raw = data.get("data") if isinstance(data, dict) else None
        if not isinstance(raw, str):
            return
        try:
            event = StrategyStateSnapshotEvent.model_validate_json(raw)
        except Exception:  # noqa: BLE001
            return
        if not self._strategy_state_event_is_current(event):
            logger.info(
                "schwab_1m_v2 ignoring stale strategy-state snapshot "
                "produced_at=%s current_session_start=%s",
                event.produced_at.isoformat(),
                _current_scanner_session_start_utc().isoformat(),
            )
            return
        symbols = self._extract_confirmed_symbols(event)
        protected = self._protected_symbols()
        selected = set(symbols[:max_watchlist]) | protected
        if selected == self._watchlist:
            return
        self._watchlist = selected
        # Drop warmup state for symbols that left the watchlist. If they
        # re-join later, REST needs to refetch the batch and the
        # buffer-and-replay path runs again.
        self._rest_warmup_done &= selected
        # Drop any buffered streamer bars for symbols no longer on the
        # watchlist — the streamer will be told to UNSUBS them below,
        # and stale buffers would replay after a re-join with bars
        # older than whatever the next warmup delivers.
        if self._streamer_pending:
            self._streamer_pending = {
                sym: bars
                for sym, bars in self._streamer_pending.items()
                if sym in selected
            }
        if self.rest_client is not None:
            self.rest_client.set_desired_symbols(selected)
        if self.streamer is not None:
            # Streamer subscribes to the FULL watchlist immediately. The
            # subscribe/evaluate decoupling lives in
            # `_handle_bar_from_streamer` (buffer until REST warmup) +
            # `_handle_bar_from_rest` (drain buffer on warmup), so
            # subscription no longer waits on `_rest_warmup_done`.
            # Rationale: keeping symbols out of the SUBS set until they
            # warmed caused Schwab to close the empty session within
            # ~3s of LOGIN-OK on cold start, producing a reconnect
            # loop that delayed first-SUBS rather than protecting
            # ordering. See docs/session-handoff-schwab-1m-v2.md
            # 2026-05-23 entry for the race analysis.
            self.streamer.set_desired_symbols(selected)
        logger.info(
            "schwab_1m_v2 watchlist updated count=%d sample=%s warmed=%d",
            len(selected),
            ",".join(sorted(selected)[:5]),
            len(self._rest_warmup_done),
        )

    @staticmethod
    def _extract_confirmed_symbols(event: StrategyStateSnapshotEvent) -> list[str]:
        payload = event.payload
        candidates: list[dict | str] = []
        candidates.extend(payload.top_confirmed)
        candidates.extend(payload.all_confirmed)
        candidates.extend(payload.watchlist)
        symbols: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if isinstance(item, dict):
                sym = str(item.get("symbol") or item.get("ticker") or "").strip().upper()
            elif isinstance(item, str):
                sym = item.strip().upper()
            else:
                sym = ""
            if sym and sym not in seen:
                symbols.append(sym)
                seen.add(sym)
        return symbols

    @staticmethod
    def _strategy_state_event_is_current(event: StrategyStateSnapshotEvent) -> bool:
        produced_at = event.produced_at
        if produced_at.tzinfo is None:
            produced_at = produced_at.replace(tzinfo=UTC)
        return produced_at.astimezone(UTC) >= _current_scanner_session_start_utc()

    def _protected_symbols(self) -> set[str]:
        protected = {
            symbol
            for symbol, state in self.strategy._symbol_states.items()
            if state.position_qty > 0
        }
        protected.update(self._fetch_open_positions())
        return protected

    async def _handle_bar_from_rest(self, symbol: str, bar: ChartBar) -> None:
        """REST callback. C3 + buffer-drain routing:

        - If REST has caught up to live (bar age <
          REST_WARMUP_FRESH_THRESHOLD_SECS) mark this symbol's warmup
          as done. After feeding the current REST bar, drain any
          streamer bars buffered during warmup in
          `_handle_bar_from_streamer`.
        - If the streamer is connected AND has already delivered a bar
          at this `bar.timestamp_ms` or later, skip the strategy feed
          (C3: streamer is signal source of truth when healthy; REST
          is warmup + gap fill only).
        - Otherwise forward to `_handle_bar` (REST is the only live
          feed, or this is a genuine gap fill bar that the streamer
          missed during a disconnect window).
        """
        if self._loop_fault_injection_remaining > 0:
            # SPOF Workstream A (v2) controlled survival test (default OFF).
            # Raises on the E1 callback path — v2's real remaining escape — so an
            # operator can prove the bar loop survives + escalates in a safe
            # window. Self-clears after N. The rest client's bar-loop backstop
            # contains this and records a "bar_loop" failure.
            self._loop_fault_injection_remaining -= 1
            raise RuntimeError(
                "[FAULT-INJECTION] simulated schwab_1m_v2 bar-handling failure "
                "(MAI_TAI_STRATEGY_SCHWAB_1M_V2_LOOP_FAULT_INJECTION_COUNT) — "
                "SPOF Workstream A v2 controlled survival test"
            )
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        bar_age_secs = (now_ms - bar.timestamp_ms) / 1000.0
        just_warmed = False
        if (
            bar_age_secs <= REST_WARMUP_FRESH_THRESHOLD_SECS
            and symbol not in self._rest_warmup_done
        ):
            self._rest_warmup_done.add(symbol)
            just_warmed = True
            logger.info(
                "[V2-REST-WARMED] schwab_v2 REST warmup complete for %s "
                "(warmed=%d/%d)",
                symbol,
                len(self._rest_warmup_done),
                len(self._watchlist),
            )

        if self._should_skip_rest_strategy_feed(symbol, bar):
            self._rest_bars_gated += 1
        else:
            # When streamer is connected but didn't pre-empt this bar,
            # count it as gap-fill (something streamer didn't deliver
            # — disconnect, missed bucket, or symbol not yet warmed).
            if self.streamer is not None and self.streamer.connected:
                self._rest_bars_gap_fill += 1
            await self._handle_bar(symbol, bar)

        # Drain buffered streamer bars AFTER the current REST bar is
        # fed, so the deque tail reflects the latest REST bar before
        # any newer streamer bars are appended on top.
        if just_warmed:
            await self._drain_streamer_pending(symbol)

    async def _handle_bar_from_streamer(self, symbol: str, bar: ChartBar) -> None:
        """Streamer callback.

        Before REST warmup completes for this symbol, streamer bars are
        buffered in `_streamer_pending[symbol]` and replayed at warmup
        completion. After warmup, bars are fed directly to the
        strategy. C3 keeps REST out of the way once the streamer is
        the signal source of truth.
        """
        if symbol not in self._rest_warmup_done:
            pending = self._streamer_pending.setdefault(symbol, [])
            if len(pending) >= STREAMER_PENDING_BARS_MAX_PER_SYMBOL:
                logger.warning(
                    "[V2-STREAMER-PENDING-FULL] dropping oldest pending bar "
                    "for %s (cap=%d, REST warmup still in flight)",
                    symbol,
                    STREAMER_PENDING_BARS_MAX_PER_SYMBOL,
                )
                pending.pop(0)
            pending.append(bar)
            return
        await self._handle_bar(symbol, bar)

    async def _drain_streamer_pending(self, symbol: str) -> None:
        """Replay buffered streamer bars for `symbol` after REST warmup
        completes. Bars whose timestamp is `>= state.bars[-1].timestamp_ms`
        are replayed in ascending order; strictly-older bars would
        corrupt the append-only deque and are dropped (logged for
        observability).

        Equal-timestamp bars are explicitly INCLUDED in the replay
        rather than dropped: REST's Price History endpoint applies a
        60s in-flight cutoff for the most recent bar, so the streamer's
        push-at-minute-close copy of the same bucket can carry more
        complete OHLC + volume. `SchwabV2Strategy.on_bar` handles same-
        timestamp arrivals via update-in-place (state.bars[-1] = ohlcv),
        so the streamer's copy wins without disturbing deque order.
        """
        pending = self._streamer_pending.pop(symbol, None)
        if not pending:
            return
        state = self.strategy.watchlist_state(symbol)
        latest_ts = state.bars[-1].timestamp_ms if state.bars else 0
        fresh = sorted(
            (b for b in pending if b.timestamp_ms >= latest_ts),
            key=lambda b: b.timestamp_ms,
        )
        dropped = len(pending) - len(fresh)
        if dropped:
            logger.info(
                "schwab_1m_v2 streamer pending: dropped %d stale buffered "
                "bars for %s (latest deque ts=%d)",
                dropped,
                symbol,
                latest_ts,
            )
        for buffered in fresh:
            await self._handle_bar(symbol, buffered)
        if fresh:
            logger.info(
                "[V2-STREAMER-DRAIN] replayed %d buffered bars for %s "
                "after warmup",
                len(fresh),
                symbol,
            )

    def _should_skip_rest_strategy_feed(self, symbol: str, bar: ChartBar) -> bool:
        """C3 gating: when streamer is connected and has already
        delivered a bar at the same timestamp (or later) for this
        symbol, REST's same-bucket fetch is redundant. Returning True
        suppresses the strategy feed; the bar is still consumed from
        the REST loop (idempotent on REST's internal cursor).

        When streamer is disconnected OR has not yet delivered any bar
        for this symbol (e.g. just subscribed, waiting for the next
        minute), REST is the only feed and must pass through.
        """
        if self.streamer is None or not self.streamer.connected:
            return False
        streamer_last_ts = self.streamer.last_bar_ts_ms(symbol)
        if streamer_last_ts <= 0:
            return False
        return bar.timestamp_ms <= streamer_last_ts

    async def _handle_bar(self, symbol: str, bar: ChartBar) -> None:
        now_et = _format_eastern(datetime.now(UTC))
        self._last_tick_at[symbol] = now_et
        self._last_bar_at[symbol] = now_et
        self._bar_counts[symbol] = self._bar_counts.get(symbol, 0) + 1

        # Only DB-persist bars within the freshness window. The cold-start
        # warmup batch (up to ~500 historical bars per symbol) was already
        # persisted by a prior service instance; re-writing them serializes
        # ~5k SQL roundtrips across all symbols and stalls the bar loop.
        # In-memory indicator state still consumes EVERY bar via strategy.on_bar.
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        # Watchdog: mark that the bar pipeline produced something (any
        # symbol, warmup or live). NOTE: warmup bars update this too, so
        # right after a warmup batch lands, bars_flowing reads True for up
        # to DATA_STALL_THRESHOLD_SECS even if no *live* bar has arrived yet
        # — the stall signal lags warmup completion by that window. Harmless
        # (self-corrects within the window), but don't read "flowing"
        # immediately post-warmup as proof of a live feed. A stalled value
        # during RTH with live quotes is the starvation signature surfaced.
        self._last_bar_processed_at_ms = now_ms
        bar_age_secs = (now_ms - bar.timestamp_ms) / 1000.0
        if bar_age_secs <= PERSIST_BAR_AGE_LIMIT_SECONDS:
            await asyncio.to_thread(self._persist_bar, symbol, bar)

        try:
            draft = self.strategy.on_bar(symbol, bar)
        except Exception:
            logger.exception("schwab_1m_v2 on_bar failed for %s", symbol)
            return
        await self._maybe_emit(draft)

    async def _handle_quote(self, symbol: str, quote: Quote) -> None:
        now = datetime.now(UTC)
        self._last_tick_at[symbol] = _format_eastern(now)
        # Watchdog: quotes flow whenever the market is actually trading
        # (holiday-safe), so this is the discriminator for whether a bar
        # stall is a real fault vs a quiet/closed market.
        self._last_quote_at_ms[symbol] = int(now.timestamp() * 1000)
        try:
            draft = self.strategy.on_quote(symbol, quote)
        except Exception:
            logger.exception("schwab_1m_v2 on_quote failed for %s", symbol)
            return
        await self._maybe_emit(draft)

    def _persist_bar(self, symbol: str, bar: ChartBar) -> None:
        """Atomic upsert into strategy_bar_history on
        (strategy_code, symbol, interval_secs, bar_time).

        Uses INSERT ... ON CONFLICT DO UPDATE so concurrent REST + streamer
        writes of the SAME bar can't collide. The prior SELECT-then-INSERT was
        non-atomic: when both writers passed the SELECT (saw no row) before
        either INSERTed, the second INSERT raised UniqueViolation — the
        GLXG-class dup at the REST/streamer seam. On conflict only the OHLCV is
        refreshed (decision_* / position_* left untouched), exactly matching the
        previous UPDATE branch. Mirrors the strategy-engine bar shape so the
        dashboard decision-tape query treats v2 bars identically.
        """
        if self.session_factory is None:
            return

        volume = int(bar.volume or 0)
        # No trade_count from Schwab Price History; synthesize so the
        # vol=0+tc=0 placeholder filter behaves correctly downstream.
        trade_count = 1 if volume > 0 else 0
        if volume == 0 and trade_count == 0:
            return

        bar_time = datetime.fromtimestamp(bar.timestamp_ms / 1000.0, UTC)
        ohlcv = {
            "open_price": Decimal(str(bar.open)),
            "high_price": Decimal(str(bar.high)),
            "low_price": Decimal(str(bar.low)),
            "close_price": Decimal(str(bar.close)),
            "volume": volume,
            "trade_count": trade_count,
        }
        stmt = (
            pg_insert(StrategyBarHistory)
            .values(
                id=uuid4(),
                strategy_code=STRATEGY_CODE,
                symbol=symbol,
                interval_secs=INTERVAL_SECS,
                bar_time=bar_time,
                position_state="flat",
                indicators_json={},
                **ohlcv,
            )
            .on_conflict_do_update(
                constraint="uq_strategy_bar_history_strategy_symbol_interval_time",
                set_=ohlcv,
            )
        )
        try:
            with self.session_factory() as session:
                session.execute(stmt)
                session.commit()
        except Exception:
            logger.exception(
                "schwab_1m_v2 failed to persist bar history for %s @ %s",
                symbol,
                bar_time,
            )

    async def _maybe_emit(self, draft) -> None:  # type: ignore[no-untyped-def]
        if draft is None:
            return
        if self.intent_emitter is None:
            logger.warning("schwab_1m_v2 intent dropped — emitter not initialized")
            return
        try:
            await self.intent_emitter.emit(draft)
        except Exception:
            logger.exception("schwab_1m_v2 emit failed")


async def main() -> None:
    service = SchwabV2BotService()
    await service.run()


def run() -> None:
    asyncio.run(main())


# Re-exports for tests / introspection
__all__ = ["SchwabV2BotService", "SERVICE_NAME", "STRATEGY_CODE", "main", "run"]
