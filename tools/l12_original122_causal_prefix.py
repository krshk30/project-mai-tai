"""Source-specific real mathematical producer; safe to project tools-only to OLD.

Only stdlib imports until Main invokes the reviewed local reconstruction. No
NEW model, store, settings defaults, factory, provider or native book imports.
"""

from decimal import Decimal
from hashlib import sha256
import json
import math


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def produce_causal_prefix(case, bars, *, period, factor, review, world, source_guard):
    """Produce expectation fields from retained inputs, never target readback.

    review binds the exact entire eligible bar sequence and explicit spanning
    pairs. The result is a reconstructed local model, NOT a readiness/delivery
    or permission certificate. Missing review must not be substituted.
    """
    from tools.l12_original122_branch_adapter import validate_original122_world

    world = validate_original122_world(world)
    if (type(period) is not int or period <= 0 or type(factor) not in (int, float)
            or not math.isfinite(factor) or factor <= 0 or type(bars) is not list
            or not 0 < len(bars) <= 4096):
        raise ValueError("explicit_bounded_causal_math_inputs_required")
    input_value = dict(case_id=case["case_id"], clock_ms=case["clock_ms"], anchor_ms=case["anchor_ms"],
        bars=bars, period=period, factor=factor, world=world,
        spanning_pairs=review.get("spanning_pairs") if type(review) is dict else None)
    digest = sha256(canonical(input_value)).hexdigest()
    if (type(review) is not dict or set(review) != {"input_sha256", "review_sha256", "scope", "spanning_pairs"}
            or review["input_sha256"] != digest or review["scope"] != "RECONSTRUCTED_LOCAL_NOT_HISTORICAL_DELIVERY_OR_READY"
            or type(review["review_sha256"]) is not str or len(review["review_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in review["review_sha256"])
            or review["review_sha256"] == "0" * 64 or type(review["spanning_pairs"]) is not list):
        raise ValueError("independent_exact_prefix_review_required")
    if source_guard(world, review, input_value) is not True:
        raise ValueError("actual_world_source_and_prefix_review_not_accepted")
    from project_mai_tai.strategy_core.session_line_restore import HistoryBar, RebuildInput, build_session_line
    from tools.l12_original122_branch_adapter import _readback_value

    values, stamps = [], []
    for item in bars:
        if set(item) != {"bar", "reference"} or type(item["reference"]) is not dict:
            raise ValueError("every_causal_bar_requires_original_row_reference")
        row = item["bar"]
        if set(row) != {"symbol", "timestamp_ms", "open", "high", "low", "close", "volume"}:
            raise ValueError("exact_HistoryBar_input_required")
        stamp = row["timestamp_ms"]
        if (row["symbol"] != case["symbol"] or type(stamp) is not int or stamp % 60000
                or not case["anchor_ms"] <= stamp or stamp + 60000 > case["clock_ms"]
                or type(row["volume"]) is not int or row["volume"] < 0):
            raise ValueError("foreign_future_open_or_invalid_causal_bar")
        prices = []
        for name in ("open", "high", "low", "close"):
            raw = row[name]
            if type(raw) is not str:
                raise ValueError("exact_source_price_text_required")
            value = Decimal(raw)
            if not value.is_finite() or value < 0 or not math.isfinite(float(value)):
                raise ValueError("finite_real_math_price_required")
            prices.append(float(value))  # Actual HistoryBar/strategy producer uses float prices.
        values.append(HistoryBar(row["symbol"], *prices, row["volume"], stamp))
        stamps.append(stamp)
    if stamps != sorted(set(stamps)):
        raise ValueError("chronological_unique_bar_ids_required_no_sort_or_dedup_here")
    adjacent_gaps = {(left, right) for left, right in zip(stamps, stamps[1:]) if right - left > 60000}
    spans = review["spanning_pairs"]
    if (any(type(pair) is not list or len(pair) != 2 or any(type(v) is not int for v in pair) for pair in spans)
            or len({tuple(p) for p in spans}) != len(spans)
            or any(tuple(pair) not in adjacent_gaps for pair in spans)):
        raise ValueError("only_explicit_reviewed_adjacent_gap_spans_allowed")
    request = RebuildInput(case["symbol"], 1, 1, case["anchor_ms"], stamps[-1], tuple(values),
        tuple(tuple(pair) for pair in spans))
    snapshot = build_session_line(request, period, float(factor))
    if source_guard(world, review, input_value) is not True:
        raise ValueError("world_source_acceptance_lost_after_math")
    indicator = dict(snapshot.indicator)
    # Real SymbolState keeps300 bars; full prefix above is NOT trimmed for math.
    state_bars = [dict(timestamp_ms=row.timestamp_ms, open=row.open, high=row.high,
        low=row.low, close=row.close, volume=row.volume) for row in values[-300:]]
    return dict(input_sha256=digest, review_sha256=review["review_sha256"], prefix_count=len(values),
        full_indicator=_readback_value(indicator), actual_signal=_readback_value(dict(snapshot.signal)),
        expected_state_fragment=dict(bars=state_bars, atr_state=indicator["atr_state"],
            atr_short_flip_bar_ts=indicator["atr_short_flip_bar_ts"]),
        bars_projection="REAL_300_BAR_STATE_SUFFIX_FULL_PREFIX_MATH_UNTRIMMED",
        independence="REAL_SOURCE_PREFIX_PRODUCER_NOT_ASSESSED_OBJECT_READBACK",
        scope=review["scope"], native_permission="UNKNOWN", permission_granted=False)
