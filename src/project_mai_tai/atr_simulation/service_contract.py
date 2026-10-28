"""V3 service boundary; opaque Gibbs episodes/results, never engine reimplementation."""

from dataclasses import dataclass


ENGINE_DEPENDENCY_HEAD = "5e3aba0dfea1bce93b50c598e522b8b67902e19e"
ENGINE_CONTRACT_VERSION = "ATRSIM1_V3_EXECUTION_ELIGIBILITY"
SIMULATOR_CALL = "simulate_many(tuple[EpisodeInput, ...]) -> tuple[EpisodeResult, ...]"
POSITION_CONTRACT = "ONE_POSITION_CURRENT_VWAP_FIRST_FILL_120S"
POSITION_TIMESTAMP_UNIT = "event_ns"
POSITION_RECEIPT_FIELDS = (
    "first_fill_ts", "fills", "vwap", "filled_qty", "remainder_cancel_reason",
)
# The 13b position API is unchanged; these receipts come from the reviewed successor.
PARTIAL_POSITION_ENGINE_HEAD = "5e3aba0dfea1bce93b50c598e522b8b67902e19e"


def canonical_position_fields(receipt):
    """Copy the engine's position receipt; never reconstruct it from fill lots."""
    missing = [name for name in POSITION_RECEIPT_FIELDS if name not in receipt]
    if missing:
        raise ValueError("canonical_position_receipt_unbound:" + ",".join(missing))
    return {name: receipt[name] for name in POSITION_RECEIPT_FIELDS}


@dataclass(frozen=True)
class SealedCohort:
    source_id: str
    policy_version: str
    episodes: tuple
    source_refs: tuple
    all_overlapping_accounts_included: bool
    coverage: dict

