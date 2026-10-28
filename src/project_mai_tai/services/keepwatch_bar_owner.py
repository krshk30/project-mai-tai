"""Bounded per-symbol chart inbox; overflow is explicit UNKNOWN, never success."""

from dataclasses import dataclass, field
from contextvars import ContextVar

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar


class BarConsumptionSuperseded(Exception):
    """An admitted observation remains queued, but cannot consume strategy state."""


bar_consumption_context: ContextVar[tuple | None] = ContextVar(
    "keepwatch_bar_consumption", default=None)
current_bar_recovery_context: ContextVar[tuple | None] = ContextVar(
    "keepwatch_current_bar_recovery", default=None)


@dataclass(frozen=True)
class OwnedBar:
    bar: ChartBar
    source: str
    anchor_ms: int
    epoch: int
    observed_ms: int = 0

    def __post_init__(self):
        bar = self.bar
        # ChartBar itself is mutable; caller revisions cannot mutate admitted work.
        object.__setattr__(self, "bar", ChartBar(
            bar.symbol, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.timestamp_ms,
            producer=bar.producer))


@dataclass
class CurrentBarRecovery:
    """Process-local current observations, never a full-history attestation.

    The caller must still check its owner/STOP/membership fences. Counting a
    receipt neither consumes its inbox work nor grants permission to trade.
    """

    symbol: str
    anchor_ms: int
    epoch: int
    source_identity: object
    ledger: object
    state: object
    period: int
    membership: int = 0
    last_work: OwnedBar | None = None
    contiguous: int = 0
    invalidated: bool = False
    publication: tuple | None = None
    gap_revision: int = -1
    gap_open: bool = False

    @property
    def required(self) -> int:
        # Identical to OLD _maybe_resume_gap_hold, not a new fixed-ten rule.
        return 2 * self.period

    def bound_to(self, *, symbol, anchor_ms, epoch, source_identity, ledger, state) -> bool:
        return bool(not self.invalidated and self.period > 0 and self.epoch > 0
                    and self.symbol == symbol and self.anchor_ms == anchor_ms
                    and self.epoch == epoch and self.source_identity == source_identity
                    and self.ledger is ledger and self.state is state)

    def observe(self, work: OwnedBar, *, canonical: ChartBar, now_ms: int) -> bool:
        bar = work.bar
        if (self.invalidated or self.period <= 0 or self.epoch <= 0
                or work.anchor_ms != self.anchor_ms or work.epoch != self.epoch
                or bar.symbol != self.symbol or work.source not in {"rest", "streamer"}
                or bar.producer not in {"massive", "schwab"}
                or not self.anchor_ms <= bar.timestamp_ms < self.anchor_ms + 57_600_000
                or bar.timestamp_ms % 60_000
                or not 60_000 <= work.observed_ms - bar.timestamp_ms <= 180_000
                or work.observed_ms > now_ms or bar != canonical):
            return False
        previous = self.last_work
        if previous is not None and bar.timestamp_ms <= previous.bar.timestamp_ms:
            if bar.timestamp_ms == previous.bar.timestamp_ms and bar == previous.bar:
                return True  # Same-minute delivery/retry is not another bar.
            # A correction behind consumed math needs repair, not another count.
            self.invalidated = True
            self.publication = None
            return False
        if previous is None or bar.timestamp_ms != previous.bar.timestamp_ms + 60_000:
            self.contiguous = 0
        self.publication = None
        self.contiguous = min(self.required, self.contiguous + 1)
        self.last_work = work
        return True

    def threshold_observed(self) -> bool:
        return bool(not self.invalidated and self.period > 0
                    and self.contiguous >= self.required)


@dataclass
class BarInbox:
    capacity: int
    pending: dict[int, OwnedBar] = field(default_factory=dict)
    received: int = 0
    accepted: int = 0
    revised: int = 0
    consumed: int = 0
    covered: int = 0
    expired: int = 0
    rejected: int = 0
    failures: int = 0
    historical_ignored: int = 0
    overflow_first_ms: int = 0
    overflow_last_ms: int = 0

    def offer(self, work: OwnedBar) -> bool:
        self.received += 1
        stamp = work.bar.timestamp_ms
        previous = self.pending.get(stamp)
        if previous is not None:
            self.revised += 1
            if (previous.source == work.source and previous.bar.producer is not None
                    and previous.bar.producer == work.bar.producer and previous.bar == work.bar):
                # Raw candidates were retained at ingress. An unchanged canonical
                # delivery must not supersede work awaiting its real owner callback.
                return True
            # A live CHART frame wins over a REST revision at a shared minute.
            if previous.source != "streamer" or work.source == "streamer":
                self.pending[stamp] = work
            return True
        if len(self.pending) >= self.capacity:
            self.rejected += 1
            if not self.overflow_first_ms:
                self.overflow_first_ms = stamp
            self.overflow_last_ms = stamp
            return False
        self.pending[stamp] = work
        self.accepted += 1
        return True

    def complete(self, work: OwnedBar, *, covered=False, expired=False) -> None:
        if self.pending.get(work.bar.timestamp_ms) is not work:
            return  # A revision arrived while the earlier version was in flight.
        self.pending.pop(work.bar.timestamp_ms)
        if expired:
            self.expired += 1
        elif covered:
            self.covered += 1
        else:
            self.consumed += 1
