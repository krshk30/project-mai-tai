from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.broker_adapters.alpaca import AlpacaPaperBrokerAdapter
from project_mai_tai.broker_adapters.protocols import BrokerAdapter, ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.session import build_oms_session_factory
from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    DashboardSnapshot,
    Fill,
    Strategy,
    StrategyBarHistory,
    TradeIntent,
)
from project_mai_tai.exit_logic.config import TradingConfig
from project_mai_tai.exit_logic.cw_exit import cw_effective_floor, cw_exit_decision
from project_mai_tai.exit_logic.engine import ExitEngine
from project_mai_tai.exit_logic.position import Position
from project_mai_tai.events import (
    HeartbeatEvent,
    HeartbeatPayload,
    OrderEventEvent,
    OrderEventPayload,
    QuoteTickEvent,
    TradeIntentEvent,
    TradeIntentPayload,
    TradeTickEvent,
    stream_name,
)
from project_mai_tai.fanout_identity import carry_fanout_identity
from project_mai_tai.log import configure_logging
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.runtime_registry import configured_broker_account_registrations, strategy_registration_map
from project_mai_tai.runtime_seed import seed_runtime_metadata
from project_mai_tai.services.runtime import _install_signal_handlers
from project_mai_tai.settings import Settings, get_settings
from project_mai_tai.strategy_core.time_utils import (
    is_fillable_et_session,
    session_day_eastern_str,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "oms-risk"


class _ExitFetchFailed:
    """Sentinel: we could NOT ask the broker about the exit (transient, typically a Webull 429).

    ⛔ Distinct from `None`, which means the broker answered "there is no exit". Collapsing the two
    booked the trade unpaired on a temporary rate limit — the exact P&L blackout the OCO exit
    capture exists to close. Truthy, so callers must test identity BEFORE any `if detail:` branch.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<EXIT_FETCH_FAILED>"


_EXIT_FETCH_FAILED = _ExitFetchFailed()


def oco_exit_client_order_id(entry_client_order_id: str, child_id: str) -> str:
    """Key for the synthetic order row that carries a native-OCO exit leg.

    ⭐ The CHILD id is part of the key (2026-07-28). It used to be `"<entry>-ocoexit"` -- derived
    from the ENTRY alone -- so several exit legs under one entry collapsed onto a single row and
    `record_fill_if_needed` rejected all but the first at `incremental_quantity <= 0`. BIYA had FOUR
    real exits on 07-27 and only ONE was recordable. That bites hardest on RECLAIM, a symbol entered
    twice in one segment, i.e. exactly the trades currently being judged.

    Fills still dedupe on `broker_fill_id` ("<child>:<qty>"), so widening the ORDER key cannot
    double-count; it only stops the second exit being swallowed. Falls back to the historical shape
    when no child id is known, so old rows keep resolving.
    """
    return (
        f"{entry_client_order_id}-ocoexit-{child_id[-8:]}"
        if child_id
        else f"{entry_client_order_id}-ocoexit"
    )
SESSION_TZ = ZoneInfo("America/New_York")
# ---------------------------------------------------------------------------------------------
# A2 — EXIT_REFUSED_POSITION_NOT_SELLABLE (design: docs/v2-a2-reverse-reject-design.md)
#
# The broker refuses a sell on a position we HOLD. Two brokers, two strings, ONE condition:
#   Webull  ORDER_NOT_SUPPORT_REVERSE_OPTION  (http 417)   394 rejects / 14 days
#   Schwab  "This order may result in an oversold/overbought position"
# ⛔ The STRING is not the argument for treating them alike -- a reject string is authoritative for
# what the broker SAID, never for why. They are one class because the CONDITION is one: the broker
# will not let us out of a position we hold. [[feedback_a_wrong_reason_is_worse_than_a_missing_one]]
#
# ⛔⭐ FASTER RETRYING CANNOT HELP. AAOG 2026-08-04: 313 attempts in 816 s -- one every 2.6 s, every
# one rejected. The blocker is broker-side ACCOUNT STATE, not price and not our limit. Anyone
# reading the reject count will reach for retry tuning; it is the one lever that provably does
# nothing here.
A2_NOT_SELLABLE_REASON_SUBSTRINGS = (
    "order_not_support_reverse",   # Webull, SCREAMING_SNAKE (matched despaced too)
    "oversold",                    # Schwab free-text
    "overbought",                  # Schwab free-text
)
SCHWAB_INELIGIBLE_REASON_SUBSTRINGS = ("must be placed with a broker",)
# Webull "not tradable today" markers for the dual-broker fan-out per-broker eligibility.
# DELIBERATELY CONSERVATIVE (operator 2026-07-24: "never seen a Webull rejection — find out
# later or never"): only a CLEAR symbol-not-tradable reject marks a name ineligible for the day.
# Rate-limit / transient / config rejects must NEVER match — see the EXCLUDE set below (the 429
# flood already burned us: project_mai_tai_webull_mirror_429_flood).
# Matched against the reason lower-cased AND with "_" normalized to " ", so Webull's SCREAMING_SNAKE
# codes (NO_SUCH_TICKER, INVALID_SYMBOL) and free-text both hit.
WEBULL_INELIGIBLE_REASON_SUBSTRINGS = (
    "no such ticker",
    "not tradable",
    "symbol not found",
    "instrument not found",
    "invalid symbol",
    "invalid ticker",
)
# Transient / rate-limit / config markers that MUST NOT mark a name ineligible even if a broader
# not-tradable substring also appears in the free-text reason. Checked FIRST (veto wins).
WEBULL_INELIGIBLE_EXCLUDE_SUBSTRINGS = (
    "429",
    "too many requests",
    "rate limit",
    "timeout",
    "timed out",
    "temporarily",
    "missing webull app key",
    "no webull account id",
)

# Exit fillable-session window (ET). Orders can only fill while the market is in a
# tradeable session; outside it (8 PM–7 AM, weekends, holidays) placing/refreshing
# an exit is pure churn (the 2026-07-13 AGEN/SOBR overnight loop: 181 cancel/replace
# cycles on SOBR). Start 7 = Schwab pre-market fills open ~7 AM ET; end 20 = after-
# hours fills end ~8 PM ET. Overridable via settings for tuning. See the v2 entry
# window (7–18) in schwab_1m_v2_bot for the narrower ENTRY gate.
OMS_FILLABLE_SESSION_START_HOUR_ET = 7
OMS_FILLABLE_SESSION_END_HOUR_ET = 20


def utcnow() -> datetime:
    return datetime.now(UTC)


def _schwab_round(price: float) -> str:
    """Format a price to Schwab's decimal-precision rule, which FIRM-REJECTS violations:
    orders above $1 accept at most 2 decimals; at/below $1, at most 4. (ADVB 2026-07-22:
    a 4-decimal OCO exit leg on an $11 stock was CANCELED_BY_FIRM.)"""
    return f"{price:.2f}" if price > 1.0 else f"{price:.4f}"


def _as_utc(value: object) -> datetime | None:
    """Best-effort UTC datetime from a managed row's entry_time (ORM datetime, ISO string,
    or None). Used only as the fresh-fill grace anchor: anything unreadable -> None -> no
    grace, i.e. it degrades to the (still safe) positive-confirmation check."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _format_limit_price(value: float | str | Decimal | None) -> str | None:
    if value is None:
        return None
    try:
        return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")
    except Exception:
        return None


def _panic_limit_price(value: float | str | Decimal | None, buffer_pct: float) -> str | None:
    if value is None:
        return None
    try:
        price = Decimal(str(value))
        if price <= 0:
            return None
        buffered = price * (Decimal("1") - (Decimal(str(buffer_pct)) / Decimal("100")))
        return format(max(buffered, Decimal("0.01")).quantize(Decimal("0.01")), "f")
    except Exception:
        return None


def _extended_hours_session(now: datetime | None = None) -> str | None:
    current = (now or utcnow()).astimezone(SESSION_TZ)
    regular_open = current.replace(hour=9, minute=30, second=0, microsecond=0)
    regular_close = current.replace(hour=16, minute=0, second=0, microsecond=0)
    if regular_open <= current < regular_close:
        return None
    return "AM" if current < regular_open else "PM"


def _is_regular_market_session(now: datetime | None = None) -> bool:
    return _extended_hours_session(now) is None


def _metadata_marks_extended_hours(metadata: dict[str, object]) -> bool:
    session = str(metadata.get("session", "") or "").strip().upper()
    if session in {"AM", "PM"}:
        return True
    return str(metadata.get("extended_hours", "")).strip().lower() == "true"


@dataclass
class ArmedHardStop:
    strategy_code: str
    broker_account_name: str
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    stop_loss_pct: float
    stop_price: Decimal
    quote_max_age_ms: int
    initial_panic_buffer_pct: float
    close_in_flight: bool = False
    last_trigger_attempt_at: datetime | None = None
    # Bug C reconcile: count consecutive FAILED protective closes; after a threshold the
    # OMS does a fresh broker read and clears this stop if the broker is flat (position
    # closed out-of-band), instead of churning closes on a phantom forever.
    consecutive_close_failures: int = 0
    # Trailing-stop ratchet (ORB TRAIL-8%). Default 0.0 => fixed stop, byte-identical
    # to prior behavior. When >0 the stop ratchets up trail_pct% below the
    # high-water-mark and never down.
    trail_pct: float = 0.0
    high_water_mark: Decimal | None = None
    # Fresh-fill grace (2026-07-15 ERNA): when this stop was armed off a real fill.
    # A broker "flat" read within `oms_reconcile_fresh_fill_grace_secs` of arming is NOT
    # credible (the positions endpoint can lag a fill) and must not delete protection.
    # In-memory ONLY: the F2 rehydrate path leaves it None, which is correct -- a stop
    # restored after a restart is by definition not fresh, so it gets no grace.
    armed_at: datetime | None = None


class _PositionRead(Enum):
    """Tri-state broker position read. The 2026-07-15 naked-position incident (ERNA) came
    from a bool that could not say "I don't know": a symbol absent from the list, an empty
    list, and None all collapsed to "flat", so an unconfirmed read DELETED a live stop.
    UNKNOWN exists so an ambiguous read can never delete protection."""

    FLAT_CONFIRMED = "flat_confirmed"  # POSITIVE: symbol present at qty 0. Unambiguous.
    FLAT_INFERRED = "flat_inferred"    # symbol absent / list empty. Ambiguous: a genuine close
                                       # and a silently-failed read look identical -> only
                                       # trustworthy once the fill is no longer fresh.
    HELD = "held"                      # POSITIVE: symbol present at qty != 0.
    UNKNOWN = "unknown"                # read raised / unparseable qty -> NEVER clear on this.


@dataclass(frozen=True)
class _V2ManagedSnapshot:
    """Plain-data read of an open v2 managed row, taken INSIDE an off-loop DB unit
    so neither the ORM row nor its Session ever crosses the worker-thread boundary
    (the `_run_db` contract). Field names mirror `OmsManagedPosition` so
    `_hydrate_v2_position` works unchanged on this snapshot (duck-typed)."""

    symbol: str
    # ⛔⭐⭐ THE EPISODE'S IDENTITY. `oms_managed_positions.id` is a fresh UUID per episode, so it
    # is the only value that distinguishes "the position the confirmation exit was decided for"
    # from "a later position on the same (account, symbol)". Timestamps cannot: a row's
    # `entry_time` and its fill's `filled_at` differ by a few hundred ms in the SAME episode,
    # so any tolerance that accepts the real pair also accepts a stale one.
    managed_row_id: str
    entry_price: float
    current_quantity: int
    entry_time: str
    entry_path: str
    peak_profit_pct: float
    tier: int
    floor_pct: float | None
    floor_price: float | None
    scales_done: list
    scale_pnl: float
    dedup_active: bool
    # C3: the latest durable fill in this managed-position episode and the broker-position
    # snapshot that may (or may not) have caught up with it. Plain values only: the ORM rows stay
    # inside `_read_v2_managed_snapshot`'s worker-thread Session.
    latest_fill_side: str | None
    latest_fill_at: datetime | None
    account_position_quantity: float | None
    account_position_source_updated_at: datetime | None
    broker_provider: str | None


@dataclass
class _PostExitStaleHeldEpisode:
    """In-memory C3 observation state for one durable post-exit sell fill.

    The durable fill and broker-position timestamps are the authorities. This object only keeps
    the per-snapshot retry latch and readable denominators; losing it on restart may repeat one
    observation, but cannot manufacture the sell-fill evidence that activates C3.
    """

    exit_fill_at: datetime
    last_retry_snapshot_at: datetime | None = None
    evaluated: int = 0
    retries_emitted: int = 0
    last_report_key: tuple[str, datetime | None] | None = None
    timeout_reported: bool = False


@dataclass(frozen=True)
class _DriftCancelCandidate:
    """Plain-data snapshot of a working order whose quote has drifted past its
    limit. Collected inside an off-loop read unit; the broker cancel runs on-loop;
    the DB write-back re-fetches order/intent by id in a second off-loop unit. No
    ORM object crosses a thread."""

    order_id: UUID
    intent_id: UUID
    client_order_id: str
    broker_account_name: str
    strategy_code: str
    symbol: str
    side: str
    quantity: Decimal
    order_type: str
    time_in_force: str
    existing_metadata: dict
    broker_order_id: str
    limit_price: str
    intent_created_at: datetime | None
    drift: float
    terminal_cancel_reports: int


# A trade intent is DONE at these statuses; anything else keeps it in the reconciler's
# stuck-intent sweep. Mirrors `INFLIGHT_INTENT_STATUSES_TERMINAL` in schwab_1m_v2_bot.
_TERMINAL_INTENT_STATUSES = ("filled", "rejected", "cancelled")

# ⛔⭐ The states in which a cancel TARGET is provably no longer working at the broker.
#
# Deliberately NOT the same set as `_TERMINAL_INTENT_STATUSES`. That one answers "is the cancel
# REQUEST finished"; this one answers "is the ORDER off the book" — the only question a cancel
# actually cares about. `expired` belongs here and not there: an order that expired is gone, which
# satisfies a cancel, but it is not an outcome a cancel intent should claim as its own.
#
# ⛔ `accepted` / `pending` / `PENDING_CANCEL` are absent ON PURPOSE. Schwab answers a just-issued
# DELETE with PENDING_CANCEL; treating that as settled is precisely the assumption that let the
# FRTT order sit working for 136 minutes on 2026-08-11.
_CANCEL_TARGET_SETTLED_STATUSES = ("cancelled", "filled", "rejected", "expired")


def resolve_cancel_intent_status(intent_type: str, report_event_type: str) -> str:
    """The status to record on the INTENT, given the broker report about the TARGET ORDER.

    ⛔⭐ A cancel intent tracks the REQUEST; the order tracks the OUTCOME. Copying the report
    straight across conflates them: Schwab answers a just-issued DELETE with `PENDING_CANCEL`,
    which maps into ACCEPTED_STATUSES, so the intent was left `accepted` — non-terminal, and
    nothing ever polls a cancel intent again.

    A terminal report still wins, including "filled": if the order filled instead of cancelling,
    the intent must say so rather than tidying it away as cancelled.
    """
    if intent_type == "cancel" and report_event_type not in _TERMINAL_INTENT_STATUSES:
        return "cancelled"
    return report_event_type


class OmsRiskService:
    # Operator manual-stop cache window. Short enough that a stop takes effect on the next intent
    # cycle (no restart, which was the whole point), long enough that it is not a per-intent query.
    _MANUAL_STOP_CACHE_SECS = 10.0

    NO_POSITION_REASONS = ("cannot be sold short", "insufficient qty", "no broker position available to sell")
    # F2 default so instances created without __init__ (test helpers) safely skip the
    # armed-stop persistence hot-path logic; __init__ overrides from settings in production.
    _armed_stop_persistence_enabled: bool = False
    NOT_TRADABLE_REASONS = ("is not tradable",)
    NATIVE_STOP_GUARD_REASON = "HARD_STOP_NATIVE_BACKUP"
    # Webull rejects a client_order_id longer than 40 chars (ILLEGAL_PARAMETER, http 417);
    # it is the tightest broker cap, so bound every id we mint to it. 2026-07-13 ORB LGPS:
    # close/guard retries appended `-r<8hex>` each attempt, blew past 40, and every retry
    # rejected -> the exit could never place. See _build/_replacement_client_order_id.
    _CLIENT_ORDER_ID_MAX_LEN = 40
    # After this many consecutive FAILED protective closes, do a fresh broker position read
    # and clear the armed stop if the broker is flat — a position flattened out-of-band
    # (manual/external close) otherwise leaves the in-memory stop churning closes on a
    # phantom forever (2026-07-13 ORB LGPS). Only ever clears on a CONFIRMED-flat read.
    _HARD_STOP_RECONCILE_AFTER_FAILURES = 3
    # Same phantom-reconcile threshold for the v2 CW managed-exit path: after this many
    # consecutive REJECTED full-closes, a fresh broker read clears the managed row iff the
    # broker is confirmed flat (position closed out-of-band) — else it churns rejected sells
    # forever because close_on_fill waits for a fill that never comes (2026-07-13 AGEN).
    _V2_EXIT_RECONCILE_AFTER_FAILURES = 3

    # ⛔ TERMINATION BOUND for the v2 close-retry loop. Live 2026-07-29: NCRA took 145 REJECTED
    # SELLS IN 55 MINUTES (AMIX 25, STFS 7) -- one exit decision, `ref=3.0587`, retried forever
    # against a broker answering NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT.
    # The cause was NOT a missing bound: `_v2_close_reconcile_flat` RESET the counter to 0 on any
    # not-flat read, so it sawtoothed 1,2,3 -> check -> 0 -> 1,2,3 and never accumulated. And
    # `_broker_symbol_is_flat` collapses HELD and UNKNOWN into one `False`, so an INCONCLUSIVE read
    # reset the counter as if we had confirmed we hold the position.
    # Now only a POSITIVELY-HELD read resets it; an inconclusive read accumulates to this bound and
    # then STANDS DOWN. ⛔ Standing down does NOT close the row or delete protection -- that is the
    # ERNA naked-position mistake. It stops the hammering and pages; the read-only exit POLL keeps
    # running and closes the row when the broker shows the OCO resolved (which is what recovered
    # AMIX on 07-29).
    _V2_EXIT_ABANDON_AFTER_FAILURES = 8

    # ⛔⭐⭐ ABSOLUTE CEILING on rejected closes for one (account, symbol) episode. A SECOND,
    # INDEPENDENT bound, because the consecutive counter above is DELIBERATELY reset by a
    # positively-HELD read (see `_v2_close_reconcile_flat` and the tests that specify it). That
    # reset is correct when a jam clears — but it means the consecutive bound cannot terminate a
    # jam in which we genuinely hold the position for its whole duration.
    # LIVE 2026-09-03, CHPT: ~200 rejected market closes in THREE MINUTES. Every one refused
    # "oversold" because our own working exit leg reserved the shares, and every reconcile read
    # answered HELD — truthfully — so the consecutive counter sawtoothed and never reached 8.
    # ⛔ This ceiling counts TOTAL rejected closes in the episode and NOTHING clears it on a broker
    # read. It clears only on real progress: a close that actually places, a confirmed-flat
    # reconcile, or the row closing.
    # ⭐ Sustained rejected-order volume is a BROKER API-ACCESS risk — a harm the trading logic
    # cannot see, which is why this bound is not conditional on any position read.
    _V2_EXIT_MAX_REJECTS_PER_EPISODE = 20
    # ⛔⭐⭐ WHAT COUNTS AS "THE CLOSE PLACED" (#885 retrospective finding 2, 2026-09-04).
    # `not rejected` is NOT evidence of progress: `_emit_v2_managed_sell` returns [] when the
    # strategy/broker-account lookup misses, and an EMPTY list has no rejected event in it, so the
    # absence of a refusal was clearing the ceiling while NO ORDER WAS EMITTED AT ALL. Progress now
    # requires POSITIVE evidence — at least one recorded order event that is not itself a refusal.
    # ⛔ Deliberately a NEGATIVE list, not a whitelist of good statuses: an unrecognised-but-healthy
    # broker status must not read as "no progress", because that direction fails CLOSED (the loop
    # would stand down while we still hold the position). Statuses are `report.event_type`.
    _V2_EXIT_NON_PROGRESS_STATUSES = frozenset({"", "rejected", "cancelled", "canceled", "expired"})
    # C3 default for instances constructed via __new__ in focused tests. Production reads the
    # configurable setting. Derived from the measured 11-episode maximum (237.1s) + one complete
    # 5s broker-position sync interval = 242.1s, rounded UP to the next whole sync interval = 245s.
    # ⛔ Age is only a STOP/PAGE boundary. It never becomes permission to emit another sell.
    _POST_EXIT_STALE_HELD_MAX_AGE_SECONDS = 245.0
    # A terminal broker refusal is the complete budget for one CANCEL TARGET.  The first request
    # is necessary to learn that the target is CANCELED/FILLED; every later request against that
    # same order is repetition with no possible new outcome.  Reset is identity-scoped only: a
    # different target order id gets its own first request.  HELD/flat reads never reset it.
    _CANCEL_DEAD_TARGET_BROKER_REPORT_BOUND = 1
    # ---- A2 ----
    # Slow the ladder from its 1-2s cadence to a probe. NOT suppression: the block can clear at any
    # second (median 271s, but 30s at the low end), so we must keep testing or we would trade the
    # burn for a missed exit -- the same bug facing the other way.
    _A2_BACKOFF_SECONDS = 15.0
    # ⛔ OPERATOR RISK DECISION, 2026-08-06, NOT a derived value. Sits inside the bimodal gap where
    # every bound from ~90s to ~250s escalates on the SAME 7 of 11 -- so nothing is traded away
    # anywhere in that range. Do not "optimise" it against a percentile.
    _A2_ESCALATE_AFTER_SECONDS = 90.0
    # Class-level default: test helpers build instances via __new__, bypassing __init__.
    _v2_exit_stood_down: set[tuple[str, str]] = set()
    # (acct, SYMBOL) -> base coid of the resting Webull exit pair we attached. The legs themselves
    # are broker-created and unqueryable, so this is the only handle that can ever release them.
    _webull_protect_base: dict[tuple[str, str], str] = {}
    # (acct, SYMBOL) already released this episode. Cancelling once per exit decision instead of
    # once per quote tick is what keeps the fix from becoming a storm of its own.
    _exit_reservation_released: set[tuple[str, str]] = set()

    # How many consecutive sync cycles we will hold a managed row open waiting for a transient
    # exit-fill fetch (Webull 429) to succeed. At the ~15s sync this is ~45s of retries. Bounded
    # because an open managed row BLOCKS fan-out re-entry: bookkeeping must never outrank entries.
    _MAX_EXIT_FETCH_DEFERRALS = 3
    # Class-level default: test helpers build instances via __new__, bypassing __init__.
    _oco_exit_fetch_deferrals: dict[tuple[str, str], int] = {}

    def __init__(
        self,
        settings: Settings | None = None,
        redis_client: Redis | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
        broker_adapter: BrokerAdapter | None = None,
        store: OmsStore | None = None,
    ):
        self.settings = settings or get_settings()
        self.redis = redis_client or Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.session_factory = session_factory or build_oms_session_factory(self.settings)
        self.broker_adapter = broker_adapter or self._build_broker_adapter()
        self.store = store or OmsStore()
        self.strategy_registrations = strategy_registration_map(self.settings)
        self.instance_name = socket.gethostname()
        # Configure root logging so the OMS emits INFO (fills, [HARD-STOP ARMED/
        # TRIGGERED/CLEARED], exits) — not just default-WARNING. Without this the
        # entrypoint never configured logging, so every INFO line (incl. real-money
        # stop arm/trigger) was silently dropped.
        self.logger = configure_logging(SERVICE_NAME, self.settings.log_level)
        # Track-2 intrabar fix: intents and market-data ticks are consumed on SEPARATE
        # loops/tasks so a slow broker-sync REST on the control loop can never starve
        # quote-driven exit evaluation. Each stream tracks its own offset.
        self._intent_offsets = {
            stream_name(self.settings.redis_stream_prefix, "strategy-intents"): "$",
        }
        self._market_offsets = {
            stream_name(self.settings.redis_stream_prefix, "market-data"): "$",
        }
        self._armed_hard_stops: dict[tuple[str, str, str], ArmedHardStop] = {}
        # F2 (restart-while-holding): the in-memory registry above is process-memory only.
        # `_armed_stop_dirty` tracks keys whose durable `oms_armed_stops` mirror row is
        # stale; they are flushed off-loop after the stop-eval / the braid so a restart
        # can rehydrate protection (ORB was NAKED across restarts before this).
        self._armed_stop_dirty: set[tuple[str, str, str]] = set()
        self._armed_stop_persistence_enabled: bool = bool(
            getattr(self.settings, "oms_armed_stop_persistence_enabled", True)
        )
        self._boot_protection_alerts: int = 0
        self._latest_quotes_by_symbol: dict[str, dict[str, object]] = {}
        # CONF1 decisions are stamped by v2's Schwab 1m process. They remain pending until a quote
        # strictly after the evaluation point and until broker-native protection is reconciled.
        self._confirmation_exit_pending: dict[tuple[str, str], dict[str, object]] = {}
        self._confirmation_exit_inflight: set[tuple[str, str]] = set()
        self._confirmation_exit_seen_fill_ids: set[str] = set()
        # (broker_account_name, symbol) -> when the broker last CONFIRMED both OCO legs open.
        # Read per quote tick (must stay in-memory: a DB round-trip on that path is the
        # #391-family freeze driver), written only by the periodic broker sync.
        self._native_oco_armed_confirmed_at: dict[tuple[str, str], datetime] = {}
        # order_id -> (when the P0a marketable-hold first engaged, symbol). Purely for the
        # edge-triggered hold logging; never consulted by a decision path, so a lost entry costs
        # a log line and nothing else.
        self._p0a_held_orders: dict[UUID, tuple[datetime, str]] = {}
        # P0a CENSUS (instrument-the-negative, 2026-08-06). `[OMS-P0A-HOLD]` at zero lines is
        # ambiguous between "nothing qualified" and "the branch never fires" — and those are
        # completely different worlds. The census counts EVERY evaluation and its outcome, so the
        # negative becomes legible. Diagnostic only; never gates anything.
        self._p0a_census: dict[str, int] = {}
        self._p0a_census_last_emit: datetime | None = None
        # ⛔⭐⭐ THE DENOMINATOR (2026-08-10). The census above counts OBSERVATIONS (evaluations at a
        # sync tick) and, until this counter, never counted OPPORTUNITIES (managed exits actually
        # emitted). So `evaluated=0` still conflated "no exits happened" with "exits happened and
        # every one filled before a tick could see it" — which is the SAME ambiguity the census was
        # built to remove, rebuilt one level up. Measured 2026-08-10: managed-exit submit->fill over
        # 14 days, n=27, ALL inside one 15s `oms_broker_sync_interval_seconds` tick, ZERO living
        # >=15s => P0a is structurally unreachable and `evaluated=0` is CORRECT, not a broken
        # caller. That verdict required a separate ad-hoc SQL query; with `submitted` on the line it
        # is readable from the tape alone. Diagnostic only; never gates anything.
        self._p0a_census_submitted: int = 0
        # ⛔⭐⭐ Q12 — THE AUDIT WRITE USED TO GATE THE LEDGER WRITES (§183, fixed 2026-08-21).
        # `append_order_event` ran BEFORE `record_fill_if_needed` / `apply_fill_to_positions`, and
        # every one of the seven paths that reach it swallows `Exception` with a log line. So any
        # failure of the AUDIT row silently cost the FILL and the POSITION UPDATE — per order,
        # behind a WARNING. The missed migration was one way to trigger it; it is not the only one
        # (statement timeout, a deadlock, a serialization failure, a full disk).
        # ⇒ The audit write is now isolated in a SAVEPOINT and can only lose ITSELF.
        # These two counters exist because "isolated and silent" would be a worse bug than the one
        # being fixed: a dropped audit row must be COUNTED, and counted against a DENOMINATOR, or
        # its zero is unreadable — the same discipline as the seed-gap census.
        self._order_event_attempts: int = 0
        self._order_event_failures: int = 0
        self._order_event_census_last_emit: datetime | None = None
        # ⛔⭐⭐ BROKER-READ RUN STATE (Q5, 2026-08-21). A successful positions read used to log
        # nothing at all, so a run of failures had no boundary on the tape and any watcher had
        # to INFER one from the gap between failure lines. Measured on the 08-20 outage that
        # inference was genuinely ambiguous — two 30s gaps against a 15s cadence, each either a
        # success or a slow cycle, which is the whole difference between "22 consecutive" and
        # "14". These three make it a fact: a run counter stamped on the failure line, and the
        # ok/failed pair that gives the census its denominator.
        self._broker_read_consecutive_failures: dict[str, int] = {}
        self._broker_read_ok: dict[str, int] = {}
        self._broker_read_failed: dict[str, int] = {}
        self._broker_read_census_last_emit: datetime | None = None
        # (broker_account_name, symbol) -> when the OCO cleared (armed -> not armed). Keeps the
        # software ladder deferred through the RESOLUTION window: an OCO leg filled and closed the
        # position, but the OMS position state lags the broker fill by ~tens of seconds, so a
        # resumed ladder would fire a redundant close on a stale "still-held" position (rejected,
        # harmless, but logs a reject on every OCO resolution). Cleared when the position
        # reconciles out of _managed_v2_symbols, or after the grace backstop.
        self._native_oco_resolving: dict[tuple[str, str], datetime] = {}
        self._latest_trades_by_symbol: dict[str, dict[str, object]] = {}
        # Track-2 Phase-2 Slice-3: OMS-managed v2 exit ladder. `_managed_v2_symbols`
        # is the hot-path guard — a quote only opens a session/evaluates when its
        # symbol has an OPEN v2 managed row. Populated by the slice-1 fill hook
        # (gated) + rehydrated at startup; empty when the flag is OFF (inert).
        self._managed_v2_symbols: set[tuple[str, str]] = set()
        # Phantom-reconcile: consecutive REJECTED v2 full-closes per (acct, symbol). After the
        # threshold, a fresh broker read clears the row iff confirmed flat (see _emit_v2_exit_on_loop).
        self._v2_exit_close_failures: dict[tuple[str, str], int] = {}
        self._v2_exit_reject_total: dict[tuple[str, str], int] = {}
        # A2: first time this (acct,symbol) was refused as not-sellable, the last probe, and
        # whether we have already paged. Cleared the moment a close PLACES or the row closes.
        self._a2_not_sellable_since: dict[tuple[str, str], datetime] = {}
        self._a2_last_probe: dict[tuple[str, str], datetime] = {}
        self._a2_escalated: set[tuple[str, str]] = set()
        # (acct, symbol) whose close-retry loop has been STOOD DOWN — see
        # `_V2_EXIT_ABANDON_AFTER_FAILURES`. Protection is untouched; only the retry stops.
        self._v2_exit_stood_down: set[tuple[str, str]] = set()
        # C3: (account, symbol) -> the latest durable SELL-fill episode. A retry is permitted only
        # once for each newer broker-position snapshot generation; stale/timestamp-less evidence
        # emits zero sells. The managed row remains owned when the measured age bound expires.
        self._post_exit_stale_held_episodes: dict[
            tuple[str, str], _PostExitStaleHeldEpisode
        ] = {}
        # Base coid of the resting Webull exit pair, and the once-per-episode release latch. See
        # `_release_exit_reservation_before_close`. Per-instance so tests cannot leak into each other.
        self._webull_protect_base: dict[tuple[str, str], str] = {}
        self._exit_reservation_released: set[tuple[str, str]] = set()
        # Consecutive TRANSIENT exit-fill fetch failures per (acct, symbol). See _defer_for_exit_fetch.
        self._oco_exit_fetch_deferrals: dict[tuple[str, str], int] = {}
        self._v2_exit_config: TradingConfig = TradingConfig().make_v2_variant()
        self._v2_exit_engine: ExitEngine = ExitEngine(self._v2_exit_config)
        # Confirmed-window (variant CW) exit [PR #2/3]. Gated on the SAME switch the
        # CW entry reads, so entry and exit move together. OFF => the ladder path runs
        # unchanged. Pcts are OMS-side settings, tunable without a code change.
        self._cw_exit_enabled: bool = bool(
            getattr(self.settings, "strategy_schwab_1m_v2_confirmed_window_enabled", False)
        )
        self._cw_target_pct: float = float(getattr(self.settings, "oms_v2_cw_target_pct", 2.0))
        self._cw_stop_pct: float = float(getattr(self.settings, "oms_v2_cw_hard_stop_pct", 5.0))
        # CW-v2 floor exit (flag): at +target% arm a floor at +floor_pct% and RIDE; close on
        # fall-back-to-floor. OFF => byte-identical hard-target close. `_cw_floor_armed` tracks the
        # armed (acct,symbol) in memory — the floor is fixed at entry*(1+floor_pct/100) so it
        # re-arms identically after a restart (no durable state). Decision = exit_logic.cw_exit.
        self._cw_floor_exit_enabled: bool = bool(
            getattr(self.settings, "oms_v2_cw_floor_exit_enabled", False)
        )
        self._cw_floor_pct: float = float(getattr(self.settings, "oms_v2_cw_floor_pct", 2.0))
        self._cw_floor_armed: set[tuple[str, str]] = set()
        # (acct, symbol) pairs with a bar-close ATR flip pending (PR #3). Set from the
        # `v2_cw_flip` signal event; consumed (full close) by the CW exit on the next
        # quote. In-memory so the hot quote path never does a per-tick Redis read.
        self._cw_flip_pending: set[tuple[str, str]] = set()
        # Operator manual-stop cache (see `_load_global_manual_stop_symbols`).
        # Per-symbol clock for the OCO exit poll, so a managed position is not re-queried on
        # every sync (Webull 429s readily — see the exit-fill probe and the mirror flood).
        self._oco_exit_poll_at: dict[tuple[str, str], float] = {}
        self._manual_stop_symbols: set[str] = set()
        self._manual_stop_loaded_at: float = -1e9
        # Bug A follow-up: native stop-guard (re)arms that reverse-rejected because the
        # just-cancelled prior guard / entry fill had not settled at the broker. Keyed by
        # hard-stop key -> (strategy_id, broker_account_id) so the periodic sync can re-arm
        # them (broker settled by then) WITHOUT blocking the fill transaction at the open.
        self._native_guard_rearm_pending: dict[tuple[str, str, str], tuple[UUID, UUID]] = {}
        # P0.2 settlement probe (2026-07-15): (account, SYMBOL) -> when we filled into it.
        # Read-only. Answers the ONE question the 120s fresh-fill grace was guessed at:
        # how long after our fill does the broker's positions endpoint actually show it,
        # and what shape does the read take while it doesn't. Per broker -- ERNA was
        # WEBULL; v2 is SCHWAB; the grace lives in a helper shared by both.
        self._settle_watch: dict[tuple[str, str], datetime] = {}
        # P0.6 EOD flatten: (session_date_et, account, SYMBOL) already flattened, so the
        # 5s loop submits ONE close per symbol per day rather than one every tick.
        self._window_flattened: set[tuple[str, str, str]] = set()
        # Phase A EOD OCO transition: (session_date_et, account, SYMBOL) whose native-OCO
        # stand-down was released at 16:00 so the software EH-limit ladder manages the +2%/−5%
        # for the rest of the day. Day-scoped key => the latch self-expires next session; empty
        # while the flag is OFF => `_native_oco_stand_down_active` is byte-identical.
        self._v2_eod_oco_transitioned: set[tuple[str, str, str]] = set()
        # 16:01 cancel-and-reexit: ONE claim per (session_day, account, symbol). Claimed BEFORE
        # the first await, so a slow broker call can never let the 5s cadence start a second run
        # for the same position. ⛔ This is the 220-in-14-minutes hole; it does not reopen here.
        self._v2_eod_cancel_reexit_done: set[tuple[str, str, str]] = set()
        self._v2_eod_cancel_reexit_summarised: set[str] = set()
        # (session_day, account, symbol) -> attempts so far at arming the RTH-edge bracket. NOT a
        # claim-once latch like the EOD one: this sweep PLACES an order, and a transient broker
        # error must not permanently skip a position that is currently unprotected. Retries are
        # rate-limited and capped, then latched LOUD.
        self._v2_rth_edge_bracket_attempts: dict[tuple[str, str, str], int] = {}
        self._v2_rth_edge_bracket_done: set[tuple[str, str, str]] = set()
        self._v2_rth_edge_bracket_last_try: dict[tuple[str, str, str], datetime] = {}

    async def _run_db(self, fn, *, commit: bool = True):
        """Run a PURE-SYNC unit of DB work on a worker thread, off the event loop.

        SPOF cure (requirement 1): even a timeout-bounded stall (Fix 1) hangs a
        throwaway worker thread — never the shared asyncio loop — so the tick
        consumer, heartbeat, and other tasks keep running. The session is opened,
        used, committed, and closed ENTIRELY inside the thread (`fn` receives it
        and must not let it escape), so SQLAlchemy's per-thread-session contract
        holds and no session ever crosses threads. Broker `await`s must stay
        OUTSIDE `fn` (they belong on the loop; the adapters already offload their
        own REST). On exception the context manager rolls back and the error
        propagates to the caller (bounded by Fix 1), where each hot handler
        already log-skip-continues (Fix 4)."""
        def _unit():
            with self.session_factory() as session:
                result = fn(session)
                if commit:
                    session.commit()
                return result

        return await asyncio.to_thread(_unit)

    async def run(self) -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)

        seed_summary = self.seed_runtime_metadata()
        self.logger.info(
            "seeded runtime metadata: %s strategies, %s broker accounts",
            seed_summary["strategies"],
            seed_summary["broker_accounts"],
        )
        self._rehydrate_managed_v2_symbols()  # slice-3: re-arm quote eval for open v2 rows
        await self._rehydrate_armed_hard_stops()  # F2: rebuild the ORB stop registry from the durable mirror
        await self._publish_heartbeat(
            "starting",
            {
                "adapter": self.settings.oms_adapter_label,
                "providers": ",".join(self.settings.active_broker_providers),
            },
        )
        # F2 (protected-before-serving): confirm every OMS-owned held position is protected
        # + broker-backed BEFORE the tick consumer starts — never serve ticks with an
        # OMS-owned position unprotected. OMS-owned only (manual holdings untouched).
        await self._reconcile_protection_before_serving()
        # Track-2 intrabar fix: a DEDICATED tick consumer evaluates quote-driven exits
        # within milliseconds of the live tick. It is decoupled from the control loop so
        # the periodic broker-sync REST (and intent processing) can NEVER starve it — the
        # root cause of the 2026-06-17 LNAI scale that fired ~70s late at 4.345 instead of
        # ~4.45. The consumer coalesces each read burst last-quote-wins per symbol and the
        # eval rejects event-time-stale quotes, so the call is always made on the FRESHEST
        # price, never a backlogged one.
        tick_task = asyncio.create_task(self._run_tick_consumer(stop_event))
        try:
            await self._run_control_loop(stop_event)
        finally:
            stop_event.set()
            tick_task.cancel()
            try:
                await tick_task
            except asyncio.CancelledError:
                pass

        await self._publish_heartbeat(
            "stopping",
            {
                "adapter": self.settings.oms_adapter_label,
                "providers": ",".join(self.settings.active_broker_providers),
            },
        )
        await self.redis.aclose()

    async def _run_control_loop(self, stop_event: asyncio.Event) -> None:
        """Intents + periodic broker-sync + heartbeat. Reads ONLY the strategy-intents
        stream; market-data ticks are handled by `_run_tick_consumer` on its own task so
        a slow broker-sync here cannot delay an exit decision."""
        heartbeat_interval_secs = max(1, self.settings.service_heartbeat_interval_seconds)
        last_heartbeat = asyncio.get_running_loop().time()
        last_broker_sync = 0.0
        while not stop_event.is_set():
            loop_now = asyncio.get_running_loop().time()
            try:
                broker_sync_interval_secs = await self._broker_sync_interval_seconds()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Best-effort cadence optimizer — a DB stall/timeout here must
                # never break the loop or skip the heartbeat below (which is what
                # keeps the watchdog informed during a DB outage). Fall back to the
                # default interval. (Un-wrapped, this was a fatal control-loop gap.)
                self.logger.warning("broker-sync interval check failed; using default cadence")
                broker_sync_interval_secs = max(1.0, float(self.settings.oms_broker_sync_interval_seconds))
            read_timeout_secs = min(
                heartbeat_interval_secs,
                max(0.1, broker_sync_interval_secs - (loop_now - last_broker_sync)),
            )
            try:
                messages = await self.redis.xread(
                    self._intent_offsets,
                    block=max(100, int(read_timeout_secs * 1000)),
                    count=50,
                )
            except Exception:
                self.logger.exception("failed reading strategy intent stream")
                await asyncio.sleep(1)
                continue

            if messages:
                for stream, entries in messages:
                    for message_id, fields in entries:
                        self._intent_offsets[stream] = message_id
                        try:
                            await self._handle_stream_message(fields)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # A bad intent (or a Fix-1 timeout-exception raised
                            # during intent processing) must skip-continue, NOT
                            # propagate to run() and exit the whole service — the
                            # fatal control-loop gap the SPOF audit identified.
                            self.logger.exception("failed handling strategy intent message")

            now = asyncio.get_running_loop().time()
            if now - last_broker_sync >= broker_sync_interval_secs:
                try:
                    sync_summary = await self.sync_broker_state()
                except Exception:
                    self.logger.exception("failed syncing broker state")
                else:
                    self.logger.debug("broker state sync complete: %s", sync_summary)
                last_broker_sync = now
                # Bug A follow-up: re-arm native stop guards whose immediate arm reverse-
                # rejected on an unsettled cancel/fill. Non-blocking, on the sync cadence.
                # getattr guard: __new__-constructed test instances may lack the attribute.
                # P0.6: flatten OMS-owned positions before the session ends. On the same 5s
                # cadence; idempotent per symbol per day. Wrapped -- a flatten error must never
                # break broker-sync (but it IS logged at error level: see the method).
                try:
                    await self._window_flatten_armed_stops()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("[ORB-WINDOW-FLATTEN] sweep failed")
                # Phase A EOD OCO transition: at 16:00 release the native-OCO stand-down for every
                # still-open managed v2 position so the software EH-limit ladder resumes (decision A
                # = keep managing +2%/−5%). Same 5s cadence, idempotent per symbol per day, flag-gated
                # OFF. Wrapped so a failure never breaks broker-sync; LOUD on error.
                try:
                    await self._v2_eod_oco_transition()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("[OMS-V2-EOD-OCO-TRANSITION] sweep failed")
                # 16:01 cancel-and-reexit: cancel our OWN working SELL legs, CONFIRM the broker
                # reports zero, then place a PM limit exit through the managed-exit path. Same 5s
                # cadence, but the work is claimed ONCE PER POSITION PER DAY inside the method --
                # the cadence drives the check, never the action. Flag-gated OFF.
                # Wrapped so a failure never breaks broker-sync; LOUD on error.
                try:
                    await self._v2_eod_cancel_and_reexit()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("[OMS-V2-EOD-CANCEL-REEXIT] sweep failed")
                # #646 Part 1, the EOD transition's mirror image: at 09:30 ARM a bracket for any
                # position still held from a PRE-MARKET entry, which today never gets one for its
                # entire life. Same 5s cadence, same day-scoped idempotence, flag-gated OFF.
                # Wrapped: a failure here must never break broker-sync, and it must never leave the
                # software exit worse off (it does not touch it).
                try:
                    await self._v2_rth_edge_bracket()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("[OMS-V2-RTH-EDGE-BRACKET] sweep failed")
                # v2 overnight flatten: close managed v2 positions at 19:55 before the 20:00 gate
                # (v2 has no native stop). Same cadence, LOUD on failure. RETRY-UNTIL-FILLED: there
                # is deliberately NO per-day claim (#478 removed it — a per-day claim silently gave
                # up when a thin-AH limit expired unfilled, which is the naked-overnight case this
                # exists to prevent), so this RE-EMITS every pass until it fills or 20:00 closes.
                # Double-submit is guarded ONLY by `dedup_active` (a working exit order => skip).
                # See _v2_overnight_flatten's docstring — it is the contract, this line is a pointer.
                try:
                    await self._v2_overnight_flatten()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("[OMS-V2-OVERNIGHT-FLATTEN] sweep failed")
                if getattr(self, "_native_guard_rearm_pending", None):
                    try:
                        await self._retry_pending_native_guard_rearms()
                    except Exception:
                        self.logger.exception("failed retrying pending native-stop-guard rearms")
            if now - last_heartbeat >= heartbeat_interval_secs:
                heartbeat_details = {
                    "adapter": self.settings.oms_adapter_label,
                    "providers": ",".join(self.settings.active_broker_providers),
                }
                try:
                    await self._publish_heartbeat("healthy", heartbeat_details)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The heartbeat is the watchdog's liveness signal — a transient
                    # publish error must not exit the loop. Advance the stamp anyway
                    # so we don't tight-loop; the next interval retries.
                    self.logger.exception("failed publishing heartbeat")
                last_heartbeat = now

    async def _run_tick_consumer(self, stop_event: asyncio.Event) -> None:
        """Dedicated market-data consumer — the tick-by-tick guarantee. Reads the
        market-data stream on its own task (never interleaved with broker-sync REST) and
        coalesces each read burst LAST-QUOTE-WINS per symbol, so a tick storm cannot build
        a serial backlog: the exit ladder always decides on the freshest quote within ms
        of arrival. Trades are dispatched in arrival order (armed-hard-stop fidelity)."""
        while not stop_event.is_set():
            try:
                messages = await self.redis.xread(
                    self._market_offsets,
                    block=200,
                    count=500,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("failed reading market-data stream")
                await asyncio.sleep(1)
                continue
            if not messages:
                continue
            payloads: list[dict] = []
            for stream, entries in messages:
                for message_id, fields in entries:
                    self._market_offsets[stream] = message_id
                    data = fields.get("data")
                    if not data:
                        continue
                    try:
                        payloads.append(json.loads(data))
                    except Exception:
                        continue
            for event in self._coalesce_ticks(payloads):
                try:
                    if isinstance(event, TradeTickEvent):
                        await self._handle_trade_tick_event(event)
                    else:
                        await self._handle_quote_tick_event(event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("failed handling market-data tick")

    @staticmethod
    def _coalesce_ticks(payloads: list[dict]) -> list[object]:
        """Collapse a read burst to the work actually worth doing: the NEWEST quote per
        symbol (the profit/floor ladder only cares about the current price, so acting on
        stale intermediate quotes just adds latency), while every trade tick is preserved
        in arrival order (armed-hard-stop fidelity). Returns validated event objects in
        dispatch order; the per-symbol quote slot is emitted at its first-seen position
        but carries the last-seen payload — last-quote-wins."""
        latest_quote_by_symbol: dict[str, dict] = {}
        order: list[tuple[str, object]] = []  # ("quote", symbol) | ("trade", payload)
        for payload in payloads:
            event_type = str(payload.get("event_type", "")).strip().lower()
            symbol = str((payload.get("payload") or {}).get("symbol", "")).upper()
            if not symbol:
                continue
            if event_type == "quote_tick":
                if symbol not in latest_quote_by_symbol:
                    order.append(("quote", symbol))
                latest_quote_by_symbol[symbol] = payload
            elif event_type == "trade_tick":
                order.append(("trade", payload))
        events: list[object] = []
        for kind, item in order:
            try:
                if kind == "trade":
                    events.append(TradeTickEvent.model_validate(item))
                else:
                    events.append(QuoteTickEvent.model_validate(latest_quote_by_symbol[item]))
            except Exception:
                continue
        return events

    async def _handle_stream_message(self, fields: dict[str, str]) -> None:
        data = fields.get("data")
        if not data:
            return

        payload = json.loads(data)
        event_type = str(payload.get("event_type", "")).strip().lower()
        if event_type == "trade_intent":
            event = TradeIntentEvent.model_validate(payload)
            await self.process_trade_intent(event)
            return
        # Quote/trade ticks: must reach the handler even without armed hard
        # stops so the Tier 1 quote-drift cancel can fire on working open
        # limit orders (which by definition have not filled yet, so no
        # armed hard stop). The handler itself short-circuits the
        # hard-stop evaluation when there are no armed stops.
        if event_type == "quote_tick":
            event = QuoteTickEvent.model_validate(payload)
            await self._handle_quote_tick_event(event)
            return
        if event_type == "trade_tick":
            event = TradeTickEvent.model_validate(payload)
            await self._handle_trade_tick_event(event)
            return
        # Confirmed-window bar-close flip signal (PR #3): mark (acct, symbol) pending so
        # the CW exit closes the managed row on the next quote via its exit machinery.
        # CW-gated + rare; ordered after the hot quote/trade paths. No-op when CW is off.
        if event_type == "v2_cw_flip":
            if self._cw_exit_enabled:
                sym = str(payload.get("symbol", "")).upper().strip()
                acct = (
                    str(payload.get("broker_account_name", "")).strip()
                    or self.settings.strategy_schwab_1m_v2_account_name
                )
                if sym:
                    # ⭐⭐ CW_FLIP FAN-OUT (2026-08-07). The flip used to arm ONE account -- the one
                    # the publisher named, which is the bot's own (Schwab). Every OTHER exit reason
                    # reaches the Webull leg for free because CW_HARD_STOP / CW_FLOOR are
                    # STATE-driven: they iterate managed ROWS, and live:orb has rows. The flip alone
                    # is EVENT-driven and per-account, so the fan-out leg was never told to exit.
                    #
                    # Measured over the 7-day corpus:  CW_HARD_STOP 400 orb / 241 schwab
                    #                                  CW_FLOOR      47 orb /   9 schwab
                    #                                  CW_FLIP        0 orb /   4 schwab
                    # ⛔ CLASS A (no owner), NOT Class B (refused): there is no reject count because
                    # nothing was ever emitted. Cost, n=2 of 4 usable: the Webull leg rode the
                    # reversal for 22m37s (AAOG 08-04, -2.31%) and 14m01s (GTE 08-05, -4.74%) until
                    # the CW_HARD_STOP fallback caught it. Rare and expensive -- ~1 event per 2 days
                    # -- NOT a running cost.
                    #
                    # ⭐ WHY THIS IS CONSISTENCY, NOT A NEW RULE: `_v2_accounts()` is already the
                    # flag-gated answer to "which accounts does the CW exit ladder manage", and it
                    # collapses to Schwab-only when the fan-out flag is off -- so this is
                    # BYTE-IDENTICAL with the flag off, and needs no flag of its own. The flip was
                    # simply the one place that never used it.
                    #
                    # ⛔ A stale arm is harmless BY EXISTING DESIGN, and that is load-bearing here:
                    # `_maybe_emit_v2_managed_exit` discards a pending flip when the symbol has no
                    # open managed row ("no open row -> drop any stale flip"). So arming an account
                    # whose leg already exited emits NOTHING. That guard is now pinned by a test.
                    for arm_acct in dict.fromkeys([*self._v2_accounts(), acct]):
                        self._cw_flip_pending.add((arm_acct, sym))
                        self.logger.info(
                            "[OMS-V2-CW] flip pending armed acct=%s sym=%s", arm_acct, sym
                        )
            return

        if event_type == "v2_confirmation_exit":
            acct = (
                str(payload.get("broker_account_name", "")).strip()
                or self.settings.strategy_schwab_1m_v2_account_name
            )
            symbol = str(payload.get("symbol", "")).strip().upper()
            if not symbol or str(payload.get("entry_slot", "")).strip().lower() != "first":
                self.logger.error(
                    "[OMS-V2-CONFIRMATION-EXIT-REFUSED] sym=%s entry_slot=%s reason=scope",
                    symbol or "-",
                    payload.get("entry_slot", "missing"),
                )
                return
            source_fill_id = str(payload.get("source_fill_id", "")).strip()
            if not source_fill_id or source_fill_id in self._confirmation_exit_seen_fill_ids:
                return
            self._confirmation_exit_seen_fill_ids.add(source_fill_id)
            if str(payload.get("atr_state", "unknown")).lower() == "long":
                self.logger.info(
                    "[OMS-V2-CONFIRMATION-EXIT-STATE-LONG] sym=%s acct=%s fill_id=%s",
                    symbol,
                    acct,
                    payload.get("source_fill_id", ""),
                )
                return
            if not bool(payload.get("should_exit")):
                self.logger.error(
                    "[OMS-V2-CONFIRMATION-EXIT-REFUSED] sym=%s reason=inconsistent_stamp",
                    symbol,
                )
                return
            # ⛔⭐⭐ BIND THE DECISION TO THE POSITION IT WAS DECIDED FOR (2026-09-04, IMRN).
            # A pending confirmation used to name only (acct, symbol), which is not a position —
            # it is a LANE. The IMRN decision taken on the 11:35 bar for the 11:34:11 fill was
            # still armed 43 minutes later and fired against a DIFFERENT position opened at
            # 12:18:03, into protective legs placed seconds earlier: 20 refusals in 33s, the
            # reject ceiling, and a 36-minute exit suppression.
            # ⛔ If there is no open row to bind to, REFUSE. An unbound confirmation is a decision
            # looking for a victim; there is nothing to exit, and arming it is how it finds one.
            bound_row_id = await self._confirmation_bound_managed_row_id(acct, symbol)
            if not bound_row_id:
                self.logger.error(
                    "[OMS-V2-CONFIRMATION-EXIT-REFUSED] sym=%s acct=%s fill_id=%s "
                    "reason=no_open_position_to_bind",
                    symbol, acct, payload.get("source_fill_id", ""),
                )
                return
            bound_payload = dict(payload)
            bound_payload["bound_managed_row_id"] = bound_row_id
            self._confirmation_exit_pending[(acct, symbol)] = bound_payload
            self.logger.info(
                "[OMS-V2-CONFIRMATION-EXIT-FIRED] sym=%s acct=%s fill_id=%s "
                "evaluated_at_ms=%s status=PENDING_EXECUTABLE_BID",
                symbol,
                acct,
                payload.get("source_fill_id", ""),
                payload.get("evaluated_at_ms", ""),
            )
            return

    async def process_trade_intent(self, event: TradeIntentEvent) -> list[OrderEventEvent]:
        strategy_code = str(event.payload.strategy_code).strip().lower()
        if strategy_code == "orb":
            self.logger.error(
                "[OMS-ORB-PAPER-REFUSED] OMS blocked ORB intent before intent/order "
                "persistence and before broker dispatch event_id=%s account=%s",
                event.event_id,
                event.payload.broker_account_name,
            )
            return []
        if strategy_code in {"polygon_30s", "webull_30s"}:
            self.logger.error(
                "[PAPER-EXIT-REFUSED] OMS blocked polygon_30s intent before intent/order "
                "persistence and before broker dispatch event_id=%s",
                event.event_id,
            )
            return []
        # Refresh the operator manual-stop cache BEFORE opening the intent transaction, never inside
        # it: a nested session shares the connection and fights the outer transaction. `_evaluate_risk`
        # then reads a plain in-memory set, so the risk path itself does no DB I/O.
        self._load_global_manual_stop_symbols()
        # Section 82 increment 1: the existing client-order identity is the attempt identity.
        # Bind it before the TradeIntent row is created so local drops remain attributable too.
        if (
            event.payload.intent_type == "open"
            and str(event.payload.metadata.get("fanout_slot_id", "")).strip()
        ):
            event.payload.metadata = {
                **event.payload.metadata,
                "fanout_attempt_id": self._build_client_order_id(event),
            }
        with self.session_factory() as session:
            registration = self.strategy_registrations.get(event.payload.strategy_code)
            strategy = self.store.ensure_strategy(
                session,
                event.payload.strategy_code,
                name=(registration.display_name if registration else event.payload.strategy_code.replace("_", " ").upper()),
                execution_mode=registration.execution_mode if registration else "paper",
                metadata_json=(
                    dict(registration.metadata)
                    if registration
                    else {"account_name": event.payload.broker_account_name}
                ),
            )
            broker_account = self.store.ensure_broker_account(
                session,
                event.payload.broker_account_name,
                provider=self.settings.provider_for_account(event.payload.broker_account_name),
                environment=self.settings.environment,
            )
            intent = self.store.create_trade_intent(
                session,
                strategy=strategy,
                broker_account=broker_account,
                event=event,
            )

            passed, risk_reason = self._evaluate_risk(event)
            outcome = "pass" if passed else "reject"
            self.store.record_risk_check(
                session,
                intent=intent,
                strategy_id=strategy.id,
                broker_account_id=broker_account.id,
                outcome=outcome,
                reason=risk_reason,
                payload={"metadata": dict(event.payload.metadata)},
            )

            if not passed:
                self.store.mark_intent_refused(
                    intent,
                    origin="client_abort",
                    code=risk_reason or "risk_rejected",
                )
                order_event = self._build_rejected_event(event, intent.id)
                session.commit()
                await self._publish_order_event(order_event)
                return [order_event]

            if event.payload.intent_type == "cancel":
                published_events = await self._process_cancel_intent(
                    session=session,
                    strategy_id=strategy.id,
                    broker_account_id=broker_account.id,
                    intent=intent,
                    event=event,
                )
                session.commit()
                for order_event in published_events:
                    await self._publish_order_event(order_event)
                return published_events

            if (
                broker_account.provider == "schwab"
                and event.payload.intent_type == "open"
                and self._has_cached_schwab_ineligible_symbol(
                    session=session,
                    broker_account_id=broker_account.id,
                    symbol=event.payload.symbol,
                )
            ):
                self.store.mark_intent_refused(
                    intent,
                    origin="skipped_before_submit",
                    code="schwab_ineligible_cached",
                )
                order_event = self._build_rejected_event(
                    event,
                    intent.id,
                    reason="schwab_ineligible_cached",
                )
                session.commit()
                # ⛔⭐ NAME THE SUPPRESSION. Risk PASSED (we are past the `not passed` return), no
                # broker order is created, and the intent is marked rejected — so from the log this
                # was indistinguishable from an intent that simply never arrived. That is open
                # thread 4's "unnamed suppression": currently PROTECTIVE, but the same shape as
                # #580 and #608, which both cost money while nobody could see them. One INFO line
                # per drop, at the drop, so the tape carries WHICH gate ate the intent.
                self.logger.info(
                    "[OMS-INTENT-DROPPED] %s %s reason=schwab_ineligible_cached "
                    "(risk PASSED; no broker order created; cached ineligible-today)",
                    event.payload.broker_account_name, event.payload.symbol,
                )
                await self._publish_order_event(order_event)
                return [order_event]

            # Dual-broker fan-out: symmetric Webull ineligible-today short-circuit. A Webull open
            # for a name Webull already rejected as not-tradable today is dropped without a broker
            # round-trip. Gated on the fan-out flag so it is INERT (byte-identical, ORB untouched)
            # when fan-out is off — the table is only ever written under fan-out anyway.
            if (
                broker_account.provider == "webull"
                and bool(getattr(self.settings, "strategy_schwab_1m_v2_dual_broker_fanout_enabled", False))
                and event.payload.intent_type == "open"
                and self._has_cached_webull_ineligible_symbol(
                    session=session,
                    broker_account_id=broker_account.id,
                    symbol=event.payload.symbol,
                )
            ):
                self.store.mark_intent_refused(
                    intent,
                    origin="skipped_before_submit",
                    code="webull_ineligible_cached",
                )
                self.store.record_fanout_pre_submit_outcome(
                    session,
                    intent=intent,
                    outcome="dropped_ineligible",
                    reason="webull_ineligible_cached",
                    broker_account_name=broker_account.name,
                )
                order_event = self._build_rejected_event(
                    event,
                    intent.id,
                    reason="webull_ineligible_cached",
                )
                session.commit()
                # Symmetric with the Schwab branch above — and it must STAY symmetric. A reject
                # query that can see one broker's drops and not the other's reads as a clean on the
                # blind side BY CONSTRUCTION.
                # [[feedback_reject_query_states_account_visibility]]
                self.logger.info(
                    "[OMS-INTENT-DROPPED] %s %s reason=webull_ineligible_cached "
                    "(risk PASSED; no broker order created; cached ineligible-today)",
                    event.payload.broker_account_name, event.payload.symbol,
                )
                await self._publish_order_event(order_event)
                return [order_event]

            # Dual-broker fan-out: collision guard on the SHARED Webull account. The v2 Webull leg
            # must never fight ORB (or a prior un-flat fan-out leg) for the same name on the same
            # account — skip if that account already has the symbol armed / managed / held. Scoped
            # to the v2 Webull fan-out open only; ORB's own opens on the account are untouched
            # (byte-identical when fan-out is off).
            fanout_webull_account = str(
                getattr(self.settings, "strategy_schwab_1m_v2_webull_account_name", "") or ""
            ).strip()
            if (
                bool(getattr(self.settings, "strategy_schwab_1m_v2_dual_broker_fanout_enabled", False))
                and event.payload.strategy_code == "schwab_1m_v2"
                and event.payload.intent_type == "open"
                and str(event.payload.side).lower() == "buy"
                and fanout_webull_account
                and broker_account.name == fanout_webull_account
            ):
                collision = self._fanout_webull_collision_reason(
                    session=session,
                    broker_account_name=broker_account.name,
                    symbol=event.payload.symbol,
                )
                if collision:
                    self.store.mark_intent_refused(
                        intent,
                        origin="skipped_before_submit",
                        code=collision,
                    )
                    self.store.record_fanout_pre_submit_outcome(
                        session,
                        intent=intent,
                        outcome="dropped_collision",
                        reason=collision,
                        broker_account_name=broker_account.name,
                    )
                    order_event = self._build_rejected_event(event, intent.id, reason=collision)
                    session.commit()
                    self.logger.info(
                        "[V2-FANOUT] skip webull leg %s on %s: %s",
                        event.payload.symbol, broker_account.name, collision,
                    )
                    await self._publish_order_event(order_event)
                    return [order_event]

            blocked_reason = await self._get_session_symbol_block_reason(
                account_name=event.payload.broker_account_name,
                symbol=event.payload.symbol,
            )
            if blocked_reason and event.payload.intent_type in {"open", "scale"}:
                self.store.mark_intent_refused(
                    intent,
                    origin="skipped_before_submit",
                    code=blocked_reason,
                )
                self.store.record_fanout_pre_submit_outcome(
                    session,
                    intent=intent,
                    outcome="dropped_risk",
                    reason=blocked_reason,
                    broker_account_name=broker_account.name,
                )
                order_event = self._build_rejected_event(
                    event,
                    intent.id,
                    reason=blocked_reason,
                )
                session.commit()
                await self._publish_order_event(order_event)
                return [order_event]

            pre_submit_events: list[OrderEventEvent] = []
            request_quantity = event.payload.quantity
            if event.payload.intent_type in {"close", "scale"} and event.payload.side == "sell":
                if not self._is_native_stop_guard_metadata(event.payload.metadata):
                    pre_submit_events.extend(
                        await self._cancel_native_stop_guard_before_sell(
                            session=session,
                            strategy=strategy,
                            broker_account=broker_account,
                            symbol=event.payload.symbol,
                        )
                    )
                if (
                    event.payload.intent_type == "close"
                    and str(event.payload.metadata.get("stop_guard", "")).strip().lower() == "true"
                ):
                    pre_submit_events.extend(
                        await self._cancel_open_exit_orders_before_hard_stop(
                            session=session,
                            strategy=strategy,
                            broker_account=broker_account,
                            symbol=event.payload.symbol,
                        )
                    )
                duplicate_exit = self.store.find_open_exit_order(
                    session,
                    strategy_id=strategy.id,
                    broker_account_id=broker_account.id,
                    symbol=event.payload.symbol,
                    include_native_stop_guard=False,
                )
                if duplicate_exit is not None:
                    self.store.mark_intent_refused(
                        intent,
                        origin="skipped_before_submit",
                        code="duplicate_exit_in_flight",
                    )
                    order_event = self._build_rejected_event(
                        event,
                        intent.id,
                        reason="duplicate_exit_in_flight",
                    )
                    session.commit()
                    for prior_event in pre_submit_events:
                        await self._publish_order_event(prior_event)
                    await self._publish_order_event(order_event)
                    return [*pre_submit_events, order_event]

                virtual_position = self.store.get_virtual_position(
                    session,
                    strategy_id=strategy.id,
                    broker_account_id=broker_account.id,
                    symbol=event.payload.symbol,
                )
                strategy_available_quantity = (
                    virtual_position.quantity
                    if virtual_position is not None and virtual_position.quantity > 0
                    else Decimal("0")
                )
                if strategy_available_quantity <= 0:
                    self.store.mark_intent_refused(
                        intent,
                        origin="skipped_before_submit",
                        code="no_strategy_position_available_to_sell",
                    )
                    order_event = self._build_rejected_event(
                        event,
                        intent.id,
                        reason="no strategy position available to sell",
                    )
                    session.commit()
                    await self._publish_order_event(order_event)
                    return [order_event]

                account_position = self.store.get_account_position(
                    session,
                    broker_account_id=broker_account.id,
                    symbol=event.payload.symbol,
                )
                available_quantity = (
                    account_position.quantity
                    if account_position is not None and account_position.quantity > 0
                    else Decimal("0")
                )
                if available_quantity <= 0:
                    available_quantity = await self._refresh_broker_position_quantity(
                        session=session,
                        broker_account_id=broker_account.id,
                        broker_account_name=broker_account.name,
                        symbol=event.payload.symbol,
                    )
                if available_quantity <= 0:
                    self.store.mark_intent_refused(
                        intent,
                        origin="skipped_before_submit",
                        code="no_broker_position_available_to_sell",
                    )
                    order_event = self._build_rejected_event(
                        event,
                        intent.id,
                        reason="no broker position available to sell",
                    )
                    session.commit()
                    await self._publish_order_event(order_event)
                    return [order_event]

                reserved_exit_quantity = self.store.get_open_exit_reserved_quantity(
                    session,
                    broker_account_id=broker_account.id,
                    symbol=event.payload.symbol,
                    include_native_stop_guard=False,
                )
                remaining_account_quantity = max(Decimal("0"), available_quantity - reserved_exit_quantity)
                if remaining_account_quantity <= 0:
                    self.store.mark_intent_refused(
                        intent,
                        origin="skipped_before_submit",
                        code="broker_quantity_reserved_for_pending_exits",
                    )
                    order_event = self._build_rejected_event(
                        event,
                        intent.id,
                        reason="broker quantity already reserved for pending exits",
                    )
                    session.commit()
                    for prior_event in pre_submit_events:
                        await self._publish_order_event(prior_event)
                    await self._publish_order_event(order_event)
                    return [*pre_submit_events, order_event]

                request_quantity = min(
                    event.payload.quantity,
                    strategy_available_quantity,
                    remaining_account_quantity,
                )
                intent.quantity = request_quantity

            # Piece 1: ORB entry priced off the OMS live quote at placement (flag-gated;
            # no-op when off / non-ORB). Mutates event.payload.metadata's limit in place,
            # or returns a rejected event (abandon) which short-circuits before any submit.
            orb_abandon_event = self._apply_orb_quote_priced_entry(
                session=session, event=event, intent=intent
            )
            if orb_abandon_event is not None:
                session.commit()
                for prior_event in pre_submit_events:
                    await self._publish_order_event(prior_event)
                await self._publish_order_event(orb_abandon_event)
                return [*pre_submit_events, orb_abandon_event]

            # RESTING-ENTRY dedup (restart safety): a v2 resting buy-stop-limit place is SKIPPED if an
            # open resting entry already exists for this symbol -- e.g. one survived a restart while
            # the strategy lost its in-memory resting flag and re-placed. Never two live resting orders
            # => never a double-fill/oversell. The strategy re-emits every bar; the existing order is
            # managed via the ratchet/cancel path. Non-resting intents are unaffected (byte-identical).
            if str(event.payload.metadata.get("resting_entry", "")).lower() == "true" and \
                    self._resting_entry_already_open(
                        session, event.payload.broker_account_name, event.payload.symbol):
                self.store.mark_intent_refused(
                    intent,
                    origin="skipped_before_submit",
                    code="resting_entry_restart_dedup",
                )
                self.store.record_fanout_pre_submit_outcome(
                    session,
                    intent=intent,
                    outcome="dropped_dedup",
                    reason="resting entry already open (restart dedup)",
                    broker_account_name=broker_account.name,
                )
                order_event = self._build_rejected_event(
                    event, intent.id, reason="resting entry already open (restart dedup)"
                )
                session.commit()
                for prior_event in pre_submit_events:
                    await self._publish_order_event(prior_event)
                await self._publish_order_event(order_event)
                self.logger.info(
                    "[V2-RESTING-DEDUP] %s %s -- open resting entry exists; skipped the duplicate place",
                    event.payload.broker_account_name, event.payload.symbol,
                )
                return [*pre_submit_events, order_event]

            # Piece: attach native-OCO bracket metadata to the v2 entry (flag-gated; no-op /
            # byte-identical when off or non-v2-entry). Mutates event.payload.metadata in place
            # so the same dict copy below carries the bracket fields to the adapter. RTH-only:
            # in EH this is a no-op (the native OCO is a regular-session construct).
            self._apply_v2_oco_bracket_entry(event=event)

            # P-B1: re-price the v2 REACTIVE entry as a marketable, max-cross-capped EH-LIMIT off the
            # OMS's own fresh ask (flag-gated OFF; no-op / byte-identical when off or non-v2-EH-reactive
            # -> the bot's plain limit-at-ask stands). Mutually exclusive with the bracket above (that is
            # RTH-only, this is EH-only). Returns a rejected event (abandon) which short-circuits before
            # any submit — the conservative "no fill beats a bad thin-pre-market fill" bias.
            eh_abandon_event = self._apply_v2_eh_reactive_entry(
                session=session, event=event, intent=intent
            )
            if eh_abandon_event is not None:
                self.store.record_fanout_pre_submit_outcome(
                    session,
                    intent=intent,
                    outcome="rejected_client_abort",
                    reason=str(eh_abandon_event.payload.reason or "eh_reactive_abandon"),
                    broker_account_name=broker_account.name,
                )
                session.commit()
                for prior_event in pre_submit_events:
                    await self._publish_order_event(prior_event)
                await self._publish_order_event(eh_abandon_event)
                return [*pre_submit_events, eh_abandon_event]

            # P-B2: band-cap the v2 EH RESTING entry (the software-emulated marketable EH-LIMIT the strategy
            # emits on the ATR up-cross, tagged eh_resting=true) off the OMS's own fresh ask -> min(ask,
            # level*(1+band)); ASK past the band or no fresh ask -> ABANDON. Flag-gated OFF / EH-only /
            # mutually exclusive with the reactive builder (that excludes resting_entry) and the RTH-only
            # OCO bracket. No-op / byte-identical when off or not a v2 EH resting open.
            resting_eh_abandon_event = self._apply_v2_eh_resting_entry(
                session=session, event=event, intent=intent
            )
            if resting_eh_abandon_event is not None:
                self.store.record_fanout_pre_submit_outcome(
                    session,
                    intent=intent,
                    outcome="rejected_client_abort",
                    reason=str(resting_eh_abandon_event.payload.reason or "eh_resting_abandon"),
                    broker_account_name=broker_account.name,
                )
                session.commit()
                for prior_event in pre_submit_events:
                    await self._publish_order_event(prior_event)
                await self._publish_order_event(resting_eh_abandon_event)
                return [*pre_submit_events, resting_eh_abandon_event]

            # RTH REACTIVE band-capped LIMIT (2026-08-10). The reactive path sent a MARKET order with
            # no ceiling: measured 21d on live:schwab_1m_v2, reactive MARKET SD 58.6 bps / worst
            # +351.7, against ~25-28 bps / +60.2 on the price-committed paths. The >=200 bps entries
            # are UNBOUNDED-PRICE events, not late-arrival events — a price cap caps the price.
            # Runs AFTER the EH builder and is mutually exclusive with it (RTH-only vs EH-only).
            rth_reactive_abandon_event = self._apply_v2_rth_reactive_limit(
                event=event, intent=intent
            )
            # RTH FAN-OUT band-capped LIMIT (2026-08-12). #674 above capped only the SCHWAB primary
            # ("the fan-out leg is deliberately untouched here"), so the Webull leg still went out as
            # an UNCAPPED MARKET in regular hours — on BOTH fan-out sources. Live 2026-08-12 BAOS:
            # the primary decided 1.1702 under its cap while this leg paid 1.1800 and lost 5.08%.
            # Mutually exclusive with the reactive builder above (that one excludes fanout legs, this
            # one requires them), so exactly one can fire for any single intent.
            if rth_reactive_abandon_event is None:
                rth_reactive_abandon_event = self._apply_v2_rth_fanout_limit(
                    event=event, intent=intent
                )
            if rth_reactive_abandon_event is not None:
                self.store.record_fanout_pre_submit_outcome(
                    session,
                    intent=intent,
                    outcome="rejected_client_abort",
                    reason=str(rth_reactive_abandon_event.payload.reason or "rth_fanout_abandon"),
                    broker_account_name=broker_account.name,
                )
                session.commit()
                for prior_event in pre_submit_events:
                    await self._publish_order_event(prior_event)
                await self._publish_order_event(rth_reactive_abandon_event)
                return [*pre_submit_events, rth_reactive_abandon_event]

            client_order_id = self._build_client_order_id(event)
            request = OrderRequest(
                client_order_id=client_order_id,
                broker_account_name=event.payload.broker_account_name,
                strategy_code=event.payload.strategy_code,
                symbol=event.payload.symbol,
                side=event.payload.side,
                intent_type=event.payload.intent_type,
                quantity=request_quantity,
                reason=event.payload.reason,
                metadata=dict(event.payload.metadata),
                order_type=str(event.payload.metadata.get("order_type", "market")),
                time_in_force=str(event.payload.metadata.get("time_in_force", "day")),
            )
            reports = await self.broker_adapter.submit_order(request)
            published_events = [*pre_submit_events]
            published_events.extend(await self._record_order_reports(
                session=session,
                intent=intent,
                strategy_id=strategy.id,
                broker_account_id=broker_account.id,
                intent_event=event,
                request=request,
                reports=reports,
            ))
            stop_reject_reason = self._stop_reject_reason(request=request, reports=reports)
            if stop_reject_reason:
                published_events.extend(
                    await self._process_stop_reject_market_fallback(
                        session=session,
                        strategy=strategy,
                        broker_account=broker_account,
                        original_event=event,
                        original_request=request,
                        rejection_reason=stop_reject_reason,
                    )
                )

            if (
                request.side == "sell"
                and request.intent_type in {"close", "scale"}
                and not self._is_native_stop_guard_metadata(request.metadata)
                and str(request.metadata.get("stop_guard", "")).strip().lower() != "true"
                and not any(
                    item.payload.status in {"accepted", "submitted", "partially_filled", "filled"}
                    for item in published_events
                )
            ):
                published_events.extend(
                    await self._rearm_native_stop_from_registry(
                        session=session,
                        strategy_id=strategy.id,
                        broker_account_id=broker_account.id,
                        strategy_code=event.payload.strategy_code,
                        broker_account_name=event.payload.broker_account_name,
                        symbol=event.payload.symbol,
                    )
                )

            session.commit()

        for order_event in published_events:
            await self._publish_order_event(order_event)

        # F2: mirror any armed-stop changes made by _record_order_reports (arm on a
        # buy-open fill, decrement/clear on a sell fill) to the durable table, off-loop.
        await self._flush_dirty_armed_stops()
        await self._reconcile_after_intent(event.payload.broker_account_name)

        # Webull mirror moved OFF the submit path (2026-07-24). The resting v2 entry PLACES
        # its Schwab order long before the up-cross, so mirroring at placement would enter
        # Webull early/wrong; a buy-STOP master is also structurally rejected by Webull's
        # combo builder ("Fork A"). The mirror now fires on the confirmed Schwab FILL from
        # `sync_broker_orders` as a MARKET master + native-OCO combo. See
        # docs/webull-mirror-on-fill-design.md.
        return published_events

    async def _mirror_v2_fill_to_webull(
        self,
        *,
        symbol: str,
        quantity,
        schwab_fill_price: float,
        source_metadata: dict[str, str],
    ) -> None:
        """Mirror a CONFIRMED primary Schwab v2 buy-open FILL to a SECOND (Webull) account as
        a native-OCO combo (MARKET master + take-profit LIMIT + stop-loss STOP). Fired from the
        async fill path (`sync_broker_orders`) once per real open — NOT at placement — because
        the resting v2 entry rests until the up-cross, so mirroring at submit would enter Webull
        early/wrong, and Webull structurally refuses a buy-STOP master ("Fork A"). Runs in its
        OWN session, wrapped so any Webull error NEVER unwinds the already-committed Schwab leg.
        The whole thing is dormant/byte-identical when the mirror flag is off (caller-guarded on
        strategy/account/side/intent; this method re-checks the flag + account for defence).

        Exit anchor (operator, 2026-07-23): the exits key off the WEBULL fill, proxied by the
        live ask at submit — a MARKET master fills at/near the ask, so target/stop geometry is
        correct to within the spread. Fall back to the Schwab fill price when no fresh ask is
        available. Both the assumed anchor and its source are logged so drift is measurable.

        See docs/webull-mirror-on-fill-design.md.
        """
        if not bool(getattr(self.settings, "strategy_schwab_1m_v2_webull_mirror_enabled", False)):
            return
        # FAIL-SAFE (operator, 2026-07-15): the mirror account must be EXPLICIT. The old default
        # was "live:orb" -- ORB's own live account -- which contradicts the 07-10 design decision
        # (a separate live:v2_webull, because ORB and v2 trade the same watchlist through different
        # exit logic and one account cannot hold two open managed rows for the same symbol).
        # Unset -> no-op + warn, so a flag-flip without provisioning does NOTHING.
        webull_account_name = str(
            getattr(self.settings, "strategy_schwab_1m_v2_webull_account_name", "") or ""
        ).strip()
        if not webull_account_name:
            self.logger.warning(
                "[OMS-V2-MIRROR] mirror flag is ON but strategy_schwab_1m_v2_webull_account_name "
                "is UNSET — refusing to mirror %s. Provision a dedicated account (e.g. "
                "live:v2_webull); do NOT point this at live:orb.",
                symbol,
            )
            return
        primary_account = str(getattr(self.settings, "strategy_schwab_1m_v2_account_name", "") or "").strip()
        if webull_account_name == primary_account:
            self.logger.warning(
                "[OMS-V2-MIRROR] mirror enabled but webull account name equals the primary (%r) "
                "— skipping mirror for %s",
                webull_account_name, symbol,
            )
            return

        try:
            # Exit anchor: the live ask at submit (a MARKET master fills at/near the ask), else
            # the Schwab fill price. Only a FRESH ask is trusted — a stale/absent quote falls
            # back rather than anchoring exits to a gap.
            anchor = float(schwab_fill_price)
            anchor_source = "schwab_fill"
            quote = self._latest_quotes_by_symbol.get(symbol)
            if quote:
                received_at = quote.get("received_at")
                fresh = isinstance(received_at, datetime)
                if fresh:
                    age_ms = (utcnow() - received_at).total_seconds() * 1000.0
                    if age_ms > float(getattr(self.settings, "oms_v2_exit_quote_max_age_ms", 5000)):
                        fresh = False
                ask = quote.get("ask")
                if fresh and ask:
                    try:
                        ask_f = float(ask)
                    except (TypeError, ValueError):
                        ask_f = 0.0
                    if ask_f > 0:
                        anchor = ask_f
                        anchor_source = "live_ask"
            self.logger.info(
                "[OMS-V2-MIRROR] %s anchor=%.4f source=%s (schwab_fill=%.4f)",
                symbol, anchor, anchor_source, float(schwab_fill_price),
            )

            target = anchor * (1.0 + self._cw_target_pct / 100.0)
            protect = anchor * (1.0 - self._cw_stop_pct / 100.0)
            # Webull native-OCO combo request: MARKET master (no limit_price — bracket_entry_type
            # MARKET) + take-profit LIMIT + stop-loss STOP. Drop the resting stop_price entirely
            # (the cross already happened). The adapter reads bracket / bracket_entry_type /
            # bracket_target_price / bracket_stop_price from metadata.
            combo_metadata: dict[str, str] = {
                "order_type": "market",
                "time_in_force": "day",
                "bracket": "true",
                "native_oco_bracket": "true",
                "bracket_entry_type": "MARKET",
                "bracket_target_price": f"{target:.4f}",
                "bracket_stop_price": f"{protect:.4f}",
                "path": source_metadata.get("path", ""),
            }

            # EH mirror (flag-gated OFF; byte-identical when off): a MARKET master + native-OCO
            # combo are BOTH RTH-only on Webull (417 in EH), so when the primary Schwab fill lands
            # in extended hours we swap the combo for a single-leg marketable EH-LIMIT master (no
            # OCO). The mirrored Webull position is then exit-managed by the account-aware software
            # EH-limit CW ladder (#390) — no naked EH position. RTH, or flag-off, keeps the combo
            # below byte-identical.
            order_metadata = combo_metadata
            order_type_str = "market"
            if (
                bool(getattr(self.settings, "strategy_schwab_1m_v2_webull_mirror_eh_enabled", False))
                and not _is_regular_market_session()
            ):
                eh_master = self._build_v2_mirror_eh_master(
                    symbol=symbol,
                    schwab_fill_price=float(schwab_fill_price),
                    source_metadata=source_metadata,
                )
                if eh_master is None:
                    # ABANDON: no fresh ask / ask past the max-cross cap. Nothing was opened on
                    # Webull, so there is no position to protect — prefer no fill to a bad thin-EH
                    # fill. No submit, logged inside the builder.
                    return
                order_metadata = eh_master
                order_type_str = "limit"

            # Distinct event → distinct event_id (uuid4) → distinct client_order_id, so the
            # mirror leg can never collide with the primary's order/intent rows.
            mirror_event = TradeIntentEvent(
                source_service=SERVICE_NAME,
                payload=TradeIntentPayload(
                    strategy_code="schwab_1m_v2",
                    broker_account_name=webull_account_name,
                    symbol=symbol,
                    side="buy",
                    quantity=quantity,
                    intent_type="open",
                    reason="oms_v2_webull_mirror_on_fill",
                    metadata=order_metadata,
                ),
            )
            published_events: list[OrderEventEvent] = []
            with self.session_factory() as session:
                registration = self.strategy_registrations.get("schwab_1m_v2")
                strategy = self.store.ensure_strategy(
                    session,
                    "schwab_1m_v2",
                    name=(
                        registration.display_name
                        if registration
                        else "SCHWAB 1M V2"
                    ),
                    execution_mode=registration.execution_mode if registration else "paper",
                    metadata_json=(
                        dict(registration.metadata)
                        if registration
                        else {"account_name": webull_account_name}
                    ),
                )
                broker_account = self.store.ensure_broker_account(
                    session,
                    webull_account_name,
                    provider=self.settings.provider_for_account(webull_account_name),
                    environment=self.settings.environment,
                )
                # Collision guard: v2 SHARES the Webull account with ORB (operator has no
                # separate account). If ANY strategy already holds this symbol on that account,
                # SKIP the mirror — never fight ORB for the same name or the same shared broker
                # position (the unique (account,symbol) open-managed-row + a doubled broker lot).
                armed_here = any(
                    st.broker_account_name == webull_account_name and st.symbol == symbol
                    for st in self._armed_hard_stops.values()
                )
                managed_here = self.store.get_open_managed_position(
                    session, broker_account_name=webull_account_name, symbol=symbol
                )
                held_qty = self.store.get_account_position_qty_by_name(
                    session, broker_account_name=webull_account_name, symbol=symbol
                )
                if armed_here or managed_here is not None or held_qty != 0:
                    self.logger.info(
                        "[OMS-V2-MIRROR] skip %s: %s already holds it (armed=%s managed=%s qty=%s) "
                        "— no collision with ORB",
                        symbol, webull_account_name, armed_here, managed_here is not None, held_qty,
                    )
                    return
                intent = self.store.create_trade_intent(
                    session,
                    strategy=strategy,
                    broker_account=broker_account,
                    event=mirror_event,
                )
                request = OrderRequest(
                    client_order_id=self._build_client_order_id(mirror_event),
                    broker_account_name=webull_account_name,
                    strategy_code=mirror_event.payload.strategy_code,
                    symbol=mirror_event.payload.symbol,
                    side=mirror_event.payload.side,
                    intent_type=mirror_event.payload.intent_type,
                    quantity=mirror_event.payload.quantity,
                    reason=mirror_event.payload.reason,
                    metadata=dict(order_metadata),
                    order_type=order_type_str,
                    time_in_force="day",
                )
                reports = await self.broker_adapter.submit_order(request)
                published_events.extend(
                    await self._record_order_reports(
                        session=session,
                        intent=intent,
                        strategy_id=strategy.id,
                        broker_account_id=broker_account.id,
                        intent_event=mirror_event,
                        request=request,
                        reports=reports,
                    )
                )
                session.commit()

            for order_event in published_events:
                await self._publish_order_event(order_event)

            # Mirror any armed-stop the webull buy-open fill created to the durable table.
            await self._flush_dirty_armed_stops()

            statuses = ",".join(item.payload.status for item in published_events) or "none"
            self.logger.info(
                "[OMS-V2-MIRROR] webull mirror-on-fill submitted sym=%s acct=%s qty=%s "
                "kind=%s target=%.4f protect=%.4f status=%s",
                symbol, webull_account_name, quantity,
                ("EH-LIMIT-single-leg" if order_type_str == "limit" else "MARKET+OCO"),
                target, protect, statuses,
            )
        except Exception as exc:  # noqa: BLE001 — a webull failure must NEVER affect the primary leg
            self.logger.warning("[OMS-V2-MIRROR] webull mirror-on-fill failed for %s: %s", symbol, exc)

    def _build_v2_mirror_eh_master(
        self,
        *,
        symbol: str,
        schwab_fill_price: float,
        source_metadata: dict[str, str],
    ) -> dict[str, str] | None:
        """Metadata for the EXTENDED-HOURS Webull mirror master: a marketable EH-LIMIT single-leg
        (NO native-OCO combo — the broker OCO is RTH-only and 417s in EH; see
        docs/premarket-eod-exit-design.md). Priced off the OMS's OWN fresh ask
        (`_latest_quotes_by_symbol` = Polygon NBBO, the ONLY feed the Webull mirror can price from —
        no Webull market-data entitlement, webull.py:521), buffered just above the ask so it crosses,
        and BOUNDED by a max-cross cap vs the primary Schwab FILL price so a thin-EH ask cannot chase
        far past where the primary actually entered. Reuses the P-B1 EH-reactive-entry constants
        (`oms_v2_eh_entry_*`) so the pricing/cap/quote-age bias is IDENTICAL across the EH entry paths.

        Returns None to ABANDON (no submit — no order is ever placed) when there is no fresh ask or
        the ask is past the cap: prefer NO fill to a bad thin-pre-market fill (design risk #3). Since
        the mirror opens nothing on abandon there is no naked EH position.

        Carries NO target/protect: the EH mirror emits no broker bracket. The mirrored Webull
        position is exit-managed by the account-aware software CW EH-limit ladder — `_emit_v2_exit`
        routes off `row.broker_account_name` and EH-routes via `_extended_hours_session()` (#390), and
        `_native_oco_stand_down_active` fails OPEN with no confirmed combo, so the ladder runs and
        anchors +2%/−5% off the ACTUAL Webull fill (more accurate than an assumed ask)."""
        max_age_ms = int(getattr(self.settings, "oms_v2_eh_entry_quote_max_age_ms", 2000))
        ask = self._fresh_ask(symbol, max_age_ms)
        if ask is None:
            self.logger.info(
                "[OMS-V2-MIRROR-EH] ABANDON %s: no fresh ask within %dms — no blind EH mirror",
                symbol, max_age_ms,
            )
            return None
        max_cross_pct = float(getattr(self.settings, "oms_v2_eh_entry_max_cross_pct", 1.0))
        cap = float(schwab_fill_price) * (1.0 + max_cross_pct / 100.0)
        if ask > cap:
            self.logger.info(
                "[OMS-V2-MIRROR-EH] ABANDON %s: ask %.4f past max-cross cap %.4f "
                "(schwab_fill %.4f +%.2f%%) — no chase",
                symbol, ask, cap, float(schwab_fill_price), max_cross_pct,
            )
            return None
        buffer_pct = float(getattr(self.settings, "oms_v2_eh_entry_limit_buffer_pct", 0.3))
        tick = Decimal("0.01") if ask >= 1.0 else Decimal("0.0001")
        buffered = Decimal(str(ask)) * (Decimal("1") + Decimal(str(buffer_pct)) / Decimal("100"))
        # min(buffered, cap) then ROUND_DOWN so tick-alignment can never push the limit above the cap.
        limit = min(buffered, Decimal(str(cap)))
        limit_s = format(limit.quantize(tick, rounding=ROUND_DOWN), "f")
        session_code = _extended_hours_session() or ""
        self.logger.info(
            "[OMS-V2-MIRROR-EH] %s session=%s ask=%.4f schwab_fill=%.4f cap=%.4f limit=%s "
            "(EH-LIMIT single-leg, no OCO)",
            symbol, session_code, ask, float(schwab_fill_price), cap, limit_s,
        )
        return {
            "order_type": "limit",
            "time_in_force": "day",
            "limit_price": limit_s,
            "reference_price": limit_s,
            "session": session_code,
            "extended_hours": "true",
            "price_source": "ask",
            "oms_v2_mirror_eh": "true",
            "oms_v2_mirror_eh_ask": f"{ask:.4f}",
            "oms_v2_mirror_eh_cap": f"{cap:.4f}",
            "path": source_metadata.get("path", ""),
        }

    async def _process_cancel_intent(
        self,
        *,
        session: Session,
        strategy_id: UUID,
        broker_account_id: UUID,
        intent,
        event: TradeIntentEvent,
    ) -> list[OrderEventEvent]:
        metadata = dict(event.payload.metadata)
        target_order = self.store.find_open_order_for_cancel(
            session,
            strategy_id=strategy_id,
            broker_account_id=broker_account_id,
            symbol=event.payload.symbol,
            metadata=metadata,
        )
        if target_order is None:
            self.store.mark_intent_refused(
                intent,
                origin="skipped_before_submit",
                code="cancel_target_not_found",
            )
            return [
                self._build_rejected_event(
                    event,
                    intent.id,
                    reason="cancel_target_not_found",
                )
            ]

        metadata.setdefault("target_client_order_id", target_order.client_order_id)
        if target_order.broker_order_id:
            metadata.setdefault("broker_order_id", target_order.broker_order_id)
        # A cancel is not a new placement attempt.  Keep the target order's
        # existing chain identity on the cancel request/outcome rather than
        # replacing it with identity derived from the cancel intent.
        metadata = carry_fanout_identity(metadata, target_order.payload or {})

        terminal_cancel_reports = self.store.count_terminal_cancel_refusals(
            session,
            order_id=target_order.id,
        )
        if terminal_cancel_reports >= self._CANCEL_DEAD_TARGET_BROKER_REPORT_BOUND:
            # LASE 2026-06-02 is the production control: one target received 48 identical
            # ``FILLED cannot be canceled`` broker replies in 15.889 seconds.  The first reply is
            # evidence; the other 47 are a retry storm.  Refuse locally after the evidence exists
            # and keep the target id on the event so the caller/page can name what was bounded.
            self.store.mark_intent_refused(
                intent,
                origin="client_abort",
                code="cancel_dead_target_retry_bound",
            )
            self.logger.warning(
                "[OMS-CANCEL-DEAD-TARGET-BOUND] symbol=%s acct=%s target_order_id=%s "
                "target_client_order_id=%s terminal_reports=%d bound=%d outcome=refused "
                "reset=new_target_order_id",
                target_order.symbol,
                event.payload.broker_account_name,
                target_order.id,
                target_order.client_order_id,
                terminal_cancel_reports,
                self._CANCEL_DEAD_TARGET_BROKER_REPORT_BOUND,
            )
            rejected_event = self._build_rejected_event(
                event,
                intent.id,
                reason="cancel_dead_target_retry_bound",
            )
            rejected_event.payload.client_order_id = target_order.client_order_id
            rejected_event.payload.broker_order_id = target_order.broker_order_id
            rejected_event.payload.metadata = {
                **metadata,
                "cancel_dead_target_terminal_reports": str(terminal_cancel_reports),
                "cancel_dead_target_bound": str(
                    self._CANCEL_DEAD_TARGET_BROKER_REPORT_BOUND
                ),
                "cancel_dead_target_bound_reset": "new_target_order_id",
            }
            return [rejected_event]

        request = OrderRequest(
            client_order_id=target_order.client_order_id,
            broker_account_name=event.payload.broker_account_name,
            strategy_code=event.payload.strategy_code,
            symbol=target_order.symbol,
            side=target_order.side,  # type: ignore[arg-type]
            intent_type="cancel",
            quantity=target_order.quantity,
            reason=event.payload.reason,
            metadata=metadata,
            order_type=target_order.order_type,
            time_in_force=target_order.time_in_force,
        )
        verify_enabled = bool(getattr(self.settings, "oms_cancel_verify_enabled", False))
        try:
            reports = await self.broker_adapter.submit_order(request)
        except Exception:
            # ⛔⭐⭐ A RAISED CANCEL IS AN UNKNOWN, NOT A FAILURE.
            # FRTT 2026-08-11: the cancel call died on the network and the order stayed WORKING for
            # 136 minutes. The request may or may not have reached the broker — the ONLY way to know
            # is to read the order back, which is exactly what verification does. So with the flag on
            # we swallow the raise HERE and let the verifier resolve it; with the flag off the
            # behaviour is byte-identical (the exception propagates as it always has).
            if not verify_enabled:
                raise
            self.logger.warning(
                "[OMS-CANCEL-SUBMIT-RAISED] %s %s coid=%s — cancel call raised; the order's state is "
                "UNKNOWN, not failed. Verifier will read it back.",
                target_order.symbol, event.payload.broker_account_name, target_order.client_order_id,
                exc_info=True,
            )
            reports = []
        published_events: list[OrderEventEvent] = []

        if verify_enabled:
            self._spawn_cancel_verification(
                request=request,
                symbol=target_order.symbol,
                account_name=event.payload.broker_account_name,
                client_order_id=target_order.client_order_id,
                broker_order_id=target_order.broker_order_id,
                target_order_id=target_order.id,
            )

        for report in reports:
            order = self.store.update_order_from_report(
                target_order,
                report=report,
                metadata=carry_fanout_identity(
                    report.metadata, request.metadata
                ),
                preserve_status=report.event_type == "rejected",
            )
            recorded_metadata = carry_fanout_identity(
                report.metadata, request.metadata
            )
            payload = {
                "client_order_id": report.client_order_id,
                "broker_order_id": report.broker_order_id,
                "broker_fill_id": report.broker_fill_id,
                "metadata": recorded_metadata,
                "reason": report.reason,
            }
            self._append_order_event_isolated(session, order=order, report=report, payload=payload)
            # ⛔⭐ A CANCEL INTENT TRACKS THE REQUEST; THE ORDER TRACKS THE OUTCOME.
            #
            # This used to copy `report.event_type` straight onto the intent. For a cancel the
            # report describes the TARGET ORDER's current state — and Schwab answers a just-issued
            # DELETE with `PENDING_CANCEL`, which `_map_order_status` maps into ACCEPTED_STATUSES.
            # So the intent was marked "accepted": non-terminal, and nothing ever polls a cancel
            # intent again. It sat there forever.
            #
            # Measured 2026-07-30: 11 stuck cancel intents by mid-session, oldest 209 minutes, all
            # `accepted` with zero broker orders of their own — and every one of their TARGET
            # ORDERS had already reached `cancelled`/`filled`. The cancels all SUCCEEDED; only the
            # bookkeeping was abandoned. Each one then produced a `stuck_intent` reconciler finding
            # every 30s: 3,954 warnings in a day, burying everything else in that table.
            #
            # The request is complete once the broker has ACKNOWLEDGED it, and PENDING_CANCEL is an
            # acknowledgement. Whether the order then cancels or races to a fill is the ORDER's
            # story, tracked on the order row, which stays open for the reconcile sweep.
            # A terminal report still wins — including "filled", which honestly records that the
            # cancel lost the race.
            self.store.mark_intent_from_report(
                intent,
                report,
                status=resolve_cancel_intent_status(
                    event.payload.intent_type,
                    report.event_type,
                ),
            )
            published_events.append(
                self._build_order_event(
                    intent_event=event,
                intent_db_id=intent.id,
                order_db_id=order.id,
                report=report,
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                )
            )

        return published_events

    # ------------------------------------------------------------------ cancel-verify (2026-08-12)
    def _spawn_cancel_verification(
        self,
        *,
        request: OrderRequest,
        symbol: str,
        account_name: str,
        client_order_id: str,
        broker_order_id: str | None,
        target_order_id: UUID | None = None,
    ) -> "asyncio.Task[str | None]":
        """Run `_verify_cancel_landed` OFF the intent path.

        ⛔ Deliberately backgrounded. Verification polls with sleeps, and the worst case is several
        seconds; doing that inline would stall `process_trade_intent` — which also carries EXITS.
        Delaying an exit to confirm a cancel would trade a rare unowned order for a common late
        stop, which is the wrong direction. The hole being closed is 136 MINUTES wide, so a few
        seconds of asynchrony costs nothing.

        The task is retained so it cannot be garbage-collected mid-flight (the asyncio footgun) and
        so tests can await it deterministically instead of sleeping.
        """
        task = asyncio.ensure_future(
            self._verify_cancel_landed(
                request=request,
                symbol=symbol,
                account_name=account_name,
                client_order_id=client_order_id,
                broker_order_id=broker_order_id,
                target_order_id=target_order_id,
            )
        )
        tasks = getattr(self, "_cancel_verify_tasks", None)
        if tasks is None:
            tasks = set()
            self._cancel_verify_tasks = tasks
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def _verify_cancel_landed(
        self,
        *,
        request: OrderRequest,
        symbol: str,
        account_name: str,
        client_order_id: str,
        broker_order_id: str | None,
        target_order_id: UUID | None = None,
    ) -> str | None:
        """Read the cancel TARGET back until it is settled; re-submit the cancel if it is not.

        ⛔⭐⭐ THE DEFECT THIS CLOSES. We treated the ATTEMPT as the OUTCOME. `_process_cancel_intent`
        submitted a cancel, recorded whatever the broker said about the target order, and never asked
        again. On 2026-08-11 the FRTT cancel died on the network and the order stayed WORKING and
        unowned for 136 minutes until the operator killed it by hand.

        ⛔ Neither an exception nor an `accepted`/`PENDING_CANCEL` report tells you what happened.
        Both are unknowns. This resolves the unknown the only way it can be resolved — by reading the
        order back from the broker — and re-submits if the read says it is still working.

        ⚠️ NEVER SILENT. Every exit from this function logs: confirmed, or a loud
        `[OMS-CANCEL-UNCONFIRMED]` carrying the ids an operator needs. An unverifiable cancel is a
        WARNING, not an absence — a cancel path that can fail quietly is the same class of defect as
        the one it is fixing.

        Returns the last observed status, or None if the order could never be read.
        """
        attempts = max(1, int(getattr(self.settings, "oms_cancel_verify_attempts", 3) or 3))
        interval = max(0.0, float(getattr(self.settings, "oms_cancel_verify_interval_seconds", 2.0) or 0.0))
        resubmits = max(0, int(getattr(self.settings, "oms_cancel_verify_resubmits", 1) or 0))

        observed: str | None = None
        reads_failed = 0
        for submit_round in range(resubmits + 1):
            for _ in range(attempts):
                if interval:
                    await asyncio.sleep(interval)
                try:
                    report = await self.broker_adapter.fetch_order_update(request)
                except Exception:
                    # A failed READ is also an unknown — keep trying, never conclude "gone".
                    reads_failed += 1
                    self.logger.warning(
                        "[OMS-CANCEL-VERIFY-READ-FAILED] %s %s coid=%s — could not read the order back",
                        symbol, account_name, client_order_id, exc_info=True,
                    )
                    continue
                if report is None:
                    reads_failed += 1
                    continue
                observed = str(getattr(report, "event_type", "") or "")
                if observed in _CANCEL_TARGET_SETTLED_STATUSES:
                    self.logger.info(
                        "[OMS-CANCEL-CONFIRMED] %s %s coid=%s broker_id=%s settled=%s "
                        "(round=%d) — the order is off the book",
                        symbol, account_name, client_order_id, broker_order_id, observed,
                        submit_round,
                    )
                    return observed
            if submit_round < resubmits:
                if target_order_id is not None:
                    bound_reached = await self._run_db(
                        lambda session: (
                            (order := session.get(BrokerOrder, target_order_id)) is not None
                            and self._direct_cancel_dead_target_bound_reached(
                                session,
                                order=order,
                                path="verify_cancel_landed",
                            )
                        ),
                        commit=False,
                    )
                    if bound_reached:
                        self._log_cancel_unconfirmed(
                            symbol=symbol,
                            account_name=account_name,
                            client_order_id=client_order_id,
                            broker_order_id=broker_order_id,
                            observed=observed,
                            reads_failed=reads_failed,
                            bounded_by_dead_target=True,
                        )
                        return observed
                self.logger.warning(
                    "[OMS-CANCEL-RESUBMIT] %s %s coid=%s still reads %s after %d reads — re-sending "
                    "the cancel (round %d of %d)",
                    symbol, account_name, client_order_id, observed or "UNREADABLE",
                    attempts, submit_round + 1, resubmits,
                )
                try:
                    reports = await self.broker_adapter.submit_order(request)
                    if target_order_id is not None and reports:
                        await self._run_db(
                            lambda session: self._record_cancel_verify_reports(
                                session,
                                target_order_id=target_order_id,
                                reports=reports,
                            ),
                            commit=True,
                        )
                except Exception:
                    self.logger.warning(
                        "[OMS-CANCEL-RESUBMIT-RAISED] %s %s coid=%s — re-sent cancel raised; "
                        "continuing to verify",
                        symbol, account_name, client_order_id, exc_info=True,
                    )

        self._log_cancel_unconfirmed(
            symbol=symbol,
            account_name=account_name,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            observed=observed,
            reads_failed=reads_failed,
            bounded_by_dead_target=False,
        )
        return observed

    def _log_cancel_unconfirmed(
        self,
        *,
        symbol: str,
        account_name: str,
        client_order_id: str,
        broker_order_id: str | None,
        observed: str | None,
        reads_failed: int,
        bounded_by_dead_target: bool,
    ) -> None:
        """Keep every verifier exit loud even when a durable budget suppresses resubmission."""
        self.logger.warning(
            "[OMS-CANCEL-UNCONFIRMED] %s %s coid=%s broker_id=%s last_status=%s reads_failed=%d "
            "dead_target_bound=%d — THE CANCEL WAS NOT CONFIRMED. The order may still be WORKING "
            "and unowned at the broker. This is the FRTT 2026-08-11 shape (136 min live). "
            "Check it by hand.",
            symbol,
            account_name,
            client_order_id,
            broker_order_id,
            observed or "UNREADABLE",
            reads_failed,
            int(bounded_by_dead_target),
        )

    def _record_cancel_verify_reports(
        self,
        session: Session,
        *,
        target_order_id: UUID,
        reports: list[ExecutionReport],
    ) -> None:
        """Make verifier re-submit replies part of the target order's durable budget."""
        order = session.get(BrokerOrder, target_order_id)
        if order is None:
            return
        self._record_direct_cancel_reports(
            session,
            order=order,
            reports=reports,
            existing_metadata={str(k): str(v) for k, v in (order.payload or {}).items()},
            internal="CANCEL_VERIFY_RESUBMIT",
        )

    # ------------------------------------------------ webull attach-after-fill (2026-08-13)
    def _check_bracket_born_triggered(
        self, *, symbol: str, broker_account_name: str, metadata: dict, fill_price: float
    ) -> bool:
        """A bracket whose TARGET is at or below the FILL is born already triggered. Count it.

        ⛔⭐⭐ WHY IT CAN ONLY BE CAUGHT HERE. The bracket is priced off the entry REFERENCE and
        emitted WITH the entry, before any fill exists, so at submit there is nothing to compare --
        the defect is only observable once the fill price is known. This is the one moment it can
        be seen at all.

        MEASURED, 30 days to 2026-08-19: 7 of 457 bracketed fills (1.5%) had target <= fill. ALL 7
        were MARKET entries whose slippage (1.78-3.63%) outran the +2% target; zero on STOP_LIMIT
        (152) or LIMIT (43). SIX of the seven exited within 5-39 SECONDS on the target leg --
        -0.62, -0.34, 0.00, 0.00, 0.00, -0.42 per cent, median 0.00 -- i.e. the entry was converted
        into an immediate scratch regardless of what the trade would have done.

        ⭐ THE PATH THAT PRODUCED THEM IS GONE. #674 and its RTH fan-out half band-cap the entry to
        level*(1+0.5%), and 0.5% < the 2% target, so a capped fill cannot outrun its own target.
        Market-entry bracketed fills: 262 up to 08-12, then ZERO. This guard exists because that is
        a PROPERTY OF THE CURRENT CONFIGURATION, not of the code: a new market path, or the band
        flag off, brings it straight back and nothing would say so.

        ⛔ NO BROKER SCOPE, DELIBERATELY. A target below the fill is an instant-loss exit at either
        venue -- the rule does not differ by broker, and adding a scope where it does not differ is
        its own defect (§164). Pinned by a test on the Schwab primary.
        """
        if str(metadata.get("native_oco_bracket", "")).lower() != "true":
            return False
        try:
            target = float(metadata.get("bracket_target_price"))
        except (TypeError, ValueError):
            return False
        if target <= 0 or fill_price <= 0 or target > fill_price:
            return False
        session_et = utcnow().astimezone(SESSION_TZ).date().isoformat()
        if getattr(self, "_born_triggered_session", None) != session_et:
            self._born_triggered_session = session_et
            self._born_triggered_count = 0
        self._born_triggered_count = getattr(self, "_born_triggered_count", 0) + 1
        self.logger.warning(
            "[OCO-TARGET-BELOW-FILL] %s %s fill=%.4f target=%.4f — the bracket is BORN TRIGGERED: "
            "the target leg sells at or below the entry the moment it works. n=%d this session "
            "(%s). Entry slippage outran the target; check the entry price cap.",
            symbol, broker_account_name, float(fill_price), target,
            self._born_triggered_count, session_et,
        )
        return True

    def _count_bare_webull_fill(
        self, *, symbol: str, broker_account_name: str, quantity: int, entry_price: float
    ) -> int:
        """One COUNTED line per bare Webull fill. Returns the running per-session count (§167).

        ⛔⭐⭐ COUNTED AT THE FILL, NOT FROM THE ATTACH OUTCOME. The position is uncovered from this
        instant until an attach succeeds, and `[WEBULL-PROTECT-ATTACHED]` has NEVER been observed
        (#689 is 0-for-ever). The exposure must be counted where it is created.

        ⛔ IT CANNOT BE READ OFF `[WEBULL-PROTECT-FAILED]`. That marker's own docstring records why:
        two attach sequences can interleave on ONE position (STKH 2026-08-14, on a single fill:
        `1/3 2/3 1/3 3/3 FAILED 2/3 3/3 FAILED` -- one fill, two FAILED lines), so a count taken
        there runs ~0.6 positions per line. One line per FILL is 1:1 by construction.

        ⛔ PER ET SESSION, AND THE SESSION IS ON THE LINE. A since-boot counter reads as a day's
        exposure to anyone who does not know when the process started -- the exact ambiguity the
        seed census had to add a denominator to fix.
        """
        session_et = utcnow().astimezone(SESSION_TZ).date().isoformat()
        if getattr(self, "_bare_webull_fill_session", None) != session_et:
            self._bare_webull_fill_session = session_et
            self._bare_webull_fill_count = 0
        self._bare_webull_fill_count = getattr(self, "_bare_webull_fill_count", 0) + 1
        self.logger.warning(
            "[WEBULL-BARE-FILL] %s %s qty=%d entry=%.4f — FILLED WITH NO BROKER-SIDE BRACKET; "
            "the software ladder is the ONLY cover until an attach succeeds. "
            "n=%d bare fill(s) this session (%s).",
            symbol, broker_account_name, int(quantity), float(entry_price),
            self._bare_webull_fill_count, session_et,
        )
        return self._bare_webull_fill_count

    def _count_premarket_unprotected_webull_fill(
        self, *, symbol: str, broker_account_name: str, quantity: int, entry_price: float
    ) -> int:
        """Emit one counted warning for each Webull fill that cannot be attached before RTH."""
        session_et = utcnow().astimezone(SESSION_TZ).date().isoformat()
        if getattr(self, "_webull_premarket_unprotected_session", None) != session_et:
            self._webull_premarket_unprotected_session = session_et
            self._webull_premarket_unprotected_count = 0
        self._webull_premarket_unprotected_count = (
            getattr(self, "_webull_premarket_unprotected_count", 0) + 1
        )
        self.logger.warning(
            "[WEBULL-PREMARKET-UNPROTECTED] %s %s qty=%d entry=%.4f — no broker-side "
            "protection is available before RTH. The software ladder owns this exit. "
            "unprotected_fills_this_session=%d session_et=%s",
            symbol,
            broker_account_name,
            int(quantity),
            float(entry_price),
            self._webull_premarket_unprotected_count,
            session_et,
        )
        return self._webull_premarket_unprotected_count

    def _spawn_webull_protection(self, **kw) -> "asyncio.Task[None] | None":
        """Run the attach OFF the fill path -- it retries with sleeps and must never stall a fill.

        ⛔⭐ ONE ATTACH PER POSITION AT A TIME (2026-08-17). The reprotect trigger can fire again
        while an earlier attach is still working through its retries, and the two sequences then
        interleave against the same shares. Live 2026-08-14 STKH, on ONE fill:
        ``1/3, 2/3, 1/3, 3/3, FAILED, 2/3, 3/3, FAILED`` -- two sequences, two FAILED lines, one
        position. Two costs, and the second is the one that misleads: the broker sees double the
        traffic, and **any unprotected count read off the FAILED lines is INFLATED** (~0.6
        positions per FAILED line, not 1).

        ⛔ COALESCE, do not queue. A second attach for the same symbol wants the SAME pair on the
        SAME shares, so the in-flight sequence already covers it; running it again afterwards would
        just re-send a pair that either already rests or was already refused.
        """
        key = (str(kw.get("broker_account_name", "")), str(kw.get("symbol", "")).upper())
        if not _is_regular_market_session():
            self.logger.info(
                "[WEBULL-PROTECT-RTH-GATED] %s %s — protective combo attach is disabled outside "
                "RTH; the in-process software ladder remains the only cover",
                key[1],
                key[0],
            )
            return None
        inflight = getattr(self, "_webull_protect_inflight", None)
        if inflight is None:
            inflight = {}
            self._webull_protect_inflight = inflight
        running = inflight.get(key)
        if running is not None and not running.done():
            self.logger.info(
                "[WEBULL-PROTECT-COALESCED] %s %s — an attach is already in flight for this "
                "position; not starting a second interleaved sequence",
                key[1], key[0],
            )
            return running
        task = asyncio.ensure_future(self._attach_webull_protection(**kw))
        tasks = getattr(self, "_webull_protect_tasks", None)
        if tasks is None:
            tasks = set()
            self._webull_protect_tasks = tasks
        tasks.add(task)
        inflight[key] = task
        task.add_done_callback(tasks.discard)
        # ⛔ Only free the slot if it still holds THIS task, never unconditionally -- otherwise a
        # finishing task could evict a newer one and let the interleaving straight back in.
        task.add_done_callback(
            lambda done, k=key: inflight.pop(k, None) if inflight.get(k) is done else None
        )
        return task

    async def _attach_webull_protection(
        self, *, broker_account_name: str, symbol: str, quantity: int,
        entry_price: float, strategy_code: str, entry_client_order_id: str = "",
    ) -> None:
        """Put a real target+stop pair at Webull for a position that filled BARE.

        ⛔ THE FAILURE THAT MATTERS: if this never lands we are HOLDING with nothing protecting us
        at the broker. So it retries, and a final failure is a WARNING carrying everything needed to
        act by hand -- never a silent return. An unprotected position that nobody is told about is
        strictly worse than one that is.

        ⛔⛔ THE TWO PRE-FLIGHT GUARDS BELOW ARE **NOISE** FIXES. NEITHER MAKES THE PAIR PLACE.
        Measured 2026-08-17 over all seven retained `oms.log` files (08-11 -> 08-17): this path has
        **NEVER ONCE SUCCEEDED** -- zero `[WEBULL-PROTECT-ATTACHED]` and zero
        `[WEBULL-EXIT-PAIR-PLACED]`, meaning `place_order` has never returned. 08-14 alone was 10
        episodes, 0 attaches, across BOTH callers (#689 bare-fill fires ~0.2s after the fill,
        #692 reprotect 37s-10min later) and refused identically whether the position was visible
        or not. So the payload itself is refused for a reason neither guard addresses.

        ⇒ **A LOWER REFUSAL COUNT AFTER THIS CHANGE IS NOT A FIX AND MUST NOT BE REPORTED AS ONE.**
        These guards stop us sending orders we can already tell will be refused; they protect
        nothing that was not protected before. The live PASS to require is a
        `[WEBULL-PROTECT-ATTACHED]`, which has never yet been observed.
        """
        if not _is_regular_market_session():
            self.logger.info(
                "[WEBULL-PROTECT-RTH-GATED] %s %s — protective combo attach is disabled outside "
                "RTH; the in-process software ladder remains the only cover",
                symbol,
                broker_account_name,
            )
            return
        target_pct = float(getattr(self.settings, "oms_v2_cw_target_pct", 2.0))
        stop_pct = float(getattr(self.settings, "oms_v2_cw_hard_stop_pct", 5.0))
        target = entry_price * (1.0 + target_pct / 100.0)
        protect = entry_price * (1.0 - stop_pct / 100.0)
        # ⛔⭐⭐ THE RETRY HORIZON MUST OUTLIVE THE SETTLE WINDOW (2026-08-17).
        # The old schedule was 3 attempts at a FIXED 2s: fired at 0s / 2s / 4s, so the whole
        # sequence was over 4.3 seconds after the fill. Measured settle lag on the same account
        # reached 12.7s (live 08-14 CGTL: `[SETTLE-LAG] VISIBLE after 12.7s`, with all three
        # attempts already spent and FAILED logged 8s BEFORE the position appeared) and once
        # logged `NEVER VISIBLE after 300s`. A protective SELL for shares the broker cannot yet
        # see is a naked short to it, so the sequence could not have succeeded at any price.
        # Backoff 2 -> 4 -> 8 -> 15 spans ~29s, which covers the observed window with margin.
        # ⛔ Attempt 1 stays IMMEDIATE on purpose: settle is usually 0.3-0.7s, and delaying every
        # attach to survive the rare slow case would leave the common case unprotected for longer.
        # ⛔ NOT PROVEN, ONLY PLAUSIBLE: in 4 of 6 bare-fill episodes attempts 2-3 fired AFTER
        # `SETTLE-LAG: VISIBLE` and were still refused. Position visibility (`list_account_positions`)
        # and order-side available-to-sell are different surfaces and have NOT been shown to move
        # together. If this lands and refusals persist, the settle window is exonerated.
        # The caller and this method both fail closed outside RTH. CORE is the proven RTH value;
        # pre-market combo attaches were broker-proven impossible against the prior-close reference.
        session_hint = "RTH"
        attempts = max(1, int(getattr(self.settings, "oms_webull_protect_attempts", 5)))
        interval = max(0.0, float(getattr(self.settings, "oms_webull_protect_interval_seconds", 2.0)))
        backoff = max(1.0, float(getattr(self.settings, "oms_webull_protect_backoff_multiplier", 2.0)))
        max_interval = max(
            0.0, float(getattr(self.settings, "oms_webull_protect_max_interval_seconds", 15.0))
        )

        def _retry_delay(attempt_number: int) -> float:
            """Seconds to wait AFTER `attempt_number` before the next one."""
            return min(interval * (backoff ** (attempt_number - 1)), max_interval)

        for attempt in range(1, attempts + 1):
            # ⛔ Do not chase a position we no longer own. 2 of 27 refusals on 08-14 were
            # SYMBOL_CAN_NOT_SELL_SHORT: the shares had already gone, so the attach was sending a
            # protective SELL against nothing -- which at Webull reads as OPENING a short.
            # ⛔⭐ ONLY a positively-CONFIRMED flat may stop us. FLAT_INFERRED is the ordinary shape
            # inside the settle window -- live 08-14 CGTL read FLAT_INFERRED (n=0) for 12.7s after
            # a real fill -- and bailing on it would abandon exactly the bare fills this exists to
            # cover. Same discipline as `_v2_close_reconcile_flat`: HELD and UNKNOWN both continue.
            state = await self._broker_symbol_position_state(broker_account_name, symbol)
            if state is _PositionRead.FLAT_CONFIRMED:
                self.logger.info(
                    "[WEBULL-PROTECT-ABANDONED] %s %s — the broker CONFIRMS we no longer hold this "
                    "position, so there is nothing to protect. Stopping at attempt %d/%d.",
                    symbol, broker_account_name, attempt, attempts,
                )
                return
            # ⛔ Do not send a pair the broker has already told us it will refuse. Our levels are
            # anchored to the ENTRY, so once price runs past either one that leg is unplaceable and
            # the WHOLE combo is 417-rejected -- live 08-14 CGTL 15:14 sent target 5.2173 against
            # Webull's own "should be higher than 5.23".
            unplaceable = await self._webull_protect_unplaceable_reason(
                broker_account_name=broker_account_name, symbol=symbol,
                target=target, protect=protect,
            )
            if unplaceable:
                self.logger.warning(
                    "[WEBULL-PROTECT-UNPLACEABLE] %s %s attempt %d/%d — %s. Not sending a pair the "
                    "broker would refuse; re-checking on the next attempt.",
                    symbol, broker_account_name, attempt, attempts, unplaceable,
                )
                if attempt < attempts and interval:
                    await asyncio.sleep(_retry_delay(attempt))
                continue
            coid = f"{strategy_code}-{symbol}-protect-{uuid4().hex[:12]}"
            request = OrderRequest(
                client_order_id=coid,
                broker_account_name=broker_account_name,
                strategy_code=strategy_code,
                symbol=symbol,
                side="sell",
                intent_type="close",
                quantity=Decimal(str(quantity)),
                reason="webull attach protection after bare resting fill",
                metadata={
                    "webull_exit_only_pair": "true",
                    "bracket_target_price": f"{target:.4f}",
                    "bracket_stop_price": f"{protect:.4f}",
                    "source": "oms_v2_webull_protect",
                    "market_session": session_hint,
                },
                order_type="limit",
                time_in_force="day",
            )
            try:
                reports = await self.broker_adapter.submit_order(request)
            except Exception:
                self.logger.warning(
                    "[WEBULL-PROTECT-RETRY] %s %s session=%s attempt %d/%d raised",
                    symbol, broker_account_name, session_hint, attempt, attempts, exc_info=True,
                )
                reports = []
            if any(getattr(r, "event_type", "") not in ("rejected",) for r in reports):
                # ⛔ REMEMBER AND PERSIST THE BASE ID. These legs hang off this exit-only coid,
                # not the filled entry coid. Without it the pair can be placed but never released,
                # and its eventual child fill can never be attributed after a restart.
                self._webull_protect_base[(broker_account_name, symbol.upper())] = coid
                persisted = await self._persist_webull_protect_base(
                    broker_account_name, symbol, coid,
                    entry_client_order_id=entry_client_order_id,
                )
                self.logger.info(
                    "[WEBULL-PROTECT-ATTACHED] %s %s qty=%d entry=%.4f -> target=%.4f stop=%.4f "
                    "session=%s (attempt %d) handle_persisted=%d — a real pair is now resting at "
                    "the broker; handle_persisted=1 means child/time/price remain addressable "
                    "after restart",
                    symbol, broker_account_name, quantity, entry_price, target, protect,
                    session_hint, attempt, int(persisted),
                )
                return
            reason = "; ".join(str(getattr(r, "reason", "")) for r in reports) or "no report"
            # ⛔⭐ DO NOT TRUNCATE THE BROKER'S OWN WORDS. At 200 chars the reject read
            # `...should be lower than the cu` — cut off exactly where it stopped being useful,
            # which is how the error CODE (`STOP_LOSS_PRICE_LT_MARKETPRICE`, naming the REQUIRED
            # relation) got glossed as its own opposite and "the stop was stale" became the
            # accepted story for a week. The message text is the only thing that disambiguates it.
            # ⛔ The session goes on the line. If tomorrow's pre-market attach fails, the FIRST
            # question is which string we actually sent — that must never have to be inferred.
            self.logger.warning(
                "[WEBULL-PROTECT-RETRY] %s %s session=%s attempt %d/%d refused: %s",
                symbol, broker_account_name, session_hint, attempt, attempts, reason[:1000],
            )
            if attempt < attempts and interval:
                await asyncio.sleep(_retry_delay(attempt))

        self.logger.warning(
            "[WEBULL-PROTECT-FAILED] %s %s qty=%d entry=%.4f session=%s — COULD NOT ATTACH "
            "target=%.4f stop=%.4f after %d attempts. THE POSITION IS HELD WITH NO BROKER-SIDE "
            "STOP; the software ladder is the only cover. Place one by hand.",
            symbol, broker_account_name, quantity, entry_price, session_hint,
            target, protect, attempts,
        )

    async def _webull_protect_unplaceable_reason(
        self, *, broker_account_name: str, symbol: str, target: float, protect: float,
    ) -> str:
        """Empty string if the protective pair can be sent; otherwise why the broker would refuse.

        Webull validates the pair against the LIVE market -- the stop must sit BELOW it, the target
        ABOVE it. Its own words, verbatim from the 08-14 rejects:
            "The stop price of the stop-loss order should be lower than the current market price."
            "The limit price of the take-profit order should be higher than 5.28"

        ⛔⭐ READ THOSE AS THE REQUIRED CONDITION, NOT THE VIOLATION. The error CODES
        (`STOP_LOSS_PRICE_LT_MARKETPRICE`, `STOP_PROFIT_PRICE_GT_OPENPRICE`) name the same required
        relation, so they read as the exact opposite of what went wrong -- which is how they were
        once glossed backwards, and how "the stop was stale" became the accepted story for refusals
        that were mostly nothing of the kind.

        ⛔ BIASED TOWARDS SENDING. We skip only when a level is unplaceable against EVERY proxy we
        have (bid, ask, last), never on a single borderline one. A false skip costs real protection;
        a false send costs one log line. The asymmetry is deliberate and must stay this way round.

        ⛔⭐ NO QUOTE => NO OPINION. Returns "" when nothing can quote, so the pair is sent exactly
        as before -- not knowing the price is not evidence the levels are wrong. This also means
        the guard is INERT wherever no provider can quote, so **its silence is not proof it ran**.
        """
        try:
            quote = await self._fetch_quote_for_order(
                broker_account_name=broker_account_name, symbol=symbol
            )
        except Exception:  # noqa: BLE001 - a quote lookup must never cost us protection
            return ""
        proxies = [
            float(quote[k]) for k in ("bid_price", "ask_price", "last_price")
            if quote.get(k) is not None and float(quote[k] or 0) > 0
        ]
        if not proxies:
            return ""
        if protect >= max(proxies):
            return (
                f"stop {protect:.4f} is not below the market (bid/ask/last max "
                f"{max(proxies):.4f}) — the stop leg would be refused"
            )
        if target <= min(proxies):
            return (
                f"target {target:.4f} is not above the market (bid/ask/last min "
                f"{min(proxies):.4f}) — the take-profit leg would be refused"
            )
        return ""

    # ------------------------------------------- release the exit reservation before a software close
    async def _release_exit_reservation_before_close(
        self, *, session, broker_account_name: str, symbol: str,
    ) -> bool:
        """Cancel the resting exit legs so the ladder's own sell is not read as a naked short.

        ⛔⭐⭐ THE DEFECT. A resting exit leg RESERVES the position at the broker. The v2 software
        ladder then sends its own market sell for the same shares, Webull sees available-to-sell = 0
        and 417s it as a short (`NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K` /
        `ORDER_NOT_SUPPORT_REVERSE_OPTION`). Live 2026-08-13 `live:orb`: 58 rejects, 56 of them one
        XHG share, 48 of those inside five minutes. The `-close-` route filled 4 of 62 at Webull
        against 5 of 6 at Schwab — because Schwab STANDS THE LADDER DOWN while its bracket is armed
        (`_native_oco_stand_down_active`) and Webull exposes no such capability, so it fails OPEN.

        ⛔⭐ CANCEL ONCE PER EPISODE, NOT PER TICK. The ladder re-evaluates on every quote tick. If
        this re-cancelled each time it would simply become a new storm in place of the old one. The
        latch is cleared when the position closes, so the next entry starts fresh.

        ⛔ VERIFICATION IS BACKGROUNDED ON PURPOSE. `_spawn_cancel_verification` documents why:
        blocking an exit to confirm a cancel "would trade a rare unowned order for a common late
        stop, which is the wrong direction". So we submit the cancels, let the close proceed on this
        tick (it may still reject once while the cancel lands — one reject, not forty-eight), and
        chase confirmation off-path.

        Returns True if cancels were actually submitted.
        """
        key = (broker_account_name, symbol.upper())
        if key in self._exit_reservation_released:
            return False
        base = self._webull_protect_base.get(key, "")
        if not base:
            # Native bracket children hang off the entry coid; bare Webull protection hangs off a
            # separately persisted attach coid. Never guess one from the other.
            try:
                entry = self._find_oco_entry_order(session, broker_account_name, symbol)
                base = self._oco_exit_base_for_entry(
                    entry, broker_account_name=broker_account_name, symbol=symbol
                )
            except Exception:  # noqa: BLE001 - never break an exit for bookkeeping
                base = ""
        if not base:
            return False
        # Trigger + polarity live beside the numbers: requested=2 is the denominator; only
        # confirmed=2 means the reservation is clear. A request is not a result.
        requested = 2
        self.logger.info(
            "[OMS-CANCEL-PAIR-REQUEST] %s %s base=%s requested=%d confirmed=0 "
            "release_confirmed=0 — trigger=software_close; polarity: 1 only when every addressed "
            "exit leg is confirmed cancelled or already absent",
            symbol, broker_account_name, base, requested,
        )
        try:
            reports = await self.broker_adapter.cancel_exit_pair(
                broker_account_name=broker_account_name, symbol=symbol,
                base_client_order_id=base,
            )
        except Exception:  # noqa: BLE001 - a failed release must never stop the close attempt
            self.logger.warning(
                "[OMS-EXIT-RELEASE-RAISED] %s %s base=%s — could not cancel the resting exit legs; "
                "closing anyway (the sell may still be refused as a short)",
                symbol, broker_account_name, base, exc_info=True,
            )
            return False
        if not reports:
            # No capability (Schwab/simulated) or no addressable legs -> behave exactly as before.
            self.logger.warning(
                "[OMS-CANCEL-PAIR-UNCERTAIN] %s %s base=%s requested=%d reports=0 confirmed=0 "
                "release_confirmed=0 — no success marker; the close still proceeds",
                symbol, broker_account_name, base, requested,
            )
            return False
        confirmed = sum(
            1 for report in reports if getattr(report, "event_type", "") == "cancelled"
        )
        refused = sum(
            1 for report in reports if getattr(report, "event_type", "") == "rejected"
        )
        if len(reports) != requested or confirmed != requested:
            reasons = "; ".join(
                str(getattr(report, "reason", "") or "<no reason>") for report in reports
            )
            self.logger.warning(
                "[OMS-CANCEL-PAIR-UNCERTAIN] %s %s base=%s requested=%d reports=%d confirmed=%d "
                "refused=%d release_confirmed=0 — no success marker; reasons=%s",
                symbol, broker_account_name, base, requested, len(reports), confirmed, refused,
                reasons[:1000],
            )
            return False
        self._exit_reservation_released.add(key)
        self.logger.info(
            "[OMS-EXIT-RELEASE] %s %s base=%s requested=%d confirmed=%d release_confirmed=1 — "
            "success: every addressed exit leg is cancelled or already absent, so the software "
            "close is not refused as a naked short",
            symbol, broker_account_name, base, requested, confirmed,
        )
        return True

    def _reprotect_after_failed_release(self, acct: str, symbol: str, row) -> None:
        """Put a protective pair back on a position whose exit legs we cancelled for a close that
        then would not go through. Never raises — this runs inside the protective sync.

        Prices off the managed row's ENTRY, which is the same anchor `_attach_webull_protection`
        uses, so the restored pair sits exactly where the original one did.
        """
        try:
            entry_price = float(getattr(row, "entry_price", 0) or 0)
            quantity = int(getattr(row, "current_quantity", 0) or 0)
            if entry_price <= 0 or quantity <= 0:
                self.logger.warning(
                    "[OMS-EXIT-REPROTECT-SKIPPED] %s %s entry=%s qty=%s — cannot price a protective "
                    "pair from this row. THE POSITION MAY BE UNCOVERED; check it by hand.",
                    symbol, acct, entry_price, quantity,
                )
                return
            self._spawn_webull_protection(
                broker_account_name=acct, symbol=symbol, quantity=quantity,
                entry_price=entry_price,
                strategy_code=str(getattr(row, "strategy_code", "") or ""),
            )
        except Exception:  # noqa: BLE001 - must never break the protective sync
            self.logger.warning(
                "[OMS-EXIT-REPROTECT-FAILED] %s %s — could not re-attach protection after a failed "
                "release. THE POSITION MAY BE UNCOVERED; check it by hand.",
                symbol, acct, exc_info=True,
            )

    def _clear_exit_reservation_release(self, broker_account_name: str, symbol: str) -> None:
        """Forget the release latch so the NEXT position on this symbol releases its own legs.

        ⛔ Must be called wherever a managed row closes. A latch that outlives its position means
        the next entry's reservation is never released and the storm comes straight back — silently,
        because the code would look like it is still handling the case."""
        key = (broker_account_name, symbol.upper())
        self._exit_reservation_released.discard(key)
        self._webull_protect_base.pop(key, None)

    def _record_internal_risk_pass(
        self,
        session: Session,
        *,
        intent: TradeIntent,
        strategy: Strategy,
        broker_account: BrokerAccount,
        metadata: dict[str, str],
        reason: str,
    ) -> None:
        self.store.record_risk_check(
            session,
            intent=intent,
            strategy_id=strategy.id,
            broker_account_id=broker_account.id,
            outcome="pass",
            reason=reason,
            payload={"metadata": dict(metadata)},
        )

    async def _cancel_native_stop_guard_before_sell(
        self,
        *,
        session: Session,
        strategy: Strategy,
        broker_account: BrokerAccount,
        symbol: str,
    ) -> list[OrderEventEvent]:
        native_order = self.store.find_open_native_stop_guard_order(
            session,
            strategy_id=strategy.id,
            broker_account_id=broker_account.id,
            symbol=symbol,
        )
        if native_order is None:
            return []

        cancel_event = TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=strategy.code,
                broker_account_name=broker_account.name,
                symbol=symbol,
                side="sell",
                quantity=native_order.quantity,
                intent_type="cancel",
                reason="NATIVE_STOP_GUARD_CANCEL",
                metadata={
                    "native_stop_guard_manage": "true",
                    "target_client_order_id": native_order.client_order_id,
                    "broker_order_id": native_order.broker_order_id or "",
                },
            ),
        )
        cancel_intent = self.store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=broker_account,
            event=cancel_event,
        )
        self._record_internal_risk_pass(
            session,
            intent=cancel_intent,
            strategy=strategy,
            broker_account=broker_account,
            metadata=dict(cancel_event.payload.metadata),
            reason="native_stop_guard_internal_cancel",
        )
        return await self._process_cancel_intent(
            session=session,
            strategy_id=strategy.id,
            broker_account_id=broker_account.id,
            intent=cancel_intent,
            event=cancel_event,
        )

    async def _cancel_open_exit_orders_before_hard_stop(
        self,
        *,
        session: Session,
        strategy: Strategy,
        broker_account: BrokerAccount,
        symbol: str,
    ) -> list[OrderEventEvent]:
        published_events: list[OrderEventEvent] = []
        seen_client_order_ids: set[str] = set()

        while True:
            open_exit = self.store.find_open_exit_order(
                session,
                strategy_id=strategy.id,
                broker_account_id=broker_account.id,
                symbol=symbol,
                include_native_stop_guard=False,
            )
            if open_exit is None:
                break

            if open_exit.client_order_id in seen_client_order_ids:
                break
            seen_client_order_ids.add(open_exit.client_order_id)

            cancel_event = TradeIntentEvent(
                source_service=SERVICE_NAME,
                payload=TradeIntentPayload(
                    strategy_code=strategy.code,
                    broker_account_name=broker_account.name,
                    symbol=symbol,
                    side="sell",
                    quantity=open_exit.quantity,
                    intent_type="cancel",
                    reason="HARD_STOP_PREEMPT_PENDING_EXIT",
                    metadata={
                        "hard_stop_preempt": "true",
                        "target_client_order_id": open_exit.client_order_id,
                        "broker_order_id": open_exit.broker_order_id or "",
                    },
                ),
            )
            cancel_intent = self.store.create_trade_intent(
                session,
                strategy=strategy,
                broker_account=broker_account,
                event=cancel_event,
            )
            self._record_internal_risk_pass(
                session,
                intent=cancel_intent,
                strategy=strategy,
                broker_account=broker_account,
                metadata=dict(cancel_event.payload.metadata),
                reason="hard_stop_preempt_pending_exit",
            )
            published_events.extend(
                await self._process_cancel_intent(
                    session=session,
                    strategy_id=strategy.id,
                    broker_account_id=broker_account.id,
                    intent=cancel_intent,
                    event=cancel_event,
                )
            )

        return published_events

    async def _arm_or_rearm_native_stop_guard(
        self,
        *,
        session: Session,
        strategy: Strategy,
        broker_account: BrokerAccount,
        stop: ArmedHardStop,
    ) -> list[OrderEventEvent]:
        if not _is_regular_market_session():
            return []
        if stop.quantity <= 0 or stop.stop_price <= 0:
            return []

        published_events: list[OrderEventEvent] = []
        existing = self.store.find_open_native_stop_guard_order(
            session,
            strategy_id=strategy.id,
            broker_account_id=broker_account.id,
            symbol=stop.symbol,
        )
        if existing is not None:
            published_events.extend(
                await self._cancel_native_stop_guard_before_sell(
                    session=session,
                    strategy=strategy,
                    broker_account=broker_account,
                    symbol=stop.symbol,
                )
            )

        stop_event = TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=strategy.code,
                broker_account_name=broker_account.name,
                symbol=stop.symbol,
                side="sell",
                quantity=stop.quantity,
                intent_type="close",
                reason=self.NATIVE_STOP_GUARD_REASON,
                metadata={
                    "native_stop_guard": "true",
                    "order_type": "STOP",
                    "time_in_force": "day",
                    "stop_price": _format_limit_price(stop.stop_price) or str(stop.stop_price),
                    "stop_loss_pct": str(stop.stop_loss_pct),
                },
            ),
        )
        stop_intent = self.store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=broker_account,
            event=stop_event,
        )
        self._record_internal_risk_pass(
            session,
            intent=stop_intent,
            strategy=strategy,
            broker_account=broker_account,
            metadata=dict(stop_event.payload.metadata),
            reason="native_stop_guard_internal_arm",
        )
        request = OrderRequest(
            client_order_id=self._build_client_order_id(stop_event),
            broker_account_name=broker_account.name,
            strategy_code=strategy.code,
            symbol=stop.symbol,
            side="sell",
            intent_type="close",
            quantity=stop.quantity,
            reason=self.NATIVE_STOP_GUARD_REASON,
            metadata=dict(stop_event.payload.metadata),
            order_type="STOP",
            time_in_force="day",
        )
        reports = await self.broker_adapter.submit_order(request)
        rearm_key = self._hard_stop_key(strategy.code, broker_account.name, stop.symbol)
        if self._is_reverse_conflict_reject(reports):
            # Bug A follow-up: a reverse reject means the just-cancelled prior guard / the entry
            # buy has not settled at the broker yet — a duplicate protective SELL would exceed
            # the held long. The IN-MEMORY hard stop (tick-evaluated) protects throughout, so
            # rather than block the fill transaction retrying here (which would stall intent
            # processing at the open), QUEUE the (re)arm and retry it on the periodic sync
            # cadence, by which time the broker has settled. (2026-07-13 ORB AGEN/VEEE.)
            self._native_guard_rearm_pending[rearm_key] = (strategy.id, broker_account.id)
            self.logger.info(
                "[NATIVE-STOP-GUARD DEFER] %s %s (re)arm reverse-rejected — queued for periodic "
                "re-arm; in-memory stop protects meanwhile",
                strategy.code,
                stop.symbol,
            )
        else:
            # armed (or any non-reverse outcome) -> nothing pending for this stop.
            self._native_guard_rearm_pending.pop(rearm_key, None)
        published_events.extend(
            await self._record_order_reports(
                session=session,
                intent=stop_intent,
                strategy_id=strategy.id,
                broker_account_id=broker_account.id,
                intent_event=stop_event,
                request=request,
                reports=reports,
            )
        )
        return published_events

    async def _manage_native_stop_after_fill(
        self,
        *,
        session: Session,
        strategy_id: UUID,
        broker_account_id: UUID,
        strategy_code: str,
        broker_account_name: str,
        symbol: str,
        side: str,
        intent_type: str,
        metadata: dict[str, str],
    ) -> list[OrderEventEvent]:
        if not _is_regular_market_session():
            return []
        if self._is_native_stop_guard_metadata(metadata):
            return []
        if str(metadata.get("stop_guard", "")).strip().lower() == "true":
            return []

        strategy = session.get(Strategy, strategy_id)
        broker_account = session.get(BrokerAccount, broker_account_id)
        if strategy is None or broker_account is None:
            return []

        if str(side).lower() == "buy" and str(intent_type).lower() == "open":
            if str(metadata.get("stop_guard_enabled", "")).lower() != "true":
                return []
        elif str(side).lower() == "sell" and str(intent_type).lower() in {"close", "scale"}:
            pass
        else:
            return []

        stop = self._armed_hard_stops.get(
            self._hard_stop_key(strategy_code, broker_account_name, symbol),
        )
        if stop is None or stop.quantity <= 0:
            return []
        return await self._arm_or_rearm_native_stop_guard(
            session=session,
            strategy=strategy,
            broker_account=broker_account,
            stop=stop,
        )

    async def _rearm_native_stop_from_registry(
        self,
        *,
        session: Session,
        strategy_id: UUID,
        broker_account_id: UUID,
        strategy_code: str,
        broker_account_name: str,
        symbol: str,
    ) -> list[OrderEventEvent]:
        if not _is_regular_market_session():
            return []
        strategy = session.get(Strategy, strategy_id)
        broker_account = session.get(BrokerAccount, broker_account_id)
        if strategy is None or broker_account is None:
            return []
        stop = self._armed_hard_stops.get(
            self._hard_stop_key(strategy_code, broker_account_name, symbol),
        )
        if stop is None or stop.quantity <= 0:
            return []
        return await self._arm_or_rearm_native_stop_guard(
            session=session,
            strategy=strategy,
            broker_account=broker_account,
            stop=stop,
        )

    async def _retry_pending_native_guard_rearms(self) -> None:
        """Bug A follow-up: re-arm native stop guards whose immediate arm reverse-rejected
        (unsettled cancel/fill). Runs on the broker-sync cadence — by then the broker has
        settled — so it never blocks the fill transaction / intent processing at the open.

        Idempotent + self-draining: skips when a live guard already exists, drops the pending
        entry once the stop is gone, and `_arm_or_rearm_native_stop_guard` itself clears the
        pending entry on a successful arm (or re-queues it if it reverse-rejects again)."""
        if not _is_regular_market_session():
            return
        for key, (strategy_id, broker_account_id) in list(self._native_guard_rearm_pending.items()):
            stop = self._armed_hard_stops.get(key)
            if stop is None or stop.quantity <= 0:
                self._native_guard_rearm_pending.pop(key, None)  # stop closed -> nothing to arm
                continue
            try:
                with self.session_factory() as session:
                    strategy = session.get(Strategy, strategy_id)
                    broker_account = session.get(BrokerAccount, broker_account_id)
                    if strategy is None or broker_account is None:
                        self._native_guard_rearm_pending.pop(key, None)
                        continue
                    # A live guard already exists (armed elsewhere) -> done, no churn.
                    if self.store.find_open_native_stop_guard_order(
                        session,
                        strategy_id=strategy.id,
                        broker_account_id=broker_account.id,
                        symbol=stop.symbol,
                    ) is not None:
                        self._native_guard_rearm_pending.pop(key, None)
                        session.commit()
                        continue
                    await self._arm_or_rearm_native_stop_guard(
                        session=session,
                        strategy=strategy,
                        broker_account=broker_account,
                        stop=stop,
                    )
                    session.commit()
            except Exception:
                self.logger.exception("[NATIVE-STOP-GUARD] periodic re-arm failed for %s", key)

    def _apply_managed_position_after_fill(
        self,
        *,
        session: Session,
        strategy_code: str,
        broker_account_name: str,
        symbol: str,
        side: str,
        intent_type: str,
        quantity: Decimal,
        price: Decimal,
        metadata: dict[str, str],
        entry_client_order_id: str = "",
    ) -> None:
        """Track-2 Phase-2 Slice-1: maintain the OMS-owned `oms_managed_positions`
        ladder state from v2's own fills. SOLE WRITER — only this OMS path writes
        the table. Slice 1 does NOT emit exits; it only records/closes state.
        Gated OFF by default (`oms_v2_exit_management_enabled`) → fully dormant.
        """
        if not bool(getattr(self.settings, "oms_v2_exit_management_enabled", False)):
            return
        if strategy_code != "schwab_1m_v2":
            return
        s = str(side).lower()
        it = str(intent_type).lower()
        if s == "buy" and it == "open":
            existing = self.store.get_open_managed_position(
                session, broker_account_name=broker_account_name, symbol=symbol
            )
            if existing is not None:
                return  # idempotent: already managing this symbol
            entry_path = str(metadata.get("path", "")).strip()
            self.store.create_managed_position(
                session,
                strategy_code=strategy_code,
                broker_account_name=broker_account_name,
                symbol=symbol,
                entry_price=price,
                quantity=int(quantity),
                entry_path=entry_path,
                config_name="make_v2_variant",
            )
            self._managed_v2_symbols.add((broker_account_name, symbol))  # slice-3: arm quote-path eval
            # P0.2 SCHWAB settlement anchor. Instrumentation must NEVER be load-bearing on the
            # live fill path, so this is guarded: a duck-typed caller (tests call this hook with a
            # deliberate minimal stand-in) or a probe-less build must not be able to break a real
            # fill. Same principle as the try/except around _observe_settlement.
            _watch = getattr(self, "_settle_watch_add", None)
            if _watch is not None:
                _watch(broker_account_name, symbol)
            logger.info(
                "[OMS-V2-MANAGED-OPEN] sym=%s acct=%s qty=%s entry=%s path=%s",
                symbol, broker_account_name, int(quantity), price, entry_path,
            )
            self._check_bracket_born_triggered(
                symbol=symbol, broker_account_name=broker_account_name,
                metadata=metadata, fill_price=float(price),
            )
            # ⭐⭐ ATTACH PROTECTION TO A BARE WEBULL FILL (2026-08-13).
            # A Webull RESTING entry cannot carry a bracket -- the broker refuses a stop-limit
            # master with legs (Probe W shape B, 417). So it fills BARE, and without this the
            # position runs on software-only stops for its whole life. Probe W4 proved the cure:
            # [STOP_PROFIT, STOP_LOSS] with NO master is ACCEPTED (HTTP 200).
            # ⛔ Only for a leg that arrived WITHOUT a bracket -- a bracketed entry already has its
            # protection live at the fill, and a second pair would reserve the shares twice.
            if (
                str(metadata.get("fanout_leg", "")).lower() == "webull"
                and str(metadata.get("native_oco_bracket", "")).lower() != "true"
            ):
                # ⛔⭐⭐ COUNT THE BARE FILL AT THE FILL, NOT FROM THE ATTACH OUTCOME (§167).
                #
                # This position is uncovered from THIS INSTANT until an attach succeeds, and
                # `[WEBULL-PROTECT-ATTACHED]` has NEVER been observed (#689 is 0-for-ever). So the
                # exposure has to be counted where it is created.
                #
                # ⛔ IT CANNOT BE READ OFF `[WEBULL-PROTECT-FAILED]`. That marker's own docstring
                # records why: two attach sequences can interleave on ONE position (STKH 08-14,
                # `1/3 2/3 1/3 3/3 FAILED 2/3 3/3 FAILED` -- one fill, two FAILED lines), so a count
                # taken there runs ~0.6 positions per line. One line per FILL is 1:1 by construction.
                #
                # ⛔ PER ET SESSION, and the session is ON THE LINE. A since-boot counter reads as a
                # day's exposure to anyone who does not know when the process started -- the exact
                # ambiguity the seed census had to add a denominator to fix.
                if _is_regular_market_session():
                    self._count_bare_webull_fill(
                        symbol=symbol, broker_account_name=broker_account_name,
                        quantity=int(quantity), entry_price=float(price),
                    )
                    self._spawn_webull_protection(
                        broker_account_name=broker_account_name, symbol=symbol,
                        quantity=int(quantity), entry_price=float(price),
                        strategy_code=strategy_code,
                        entry_client_order_id=entry_client_order_id,
                    )
                else:
                    self._count_premarket_unprotected_webull_fill(
                        symbol=symbol, broker_account_name=broker_account_name,
                        quantity=int(quantity), entry_price=float(price),
                    )
        elif s == "sell":
            # #6: when close-on-fill is ON, the managed-exit row is closed HERE, on the
            # CONFIRMED fill (current_quantity decrement + close-at-0) — NOT on submit in
            # the quote eval. So managed-exit sell fills fall through to the shared
            # decrement/close below (the same path external flattens already use).
            # Legacy (flag OFF): slice-3 closed the row on submit → skip to avoid
            # double-handling (rollback lever).
            if str(metadata.get("oms_v2_managed_exit", "")).strip().lower() == "true":
                if not bool(getattr(self.settings, "oms_v2_exit_close_on_fill_enabled", True)):
                    return
            # External flatten (operator-initiated): keep the row honest.
            row = self.store.get_open_managed_position(
                session, broker_account_name=broker_account_name, symbol=symbol
            )
            if row is None:
                return
            row.current_quantity = max(0, int(row.current_quantity) - int(quantity))
            if row.current_quantity <= 0:
                self.store.close_managed_position(session, row)
                self._managed_v2_symbols.discard((broker_account_name, symbol))  # slice-3: disarm eval
                # #885 finding 1: a confirmed SELL fill ENDS the episode. Before this it cleared
                # neither the reject total nor the stand-down, so a symbol that jammed once stayed
                # stood down into its NEXT position — exits suppressed on a position that never
                # rejected anything.
                self._v2_exit_end_episode((broker_account_name, symbol))
                self._clear_exit_reservation_release(broker_account_name, symbol)
                logger.info("[OMS-V2-MANAGED-CLOSE] sym=%s acct=%s flat", symbol, broker_account_name)
            else:
                session.flush()

    # ---- Track-2 Phase-2 Slice-3: OMS-managed v2 exit ladder (quote-driven) ----

    def _v2_accounts(self) -> list[str]:
        """v2 broker accounts the CW exit ladder manages. Single (primary Schwab) unless the
        Webull leg is active on the SECOND account — either the mirror-on-fill flag OR the
        dual-broker FAN-OUT flag adds the Webull account so its per-account exit ladder runs.
        One account == today (byte-identical when both flags off)."""
        accounts = [self.settings.strategy_schwab_1m_v2_account_name]
        if bool(
            getattr(self.settings, "strategy_schwab_1m_v2_webull_mirror_enabled", False)
        ) or bool(
            getattr(self.settings, "strategy_schwab_1m_v2_dual_broker_fanout_enabled", False)
        ):
            web = self.settings.strategy_schwab_1m_v2_webull_account_name
            if web and web not in accounts:
                accounts.append(web)
        return accounts

    def _rehydrate_managed_v2_symbols(self) -> None:
        """At startup, repopulate the hot-path guard from open managed rows so a
        restart keeps protecting positions opened before it. Inert when OFF."""
        if not bool(getattr(self.settings, "oms_v2_exit_management_enabled", False)):
            return
        try:
            symbols: set[tuple[str, str]] = set()
            with self.session_factory() as session:
                for acct in self._v2_accounts():
                    for sym in self.store.list_open_managed_symbols(session, broker_account_name=acct):
                        symbols.add((acct, sym))
            self._managed_v2_symbols = symbols
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("v2 managed-symbol rehydrate failed: %s", exc)
            return
        if self._managed_v2_symbols:
            self.logger.info(
                "[OMS-V2-MANAGED-REHYDRATE] armed %d symbol(s): %s",
                len(self._managed_v2_symbols),
                ",".join(sorted(f"{a}:{s}" for a, s in self._managed_v2_symbols)),
            )

    # ------------------------------------------------------------------ #
    # F2: durable armed-stop mirror — persist / rehydrate / boot reconcile
    # ------------------------------------------------------------------ #
    @staticmethod
    def _armed_stop_row_kwargs(stop: ArmedHardStop) -> dict:
        """Persistable fields of an ArmedHardStop (transient throttle state excluded)."""
        return {
            "quantity": stop.quantity,
            "entry_price": stop.entry_price,
            "stop_loss_pct": float(stop.stop_loss_pct),
            "stop_price": stop.stop_price,
            "quote_max_age_ms": int(stop.quote_max_age_ms),
            "initial_panic_buffer_pct": float(stop.initial_panic_buffer_pct),
            "trail_pct": float(stop.trail_pct),
            "high_water_mark": stop.high_water_mark,
            "close_in_flight": bool(stop.close_in_flight),
        }

    def _persist_armed_stop_snapshot(self, session: Session, snapshot: list) -> None:
        """Off-loop WRITE unit: upsert present keys, delete absent ones — mirroring the
        in-memory registry state captured on-loop before the thread hop."""
        for (strategy_code, broker_account_name, symbol), kwargs in snapshot:
            if kwargs is None:
                self.store.delete_armed_stop(
                    session, strategy_code=strategy_code,
                    broker_account_name=broker_account_name, symbol=symbol,
                )
            else:
                self.store.upsert_armed_stop(
                    session, strategy_code=strategy_code,
                    broker_account_name=broker_account_name, symbol=symbol, **kwargs,
                )

    async def _flush_dirty_armed_stops(self) -> None:
        """Persist dirtied armed-stop keys to the durable mirror OFF the loop (best-effort).
        The in-memory stop is authoritative for live triggering; the mirror exists only for
        restart-recovery, so a failed/slow flush never affects protection (and the boot
        reconcile is the safety net). Snapshots on-loop so no dict is read from the thread."""
        if not self._armed_stop_persistence_enabled or not self._armed_stop_dirty:
            return
        keys = list(self._armed_stop_dirty)
        self._armed_stop_dirty.clear()
        snapshot: list = []
        for key in keys:
            stop = self._armed_hard_stops.get(key)
            snapshot.append((key, self._armed_stop_row_kwargs(stop) if stop is not None else None))
        try:
            await self._run_db(
                lambda session: self._persist_armed_stop_snapshot(session, snapshot), commit=True
            )
        except Exception as exc:  # noqa: BLE001 — mirror is best-effort; reconcile is the net
            self.logger.warning("armed-stop mirror flush failed (best-effort): %s", exc)
            self._armed_stop_dirty.update(keys)  # retry on the next flush

    @staticmethod
    def _armed_stop_row_to_dict(row) -> dict:
        """Convert a durable OmsArmedStop ORM row to primitives INSIDE the worker thread
        (so no ORM object escapes the `_run_db` unit)."""
        return {
            "strategy_code": str(row.strategy_code),
            "broker_account_name": str(row.broker_account_name),
            "symbol": str(row.symbol).upper(),
            "quantity": Decimal(str(row.quantity)),
            "entry_price": Decimal(str(row.entry_price)),
            "stop_loss_pct": float(row.stop_loss_pct),
            "stop_price": Decimal(str(row.stop_price)),
            "quote_max_age_ms": int(row.quote_max_age_ms),
            "initial_panic_buffer_pct": float(row.initial_panic_buffer_pct),
            "trail_pct": float(row.trail_pct),
            "high_water_mark": (Decimal(str(row.high_water_mark)) if row.high_water_mark is not None else None),
            "close_in_flight": bool(row.close_in_flight),
        }

    async def _rehydrate_armed_hard_stops(self) -> None:
        """Boot: rebuild the in-memory `_armed_hard_stops` registry from the durable mirror
        so an ORB position stays PROTECTED across a restart (the pre-F2 naked gap). Off-loop
        read; the dict assignment (registry mutation) stays on-loop."""
        if not self._armed_stop_persistence_enabled:
            return
        try:
            rows = await self._run_db(
                lambda session: [
                    self._armed_stop_row_to_dict(r) for r in self.store.list_armed_stops(session)
                ],
                commit=False,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("armed-stop rehydrate read failed: %s", exc)
            return
        for d in rows:
            key = self._hard_stop_key(d["strategy_code"], d["broker_account_name"], d["symbol"])
            self._armed_hard_stops[key] = ArmedHardStop(
                strategy_code=d["strategy_code"], broker_account_name=d["broker_account_name"],
                symbol=d["symbol"], quantity=d["quantity"], entry_price=d["entry_price"],
                stop_loss_pct=d["stop_loss_pct"], stop_price=d["stop_price"],
                quote_max_age_ms=d["quote_max_age_ms"], initial_panic_buffer_pct=d["initial_panic_buffer_pct"],
                close_in_flight=d["close_in_flight"], last_trigger_attempt_at=None,
                trail_pct=d["trail_pct"], high_water_mark=d["high_water_mark"],
            )
        if rows:
            self.logger.info(
                "[OMS-ARMED-STOP-REHYDRATE] restored %d armed stop(s): %s",
                len(rows), ",".join(sorted(str(d["symbol"]) for d in rows)),
            )

    def _read_owned_positions_with_broker_qty(self, session: Session) -> list:
        """Off-loop READ unit: OMS-owned open positions (per-strategy virtual ledger) with
        their current broker-truth quantity. OMS-owned by construction — a manual holding
        has no virtual_positions row, so it is never returned (scoping invariant)."""
        out: list = []
        for sc, ban, sym, qty in self.store.list_owned_open_positions(session):
            if qty <= 0:
                continue
            broker_qty = self.store.get_account_position_qty_by_name(
                session, broker_account_name=ban, symbol=sym
            )
            out.append((sc, ban, str(sym).upper(), Decimal(str(qty)), Decimal(str(broker_qty))))
        return out

    async def _reconcile_protection_before_serving(self) -> None:
        """PROTECTED-BEFORE-SERVING: before the tick consumer starts, confirm every
        OMS-OWNED open position is protected (a rehydrated stop / managed row) AND backed at
        the broker. OMS-owned ONLY (manual holdings are invisible — no virtual_positions row,
        never touched: the scoping invariant). Loud-logs the INVERSE mismatch only: an OMS
        record present but the position missing/short at the broker, or an owned position
        with no rehydrated protection. Never arms/sells/flags a holding it did not place."""
        if not self._armed_stop_persistence_enabled:
            return
        try:
            await self.sync_broker_positions()  # refresh account_positions (off-loop, #391)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("boot reconcile: broker position sync failed: %s", exc)
        try:
            owned = await self._run_db(self._read_owned_positions_with_broker_qty, commit=False)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("boot reconcile: owned-position read failed: %s", exc)
            return
        alerts = 0
        for sc, ban, sym, owned_qty, broker_qty in owned:
            key = self._hard_stop_key(sc, ban, sym)
            protected = key in self._armed_hard_stops or any(
                (a, sym) in self._managed_v2_symbols for a in self._v2_accounts()
            )
            if not protected:
                alerts += 1
                self.logger.error(
                    "[OMS-BOOT-PROTECTION-ALERT] NAKED OMS-owned position %s %s qty=%s has NO "
                    "rehydrated stop after restart — investigate",
                    ban, sym, owned_qty,
                )
            if broker_qty < owned_qty:
                alerts += 1
                self.logger.error(
                    "[OMS-BOOT-PROTECTION-ALERT] VANISHED OMS-owned position %s %s expected "
                    "qty=%s but broker shows %s — investigate",
                    ban, sym, owned_qty, broker_qty,
                )
        self._boot_protection_alerts = alerts
        if alerts == 0:
            self.logger.info(
                "[OMS-BOOT-PROTECTION] all %d OMS-owned position(s) protected + broker-backed",
                len(owned),
            )

    def _hydrate_v2_position(self, row) -> Position:
        """Rebuild an exit_logic.Position from a managed row, restoring the
        accumulated ladder state (peak/tier/floor/scales) so the ratchet CONTINUES
        — never resets — across quotes. Floor params from make_v2_variant."""
        cfg = self._v2_exit_config
        p = Position(
            ticker=row.symbol,
            entry_price=float(row.entry_price),
            quantity=int(row.current_quantity),
            entry_time=str(row.entry_time),
            path=row.entry_path or "",
            scale_profile="NORMAL",
            floor_lock_at_1pct_peak_pct=cfg.profit_floor_lock_at_1pct_peak_pct,
            floor_lock_at_2pct_peak_pct=cfg.profit_floor_lock_at_2pct_peak_pct,
            floor_lock_at_3pct_peak_pct=cfg.profit_floor_lock_at_3pct_peak_pct,
            floor_trail_buffer_over_4pct_pct=cfg.profit_floor_trail_buffer_over_4pct_pct,
        )
        p.peak_profit_pct = float(row.peak_profit_pct or 0.0)
        p.tier = int(row.tier or 1)
        p.floor_pct = float(row.floor_pct) if row.floor_pct is not None else -999.0
        p.floor_price = float(row.floor_price) if row.floor_price is not None else 0.0
        p.scales_done = list(row.scales_done or [])
        p.scale_pnl = float(row.scale_pnl or 0.0)
        return p

    def _v2_scale_level_price(self, entry_price: float, level: str) -> float:
        cfg = self._v2_exit_config
        pct = {
            "PCT2": cfg.scale_normal2_pct,
            "FAST4": cfg.scale_fast4_pct,
            "PCT4_AFTER2": cfg.scale_4after2_pct,
        }.get(str(level))
        return entry_price if pct is None else entry_price * (1.0 + float(pct) / 100.0)

    def _fillable_session_start_hour_et(self) -> int:
        return int(
            getattr(
                self.settings,
                "oms_fillable_session_start_hour_et",
                OMS_FILLABLE_SESSION_START_HOUR_ET,
            )
        )

    def _fillable_session_end_hour_et(self) -> int:
        return int(
            getattr(
                self.settings,
                "oms_fillable_session_end_hour_et",
                OMS_FILLABLE_SESSION_END_HOUR_ET,
            )
        )

    def _market_is_fillable(self, now: datetime | None = None) -> bool:
        """True while the market is in a session where an order can actually fill
        (default 7 AM–8 PM ET, weekday, non-holiday). Outside it, exit orders must
        not be placed or refreshed — they cannot fill, so doing so is pure churn."""
        return is_fillable_et_session(
            now or utcnow(),
            self._fillable_session_start_hour_et(),
            self._fillable_session_end_hour_et(),
        )

    async def _confirmation_bound_managed_row_id(self, acct: str, symbol: str) -> str:
        """The id of the managed row open RIGHT NOW for ``(acct, symbol)``, or "" if none.

        ⛔ Fails CLOSED: any read fault returns "", which refuses the confirmation. Not exiting
        leaves the position under the normal ladder, which still has its stop; selling the WRONG
        position does not have an equivalent recovery.
        """
        try:
            def _read(session: Session) -> str:
                row = self.store.get_open_managed_position(
                    session, broker_account_name=acct, symbol=symbol
                )
                return str(row.id) if row is not None else ""
            return str(await self._run_db(_read, commit=False) or "")
        except Exception:  # noqa: BLE001 — a DB fault must never arm an unbound confirmation
            self.logger.exception(
                "[OMS-V2-CONFIRMATION-EXIT-REFUSED] sym=%s acct=%s reason=bind_read_failed",
                symbol, acct,
            )
            return ""

    async def _reconcile_confirmation_exit_protection(self, acct: str, symbol: str) -> str:
        """Return released/resolved_by_fill/unanswerable from fresh broker evidence."""
        adapter = getattr(self, "broker_adapter", None)
        release = getattr(adapter, "release_native_oco_for_close", None)
        if release is None:
            self.logger.error(
                "[OMS-V2-CONFIRMATION-EXIT-PROTECTION] sym=%s acct=%s status=COULD_NOT_TELL "
                "reason=adapter_capability_missing",
                symbol,
                acct,
            )
            return "unanswerable"
        pending = self._confirmation_exit_pending.get((acct, symbol), {})
        broker_order_id = str(pending.get("broker_order_id", "")).strip()
        if not broker_order_id:
            self.logger.error(
                "[OMS-V2-CONFIRMATION-EXIT-PROTECTION] sym=%s acct=%s status=COULD_NOT_TELL "
                "reason=entry_broker_order_id_missing",
                symbol,
                acct,
            )
            return "unanswerable"
        try:
            result = str(await release(acct, broker_order_id))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never race an unreadable OCO
            result = "unanswerable"
            self.logger.exception(
                "[OMS-V2-CONFIRMATION-EXIT-PROTECTION] sym=%s acct=%s status=COULD_NOT_TELL "
                "reason=release_failed",
                symbol,
                acct,
            )
        if result == "released":
            self._native_oco_armed_confirmed_at.pop((acct, symbol), None)
            self._native_oco_resolving.pop((acct, symbol), None)
            self.logger.info(
                "[OMS-V2-CONFIRMATION-EXIT-PROTECTION] sym=%s acct=%s status=RELEASED",
                symbol,
                acct,
            )
        elif result == "resolved_by_fill":
            self.logger.info(
                "[OMS-V2-CONFIRMATION-EXIT-PROTECTION] sym=%s acct=%s "
                "status=ALREADY_RESOLVED_BY_OCO_FILL",
                symbol,
                acct,
            )
        else:
            self.logger.error(
                "[OMS-V2-CONFIRMATION-EXIT-PROTECTION] sym=%s acct=%s status=COULD_NOT_TELL "
                "reason=release_unconfirmed",
                symbol,
                acct,
            )
        return result

    async def _evaluate_v2_managed_exit(self, acct: str, symbol: str) -> None:
        """Run the v2 exit ladder for one symbol on the latest quote. DECISION uses
        the live bid; FILL reference_price is the leg LEVEL (decision B — stop/floor/
        scale level) so live-paper agrees with the re-score by construction. Precedence
        hard>floor>scale, one action per quote. Sole-writer of the managed row; the
        quote->Position state-update is co-located here (deferred from slice 1).

        PR-A off-load: the per-tick READ and the no-exit / dedup price-state WRITE are
        the high-frequency freeze driver — they carry no broker await and no in-memory
        dict mutation, so they run OFF the event loop via ``_run_db``. Decisions
        (hydrate/ratchet) and the ``_managed_v2_symbols`` guard mutation stay on-loop.
        The RARE exit-emit (``_emit_v2_exit_on_loop``) keeps its on-loop session — it
        reaches the shared, dict-mutating, broker-awaiting ``_record_order_reports``
        (owned by PR-D); it is bounded to ~5s by #391 Fix-1 and fires only on an exit."""
        if not bool(getattr(self.settings, "oms_v2_exit_management_enabled", False)):
            return
        key = (acct, symbol)
        confirmation_pending = self.__dict__.setdefault("_confirmation_exit_pending", {})
        confirmation_inflight = self.__dict__.setdefault("_confirmation_exit_inflight", set())
        confirmation = confirmation_pending.get(key)
        quote = self._latest_quotes_by_symbol.get(symbol)
        if confirmation is not None:
            if not quote or key in confirmation_inflight:
                return
            try:
                evaluated_at = datetime.fromtimestamp(
                    float(str(confirmation["evaluated_at_ms"])) / 1000.0,
                    UTC,
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                self.logger.error(
                    "[OMS-V2-CONFIRMATION-EXIT-REFUSED] sym=%s reason=invalid_evaluation_time",
                    symbol,
                )
                confirmation_pending.pop(key, None)
                return
            quote_at = quote.get("received_at")
            if not isinstance(quote_at, datetime) or quote_at <= evaluated_at:
                return
            # ⛔⭐⭐ IDENTITY BEFORE PROTECTION (codex-2, #897 R1). Checking the binding only at the
            # emit was too late: a stale decision still reached
            # `_reconcile_confirmation_exit_protection`, which RELEASES the native OCO — so a
            # confirmation for a CLOSED position would strip the broker-side protection off the
            # position that replaced it, and then refuse to sell. On `resolved_by_fill` it would
            # close that position's managed row outright. Both are worse than the sell we were
            # already refusing.
            # ⇒ Nothing that touches broker protection may run until we know the decision is about
            # the position currently open.
            bound_row_id = str(confirmation.get("bound_managed_row_id", "") or "")
            open_row_id = await self._confirmation_bound_managed_row_id(acct, symbol)
            if not open_row_id or bound_row_id != open_row_id:
                self.logger.error(
                    "[OMS-V2-CONFIRMATION-EXIT-REFUSED] sym=%s acct=%s fill_id=%s bound_row=%s "
                    "open_row=%s reason=different_position — dropped BEFORE any OCO release",
                    symbol, acct, confirmation.get("source_fill_id", ""),
                    bound_row_id or "-", open_row_id or "-",
                )
                confirmation_pending.pop(key, None)
                return
            confirmation_inflight.add(key)
            protection = await self._reconcile_confirmation_exit_protection(acct, symbol)
            confirmation_inflight.discard(key)
            if protection == "resolved_by_fill":
                confirmation_pending.pop(key, None)
                # ⛔ Scope the close to the episode the identity check above actually verified.
                # `_reconcile_confirmation_exit_protection` awaited the broker; B may have replaced
                # A in the meantime, and B must not be closed by A's OCO fill.
                await self._close_resolved_oco_managed_row(
                    acct, symbol, expected_row_id=open_row_id
                )
                return
            if protection != "released":
                return
        elif self._native_oco_stand_down_active(acct, symbol):
            # A broker-native OCO owns this exit: target + stop are ONE broker-arbitrated
            # pair. Running the software ladder here would place a THIRD protective sell
            # against the same shares -- the NXTC oversell, merely relocated. Fail-open
            # lives in the predicate: anything short of fresh broker confirmation runs
            # the ladder instead of skipping it.
            return
        if not quote:
            return
        received_at = quote.get("received_at")
        if isinstance(received_at, datetime):
            age_ms = (utcnow() - received_at).total_seconds() * 1000.0
            if age_ms > float(getattr(self.settings, "oms_v2_exit_quote_max_age_ms", 5000)):
                return  # stale quote — never act on a gap
        bid = float(quote.get("bid") or 0.0)
        if bid <= 0:
            return
        # #6 (CLRO desync fix): mark closed on the confirmed FILL, not on submit. Default on.
        close_on_fill = bool(getattr(self.settings, "oms_v2_exit_close_on_fill_enabled", True))
        try:
            # Phase 1 — READ (off-loop): snapshot the open row + dedup state. Neither the
            # ORM row nor its Session escapes the worker thread. None => no open row.
            snapshot = await self._run_db(
                lambda session: self._read_v2_managed_snapshot(session, acct, symbol, close_on_fill),
                commit=False,
            )
            if snapshot is None:
                self._managed_v2_symbols.discard((acct, symbol))  # dict mutation stays on-loop
                self._cw_flip_pending.discard((acct, symbol))  # no open row -> drop any stale flip
                self._cw_floor_armed.discard((acct, symbol))  # no open row -> drop any armed floor
                self._post_exit_stale_held_clear(acct, symbol)
                confirmation_pending.pop(key, None)
                return

            # C3 — the broker has already sold, but its position view can remain HELD for minutes.
            # A durable SELL fill plus an older position snapshot is positive evidence of that
            # settlement window; quote cadence is not. Suppress until a NEW snapshot arrives,
            # allow at most one attempt per fresh HELD generation, and close on fresh FLAT.
            c3_action = self._post_exit_stale_held_action(acct, symbol, snapshot)
            if c3_action == "fresh_flat":
                await self._close_post_exit_stale_held_row(acct, symbol)
                return
            if c3_action not in ("not_applicable", "fresh_held_retry"):
                return

            # Phase 2 — DECIDE (on-loop, pure): hydrate + ratchet off the snapshot.
            entry_price = snapshot.entry_price
            position = self._hydrate_v2_position(snapshot)
            position.update_price(bid)

            # #6 dedup guard: an exit order already works for this symbol -> keep the
            # position open + monitored + broker-consistent; refresh ladder PRICE-state
            # only (write_quantity=False, held qty stays fill-gated) and do NOT re-emit.
            if snapshot.dedup_active:
                await self._run_db(
                    lambda session: self._persist_v2_price_state(
                        session, acct, symbol, position, write_quantity=False
                    ),
                    commit=True,
                )
                # A previously-triggered exit owns the shares and wins the time ordering.
                confirmation_pending.pop(key, None)
                return

            if confirmation is not None:
                # ⛔⭐⭐ THE DECISION MUST STILL BE ABOUT *THIS* POSITION.
                bound_row_id = str(confirmation.get("bound_managed_row_id", "") or "")
                if bound_row_id != snapshot.managed_row_id:
                    self.logger.error(
                        "[OMS-V2-CONFIRMATION-EXIT-REFUSED] sym=%s acct=%s fill_id=%s "
                        "bound_row=%s open_row=%s reason=different_position — the confirmation "
                        "was decided for a position that is no longer open; DROPPING it rather "
                        "than selling the one that is",
                        symbol, acct, confirmation.get("source_fill_id", ""),
                        bound_row_id or "-", snapshot.managed_row_id,
                    )
                    confirmation_pending.pop(key, None)
                    return
                # ⛔⭐⭐ ONE-SHOT, AND THE POP MUST HAPPEN *BEFORE* THE EMIT.
                # The tracker upstream calls itself a one-shot registry; the OMS side was not one.
                # Nothing here popped the pending entry after emitting, so once it became
                # executable it re-emitted on EVERY quote tick — 20 sells in 33 seconds on IMRN.
                # Popping first also means an emit that raises cannot leave it armed to repeat.
                confirmation_pending.pop(key, None)
                position = self._hydrate_v2_position(snapshot)
                position.update_price(bid)
                await self._emit_v2_exit_on_loop(
                    acct,
                    symbol,
                    position,
                    snapshot.entry_price,
                    kind="HARD",
                    reference_price=bid,
                    reason="oms_v2_managed_exit:CONFIRMATION_EXIT",
                    bid=bid,
                    close_on_fill=close_on_fill,
                )
                return

            # Confirmed-window (variant CW) exit: when on, this REPLACES the scale/floor/
            # stoch ladder with a full close at +target% OR -stop% OR a bar-close ATR flip
            # (no scales/floor). Precedence target > hard > flip: target/hard are mutually
            # exclusive on one bid; a pending flip that coincides with a +2% bid still
            # takes the better +2% exit, otherwise it closes at the bid. OFF => fall
            # through to the unchanged ladder below.
            if self._cw_exit_enabled:
                # Decision is the SHARED helper (exit_logic.cw_exit) — same code path as the
                # backtest so live == backtest. floor OFF => byte-identical to the prior hard-target
                # close; floor ON => arm a fixed +floor_pct% minimum, then consume the durable
                # BID-derived high-water floor that `_hydrate_v2_position` restored above. BID-only
                # is load-bearing: an ask spike on a wide spread must not manufacture a ratchet that
                # the unchanged bid immediately breaches.
                flip_pending = (acct, symbol) in self._cw_flip_pending
                armed = (acct, symbol) in self._cw_floor_armed
                action, _armed_out = cw_exit_decision(
                    entry_price, bid, armed,
                    target_pct=self._cw_target_pct, stop_pct=self._cw_stop_pct,
                    floor_pct=self._cw_floor_pct, floor_enabled=self._cw_floor_exit_enabled,
                    flip_pending=flip_pending,
                    ratcheted_floor_price=position.floor_price,
                )
                if action == "arm":
                    # reached +target% -> lock the floor, keep riding (NO exit); persist state.
                    self._cw_floor_armed.add((acct, symbol))
                    self.logger.info(
                        "[OMS-V2-CW-FLOOR-ARMED] sym=%s acct=%s bid=%.4f floor=%.4f (ride past +%.1f%%)",
                        symbol, acct, bid,
                        entry_price * (1.0 + self._cw_floor_pct / 100.0), self._cw_target_pct,
                    )
                    await self._run_db(
                        lambda session: self._persist_v2_price_state(
                            session, acct, symbol, position, write_quantity=True
                        ),
                        commit=True,
                    )
                elif action == "hold":
                    await self._run_db(
                        lambda session: self._persist_v2_price_state(
                            session, acct, symbol, position, write_quantity=True
                        ),
                        commit=True,
                    )
                else:  # target | floor | stop | flip -> full close
                    if action == "target":
                        ref, tag = entry_price * (1.0 + self._cw_target_pct / 100.0), "CW_TARGET"
                    elif action == "floor":
                        ref, tag = cw_effective_floor(
                            entry_price, self._cw_floor_pct, position.floor_price
                        ), "CW_FLOOR"
                    elif action == "stop":
                        ref, tag = entry_price * (1.0 - self._cw_stop_pct / 100.0), "CW_HARD_STOP"
                    else:  # flip: full close at the current bid (trend exit)
                        ref, tag = bid, "CW_FLIP"
                    await self._emit_v2_exit_on_loop(
                        acct, symbol, position, entry_price, kind="HARD",
                        reference_price=ref, reason=f"oms_v2_managed_exit:{tag}",
                        bid=bid, close_on_fill=close_on_fill,
                    )
                    self._cw_flip_pending.discard((acct, symbol))
                    self._cw_floor_armed.discard((acct, symbol))
                return

            hard = self._v2_exit_engine.check_hard_stop(position, bid)
            intrabar = None if hard is not None else self._v2_exit_engine.check_intrabar_exit(position)

            if hard is not None:
                ref = entry_price * (1.0 - float(self._v2_exit_config.stop_loss_pct) / 100.0)
                await self._emit_v2_exit_on_loop(
                    acct, symbol, position, entry_price, kind="HARD",
                    reference_price=ref, reason="oms_v2_managed_exit:HARD_STOP",
                    bid=bid, close_on_fill=close_on_fill,
                )
            elif intrabar is not None and intrabar.get("action") == "CLOSE":
                ref = float(position.floor_price) or bid
                await self._emit_v2_exit_on_loop(
                    acct, symbol, position, entry_price, kind="FLOOR",
                    reference_price=ref, reason="oms_v2_managed_exit:FLOOR_BREACH",
                    bid=bid, close_on_fill=close_on_fill,
                )
            elif intrabar is not None and intrabar.get("action") == "SCALE" and int(intrabar.get("sell_qty") or 0) > 0:
                sell_qty = int(intrabar["sell_qty"])
                level = str(intrabar.get("level") or "")
                ref = self._v2_scale_level_price(entry_price, level)
                await self._emit_v2_exit_on_loop(
                    acct, symbol, position, entry_price, kind="SCALE",
                    reference_price=ref, reason=f"oms_v2_managed_exit:SCALE_{level}",
                    bid=bid, close_on_fill=close_on_fill, sell_qty=sell_qty, level=level,
                )
            else:
                # no exit this quote — co-located quote->Position state update (off-loop write)
                await self._run_db(
                    lambda session: self._persist_v2_price_state(
                        session, acct, symbol, position, write_quantity=True
                    ),
                    commit=True,
                )
        except Exception as exc:  # noqa: BLE001 — the quote path must never die
            self.logger.warning("v2 managed-exit eval failed for %s: %s", symbol, exc)
            return

    def _post_exit_stale_held_clear(self, acct: str, symbol: str) -> None:
        self.__dict__.setdefault("_post_exit_stale_held_episodes", {}).pop(
            (acct, symbol), None
        )

    def _post_exit_stale_held_report(
        self,
        *,
        acct: str,
        symbol: str,
        episode: _PostExitStaleHeldEpisode,
        outcome: str,
        snapshot_at: datetime | None,
        age_seconds: float,
        level: int = logging.INFO,
    ) -> None:
        """Edge-triggered C3 marker with its observation and retry denominators on the line."""
        report_key = (outcome, snapshot_at)
        if episode.last_report_key == report_key:
            return
        episode.last_report_key = report_key
        self.logger.log(
            level,
            "[OMS-POST-EXIT-STALE-HELD] sym=%s acct=%s outcome=%s "
            "exit_fill_at=%s position_snapshot_at=%s stale_age_seconds=%.1f "
            "evaluated=%d retry_emitted=%d max_age_seconds=%.1f — "
            "trigger=latest durable fill in this managed episode is SELL; polarity: stale, "
            "missing, or reused position evidence emits 0 sells; only a newer HELD snapshot "
            "permits 1 bounded attempt; a newer FLAT snapshot closes without a sell",
            symbol,
            acct,
            outcome,
            episode.exit_fill_at.isoformat(),
            snapshot_at.isoformat() if snapshot_at is not None else "MISSING",
            age_seconds,
            episode.evaluated,
            episode.retries_emitted,
            self._post_exit_stale_held_max_age_seconds(),
        )

    def _post_exit_stale_held_max_age_seconds(self) -> float:
        settings = getattr(self, "settings", None)
        return float(
            getattr(
                settings,
                "oms_post_exit_stale_held_max_age_seconds",
                self._POST_EXIT_STALE_HELD_MAX_AGE_SECONDS,
            )
        )

    def _post_exit_stale_held_action(
        self,
        acct: str,
        symbol: str,
        snapshot: _V2ManagedSnapshot,
        *,
        now: datetime | None = None,
    ) -> str:
        """Return the C3 action for a managed position without performing broker I/O.

        `not_applicable` is the important negative polarity: absent a latest SELL fill, C3 has no
        authority to call a no-position refusal settlement lag and the existing exit path runs.
        Once a SELL fill is the latest durable fill in this position episode, elapsed time may
        stop/page but can never grant another sell. Only broker-position evidence newer than that
        fill can do so, once per snapshot generation.
        """
        key = (acct, symbol)
        exit_fill_at = _as_utc(snapshot.latest_fill_at)
        # The measured class and venue reason are Webull-specific. Do not silently broaden a
        # live-money exit change to Schwab merely because both venues persist the same fields.
        if (
            str(snapshot.broker_provider or "").lower() != "webull"
            or snapshot.latest_fill_side != "sell"
            or exit_fill_at is None
        ):
            self._post_exit_stale_held_clear(acct, symbol)
            return "not_applicable"

        episodes = self.__dict__.setdefault("_post_exit_stale_held_episodes", {})
        episode = episodes.get(key)
        if episode is None or episode.exit_fill_at != exit_fill_at:
            episode = _PostExitStaleHeldEpisode(exit_fill_at=exit_fill_at)
            episodes[key] = episode
        episode.evaluated += 1

        observed_at = _as_utc(now) or utcnow()
        age_seconds = max(0.0, (observed_at - exit_fill_at).total_seconds())
        snapshot_at = _as_utc(snapshot.account_position_source_updated_at)
        quantity = snapshot.account_position_quantity

        # Fresh FLAT is positive resolution evidence and remains useful even if it arrives after
        # the observation bound. The durable row closes without placing another order.
        if snapshot_at is not None and snapshot_at > exit_fill_at and quantity is not None:
            if quantity <= 0:
                self._post_exit_stale_held_report(
                    acct=acct,
                    symbol=symbol,
                    episode=episode,
                    outcome="fresh_flat_close",
                    snapshot_at=snapshot_at,
                    age_seconds=age_seconds,
                )
                return "fresh_flat"

        max_age_seconds = self._post_exit_stale_held_max_age_seconds()
        if age_seconds > max_age_seconds:
            if not episode.timeout_reported:
                episode.timeout_reported = True
                self._post_exit_stale_held_report(
                    acct=acct,
                    symbol=symbol,
                    episode=episode,
                    outcome="bound_exceeded_stop_and_report",
                    snapshot_at=snapshot_at,
                    age_seconds=age_seconds,
                    level=logging.ERROR,
                )
            return "bound_exceeded"

        if snapshot_at is None or quantity is None:
            self._post_exit_stale_held_report(
                acct=acct,
                symbol=symbol,
                episode=episode,
                outcome="could_not_tell",
                snapshot_at=snapshot_at,
                age_seconds=age_seconds,
                level=logging.WARNING,
            )
            return "could_not_tell"

        if snapshot_at <= exit_fill_at:
            self._post_exit_stale_held_report(
                acct=acct,
                symbol=symbol,
                episode=episode,
                outcome="waiting_for_fresh_position_evidence",
                snapshot_at=snapshot_at,
                age_seconds=age_seconds,
            )
            return "waiting_for_fresh_position_evidence"

        # Fresh and still HELD. One snapshot generation can fund exactly one retry, regardless of
        # quote volume; a later generation is required for another attempt.
        if (
            episode.last_retry_snapshot_at is not None
            and snapshot_at <= episode.last_retry_snapshot_at
        ):
            self._post_exit_stale_held_report(
                acct=acct,
                symbol=symbol,
                episode=episode,
                outcome="fresh_held_already_retried",
                snapshot_at=snapshot_at,
                age_seconds=age_seconds,
            )
            return "fresh_held_already_retried"
        episode.last_retry_snapshot_at = snapshot_at
        episode.retries_emitted += 1
        self._post_exit_stale_held_report(
            acct=acct,
            symbol=symbol,
            episode=episode,
            outcome="fresh_held_retry",
            snapshot_at=snapshot_at,
            age_seconds=age_seconds,
        )
        return "fresh_held_retry"

    async def _close_post_exit_stale_held_row(self, acct: str, symbol: str) -> None:
        """Close only on durable SELL fill + a newer broker-position snapshot that says FLAT."""

        def _close(session: Session) -> None:
            row = self.store.get_open_managed_position(
                session, broker_account_name=acct, symbol=symbol
            )
            if row is not None:
                self.store.close_managed_position(session, row)

        await self._run_db(_close, commit=True)
        key = (acct, symbol)
        self._managed_v2_symbols.discard(key)
        self._cw_flip_pending.discard(key)
        self._cw_floor_armed.discard(key)
        # #885 finding 1: this cleared the stand-down but NOT the reject total that produced it.
        self._v2_exit_end_episode(key)
        self._clear_exit_reservation_release(acct, symbol)
        self._a2_clear(acct, symbol)
        self._post_exit_stale_held_clear(acct, symbol)

    def _read_v2_managed_snapshot(
        self, session: Session, acct: str, symbol: str, close_on_fill: bool
    ) -> _V2ManagedSnapshot | None:
        """Off-loop READ unit: snapshot the open managed row + whether an exit order is
        already working (dedup). Returns None when there is no open row. Pure DB read —
        no ORM object leaves this function (the `_run_db` contract)."""
        row = self.store.get_open_managed_position(
            session, broker_account_name=acct, symbol=symbol
        )
        if row is None:
            return None
        dedup_active = False
        latest_fill_side: str | None = None
        latest_fill_at: datetime | None = None
        account_position_quantity: float | None = None
        account_position_source_updated_at: datetime | None = None
        broker_provider: str | None = None
        broker_account = session.scalar(
            select(BrokerAccount).where(BrokerAccount.name == row.broker_account_name)
        )
        if broker_account is not None:
            broker_provider = str(broker_account.provider or "").lower()
            # C3 is measured on Webull only. Keep the extra durable-evidence reads off every
            # Schwab quote path; widening a live-money control and its DB cost would both be
            # unsupported. A SCALE fill is deliberately excluded: it reduces a held position but
            # does not establish that the venue has already sold the whole exit. Full software
            # CLOSEs and broker-created native OCO children are the two authoritative shapes.
            if broker_provider == "webull":
                latest_fill = session.execute(
                    select(Fill.side, Fill.filled_at)
                    .join(BrokerOrder, BrokerOrder.id == Fill.order_id)
                    .outerjoin(TradeIntent, TradeIntent.id == BrokerOrder.intent_id)
                    .where(
                        Fill.broker_account_id == broker_account.id,
                        Fill.symbol == symbol,
                        Fill.side == "sell",
                        Fill.filled_at >= row.entry_time,
                        or_(
                            BrokerOrder.order_type == "oco_exit",
                            TradeIntent.intent_type == "close",
                        ),
                    )
                    .order_by(desc(Fill.filled_at))
                    .limit(1)
                ).first()
                if latest_fill is not None:
                    latest_fill_side = str(latest_fill.side or "").lower()
                    latest_fill_at = latest_fill.filled_at
                account_position = session.scalar(
                    select(AccountPosition).where(
                        AccountPosition.broker_account_id == broker_account.id,
                        AccountPosition.symbol == symbol,
                    )
                )
                if account_position is not None:
                    account_position_quantity = float(account_position.quantity)
                    account_position_source_updated_at = account_position.source_updated_at
        if close_on_fill:
            if broker_account is not None and self.store.get_open_exit_reserved_quantity(
                session,
                broker_account_id=broker_account.id,
                symbol=symbol,
                include_native_stop_guard=False,
            ) > 0:
                dedup_active = True
        return _V2ManagedSnapshot(
            symbol=row.symbol,
            managed_row_id=str(row.id),
            entry_price=float(row.entry_price),
            current_quantity=int(row.current_quantity),
            entry_time=str(row.entry_time),
            entry_path=row.entry_path or "",
            peak_profit_pct=float(row.peak_profit_pct or 0.0),
            tier=int(row.tier or 1),
            floor_pct=(float(row.floor_pct) if row.floor_pct is not None else None),
            floor_price=(float(row.floor_price) if row.floor_price is not None else None),
            scales_done=list(row.scales_done or []),
            scale_pnl=float(row.scale_pnl or 0.0),
            dedup_active=dedup_active,
            latest_fill_side=latest_fill_side,
            latest_fill_at=latest_fill_at,
            account_position_quantity=account_position_quantity,
            account_position_source_updated_at=account_position_source_updated_at,
            broker_provider=broker_provider,
        )

    def _persist_v2_price_state(
        self, session: Session, acct: str, symbol: str, position: Position, *, write_quantity: bool
    ) -> None:
        """Off-loop WRITE unit: persist ladder state for the still-open managed row.
        Re-fetches the row in this fresh session (no ORM crosses threads); no-op if the
        row has since closed (safe under the single-loop-thread model)."""
        row = self.store.get_open_managed_position(
            session, broker_account_name=acct, symbol=symbol
        )
        if row is None:
            return
        self.store.update_managed_position_from_position(
            session, row, position, write_quantity=write_quantity
        )

    # ------------------------------------------------ native-OCO exit fill capture (2026-07-27)
    # Since the native OCO went live (2026-07-22) NO exit fill has been recorded: the exit executes
    # on a broker-created child leg the OMS never placed, so nothing on the order path books it.
    # `collect_completed_trade_cycles` then has entries with no exits to pair, and the operator's
    # completed-trades table and P&L render BLANK. Measured: Schwab sell fills 07-21: 5 -> 07-23: 0.

    def _find_oco_entry_order(
        self, session: Session, acct: str, symbol: str, *, client_order_id: str = ""
    ):
        """The filled ENTRY order carrying the strategy/account ids the synthetic exit row needs.

        Native-bracket children hang off its client id. Bare Webull protection uses the separately
        persisted ``webull_protect_base_client_order_id`` in its payload. SYNC; caller's session.

        Tolerates a missing session: some callers of the close path drive it without one, and the
        exit capture is BOOKKEEPING — it must degrade to "no fill recorded", never raise into a
        path whose job is to clear a phantom row.
        """
        if session is None:
            return None
        stmt = (
            select(BrokerOrder)
            .join(BrokerAccount, BrokerAccount.id == BrokerOrder.broker_account_id)
            .where(
                BrokerAccount.name == acct,
                BrokerOrder.symbol == symbol.upper(),
                BrokerOrder.side == "buy",
                # ⛔⭐ FILLED ONLY (2026-07-31). Without this the lookup ordered by `updated_at`
                # alone, so the newest *cancelled* buy could win -- and a cancelled entry never
                # held a position, so it has NO OCO children and never will. The poll then asks
                # the broker about a bracket that cannot exist, gets nothing, and the managed row
                # stays open forever (blocking fan-out re-entry).
                #
                # Live AXTU 2026-07-31 on live:schwab_1m_v2:
                #     15:15:47  entry-1 FILLED     -> its OCO exit filled 15:26:52 @3.60
                #     15:31:16  a buy CANCELLED    <- newest by updated_at from here on
                #     16:03:05  entry-2 FILLED     -> its OCO exit filled 16:17:07 @3.83
                # Both exits went unrecorded and had to be recovered from Schwab history hours
                # later. Schwab's fill -> order-history propagation lags minutes, so the window in
                # which the correct filled entry was still the newest row was small.
                #
                # ⛔ Returning None when nothing is filled is CORRECT, not a regression: no filled
                # entry means no position, so there is no exit to find. `_fetch_oco_exit_detail`
                # already degrades to "no fill recorded" on an empty base coid.
                #
                # ⛔⭐ MUST INCLUDE `partially_filled`, not just "filled". A partially-filled entry
                # HOLDS SHARES, so it has a real position and a real bracket -- excluding it would
                # trade this bug for a NEW miss source, which is the exact failure being closed.
                # Not hypothetical: the adapter emits `partially_filled` both from the broker's own
                # status and computed (`0 < filledQuantity < quantity`, schwab.py), and orders here
                # really do split -- 2124 orders have 2 fills, 406 have 3, 11 have 4. A qty-2 v2
                # entry filling 1+1 passes through exactly this state.
                BrokerOrder.status.in_(("filled", "partially_filled")),
            )
            .order_by(desc(BrokerOrder.updated_at))
        )
        if client_order_id:
            stmt = stmt.where(BrokerOrder.client_order_id == client_order_id)
        return session.scalar(stmt)

    def _oco_exit_base_for_entry(
        self, entry_order, *, broker_account_name: str, symbol: str
    ) -> str:
        """Return the exact client id whose T/S children own the exit.

        A native bracket hangs its children off the entry coid. A bare Webull fan-out fill gets a
        *separate* exit-only pair, so using the entry coid polls two orders that never existed. That
        was DAIC 2026-08-25: the real pair used ``...-protect-9fea4541aa97`` while every poll asked
        for ``...-open-a2f8fc2f3f24T/S`` and therefore could never name the child that flattened it.
        The attach handle is persisted into the filled entry order payload; the in-memory map is a
        same-process fallback, never the durable source.
        """
        if entry_order is None:
            return ""
        payload = dict(getattr(entry_order, "payload", None) or {})
        is_bare_webull = (
            str(payload.get("fanout_leg", "")).lower() == "webull"
            and str(payload.get("native_oco_bracket", "")).lower() != "true"
        )
        if not is_bare_webull:
            return str(getattr(entry_order, "client_order_id", "") or "")
        persisted = str(payload.get("webull_protect_base_client_order_id", "") or "")
        if persisted:
            return persisted
        return str(
            getattr(self, "_webull_protect_base", {}).get(
                (broker_account_name, symbol.upper()), ""
            )
            or ""
        )

    async def _persist_webull_protect_base(
        self, broker_account_name: str, symbol: str, base_client_order_id: str, *,
        entry_client_order_id: str = "",
    ) -> bool:
        """Persist the only handle that can address broker-created Webull exit children."""
        try:
            def _write(session: Session) -> bool:
                # Use the fill event's exact entry id. Selecting merely "newest filled buy" can
                # write the handle onto yesterday's row if this background task beats the outer
                # fill transaction's commit.
                entry = self._find_oco_entry_order(
                    session, broker_account_name, symbol,
                    client_order_id=entry_client_order_id,
                )
                if entry is None:
                    return False
                payload = dict(entry.payload or {})
                payload["webull_protect_base_client_order_id"] = base_client_order_id
                entry.payload = payload
                session.flush()
                return True

            persisted = bool(await self._run_db(_write, commit=True))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - protection already exists; surface lost handle loudly
            self.logger.warning(
                "[WEBULL-PROTECT-HANDLE-LOST] %s %s base=%s handle_persisted=0 — the pair is "
                "resting but its child ids will be unaddressable after restart",
                symbol, broker_account_name, base_client_order_id, exc_info=True,
            )
            return False
        if not persisted:
            self.logger.warning(
                "[WEBULL-PROTECT-HANDLE-LOST] %s %s base=%s handle_persisted=0 — no filled entry "
                "order accepted the handle; child/time/price attribution is not restart-safe",
                symbol, broker_account_name, base_client_order_id,
            )
        return persisted

    async def _fetch_oco_exit_detail(
        self, acct: str, symbol: str, base_coid: str, *,
        entry_broker_order_id: str = "", entry_filled_at=None, entry_quantity=None,
    ):
        """Broker read ONLY — never touches the DB, so it is safe to await on the loop and MUST
        stay outside any `_run_db` unit (see that docstring). Never raises: a missing exit must
        degrade to 'no fill recorded', never break the close path that protects the account.

        ⭐ Returns THREE outcomes, not two (2026-07-28). `None` means "the broker says there is no
        exit" — a real answer. `_EXIT_FETCH_FAILED` means "we could not ask" — typically a Webull
        429, which is TRANSIENT. Collapsing the two lost the trade's P&L permanently on a temporary
        rate limit, which is the exact blackout this capture exists to close (live: CNET 16:11 ET).
        ⛔ The sentinel is a truthy object, so every caller MUST test for it explicitly BEFORE any
        `if detail:` / `if not detail:` branch."""
        # `self.settings` / `self.broker_adapter` are absent on instances built via __new__ by test
        # helpers (the class already carries class-level defaults for this reason), so resolve both
        # defensively — bookkeeping must never raise into the close path.
        settings = getattr(self, "settings", None)
        if not bool(getattr(settings, "oms_record_native_oco_exit_fills_enabled", False)):
            return None
        fn = getattr(getattr(self, "broker_adapter", None), "fetch_oco_exit_fill", None)
        if fn is None or not base_coid:
            return None
        try:
            # ⛔ Ownership proof, not a hint. Schwab used to match on SYMBOL ALONE and booked the
            # operator's hand-placed TOS sell as our exit (2026-07-29). It now fails CLOSED without
            # `entry_broker_order_id`, so these must be threaded through from the ENTRY order.
            return await fn(
                acct, symbol, base_coid,
                entry_broker_order_id=entry_broker_order_id,
                entry_filled_at=entry_filled_at,
                entry_quantity=entry_quantity,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self.logger.warning(
                "[OMS-OCO-EXIT-FILL] %s %s exit-fill fetch FAILED (transient, e.g. 429) — will "
                "retry on the next sync rather than book the trade unpaired", acct, symbol,
            )
            return _EXIT_FETCH_FAILED

    def _defer_for_exit_fetch(self, acct: str, symbol: str) -> bool:
        """True = hold the managed row open one more cycle so the next poll can retry the fetch.

        ⛔ BOUNDED on purpose. An open managed row is what blocks fan-out re-entry via
        `fanout_webull_collision_managed` — the very lag P0-a was enabled to remove — so a symbol
        whose fetch keeps failing must NOT pin the row open forever. After
        `_MAX_EXIT_FETCH_DEFERRALS` tries we close it and accept the unpaired trade: protecting
        entries outranks bookkeeping."""
        key = (acct, symbol)
        n = self._oco_exit_fetch_deferrals.get(key, 0) + 1
        if n > self._MAX_EXIT_FETCH_DEFERRALS:
            self._oco_exit_fetch_deferrals.pop(key, None)
            self.logger.warning(
                "[OMS-OCO-EXIT-FILL] %s %s exit-fill fetch failed %d times — closing the managed "
                "row anyway so it cannot block re-entry (P&L for this trade stays unpaired)",
                acct, symbol, self._MAX_EXIT_FETCH_DEFERRALS,
            )
            return False
        self._oco_exit_fetch_deferrals[key] = n
        self.logger.info(
            "[OMS-OCO-EXIT-FILL] %s %s holding the managed row open for retry %d/%d",
            acct, symbol, n, self._MAX_EXIT_FETCH_DEFERRALS,
        )
        return True

    def _persist_oco_exit_fill(self, session: Session, acct: str, symbol: str, entry_order, detail) -> bool:
        """Book the broker's exit as a real order + fill. SYNC, on the CALLER'S session — never
        opens its own (a nested session shares the connection and fights the outer transaction).

        The child leg IS a genuine broker order, so it is recorded as one rather than faked onto
        the entry row. Idempotent twice over: `get_or_create_order` keys on a deterministic
        client_order_id, and `record_fill_if_needed` refuses a duplicate `broker_fill_id`.
        """
        # One terminal attribution line per candidate. `evaluated=1` is the denominator: without
        # it, zero RECORDED markers cannot distinguish "nothing was evaluated" from "a candidate
        # reached this function but could not be made durable". The dedicated RECORDED marker below
        # remains success-only; this outcome line makes every refused branch visible as the other
        # polarity.
        def _outcome(*, attributed: int, could_not_tell: int, outcome: str, child_id: str = "") -> None:
            self.logger.info(
                "[OMS-CHILD-EXIT-ATTRIBUTION] %s %s trigger=filled_child_candidate evaluated=1 "
                "attributed=%d could_not_tell=%d outcome=%s child_order=%s — polarity: "
                "attributed=1 means the child/time/price already has a durable fill; "
                "could_not_tell=1 means the candidate was not safely attributable",
                acct, symbol, attributed, could_not_tell, outcome, child_id or "-",
            )

        if session is None:
            _outcome(attributed=0, could_not_tell=1, outcome="missing_session")
            return False
        if entry_order is None:
            _outcome(attributed=0, could_not_tell=1, outcome="missing_entry_order")
            return False
        if not detail:
            _outcome(attributed=0, could_not_tell=1, outcome="missing_child_detail")
            return False
        qty = detail.get("quantity")
        price = detail.get("price")
        if not qty or not price or Decimal(str(qty)) <= 0 or Decimal(str(price)) <= 0:
            _outcome(
                attributed=0, could_not_tell=1, outcome="invalid_quantity_or_price",
                child_id=str(detail.get("broker_order_id") or ""),
            )
            return False   # the $0 cancelled-sibling artefact must never become a -100% trade
        child_id = str(detail.get("broker_order_id") or "")
        intent = session.get(TradeIntent, entry_order.intent_id) if entry_order.intent_id else None
        if intent is None:
            _outcome(
                attributed=0, could_not_tell=1, outcome="missing_trade_intent",
                child_id=child_id,
            )
            return False
        exit_order = self.store.get_or_create_order(
            session,
            intent=intent,
            strategy_id=entry_order.strategy_id,
            broker_account_id=entry_order.broker_account_id,
            client_order_id=oco_exit_client_order_id(entry_order.client_order_id, child_id),
            symbol=symbol.upper(),
            side="sell",
            quantity=Decimal(str(qty)),
            metadata={"source": "native_oco_child_leg", "broker_order_id": child_id},
            broker_order_id=child_id or None,
            status="filled",
            order_type="oco_exit",
        )
        report = ExecutionReport(
            event_type="filled",
            client_order_id=exit_order.client_order_id,
            broker_order_id=child_id,
            broker_fill_id=f"{child_id}:{qty}" if child_id else None,
            symbol=symbol.upper(),
            side="sell",
            intent_type="close",
            quantity=Decimal(str(qty)),
            filled_quantity=Decimal(str(qty)),
            fill_price=Decimal(str(price)),
            reason="native_oco_exit",
            metadata={"source": "native_oco_child_leg"},
            reported_at=detail.get("filled_at"),
        )
        fill = self.store.record_fill_if_needed(
            session,
            order=exit_order,
            strategy_id=entry_order.strategy_id,
            broker_account_id=entry_order.broker_account_id,
            report=report,
            payload={"source": "native_oco_child_leg", "broker_order_id": child_id},
        )
        if fill is None:
            # All invalid-report cases were rejected above. Here `record_fill_if_needed` can only
            # mean the deterministic broker fill is already present / has no incremental quantity,
            # so attribution is durable even though this invocation created no second row.
            _outcome(
                attributed=1, could_not_tell=0, outcome="already_recorded",
                child_id=child_id,
            )
            return False
        _outcome(
            attributed=1, could_not_tell=0, outcome="recorded", child_id=child_id,
        )
        self.logger.info(
            "[OMS-CHILD-EXIT-RECORDED] %s %s trigger=filled_child_found evaluated=1 "
            "attributed=1 could_not_tell=0 qty=%s "
            "price=%s filled_at=%s child_order=%s — polarity: attributed=1 means child, time, "
            "price and fill are durably recorded; completed trade can now pair",
            acct, symbol, qty, price, detail.get("filled_at"), child_id or "?",
        )
        return True

    # ------------------------------------------------------------------ #
    # A2 — the broker refuses a sell on a position we HOLD
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_exit_refused_not_sellable(reason: str | None) -> bool:
        """True for the EXIT_REFUSED_POSITION_NOT_SELLABLE class, either broker.

        ⛔ Normalised, never a literal match on one broker's text: keying Schwab-only queries on
        "oversold" is exactly how 394 Webull rejects stayed invisible for 14 days.
        [[feedback_reject_query_states_account_visibility]]"""
        normalized = str(reason or "").strip().lower()
        if not normalized:
            return False
        despaced = normalized.replace("_", " ")
        return any(
            f in normalized or f in despaced.replace(" ", "_") or f.replace("_", " ") in despaced
            for f in A2_NOT_SELLABLE_REASON_SUBSTRINGS
        )

    def _a2_enabled_for(self, broker_account_name: str) -> bool:
        """Flag-gated AND scoped to `live:orb`.

        ⛔ SCOPE IS DELIBERATE. The class was measured on `live:orb` only -- ZERO reverse-option
        occurrences on Schwab. The CLASSIFIER is broker-agnostic (acceptance A6 requires it to catch
        both strings), but the BEHAVIOUR change is confined to where the evidence is. Schwab's
        oversold population belongs to D1 / slice C, which are separately analysed and PARKED;
        widening this to Schwab would change a live exit path on an unmeasured population."""
        if not bool(getattr(self.settings, "oms_a2_exit_not_sellable_backoff_enabled", False)):
            return False
        return broker_account_name == "live:orb"

    def _a2_note_reject(self, acct: str, symbol: str) -> None:
        """Record the episode start (first refusal) and this probe. Never raises."""
        try:
            key = (acct, symbol)
            now = utcnow()
            self.__dict__.setdefault("_a2_not_sellable_since", {}).setdefault(key, now)
            self.__dict__.setdefault("_a2_last_probe", {})[key] = now
        except Exception:  # noqa: BLE001 - bookkeeping must never break the exit path
            pass

    def _a2_should_defer(self, acct: str, symbol: str) -> bool:
        """True while inside the backoff interval since the last probe.

        ⛔ NOT abandonment. The managed row stays open and stays owned throughout -- Ship 2 must keep
        seeing it, or the unowned-position watch will correctly page. This only slows the cadence."""
        if not self._a2_enabled_for(acct):
            return False
        last = self.__dict__.get("_a2_last_probe", {}).get((acct, symbol))
        if last is None:
            return False
        return (utcnow() - last).total_seconds() < self._A2_BACKOFF_SECONDS

    def _a2_clear(self, acct: str, symbol: str) -> None:
        """The block ended (a close PLACED, or the row closed). Forget the episode."""
        key = (acct, symbol)
        self.__dict__.setdefault("_a2_not_sellable_since", {}).pop(key, None)
        self.__dict__.setdefault("_a2_last_probe", {}).pop(key, None)
        self.__dict__.setdefault("_a2_escalated", set()).discard(key)

    async def _a2_maybe_escalate(self, acct: str, symbol: str) -> None:
        """At the bound, PAGE if the position is still held.

        ⭐ GATED ON THE OUTCOME, NEVER ON THE CAUSE. What separates a harmless block (a live broker
        bracket reserving the shares, which will exit the position itself) from a dangerous one (the
        position is genuinely unsellable) is whether a broker OCO leg is live -- and OCO children are
        broker-created, never land in `broker_orders`, and CANNOT BE OBSERVED. So we do not try. We
        ask the only question the system can answer: IS IT STILL HELD? The harmless half self-
        resolves to flat and is never escalated; the dangerous half stays held and is.

        ⛔ Uses the TRI-STATE and treats UNKNOWN as still-held. Collapsing UNKNOWN into flat is the
        #608 defect (145 rejected sells).
        ⚠️ `account_positions` syncs on ~1-minute cadence, so a position that went flat seconds
        before the bound can still read HELD -- at most one sync interval of over-escalation at a
        90 s bound. Stated, not discovered."""
        key = (acct, symbol)
        if key in self.__dict__.setdefault("_a2_escalated", set()):
            return
        since = self.__dict__.get("_a2_not_sellable_since", {}).get(key)
        if since is None:
            return
        held_secs = (utcnow() - since).total_seconds()
        if held_secs < self._A2_ESCALATE_AFTER_SECONDS:
            return
        try:
            state = await self._broker_symbol_position_state(acct, symbol)
        except Exception:  # noqa: BLE001
            state = None
        # ⛔ ONLY a POSITIVELY-CONFIRMED flat cancels the page. There is no `_PositionRead.FLAT`
        # -- the tri-state is FLAT_CONFIRMED / FLAT_INFERRED / HELD / UNKNOWN, and FLAT_INFERRED
        # means "absent from the read", which a genuine close and a silently-failed read produce
        # IDENTICALLY (the ERNA lesson).
        # ⭐ For a PAGE the ambiguity resolves the opposite way to protection-deletion: a false page
        # is noise, a missed page is a position nobody knows is stuck. So everything that is not
        # FLAT_CONFIRMED -- including UNKNOWN, FLAT_INFERRED and a read that raised -- escalates.
        if state is _PositionRead.FLAT_CONFIRMED:
            self._a2_clear(acct, symbol)
            return
        self._a2_escalated.add(key)
        self.logger.error(
            "[OMS-A2-EXIT-BLOCKED] sym=%s acct=%s the broker has refused our exit for %.0fs "
            "(bound %.0fs) and the position is still %s. This is STRUCTURAL, not a transient "
            "reject: retrying faster cannot help. The managed row and any protection are LEFT IN "
            "PLACE and the probe continues. OPERATOR: a stop we cannot execute is your decision.",
            symbol, acct, held_secs, self._A2_ESCALATE_AFTER_SECONDS,
            getattr(state, "value", state),
        )

    def _v2_exit_end_episode(self, key: tuple[str, str]) -> None:
        """THE EPISODE IS OVER — drop every per-episode exit-retry counter for ``key``.

        ⛔⭐⭐ #885 retrospective finding 1 (2026-09-04). The absolute reject ceiling is documented
        as "per episode", but only TWO of the paths that end an episode actually cleared it. A
        position could accrue 17 rejected closes, close perfectly legitimately (a confirmed SELL
        fill, an OCO fill resolving the row, the post-exit stale-held close, or the emitter finding
        the row already gone), and the NEXT position on the same (account, symbol) would inherit
        those 17 and stand down after only 3 of its own. That is not a per-episode bound.

        ⛔ CALL THIS ONLY WHERE THE EPISODE GENUINELY ENDS — i.e. the managed row is closed or is
        already absent. It must NEVER be called from a broker READ. `_v2_close_reconcile_flat`
        deliberately lifts the stand-down on a positively-HELD read while LEAVING the total intact:
        in an exit-reservation jam we truthfully hold the position for the jam's whole duration, so
        a read-conditional ceiling can never terminate it. That asymmetry is the CHPT fix, not an
        oversight — do not "tidy" it into symmetry.
        """
        self._v2_exit_close_failures.pop(key, None)
        getattr(self, "_v2_exit_reject_total", {}).pop(key, None)
        self._v2_exit_stood_down.discard(key)

    async def _v2_close_reconcile_flat(self, session, acct: str, symbol: str, row) -> bool:
        """Phantom guard for the v2 CW full-close: count consecutive REJECTED closes; at the
        threshold, confirm against the broker. If FLAT (position closed out-of-band), close the
        managed row + disarm quote-eval and return True. Otherwise (still held, read failed, or
        below threshold) return False and keep managing. Clears ONLY on a CONFIRMED-flat read."""
        key = (acct, symbol)
        self._v2_exit_close_failures[key] = self._v2_exit_close_failures.get(key, 0) + 1
        if self._v2_exit_close_failures[key] < self._V2_EXIT_RECONCILE_AFTER_FAILURES:
            return False
        # Fresh-fill grace anchor for v2 = the managed row's entry_time (when we filled in).
        entry_at = _as_utc(getattr(row, "entry_time", None))
        # ONE broker read, reused for both the flat decision and the HELD-vs-UNKNOWN distinction.
        state = await self._broker_symbol_position_state(acct, symbol)
        if await self._broker_symbol_is_flat(acct, symbol, established_at=entry_at, state=state):
            # Capture the broker's own exit BEFORE closing the row, so the completed trade can
            # pair. This path is broker-agnostic (it fires for Schwab and Webull alike), which is
            # why it is the primary hook: Webull has no armed-OCO tracking to drive the fast path.
            entry_order = self._find_oco_entry_order(session, acct, symbol)
            base_coid = self._oco_exit_base_for_entry(
                entry_order, broker_account_name=acct, symbol=symbol
            )
            detail = await self._fetch_oco_exit_detail(
                acct, symbol, base_coid,
                entry_broker_order_id=str(getattr(entry_order, "broker_order_id", "") or ""),
                entry_quantity=getattr(entry_order, "quantity", None),
            )
            if detail is _EXIT_FETCH_FAILED:
                # Transient — hold the row so the next cycle can retry rather than book unpaired.
                if self._defer_for_exit_fetch(acct, symbol):
                    return False
                detail = None
            else:
                self._oco_exit_fetch_deferrals.pop((acct, symbol), None)
            if detail:
                self._persist_oco_exit_fill(session, acct, symbol, entry_order, detail)
            self.store.close_managed_position(session, row)
            self._managed_v2_symbols.discard(key)
            self._cw_flip_pending.discard(key)
            self._cw_floor_armed.discard(key)
            self._v2_exit_end_episode(key)  # confirmed FLAT + row closed — the episode ended
            self._clear_exit_reservation_release(acct, symbol)
            self._a2_clear(acct, symbol)
            self.logger.info(
                "[OMS-V2-RECONCILE-FLAT] sym=%s acct=%s broker flat after %d rejected closes -> "
                "clearing phantom managed row",
                symbol, acct, self._V2_EXIT_RECONCILE_AFTER_FAILURES,
            )
            return True
        # ⛔ ONLY a POSITIVELY-HELD read may reset the accumulator. `_broker_symbol_is_flat`
        # returns False for HELD *and* UNKNOWN; resetting on both is what let NCRA retry 145 times.
        if state is _PositionRead.HELD:
            # ⛔⭐⭐ RE-PROTECT WHAT WE UNCOVERED. If we cancelled this position's resting exit legs
            # to clear the way for a software close, and that close has now been REFUSED
            # `_V2_EXIT_RECONCILE_AFTER_FAILURES` times while the broker CONFIRMS we still hold it,
            # then the position is sitting there with nothing at the broker protecting it.
            #
            # Before the release existed, a failing close was survivable: the OCO legs stayed put
            # and took the position out at +2%/-5% on their own. Cancelling them removes that net,
            # so the release MUST be able to put it back or it is a strictly worse trade than the
            # reject storm it replaced.
            #
            # ⛔ Only on a POSITIVELY-HELD read. Re-attaching on an inconclusive one could place a
            # protective pair against shares we no longer own -- an unpaired sell, the E5/NXTC
            # oversell shape. And the latch is cleared so the NEXT exit decision may release again;
            # leaving it set would mean the re-attached pair could never be cleared out of the way.
            if key in self._exit_reservation_released:
                self._exit_reservation_released.discard(key)
                self.logger.warning(
                    "[OMS-EXIT-REPROTECT] %s %s — %d refused closes after releasing the resting exit "
                    "legs, and the broker still shows the position HELD. Re-attaching protection so "
                    "it is not left uncovered.",
                    symbol, acct, self._v2_exit_close_failures[key],
                )
                self._reprotect_after_failed_release(acct, symbol, row)
            self._v2_exit_close_failures[key] = 0  # we DO hold it -> keep managing, re-count later
            # A positive HELD read is new information: the loop may resume.
            self._v2_exit_stood_down.discard(key)
            # ⛔ A2: do NOT clear `_a2_not_sellable_since` here. A HELD read means the block is
            # STILL RUNNING -- clearing the episode start would restart the 90s clock on every pass
            # and the bound would never be reached. That is exactly the defect this fixes: #608's
            # counter resets on HELD, which is why the existing bound is unreachable during a jam.
            return False

        # Inconclusive (UNKNOWN, or a FLAT_INFERRED refused by the fresh-fill grace). We can neither
        # confirm we hold it nor safely clear it, so accumulate -- and stop hammering at the bound.
        if self._v2_exit_close_failures[key] >= self._V2_EXIT_ABANDON_AFTER_FAILURES:
            if key not in self._v2_exit_stood_down:
                self._v2_exit_stood_down.add(key)
                self.logger.error(
                    "[OMS-V2-EXIT-STAND-DOWN] sym=%s acct=%s %d rejected closes with an "
                    "INCONCLUSIVE broker read (state=%s) -> STOPPING the retry loop. The managed "
                    "row and any protection are LEFT IN PLACE; the exit poll will close it when the "
                    "broker shows the OCO resolved. OPERATOR: check the position by hand.",
                    symbol, acct, self._v2_exit_close_failures[key], getattr(state, "value", state),
                )
        return False

    async def _poll_native_oco_exits(self) -> None:
        """Ask the broker, on the PERIODIC SYNC, whether each open managed position's OCO exit has
        already filled — instead of waiting for the close path to notice.

        ⭐ WHY (measured live 2026-07-28): the close path needs 3 REJECTED CLOSES plus the 120s
        fresh-fill grace, so an exit at 09:36:40 was not recorded until 09:53:33 — **~17 minutes**.
        That lag is not only a reporting problem. The fan-out guard
        `fanout_webull_collision_managed` refuses a new leg while an open managed row exists, so the
        stale row BLOCKS RE-ENTRY: INLF signalled 5.4850, was skipped because the prior row was
        still open (its exit filled 5 SECONDS later), and the next signal a minute on filled at
        5.6200 — **+2.46% worse entry against a +2% target**. 7 of 9 lost signals that day were
        this guard.

        Safe to act on without a flat-read grace: unlike `_broker_symbol_is_flat`, this returns the
        broker's POSITIVE execution record (a FILLED child leg with a price and an order id), which
        is the same authority `_close_resolved_oco_managed_row` already documents.

        Never raises — bookkeeping must not break the sync that protects the account.
        """
        settings = getattr(self, "settings", None)
        if not bool(getattr(settings, "oms_native_oco_exit_poll_enabled", False)):
            return
        min_secs = float(getattr(settings, "oms_native_oco_exit_poll_min_secs", 30.0) or 0)
        now = time.monotonic()

        # ⭐⭐ THE WORK-LIST IS THE OPEN ROWS, NOT THE IN-MEMORY SET (2026-08-03).
        #
        # This loop used to iterate `_managed_v2_symbols`. That set is the QUOTE hot-path guard —
        # it exists so a quote does not open a DB session per tick, which is a real justification —
        # but reusing it as this poll's work-list has none: the poll runs on the periodic sync with
        # a >=30s per-key throttle, where a query is free.
        #
        # ⛔ THE FAILURE IT CAUSED. An open `oms_managed_positions` row whose key was missing from
        # the set was NEVER polled, NEVER logged and NEVER closed, and it blocks fan-out re-entry
        # via `fanout_webull_collision_managed` for as long as it lives. Because the loop body never
        # ran there was not even a miss line, so from outside "never polled" was indistinguishable
        # from "polled and found nothing". Three such phantoms on 2026-08-03 — live:orb FUSE (2h17m),
        # live:orb HYFM (1h41m), live:schwab_1m_v2 HYFM — every one with a filled entry, an OCO
        # bracket emitted, the broker flat, and ZERO miss lines. Meanwhile the SAME account polled
        # other symbols fine, so the account was never the discriminator.
        #
        # ⭐ CAUSE-AGNOSTIC BY DESIGN. Collision-skip, all five discard sites, rehydrate,
        # `_v2_accounts()`, the store lookup and a loop-abort were each ruled out with evidence and
        # HOW the keys left the set is still unpinned. Driving the poll from ground truth closes the
        # class whatever the eviction path turns out to be — the fix must not wait on that answer.
        #
        # Re-enrolling below repairs the quote guard as a side effect, so the hot path self-heals
        # instead of silently under-protecting a position it has stopped watching.
        try:
            def _open_keys(session: Session) -> list[tuple[str, str]]:
                out: list[tuple[str, str]] = []
                for a in self._v2_accounts():
                    for sym in self.store.list_open_managed_symbols(session, broker_account_name=a):
                        out.append((a, sym))
                return out
            work = await self._run_db(_open_keys, commit=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never break the protective sync
            self.logger.warning("v2 exit-poll work-list read failed: %s", exc)
            return

        for key in work:
            acct, symbol = key
            if key not in self._managed_v2_symbols:
                # The row is open but the hot-path guard had stopped watching it. Repair, and SAY SO
                # — a silent repair would hide exactly the divergence this fix exists to surface.
                self._managed_v2_symbols.add(key)
                self.logger.warning(
                    "[OMS-V2-POLL-REENROLL] %s %s open managed row was MISSING from the in-memory "
                    "guard — re-enrolled. It was invisible to the exit poll and was blocking "
                    "fan-out re-entry; the quote path had stopped evaluating it too.",
                    acct, symbol,
                )
            if now - self._oco_exit_poll_at.get(key, -1e9) < min_secs:
                continue
            self._oco_exit_poll_at[key] = now      # stamp BEFORE the call: a failure must not spin
            def _read_entry(session: Session, _a=acct, _s=symbol) -> tuple:
                o = self._find_oco_entry_order(session, _a, _s)
                return (self._oco_exit_base_for_entry(
                            o, broker_account_name=_a, symbol=_s
                        ),
                        str(getattr(o, "broker_order_id", "") or ""),
                        getattr(o, "quantity", None))

            try:
                base_coid, entry_oid, entry_qty = await self._run_db(_read_entry, commit=False)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # MISS-1. Was silent. An entry-order lookup that raises means this symbol can never
                # resolve its exit, and nothing said so.
                await self._log_oco_exit_miss(acct, symbol, base_coid="", reason="entry_lookup_raised")
                continue
            detail = await self._fetch_oco_exit_detail(
                acct, symbol, base_coid,
                entry_broker_order_id=entry_oid, entry_quantity=entry_qty,
            )
            if detail is _EXIT_FETCH_FAILED:
                continue     # transient: already logged by _fetch_oco_exit_detail
            if not detail:
                # ⭐⭐ MISS-3 — THE BLIND SPOT (instrumented 2026-07-31).
                # We polled, the broker answered, and it reported no filled exit leg. This path was
                # SILENT, so from outside the process "polled and found nothing" was indistinguishable
                # from "never polled at all" — which is exactly why the 07-31 AXTU/AXTX misses could
                # not be root-caused. Live that day: both symbols' OCO exits sat unrecorded for 26-90
                # minutes while FCUV's recorded fine every time; the managed rows stayed open, blocked
                # fan-out re-entry, and were only cleared by an OMS restart. Two of three AXTU round
                # trips have no exit record at all.
                # Log WHICH entry order we resolved and HOW LONG the row has been open: a miss on a
                # row open for an hour is the defect; a miss seconds after entry is normal (the OCO
                # simply has not fired yet).
                await self._log_oco_exit_miss(
                    acct, symbol, base_coid=base_coid,
                    reason=(
                        "broker_reported_no_filled_exit_leg"
                        if base_coid else "missing_exit_pair_base"
                    ),
                    entry_oid=entry_oid, entry_qty=entry_qty,
                )
                continue
            self.logger.info(
                "[OMS-OCO-EXIT-POLL] %s %s broker shows the OCO exit filled qty=%s @%s — "
                "recording now instead of waiting for the close path",
                acct, symbol, detail.get("quantity"), detail.get("price"),
            )
            await self._close_resolved_oco_managed_row(acct, symbol, detail=detail)

    # Anti-spam state for the OCO-exit miss log: (acct, symbol) -> (base_coid, last_log_monotonic).
    _OCO_EXIT_MISS_REPEAT_SECS = 300.0

    async def _log_oco_exit_miss(
        self, acct: str, symbol: str, *, base_coid: str, reason: str,
        entry_oid: str = "", entry_qty=None,
    ) -> None:
        """Make the OCO-exit-poll MISS path visible (log-only, 2026-07-31).

        ⛔ The poll logs on success and on fetch-FAILURE, but said nothing when it polled and the
        broker reported no filled exit leg. That silence is why the 07-31 AXTU/AXTX misses could not
        be diagnosed: we could not tell "polled, found nothing" from "never polled".

        ⛔ RATE-LIMITED ON PURPOSE. The poll runs per managed symbol every ~30s, so logging every
        miss would add thousands of lines a session to a box that already had a 559 MB log problem.
        Logs on the FIRST miss for a given entry order (the state that matters), then at most once
        per `_OCO_EXIT_MISS_REPEAT_SECS` while the same miss persists.

        ⭐ `row_age` is the discriminator: a miss seconds after entry is NORMAL (the OCO simply has
        not fired). A miss on a row open for tens of minutes is the defect.

        Never raises — this is diagnostics on a path whose job is protecting the account.
        """
        try:
            key = (acct, symbol)
            now = time.monotonic()
            state = getattr(self, "_oco_exit_miss_log_at", None)
            if state is None:
                state = self._oco_exit_miss_log_at = {}
            prev_coid, prev_at = state.get(key, (None, -1e9))
            if prev_coid == base_coid and (now - prev_at) < self._OCO_EXIT_MISS_REPEAT_SECS:
                return
            state[key] = (base_coid, now)

            row_age = "?"
            try:
                def _age(session: Session) -> str:
                    row = self.store.get_open_managed_position(
                        session, broker_account_name=acct, symbol=symbol
                    )
                    if row is None or getattr(row, "entry_time", None) is None:
                        return "no-open-row"
                    return f"{(utcnow() - row.entry_time).total_seconds():.0f}s"
                row_age = await self._run_db(_age, commit=False)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

            could_not_tell = int(reason != "broker_reported_no_filled_exit_leg")
            self.logger.info(
                "[OMS-OCO-EXIT-MISS] %s %s reason=%s entry_coid=%s entry_order_id=%s entry_qty=%s "
                "managed_row_age=%s trigger=broker_child_fill_evaluation evaluated=1 attributed=0 "
                "could_not_tell=%d — polarity: attributed=0 means no child/time/price was recorded; "
                "could_not_tell=1 means no valid handle/read could answer. Managed row stays OPEN "
                "(blocks fan-out re-entry). A miss on an OLD row is the 07-31 AXTU/AXTX defect.",
                acct, symbol, reason, base_coid or "-", entry_oid or "-", entry_qty, row_age,
                could_not_tell,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - diagnostics must never break the protective sync
            return

    async def _close_resolved_oco_managed_row(
        self, acct: str, symbol: str, *, detail=None, expected_row_id: str | None = None
    ) -> None:
        """Close the phantom v2 managed row for a symbol whose native OCO resolved BY A FILL.

        ⭐ WHY THIS EXISTS (2026-07-22): the broker-created OCO fill closes the position but never
        decrements our managed row -- the OMS never placed that sell, so nothing on the order path
        knows to. Without this, the row's ONLY close-path is the reject-driven
        `_v2_close_reconcile_flat`: the exit ladder resumes (the 90s grace is dwarfed by Schwab's
        ~6min fill->positions propagation) and churns ~3 rejected closes before the phantom clears.

        The caller (`_refresh_native_oco_armed_state`) closes ONLY on the broker's OWN execution
        record -- a recently-FILLED child SELL leg (`fetch_oco_resolved_by_fill_symbols`) -- NOT on
        a positions-endpoint read. That is authoritative ("the target/stop sold; you are flat") and
        so carries none of the FLAT_INFERRED ambiguity that made the 07-15 ERNA possible: a bracket
        that resolved by expiry/cancel (still held) has no filled leg and is never passed here."""
        # The broker await must stay OUTSIDE `_run_db` (see its docstring), so this is read ->
        # fetch -> write rather than one unit. Worst case the fetch fails and we close exactly as
        # before, just without a recorded exit.
        def _read_base(session: Session) -> tuple:
            order = self._find_oco_entry_order(session, acct, symbol)
            return (self._oco_exit_base_for_entry(
                        order, broker_account_name=acct, symbol=symbol
                    ),
                    str(getattr(order, "broker_order_id", "") or ""),
                    getattr(order, "quantity", None))

        base_coid, entry_oid, entry_qty = "", "", None
        try:
            if detail is None:
                base_coid, entry_oid, entry_qty = await self._run_db(_read_base, commit=False)
        except Exception:  # noqa: BLE001 - bookkeeping only; never block the phantom-row close
            self.logger.warning("[OMS-OCO-EXIT-FILL] %s %s entry-order lookup failed", acct, symbol)
        if detail is None:
            # the poll already fetched it; do not spend a second broker round trip (Webull 429s)
            detail = await self._fetch_oco_exit_detail(
                acct, symbol, base_coid,
                entry_broker_order_id=entry_oid, entry_quantity=entry_qty,
            )
        if detail is _EXIT_FETCH_FAILED:
            # Transient — hold the managed row open so the next sync retries the fetch, up to the
            # bounded cap, rather than closing the trade with no exit recorded.
            if self._defer_for_exit_fetch(acct, symbol):
                return
            detail = None
        else:
            self._oco_exit_fetch_deferrals.pop((acct, symbol), None)

        def _close(session: Session) -> None:
            row = self.store.get_open_managed_position(
                session, broker_account_name=acct, symbol=symbol
            )
            # ⛔⭐⭐ THE REPLACEMENT-DURING-AWAIT RACE (codex-2, #897 R2).
            # Everything above this point awaited a BROKER round trip. An episode can end and a new
            # one open inside that await, so the row read here need not be the row the caller
            # checked. Closing by (account, symbol) would then close the REPLACEMENT position on
            # the strength of the previous position's OCO leg filling.
            # ⇒ When the caller names the episode it resolved, the close is scoped to that UUID and
            # refuses anything else. Callers driven by a fresh broker execution record for the
            # symbol (not by a cached decision) pass nothing and are unchanged.
            if expected_row_id is not None:
                current = str(row.id) if row is not None else ""
                if current != expected_row_id:
                    self.logger.error(
                        "[OMS-V2-RESOLVED-CLOSE-REFUSED] sym=%s acct=%s expected_row=%s "
                        "open_row=%s reason=position_replaced_during_broker_await — NOT closing "
                        "the replacement position on the previous position's OCO fill",
                        symbol, acct, expected_row_id, current or "-",
                    )
                    return
            if detail:
                entry_order = self._find_oco_entry_order(session, acct, symbol)
                self._persist_oco_exit_fill(session, acct, symbol, entry_order, detail)
            if row is not None:
                self.store.close_managed_position(session, row)

        await self._run_db(_close, commit=True)
        key = (acct, symbol)
        self._managed_v2_symbols.discard(key)
        self._cw_flip_pending.discard(key)
        self._cw_floor_armed.discard(key)
        # ⛔ MUST clear: this is the RECOVERY path a stand-down relies on. Leaving it set would
        # silently suppress exits for the NEXT position on this symbol.
        # #885 finding 1: the reject TOTAL that produced the stand-down was missing from this very
        # block — clearing the flag while keeping its counter just rebuilds the stand-down at 3.
        self._v2_exit_end_episode(key)
        self._clear_exit_reservation_release(acct, symbol)
        self._a2_clear(acct, symbol)  # same reason: a stale A2 episode would defer the NEXT position
        self.logger.info(
            "[OMS-V2-OCO-RESOLVED-FLAT] sym=%s acct=%s OCO resolved by FILL (broker execution "
            "record) -> closing phantom managed row (no ladder rejects)",
            symbol, acct,
        )

    async def _emit_v2_exit_on_loop(
        self,
        acct: str,
        symbol: str,
        position: Position,
        entry_price: float,
        *,
        kind: str,
        reference_price: float,
        reason: str,
        bid: float,
        close_on_fill: bool,
        sell_qty: int | None = None,
        level: str | None = None,
    ) -> None:
        """The RARE v2 exit-emit, kept ON-LOOP (single session, one commit) exactly as
        before PR-A: it reaches the shared ``_record_order_reports``, which mutates
        ``_armed_hard_stops`` and awaits a broker submit, so it must not run in a worker
        thread. Bounded to ~5s by #391 Fix-1; fires only when an exit actually triggers.
        Behaviour of the per-kind write/close/scale + publish is byte-identical to the
        pre-split inline branches."""
        # The decision instant: the caller decided to exit on THIS quote and awaited us
        # directly, so nothing has blocked yet — no session, no DB read, no broker call.
        # Everything downstream (session open, intent write, submit, record) trails this.
        decided_at = datetime.now(UTC)
        events: list = []
        try:
            with self.session_factory() as session:
                row = self.store.get_open_managed_position(
                    session, broker_account_name=acct, symbol=symbol
                )
                if row is None:
                    self._managed_v2_symbols.discard((acct, symbol))
                    # #885 finding 1: no open row means the episode is already over. This lifted
                    # the stand-down but left its counter, so the next position started part-way
                    # to the ceiling.
                    self._v2_exit_end_episode((acct, symbol))
                    return
                if kind == "SCALE":
                    events = await self._emit_v2_managed_sell(
                        session, row, intent_type="scale", quantity=int(sell_qty or 0),
                        reference_price=reference_price, reason=reason, bid=bid,
                        decided_at=decided_at,
                    )
                    position.apply_scale(str(level or ""), int(sell_qty or 0), exit_price=reference_price)
                    # #6: fill-gate the scale quantity (write_quantity=False) — the scale fill
                    # decrements current_quantity; on submit persist only the ladder state.
                    self.store.update_managed_position_from_position(
                        session, row, position, write_quantity=not close_on_fill
                    )
                elif self._a2_should_defer(acct, symbol):
                    # A2 backoff. The broker is refusing this exit as not-sellable; the block is
                    # broker-side ACCOUNT STATE and re-emitting at the 1-2s ladder cadence provably
                    # achieves nothing (313 attempts / 816s on AAOG, all rejected). We keep probing
                    # every _A2_BACKOFF_SECONDS so we catch the moment it clears.
                    # ⛔ The managed row stays OPEN and OWNED -- this is not abandonment.
                    self.logger.info(
                        "[OMS-A2-BACKOFF] sym=%s acct=%s deferring a %s close — broker refuses the "
                        "exit as not-sellable; probing every %.0fs",
                        symbol, acct, kind, self._A2_BACKOFF_SECONDS,
                    )
                    return
                elif (acct, symbol) in self._v2_exit_stood_down:
                    # ⛔ Retry loop stood down (see _V2_EXIT_ABANDON_AFTER_FAILURES). Emitting again
                    # would just re-reject: 145 times on NCRA 2026-07-29. The row and any protection
                    # stay in place and the read-only exit poll still resolves it.
                    self.logger.warning(
                        "[OMS-V2-EXIT-STAND-DOWN] sym=%s acct=%s suppressing a %s close — the retry "
                        "loop is stood down pending an operator check", symbol, acct, kind,
                    )
                    return
                else:  # HARD / FLOOR — full close
                    events = await self._emit_v2_managed_sell(
                        session, row, intent_type="close", quantity=int(position.quantity),
                        reference_price=reference_price, reason=reason, bid=bid,
                        decided_at=decided_at,
                    )
                    key = (acct, symbol)
                    rejected = any(
                        str(getattr(ev.payload, "status", "")).lower() == "rejected" for ev in events
                    )
                    # ⛔⭐⭐ ABSOLUTE CEILING (2026-09-03 CHPT). Independent of the consecutive
                    # counter, which a truthful HELD read legitimately resets. Nothing clears this
                    # on a broker read — only real progress does.
                    if rejected:
                        totals = getattr(self, "_v2_exit_reject_total", None)
                        if totals is None:
                            totals = {}
                            self._v2_exit_reject_total = totals
                        totals[key] = totals.get(key, 0) + 1
                        if (
                            totals[key] >= self._V2_EXIT_MAX_REJECTS_PER_EPISODE
                            and key not in self._v2_exit_stood_down
                        ):
                            self._v2_exit_stood_down.add(key)
                            self.logger.error(
                                "[OMS-V2-EXIT-REJECT-CEILING] sym=%s acct=%s %d REJECTED closes in "
                                "this episode -> STOPPING the retry loop regardless of the broker "
                                "read. The managed row and ALL protection are LEFT IN PLACE and the "
                                "read-only exit poll still resolves it. Sustained rejected-order "
                                "volume risks broker API access. OPERATOR: check the position.",
                                symbol, acct, totals[key],
                            )
                    # Phantom guard: a rejected full-close may mean the broker is already flat
                    # (position closed out-of-band). Without this, close_on_fill waits for a fill
                    # that never comes and the exit churns rejected sells forever (2026-07-13 AGEN).
                    # A2: is this the "broker will not let us out of a position we hold" class?
                    a2_reason = next(
                        (
                            str(getattr(ev.payload, "reason", "") or "")
                            for ev in events
                            if str(getattr(ev.payload, "status", "")).lower() == "rejected"
                        ),
                        "",
                    )
                    a2_hit = (
                        rejected
                        and self._a2_enabled_for(acct)
                        and self._is_exit_refused_not_sellable(a2_reason)
                    )
                    if a2_hit:
                        self._a2_note_reject(acct, symbol)
                    reconciled = rejected and await self._v2_close_reconcile_flat(session, acct, symbol, row)
                    if a2_hit and not reconciled:
                        await self._a2_maybe_escalate(acct, symbol)
                    if not reconciled:
                        # ⛔ #885 finding 2: this was `if not rejected`, and an EMPTY event list has
                        # no rejected event in it. `_emit_v2_managed_sell` returns [] when the
                        # strategy/broker-account lookup misses, so a call that emitted NO ORDER AT
                        # ALL was booked as "the close placed" and wiped the ceiling. Progress now
                        # needs positive evidence of a recorded, non-refused order event.
                        # ⛔⭐⭐ codex-2, #893 R1: ONE close can return MULTIPLE reports, and Schwab
                        # can answer `accepted` THEN `rejected` for the same order. A bare
                        # "any positive status" test sees the `accepted` and clears the ceiling on
                        # the very tick that just incremented it — the counter rises to N and is
                        # popped straight back, which is the ORIGINAL defect wearing a new mask.
                        # ⇒ ANY rejection in the batch disqualifies the whole close from counting as
                        # progress. `not rejected` is the load-bearing half; the positive test is
                        # what keeps an EMPTY list (finding 2) from reading as success.
                        progressed = not rejected and any(
                            str(getattr(ev.payload, "status", "")).strip().lower()
                            not in self._V2_EXIT_NON_PROGRESS_STATUSES
                            for ev in events
                        )
                        if progressed:
                            self._v2_exit_close_failures.pop(key, None)  # the close placed -> reset counter
                            getattr(self, "_v2_exit_reject_total", {}).pop(key, None)  # real progress
                            self._a2_clear(acct, symbol)  # A2: the block ended
                        if close_on_fill:
                            # #6: do NOT close on submit — the confirmed fill closes the row.
                            # Persist price-state only; keep the position monitored/protected.
                            self.store.update_managed_position_from_position(
                                session, row, position, write_quantity=False
                            )
                        else:
                            self.store.close_managed_position(session, row)
                            self._managed_v2_symbols.discard(key)
                            self._v2_exit_end_episode(key)  # row closed — the episode ended
                            self._clear_exit_reservation_release(acct, symbol)
                            self._a2_clear(acct, symbol)
                session.commit()
        except Exception as exc:  # noqa: BLE001 — the quote path must never die
            self.logger.warning("v2 managed-exit emit failed for %s: %s", symbol, exc)
            return
        for ev in events:
            await self._publish_order_event(ev)

    async def _emit_v2_managed_sell(
        self,
        session: Session,
        row,
        *,
        intent_type: str,
        quantity: int,
        reference_price: float,
        reason: str,
        bid: float | None = None,
        decided_at: datetime | None = None,
    ) -> list:
        """THE SINGLE place a v2 managed-exit SELL is built. The order's
        broker_account_name is ALWAYS the managed row's account — the safe-by-
        construction invariant that pins routing to the simulated adapter
        (paper-isolation; proven by test_v2_exit_paper_isolation).

        Extended-hours routing (2026-07-05): in RTH the order stays MARKET/NORMAL
        (byte-identical). In extended hours a MARKET order cannot fill, so route a
        LIMIT with session=AM|PM off the live ``bid``: protective legs (hard-stop /
        floor, intent_type="close") price a MARKETABLE buffered limit so they
        reliably cross the spread; scale partials price AT the bid (patient). The
        leg-level ``reference_price`` is left unchanged so the [OMS-V2-MANAGED-EXIT]
        log and the live-paper re-score stay identical; ``limit_price`` drives the
        live order (adapter prefers limit_price, falls back to reference_price)."""
        strategy = session.scalar(select(Strategy).where(Strategy.code == row.strategy_code))
        broker_account = session.scalar(
            select(BrokerAccount).where(BrokerAccount.name == row.broker_account_name)
        )
        if strategy is None or broker_account is None:
            self.logger.warning(
                "[OMS-V2-MANAGED-EXIT] missing strategy/account %s/%s — no exit emitted",
                row.strategy_code, row.broker_account_name,
            )
            return []
        metadata = {
            "oms_v2_managed_exit": "true",
            "reference_price": f"{float(reference_price):.4f}",
            "order_type": "market",
            "time_in_force": "day",
        }
        order_type = "market"
        session_code = _extended_hours_session()
        if session_code is not None and bid and bid > 0:
            if intent_type == "scale":
                routed = _format_limit_price(bid)  # profit-taking: at the bid, zero buffer
            else:  # "close" = hard-stop / floor: buffered marketable limit that must fill
                buffer_pct = float(
                    getattr(self.settings, "oms_v2_exit_eh_protective_limit_buffer_pct", 0.5)
                )
                routed = _panic_limit_price(bid, buffer_pct)
            if routed is not None:
                # P0b GUARD (2026-07-31): a sell exit must never rest ABOVE the bid we priced it
                # from — such an order cannot fill at placement. Belt-and-braces over
                # `_panic_limit_price`, which should already buffer below the bid.
                # ⛔ This does NOT close P0b. Live KUST placed a 1.77 exit at 13:28:20 while the
                # SCHWAB bid was 1.75, and the caller applies a 5s freshness check — so the bid it
                # priced from was fresh but probably NOT Schwab's (`_latest_quotes_by_symbol` is
                # documented as Polygon NBBO at the mirror site). Pricing one venue's exit off
                # another venue's book is the open question; this guard only bounds the symptom.
                routed = _format_limit_price(
                    self._cap_exit_limit_to_bid(float(routed), bid=bid)
                ) or routed
                order_type = "limit"
                metadata.update(
                    {
                        "order_type": "limit",
                        "limit_price": routed,
                        "price_source": "bid",
                        "session": session_code,
                        "extended_hours": "true",
                    }
                )
        event = TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=row.strategy_code,
                broker_account_name=row.broker_account_name,  # <-- THE INVARIANT
                symbol=row.symbol,
                side="sell",
                quantity=Decimal(str(quantity)),
                intent_type=intent_type,
                reason=reason,
                metadata=dict(metadata),
            ),
        )
        intent = self.store.create_trade_intent(
            session, strategy=strategy, broker_account=broker_account, event=event
        )
        self._record_internal_risk_pass(
            session, intent=intent, strategy=strategy, broker_account=broker_account,
            metadata=dict(metadata), reason="oms_v2_managed_exit",
        )
        request = OrderRequest(
            client_order_id=self._build_client_order_id(event),
            broker_account_name=row.broker_account_name,  # <-- THE INVARIANT
            strategy_code=row.strategy_code,
            symbol=row.symbol,
            side="sell",
            intent_type=intent_type,
            quantity=Decimal(str(quantity)),
            reason=reason,
            metadata=dict(metadata),
            order_type=order_type,
            time_in_force="day",
        )
        # ⛔⭐ RELEASE THE RESERVATION FIRST. A resting exit leg holds these very shares, so without
        # this the sell below is refused as a naked short every time (58 rejects on live:orb
        # 2026-08-13). Flag-gated and capability-gated: with the flag off, or on an adapter with no
        # addressable legs (Schwab/simulated), this is a no-op and the submit is byte-identical.
        if bool(getattr(self.settings, "oms_v2_exit_release_reservation_enabled", False)):
            await self._release_exit_reservation_before_close(
                session=session,
                broker_account_name=row.broker_account_name,
                symbol=row.symbol,
            )
        reports = await self.broker_adapter.submit_order(request)
        events = await self._record_order_reports(
            session=session, intent=intent, strategy_id=strategy.id,
            broker_account_id=broker_account.id, intent_event=event,
            request=request, reports=reports,
        )
        # This line is emitted AFTER submit_order + _record_order_reports, so its own
        # timestamp trails the broker round-trip (measured 2026-07-15: median +1.4s, up
        # to +4.5s past the fill). decided_at carries the pre-submit decision instant so
        # exit latency can be measured against it, not against this line's timestamp.
        self.logger.info(
            "[OMS-V2-MANAGED-EXIT] %s sym=%s acct=%s qty=%s ref=%.4f decided_at=%s",
            reason, row.symbol, row.broker_account_name, quantity, float(reference_price),
            (decided_at or datetime.now(UTC)).isoformat(timespec="milliseconds"),
        )
        # P0a census DENOMINATOR: one managed exit emitted = one opportunity for the hold to be
        # evaluated. Counted here, beside the line that already marks the emit, so the two can
        # never disagree about what happened.
        self._p0a_census_note_submitted()
        return events

    async def _has_active_native_stop_guard_order(
        self,
        *,
        strategy_code: str,
        broker_account_name: str,
        symbol: str,
    ) -> bool:
        def _unit(session) -> bool:
            strategy = session.scalar(select(Strategy).where(Strategy.code == strategy_code))
            broker_account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == broker_account_name))
            if strategy is None or broker_account is None:
                return False
            native_order = self.store.find_open_native_stop_guard_order(
                session,
                strategy_id=strategy.id,
                broker_account_id=broker_account.id,
                symbol=symbol,
            )
            return native_order is not None

        # Off-loop (Fix 2): this pre-close dedup check sits on the hard-stop path
        # (A1). A stall here must never freeze the loop; and per Fix 3 the caller
        # treats a raised timeout as "proceed to fire the stop".
        return await self._run_db(_unit, commit=False)

    def _native_oco_stand_down_active(self, broker_account_name: str, symbol: str) -> bool:
        """True only when a broker-native OCO bracket is CONFIRMED armed for this position.

        *** THIS IS THE STAND-DOWN, AND IT FAILS OPEN BY DESIGN (operator-confirmed 2026-07-21).

        When it returns True the OMS does NOT run its exit ladder: the broker OCO owns the
        exit. That makes a WRONG True the worst failure in this system -- the software ladder
        stands down while no broker bracket is actually working, and the position has no exit
        at all (the ERNA shape). A wrong False merely risks an oversell, which is loud, logged
        and reconcilable (the NXTC class we already know how to recover).

        So the asymmetry is deliberate: stand-down requires positive, FRESH confirmation.
        Anything else -- no entry, a stale entry, a sync that stopped running -- resumes the
        ladder. This preserves the PRINCIPLE behind `_trigger_hard_stop`'s native-guard defer
        ("an OPTIMIZATION, not a safety gate"; it fires the stop anyway on a DB stall) rather
        than copying its mechanism, which would invert the failure direction here.

        Staleness is the fail-open trigger: the confirmation is refreshed by the periodic
        broker sync, so if that sync stalls, entries age out and the ladder comes back
        automatically. No liveness signal, no stand-down.
        """
        # getattr, not attribute access: this runs on the per-quote-tick path, and an
        # AttributeError there would propagate into the exit loop. Missing state resolves
        # to "no confirmation" -> the ladder runs, which is the safe direction.
        key = (broker_account_name, symbol)
        # Phase A EOD OCO transition: once 16:00 has released this position for the day, the
        # RTH OCO is dead (session=NORMAL can't fill in EH) — never stand down again today,
        # so the software EH-limit ladder owns the exit even if a stale broker read still
        # reports the expiring OCO as armed (defeats the 16:00 re-arm flip-flop). The set is
        # empty unless the flag fired, so this is byte-identical when the transition is OFF.
        eod_done = getattr(self, "_v2_eod_oco_transitioned", None)
        if eod_done and (self._session_day_et(), broker_account_name, symbol) in eod_done:
            return False
        armed = getattr(self, "_native_oco_armed_confirmed_at", None)
        if armed:
            confirmed_at = armed.get(key)
            if confirmed_at is not None:
                max_age = float(
                    getattr(self.settings, "oms_native_oco_confirmation_max_age_seconds", 30)
                )
                age_seconds = (utcnow() - confirmed_at).total_seconds()
                if age_seconds <= max_age:
                    return True
                self.logger.warning(
                    "[OMS-OCO-STAND-DOWN-EXPIRED] %s %s - broker confirmation is %.1fs old "
                    "(max %.1fs); RESUMING the software exit ladder (fail-open).",
                    broker_account_name, symbol, age_seconds, max_age,
                )
                armed.pop(key, None)

        # RESOLUTION grace: the OCO just cleared (a leg filled -> position closed), but the OMS
        # position state lags the broker fill. Keep deferring so the ladder does not fire a
        # redundant close on a stale "still-held" position (the rejected-sell-on-every-resolution
        # noise). Cleared early once the position reconciles out of _managed_v2_symbols (in the
        # refresh); the backstop below caps it so a genuinely-still-held position resumes.
        resolving = getattr(self, "_native_oco_resolving", None)
        if resolving:
            cleared_at = resolving.get(key)
            if cleared_at is not None:
                grace = float(getattr(self.settings, "oms_native_oco_resolve_grace_seconds", 90))
                if (utcnow() - cleared_at).total_seconds() <= grace:
                    return True
                resolving.pop(key, None)
        return False

    async def _refresh_native_oco_armed_state(self, account_names: list[str] | None) -> None:
        """Re-derive the stand-down set from the BROKER (never memory, never `broker_orders`).

        ⭐ WHY THE BROKER, NOT THE DB: OCO child legs are created BY THE BROKER, atomically with
        the parent -- the OMS never places them, so they never land in `broker_orders`. Asking
        the DB would always find nothing and the stand-down would never activate = the software
        ladder keeps running on an OCO'd position = the relocated collision. So this asks the
        broker directly (adapter `fetch_armed_native_oco_symbols`, the STEP-1-proven
        childOrderStrategies walk), matching the design's "re-derived from the broker on boot".

        Runs on the periodic sync (~5s), off-loop; the per-tick predicate stays a dict lookup.
        FAIL-OPEN: any error (unreachable broker, adapter without the capability) -> do NOT
        refresh -> confirmations age out -> the ladder resumes. Only symbols the broker confirms
        have BOTH exit legs WORKING count as armed.
        """
        if not bool(getattr(self.settings, "oms_native_oco_stand_down_enabled", False)):
            self._native_oco_armed_confirmed_at.clear()
            getattr(self, "_native_oco_resolving", {}).clear()
            return

        adapter = getattr(self, "broker_adapter", None)
        fn = getattr(adapter, "fetch_armed_native_oco_symbols", None)
        if fn is None:
            # No adapter / no capability -> nothing can be confirmed armed -> ladder runs.
            self._native_oco_armed_confirmed_at.clear()
            getattr(self, "_native_oco_resolving", {}).clear()
            return

        # Group the managed positions by broker account (one broker round-trip per account).
        by_account: dict[str, list[str]] = {}
        for (acct_name, symbol) in list(self._managed_v2_symbols):
            by_account.setdefault(acct_name, []).append(symbol)

        armed: set[tuple[str, str]] = set()
        try:
            for acct_name, symbols in by_account.items():
                confirmed = await fn(acct_name, symbols)
                for sym in confirmed:
                    armed.add((acct_name, sym))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail-open: do NOT refresh. Existing entries age out and the ladder resumes.
            self.logger.warning(
                "[OMS-OCO-STAND-DOWN] broker armed-state fetch failed; confirmations will age "
                "out and the software exit ladder will resume (fail-open)",
            )
            return

        now = utcnow()
        for key in armed:
            self._native_oco_armed_confirmed_at[key] = now
            # re-armed -> no longer resolving
            getattr(self, "_native_oco_resolving", {}).pop(key, None)
        for key in list(self._native_oco_armed_confirmed_at):
            if key not in armed:
                # Bracket gone (a leg filled / cancelled). Do NOT hand the ladder straight back:
                # enter the RESOLUTION grace so the position sync can reconcile the managed row to
                # flat before the ladder runs (else it fires a redundant close on a stale
                # position -> a rejected sell on every OCO resolution). The grace clears early
                # below once the position leaves _managed_v2_symbols.
                self._native_oco_armed_confirmed_at.pop(key, None)
                if hasattr(self, "_native_oco_resolving"):
                    self._native_oco_resolving[key] = now
                self.logger.info(
                    "[OMS-OCO-STAND-DOWN-CLEARED] %s %s - OCO gone; ladder deferred through the "
                    "resolution grace while the position reconciles", key[0], key[1],
                )
        # Drop resolving entries whose position has reconciled out of the managed set (flat) --
        # the common resolved-by-fill case, cleared well before the grace backstop.
        managed = getattr(self, "_managed_v2_symbols", set())
        for key in list(getattr(self, "_native_oco_resolving", {})):
            if key not in managed:
                self._native_oco_resolving.pop(key, None)

        # ⭐ Proactively close the phantom row for any STILL-managed symbol whose OCO resolved BY A
        # FILL -- instead of waiting for the exit ladder to resume and self-heal via ~3 rejected
        # closes. The broker-created OCO fill never decrements our managed row (the OMS never placed
        # that sell), so absent this the reject churn is the only close-path (the 90s grace is
        # dwarfed by Schwab's ~6min fill->positions propagation). Keyed on the broker's OWN
        # execution record (a recently-FILLED child SELL leg), NOT a positions-endpoint read: that
        # is authoritative and carries none of the FLAT_INFERRED ambiguity behind the 07-15 ERNA --
        # a bracket that resolved by expiry/cancel (still held) has no filled leg and is skipped, so
        # the ladder correctly manages it. FAIL-OPEN: any fetch error leaves the row for the grace
        # backstop + reject self-heal. Default-False flag ships this inert.
        if bool(getattr(self.settings, "oms_native_oco_resolve_flat_reconcile_enabled", False)):
            resolving_now = getattr(self, "_native_oco_resolving", {})
            resolving_by_acct: dict[str, list[str]] = {}
            for (acct_name, sym) in list(resolving_now):
                resolving_by_acct.setdefault(acct_name, []).append(sym)
            resolved_fn = getattr(adapter, "fetch_oco_resolved_by_fill_symbols", None)
            if resolved_fn is not None:
                for acct_name, syms in resolving_by_acct.items():
                    try:
                        filled = await resolved_fn(acct_name, syms)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 — best-effort; never break the sync
                        self.logger.warning(
                            "[OMS-V2-OCO-RESOLVED-FLAT] resolved-by-fill fetch failed for %s; "
                            "leaving the rows for the grace backstop + reject self-heal (fail-open)",
                            acct_name,
                        )
                        continue
                    for sym in filled:
                        await self._close_resolved_oco_managed_row(acct_name, sym)
                        self._native_oco_resolving.pop((acct_name, sym), None)

    async def _reconcile_after_intent(self, broker_account_name: str) -> None:
        """Best-effort post-intent broker→DB reconcile (Fix 3b).

        By the time this runs the order has ALREADY been submitted and committed,
        and the broker is the source of truth. A stall/failure here must NOT unwind
        the submitted (possibly protective) order or propagate to the caller — the
        next periodic ``sync_broker_orders`` back-fills the DB from the broker. So a
        hung reconcile degrades bookkeeping only, it never blocks or unwinds a stop."""
        try:
            await self.sync_broker_state(account_names=[broker_account_name])
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.warning(
                "post-intent broker-state reconcile failed for %s (order already "
                "submitted; next periodic sync reconciles)",
                broker_account_name,
            )

    async def sync_broker_state(self, *, account_names: list[str] | None = None) -> dict[str, int]:
        order_summary = await self.sync_broker_orders(account_names=account_names)
        # P0a census rollup. ⭐ Deliberately OUTSIDE any "did we evaluate anything" condition and
        # outside the per-account loop: it must emit `evaluated=0` on a quiet day, because that is
        # the reading that distinguishes "nothing qualified" from "the branch never runs". Gating
        # it on having something to report would rebuild the silence it exists to remove.
        self._maybe_emit_p0a_census()
        self._maybe_emit_order_event_census()
        self._maybe_emit_broker_read_census()
        # Re-derive the OCO stand-down from the rows this sync just refreshed -- the broker
        # is the source of truth, never in-memory arm state. Safe on the boot path too: a
        # restart starts with an empty set and only stands down once the broker confirms.
        await self._refresh_native_oco_armed_state(account_names)
        # Catch a resolved OCO exit HERE (~15s) rather than letting the close path discover it
        # ~17min later — the stale managed row blocks fan-out re-entry. See _poll_native_oco_exits.
        try:
            await self._poll_native_oco_exits()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - bookkeeping must never break the protective sync
            self.logger.exception("[OMS-OCO-EXIT-POLL] pass failed")
        position_summary = await self.sync_broker_positions(account_names=account_names)
        return {
            "accounts": position_summary["accounts"],
            "positions": position_summary["positions"],
            "orders": order_summary["orders"],
            "terminal_orders": order_summary["terminal_orders"],
        }

    async def sync_broker_positions(self, *, account_names: list[str] | None = None) -> dict[str, int]:
        # SPOF fix (Fix 2): this is the method BOTH 2026-07-01/02 zombies hung in
        # — `sync_account_positions -> session.flush()` ran on the event loop and
        # hung on a stalled connection. Split into phases so the DB work runs OFF
        # the loop via `_run_db` while the broker REST `await`s stay ON the loop
        # (the adapter already offloads them). Behavior is identical when healthy:
        # same accounts, same per-account sync, same virtual-clear, same commit
        # boundary (nothing is committed unless all broker fetches succeed).
        # Phase 1 (DB, off-loop): resolve target accounts as (id, name) tuples.
        def _load_accounts(session) -> list[tuple[UUID, str]]:
            if account_names is None:
                accounts = self.store.list_active_broker_accounts(session)
            else:
                accounts = self.store.list_named_broker_accounts(session, account_names)
            return [(account.id, account.name) for account in accounts]

        accounts = await self._run_db(_load_accounts, commit=False)

        # Phase 2 (broker REST, on-loop): fetch each account's live positions.
        # ⛔⭐⭐ L2 — REFUSE TO ZERO FROM A READ THAT FAILED. Independent of any adapter, so an
        # adapter regression cannot reopen the path on its own.
        #
        # An account whose read RAISED is EXCLUDED from `fetched` entirely, so neither
        # `sync_account_positions` (which zeroes every symbol absent from a snapshot) nor
        # `clear_virtual_positions_without_account_backing` ever sees it. #714 can later restore
        # an owned managed row, but consumers can act on the intervening zero before that repair.
        # A genuinely flat account still arrives as an empty LIST and still zeroes — that
        # distinction is the whole point and it only exists because the adapter now raises (L1).
        #
        # ⛔ PER-ACCOUNT, not per-sync. Previously this call was bare: one raising account aborted
        # the sync for EVERY account. Webull has raised since 2026-07-24, so a Webull 429 with no
        # cached snapshot was already able to stop Schwab's sync too.
        fetched: list[tuple[UUID, list]] = []
        unreadable: list[str] = []
        for account_id, account_name in accounts:
            try:
                snapshots = await self.broker_adapter.list_account_positions(account_name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a failed read is UNKNOWN, never flat
                unreadable.append(account_name)
                # ⛔⭐⭐ `consecutive=` MAKES A RUN A FACT INSTEAD OF AN INFERENCE.
                # A successful read logs NOTHING, so before this counter existed the only way
                # to tell "the next cycle also failed" from "one success then failed again"
                # was the GAP between failure lines measured against the sync cadence. On the
                # 2026-08-20 outage two gaps were 30s where the rest were 15s, and at 15s
                # cadence a 30s gap is either one success or one slow cycle — unresolvable
                # from the tape. That ambiguity is the difference between calling that run 22
                # and calling it 14. The OMS knows which it was; now it says so.
                # ⛔ Stamped on the line that ALREADY exists — no new log volume. A per-read
                # success line would be ~4/min/account and drown its own signal, which is the
                # `[SCHWAB30-REVISE-STORM]` shape this codebase has been bitten by before.
                # ⛔⭐ INSTRUMENTATION IS NEVER LOAD-BEARING ON THIS PATH. This block sits inside
                # the failure handler of a live-money positions sync; if it raised, a broker
                # outage would become a broken sync. Guarded the same way `_observe_settlement`
                # is ten lines below, and reached through `__dict__.setdefault` like
                # `_p0a_census` — a duck-typed or partially-constructed service (several tests
                # build one without `__init__`) must not be able to break a real sync. Found by
                # exactly those tests: the first version raised AttributeError in seven of them.
                # ⛔ `-1` is an OUT-OF-BAND sentinel, never a run length: if the counter itself
                # failed, the line must not print a number that reads like a count.
                try:
                    _runs = self.__dict__.setdefault("_broker_read_consecutive_failures", {})
                    n = int(_runs.get(account_name, 0)) + 1
                    _runs[account_name] = n
                    _failed = self.__dict__.setdefault("_broker_read_failed", {})
                    _failed[account_name] = int(_failed.get(account_name, 0)) + 1
                except Exception:  # noqa: BLE001 - a counter must never cost us the sync
                    n = -1
                self.logger.warning(
                    "[BROKER-SYNC-UNREADABLE] acct=%s consecutive=%d — positions read FAILED (%s). "
                    "This account is EXCLUDED from the sync: nothing is zeroed and no virtual "
                    "position is cleared for it this cycle. An empty list from a failed call is "
                    "not a flat account.",
                    account_name, n, exc,
                )
                continue
            # ⭐ The read SUCCEEDED. Reset the run — this is the event that was previously
            # invisible, and its absence is what forced Q5 to infer run boundaries from gaps.
            try:
                _runs = self.__dict__.setdefault("_broker_read_consecutive_failures", {})
                if _runs.get(account_name):
                    self.logger.info(
                        "[BROKER-SYNC-OK] acct=%s — positions read RECOVERED after %d consecutive "
                        "failure(s). This line is the RUN BOUNDARY; a reader no longer has to "
                        "infer one from the gap between failures.",
                        account_name, _runs[account_name],
                    )
                _runs[account_name] = 0
                _ok = self.__dict__.setdefault("_broker_read_ok", {})
                _ok[account_name] = int(_ok.get(account_name, 0)) + 1
            except Exception:  # noqa: BLE001 - a counter must never cost us the sync
                pass
            fetched.append((account_id, snapshots))
            # P0.2: read-only settlement probe on the read we ALREADY made (no extra call).
            # Wrapped: a probe must never be able to break broker-sync.
            try:
                self._observe_settlement(account_name, snapshots)
            except Exception:  # noqa: BLE001 — instrumentation is never load-bearing
                self.logger.warning("[SETTLE-LAG] probe failed for %s (ignored)", account_name)

        # Phase 3 (DB writes, off-loop): persist snapshots + clear unbacked
        # virtuals, committed inside the worker thread (the flush that froze the
        # loop now cannot).
        # ⛔ SCOPED TO ACCOUNTS WE ACTUALLY READ, not to every configured account. Using `accounts`
        # here would let the destructive clear run against an account whose read just failed — the
        # very thing L2 exists to prevent.
        account_ids = [account_id for account_id, _ in fetched]

        def _persist(session) -> int:
            synced_positions = 0
            for account_id, snapshots in fetched:
                synced_positions += self.store.sync_account_positions(
                    session,
                    broker_account_id=account_id,
                    snapshots=snapshots,
                )
            # ⛔⭐ N3 — NEVER PUBLISH A FRESH FALSE ZERO. #714 made an erased row restorable, but
            # measured restores took 6.648s--19.119s while downstream consumers act inside 10s.
            # Restoration is therefore recovery, never permission to erase early. The measured
            # minimum age holds a fresh fill through the settlement window; a genuinely stale
            # unbacked row is still cleared once the bound expires.
            deferred: list[tuple[UUID, str, Decimal, float]] = []
            clear_min_age_seconds = max(
                0.0,
                float(
                    getattr(
                        self.settings,
                        "oms_virtual_position_clear_min_age_seconds",
                        24.119,
                    )
                    or 0.0
                ),
            )
            cleared = self.store.clear_virtual_positions_without_account_backing(
                session,
                broker_account_ids=account_ids,
                minimum_age_seconds=clear_min_age_seconds,
                observed_at=utcnow(),
                deferred_out=deferred,
            )
            if deferred:
                account_names = {account_id: name for account_id, name in accounts}
                detail = ", ".join(
                    f"{account_names.get(account_id, account_id)}:{symbol}={quantity} "
                    f"age={age_seconds:.3f}s"
                    for account_id, symbol, quantity, age_seconds in deferred
                )
                self.logger.info(
                    "[VIRTUAL-CLEAR-DEFERRED] deferred=%d of unbacked_positive=%d "
                    "clear_allowed=0 min_age_seconds=%.3f: %s",
                    len(deferred),
                    len(deferred) + len(cleared),
                    clear_min_age_seconds,
                    detail,
                )
            if cleared:
                account_names = {account_id: name for account_id, name in accounts}
                detail = ", ".join(
                    f"{account_names.get(account_id, account_id)}:{symbol}={quantity}"
                    for account_id, symbol, quantity in cleared
                )
                self.logger.warning(
                    "[VIRTUAL-CLEAR] zeroed %d virtual position(s) with no broker backing "
                    "clear_allowed=1 evaluated_unbacked=%d min_age_seconds=%.3f: %s",
                    len(cleared),
                    len(deferred) + len(cleared),
                    clear_min_age_seconds,
                    detail,
                )
            # ⛔⭐⭐ L3 — THE ERASURE IS NO LONGER ONE-WAY. L1+L2 stop NEW wrongful clears; neither
            # repairs one that already happened, and it is the one-way property that turns a
            # transient broker hiccup into permanent ledger corruption.
            #
            # ⛔ RE-DERIVED FROM OUR OWN MANAGED ROWS, never from `account_positions`. The account
            # book is SHARED — restoring from it would attribute the operator's shares to us, which
            # is the scoping-invariant bypass (the operator held 5000 IVF on our account on 08-17).
            # `oms_managed_positions` is the ownership discriminator (#704) and carries OUR quantity.
            restored = self.store.restore_virtual_positions_from_managed(
                session, broker_account_ids=account_ids,
            )
            if restored:
                account_names = {account_id: name for account_id, name in accounts}
                detail = ", ".join(
                    f"{account_names.get(account_id, account_id)}:{symbol}={quantity}"
                    for account_id, symbol, quantity in restored
                )
                self.logger.warning(
                    "[VIRTUAL-RESTORE] re-derived %d virtual position(s) from our OWN open managed "
                    "rows after a wrongful clear: %s",
                    len(restored), detail,
                )
            return synced_positions

        synced_positions = await self._run_db(_persist)

        return {
            "accounts": len(accounts),
            "positions": synced_positions,
        }

    async def sync_broker_orders(self, *, account_names: list[str] | None = None) -> dict[str, int]:
        # Webull mirror-on-fill (see docs/webull-mirror-on-fill-design.md): collect confirmed
        # v2-primary buy-open FILLS observed this sync, then fire the async Webull mirror AFTER
        # the session closes (submit_order is async and must not run inside the DB session).
        # `record_fill_if_needed` is idempotent, so each real fill lands here exactly once.
        mirror_fill_candidates: list = []
        with self.session_factory() as session:
            if account_names is None:
                broker_accounts = self.store.list_active_broker_accounts(session)
            else:
                broker_accounts = self.store.list_named_broker_accounts(session, account_names)

            account_lookup = {account.id: account for account in broker_accounts}
            strategy_lookup = {
                strategy.id: strategy
                for strategy in session.scalars(select(Strategy)).all()
            }
            open_orders = self.store.list_open_orders(
                session,
                broker_account_ids=list(account_lookup.keys()),
            )

            synced_orders = 0
            terminal_orders = 0
            published_events: list[OrderEventEvent] = []
            for order in open_orders:
                account = account_lookup.get(order.broker_account_id)
                # Poll if we can identify the order at the broker by EITHER id. Webull's
                # place response returns only a client_order_id (broker_order_id arrives
                # later via order-detail), so gating on broker_order_id alone meant Webull
                # fills were never polled -> the fill went undetected and the hard stop
                # never armed (naked position). fetch_order_update keys on client_order_id,
                # so client_order_id is sufficient; Alpaca/Schwab always have a
                # broker_order_id by this point, so this is behaviour-identical for them.
                if account is None or not (order.broker_order_id or order.client_order_id):
                    continue

                intent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
                if intent is None:
                    continue
                strategy = strategy_lookup.get(order.strategy_id)

                request = OrderRequest(
                    client_order_id=order.client_order_id,
                    broker_account_name=account.name,
                    strategy_code="",
                    symbol=order.symbol,
                    side=order.side,  # type: ignore[arg-type]
                    intent_type=intent.intent_type,  # type: ignore[arg-type]
                    quantity=order.quantity,
                    reason=intent.reason,
                    metadata={**{str(k): str(v) for k, v in (order.payload or {}).items()}, "broker_order_id": order.broker_order_id or ""},
                    order_type=str((order.payload or {}).get("order_type", order.order_type)),
                    time_in_force=str((order.payload or {}).get("time_in_force", order.time_in_force)),
                )
                report = await self.broker_adapter.fetch_order_update(request)
                if report is None:
                    continue

                previous_status = order.status
                # Section 82 increment 1: a venue status/detail response may omit the
                # request metadata.  The durable order payload is authoritative for the
                # fan-out identity, so a later poll must not erase it from the order,
                # order event, or fill after submission already established the chain.
                recorded_metadata = carry_fanout_identity(
                    report.metadata, request.metadata
                )
                payload = {
                    "client_order_id": report.client_order_id,
                    "broker_order_id": report.broker_order_id,
                    "broker_fill_id": report.broker_fill_id,
                    "metadata": recorded_metadata,
                    "reason": report.reason,
                }
                fill = self.store.record_fill_if_needed(
                    session,
                    order=order,
                    strategy_id=order.strategy_id,
                    broker_account_id=order.broker_account_id,
                    report=report,
                    payload=payload,
                )
                status_changed = report.event_type != previous_status
                should_refresh = (
                    report.event_type in self.store.OPEN_ORDER_STATUSES
                    and self._should_refresh_working_order(order)
                )

                if status_changed or fill is not None:
                    synced_orders += 1
                    self.store.update_order_from_report(
                        order,
                        report=report,
                        metadata=recorded_metadata,
                    )
                    # ⛔ Q12/§183 — isolated. `record_fill_if_needed` already ran above this
                    # block, so at THIS site a failing audit row cost the POSITION UPDATE while
                    # leaving the fill row behind: a fill on the books that never moved a
                    # position, which is worse than losing both because it reconciles as real.
                    self._append_order_event_isolated(
                        session, order=order, report=report, payload=payload
                    )
                    if fill is not None:
                        self.store.apply_fill_to_positions(
                            session,
                            strategy_id=order.strategy_id,
                            broker_account_id=order.broker_account_id,
                            symbol=order.symbol,
                            side=order.side,
                            quantity=fill.quantity,
                            price=fill.price,
                            reported_at=fill.filled_at,
                        )
                        self._update_hard_stop_registry_from_fill(
                            strategy_code=strategy.code if strategy is not None else "",
                            broker_account_name=account.name,
                            symbol=order.symbol,
                            side=order.side,
                            intent_type=intent.intent_type,
                            quantity=fill.quantity,
                            price=fill.price,
                            metadata={str(k): str(v) for k, v in (order.payload or {}).items()},
                        )
                        published_events.extend(
                            await self._manage_native_stop_after_fill(
                                session=session,
                                strategy_id=order.strategy_id,
                                broker_account_id=order.broker_account_id,
                                strategy_code=strategy.code if strategy is not None else "",
                                broker_account_name=account.name,
                                symbol=order.symbol,
                                side=order.side,
                                intent_type=intent.intent_type,
                                metadata={str(k): str(v) for k, v in (order.payload or {}).items()},
                            )
                        )
                        self._apply_managed_position_after_fill(
                            session=session,
                            strategy_code=strategy.code if strategy is not None else "",
                            broker_account_name=account.name,
                            symbol=order.symbol,
                            side=order.side,
                            intent_type=intent.intent_type,
                            quantity=fill.quantity,
                            price=fill.price,
                            metadata={str(k): str(v) for k, v in (order.payload or {}).items()},
                            entry_client_order_id=str(order.client_order_id or ""),
                        )
                        # Webull mirror-on-fill: queue this fill iff it's a v2-primary buy-open
                        # (flag on + strategy schwab_1m_v2 + primary account + buy + open). Fired
                        # after the session closes; `record_fill_if_needed` idempotency guarantees
                        # exactly one queue per real fill (no double-place across re-syncs).
                        # ⭐ Suppressed when the dual-broker FAN-OUT flag is on — fan-out and
                        # mirror-on-fill are mutually exclusive: the bot already emitted the Webull
                        # leg in parallel at the cross, so mirroring the Schwab fill would double it.
                        if (
                            bool(getattr(self.settings, "strategy_schwab_1m_v2_webull_mirror_enabled", False))
                            and not bool(getattr(self.settings, "strategy_schwab_1m_v2_dual_broker_fanout_enabled", False))
                            and (strategy.code if strategy is not None else "") == "schwab_1m_v2"
                            and account.name == self.settings.strategy_schwab_1m_v2_account_name
                            and str(order.side).lower() == "buy"
                            and str(intent.intent_type).lower() == "open"
                        ):
                            mirror_fill_candidates.append(
                                (
                                    order.symbol,
                                    fill.quantity,
                                    float(fill.price),
                                    {str(k): str(v) for k, v in (order.payload or {}).items()},
                                )
                            )

                    self.store.mark_intent_from_report(intent, report)
                    self._update_hard_stop_registry_from_order_status(
                        strategy_code=strategy.code if strategy is not None else "",
                        broker_account_name=account.name,
                        symbol=order.symbol,
                        metadata={str(k): str(v) for k, v in (order.payload or {}).items()},
                        status=report.event_type,
                        reason=report.reason,
                    )
                    if previous_status in self.store.OPEN_ORDER_STATUSES and report.event_type in {"filled", "cancelled", "rejected"}:
                        terminal_orders += 1
                    if (
                        report.event_type in {"cancelled", "rejected"}
                        and not self._is_native_stop_guard_metadata(order.payload or {})
                        and str((order.payload or {}).get("stop_guard", "")).strip().lower() != "true"
                        and str(order.side).lower() == "sell"
                        and str(intent.intent_type).lower() in {"close", "scale"}
                    ):
                        published_events.extend(
                            await self._rearm_native_stop_from_registry(
                                session=session,
                                strategy_id=order.strategy_id,
                                broker_account_id=order.broker_account_id,
                                strategy_code=strategy.code if strategy is not None else "",
                                broker_account_name=account.name,
                                symbol=order.symbol,
                            )
                        )
                    published_events.append(
                        self._build_order_event(
                            intent_event=TradeIntentEvent(
                                source_service=SERVICE_NAME,
                                payload=TradeIntentPayload(
                                    strategy_code=strategy.code if strategy is not None else "",
                                    broker_account_name=account.name,
                                    symbol=order.symbol,
                                    side=order.side,  # type: ignore[arg-type]
                                    quantity=order.quantity,
                                    intent_type=intent.intent_type,  # type: ignore[arg-type]
                                    reason=intent.reason,
                                    metadata={**{str(k): str(v) for k, v in (order.payload or {}).items()}},
                                ),
                            ),
                            intent_db_id=intent.id,
                            order_db_id=order.id,
                            report=report,
                            client_order_id=order.client_order_id,
                            symbol=order.symbol,
                            side=order.side,
                            quantity=order.quantity,
                        )
                    )

                if should_refresh:
                    # Tier 2 + Tier 3: before paying for another cancel-and-replace
                    # cycle, decide whether the intent itself should be abandoned.
                    # The 2026-05-18 incident had 414 retries on a single intent;
                    # these guards stop that.
                    abandon_code: str | None = None
                    abandon_detail: str | None = None
                    # MARKET_CLOSED: when the market is not in a fillable session, a
                    # working order (open OR close) cannot fill — refreshing it just
                    # cancel/re-places forever (the 2026-07-13 AGEN/SOBR overnight
                    # churn: close intents were never abandoned because the guards
                    # below only covered `open`). Cancel + abandon it and stay quiet;
                    # for an exit, the managed row stays open and _evaluate_v2_managed_exit
                    # re-emits a fresh close when the market reopens. Native stop-guard
                    # orders are exempt — they are the resting overnight protection net.
                    if not self._is_stop_guard_order(order) and not self._market_is_fillable():
                        abandon_code = "MARKET_CLOSED"
                        abandon_detail = (
                            "market not in a fillable session "
                            f"({self._fillable_session_start_hour_et():02d}:00–"
                            f"{self._fillable_session_end_hour_et():02d}:00 ET); "
                            "parking until it reopens"
                        )
                    elif (
                        str(intent.intent_type).lower() == "open"
                        and not self._is_stop_guard_order(order)
                        and not self._is_resting_trigger_order(order)
                    ):
                        if self._intent_too_old(intent):
                            abandon_code = "INTENT_MAX_AGE"
                            abandon_detail = (
                                f"intent age {self._intent_age_secs(intent):.1f}s "
                                f"exceeds max {self._intent_max_age_secs()}s"
                            )
                        else:
                            invalid_reason = self._intent_setup_invalid_reason(
                                session,
                                intent=intent,
                                strategy=strategy,
                            )
                            if invalid_reason:
                                abandon_code = "SETUP_INVALID"
                                abandon_detail = invalid_reason
                    if abandon_code is not None:
                        await self._cancel_working_order_and_abandon_intent(
                            session=session,
                            order=order,
                            intent=intent,
                            strategy=strategy,
                            broker_account=account,
                            reason_code=abandon_code,
                            reason_detail=abandon_detail or abandon_code,
                        )
                        synced_orders += 1
                        terminal_orders += 1
                    elif self._resting_trigger_refresh_exempt(order):
                        # FULL-EXEMPT (default): a resting buy STOP/STOP_LIMIT is designed to sit at
                        # the ATR line until price crosses — never cancel/replace it on the refresh
                        # cadence (that tears it down 12x a min = the "no order resting when price
                        # crosses" miss the STABLE-REST rework fixed, 2026-07-23 NVVE). The strategy's
                        # STABLE-REST re-prices it on a >=1% trail move; MARKET_CLOSED above still
                        # abandons it out-of-session (the backstop is preserved). Leave it resting.
                        # Set oms_refresh_resting_trigger_orders=true to restore the old refresh.
                        pass
                    elif self._managed_exit_refresh_exempt(
                        order,
                        bid=(bid_now := (self._latest_quotes_by_symbol.get(order.symbol) or {}).get("bid")),
                    ):
                        # MANAGED-EXIT HOLD (P0, 2026-07-31 KUST). The exit is still marketable at
                        # its resting limit, so leave it on the book and let it fill. Cancel/replacing
                        # it here is what turned a +1.76% Webull fill into a -5.17% Schwab stop-out:
                        # nine cancels in six minutes against a bid that never once dropped below the
                        # limit. Once the bid falls below the limit this branch stops matching and the
                        # normal refresh below re-prices it.
                        #
                        # ⭐ OBSERVABILITY (2026-08-04). This branch was a BARE `pass` for its first
                        # four days, so the hold left no trace whatsoever: P0a sat
                        # "deployed-not-validated" partly because there was nothing to look FOR. A
                        # watch could only infer it from the ABSENCE of cancels, and absence-of-a-thing
                        # is exactly how a broken watch reports health. Log the engage EDGE (not every
                        # 5s tick, which would flood) so the hold is provable from the tape.
                        self._log_p0a_hold_edge(order, bid=bid_now)
                        self._p0a_census_note("held")
                    else:
                        # INSTRUMENT THE NEGATIVE (2026-08-06). Record WHY this evaluation declined.
                        # Without it a silent `[OMS-P0A-HOLD]` cannot be read: "no managed exit
                        # existed" and "the hold never engages" look identical from outside, and
                        # that ambiguity is what kept P0a deployed-not-validated since 07-31.
                        self._p0a_census_note(
                            self._p0a_decline_reason(
                                order,
                                bid=(self._latest_quotes_by_symbol.get(order.symbol) or {}).get("bid"),
                            )
                            or "declined_unclassified"
                        )
                        # If this order was being HELD and no longer qualifies, say so before the
                        # reprice — otherwise the hold's end is invisible and its duration
                        # unmeasurable, which is what made P0a unprovable in the first place.
                        if order.id in self._p0a_held_orders:
                            self._log_p0a_hold_release(
                                order,
                                bid=(self._latest_quotes_by_symbol.get(order.symbol) or {}).get("bid"),
                            )
                        refresh_result = await self._refresh_working_order(
                            session=session,
                            order=order,
                            intent=intent,
                            strategy_code=strategy.code if strategy is not None else "",
                            broker_account_name=account.name,
                            report=report,
                        )
                        synced_orders += refresh_result["orders"]
                        terminal_orders += refresh_result["terminal_orders"]
                        published_events.extend(refresh_result["published_events"])
                elif not status_changed and fill is None:
                    continue

            self._terminalize_orphaned_active_intents(
                session,
                broker_account_ids=list(account_lookup.keys()),
            )
            session.commit()

        for order_event in published_events:
            await self._publish_order_event(order_event)

        # F2: mirror armed-stop changes made during the per-order sync (arm/decrement/
        # clear/rearm) to the durable table, off-loop after the session closed.
        await self._flush_dirty_armed_stops()

        # Webull mirror-on-fill: fire each queued v2-primary buy-open fill to the Webull
        # account as an INDEPENDENT post-step (own session, all-swallowing) — a Webull failure
        # can NEVER unwind the already-committed Schwab fill. Dormant when the flag is off (no
        # candidate is ever queued).
        for sym, qty, px, md in mirror_fill_candidates:
            await self._mirror_v2_fill_to_webull(
                symbol=sym, quantity=qty, schwab_fill_price=px, source_metadata=md
            )
        return {
            "orders": synced_orders,
            "terminal_orders": terminal_orders,
        }

    def _terminalize_orphaned_active_intents(
        self,
        session: Session,
        *,
        broker_account_ids: list[UUID],
    ) -> int:
        """Repair active intents whose broker orders have already gone terminal."""
        if not broker_account_ids:
            return 0

        active_statuses = set(self.store.OPEN_ORDER_STATUSES)
        repaired = 0
        active_intents = session.scalars(
            select(TradeIntent)
            .where(TradeIntent.broker_account_id.in_(broker_account_ids))
            .where(TradeIntent.status.in_(("pending", "submitted", "accepted")))
        ).all()

        for intent in active_intents:
            related_orders = session.scalars(
                select(BrokerOrder).where(BrokerOrder.intent_id == intent.id)
            ).all()
            if related_orders:
                statuses = {str(order.status).lower() for order in related_orders}
                if statuses & active_statuses:
                    continue
                terminal_status = self._terminal_intent_status_from_order_statuses(statuses)
                if terminal_status is None:
                    continue
                self.store.mark_intent_status(intent, terminal_status)
                repaired += 1
                continue

            if str(intent.intent_type).lower() != "cancel":
                continue
            target_order = self._target_order_for_cancel_intent(session, intent)
            if target_order is None or str(target_order.status).lower() in active_statuses:
                continue
            self.store.mark_intent_status(intent, str(target_order.status).lower())
            repaired += 1

        if repaired:
            self.logger.info("[OMS-INTENT-REPAIR] terminalized %s orphaned active intents", repaired)
        return repaired

    @staticmethod
    def _terminal_intent_status_from_order_statuses(statuses: set[str]) -> str | None:
        if not statuses:
            return None
        if "filled" in statuses:
            return "filled"
        if "partially_filled" in statuses:
            return None
        if "cancelled" in statuses:
            return "cancelled"
        if "rejected" in statuses:
            return "rejected"
        return None

    def _target_order_for_cancel_intent(self, session: Session, intent: TradeIntent) -> BrokerOrder | None:
        payload = intent.payload or {}
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        if not isinstance(metadata, dict):
            return None
        target_client_order_id = str(metadata.get("target_client_order_id") or "").strip()
        broker_order_id = str(metadata.get("broker_order_id") or "").strip()
        if target_client_order_id:
            order = session.scalar(
                select(BrokerOrder).where(BrokerOrder.client_order_id == target_client_order_id)
            )
            if order is not None:
                return order
        if broker_order_id:
            return session.scalar(select(BrokerOrder).where(BrokerOrder.broker_order_id == broker_order_id))
        return None

    async def _publish_order_event(self, event: OrderEventEvent) -> None:
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "order-events"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_order_event_stream_maxlen,
            approximate=True,
        )

    async def _publish_heartbeat(self, status: str, details: dict[str, str]) -> None:
        event = HeartbeatEvent(
            source_service=SERVICE_NAME,
            payload=HeartbeatPayload(
                service_name=SERVICE_NAME,
                instance_name=self.instance_name,
                status=status,
                details=details,
            ),
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "heartbeats"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_heartbeat_stream_maxlen,
            approximate=True,
        )

    async def _broker_sync_interval_seconds(self) -> float:
        default_interval = max(1.0, float(self.settings.oms_broker_sync_interval_seconds))
        if await self._has_active_stop_guard_orders():
            return min(
                default_interval,
                max(0.1, float(self.settings.oms_stop_guard_refresh_stage_1_seconds)),
            )
        return default_interval

    async def _has_active_stop_guard_orders(self) -> bool:
        def _unit(session) -> bool:
            broker_accounts = self.store.list_active_broker_accounts(session)
            open_orders = self.store.list_open_orders(
                session,
                broker_account_ids=[account.id for account in broker_accounts],
            )
            return any(self._is_stop_guard_order(order) for order in open_orders)

        return await self._run_db(_unit, commit=False)

    @staticmethod
    def _hard_stop_key(strategy_code: str, broker_account_name: str, symbol: str) -> tuple[str, str, str]:
        return (str(strategy_code), str(broker_account_name), str(symbol).upper())

    @staticmethod
    def _is_native_stop_guard_metadata(metadata: dict[str, object] | None) -> bool:
        payload = metadata or {}
        return str(payload.get("native_stop_guard", "")).strip().lower() == "true"

    def _is_native_stop_guard_order(self, order: BrokerOrder) -> bool:
        return self._is_native_stop_guard_metadata(order.payload or {})

    async def _handle_quote_tick_event(self, event: QuoteTickEvent) -> None:
        symbol = str(event.payload.symbol).upper()
        # Event-time, not processing-time: stamp with the producer's `produced_at` so the
        # downstream staleness guard measures TRUE price age. (Same host as market-data →
        # no clock skew.) Previously this was utcnow() at processing time, which made the
        # guard blind to consumption lag and let the LNAI exit act on a 70s-old quote.
        self._latest_quotes_by_symbol[symbol] = {
            "bid": float(event.payload.bid_price),
            "ask": float(event.payload.ask_price),
            "received_at": self._event_time(event),
        }
        if self._armed_hard_stops:
            await self._evaluate_hard_stop_market_event(symbol)
        await self._cancel_drifted_working_orders(symbol)
        # Slice-3: run the v2 exit ladder on this quote, but ONLY for symbols with an
        # open v2 managed row (the in-memory guard keeps the hot path free of DB hits
        # for everything else; empty set when the flag is OFF → no-op).
        # Fillable-session gate: outside 7 AM–8 PM ET an exit cannot fill, so do not
        # emit one — the refresh-loop MARKET_CLOSED abandon (sync_broker_orders) mops
        # up any working exit, keeping v2 quiet when the market is closed instead of
        # churning unfillable exits overnight. Management resumes when it reopens.
        if not self._market_is_fillable():
            return
        for acct in self._v2_accounts():
            if (acct, symbol) in self._managed_v2_symbols:
                await self._evaluate_v2_managed_exit(acct, symbol)

    async def _handle_trade_tick_event(self, event: TradeTickEvent) -> None:
        symbol = str(event.payload.symbol).upper()
        self._latest_trades_by_symbol[symbol] = {
            "price": float(event.payload.price),
            "received_at": self._event_time(event),
        }
        await self._evaluate_hard_stop_market_event(symbol)

    @staticmethod
    def _event_time(event: object) -> datetime:
        """Producer publish time for staleness measurement, falling back to now() if the
        envelope lacks a usable timestamp (so a missing field can never wedge the path)."""
        produced_at = getattr(event, "produced_at", None)
        if isinstance(produced_at, datetime):
            return produced_at
        return utcnow()

    async def _evaluate_hard_stop_market_event(self, symbol: str) -> None:
        normalized_symbol = str(symbol).upper()
        matching_stops = [
            stop
            for stop in self._armed_hard_stops.values()
            if stop.symbol == normalized_symbol and stop.quantity > 0
        ]
        if not matching_stops:
            return
        for stop in matching_stops:
            if stop.close_in_flight:
                continue
            self._ratchet_trailing_stop(stop)  # raise the trailing stop on favorable moves (inert when trail_pct=0)
            if self._is_hard_stop_trigger_throttled(stop):
                continue
            trigger_price, trigger_source = self._resolve_hard_stop_trigger_price(stop)
            if trigger_price is None or trigger_source is None:
                continue
            if Decimal(str(trigger_price)) > stop.stop_price:
                continue
            await self._trigger_hard_stop(stop, trigger_price=Decimal(str(trigger_price)), trigger_source=trigger_source)
        # F2: persist any ratcheted/cleared stops OFF the loop, AFTER every trigger decision
        # above — so the mirror stays fresh for restart-recovery without ever delaying a
        # stop (the in-memory stop is authoritative). No-op when nothing dirtied.
        await self._flush_dirty_armed_stops()

    def _resolve_hard_stop_trigger_price(self, stop: ArmedHardStop) -> tuple[float | None, str | None]:
        max_age_ms = max(0, stop.quote_max_age_ms)
        fresh_bid: float | None = None
        quote = self._latest_quotes_by_symbol.get(stop.symbol)
        if quote is not None:
            received_at = quote.get("received_at")
            bid = quote.get("bid")
            if isinstance(received_at, datetime) and bid is not None:
                age_ms = (utcnow() - received_at).total_seconds() * 1000
                if age_ms <= max_age_ms:
                    fresh_bid = float(bid)
        fresh_last: float | None = None
        trade = self._latest_trades_by_symbol.get(stop.symbol)
        if trade is not None and trade.get("price") is not None:
            received_at = trade.get("received_at")
            if isinstance(received_at, datetime):
                age_ms = (utcnow() - received_at).total_seconds() * 1000
                if age_ms <= max_age_ms:
                    fresh_last = float(trade["price"])
        if fresh_bid is not None and Decimal(str(fresh_bid)) <= stop.stop_price:
            return fresh_bid, "bid"
        if fresh_last is not None and Decimal(str(fresh_last)) <= stop.stop_price:
            return fresh_last, "last"
        if fresh_bid is not None:
            return fresh_bid, "bid"
        if fresh_last is not None:
            return fresh_last, "last"
        return None, None

    @staticmethod
    def _ratcheted_trailing_stop(
        stop_price: Decimal, high_water_mark: Decimal, observed_price: Decimal, trail_pct: float
    ) -> tuple[Decimal, Decimal]:
        """Pure ratchet math. Returns (new_stop_price, new_high_water_mark); the
        stop only ever rises. ``trail_pct <= 0`` is inert (returns inputs)."""
        if trail_pct <= 0 or observed_price <= high_water_mark:
            return stop_price, high_water_mark
        candidate = observed_price * (Decimal("1") - Decimal(str(trail_pct)) / Decimal("100"))
        return (candidate if candidate > stop_price else stop_price), observed_price

    def _ratchet_trailing_stop(self, stop: ArmedHardStop) -> None:
        """Raise a trailing stop toward ``trail_pct`` below the high-water-mark of the
        freshest BID. No-op for fixed stops (trail_pct=0).

        BID-ONLY (deliberate): the breach trigger fires on the bid, so the ratchet
        must track the bid too. Tracking the *last* trade instead would, on a
        wide-spread thin microcap (spread > trail_pct), ratchet the stop up off a
        high last print and then immediately trigger on a much-lower bid — running
        the trail tighter than the backtested TRAIL-8% width (the TRAIL-3%-overfit
        failure mode already ruled out). Keeping ratchet and trigger on the same
        reference preserves the robust 8% room that made TRAIL-8% win."""
        if stop.trail_pct <= 0:
            return
        quote = self._latest_quotes_by_symbol.get(stop.symbol)
        if quote is None or quote.get("bid") is None:
            return
        received_at = quote.get("received_at")
        if not isinstance(received_at, datetime):
            return
        if (utcnow() - received_at).total_seconds() * 1000 > max(0, stop.quote_max_age_ms):
            return
        hwm = stop.high_water_mark if stop.high_water_mark is not None else stop.entry_price
        prev_stop_price, prev_hwm = stop.stop_price, stop.high_water_mark
        stop.stop_price, stop.high_water_mark = self._ratcheted_trailing_stop(
            stop.stop_price, hwm, Decimal(str(quote["bid"])), stop.trail_pct
        )
        # F2: persist the ratcheted level (full fidelity) only when it actually moved.
        if self._armed_stop_persistence_enabled and (
            stop.stop_price != prev_stop_price or stop.high_water_mark != prev_hwm
        ):
            self._armed_stop_dirty.add(
                self._hard_stop_key(stop.strategy_code, stop.broker_account_name, stop.symbol)
            )

    def _is_hard_stop_trigger_throttled(self, stop: ArmedHardStop) -> bool:
        if stop.last_trigger_attempt_at is None:
            return False
        return (utcnow() - stop.last_trigger_attempt_at).total_seconds() < 0.25

    def _window_flatten_due(self, now: datetime | None = None) -> bool:
        """True once the ET clock has passed the flatten time on a real trading day.

        Weekday/holiday handling comes from the LIVE session helper, not a wall-clock guess.
        Flatten time is config (`orb_window_flatten_hour_et`/`_minute_et`, default 10:00 ET). At
        10:00 the half-day (13:00 close) problem is MOOT -- 10:00 is well within RTH on both full and
        half days, so the flatten always fires into a live, liquid market.
        """
        et = (now or datetime.now(UTC)).astimezone(SESSION_TZ)
        if et.weekday() >= 5:
            return False
        hh = int(getattr(self.settings, "orb_window_flatten_hour_et", 10))
        mm = int(getattr(self.settings, "orb_window_flatten_minute_et", 0))
        return (et.hour, et.minute) >= (hh, mm)

    async def _window_flatten_armed_stops(self) -> None:
        """P0.6 — close OMS-owned positions before the session ends, so nothing rides overnight.

        WHY THIS EXISTS: an ORB position held past the close has NO protection at all. The native
        broker STOP is `time_in_force=day` AND Webull stops are RTH-only (no native guard has ever
        terminated later than 15:16 ET in production), so it is gone by 16:00. The OMS software stop
        cannot fill outside the 7:00-20:00 fillable gate. Between the close and 07:00 the belt cannot
        fill and the suspenders have expired. It has happened three times in three weeks (ERNA 07-15,
        AGEN + LGPS 07-13) and every one was closed by hand.

        WHY 10:00 (config `orb_window_flatten_*`): ORB trades the 09:30-10:00 opening range and is
        BLANK after 10:00 BY DESIGN -- a deliberate strategy cap (operator-owned), not merely a
        protection window. It closes the overnight-naked hole for free: nothing rides past 10:00, so
        the expired-native-stop / illiquid-16:00-20:00 gap is never reached. (An earlier draft argued
        15:55 to beat the RTH-only native stop; the data killed it -- no ORB trade ever lasted >5 min,
        and holding past 10:00 = a broken exit, not a winner.)

        WHY OFF `_armed_hard_stops`: that registry is OMS-owned BY CONSTRUCTION (a stop arms only
        from a fill on an intent the OMS placed), so the scoping invariant holds for free -- a manual
        holding is invisible here and can never be flattened. F2's durable mirror means a restart at
        15:54 does not lose the flatten.

        WHY NOT `_trigger_hard_stop`: during RTH it DEFERS to an active native guard and returns
        without closing (correct for a trail breach -- the broker-side stop handles it). For an EOD
        flatten that deferral is exactly wrong: the guard is about to expire, which is the point.

        The guard cancel is NOT done here: `process_trade_intent` already calls
        `_cancel_native_stop_guard_before_sell` for any sell/close that is not itself a guard. A
        flatten that skipped it would be reverse-rejected (ORDER_NOT_SUPPORT_REVERSE_OPTION -- the
        ERNA/NXTC class) and, per P0.1, would fail SILENTLY on the watchdog-refresh path.
        """
        if not bool(getattr(self.settings, "orb_window_flatten_enabled", False)):
            return
        if not self._window_flatten_due():
            return
        session_day = datetime.now(UTC).astimezone(SESSION_TZ).strftime("%Y-%m-%d")
        enabled = {
            c.strip() for c in
            str(getattr(self.settings, "orb_window_flatten_strategies", "orb") or "").split(",")
            if c.strip()
        }
        for stop in list(self._armed_hard_stops.values()):
            if stop.strategy_code not in enabled or stop.quantity <= 0:
                continue
            key = (session_day, stop.broker_account_name, stop.symbol)
            if key in self._window_flattened:
                continue
            self._window_flattened.add(key)   # claim BEFORE the await: one close per symbol per day
            _q = self._latest_quotes_by_symbol.get(stop.symbol) or {}
            try:
                bid = Decimal(str(_q.get("bid"))) if _q.get("bid") else None
            except (TypeError, ValueError, ArithmeticError):
                bid = None
            self.logger.info(
                "[ORB-WINDOW-FLATTEN] %s %s qty=%s -> closing before the session ends "
                "(native stop is RTH-only and expires at the close)",
                stop.strategy_code, stop.symbol, stop.quantity,
            )
            event = TradeIntentEvent(
                source_service=SERVICE_NAME,
                payload=TradeIntentPayload(
                    strategy_code=stop.strategy_code,
                    broker_account_name=stop.broker_account_name,
                    symbol=stop.symbol,
                    side="sell",
                    quantity=stop.quantity,
                    intent_type="close",
                    reason="WINDOW_FLATTEN",
                    metadata=self._build_hard_stop_metadata(
                        stop=stop,
                        trigger_price=bid if bid is not None else stop.stop_price,
                        trigger_source="window_flatten",
                    ),
                ),
            )
            try:
                order_events = await self.process_trade_intent(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.error(
                    "[ORB-WINDOW-FLATTEN] %s %s CLOSE RAISED (%s) — POSITION MAY RIDE OVERNIGHT "
                    "UNPROTECTED. Operator action required.",
                    stop.strategy_code, stop.symbol, exc,
                )
                continue
            placed = any(
                item.payload.status in {"accepted", "submitted", "partially_filled", "filled"}
                for item in order_events
            )
            if placed:
                self.logger.info(
                    "[ORB-WINDOW-FLATTEN] %s %s close placed", stop.strategy_code, stop.symbol
                )
            elif any(item.payload.reason in self.NO_POSITION_REASONS for item in order_events):
                self.logger.info(
                    "[ORB-WINDOW-FLATTEN] %s %s already flat at the broker — nothing to do",
                    stop.strategy_code, stop.symbol,
                )
            else:
                # LOUD by design. A silently-failed flatten IS the naked-overnight state, and the
                # only control that has ever caught it is the operator noticing.
                self.logger.error(
                    "[ORB-WINDOW-FLATTEN] %s %s CLOSE DID NOT PLACE (%s) — POSITION MAY RIDE "
                    "OVERNIGHT UNPROTECTED. Operator action required.",
                    stop.strategy_code, stop.symbol,
                    ",".join(sorted({str(i.payload.reason) for i in order_events})) or "no reason",
                )
                self._window_flattened.discard(key)   # allow a retry on the next 5s tick

    def _session_day_et(self, now: datetime | None = None) -> str:
        """The ET calendar day (YYYY-MM-DD) used to scope per-day idempotency latches. Mirrors the
        `_window_flatten_armed_stops` key so a day-scoped set self-expires next session with no
        explicit reset (an old day's keys simply never match today's)."""
        return (now or datetime.now(UTC)).astimezone(SESSION_TZ).strftime("%Y-%m-%d")

    def _v2_rth_edge_bracket_due(self, now: datetime | None = None) -> bool:
        """True once the ET clock reaches the RTH edge (default 09:30) on a weekday.

        ⛔ This time is the BROKER'S, not ours. Schwab rejects a STOP leg in the AM session
        outright, so a bracket cannot exist before the open no matter how we schedule it."""
        et = (now or datetime.now(UTC)).astimezone(SESSION_TZ)
        if et.weekday() >= 5:
            return False
        hh = int(getattr(self.settings, "oms_v2_rth_edge_bracket_hour_et", 9))
        mm = int(getattr(self.settings, "oms_v2_rth_edge_bracket_minute_et", 30))
        return (et.hour, et.minute) >= (hh, mm)

    async def _v2_rth_edge_bracket(self) -> None:
        """#646 Part 1 — bracket a still-held PRE-MARKET entry the instant the broker allows it.

        THE HOLE THIS CLOSES. `_apply_v2_oco_bracket_entry` decorates a BUY-open intent, so a
        bracket can only ever be attached to an entry we are placing. A position entered at 07:30
        and still held at 09:30 is therefore never bracketed for its entire life — nothing in the
        OMS revisits it. It rode the software ladder all day, which is the ladder that produced
        KUST (−5.17% on a right signal, nine cancels against a bid that never dropped below the
        limit).

        ⭐ SCOPE IS PRE-MARKET ENTRIES ONLY (`entry_time` before today's edge). An RTH entry gets
        its bracket from the entry path exactly as it does today; this sweep must not touch it.
        That is acceptance criterion A5 — a pre-market fix may not perturb the flow that works.

        ⭐ ORDERING IS FREE, AND DELIBERATELY SO. We only PLACE the OCO here; we never stand the
        software exit down. The stand-down is driven by `_refresh_native_oco_armed_state`, which
        activates only once the BROKER confirms both legs WORKING. So the software exit keeps
        protecting the position until the bracket is provably live, and if placement fails the
        position simply keeps the exit it already had. There is no unprotected gap by
        construction, and no new ordering logic to get wrong. [[feedback_has_the_other_bot_solved_this]]

        NOT claim-once. The EOD transition claims its key before acting because it only releases a
        latch; this one places an order against an unprotected position, so a transient broker
        error must not silently forfeit the whole day. Retries are rate-limited (60s) and capped
        (`oms_v2_rth_edge_bracket_max_attempts`), then latched with an ERROR — a position we could
        not bracket is a thing a human needs told about, not a line in a debug log."""
        if not bool(getattr(self.settings, "oms_v2_rth_edge_bracket_enabled", False)):
            return
        if not self._v2_rth_edge_bracket_due():
            return
        adapter = getattr(self, "broker_adapter", None)
        fetch_armed = getattr(adapter, "fetch_armed_native_oco_symbols", None)
        if fetch_armed is None:
            # No broker truth available => we cannot tell a bracketed position from an
            # unbracketed one. Emitting blind could double-bracket a position (two OCO pairs
            # reserving the same shares = the E5 oversell). Do nothing.
            return

        now = datetime.now(UTC)
        session_day = self._session_day_et(now)
        edge = now.astimezone(SESSION_TZ).replace(
            hour=int(getattr(self.settings, "oms_v2_rth_edge_bracket_hour_et", 9)),
            minute=int(getattr(self.settings, "oms_v2_rth_edge_bracket_minute_et", 30)),
            second=0, microsecond=0,
        )
        max_attempts = int(getattr(self.settings, "oms_v2_rth_edge_bracket_max_attempts", 3))

        by_account: dict[str, list[str]] = {}
        for acct, symbol in list(self._managed_v2_symbols):
            # ⛔ The day latch must NOT filter out a symbol whose bracket has since stood down:
            # doing so made the Part 3 re-arm unreachable for any position already handled at the
            # open (caught by test_a_re_arm_gets_a_FRESH_attempt_budget). A stand-down is a new
            # event, so it re-opens eligibility.
            if (session_day, acct, symbol) in self._v2_rth_edge_bracket_done and not (
                self._v2_stand_down_rearm_due(acct, symbol, now=now)
            ):
                continue
            by_account.setdefault(acct, []).append(symbol)
        if not by_account:
            return

        for acct, symbols in by_account.items():
            try:
                already_bracketed = set(await fetch_armed(acct, symbols))
            except Exception:
                # FAIL CLOSED on the read: an unreadable broker means we cannot prove a position
                # is unbracketed, and a double bracket is worse than a late one.
                self.logger.warning(
                    "[OMS-V2-RTH-EDGE-BRACKET] %s could not read armed OCO state; skipping this "
                    "pass (will retry on the next sync)", acct,
                )
                continue
            for symbol in symbols:
                key = (session_day, acct, symbol)
                # #646 Part 3 -- THE STAND-DOWN-CLEAR CONSTRAINT. A bracket that resolved or stood
                # down on a STILL-HELD position must not hand the exit back to the bare timer
                # ladder. NVVE 2026-07-23 proves that path is real, not theoretical: 11 cancelled
                # sells on a BRACKETED entry.
                rearm = self._v2_stand_down_rearm_due(acct, symbol, now=now)
                if rearm and key in self._v2_rth_edge_bracket_done:
                    # A genuinely NEW event, not a retry of the morning's arm. Give it a fresh
                    # budget -- otherwise one position that used up its attempts at 09:30 could
                    # never be re-armed for the rest of the session.
                    self._v2_rth_edge_bracket_done.discard(key)
                    self._v2_rth_edge_bracket_attempts.pop(key, None)
                    self._v2_rth_edge_bracket_last_try.pop(key, None)
                if symbol in already_bracketed:
                    # Already protected at the broker -- nothing owed. Latch so we stop asking.
                    self._v2_rth_edge_bracket_done.add(key)
                    continue
                last = self._v2_rth_edge_bracket_last_try.get(key)
                if last is not None and (now - last).total_seconds() < 60:
                    continue
                attempts = self._v2_rth_edge_bracket_attempts.get(key, 0)
                if attempts >= max_attempts:
                    if key not in self._v2_rth_edge_bracket_done:
                        self._v2_rth_edge_bracket_done.add(key)
                        self.logger.error(
                            "[OMS-V2-RTH-EDGE-BRACKET] %s %s GAVE UP after %d attempts — the "
                            "position is still held and is running on the SOFTWARE ladder for the "
                            "rest of the session. It is not naked (the P0a-held exit still owns "
                            "it), but it has no broker-side protection.",
                            acct, symbol, attempts,
                        )
                    continue
                self._v2_rth_edge_bracket_last_try[key] = now
                self._v2_rth_edge_bracket_attempts[key] = attempts + 1
                try:
                    await self._emit_v2_rth_edge_bracket(
                        acct=acct, symbol=symbol, edge_et=edge, rearm=rearm
                    )
                except Exception:
                    self.logger.exception(
                        "[OMS-V2-RTH-EDGE-BRACKET] %s %s attempt %d failed; the software exit is "
                        "untouched and still owns the position",
                        acct, symbol, attempts + 1,
                    )
                else:
                    self._v2_rth_edge_bracket_done.add(key)

    def _v2_stand_down_rearm_due(self, acct: str, symbol: str, *, now: datetime) -> bool:
        """True when a bracket CLEARED on a position we still hold — the re-arm case (#646 Part 3).

        ⛔⭐ THE GATE IS THE RESOLUTION GRACE, AND IT IS LOAD-BEARING. The COMMON reason a bracket
        clears is that a leg FILLED and the position is closing; the OMS position state lags that
        fill by tens of seconds (Schwab's fill -> positions propagation runs to ~6 min). Re-arming
        into that window would place a fresh pair of sells against a position about to be flat —
        an oversell, the exact E5 shape the bracket exists to eliminate.

        `_native_oco_resolving` records when the bracket went away, and the existing 90s grace is
        precisely "long enough for a resolving fill to reconcile". So: cleared, grace elapsed, AND
        the symbol is STILL in the managed set => it did NOT resolve by a fill, it stood down on a
        position we are still carrying. That is NVVE, and that is the only case we re-arm.

        ⚠️ This does NOT cover a stand-down while the exit is NOT marketable. P0a's hold engages
        only while `limit <= bid`, so that case still reaches the plain refresh. It needs an
        operator decision (a one-shot reprice-to-bid changes exit PRICING), and is deliberately
        left unbuilt rather than invented here. See #646 §7."""
        if not bool(getattr(self.settings, "oms_v2_stand_down_clear_rearm_enabled", False)):
            return False
        cleared_at = getattr(self, "_native_oco_resolving", {}).get((acct, symbol))
        if cleared_at is None:
            return False
        grace = float(getattr(self.settings, "oms_native_oco_resolve_grace_seconds", 90))
        return (now - cleared_at).total_seconds() > grace

    async def _emit_v2_rth_edge_bracket(
        self, *, acct: str, symbol: str, edge_et: datetime, rearm: bool = False
    ) -> None:
        """Place the exit-only OCO for one still-held pre-market position. Raises on failure so the
        caller can count the attempt and leave the software exit in charge."""
        events: list = []
        with self.session_factory() as session:
            row = self.store.get_open_managed_position(
                session, broker_account_name=acct, symbol=symbol
            )
            if row is None:
                return
            entry_time = row.entry_time
            if entry_time is not None and entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=UTC)
            if not rearm and entry_time is not None and entry_time >= edge_et.astimezone(UTC):
                # An RTH entry already gets its bracket from the entry path. Touching it here would
                # perturb the flow that works -- acceptance criterion A5.
                #
                # ⭐ A RE-ARM IS THE EXCEPTION, and deliberately so: NVVE was an RTH-entered,
                # properly-bracketed position whose bracket then stood down. Excluding RTH entries
                # from the re-arm would leave exactly the case the constraint exists to cover.
                return
            quantity = int(row.current_quantity)
            entry = float(row.entry_price)
            if quantity <= 0 or entry <= 0:
                return
            strategy = session.scalar(select(Strategy).where(Strategy.code == row.strategy_code))
            broker_account = session.scalar(
                select(BrokerAccount).where(BrokerAccount.name == row.broker_account_name)
            )
            if strategy is None or broker_account is None:
                return
            # ⛔ SHARES ALREADY RESERVED => DO NOT PLACE. A working software exit reserves the
            # position, so adding an OCO pair on top is two sets of sells against one holding --
            # Schwab answers that with "This order may result in an oversold position" (seen in
            # Probe P), and it is the E5 oversell shape besides. Defer; the caller retries. This
            # returns WITHOUT raising so a deferral does not burn one of the capped attempts.
            if self.store.get_open_exit_reserved_quantity(
                session,
                broker_account_id=broker_account.id,
                symbol=symbol,
                include_native_stop_guard=False,
            ) > 0:
                self.logger.info(
                    "[OMS-V2-RTH-EDGE-BRACKET] %s %s deferred — a software exit is working and has "
                    "the shares reserved; will arm once it clears", acct, symbol,
                )
                return
            # Same geometry the software ladder would have used: the bracket RELOCATES the exit to
            # the broker, it does not change it.
            target = _schwab_round(entry * (1.0 + self._cw_target_pct / 100.0))
            protect = _schwab_round(entry * (1.0 - self._cw_stop_pct / 100.0))
            metadata = {
                "exit_only_oco": "true",
                "session": "NORMAL",
                "bracket_target_price": target,
                "bracket_stop_price": protect,
                "oms_v2_rth_edge_bracket": "true",
                "entry_price": f"{entry:.4f}",
                "time_in_force": "day",
            }
            event = TradeIntentEvent(
                source_service=SERVICE_NAME,
                payload=TradeIntentPayload(
                    strategy_code=row.strategy_code,
                    broker_account_name=row.broker_account_name,   # <-- THE SCOPING INVARIANT
                    symbol=row.symbol,
                    side="sell",
                    quantity=Decimal(str(quantity)),
                    intent_type="close",
                    reason="oms_v2_rth_edge_bracket",
                    metadata=dict(metadata),
                ),
            )
            intent = self.store.create_trade_intent(
                session, strategy=strategy, broker_account=broker_account, event=event
            )
            self._record_internal_risk_pass(
                session, intent=intent, strategy=strategy, broker_account=broker_account,
                metadata=dict(metadata), reason="oms_v2_rth_edge_bracket",
            )
            request = OrderRequest(
                client_order_id=self._build_client_order_id(event),
                broker_account_name=row.broker_account_name,
                strategy_code=row.strategy_code,
                symbol=row.symbol,
                side="sell",
                intent_type="close",
                quantity=Decimal(str(quantity)),
                reason="oms_v2_rth_edge_bracket",
                metadata=dict(metadata),
                order_type="limit",
                time_in_force="day",
            )
            reports = await self.broker_adapter.submit_order(request)
            events = await self._record_order_reports(
                session=session, intent=intent, strategy_id=strategy.id,
                broker_account_id=broker_account.id, intent_event=event,
                request=request, reports=reports,
            )
            if any(str(getattr(ev.payload, "status", "")).lower() == "rejected" for ev in events):
                session.commit()
                raise RuntimeError(
                    f"exit-only OCO rejected for {symbol} — software exit still owns the position"
                )
            self.logger.info(
                "[%s] %s %s ARMED qty=%d entry=%.4f -> OCO[target=%s stop=%s] (%s)",
                "OMS-V2-STAND-DOWN-REARM" if rearm else "OMS-V2-RTH-EDGE-BRACKET",
                acct, symbol, quantity, entry, target, protect,
                "bracket stood down on a still-held position; re-armed instead of falling back to "
                "the bare timer ladder (NVVE 2026-07-23)" if rearm else
                "pre-market entry at %s ET finally has broker-side protection" % (
                    entry_time.astimezone(SESSION_TZ).strftime("%H:%M:%S") if entry_time else "?"),
            )
            session.commit()
        for ev in events:
            await self._publish_order_event(ev)

    def _latest_filled_entry_order_id(self, session, acct: str, symbol: str) -> str | None:
        """The most recent FILLED buy's broker order id for this position.

        ⛔ `oms_managed_positions` carries no entry broker order id, which is why the first version
        swept the account's whole order list. It is derivable exactly from `broker_orders`, and
        having it lets the confirm read one order TREE instead of a paginated list.
        """
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == acct))
        if account is None:
            return None
        row = session.scalars(
            select(BrokerOrder)
            .where(
                BrokerOrder.broker_account_id == account.id,
                BrokerOrder.symbol == symbol,
                BrokerOrder.side == "buy",
                BrokerOrder.status == "filled",
            )
            .order_by(BrokerOrder.submitted_at.desc())
            .limit(1)
        ).first()
        oid = str(getattr(row, "broker_order_id", "") or "") if row is not None else ""
        return oid or None

    def _v2_eod_cancel_reexit_due(self, now: datetime | None = None) -> bool:
        """True only INSIDE the cancel-and-reexit window (default 16:01-16:15 ET) on a weekday."""
        et = (now or datetime.now(UTC)).astimezone(SESSION_TZ)
        if et.weekday() >= 5:
            return False
        hh = int(getattr(self.settings, "oms_v2_eod_cancel_reexit_hour_et", 16))
        mm = int(getattr(self.settings, "oms_v2_eod_cancel_reexit_minute_et", 1))
        end = int(getattr(self.settings, "oms_v2_eod_cancel_reexit_window_minutes", 14))
        # ⛔⭐ CLOSED WINDOW, NOT AN OPEN-ENDED ">=". The first version stayed due from 16:01 until
        # midnight, so a position that became managed at 19:30 -- after the 19:55 flatten had begun
        # to matter -- would have had its legs cancelled and a PM exit placed hours out of context.
        # This is a 16:01 handoff; outside those minutes it must be INERT.
        start_min = hh * 60 + mm
        now_min = et.hour * 60 + et.minute
        return start_min <= now_min < (start_min + max(1, end))

    async def _v2_eod_cancel_and_reexit(self) -> None:
        """16:01 ET: cancel our OWN working SELL legs, CONFIRM zero, then place a PM limit exit.

        WHY THIS EXISTS. An RTH bracket's legs are session=NORMAL/duration=DAY, so at the bell they
        stop being able to FILL. Whether they also stop RESERVING is the question board row OVSD1
        parks. This path removes the ambiguity instead of waiting it out: cancel what we placed,
        make the broker confirm it is gone, then own the exit in the PM session.

        ⛔ ONCE PER POSITION PER DAY, NOT ONCE PER TICK. The claim is taken BEFORE the first await.
        ⛔ NO RETRY ANYWHERE. Every failure path ends the attempt for that position for the day.
        ⛔ A DELETE REFUSAL STOPS EVERYTHING: no further legs, no PM exit, legs left working, and
           the existing path continues to own the position exactly as it does today.
        ⛔ THE PM EXIT NEVER GOES OUT ON AN INFERRED RELEASE. It requires an INDEPENDENT re-read
           reporting zero working SELL legs. "We sent a cancel" is not evidence the shares are free.
        ⛔ THE PM EXIT GOES THROUGH `_emit_v2_exit_on_loop`, never a direct POST, so it lands in
           `broker_orders` and `get_open_exit_reserved_quantity` sees it -- which is the ONLY thing
           stopping the 19:55 flatten from placing a SECOND sell against the same shares.

        ⚠ THE 16:01-16:05 UNPROTECTED WINDOW, NAMED RATHER THAN HIDDEN. Schwab's PM session does
        not open until ~16:05, so between the cancel and the first fillable moment the position has
        nothing working. That is a DELIBERATE trade of a bounded four-minute gap against the
        open-ended one it replaces. On one share it is cents; on a real position it is not, and the
        operator has seen it named on the board row.

        ⚠ INHERITED LIMIT (OVSD1, parked, NOT fixed here): the harvest walk cannot see a childless
        OCO wrapper, so "no working legs" could in principle be a blind reading rather than a clear
        one. Flag-gated OFF until that is settled or the harness answers it.
        """
        if not bool(getattr(self.settings, "oms_v2_eod_cancel_reexit_enabled", False)):
            return
        if not self._v2_eod_cancel_reexit_due():
            return
        session_day = self._session_day_et()
        close_on_fill = bool(getattr(self.settings, "oms_v2_exit_close_on_fill_enabled", True))
        considered = 0
        for acct, symbol in list(self._managed_v2_symbols):
            key = (session_day, acct, symbol)
            if key in self._v2_eod_cancel_reexit_done:
                continue
            self._v2_eod_cancel_reexit_done.add(key)  # claim FIRST -- before any await
            considered += 1
            try:
                await self._v2_eod_cancel_and_reexit_one(acct, symbol, close_on_fill)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one position must never break the sweep
                self.logger.exception(
                    "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=ERROR — attempt abandoned for today "
                    "(no retry by design). The position keeps whatever protection it still has.",
                    acct, symbol,
                )
        if considered == 0 and session_day not in self._v2_eod_cancel_reexit_summarised:
            self._v2_eod_cancel_reexit_summarised.add(session_day)
            # ⛔ UNEXERCISED IS NOT A PASS. A day with no position open at 16:01 tested nothing.
            self.logger.info(
                "[OMS-V2-EOD-CANCEL-REEXIT] day=%s considered=0 outcome=UNEXERCISED — no v2 "
                "position was still open at 16:01, so this path did not run. NOT a pass: it is an "
                "untested day against a denominator of zero.",
                session_day,
            )

    async def _v2_eod_cancel_and_reexit_one(
        self, acct: str, symbol: str, close_on_fill: bool
    ) -> None:
        """One position, one attempt. Every exit from this method is a terminal outcome for today."""
        # --- 1. HARVEST -------------------------------------------------------------------
        entry_id = await self._run_db(
            lambda session: self._latest_filled_entry_order_id(session, acct, symbol),
            commit=False,
        )
        if not entry_id:
            self.logger.error(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=NO_ENTRY_ORDER_ID — cannot address this "
                "position's own order tree, so nothing was cancelled and NO PM exit was placed.",
                acct, symbol,
            )
            return
        try:
            before = await self.broker_adapter.fetch_exit_legs_for_entry(acct, entry_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - an unreadable broker is NOT a clear broker
            self.logger.error(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=UNANSWERABLE_HARVEST entry=%s — could not "
                "read the legs, so nothing was cancelled and NO PM exit was placed.",
                acct, symbol, entry_id, exc_info=True,
            )
            return
        if before.get("unsafe"):
            self.logger.error(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=UNSAFE_LEG_STATE entry=%s — a partial fill "
                "or an unrecognised leg status. Refusing to act on a state we do not understand.",
                acct, symbol, entry_id,
            )
            return
        if before.get("filled"):
            self.logger.error(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=RESOLVED_BY_FILL entry=%s — an OCO child "
                "already FILLED; the position is closed. NO PM exit.", acct, symbol, entry_id,
            )
            await self._close_resolved_oco_managed_row(acct, symbol)
            return
        leg_ids = list(before.get("working") or [])

        # ⛔ An unsupported venue returns {} from routing, which is indistinguishable HERE from
        # "no legs". Both are handled identically and SAFELY: we only ever proceed to the PM exit
        # after an independent confirm read, and on a venue that cannot answer that read is empty
        # for the same reason -- so the guard below is what actually protects us, not this branch.
        cancelled_by_us = False
        if leg_ids:
            try:
                result = await self.broker_adapter.cancel_exit_leg_ids(acct, leg_ids)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.logger.error(
                    "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=UNANSWERABLE_CANCEL legs=%d — the "
                    "cancel call itself failed. NO PM exit. Legs may still be working.",
                    acct, symbol, len(leg_ids), exc_info=True,
                )
                return
            refused = result.get("refused")
            if refused:
                # ⛔ FAIL SAFE ON ATTEMPT ONE. Verbatim body, no retry, no PM order.
                # ⛔⭐⭐ BUT A PARTIAL REFUSAL IS NOT "NOTHING HAPPENED". If leg 1 was cancelled and
                # leg 2 refused, the OCO pair is now HALF a pair -- the stop may be gone while the
                # target remains. The first version logged and returned, leaving the position with
                # DEGRADED protection and calling it fail-safe. It is not: we broke it.
                partly = list(result.get("cancelled") or [])
                self.logger.error(
                    "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=REFUSED order_id=%s http=%s "
                    "cancelled=%d untouched=%d body=%r — STOPPING. No PM exit was placed and the "
                    "remaining legs were NOT touched; the existing path still owns this position.",
                    acct, symbol, refused.get("order_id"), refused.get("status_code"),
                    len(result.get("cancelled") or []), len(result.get("untouched") or []),
                    refused.get("body"),
                )
                if partly:
                    self.logger.error(
                        "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=PARTIAL_CANCEL cancelled=%d — the "
                        "OCO pair is now INCOMPLETE (one side removed, one refused). Protection is "
                        "DEGRADED, not intact.", acct, symbol, len(partly),
                    )
                    await self._v2_eod_restore_protection(acct, symbol, why="partial_cancel")
                return
            cancelled_by_us = True
            self.logger.info(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=CANCELLED legs=%d ids=%s",
                acct, symbol, len(leg_ids), ",".join(leg_ids),
            )
        else:
            self.logger.info(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=NOTHING_TO_CANCEL — the broker reported "
                "no working SELL legs at 16:01 (the DAY legs expired at the bell, as on DAIC 08-25 "
                "and CELU 08-27). Nothing was removed.", acct, symbol,
            )

        # --- 2. CONFIRM ZERO -- independent re-read, never inferred -----------------------
        try:
            after = await self.broker_adapter.fetch_exit_legs_for_entry(acct, entry_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self.logger.error(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=UNANSWERABLE_CONFIRM cancelled_by_us=%s — "
                "could not confirm the legs are gone, so NO PM exit was placed.",
                acct, symbol, cancelled_by_us, exc_info=True,
            )
            if cancelled_by_us:
                await self._v2_eod_restore_protection(acct, symbol, why="confirm_unreadable")
            return
        if after.get("unsafe") or after.get("filled"):
            self.logger.error(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=CONFIRM_NOT_CLEAN unsafe=%s filled=%s — "
                "NO PM exit.", acct, symbol, after.get("unsafe"), after.get("filled"),
            )
            return
        remaining = list(after.get("working") or [])
        if remaining:
            self.logger.error(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=STILL_WORKING remaining=%d ids=%s — the "
                "shares are still reserved, so NO PM exit was placed.",
                acct, symbol, len(remaining), ",".join(remaining),
            )
            return

        # --- 3. PM EXIT through the NORMAL path ------------------------------------------
        placed = await self._v2_eod_place_pm_exit(acct, symbol, close_on_fill)
        if placed:
            self.logger.info(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=PM_EXIT_PLACED cancelled_by_us=%s — "
                "confirmed zero working legs, then placed a PM limit exit via the managed-exit "
                "path (visible to the 19:55 flatten's dedup).", acct, symbol, cancelled_by_us,
            )
            return

        # --- 4. RESTORE -- never leave a position with nothing after removing what it had --
        if not cancelled_by_us:
            self.logger.error(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=PM_EXIT_NOT_PLACED cancelled_by_us=False "
                "— nothing was removed by us, so there is nothing to restore. The position is as it "
                "was before this ran.", acct, symbol,
            )
            return
        await self._v2_eod_restore_protection(acct, symbol, why="pm_exit_not_placed")

    async def _v2_eod_restore_protection(self, acct: str, symbol: str, *, why: str) -> None:
        """Put the bracket back ONCE after we cancelled legs and then could not place the exit.

        ⛔ ONCE, NOT A LOOP. If the restore also fails we stop, mark it and page -- cycling here
        would be the same unbounded-retry shape this whole path is written to avoid.
        ⭐ Goes through `_emit_v2_rth_edge_bracket`, the existing exit-only OCO path, so the restored
        pair lands in `broker_orders` and the 19:55 flatten's dedup sees it -- the same constraint
        the PM exit inherits.
        """
        # ⛔⭐⭐ AFTER 16:00 THERE IS NO BRACKET TO RESTORE. Schwab REJECTS a STOP leg outside the
        # regular session -- "This order type is not available for this session", measured
        # 2026-08-04 by Probe P against an accepted session=NORMAL control, and
        # `_build_exit_only_oco_payload` refuses to construct one for exactly that reason. The
        # first version called that builder at 16:01, which would have placed nothing and
        # reported RESTORED. A restore that cannot work must not be attempted and must never be
        # logged as success. ⇒ Say so, page, and name the backstop that IS real.
        if not _is_regular_market_session():
            self.logger.error(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=RESTORE_IMPOSSIBLE why=%s — we cancelled "
                "the legs and could not place the PM exit, and a protective OCO CANNOT be re-armed "
                "outside RTH (Schwab rejects a STOP leg after the close). THE POSITION IS UNCOVERED "
                "until the 19:55 flatten, which retries until filled. Operator action may be "
                "required NOW; do not wait for 19:55.",
                acct, symbol, why,
            )
            return
        try:
            await self._emit_v2_rth_edge_bracket(
                acct=acct, symbol=symbol, edge_et=datetime.now(UTC), rearm=True
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self.logger.error(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=RESTORE_FAILED why=%s — we cancelled the "
                "legs, could not place the PM exit, AND could not put protection back. THE POSITION "
                "IS UNCOVERED; operator action required. Not retrying (once, by design).",
                acct, symbol, why, exc_info=True,
            )
            return
        self.logger.warning(
            "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=RESTORED why=%s — PM exit did not go out, so "
            "the protective pair was put back. The position is covered again.", acct, symbol, why,
        )

    async def _v2_eod_place_pm_exit(
        self, acct: str, symbol: str, close_on_fill: bool
    ) -> bool:
        """Place the PM limit exit via the managed-exit path. True only if an exit order is working.

        ⛔ Success is CONFIRMED BY READ-BACK, not assumed from the absence of an exception:
        `_emit_v2_exit_on_loop` returns None whether or not the broker took the order.
        """
        # ⛔⭐⭐ A CHILD MAY HAVE FILLED. "No WORKING legs" has two causes and only one of them is
        # safe: the legs lapsed (nothing sold), or a leg FILLED and the position is already gone.
        # Both read identically to a working-leg harvest. Selling on the second is a naked short.
        # `fetch_oco_resolved_by_fill_symbols` is the existing, production-proven read for exactly
        # this distinction, and it is recency-bounded so a stale fill from an earlier bracket on
        # the same symbol cannot false-positive.
        resolved = getattr(self.broker_adapter, "fetch_oco_resolved_by_fill_symbols", None)
        if resolved is not None:
            try:
                if symbol.upper() in {str(x).upper() for x in await resolved(acct, [symbol])}:
                    self.logger.error(
                        "[OMS-V2-EOD-CANCEL-REEXIT] %s %s outcome=RESOLVED_BY_FILL — an OCO child "
                        "already FILLED, so the position is closed. NO PM exit placed.",
                        acct, symbol,
                    )
                    await self._close_resolved_oco_managed_row(acct, symbol)
                    return True  # nothing to place and nothing to restore
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - an unreadable broker is not a flat broker
                self.logger.error(
                    "[OMS-V2-EOD-CANCEL-REEXIT] %s %s could not check resolved-by-fill — refusing "
                    "to place a PM exit on an unverified position.", acct, symbol, exc_info=True,
                )
                return False
        snapshot = await self._run_db(
            lambda session: self._read_v2_managed_snapshot(session, acct, symbol, close_on_fill),
            commit=False,
        )
        if snapshot is None:
            self._managed_v2_symbols.discard((acct, symbol))
            return False
        if snapshot.dedup_active:
            return True  # an exit already works -- nothing to place, nothing to restore
        quote = self._latest_quotes_by_symbol.get(symbol) or {}
        bid = float(quote.get("bid") or 0.0)
        if bid <= 0.0:
            self.logger.error(
                "[OMS-V2-EOD-CANCEL-REEXIT] %s %s NO BID at 16:01 — cannot price a PM limit exit.",
                acct, symbol,
            )
            return False
        position = self._hydrate_v2_position(snapshot)
        position.update_price(bid)
        await self._emit_v2_exit_on_loop(
            acct, symbol, position, snapshot.entry_price,
            kind="EOD_CANCEL_REEXIT", reference_price=bid, reason="V2_EOD_CANCEL_REEXIT",
            bid=bid, close_on_fill=close_on_fill,
        )
        after = await self._run_db(
            lambda session: self._read_v2_managed_snapshot(session, acct, symbol, close_on_fill),
            commit=False,
        )
        return bool(after is not None and after.dedup_active)

    def _v2_eod_oco_transition_due(self, now: datetime | None = None) -> bool:
        """True once the ET clock reaches the EOD OCO-transition time (default 16:00) on a weekday.

        HALF-DAY caveat (same as the ORB window-flatten / v2 overnight-flatten): there is no session
        calendar yet, so on an early-close day 16:00 is already post-close — harmless here (the RTH
        OCO has already expired and releasing the stand-down only hands the still-held position to the
        EH-limit ladder, which is exactly right), but keep the flag OFF on half-days until the calendar
        lands if you want the transition pinned to the real close."""
        et = (now or datetime.now(UTC)).astimezone(SESSION_TZ)
        if et.weekday() >= 5:
            return False
        hh = int(getattr(self.settings, "oms_v2_eod_oco_transition_hour_et", 16))
        mm = int(getattr(self.settings, "oms_v2_eod_oco_transition_minute_et", 0))
        return (et.hour, et.minute) >= (hh, mm)

    async def _v2_eod_oco_transition(self) -> None:
        """Phase A EOD OCO cleanup (docs/premarket-eod-exit-design.md; decision A = KEEP MANAGING).

        At 16:00 ET the native OCO exit legs expire with the RTH close — they carry session=NORMAL
        (RTH-only) + duration=DAY (schwab.py `_build_bracket_payload`), so after the close they can no
        longer fill on EITHER broker. For every OMS-managed v2 position still open, RELEASE the
        native-OCO stand-down for the rest of the day so `_evaluate_v2_managed_exit` resumes and runs
        the software +2%/−5% ladder as EH-LIMIT exits (#390 MARKET->LIMIT+session routing; the OMS
        fillable gate keeps 16:00–20:00 fillable). The 19:55 `_v2_overnight_flatten` stays the backstop.

        WHY A LOCAL RELEASE, NOT A BROKER CANCEL: the OCO child legs are broker-created and never land
        in `broker_orders` (see `_refresh_native_oco_armed_state`), so there is no OMS order to cancel
        via the existing cancel path — and none is needed: a session=NORMAL DAY order cannot fill in EH,
        so nothing is lost by letting it lapse. Absent this method the ladder ALSO resumes on its own
        once the broker drops the expired legs from `fetch_armed_native_oco_symbols` (the resolve-by-fill
        docstring documents exactly this "timed out at the close -> software ladder manages" case), but
        that path waits on the broker sync noticing the expiry PLUS the 90s resolution grace, and can
        flip-flop if a stale read still reports the expiring OCO as armed. This makes the 16:00 handover
        IMMEDIATE and deterministic: the day-scoped latch short-circuits `_native_oco_stand_down_active`
        to False, so a re-arm read cannot re-defer the ladder.

        NOT a liquidation (decision A keeps +2%/−5% running), NOT a broker mutation, and it never touches
        the 19:55 backstop. Idempotent per (session_day, account, symbol). Flag-gated OFF => the latch set
        stays empty and every consulting predicate is byte-identical."""
        if not bool(getattr(self.settings, "oms_v2_eod_oco_transition_enabled", False)):
            return
        if not self._v2_eod_oco_transition_due():
            return
        session_day = self._session_day_et()
        armed = getattr(self, "_native_oco_armed_confirmed_at", {})
        resolving = getattr(self, "_native_oco_resolving", {})
        for acct, symbol in list(self._managed_v2_symbols):
            key = (session_day, acct, symbol)
            if key in self._v2_eod_oco_transitioned:
                continue
            self._v2_eod_oco_transitioned.add(key)  # claim first: fire once per position per day
            # Drop any live broker-armed confirmation / resolution-grace entry so the ladder is not
            # deferred waiting for either to lapse. The stand-down short-circuit (keyed on this same
            # latch) is the durable guard; these pops just avoid stale log churn from the sync refresh.
            armed.pop((acct, symbol), None)
            resolving.pop((acct, symbol), None)
            self.logger.info(
                "[OMS-V2-EOD-OCO-TRANSITION] %s %s -> RTH OCO expired at %02d:%02d ET; released the "
                "native-OCO stand-down for the day; software +2%%/−5%% EH-limit ladder now owns the exit "
                "(19:55 overnight-flatten remains the backstop)",
                acct, symbol,
                int(getattr(self.settings, "oms_v2_eod_oco_transition_hour_et", 16)),
                int(getattr(self.settings, "oms_v2_eod_oco_transition_minute_et", 0)),
            )

    def _v2_overnight_flatten_due(self, now: datetime | None = None) -> bool:
        """True once the ET clock passes the v2 overnight-flatten time on a weekday. Same half-day
        caveat as the ORB window-flatten (keep the flag OFF on half-days until the session calendar
        lands — 19:55 is after a 13:00 close)."""
        et = (now or datetime.now(UTC)).astimezone(SESSION_TZ)
        if et.weekday() >= 5:
            return False
        hh = int(getattr(self.settings, "oms_v2_overnight_flatten_hour_et", 19))
        mm = int(getattr(self.settings, "oms_v2_overnight_flatten_minute_et", 55))
        return (et.hour, et.minute) >= (hh, mm)

    async def _v2_overnight_flatten(self) -> None:
        """Safety: close every OMS-managed v2 position at 19:55 ET so nothing rides past the 20:00
        fillable gate NAKED (v2 arms zero native stops; #464 fixed the false-flat deletion, NOT the
        clock). Drives off `_managed_v2_symbols` (OMS-owned => manual holdings invisible = scoping
        invariant). Full-qty close via the existing v2 exit primitive (LIMIT+session, EH-fillable — a
        market order won't fill in AH). A single close, not a resting stop => NOT the E5 oversell class.

        RETRY-UNTIL-FILLED — there is NO per-day claim (by design). A limit that expires unfilled
        (thin AH) leaves the position open with no working order, so the next 5s pass RE-EMITS; the
        flatten keeps trying until it fills or the 20:00 gate closes. Double-submit is prevented by
        `dedup_active` (a working exit order => skip) — the same guard the managed exit uses. A per-day
        claim would silently give up on the exact naked-overnight case this exists to prevent.

        ⚠️ KNOWN GAP (traced 2026-07-17, NOT yet fixed — design-first; do not "fix" this inline).
        `dedup_active` is a DB read, and it is BLIND for the duration of a submit. Flatten-vs-flatten
        is safe: the control loop awaits this method to completion, and the per-symbol loop awaits
        `_emit_v2_exit_on_loop` (which commits at the end of its own session), so the next pass always
        reads a committed order. But `await submit_order` YIELDS the event loop, and
        `_run_tick_consumer` is a SEPARATE task (see `_run_control_loop`'s docstring — the decoupling
        is deliberate, so a slow control loop cannot delay an exit decision). In that window the quote
        handler can reach `_evaluate_v2_managed_exit` (gated only by `_market_is_fillable()`, TRUE at
        19:55 since the gate is 7–20 ET) whose OWN dedup read cannot see this flatten's UNCOMMITTED
        order. => FLATTEN-vs-MANAGED-EXIT can double-submit. Window is measured: #459 found the
        post-submit marker postdates the broker fill stamp by median +1.4s, max +4.5s (30/30). The OMS
        holds ZERO locks — all safety is single-loop-thread, and this is the seam where that
        assumption breaks (two callers, shared state, one thread that yields). Narrow (needs the
        managed exit to also want to sell in that window) but it concentrates on exactly the case this
        exists for: a thin AH book with the bid gapping through −5%. Bounded, NOT naked: the second
        sell is rejected oversold — but that means the ONLY thing preventing a real double-sell here
        is the BROKER's oversold check, an external control we do not own.
        FIX SHAPE: a per-SUBMIT in-memory claim spanning BOTH callers — set before the await, released
        on the report. NOT a per-day claim: #478's claim was not wrong in kind, it was wrong in SCOPE
        (it protected a day when it needed to protect an await). The entry side already has exactly
        this pattern — `schwab_1m_v2.py::cw_v2_emit_claimed`.
        Flag-gated; OFF => never runs. HALF-DAY: 19:55 is after a 13:00 close so there is no session
        => the LOUD no-bid path fires, which is CORRECT (the position IS naked) — keep the flag ON."""
        if not bool(getattr(self.settings, "oms_v2_overnight_flatten_enabled", False)):
            return
        if not self._v2_overnight_flatten_due():
            return
        close_on_fill = bool(getattr(self.settings, "oms_v2_exit_close_on_fill_enabled", True))
        for acct, symbol in list(self._managed_v2_symbols):
            snapshot = await self._run_db(
                lambda session: self._read_v2_managed_snapshot(session, acct, symbol, close_on_fill),
                commit=False,
            )
            if snapshot is None:
                self._managed_v2_symbols.discard((acct, symbol))
                continue
            if snapshot.dedup_active:
                continue  # a close already works — no double-submit (re-emits when it expires)
            quote = self._latest_quotes_by_symbol.get(symbol) or {}
            bid = float(quote.get("bid") or 0.0)
            if bid <= 0.0:
                # No bid. Two VERY different situations wear the same face here, and the old code
                # treated both as the naked one:
                #   (a) we genuinely hold it and the AH book is empty  -> NAKED. Stay loud, retry.
                #   (b) the broker holds NOTHING and the row is a PHANTOM -> nothing to flatten.
                # Live 2026-07-27: two phantom QBTX rows (hand-closed earlier) produced 58 ERROR
                # lines in four minutes, every 15s, and cleared nothing — a human had to delete the
                # rows. The flatten cannot price a close for stock that does not exist, so it can
                # never make progress; it just pages forever and drowns the real signal.
                #
                # Distinguishing them needs no quote: ASK THE BROKER. `_broker_symbol_is_flat` is
                # the same positive-confirmation helper the reject-driven reconcile already uses to
                # delete protection — UNKNOWN and HELD both return False, and a flat read inside the
                # fresh-fill grace is refused (the 07-15 ERNA shape). So (a) is unchanged in every
                # respect, including a failed or rate-limited read.
                entry_at = _as_utc(getattr(snapshot, "entry_time", None))
                phantom = False
                try:
                    phantom = await self._broker_symbol_is_flat(acct, symbol, established_at=entry_at)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - an unreadable broker is NOT a flat broker
                    phantom = False
                if phantom:
                    await self._close_resolved_oco_managed_row(acct, symbol)
                    self.logger.info(
                        "[OMS-V2-OVERNIGHT-FLATTEN] %s %s qty=%s NO BID but the broker confirms "
                        "FLAT -> phantom row, reconciled away (nothing to flatten)",
                        acct, symbol, snapshot.current_quantity,
                    )
                    continue
                # Genuinely held with no bid: LOUD + retry next loop (never give up).
                self.logger.error(
                    "[OMS-V2-OVERNIGHT-FLATTEN] %s %s qty=%s NO BID — cannot place close before the "
                    "20:00 gate; retrying. Operator action may be required.",
                    acct, symbol, snapshot.current_quantity,
                )
                continue
            position = self._hydrate_v2_position(snapshot)
            position.update_price(bid)
            self.logger.info(
                "[OMS-V2-OVERNIGHT-FLATTEN] %s %s qty=%s -> closing before the 20:00 gate "
                "(no native stop; software fill impossible after 20:00)",
                acct, symbol, snapshot.current_quantity,
            )
            await self._emit_v2_exit_on_loop(
                acct, symbol, position, snapshot.entry_price,
                kind="OVERNIGHT_FLATTEN", reference_price=bid, reason="V2_OVERNIGHT_FLATTEN",
                bid=bid, close_on_fill=close_on_fill,
            )

    async def _trigger_hard_stop(
        self,
        stop: ArmedHardStop,
        *,
        trigger_price: Decimal,
        trigger_source: str,
    ) -> None:
        if _is_regular_market_session():
            try:
                has_native_guard = await self._has_active_native_stop_guard_order(
                    strategy_code=stop.strategy_code,
                    broker_account_name=stop.broker_account_name,
                    symbol=stop.symbol,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Fix 3: the pre-close native-guard dedup check is an OPTIMIZATION,
                # not a safety gate. If it stalls/times out (DB hung), PROCEED to
                # fire the protective close — a DB stall must NEVER abort real-money
                # stop protection. Worst case is a duplicate close the periodic sync
                # reconciles, which is strictly safer than a missed stop.
                self.logger.warning(
                    "[HARD-STOP] native-guard pre-check failed (DB stall?) for %s %s — "
                    "proceeding to submit the protective close",
                    stop.strategy_code,
                    stop.symbol,
                )
                has_native_guard = False
            if has_native_guard:
                stop.last_trigger_attempt_at = utcnow()
                return
        stop.last_trigger_attempt_at = utcnow()
        stop.close_in_flight = True
        self.logger.info(
            "[HARD-STOP TRIGGERED] %s %s qty=%s stop=%.4f trigger=%.4f source=%s -> submitting close",
            stop.strategy_code,
            stop.symbol,
            stop.quantity,
            float(stop.stop_price),
            float(trigger_price),
            trigger_source,
        )
        event = TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=stop.strategy_code,
                broker_account_name=stop.broker_account_name,
                symbol=stop.symbol,
                side="sell",
                quantity=stop.quantity,
                intent_type="close",
                reason="HARD_STOP",
                metadata=self._build_hard_stop_metadata(
                    stop=stop,
                    trigger_price=trigger_price,
                    trigger_source=trigger_source,
                ),
            ),
        )
        order_events = await self.process_trade_intent(event)
        if any(item.payload.status in {"accepted", "submitted", "partially_filled", "filled"} for item in order_events):
            stop.consecutive_close_failures = 0  # the close placed — reset the reconcile counter
            if any(item.payload.status == "filled" for item in order_events):
                _popkey = self._hard_stop_key(stop.strategy_code, stop.broker_account_name, stop.symbol)
                self._armed_hard_stops.pop(_popkey, None)
                if self._armed_stop_persistence_enabled:
                    self._armed_stop_dirty.add(_popkey)  # F2: flush deletes the mirror row
            return
        stop.close_in_flight = False
        if any(item.payload.reason in self.NO_POSITION_REASONS for item in order_events):
            _popkey = self._hard_stop_key(stop.strategy_code, stop.broker_account_name, stop.symbol)
            self._armed_hard_stops.pop(_popkey, None)
            if self._armed_stop_persistence_enabled:
                self._armed_stop_dirty.add(_popkey)  # F2: flush deletes the mirror row
            return
        # Bug C: the close neither placed nor named a no-position reason (e.g. Webull
        # ORDER_NOT_SUPPORT_REVERSE_OPTION after the shares were flattened out-of-band).
        # After a few such failures, confirm against the broker; if flat, clear the stop so
        # it stops churning closes on a phantom. NEVER clears on an unconfirmed/failed read.
        stop.consecutive_close_failures += 1
        if stop.consecutive_close_failures >= self._HARD_STOP_RECONCILE_AFTER_FAILURES:
            if await self._broker_position_is_flat(stop):
                _popkey = self._hard_stop_key(stop.strategy_code, stop.broker_account_name, stop.symbol)
                self._armed_hard_stops.pop(_popkey, None)
                if self._armed_stop_persistence_enabled:
                    self._armed_stop_dirty.add(_popkey)  # F2: flush deletes the mirror row
                self.logger.info(
                    "[HARD-STOP RECONCILE-FLAT] %s %s broker flat after %d failed closes -> clearing phantom armed stop",
                    stop.strategy_code, stop.symbol, stop.consecutive_close_failures,
                )
            else:
                # Position still genuinely held — keep protecting; reset so the next burst
                # of failures re-checks (throttles the broker position reads).
                stop.consecutive_close_failures = 0

    @staticmethod
    def _classify_position_read(positions, symbol: str) -> _PositionRead:
        """PURE shape classifier — the single definition of what a positions read MEANS.
        Shared by the live flat-read (`_broker_symbol_position_state`) and the read-only
        settlement probe, so the thing the grace keys on and the thing we measure can never
        drift apart."""
        if not positions:
            return _PositionRead.FLAT_INFERRED          # empty/None: ambiguous
        target = str(symbol).upper()
        for position in positions:
            if str(getattr(position, "symbol", "")).upper() != target:
                continue
            try:
                qty = Decimal(str(getattr(position, "quantity", 0)))
            except (TypeError, ValueError, ArithmeticError):
                return _PositionRead.UNKNOWN
            return _PositionRead.FLAT_CONFIRMED if qty == 0 else _PositionRead.HELD
        return _PositionRead.FLAT_INFERRED              # absent from a non-empty read

    def _settle_watch_add(self, broker_account_name: str, symbol: str) -> None:
        """Anchor a settlement probe at a REAL fill. No-op unless the probe flag is on."""
        if not bool(getattr(self.settings, "oms_settlement_probe_enabled", True)):
            return
        self._settle_watch.setdefault(
            (str(broker_account_name), str(symbol).upper()), datetime.now(UTC)
        )

    def _observe_settlement(self, broker_account_name: str, positions) -> None:
        """READ-ONLY probe, hung on the EXISTING 5s position poll — no extra broker calls.

        Logs, per broker, how long after our own fill the position becomes visible, and the
        SHAPE of every read until it does. FLAT_INFERRED here is the exact ambiguity the
        fresh-fill grace exists to disambiguate: an empty/absent read is indistinguishable
        from a genuine close, so how OFTEN it occurs -- and for how long -- is what tells us
        whether 120s is right, too tight, or too loose. Latency alone would not.
        """
        if not self._settle_watch:
            return
        timeout_s = float(getattr(self.settings, "oms_settlement_probe_timeout_secs", 300) or 300)
        now = datetime.now(UTC)
        for (acct, sym), anchor in list(self._settle_watch.items()):
            if acct != broker_account_name:
                continue
            age = (now - anchor).total_seconds()
            shape = self._classify_position_read(positions, sym)
            if shape is _PositionRead.HELD:
                self.logger.info(
                    "[SETTLE-LAG] acct=%s sym=%s VISIBLE after %.1fs (n=%d)",
                    acct, sym, age, len(positions or []),
                )
                self._settle_watch.pop((acct, sym), None)
            elif age >= timeout_s:
                self.logger.warning(
                    "[SETTLE-LAG] acct=%s sym=%s NEVER VISIBLE after %.0fs — last shape=%s "
                    "(n=%d). Position closed, or this broker never lists it.",
                    acct, sym, age, shape.value.upper(), len(positions or []),
                )
                self._settle_watch.pop((acct, sym), None)
            else:
                self.logger.info(
                    "[SETTLE-PENDING] acct=%s sym=%s age=%.1fs shape=%s (n=%d) — "
                    "our fill is not visible yet",
                    acct, sym, age, shape.value.upper(), len(positions or []),
                )

    async def _broker_symbol_position_state(
        self, broker_account_name: str, symbol: str
    ) -> _PositionRead:
        """Fresh broker read -> FLAT | HELD | UNKNOWN. Only a POSITIVE confirmation is FLAT.

        2026-07-15 (ERNA, real money): the predecessor returned a bool with no way to say "I
        don't know", and the caller DELETED a live armed stop while we held 2 shares -> naked,
        unclosable position. UNKNOWN exists so a failed/unparseable read can never delete
        protection. The `[]`/absent cases stay FLAT here on purpose (see the EMPTY branch) and
        are disarmed by the fresh-fill grace in `_broker_symbol_is_flat` instead.

        Also logs every read (Fix 0 of the design): the 07-15 root cause could only ever be
        INFERRED because nothing recorded what the broker actually returned."""
        try:
            positions = await self.broker_adapter.list_account_positions(broker_account_name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[RECONCILE-READ] acct=%s sym=%s result=ERROR (%s) -> UNKNOWN, keeping protection",
                broker_account_name, symbol, exc,
            )
            return _PositionRead.UNKNOWN
        # Shape decision delegated to the shared pure classifier so the live stop path and the
        # settlement probe can never disagree about what a read MEANS.
        state = self._classify_position_read(positions, symbol)
        self.logger.info(
            "[RECONCILE-READ] acct=%s sym=%s result=%s (n=%d)",
            broker_account_name, symbol, state.value.upper(), len(positions or []),
        )
        return state

    async def _broker_symbol_is_flat(
        self,
        broker_account_name: str,
        symbol: str,
        *,
        established_at: datetime | None = None,
        state: "_PositionRead | None" = None,
    ) -> bool:
        """True ONLY on a positively-confirmed flat. Shared by the ORB hard-stop reconcile
        (#436) and the v2 CW managed-exit reconcile — both DELETE protection on True, so the
        bar is deliberately high.

        `established_at` = when we filled into this position. A flat read inside
        `oms_reconcile_fresh_fill_grace_secs` of it is not credible (a broker positions
        endpoint can lag a fresh fill) and is refused — the 07-15 ERNA shape, where the stop
        triggered 61s after the fill and the read said flat while we held 2 shares.

        Rollback: `oms_reconcile_require_positive_flat=false` restores the pre-fix semantics
        (empty/absent read == flat)."""
        # `state` lets a caller that already read the broker reuse it. `_broker_symbol_position_state`
        # does a FRESH read every time, and Webull 429s under load, so the reconcile path must not
        # pay for two reads to answer one question.
        if state is None:
            state = await self._broker_symbol_position_state(broker_account_name, symbol)
        settings = getattr(self, "settings", None)
        if not bool(getattr(settings, "oms_reconcile_require_positive_flat", True)):
            # Pre-fix semantics, exactly: empty/absent/qty-0 => flat; held => not flat; a
            # raised or unparseable read => not flat (the old code's one correct instinct).
            # UNKNOWN must NOT map to flat here, or the rollback lever would be MORE dangerous
            # than the behaviour it restores.
            return state in (_PositionRead.FLAT_CONFIRMED, _PositionRead.FLAT_INFERRED)
        if state is _PositionRead.HELD or state is _PositionRead.UNKNOWN:
            return False
        if state is _PositionRead.FLAT_CONFIRMED:
            return True   # positive: the broker named this symbol at qty 0. No grace needed.
        # FLAT_INFERRED: ambiguous. Trust it only once our fill is no longer fresh.
        grace_s = float(getattr(settings, "oms_reconcile_fresh_fill_grace_secs", 120) or 0)
        if established_at is not None and grace_s > 0:
            age = (datetime.now(UTC) - established_at).total_seconds()
            if 0 <= age < grace_s:
                self.logger.warning(
                    "[RECONCILE-FRESH-FILL] acct=%s sym=%s broker says FLAT %.1fs after we "
                    "filled (<%.0fs) — refusing to clear protection on a possibly-lagging read",
                    broker_account_name, symbol, age, grace_s,
                )
                return False
        return True

    async def _broker_position_is_flat(self, stop: ArmedHardStop) -> bool:
        """Fresh broker read for a hard-stop's symbol (delegates to _broker_symbol_is_flat).
        Never clears a protective stop on an unconfirmed read, nor within the fresh-fill
        grace of the fill that armed it."""
        return await self._broker_symbol_is_flat(
            stop.broker_account_name, stop.symbol, established_at=stop.armed_at
        )

    def _build_hard_stop_metadata(
        self,
        *,
        stop: ArmedHardStop,
        trigger_price: Decimal,
        trigger_source: str,
    ) -> dict[str, str]:
        metadata = {
            "stop_guard": "true",
            "stop_loss_pct": str(stop.stop_loss_pct),
            "stop_price": _format_limit_price(stop.stop_price) or str(stop.stop_price),
            "stop_trigger_price": _format_limit_price(trigger_price) or str(trigger_price),
            "stop_trigger_source": str(trigger_source),
            "panic_buffer_pct": str(stop.initial_panic_buffer_pct),
            "reference_price": _format_limit_price(trigger_price) or str(trigger_price),
        }
        routed_price = _panic_limit_price(trigger_price, stop.initial_panic_buffer_pct)
        if routed_price is None:
            return metadata
        metadata.update(
            {
                "order_type": "limit",
                "time_in_force": "day",
                "limit_price": routed_price,
                "reference_price": routed_price,
                "price_source": "bid" if trigger_source == "bid" else "last",
            }
        )
        session = _extended_hours_session()
        if session is not None:
            metadata.update(
                {
                    "session": session,
                    "extended_hours": "true",
                }
            )
        return metadata

    def _update_hard_stop_registry_from_fill(
        self,
        *,
        strategy_code: str,
        broker_account_name: str,
        symbol: str,
        side: str,
        intent_type: str,
        quantity: Decimal,
        price: Decimal,
        metadata: dict[str, object],
    ) -> None:
        normalized_symbol = str(symbol).upper()
        key = self._hard_stop_key(strategy_code, broker_account_name, normalized_symbol)
        # F2: this call may mutate the registry for `key` — mark it for durable mirroring.
        # Over-marking (a no-op path) is harmless: the flush reflects the ACTUAL dict state
        # (upsert if present, delete if absent). The dict remains the source of truth.
        if self._armed_stop_persistence_enabled:
            self._armed_stop_dirty.add(key)
        if str(side).lower() == "buy" and str(intent_type).lower() == "open":
            if str(metadata.get("stop_guard_enabled", "")).lower() != "true":
                return
            try:
                stop_loss_pct = float(metadata.get("stop_loss_pct", 0) or 0)
            except (TypeError, ValueError):
                return
            if stop_loss_pct <= 0 or quantity <= 0 or price <= 0:
                return
            try:
                quote_max_age_ms = int(metadata.get("stop_guard_quote_max_age_ms", 2000) or 2000)
            except (TypeError, ValueError):
                quote_max_age_ms = 2000
            try:
                initial_panic_buffer_pct = float(metadata.get("stop_guard_initial_panic_buffer_pct", 1.5) or 1.5)
            except (TypeError, ValueError):
                initial_panic_buffer_pct = 1.5
            if _metadata_marks_extended_hours(metadata):
                quote_max_age_ms = min(
                    max(0, quote_max_age_ms),
                    max(0, int(self.settings.oms_after_hours_stop_guard_quote_max_age_ms)),
                )
                initial_panic_buffer_pct = max(
                    float(initial_panic_buffer_pct),
                    float(self.settings.oms_after_hours_stop_guard_initial_panic_buffer_pct),
                )
            existing = self._armed_hard_stops.get(key)
            if existing is None:
                entry_price = price
                total_quantity = quantity
            else:
                total_quantity = existing.quantity + quantity
                weighted_cost = existing.entry_price * existing.quantity + price * quantity
                entry_price = weighted_cost / total_quantity if total_quantity > 0 else price
            stop_price = entry_price * (Decimal("1") - (Decimal(str(stop_loss_pct)) / Decimal("100")))
            # Trailing-stop ratchet (ORB TRAIL-8%). Absent metadata => trail_pct 0.0
            # => fixed stop, byte-identical to prior behavior. On a scale-in we
            # preserve the existing ratchet (don't reset the HWM or lower the stop).
            try:
                trail_pct = float(metadata.get("trail_pct", 0) or 0)
            except (TypeError, ValueError):
                trail_pct = 0.0
            if existing is not None and trail_pct <= 0:
                trail_pct = float(existing.trail_pct)
            if trail_pct > 0:
                prior_hwm = (
                    existing.high_water_mark
                    if existing is not None and existing.high_water_mark is not None
                    else entry_price
                )
                high_water_mark: Decimal | None = max(entry_price, prior_hwm)
                if existing is not None and existing.stop_price > stop_price:
                    stop_price = existing.stop_price
            else:
                high_water_mark = None
            self._armed_hard_stops[key] = ArmedHardStop(
                strategy_code=strategy_code,
                broker_account_name=broker_account_name,
                symbol=normalized_symbol,
                quantity=total_quantity,
                entry_price=entry_price,
                stop_loss_pct=stop_loss_pct,
                stop_price=stop_price,
                quote_max_age_ms=max(0, quote_max_age_ms),
                initial_panic_buffer_pct=initial_panic_buffer_pct,
                close_in_flight=False,
                last_trigger_attempt_at=None,
                trail_pct=trail_pct,
                high_water_mark=high_water_mark,
                # Fresh-fill grace anchor: this arm is driven by a real fill. On a scale-in
                # keep the ORIGINAL arm time -- the grace protects the position, and refreshing
                # it on every add would extend the window indefinitely on a laddered entry.
                armed_at=(existing.armed_at if existing is not None and existing.armed_at
                          else datetime.now(UTC)),
            )
            _watch = getattr(self, "_settle_watch_add", None)   # P0.2: WEBULL anchor (guarded)
            if _watch is not None:
                _watch(broker_account_name, normalized_symbol)
            self.logger.info(
                "[HARD-STOP ARMED] %s %s qty=%s entry=%.4f stop=%.4f stop_loss_pct=%s trail_pct=%s",
                strategy_code,
                normalized_symbol,
                total_quantity,
                float(entry_price),
                float(stop_price),
                stop_loss_pct,
                trail_pct,
            )
            return

        existing = self._armed_hard_stops.get(key)
        if existing is None or quantity <= 0:
            return
        if str(side).lower() == "sell":
            remaining_quantity = max(Decimal("0"), existing.quantity - quantity)
            if remaining_quantity <= 0:
                self._armed_hard_stops.pop(key, None)
                self.logger.info(
                    "[HARD-STOP CLEARED] %s %s (position flat)",
                    strategy_code,
                    normalized_symbol,
                )
                return
            existing.quantity = remaining_quantity
            self.logger.info(
                "[HARD-STOP DECREMENT] %s %s remaining_qty=%s",
                strategy_code,
                normalized_symbol,
                remaining_quantity,
            )

    def _update_hard_stop_registry_from_order_status(
        self,
        *,
        strategy_code: str,
        broker_account_name: str,
        symbol: str,
        metadata: dict[str, object],
        status: str,
        reason: str,
    ) -> None:
        key = self._hard_stop_key(strategy_code, broker_account_name, symbol)
        if self._armed_stop_persistence_enabled:
            self._armed_stop_dirty.add(key)  # F2: mirror the resulting state (see _from_fill)
        stop = self._armed_hard_stops.get(key)
        if stop is None:
            return
        if str(metadata.get("stop_guard", "")).lower() != "true":
            return
        normalized_status = str(status).lower()
        normalized_reason = str(reason).lower()
        if normalized_status in {"accepted", "submitted", "partially_filled"}:
            stop.close_in_flight = True
            return
        if normalized_status == "filled":
            self._armed_hard_stops.pop(key, None)
            return
        if normalized_status in {"cancelled", "rejected"}:
            if any(token in normalized_reason for token in self.NO_POSITION_REASONS):
                self._armed_hard_stops.pop(key, None)
                return
            stop.close_in_flight = "duplicate_exit_in_flight" in normalized_reason or (
                "broker quantity already reserved for pending exits" in normalized_reason
            )

    def _build_broker_adapter(self) -> BrokerAdapter:
        registrations = configured_broker_account_registrations(self.settings)
        provider_by_account = {registration.name: registration.provider for registration in registrations}
        unique_providers = {provider for provider in provider_by_account.values() if provider}
        if not unique_providers:
            unique_providers = {self.settings.resolved_broker_provider}

        if len(unique_providers) == 1:
            return self._build_provider_adapter(next(iter(unique_providers)))

        return RoutingBrokerAdapter(
            default_provider=self.settings.resolved_broker_provider,
            provider_by_account=provider_by_account,
            factories_by_provider={
                provider: (lambda provider=provider: self._build_provider_adapter(provider))
                for provider in unique_providers | {self.settings.resolved_broker_provider}
            },
        )

    def _build_provider_adapter(self, provider: str) -> BrokerAdapter:
        normalized = str(provider).strip().lower()
        if self.settings.oms_adapter == "simulated":
            return SimulatedBrokerAdapter()
        if normalized == "simulated":
            return SimulatedBrokerAdapter()
        if normalized == "alpaca":
            return AlpacaPaperBrokerAdapter(self.settings)
        if normalized == "schwab":
            return SchwabBrokerAdapter(self.settings)
        if normalized == "webull":
            return WebullBrokerAdapter(self.settings)
        raise RuntimeError(f"Unsupported broker provider: {provider}")

    def seed_runtime_metadata(self) -> dict[str, int]:
        summary = seed_runtime_metadata(
            self.settings,
            session_factory=self.session_factory,
            store=self.store,
        )
        return {
            "strategies": summary.strategies,
            "broker_accounts": summary.broker_accounts,
        }

    def _load_global_manual_stop_symbols(self) -> set[str]:
        """Operator manual-stop list, read from the SAME `dashboard_snapshots` row the control
        plane writes (`global_manual_stop_symbols`, payload {"symbols": [...]}).

        Cached for `_MANUAL_STOP_CACHE_SECS` so this costs one query per window, not one per intent.
        Any failure returns the LAST GOOD set (never an empty one) — a DB blip must not silently
        un-stop a symbol the operator deliberately halted.
        """
        now = time.monotonic()
        if now - self._manual_stop_loaded_at < self._MANUAL_STOP_CACHE_SECS:
            return self._manual_stop_symbols
        if self.session_factory is None:
            return self._manual_stop_symbols
        try:
            with self.session_factory() as session:
                snapshot = session.scalar(
                    select(DashboardSnapshot)
                    .where(DashboardSnapshot.snapshot_type == "global_manual_stop_symbols")
                    .order_by(desc(DashboardSnapshot.created_at))
                )
            payload = getattr(snapshot, "payload", None)
            syms = payload.get("symbols", []) if isinstance(payload, dict) else []
            self._manual_stop_symbols = {
                str(x).strip().upper() for x in syms if str(x).strip()
            } if isinstance(syms, list) else set()
            self._manual_stop_loaded_at = now
        except Exception:  # noqa: BLE001 - keep the last good set; never fail OPEN
            # Stamp the clock on failure too, so a DB outage retries once per window instead of
            # logging a traceback on every single intent.
            self._manual_stop_loaded_at = now
            self.logger.exception(
                "manual-stop load failed; keeping the previous set %s", sorted(self._manual_stop_symbols)
            )
        return self._manual_stop_symbols

    def _evaluate_risk(self, event: TradeIntentEvent) -> tuple[bool, str]:
        symbol = str(event.payload.symbol).strip().upper()
        if symbol and symbol in self.settings.protected_symbol_set:
            return False, f"protected_symbol:{symbol}"
        # OPERATOR MANUAL STOP (2026-07-27). The operator cancelled a v2 resting order on DFNS twice
        # and the bot RE-PLACED it within ~2 minutes each time: `_cw_v2_resting_track` places whenever
        # `state.resting_active` is False, and a broker-side cancel clears exactly that flag — so the
        # bot cannot tell "my order expired" from "a human killed this". There was no live, no-restart
        # way to veto a symbol: manual-stop was wired to the scanner and the in-process bots ONLY
        # (`grep manual_stop` hit strategy_engine_app.py but NOT oms/service.py or the v2 bot), so
        # stopping one symbol needed a blacklist AND an env edit AND a service restart.
        # Enforcing here covers EVERY strategy through the one chokepoint every intent passes.
        #
        # EXPOSURE-DIRECTIONAL (corrected same day): a manual stop halts anything that OPENS or
        # INCREASES exposure and NEVER blocks getting out. The first cut blocked every intent type,
        # which would have STRANDED an open position -- the OMS could not have closed it and the
        # operator would have had to sell by hand, the exact thing the operator does not want. A
        # stop means "stop buying this", not "abandon what I already hold".
        #   blocked : open (any side -- a short entry is still new exposure), scale-IN (buy)
        #   allowed : close, cancel (both REDUCE risk), scale-OUT (sell -- the +2/4% profit ladder)
        if symbol and symbol in self._manual_stop_symbols:
            intent_type = str(event.payload.intent_type or "").strip().lower()
            side = str(event.payload.side or "").strip().lower()
            reduces_or_cancels = intent_type in {"close", "cancel"} or (
                intent_type == "scale" and side == "sell"
            )
            if not reduces_or_cancels:
                return False, f"manual_stop:{symbol}"
        if event.payload.intent_type == "cancel":
            if event.payload.quantity < 0:
                return False, "cancel quantity cannot be negative"
        elif event.payload.quantity <= 0:
            return False, "quantity must be positive"
        if event.payload.intent_type not in {"open", "scale", "close", "cancel"}:
            return False, f"unsupported intent_type={event.payload.intent_type}"
        if event.payload.side not in {"buy", "sell"}:
            return False, f"unsupported side={event.payload.side}"
        return True, "ok"

    def _build_client_order_id(self, event: TradeIntentEvent) -> str:
        intent_id = event.event_id.hex[:12]
        coid = f"{event.payload.strategy_code}-{event.payload.symbol}-{event.payload.intent_type}-{intent_id}"
        # Bound to the broker cap (no-op for real strategy/symbol/type combos, which are
        # <=38 chars; a safety net so an unusually long code can never emit an over-cap id).
        return coid[: self._CLIENT_ORDER_ID_MAX_LEN]

    def _build_order_event(
        self,
        *,
        intent_event: TradeIntentEvent,
        intent_db_id: UUID,
        order_db_id: UUID,
        report: ExecutionReport,
        client_order_id: str | None = None,
        symbol: str | None = None,
        side: str | None = None,
        quantity: Decimal | None = None,
    ) -> OrderEventEvent:
        return OrderEventEvent(
            source_service=SERVICE_NAME,
            correlation_id=intent_event.event_id,
            payload=OrderEventPayload(
                intent_event_id=intent_event.event_id,
                intent_db_id=intent_db_id,
                order_db_id=order_db_id,
                strategy_code=intent_event.payload.strategy_code,
                broker_account_name=intent_event.payload.broker_account_name,
                client_order_id=client_order_id if client_order_id is not None else report.client_order_id,
                broker_order_id=report.broker_order_id,
                broker_fill_id=report.broker_fill_id,
                symbol=symbol if symbol is not None else intent_event.payload.symbol,
                side=(side or intent_event.payload.side),  # type: ignore[arg-type]
                intent_type=intent_event.payload.intent_type,
                status=report.event_type,
                quantity=quantity if quantity is not None else intent_event.payload.quantity,
                filled_quantity=report.filled_quantity,
                fill_price=report.fill_price,
                reason=report.reason or intent_event.payload.reason,
                metadata=carry_fanout_identity(
                    report.metadata, intent_event.payload.metadata
                ),
            ),
        )

    def _orb_quote_priced_entry_applies(self, event: TradeIntentEvent) -> bool:
        """Piece 1 gate: only the flag-on ORB entry buy with the quote-priced contract
        (order_type=limit + price_source=ask). Everything else is a no-op -> byte-identical."""
        md = event.payload.metadata
        return (
            bool(getattr(self.settings, "orb_oms_quote_priced_entry_enabled", False))
            and event.payload.strategy_code == "orb"
            and event.payload.intent_type == "open"
            and event.payload.side == "buy"
            and str(md.get("order_type", "")).lower() == "limit"
            and str(md.get("price_source", "")).lower() == "ask"
        )

    def _fresh_ask(self, symbol: str, max_age_ms: int) -> float | None:
        """The live ask from the OMS quote book (Polygon NBBO) if fresh enough, else None.
        NOTE (standing): no Webull quote entitlement -> ORB prices/stops off Polygon
        consolidated NBBO while executing on Webull; first suspect if thin-name fills look off."""
        quote = self._latest_quotes_by_symbol.get(symbol)
        if not quote:
            return None
        received_at = quote.get("received_at")
        ask = quote.get("ask")
        if ask in (None, 0) or not isinstance(received_at, datetime):
            return None
        if (utcnow() - received_at).total_seconds() * 1000.0 > max(0, max_age_ms):
            return None
        ask_f = float(ask)
        return ask_f if ask_f > 0 else None

    def _abandon_orb_entry(
        self,
        *,
        event: TradeIntentEvent,
        intent: TradeIntent,
        reason_code: str,
        reason_detail: str,
    ) -> OrderEventEvent:
        """Pre-submission abandon for the quote-priced ORB entry (no broker order exists yet).
        Stamps the reason onto the intent metadata for later winners-missed vs fakeouts-dodged
        analysis, marks the intent rejected, logs [OMS-ABANDON-INTENT], and returns the event."""
        md = event.payload.metadata
        md["abandon_intent"] = "true"
        md["abandon_reason_code"] = reason_code
        md["abandon_reason_detail"] = reason_detail
        md["oms_quote_priced"] = "abandoned"
        self.store.mark_intent_refused(
            intent,
            origin="client_abort",
            code=reason_code,
        )
        self.logger.info(
            "[OMS-ABANDON-INTENT] code=%s symbol=%s strategy=%s side=%s reason=%s",
            reason_code,
            event.payload.symbol,
            event.payload.strategy_code,
            event.payload.side,
            reason_detail,
        )
        return self._build_rejected_event(event, intent.id, reason=reason_code)

    def _resting_entry_already_open(self, session, account_name: str, symbol: str) -> bool:
        """True if an OPEN order tagged `resting_entry` already exists for (account, symbol) -- the
        restart-dedup guard so the v2 resting flip-entry can never place a second live buy order."""
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == account_name))
        if account is None:
            return False
        want = str(symbol).upper()
        for order in self.store.list_open_orders(session, broker_account_ids=[account.id]):
            if (str(order.symbol).upper() == want
                    and str((order.payload or {}).get("resting_entry", "")).lower() == "true"):
                return True
        return False

    def _apply_v2_oco_bracket_entry(self, *, event: TradeIntentEvent) -> None:
        """Attach native-OCO bracket metadata to a v2 buy-open so the Schwab adapter places a
        TRIGGER->OCO combo instead of a single-leg order. No-op / byte-identical when the flag
        is off or this is not a v2 entry.

        The exit legs use the SAME percentages the CW software ladder uses (target +
        ``oms_v2_cw_target_pct``, protective - ``oms_v2_cw_hard_stop_pct``), so the broker OCO
        is the same geometry the OMS would otherwise run -- the bracket relocates the exit to
        the broker, it does not change it. The entry stays whatever the intent asked for
        (market/limit); ``bracket_entry_type`` mirrors the order_type so a LIMIT entry stays a
        LIMIT parent.

        ⚠ Emitting the bracket does NOT itself stand the software ladder down -- that is the
        stand-down's job (`_native_oco_stand_down_active`), keyed on the broker CONFIRMING the
        legs are live. This only places the combo. The two flags are independent by design so
        neither silently implies the other.
        """
        if not bool(getattr(self.settings, "oms_v2_emit_native_oco_bracket_enabled", False)):
            return
        payload = event.payload
        if payload.strategy_code != "schwab_1m_v2":
            return
        if payload.side != "buy" or payload.intent_type != "open":
            return
        # ⛔⭐⭐ BROKER SCOPE (2026-08-19). THIS DECORATOR IS SCHWAB-SHAPED AND HAD NO BROKER GATE.
        #
        # It keyed on `strategy_code == "schwab_1m_v2"` alone, so it could not tell the SCHWAB
        # PRIMARY from the WEBULL FAN-OUT legs of the same signal, and stamped a Schwab-shaped
        # bracket onto both. On the `rth_resting_mirror` leg that is fatal:
        #
        #   * the strategy emits that leg BARE, on purpose -- "⛔ NO bracket_* keys" -- because
        #     Probe W (2026-08-12, CORE/RTH, live account) proved Webull ACCEPTS a stop-limit master
        #     STANDALONE (200) and refuses it only with legs attached (417);
        #   * this function then added `bracket_entry_type=STOP_LIMIT` + `native_oco_bracket` +
        #     target/stop, converting the shape Webull accepts into the one it refuses;
        #   * the adapter's combo guard -- CORRECT for combos -- then aborted it CLIENT-SIDE, so the
        #     order never reached Webull at all.
        #
        # MEASURED: 570 of 572 mirror orders carry those keys and were refused; the ONLY 2 that ever
        # FILLED are the 2 that escaped this stamping. Five sessions, zero mirror fills.
        #
        # ⛔ THE SCOPE IS DELIBERATELY NARROW: webull + STOP_LIMIT only. The LIMIT/MARKET fan-out
        # legs DEPEND on this bracket -- 174 live fan-out brackets in 14 days -- and excluding all
        # Webull legs would strip protection from every one of them. Only the stop-limit master is
        # the illegal-as-combo shape.
        #
        # ⛔ THIS IS A DEFECT FIX, NOT A FEATURE TOGGLE. Stamping a Schwab-shaped bracket onto a
        # Webull leg is wrong whether or not the mirror flag is on; do not couple the two.
        md_scope = payload.metadata
        if (
            str(md_scope.get("fanout_leg", "")).lower() == "webull"
            and str(md_scope.get("order_type", "")).upper() == "STOP_LIMIT"
        ):
            self.logger.info(
                "[V2-OCO-EMIT] %s %s SKIPPED (webull stop-limit master) -- Webull accepts this "
                "shape only BARE; attaching a bracket makes it a combo master the broker refuses",
                payload.symbol, payload.broker_account_name,
            )
            return
        # ⭐ RTH-ONLY (the native OCO is a regular-session construct). v2 enters from 07:00 ET
        # but the bracket uses session=NORMAL: a MARKET+STOP OTOCO placed PRE-market would queue
        # to 09:30 (missing the pre-market entry) or firm-reject. So OUTSIDE regular hours we do
        # NOT emit the bracket -- the entry is a plain single-leg order and the software CW ladder
        # (extended-hours-capable via the #390 LIMIT+session-off-bid exit) protects it. The
        # stand-down then stays inactive (no armed OCO to detect), so the ladder runs. Matches the
        # design's RTH-first scope and mirrors the native-stop-guard, which is RTH-only for the
        # identical reason.
        if not _is_regular_market_session():
            self.logger.info(
                "[V2-OCO-EMIT] %s %s SKIPPED (outside regular hours) -- plain entry, software "
                "ladder owns the exit", payload.symbol, payload.broker_account_name,
            )
            return
        md = payload.metadata
        # entry reference: the price the CW entry computed (the break level / fill ref).
        entry_ref = md.get("entry_price") or md.get("reference_price")
        try:
            entry = float(entry_ref)
        except (TypeError, ValueError):
            entry = 0.0
        if entry <= 0:
            # No usable entry reference -> do NOT emit a half-specified bracket; fall back to
            # the plain single-leg entry (the adapter also refuses an incomplete bracket).
            self.logger.warning(
                "[V2-OCO-EMIT] %s %s no usable entry reference (entry_price/reference_price); "
                "placing the plain single-leg entry instead of a bracket",
                payload.symbol, payload.broker_account_name,
            )
            return
        target = entry * (1.0 + self._cw_target_pct / 100.0)
        protect = entry * (1.0 - self._cw_stop_pct / 100.0)
        order_type = str(md.get("order_type", "market")).upper()
        md["bracket"] = "true"
        if order_type == "STOP_LIMIT":
            # The resting flip-entry: a buy-stop-limit master. The strategy supplies stop_price
            # (the ATR line = trigger) and limit_price (line*(1+band) = the slippage cap); the
            # adapter needs BOTH. Round both to the Schwab tick rule (firm-rejects off-tick).
            md["bracket_entry_type"] = "STOP_LIMIT"
            if md.get("stop_price"):
                md["stop_price"] = _schwab_round(float(md["stop_price"]))
            if md.get("limit_price"):
                md["limit_price"] = _schwab_round(float(md["limit_price"]))
        elif order_type == "LIMIT":
            md["bracket_entry_type"] = "LIMIT"
        else:
            md["bracket_entry_type"] = "MARKET"
        md["native_oco_bracket"] = "true"
        # Schwab tick rule (firm-rejects otherwise): >$1 -> 2 decimals, <=$1 -> 4 decimals.
        md["bracket_target_price"] = _schwab_round(target)
        md["bracket_stop_price"] = _schwab_round(protect)
        self.logger.info(
            # ⛔⭐ THE ACCOUNT IS LOAD-BEARING ON THIS LINE (added 2026-08-17). Without it this
            # marker is the ONLY unscoped one in the exit-protection picture, and the fan-out puts
            # BOTH accounts on the same symbol at the same moment — so any per-account attribution
            # has to guess, and a Schwab bracket can be read as covering a Webull fill. The 08-17
            # denominator had to carry a `NATIVE_OCO?` question mark for exactly this reason, which
            # blocked stating "the attach never succeeds" per account at all.
            "[V2-OCO-EMIT] %s %s bracket entry=%.4f -> OCO[target=%.4f stop=%.4f] (type=%s)",
            payload.symbol, payload.broker_account_name, entry, target, protect,
            md["bracket_entry_type"],
        )

    def _apply_orb_quote_priced_entry(
        self,
        *,
        session: Session,
        event: TradeIntentEvent,
        intent: TradeIntent,
    ) -> OrderEventEvent | None:
        """Piece 1: price the ORB entry limit off the OMS's own live quote at placement.

        Returns None to PROCEED (after mutating the limit in event.payload.metadata), or a
        rejected OrderEventEvent to ABANDON (short-circuit before any broker submit). No-op
        (returns None, no mutation) when the flag is off or the intent is not a quote-priced
        ORB entry -> byte-identical. ``session`` is unused today but kept for symmetry with
        the other pre-submit helpers and future per-symbol lookups.
        """
        del session  # reserved; abandon marks intent in the caller's open session
        if not self._orb_quote_priced_entry_applies(event):
            return None
        md = event.payload.metadata
        symbol = str(event.payload.symbol).upper()
        # Bound base is mandatory (fail-closed): without it we cannot bound the chase.
        try:
            break_level = float(md["orb_intended_break_level"])
        except (KeyError, TypeError, ValueError):
            return self._abandon_orb_entry(
                event=event, intent=intent, reason_code="MISSING_BOUND",
                reason_detail="orb_intended_break_level absent/invalid; cannot bound quote-priced entry",
            )
        if break_level <= 0:
            return self._abandon_orb_entry(
                event=event, intent=intent, reason_code="MISSING_BOUND",
                reason_detail=f"orb_intended_break_level non-positive ({break_level})",
            )
        try:
            gap_cap_pct = float(md.get("orb_gap_cap_pct", 0.0))
        except (TypeError, ValueError):
            gap_cap_pct = 0.0
        bound = break_level * (1.0 + gap_cap_pct / 100.0)
        max_age_ms = int(getattr(self.settings, "orb_oms_quote_priced_max_age_ms", 2000))
        ask = self._fresh_ask(symbol, max_age_ms)
        if ask is None:
            return self._abandon_orb_entry(
                event=event, intent=intent, reason_code="NO_FRESH_QUOTE",
                reason_detail=f"no fresh ask within {max_age_ms}ms for {symbol}",
            )
        if ask > bound:
            return self._abandon_orb_entry(
                event=event, intent=intent, reason_code="ASK_PAST_GAP_CAP",
                reason_detail=(
                    f"ask {ask:.4f} past gap-cap bound {bound:.4f} "
                    f"(break {break_level:.4f} +{gap_cap_pct}%)"
                ),
            )
        # ask <= bound: marketable buy limit at ask + 1 tick, never exceeding the bound (Q3).
        tick = Decimal("0.01") if ask >= 1.0 else Decimal("0.0001")
        limit = min(Decimal(str(ask)) + tick, Decimal(str(bound)))
        # ROUND_DOWN so tick-alignment can never push the limit back above the gap-cap bound.
        limit_s = format(limit.quantize(tick, rounding=ROUND_DOWN), "f")
        md["limit_price"] = limit_s
        md["reference_price"] = limit_s
        md["oms_quote_priced"] = "true"
        md["oms_quote_ask"] = f"{ask:.4f}"
        md["oms_quote_bound"] = f"{bound:.4f}"
        self.logger.info(
            "[OMS-ORB-QUOTE-PRICED] symbol=%s ask=%.4f break=%.4f bound=%.4f limit=%s",
            symbol, ask, break_level, bound, limit_s,
        )
        return None

    def _v2_eh_reactive_entry_applies(self, event: TradeIntentEvent) -> bool:
        """Gate for the EH reactive-entry marketable-limit enhancement (P-B1). Only the flag-on v2
        REACTIVE buy-open in extended hours qualifies. The RESTING entry (metadata resting_entry=true)
        is EXCLUDED — it is P-B2 and is drained on a path that never reaches this builder. Everything
        else (RTH, non-v2, sells, closes, flag-off) is a no-op -> byte-identical."""
        if not bool(getattr(self.settings, "oms_v2_eh_entry_enabled", False)):
            return False
        p = event.payload
        md = p.metadata
        return (
            p.strategy_code == "schwab_1m_v2"
            and p.intent_type == "open"
            and p.side == "buy"
            and str(md.get("resting_entry", "")).lower() != "true"
            and not _is_regular_market_session()
        )

    def _apply_v2_eh_reactive_entry(
        self,
        *,
        session: Session,
        event: TradeIntentEvent,
        intent: TradeIntent,
    ) -> OrderEventEvent | None:
        """Re-price the v2 REACTIVE entry as a MARKETABLE, capped EH-LIMIT off the OMS's own fresh ask.

        CONTEXT (2026-07-24, P-B1): the bot already routes a v2 EH open to a session=AM/PM LIMIT at the
        live ask (`_apply_extended_hours_routing`, restored dc11d5a 2026-06-23) — so the reactive entry is
        fillable pre-market TODAY, NOT the unfillable MARKET the older design assumed. This method (flag-
        gated OFF) layers the design's thin-EH slippage protection on top: it prices the limit off the
        OMS Polygon quote book (`_latest_quotes_by_symbol`, the ONLY feed available for the Webull mirror,
        which has no market-data entitlement), buffers it just above the ask so it crosses, and BOUNDS it
        by a max-cross cap vs the strategy's signal price (`entry_price`, the break level). Past the cap,
        or with no fresh ask, it ABANDONS — preferring no fill to a bad thin-pre-market fill.

        Returns None to PROCEED (after mutating event.payload.metadata's limit) or a rejected event to
        ABANDON (short-circuit before any broker submit). No-op (returns None, no mutation) when the flag
        is off / not a v2 EH reactive entry -> byte-identical: the bot's plain limit-at-ask stands.
        ``session`` is unused (abandon marks the intent in the caller's open session); kept for symmetry
        with the other pre-submit helpers."""
        del session
        if not self._v2_eh_reactive_entry_applies(event):
            return None
        md = event.payload.metadata
        symbol = str(event.payload.symbol).upper()
        session_code = _extended_hours_session()  # "AM"/"PM" (applies() already confirmed EH)
        # Signal price = the break level the strategy computed (the cap anchor). Mandatory (fail-closed):
        # without it we cannot bound the chase, and a reactive draft always carries it.
        try:
            signal_px = float(md["entry_price"])
        except (KeyError, TypeError, ValueError):
            return self._abandon_v2_eh_entry(
                event=event, intent=intent, reason_code="MISSING_SIGNAL",
                reason_detail="entry_price absent/invalid; cannot bound the EH reactive entry",
            )
        if signal_px <= 0:
            return self._abandon_v2_eh_entry(
                event=event, intent=intent, reason_code="MISSING_SIGNAL",
                reason_detail=f"entry_price non-positive ({signal_px})",
            )
        max_age_ms = int(getattr(self.settings, "oms_v2_eh_entry_quote_max_age_ms", 2000))
        ask = self._fresh_ask(symbol, max_age_ms)
        if ask is None:
            return self._abandon_v2_eh_entry(
                event=event, intent=intent, reason_code="NO_FRESH_QUOTE",
                reason_detail=f"no fresh ask within {max_age_ms}ms for {symbol}",
            )
        max_cross_pct = float(getattr(self.settings, "oms_v2_eh_entry_max_cross_pct", 1.0))
        cap = signal_px * (1.0 + max_cross_pct / 100.0)
        if ask > cap:
            # The live ask has run past the signal by more than the cap -> the market moved away from the
            # setup; prefer NO fill to chasing a thin-pre-market spike (design risk #3).
            return self._abandon_v2_eh_entry(
                event=event, intent=intent, reason_code="ASK_PAST_CROSS_CAP",
                reason_detail=(
                    f"ask {ask:.4f} past max-cross cap {cap:.4f} "
                    f"(signal {signal_px:.4f} +{max_cross_pct}%)"
                ),
            )
        # ask <= cap: marketable buy limit = ask * (1 + buffer%), never exceeding the cap.
        buffer_pct = float(getattr(self.settings, "oms_v2_eh_entry_limit_buffer_pct", 0.3))
        tick = Decimal("0.01") if ask >= 1.0 else Decimal("0.0001")
        buffered = Decimal(str(ask)) * (Decimal("1") + Decimal(str(buffer_pct)) / Decimal("100"))
        limit = min(buffered, Decimal(str(cap)))
        # ROUND_DOWN so tick-alignment can never push the limit back above the cap.
        limit_s = format(limit.quantize(tick, rounding=ROUND_DOWN), "f")
        md["order_type"] = "limit"
        md["limit_price"] = limit_s
        md["reference_price"] = limit_s
        md["session"] = session_code
        md["extended_hours"] = "true"
        md["price_source"] = "ask"
        md["oms_v2_eh_entry"] = "true"
        md["oms_v2_eh_entry_ask"] = f"{ask:.4f}"
        md["oms_v2_eh_entry_cap"] = f"{cap:.4f}"
        self.logger.info(
            "[OMS-V2-EH-ENTRY] symbol=%s session=%s ask=%.4f signal=%.4f cap=%.4f limit=%s",
            symbol, session_code, ask, signal_px, cap, limit_s,
        )
        return None

    def _abandon_v2_eh_entry(
        self,
        *,
        event: TradeIntentEvent,
        intent: TradeIntent,
        reason_code: str,
        reason_detail: str,
    ) -> OrderEventEvent:
        """Pre-submission abandon for the EH reactive entry (no broker order exists yet). Marks the
        intent rejected, logs [OMS-ABANDON-INTENT], and returns the rejected event — mirrors the ORB
        quote-priced abandon so the conservative no-blind-order bias is identical across entry paths."""
        md = event.payload.metadata
        md["abandon_intent"] = "true"
        md["abandon_reason_code"] = reason_code
        md["abandon_reason_detail"] = reason_detail
        md["oms_v2_eh_entry"] = "abandoned"
        self.store.mark_intent_refused(
            intent,
            origin="client_abort",
            code=reason_code,
        )
        self.logger.info(
            "[OMS-ABANDON-INTENT] code=%s symbol=%s strategy=%s side=%s reason=%s",
            reason_code, event.payload.symbol, event.payload.strategy_code,
            event.payload.side, reason_detail,
        )
        return self._build_rejected_event(event, intent.id, reason=reason_code)

    def _band_capped_marketable_limit(
        self, *, symbol: str, level: float, band_pct: float, max_age_ms: int
    ) -> tuple[str, float, float] | tuple[None, str, str]:
        """ONE implementation of the band-capped marketable buy limit, shared by the EH resting
        entry (P-B2, deployed 2026-07) and the RTH reactive entry (2026-08-10).

        Returns `(limit_str, ask, cap)` to proceed, or `(None, reason_code, reason_detail)` to
        abandon. ⛔ Extracted VERBATIM from `_apply_v2_eh_resting_entry` — the EH path's arithmetic,
        rounding and abandon reasons are unchanged, and its tests still pin them. A second
        implementation would be free to drift; **any RTH-vs-EH difference must be deliberate and
        stated at the call site**, not an accident of copy-paste.
        """
        cap = level * (1.0 + band_pct / 100.0)
        ask = self._fresh_ask(symbol, max_age_ms)
        if ask is None:
            return (None, "NO_FRESH_QUOTE", f"no fresh ask within {max_age_ms}ms for {symbol}")
        if ask > cap:
            # The ask has gapped past the band -> prefer NO fill to chasing (no-chase).
            return (None, "ASK_PAST_BAND",
                    f"ask {ask:.4f} past band cap {cap:.4f} (level {level:.4f} +{band_pct}%) "
                    f"— gap-through, prefer no fill")
        tick = Decimal("0.01") if ask >= 1.0 else Decimal("0.0001")
        # ⭐ THE CEILING IS THE ABANDON ABOVE, NOT THIS `min`. Past that guard `ask <= cap` always
        # holds, so `min(ask, cap) == ask` in every reachable case — the min can never bind. Kept as
        # written (inherited verbatim from the EH pricer) because it is harmless and defensive, but
        # do NOT read it as the price cap: a mutation deleting it leaves every test green, while
        # deleting the ASK_PAST_BAND abandon fails 3 and widening the band fails 6. **Mutate the
        # abandon, not the min, when checking that the ceiling is still protected.**
        limit = min(Decimal(str(ask)), Decimal(str(cap)))
        # ROUND_DOWN so tick-alignment can never push the limit back above the band cap.
        return (format(limit.quantize(tick, rounding=ROUND_DOWN), "f"), ask, cap)

    def _v2_rth_reactive_limit_applies(self, event: TradeIntentEvent) -> bool:
        """Gate for the RTH REACTIVE band-capped limit (2026-08-10).

        ⭐⭐ WHY. The reactive path sends a MARKET order after the print. Measured over 21 days on
        `live:schwab_1m_v2`, same universe and window: reactive MARKET **SD 58.6 bps, worst adverse
        +351.7**; the price-committed paths **SD ~25-28 bps, worst +60.2**. The ≥200 bps entries are
        **unbounded-price events, not late-arrival events** — chasing costs the spread and the drift
        (tens of bps); having NO CEILING is what produces 352. A price cap caps the price.

        ⛔ This does NOT fix the chasing — the trigger and timing are unchanged. That is the separate
        resting-reactive change (docs/v2-reactive-resting-entry-design.md), which is blocked tonight
        by `_resting_entry_already_open` (one resting order per symbol; a second slot is the
        #580/EGG-POLA orphan surface) and is worth doing on its own.

        Matches a v2 RTH reactive open only: EH is the EH pricer's job, `resting_entry`/`eh_resting`
        are the resting path's, and the fan-out leg is deliberately untouched here."""
        p = event.payload
        md = p.metadata
        return (
            p.strategy_code == "schwab_1m_v2"
            and p.intent_type == "open"
            and p.side == "buy"
            and str(md.get("atr_variant", "")) == "CW-v2"
            and str(md.get("resting_entry", "")).lower() != "true"
            and str(md.get("eh_resting", "")).lower() != "true"
            and str(md.get("fanout_leg", "")) == ""
            and _is_regular_market_session()
        )

    def _v2_rth_fanout_limit_applies(self, event: TradeIntentEvent) -> bool:
        """Gate for the RTH FAN-OUT band-cap re-price (2026-08-12) — the half #674 left out.

        ⛔ #674 capped the SCHWAB primary and said so explicitly: *"the fan-out leg is deliberately
        untouched here."* The strategy still builds that leg as
        `order_type: "limit" if session_is_eh else "market"` (`schwab_1m_v2.py:2236`), so in regular
        hours the Webull leg is an UNCAPPED MARKET order — on BOTH fan-out sources, `reactive` and
        `rth_resting`. This gate is deliberately keyed on `fanout_leg` + `order_type == market`
        rather than on the source, so it covers both without naming either.

        ⭐ Matching on `order_type == "market"` also makes it self-limiting: once the leg carries a
        price it is out of scope, so this can never double-price an order.

        EH is out of scope — the EH fan-out leg is already a limit and is priced by the EH builder.
        """
        if not bool(getattr(self.settings, "oms_v2_rth_fanout_limit_enabled", False)):
            return False
        p = event.payload
        md = p.metadata
        return (
            p.strategy_code == "schwab_1m_v2"
            and p.intent_type == "open"
            and p.side == "buy"
            and str(md.get("fanout_leg", "")).lower() == "webull"
            and str(md.get("order_type", "")).lower() == "market"
            and _is_regular_market_session()
        )

    def _apply_v2_rth_fanout_limit(
        self, *, event: TradeIntentEvent, intent: TradeIntent
    ) -> OrderEventEvent | None:
        """Re-price the RTH Webull FAN-OUT leg as a band-capped marketable LIMIT instead of a MARKET.

        Same anchor, band and pricer as #674's primary path, so the two legs of one signal are finally
        priced by the same rule. Returns None to PROCEED, or a rejected event to ABANDON.

        ⭐ WHY A LIMIT AND NOT A STOP-LIMIT. Probe W (2026-08-12, CORE/RTH) proved Webull REFUSES a
        STOP_LIMIT combo master (417 `invalid order_type`) and ACCEPTS a LIMIT master with
        STOP_PROFIT + STOP_LOSS attached (HTTP 200, placed live). A capped LIMIT therefore keeps the
        broker-side bracket that 174 live fan-out entries depend on — there is no price/protection
        trade-off on this shape, which is exactly why this is the change worth making.

        ⚠️ THE NEW FAILURE MODE, NAMED: a market order always fills; this one will sometimes not.
        That is a real behaviour change, and it is the operator's stated preference on entries —
        a no-fill is acceptable, a bad fill is not. Both outcomes are logged so the tape answers the
        frequency directly instead of needing a reconstruction."""
        if not self._v2_rth_fanout_limit_applies(event):
            return None
        md = event.payload.metadata
        symbol = str(event.payload.symbol).upper()
        try:
            level = float(md["entry_price"])
        except (KeyError, TypeError, ValueError):
            return None          # no anchor -> leave the leg exactly as it is today (MARKET)
        if level <= 0:
            return None
        # ⛔⭐⭐ THE BAND ANCHOR IS NOT ALWAYS `entry_price` (fixed 2026-08-13, day-one validation).
        # `_fanout_rth_resting_cross` sets `entry_price` to the price at which SOFTWARE DETECTED the
        # cross, not to the level the Schwab stop-limit was resting at. Live FGI 08-13: resting level
        # 8.3015, cross detected at 8.6461 — so a band off `entry_price` permitted the entire 4.15%
        # run-up and then capped half a percent beyond THAT. The cap must measure from the price we
        # decided to buy at, which is the RESTING LEVEL.
        #
        # ⛔ AND IT MUST BE A SEPARATE FIELD, NOT A CHANGE TO `entry_price`. That field also anchors
        # the OCO bracket (`_apply_v2_oco_bracket_entry`). Re-anchoring it to the resting level would
        # have put FGI's target at 8.4675 while the leg filled near 8.69 — a target BELOW the fill,
        # i.e. an instant loss exit. A worse bug than the one being fixed.
        #
        # ⭐ The two anchors converge by construction once this is in: we only PLACE when the cross is
        # within the band of the level, so a placed leg has cross ≈ level and the bracket is sound.
        # Everything else abandons, and an abandoned leg has no bracket to mis-anchor.
        anchor = level
        try:
            band_anchor = float(md["resting_band_anchor"])
            if band_anchor > 0:
                anchor = band_anchor
        except (KeyError, TypeError, ValueError):
            pass
        try:
            band_pct = float(md["resting_band_pct"])
        except (KeyError, TypeError, ValueError):
            band_pct = float(getattr(self.settings, "oms_v2_eh_resting_entry_band_pct", 0.5))
        max_age_ms = int(getattr(self.settings, "oms_v2_eh_resting_entry_quote_max_age_ms", 2000))
        limit_s, a, b = self._band_capped_marketable_limit(
            symbol=symbol, level=anchor, band_pct=band_pct, max_age_ms=max_age_ms
        )
        if limit_s is None:
            reason_code, reason_detail = a, b
            self.logger.info(
                "[OMS-V2-RTH-FANOUT-LIMIT] symbol=%s source=%s ABANDONED reason=%s — %s "
                "(anchor=%.4f, entry_price=%.4f)",
                symbol, md.get("fanout_source", "?"), reason_code, reason_detail, anchor, level,
            )
            return self._abandon_v2_eh_entry(
                event=event, intent=intent, reason_code=reason_code, reason_detail=reason_detail,
            )
        ask, cap = a, b
        md["order_type"] = "limit"
        md["limit_price"] = limit_s
        # ⛔⭐⭐ THE BRACKET PATH READS A DIFFERENT FIELD (fixed 2026-08-13, day-one validation).
        # `webull.py::_build_combo_payload` types the combo MASTER off `bracket_entry_type`, NOT off
        # `order_type`. The strategy stamps that `MARKET` when it builds the fan-out draft, so a
        # BRACKETED fan-out leg went out as a MARKET master and this cap was silently ignored —
        # decorative on exactly the orders it was written for (181 of 215 RTH fan-out entries over
        # 14 days carry a bracket).
        # PROVEN LIVE: XHG 08-13 11:43:29 — we sent `limit_price 3.87` and it FILLED AT 3.8873,
        # 45 bps ABOVE our own ceiling, with `bracket_entry_type: MARKET` sitting in the same payload.
        # ⭐ A LIMIT master + attached legs is broker-valid — Probe W shape A, HTTP 200, placed live.
        md["bracket_entry_type"] = "LIMIT"
        md["price_source"] = "ask"
        md["oms_v2_rth_fanout_limit"] = "true"
        md["oms_v2_rth_fanout_limit_ask"] = f"{ask:.4f}"
        md["oms_v2_rth_fanout_limit_cap"] = f"{cap:.4f}"
        md["oms_v2_rth_fanout_limit_anchor"] = f"{anchor:.4f}"
        # ⛔ `reference_price` is NOT overwritten — same reason as the primary path: every slippage
        # study measures fill-vs-DECISION, and overwriting the reference silently turns that into
        # fill-vs-fill and reports ~0. The fan-out leg is the one whose slippage we most want to see.
        self.logger.info(
            "[OMS-V2-RTH-FANOUT-LIMIT] symbol=%s source=%s PLACED ask=%.4f level=%.4f cap=%.4f "
            "limit=%s (was MARKET; the fan-out leg now has a ceiling too)",
            symbol, md.get("fanout_source", "?"), ask, level, cap, limit_s,
        )
        return None

    def _apply_v2_rth_reactive_limit(
        self, *, event: TradeIntentEvent, intent: TradeIntent
    ) -> OrderEventEvent | None:
        """Re-price the RTH reactive v2 entry as a band-capped marketable LIMIT instead of a MARKET.

        Anchor is the strategy's own `entry_price` (the quote that broke the trigger) — the same
        anchor the fan-out leg and the OCO bracket already use, so the entry price the tape records
        does not move. Returns None to PROCEED, or a rejected event to ABANDON.

        ⚠️ THE NEW FAILURE MODE, NAMED: **a market order always fills; this one will sometimes not.**
        That is a real behaviour change and the counterpart to the missed-entry question. Both the
        placement and the abandon are logged so tomorrow's tape answers the frequency directly
        instead of needing another reconstruction."""
        if not self._v2_rth_reactive_limit_applies(event):
            return None
        md = event.payload.metadata
        symbol = str(event.payload.symbol).upper()
        try:
            level = float(md["entry_price"])
        except (KeyError, TypeError, ValueError):
            return None          # no anchor -> leave the order exactly as it is today (MARKET)
        if level <= 0:
            return None
        try:
            band_pct = float(md["resting_band_pct"])
        except (KeyError, TypeError, ValueError):
            band_pct = float(getattr(self.settings, "oms_v2_eh_resting_entry_band_pct", 0.5))
        max_age_ms = int(getattr(self.settings, "oms_v2_eh_resting_entry_quote_max_age_ms", 2000))
        limit_s, a, b = self._band_capped_marketable_limit(
            symbol=symbol, level=level, band_pct=band_pct, max_age_ms=max_age_ms
        )
        if limit_s is None:
            reason_code, reason_detail = a, b
            self.logger.info(
                "[OMS-V2-RTH-REACTIVE-LIMIT] symbol=%s ABANDONED reason=%s — %s",
                symbol, reason_code, reason_detail,
            )
            return self._abandon_v2_eh_entry(
                event=event, intent=intent, reason_code=reason_code, reason_detail=reason_detail,
            )
        ask, cap = a, b
        md["order_type"] = "limit"
        md["limit_price"] = limit_s
        md["price_source"] = "ask"
        md["oms_v2_rth_reactive_limit"] = "true"
        md["oms_v2_rth_reactive_limit_ask"] = f"{ask:.4f}"
        md["oms_v2_rth_reactive_limit_cap"] = f"{cap:.4f}"
        # ⛔ `reference_price` is NOT overwritten. The EH pricer sets it to the limit; here the
        # strategy's own trigger price must remain the recorded reference, or every slippage study
        # that measures fill-vs-decision would silently start measuring fill-vs-fill and report ~0.
        # THIS IS THE ONE DELIBERATE RTH/EH DIVERGENCE.
        self.logger.info(
            "[OMS-V2-RTH-REACTIVE-LIMIT] symbol=%s PLACED ask=%.4f level=%.4f cap=%.4f limit=%s "
            "(was MARKET; price is now capped)",
            symbol, ask, level, cap, limit_s,
        )
        return None

    def _v2_eh_resting_entry_applies(self, event: TradeIntentEvent) -> bool:
        """Gate for the EH RESTING-entry band-cap re-price (P-B2). Only the flag-on v2 EH resting open
        (metadata eh_resting=true) qualifies. The strategy software-emulates the resting cross in EH and
        emits a MARKETABLE open tagged eh_resting; this builder re-prices it off the OMS's OWN fresh ask
        and band-caps it. RTH / flag-off / non-v2 / the reactive entry / the RTH broker STOP_LIMIT are all
        no-ops -> byte-identical. Shares one env switch with the strategy (like confirmed_window_enabled)."""
        if not bool(getattr(self.settings, "strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled", False)):
            return False
        p = event.payload
        md = p.metadata
        return (
            p.strategy_code == "schwab_1m_v2"
            and p.intent_type == "open"
            and p.side == "buy"
            and str(md.get("eh_resting", "")).lower() == "true"
            and not _is_regular_market_session()
        )

    def _apply_v2_eh_resting_entry(
        self,
        *,
        session: Session,
        event: TradeIntentEvent,
        intent: TradeIntent,
    ) -> OrderEventEvent | None:
        """Re-price the v2 EH RESTING entry as a marketable, band-capped EH-LIMIT off the OMS's OWN fresh
        ask (`_latest_quotes_by_symbol`, the ONLY feed the Webull mirror can price from — no Webull market-
        data entitlement). limit = min(ask, level*(1+band)); ABANDON if the ask has gapped past the band
        (no-chase) or there is no fresh ask (no blind order) — the SAME conservative bias as the EH reactive
        entry and the ORB quote-priced entry. This is the software emulation of the RTH broker buy-stop-limit
        (which triggers at the level and fills up to level*(1+band), missing a gap-through) for extended
        hours where a broker stop is dead.

        Returns None to PROCEED (after mutating event.payload.metadata's limit) or a rejected event to
        ABANDON (short-circuit before any broker submit). No-op (returns None, no mutation) when the flag is
        off / not a v2 EH resting open -> byte-identical. ``session`` is unused (abandon marks the intent in
        the caller's open session); kept for symmetry with the other pre-submit helpers."""
        del session
        if not self._v2_eh_resting_entry_applies(event):
            return None
        md = event.payload.metadata
        symbol = str(event.payload.symbol).upper()
        session_code = _extended_hours_session()  # "AM"/"PM" (applies() already confirmed EH)
        # The resting level = the ATR line the cross fired at = the band anchor. Mandatory (fail-closed):
        # without it we cannot bound the fill, and the EH resting draft always carries it.
        try:
            level = float(md.get("resting_level") or md["entry_price"])
        except (KeyError, TypeError, ValueError):
            return self._abandon_v2_eh_entry(
                event=event, intent=intent, reason_code="MISSING_SIGNAL",
                reason_detail="resting_level/entry_price absent/invalid; cannot band-cap the EH resting entry",
            )
        if level <= 0:
            return self._abandon_v2_eh_entry(
                event=event, intent=intent, reason_code="MISSING_SIGNAL",
                reason_detail=f"resting level non-positive ({level})",
            )
        # Band = the strategy's own band (single source, passed in metadata); the setting is the fallback belt.
        try:
            band_pct = float(md["resting_band_pct"])
        except (KeyError, TypeError, ValueError):
            band_pct = float(getattr(self.settings, "oms_v2_eh_resting_entry_band_pct", 0.5))
        max_age_ms = int(getattr(self.settings, "oms_v2_eh_resting_entry_quote_max_age_ms", 2000))
        # ⛔ Arithmetic EXTRACTED to `_band_capped_marketable_limit` (2026-08-10) so the RTH reactive
        # limit shares ONE implementation with this one. Behaviour here is unchanged — same cap, same
        # ask, same ROUND_DOWN tick alignment, same two abandon reasons — and this path's tests still
        # pin it. Reuse beats a second implementation that is free to drift.
        limit_s, ask, cap = self._band_capped_marketable_limit(
            symbol=symbol, level=level, band_pct=band_pct, max_age_ms=max_age_ms
        )
        if limit_s is None:
            reason_code, reason_detail = ask, cap
            return self._abandon_v2_eh_entry(
                event=event, intent=intent, reason_code=reason_code, reason_detail=reason_detail,
            )
        md["order_type"] = "limit"
        md["limit_price"] = limit_s
        md["reference_price"] = limit_s
        md["session"] = session_code
        md["extended_hours"] = "true"
        md["price_source"] = "ask"
        md["oms_v2_eh_resting_entry"] = "true"
        md["oms_v2_eh_resting_entry_ask"] = f"{ask:.4f}"
        md["oms_v2_eh_resting_entry_cap"] = f"{cap:.4f}"
        self.logger.info(
            "[OMS-V2-EH-RESTING] symbol=%s session=%s ask=%.4f level=%.4f cap=%.4f limit=%s",
            symbol, session_code, ask, level, cap, limit_s,
        )
        return None

    def _build_rejected_event(
        self,
        intent_event: TradeIntentEvent,
        intent_db_id: UUID,
        *,
        reason: str = "risk_rejected",
    ) -> OrderEventEvent:
        client_order_id = (
            intent_event.payload.metadata.get("target_client_order_id")
            or self._build_client_order_id(intent_event)
        )
        return OrderEventEvent(
            source_service=SERVICE_NAME,
            correlation_id=intent_event.event_id,
            payload=OrderEventPayload(
                intent_event_id=intent_event.event_id,
                intent_db_id=intent_db_id,
                order_db_id=None,
                strategy_code=intent_event.payload.strategy_code,
                broker_account_name=intent_event.payload.broker_account_name,
                client_order_id=client_order_id,
                broker_order_id=None,
                broker_fill_id=None,
                symbol=intent_event.payload.symbol,
                side=intent_event.payload.side,
                intent_type=intent_event.payload.intent_type,
                status="rejected",
                quantity=intent_event.payload.quantity,
                filled_quantity=Decimal("0"),
                fill_price=None,
                reason=reason,
                metadata=dict(intent_event.payload.metadata),
            ),
        )

    def _session_symbol_block_key(self, *, account_name: str, symbol: str, session_date: str | None = None) -> str:
        day = session_date or datetime.now(SESSION_TZ).date().isoformat()
        safe_account = account_name.replace(":", "_")
        return f"{self.settings.redis_stream_prefix}:symbol-block:{day}:{safe_account}:{symbol.upper()}"

    def _seconds_until_session_end(self) -> int:
        now = datetime.now(SESSION_TZ)
        tomorrow = (now + timedelta(days=1)).date()
        next_midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=SESSION_TZ)
        return max(60, int((next_midnight - now).total_seconds()))

    @staticmethod
    def _current_session_day(now: datetime | None = None) -> str:
        return session_day_eastern_str(now or utcnow())

    async def _get_session_symbol_block_reason(self, *, account_name: str, symbol: str) -> str | None:
        getter = getattr(self.redis, "get", None)
        if getter is None:
            return None
        value = await getter(self._session_symbol_block_key(account_name=account_name, symbol=symbol))
        return str(value) if value else None

    async def _set_session_symbol_block(self, *, account_name: str, symbol: str, reason: str) -> None:
        setter = getattr(self.redis, "set", None)
        if setter is None:
            return
        await setter(
            self._session_symbol_block_key(account_name=account_name, symbol=symbol),
            reason,
            ex=self._seconds_until_session_end(),
        )

    async def _refresh_broker_position_quantity(
        self,
        *,
        session: Session,
        broker_account_id: UUID,
        broker_account_name: str,
        symbol: str,
    ) -> Decimal:
        try:
            snapshots = await self.broker_adapter.list_account_positions(broker_account_name)
        except Exception as exc:
            self.logger.warning(
                "failed broker position refresh before exit recheck for %s %s: %s",
                broker_account_name,
                symbol,
                exc,
            )
            return Decimal("0")

        self.store.sync_account_positions(
            session,
            broker_account_id=broker_account_id,
            snapshots=snapshots,
        )
        refreshed_position = self.store.get_account_position(
            session,
            broker_account_id=broker_account_id,
            symbol=symbol,
        )
        if refreshed_position is None or refreshed_position.quantity <= 0:
            return Decimal("0")
        return refreshed_position.quantity

    async def _record_order_reports(
        self,
        *,
        session: Session,
        intent,
        strategy_id: UUID,
        broker_account_id: UUID,
        intent_event: TradeIntentEvent,
        request: OrderRequest,
        reports: list[ExecutionReport],
    ) -> list[OrderEventEvent]:
        published_events: list[OrderEventEvent] = []
        for report in reports:
            order = self.store.get_or_create_order(
                session,
                intent=intent,
                strategy_id=strategy_id,
                broker_account_id=broker_account_id,
                client_order_id=request.client_order_id,
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                metadata=dict(request.metadata),
                broker_order_id=report.broker_order_id,
                status=report.event_type,
                order_type=request.order_type,
                time_in_force=request.time_in_force,
                reject_reason=report.reason if report.event_type == "rejected" else None,
            )
            recorded_metadata = carry_fanout_identity(
                report.metadata, request.metadata
            )
            payload = {
                "client_order_id": report.client_order_id,
                "broker_order_id": report.broker_order_id,
                "broker_fill_id": report.broker_fill_id,
                "metadata": recorded_metadata,
                "reason": report.reason,
            }
            # ⛔⭐⭐ Q12/§183 — THE LEDGER WRITES GO FIRST, AND THE AUDIT WRITE IS ISOLATED.
            # This ordering used to be the other way round, and because every caller of this path
            # swallows `Exception`, a failing audit row silently took the FILL and the POSITION
            # UPDATE with it. Recording WHAT HAPPENED must never be gated on recording THAT it
            # happened. Two independent protections, deliberately both:
            #   1. order   — the fill and position writes no longer sit downstream of the audit row;
            #   2. savepoint — the audit row cannot abort the transaction they share.
            # Either alone would leave a hole: reordering does not stop a failed flush poisoning
            # the transaction for everything AFTER it, and the savepoint alone would still leave
            # the ledger downstream of a call that can fail for reasons we have not thought of.
            fill = self.store.record_fill_if_needed(
                session,
                order=order,
                strategy_id=strategy_id,
                broker_account_id=broker_account_id,
                report=report,
                payload=payload,
            )
            self._append_order_event_isolated(
                session, order=order, report=report, payload=payload
            )
            if fill is not None:
                self.store.apply_fill_to_positions(
                    session,
                    strategy_id=strategy_id,
                    broker_account_id=broker_account_id,
                    symbol=request.symbol,
                    side=request.side,
                    quantity=fill.quantity,
                    price=fill.price,
                    reported_at=fill.filled_at,
                )
                self._update_hard_stop_registry_from_fill(
                    strategy_code=intent_event.payload.strategy_code,
                    broker_account_name=intent_event.payload.broker_account_name,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=fill.quantity,
                    price=fill.price,
                    metadata=dict(request.metadata),
                )
                published_events.extend(
                    await self._manage_native_stop_after_fill(
                        session=session,
                        strategy_id=strategy_id,
                        broker_account_id=broker_account_id,
                        strategy_code=intent_event.payload.strategy_code,
                        broker_account_name=intent_event.payload.broker_account_name,
                        symbol=request.symbol,
                        side=request.side,
                        intent_type=request.intent_type,
                        metadata=dict(request.metadata),
                    )
                )
                self._apply_managed_position_after_fill(
                    session=session,
                    strategy_code=intent_event.payload.strategy_code,
                    broker_account_name=intent_event.payload.broker_account_name,
                    symbol=request.symbol,
                    side=request.side,
                    intent_type=request.intent_type,
                    quantity=fill.quantity,
                    price=fill.price,
                    metadata=dict(request.metadata),
                    entry_client_order_id=str(request.client_order_id or ""),
                )

            self.store.mark_intent_from_report(intent, report)
            self._update_hard_stop_registry_from_order_status(
                strategy_code=intent_event.payload.strategy_code,
                broker_account_name=intent_event.payload.broker_account_name,
                symbol=request.symbol,
                metadata=dict(request.metadata),
                status=report.event_type,
                reason=report.reason,
            )
            if report.event_type == "rejected" and self._is_schwab_ineligible_reason(report.reason):
                self.store.record_schwab_ineligible_entry(
                    session,
                    broker_account_id=broker_account_id,
                    symbol=request.symbol,
                    session_date=self._current_session_day(report.reported_at),
                    reason_text=report.reason or "",
                    first_seen_at=report.reported_at,
                )
            # Dual-broker fan-out: symmetric Webull ineligible-today cache. Only a CLEAR
            # not-tradable Webull reject (never 429/transient — the classifier vetoes those) on a
            # Webull-provider account marks the name ineligible for the day. Discovery is still
            # learn-by-failing; under fan-out the Schwab leg fired in parallel so the discovery
            # trade is not lost. Byte-identical when nothing routes to a Webull account.
            if (
                report.event_type == "rejected"
                and bool(getattr(self.settings, "strategy_schwab_1m_v2_dual_broker_fanout_enabled", False))
                and self._is_webull_ineligible_reason(report.reason)
                and self.settings.provider_for_account(
                    intent_event.payload.broker_account_name
                )
                == "webull"
            ):
                self.store.record_webull_ineligible_entry(
                    session,
                    broker_account_id=broker_account_id,
                    symbol=request.symbol,
                    session_date=self._current_session_day(report.reported_at),
                    reason_text=report.reason or "",
                    first_seen_at=report.reported_at,
                )
            if report.event_type == "rejected" and self._is_not_tradable_reason(report.reason):
                await self._set_session_symbol_block(
                    account_name=intent_event.payload.broker_account_name,
                    symbol=intent_event.payload.symbol,
                    reason="broker_symbol_not_tradable_for_session",
                )

            published_events.append(
                self._build_order_event(
                    intent_event=intent_event,
                    intent_db_id=intent.id,
                    order_db_id=order.id,
                    report=report,
                    client_order_id=request.client_order_id,
                    symbol=request.symbol,
                    side=request.side,
                    quantity=request.quantity,
                )
            )
        return published_events

    def _should_refresh_working_order(self, order: BrokerOrder) -> bool:
        refresh_after = self._refresh_after_seconds(order)
        last_activity = order.updated_at or order.submitted_at
        if last_activity is None:
            return True
        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=UTC)
        return (utcnow() - last_activity).total_seconds() >= refresh_after

    # ----- Stuck-intent cancellation (2026-05-18 incident) ----------------
    # AUUD/QNCX/SBFM pre-market intents at 09:27 ET kept retrying for 4.5
    # hours and 400+ attempts each. Three guards stop that:
    #   Tier 1 (quote-driven): _cancel_drifted_working_orders cancels a
    #          working limit on the very next quote tick when the ask
    #          (buy) / bid (sell) has moved past the limit by more than
    #          the configured tolerance. Fires within ms of the quote
    #          update; no retry.
    #   Tier 2 (age cap): _intent_too_old marks an intent as abandoned
    #          once it has been open longer than
    #          oms_intent_max_age_seconds (default 30s). Belt-and-braces
    #          for stocks that stop quoting entirely.
    #   Tier 3 (setup revalidation): _intent_setup_invalid_reason checks
    #          strategy_bar_history for the latest bar of the intent's
    #          symbol+strategy; if the bar is no longer status=signal
    #          with the same path, the intent is abandoned. Prevents
    #          buying on a setup that has expired since the original
    #          intent fired.

    def _intent_max_age_secs(self) -> int:
        return max(0, int(getattr(self.settings, "oms_intent_max_age_seconds", 0) or 0))

    def _quote_drift_tolerance_dollars(self) -> float:
        return max(
            0.0,
            float(getattr(self.settings, "oms_quote_drift_cancel_tolerance_cents", 0.0) or 0.0),
        ) / 100.0

    @staticmethod
    def _normalize_intent_created_at(intent: TradeIntent) -> datetime | None:
        created = intent.created_at
        if created is None:
            return None
        return created if created.tzinfo is not None else created.replace(tzinfo=UTC)

    def _intent_age_secs(self, intent: TradeIntent) -> float:
        created = self._normalize_intent_created_at(intent)
        if created is None:
            return 0.0
        return max(0.0, (utcnow() - created).total_seconds())

    def _intent_too_old(self, intent: TradeIntent) -> bool:
        max_age = self._intent_max_age_secs()
        if max_age <= 0:
            return False
        return self._intent_age_secs(intent) > max_age

    def _intent_path(self, intent: TradeIntent) -> str:
        payload = intent.payload if isinstance(intent.payload, dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return str(metadata.get("path", "")).strip()

    def _intent_setup_invalid_reason(
        self,
        session: Session,
        *,
        intent: TradeIntent,
        strategy: Strategy | None,
    ) -> str | None:
        if not bool(getattr(self.settings, "oms_intent_setup_revalidation_enabled", True)):
            return None
        if str(intent.intent_type).lower() != "open":
            return None
        if strategy is None:
            return None
        intent_path = self._intent_path(intent)
        if not intent_path:
            return None
        record = session.scalar(
            select(StrategyBarHistory)
            .where(
                StrategyBarHistory.strategy_code == strategy.code,
                StrategyBarHistory.symbol == intent.symbol,
            )
            .order_by(StrategyBarHistory.bar_time.desc())
            .limit(1)
        )
        if record is None:
            return None
        decision_status = str(record.decision_status or "").strip()
        decision_path = str(record.decision_path or "").strip()
        if not decision_status:
            # FAIL OPEN: the strategy records no decision tape. The isolated
            # schwab_1m_v2 bot persists OHLCV bars but never writes
            # decision_status/decision_path, so this revalidation can only judge
            # tape-writing strategies (the momentum bots it was built for). For a
            # tape-less strategy every bar reads as 'idle' != 'signal', which made
            # this guard ABANDON every v2 ATR-Flip intent that did not fill
            # instantly — i.e. all after-hours fills (thin liquidity -> the order
            # reaches the cancel-and-replace cycle -> SETUP_INVALID). We cannot
            # revalidate what isn't recorded, so do NOT abandon a good order.
            return None
        if decision_status == "signal" and decision_path == intent_path:
            return None
        bar_et = record.bar_time.astimezone(SESSION_TZ).strftime("%H:%M:%S") if record.bar_time else "?"
        return (
            f"latest bar {bar_et} ET status={decision_status or 'idle'} "
            f"path={decision_path or 'none'} != intent path={intent_path}"
        )

    def _quote_drift_dollars_against(
        self,
        order: BrokerOrder,
        quote: dict[str, object],
    ) -> float | None:
        if self._is_stop_guard_order(order):
            return None
        payload = order.payload or {}
        if str(payload.get("order_type", "")).strip().lower() != "limit":
            return None
        try:
            limit_price = float(str(payload.get("limit_price", "")).strip())
        except (TypeError, ValueError):
            return None
        if limit_price <= 0:
            return None
        side = str(order.side).lower()
        if side == "buy":
            ask = quote.get("ask")
            if not isinstance(ask, (int, float)) or ask <= 0:
                return None
            return float(ask) - limit_price
        if side == "sell":
            bid = quote.get("bid")
            if not isinstance(bid, (int, float)) or bid <= 0:
                return None
            return limit_price - float(bid)
        return None

    def _log_direct_cancel_dead_target_bound(
        self,
        *,
        symbol: str,
        broker_account_name: str,
        target_order_id: UUID,
        client_order_id: str,
        terminal_cancel_reports: int,
        path: str,
    ) -> None:
        """Log one edge per (target, path, process) when a direct path reaches its budget.

        Unlike #829's strategy-intent marker, this edge is deduplicated and cannot be counted
        against that marker's per-refusal emissions. The common field names only make either
        population independently queryable.
        """
        key = (target_order_id, path)
        logged = self.__dict__.setdefault("_direct_cancel_dead_target_bound_logged", set())
        if key in logged:
            return
        logged.add(key)
        self.logger.warning(
            "[OMS-DIRECT-CANCEL-DEAD-TARGET-BOUND] symbol=%s acct=%s target_order_id=%s "
            "target_client_order_id=%s terminal_reports=%d bound=%d reset=new_target_order_id "
            "scope=strategy_internal_direct_cancel path=%s outcome=refused "
            "emission=once_per_target_path_per_process",
            symbol,
            broker_account_name,
            target_order_id,
            client_order_id,
            terminal_cancel_reports,
            self._CANCEL_DEAD_TARGET_BROKER_REPORT_BOUND,
            path,
        )

    def _direct_cancel_dead_target_bound_reached(
        self,
        session: Session,
        *,
        order: BrokerOrder,
        path: str,
    ) -> bool:
        terminal_cancel_reports = self.store.count_terminal_cancel_refusals(
            session,
            order_id=order.id,
        )
        if not self._direct_cancel_dead_target_bound_reached_count(terminal_cancel_reports):
            return False
        account = session.get(BrokerAccount, order.broker_account_id)
        self._log_direct_cancel_dead_target_bound(
            symbol=order.symbol,
            broker_account_name=(
                account.name if account is not None else str(order.broker_account_id)
            ),
            target_order_id=order.id,
            client_order_id=order.client_order_id,
            terminal_cancel_reports=terminal_cancel_reports,
            path=path,
        )
        return True

    def _direct_cancel_dead_target_bound_reached_count(self, count: int) -> bool:
        """Pure threshold shared by every covered strategy-internal direct CANCEL path."""
        return count >= self._CANCEL_DEAD_TARGET_BROKER_REPORT_BOUND

    def _record_direct_cancel_reports(
        self,
        session: Session,
        *,
        order: BrokerOrder,
        reports: list[ExecutionReport],
        existing_metadata: dict[str, str],
        internal: str,
        extra_metadata: dict[str, str] | None = None,
    ) -> ExecutionReport | None:
        """Persist direct-path CANCEL outcomes so the next attempt can read its target budget.

        A rejection does not change the working order's status: it says the CANCEL request failed,
        not that the target ceased to exist. A confirmed cancellation still updates the target as
        before. Other intermediate reports retain their existing behavior and are not invented as
        terminal evidence.
        """
        cancelled_report: ExecutionReport | None = None
        for report in reports:
            if report.event_type not in {"cancelled", "rejected"}:
                continue
            metadata = carry_fanout_identity({
                **existing_metadata,
                **{str(k): str(v) for k, v in report.metadata.items()},
                **(extra_metadata or {}),
            }, existing_metadata)
            if report.event_type == "cancelled":
                cancelled_report = report
                self.store.update_order_from_report(
                    order,
                    report=report,
                    metadata=metadata,
                )
            self._append_order_event_isolated(
                session,
                order=order,
                report=report,
                payload={
                    "client_order_id": report.client_order_id,
                    "broker_order_id": report.broker_order_id,
                    "broker_fill_id": report.broker_fill_id,
                    "metadata": metadata,
                    "reason": report.reason,
                    "internal": internal,
                },
            )
        return cancelled_report

    async def _cancel_working_order_and_abandon_intent(
        self,
        *,
        session: Session,
        order: BrokerOrder,
        intent: TradeIntent,
        strategy: Strategy | None,
        broker_account: BrokerAccount,
        reason_code: str,
        reason_detail: str,
    ) -> list[OrderEventEvent]:
        existing_metadata = {str(k): str(v) for k, v in (order.payload or {}).items()}
        cancel_request = OrderRequest(
            client_order_id=order.client_order_id,
            broker_account_name=broker_account.name,
            strategy_code=strategy.code if strategy is not None else "",
            symbol=order.symbol,
            side=order.side,  # type: ignore[arg-type]
            intent_type="cancel",
            quantity=order.quantity,
            reason=reason_code,
            metadata={
                **existing_metadata,
                "broker_order_id": order.broker_order_id or "",
                "target_client_order_id": order.client_order_id,
                "abandon_intent": "true",
                "abandon_reason_code": reason_code,
                "abandon_reason_detail": reason_detail,
            },
            order_type=order.order_type,
            time_in_force=order.time_in_force,
        )
        if not self._direct_cancel_dead_target_bound_reached(
            session,
            order=order,
            path="cancel_working_order_and_abandon_intent",
        ):
            cancel_reports = await self.broker_adapter.submit_order(cancel_request)
            self._record_direct_cancel_reports(
                session,
                order=order,
                reports=cancel_reports,
                existing_metadata=existing_metadata,
                internal=reason_code,
                extra_metadata={
                    "abandon_intent": "true",
                    "abandon_reason_code": reason_code,
                    "abandon_reason_detail": reason_detail,
                },
            )
        self.store.mark_intent_status(intent, "cancelled")
        self.logger.info(
            "[OMS-ABANDON-INTENT] code=%s symbol=%s strategy=%s side=%s "
            "intent_age_s=%.1f limit=%s reason=%s",
            reason_code,
            order.symbol,
            strategy.code if strategy is not None else "?",
            order.side,
            self._intent_age_secs(intent),
            str((order.payload or {}).get("limit_price", "")),
            reason_detail,
        )
        return []

    async def _cancel_drifted_working_orders(self, symbol: str) -> None:
        """Tier 1: cancel working limit orders the instant the quote drifts past the limit.

        PR-A off-load: the candidate READ and the cancel WRITE-BACK both run OFF the
        event loop via ``_run_db`` — this path mutates no in-memory dict, so it splits
        cleanly (unlike the v2 exit-emit). Only the per-order broker cancel await stays
        on-loop. Broker-agnostic: covers ORB (Webull) and v2 (Schwab) working limits."""
        tolerance_dollars = self._quote_drift_tolerance_dollars()
        if tolerance_dollars <= 0:
            return
        quote = self._latest_quotes_by_symbol.get(symbol.upper())
        if not quote:
            return
        try:
            await self._run_drift_cancel(symbol.upper(), quote, tolerance_dollars)
        except Exception as exc:  # noqa: BLE001 — the quote path must never die; a stall here
            # must NEVER skip the downstream v2 hard-stop eval that runs later in the same
            # quote handler (loop-hardening; the happy path is unchanged).
            self.logger.warning("quote-drift cancel failed for %s: %s", symbol, exc)

    async def _run_drift_cancel(self, symbol: str, quote: dict, tolerance_dollars: float) -> None:
        """The drift-cancel phases (off-loop read -> on-loop broker cancels -> off-loop
        write-back), split out so ``_cancel_drifted_working_orders`` can wrap them in the
        never-die guard. ``symbol`` arrives already upper-cased."""
        # Phase 1 — READ (off-loop): drift-eligible candidates as plain snapshots.
        candidates = await self._run_db(
            lambda session: self._collect_drift_cancel_candidates(
                session, symbol, quote, tolerance_dollars
            ),
            commit=False,
        )
        if not candidates:
            return
        # Phase 2 — BROKER (on-loop): submit each cancel, collect the reports.
        results: list[tuple[_DriftCancelCandidate, list[ExecutionReport], str]] = []
        for candidate in candidates:
            reason_detail = (
                f"quote drift {candidate.drift * 100:.1f}c past limit "
                f"(tolerance {tolerance_dollars * 100:.1f}c); ask/bid moved away"
            )
            cancel_request = OrderRequest(
                client_order_id=candidate.client_order_id,
                broker_account_name=candidate.broker_account_name,
                strategy_code=candidate.strategy_code,
                symbol=candidate.symbol,
                side=candidate.side,  # type: ignore[arg-type]
                intent_type="cancel",
                quantity=candidate.quantity,
                reason="QUOTE_DRIFT_CANCEL",
                metadata={
                    **candidate.existing_metadata,
                    "broker_order_id": candidate.broker_order_id,
                    "target_client_order_id": candidate.client_order_id,
                    "abandon_intent": "true",
                    "abandon_reason_code": "QUOTE_DRIFT_CANCEL",
                    "abandon_reason_detail": reason_detail,
                },
                order_type=candidate.order_type,
                time_in_force=candidate.time_in_force,
            )
            if self._direct_cancel_dead_target_bound_reached_count(
                candidate.terminal_cancel_reports
            ):
                self._log_direct_cancel_dead_target_bound(
                    symbol=candidate.symbol,
                    broker_account_name=candidate.broker_account_name,
                    target_order_id=candidate.order_id,
                    client_order_id=candidate.client_order_id,
                    terminal_cancel_reports=candidate.terminal_cancel_reports,
                    path="run_drift_cancel",
                )
                results.append((candidate, [], reason_detail))
                continue
            cancel_reports = await self.broker_adapter.submit_order(cancel_request)
            results.append((candidate, cancel_reports, reason_detail))
        # Phase 3 — WRITE-BACK (off-loop): record cancels + always abandon the intents.
        await self._run_db(
            lambda session: self._apply_drift_cancel_writes(session, results), commit=True
        )
        # Logging on-loop — parity with the prior [OMS-ABANDON-INTENT] line (always emitted).
        for candidate, _reports, reason_detail in results:
            self.logger.info(
                "[OMS-ABANDON-INTENT] code=%s symbol=%s strategy=%s side=%s "
                "intent_age_s=%.1f limit=%s reason=%s",
                "QUOTE_DRIFT_CANCEL",
                candidate.symbol,
                candidate.strategy_code or "?",
                candidate.side,
                self._drift_candidate_intent_age_secs(candidate),
                candidate.limit_price,
                reason_detail,
            )

    def _collect_drift_cancel_candidates(
        self, session: Session, symbol: str, quote: dict, tolerance_dollars: float
    ) -> list[_DriftCancelCandidate]:
        """Off-loop READ unit: working orders for `symbol` whose quote has drifted past
        the limit beyond tolerance, as plain snapshots (no ORM crosses the thread).
        Mirrors the prior in-line filter exactly: open-intent only; stop-guard / non-limit
        orders are excluded by ``_quote_drift_dollars_against`` returning None."""
        orders = session.scalars(
            select(BrokerOrder)
            .where(BrokerOrder.status.in_(self.store.OPEN_ORDER_STATUSES))
            .where(BrokerOrder.symbol == symbol)
        ).all()
        if not orders:
            return []
        account_lookup = {
            account.id: account for account in self.store.list_active_broker_accounts(session)
        }
        strategy_lookup = {
            strategy.id: strategy for strategy in session.scalars(select(Strategy)).all()
        }
        candidates: list[_DriftCancelCandidate] = []
        for order in orders:
            if order.intent_id is None:
                continue
            drift = self._quote_drift_dollars_against(order, quote)
            if drift is None or drift <= tolerance_dollars:
                continue
            intent = session.get(TradeIntent, order.intent_id)
            if intent is None:
                continue
            if str(intent.intent_type).lower() != "open":
                continue  # don't auto-cancel close/scale chases here
            account = account_lookup.get(order.broker_account_id)
            if account is None:
                continue
            strategy = strategy_lookup.get(order.strategy_id)
            candidates.append(
                _DriftCancelCandidate(
                    order_id=order.id,
                    intent_id=order.intent_id,
                    client_order_id=order.client_order_id,
                    broker_account_name=account.name,
                    strategy_code=(strategy.code if strategy is not None else ""),
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    order_type=order.order_type,
                    time_in_force=order.time_in_force,
                    existing_metadata={str(k): str(v) for k, v in (order.payload or {}).items()},
                    broker_order_id=order.broker_order_id or "",
                    limit_price=str((order.payload or {}).get("limit_price", "")),
                    intent_created_at=intent.created_at,
                    drift=drift,
                    terminal_cancel_reports=self.store.count_terminal_cancel_refusals(
                        session,
                        order_id=order.id,
                    ),
                )
            )
        return candidates

    def _apply_drift_cancel_writes(
        self,
        session: Session,
        results: list[tuple[_DriftCancelCandidate, list[ExecutionReport], str]],
    ) -> None:
        """Off-loop WRITE unit: for each drift-cancel candidate, record the broker cancel
        report (when one was returned) and ALWAYS abandon the intent — byte-for-byte the
        DB writes the prior ``_cancel_working_order_and_abandon_intent`` performed, minus
        its (now on-loop) broker await and logging. Re-fetches order/intent by id."""
        for candidate, reports, reason_detail in results:
            order = session.get(BrokerOrder, candidate.order_id)
            intent = session.get(TradeIntent, candidate.intent_id)
            if intent is None:
                continue
            if order is not None:
                self._record_direct_cancel_reports(
                    session,
                    order=order,
                    reports=reports,
                    existing_metadata=candidate.existing_metadata,
                    internal="QUOTE_DRIFT_CANCEL",
                    extra_metadata={
                        "abandon_intent": "true",
                        "abandon_reason_code": "QUOTE_DRIFT_CANCEL",
                        "abandon_reason_detail": reason_detail,
                    },
                )
            self.store.mark_intent_status(intent, "cancelled")

    def _drift_candidate_intent_age_secs(self, candidate: _DriftCancelCandidate) -> float:
        created = candidate.intent_created_at
        if created is None:
            return 0.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return max(0.0, (utcnow() - created).total_seconds())

    def _refresh_after_seconds(self, order: BrokerOrder) -> float:
        if self._is_stop_guard_order(order):
            stage = self._stop_guard_refresh_stage(order.payload or {})
            if stage <= 0:
                return max(0.1, float(self.settings.oms_stop_guard_refresh_stage_1_seconds))
            if stage == 1:
                return max(0.1, float(self.settings.oms_stop_guard_refresh_stage_2_seconds))
            return max(0.1, float(self.settings.oms_stop_guard_refresh_stage_3_seconds))
        return max(1.0, float(self.settings.oms_working_order_refresh_seconds))

    @staticmethod
    def _is_stop_guard_order(order: BrokerOrder) -> bool:
        payload = order.payload or {}
        return str(payload.get("stop_guard", "")).strip().lower() == "true"

    def _resting_trigger_refresh_exempt(self, order: BrokerOrder) -> bool:
        """True when the working-order refresh must LEAVE a resting buy STOP/STOP_LIMIT ENTRY trigger
        in place instead of cancel/replacing it. Default (oms_refresh_resting_trigger_orders=False) =
        exempt, so the resting entry actually sits at the ATR line (the strategy's STABLE-REST owns
        re-pricing; MARKET_CLOSED still parks it out-of-session). Excludes protective stop-guards —
        those are also STOP-typed but MUST keep their staged re-arm cadence — and marketable chases."""
        return (
            self._is_resting_trigger_order(order)
            and not self._is_stop_guard_order(order)
            and not bool(getattr(self.settings, "oms_refresh_resting_trigger_orders", False))
        )

    def _p0a_decline_reason(self, order: BrokerOrder, *, bid: float | None) -> str | None:
        """WHY `_managed_exit_refresh_exempt` said no. Returns None when it said yes.

        ⛔⭐ DIAGNOSTIC ONLY. This must NEVER gate behaviour — `_managed_exit_refresh_exempt`
        remains the single authority for the hold decision. This function exists solely to make a
        FALSE legible, because `[OMS-P0A-HOLD]` sitting at zero lines is ambiguous between at least
        five different worlds and we spent a week unable to tell them apart.

        ⛔ It duplicates the predicate's structure, so the two CAN drift. That is pinned by
        `test_p0a_decline_reason_matches_predicate`, which asserts
        `(_p0a_decline_reason(...) is None) == _managed_exit_refresh_exempt(...)` across a matrix of
        inputs. If you edit one, that test fails until you edit the other. Do not delete it.
        [[feedback_authoritative_for_a_is_not_for_b]] — two sources for one question is exactly the
        bug class; the test is what keeps this pair honest.
        """
        if not bool(getattr(self.settings, "oms_hold_marketable_managed_exit", True)):
            return "flag_off"
        payload = order.payload or {}
        if str(payload.get("oms_v2_managed_exit", "")).strip().lower() != "true":
            return "not_managed_exit"
        if str(payload.get("order_type", "")).strip().upper() != "LIMIT":
            return "not_limit"
        try:
            limit_price = float(payload.get("limit_price"))
        except (TypeError, ValueError):
            return "no_limit_price"
        if limit_price <= 0.0:
            return "no_limit_price"
        try:
            bid_f = float(bid) if bid is not None else 0.0
        except (TypeError, ValueError):
            return "no_bid"
        if bid_f <= 0.0:
            return "no_bid"
        if limit_price > bid_f:
            return "not_marketable"
        return None

    def _append_order_event_isolated(
        self,
        session: Session,
        *,
        order: BrokerOrder,
        report: ExecutionReport,
        payload: dict[str, object],
    ) -> bool:
        """Write the audit row WITHOUT letting its failure reach the ledger writes. Q12/§183.

        ⛔⭐⭐ WHY A SAVEPOINT AND NOT A `try/except` (this is the whole fix).
        A bare try/except does NOT work here and would ship as a fix that isn't one.
        `append_order_event` ends in `session.flush()`; when that flush fails at the DATABASE
        level, Postgres marks the whole transaction ABORTED and refuses every subsequent
        statement on it with `InFailedSqlTransaction`. Catching the exception leaves the session
        exactly as poisoned as before, so `record_fill_if_needed` and `apply_fill_to_positions`
        would still fail — silently, one line further down. The identical cascade was diagnosed
        in the seed-gap work: one timeout turning three later lookups into "failures".
        ⇒ `begin_nested()` issues a SAVEPOINT. A failure rolls back to it and leaves the outer
        transaction USABLE, which is the only property that makes the ledger writes survivable.

        ⛔ AND IT MUST NOT BE SILENT. "Swallowing it more politely" leaves the same silence that
        made §183 invisible for as long as it was. A dropped audit row is COUNTED, and counted
        against the number of attempts, because a bare `failures=0` cannot be told apart from a
        counter that never ran.

        Returns True when the audit row was written, False when it was dropped. The caller is
        expected to CARRY ON either way -- the return value is for tests and for callers that
        want to note the degradation, never a gate on recording what happened.
        """
        self._order_event_attempts += 1
        try:
            with session.begin_nested():
                self.store.append_order_event(session, order=order, report=report, payload=payload)
            return True
        except Exception:  # noqa: BLE001 - an audit row must never cost a fill
            self._order_event_failures += 1
            self.logger.error(
                "[OMS-ORDER-EVENT-DROPPED] %s %s coid=%s status=%s — the audit row was NOT "
                "written; the SAVEPOINT was rolled back and the FILL + POSITION writes continue. "
                "n=%d dropped of %d attempts since boot. ⛔ This is real data loss in "
                "`broker_order_events` (reject reasons, event_source): every reject count over a "
                "window containing this line is UNDER-counted, and the gap is invisible in the "
                "table itself.",
                getattr(order, "symbol", "?"),
                getattr(report, "event_type", "?"),
                getattr(report, "client_order_id", "?"),
                getattr(report, "event_type", "?"),
                self._order_event_failures,
                self._order_event_attempts,
                exc_info=True,
            )
            return False

    def _maybe_emit_order_event_census(self, *, interval_seconds: float = 300.0) -> None:
        """Periodic rollup of audit-row writes. Emits at ZERO failures, deliberately.

        ⭐ Same discipline as `[OMS-P0A-CENSUS]` and the seed-gap census: a counter that only
        speaks when it has something to report cannot be told apart from a counter that stopped
        running, and `failures=0` is the success criterion here — which is exactly the shape that
        makes a broken watch and a healthy system print the same number.
        ⛔ `attempts` is the DENOMINATOR and is read FIRST:
             attempts=0 failures=0  -> no order event occurred. Nothing to conclude.
             attempts>0 failures=0  -> every audit row landed. THAT is the pass.
        """
        try:
            now = utcnow()
            last = self._order_event_census_last_emit
            if last is not None and (now - last).total_seconds() < interval_seconds:
                return
            self._order_event_census_last_emit = now
            self.logger.info(
                "[OMS-ORDER-EVENT-CENSUS] audit rows: %d dropped of %d attempted since boot "
                "(⛔ read attempts FIRST: attempts=0 means UNMEASURED, not clean). A drop costs "
                "the audit row only — the fill and the position update are no longer gated on it.",
                self._order_event_failures,
                self._order_event_attempts,
            )
        except Exception:  # noqa: BLE001 - bookkeeping must never break the sync path
            pass

    def _maybe_emit_broker_read_census(self, *, interval_seconds: float = 300.0) -> None:
        """Periodic per-account rollup of broker positions reads. Emits at ZERO failures.

        ⭐ Same discipline as `[OMS-P0A-CENSUS]` and the seed-gap census: a counter that only
        speaks when it has something to report cannot be told apart from one that stopped
        running — and here `failed=0` is the healthy state, which is exactly the shape where a
        broken watch and a working system print the same number.

        ⛔ `ok` IS THE DENOMINATOR AND IS READ FIRST:
             ok=0 failed=0  -> the sync did not run this window. UNMEASURED, not healthy.
             ok>0 failed=0  -> reads are landing. THAT is the pass.
        ⛔ Per ACCOUNT, never summed. Webull and Schwab fail independently and for different
        reasons; a fleet total would hide one venue being blind behind the other being fine —
        the same mistake as a Schwab-vs-Webull comparison that differences the two.
        """
        try:
            now = utcnow()
            last = self._broker_read_census_last_emit
            if last is not None and (now - last).total_seconds() < interval_seconds:
                return
            self._broker_read_census_last_emit = now
            _ok = self.__dict__.setdefault("_broker_read_ok", {})
            _failed = self.__dict__.setdefault("_broker_read_failed", {})
            _runs = self.__dict__.setdefault("_broker_read_consecutive_failures", {})
            accounts = sorted(set(_ok) | set(_failed))
            if not accounts:
                self.logger.info(
                    "[BROKER-SYNC-CENSUS] no account was read this window — UNMEASURED, not clean."
                )
                return
            parts = []
            for acct in accounts:
                parts.append(
                    f"{acct}: ok={_ok.get(acct, 0)} "
                    f"failed={_failed.get(acct, 0)} "
                    f"consecutive_now={_runs.get(acct, 0)}"
                )
            self.logger.info(
                "[BROKER-SYNC-CENSUS] %s (⛔ read `ok` FIRST: ok=0 means the sync did not run, "
                "not that reads are healthy). `consecutive_now` is the live run length for that "
                "account — a run boundary is now a FACT on this tape, never inferred from a gap.",
                " | ".join(parts),
            )
            self._broker_read_ok = {}
            self._broker_read_failed = {}
        except Exception:  # noqa: BLE001 - bookkeeping must never break the sync path
            pass

    def _p0a_census_note(self, key: str) -> None:
        """Count one P0a evaluation outcome. Diagnostic only; never raises."""
        try:
            c = self.__dict__.setdefault("_p0a_census", {})
            c[key] = c.get(key, 0) + 1
        except Exception:  # noqa: BLE001 - bookkeeping must never break the protective sync
            pass

    def _p0a_census_note_submitted(self) -> None:
        """Count one managed exit EMITTED — the census denominator.

        ⛔ Deliberately a SEPARATE counter, not a `_p0a_census` key: `evaluated` is
        `sum(_p0a_census.values())`, so folding submissions in there would inflate the very number
        it exists to qualify. Diagnostic only; never raises, never gates.
        """
        try:
            self._p0a_census_submitted = int(self.__dict__.get("_p0a_census_submitted", 0)) + 1
        except Exception:  # noqa: BLE001 - bookkeeping must never break the exit path
            pass

    def _maybe_emit_p0a_census(self, *, interval_seconds: float = 300.0) -> None:
        """Periodic rollup of P0a evaluations.

        ⭐⭐ THE WHOLE POINT: **this emits even when `evaluated=0`.** A census that only speaks when
        it has something to say reproduces the exact failure it exists to cure — silence that could
        mean "nothing qualified" or "the code never ran". `evaluated=0` is a RESULT and must appear
        on the tape.

        ⭐ Why a rollup and not a line per decline: this sits on the periodic order sync, so a
        per-evaluation line would emit per working order per tick. That is the trade-coach
        retry-storm shape (45% CPU while nominally disabled). Edge-triggered HOLD/RELEASED lines
        stay as they are; the census carries the volume.
        """
        try:
            now = utcnow()
            last = self.__dict__.get("_p0a_census_last_emit")
            if last is not None and (now - last).total_seconds() < interval_seconds:
                return
            self._p0a_census_last_emit = now
            c = dict(self.__dict__.setdefault("_p0a_census", {}))
            self._p0a_census = {}
            submitted = int(self.__dict__.get("_p0a_census_submitted", 0))
            self._p0a_census_submitted = 0
            evaluated = sum(c.values())
            held = c.pop("held", 0)
            declines = " ".join(f"{k}={v}" for k, v in sorted(c.items())) or "-"
            # ⭐ READ `submitted` FIRST. It is the denominator, and it is what separates the two
            # worlds `evaluated=0` collapses together:
            #   submitted=0 evaluated=0  -> no managed exit occurred. Nothing to conclude.
            #   submitted>0 evaluated=0  -> exits occurred and NONE was ever seen working at a sync
            #                               tick => P0a is UNREACHABLE on this population, not idle.
            self.logger.info(
                "[OMS-P0A-CENSUS] window=%.0fs submitted=%d evaluated=%d held=%d declined: %s",
                interval_seconds, submitted, evaluated, held, declines,
            )
        except Exception:  # noqa: BLE001
            pass

    def _log_p0a_hold_edge(self, order: BrokerOrder, *, bid: float | None) -> None:
        """Log the P0a hold ENGAGING, once per order, never per 5s tick.

        ⛔ Edge-triggered on purpose. The refresh loop re-evaluates every working order every ~5s,
        so a level-triggered log would emit ~12 lines/min/order and the signal would drown in its
        own volume — the `[SCHWAB30-REVISE-STORM]` failure mode. One line when the hold takes the
        order off the refresh path, one when it hands it back (`_log_p0a_hold_release`), and the
        duration between them is the evidence that the exit rested instead of churning."""
        # setdefault, not attribute access: __new__-constructed test instances (and any partially
        # constructed service) lack the dict, and a missing log-only attribute must never raise on
        # the working-order path. Same getattr-guard convention as _native_oco_armed_confirmed_at.
        held = self.__dict__.setdefault("_p0a_held_orders", {})
        if order.id in held:
            return
        held[order.id] = (datetime.now(UTC), order.symbol)
        payload = order.payload or {}
        self.logger.info(
            "[OMS-P0A-HOLD] %s ENGAGED limit=%s bid=%s session=%s — exit is marketable, holding it "
            "on the book instead of cancel/replacing on the refresh cadence (KUST 2026-07-31)",
            order.symbol, payload.get("limit_price"), bid, payload.get("session") or "NORMAL",
        )

    def _log_p0a_hold_release(self, order: BrokerOrder, *, bid: float | None) -> None:
        """Log the hold HANDING BACK to the refresh path, with how long it held.

        A release is NOT a failure — it is the exemption working as specified: once the bid falls
        below the limit the order cannot fill where it sits, so the refresh must reprice it.
        Holding forever would trade the KUST churn for a stale exit that never adjusts, which is
        the same bug facing the other way."""
        started = self.__dict__.setdefault("_p0a_held_orders", {}).pop(order.id, None)
        if started is None:
            return
        secs = (datetime.now(UTC) - started[0]).total_seconds()
        self.logger.info(
            "[OMS-P0A-HOLD-RELEASED] %s held %.1fs then released to the refresh (limit=%s bid=%s) — "
            "the bid fell through the limit; repricing is correct here, not churn",
            order.symbol, secs, (order.payload or {}).get("limit_price"), bid,
        )

    def _managed_exit_refresh_exempt(self, order: BrokerOrder, *, bid: float | None) -> bool:
        """True when the working-order refresh must LEAVE a v2 managed EXIT in place (P0, 2026-07-31).

        ⭐⭐ THE KUST INCIDENT. This is the exit-side twin of `_resting_trigger_refresh_exempt` above,
        and the reasoning in that docstring -- "no order resting when price crosses" -- applies to an
        exit verbatim. It had simply never been applied there: a managed exit is a LIMIT, so it fell
        through to `_refresh_working_order` and was cancel/replaced on the cadence.

        Live, real money, 2026-07-31: a sell LIMIT 1.74 placed 13:26:20 was cancelled and re-placed
        NINE times over six minutes. The captured Schwab bid tape for that exact window:

            13:26:13 1.76 | 13:26:54 1.75 | 13:27:34 1.74 | 13:28:02 1.78
            13:26:14 1.77 | 13:27:13 1.76 | 13:27:38 1.75 | 13:28:04 1.78

        The bid was >= the limit at EVERY tick. The order was fillable the entire time and we kept
        taking it off the book. It ended at the -5% hard stop (-5.17%), while the Webull leg -- same
        bid-sourced 1.74, placed once, never cancelled -- filled in 34 milliseconds at 1.7501 (+1.76%).

        ⛔ This is NOT "never reprice". Exemption holds only while the limit is still MARKETABLE
        (limit <= bid). Once the bid falls below it the order cannot fill where it sits, so the
        refresh resumes and re-prices it -- otherwise we would trade the KUST failure for a stale
        exit that never adjusts, which is the same bug facing the other way.

        ⛔ Fail-OPEN on a missing/zero bid: with no usable quote we cannot prove the order is
        marketable, so we do NOT claim the exemption and the old behaviour stands. An exit is
        protection; when in doubt keep the existing machinery, never invent a hold.
        """
        if not bool(getattr(self.settings, "oms_hold_marketable_managed_exit", True)):
            return False
        payload = order.payload or {}
        if str(payload.get("oms_v2_managed_exit", "")).strip().lower() != "true":
            return False
        if str(payload.get("order_type", "")).strip().upper() != "LIMIT":
            return False   # a MARKET exit has no resting price to protect
        try:
            limit_price = float(payload.get("limit_price"))
        except (TypeError, ValueError):
            return False
        if limit_price <= 0.0:
            return False
        try:
            bid_f = float(bid) if bid is not None else 0.0
        except (TypeError, ValueError):
            return False
        if bid_f <= 0.0:
            return False
        return limit_price <= bid_f

    @staticmethod
    def _cap_exit_limit_to_bid(price: float, *, bid: float | None) -> float:
        """Never place a sell exit ABOVE the current bid (P0b, 2026-07-31).

        The KUST ladder placed one exit at 1.77 while the bid was 1.76 and below -- an order that
        could not fill at placement, because the limit came off a stale reference price rather than
        a fresh quote. Capping at the bid makes every repriced exit marketable by construction.

        ⛔ Inert without a usable bid: return the caller's price untouched rather than capping to
        zero. A 0.0 limit would be far worse than a stale one.
        """
        try:
            bid_f = float(bid) if bid is not None else 0.0
        except (TypeError, ValueError):
            return price
        if bid_f <= 0.0:
            return price
        return min(float(price), bid_f)

    @staticmethod
    def _is_resting_trigger_order(order: BrokerOrder) -> bool:
        """A buy STOP / STOP_LIMIT entry is a TRIGGER order -- DESIGNED to rest until price crosses its
        trigger. Exempt it from the INTENT_MAX_AGE / SETUP_INVALID abandons, which exist to kill stuck
        MARKETABLE (LIMIT / MARKET) chases (PR #178) -- a trigger order that simply hasn't triggered is
        NOT stuck. Without this, the resting flip-entry is abandoned at 30s and can never stably sit at
        the ATR line (2026-07-23 live finding: the OMS re-cancelled the resting order every ~30-58s on
        quiet names, re-introducing the missed cross the stable-rest rework had just fixed). Segregation
        is by order TYPE, so it never exempts a marketable chase."""
        payload = order.payload or {}
        otype = str(payload.get("order_type", getattr(order, "order_type", "") or "")).upper()
        return otype in {"STOP", "STOP_LIMIT"}

    @staticmethod
    def _stop_guard_refresh_stage(metadata: dict[str, object]) -> int:
        try:
            return max(0, int(str(metadata.get("stop_guard_refresh_stage", "0"))))
        except (TypeError, ValueError):
            return 0

    def _next_stop_guard_refresh_stage(self, metadata: dict[str, object]) -> int:
        return min(2, self._stop_guard_refresh_stage(metadata) + 1)

    def _stop_guard_buffer_pct_for_stage(self, stage: int, metadata: dict[str, object]) -> float:
        if str(metadata.get("catastrophic_stop_guard", "")).strip().lower() == "true":
            return float(self.settings.oms_after_hours_stop_guard_catastrophic_panic_buffer_pct)
        if stage <= 0:
            try:
                return float(metadata.get("panic_buffer_pct", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        if stage == 1:
            return float(self.settings.oms_stop_guard_refresh_stage_1_buffer_pct)
        return float(self.settings.oms_stop_guard_refresh_stage_2_buffer_pct)

    def _stop_guard_catastrophic_refresh_metadata(
        self,
        *,
        metadata: dict[str, str],
        quote: dict[str, float | None],
    ) -> dict[str, str] | None:
        if not _metadata_marks_extended_hours(metadata):
            return None
        try:
            stop_price = Decimal(str(metadata.get("stop_price", "")).strip())
        except Exception:
            return None
        if stop_price <= 0:
            return None
        bid_price = quote.get("bid_price")
        last_price = quote.get("last_price")
        current_price = bid_price if bid_price is not None and bid_price > 0 else last_price
        if current_price is None or current_price <= 0:
            return None
        try:
            catastrophic_gap_pct = float(self.settings.oms_after_hours_stop_guard_catastrophic_gap_pct)
        except (TypeError, ValueError):
            catastrophic_gap_pct = 0.0
        if catastrophic_gap_pct <= 0:
            return None
        catastrophic_trigger = stop_price * (
            Decimal("1") - (Decimal(str(catastrophic_gap_pct)) / Decimal("100"))
        )
        if Decimal(str(current_price)) > catastrophic_trigger:
            return None
        panic_buffer_pct = float(self.settings.oms_after_hours_stop_guard_catastrophic_panic_buffer_pct)
        refreshed_price = _panic_limit_price(current_price, panic_buffer_pct)
        if refreshed_price is None:
            return None
        metadata["limit_price"] = refreshed_price
        metadata["reference_price"] = refreshed_price
        metadata["price_source"] = "bid" if bid_price is not None and bid_price > 0 else "last"
        metadata["panic_buffer_pct"] = str(panic_buffer_pct)
        metadata["catastrophic_stop_guard"] = "true"
        metadata["stop_guard_refresh_stage"] = "2"
        metadata["watchdog_refresh_reason"] = "catastrophic_gap"
        return metadata

    async def _refresh_working_order(
        self,
        *,
        session: Session,
        order: BrokerOrder,
        intent: TradeIntent,
        strategy_code: str,
        broker_account_name: str,
        report: ExecutionReport,
    ) -> dict[str, object]:
        remaining_quantity = max(Decimal("0"), order.quantity - report.filled_quantity)
        if remaining_quantity <= 0:
            return {"orders": 0, "terminal_orders": 0, "published_events": []}

        refreshed_metadata = await self._build_refreshed_order_metadata(
            broker_account_name=broker_account_name,
            order=order,
        )
        if refreshed_metadata is None:
            return {"orders": 0, "terminal_orders": 0, "published_events": []}

        existing_metadata = {str(k): str(v) for k, v in (order.payload or {}).items()}
        cancel_request = OrderRequest(
            client_order_id=order.client_order_id,
            broker_account_name=broker_account_name,
            strategy_code=strategy_code,
            symbol=order.symbol,
            side=order.side,  # type: ignore[arg-type]
            intent_type="cancel",
            quantity=remaining_quantity,
            reason="WORKING_ORDER_REFRESH",
            metadata={
                **existing_metadata,
                "broker_order_id": order.broker_order_id or "",
                "target_client_order_id": order.client_order_id,
                "watchdog_refresh": "true",
            },
            order_type=order.order_type,
            time_in_force=order.time_in_force,
        )
        if self._direct_cancel_dead_target_bound_reached(
            session,
            order=order,
            path="refresh_working_order",
        ):
            return {"orders": 0, "terminal_orders": 0, "published_events": []}
        cancel_reports = await self.broker_adapter.submit_order(cancel_request)
        cancelled_report = self._record_direct_cancel_reports(
            session,
            order=order,
            reports=cancel_reports,
            existing_metadata=existing_metadata,
            internal="watchdog_refresh",
            extra_metadata={"watchdog_refresh": "true"},
        )
        if cancelled_report is None:
            return {"orders": 0, "terminal_orders": 0, "published_events": []}

        replacement_client_order_id = self._replacement_client_order_id(order.client_order_id)
        prior_attempt_id = str(refreshed_metadata.get("fanout_attempt_id", "") or "").strip()
        if str(refreshed_metadata.get("fanout_slot_id", "") or "").strip():
            refreshed_metadata["fanout_attempt_id"] = replacement_client_order_id
            if prior_attempt_id:
                refreshed_metadata["fanout_predecessor_attempt_id"] = prior_attempt_id
            else:
                refreshed_metadata.pop("fanout_predecessor_attempt_id", None)
        replacement_request = OrderRequest(
            client_order_id=replacement_client_order_id,
            broker_account_name=broker_account_name,
            strategy_code=strategy_code,
            symbol=order.symbol,
            side=order.side,  # type: ignore[arg-type]
            intent_type=intent.intent_type,  # type: ignore[arg-type]
            quantity=remaining_quantity,
            reason=intent.reason,
            metadata=refreshed_metadata,
            order_type=str(refreshed_metadata.get("order_type", order.order_type)),
            time_in_force=str(refreshed_metadata.get("time_in_force", order.time_in_force)),
        )
        replacement_reports = await self.broker_adapter.submit_order(replacement_request)
        replacement_event = TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=strategy_code,
                broker_account_name=broker_account_name,
                symbol=order.symbol,
                side=order.side,  # type: ignore[arg-type]
                quantity=remaining_quantity,
                intent_type=intent.intent_type,  # type: ignore[arg-type]
                reason=intent.reason,
                metadata=dict(refreshed_metadata),
            ),
        )
        published_events = await self._record_order_reports(
            session=session,
            intent=intent,
            strategy_id=order.strategy_id,
            broker_account_id=order.broker_account_id,
            intent_event=replacement_event,
            request=replacement_request,
            reports=replacement_reports,
        )
        return {
            "orders": len(replacement_reports),
            "terminal_orders": 1,
            "published_events": published_events,
        }

    async def _build_refreshed_order_metadata(
        self,
        *,
        broker_account_name: str,
        order: BrokerOrder,
    ) -> dict[str, str] | None:
        metadata = {str(k): str(v) for k, v in (order.payload or {}).items()}
        metadata["watchdog_refresh"] = "true"
        metadata["watchdog_replaces_client_order_id"] = order.client_order_id
        metadata["watchdog_replaced_at"] = utcnow().isoformat()

        order_type = str(metadata.get("order_type", order.order_type or "market")).lower()
        if order_type != "limit":
            return metadata

        quote = await self._fetch_quote_for_order(
            broker_account_name=broker_account_name,
            symbol=order.symbol,
        )
        if not quote:
            return None
        if self._is_stop_guard_order(order):
            catastrophic_metadata = self._stop_guard_catastrophic_refresh_metadata(
                metadata=metadata,
                quote=quote,
            )
            if catastrophic_metadata is not None:
                return catastrophic_metadata
            next_stage = self._next_stop_guard_refresh_stage(metadata)
            panic_buffer_pct = self._stop_guard_buffer_pct_for_stage(next_stage, metadata)
            bid_price = quote.get("bid_price")
            last_price = quote.get("last_price")
            refreshed_price = _panic_limit_price(
                bid_price if bid_price is not None and bid_price > 0 else last_price,
                panic_buffer_pct,
            )
            if refreshed_price is None:
                return None
            metadata["limit_price"] = refreshed_price
            metadata["reference_price"] = refreshed_price
            metadata["price_source"] = "bid" if bid_price is not None and bid_price > 0 else "last"
            metadata["panic_buffer_pct"] = str(panic_buffer_pct)
            metadata["stop_guard_refresh_stage"] = str(next_stage)
            return metadata
        price_source = str(
            metadata.get("price_source")
            or ("ask" if str(order.side).lower() == "buy" else "bid")
        ).lower()
        quote_field = "ask_price" if price_source == "ask" else "bid_price"
        refreshed_price = quote.get(quote_field) or quote.get("last_price")
        if refreshed_price is None:
            return None
        price_text = format(Decimal(str(refreshed_price)).quantize(Decimal("0.01")), "f")
        metadata["limit_price"] = price_text
        metadata["reference_price"] = price_text
        return metadata

    async def _fetch_quote_for_order(
        self,
        *,
        broker_account_name: str,
        symbol: str,
    ) -> dict[str, float | None]:
        fetcher = getattr(self.broker_adapter, "fetch_quotes", None)
        if callable(fetcher):
            quotes = await fetcher([symbol])
            return dict(quotes.get(symbol.upper(), {}))
        if isinstance(self.broker_adapter, RoutingBrokerAdapter):
            adapter = self.broker_adapter._adapter_for_account(broker_account_name)
            fetcher = getattr(adapter, "fetch_quotes", None)
            if callable(fetcher):
                quotes = await fetcher([symbol])
                return dict(quotes.get(symbol.upper(), {}))
        return {}

    @staticmethod
    def _replacement_client_order_id(client_order_id: str) -> str:
        base = str(client_order_id).strip()
        # Strip any prior retry suffix(es) so repeated retries REPLACE the suffix instead of
        # ACCUMULATING it — each `-r<8hex>` is 10 chars, and a few retries pushed the id past
        # Webull's 40-char cap -> ILLEGAL_PARAMETER, so every retry rejected and the order
        # (close or native guard) could never place (2026-07-13 ORB LGPS reject loop).
        base = re.sub(r"(-r[0-9a-f]{8})+$", "", base)
        suffix = f"-r{uuid4().hex[:8]}"
        base = base[: OmsRiskService._CLIENT_ORDER_ID_MAX_LEN - len(suffix)]
        return f"{base}{suffix}"

    @staticmethod
    def _is_reverse_conflict_reject(reports: list[ExecutionReport]) -> bool:
        """True if any report is a Webull ORDER_NOT_SUPPORT_REVERSE_OPTION rejection — a
        protective SELL that would exceed the held long (a resting sell still reserves the
        shares, or the position is already flat)."""
        return any(
            report.event_type == "rejected"
            and "REVERSE" in str(getattr(report, "reason", "") or "").upper()
            for report in reports
        )

    def _stop_reject_reason(
        self,
        *,
        request: OrderRequest,
        reports: list[ExecutionReport],
    ) -> str | None:
        if str(request.metadata.get("stop_reject_fallback", "")).lower() == "true":
            return None
        is_stop_guard_close = (
            request.intent_type == "close"
            and str(request.metadata.get("stop_guard", "")).strip().lower() == "true"
        )
        if request.intent_type not in {"open", "scale"} and not is_stop_guard_close:
            return None
        for report in reports:
            if report.event_type == "rejected" and self._is_stop_rejection_reason(report.reason):
                return report.reason or "stop_rejected"
        return None

    def _has_cached_schwab_ineligible_symbol(
        self,
        *,
        session: Session,
        broker_account_id: UUID,
        symbol: str,
    ) -> bool:
        return (
            self.store.get_schwab_ineligible_entry(
                session,
                broker_account_id=broker_account_id,
                symbol=symbol,
                session_date=self._current_session_day(),
            )
            is not None
        )

    @staticmethod
    def _is_schwab_ineligible_reason(reason: str | None) -> bool:
        normalized = str(reason or "").strip().lower()
        return any(fragment in normalized for fragment in SCHWAB_INELIGIBLE_REASON_SUBSTRINGS)

    def _has_cached_webull_ineligible_symbol(
        self,
        *,
        session: Session,
        broker_account_id: UUID,
        symbol: str,
    ) -> bool:
        return (
            self.store.get_webull_ineligible_entry(
                session,
                broker_account_id=broker_account_id,
                symbol=symbol,
                session_date=self._current_session_day(),
            )
            is not None
        )

    @staticmethod
    def _is_webull_ineligible_reason(reason: str | None) -> bool:
        """Dual-broker fan-out: True only for a CLEAR not-tradable Webull reject. The transient /
        rate-limit / config veto set is checked FIRST so a 429 (or "missing key") can NEVER mark a
        name ineligible even if a broader substring also appears."""
        normalized = str(reason or "").strip().lower()
        if not normalized:
            return False
        # Match SCREAMING_SNAKE codes AND free-text by also testing an underscore->space variant.
        despaced = normalized.replace("_", " ")

        def _hit(fragments: tuple[str, ...]) -> bool:
            return any(f in normalized or f in despaced for f in fragments)

        if _hit(WEBULL_INELIGIBLE_EXCLUDE_SUBSTRINGS):
            return False
        return _hit(WEBULL_INELIGIBLE_REASON_SUBSTRINGS)

    def _fanout_webull_collision_reason(
        self,
        *,
        session: Session,
        broker_account_name: str,
        symbol: str,
    ) -> str | None:
        """Dual-broker fan-out collision guard (mirrors the mirror-on-fill guard). Returns a reason
        string if the shared Webull account already holds this symbol armed / managed / at the
        broker (never fight ORB or a prior un-flat leg), else None to proceed."""
        armed_here = any(
            st.broker_account_name == broker_account_name and st.symbol == symbol
            for st in self._armed_hard_stops.values()
        )
        if armed_here:
            return "fanout_webull_collision_armed"
        if (
            self.store.get_open_managed_position(
                session, broker_account_name=broker_account_name, symbol=symbol
            )
            is not None
        ):
            return "fanout_webull_collision_managed"
        held_qty = self.store.get_account_position_qty_by_name(
            session, broker_account_name=broker_account_name, symbol=symbol
        )
        if held_qty != 0:
            return "fanout_webull_collision_held"
        return None

    async def _process_stop_reject_market_fallback(
        self,
        *,
        session: Session,
        strategy,
        broker_account,
        original_event: TradeIntentEvent,
        original_request: OrderRequest,
        rejection_reason: str,
    ) -> list[OrderEventEvent]:
        available_quantity = await self._refresh_broker_position_quantity(
            session=session,
            broker_account_id=broker_account.id,
            broker_account_name=broker_account.name,
            symbol=original_event.payload.symbol,
        )
        if available_quantity <= 0:
            return []

        fallback_metadata = {
            **{str(k): str(v) for k, v in original_request.metadata.items()},
            "fallback_for_client_order_id": original_request.client_order_id,
            "fallback_rejection_reason": rejection_reason,
            "stop_reject_fallback": "true",
            "order_type": "market",
        }
        fallback_event = TradeIntentEvent(
            source_service=SERVICE_NAME,
            payload=TradeIntentPayload(
                strategy_code=original_event.payload.strategy_code,
                broker_account_name=original_event.payload.broker_account_name,
                symbol=original_event.payload.symbol,
                side="sell",
                quantity=available_quantity,
                intent_type="close",
                reason="STOP_REJECTED_FALLBACK",
                metadata=fallback_metadata,
            ),
        )
        fallback_intent = self.store.create_trade_intent(
            session,
            strategy=strategy,
            broker_account=broker_account,
            event=fallback_event,
        )
        self.store.record_risk_check(
            session,
            intent=fallback_intent,
            strategy_id=strategy.id,
            broker_account_id=broker_account.id,
            outcome="pass",
            reason="stop_rejected_fallback",
            payload={"metadata": dict(fallback_metadata)},
        )
        fallback_request = OrderRequest(
            client_order_id=self._build_client_order_id(fallback_event),
            broker_account_name=broker_account.name,
            strategy_code=original_event.payload.strategy_code,
            symbol=original_event.payload.symbol,
            side="sell",
            intent_type="close",
            quantity=available_quantity,
            reason="STOP_REJECTED_FALLBACK",
            metadata=fallback_metadata,
        )
        fallback_reports = await self.broker_adapter.submit_order(fallback_request)
        return await self._record_order_reports(
            session=session,
            intent=fallback_intent,
            strategy_id=strategy.id,
            broker_account_id=broker_account.id,
            intent_event=fallback_event,
            request=fallback_request,
            reports=fallback_reports,
        )

    def _is_not_tradable_reason(self, reason: str | None) -> bool:
        if not reason:
            return False
        lowered = reason.lower()
        return any(fragment in lowered for fragment in self.NOT_TRADABLE_REASONS)

    def _is_stop_rejection_reason(self, reason: str | None) -> bool:
        if not reason:
            return False
        lowered = reason.lower()
        return "stop" in lowered and ("reject" in lowered or "below" in lowered or "at/below" in lowered)
