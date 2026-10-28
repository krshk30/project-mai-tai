"""One-case concrete source-fact binding consumer for COMPLETE bb shards.

Creates partial independent expectation fields, never default historical RAM.
No engine, DB read, unfiltered loader, actor callback, provider, or native claim.
"""

from copy import deepcopy
from hashlib import sha256

from tools import l12_original122_retained_materialization as material
from tools.l12_original122_typed_decoders import decode_materialized_case, original_log_prefix
from tools.l12_original122_branch_adapter import (
    STATE_READBACK_FIELDS, STRATEGY_READBACK_FIELDS, _readback_value, validate_original122_world,
)
from tools.l12_original122_causal_prefix import produce_causal_prefix


PROBE_BOOLS = {"cw_resting_taken": "cw_resting_taken", "cw_reclaim_taken": "cw_reclaim_taken",
    "resting_active": "resting_active", "webull_resting_active": "webull_resting_active",
    "emit_claimed": "cw_v2_emit_claimed", "retry_one_budget_readable": "retry_one_budget_readable",
    "flip_owner_evidence_readable": "flip_owner_evidence_readable"}
PROBE_INTS = ("atr_short_flip_bar_ts", "fanout_segment_id", "flip_owner_evidence_at_ms",
    "retry_one_segment_id", "retry_one_closes_in_segment", "slotclear_fresh_buy_bar_ms", "slotclear_fresh_buy_segment_id")


def eligible_causal_bars(record):
    case = record["case"]
    selected, excluded = [], []
    for item in record["rows"]["strategy_bar_history"]:
        row = item["row"]
        chronology = material.classify_retained_row("strategy_bar_history", row, case)
        if chronology != item["chronology"]:
            raise ValueError("materialized_bar_chronology_changed")
        if chronology != "RETAINED_PAYLOAD_AT_OR_BEFORE_CUTOFF_NOT_RAM_PROOF":
            excluded.append(dict(reference=item["reference"], reason=chronology))
            continue
        stamp = int(material._stamp(row["bar_time"]).timestamp() * 1000)
        if (row["symbol"] != case["symbol"] or row["strategy_code"] != "schwab_1m_v2"
                or row["interval_secs"] != 60 or stamp % 60000
                or stamp < int(material._stamp(case["session_anchor"]).timestamp() * 1000)):
            raise ValueError("exact_original_symbol_strategy_session_minute_required")
        if item["reference"]["primary_key"] != [row["id"]]:
            raise ValueError("exact_bar_PK_reference_required")
        selected.append(dict(bar=dict(symbol=row["symbol"], timestamp_ms=stamp,
            open=row["open_price"], high=row["high_price"], low=row["low_price"], close=row["close_price"],
            volume=row["volume"]), reference=item["reference"]))
    # Source query is PK ordered. Mathematical order is explicitly chronological,
    # not a repaired log clock, and duplicate logical bars are never discarded.
    selected.sort(key=lambda item: item["bar"]["timestamp_ms"])
    stamps = [item["bar"]["timestamp_ms"] for item in selected]
    if len(selected) > 4096 or len(stamps) != len(set(stamps)):
        raise ValueError("bounded_unique_causal_bar_prefix_required")
    return selected, excluded


def bind_materialized_case_fields(record, extracted_case, *, world, prefix_review,
        period, factor, record_guard, source_guard, field_guard, policy_review=None):
    """Run real typed decoders/math and retain independently traceable fragments.

    field_guard must bind original identity, exact callback/continuity and scope.
    No positive callback is a substitute for actual evidence; Main supplies and
    separately reviews those acceptance operands. Missing evidence stays missing.
    """
    world = validate_original122_world(world)
    if record_guard(record) is not True:
        raise ValueError("accepted_complete_manifest_case_readback_required")
    case = record["case"]
    if extracted_case["case_id"] != case["case_id"]:
        raise ValueError("same_original_case_extraction_required")
    decoded = decode_materialized_case(record)
    fields, candidates, gaps = {}, {}, {}

    def propose(target, value, provenance):
        value = _readback_value(value)
        entry = dict(value=value, provenance=_readback_value(provenance))
        candidates.setdefault(target, []).append(entry)
        if field_guard(case, target, entry) is not True:
            return
        if target in fields and fields[target]["value"] != value:
            fields.pop(target)
            gaps[target] = "CONFLICTING_POSITIVE_FACTS_REQUIRE_CAUSAL_RECONCILIATION"
            return
        if target not in gaps:
            fields[target] = entry

    propose("state.symbol", case["symbol"], dict(kind="EXACT_ORIGINAL_CASE_SYMBOL", case_id=case["case_id"],
        pointer=case["original_pointer"], source_sha256=case["source_sha256"],
        original_record_sha256=case["original_record_sha256"]))
    for name, fact in decoded.fields.items():
        if name in STATE_READBACK_FIELDS:
            if name.startswith("flip_owner_") and "owner_active_segment_link" in decoded.missing:
                gaps["state." + name] = "OWNER_AND_ACTIVE_SEGMENT_LINK_UNBOUND"
                continue
            propose("state." + name, fact["value"], dict(kind="REAL_TYPED_RECORDED_SNAPSHOT_FIELD", **fact))

    budgets = decoded.typed.get("v2_falseflip_budget")
    if budgets:
        propose("strategy._falseflip_budgets", budgets, dict(kind="POSITIVE_SYMBOL_LOCAL_TYPED_BUDGET_PROJECTION",
            completeness="OTHER_SYMBOL_RAM_NOT_CLAIMED", references=decoded.evidence["v2_falseflip_budget"]))
    request = decoded.typed.get("v2_removed_wait")
    if request is not None:
        propose("strategy._removed_wait_requests", {case["symbol"]: request},
            dict(kind="POSITIVE_SYMBOL_LOCAL_TYPED_REQUEST_PROJECTION", reference=decoded.evidence["v2_removed_wait"],
                completeness="OTHER_SYMBOL_RAM_NOT_CLAIMED"))
    if policy_review is not None:
        policy_names = {"_flip_owned_first_entry_enabled", "_retry_one_enabled", "_retry_one_max_retries", "_removed_wait_enabled"}
        if (type(policy_review) is not dict or set(policy_review) != {"world_head", "review_sha256", "source_reference", "values"}
                or policy_review["world_head"] != world["head"] or set(policy_review["values"]) != policy_names
                or type(policy_review["source_reference"]) is not str or not policy_review["source_reference"]
                or type(policy_review["review_sha256"]) is not str or len(policy_review["review_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in policy_review["review_sha256"])
                or policy_review["review_sha256"] == "0" * 64):
            raise ValueError("explicit_per_world_reviewed_configuration_not_RAM_defaults_required")
        for name, value in policy_review["values"].items():
            if ((name == "_retry_one_max_retries" and (type(value) is not int or value < 0))
                    or (name != "_retry_one_max_retries" and type(value) is not bool)):
                raise ValueError("explicit_policy_scalar_types_required")
            propose("strategy." + name, value, dict(kind="EXPLICIT_REVIEWED_MODEL_SETTINGS_NOT_HISTORICAL_RAM",
                world_head=world["head"], review_sha256=policy_review["review_sha256"],
                source_reference=policy_review["source_reference"]))

    prefix, bar_exclusions = eligible_causal_bars(record)
    mathematical = None
    if prefix_review is not None and prefix:
        mathematical = produce_causal_prefix(dict(case_id=case["case_id"], symbol=case["symbol"],
            clock_ms=int(material._stamp(case["selection_clock"]).timestamp() * 1000),
            anchor_ms=int(material._stamp(case["session_anchor"]).timestamp() * 1000)), prefix,
            period=period, factor=factor, review=prefix_review, world=world, source_guard=source_guard)
        for name, value in mathematical["expected_state_fragment"].items():
            propose("state." + name, value, dict(kind=mathematical["independence"],
                input_sha256=mathematical["input_sha256"], review_sha256=mathematical["review_sha256"],
                scope=mathematical["scope"], source_references=[item["reference"] for item in prefix]))

    logs = original_log_prefix(extracted_case, case)
    original_members = extracted_case["members"]["bot_log_lines"]
    positions = {member["source_pointer"]: i for i, member in enumerate(original_members)}
    callbacks = case["clock_references"]
    callback_index = positions.get(callbacks[0]["source_pointer"]) if len(callbacks) == 1 else None
    for member in logs["prefix"]:
        fact = member["fact"]
        if fact.get("tag") != "V2-CW-STATE-PROBE":
            continue
        refs = dict(kind="EXACT_ORIGINAL_PROBE_FIELD_NOT_NATIVE_BOOK", pointer=member["source_pointer"],
            decoded_text_sha256=member["decoded_text_sha256"], recorded_utc=fact["recorded_utc"],
            before_callback_in_original_order=(callback_index is not None
                and positions[member["source_pointer"]] < callback_index))
        for key, raw in fact["fields"].items():
            name, value = None, None
            if key in PROBE_BOOLS and raw in {"True", "False"}:
                name, value = PROBE_BOOLS[key], raw == "True"
            elif key in PROBE_INTS and type(raw) is str and raw.isascii() and raw.isdecimal():
                name, value = key, int(raw)
            elif key == "flip_owner_phase" and raw in {"idle", "resting", "awaiting_fill", "provisional", "bound", "consumed", "awaiting_close", "unknown"}:
                name, value = key, raw
            if name is None:
                continue
            if refs["before_callback_in_original_order"]:
                propose("state." + name, value, dict(refs, field=key))
            else:
                candidates.setdefault("state." + name, []).append(dict(value=value,
                    provenance=dict(refs, field=key), missing="EXACT_CALLBACK_OR_PRE_CALLBACK_LOG_ORDER_UNBOUND"))

    typed_fragments = {kind: _readback_value(value) for kind, value in decoded.typed.items()}
    required = ["state." + name for name in STATE_READBACK_FIELDS] + ["strategy." + name for name in STRATEGY_READBACK_FIELDS]
    for name in required:
        if name not in fields:
            gaps.setdefault(name, "NO_ACCEPTED_POSITIVE_FIELD_PROVENANCE_NO_CONSTRUCTOR_DEFAULT")
    if case["clock_precision"] == "RECORDED_FLIP_MINUTE_PREFIX_NOT_EXACT_CALLBACK":
        gaps["callback_clock_ms"] = "EXACT_ORIGINAL_CALLBACK_CLOCK_UNBOUND_NOT_RETIMED"
    else:
        propose("callback_clock_ms", int(material._stamp(case["selection_clock"]).timestamp() * 1000),
            dict(kind="EXACT_ORIGINAL_MEMBER_CLOCK_STILL_REQUIRES_CASE_LINEAGE_REVIEW", references=case["clock_references"]))
        if "callback_clock_ms" not in fields:
            gaps["callback_clock_ms"] = "EXACT_CLOCK_CASE_LINEAGE_REVIEW_NOT_ACCEPTED"
    for name in ("falseflip_entry_classifications", "cap_binding", "admission_memory", "brokerdecides_enabled", "falseflip_enabled"):
        gaps[name] = "SEPARATE_EXPLICIT_REVIEWED_BOOK_OR_CONFIGURATION_OPERAND_REQUIRED_NOT_DEFAULTED"
    output = dict(case_id=case["case_id"], world=world, fields=fields, candidates=candidates,
        typed_fragments=typed_fragments, mathematical_prefix=mathematical,
        eligible_prefix_count=len(prefix), excluded_bars=bar_exclusions,
        unresolved=gaps, callback_precision=case["clock_precision"],
        complete_constructor_packet=None, actual_predicate="UNRUN", historical_native_permission="UNKNOWN",
        permission_granted=False, scope="RECONSTRUCTED_LOCAL_FIELD_FRAGMENTS_NOT_COMPLETE_HISTORICAL_RAM")
    if len(material.canonical(output)) > 64 * 1024 * 1024:
        raise ValueError("single_case_field_binding_64MiB_exceeded_no_partial_acceptance")
    return output


def iter_complete122_field_bindings(manifest_path, *, manifest_sha256, manifest_guard,
        case_operands, record_guard, source_guard, field_guard):
    """Reuse complete bb iterator, consume/release one case. No batch retention."""
    from tools.l12_original122_sharded_materialization import iter_complete_original122

    for record in iter_complete_original122(manifest_path, manifest_sha256=manifest_sha256, source_guard=manifest_guard):
        operands = case_operands(record["case"]["case_id"])
        if set(operands) != {"extracted_case", "world", "prefix_review", "period", "factor", "policy_review"}:
            raise ValueError("explicit_per_case_world_clock_and_settings_operands_required")
        yield bind_materialized_case_fields(record, **operands, record_guard=record_guard,
            source_guard=source_guard, field_guard=field_guard)


def _constructor_fields(expected):
    outer = {"state", "strategy", "falseflip_entry_classifications", "cap_binding",
        "admission_memory", "callback_clock_ms", "brokerdecides_enabled", "falseflip_enabled"}
    if (type(expected) is not dict or set(expected) != outer
            or type(expected["state"]) is not dict or set(expected["state"]) != set(STATE_READBACK_FIELDS)
            or type(expected["strategy"]) is not dict or set(expected["strategy"]) != set(STRATEGY_READBACK_FIELDS)):
        raise ValueError("complete_original_constructor_readset_required_not_branch_subset")
    return {**{"state." + k: v for k, v in expected["state"].items()},
        **{"strategy." + k: v for k, v in expected["strategy"].items()},
        **{k: expected[k] for k in outer - {"state", "strategy"}}}


def _require_live_case_bound(*values):
    """Serialized live-case envelope, not a claim about Python allocator RSS."""
    size = 0
    for value in values:
        size += len(material.canonical(value))
        if size > 64 * 1024 * 1024:
            raise ValueError("one_live_case_64MiB_serialized_envelope_exceeded")


def validate_causal_constructor_fields(record, binding, packet, lineage, *, extracted_case, world, field_guard):
    """Validate expected operands against positive sources BEFORE object creation.

    Additional lineage is Main's independently reviewed causal reconstruction,
    not a way to turn a missing row/constructor default into a historical fact.
    """
    from tools.l12_original122_branch_adapter import _references, _hash

    case = record["case"]
    if (type(packet) is not dict or set(packet) != {"references", "restoration", "expected_object_readback", "operands"}
            or type(lineage) is not dict or not callable(field_guard)
            or binding["case_id"] != case["case_id"] or binding["world"] != world):
        raise ValueError("exact_case_packet_and_independent_field_lineage_required")
    values = _constructor_fields(packet["expected_object_readback"])
    if set(lineage) - set(values):
        raise ValueError("foreign_or_unused_field_lineage_refused")
    cutoff = int(material._stamp(case["selection_clock"]).timestamp() * 1000)
    if (type(values["callback_clock_ms"]) is not int or values["callback_clock_ms"] != cutoff
            or packet["operands"].get("case_id") != case["case_id"]
            or packet["operands"].get("case_readback_sha256") != record["readback_sha256"]):
        raise ValueError("original_selection_clock_and_exact_restored_case_readback_required")
    allowed_policy = {"strategy._flip_owned_first_entry_enabled", "strategy._retry_one_enabled",
        "strategy._retry_one_max_retries", "strategy._removed_wait_enabled",
        "brokerdecides_enabled", "falseflip_enabled"}
    known = {(name, tuple(item["reference"]["primary_key"]), item["reference"]["source_row_sha256"]): item
        for name, items in record["rows"].items() for item in items}
    refs = packet["operands"].get("restored_row_refs")
    actual_refs = {material.canonical(item["reference"]) for items in record["rows"].values() for item in items}
    if (type(refs) is not list or not 0 < len(refs) <= 4096
            or any(type(ref) is not dict or material.canonical(ref) not in actual_refs for ref in refs)
            or len({material.canonical(ref) for ref in refs}) != len(refs)):
        raise ValueError("constructor_row_refs_must_be_exact_unique_members_of_actual_case")
    missing = []
    for name, value in values.items():
        fact = binding["fields"].get(name)
        if fact is not None:
            if material.canonical(_readback_value(value)) != material.canonical(fact["value"]):
                raise ValueError("constructor_value_differs_from_independent_bound_field")
            continue
        proof = lineage.get(name)
        if proof is None:
            missing.append(name)
            continue
        fields = {"origin", "value", "case_id", "world_head", "original_record_sha256",
            "callback_clock_ms", "review_sha256", "row_references", "original_members"}
        if (type(proof) is not dict or set(proof) != fields
                or proof["case_id"] != case["case_id"] or proof["world_head"] != world["head"]
                or proof["original_record_sha256"] != case["original_record_sha256"]
                or type(proof["callback_clock_ms"]) is not int or proof["callback_clock_ms"] != cutoff
                or not _hash(proof["review_sha256"]) or proof["review_sha256"] == "0" * 64
                or material.canonical(_readback_value(value)) != material.canonical(proof["value"])
                or type(proof["row_references"]) is not list or len(proof["row_references"]) > 4096
                or type(proof["original_members"]) is not list or len(proof["original_members"]) > 256):
            raise ValueError("independent_exact_field_value_case_clock_and_review_required")
        origin = proof["origin"]
        if origin == "EXPLICIT_REVIEWED_MODEL_SETTINGS_NOT_HISTORICAL_RAM":
            if name not in allowed_policy or proof["row_references"] or proof["original_members"]:
                raise ValueError("model_policy_cannot_supply_historical_RAM_or_native_facts")
            if (name == "strategy._retry_one_max_retries" and (type(value) is not int or value < 0)
                    or name != "strategy._retry_one_max_retries" and type(value) is not bool):
                raise ValueError("explicit_model_policy_scalar_type_required")
        elif origin == "C11929_CONTROLLED_NATIVE_UNKNOWN":
            if name not in {"cap_binding", "admission_memory"} or proof["row_references"] or proof["original_members"]:
                raise ValueError("controlled_native_not_historical_RAM_or_callback_clock")
            if value is not None:
                if (name != "admission_memory" or type(value) is not dict
                        or set(value) != {"snapshot", "reservations"} or value["reservations"]):
                    raise ValueError("controlled_native_must_not_invent_local_reservations")
                snap = value["snapshot"]
                if snap is not None and (type(snap) is not dict or snap.get("local_complete") is not False
                        or type(snap.get("books")) is not list
                        or any(type(book) is not dict or book.get("complete") is not False for book in snap["books"])):
                    raise ValueError("controlled_native_completeness_must_remain_UNKNOWN")
        elif origin == "INDEPENDENT_RETAINED_LOCAL_CAUSAL_FACT":
            if not proof["row_references"] and not proof["original_members"]:
                raise ValueError("absence_or_constructor_default_is_not_positive_causal_evidence")
            if proof["original_members"]:
                members = _references(extracted_case, proof["original_members"])
                positions = {m["source_pointer"]: i for i, m in enumerate(extracted_case["members"]["bot_log_lines"])}
                refs = case["clock_references"]
                callback_index = positions.get(refs[0]["source_pointer"]) if len(refs) == 1 else None
                for member in members:
                    fact = member["fact"]
                    if (not fact.get("recorded_utc") or fact.get("source_symbol_matches") is not True
                            or fact.get("within_declared_log_window") is not True or fact.get("duplicate_field_names")):
                        raise ValueError("unbound_original_log_identity_or_clock_is_not_state_proof")
                    stamp = int(material._stamp(fact["recorded_utc"]).timestamp() * 1000)
                    if name == "callback_clock_ms":
                        if (stamp != cutoff or fact.get("tag") != "V2-FLIP-OWNER-ADMISSION"
                                or fact.get("fields", {}).get("slot") != "first"):
                            raise ValueError("exact_first_admission_clock_not_minute_or_arbitrary_log_required")
                    elif (stamp > cutoff or stamp == cutoff and (callback_index is None
                            or positions.get(member["source_pointer"], callback_index) >= callback_index)):
                        raise ValueError("later_or_equal_after_callback_log_is_not_state_proof")
            for ref in proof["row_references"]:
                if type(ref) is not dict or set(ref) != {"table", "primary_key", "source_row_sha256"}:
                    raise ValueError("exact_retained_field_row_reference_required")
                item = known.get((ref["table"], tuple(ref["primary_key"]), ref["source_row_sha256"]))
                if item is None:
                    raise ValueError("field_reference_not_in_accepted_case_materialization")
                chronology = material.classify_retained_row(ref["table"], item["row"], case)
                if chronology != item["chronology"] or chronology in {"LATER_ONLY_CONTEXT",
                        "MUTATED_AFTER_CUTOFF_NOT_HISTORICAL_PAYLOAD", "CREATION_CLOCK_UNKNOWN", "BAR_NOT_CLOSED_AT_CUTOFF"}:
                    raise ValueError("future_mutated_or_unknown_row_cannot_supply_historical_field")
                if chronology == "EVENT_TIME_AT_OR_BEFORE_CUTOFF_ARRIVAL_UNKNOWN" and not proof["original_members"]:
                    raise ValueError("event_time_alone_does_not_prove_historical_arrival")
        else:
            raise ValueError("unreviewed_field_origin_not_a_causal_fact")
        if field_guard(case, name, dict(value=_readback_value(value), provenance=deepcopy(proof))) is not True:
            missing.append(name)
    return sorted(missing)


def replay_complete122_causal_world(original_raw, *, world, manifest_path, manifest_sha256,
        manifest_guard, case_operands, record_guard, prefix_source_guard, field_guard,
        method_source_guard, operand_guard, restoration_guard, settings):
    """One reviewed source-world process; one complete shard/real object at a time.

    No DB/readback retry, state defaults or source approval is created here.
    Unknown cases stay in the denominator; no complete result on a late failure.
    """
    from tools.l12_aa87_original122_ledger import GROUPS, index_rows
    from tools.l12_original122_embedded_identity import _extract, FIELDS, MAX_MEMBERS
    from tools.l12_original122_sharded_materialization import iter_complete_original122, SOURCE_LIMIT
    from tools.l12_original122_branch_adapter import (
        C11229_INPUT_LIMITED, _construct_one_original122_case, construct_serialized_first_rest_case,
        _world, summarize_original122_local_callbacks,
    )

    world = validate_original122_world(world)
    if (type(original_raw) is not bytes or len(original_raw) > SOURCE_LIMIT
            or not all(callable(fn) for fn in (manifest_guard, case_operands, record_guard,
                prefix_source_guard, field_guard, method_source_guard, operand_guard, restoration_guard))):
        raise ValueError("bounded_original_raw_and_actual_independent_world_guards_required")
    indexed = index_rows(original_raw, expected_sha=material.EXPORT_SHA, expected_groups=GROUPS)
    if sum(len(row["original_record"][name]) for row in indexed for name in FIELDS) > MAX_MEMBERS:
        raise ValueError("complete_original_member_bound_exceeded")
    outcomes, constructions, seen = [], [], set()
    iterator = iter_complete_original122(manifest_path, manifest_sha256=manifest_sha256,
        source_guard=manifest_guard)
    try:
        for index, record in enumerate(iterator):
            if index >= 122:
                raise ValueError("extra_case_in_complete_manifest")
            extracted = _extract([indexed[index]])
            case = extracted["cases"][0]
            cid = case["case_id"]
            if (record["case"]["case_id"] != cid or cid in seen
                    or record["case"]["original_record_sha256"] != case["original_record_sha256"]
                    or record["case"]["source_sha256"] != material.EXPORT_SHA):
                raise ValueError("exact_original122_order_record_and_source_identity_required")
            seen.add(cid)
            operands = case_operands(cid, record, case, dict(world))
            if (type(operands) is not dict or set(operands) != {"prefix_review", "period", "factor",
                    "policy_review", "serialized_packet", "field_lineage"}):
                raise ValueError("explicit_case_math_policy_packet_and_lineage_operands_required")
            _require_live_case_bound(record, case, operands)
            binding = bind_materialized_case_fields(record, case, world=world,
                prefix_review=operands["prefix_review"], period=operands["period"], factor=operands["factor"],
                record_guard=record_guard, source_guard=prefix_source_guard, field_guard=field_guard,
                policy_review=operands["policy_review"])
            _require_live_case_bound(record, case, operands, binding)
            packet = operands["serialized_packet"]
            if packet is None and operands["field_lineage"]:
                raise ValueError("unused_field_lineage_cannot_be_silently_dropped")
            supplied = None
            construction = dict(case_id=cid, construction="UNRUN_NO_COMPLETE_CAUSAL_PACKET",
                missing_operands=sorted(binding["unresolved"]), eligible_prefix_count=binding["eligible_prefix_count"])
            if cid in C11229_INPUT_LIMITED:
                if binding["eligible_prefix_count"] != 0 or packet is not None:
                    raise ValueError("C11229_five_cannot_receive_invented_prefix_or_predicate_packet")
                construction["disposition"] = "UNKNOWN_INPUT_LIMITED"
            elif binding["eligible_prefix_count"] == 0:
                raise ValueError("new_no_bar_case_requires_separate_disposition_not_five_alias")
            elif packet is not None:
                if (type(packet) is not dict or type(packet.get("operands")) is not dict
                        or packet["operands"].get("materialization_sha256") != manifest_sha256):
                    raise ValueError("packet_must_bind_actual_complete122_manifest")
                missing = validate_causal_constructor_fields(record, binding, packet, operands["field_lineage"],
                    extracted_case=case, world=world, field_guard=field_guard)
                if missing:
                    construction["missing_operands"] = missing
                else:
                    def constructor(original, serialized, selected_world):
                        return construct_serialized_first_rest_case(original, serialized, selected_world, settings=settings)

                    supplied, construction = _construct_one_original122_case(case, packet, world,
                        construct_real_case=constructor, source_guard=method_source_guard, operand_guard=operand_guard)
            result = _world(extracted, world=world, bound_cases={} if supplied is None else {cid: supplied},
                source_guard=method_source_guard, restoration_guard=restoration_guard)
            if len(result["rows"]) != 1 or result["rows"][0]["case_id"] != cid:
                raise ValueError("one_actual_original_case_outcome_required")
            outcomes.append(result["rows"][0])
            constructions.append(dict(construction, field_binding_sha256=sha256(material.canonical(binding)).hexdigest()))
            del supplied, binding, operands, packet, record, extracted, case, result
    finally:
        iterator.close()
    if len(outcomes) != 122 or seen != {row["case_id"] for row in indexed}:
        raise ValueError("no_partial_original122_world_report_accepted")
    return dict(version="ORIGINAL122_COMPLETE_CAUSAL_WORLD_CONSUMER_V1", world=world,
        original_cases=122, omitted_original_cases=0, rows=outcomes, constructions=constructions,
        local_reconstruction=summarize_original122_local_callbacks(outcomes),
        manifest_sha256=manifest_sha256, permission_granted=False,
        historical_native_permission="UNKNOWN", actual_close_boot="UNRUN",
        scope="REAL_RECONSTRUCTED_LOCAL_PREDICATES_NOT_HISTORICAL_FULL_BUY_OR_NATIVE_PASS")
