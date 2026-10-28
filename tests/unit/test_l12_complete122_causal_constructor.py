"""CONTROLLED causal/readback seams; not actual122 or native acceptance."""

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from tools import l12_original122_branch_adapter as adapter
from tools import l12_original122_field_binding as binding


STATE = """symbol bars atr_state atr_short_flip_bar_ts slotclear_fresh_buy_bar_ms
slotclear_fresh_buy_segment_id cw_seed_cap_watch_start_ms cw_resting_taken cw_reclaim_taken
flip_owner_phase flip_owner_first_rest_placed flip_owner_fill_accounts flip_owner_position_ids
flip_owner_open_positions position_qty position_qty_held resting_active webull_resting_active
cw_v2_emit_claimed fanout_webull_claimed fanout_claim_outcome fanout_segment_id
flip_owner_opportunity_id retry_one_budget_readable retry_one_segment_id retry_one_closes_in_segment
flip_owner_evidence_readable flip_owner_evidence_at_ms""".split()
STRATEGY = """_flip_owned_first_entry_enabled _retry_one_enabled _retry_one_max_retries
_flip_owner_restore_readable _removed_wait_enabled _removed_wait_restore_readable
_removed_wait_requests _removed_scanner_symbols _falseflip_readable _falseflip_budgets
_falseflip_pending _falseflip_cross_high""".split()


def literal_constructor_packet():
    state = dict(symbol="CLWT", bars=[], atr_state=None, atr_short_flip_bar_ts=0,
        slotclear_fresh_buy_bar_ms=0, slotclear_fresh_buy_segment_id=0,
        cw_seed_cap_watch_start_ms=0, cw_resting_taken=False, cw_reclaim_taken=False,
        flip_owner_phase="idle", flip_owner_first_rest_placed=False, flip_owner_fill_accounts=[],
        flip_owner_position_ids={}, flip_owner_open_positions={}, position_qty=0, position_qty_held=0,
        resting_active=False, webull_resting_active=False, cw_v2_emit_claimed=False,
        fanout_webull_claimed=False, fanout_claim_outcome="", fanout_segment_id=0,
        flip_owner_opportunity_id=0, retry_one_budget_readable=False, retry_one_segment_id=0,
        retry_one_closes_in_segment=0, flip_owner_evidence_readable=False, flip_owner_evidence_at_ms=0)
    strategy = dict(_flip_owned_first_entry_enabled=False, _retry_one_enabled=False,
        _retry_one_max_retries=1, _flip_owner_restore_readable=False, _removed_wait_enabled=False,
        _removed_wait_restore_readable=False, _removed_wait_requests={}, _removed_scanner_symbols=[],
        _falseflip_readable=False, _falseflip_budgets={}, _falseflip_pending={}, _falseflip_cross_high=[])
    return dict(expected_object_readback=dict(state=state, strategy=strategy,
        falseflip_entry_classifications=None, cap_binding=None, admission_memory=None,
        callback_clock_ms=1000, brokerdecides_enabled=True, falseflip_enabled=False))


def controlled_settings():
    from project_mai_tai.settings import Settings
    return Settings(_env_file=None, strategy_schwab_1m_v2_brokerdecides1_enabled=True,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=False,
        strategy_schwab_1m_v2_retry_one_enabled=False,
        strategy_schwab_1m_v2_retry_one_max_retries=1,
        strategy_schwab_1m_v2_removed_wait_clear_enabled=False,
        strategy_schwab_1m_v2_false_flip_enabled=False)


def new_world():
    return dict(label="NEW", head="f" * 40, source_review_sha256="a" * 64)


def inputs():
    cid = "2026-10-12/YMAT/07:59"
    case = dict(case_id=cid, symbol="YMAT", selection_clock="1970-01-01T00:00:10+00:00",
        original_record_sha256="a" * 64, clock_references=[])
    expected = dict(state=dict.fromkeys(STATE), strategy=dict.fromkeys(STRATEGY),
        falseflip_entry_classifications=None, cap_binding=None, admission_memory=None,
        callback_clock_ms=10000, brokerdecides_enabled=True, falseflip_enabled=False)
    expected["state"]["symbol"] = "YMAT"
    world = dict(label="NEW", head="f" * 40, source_review_sha256="b" * 64)
    facts = {**{"state." + k: dict(value=v) for k, v in expected["state"].items()},
        **{"strategy." + k: dict(value=v) for k, v in expected["strategy"].items()},
        **{k: dict(value=expected[k]) for k in ("falseflip_entry_classifications", "cap_binding",
            "admission_memory", "callback_clock_ms", "brokerdecides_enabled", "falseflip_enabled")}}
    ref = dict(table="broker_accounts", primary_key=["CONTROLLED"], source_row_sha256="e" * 64)
    record = dict(case=case, rows={"broker_accounts": [dict(row={}, reference=deepcopy(ref))]}, readback_sha256="c" * 64)
    fragments = dict(case_id=cid, world=world, fields=facts)
    packet = dict(references=[], restoration={}, expected_object_readback=expected,
        operands=dict(case_id=cid, case_readback_sha256="c" * 64, restored_row_refs=[deepcopy(ref)]))
    original = dict(case_id=cid, members=dict(bot_log_lines=[], orders_symbol_day=[], fills_symbol_day=[]))
    return record, fragments, packet, original, world


def proof(record, world, value, origin="INDEPENDENT_RETAINED_LOCAL_CAUSAL_FACT"):
    return dict(origin=origin, value=value, case_id=record["case"]["case_id"], world_head=world["head"],
        original_record_sha256="a" * 64, callback_clock_ms=10000, review_sha256="d" * 64,
        row_references=[], original_members=[])


def check(args, lineage=None, guard=lambda *args: True):
    record, fragments, packet, original, world = args
    return binding.validate_causal_constructor_fields(record, fragments, packet, lineage or {},
        extracted_case=original, world=world, field_guard=guard)


def test_full_readset46_is_independently_proven_before_constructor():
    args = inputs()
    assert len(args[1]["fields"]) == 46
    assert check(args) == []


@pytest.mark.parametrize("fault", ["state", "strategy", "missing_state", "extra_state", "outer"])
def test_no_branch_shortcut_can_shrink_constructor_readset(fault):
    args = inputs()
    expected = args[2]["expected_object_readback"]
    if fault in {"state", "strategy"}:
        del expected[fault]
    elif fault == "missing_state":
        del expected["state"]["retry_one_segment_id"]
    elif fault == "extra_state":
        expected["state"]["inferred_ready"] = True
    else:
        del expected["admission_memory"]
    with pytest.raises(ValueError):
        check(args)


def test_false_is_not_zero_under_bound_field_comparison():
    args = inputs()
    args[2]["expected_object_readback"]["state"]["resting_active"] = False
    args[1]["fields"]["state.resting_active"]["value"] = 0
    with pytest.raises(ValueError, match="independent_bound_field"):
        check(args)


def test_missing_RAM_stays_missing_and_no_constructor_is_invoked():
    args = inputs()
    del args[1]["fields"]["state.retry_one_budget_readable"]
    assert check(args) == ["state.retry_one_budget_readable"]


@pytest.mark.parametrize("fault", ["review", "head", "case", "record", "clock", "origin", "extra"])
def test_exact_independent_lineage_cannot_be_replaced_by_a_claimed_value(fault):
    args = inputs()
    name = "state.resting_active"
    del args[1]["fields"][name]
    value = proof(args[0], args[4], None)
    if fault == "extra":
        value["permission"] = True
    else:
        key = {"review": "review_sha256", "head": "world_head", "case": "case_id",
            "record": "original_record_sha256", "clock": "callback_clock_ms", "origin": "origin"}[fault]
        value[key] = 10001 if fault == "clock" else "0" * 64
    with pytest.raises(ValueError):
        check(args, {name: value})


def test_policy_may_bind_configuration_but_never_restore_readability():
    args = inputs()
    name = "strategy._retry_one_enabled"
    del args[1]["fields"][name]
    args[2]["expected_object_readback"]["strategy"]["_retry_one_enabled"] = False
    policy = proof(args[0], args[4], False, "EXPLICIT_REVIEWED_MODEL_SETTINGS_NOT_HISTORICAL_RAM")
    assert check(args, {name: policy}) == []
    assert check(args, {name: policy}, guard=lambda *args: False) == [name]
    historical = "strategy._flip_owner_restore_readable"
    del args[1]["fields"][historical]
    args[2]["expected_object_readback"]["strategy"]["_flip_owner_restore_readable"] = False
    with pytest.raises(ValueError, match="cannot_supply_historical"):
        check(args, {name: policy, historical: policy})


def test_absence_of_rows_cannot_prove_idle_or_unused_slot():
    args = inputs()
    name = "state.cw_resting_taken"
    del args[1]["fields"][name]
    with pytest.raises(ValueError, match="absence_or_constructor_default"):
        check(args, {name: proof(args[0], args[4], None)})


@pytest.mark.parametrize("fault", ["local", "book", "reservations"])
def test_controlled_native_cannot_promote_completeness_or_invent_local_reservation(fault):
    args = inputs()
    name = "admission_memory"
    del args[1]["fields"][name]
    memory = dict(snapshot=dict(local_complete=False, books=[dict(complete=False)]), reservations={})
    if fault == "local":
        memory["snapshot"]["local_complete"] = True
    elif fault == "book":
        memory["snapshot"]["books"][0]["complete"] = True
    else:
        memory["reservations"]["YMAT"] = "invented"
    args[2]["expected_object_readback"][name] = memory
    with pytest.raises(ValueError, match="controlled_native"):
        check(args, {name: proof(args[0], args[4], memory, "C11929_CONTROLLED_NATIVE_UNKNOWN")})


def test_explicit_controlled_native_unknown_is_not_a_historical_fact():
    args = inputs()
    name = "cap_binding"
    del args[1]["fields"][name]
    assert check(args, {name: proof(args[0], args[4], None, "C11929_CONTROLLED_NATIVE_UNKNOWN")}) == []
    assert args[2]["expected_object_readback"]["admission_memory"] is None


@pytest.mark.parametrize("chronology", ["LATER_ONLY_CONTEXT", "MUTATED_AFTER_CUTOFF_NOT_HISTORICAL_PAYLOAD",
    "CREATION_CLOCK_UNKNOWN", "BAR_NOT_CLOSED_AT_CUTOFF"])
def test_noncausal_row_never_supplies_RAM_even_with_positive_guard(monkeypatch, chronology):
    args = inputs()
    name = "state.resting_active"
    del args[1]["fields"][name]
    ref = dict(table="broker_orders", primary_key=["pk"], source_row_sha256="e" * 64)
    args[0]["rows"] = {"broker_orders": [dict(row={}, reference=deepcopy(ref), chronology=chronology)]}
    args[2]["operands"]["restored_row_refs"] = [deepcopy(ref)]
    value = proof(args[0], args[4], None)
    value["row_references"] = [ref]
    monkeypatch.setattr(binding.material, "classify_retained_row", lambda *args: chronology)
    with pytest.raises(ValueError, match="future_mutated_or_unknown"):
        check(args, {name: value})


def test_fill_event_time_without_arrival_log_cannot_prove_consumed_state(monkeypatch):
    args = inputs()
    name = "state.position_qty"
    del args[1]["fields"][name]
    ref = dict(table="fills", primary_key=["pk"], source_row_sha256="e" * 64)
    chronology = "EVENT_TIME_AT_OR_BEFORE_CUTOFF_ARRIVAL_UNKNOWN"
    args[0]["rows"] = {"fills": [dict(row={}, reference=deepcopy(ref), chronology=chronology)]}
    args[2]["operands"]["restored_row_refs"] = [deepcopy(ref)]
    value = proof(args[0], args[4], None)
    value["row_references"] = [ref]
    monkeypatch.setattr(binding.material, "classify_retained_row", lambda *args: chronology)
    with pytest.raises(ValueError, match="historical_arrival"):
        check(args, {name: value})


@pytest.mark.parametrize("fault", ["future", "equal_after", "wrong_symbol"])
def test_original_log_clock_and_order_are_not_guessed(fault):
    args = inputs()
    name = "state.resting_active"
    del args[1]["fields"][name]
    member = dict(source_pointer="/0/bot_log_lines/0", decoded_text_sha256="e" * 64,
        fact=dict(recorded_utc="1970-01-01T00:00:10+00:00", source_symbol_matches=True,
            within_declared_log_window=True))
    if fault == "future":
        member["fact"]["recorded_utc"] = "1970-01-01T00:00:11+00:00"
    elif fault == "wrong_symbol":
        member["fact"]["source_symbol_matches"] = False
    args[3]["members"]["bot_log_lines"] = [member]
    value = proof(args[0], args[4], None)
    value["original_members"] = [dict(pointer=member["source_pointer"], decoded_text_sha256="e" * 64)]
    with pytest.raises(ValueError):
        check(args, {name: value})


def test_serialized_case_bound_is_not_raised_by_field_acceptance(monkeypatch):
    class Sized:
        def __len__(self):
            return 33 * 1024 * 1024
    monkeypatch.setattr(binding.material, "canonical", lambda value: Sized())
    with pytest.raises(ValueError, match="64MiB"):
        binding._require_live_case_bound({}, {})


@pytest.mark.parametrize("fault", ["foreign", "duplicate", "empty"])
def test_packet_refs_are_actual_case_members_not_merely_hash_shaped(fault):
    args = inputs()
    refs = args[2]["operands"]["restored_row_refs"]
    if fault == "foreign":
        refs[0]["primary_key"] = ["FOREIGN"]
    elif fault == "duplicate":
        refs.append(deepcopy(refs[0]))
    else:
        refs.clear()
    with pytest.raises(ValueError, match="exact_unique_members"):
        check(args)


def test_five_byte_emitter_requires_complete_accepted_world_report():
    five = ["2026-10-15/BBCI/07:55", "2026-10-16/JBDI/09:18", "2026-10-16/WORX/08:17",
        "2026-10-19/EXYN/11:15", "2026-10-19/WORX/10:10"]
    rows = [dict(case_id=cid, original_record_sha256="a" * 64, decision="UNKNOWN",
        permission_granted=False, callback="UNRUN_MISSING_RESTORED_OPERANDS", branch_value=None,
        native_asof="UNKNOWN_NOT_STORED", same_entry_opportunity="UNKNOWN", missing_operands=["UNBOUND"])
        for cid in five]
    rows.extend(dict(case_id="CONTROLLED_" + str(i)) for i in range(117))
    report = dict(original_cases=122, omitted_original_cases=0, rows=rows)
    result = adapter.emit_original122_five_case_bytes(report, report_guard=lambda report: True)
    assert set(result) == set(five) and all(type(raw) is bytes for raw in result.values())
    compared = adapter.compare_c11229_input_limited(result, deepcopy(result), ruling_sha256="a" * 64,
        case_guard=lambda *args: True)
    assert compared["input_limited_case_count"] == 5 and compared["permission_granted"] is False
    assert compared["remaining_117_caused"] == "NOT_ESTABLISHED_BY_THIS_CHECK"
    with pytest.raises(ValueError, match="report_acceptance"):
        adapter.emit_original122_five_case_bytes(report, report_guard=lambda report: False)
    report["rows"].pop()
    with pytest.raises(ValueError, match="report_acceptance"):
        adapter.emit_original122_five_case_bytes(report, report_guard=lambda report: True)


def complete_controlled_packet():
    packet = literal_constructor_packet()
    case = dict(case_id="2026-10-12/CLWT/07:59", original_record_sha256="a" * 64,
        members=dict(bot_log_lines=[dict(source_pointer="/0/bot_log_lines/0", decoded_text_sha256="b" * 64)]))
    packet.update(references=[dict(pointer="/0/bot_log_lines/0", decoded_text_sha256="b" * 64)],
        restoration=dict(origin="EXPLICIT_REVIEWED_RECONSTRUCTED_LOCAL_BRANCH", reference_input_sha256=sha256(
            json.dumps(dict(case_id=case["case_id"], record_sha256="a" * 64,
                members=[dict(pointer="/0/bot_log_lines/0", sha256="b" * 64)]),
                sort_keys=True, separators=(",", ":")).encode()).hexdigest(), review_sha256="c" * 64,
            readback_sha256=sha256(json.dumps(packet["expected_object_readback"], sort_keys=True,
                separators=(",", ":")).encode()).hexdigest(), missing_operands=[]),
        operands=dict(case_id=case["case_id"], materialization_sha256="d" * 64,
            case_readback_sha256="e" * 64, restored_row_refs=[dict(CONTROLLED=True)], unbound_fields=[]))
    return case, packet


def test_shared_constructor_kernel_builds_real_objects_and_readback_without_predicate(monkeypatch):
    case, packet = complete_controlled_packet()
    def no_predicate(*args):
        pytest.fail("construction must not execute entry predicate")
    monkeypatch.setattr(adapter, "_call_real_first_rest", no_predicate)
    def constructor(case, packet, world):
        return adapter.construct_serialized_first_rest_case(case, packet, world, settings=controlled_settings())
    supplied, row = adapter._construct_one_original122_case(case, packet, new_world(),
        construct_real_case=constructor, source_guard=lambda *args: True, operand_guard=lambda *args: True)
    assert supplied["state"].symbol == "CLWT"
    assert adapter.first_rest_object_readback(supplied["strategy"], supplied["state"]) == packet["expected_object_readback"]
    assert row["construction"] == "REAL_OBJECTS_READ_BACK_NOT_PREDICATE_EXECUTION"


@pytest.mark.parametrize("missing", [False, True])
def test_independent_guard_or_missing_field_prevents_real_constructor(missing):
    case, packet = complete_controlled_packet()
    if missing:
        packet["restoration"]["missing_operands"] = ["historical_retry_unbound"]
    def forbidden(*args):
        pytest.fail("unaccepted input must never construct a default state")
    supplied, row = adapter._construct_one_original122_case(case, packet, new_world(),
        construct_real_case=forbidden, source_guard=lambda *args: True, operand_guard=lambda *args: False)
    assert supplied is None and row["construction"].startswith("UNRUN_")


def install_controlled_stream(monkeypatch, *, count=122, foreign=False):
    from tools import l12_aa87_original122_ledger as ledger
    from tools import l12_original122_embedded_identity as embedded
    from tools import l12_original122_sharded_materialization as shards
    five = ["2026-10-15/BBCI/07:55", "2026-10-16/JBDI/09:18", "2026-10-16/WORX/08:17",
        "2026-10-19/EXYN/11:15", "2026-10-19/WORX/10:10"]
    ids = five + ["2026-10-12/CONTROLLED" + str(i) + "/07:59" for i in range(117)]
    indexed = [dict(case_id=cid, original_record=dict(bot_log_lines=[], orders_symbol_day=[], fills_symbol_day=[])) for cid in ids]
    monkeypatch.setattr(ledger, "index_rows", lambda *args, **kwargs: indexed)
    def extract(rows):
        return dict(cases=[dict(case_id=rows[0]["case_id"], original_record_sha256="a" * 64)])
    monkeypatch.setattr(embedded, "_extract", extract)
    closed = []
    def iterate(*args, **kwargs):
        try:
            for i in range(count):
                cid = ids[i] if i < 122 else "EXTRA"
                if foreign and i == 0:
                    cid = "FOREIGN"
                yield dict(case=dict(case_id=cid, original_record_sha256="a" * 64,
                    source_sha256=ledger.EXPORT_SHA), rows={})
        finally:
            closed.append(True)
    monkeypatch.setattr(shards, "iter_complete_original122", iterate)
    def fragments(record, extracted, *, world, **kwargs):
        return dict(case_id=record["case"]["case_id"], world=world,
            fields={}, unresolved=dict(historical_retry="NOT_RECORDED"),
            eligible_prefix_count=0 if record["case"]["case_id"] in five else 1)
    monkeypatch.setattr(binding, "bind_materialized_case_fields", fragments)
    def forbidden(*args, **kwargs):
        pytest.fail("missing state must not construct or call a predicate")
    monkeypatch.setattr(adapter, "_construct_one_original122_case", forbidden)
    monkeypatch.setattr(adapter, "_call_real_first_rest", forbidden)
    return closed


def run_controlled_stream():
    return binding.replay_complete122_causal_world(b"CONTROLLED", world=new_world(),
        manifest_path="CONTROLLED_NOT_READ", manifest_sha256="e" * 64, manifest_guard=lambda *args: True,
        case_operands=lambda *args: dict(prefix_review=None, period=5, factor=1.0, policy_review=None,
            serialized_packet=None, field_lineage={}), record_guard=lambda *args: True,
        prefix_source_guard=lambda *args: True, field_guard=lambda *args: True,
        method_source_guard=lambda *args: True, operand_guard=lambda *args: True,
        restoration_guard=lambda *args: True, settings=None)


def test_complete122_stream_keeps_all_unknowns_and_five_dispositions_without_constructor(monkeypatch):
    closed = install_controlled_stream(monkeypatch)
    result = run_controlled_stream()
    assert result["original_cases"] == 122 and result["omitted_original_cases"] == 0
    assert result["local_reconstruction"]["local_state_UNKNOWN_count"] == 122
    assert result["local_reconstruction"]["C1_case_review_required"] is True
    assert sum(row.get("disposition") == "UNKNOWN_INPUT_LIMITED" for row in result["constructions"]) == 5
    assert result["permission_granted"] is False and closed == [True]


@pytest.mark.parametrize("fault", ["partial", "extra", "foreign"])
def test_no_complete_world_result_from_partial_extra_or_foreign_case(monkeypatch, fault):
    closed = install_controlled_stream(monkeypatch, count=121 if fault == "partial" else 123 if fault == "extra" else 122,
        foreign=fault == "foreign")
    with pytest.raises(ValueError):
        run_controlled_stream()
    assert closed == [True]
