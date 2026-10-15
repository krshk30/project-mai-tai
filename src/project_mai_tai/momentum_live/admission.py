from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

STRATEGY_CODE = "momentum_warn_live"
ET = ZoneInfo("America/New_York")
ACTIVE_STATES = frozenset({"claimed", "submitting", "working", "partial", "held", "cancel_requested", "exiting", "unknown"})


class Refusal(ValueError):
    pass


class Route(StrEnum):
    SCHWAB = "schwab"
    WEBULL = "webull"


@dataclass(frozen=True)
class Signal:
    session_date: str
    symbol: str
    source_id: str
    interval_start_ms: int
    interval_end_ms: int
    signal_price: Decimal
    observed_ns: int
    day_volume: int | None
    country_route: str | None = None
    country_category: str | None = None
    country_evidence_json: str | None = None

    def validate(self) -> None:
        if (not self.symbol or self.symbol != self.symbol.upper() or not self.source_id
                or self.interval_start_ms < 0 or self.interval_start_ms > self.interval_end_ms
                or not self.signal_price.is_finite() or self.signal_price <= 0):
            raise Refusal("signal_invalid")
        if self.day_volume is None:
            raise Refusal("day_volume_unknown")
        if self.day_volume < 50_000:
            raise Refusal("day_volume_below_50000")


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    reason: str


def select_route(now: datetime, *, market_open: bool, is_us: bool | None) -> RouteDecision:
    local = now.astimezone(ET)
    if not market_open or not 4 <= local.hour < 16:
        raise Refusal("entry_window_closed")
    if local.hour < 7:
        return RouteDecision(Route.WEBULL, "all_before_0700")
    if is_us is True:
        return RouteDecision(Route.SCHWAB, "sec_us")
    if is_us is False:
        return RouteDecision(Route.WEBULL, "sec_non_us")
    if is_us is None:
        return RouteDecision(Route.WEBULL, "sec_unknown_at_decision")
    raise Refusal("sec_classification_invalid")


@dataclass(frozen=True)
class EntryPrice:
    limit: Decimal
    cap: Decimal
    quantity: int


def price_entry(signal_price: Decimal, ask: Decimal | None, *, quote_ns: int | None,
                now_ns: int, route: Route, tick: Decimal) -> EntryPrice:
    if (ask is None or not ask.is_finite() or ask <= 0 or quote_ns is None
            or not 0 <= now_ns - quote_ns <= 2_000_000_000):
        raise Refusal("quote_missing_future_or_stale")
    if not signal_price.is_finite() or signal_price <= 0 or not tick.is_finite() or tick <= 0:
        raise Refusal("price_invalid")
    cap = signal_price * Decimal("1.015")
    limit = (ask / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    if limit > cap:
        raise Refusal("rounded_ask_above_cap")
    quantity = max(1, int((Decimal(100) / limit).to_integral_value(rounding=ROUND_FLOOR)))
    if route == Route.WEBULL and limit < 1 and quantity < 100:
        raise Refusal("webull_subdollar_quantity_below_100")
    return EntryPrice(limit, cap, quantity)


@dataclass
class Episode:
    episode_id: str
    session_date: str
    symbol: str
    start_ms: int
    end_ms: int
    source_ids: set[str] = field(default_factory=set)
    state: str = "claimed"
    route: Route | None = None
    first_submit_ns: int | None = None
    fallback_used: bool = False
    cancel_requested: bool = False
    filled: Decimal = Decimal(0)
    exited: Decimal = Decimal(0)
    first_fill_ns: int | None = None
    closed_ns: int | None = None

    @property
    def remaining(self) -> Decimal:
        return self.filled - self.exited


class EpisodeLedger:
    """Single-event-loop admission owner; export/restore is wired to durable OMS claims."""

    def __init__(self, *, capacity: int = 2, history_limit: int = 4096):
        self.capacity = capacity
        self.history_limit = history_limit
        self.episodes: dict[str, Episode] = {}
        self._latest: dict[tuple[str, str], Episode] = {}
        self._sources: dict[tuple[str, str, str], Episode] = {}
        self._active_by_symbol: dict[str, set[str]] = {}
        self._active_count = 0
        self.ready = False

    def claim(self, signal: Signal, *, now_ns: int, foreign_owner: bool = False) -> tuple[Episode, bool]:
        signal.validate()
        if not self.ready:
            raise Refusal("ownership_restore_pending")
        if foreign_owner:
            raise Refusal("foreign_account_symbol_owner")
        key = (signal.session_date, signal.symbol)
        source_key = (*key, signal.source_id)
        episode = self._sources.get(source_key)
        if episode is not None:
            return episode, False
        prior = self._latest.get(key)
        active = [self.episodes[identity] for identity in self._active_by_symbol.get(signal.symbol, ())]
        overlapping = [owned for owned in active if owned.session_date == signal.session_date
                       and signal.interval_start_ms <= owned.end_ms and signal.interval_end_ms >= owned.start_ms]
        if len(overlapping) > 1:
            raise Refusal("multiple_owned_overlapping_episodes")
        if overlapping:
            prior = overlapping[0]
        if prior is not None and signal.interval_start_ms <= prior.end_ms and signal.interval_end_ms >= prior.start_ms:
            if len(self._sources) >= self.history_limit:
                raise Refusal("episode_history_full")
            prior.start_ms = min(prior.start_ms, signal.interval_start_ms)
            prior.end_ms = max(prior.end_ms, signal.interval_end_ms)
            prior.source_ids.add(signal.source_id)
            self._sources[source_key] = prior
            return prior, False
        if active:
            raise Refusal("symbol_episode_pending_or_held")
        if prior is not None and prior.closed_ns is not None and now_ns - prior.closed_ns < 30_000_000_000:
            raise Refusal("symbol_close_cooldown")
        if self._active_count >= self.capacity:
            raise Refusal("two_reservations_full")
        if len(self._sources) >= self.history_limit:
            raise Refusal("episode_history_full")
        identity = f"{STRATEGY_CODE}:{signal.session_date}:{signal.symbol}:{signal.source_id}"
        episode = Episode(str(uuid5(NAMESPACE_URL, identity)), signal.session_date, signal.symbol,
                          signal.interval_start_ms, signal.interval_end_ms, {signal.source_id})
        self.episodes[episode.episode_id] = episode
        self._latest[key] = episode
        self._sources[source_key] = episode
        self._active_count += 1
        self._active_by_symbol.setdefault(signal.symbol, set()).add(episode.episode_id)
        return episode, True

    def begin_submit(self, episode_id: str, route: Route, *, now_ns: int) -> None:
        episode = self.episodes[episode_id]
        if episode.state != "claimed" or episode.first_submit_ns is not None:
            raise Refusal("entry_already_attempted")
        episode.route = route
        episode.first_submit_ns = now_ns
        episode.state = "submitting"

    def claim_fallback(self, episode_id: str, *, now_ns: int, proven_ineligible: bool,
                       proven_terminal_zero_fill: bool) -> None:
        # Keep the legacy method fail-closed and the serialized field readable:
        # real restored exposure is retained, but no caller can authorize resend.
        raise Refusal("fallback_disabled_operator_142x")

    def cancel_due(self, episode_id: str, *, now_ns: int) -> bool:
        episode = self.episodes[episode_id]
        if (episode.first_submit_ns is None or now_ns - episode.first_submit_ns < 2_000_000_000
                or episode.cancel_requested or episode.state not in {"submitting", "working", "partial", "unknown"}):
            return False
        episode.cancel_requested = True
        episode.state = "cancel_requested"
        return True

    def reconcile(self, episode_id: str, *, cumulative_filled: Decimal,
                  cumulative_exited: Decimal, now_ns: int, terminal_entry: bool,
                  outstanding_exit: bool = False, first_fill_ns: int | None = None) -> None:
        episode = self.episodes[episode_id]
        was_active = episode.state in ACTIVE_STATES
        if (not cumulative_filled.is_finite() or not cumulative_exited.is_finite()
                or cumulative_filled < episode.filled or cumulative_exited < episode.exited
                or cumulative_exited < 0 or cumulative_exited > cumulative_filled):
            raise Refusal("fill_reconciliation_invalid")
        if first_fill_ns is not None and not 0 < first_fill_ns <= now_ns:
            raise Refusal("first_fill_time_unknown_or_future")
        if cumulative_filled > 0:
            if first_fill_ns is not None:
                episode.first_fill_ns = min(episode.first_fill_ns, first_fill_ns) if episode.first_fill_ns is not None else first_fill_ns
        episode.filled, episode.exited = cumulative_filled, cumulative_exited
        if terminal_entry and episode.remaining == 0 and not outstanding_exit:
            episode.state = "closed" if episode.filled else "terminal_no_fill"
            if episode.filled and episode.closed_ns is None:
                episode.closed_ns = now_ns
        elif episode.remaining > 0:
            episode.state = "held" if terminal_entry else "partial"
        elif not terminal_entry:
            episode.state = "unknown"
        self._active_count += int(episode.state in ACTIVE_STATES) - int(was_active)
        if episode.state in ACTIVE_STATES:
            self._active_by_symbol.setdefault(episode.symbol, set()).add(episode.episode_id)
        else:
            active_ids = self._active_by_symbol.get(episode.symbol)
            if active_ids is not None:
                active_ids.discard(episode.episode_id)
                if not active_ids:
                    self._active_by_symbol.pop(episode.symbol)

    def time_exit_due(self, episode_id: str, *, now_ns: int) -> bool:
        episode = self.episodes[episode_id]
        clock = episode.first_fill_ns if episode.first_fill_ns is not None else episode.first_submit_ns
        return (episode.remaining > 0 and clock is not None
                and now_ns - clock >= 300_000_000_000)

    def snapshot(self) -> list[dict]:
        result = []
        for episode in self.episodes.values():
            row = asdict(episode)
            row.update(source_ids=sorted(episode.source_ids), route=episode.route.value if episode.route else None,
                       filled=str(episode.filled), exited=str(episode.exited))
            result.append(row)
        return result

    @classmethod
    def restore(cls, rows: list[dict], *, capacity: int = 2, history_limit: int = 4096) -> EpisodeLedger:
        ledger = cls(capacity=capacity, history_limit=history_limit)
        for row in rows:
            values = dict(row)
            values.update(source_ids=set(row["source_ids"]), filled=Decimal(row["filled"]),
                          exited=Decimal(row["exited"]), route=Route(row["route"]) if row["route"] else None)
            episode = Episode(**values)
            if (episode.episode_id in ledger.episodes or episode.state not in ACTIVE_STATES | {"closed", "terminal_no_fill"}
                    or not episode.filled.is_finite() or not episode.exited.is_finite()
                    or episode.exited < 0 or episode.exited > episode.filled):
                raise Refusal("restore_invalid")
            key = (episode.session_date, episode.symbol)
            ledger.episodes[episode.episode_id] = episode
            previous = ledger._latest.get(key)
            if previous is None or (episode.end_ms, episode.start_ms, episode.episode_id) > (
                    previous.end_ms, previous.start_ms, previous.episode_id):
                ledger._latest[key] = episode
            ledger._active_count += int(episode.state in ACTIVE_STATES)
            if episode.state in ACTIVE_STATES:
                ledger._active_by_symbol.setdefault(episode.symbol, set()).add(episode.episode_id)
            for source in episode.source_ids:
                source_key = (*key, source)
                if source_key in ledger._sources:
                    raise Refusal("restore_duplicate_source")
                ledger._sources[source_key] = episode
        if len(ledger._sources) > history_limit or ledger._active_count > capacity:
            raise Refusal("restore_capacity_invalid")
        ledger.ready = True
        return ledger
