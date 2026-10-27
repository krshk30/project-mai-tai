"""C1 10:19:36: exact existing classifier, round lots, all-print body retained."""

from functools import lru_cache
import hashlib
import importlib.util
from pathlib import Path
import sys

from .types import ConditionRuleEvidence, ExecutionEligibility, ExecutionPolicy, TradePrint


CLASSIFIER_SHA256 = "e9da2f80e45439fdcc5a9d32565af7d62f8b6559a02f85e55eb14e12f182edee"


@lru_cache(maxsize=1)
def classifier_module():
    # Loading the package executes momentum_paper.__init__/engine. Load only
    # this pinned standalone stdlib file, not any provider/application module.
    path = Path(__file__).resolve().parents[1] / "momentum_paper/conditions.py"
    if hashlib.sha256(path.read_bytes()).hexdigest() != CLASSIFIER_SHA256:
        raise ValueError("existing_condition_classifier_source_mismatch")
    name = "_atr_sim_exact_momentum_condition_classifier"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=8)
def _rules(policy):
    if (not policy.version or not policy.source.artifact or not policy.source.pointer
            or len(policy.source.sha256) != 64
            or any(char not in "0123456789abcdef" for char in policy.source.sha256)):
        raise ValueError("source_backed_snapshot_required")
    module = classifier_module()
    result = {}
    for rule in policy.rules:
        if type(rule.condition_id) is not int or rule.condition_id < 0 or rule.condition_id in result:
            raise ValueError("unique_numeric_condition_rule_required")
        if any(type(flag) is not bool for flag in (
            rule.updates_high_low, rule.updates_open_close, rule.updates_volume,
        )):
            raise ValueError("explicit_snapshot_boolean_flags_required")
        result[rule.condition_id] = module.ConditionRule(rule.condition_id, rule.name,
            rule.updates_high_low, rule.updates_open_close, rule.updates_volume, {})
    if not result:
        raise ValueError("nonempty_massive_snapshot_required")
    return result


def classify_execution(trade: TradePrint, raw_conditions, policy: ExecutionPolicy | None,
                       *, conditions_present: bool = True) -> ExecutionEligibility:
    """Attach evidence; do not remove this print from the body denominator."""
    if type(trade.size) is not int or trade.size < 0:
        raise ValueError("nonnegative_integer_size_required")
    if type(conditions_present) is not bool:
        raise ValueError("conditions_presence_boolean_required")
    raw = tuple(raw_conditions) if isinstance(raw_conditions, (list, tuple)) else raw_conditions
    eligible, reason = None, "UNKNOWN_EXECUTION_POLICY"
    if trade.size < 100:
        eligible, reason = False, "ROUND_LOT_REQUIRED"
    elif not conditions_present:
        reason = "UNKNOWN_CONDITIONS_COLUMN_MISSING"
    elif policy is not None:
        try:
            if raw is None or raw == "" or raw == ():
                codes = ()
            elif isinstance(raw, str):
                parts = raw.split(",")
                if any(not part.isascii() or not part.isdigit() for part in parts):
                    raise ValueError("malformed")
                codes = tuple(int(part) for part in parts)
            elif isinstance(raw, tuple) and all(type(code) is int and code >= 0 for code in raw):
                codes = raw
            else:
                raise ValueError("malformed")
        except ValueError:
            reason = "UNKNOWN_MALFORMED_CONDITIONS"
        else:
            eligible, reason = classifier_module().classify_condition_codes(_rules(policy), codes)
    return ExecutionEligibility(trade.key.raw_id, trade.key.source, trade.size,
        raw, policy, eligible, reason, conditions_present)


def execution_status(trade: TradePrint) -> tuple[bool | None, str]:
    evidence = trade.execution
    if evidence is None:
        missing = classify_execution(trade, None, None, conditions_present=False)
        return (False, missing.reason) if missing.eligible is False else (None, "UNKNOWN_EXECUTION_EVIDENCE")
    if evidence.eligible is not None and type(evidence.eligible) is not bool:
        raise ValueError("tristate_execution_evidence_required")
    if type(evidence.size) is not int:
        raise ValueError("integer_execution_evidence_size_required")
    expected = classify_execution(trade, evidence.raw_conditions, evidence.policy,
                                  conditions_present=evidence.conditions_present)
    if evidence != expected:
        raise ValueError("execution_evidence_identity_or_classification_mismatch")
    return evidence.eligible, evidence.reason


def note_unknown(issues: list[str], issue: str = "UNKNOWN_EXECUTION_ELIGIBILITY") -> None:
    if issue not in issues:
        issues.append(issue)


def policy_from_payload(payload: dict, source) -> ExecutionPolicy:
    """Retained flat rule map or ConditionSnapshot payload, no invented date."""
    if not isinstance(payload, dict):
        raise ValueError("snapshot_mapping_required")
    values = payload.get("rules", payload)
    if not isinstance(values, dict):
        raise ValueError("snapshot_rule_mapping_required")
    rules = []
    for key, value in values.items():
        if not isinstance(key, str) or not key.isascii() or not key.isdigit() or not isinstance(value, dict):
            raise ValueError("snapshot_rule_entry_required")
        if "condition_id" in value and (type(value["condition_id"]) is not int or value["condition_id"] != int(key)):
            raise ValueError("snapshot_code_identity_mismatch")
        flags = tuple(value.get(name) for name in ("updates_high_low", "updates_open_close", "updates_volume"))
        if any(type(flag) is not bool for flag in flags):
            raise ValueError("explicit_snapshot_boolean_flags_required")
        rules.append(ConditionRuleEvidence(int(key), str(value.get("name", "")), *flags))
    policy = ExecutionPolicy("C1_20261023_101936_MASSIVE:" + source.sha256, source,
                             tuple(sorted(rules, key=lambda rule: rule.condition_id)))
    _rules(policy)
    return policy


def condition_coverage(trades):
    from decimal import Decimal
    total = len(trades)
    unknown = sum(execution_status(trade)[0] is None for trade in trades)
    blanks = sum(trade.execution is not None and trade.execution.conditions_present
                 and trade.execution.raw_conditions in (None, "", ()) for trade in trades)
    missing = sum(trade.execution is None or not trade.execution.conditions_present for trade in trades)
    return {"total_prints": total, "blank_or_null_prints": blanks,
            "actual_blank_or_null_pct": None if not total else Decimal(blanks) * 100 / total,
            "missing_condition_evidence_prints": missing, "unknown_eligibility_prints": unknown,
            "note": "ALL raw prints; CSV blank conflates empty/NULL. No X threshold invented."}
