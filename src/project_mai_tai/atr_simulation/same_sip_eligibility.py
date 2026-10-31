"""Successor-only identity rule lookup; original classifier/seals stay unchanged.

Classification and evidence validation below are literal original function
bodies. Only rule-map lookup uses an eight-entry strong-reference identity
cache. First use still calls the original validating/source-pinned rule loader.
No whole-trade/evidence validation is skipped; no global module is rebound.
"""
from collections import OrderedDict
from . import execution_eligibility as legacy
from .types import (ConditionRuleEvidence, ExecutionEligibility, ExecutionPolicy,
                    TradePrint, SourceRef, EvidenceKind)

classifier_module = legacy.classifier_module
note_unknown = legacy.note_unknown
_RULES_BY_IDENTITY = OrderedDict()


def _cacheable(policy):
    # Only the exact immutable typed graph is retained. Odd/malformed/custom
    # inputs keep the original loader's outcome and cannot become cache hits.
    return (type(policy) is ExecutionPolicy and type(policy.version) is str
        and type(policy.source) is SourceRef and type(policy.rules) is tuple
        and all(type(value) is str for value in (policy.source.artifact, policy.source.sha256, policy.source.pointer))
        and type(policy.source.kind) is EvidenceKind
        and (policy.source.asof_ns is None or type(policy.source.asof_ns) is int)
        and all(type(rule) is ConditionRuleEvidence and type(rule.condition_id) is int
            and type(rule.name) is str and all(type(flag) is bool for flag in
                (rule.updates_high_low, rule.updates_open_close, rule.updates_volume))
            for rule in policy.rules))


def _rules(policy):
    key = id(policy)
    found = _RULES_BY_IDENTITY.get(key)
    if found is not None and found[0] is policy:
        _RULES_BY_IDENTITY.move_to_end(key)
        return found[1]
    # Original validates policy shape, unique IDs/flags and pinned classifier.
    # Its hash/equality is paid once for this object, never per print on a hit.
    result = legacy._rules(policy)
    if _cacheable(policy):
        _RULES_BY_IDENTITY[key] = (policy, result)
        _RULES_BY_IDENTITY.move_to_end(key)
        while len(_RULES_BY_IDENTITY) > 8:
            _RULES_BY_IDENTITY.popitem(last=False)
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
