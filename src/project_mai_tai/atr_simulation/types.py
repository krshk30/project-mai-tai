"""Versioned pure-data boundary for ATRSIM1, independent of live strategy state.

Nanoseconds are integers. Decimal prices retain source precision. An event's
sequence is the adapter's explicit causal serialization, not a database ID or
proof of native exchange queue priority. Unknown evidence is never false/zero.
raw_id must identify the provider's event across export artifacts; sequence must
be shared across overlapping inputs, not restarted independently per account.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Protocol


CONTRACT_VERSION = "ATRSIM1_V3_EXECUTION_ELIGIBILITY"


class SimExitReason(str, Enum):
    TARGET_5 = "TARGET_5"
    STOP_8 = "STOP_8"
    TIME_NO_1PCT_120S = "TIME_NO_1PCT_120S"
    FLOOR_NEG05_AFTER_2 = "FLOOR_NEG05_AFTER_2"
    SELL_FLIP = "SELL_FLIP"
    SESSION_END = "SESSION_END"
    OPEN = "OPEN"
    SAME_AS_LIVE = "SAME_AS_LIVE"


class LiveReason(str, Enum):
    TARGET = "TARGET"
    STOP = "STOP"
    SELL_FLIP = "SELL_FLIP"
    CONFIRMATION = "CONFIRMATION"
    FLOOR = "FLOOR"
    BROKER_STOP = "BROKER_STOP"
    MANUAL = "MANUAL"
    OTHER = "OTHER"


class EntryMode(str, Enum):
    TODAY = "TODAY"
    VOLBODY = "VOLBODY"


class FillModel(str, Enum):
    QUEUE_AWARE = "QUEUE_AWARE"
    OPTIMISTIC = "OPTIMISTIC"


class EntryDecision(str, Enum):
    BUY_ON_TOUCH = "BUY_ON_TOUCH"
    MODELLED_ENTRY = "MODELLED_ENTRY"
    NO_MODELLED_ENTRY = "NO_MODELLED_ENTRY"
    VB_WAIT_BUY = "VB_WAIT_BUY"
    VB_WAIT_NEVER_FILLED = "VB_WAIT_NEVER_FILLED"
    VB_SKIP_TO_SELL_FLIP = "VB_SKIP_TO_SELL_FLIP"
    UNKNOWN = "UNKNOWN"


class EvidenceKind(str, Enum):
    RETAINED = "RETAINED"
    REFETCHED_SAME_ENDPOINT = "REFETCHED_SAME_ENDPOINT"
    CONTROLLED = "CONTROLLED"
    DERIVED = "DERIVED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SourceRef:
    artifact: str
    sha256: str
    pointer: str
    kind: EvidenceKind
    asof_ns: int | None = None


@dataclass(frozen=True)
class Coverage:
    """Complete is an adapter claim with evidence, not inferred from row counts."""

    start_ns: int
    end_ns: int
    complete: bool | None
    gaps: tuple[str, ...]
    sources: tuple[SourceRef, ...]


@dataclass(frozen=True)
class EventKey:
    event_ns: int
    sequence: int
    raw_id: str
    source: SourceRef
    received_ns: int | None = None


@dataclass(frozen=True)
class ConditionRuleEvidence:
    condition_id: int
    name: str
    updates_high_low: bool
    updates_open_close: bool
    updates_volume: bool


@dataclass(frozen=True)
class ExecutionPolicy:
    """Pinned Massive snapshot used by the existing momentum classifier."""

    version: str
    source: SourceRef
    rules: tuple[ConditionRuleEvidence, ...]


@dataclass(frozen=True)
class ExecutionEligibility:
    raw_id: str
    raw_source: SourceRef
    size: int
    raw_conditions: str | tuple[int, ...] | None
    policy: ExecutionPolicy | None
    eligible: bool | None
    reason: str
    conditions_present: bool = True


@dataclass(frozen=True)
class TradePrint:
    key: EventKey
    symbol: str
    price: Decimal
    size: int
    execution: ExecutionEligibility | None = None


@dataclass(frozen=True)
class Quote:
    key: EventKey
    symbol: str
    bid: Decimal | None
    ask: Decimal | None
    bid_size: int | None
    ask_size: int | None


@dataclass(frozen=True)
class FlipIdentity:
    """No symbol/minute dedup: accounts, brokers, flips and source IDs survive."""

    trading_day: str
    symbol: str
    broker: str
    account_id: str
    strategy: str
    flip_id: str
    segment_id: str


@dataclass(frozen=True)
class Boundary:
    available_ns: int
    source: SourceRef
    bar_start_ns: int | None = None


@dataclass(frozen=True)
class EntryFact:
    fill_id: str
    at: EventKey
    price: Decimal
    quantity: int
    source: SourceRef


@dataclass(frozen=True)
class LiveExitFact:
    fill_id: str
    at: EventKey
    price: Decimal
    quantity: int
    raw_reason: str | None
    mapped_reason: LiveReason | None
    source: SourceRef


@dataclass(frozen=True)
class LiveFacts:
    """bought=False requires affirmative coverage; missing fills mean UNKNOWN."""

    bought: bool | None
    entries: tuple[EntryFact, ...]
    exits: tuple[LiveExitFact, ...]
    coverage: Coverage
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class EpisodeInput:
    identity: FlipIdentity
    buy_flip: Boundary
    watched: bool | None
    watched_source: SourceRef | None
    trigger: Decimal | None
    quantity: int | None
    sizing_source: SourceRef | None
    sell_flip: Boundary | None
    session_end: Boundary
    trades: tuple[TradePrint, ...]
    quotes: tuple[Quote, ...]
    tape_coverage: Coverage
    flip_coverage: Coverage
    today_entries: tuple[EntryFact, ...]
    today_entry_coverage: Coverage
    live: LiveFacts
    execution_policy: ExecutionPolicy | None = None


@dataclass(frozen=True)
class BodySnapshot:
    minute_start_ns: int
    low_bin: int | None
    high_bin: int | None
    poc_bin: int | None
    included_volume: int
    total_volume: int
    bins: tuple[tuple[int, int], ...]
    prefix_complete: bool | None


@dataclass(frozen=True)
class SimOrder:
    order_id: str
    identity: FlipIdentity
    model: FillModel
    placed_at: EventKey
    limit: Decimal
    quantity: int
    queue_ahead: int | None
    quote_source: SourceRef | None
    placement_branch: str = "LEGACY_LATER_PRINT"
    placement_quote: Quote | None = None
    valid_until_ns: int | None = None


@dataclass(frozen=True)
class BodyObservation:
    at: EventKey
    body: BodySnapshot
    touched: bool
    ready: bool


@dataclass(frozen=True)
class EntryPlan:
    order: SimOrder | None
    observations: tuple[BodyObservation, ...]
    touched: bool
    issues: tuple[str, ...]
    execution_unknowns: tuple[ExecutionEligibility, ...] = ()


@dataclass(frozen=True)
class FillAllocation:
    order_id: str
    print_key: EventKey
    price: Decimal
    quantity: int
    queue_consumed: int
    assumption: str


@dataclass(frozen=True)
class SimExit:
    reason: SimExitReason
    at: EventKey | None
    price: Decimal | None
    quantity: int
    price_basis: str
    issues: tuple[str, ...] = ()
    live_exit: LiveExitFact | None = None


@dataclass(frozen=True)
class PositionReceipt:
    """One simulated position, including every accepted partial-fill fact."""

    first_fill_ts: int | None
    fills: tuple[EntryFact, ...]
    vwap: Decimal | None
    filled_qty: int
    remainder_cancel_reason: str | None
    ignored_fills: tuple[EntryFact, ...] = ()


@dataclass(frozen=True)
class ScenarioResult:
    mode: EntryMode
    model: FillModel | None
    decision: EntryDecision
    orders: tuple[SimOrder, ...]
    fills: tuple[FillAllocation, ...]
    exits: tuple[SimExit, ...]
    gross_pnl: Decimal | None
    issues: tuple[str, ...]
    body_observations: tuple[BodyObservation, ...] = ()
    entry_facts: tuple[EntryFact, ...] = ()
    position: PositionReceipt | None = None
    execution_unknowns: tuple[ExecutionEligibility, ...] = ()


@dataclass(frozen=True)
class CompatibilityResult:
    """Reference fallback/aggregated USD stays outside canonical ledger rows."""

    reference: SourceRef
    reference_key: str
    reference_rule: str
    reference_return: Decimal
    reference_usd: Decimal | None
    matched: bool | None
    differences: tuple[str, ...]


@dataclass(frozen=True)
class EpisodeResult:
    contract_version: str
    identity: FlipIdentity
    live: LiveFacts
    today: ScenarioResult
    volbody_queue_aware: ScenarioResult
    volbody_optimistic: ScenarioResult
    compatibility: tuple[CompatibilityResult, ...]
    issues: tuple[str, ...]
    execution_policy: ExecutionPolicy | None = None
    clock_label: str = "reconstructed SIP clock"
    condition_coverage: dict | None = None
    live_lag_note: str = "Live receipt lag today09:30 ~107ms p50 / 1.0s p99 (Claude); SIM may be optimistic in opening bursts."


class EpisodeSimulator(Protocol):
    """Caller supplies immutable inputs; no fetching or persistence in engine."""

    def simulate(self, episode: EpisodeInput) -> EpisodeResult: ...

    def simulate_many(self, episodes: tuple[EpisodeInput, ...]) -> tuple[EpisodeResult, ...]: ...
