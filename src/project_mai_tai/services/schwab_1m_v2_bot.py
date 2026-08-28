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
import os
import signal
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as time_cls  # `time` the module is already imported above
from decimal import Decimal
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    Fill,
    OmsManagedPosition,
    Strategy,
    StrategyBarHistory,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.db.session import build_timed_session_factory
from project_mai_tai.fanout_outcome_consumer import (
    FanoutOutcomeJournal,
    identity_from_metadata,
)
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.oms.store import OmsStore
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
from project_mai_tai.strategy_core.time_utils import (
    US_MARKET_HOLIDAYS,
    session_day_eastern_str,
)
from project_mai_tai.strategy_core.order_routing import (
    extended_hours_session,
)
from project_mai_tai.strategy_core import entry_gate
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    PostCloseEntryRelease,
    SERVICE_NAME,
    STRATEGY_CODE,
    SchwabV2IntentEmitter,
    SchwabV2Strategy,
    session_start_ts_ms,
)

logger = logging.getLogger(__name__)

INTERVAL_SECS = 60
# Fix (b): number of persisted 60s bars to replay into the strategy buffer on a
# symbol's cold-start. >= the 135-bar MACD settling with headroom, and bounded so
# the seed + early live bars sit comfortably under the strategy's deque(maxlen=300).
DB_SEED_BAR_LIMIT = 250
# ⛔⭐⭐ THE SEED IS BOUNDED BY COUNT, SO IT MUST ALSO BE BOUNDED BY CONTINUITY (2026-08-18, P0).
# `DB_SEED_BAR_LIMIT` takes N ROWS. On a thinly-traded name that reaches back as far as the rows do:
# CAST had 38 bars on 08-18 and a 61-day hole behind them, so 212 of its 250 seeded bars came from
# 06-18 and the strategy armed at flip_level 7.99 while CAST traded 1.04-1.28 (6.7x off).
#
# ⛔⭐⭐ THE VARIABLE IS MISSED TRADING SESSIONS, NOT WALL-CLOCK. Measured over 256k gaps in
# `strategy_bar_history`, the median price discontinuity across a gap is:
#     same session (contiguous)      255,243 gaps    0.7%      <- fine
#     0 sessions missed (a CLOSURE)      345 gaps   10.2%      <- LEGITIMATE, must stay seeded
#     1 session missed                    75 gaps   26.2%      <- 2.6x jump
#     2..10 sessions missed              ~190 gaps   16-32%    <- FLAT, no further structure
# ⛔ Wall-clock is the WRONG variable: a weekend is a legitimate closure (0 sessions missed) and a
# price cut would truncate every Monday, because penny-stock weekend gaps genuinely run ~10-18%.
# Duration is equally wrong: beyond one missed session the discontinuity stops growing, so a 2-day
# absence is as dangerous as a 60-day one. An earlier 4-DAY threshold let 110 gaps through whose
# median discontinuity was 18%.
#
# The governing principle: SEED ACROSS A MARKET CLOSURE, REFUSE TO SEED ACROSS AN ABSENCE.
# Insufficient history means NO SIGNAL, NOT OLD SIGNAL -- the same shape as "an empty list from a
# failed call is not a flat account".
DB_SEED_MAX_MISSED_SESSIONS = 0
FANOUT_OUTCOME_POLL_INTERVAL_SECONDS = 5.0
# Gaps below this never need a session lookup (>99.9% of all gaps), so the calendar query is rare.
_DB_SEED_GAP_PROBE_MIN = timedelta(hours=2)
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
# Full-closure holidays now live in `strategy_core.time_utils` (shared with the
# OMS fillable-session gate). Alias keeps existing `_US_MARKET_HOLIDAYS` usages.
_US_MARKET_HOLIDAYS: frozenset[date] = US_MARKET_HOLIDAYS

# Operator trading window (ET): v2 only ENTERS within [start, end). Outside it
# — before 7 AM, at/after 4 PM, weekends, and full-closure holidays —
# `_maybe_emit` drops "open" intents. Exits (cw_flip + OMS-managed) are governed
# separately (the OMS fillable-session gate). The canonical default bounds
# (07:00–16:00 ET) now live in `strategy_core.entry_gate` (shared with the
# backtest replay, Decision 1 of the replay-engine design) and are resolved via
# `entry_gate.resolve_entry_window(settings)`, so the live window can never drift
# from the replay's. Overridable via settings of the same name.


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
        # Dual-broker FAN-OUT: second emitter bound to the Webull account (built in run() only when
        # the fan-out flag is on AND the Webull account is set). None => no fan-out (byte-identical).
        self.webull_intent_emitter: SchwabV2IntentEmitter | None = None
        self.session_factory: sessionmaker[Session] | None = session_factory
        self.fanout_identity_store: FanoutSegmentIdentityStore | None = None
        self.fanout_outcome_journal: FanoutOutcomeJournal | None = None
        self._fanout_outcome_evaluations = 0
        self._stop_event = asyncio.Event()
        self._strategy_state_stream = stream_name(
            self.settings.redis_stream_prefix, "strategy-state"
        )
        self._isolated_state_stream = stream_name(
            self.settings.redis_stream_prefix, "strategy-state-isolated"
        )
        self._strategy_state_last_id = "$"
        self._watchlist: set[str] = set()
        # P1 boot ordering: `_state_publish_loop` and `_scanner_consumer_loop` start together.
        # Until the scanner has applied one current snapshot (including its synchronous DB-seed
        # replays), an empty `cw_armed_segments()` means "nothing loaded yet", not "restoration
        # found nothing dangerous". The boot hold may release only after a NON-EMPTY selected
        # population is restored. Once True it is a one-way boot latch: later scanner refreshes
        # must not suppress every symbol mid-session by turning boot incomplete again.
        self._boot_state_restoration_complete = False
        # symbol -> epoch ms at which it JOINED the watchlist. Absent => present since boot, which
        # falls back to `_boot_ms` (see `_watch_start_for`), preserving pre-2026-07-30 behaviour for
        # every symbol that was already being watched.
        self._watch_start_ms: dict[str, int] = {}
        # ⛔⭐⭐ EXIT COVERAGE — symbols we HOLD, kept subscribed even after the scanner drops them.
        # EXIT-ONLY: this set feeds market-data subscriptions so the OMS exit ladder keeps receiving
        # quotes. It must NEVER be unioned into an ENTRY decision — see
        # docs/design/held-symbol-exit-coverage.md §2. Held-symbol coverage exists to CLOSE
        # positions, never to OPEN them; entering a name the scanner dropped is a fresh decision
        # that contradicts the scanner's purpose.
        self._exit_coverage: set[str] = set()
        # Symbols Schwab refused to OPEN today (cached <=60s; see
        # `_schwab_ineligible_symbols`). Evicted from the watchlist so v2 stops
        # emitting intents for names the broker already bounced.
        self._schwab_ineligible_cache: set[str] = set()
        self._schwab_ineligible_loaded_monotonic: float | None = None
        # Dual-broker FAN-OUT: symbols Webull refused to OPEN today (cached <=60s; see
        # `_webull_ineligible_symbols`). Used to skip the Webull leg + (with schwab) to evict a name
        # only when BOTH brokers reject it. Empty unless fan-out is on.
        self._webull_ineligible_cache: set[str] = set()
        self._webull_ineligible_loaded_monotonic: float | None = None
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
        # Last 04:00-ET anchor the time-driven session roll reported on. 0 => the first sweep
        # after boot logs, which is the proof-of-life line: "the sweep is running and found N".
        self._session_roll_last_anchor: int = 0
        # B20: ET date on which the 16:00 entry-window arm release last ran (once per boundary).
        self._entry_window_arm_release_day: str = ""
        # Per-symbol queue of streamer bars received before this symbol's
        # REST warmup completed. Drained in `_handle_bar_from_rest`
        # when the symbol crosses into `_rest_warmup_done`, replaying
        # in timestamp order only those bars strictly newer than the
        # latest bar already in `state.bars`.
        self._streamer_pending: dict[str, list[ChartBar]] = {}
        # Fix (b): symbols whose strategy bar buffer has been hydrated from
        # `strategy_bar_history` on cold-start (see `_seed_strategy_bars_from_db`).
        # Without this, `state.bars` is live-only after a restart (the C3 dedup
        # gate skips the REST warmup batch once the streamer out-timestamps it),
        # so MACD/VWAP/ATR are blind for ~135 minutes (the line-676 min_bars
        # guard). Seeding clears that blackout. Pruned with the watchlist so a
        # re-added symbol re-seeds.
        self._db_seeded: set[str] = set()
        # ⛔⭐ Counter for [V2-DB-SEED-GAP]. A refusal that only logs per-occurrence cannot be
        # distinguished from a refusal that stopped happening — see the census discipline on
        # `evaluated=0`. Reported on the session roll so a ZERO is a MEASUREMENT, not a silence.
        self._db_seed_gap_truncations: int = 0
        # ⛔⭐⭐ THE CENSUS DENOMINATOR, AND WHY IT IS NOT `len(self._db_seeded)` (P11, 2026-08-20).
        # `_db_seeded` is a DEDUP set, pruned to the live watchlist on every selection pass
        # (`self._db_seeded &= selected`). It is not, and never was, a "since boot" population: at
        # the 04:00 roll the watchlist turns over and the set intersects towards EMPTY while the
        # truncation counter beside it keeps climbing. That printed `truncations=7 of 0 symbols
        # seeded since boot` on 08-20 — a numerator with no denominator, on the one line whose
        # stated purpose is to supply the denominator.
        # ⇒ Count seed EVALUATIONS: monotonic, never pruned, and in the SAME UNIT as the numerator
        #   (one per symbol per seed attempt that actually loaded rows, so a symbol that leaves and
        #   re-joins the watchlist contributes one to each — exactly as it contributes one possible
        #   truncation to each). Same unit is the point: the ratio is only readable if both halves
        #   count the same events.
        self._db_seed_evaluations: int = 0
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
        # Latest quote (bid/ask) per symbol — fed by _handle_quote, read at emit to
        # source the extended-hours limit price (mirrors legacy _resolve_routed_price,
        # which routes entries at the live ask). RTH ignores it (order stays market).
        self._last_quote_by_symbol: dict[str, Quote] = {}
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

    def _configure_fanout_identity_store(self) -> dict[str, int]:
        """Restore the current segment before any market-data task can emit a draft."""

        if self.session_factory is None:
            logger.error(
                "[V2-FANOUT-IDENTITY-RESTORE-FAILED] restored=0 could_not_tell=1 "
                "reason=no_session_factory"
            )
            return {}
        self.fanout_identity_store = FanoutSegmentIdentityStore(self.session_factory)
        try:
            restored_segments = self.fanout_identity_store.restore_active()
        except Exception:  # noqa: BLE001 - identity is observational, never an entry gate
            restored_segments = {}
            logger.exception(
                "[V2-FANOUT-IDENTITY-RESTORE-FAILED] restored=0 could_not_tell=1"
            )
        else:
            logger.info(
                "[V2-FANOUT-IDENTITY-RESTORE] restored=%d could_not_tell=0",
                len(restored_segments),
            )
        self.strategy.configure_fanout_identity_persistence(
            self.fanout_identity_store.record,
            restored_segments,
        )
        return restored_segments

    def _configure_fanout_outcome_journal(self, active_segments: dict[str, int]) -> None:
        """Replay durable outcomes before any market-data task can emit a new leg."""

        if self.session_factory is None:
            logger.error(
                "[V2-FANOUT-OUTCOME-RESTORE] evaluated=0 applied=0 could_not_tell=1 "
                "reason=no_session_factory"
            )
            return
        self.fanout_outcome_journal = FanoutOutcomeJournal(self.session_factory)

        def persist(metadata, symbol: str, outcome: str, reason: str) -> None:  # type: ignore[no-untyped-def]
            journal = self.fanout_outcome_journal
            if journal is None:
                raise RuntimeError("fan-out outcome journal is not configured")
            journal.record(
                metadata=metadata,
                symbol=symbol,
                outcome=outcome,
                evidence_id=f"strategy:{uuid4()}",
                reason=reason,
                event_source="client",
                broker_account_name=str(
                    getattr(self.settings, "strategy_schwab_1m_v2_webull_account_name", "") or ""
                ),
            )

        self.strategy.configure_fanout_outcome_persistence(persist)
        try:
            applied = self.fanout_outcome_journal.bootstrap(
                self.strategy.apply_fanout_outcome,
                active_segments=active_segments,
            )
        except Exception:  # noqa: BLE001 - startup stays in visible-release direction
            logger.exception(
                "[V2-FANOUT-OUTCOME-RESTORE] evaluated=0 applied=0 could_not_tell=1"
            )
            return
        logger.info(
            "[V2-FANOUT-OUTCOME-RESTORE] evaluated=%d applied=%d could_not_tell=0",
            applied,
            applied,
        )

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
        logger.info("schwab_1m_v2 bot starting pid=%d (enabled=%s)", os.getpid(), self.enabled)

        if not self.enabled:
            logger.warning(
                "schwab_1m_v2 disabled: set MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENABLED=true "
                "to activate. Service will heartbeat as degraded and idle."
            )

        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        if self.session_factory is None:
            try:
                self.session_factory = build_timed_session_factory(self.settings, service="schwab_1m_v2", profile="fast")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "schwab_1m_v2 session_factory unavailable, bar persistence "
                    "disabled: %s",
                    exc,
                )
        active_segments = self._configure_fanout_identity_store()
        self._configure_fanout_outcome_journal(active_segments)
        self.intent_emitter = SchwabV2IntentEmitter(
            self.settings,
            self.redis,
            broker_account_name=self.settings.strategy_schwab_1m_v2_account_name,
        )
        # Dual-broker FAN-OUT: build the SECOND emitter bound to the Webull account only when the
        # flag is on AND the account is set. Unset => warn + stay single-leg (never point at nothing).
        if bool(getattr(self.settings, "strategy_schwab_1m_v2_dual_broker_fanout_enabled", False)):
            webull_account = str(
                getattr(self.settings, "strategy_schwab_1m_v2_webull_account_name", "") or ""
            ).strip()
            if webull_account and webull_account != self.settings.strategy_schwab_1m_v2_account_name:
                self.webull_intent_emitter = SchwabV2IntentEmitter(
                    self.settings,
                    self.redis,
                    broker_account_name=webull_account,
                )
                logger.info(
                    "[V2-FANOUT] dual-broker fan-out ENABLED — Webull leg -> account %s",
                    webull_account,
                )
            else:
                logger.warning(
                    "[V2-FANOUT] fan-out flag ON but strategy_schwab_1m_v2_webull_account_name is "
                    "unset/equal-to-primary (%r) — staying single-leg, no Webull leg emitted",
                    webull_account,
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
                    self.session_factory = build_timed_session_factory(self.settings, service="schwab_1m_v2", profile="fast")
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
            if self.fanout_outcome_journal is not None:
                self._tasks["fanout_outcomes"] = asyncio.create_task(
                    self._fanout_outcome_loop()
                )
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
            "fanout_outcome_evaluations": str(self._fanout_outcome_evaluations),
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

    def _cw_boot_hold_check(self) -> None:
        """Boot-hold self-verify (P1.3+P1.4). Runs each state-publish cycle.

        RELEASES entries only after the scanner's initial current snapshot has been fully applied
        AND there are zero reconstructed-uncapped (``dangerous``) segments. Before restoration is
        complete, an empty segment list is absence of data, never evidence of safety. Re-holds and
        warns if a dangerous segment ever appears (a P1.3 miss). Never releases on a timeout: if
        restoration has not completed or dangerous persists, entries stay held. The external
        ``armed_segments_check`` cron pages off the published snapshot. The bar-ts discriminator
        keeps the continuous check safe: a live post-boot flip is never counted dangerous.
        """
        strat = self.strategy
        if not getattr(strat, "_cw_armed_segment_safety_enabled", False):
            return
        segments = strat.cw_armed_segments()
        dangerous = [s for s in segments if s["dangerous"]]
        if not self._boot_state_restoration_complete:
            if not strat._entries_held:
                strat._entries_held = True
            logger.warning(
                "[V2-BOOT-HOLD] HELD — restoration_complete=0 armed_segments_observed=%d "
                "dangerous_observed=%d; absence before initial state restoration is not safety",
                len(segments),
                len(dangerous),
            )
            return
        if not dangerous:
            if strat._entries_held:
                strat._entries_held = False
                logger.info(
                    "[V2-BOOT-HOLD] released — restoration_complete=1 "
                    "reconstructed_uncapped=0; CW-v2 entries open"
                )
            return
        if not strat._entries_held:
            strat._entries_held = True
        logger.warning(
            "[V2-BOOT-HOLD] HELD — restoration_complete=1 reconstructed-uncapped "
            "segment(s) survived P1.3: %s "
            "(CW-v2 entries suppressed; armed_segments_check will page)",
            ",".join(
                f"{s['symbol']}(n={s['entries_this_flip']}/{s['max_entries']})" for s in dangerous
            ),
        )

    async def _publish_bot_state(self) -> None:
        if self.redis is None:
            return
        self._cw_boot_hold_check()
        reportable = await asyncio.to_thread(self._fetch_reportable_state)
        payload = StrategyBotStatePayload(
            strategy_code=STRATEGY_CODE,
            account_name=self.settings.strategy_schwab_1m_v2_account_name,
            watchlist=sorted(self._watchlist),
            prewarm_symbols=[],
            data_health=dict(self._data_health),
            retention_states=[],
            positions=reportable["positions"],
            pending_open_symbols=reportable["pending_open"],
            pending_close_symbols=reportable["pending_close"],
            pending_scale_levels=[],
            daily_pnl=reportable["daily_pnl"],
            closed_today=reportable["closed_today"],
            recent_decisions=[],
            indicator_snapshots=[],
            bar_counts=dict(self._bar_counts),
            last_tick_at=dict(self._last_tick_at),
            cw_armed_segments=self.strategy.cw_armed_segments(),
            entries_held=bool(getattr(self.strategy, "_entries_held", False)),
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

    async def _fanout_outcome_loop(self) -> None:
        """Poll committed outcome evidence; Redis is deliberately not an authority."""

        await run_resilient_loop(
            stop_event=self._stop_event,
            tracker=self._loop_health,
            name="fanout_outcomes",
            iteration=self._fanout_outcome_pass,
            backoff_secs=self._loop_backoff_secs,
            logger=logger,
            idle=lambda: sleep_or_stop(
                self._stop_event,
                FANOUT_OUTCOME_POLL_INTERVAL_SECONDS,
            ),
        )

    async def _fanout_outcome_pass(self) -> None:
        journal = self.fanout_outcome_journal
        if journal is None:
            return
        rows = await asyncio.to_thread(journal.read_pending)
        # Strategy state is event-loop-owned.  Never call ``apply_fanout_outcome`` from the DB
        # worker thread: quote/bar callbacks mutate the same SymbolState objects.
        for row in rows:
            self.strategy.apply_fanout_outcome(row)
        evaluated = await asyncio.to_thread(journal.advance, rows)
        self._fanout_outcome_evaluations += evaluated
        if evaluated:
            logger.info(
                "[V2-FANOUT-OUTCOME-CENSUS] trigger=durable_poll evaluated=%d "
                "evaluated_since_boot=%d — polarity: evaluated=0 is UNEXERCISED, not clean",
                evaluated,
                self._fanout_outcome_evaluations,
            )

    async def _position_poll_pass(self) -> None:
        maps = await asyncio.to_thread(self._fetch_position_maps)
        if maps is None:
            # A DB read failure is not permission to leave entry state alive after the close.
            # Releasing entry permission is safe without a position answer: held positions keep
            # their independent EXIT state and subscription coverage. Any resting BUY cancellation
            # queued here must still be emitted rather than waiting for another bar.
            self._release_entry_state_at_window_close()
            await self._drain_direct_strategy_intents()
            logger.warning(
                "[V2-POSITION-READ-UNKNOWN] result=COULD_NOT_TELL entry_permission=BLOCKED "
                "known_position_state=RETAINED — polarity: this is not broker-flat evidence"
            )
            return
        positions, held = maps
        tracked = set(self._watchlist) | set(self.strategy._symbol_states.keys())
        for symbol in tracked:
            qty = positions.get(symbol, 0)
            self.strategy.update_position(symbol, qty, held_qty=held.get(symbol, 0))
        self._release_entry_state_at_window_close()
        await self._drain_direct_strategy_intents()
        self._roll_stale_session_state(positions, held)
        # Refresh EXIT COVERAGE on the same pass (one extra read, already off-loop).
        managed = await asyncio.to_thread(self._fetch_managed_symbols)
        coverage = {s for s, q in held.items() if q > 0} | managed
        if coverage != self._exit_coverage:
            gained = sorted(coverage - self._exit_coverage)
            lost = sorted(self._exit_coverage - coverage)
            self._exit_coverage = coverage
            logger.info(
                "[V2-EXIT-COVERAGE] held=%d gained=%s lost=%s "
                "(subscription-only; NEVER an entry input)",
                len(coverage), ",".join(gained) or "-", ",".join(lost) or "-",
            )
            await self._sync_gateway_subscription()
            self._push_desired_symbols()

    def _fetch_managed_symbols(self) -> set[str]:
        """Symbols we own, from EVERY layer that can assert ownership. **ADD-only union.**

        Three sources, because the fix's own premise is *"if we hold it, we watch it"* and NO single
        layer is the authority on what we hold:

        | source | why it alone is not enough |
        |---|---|
        | `virtual_positions` (caller) | read **ZERO for a position we genuinely held** (DSY 2026-08-07) |
        | `oms_managed_positions`      | has carried **PHANTOM rows** (#644) |
        | `account_positions`          | broker truth, but lags fills and is silent on a leg the broker has not settled |

        ⛔⭐⭐ **THE THIRD SOURCE MAY ONLY ADD, NEVER GATE.** If the broker says flat and either
        internal layer says held, we STAY SUBSCRIBED. Over-subscription is free — a few quotes per
        second. Under-subscription is the defect this whole change exists to fix, and it would
        degrade in exactly the situation it was built for.

        ⛔ Protected symbols (e.g. the operator's standing CYN) are EXCLUDED: v2 must never watch,
        subscribe to or act on them, and coverage is not a back door to that.

        Never raises — a DB blip must not SHRINK coverage, so the caller keeps the previous set.
        """
        if self.session_factory is None:
            return set(self._exit_coverage)
        owned: set[str] = set()
        try:
            with self.session_factory() as session:
                # (a) our own managed rows — spans both fan-out legs
                owned |= {
                    str(mp.symbol or "").upper()
                    for mp in session.scalars(
                        select(OmsManagedPosition).where(
                            OmsManagedPosition.strategy_code == STRATEGY_CODE,
                            OmsManagedPosition.current_quantity != 0,
                        )
                    ).all()
                    if mp.symbol
                }
                # (b) BROKER TRUTH — account_positions for v2's accounts (primary + Webull leg)
                account_names = {
                    n for n in (
                        self.settings.strategy_schwab_1m_v2_account_name,
                        self.settings.strategy_schwab_1m_v2_webull_account_name,
                    ) if n
                }
                if account_names:
                    owned |= {
                        str(ap.symbol or "").upper()
                        for ap in session.scalars(
                            select(AccountPosition)
                            .join(BrokerAccount, BrokerAccount.id == AccountPosition.broker_account_id)
                            .where(
                                BrokerAccount.name.in_(account_names),
                                AccountPosition.quantity != 0,
                            )
                        ).all()
                        if ap.symbol
                    }
        except Exception:  # noqa: BLE001
            logger.warning(
                "[V2-EXIT-COVERAGE] ownership read failed; KEEPING the previous coverage set "
                "(shrinking it on a DB blip would unsubscribe a held symbol)",
                exc_info=True,
            )
            return set(self._exit_coverage)
        protected = set(self.settings.protected_symbol_set)
        return owned - protected if protected else owned

    def _subscription_symbols(self) -> set[str]:
        """Symbols to SUBSCRIBE: the watchlist plus anything we still hold.

        ⛔⭐⭐ SUBSCRIPTION ONLY. Never use this for an entry, arm, re-entry or fan-out decision —
        those read `self._watchlist`. See docs/design/held-symbol-exit-coverage.md §2.
        """
        return set(self._watchlist) | set(self._exit_coverage)

    def _release_entry_state_at_window_close(
        self, now: datetime | None = None
    ) -> PostCloseEntryRelease | None:
        """B20 — make the 16:00 boundary exit-only while continuing to listen.

        Fires once per ET day. Every entry arm and claim is released, including symbols we hold;
        held symbols remain exit-managed and subscribed through position/coverage state, not an
        entry arm. A working resting BUY gets a cancellation request before its identity is cleared.
        The per-bar strategy guard prevents a later after-hours BUY flip from rebuilding the state.
        """
        et = (now or datetime.now(UTC)).astimezone(EASTERN_TZ)
        if not self.strategy._entry_window_closed_for_session(et):
            return
        # 00:00-03:59 belongs to the PREVIOUS 04:00-anchored trading session. Using the calendar
        # date here would consume today's key at 00:01 and suppress today's real 16:00 sweep.
        minutes = et.hour * 60 + et.minute
        close_session_et = et - timedelta(days=1) if minutes < 4 * 60 else et
        day_key = close_session_et.strftime("%Y-%m-%d")
        if self._entry_window_arm_release_day == day_key:
            return
        self._entry_window_arm_release_day = day_key
        census = self.strategy.release_entry_state_at_window_close()
        armed_after_close = len(self.strategy.cw_armed_segments())
        # ⭐ LOG ZERO AND THE DENOMINATOR. "released nothing" and "never ran" must not read the
        # same, and a zero without evaluated cannot distinguish a clean boundary from no symbols.
        logger.info(
            "[V2-ENTRY-WINDOW-EXIT-ONLY] evaluated=%d released=%d arms_released=%d "
            "cancel_requested=%d held_positions=%d armed_after_close=%d symbols=%s — polarity: "
            "armed_after_close must be 0; held positions stay exit-managed and subscribed",
            census.evaluated,
            len(census.released),
            len(census.arms_released),
            len(census.cancel_requested),
            len(census.held_positions),
            armed_after_close,
            ",".join(census.released[:20]) or "-",
        )
        return census

    def _roll_stale_session_state(
        self, positions: dict[str, int], held: dict[str, int]
    ) -> None:
        """TIME-driven 04:00-ET session roll for symbols that stopped receiving bars.

        The strategy's reset is BAR-driven, so a de-watchlisted symbol never rolls and keeps
        `cw_armed=True` indefinitely (FUSE/HYFM/AXTL: ~33h on 2026-08-05). This applies the SAME
        reset on a timer. Runs on the existing 5s position poll — no new task, so no new
        liveness surface.

        ⛔⭐ THE CARVE-OUT IS WIDER THAN `_protected_symbols()`. That helper answers a DIFFERENT
        question — "which symbols must stay on the WATCHLIST" — and covers positions only. The
        session reset also clears `resting_active`, and clearing that while a buy-stop is WORKING
        AT THE BROKER orphans the order: #580's latch race, where losing it once means it never
        reprices again. So this predicate adds `resting_active` rather than reusing that helper,
        and `_protected_symbols()` keeps its meaning untouched (RTH path unchanged).

        ⛔ A symbol mid-warmup is SKIPPED, not protected. Warmup/DB-seed replays historical bars
        whose anchors are legitimately older; the bar-driven path is in flight for it and will
        roll it correctly. Rolling underneath the replay would reset the trail mid-series.
        """
        if not bool(
            getattr(self.settings, "strategy_schwab_1m_v2_session_time_roll_enabled", False)
        ):
            return
        operator_protected = set(self.settings.protected_symbol_set)

        def _skip(symbol: str, state) -> bool:
            if symbol in operator_protected:
                return True
            if positions.get(symbol, 0) > 0 or held.get(symbol, 0) > 0:
                return True
            if state.position_qty > 0 or state.position_qty_held > 0:
                return True
            if state.resting_active:  # #580: a working resting order must never be orphaned
                return True
            # mid-warmup: the bar-driven path owns this symbol right now
            return symbol in self._watchlist and symbol not in self._rest_warmup_done

        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        anchor = session_start_ts_ms(now_ms)
        crossed = anchor != self._session_roll_last_anchor
        rolled = self.strategy.roll_stale_session_state(now_ms, is_protected=_skip)
        # ⛔⭐ LOG ZERO. A line emitted only `if rolled` makes "rolled nothing" and "never ran"
        # identical on the tape — the exact false-clean this change exists to correct, and the
        # third instance of that pattern on 2026-08-05. So: one line per BOUNDARY CROSSING
        # carrying the count INCLUDING ZERO (and a boot-time line, since `_session_roll_last_anchor`
        # starts at 0), plus a line whenever anything is actually rolled off-boundary. Not every
        # 5s: a rolled symbol becomes current and cannot be rolled again, and a skipped symbol
        # never enters `rolled`, so neither branch can spam.
        if crossed or rolled:
            logger.info(
                "[V2-SESSION-ROLL] boundary_crossed=%s rolled=%d symbols=%s "
                "(anchor=%d watchlist=%d armed=%d; protected/skipped untouched)",
                crossed, len(rolled), ",".join(sorted(rolled)[:20]) or "-",
                anchor, len(self._watchlist), len(self.strategy.cw_armed_segments()),
            )
        if crossed:
            # ⛔⭐ SEED-GAP CENSUS, on the same boundary line the roll already earns. Emitted even
            # at ZERO, deliberately: a truncation counter that only speaks when it truncates cannot
            # be told apart from one that has stopped running. Same discipline as `evaluated=0`.
            logger.info(
                "[V2-DB-SEED-GAP-CENSUS] truncations=%d of %d seed evaluations since boot "
                "(threshold: >%d missed trading session) — ZERO here means MEASURED-NONE, not unmeasured. "
                "⛔ The DENOMINATOR is load-bearing: a bare zero cannot tell a clean day from a "
                "census that never ran, which is the only reason this line exists. Both halves are "
                "MONOTONIC since boot and count the same unit (one seed evaluation per symbol per "
                "attempt), so truncations can never exceed evaluations.",
                self._db_seed_gap_truncations,
                self._db_seed_evaluations,
                DB_SEED_MAX_MISSED_SESSIONS,
            )
            self._session_roll_last_anchor = anchor

    def _fetch_reportable_state(self) -> dict[str, list]:
        """REPORTING ONLY -- what the operator sees. Deliberately SEPARATE from
        `_fetch_open_positions`, which drives cooldown/re-entry: widening that one to a second
        broker account would make v2 believe it is "in position" on Schwab when only the Webull
        fan-out leg is open, silently changing ENTRY behaviour. This read changes no decision.

        Source is `oms_managed_positions` -- the OMS is its sole writer and it is the same table
        the exit ladder runs off, so the snapshot agrees with the thing that actually manages the
        trade. It spans BOTH broker accounts, so a dual-broker fan-out shows as the two legs it
        really is; before this the field was a hardcoded `[]` and every v2 position -- Schwab and
        Webull alike -- was invisible to the operator (live 2026-07-27: four filled Webull legs
        while the snapshot said positions=[] and daily_pnl=0.0).

        Never raises: the snapshot MUST still publish on a DB blip, because data_health and
        cw_armed_segments in the same payload are what the health crons page on.
        """
        empty: dict[str, object] = {
            "positions": [], "pending_open": [], "pending_close": [],
            "closed_today": [], "daily_pnl": 0.0,
        }
        if self.session_factory is None:
            return empty
        primary = self.settings.strategy_schwab_1m_v2_account_name
        out: dict[str, object] = {
            "positions": [], "pending_open": [], "pending_close": [],
            "closed_today": [], "daily_pnl": 0.0,
        }
        try:
            with self.session_factory() as session:
                for mp in session.scalars(
                    select(OmsManagedPosition).where(
                        OmsManagedPosition.strategy_code == STRATEGY_CODE,
                        OmsManagedPosition.current_quantity != 0,
                    )
                ).all():
                    account = str(mp.broker_account_name or "")
                    out["positions"].append({
                        "symbol": str(mp.symbol or "").upper(),
                        "quantity": int(mp.current_quantity or 0),
                        "broker_account_name": account,
                        # the operator-facing bit: which side of a fan-out this leg is
                        "leg": "primary" if account == primary else "fanout",
                        "entry_price": float(mp.entry_price or 0),
                        "entry_path": str(mp.entry_path or ""),
                        "current_profit_pct": float(mp.current_profit_pct or 0),
                        "peak_profit_pct": float(mp.peak_profit_pct or 0),
                    })
                out["positions"].sort(key=lambda p: (p["symbol"], p["broker_account_name"]))

                strategy = session.scalar(
                    select(Strategy).where(Strategy.code == STRATEGY_CODE)
                )
                if strategy is not None:
                    for ti in session.scalars(
                        select(TradeIntent).where(
                            TradeIntent.strategy_id == strategy.id,
                            TradeIntent.intent_type.in_(("open", "close")),
                            TradeIntent.status.notin_(INFLIGHT_INTENT_STATUSES_TERMINAL),
                        )
                    ).all():
                        symbol = str(ti.symbol or "").upper()
                        if not symbol:
                            continue
                        key = "pending_open" if ti.intent_type == "open" else "pending_close"
                        if symbol not in out[key]:
                            out[key].append(symbol)
                    out["pending_open"].sort()
                    out["pending_close"].sort()

                closed, realized = self._closed_round_trips_today(session)
                out["closed_today"] = closed
                out["daily_pnl"] = realized
        except Exception:
            logger.exception("schwab_1m_v2 _fetch_reportable_state failed")
            return empty
        return out

    def _closed_round_trips_today(self, session) -> tuple[list[dict], float]:
        """Today's COMPLETED trades, paired FIFO from `fills`, with per-trade PERCENT.

        ⭐ PERCENT IS THE PRIMARY FIGURE, per the standing output rule: one $25 name outweighs
        sixteen $1-7 names and flips conclusions. `daily_pnl` is a float dollar field on the wire
        so it is filled in for the card, but `closed_today` carries `profit_pct` per trade and
        that is what any judgement should be made on.

        ⛔ DEPENDS ON EXIT FILLS EXISTING. Native-OCO exits execute on a broker child leg the OMS
        never placed, so until the exit-fill capture is confirmed working this returns ([], 0.0) --
        which is the TRUTH ("no completed round trips recorded"), not the hardcoded 0.0 it replaces.
        See project_mai_tai_oco_exit_fill_blackout.

        FIFO because a symbol can be entered twice in one segment (reclaim) and the exits must pair
        with the entries in order, not be averaged.
        """
        # ORM, not raw SQL: the raw form needed Postgres `AT TIME ZONE`, which SQLite cannot run,
        # so every unit test would have fallen into the except and returned empty -- the pairing
        # logic would have been untestable while LOOKING covered.
        et_now = datetime.now(EASTERN_TZ)
        day_start = et_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        day_end = day_start + timedelta(days=1)
        rows = session.execute(
            select(BrokerAccount.name, Fill.symbol, Fill.side, Fill.quantity, Fill.price,
                   Fill.filled_at)
            .join(Strategy, Strategy.id == Fill.strategy_id)
            .join(BrokerAccount, BrokerAccount.id == Fill.broker_account_id)
            .where(
                Strategy.code == STRATEGY_CODE,
                Fill.filled_at >= day_start,
                Fill.filled_at < day_end,
            )
            .order_by(Fill.filled_at)
        ).all()

        open_lots: dict[tuple[str, str], list[list]] = {}
        closed: list[dict] = []
        realized = 0.0
        primary = self.settings.strategy_schwab_1m_v2_account_name
        for acct, symbol, side, qty, price, filled_at in rows:
            key = (str(acct), str(symbol).upper())
            q, px = float(qty or 0), float(price or 0)
            if q <= 0 or px <= 0:
                continue                      # the $0 cancelled-leg artefact must never price a trade
            if str(side).lower() == "buy":
                open_lots.setdefault(key, []).append([q, px, filled_at])
                continue
            remaining = q
            while remaining > 0 and open_lots.get(key):
                lot = open_lots[key][0]
                take = min(remaining, lot[0])
                if take <= 0:
                    # Defensive: a zero-qty lot would leave `remaining` unchanged and spin this
                    # loop forever. It should be impossible (a lot is popped the moment it hits 0),
                    # but this runs every 5s inside a live service and an unbounded while-loop is
                    # the shape behind the old OMS blocking-loop SPOF. A mutation test that stopped
                    # popping the lot hung the suite outright, which is how this got noticed.
                    open_lots[key].pop(0)
                    continue
                pct = (px / lot[1] - 1.0) * 100.0 if lot[1] else 0.0
                closed.append({
                    "symbol": key[1],
                    "broker_account_name": key[0],
                    "leg": "primary" if key[0] == primary else "fanout",
                    "quantity": take,
                    "entry_price": round(lot[1], 6),
                    "exit_price": round(px, 6),
                    "profit_pct": round(pct, 4),          # ⭐ the figure to judge on
                    "entry_time": lot[2].isoformat() if lot[2] else "",
                    "exit_time": filled_at.isoformat() if filled_at else "",
                })
                realized += (px - lot[1]) * take
                lot[0] -= take
                remaining -= take
                if lot[0] <= 0:
                    open_lots[key].pop(0)
        closed.sort(key=lambda c: c["exit_time"])
        return closed, round(realized, 4)

    def _fetch_open_positions(self) -> dict[str, int]:
        """SQL: virtual_positions(qty>0) ∪ in-flight trade_intents(open)
        for the v2 broker account, keyed by symbol. Quantity is the max
        across sources (a conservative "do we own this" signal).
        """
        maps = self._fetch_position_maps()
        if maps is not None:
            return maps[0]
        # The scanner/watchlist path needs a set even when the DB read fails. Preserve the last
        # known held/in-flight state rather than translating COULD_NOT_TELL to flat.
        return {
            symbol: int(state.position_qty)
            for symbol, state in self.strategy._symbol_states.items()
            if int(state.position_qty) > 0
        }

    def _fetch_position_maps(self) -> tuple[dict[str, int], dict[str, int]] | None:
        """Returns (union, held).

        `union` is the historical conservative signal -- virtual_positions ∪ in-flight open intents
        -- and is what `_fetch_open_positions` still returns, so every existing caller is unchanged.

        `held` counts virtual_positions ONLY, i.e. shares we actually own per filled orders. The two
        differ for exactly one reason: a resting buy-stop's open intent stays `submitted` for its
        whole life, so the union calls it a position before it has filled. The resting-order
        ownership gate needs `held`; nothing else does. See the comment in `_cw_v2_resting_track`.
        """
        if self.session_factory is None:
            return {}, {}
        account_name = self.settings.strategy_schwab_1m_v2_account_name
        positions: dict[str, int] = {}
        held: dict[str, int] = {}
        try:
            with self.session_factory() as session:
                broker = session.scalar(
                    select(BrokerAccount).where(BrokerAccount.name == account_name)
                )
                if broker is None:
                    return positions, held
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
                        held[symbol] = max(held.get(symbol, 0), int(vp.quantity))
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
            return None
        return positions, held

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
        # ⛔⭐⭐ WATCHLIST **PLUS HELD**. `mode="replace"` means a symbol absent here is
        # UNSUBSCRIBED, and the OMS exit ladder is quote-driven (`_handle_quote_tick_event` is
        # `_evaluate_v2_managed_exit`'s only caller). So dropping a held symbol from this list does
        # not merely stop watching it — it silently disarms CW_TARGET, CW_FLOOR, CW_HARD_STOP and
        # CW_FLIP on a live position at once. The rules are not watchlist-gated; their INPUT is.
        # ⛔ EXIT-ONLY: this union must never reach an entry decision. See
        # docs/design/held-symbol-exit-coverage.md §2.
        desired = sorted(self._subscription_symbols())
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
        # HARD-EXCLUDE operator-protected symbols (e.g. CYN) from v2's watchlist so
        # v2 never evaluates, subscribes, or signals on them — defense-in-depth on
        # top of the OMS protected-symbol order reject. CYN is the operator's
        # standing real-account position v2 must NEVER touch under live credentials.
        # Subtracted AFTER the union so it wins even if a protected symbol somehow
        # appeared in the confirmed list or the position-protection set.
        protected_exclude = set(self.settings.protected_symbol_set)
        if protected_exclude:
            selected -= protected_exclude
        # Stop EMITTING for symbols Schwab already refused to OPEN today
        # ("must be placed with a broker" — foreign/manual-handling names). The
        # OMS also blocks re-submission per account, but the isolated bot would
        # otherwise re-fire an intent on every flip; evicting from the watchlist
        # halts it at the source. Mirrors the main engine's
        # `_load_schwab_ineligible_symbols_by_strategy` eviction.
        # Dual-broker fan-out changes the eviction rule: a name keeps trading as long as >=1 broker
        # accepts it, so evict ONLY names BOTH brokers rejected (schwab ∩ webull). Flag-OFF keeps the
        # schwab-only eviction (byte-identical — `_webull_ineligible_symbols` returns empty when off).
        ineligible_exclude = self._schwab_ineligible_symbols()
        if bool(getattr(self.settings, "strategy_schwab_1m_v2_dual_broker_fanout_enabled", False)):
            ineligible_exclude = ineligible_exclude & self._webull_ineligible_symbols()
        if ineligible_exclude:
            selected -= ineligible_exclude
        if selected == self._watchlist:
            self._try_complete_boot_state_restoration(selected, new_symbols=set())
            return
        new_symbols = selected - self._watchlist
        # ⛔ Captured HERE, before `self._watchlist` is reassigned below — computing it after the
        # reassignment yields the empty set and the B19 release silently never runs.
        departed_symbols = self._watchlist - selected
        # ⭐ Stamp WHEN we started watching each symbol. This is the reference
        # `_cap_reconstructed_segment` uses to decide whether an armed segment was observed LIVE or
        # merely reconstructed from warmup history — see that method for the full rationale.
        # ⛔ A symbol that LEAVES and re-joins gets a FRESH stamp: we stopped watching, so we may
        # have missed flips in between, and its warmup will replay them as if they were current.
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        for sym in new_symbols:
            self._watch_start_ms[sym] = now_ms
        self._watch_start_ms = {
            sym: ts for sym, ts in self._watch_start_ms.items() if sym in selected
        }
        self._watchlist = selected
        # Drop warmup state for symbols that left the watchlist. If they
        # re-join later, REST needs to refetch the batch and the
        # buffer-and-replay path runs again.
        # B19 — a symbol LEAVING the watchlist is disarmed, not frozen.
        # ⛔ The bot's reset is bar-driven, so once we stop watching a symbol nothing ever drives
        # its state machine to the SELL flip that ends the segment. Its `cw_armed` used to sit
        # True forever, and `cw_armed_segments()` — which the restart gate reads — kept reporting
        # a segment nobody was watching. That is how stopping a symbol made the gate red until the
        # next restart.
        # ⛔ Done BEFORE the sets below are pruned: the release logs a transition, and the log is
        # the only record that this segment ended rather than simply stopped being observed.
        for sym in sorted(departed_symbols):
            self.strategy.release_and_drop_symbol(sym)
        self._rest_warmup_done &= selected
        self._db_seeded &= selected
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
        self._push_desired_symbols()
        # Fix (b): hydrate the strategy bar buffer for newly-joined symbols from
        # persisted history so MACD/VWAP/ATR clear their warmup at once instead
        # of waiting ~135 live bars. Runs once per symbol; replayed bars carry
        # historical timestamps (not fresh) so no entry fires on the seed.
        self._try_complete_boot_state_restoration(selected, new_symbols=new_symbols)
        logger.info(
            "schwab_1m_v2 watchlist updated count=%d sample=%s warmed=%d",
            len(selected),
            ",".join(sorted(selected)[:5]),
            len(self._rest_warmup_done),
        )

    def _try_complete_boot_state_restoration(
        self, selected: set[str], *, new_symbols: set[str]
    ) -> None:
        """Latch boot restoration complete from one non-vacuous current watchlist read.

        ``all([])`` is True in Python, but a current snapshot selecting zero symbols is the exact
        pre-restoration production state that caused the early release. It is not evidence that
        there were zero dangerous restored segments. Require at least one selected symbol and a
        confirmed persisted-state read for every selected symbol. Once latched, later watchlist or
        DB refreshes cannot turn this boot-only gate back on across the whole fleet.
        """
        if self._boot_state_restoration_complete:
            # The boot latch is already open. Preserve ordinary watchlist hydration for later
            # additions without reconsidering the fleet-wide boot decision.
            for sym in sorted(new_symbols):
                self._seed_strategy_bars_from_db(sym)
            return
        evaluated = len(selected)
        if evaluated == 0:
            logger.warning(
                "[V2-BOOT-RESTORE] restoration_complete=0 evaluated=0 confirmed=0 "
                "reason=empty_current_watchlist; zero restored states is not safety"
            )
            return
        results = [self._seed_strategy_bars_from_db(sym) for sym in sorted(selected)]
        confirmed = sum(result is True for result in results)
        if confirmed != evaluated:
            logger.warning(
                "[V2-BOOT-RESTORE] restoration_complete=0 evaluated=%d confirmed=%d "
                "could_not_tell=%d; boot hold remains closed",
                evaluated,
                confirmed,
                evaluated - confirmed,
            )
            return
        self._boot_state_restoration_complete = True
        logger.info(
            "[V2-BOOT-RESTORE] restoration_complete=1 evaluated=%d confirmed=%d "
            "could_not_tell=0",
            evaluated,
            confirmed,
        )

    def _push_desired_symbols(self) -> None:
        """Push the SUBSCRIPTION set (watchlist ∪ held) to the REST client and streamer.

        Held-but-de-listed symbols stay subscribed so their exits keep working: the bar feed is
        what arms CW_FLIP, and the quote feed is what drives every other exit rule.
        ⛔ EXIT-ONLY — see docs/design/held-symbol-exit-coverage.md §2.
        """
        desired = self._subscription_symbols()
        if self.rest_client is not None:
            self.rest_client.set_desired_symbols(desired)
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
            self.streamer.set_desired_symbols(desired)

    def _schwab_ineligible_symbols(self) -> set[str]:
        """Symbols Schwab refused to OPEN today ("must be placed with a broker").

        v2 must stop putting these on its watchlist so it stops EMITTING intents for
        them — otherwise the ATR path re-fires every flip and the OMS rejects each as
        `schwab_ineligible_cached`. Mirrors the main engine's
        `_load_schwab_ineligible_symbols_by_strategy`, scoped to v2's one account.
        The OMS still blocks re-submission immediately; this halts the bot at the
        source. Cached <=60s to avoid a DB read per snapshot (eviction lag is
        harmless given the OMS block). Empty in simulated/paper mode (no Schwab
        rejects are recorded for the paper account); auto-clears daily via the
        `session_date`-keyed `schwab_ineligible_today` table.
        """
        if self.session_factory is None:
            return set()
        now_m = time.monotonic()
        if (
            self._schwab_ineligible_loaded_monotonic is not None
            and now_m - self._schwab_ineligible_loaded_monotonic < 60.0
        ):
            return self._schwab_ineligible_cache
        account_name = self.settings.strategy_schwab_1m_v2_account_name
        blocked: set[str] = set()
        try:
            with self.session_factory() as session:
                account_id = session.scalar(
                    select(BrokerAccount.id).where(BrokerAccount.name == account_name)
                )
                if account_id is not None:
                    by_account = OmsStore().list_schwab_ineligible_symbols_by_account(
                        session,
                        broker_account_ids=[account_id],
                        session_date=session_day_eastern_str(datetime.now(UTC)),
                    )
                    blocked = by_account.get(account_id, set())
        except Exception:  # noqa: BLE001
            logger.exception("schwab_1m_v2 failed loading Schwab ineligible symbols")
            return self._schwab_ineligible_cache
        self._schwab_ineligible_cache = blocked
        self._schwab_ineligible_loaded_monotonic = now_m
        return blocked

    def _webull_ineligible_symbols(self) -> set[str]:
        """Dual-broker fan-out: symbols Webull refused to OPEN today (symmetric to
        `_schwab_ineligible_symbols`, scoped to the Webull account). Used to skip the Webull leg and
        — intersected with the Schwab set — to evict a name only when BOTH brokers reject it. Cached
        <=60s. Empty when fan-out is off / the Webull account is unset / paper mode; auto-clears daily
        via the `session_date`-keyed `webull_ineligible_today` table."""
        if self.session_factory is None:
            return set()
        if not bool(getattr(self.settings, "strategy_schwab_1m_v2_dual_broker_fanout_enabled", False)):
            return set()
        account_name = str(
            getattr(self.settings, "strategy_schwab_1m_v2_webull_account_name", "") or ""
        ).strip()
        if not account_name:
            return set()
        now_m = time.monotonic()
        if (
            self._webull_ineligible_loaded_monotonic is not None
            and now_m - self._webull_ineligible_loaded_monotonic < 60.0
        ):
            return self._webull_ineligible_cache
        blocked: set[str] = set()
        try:
            with self.session_factory() as session:
                account_id = session.scalar(
                    select(BrokerAccount.id).where(BrokerAccount.name == account_name)
                )
                if account_id is not None:
                    by_account = OmsStore().list_webull_ineligible_symbols_by_account(
                        session,
                        broker_account_ids=[account_id],
                        session_date=session_day_eastern_str(datetime.now(UTC)),
                    )
                    blocked = by_account.get(account_id, set())
        except Exception:  # noqa: BLE001
            logger.exception("schwab_1m_v2 failed loading Webull ineligible symbols")
            return self._webull_ineligible_cache
        self._webull_ineligible_cache = blocked
        self._webull_ineligible_loaded_monotonic = now_m
        return blocked

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
        was_warmed = symbol in self._rest_warmup_done
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
            await self._handle_bar(
                symbol,
                bar,
                observation_phase="live" if was_warmed else "replay",
            )

        # Drain buffered streamer bars AFTER the current REST bar is
        # fed, so the deque tail reflects the latest REST bar before
        # any newer streamer bars are appended on top.
        if just_warmed:
            await self._drain_streamer_pending(symbol)
            # ⛔⭐ CAP LAST — after the bar feed AND the streamer drain, never inside the
            # `just_warmed` block above.
            #
            # It used to run the moment `[V2-REST-WARMED]` was logged, which is BEFORE the final
            # warmup bar reaches `_handle_bar` and before the drain. So the cap inspected a segment
            # that was not armed yet, found nothing, and the arm landed uncapped microseconds later.
            # Observed live on the 2026-07-30 11:22 ET restart:
            #
            #   15:22:23,607  [V2-REST-WARMED] CRWU            <- cap ran here, saw nothing
            #   15:22:23,624  [V2-CW-ARM]      CRWU armed      <- armed AFTER the cap
            #   15:22:27,962  [V2-BOOT-HOLD]   HELD — CRWU(n=0/2)   <- entries frozen BOT-WIDE
            #
            # `[V2-CW-SEED-CAP]` had never fired once. The boot-hold is fail-safe (it suppresses
            # entries rather than trading a stale segment) but it froze the whole bot — the same
            # 54-minute freeze seen on 2026-07-27, whose fix moved this call but not far enough.
            #
            # ⛔ The invariant is in the method's own docstring: "MUST run after EVERY replay that
            # can arm a segment." Both the final bar feed and the drain are such replays.
            self._cap_reconstructed_segment(symbol, stage="rest-warmup")

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
        await self._handle_bar(symbol, bar, observation_phase="live")

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
            await self._handle_bar(symbol, buffered, observation_phase="replay")
        if fresh:
            logger.info(
                "[V2-STREAMER-DRAIN] replayed %d buffered bars for %s "
                "after warmup",
                len(fresh),
                symbol,
            )

    def _watch_start_for(self, symbol: str) -> int:
        """Epoch ms from which this symbol's flips are OBSERVED-LIVE rather than reconstructed.

        Its watchlist join time, or `_boot_ms` for anything present since process start.
        """
        return self._watch_start_ms.get(symbol, self.strategy._boot_ms)

    def _cap_reconstructed_segment(self, symbol: str, *, stage: str) -> None:
        """P1.3: mark a RECONSTRUCTED armed segment as USED, so v2 can only enter on a flip we
        actually WATCHED HAPPEN. Fail-closed: costs at most one legit first-entry.

        ⛔⭐ THE REFERENCE IS PER-SYMBOL, NOT GLOBAL BOOT (changed 2026-07-30 after a live loss).
        It used to compare `arm_bar_ts < self.strategy._boot_ms`. That is the right idea against the
        WRONG clock: it only screens segments older than the PROCESS, and says nothing about a
        symbol the scanner promoted mid-session. When a new symbol is confirmed, the REST warmup
        replays its history and re-arms segments from flips that happened before we were watching —
        and those sailed through, because they are newer than boot.

        Measured on 2026-07-30 (v2 booted 07-28 19:05 ET):

            APLX  flip bar 09:16 ET | joined the watchlist 09:38 ET | bought 10:00 at +23.7%
            SNDG  flip bar 09:23 ET | joined the watchlist 09:34 ET | bought 10:00 at +18.9%

        Both flips are two days AFTER boot and ~20 minutes BEFORE the symbol was being watched, so
        the old check passed them. Both trades stopped out.

        ⭐ Operator's rule (2026-07-30): "if the momentum scanner confirms a NEW stock it needs to
        wait for a fresh flip; the stocks we've had since 07:00 don't have to — they've been in the
        system, we saw the flips happen." A per-symbol watch-start expresses exactly that, and
        symbols present at boot keep `_boot_ms`, so their behaviour is unchanged.

        ⛔ The comparison is `<=`, not `<`. A bar timestamp is the bar's OPEN, so a symbol that
        joined at 09:38:30 was NOT watching when the 09:38 bar opened — that bar's flip is not
        observable-live. Fail-closed.

        ⛔ Warmup bars are still INGESTED — the ATR needs the history to be correct. Only the ARM
        they produce is disqualified.

        MUST run after EVERY replay that can arm a segment. It originally ran only at the end of the

        MUST run after EVERY replay that can arm a segment. It originally ran only at the end of the
        DB seed, but the REST warmup replays again a fraction of a second LATER and RE-ARMS — and
        those arms were never capped:

            10:33:07,137  [V2-CW-ARM] GMEX bar_ts=1780439040000   <- db-seed replay
            10:33:07,187  db-seed: GMEX hydrated 250 bars         <- cap ran HERE
            10:33:07,510  warmup feed for GMEX: 716 bars          <- REST warmup
            10:33:07,531  [V2-CW-ARM] GMEX bar_ts=1784555700000   <- re-armed, NEVER capped

        Those uncapped pre-boot segments read as "dangerous" to `cw_armed_segments()`, so the P1.3
        boot-hold suppressed CW-v2 entries **BOT-WIDE** for 54 minutes on 2026-07-27 (06:33->07:26)
        until a human restarted. One stale symbol froze every symbol.
        """
        strat = self.strategy
        if not getattr(strat, "_cw_armed_segment_safety_enabled", False):
            return
        st = strat.watchlist_state(symbol)
        max_e = strat._cw_v2_max_entries_per_flip
        watch_start = self._watch_start_for(symbol)
        if (
            st.cw_armed
            and 0 < st.cw_arm_bar_ts <= watch_start
            and st.cw_entries_this_flip < max_e
        ):
            st.cw_entries_this_flip = max_e
            logger.info(
                "[V2-CW-SEED-CAP] %s reconstructed armed segment capped — the flip predates our "
                "watch (entries->%d, arm_bar_ts=%d, watch_start=%d, boot=%d, stage=%s)",
                symbol, max_e, st.cw_arm_bar_ts, watch_start, strat._boot_ms, stage,
            )

    def _missed_sessions_between(self, session, older, newer) -> int:
        """Whether any TRADING SESSION falls strictly between two bar dates.

        While ``DB_SEED_MAX_MISSED_SESSIONS`` is zero the return saturates at 1: the caller asks
        only whether the gap crosses at least one intervening session.  If the threshold is ever
        raised, the exact-count branch below is selected instead.

        ⛔⭐ The calendar is derived from the DATA (any symbol, this strategy code), so market
        holidays need no separate table and cannot drift out of date. A weekend contributes ZERO
        because no session falls inside it — which is the whole point: we seed across a CLOSURE and
        refuse to seed across an ABSENCE.

        ⛔ On any DB error this returns 0, i.e. "no sessions missed", so the seed behaves exactly as
        it did before. A failed calendar read must never silently truncate real history — the same
        direction of bias as "no quote => no opinion".
        """
        older_date = older.astimezone(EASTERN_TZ).date()
        newer_date = newer.astimezone(EASTERN_TZ).date()
        # Session dates strictly between the two bars => [next ET midnight, newer ET midnight).
        # Keeping the indexed column bare is the same sargability invariant as the boundary lookup.
        lo_ts = datetime.combine(older_date + timedelta(days=1), time_cls.min, tzinfo=EASTERN_TZ)
        hi_ts = datetime.combine(newer_date, time_cls.min, tzinfo=EASTERN_TZ)
        if lo_ts >= hi_ts:
            return 0
        params = {"sc": STRATEGY_CODE, "iv": INTERVAL_SECS, "lo": lo_ts, "hi": hi_ts}
        try:
            if DB_SEED_MAX_MISSED_SESSIONS == 0:
                # The 2026-08-25 16:34 ET fail-open was this sibling, not #765's boundary lookup.
                # Counting and sorting distinct dates bought a cardinality the caller discarded.
                # EXISTS preserves the decision and stops at the first intervening-session row.
                found = session.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 "
                        "FROM strategy_bar_history "
                        "WHERE strategy_code = :sc AND interval_secs = :iv "
                        "AND bar_time >= :lo AND bar_time < :hi)"
                    ),
                    params,
                ).scalar()
                return 1 if found else 0
            rows = session.execute(
                text(
                    "SELECT count(DISTINCT ((bar_time AT TIME ZONE 'America/New_York')::date)) "
                    "FROM strategy_bar_history "
                    "WHERE strategy_code = :sc AND interval_secs = :iv "
                    "AND bar_time >= :lo AND bar_time < :hi"
                ),
                params,
            ).scalar()
            return max(0, int(rows or 0))
        except Exception:  # noqa: BLE001 - a calendar read must never cost us real history
            logger.warning(
                "[V2-DB-SEED-GAP] session-calendar lookup failed; treating the gap as CONTIGUOUS "
                "(seeding unchanged). This biases towards the pre-fix behaviour, never towards "
                "silently dropping real bars.",
                exc_info=True,
            )
            self._rollback_quietly(session)
            return 0

    @staticmethod
    def _rollback_quietly(session) -> None:
        """Clear a failed transaction so ONE bad lookup cannot fail every lookup after it.

        ⛔⭐⭐ THE CASCADE THIS EXISTS TO STOP (P11, 2026-08-20). A `statement_timeout` leaves the
        transaction ABORTED, and Postgres refuses every subsequent statement on it
        (`InFailedSqlTransaction`). The seed walk calls the calendar again per gap on the SAME
        session, so the first timeout converted every later lookup in that seed into a "failure"
        too. That is why the failures arrived in same-millisecond clusters — one boundary line and
        then three gap lines at 21:57:04.372/.391/.394/.397 — which reads as three independent slow
        queries and is actually one timeout plus three refusals.
        ⛔ The two are NOT interchangeable diagnoses: a cluster blamed on "the DB is slow" sends you
        to the wrong query entirely. Rolling back keeps each lookup's verdict its own.
        """
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 - best-effort; the caller has already decided to bias safe
            logger.debug("[V2-DB-SEED-GAP] rollback after a failed calendar lookup failed", exc_info=True)

    def _missed_sessions_before_today(self, session, newest_bar) -> int:
        """Trading sessions strictly BETWEEN the newest stored bar's session and TODAY's.

        ⛔⭐ THE RETURN SATURATES (§256, 2026-08-23). While `DB_SEED_MAX_MISSED_SESSIONS` is 0 this
        answers 0 or 1 — "none" or "at least one" — never the true cardinality, because the caller
        only ever compares it against that constant. **Do not log this number as a session count.**
        The refusal message reports the two ET dates instead, which is both honest and strictly
        more informative than "56". See the branch below for why the exact count is unaffordable.

        ⛔⭐⭐ WHY THIS IS NOT `_missed_sessions_between(session, newest_bar, now)` (P10, 2026-08-19).
        That function subtracts 1 because BOTH its endpoints are bars whose own sessions appear in
        the count. Here the newer endpoint is the WALL CLOCK, not a bar, and the calendar counts any
        symbol's bars — so pre-open, a symbol whose newest bar is YESTERDAY would score 1 and have
        its entire history wiped. That is the exact inverse of the rule: a weekend or an overnight
        is a CLOSURE and must be seeded across.

        Comparing ET DATES, exclusive at both ends, removes the ambiguity — no offset to reason
        about, and "newest bar is yesterday" is 0 by construction.

        ⛔ Same failure mode as its sibling: any DB error returns 0 ("no sessions missed"), so a
        calendar blip biases towards the pre-fix behaviour and never towards dropping real history.

        ⛔⭐⭐ THE BOUNDS ARE TIMESTAMPS, NOT ET DATES (P11, 2026-08-20). Filtering on
        ``(bar_time AT TIME ZONE ...)::date`` wraps the indexed column in an expression, so Postgres
        cannot use it as an index CONDITION — it degrades to a post-index FILTER and walks every row
        this strategy owns however narrow the window is. Measured on the box: 1603 ms warm,
        `Rows Removed by Filter: 257621`, `Heap Fetches: 112449` — over the 5 s ``statement_timeout``
        the "fast" session profile sets, which is exactly how this lookup was failing. Converting the
        two ET dates to their ET-midnight instants makes them an index cond: **32.7 ms, same answer**
        (verified equal for gaps of 0, 1, 2, 3 and 35 sessions).
        ⛔ Build the instants through ``EASTERN_TZ``, never by subtracting a fixed offset — the
        boundary must stay correct across a DST change.
        """
        lo_date = newest_bar.astimezone(EASTERN_TZ).date()
        hi_date = datetime.now(UTC).astimezone(EASTERN_TZ).date()
        # ET dates strictly between the two ⇒ instants in [lo_date + 1 day, hi_date), ET midnights.
        lo_ts = datetime.combine(lo_date + timedelta(days=1), time_cls.min, tzinfo=EASTERN_TZ)
        hi_ts = datetime.combine(hi_date, time_cls.min, tzinfo=EASTERN_TZ)
        if lo_ts >= hi_ts:
            return 0  # newest bar is today or later — no session can fall in an empty range
        params = {"sc": STRATEGY_CODE, "iv": INTERVAL_SECS, "lo": lo_ts, "hi": hi_ts}
        try:
            if DB_SEED_MAX_MISSED_SESSIONS == 0:
                # ⛔⭐⭐ THE DECISION IS ONE BIT, SO ASK FOR ONE BIT (§256, 2026-08-23).
                # The caller's ONLY use of this number is `> DB_SEED_MAX_MISSED_SESSIONS`, and
                # that constant is 0 — so `count(DISTINCT date) > 0` is EXACTLY `EXISTS`. The
                # count form made Postgres materialise every matching row and sort it to compute
                # a cardinality that was then thrown away.
                #
                # ⛔⭐⭐ AND THE COST SCALED WITH THE STALENESS IT MEASURES. `lo` is the day AFTER
                # the newest stored bar, so THE WINDOW WIDTH *IS* THE GAP. The staler the history,
                # the wider the scan, the likelier the 5 s `statement_timeout` — and the failure
                # is fail-open, which declares the series CURRENT. **The guard timed out precisely
                # in the case it exists to catch.** Measured on the box 2026-08-23 against the
                # exact window of both 08-21 failures (LSTA, 2026-05-30..2026-08-21, 83 days):
                #     count(DISTINCT ...)  214,470 rows + external merge sort 4640 kB → 3580 ms
                #     EXISTS (SELECT 1)              1 row, same Index Cond          →    0.182 ms
                # 3580 ms is 72% of the timeout on an IDLE box; 08-21 ran mid-session. Same
                # answer both ways on that window: counted=56, exists=true.
                #
                # ⛔ MEASURED AND REJECTED: `SELECT DISTINCT ... LIMIT 1` does NOT short-circuit —
                # HashAggregate cannot emit before it has consumed its input, so it still read all
                # 214,470 rows (523 ms warm). Only EXISTS stops at the first match.
                #
                # ⛔ THE EQUIVALENCE IS CONDITIONAL, SO THE BRANCH IS TOO. Raise the constant above
                # 0 and "at least one" stops answering the question; this falls back to the exact
                # count automatically rather than silently returning a wrong verdict.
                found = session.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 "
                        "FROM strategy_bar_history "
                        "WHERE strategy_code = :sc AND interval_secs = :iv "
                        "AND bar_time >= :lo AND bar_time < :hi)"
                    ),
                    params,
                ).scalar()
                return 1 if found else 0
            rows = session.execute(
                text(
                    "SELECT count(DISTINCT ((bar_time AT TIME ZONE 'America/New_York')::date)) "
                    "FROM strategy_bar_history "
                    "WHERE strategy_code = :sc AND interval_secs = :iv "
                    "AND bar_time >= :lo AND bar_time < :hi"
                ),
                params,
            ).scalar()
            return max(0, int(rows or 0))
        except Exception:  # noqa: BLE001 - a calendar read must never cost us real history
            logger.warning(
                "[V2-DB-SEED-GAP] boundary session-calendar lookup failed; treating the series as "
                "CURRENT (seeding unchanged). Biases towards the pre-fix behaviour, never towards "
                "silently dropping real bars.",
                exc_info=True,
            )
            self._rollback_quietly(session)
            return 0

    def _truncate_seed_rows_at_gap(self, session, symbol: str, rows: list) -> list:
        """Return the CONTIGUOUS tail of `rows` (newest-first), refusing stale history.

        Extracted from `_seed_strategy_bars_from_db` (P11, 2026-08-20) so the two calendar
        lookups run inside the caller's `with self.session_factory()` block instead of against
        a session it had already closed. Behaviour is otherwise unchanged: same constants,
        same log lines, same counter.
        """
        # ⛔⭐⭐ TRUNCATE AT THE FIRST WIDE GAP (P0, 2026-08-18). `rows` is newest-first. Walk back
        # and keep only the CONTIGUOUS tail; everything beyond a > DB_SEED_MAX_GAP_DAYS jump is a
        # different market regime for this name and must never reach the strategy.
        # ⛔ Downstream guards CANNOT cover this and it is not safe to lean on them:
        #   * `min_bars` (~135) explicitly EXEMPTS ATR-Flip -- which is the path that armed CAST;
        #   * `[V2-CW-SEED-CAP]` is post-hoc and has failed twice, once by 50ms ordering (#619, the
        #     REST-warmup path) and once by never running at all (CAST, 08-18);
        #   * clearing the pending-cross stash below only stops a MACD/VWAP cross, never an ARM.
        # Remove the bad input; then none of them has to be right.
        # ⛔⭐⭐ THE BOUNDARY GAP (P10, 2026-08-19). The loop below only ever compares ADJACENT
        # LOADED BARS, so a history that is wholly stale but internally contiguous has no gap to
        # find and seeded IN FULL — no truncation, no log line. Measured that day: 178 symbols in
        # exactly that state, 600-780 bars each, 35-62 days stale at the worst. VRAX would have
        # seeded 241 bars from 07-09 (traded 5.92-12.85) while it traded 3.22-4.07; it escaped only
        # because it joined the watchlist AFTER its first bar of the day, which put both islands in
        # the window and made the gap internal.
        # ⇒ The gap between the NEWEST loaded bar and TODAY counts as a missed-session gap, on the
        #   same constant and in the same units as the internal check.
        self._db_seed_evaluations += 1
        boundary_missed = 0
        if rows:
            newest_bt = (
                rows[0].bar_time
                if rows[0].bar_time.tzinfo
                else rows[0].bar_time.replace(tzinfo=UTC)
            )
            boundary_missed = self._missed_sessions_before_today(session, newest_bt)
        if boundary_missed > DB_SEED_MAX_MISSED_SESSIONS:
            # ⛔ SPEAK WHEN REFUSING — same census line, same counter, so the existing watch sees it.
            newest_et = newest_bt.astimezone(EASTERN_TZ)
            today_et = datetime.now(UTC).astimezone(EASTERN_TZ)
            logger.warning(
                "[V2-DB-SEED-GAP] %s dropped ALL %d seed bars — the newest stored bar is %s ET "
                "and today is %s ET, with at least one trading session in between (%.1f days). "
                "The whole window is a different market regime for this name; there is no "
                "contiguous tail to keep. A market CLOSURE is seeded across; an ABSENCE is not.",
                symbol, len(rows),
                newest_et.strftime("%Y-%m-%d %H:%M"), today_et.strftime("%Y-%m-%d"),
                (today_et - newest_et).total_seconds() / 86400.0,
            )
            self._db_seed_gap_truncations += 1
            rows = []

        kept: list = []
        prev_bt = None
        for row in rows:  # newest -> oldest
            bt = row.bar_time if row.bar_time.tzinfo else row.bar_time.replace(tzinfo=UTC)
            if prev_bt is not None and (prev_bt - bt) >= _DB_SEED_GAP_PROBE_MIN:
                missed = self._missed_sessions_between(session, bt, prev_bt)
                if missed > DB_SEED_MAX_MISSED_SESSIONS:
                    break
            kept.append(row)
            prev_bt = bt
        dropped = len(rows) - len(kept)
        if dropped:
            # ⛔⭐ SPEAK WHEN REFUSING. A silent truncation replaces a visible failure with an
            # invisible one, and we would never learn the fix had stopped working.
            oldest_kept = kept[-1].bar_time
            first_dropped = rows[len(kept)].bar_time
            logger.warning(
                "[V2-DB-SEED-GAP] %s dropped %d of %d seed bars — the series SKIPS at least one "
                "trading session (%s -> %s, %.1f days). Seeded the contiguous tail only (%d bars). "
                "A market CLOSURE is seeded across; an ABSENCE is not — median price "
                "discontinuity is 10.2%% across a closure and 26.2%% across one missed session.",
                symbol, dropped, len(rows),
                first_dropped, oldest_kept,
                (oldest_kept - first_dropped).total_seconds() / 86400.0, len(kept),
            )
            self._db_seed_gap_truncations += 1
        return kept

    def _seed_strategy_bars_from_db(self, symbol: str) -> bool:
        """Fix (b): hydrate `state.bars` from `strategy_bar_history` on cold-start.

        Replays the last DB_SEED_BAR_LIMIT persisted 60s bars (ascending) through
        the strategy so MACD/VWAP/stoch clear their warmup immediately — killing
        the ~135-minute post-restart entry blackout (the line-676 min_bars guard
        otherwise blinds ALL paths until `state.bars` refills live-only, which the
        C3 dedup gate forces). Seed bars carry historical timestamps so
        `bar_is_fresh` is False → no intent fires on the replay. Cross-session is
        safe: VWAP/ATR self-reset at the 04:00-ET anchor; MACD wants the continuity.

        SAFETY (the load-bearing bit): after the replay, CLEAR the pending-cross
        stash. A native MACD/VWAP cross on the LAST seed bar would otherwise be
        consumed by the first live bar (gap <= pending_cross_max_gap_secs) and fire
        a PHANTOM entry from replayed history — worse than the blackout. The prev_*
        memos are KEPT (that's the point — live crosses then detect correctly).
        Runs once per symbol (`_db_seeded`, pruned with the watchlist). Returns True only when the
        persisted-state source was successfully read (including a confirmed empty result). A
        missing session or failed query returns False and remains retryable, so the boot hold can
        never turn "could not read restoration state" into "restored zero dangerous states".
        """
        if symbol in self._db_seeded:
            return True
        if self.session_factory is None:
            return False
        self._db_seeded.add(symbol)
        try:
            with self.session_factory() as session:
                rows = (
                    session.execute(
                        select(StrategyBarHistory)
                        .where(
                            StrategyBarHistory.strategy_code == STRATEGY_CODE,
                            StrategyBarHistory.symbol == symbol,
                            StrategyBarHistory.interval_secs == INTERVAL_SECS,
                        )
                        .order_by(StrategyBarHistory.bar_time.desc())
                        .limit(DB_SEED_BAR_LIMIT)
                    )
                    .scalars()
                    .all()
                )
                # ⛔⭐⭐ THE GAP ANALYSIS RUNS INSIDE THIS `with` ON PURPOSE (P11, 2026-08-20).
                # It used to run BELOW the block, against a session the context manager had already
                # CLOSED. SQLAlchemy hides that — a closed Session silently re-opens a connection
                # and begins a NEW transaction on next use — so it "worked", while leaving a
                # transaction nothing ever commits or closes, once per seeded symbol. Two calendar
                # lookups that must share the seed's transaction had instead each escaped it.
                # ⛔ "It works in production" was never evidence here: the defect's whole shape is
                # that the failure is invisible until the connection pool or the timeout notices.
                # ⛔ Guarded on `rows` so an empty history stays a NON-EVENT: it must not spend a
                # calendar lookup and must not count towards the census denominator, exactly as
                # before, when `if not rows: return` sat above this block.
                if rows:
                    rows = self._truncate_seed_rows_at_gap(session, symbol, rows)
        except Exception:  # noqa: BLE001
            # Keep the query retryable on the next current scanner snapshot. Before this method's
            # return value became a boot-release prerequisite, leaving the symbol in `_db_seeded`
            # after failure would have made a transient DB miss indistinguishable from success.
            self._db_seeded.discard(symbol)
            logger.warning(
                "schwab_1m_v2 db-seed query failed for %s; falling back to "
                "live-only warmup",
                symbol,
                exc_info=True,
            )
            return False
        if not rows:
            return True
        for row in reversed(rows):  # ascending (oldest first)
            bt = row.bar_time
            if bt.tzinfo is None:  # defensive: treat a naive timestamp as UTC
                bt = bt.replace(tzinfo=UTC)
            ts_ms = int(bt.timestamp() * 1000)
            self._strategy_on_bar(
                symbol,
                ChartBar(
                    symbol,
                    float(row.open_price),
                    float(row.high_price),
                    float(row.low_price),
                    float(row.close_price),
                    int(row.volume),
                    ts_ms,
                ),
                observation_phase="replay",
            )
        st = self.strategy.watchlist_state(symbol)
        st.pending_path_macd = False
        st.pending_path_vwap = False
        st.pending_cross_bar_ts_ms = 0
        # P1.3: a CW-v2 segment reconstructed by this replay carries a historical arm_bar_ts (< boot).
        # Mark it USED so v2 can only enter on flips AFTER boot — a restart can never re-issue the
        # per-segment cap (the CPHI class). Flag-gated; costs one legit first-entry on a pre-restart
        # segment (fail-closed). The boot-hold self-verify catches this failing (segment stays
        # dangerous => held + paged).
        # ⛔⭐⭐ NOT LOAD-BEARING — DO NOT TRUST THIS THE WAY THE LAST READER DID (2026-08-18).
        # It runs HERE, after the replay loop above, so every arm inside `on_bar` has already fired
        # and stamped. It has failed twice in production:
        #   * 2026-07-30 REST-warmup path — ran 50ms BEFORE the arm and saw nothing (#619);
        #   * 2026-08-18 CAST — never ran at all; one [V2-CW-SEED-CAP] all day (WFF), zero
        #     [V2-BOOT-HOLD], and CAST armed uncapped off a 06-18 bar at flip_level 7.99 vs a
        #     live price of 1.21.
        # The gap truncation above is what actually prevents that now, by removing the bad input.
        # Repair or delete this once the truncation has proven out; until then it is decoration.
        self._cap_reconstructed_segment(symbol, stage="db-seed")
        logger.info(
            "schwab_1m_v2 db-seed: %s hydrated %d bars (state.bars=%d)",
            symbol,
            len(rows),
            len(st.bars),
        )
        return True

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

    async def _handle_bar(
        self,
        symbol: str,
        bar: ChartBar,
        *,
        observation_phase: Literal["replay", "live"],
    ) -> None:
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
            draft = self._strategy_on_bar(
                symbol,
                bar,
                observation_phase=observation_phase,
            )
        except Exception:
            logger.exception("schwab_1m_v2 on_bar failed for %s", symbol)
            return
        await self._maybe_emit(draft)
        await self._drain_direct_strategy_intents()
        # Dual-broker fan-out: emit any Webull legs the strategy queued this bar (no-op if off).
        await self._emit_webull_fanout_legs()

    def _strategy_on_bar(
        self,
        symbol: str,
        bar: ChartBar,
        *,
        observation_phase: Literal["replay", "live"],
    ):
        """Classify real strategy bars while preserving lightweight test-double compatibility."""

        observed = getattr(self.strategy, "on_observed_bar", None)
        if callable(observed):
            return observed(symbol, bar, observation_phase=observation_phase)
        return self.strategy.on_bar(symbol, bar)

    async def _drain_direct_strategy_intents(self) -> None:
        """Emit strategy-owned resting place/cancel queues without an entry-window gate.

        The close-boundary sweep runs from the 5-second position poll, not necessarily from a bar.
        Keeping this drain shared with `_handle_bar` makes its cancel requests leave the process
        immediately. It deliberately does not drain fan-out entry legs; those remain bar-owned.
        """
        # Drain the RESTING flip-entry place/cancel drafts the strategy queued this bar. Emit them
        # DIRECTLY (bypassing _maybe_emit's reactive-only EH/entry-window gates): the resting manager
        # already gates a place to RTH + short + in-window, and a cancel must NEVER be gated. No-op
        # unless the resting entry flag is on (drain() returns []).
        drain = getattr(self.strategy, "drain_pending_intents", None)
        if callable(drain) and self.intent_emitter is not None:
            for d in drain():
                try:
                    await self.intent_emitter.emit(d)
                except Exception:
                    logger.exception(
                        "schwab_1m_v2 resting-entry emit failed for %s", getattr(d, "symbol", "?")
                    )
        # ⛔⭐⭐ WEBULL CANCEL-SAFE DRAIN — emitted DIRECTLY, bypassing `_maybe_emit`, exactly as the
        # Schwab resting drain above does and for the same reason: a CANCEL must never be gated.
        # `_maybe_emit` carries the entry-window gate, the ATR-only belt and the exit-only chokepoint;
        # any of them silently dropping a cancel leaves a live order at Webull that nothing owns --
        # the FRTT 2026-08-11 shape. No-op unless the resting mirror is on (drain returns []).
        wdrain = getattr(self.strategy, "drain_webull_direct_intents", None)
        if callable(wdrain):
            for d in wdrain():
                if self.webull_intent_emitter is None:
                    logger.warning(
                        "schwab_1m_v2 webull direct intent DROPPED for %s (%s) — no webull emitter",
                        getattr(d, "symbol", "?"), getattr(d, "intent_type", "?"),
                    )
                    await self._record_local_fanout_outcome(
                        d,
                        outcome=(
                            "could_not_tell"
                            if getattr(d, "intent_type", "") == "cancel"
                            else "dropped_no_emitter"
                        ),
                        reason="webull_direct_emitter_not_initialized",
                    )
                    continue
                try:
                    await self.webull_intent_emitter.emit(d)
                except Exception:
                    logger.exception(
                        "schwab_1m_v2 webull direct emit failed for %s (%s)",
                        getattr(d, "symbol", "?"), getattr(d, "intent_type", "?"),
                    )
                    await self._record_local_fanout_outcome(
                        d,
                        outcome="could_not_tell",
                        reason="webull_direct_redis_emit_failed",
                    )

    async def _handle_quote(self, symbol: str, quote: Quote) -> None:
        now = datetime.now(UTC)
        self._last_tick_at[symbol] = _format_eastern(now)
        # Watchdog: quotes flow whenever the market is actually trading
        # (holiday-safe), so this is the discriminator for whether a bar
        # stall is a real fault vs a quiet/closed market.
        self._last_quote_at_ms[symbol] = int(now.timestamp() * 1000)
        self._last_quote_by_symbol[str(symbol).upper()] = quote
        try:
            draft = self.strategy.on_quote(symbol, quote)
        except Exception:
            logger.exception("schwab_1m_v2 on_quote failed for %s", symbol)
            return
        await self._maybe_emit(draft)
        # Dual-broker fan-out: emit any Webull legs the strategy queued this quote (no-op if off).
        await self._emit_webull_fanout_legs()

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

    def _entry_window_start_hour_et(self) -> int:
        return entry_gate.resolve_entry_window(self.settings)[0]

    def _entry_window_end_hour_et(self) -> int:
        return entry_gate.resolve_entry_window(self.settings)[2]

    def _entry_window_start_minute_et(self) -> int:
        return entry_gate.resolve_entry_window(self.settings)[1]

    def _entry_window_end_minute_et(self) -> int:
        return entry_gate.resolve_entry_window(self.settings)[3]

    def _within_entry_window(self, now: datetime) -> bool:
        """True iff `now` falls in the operator entry window: a weekday, non-holiday ET
        day, inside [start, end). Minute granularity — the default 7:00–16:00 allows from
        07:00:00 and blocks at 16:00:00 sharp (4:00 PM RTH close).

        Delegates to the shared `entry_gate.within_entry_window` (Decision 1 of the
        replay-engine design) so the live window and the backtest replay run one
        implementation."""
        return entry_gate.within_entry_window(now, self.settings)

    async def _record_local_fanout_outcome(
        self,
        draft,
        *,
        outcome: str,
        reason: str,
    ) -> None:  # type: ignore[no-untyped-def]
        metadata = getattr(draft, "metadata", {}) or {}
        if identity_from_metadata(metadata) is None:
            return
        journal = self.fanout_outcome_journal
        if journal is None:
            logger.error(
                "[V2-FANOUT-LOCAL-OUTCOME] %s outcome=%s durable=0 could_not_tell=1 "
                "reason=no_journal",
                getattr(draft, "symbol", "?"),
                outcome,
            )
            return
        attempt_id = journal.local_attempt_id(metadata)
        try:
            await asyncio.to_thread(
                journal.record,
                metadata=metadata,
                symbol=str(getattr(draft, "symbol", "")),
                outcome=outcome,
                evidence_id=f"local:{uuid4()}",
                attempt_id=attempt_id,
                reason=reason,
                event_source="client",
                broker_account_name=str(
                    getattr(self.settings, "strategy_schwab_1m_v2_webull_account_name", "") or ""
                ),
            )
        except Exception:  # noqa: BLE001 - a failed observation cannot break the bar/quote loop
            logger.exception(
                "[V2-FANOUT-LOCAL-OUTCOME] %s outcome=%s durable=0 could_not_tell=1 "
                "failure_direction=release_after_grace",
                getattr(draft, "symbol", "?"),
                outcome,
            )
            return
        logger.info(
            "[V2-FANOUT-LOCAL-OUTCOME] %s outcome=%s durable=1 reason=%s",
            getattr(draft, "symbol", "?"),
            outcome,
            reason,
        )

    async def _maybe_emit(self, draft, emitter=None) -> str:  # type: ignore[no-untyped-def]
        """Emit a draft through the entry-window gate + ATR-only belt + EH-routing chokepoint.
        `emitter` defaults to the primary (Schwab) emitter; the dual-broker fan-out passes the
        Webull emitter so its parallel leg runs the SAME gates/routing, just to the other account."""
        if draft is None:
            return "not_applicable"
        target_emitter = emitter if emitter is not None else self.intent_emitter
        # Confirmed-window (variant CW) bar-close flip: the strategy expresses the trend
        # exit as a CLOSE draft tagged cw_flip. Publish it as a lightweight `v2_cw_flip`
        # signal (the OMS closes the managed row) rather than a normal intent — skip the
        # entry-side ATR-only belt / EH-routing below. Only ever set when CW is enabled;
        # otherwise no draft carries cw_flip, so this is byte-neutral.
        if (
            getattr(draft, "intent_type", "") == "close"
            and str(getattr(draft, "metadata", {}).get("cw_flip", "")).lower() == "true"
        ):
            if self.intent_emitter is None:
                logger.warning("schwab_1m_v2 cw_flip dropped — emitter not initialized")
                return "dropped_no_emitter"
            try:
                await self.intent_emitter.emit_cw_flip(
                    draft.symbol, str(draft.metadata.get("bar_time_ms", ""))
                )
            except Exception:
                logger.exception("schwab_1m_v2 cw_flip emit failed for %s", draft.symbol)
            return "queued" if self.intent_emitter is not None else "could_not_tell"
        # ⛔⭐⭐ EXIT-ONLY CHOKEPOINT (2026-08-11). Held-symbol coverage keeps a de-listed symbol
        # SUBSCRIBED so its exits keep working — which means bars and quotes now arrive for a name
        # the scanner has dropped, and the strategy will happily evaluate it for ENTRY. Block that
        # here: coverage exists to CLOSE positions, never to OPEN them.
        #
        # On a held position we are ALREADY exposed and exiting is not a choice. ENTERING a symbol
        # the scanner dropped is a fresh decision nobody asked for, and the drop IS the signal that
        # the name no longer qualifies. The asymmetry is the design, not an oversight.
        #
        # ⛔ Do NOT "complete" this by allowing held symbols to arm/enter/take a fan-out leg.
        # See docs/design/held-symbol-exit-coverage.md §2.
        # ⭐ SCOPED TO THE HAZARD THIS CHANGE INTRODUCES — coverage-only symbols, nothing else.
        # A broader "not on the watchlist" test would re-police an invariant that already held
        # (pre-change, subscribed == watchlist, so every emitted symbol was on it by construction)
        # and would silently change five existing entry paths. With `_exit_coverage` empty — every
        # pre-change state, and every existing test — this guard is INERT and byte-neutral.
        _sym = str(getattr(draft, "symbol", "")).upper()
        if (
            getattr(draft, "intent_type", "") == "open"
            and _sym in getattr(self, "_exit_coverage", set())
            and _sym not in self._watchlist
        ):
            logger.info(
                "[V2-ENTRY-OFF-WATCHLIST-BLOCK] dropped open intent symbol=%s reason=%s — "
                "symbol is HELD but no longer on the watchlist; coverage keeps it subscribed for "
                "EXITS ONLY and must never open a position (design §2)",
                _sym,
                getattr(draft, "reason", ""),
            )
            # Recording is additive evidence.  The exit-only refusal must still hold in a
            # minimal harness (and in production if the optional journal wiring is absent).
            _record_local = getattr(self, "_record_local_fanout_outcome", None)
            if _record_local is not None:
                await _record_local(
                    draft,
                    outcome="dropped_routing",
                    reason="off_watchlist_exit_coverage",
                )
            return "dropped_routing"
        # Trading-window gate: v2 only ENTERS inside the operator's window
        # (default 7:00 AM–4:30 PM ET, weekdays, non-holiday). Outside it, an
        # "open" intent is dropped at the chokepoint so v2 never opens a position
        # it can't manage inside hours — the 2026-07-13 7:51 PM ET after-hours
        # AGEN/SOBR entries then churned unfillable exits overnight. Exits are
        # unaffected (cw_flip handled above; OMS owns managed exits), so narrowing
        # this window can never strand an open position. Inside the window this is
        # byte-neutral.
        if getattr(draft, "intent_type", "") == "open" and not self._within_entry_window(
            datetime.now(UTC)
        ):
            logger.info(
                "[V2-ENTRY-WINDOW-BLOCK] dropped open intent symbol=%s reason=%s — "
                "outside entry window %02d:%02d–%02d:%02d ET (now=%s)",
                getattr(draft, "symbol", "?"),
                getattr(draft, "reason", ""),
                self._entry_window_start_hour_et(),
                self._entry_window_start_minute_et(),
                self._entry_window_end_hour_et(),
                self._entry_window_end_minute_et(),
                _format_eastern(datetime.now(UTC)),
            )
            await self._record_local_fanout_outcome(
                draft,
                outcome="dropped_routing",
                reason="outside_entry_window",
            )
            return "dropped_routing"
        # ATR-ONLY belt-and-suspenders: even if some path computed a non-ATR open,
        # refuse to emit it at the chokepoint. Paths 1/2 are the 7wk losers and
        # must never reach the broker under live credentials. Defense-in-depth on
        # top of the strategy-level disable; drops loudly so any leak is visible.
        if bool(getattr(self.settings, "strategy_schwab_1m_v2_atr_only_mode", False)):
            reason = str(getattr(draft, "reason", ""))
            if getattr(draft, "intent_type", "") == "open" and "ATR Flip" not in reason:
                logger.error(
                    "schwab_1m_v2 ATR-ONLY mode DROPPED non-ATR open intent "
                    "(symbol=%s reason=%s) — Paths 1/2 must not fire under go-live",
                    getattr(draft, "symbol", "?"),
                    reason,
                )
                await self._record_local_fanout_outcome(
                    draft,
                    outcome="dropped_routing",
                    reason="atr_only_guard",
                )
                return "dropped_routing"
        if target_emitter is None:
            logger.warning("schwab_1m_v2 intent dropped — emitter not initialized")
            await self._record_local_fanout_outcome(
                draft,
                outcome="dropped_no_emitter",
                reason="emitter_not_initialized",
            )
            return "dropped_no_emitter"
        if not self._apply_extended_hours_routing(draft, datetime.now(UTC)):
            await self._record_local_fanout_outcome(
                draft,
                outcome="dropped_routing",
                reason="extended_hours_routing_refused",
            )
            return "dropped_routing"
        try:
            await target_emitter.emit(draft)
        except Exception:
            logger.exception("schwab_1m_v2 emit failed")
            await self._record_local_fanout_outcome(
                draft,
                outcome="could_not_tell",
                reason="redis_emit_failed",
            )
            return "could_not_tell"
        return "queued"

    async def _emit_webull_fanout_legs(self) -> None:
        """Dual-broker fan-out: drain the strategy's queued Webull leg drafts and emit each through
        the SECOND (Webull) emitter — same entry-window gate + EH-routing as the primary — skipping
        Webull-ineligible names. Always drains (to clear the queue) even when the emitter is unset.
        No-op unless fan-out is on (nothing is queued)."""
        drain = getattr(self.strategy, "drain_webull_fanout_intents", None)
        if not callable(drain):
            return
        legs = drain()
        if not legs:
            return
        if self.webull_intent_emitter is None:
            for leg in legs:
                await self._record_local_fanout_outcome(
                    leg,
                    outcome="dropped_no_emitter",
                    reason="webull_emitter_not_initialized",
                )
            return
        webull_ineligible = self._webull_ineligible_symbols()
        for d in legs:
            sym = str(getattr(d, "symbol", "")).upper()
            if sym in webull_ineligible:
                logger.info("[V2-FANOUT] skip Webull leg %s — webull-ineligible today", sym)
                await self._record_local_fanout_outcome(
                    d,
                    outcome="dropped_ineligible",
                    reason="webull_ineligible_today",
                )
                continue
            try:
                await self._maybe_emit(d, emitter=self.webull_intent_emitter)
            except Exception:
                logger.exception("schwab_1m_v2 webull fan-out emit failed for %s", sym)

    def _apply_extended_hours_routing(self, draft, now: datetime) -> bool:  # type: ignore[no-untyped-def]
        """Restore the legacy entry handoff: in extended hours, merge
        ``order_routing_metadata`` (session=AM/PM + order_type=limit +
        limit_price=ask) onto open intents so they can fill pre/post-market —
        mirroring the macd_30s / schwab_1m path verbatim (no buffer, no new
        pricing). The limit price is the live ask, exactly like legacy
        ``_resolve_routed_price``; if there is no ask quote in extended hours we
        skip the entry (legacy's block), since a limit with no price is invalid.

        RTH is byte-identical to today: ``order_routing_metadata`` returns ``{}``
        when the session is regular, so the order stays market/NORMAL and this
        method never touches the draft. Returns False only to skip the emit.

        Delegates to the shared, pure ``entry_gate.route_extended_hours`` (Decision 1
        of the replay-engine design) so the live routing and the backtest replay run
        one implementation. ``extended_hours_session`` is passed as ``session_fn`` from
        THIS module's binding (keeping the existing monkeypatch seam), and ``logger``
        is passed so the skip-warning stays under this module's logger byte-identically.
        """
        return entry_gate.route_extended_hours(
            draft,
            now,
            self._last_quote_by_symbol.get,
            session_fn=extended_hours_session,
            log=logger,
        )


async def main() -> None:
    service = SchwabV2BotService()
    await service.run()


def run() -> None:
    asyncio.run(main())


# Re-exports for tests / introspection
__all__ = ["SchwabV2BotService", "SERVICE_NAME", "STRATEGY_CODE", "main", "run"]
