"""Current TODAY input extension; legacy result types and seals stay original."""
from dataclasses import dataclass
from .types import (
    BodyObservation as BodyObservation,
    BodySnapshot as BodySnapshot,
    Boundary as Boundary,
    CONTRACT_VERSION as CONTRACT_VERSION,
    CompatibilityResult as CompatibilityResult,
    ConditionRuleEvidence as ConditionRuleEvidence,
    Coverage as Coverage,
    Decimal as Decimal,
    EntryDecision as EntryDecision,
    EntryFact as EntryFact,
    EntryMode as EntryMode,
    EntryPlan as EntryPlan,
    Enum as Enum,
    EpisodeResult as EpisodeResult,
    EpisodeSimulator as EpisodeSimulator,
    EventKey as EventKey,
    EvidenceKind as EvidenceKind,
    ExecutionEligibility as ExecutionEligibility,
    ExecutionPolicy as ExecutionPolicy,
    FillAllocation as FillAllocation,
    FillModel as FillModel,
    FlipIdentity as FlipIdentity,
    LiveExitFact as LiveExitFact,
    LiveFacts as LiveFacts,
    LiveReason as LiveReason,
    PositionReceipt as PositionReceipt,
    Protocol as Protocol,
    Quote as Quote,
    ScenarioResult as ScenarioResult,
    SimExit as SimExit,
    SimExitReason as SimExitReason,
    SimOrder as SimOrder,
    SourceRef as SourceRef,
    TradePrint as TradePrint,
)
from .types import EpisodeInput as LegacyEpisodeInput


@dataclass(frozen=True)
class EpisodeInput(LegacyEpisodeInput):
    same_sip_policy: str | None = None
