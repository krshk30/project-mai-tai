"""Finite real first-rest predicate adapter, not full BUY/native acceptance.

Main runs OLD and NEW in separate source-root processes. Concrete constructors
create real local objects only when Main supplies complete explicit operands
and independently accepted readback. No service, DB/provider, invented book or
missing-state default is supplied. Main binds source and restoration guards.
"""

from hashlib import sha256
from dataclasses import asdict, is_dataclass
from collections import deque
from copy import deepcopy
import inspect
import json
import re
import sys

from tools.l12_original122_embedded_identity import extract_actual_original122_identity


VERSION = "ORIGINAL122_REAL_FIRST_REST_BRANCH_V1"
OLD_HEAD = "2e79a1282eaed6ec8ae6b8ca4770ebdf08e0e8e9"
OLD_APP_TREE = "bdababf8f34b7091418ffd9df8881fa6bd6058b5"
OLD_EXECUTION_VERSION = "ORIGINAL122_OLD_APP_TOOLS_ONLY_EXECUTION_V2"
MODULE = "project_mai_tai.strategy_core.schwab_1m_v2"
METHOD = "_strict_first_rest_admitted"
C11229_INPUT_LIMITED = frozenset(("2026-10-15/BBCI/07:55", "2026-10-16/JBDI/09:18",
    "2026-10-16/WORX/08:17", "2026-10-19/EXYN/11:15", "2026-10-19/WORX/10:10"))


def compare_c11229_input_limited(old_case_bytes, new_case_bytes, *, ruling_sha256, case_guard):
    """Compare actual emitted case bytes, not normalized JSON or invented RAM.

    Main's guard binds each result to its independently reviewed OLD/NEW source
    process, original record and actual output artifacts. This proves only the
    five input-limited invariances, never the remaining117 or a predicate call.
    """
    if (type(old_case_bytes) is not dict or type(new_case_bytes) is not dict
            or set(old_case_bytes) != C11229_INPUT_LIMITED
            or set(new_case_bytes) != C11229_INPUT_LIMITED
            or not _hash(ruling_sha256) or ruling_sha256 == "0" * 64 or not callable(case_guard)):
        raise ValueError("C11229_exact_five_actual_result_pairs_and_ruling_required")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("C11229_duplicate_result_field")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("C11229_nonfinite_result")

    results = []
    for case_id in sorted(C11229_INPUT_LIMITED):
        old, new = old_case_bytes[case_id], new_case_bytes[case_id]
        if (type(old) is not bytes or type(new) is not bytes
                or not 0 < len(old) <= 65536 or not 0 < len(new) <= 65536):
            raise ValueError("C11229_bounded_actual_case_bytes_required")
        if old != new:
            raise ValueError("C11229_input_limited_NEW_differs_from_OLD_STOP")
        row = json.loads(old.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite)
        required = {"case_id", "original_record_sha256", "decision", "permission_granted",
            "callback", "branch_value", "native_asof", "same_entry_opportunity", "missing_operands"}
        if (type(row) is not dict or not required <= set(row)
                or set(row) - required - {"reference_input_sha256"}
                or row["case_id"] != case_id or not _hash(row["original_record_sha256"])
                or row["original_record_sha256"] == "0" * 64
                or row["decision"] != "UNKNOWN" or row["permission_granted"] is not False
                or row["branch_value"] is not None or type(row["callback"]) is not str or row["callback"] not in {
                    "UNRUN_MISSING_RESTORED_OPERANDS",
                    "UNRUN_INDEPENDENT_SOURCE_OR_PRIVATE_RESTORATION_UNBOUND"}
                or row["native_asof"] != "UNKNOWN_NOT_STORED"
                or row["same_entry_opportunity"] != "UNKNOWN"
                or type(row["missing_operands"]) is not list or not row["missing_operands"]
                or any(type(value) is not str or not value for value in row["missing_operands"])
                or "reference_input_sha256" in row and (not _hash(row["reference_input_sha256"])
                    or row["reference_input_sha256"] == "0" * 64)):
            raise ValueError("C11229_five_remain_UNRUN_UNKNOWN_not_fabricated_outcomes")
        if case_guard(case_id, old, new) is not True:
            raise ValueError("C11229_independent_original_and_two_world_artifact_binding_required")
        results.append(dict(case_id=case_id, disposition="UNKNOWN_INPUT_LIMITED",
            old_result_sha256=sha256(old).hexdigest(), new_result_sha256=sha256(new).hexdigest(),
            byte_identical=True, predicate_executed=False, permission_granted=False))
    return dict(version="C11229_FIVE_INPUT_LIMITED_BYTE_INVARIANCE_V1", cases=results,
        input_limited_case_count=5, retained_original_denominator=122,
        remaining_original_cases=117, remaining_117_caused="NOT_ESTABLISHED_BY_THIS_CHECK",
        ruling_sha256=ruling_sha256, permission_granted=False,
        scope="EXACT_FIVE_INVARIANCE_NOT_122_REPLAY_OR_CASE_PASS")


STATE_READBACK_FIELDS = (
    "symbol", "bars", "atr_state", "atr_short_flip_bar_ts",
    "slotclear_fresh_buy_bar_ms", "slotclear_fresh_buy_segment_id",
    "cw_seed_cap_watch_start_ms", "cw_resting_taken", "cw_reclaim_taken",
    "flip_owner_phase", "flip_owner_first_rest_placed", "flip_owner_fill_accounts",
    "flip_owner_position_ids", "flip_owner_open_positions", "position_qty",
    "position_qty_held", "resting_active", "webull_resting_active", "cw_v2_emit_claimed",
    "fanout_webull_claimed", "fanout_claim_outcome", "fanout_segment_id",
    "flip_owner_opportunity_id", "retry_one_budget_readable", "retry_one_segment_id",
    "retry_one_closes_in_segment", "flip_owner_evidence_readable", "flip_owner_evidence_at_ms",
)
STRATEGY_READBACK_FIELDS = (
    "_flip_owned_first_entry_enabled", "_retry_one_enabled", "_retry_one_max_retries",
    "_flip_owner_restore_readable", "_removed_wait_enabled", "_removed_wait_restore_readable",
    "_removed_wait_requests", "_removed_scanner_symbols", "_falseflip_readable",
    "_falseflip_budgets", "_falseflip_pending", "_falseflip_cross_high",
)


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def validate_original122_world(world):
    fields = {"label", "head", "source_review_sha256"}
    carried = isinstance(world, dict) and world.get("version") == OLD_EXECUTION_VERSION
    if carried:
        fields |= {"version", "execution_head", "app_tree"}
    if (not isinstance(world, dict) or set(world) != fields
            or world["label"] not in {"OLD", "NEW"}
            or not isinstance(world["head"], str) or re.fullmatch(r"[0-9a-f]{40}", world["head"]) is None
            or not _hash(world["source_review_sha256"])
            or (world["label"] == "OLD" and world["head"] != OLD_HEAD)
            or (world["label"] == "NEW" and world["head"] == OLD_HEAD)):
        raise ValueError("branch_exact_independently_reviewed_old_or_new_head_required")
    if carried and (world["label"] != "OLD" or world["app_tree"] != OLD_APP_TREE
            or not isinstance(world["execution_head"], str)
            or re.fullmatch(r"[0-9a-f]{40}", world["execution_head"]) is None
            or world["execution_head"] == "0" * 40 or world["source_review_sha256"] == "0" * 64):
        raise ValueError("branch_literal_OLD_APP_separate_actual_execution_and_review_required")
    return dict(world)


def _references(case, refs):
    if not isinstance(refs, list) or not 0 < len(refs) <= 256:
        raise ValueError("branch_explicit_bounded_original_member_references_required")
    known = {member["source_pointer"]: member for values in case["members"].values() for member in values}
    seen, result = set(), []
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {"pointer", "decoded_text_sha256"}
                or not isinstance(ref["pointer"], str) or ref["pointer"] not in known
                or ref["pointer"] in seen
                or ref["decoded_text_sha256"] != known[ref["pointer"]]["decoded_text_sha256"]):
            raise ValueError("branch_exact_original_case_member_hash_pointer_required")
        seen.add(ref["pointer"])
        result.append(known[ref["pointer"]])
    return result


def _real_method(strategy, state):
    cls, state_cls = type(strategy), type(state)
    method = getattr(strategy, METHOD, None)
    original = getattr(cls, METHOD, None)
    loaded = sys.modules.get(MODULE)
    if (cls.__module__ != MODULE or cls.__name__ != "SchwabV2Strategy"
            or state_cls.__module__ != MODULE or state_cls.__name__ != "SymbolState"
            or not inspect.ismethod(method) or method.__func__ is not original
            or getattr(original, "__module__", None) != MODULE
            or loaded is None or getattr(loaded, "SchwabV2Strategy", None) is not cls
            or getattr(loaded, "SymbolState", None) is not state_cls
            or not inspect.isfunction(original)
            or original.__code__.co_name != METHOD):
        raise ValueError("branch_real_unwrapped_application_method_and_state_required")
    return method


def _call_real_first_rest(strategy, state):
    """No shadow predicate: invoke the actual selected source-root method once."""
    result = _real_method(strategy, state)(state, slot="first")
    if type(result) is not bool:
        raise ValueError("branch_application_predicate_did_not_return_bool")
    return result


def _readback_value(value):
    """Explicit stable dataclass/set/map representation, not repr or pickle."""
    if is_dataclass(value) and not isinstance(value, type):
        return _readback_value(asdict(value))
    if isinstance(value, dict):
        if all(type(key) is str for key in value):
            return {key: _readback_value(item) for key, item in value.items()}
        pairs = [[_readback_value(key), _readback_value(item)] for key, item in value.items()]
        return sorted(pairs, key=lambda pair: json.dumps(pair[0], sort_keys=True))
    if isinstance(value, (set, frozenset)):
        return sorted((_readback_value(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, (tuple, list)) or type(value).__name__ == "deque":
        return [_readback_value(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        # Reject NaN/Infinity rather than persisting a permissive JSON value.
        json.dumps(value, allow_nan=False)
        return value
    raise ValueError("unsupported_real_object_readback_type_no_repr_fallback")


def first_rest_object_readback(strategy, state):
    _real_method(strategy, state)
    if any(not hasattr(state, field) for field in STATE_READBACK_FIELDS):
        raise ValueError("real_state_required_field_missing")
    if any(not hasattr(strategy, field) for field in STRATEGY_READBACK_FIELDS):
        raise ValueError("real_strategy_required_field_missing_not_defaulted")
    book = getattr(strategy, "_falseflip_book", None)
    memory = getattr(strategy, "_brokerdecides1_memory", None)
    if memory is not None:
        module = sys.modules.get("project_mai_tai.brokerdecides1")
        if module is None or type(memory) is not getattr(module, "AdmissionMemory", None):
            raise ValueError("actual_source_root_admission_memory_required")
    return dict(state={field: _readback_value(getattr(state, field)) for field in STATE_READBACK_FIELDS},
        strategy={field: _readback_value(getattr(strategy, field)) for field in STRATEGY_READBACK_FIELDS},
        falseflip_entry_classifications=None if book is None else _readback_value(book.entry_classifications),
        cap_binding=_readback_value(getattr(state, "_brokerdecides1_cap_binding", None)),
        admission_memory=None if memory is None else dict(snapshot=_readback_value(memory.snapshot),
            reservations=_readback_value(memory._reservations)),
        callback_clock_ms=strategy._now_ms(),
        brokerdecides_enabled=getattr(strategy.settings, "strategy_schwab_1m_v2_brokerdecides1_enabled", False),
        falseflip_enabled=getattr(strategy.settings, "strategy_schwab_1m_v2_false_flip_enabled", False))


def construct_serialized_first_rest_case(case, packet, world, *, settings):
    """Concrete no-I/O constructor for this process's real source-root objects.

    Only already accepted explicit readback operands enter here. Unsupported
    book/state operands must remain missing, never be substituted with defaults.
    No store restore method, DB factory, provider or entry callback is called.
    """
    from dataclasses import fields
    from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy, SymbolState, OHLCVBar
    from project_mai_tai.settings import Settings
    from project_mai_tai.v2_flip_entry_ownership import FlipPositionLeg
    from project_mai_tai.v2_removed_wait import RemovedWait
    from project_mai_tai.falseflip1_runtime import FalseBudget

    world = validate_original122_world(world)
    if type(settings) is not Settings:
        raise ValueError("actual_this_root_Settings_required")
    expected = deepcopy(packet["expected_object_readback"])
    exact = {"state", "strategy", "falseflip_entry_classifications", "cap_binding",
             "admission_memory", "callback_clock_ms", "brokerdecides_enabled", "falseflip_enabled"}
    if (type(expected) is not dict or set(expected) != exact
            or type(expected["state"]) is not dict or set(expected["state"]) != set(STATE_READBACK_FIELDS)
            or type(expected["strategy"]) is not dict or set(expected["strategy"]) != set(STRATEGY_READBACK_FIELDS)
            or type(expected["callback_clock_ms"]) is not int or expected["callback_clock_ms"] <= 0
            or expected["brokerdecides_enabled"] is not (world["label"] == "NEW")):
        raise ValueError("complete_explicit_per_world_reconstruction_values_required")
    if expected["falseflip_entry_classifications"] is not None:
        raise ValueError("full_historical_FlipPositionBook_not_supplied_keep_case_UNKNOWN")
    strategy = SchwabV2Strategy(settings)
    if (getattr(settings, "strategy_schwab_1m_v2_brokerdecides1_enabled", False) is not expected["brokerdecides_enabled"]
            or getattr(settings, "strategy_schwab_1m_v2_false_flip_enabled", False) is not expected["falseflip_enabled"]):
        raise ValueError("actual_source_Settings_not_reviewed_world_profile")
    raw = expected["state"]
    if raw["symbol"] != case["case_id"].split("/")[1]:
        raise ValueError("constructed_symbol_not_exact_original_case")
    state = SymbolState(symbol=raw["symbol"])
    for name in STATE_READBACK_FIELDS:
        value = raw[name]
        if name == "bars":
            if type(value) is not list or len(value) > 300:
                raise ValueError("explicit_actual_300_bar_state_bound_no_silent_truncation")
            bar_fields = {field.name for field in fields(OHLCVBar)}
            if any(type(row) is not dict or set(row) != bar_fields for row in value):
                raise ValueError("exact_real_OHLCVBar_fields_required")
            stamps = [row["timestamp_ms"] for row in value]
            if (any(type(stamp) is not int or stamp % 60000 or stamp + 60000 > expected["callback_clock_ms"]
                    for stamp in stamps) or stamps != sorted(set(stamps))):
                raise ValueError("only_causal_unique_closed_bars_from_materialization_required")
            value = deque((OHLCVBar(**row) for row in value), maxlen=300)
        elif name == "flip_owner_fill_accounts":
            if type(value) is not list or len(set(value)) != len(value):
                raise ValueError("explicit_fill_accounts_required")
            value = set(value)
        elif name == "flip_owner_open_positions":
            if type(value) is not dict:
                raise ValueError("explicit_position_leg_map_required")
            leg_fields = {field.name for field in fields(FlipPositionLeg)}
            if any(type(row) is not dict or set(row) != leg_fields for row in value.values()):
                raise ValueError("complete_real_position_leg_fields_required")
            value = {key: FlipPositionLeg(**row) for key, row in value.items()}
        else:
            before = getattr(state, name)
            if type(before) in (bool, int, str) and type(value) is not type(before):
                raise ValueError("explicit_state_scalar_type_required")
            if type(before) is dict and type(value) is not dict:
                raise ValueError("explicit_state_mapping_required")
        setattr(state, name, value)
    policy = expected["strategy"]
    for name in ("_flip_owned_first_entry_enabled", "_retry_one_enabled", "_retry_one_max_retries", "_removed_wait_enabled"):
        if getattr(strategy, name) != policy[name] or type(getattr(strategy, name)) is not type(policy[name]):
            raise ValueError("constructor_policy_must_match_actual_Settings_not_attribute_override")

    def budget(payload):
        if type(payload) is not dict or set(payload) != {field.name for field in fields(FalseBudget)}:
            raise ValueError("complete_falseflip_record_required_no_optional_defaults")
        return FalseBudget.read(dict(payload, schema_version=1))

    budgets = {(key[0], key[1]): budget(value) for key, value in policy["_falseflip_budgets"]}
    strategy.configure_falseflip(budgets, readable=policy["_falseflip_readable"])
    strategy._falseflip_pending = {tuple(key): (budget(values[0]), budget(values[1]))
        for key, values in policy["_falseflip_pending"]}
    strategy._falseflip_cross_high = {tuple(key) for key in policy["_falseflip_cross_high"]}
    requests = {}
    for key, value in policy["_removed_wait_requests"].items():
        if type(value) is not dict or set(value) != {field.name for field in fields(RemovedWait)}:
            raise ValueError("complete_real_removed_wait_record_required")
        requests[key] = RemovedWait(**dict(value, account_names=tuple(value["account_names"])))
    strategy._removed_wait_requests = requests
    strategy._removed_scanner_symbols = set(policy["_removed_scanner_symbols"])
    for name in ("_flip_owner_restore_readable", "_removed_wait_restore_readable"):
        if type(policy[name]) is not bool:
            raise ValueError("explicit_readability_bool_required")
        setattr(strategy, name, policy[name])
    if expected["cap_binding"] is not None:
        binding = expected["cap_binding"]
        if type(binding) is not list or len(binding) != 2 or any(type(part) is not list or len(part) != 2 for part in binding):
            raise ValueError("exact_cap_binding_pairs_required")
        state._brokerdecides1_cap_binding = tuple(tuple(part) for part in binding)
    if expected["admission_memory"] is not None:
        # OLD without L12 source must never import the NEW root to satisfy this.
        if world["label"] != "NEW":
            raise ValueError("OLD_cannot_receive_NEW_admission_memory")
        from project_mai_tai.brokerdecides1 import AdmissionMemory, AdmissionSnapshot, NativeAccountBook
        supplied = expected["admission_memory"]
        if type(supplied) is not dict or set(supplied) != {"snapshot", "reservations"}:
            raise ValueError("complete_explicit_admission_memory_required")
        memory = AdmissionMemory()
        snapshot = supplied["snapshot"]
        if snapshot is not None:
            if set(snapshot) != {field.name for field in fields(AdmissionSnapshot)}:
                raise ValueError("complete_admission_snapshot_no_defaults_required")
            books = []
            for item in snapshot["books"]:
                if set(item) != {field.name for field in fields(NativeAccountBook)}:
                    raise ValueError("complete_native_book_record_required")
                books.append(NativeAccountBook(**dict(item, working=frozenset(item["working"]),
                    positions=frozenset(item["positions"]))))
            memory.snapshot = AdmissionSnapshot(**dict(snapshot, books=tuple(books),
                inflight=frozenset(snapshot["inflight"]),
                filled_segments=frozenset(tuple(value) for value in snapshot["filled_segments"]),
                terminal_reservations=frozenset(tuple(value) for value in snapshot["terminal_reservations"])))
        for symbol, token in supplied["reservations"].items():
            memory.reserve(symbol, token)  # Raises on duplicate/overflow; returnsNone.
        strategy._brokerdecides1_memory = memory
    strategy._symbol_states[state.symbol] = state
    strategy._now_ms = lambda: expected["callback_clock_ms"]
    return strategy, state


def _construct_one_original122_case(case, packet, world, *, construct_real_case, source_guard, operand_guard):
    """One case of the original constructor contract, shared by both consumers."""
    raw = json.dumps(packet, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    packet = json.loads(raw)
    if set(packet) != {"references", "restoration", "expected_object_readback", "operands"}:
        raise ValueError("exact_serialized_case_operands_and_readback_required")
    members = _references(case, packet["references"])
    restoration = packet["restoration"]
    if (type(restoration) is not dict or set(restoration) != {
            "origin", "reference_input_sha256", "review_sha256", "readback_sha256", "missing_operands"}
            or restoration["origin"] != "EXPLICIT_REVIEWED_RECONSTRUCTED_LOCAL_BRANCH"
            or type(restoration.get("missing_operands")) is not list):
        raise ValueError("explicit_missing_operand_list_required")
    if restoration["missing_operands"]:
        return None, dict(case_id=case["case_id"], construction="UNRUN_MISSING_LOCAL_OPERANDS",
            missing_operands=restoration["missing_operands"])
    reference_input = dict(case_id=case["case_id"], record_sha256=case["original_record_sha256"],
        members=[dict(pointer=item["source_pointer"], sha256=item["decoded_text_sha256"]) for item in members])
    reference_sha = sha256(json.dumps(reference_input, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if (restoration["reference_input_sha256"] != reference_sha
            or any(not _hash(restoration[key]) or restoration[key] == "0" * 64
                   for key in ("review_sha256", "readback_sha256"))):
        raise ValueError("exact_reference_input_and_independent_restoration_readback_acceptance_required")
    operands = packet["operands"]
    if (type(operands) is not dict or set(operands) != {
            "case_id", "materialization_sha256", "case_readback_sha256", "restored_row_refs", "unbound_fields"}
            or operands["case_id"] != case["case_id"]
            or any(not _hash(operands[key]) or operands[key] == "0" * 64
                   for key in ("materialization_sha256", "case_readback_sha256"))
            or type(operands["restored_row_refs"]) is not list
            or not 0 < len(operands["restored_row_refs"]) <= 4096
            or type(operands["unbound_fields"]) is not list or operands["unbound_fields"]):
        raise ValueError("explicit_materialized_case_row_refs_and_no_unbound_fields_required")
    if operand_guard(case, members, packet, dict(world)) is not True:
        return None, dict(case_id=case["case_id"], construction="UNRUN_RECONSTRUCTION_NOT_ACCEPTED")
    strategy, state = construct_real_case(case, packet, dict(world))
    method = _real_method(strategy, state)
    if (state.symbol != case["case_id"].split("/")[1]
            or getattr(strategy, "_symbol_states", {}).get(state.symbol) is not state
            or source_guard(dict(world), method) is not True):
        raise ValueError("real_world_source_or_state_membership_not_bound")
    actual = first_rest_object_readback(strategy, state)
    if actual != packet["expected_object_readback"]:
        raise ValueError("constructed_real_state_or_strategy_readback_differs")
    actual_raw = json.dumps(actual, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if restoration["readback_sha256"] != sha256(actual_raw).hexdigest():
        raise ValueError("actual_object_readback_hash_not_accepted")
    return dict(strategy=strategy, state=state, references=packet["references"], restoration=restoration), dict(
        case_id=case["case_id"], construction="REAL_OBJECTS_READ_BACK_NOT_PREDICATE_EXECUTION",
        serialized_case_sha256=sha256(raw).hexdigest(), object_readback_sha256=sha256(actual_raw).hexdigest())


def construct_original122_bound_cases(original_raw, *, world, serialized_cases,
        construct_real_case, source_guard, operand_guard):
    """Original whole-report API; streaming consumer shares the same case kernel."""
    world = validate_original122_world(world)
    extracted = extract_actual_original122_identity(original_raw)
    known = {case["case_id"]: case for case in extracted["cases"]}
    if (not isinstance(serialized_cases, dict) or set(serialized_cases) - set(known)
            or not all(callable(fn) for fn in (construct_real_case, source_guard, operand_guard))):
        raise ValueError("finite_original_case_serialization_and_real_world_callbacks_required")
    bound, rows = {}, []
    for case in extracted["cases"]:
        packet = serialized_cases.get(case["case_id"])
        if packet is None:
            rows.append(dict(case_id=case["case_id"], construction="UNRUN_NO_SERIALIZED_OPERANDS"))
            continue
        supplied, row = _construct_one_original122_case(case, packet, world,
            construct_real_case=construct_real_case, source_guard=source_guard, operand_guard=operand_guard)
        if supplied is not None:
            bound[case["case_id"]] = supplied
        rows.append(row)
    return dict(bound_cases=bound, construction_rows=rows, original_cases=122,
        omitted_cases=0, permission_granted=False, callback_execution="UNRUN")


def summarize_original122_local_callbacks(rows):
    """Local predicate coverage is distinct from UNKNOWN native permission."""
    executed = "REAL_FIRST_REST_PREDICATE_EXECUTED_LOCAL_BRANCH_ONLY"
    unknown = [row["case_id"] for row in rows if row["callback"] != executed]
    return dict(reconstructed_local_predicate_cases=len(rows) - len(unknown),
        local_state_UNKNOWN_count=len(unknown), local_state_UNKNOWN_case_ids=unknown,
        native_entry_permission="UNKNOWN", native_asof_UNKNOWN_count=len(rows),
        C1_case_review_required=len(unknown) * 10 > len(rows),
        threshold="STRICTLY_MORE_THAN_10_PERCENT_OF_ALL_ORIGINAL_CASES",
        local_predicate_result_is_not_full_BUY_case_PASS=True)


def emit_original122_five_case_bytes(report, *, report_guard):
    """Emit actual producer rows; never repair already-different OLD/NEW bytes."""
    if (type(report) is not dict or report.get("original_cases") != 122
            or report.get("omitted_original_cases") != 0
            or type(report.get("rows")) is not list or len(report["rows"]) != 122
            or len({row["case_id"] for row in report["rows"]}) != 122
            or not callable(report_guard) or report_guard(report) is not True):
        raise ValueError("actual_complete_two_world_report_acceptance_required_before_emission")
    selected = {row["case_id"]: json.dumps(row, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8") for row in report["rows"] if row["case_id"] in C11229_INPUT_LIMITED}
    if set(selected) != C11229_INPUT_LIMITED or any(not 0 < len(raw) <= 65536 for raw in selected.values()):
        raise ValueError("all_five_actual_bounded_case_rows_required")
    return selected


def _world(extracted, *, world, bound_cases, source_guard, restoration_guard):
    """UNIT seam. Guard callbacks are Main's independent evidence boundary.

    A controlled lambda True is NOT a source/restoration proof. Missing operands
    yield UNKNOWN without executing the app. Per-case objects must have been
    restored OFFLINE using the reviewed same-input policy, never live objects.
    """
    world = validate_original122_world(world)
    known = {case["case_id"]: case for case in extracted["cases"]}
    if not isinstance(bound_cases, dict) or not set(bound_cases) <= set(known):
        raise ValueError("branch_no_extra_or_extension_case_ids")
    rows = []
    for case in extracted["cases"]:
        row = dict(case_id=case["case_id"], original_record_sha256=case["original_record_sha256"],
            decision="UNKNOWN", permission_granted=False, callback="UNRUN_MISSING_RESTORED_OPERANDS",
            branch_value=None, native_asof="UNKNOWN_NOT_STORED", same_entry_opportunity="UNKNOWN")
        supplied = bound_cases.get(case["case_id"])
        if supplied is None:
            row["missing_operands"] = ["EXACT_PER_CASE_RESTORED_STATE_REFERENCES_AND_READBACK_NOT_BOUND"]
            rows.append(row)
            continue
        if (not isinstance(supplied, dict) or set(supplied) != {"strategy", "state", "references", "restoration"}):
            raise ValueError("branch_exact_restoration_objects_and_references_required")
        members = _references(case, supplied["references"])
        reference_input = dict(case_id=case["case_id"], record_sha256=case["original_record_sha256"],
            members=[dict(pointer=item["source_pointer"], sha256=item["decoded_text_sha256"]) for item in members])
        reference_sha = sha256(json.dumps(reference_input, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        row["reference_input_sha256"] = reference_sha
        restoration = supplied["restoration"]
        if (not isinstance(restoration, dict) or set(restoration) != {
                "origin", "reference_input_sha256", "review_sha256", "readback_sha256", "missing_operands"}
                or restoration["origin"] != "EXPLICIT_REVIEWED_RECONSTRUCTED_LOCAL_BRANCH"
                or restoration["reference_input_sha256"] != reference_sha
                or not _hash(restoration["review_sha256"]) or not _hash(restoration["readback_sha256"])
                or not isinstance(restoration["missing_operands"], list)):
            raise ValueError("branch_explicit_same_input_restoration_receipt_required")
        if restoration["missing_operands"]:
            row["missing_operands"] = list(restoration["missing_operands"])
            rows.append(row)
            continue
        strategy, state = supplied["strategy"], supplied["state"]
        if (getattr(state, "symbol", None) != case["case_id"].split("/")[1]
                or getattr(strategy, "_symbol_states", {}).get(state.symbol) is not state):
            raise ValueError("branch_exact_restored_case_state_membership_required")
        method = _real_method(strategy, state)
        if (not callable(source_guard) or source_guard(dict(world), method) is not True
                or not callable(restoration_guard)
                or restoration_guard(case, members, supplied) is not True):
            row["callback"] = "UNRUN_INDEPENDENT_SOURCE_OR_PRIVATE_RESTORATION_UNBOUND"
            row["missing_operands"] = ["INDEPENDENT_SOURCE_OR_PRIVATE_RESTORATION_NOT_ACCEPTED"]
            rows.append(row)
            continue
        enabled = getattr(getattr(strategy, "settings", None),
                          "strategy_schwab_1m_v2_brokerdecides1_enabled", False)
        if enabled is not (world["label"] == "NEW"):
            raise ValueError("branch_old_real_head_vs_new_on_not_new_feature_off")
        try:
            row["branch_value"] = _call_real_first_rest(strategy, state)
        except Exception as exc:
            row["callback"] = "APPLICATION_CALLBACK_ERROR_NOT_ACCEPTANCE"
            row["error_type"] = type(exc).__name__
        else:
            row["callback"] = "REAL_FIRST_REST_PREDICATE_EXECUTED_LOCAL_BRANCH_ONLY"
        # Even True is a single predicate result, NOT a BUY or native permission.
        rows.append(row)
    return dict(version=VERSION, world=dict(world), original_cases=len(rows), omitted_original_cases=0,
        local_reconstruction=summarize_original122_local_callbacks(rows),
        rows=rows, permission_granted=False, actual_close_boot="UNRUN",
        scope="RECONSTRUCTED_LOCAL_FIRST_REST_BRANCH_NOT_FULL_ENTRY_CALLBACK_OR_NATIVE_ACCEPTANCE")


def replay_actual_original122_first_rest(original_raw, *, world, bound_cases,
                                         source_guard, restoration_guard):
    extracted = extract_actual_original122_identity(original_raw)
    return _world(extracted, world=world, bound_cases=bound_cases,
                  source_guard=source_guard, restoration_guard=restoration_guard)
