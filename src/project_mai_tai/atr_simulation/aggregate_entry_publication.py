"""Pure immutable aggregate receipt -> four-SIM-table writer arguments.

No engine, storage, DB, application, provider, or producer module imports.
Main supplies independently reviewed producer head/pins and the final receipt
SHA. Neither a receipt's self-attestation nor this preparation is a DB save.
"""

import base64
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import PurePosixPath
import re

from .aggregate_entry_contract import (
    ENTRY_ASSUMPTION, EXIT_POLICY, LABEL, MODE, MODEL, VERSION, require_provenance,
)
from .service_contract import canonical_position_fields


PUBLICATION_VERSION = "ATRSIM_AGGREGATE_PUBLICATION_V1"
ADAPTER_VERSION = "ATRSIM_RECORDED_AGGREGATE_ENTRY_BASELINE_V1"
INDEPENDENT_ADAPTER_VERSION = "ATRSIM_RECORDED_AGGREGATE_ENTRY_BASELINE_V2_INDEPENDENT_ACCOUNT_LEGS"
CONFIRMATION_ADAPTER_VERSION = "ATRSIM_RECORDED_AGGREGATE_ENTRY_BASELINE_V3_CONFIRMATION_EXIT"
CONFIRMATION_POLICY_VERSION = "C1_20261024_0000_ENTRY_BAR_SHORT_CONFIRMATION_EXIT"
INDEPENDENT_VERSIONS = (INDEPENDENT_ADAPTER_VERSION, CONFIRMATION_ADAPTER_VERSION)
INDEPENDENT_COHORT_POLICY = "INDEPENDENT_RECORDED_ACCOUNT_LEGS_SAME_ACCOUNT_OVERLAP_INFORMATIONAL"
VARIANT = "ACTUAL_AGGREGATE_ENTRY_SAME_EXIT_POLICY"
ENTRY_MODEL = "RECORDED_ORDER_AGGREGATE_AT_RECORDED_FILLED_NS"
ASSUMPTION = "COLLAPSE_RECORDED_ORDER_TO_ONE_ENTRY_AT_RECORDED_FILLED_NS"
TIE_POLICY = "OMIT_ALL_PRINTS_AT_RECORDED_AGGREGATE_SIP"
CAPTURE_LABEL = "replay, in-sample; underlying capture completeness UNKNOWN"
KEYS_SHA = "ad69e2256eacbc9cdd108e0b90af26bb032241d8f92e016a2c624c9083272162"
BUY_SHA = "a7c719d0a0a7015ec8062ad18483de71dacd2ca1abfe2eebee440a43d125b750"
FAC9_SHA = "fac9c0fc0efd00f15a00776f869eafb28b4010f47c57dfc86e0a105357c93e2d"
HELD_F7_SHA = "f7a604894543b56c789336bd4d1ebc9015f21f3f5a01e5509c50bfe8f380592f"
MAX_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
IDENTITY_FIELDS = ("trading_day", "symbol", "broker", "account_id", "strategy", "flip_id", "segment_id")
PRODUCER_FILES = (
    "tools/atr_firstbar_live_baseline.py", "src/project_mai_tai/__init__.py",
    "src/project_mai_tai/atr_simulation/__init__.py", "src/project_mai_tai/backtest/__init__.py",
    "src/project_mai_tai/backtest/atr_oracle.py", "src/project_mai_tai/momentum_paper/conditions.py",
    "src/project_mai_tai/broker_adapters/target_price.py",
    *(f"src/project_mai_tai/atr_simulation/{name}.py" for name in (
        "firstbar_live_baseline", "firstbar", "firstbar_exits", "firstbar_target_rule",
        "firstbar_study", "firstbar_replay", "firstbar_reference", "types", "exits", "execution_eligibility")),
)
EXCLUDED_KEYS = {
    ("BENF", "2026-09-23", "12:53:25"), ("MI", "2026-10-05", "09:34:24"),
    ("BIYA", "2026-10-07", "11:17:38"), ("BIYA", "2026-10-07", "11:58:39"),
    ("PFAI", "2026-10-07", "13:17:25"), ("SAIQ", "2026-10-22", "11:22:04"),
    ("AIOK", "2026-10-23", "14:46:16"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checksum(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
            "aggregate_explicit_sha_required")
    return value


def encode(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def document(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "aggregate_duplicate_json_key")
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("aggregate_nonfinite_json")
    return json.loads(raw, object_pairs_hook=pairs, parse_float=Decimal, parse_constant=nonfinite)


def amount(value):
    require(not isinstance(value, bool) and isinstance(value, (str, int, Decimal)), "aggregate_decimal_required")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("aggregate_decimal_required") from error
    require(result.is_finite(), "aggregate_finite_money_required")
    return result


def source_ref(value):
    require(isinstance(value, dict) and isinstance(value.get("artifact"), str)
            and bool(value["artifact"]) and isinstance(value.get("pointer"), str) and bool(value["pointer"])
            and value.get("kind") in {"RETAINED", "DERIVED", "CONTROLLED", "REFETCHED_SAME_ENDPOINT", "UNKNOWN"},
            "aggregate_source_reference_required")
    checksum(value.get("sha256"))
    return value


@dataclass(frozen=True)
class AggregateProducerBinding:
    root: str
    reviewed_head: str
    source_pins: tuple[tuple[str, str], ...]
    account_brokers: tuple[tuple[str, str], ...]
    strategy_code: str


@dataclass(frozen=True)
class AggregatePublication:
    receipt_raw: bytes
    receipt_sha256: str
    receipt_source_raw: bytes
    publication_raw: bytes
    publication_sha256: str


def producer_pins(receipt, producer):
    require(isinstance(producer, AggregateProducerBinding), "aggregate_external_producer_binding_required")
    require(re.fullmatch(r"[0-9a-f]{40}", producer.reviewed_head or "") is not None,
            "aggregate_external_producer_head_required")
    root = PurePosixPath(producer.root)
    require(root.is_absolute() and ".." not in root.parts, "aggregate_absolute_reviewed_root_required")
    pins = dict(producer.source_pins)
    require(len(pins) == len(producer.source_pins) and set(pins) == set(PRODUCER_FILES),
            "aggregate_exact_external17_members_required")
    expected = {str(root / name): checksum(sha) for name, sha in pins.items()}
    require(receipt.get("reviewed_head") == producer.reviewed_head and receipt.get("source_pins") == expected,
            "aggregate_external_producer_receipt_mismatch")
    brokers = dict(producer.account_brokers)
    require(len(brokers) == len(producer.account_brokers) and bool(brokers)
            and all(isinstance(a, str) and a and b in ("schwab", "webull") for a, b in brokers.items())
            and isinstance(producer.strategy_code, str) and bool(producer.strategy_code),
            "aggregate_explicit_account_strategy_binding_required")
    return expected, brokers


def canonical_row(raw, *, key, producer, pins, brokers, buy_source, adapter_version=ADAPTER_VERSION):
    require(isinstance(raw, dict) and raw.get("reference_key") == key
            and "mode" not in raw and "model" not in raw
            and "modeled_gross_pnl" in raw
            and raw.get("version") == adapter_version and raw.get("variant") == VARIANT
            and raw.get("entry_model") == ENTRY_MODEL and raw.get("label") == LABEL
            and raw.get("history_label") == CAPTURE_LABEL and raw.get("exit_policy_version") == EXIT_POLICY,
            "aggregate_raw_leg_contract_required")
    identity = raw.get("identity")
    require(isinstance(identity, dict) and set(identity) == set(IDENTITY_FIELDS)
            and all(isinstance(identity[n], str) and identity[n] for n in IDENTITY_FIELDS),
            "aggregate_complete_account_identity_required")
    date.fromisoformat(identity["trading_day"])
    order = raw.get("order_id")
    require(isinstance(order, str) and bool(order) and identity["trading_day"] == key[1]
            and identity["symbol"] == key[0] and identity["account_id"] in brokers
            and identity["broker"] == brokers[identity["account_id"]]
            and identity["strategy"] == producer.strategy_code
            and identity["flip_id"] == "RECORDED_AGGREGATE_ORDER:" + order
            and identity["segment_id"] == "ACTUAL_ENTRY_BASELINE:" + order,
            "aggregate_original_account_order_route_required")
    leg, entry, position = raw.get("recorded_aggregate"), raw.get("aggregate_entry"), raw.get("position")
    require(isinstance(leg, dict) and isinstance(entry, dict) and isinstance(position, dict),
            "aggregate_original_entry_position_required")
    source_ref(leg.get("source"))
    require(leg.get("account") == identity["account_id"] and leg.get("symbol") == key[0]
            and leg.get("order_id") == order and type(leg.get("quantity")) is int and leg["quantity"] > 0
            and type(leg.get("filled_ns")) is int and amount(leg.get("price")) > 0
            and leg["source"]["artifact"] == buy_source["artifact"]
            and leg["source"]["sha256"] == BUY_SHA and leg["source"]["kind"] == "RETAINED"
            and re.fullmatch(r"line:[1-9][0-9]*", leg["source"]["pointer"]) is not None,
            "aggregate_retained_original_entry_source_required")
    source_ref(entry.get("source"))
    at = entry.get("at", {})
    require(isinstance(at, dict) and at.get("event_ns") == leg["filled_ns"]
            and type(at.get("sequence")) is int and at["sequence"] == 0
            and entry.get("price") == leg["price"] and entry.get("quantity") == leg["quantity"]
            and entry.get("fill_id") == f"ACTUAL_ORDER_AGGREGATE:{leg['account']}:{order}"
            and at.get("raw_id") == entry["fill_id"] and at.get("source") == entry["source"]
            and entry["source"]["artifact"] == leg["source"]["artifact"]
            and entry["source"]["sha256"] == BUY_SHA and entry["source"]["kind"] == "DERIVED"
            and entry["source"]["pointer"] == leg["source"]["pointer"] + ";" + ASSUMPTION,
            "aggregate_explicit_collapse_fact_required")
    canonical_position_fields(position)
    fills = position.get("fills")
    require(isinstance(fills, list) and (fills == [entry] or fills == [])
            and type(position.get("filled_qty")) is int
            and position["filled_qty"] == (leg["quantity"] if fills else 0)
            and position.get("first_fill_ts") == (leg["filled_ns"] if fills else None)
            and position.get("vwap") == (leg["price"] if fills else None),
            "aggregate_canonical_position_must_match_raw_entry")
    issues, exits, assumptions = raw.get("issues"), raw.get("exits"), raw.get("assumptions")
    require(isinstance(issues, list) and all(isinstance(v, str) and v for v in issues)
            and isinstance(exits, list) and len(exits) <= 1
            and isinstance(assumptions, list) and ASSUMPTION in assumptions and TIE_POLICY in assumptions,
            "aggregate_issues_exits_assumptions_required")
    for exit in exits:
        require(isinstance(exit, dict) and type(exit.get("quantity")) is int and exit["quantity"] == position["filled_qty"]
                and exit.get("reason") in ({"TARGET_5", "STOP_8", "SELL_FLIP", "SESSION_END", "OPEN"}
                    | ({"CONFIRMATION_EXIT"} if adapter_version == CONFIRMATION_ADAPTER_VERSION else set())),
                "aggregate_exact_modeled_exit_required")
        if exit.get("at") is not None:
            source_ref(exit["at"].get("source"))
            require(type(exit["at"].get("event_ns")) is int
                    and exit["at"]["event_ns"] > leg["filled_ns"], "aggregate_exit_strictly_after_entry_required")
    known = raw.get("decision") == "MODELED_BASELINE_CLOSED"
    require(raw.get("decision") in {"MODELED_BASELINE_CLOSED", "UNKNOWN"}, "aggregate_explicit_decision_required")
    if known:
        require(not issues and len(exits) == 1 and fills and exits[0].get("at") is not None
                and exits[0].get("reason") != "OPEN" and not exits[0].get("issues")
                and amount(exits[0].get("price")) > 0 and raw.get("modeled_gross_pnl") is not None,
                "aggregate_known_economics_need_closed_issue_free_position")
        require(amount(raw["modeled_gross_pnl"]) == leg["quantity"] * (amount(exits[0]["price"]) - amount(leg["price"])),
                "aggregate_recorded_money_accounting_mismatch")
    else:
        require(bool(issues) and raw.get("modeled_gross_pnl") is None,
                "aggregate_UNKNOWN_cannot_be_zero_or_known")
    if adapter_version == CONFIRMATION_ADAPTER_VERSION:
        require(raw.get("baseline_policy_version") == CONFIRMATION_POLICY_VERSION,
                "aggregate_confirmation_policy_required")
        close, confirmation = raw.get("entry_bar_close"), raw.get("confirmation_exit")
        require("entry_bar_close" in raw and "confirmation_exit" in raw,
                "aggregate_confirmation_evidence_fields_required")
        require(close is None or isinstance(close, dict), "aggregate_typed_entry_close_required")
        if close is None or close.get("line") is None or close.get("state") not in ("long", "short"):
            require(confirmation is None and "UNKNOWN_ENTRY_BAR_ORACLE_CLOSE" in issues,
                    "aggregate_missing_confirmation_evidence_must_remain_UNKNOWN")
        else:
            start = leg["filled_ns"] // 60000000000 * 60000000000
            require(type(close.get("bar_start_ns")) is int and close["bar_start_ns"] == start
                    and type(close.get("available_ns")) is int
                    and close["available_ns"] == start + 60000000000
                    and amount(close.get("close")) > 0 and amount(close.get("line")) > 0,
                    "aggregate_exact_entry_bar_close_required")
            source_ref(close.get("source"))
            should_confirm = close["state"] == "short" and amount(close["close"]) < amount(close["line"])
            expected = dict(available_ns=close["available_ns"], source=close["source"], bar_start_ns=start)
            require(confirmation == (expected if should_confirm else None),
                    "aggregate_confirmation_boundary_must_match_entry_close")
            if ((close["state"] == "long" and amount(close["close"]) < amount(close["line"]))
                    or (close["state"] == "short" and amount(close["close"]) > amount(close["line"]))):
                require("UNKNOWN_ENTRY_BAR_ORACLE_STATE_LINE_CONFLICT" in issues,
                        "aggregate_conflicting_entry_close_must_remain_UNKNOWN")
        for exit in exits:
            if exit["reason"] == "CONFIRMATION_EXIT":
                require(confirmation is not None and exit.get("at") is not None
                        and exit["at"]["event_ns"] >= confirmation["available_ns"],
                        "aggregate_confirmation_cannot_precede_entry_bar_close")
    if adapter_version in INDEPENDENT_VERSIONS:
        flags, peers = raw.get("flags"), raw.get("same_account_overlap_order_ids")
        require(raw.get("cohort_policy") == INDEPENDENT_COHORT_POLICY
                and isinstance(flags, list) and flags in ([], ["SAME_ACCOUNT_OVERLAP"])
                and isinstance(peers, list) and all(isinstance(order, str) and order for order in peers)
                and peers == sorted(set(peers)) and bool(peers) == bool(flags)
                and order not in peers and "UNKNOWN_OVERLAPPING_ACTUAL_ENTRY_POSITIONS" not in issues,
                "aggregate_v2_informational_overlap_contract_required")
    result = deepcopy(raw)
    result.update(source_adapter_version=raw["version"], version=VERSION, mode=MODE, model=MODEL,
        history_label=LABEL, underlying_capture_label=raw["history_label"], entry_assumption=ENTRY_ASSUMPTION,
        partial_fill_timing="UNKNOWN", native_execution_qualified=False,
        producer_head=producer.reviewed_head, source_pins=deepcopy(pins),
        engine_dependency_head=producer.reviewed_head, qualification="UNQUALIFIED_REPLAY",
        live_totals_included=False, gross_pnl=raw["modeled_gross_pnl"],
        recorded_entry_present=True, outcome_kind="TRADE",
        raw_baseline_result=deepcopy(raw), **deepcopy(canonical_position_fields(position)))
    result["outcome_id"] = digest((identity, MODE, MODEL))
    result["trade_id"] = result["outcome_id"]
    require_provenance(result, producer_head=producer.reviewed_head, source_pins=pins)
    return result


def independent_totals(rows):
    def total(values):
        values = tuple(values)
        known = [row for row in values if row["gross_pnl"] is not None]
        component = sum((amount(row["gross_pnl"]) for row in known), Decimal(0))
        return dict(legs=len(values), unknown_legs=len(values) - len(known),
                    gross_pnl=component if len(known) == len(values) else None,
                    known_component_gross_pnl=component)
    return dict(all_legs=total(rows),
        excluding_same_account_overlap=total(row for row in rows if not row["flags"]),
        same_account_overlap=total(row for row in rows if row["flags"]))


def verify_independent_metadata(receipt, rows, day_index):
    require(receipt.get("cohort_policy") == INDEPENDENT_COHORT_POLICY,
            "aggregate_v2_explicit_independent_leg_policy_required")
    require(all(isinstance(day.get("session_end"), dict)
                and type(day["session_end"].get("available_ns")) is int for day in day_index.values()),
            "aggregate_v2_explicit_session_end_required")
    expected_peers = {row["outcome_id"]: set() for row in rows}
    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
            fields = ("trading_day", "symbol", "account_id", "strategy")
            if any(left["identity"][field] != right["identity"][field] for field in fields):
                continue
            def interval(row):
                identity = row["identity"]
                end = next((exit["at"]["event_ns"] for exit in row["exits"] if exit.get("at") is not None),
                    day_index[identity["symbol"], identity["trading_day"]]["session_end"]["available_ns"])
                require(type(end) is int, "aggregate_v2_explicit_unresolved_horizon_required")
                return row["recorded_aggregate"]["filled_ns"], end
            a, b = interval(left), interval(right)
            if max(a[0], b[0]) <= min(a[1], b[1]):
                expected_peers[left["outcome_id"]].add(right["order_id"])
                expected_peers[right["outcome_id"]].add(left["order_id"])
    for row in rows:
        peers = expected_peers[row["outcome_id"]]
        require(row["same_account_overlap_order_ids"] == sorted(peers)
                and row["flags"] == (["SAME_ACCOUNT_OVERLAP"] if peers else []),
                "aggregate_v2_overlap_must_match_same_account_stored_intervals")
    totals = independent_totals(rows)
    recorded = receipt.get("independent_leg_totals")
    require(isinstance(recorded, dict) and set(recorded) == set(totals), "aggregate_v2_three_totals_required")
    for name, expected in totals.items():
        slot = recorded[name]
        require(isinstance(slot, dict) and set(slot) == set(expected), "aggregate_v2_total_fields_required")
        for field in ("legs", "unknown_legs"):
            require(type(slot[field]) is int and slot[field] == expected[field], "aggregate_v2_total_count_mismatch")
        for field in ("gross_pnl", "known_component_gross_pnl"):
            require((slot[field] is None if expected[field] is None else amount(slot[field]) == expected[field]),
                    "aggregate_v2_total_money_mismatch")
    return totals


def prepare_aggregate_publication(receipt_raw: bytes, *, receipt_source: dict,
                                  producer: AggregateProducerBinding) -> AggregatePublication:
    """No filesystem/DB/model calls; complete canonical publication metadata."""
    source_ref(receipt_source)
    require(type(receipt_raw) is bytes and 0 < len(receipt_raw) <= MAX_BYTES
            and receipt_source["kind"] == "RETAINED" and receipt_source["pointer"] == "$"
            and hashlib.sha256(receipt_raw).hexdigest() == receipt_source["sha256"]
            and receipt_source["sha256"] != HELD_F7_SHA, "aggregate_final_immutable_receipt_required")
    receipt = document(receipt_raw)
    pins, brokers = producer_pins(receipt, producer)
    adapter_version = receipt.get("version")
    require(adapter_version in (ADAPTER_VERSION, *INDEPENDENT_VERSIONS) and receipt.get("variant") == VARIANT
            and receipt.get("entry_model") == ENTRY_MODEL and receipt.get("label") == LABEL
            and receipt.get("history_label") == CAPTURE_LABEL and receipt.get("exit_policy_version") == EXIT_POLICY
            and receipt.get("assumption") == ASSUMPTION and receipt.get("same_clock_policy") == TIE_POLICY
            and receipt.get("fac9_input_sha256") == FAC9_SHA and receipt.get("original_model_reexecuted") is False,
            "aggregate_approved_adapter_receipt_contract_required")
    cases = receipt.get("cases")
    require(isinstance(cases, list) and len(cases) == 173 and receipt.get("case_count") == 173
            and type(receipt["case_count"]) is int and receipt.get("account_leg_count") == 253
            and type(receipt["account_leg_count"]) is int,
            "aggregate_all173_cases253_legs_required")
    keys = [case.get("reference_key") for case in cases]
    require(all(isinstance(k, list) and len(k) == 3 and all(isinstance(v, str) and v for v in k) for k in keys)
            and len({tuple(k) for k in keys}) == 173
            and hashlib.sha256(json.dumps(keys, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest() == KEYS_SHA,
            "aggregate_exact_seed_case_order_required")
    buy_source = source_ref(receipt.get("buy_source"))
    require(buy_source["sha256"] == BUY_SHA and buy_source["kind"] == "RETAINED", "aggregate_original_buy_source_required")
    days = receipt.get("day_evidence")
    expected_days = {tuple(key[:2]) for key in keys}
    require(isinstance(days, list) and len(days) == len(expected_days), "aggregate_all_selected_day_sources_required")
    day_index = {}
    for day in days:
        require(isinstance(day, dict), "aggregate_day_source_required")
        day_key = day.get("symbol"), day.get("day")
        require(day_key in expected_days and day_key not in day_index, "aggregate_exact_unique_day_source_required")
        source_ref(day.get("source"))
        require(isinstance(day.get("coverage"), dict) and isinstance(day.get("issues"), list)
                and isinstance(day.get("all_day_sell_flips"), list)
                and isinstance(day.get("all_day_flip_events"), list), "aggregate_raw_day_evidence_required")
        day_index[day_key] = day
    rows, index, seen, orders = [], [], set(), set()
    all_known, known_component, unknown = True, Decimal(0), 0
    for case, key in zip(cases, keys):
        require(case.get("label") == LABEL and case.get("history_label") == CAPTURE_LABEL
                and case.get("variant") == VARIANT and case.get("entry_model") == ENTRY_MODEL
                and "modeled_gross_pnl" in case
                and isinstance(case.get("account_rows"), list) and bool(case["account_rows"]),
                "aggregate_case_label_and_account_rows_required")
        case_rows = []
        for raw in case["account_rows"]:
            row = canonical_row(raw, key=key, producer=producer, pins=pins, brokers=brokers,
                                buy_source=buy_source, adapter_version=adapter_version)
            day = day_index[tuple(key[:2])]
            if adapter_version == CONFIRMATION_ADAPTER_VERSION:
                require(isinstance(day.get("all_bar_closes"), list)
                        and (row["entry_bar_close"] is None or row["entry_bar_close"] in day["all_bar_closes"]),
                        "aggregate_confirmation_close_must_match_bound_day")
                if row["entry_bar_close"] is not None:
                    ref = row["entry_bar_close"]["source"]
                    require(all(ref[field] == day["source"][field] for field in ("artifact", "sha256"))
                            and ref["kind"] == "DERIVED", "aggregate_confirmation_oracle_source_mismatch")
            require(all(issue in row["issues"] for issue in day["issues"]), "aggregate_day_UNKNOWN_cannot_disappear")
            for exit in row["exits"]:
                if exit.get("at") is not None:
                    ref = exit["at"]["source"]
                    require(all(ref[field] == day["source"][field] for field in ("artifact", "sha256"))
                            and re.fullmatch(r"line:[1-9][0-9]*", ref["pointer"]) is not None,
                            "aggregate_exit_must_reference_same_bound_day_tape")
            identity = tuple(row["identity"][field] for field in IDENTITY_FIELDS)
            order = row["identity"]["account_id"], row["order_id"]
            require(identity not in seen and order not in orders, "aggregate_duplicate_case_account_order_refused")
            seen.add(identity)
            orders.add(order)
            rows.append(row)
            case_rows.append(row)
            if row["gross_pnl"] is None:
                unknown += 1
                all_known = False
            else:
                known_component += amount(row["gross_pnl"])
        case_known = all(row["gross_pnl"] is not None for row in case_rows)
        expected_total = sum((amount(row["gross_pnl"]) for row in case_rows), Decimal(0)) if case_known else None
        require((case.get("modeled_gross_pnl") is None if expected_total is None
                 else amount(case.get("modeled_gross_pnl")) == expected_total)
                and case.get("decision") == ("MODELED_BASELINE_CLOSED" if case_known else "UNKNOWN"),
                "aggregate_case_total_unknown_mismatch")
        index.append(dict(reference_key=key, canonical_rows=len(case_rows),
            identities=[row["identity"] for row in case_rows], outcome_ids=[row["outcome_id"] for row in case_rows],
            decision=case["decision"], modeled_gross_pnl=case["modeled_gross_pnl"],
            unknown_account_legs=sum(row["gross_pnl"] is None for row in case_rows)))
    require(len(rows) == 253 and set(brokers) == {row["identity"]["account_id"] for row in rows},
            "aggregate_complete253_original_account_legs_required")
    require("total_modeled_gross_pnl" in receipt
            and type(receipt.get("unknown_account_legs")) is int and receipt["unknown_account_legs"] == unknown
            and amount(receipt.get("known_component_gross_pnl")) == known_component
            and (receipt.get("total_modeled_gross_pnl") is None if not all_known
                 else amount(receipt.get("total_modeled_gross_pnl")) == known_component),
            "aggregate_full_total_must_remain_NULL_if_any_UNKNOWN")
    exclusions = receipt.get("reference_exclusions")
    require(isinstance(exclusions, list) and len(exclusions) == 7
            and all(isinstance(item, dict) and isinstance(item.get("reference_key"), list)
                    and len(item["reference_key"]) == 3 for item in exclusions)
            and {tuple(item["reference_key"]) for item in exclusions} == EXCLUDED_KEYS,
            "aggregate_seven_reference_exclusions_required")
    independent = (verify_independent_metadata(receipt, rows, day_index)
                   if adapter_version in INDEPENDENT_VERSIONS else None)
    if adapter_version == CONFIRMATION_ADAPTER_VERSION:
        require(receipt.get("baseline_policy_version") == CONFIRMATION_POLICY_VERSION
                and isinstance(receipt.get("exit_reason_counts"), dict)
                and all(type(n) is int and n > 0 for n in receipt["exit_reason_counts"].values())
                and receipt.get("exit_reason_counts") == dict(Counter(exit["reason"] for row in rows for exit in row["exits"])),
                "aggregate_confirmation_root_policy_and_exit_counts_required")
    sha = receipt_source["sha256"]
    source_id, stream, consumer = "aggregate-entry:" + sha, "atrsim-aggregate-raw:" + sha, "atrsim-aggregate:" + sha
    coverage = dict(complete=None, native_asof="UNKNOWN", underlying_capture_completeness="UNKNOWN",
        population_label=LABEL, engine_dependency_head=producer.reviewed_head, engine_contract_version=VERSION,
        qualification="UNQUALIFIED_REPLAY", aggregate_entry_study=dict(version=PUBLICATION_VERSION,
            receipt_sha256=sha, producer_reviewed_head=producer.reviewed_head, source_pins=pins,
            selected_cases=index, reference_exclusions=exclusions, canonical_rows=len(rows),
            unknown_account_legs=unknown, total_modeled_gross_pnl=receipt["total_modeled_gross_pnl"],
            known_component_gross_pnl=str(known_component), global_pnl_qualified=False,
            unknown_issue_counts=dict(Counter(issue for row in rows if row["gross_pnl"] is None for issue in row["issues"])),
            raw_receipt_stream=stream, raw_receipt_bytes=len(receipt_raw), chunk_bytes=CHUNK_BYTES,
            raw_receipt_chunks=(len(receipt_raw) + CHUNK_BYTES - 1) // CHUNK_BYTES,
            consumer_id=consumer, actual_realized_pnl_included=False))
    if independent is not None:
        coverage["aggregate_entry_study"].update(source_adapter_version=adapter_version,
            cohort_policy=INDEPENDENT_COHORT_POLICY, independent_leg_totals=independent,
            same_account_overlap_informational=True)
    if adapter_version == CONFIRMATION_ADAPTER_VERSION:
        coverage["aggregate_entry_study"].update(baseline_policy_version=CONFIRMATION_POLICY_VERSION,
            exit_reason_counts=receipt["exit_reason_counts"])
    refs = [deepcopy(receipt_source), buy_source, source_ref(receipt.get("policy_source"))]
    for day in days:
        refs.append(source_ref(day.get("source")))
    payload = dict(version=PUBLICATION_VERSION, source_id=source_id, input_sha256=sha,
        policy_version=VERSION, source_refs=refs, coverage=coverage, result_rows=rows)
    raw = encode(payload)
    return AggregatePublication(receipt_raw, sha, encode(receipt_source), raw, hashlib.sha256(raw).hexdigest())


def aggregate_writer_arguments(prepared: AggregatePublication, *, producer: AggregateProducerBinding,
                               expected_revision: int) -> dict:
    """Return capture/publish kwargs ONLY; caller owns checkpoint/DB/ACK claims.

    Existing writer performs capture and publish in separate transactions.
    Unknown ACKs propagate in caller; this metadata never retries or deletes.
    """
    require(isinstance(prepared, AggregatePublication) and type(expected_revision) is int and expected_revision >= 0,
            "aggregate_prepared_bytes_and_explicit_revision_required")
    rebuilt = prepare_aggregate_publication(prepared.receipt_raw,
        receipt_source=document(prepared.receipt_source_raw), producer=producer)
    require(prepared == rebuilt, "aggregate_prepared_publication_changed_or_foreign")
    payload = document(prepared.publication_raw)
    study = payload["coverage"]["aggregate_entry_study"]
    stream, consumer = study["raw_receipt_stream"], study["consumer_id"]
    events = [dict(stream=stream, stream_id=str(offset // CHUNK_BYTES), fields=dict(
        version=PUBLICATION_VERSION, receipt_sha256=prepared.receipt_sha256, offset=offset,
        encoding="base64", bytes_base64=base64.b64encode(prepared.receipt_raw[offset:offset + CHUNK_BYTES]).decode("ascii")))
        for offset in range(0, len(prepared.receipt_raw), CHUNK_BYTES)]
    return dict(checkpoint_consumer_id=consumer,
        capture=dict(consumer_id=consumer, expected_revision=expected_revision, events=events,
            cursors={stream: prepared.receipt_sha256}, issues=["RECORDED_AGGREGATE_REPLAY_NOT_NATIVE_CAPTURE"]),
        publish={key: payload[key] for key in ("source_id", "input_sha256", "policy_version", "source_refs", "coverage", "result_rows")},
        four_table_metadata=dict(inputs=len(events), consumers=1, runs=1, rows=len(payload["result_rows"])),
        readback="UNRUN", database_saved=False, publication_transactions="CAPTURE_THEN_PUBLISH_NOT_ONE_ATOMIC_TRANSACTION")
