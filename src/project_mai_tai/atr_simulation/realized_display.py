"""Display an externally reviewed allocator receipt; never allocate executions.

Only startup reads the artifact. The reviewed digest is a separate Main operand,
not a value recovered from the artifact. No database, model or provider port.
"""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
from html import escape
import json
import math
import os
from pathlib import Path
import re
import stat

from .aggregate_entry_read_model import AggregateReadBinding


VERSION = "ATR_REALIZED_RECONCILIATION_V3_MANUAL_EXCLUSION_COMPLETE_SIGNALS"
DISPLAY_VERSION = "ATR_BOUND_REALIZED_DISPLAY_V2_COMPLETE_SIGNALS"
MAX_BYTES = 4 * 1024 * 1024
WINDOW = ("2026-09-22", "2026-10-23")


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _sha(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
             "realized_external_sha256_required")
    return value


def _pairs(items):
    value = {}
    for key, item in items:
        _require(key not in value, "realized_duplicate_json_key")
        value[key] = item
    return value


def _constant(value):
    raise ValueError("realized_nonfinite_json")


def _document(raw):
    def finite_float(value):
        number = float(value)
        _require(math.isfinite(number), "realized_nonfinite_json")
        return number
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant,
                          parse_float=finite_float)
    except (UnicodeError, RecursionError) as error:
        raise ValueError("realized_invalid_json_tree") from error


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode()


def _amount(value):
    _require(type(value) is str and len(value) <= 128
             and re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,4})?", value) is not None,
             "realized_finite_decimal_text_required")
    try:
        _require(Decimal(value).is_finite(), "realized_finite_decimal_text_required")
    except InvalidOperation as error:
        raise ValueError("realized_finite_decimal_text_required") from error
    return value


@dataclass(frozen=True, init=False)
class BoundRealizedDisplay:
    """Contains only detached display bytes, not a mutable allocator result."""

    raw: bytes

    def __init__(self, raw, *, expected_sha256, aggregate_binding):
        object.__setattr__(self, "raw", _projection(raw, expected_sha256=expected_sha256,
                                                  aggregate_binding=aggregate_binding))

    def view(self):
        return _document(self.raw)


def unknown_realized_view():
    return dict(version=DISPLAY_VERSION, status="UNKNOWN", receipt_sha256=None,
        complete_signal_gross_pnl=None, full_gross_pnl=None, matched_signals=None,
        unmatched_signals=None, matched_account_legs=None, signal_count=173,
        account_leg_count=253, fees_status="UNKNOWN", reasons={},
        scope="ALL_173_SELECTED_SIGNALS_NOT_REQUESTED_PAGE_OR_ACCOUNT_TOTAL",
        start=WINDOW[0], end=WINDOW[1], native_execution_qualified=False,
        included_in_live_totals=False)


def _projection(raw, *, expected_sha256, aggregate_binding):
    """Validate identity/shape only; copy allocator amounts, never recompute P&L.

    Main must independently review the actual-copy normalizer/allocator chain
    and issue expected_sha256. Hash and shape validation are NOT copy acceptance.
    """
    _sha(expected_sha256)
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES
             and hashlib.sha256(raw).hexdigest() == expected_sha256,
             "realized_external_receipt_sha_mismatch")
    _require(isinstance(aggregate_binding, AggregateReadBinding)
             and hashlib.sha256(aggregate_binding.publication_raw).hexdigest()
             == aggregate_binding.publication_sha256,
             "realized_accepted_aggregate_binding_required")
    expected = _document(aggregate_binding.publication_raw)
    _require(expected.get("input_sha256") == aggregate_binding.receipt_sha256
             and expected.get("source_id") == aggregate_binding.source_id,
             "realized_aggregate_identity_mismatch")
    result = _document(raw)
    _require(type(result) is dict and result.get("version") == VERSION
             and result.get("display_contract") == "COMPLETE_SIGNALS_ONLY_GROSS_FEES_NOT_INCLUDED_V1"
             and "status" not in result and result.get("native_execution_qualified") is False,
             "realized_completed_allocator_result_required")
    inputs = result.get("input_sha256")
    proof = result.get("restore_receipt")
    _require(type(inputs) is dict and set(inputs) == {
        "capture", "restore_receipt", "selected_signal_receipt"}, "realized_exact_input_binding_required")
    for value in inputs.values():
        _sha(value)
    _require(inputs["selected_signal_receipt"] == aggregate_binding.receipt_sha256,
             "realized_foreign_173_seed_refused")
    _require(type(proof) is dict and proof.get("version") == "ATR_RESTORED_FILL_NORMALIZATION_V1"
             and proof.get("receipt_type") == "NORMALIZATION_BINDING_NOT_RESTORE_EXECUTION"
             and proof.get("restore_complete") is True
             and proof.get("native_execution_qualified") is False
             and proof.get("source_counts") == {"fills": 20733, "broker_orders": 85174}
             and proof.get("capture_sha256") == inputs["capture"]
             and proof.get("receipt_sha256") == inputs["restore_receipt"]
             and type(proof.get("target_identity")) is dict and bool(proof["target_identity"]),
             "realized_bound_actual_copy_normalization_required")
    for field in ("restore_receipt_sha256", "main_authority_sha256", "projection_sha256"):
        _sha(proof.get(field))
    _require(len(_encode(proof["target_identity"])) <= 16384,
             "realized_bounded_target_identity_required")

    expected_legs = {}
    for row in expected["result_rows"]:
        identity = row["identity"]
        key = identity["account_id"] + ":" + row["order_id"]
        _require(key not in expected_legs, "realized_duplicate_accepted_leg")
        expected_legs[key] = (identity["account_id"], row["order_id"],
            tuple(row["raw_baseline_result"]["reference_key"]), row["raw_baseline_result"]["recorded_aggregate"]["quantity"])
    _require(len(expected_legs) == 253 and len({v[2] for v in expected_legs.values()}) == 173,
             "realized_exact_173_253_binding_required")
    legs, signals = result.get("account_legs"), result.get("signals")
    _require(type(legs) is list and len(legs) == 253 and type(signals) is list
             and len(signals) == 173, "realized_complete_result_denominators_required")
    seen, by_signal, reasons, matched_legs = {}, {}, Counter(), 0
    for leg in legs:
        _require(type(leg) is dict and type(leg.get("seed_id")) is str
                 and leg["seed_id"] in expected_legs and leg["seed_id"] not in seen,
                 "realized_duplicate_or_foreign_leg")
        account, order, signal, quantity = expected_legs[leg["seed_id"]]
        _require(leg.get("account") == account and leg.get("client_order_id") == order
                 and leg.get("signal_key") == list(signal)
                 and type(leg.get("quantity")) in (str, int)
                 and Decimal(_amount(str(leg["quantity"]))) == Decimal(str(quantity)),
                 "realized_exact_account_order_signal_quantity_required")
        issues, fills = leg.get("issues"), leg.get("fill_ids")
        _require(type(issues) is list and len(issues) <= 32
                 and all(type(x) is str and 0 < len(x) <= 128 for x in issues)
                 and type(fills) is list and len(fills) <= 20733
                 and all(type(x) is str and 0 < len(x) <= 128 for x in fills)
                 and len(set(fills)) == len(fills),
                 "realized_bounded_execution_references_required")
        try:
            for issue in issues:
                issue.encode("utf-8", errors="strict")
        except UnicodeError as error:
            raise ValueError("realized_utf8_issue_required") from error
        if leg.get("status") == "MATCHED":
            _require(not issues and bool(fills), "realized_matched_leg_requires_execution_references")
            _amount(leg.get("gross_pnl"))
            matched_legs += 1
        else:
            _require(leg.get("status") == "UNKNOWN" and bool(issues)
                     and leg.get("gross_pnl") is None, "realized_unknown_leg_cannot_be_zero")
            reasons.update(issues)
        seen[leg["seed_id"]] = leg
        by_signal.setdefault(signal, []).append(leg)
    matched, signal_keys, complete_total = 0, set(), Decimal(0)
    for signal in signals:
        _require(type(signal) is dict and type(signal.get("signal_key")) is list
                 and len(signal["signal_key"]) == 3
                 and all(type(x) is str for x in signal["signal_key"]),
                 "realized_signal_identity_required")
        key = tuple(signal["signal_key"])
        _require(key in by_signal and key not in signal_keys and type(signal.get("legs")) is list
                 and len(signal["legs"]) == len(by_signal[key])
                 and sorted(_encode(x) for x in signal["legs"])
                 == sorted(_encode(x) for x in by_signal[key]),
                 "realized_signal_membership_mismatch")
        known = all(x["status"] == "MATCHED" for x in by_signal[key])
        _require(signal.get("status") == ("MATCHED" if known else "UNKNOWN"),
                 "realized_signal_status_mismatch")
        if known:
            complete_total += Decimal(_amount(signal.get("gross_pnl")))
            matched += 1
        else:
            _require(signal.get("gross_pnl") is None, "realized_unknown_signal_cannot_be_zero")
        signal_keys.add(key)
    _require(type(result.get("matched_signals")) is int and result["matched_signals"] == matched
             and type(result.get("unmatched_signals")) is int
             and result["unmatched_signals"] == 173-matched, "realized_signal_counts_mismatch")
    _require(type(result.get("complete_signal_count")) is int
             and result["complete_signal_count"] == matched
             and type(result.get("unreconciled_signal_count")) is int
             and result["unreconciled_signal_count"] == 173-matched,
             "realized_complete_signal_denominator_mismatch")
    _require(len(reasons) <= 64, "realized_bounded_reason_codes_required")
    # Check declared gross-field arithmetic, never allocate fills or derive
    # a trade's PnL from price/quantity. Display still copies the bound field.
    complete_gross = _amount(result.get("complete_signal_gross_pnl"))
    _require(Decimal(complete_gross) == complete_total,
             "realized_complete_signal_gross_mismatch")
    _require(matched != 0 or Decimal(complete_gross) == 0,
             "realized_no_complete_signals_requires_zero_raw_subtotal")
    partial_legs = [leg for group in by_signal.values()
        if any(leg["status"] == "UNKNOWN" for leg in group)
        for leg in group if leg["status"] == "MATCHED"]
    partial_count = len(partial_legs)
    _require(type(result.get("known_partial_account_leg_count")) is int
             and result["known_partial_account_leg_count"] == partial_count,
             "realized_partial_known_leg_count_mismatch")
    members = result.get("known_partial_account_legs")
    _require(type(members) is list and len(members) == partial_count
             and sorted(_encode(leg) for leg in members)
             == sorted(_encode(leg) for leg in partial_legs),
             "realized_partial_known_leg_membership_mismatch")
    partial_gross = _amount(result.get("known_partial_account_leg_gross_pnl"))
    _require(partial_count != 0 or Decimal(partial_gross) == 0,
             "realized_no_partial_legs_requires_zero_audit_subtotal")
    partial_total = sum((Decimal(leg["gross_pnl"]) for leg in partial_legs), Decimal(0))
    _require(Decimal(partial_gross) == partial_total,
             "realized_partial_known_leg_gross_mismatch")
    audit_total = _amount(result.get("matched_component_gross_pnl"))
    _require(Decimal(audit_total) == complete_total + partial_total,
             "realized_known_leg_audit_bridge_mismatch")
    full = result.get("full_gross_pnl")
    if matched == 173:
        _require(Decimal(_amount(full)) == Decimal(complete_gross),
                 "realized_complete_full_signal_mismatch")
    else:
        _require(full is None, "realized_incomplete_cannot_claim_full_total")
    fees = result.get("fee_key_census")
    _require(type(fees) is dict and all(type(k) is str and len(k) <= 2048
             and type(v) is int and v > 0 for k, v in fees.items())
             and result.get("fees_status") == ("RECORDED_FIELDS_REQUIRE_SEMANTIC_REVIEW"
                 if fees else "fees not included"), "realized_fee_caveat_required")
    view = unknown_realized_view()
    view.update(status="BOUND_COMPLETE_SIGNAL_GROSS" if matched else "UNKNOWN",
        receipt_sha256=expected_sha256, seed_receipt_sha256=aggregate_binding.receipt_sha256,
        complete_signal_gross_pnl=complete_gross if matched else None, full_gross_pnl=full,
        matched_signals=matched, unmatched_signals=173-matched,
        matched_account_legs=matched_legs, fees_status=result["fees_status"],
        reasons=dict(sorted(reasons.items())), source_sha256=inputs,
        actual_restore_receipt_sha256=proof["restore_receipt_sha256"],
        main_authority_sha256=proof["main_authority_sha256"],
        target_identity_sha256=hashlib.sha256(_encode(proof["target_identity"])).hexdigest())
    return _encode(view)


def prepare_realized_display(raw, *, expected_sha256, aggregate_binding):
    return BoundRealizedDisplay(raw, expected_sha256=expected_sha256,
                               aggregate_binding=aggregate_binding)


def configured_realized_display(path, expected_sha256, *, aggregate_binding):
    """Read once before serving. Explicit unsafe/mismatched configuration refuses."""
    if path is None and expected_sha256 is None:
        return None
    _require(type(path) is str and type(expected_sha256) is str,
             "realized_path_and_external_sha_required_together")
    _sha(expected_sha256)
    selected = Path(path)
    _require(selected.is_absolute() and selected.resolve(strict=True) == selected,
             "realized_canonical_artifact_path_required")
    fd = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_BYTES
                 and info.st_mode & 0o022 == 0, "realized_private_bounded_regular_artifact_required")
        raw = stream.read(MAX_BYTES+1)
    return prepare_realized_display(raw, expected_sha256=expected_sha256,
                                   aggregate_binding=aggregate_binding)


def render_realized_display(view):
    """Separate month ledger; do not touch LIVE snapshots or modeled subtotals."""
    if view is None:
        view = unknown_realized_view()
    _require(type(view) is dict and view.get("version") == DISPLAY_VERSION
             and view.get("included_in_live_totals") is False
             and view.get("native_execution_qualified") is False,
             "realized_detached_display_required")
    if (view.get("status") != "BOUND_COMPLETE_SIGNAL_GROSS"
            or view.get("receipt_sha256") is None):
        return ('<section data-realized-reconciliation="true">'
                '<p>realized: under reconciliation</p></section>')
    _sha(view["receipt_sha256"])
    def text(value):
        return escape("UNKNOWN" if value is None else str(value))
    amount = view["complete_signal_gross_pnl"]
    signed = format(Decimal(_amount(amount)), "+.2f")
    money = signed[0] + "$" + signed[1:]  # Presentation only; API retains exact gross.
    reason_text = "; ".join(f"{key}: {value}" for key, value in view["reasons"].items())
    return ('<section data-realized-reconciliation="true"><h3>Actual fills reconciliation</h3>'
        '<p>All 173 selected signals / 253 account legs; 2026-09-22 through 2026-10-23; '
        'not a requested-page or account-wide total.</p><p>Realized (fills): '
        + text(money) + ' gross over ' + text(view["matched_signals"])
        + ' complete signals &middot; ' + text(view["unmatched_signals"])
        + ' signals unreconciled &middot; fees not included.</p>'
        + '<p>Receipt fee status: ' + text(view["fees_status"])
        + '; no native statement completeness claim. Modeled exits remain simulated; '
        'this ledger is never added to LIVE daily totals.</p><p>Receipt SHA256: '
        + text(view["receipt_sha256"]) + '</p><p>Unmatched account-leg reason occurrences: '
        + text(reason_text or None) + '</p></section>')
