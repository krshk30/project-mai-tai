"""Canonical FIRSTBAR result/store adapter; no entry, fill, exit or PnL oracle."""

from collections.abc import Mapping
from dataclasses import dataclass

from .firstbar_binding import (
    FIRSTBAR_ENGINE_HEAD, FIRSTBAR_FILL_POLICY, FIRSTBAR_RESULT_VERSION,
    load_firstbar_engine,
)
from .repository import digest, json_value
from .service_contract import canonical_position_fields


IDENTITY_FIELDS = ("trading_day", "symbol", "broker", "account_id", "strategy",
                   "flip_id", "segment_id")
FIRSTBAR_MODES = {"FIRSTBAR", "FIRSTBAR_X3"}
FIRSTBAR_SEED_LABEL = "replay, in-sample; underlying capture completeness UNKNOWN"


def firstbar_result_row(result, *, population_label="replay, in-sample"):
    """Copy a typed, versioned Gibbs allocation result, retaining its raw facts."""
    from .firstbar_replay import FirstBarReplayResult

    if not isinstance(result, FirstBarReplayResult):
        raise ValueError("actual_firstbar_replay_result_required")
    if population_label not in ("replay, in-sample", FIRSTBAR_SEED_LABEL):
        raise ValueError("firstbar_population_label_required")
    raw = json_value(result)
    if (raw["version"] != FIRSTBAR_RESULT_VERSION or raw["mode"] not in FIRSTBAR_MODES
            or raw["fill_policy"] != FIRSTBAR_FILL_POLICY):
        raise ValueError("firstbar_projection_version_or_policy_mismatch")
    identity = raw["identity"]
    if any(not isinstance(identity.get(name), str) or not identity[name] for name in IDENTITY_FIELDS):
        raise ValueError("complete_firstbar_identity_required")
    position = raw["position"]
    if position["fills"] != raw["fills"]:
        raise ValueError("firstbar_accepted_position_fill_mismatch")
    if (any(type(fill["quantity"]) is not int or fill["quantity"] <= 0 for fill in raw["fills"])
            or type(position["filled_qty"]) is not int
            or position["filled_qty"] != sum(fill["quantity"] for fill in raw["fills"])):
        raise ValueError("firstbar_accepted_position_quantity_mismatch")
    if len({fill["fill_id"] for fill in raw["fills"]}) != len(raw["fills"]):
        raise ValueError("duplicate_firstbar_accepted_fill_identity")
    if raw["fills"]:
        if position["first_fill_ts"] != min(fill["at"]["event_ns"] for fill in raw["fills"]):
            raise ValueError("firstbar_first_fill_clock_mismatch")
        kind = "TRADE"
    elif raw["decision"].startswith("SKIPPED_") and not raw["issues"]:
        kind = "SKIP"
    else:
        kind = "UNKNOWN"
    # Unknown economics never erases already accepted positive fills.
    known_pnl = raw["gross_pnl"] if raw["decision"] == "TRADE_CLOSED" and not raw["issues"] else None
    key = digest((identity, raw["mode"], raw["fill_policy"]))
    return {**raw, **canonical_position_fields(position), "model": raw["fill_policy"],
        "engine_dependency_head": FIRSTBAR_ENGINE_HEAD,
        "outcome_id": key, "outcome_kind": kind,
        "trade_id": key if kind == "TRADE" else None,
        "engine_gross_pnl": raw["gross_pnl"], "gross_pnl": known_pnl,
        "qualification": "UNQUALIFIED_REPLAY", "live_totals_included": False,
        "population_label": population_label}


@dataclass(frozen=True)
class SealedFirstBarCohort:
    """Caller binds actual causal inputs/evidence; no missing-population defaults."""

    source_id: str
    input_sha256: str
    inputs: tuple
    source_refs: tuple
    coverage: dict
    all_overlapping_accounts_included: bool


def publish_firstbar_cohort(repository, cohort):
    """ONE canonical cohort execution and atomic append; no source/provider IO.

The caller must use the existing causal assembler to establish membership.
This function neither discovers watched symbols nor infers coverage from rows.
"""
    if (not isinstance(cohort, SealedFirstBarCohort)
            or cohort.all_overlapping_accounts_included is not True
            or not isinstance(cohort.inputs, tuple) or not cohort.inputs
            or not isinstance(cohort.source_refs, tuple) or not cohort.source_refs
            or not isinstance(cohort.coverage, Mapping)):
        raise ValueError("explicit_sealed_firstbar_cohort_required")
    engine, api = load_firstbar_engine()
    if not all(isinstance(item, api.FirstBarReplayInput) for item in cohort.inputs):
        raise ValueError("canonical_firstbar_replay_inputs_required")
    results = engine.simulate_many(cohort.inputs,
        fill_policy=api.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)
    from .firstbar_ui_reader import SEED_BINDING

    seed_bound = cohort.coverage.get("reference_seed_binding") == SEED_BINDING
    label = FIRSTBAR_SEED_LABEL if seed_bound else "replay, in-sample"
    rows = tuple(firstbar_result_row(result, population_label=label) for result in results)
    expected = {(tuple(json_value(item.episode.identity)[name] for name in IDENTITY_FIELDS), mode)
                for item in cohort.inputs for mode in FIRSTBAR_MODES}
    observed = {(tuple(row["identity"][name] for name in IDENTITY_FIELDS), row["mode"]) for row in rows}
    if len(rows) != len(expected) or observed != expected:
        raise ValueError("firstbar_complete_cohort_result_membership_mismatch")
    coverage = json_value(dict(cohort.coverage))
    # This witnesses the executed adapter dependency, not historical/native completeness.
    coverage.update(population_label=label, engine_dependency_head=FIRSTBAR_ENGINE_HEAD,
                    engine_contract_version=FIRSTBAR_RESULT_VERSION,
                    fill_model_policy=FIRSTBAR_FILL_POLICY,
                    qualification="UNQUALIFIED_REPLAY")
    repository.publish(source_id=cohort.source_id, input_sha256=cohort.input_sha256,
        policy_version=FIRSTBAR_RESULT_VERSION, source_refs=cohort.source_refs,
        coverage=coverage, result_rows=rows)
    return rows
