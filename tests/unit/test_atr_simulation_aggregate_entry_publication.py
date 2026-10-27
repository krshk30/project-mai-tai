"""CONTROLLED receipt metadata only: no model, DB, filesystem or provider IO."""

import base64
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json

import pytest

from project_mai_tai.atr_simulation import aggregate_entry_publication as pub
from project_mai_tai.atr_simulation.aggregate_entry_contract import LABEL, MODE, MODEL, VERSION


def source(artifact="CONTROLLED:retained-buy", sha=pub.BUY_SHA, pointer="$"):
    return dict(artifact=artifact, sha256=sha, pointer=pointer, kind="RETAINED", asof_ns=None)


@pytest.fixture
def operands(monkeypatch):
    pins = tuple((name, hashlib.sha256(("CONTROLLED:" + name).encode()).hexdigest()) for name in pub.PRODUCER_FILES)
    binding = pub.AggregateProducerBinding("/CONTROLLED/producer", "a" * 40, pins,
                                           (("A", "webull"), ("B", "schwab")), "CONTROLLED:ATR")
    absolute_pins = {binding.root + "/" + name: sha for name, sha in pins}
    cases = []
    for index in range(173):
        key = ["X", "2026-10-12", f"07:{index // 60:02}:{index % 60:02}"]
        rows = []
        for account in (("A", "B") if index < 80 else ("A",)):
            order = f"CONTROLLED-order-{index}-{account}"
            identity = dict(trading_day=key[1], symbol="X", broker=dict(binding.account_brokers)[account],
                account_id=account, strategy=binding.strategy_code,
                flip_id="RECORDED_AGGREGATE_ORDER:" + order, segment_id="ACTUAL_ENTRY_BASELINE:" + order)
            original = source(pointer=f"line:{index * 2 + (1 if account == 'A' else 2)}")
            aggregate_source = {**original, "kind":"DERIVED", "pointer":original["pointer"] + ";" + pub.ASSUMPTION}
            ns = 1791802975000000000 + index * 10000000000
            raw_id = f"ACTUAL_ORDER_AGGREGATE:{account}:{order}"
            entry = dict(fill_id=raw_id, at=dict(event_ns=ns, sequence=0, raw_id=raw_id,
                source=aggregate_source, received_ns=None), price="10", quantity=100, source=aggregate_source)
            leg = dict(account=account, order_id=order, symbol="X", price="10", quantity=100,
                       filled_ns=ns, source=original, submitted_ns=ns-1, order_type="limit", stop="", limit="10")
            exit = dict(reason="TARGET_5", price="10.50", quantity=100, issues=[],
                at=dict(event_ns=ns+1000000000, sequence=2, raw_id=f"CONTROLLED:print:{index}",
                        source=source("CONTROLLED:tape", "b"*64, "line:2"), received_ns=None),
                price_basis="CONTROLLED modeled exit; NOT market proof", live_exit=None)
            rows.append(dict(version=pub.ADAPTER_VERSION, variant=pub.VARIANT, entry_model=pub.ENTRY_MODEL,
                exit_policy_version=pub.EXIT_POLICY, label=LABEL, history_label=pub.CAPTURE_LABEL,
                clock_label="reconstructed SIP clock; CONTROLLED metadata fixture",
                reference_key=key, identity=identity, order_id=order, recorded_aggregate=leg,
                aggregate_entry=entry, position=dict(first_fill_ts=ns, fills=[entry], vwap="10", filled_qty=100,
                    remainder_cancel_reason="TARGET_5", ignored_fills=[]), exits=[exit], previous_sell=None,
                next_sell=None, decision="MODELED_BASELINE_CLOSED", modeled_gross_pnl="50.00", issues=[],
                assumptions=[pub.ASSUMPTION, pub.TIE_POLICY], same_sip_print_sources=[],
                evidence_limits=["CONTROLLED bytes, not actual173/native qualification"]))
        cases.append(dict(reference_key=key, label=LABEL, history_label=pub.CAPTURE_LABEL,
            variant=pub.VARIANT, entry_model=pub.ENTRY_MODEL, account_rows=rows,
            decision="MODELED_BASELINE_CLOSED", modeled_gross_pnl=str(len(rows) * 50)))
    # Tests exercise declared schema mechanics, NOT actual173 membership proof.
    controlled_digest = hashlib.sha256(json.dumps([c["reference_key"] for c in cases],
        separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    monkeypatch.setattr(pub, "KEYS_SHA", controlled_digest)
    receipt = dict(version=pub.ADAPTER_VERSION, variant=pub.VARIANT, entry_model=pub.ENTRY_MODEL,
        label=LABEL, history_label=pub.CAPTURE_LABEL, exit_policy_version=pub.EXIT_POLICY,
        assumption=pub.ASSUMPTION, same_clock_policy=pub.TIE_POLICY, fac9_input_sha256=pub.FAC9_SHA,
        original_model_reexecuted=False, reviewed_head=binding.reviewed_head, source_pins=absolute_pins,
        case_count=173, account_leg_count=253, cases=cases, buy_source=source(),
        policy_source=source("CONTROLLED:policy", "c"*64),
        day_evidence=[dict(symbol="X", day="2026-10-12", source=source("CONTROLLED:tape", "b"*64),
                           coverage={"CONTROLLED":"supplied fixture only"}, issues=[],
                           all_day_sell_flips=[], all_day_flip_events=[])],
        reference_exclusions=[dict(reference_key=list(key), reasons=["CONTROLLED reference exclusion descriptor"])
                              for key in sorted(pub.EXCLUDED_KEYS)],
        unknown_account_legs=0, known_component_gross_pnl="12650", total_modeled_gross_pnl="12650")
    return receipt, binding


def prepare(receipt, binding):
    raw = pub.encode(receipt)
    ref = source("CONTROLLED:final-receipt", hashlib.sha256(raw).hexdigest())
    return pub.prepare_aggregate_publication(raw, receipt_source=ref, producer=binding)


def test_complete_four_table_metadata_exact253rows173index_and_raw_recovery(operands):
    receipt, binding = operands
    original = deepcopy(receipt)
    prepared = prepare(receipt, binding)
    args = pub.aggregate_writer_arguments(prepared, producer=binding, expected_revision=4)
    assert receipt == original
    assert args["four_table_metadata"] == dict(inputs=1, consumers=1, runs=1, rows=253)
    assert args["capture"]["expected_revision"] == 4
    assert args["capture"]["consumer_id"] == args["checkpoint_consumer_id"]
    recovered = b"".join(base64.b64decode(e["fields"]["bytes_base64"]) for e in args["capture"]["events"])
    assert recovered == prepared.receipt_raw
    assert hashlib.sha256(recovered).hexdigest() == prepared.receipt_sha256
    study = args["publish"]["coverage"]["aggregate_entry_study"]
    assert len(study["selected_cases"]) == 173
    assert sum(c["canonical_rows"] for c in study["selected_cases"]) == 253
    assert len(study["reference_exclusions"]) == 7
    assert study["total_modeled_gross_pnl"] == "12650"
    assert study["global_pnl_qualified"] is False
    assert args["database_saved"] is False and args["readback"] == "UNRUN"
    assert "NOT_ONE_ATOMIC_TRANSACTION" in args["publication_transactions"]


def test_canonical_rows_copy_position_raw_exits_and_exact_new_provenance(operands):
    receipt, binding = operands
    payload = pub.document(prepare(receipt, binding).publication_raw)
    rows = payload["result_rows"]
    assert len({row["outcome_id"] for row in rows}) == 253
    for row, original in zip(rows, [r for c in receipt["cases"] for r in c["account_rows"]]):
        assert row["raw_baseline_result"] == original
        assert row["version"] == VERSION and row["source_adapter_version"] == pub.ADAPTER_VERSION
        assert (row["mode"], row["model"]) == (MODE, MODEL)
        assert row["history_label"] == LABEL and row["partial_fill_timing"] == "UNKNOWN"
        assert row["native_execution_qualified"] is False and row["live_totals_included"] is False
        assert row["fills"] == original["position"]["fills"]
        assert row["vwap"] == original["position"]["vwap"]
        assert row["exits"] == original["exits"]
        assert row["producer_head"] == binding.reviewed_head
        assert row["gross_pnl"] == original["modeled_gross_pnl"]


def unknown_first(receipt):
    row = receipt["cases"][0]["account_rows"][0]
    row.update(decision="UNKNOWN", modeled_gross_pnl=None, issues=["CONTROLLED genuine remaining overlap"])
    receipt["cases"][0].update(decision="UNKNOWN", modeled_gross_pnl=None)
    receipt.update(unknown_account_legs=1, total_modeled_gross_pnl=None, known_component_gross_pnl="12600")


def test_UNKNOWN_filled_leg_never_disappears_or_becomes_zero_fulltotal(operands):
    receipt, binding = operands
    unknown_first(receipt)
    payload = pub.document(prepare(receipt, binding).publication_raw)
    row = payload["result_rows"][0]
    assert row["gross_pnl"] is None and row["outcome_kind"] == "TRADE"
    assert row["recorded_entry_present"] is True and row["filled_qty"] == 100
    assert row["trade_id"] == row["outcome_id"]
    study = payload["coverage"]["aggregate_entry_study"]
    assert study["total_modeled_gross_pnl"] is None
    assert study["unknown_account_legs"] == 1
    assert Decimal(study["known_component_gross_pnl"]) == Decimal("12600")
    assert len(payload["result_rows"]) == 253


@pytest.mark.parametrize("fault", ("pin-missing", "pin-extra", "pin-duplicate", "pin-wrong", "foreign-root", "head", "self-only", "route", "strategy"))
def test_external_source_head_members_routes_cannot_be_self_attested(operands, fault):
    receipt, binding = operands
    if fault == "pin-missing":
        binding = replace(binding, source_pins=binding.source_pins[:-1])
    elif fault == "pin-extra":
        binding = replace(binding, source_pins=(*binding.source_pins, ("foreign.py", "a"*64)))
    elif fault == "pin-duplicate":
        binding = replace(binding, source_pins=(*binding.source_pins, binding.source_pins[0]))
    elif fault == "pin-wrong":
        receipt["source_pins"][next(iter(receipt["source_pins"]))] = "f"*64
    elif fault == "foreign-root":
        binding = replace(binding, root="/CONTROLLED/foreign")
    elif fault == "head":
        binding = replace(binding, reviewed_head="b"*40)
    elif fault == "self-only":
        binding = replace(binding, source_pins=())
    elif fault == "route":
        binding = replace(binding, account_brokers=(("A", "schwab"), ("B", "schwab")))
    else:
        binding = replace(binding, strategy_code="other")
    with pytest.raises(ValueError):
        prepare(receipt, binding)


@pytest.mark.parametrize("fault", ("case-missing", "case-order", "duplicate-order", "leg-missing", "253-lie", "label", "mode-version", "legacy-mode", "price", "position", "exit-qty", "entry-clock", "source", "case-total", "root-total", "known-component", "unknown-zero", "missing-unknown-money", "missing-day", "foreign-exit-tape", "exclusions"))
def test_membership_money_source_or_legacy_mode_corruption_refuses(operands, fault):
    receipt, binding = operands
    row = receipt["cases"][0]["account_rows"][0]
    if fault == "case-missing":
        receipt["cases"].pop()
    elif fault == "case-order":
        receipt["cases"][0], receipt["cases"][1] = receipt["cases"][1], receipt["cases"][0]
    elif fault == "duplicate-order":
        receipt["cases"][0]["account_rows"][1] = deepcopy(row)
    elif fault == "leg-missing":
        receipt["cases"][0]["account_rows"].pop()
    elif fault == "253-lie":
        receipt["account_leg_count"] = 252
    elif fault == "label":
        row["label"] = "LIVE/ACTUAL"
    elif fault == "mode-version":
        row["version"] = "LIVE"
    elif fault == "legacy-mode":
        row["mode"] = "LIVE"
    elif fault == "price":
        row["recorded_aggregate"]["price"] = "11"
    elif fault == "position":
        row["position"]["filled_qty"] = 99
    elif fault == "exit-qty":
        row["exits"][0]["quantity"] = 101
    elif fault == "entry-clock":
        row["recorded_aggregate"]["filled_ns"] -= 1
    elif fault == "source":
        row["recorded_aggregate"]["source"]["sha256"] = "d"*64
    elif fault == "case-total":
        receipt["cases"][0]["modeled_gross_pnl"] = "0"
    elif fault == "root-total":
        receipt["total_modeled_gross_pnl"] = "0"
    elif fault == "known-component":
        receipt["known_component_gross_pnl"] = "0"
    elif fault == "unknown-zero":
        unknown_first(receipt)
        receipt["cases"][0]["account_rows"][0]["modeled_gross_pnl"] = "0"
    elif fault == "missing-unknown-money":
        unknown_first(receipt)
        del receipt["cases"][0]["account_rows"][0]["modeled_gross_pnl"]
    elif fault == "missing-day":
        receipt["day_evidence"] = []
    elif fault == "foreign-exit-tape":
        row["exits"][0]["at"]["source"]["sha256"] = "d"*64
    else:
        receipt["reference_exclusions"][0] = deepcopy(receipt["reference_exclusions"][1])
    with pytest.raises(ValueError):
        prepare(receipt, binding)


@pytest.mark.parametrize("fault", ("receipt-bytes", "publication-bytes", "receipt-source", "revision-bool", "revision-negative"))
def test_writer_metadata_revalidates_immutable_bytes_and_explicit_revision(operands, fault):
    receipt, binding = operands
    prepared = prepare(receipt, binding)
    revision = 0
    if fault == "receipt-bytes":
        prepared = replace(prepared, receipt_raw=prepared.receipt_raw+b" ")
    elif fault == "publication-bytes":
        changed = prepared.publication_raw+b" "
        prepared = replace(prepared, publication_raw=changed, publication_sha256=hashlib.sha256(changed).hexdigest())
    elif fault == "receipt-source":
        changed = pub.document(prepared.receipt_source_raw)
        changed["artifact"] = "CONTROLLED:foreign"
        prepared = replace(prepared, receipt_source_raw=pub.encode(changed))
    else:
        revision = True if fault == "revision-bool" else -1
    with pytest.raises(ValueError):
        pub.aggregate_writer_arguments(prepared, producer=binding, expected_revision=revision)


def test_raw_chunk_offsets_and_bytes_are_exact_and_deterministic(operands, monkeypatch):
    receipt, binding = operands
    monkeypatch.setattr(pub, "CHUNK_BYTES", 4096)
    prepared = prepare(receipt, binding)
    first = pub.aggregate_writer_arguments(prepared, producer=binding, expected_revision=0)
    second = pub.aggregate_writer_arguments(prepared, producer=binding, expected_revision=0)
    assert first == second
    events = first["capture"]["events"]
    assert len(events) > 1
    assert [event["fields"]["offset"] for event in events] == list(range(0, len(prepared.receipt_raw), 4096))
    assert b"".join(base64.b64decode(event["fields"]["bytes_base64"]) for event in events) == prepared.receipt_raw


def test_duplicate_json_keys_and_nonfinite_input_refused():
    with pytest.raises(ValueError, match="duplicate_json"):
        pub.document(b'{"a":1,"a":2}')
    with pytest.raises(ValueError, match="nonfinite_json"):
        pub.document(b'{"a":NaN}')
