"""CONTROLLED aggregate-entry fixtures; no original study/reference execution."""

from dataclasses import asdict, replace
from decimal import Decimal
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_mai_tai.atr_simulation.firstbar_live_baseline import (
    AGGREGATE_ASSUMPTION, BASELINE_LABEL, BASELINE_VARIANT, BaselineBarClose, BaselineDay,
    AggregateBaselineInput, prepare_baseline_day, require_fresh_sell_transition, sell_window,
    simulate_aggregate_cohort, simulate_aggregate_entry,
)
from project_mai_tai.atr_simulation.firstbar_study import StudyBuyLeg, StudyTapePacket, _boundary
from project_mai_tai.atr_simulation.types import (
    Boundary, Coverage, EventKey, EvidenceKind, FlipIdentity, SimExitReason, SourceRef,
)
from tests.support.atr_simulation_eligibility import POLICY, controlled_trade


SOURCE = SourceRef("CONTROLLED:aggregate-baseline", "a" * 64, "line:1", EvidenceKind.CONTROLLED)
DAY = "2026-10-23"
BASE = _boundary(DAY, 4)
END = _boundary(DAY, 20)
NS = 1_000_000_000
RUNNER_PATH = Path(__file__).resolve().parents[2] / "tools/atr_firstbar_live_baseline.py"
SPEC = importlib.util.spec_from_file_location("controlled_baseline_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def trade(seconds, price, *, seq=1, size=100, conditions="14"):
    event = EventKey(BASE + seconds * NS, seq, f"CONTROLLED:{seconds}:{seq}", SOURCE)
    return controlled_trade(event, "X", Decimal(price), size, conditions=conditions)


def item(trades=(), *, account="A", order="order1", filled=100, price="1.56", quantity=196,
         sells=(50, 500)):
    leg = StudyBuyLeg(account, order, "STOP_LIMIT", "X", BASE + 90 * NS,
        BASE + filled * NS, Decimal(price), quantity, price, price, SOURCE)
    day = BaselineDay("X", DAY, tuple(trades), tuple(Boundary(BASE + at * NS, SOURCE) for at in sells),
        Boundary(END, SOURCE), Coverage(BASE, END, True, (), (SOURCE,)), (), SOURCE,
        tuple(sorted((("BUY", Boundary(BASE + 75 * NS, SOURCE)),
                      *(("SELL", Boundary(BASE + at * NS, SOURCE)) for at in sells)),
                     key=lambda pair: pair[1].available_ns)),
        (BaselineBarClose(BASE + filled // 60 * 60 * NS, BASE + (filled // 60 + 1) * 60 * NS,
            Decimal(price), Decimal(price) * Decimal("0.95"), "long", SOURCE),))
    identity = FlipIdentity(DAY, "X", "webull", account, "CONTROLLED:strategy", order, order)
    return AggregateBaselineInput(("X", DAY, "04:01:40"), identity, leg, day, AGGREGATE_ASSUMPTION)


def test_actual_aggregate_price_not_firstbar_proxy_uses_same_ceiling_policy():
    result = simulate_aggregate_entry(item((trade(101, "1.638"), trade(102, "1.64"))))
    assert result.label == BASELINE_LABEL
    assert result.label == "RECORDED AGGREGATE-ENTRY REPLAY \u00b7 partial timing UNKNOWN"
    assert result.variant == BASELINE_VARIANT
    assert result.position.vwap == Decimal("1.56")
    assert result.position.filled_qty == 196
    assert result.exits[0].price == Decimal("1.64")
    assert result.exits[0].reason is SimExitReason.TARGET_5
    assert result.modeled_gross_pnl == Decimal("15.68")
    assert result.decision == "MODELED_BASELINE_CLOSED"
    assert result.aggregate_entry.source.kind is EvidenceKind.DERIVED
    assert "AGGREGATE" in result.aggregate_entry.at.raw_id


def test_same_policy_stop_uses_current_first_eligible_print_not_threshold():
    result = simulate_aggregate_entry(item((trade(101, "3.90"), trade(102, "3.87")),
                                           price="4.23", quantity=213))
    assert result.exits[0].reason is SimExitReason.STOP_8
    assert result.exits[0].price == Decimal("3.87")
    assert result.modeled_gross_pnl == Decimal("-76.68")


def test_no_x3_and_prior_SELL_does_not_cancel_actual_later_entry():
    result = simulate_aggregate_entry(item((trade(221, "1.56"), trade(500, "1.55"))))
    assert result.previous_sell.available_ns == BASE + 50 * NS
    assert result.next_sell.available_ns == BASE + 500 * NS
    assert result.exits[0].reason is SimExitReason.SELL_FLIP
    assert result.exits[0].at.event_ns == BASE + 500 * NS
    assert result.exits[0].price == Decimal("1.55")
    assert result.position.filled_qty == 196


def test_same_SIP_prints_all_omitted_not_assigned_fabricated_fill_sequence():
    result = simulate_aggregate_entry(item((trade(100, "1.64", seq=9), trade(100, "1.70", seq=10),
                                           trade(101, "1.56"), trade(500, "1.55"))))
    assert len(result.same_sip_print_sources) == 2
    assert result.exits[0].reason is SimExitReason.SELL_FLIP
    assert result.aggregate_entry.at.sequence == 0
    assert result.aggregate_entry.at.source.artifact == SOURCE.artifact
    assert "UNKNOWN" in result.evidence_limits[0]


def test_equal_SELL_available_clock_keeps_recorded_entry_but_economics_unknown():
    result = simulate_aggregate_entry(item((trade(101, "1.64"),), sells=(100, 500)))
    assert result.modeled_gross_pnl is None
    assert "UNKNOWN_AGGREGATE_ENTRY_ON_SELL_BOUNDARY" in result.issues
    assert result.position.filled_qty == 196


def test_recorded_entry_after_prior_SELL_is_external_fact_not_new_BUY_permission():
    value = item((trade(101, "1.64"),))
    value = replace(value, day=replace(value.day, all_flips=tuple(
        ("SELL", event) for event in value.day.sell_flips)))
    result = simulate_aggregate_entry(value)
    assert result.modeled_gross_pnl == Decimal("15.68")
    assert result.issues == ()
    assert result.previous_sell.available_ns == BASE + 50 * NS
    assert result.next_sell.available_ns == BASE + 500 * NS
    assert result.exits[0].reason is SimExitReason.TARGET_5
    assert result.exits[0].at.event_ns == BASE + 101 * NS
    assert result.recorded_aggregate.quantity == 196


def test_intrabar_recorded_entry_does_not_wait_for_future_closed_BUY_to_admit():
    value = item((trade(101, "1.64"),))
    flips = (("SELL", value.day.sell_flips[0]),
             ("BUY", Boundary(BASE + 120 * NS, SOURCE, BASE + 60 * NS)),
             ("SELL", value.day.sell_flips[1]))
    result = simulate_aggregate_entry(replace(value, day=replace(value.day, all_flips=flips)))
    assert result.position.first_fill_ts == BASE + 100 * NS
    assert result.exits[0].at.event_ns == BASE + 101 * NS
    assert result.exits[0].at.event_ns < flips[1][1].available_ns
    assert result.modeled_gross_pnl == Decimal("15.68")
    assert result.previous_sell == flips[0][1]
    assert result.next_sell == flips[2][1]
    assert result.issues == ()


def test_intrabar_actual_entry_prior_SELL_never_exits_until_fresh_SELL_transition():
    value = item((trade(101, "1.56"), trade(120, "1.56"), trade(499, "1.56"), trade(500, "1.55")))
    flips = (("SELL", value.day.sell_flips[0]),
             ("BUY", Boundary(BASE + 120 * NS, SOURCE, BASE + 60 * NS)),
             ("SELL", value.day.sell_flips[1]))
    result = simulate_aggregate_entry(replace(value, day=replace(value.day, all_flips=flips)))
    assert result.position.first_fill_ts == BASE + 100 * NS
    assert result.previous_sell == flips[0][1]
    assert result.next_sell == flips[2][1]
    assert len(result.exits) == 1
    assert result.exits[0].reason is SimExitReason.SELL_FLIP
    assert result.exits[0].at.event_ns == BASE + 500 * NS
    assert result.exits[0].at.event_ns > flips[1][1].available_ns
    assert result.exits[0].price == Decimal("1.55")
    assert result.modeled_gross_pnl == Decimal("-1.96")
    assert result.issues == ()


@pytest.mark.parametrize("bad_selection", ("prior", "equal", "foreign", "BUY"))
def test_selected_SELL_guard_refuses_stale_equal_foreign_or_BUY_event(bad_selection):
    value = item()
    day = value.day
    if bad_selection == "prior":
        selected = day.sell_flips[0]
    elif bad_selection == "equal":
        selected = Boundary(value.leg.filled_ns, SOURCE)
    elif bad_selection == "foreign":
        selected = Boundary(BASE + 600 * NS, SOURCE)
    else:
        selected = Boundary(BASE + 120 * NS, SOURCE, BASE + 60 * NS)
        day = replace(day, all_flips=(("SELL", day.sell_flips[0]),
                                      ("BUY", selected), ("SELL", day.sell_flips[1])))
    with pytest.raises(ValueError, match="selected_SELL_must_be_new_transition"):
        require_fresh_sell_transition(day, value.leg.filled_ns, selected)


def test_no_future_SELL_does_not_invent_boundary_or_last_print_exit():
    result = simulate_aggregate_entry(item((trade(101, "1.56"),), sells=(50,)))
    assert result.next_sell is None
    assert result.exits[0].reason is SimExitReason.OPEN
    assert result.exits[0].price is None
    assert result.modeled_gross_pnl is None


@pytest.mark.parametrize("size,conditions", ((99, "14"), (100, "999999")))
def test_ineligible_print_cannot_fill_model_exit(size, conditions):
    result = simulate_aggregate_entry(item((trade(101, "1.64", size=size, conditions=conditions),
                                           trade(500, "1.55"))))
    assert result.exits[0].reason is SimExitReason.SELL_FLIP


def test_missing_execution_evidence_never_certifies_later_economics():
    unknown = replace(trade(101, "1.56"), execution=None)
    result = simulate_aggregate_entry(item((unknown, trade(102, "1.64"))))
    assert result.modeled_gross_pnl is None
    assert result.decision == "UNKNOWN"
    assert "UNKNOWN_FIRSTBAR_EXIT_EXECUTION_ELIGIBILITY" in result.issues


def test_incomplete_envelope_not_hidden_by_later_target():
    value = item((trade(101, "1.64"),))
    value = replace(value, day=replace(value.day, issues=("UNKNOWN_SUPPLIED_0400_2000_ENVELOPE",)))
    result = simulate_aggregate_entry(value)
    assert result.modeled_gross_pnl is None
    assert result.position.filled_qty == 196


def test_duplicate_account_order_refused_not_double_exposure():
    value = item((trade(101, "1.64"),))
    with pytest.raises(ValueError, match="duplicate_actual_account_order"):
        simulate_aggregate_cohort((value, value))


def test_two_accounts_keep_observed_entries_without_fake_print_volume_entry():
    tape = (trade(101, "1.64", size=100),)
    results = simulate_aggregate_cohort((item(tape, account="A"), item(tape, account="B")))
    assert [r.position.filled_qty for r in results] == [196, 196]
    assert [r.modeled_gross_pnl for r in results] == [Decimal("15.68"), Decimal("15.68")]
    assert all("NOT_PRINT_VOLUME" in r.assumptions[2] for r in results)


def test_same_account_overlap_is_informational_and_closed_transition_has_no_flag():
    tape = (trade(101, "1.56"), trade(110, "1.64"), trade(130, "1.64"))
    results = simulate_aggregate_cohort((item(tape), item(tape, order="order2", filled=105)))
    assert all(r.modeled_gross_pnl == Decimal("15.68") for r in results)
    assert all(r.flags == ("SAME_ACCOUNT_OVERLAP",) for r in results)
    assert results[0].same_account_overlap_order_ids == ("order2",)
    assert results[1].same_account_overlap_order_ids == ("order1",)
    assert all(r.issues == () for r in results)
    later = simulate_aggregate_cohort((item(tape), item(tape, order="order2", filled=120)))
    assert all(r.modeled_gross_pnl == Decimal("15.68") for r in later)
    assert all(r.flags == () and r.same_account_overlap_order_ids == () for r in later)


def test_assumption_must_be_explicit_not_default_permission():
    with pytest.raises(ValueError, match="explicit_approved_aggregate"):
        simulate_aggregate_entry(replace(item(), assumption=""))


@pytest.mark.parametrize("field", ("symbol", "account_id", "trading_day", "broker"))
def test_foreign_day_account_symbol_route_refused(field):
    value = item()
    wrong = {"symbol":"Y", "account_id":"other", "trading_day":"2026-10-22", "broker":"guess"}[field]
    with pytest.raises(ValueError, match="exact_actual_leg"):
        simulate_aggregate_entry(replace(value, identity=replace(value.identity, **{field:wrong})))


def test_sell_selection_uses_whole_day_availability_not_bar_start():
    value = item(sells=(50, 500))
    previous, following, equal = sell_window(value.day, BASE + 499 * NS)
    assert previous.available_ns == BASE + 50 * NS
    assert following.available_ns == BASE + 500 * NS
    assert not equal


def test_full_day_preparation_records_every_actual_oracle_SELL_close():
    # Controlled bars stress actual pinned oracle; no fixed expected trade/PnL.
    rows = []
    for minute in range(80):
        price = Decimal("10") + (Decimal(minute % 20) if minute % 40 < 20 else Decimal(20 - minute % 20)) / 10
        rows.append(f"{BASE // NS + minute * 60}|{price}|100|{{14}}\n")
    raw = "".join(rows).encode()
    src = replace(SOURCE, sha256=hashlib.sha256(raw).hexdigest())
    coverage = Coverage(BASE, END, True, (), (src,))
    day = prepare_baseline_day(StudyTapePacket("X", DAY, raw, src, coverage), POLICY)
    assert not day.issues
    assert len(day.trades) == 80
    assert day.sell_flips
    assert all(event.available_ns == event.bar_start_ns + 60 * NS for event in day.sell_flips)
    assert all("oracle-SELL-bar" in event.source.pointer for event in day.sell_flips)
    assert len({event.available_ns for event in day.sell_flips}) == len(day.sell_flips)


def test_literal_original_leg_binding_ignores_simulated_entry_fills():
    value = item()
    original = runner.plain(asdict(value.leg))
    receipt = {"cases":[dict(reference_key=list(value.reference_key), leg_bindings=[original],
                             results=[{"fills":[{"price":"999"}]}])]}
    result = runner.bind_actual_legs((value.reference_key,), receipt, (value.leg,))
    assert result[value.reference_key][0].price == Decimal("1.56")
    assert result[value.reference_key][0].order_id == "order1"


@pytest.mark.parametrize("change", ("price", "quantity", "source", "duplicate", "missing", "seed"))
def test_original_leg_provenance_join_mismatch_refuses(change):
    value = item()
    original = runner.plain(asdict(value.leg))
    receipt = {"cases":[dict(reference_key=list(value.reference_key), leg_bindings=[original])]}
    parsed = (value.leg,)
    if change == "duplicate":
        receipt["cases"][0]["leg_bindings"].append(original)
    elif change == "missing":
        parsed = ()
    elif change == "seed":
        receipt["cases"][0]["reference_key"][0] = "Y"
    else:
        original[change] = "999" if change != "source" else {}
    with pytest.raises(ValueError):
        runner.bind_actual_legs((value.reference_key,), receipt, parsed)


def test_explicit_pin_generation_refuses_missing_or_mutated_source(tmp_path):
    pins = {}
    for name in runner.REQUIRED_PINS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("# CONTROLLED:" + name).encode())
        pins[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = dict(reviewed_head="a" * 40, source_pins=pins)
    assert len(runner.verify_source_manifest(manifest, tmp_path)) == len(runner.REQUIRED_PINS)
    (tmp_path / "src/project_mai_tai/atr_simulation/firstbar_exits.py").write_bytes(b"changed")
    with pytest.raises(ValueError, match="exact_input_bytes"):
        runner.verify_source_manifest(manifest, tmp_path)
    del pins["src/project_mai_tai/atr_simulation/firstbar_exits.py"]
    with pytest.raises(ValueError, match="explicit_exact_baseline"):
        runner.verify_source_manifest(manifest, tmp_path)


def test_missing_day_BUY_SELL_sequence_refuses_instead_of_assuming_reentry():
    value = item()
    with pytest.raises(ValueError, match="complete_consistent_day"):
        simulate_aggregate_entry(replace(value, day=replace(value.day, all_flips=())))


def test_duplicate_json_input_keys_refuse():
    with pytest.raises(ValueError, match="duplicate_input_key"):
        runner.json_data(b'{"a":1,"a":2}')


def test_missing_explicit_cli_assumption_cannot_start_replay():
    with pytest.raises(SystemExit) as error:
        runner.main([])
    assert error.value.code == 2


def test_foreign_import_root_refuses_even_with_reviewed_disk_pins(monkeypatch):
    monkeypatch.setattr(runner, "REQUIRED_PINS", {"src/project_mai_tai/atr_simulation/firstbar_live_baseline.py"})
    monkeypatch.setitem(runner.sys.modules, "project_mai_tai.atr_simulation.firstbar_live_baseline",
                        SimpleNamespace(__file__="/private/tmp/foreign-baseline.py"))
    with pytest.raises(ValueError, match="actual_loaded_baseline_module"):
        runner.verify_loaded_sources(Path(__file__).resolve().parents[2])


def test_serialized_baseline_is_explicit_new_variant_not_existing_native_or_print_entry_mode():
    result = simulate_aggregate_entry(item((trade(101, "1.64"),)))
    serialized = runner.plain(asdict(result))
    assert serialized["label"] == "RECORDED AGGREGATE-ENTRY REPLAY \u00b7 partial timing UNKNOWN"
    assert serialized["version"] == "ATRSIM_RECORDED_AGGREGATE_ENTRY_BASELINE_V3_CONFIRMATION_EXIT"
    assert serialized["variant"] == "ACTUAL_AGGREGATE_ENTRY_SAME_EXIT_POLICY"
    assert serialized["entry_model"] == "RECORDED_ORDER_AGGREGATE_AT_RECORDED_FILLED_NS"
    assert "mode" not in serialized and "model" not in serialized
    assert serialized["position"]["filled_qty"] == 196
    assert serialized["modeled_gross_pnl"] == "15.68"
