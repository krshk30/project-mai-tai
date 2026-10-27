"""CONTROLLED compatibility and byte sealing, not a historical replay."""

from dataclasses import asdict, replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from project_mai_tai.atr_simulation.engine_binding import verify_engine_files
from project_mai_tai.atr_simulation.firstbar import FirstBarMode
from project_mai_tai.atr_simulation.firstbar_binding import verify_firstbar_files
from project_mai_tai.atr_simulation.firstbar_exits import FirstBarPosition
from project_mai_tai.atr_simulation.firstbar_live_baseline import (
    AggregateBaselinePosition, AggregateExitReason, simulate_aggregate_entry,
)
from project_mai_tai.atr_simulation.types import Boundary, Coverage, EntryFact, SimExitReason
from tests.unit.test_atr_firstbar_live_baseline import BASE, END, NS, SOURCE, item, trade
from tests.unit.test_atr_simulation_aggregate_confirmation_exit import short


@pytest.mark.parametrize("name,expected", (
    ("types.py", "90f8dfac9a732c1fd247e5c10f2af18c7d3360ef987f43826ce3b8f708cef4ef"),
    ("firstbar_exits.py", "ee38e47e7188c2dec75cb5f58edad7b897841cdb745ed4be8facaedccffb11d2")))
def test_original_sealed_files_are_exact_not_refreshable_expectations(name, expected):
    root = Path(__file__).resolve().parents[2] / "src/project_mai_tai/atr_simulation"
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected


def test_both_real_legacy_verifiers_accept_unmodified_existing_seals():
    assert verify_engine_files() == "5e3aba0dfea1bce93b50c598e522b8b67902e19e"
    assert verify_firstbar_files() == "e712a99800b9cfd716b406e027845f89567647fe"


def test_sealed_global_enum_is_unchanged_and_confirmation_is_aggregate_local():
    assert tuple(value.value for value in SimExitReason) == (
        "TARGET_5", "STOP_8", "TIME_NO_1PCT_120S", "FLOOR_NEG05_AFTER_2",
        "SELL_FLIP", "SESSION_END", "OPEN", "SAME_AS_LIVE")
    assert tuple(value.value for value in AggregateExitReason) == ("CONFIRMATION_EXIT",)
    assert AggregateBaselinePosition.__module__ == "project_mai_tai.atr_simulation.firstbar_live_baseline"
    assert FirstBarPosition.__module__ == "project_mai_tai.atr_simulation.firstbar_exits"


def position(*, aggregate=True, confirmation=True):
    args = dict(mode=FirstBarMode.FIRSTBAR, sell_flip=None, session_end=Boundary(END, SOURCE),
                coverage=Coverage(BASE, END, True, (), (SOURCE,)))
    if aggregate:
        args["confirmation_exit"] = Boundary(BASE+120*NS, SOURCE, BASE+60*NS) if confirmation else None
    obj = (AggregateBaselinePosition if aggregate else FirstBarPosition)(**args)
    obj.add_fill(EntryFact("CONTROLLED:aggregate-fact", trade(100, "1.56").key,
                          Decimal("1.56"), 196, SOURCE))
    return obj


def test_original_firstbar_path_stays_target_not_aggregate_confirmation():
    obj = position(aggregate=False)
    assert not hasattr(obj, "confirmation_exit")
    obj.on_trade(trade(120, "1.65"))
    exits = obj.finish()
    assert len(exits) == 1 and exits[0].reason is SimExitReason.TARGET_5
    assert exits[0].price == Decimal("1.64") and exits[0].quantity == 196


def test_original_constructor_does_not_acquire_optional_aggregate_policy():
    with pytest.raises(TypeError, match="confirmation_exit"):
        FirstBarPosition(mode=FirstBarMode.FIRSTBAR, sell_flip=None, session_end=Boundary(END, SOURCE),
            coverage=Coverage(BASE, END, True, (), (SOURCE,)), confirmation_exit=Boundary(BASE+120*NS, SOURCE))


def test_aggregate_reason_serializes_without_adding_to_or_coercing_through_global_enum():
    result = simulate_aggregate_entry(short(item((trade(120, "1.55"),))))
    exit = result.exits[0]
    assert exit.reason is AggregateExitReason.CONFIRMATION_EXIT
    assert exit.reason is not SimExitReason.OPEN
    assert exit.reason.value == "CONFIRMATION_EXIT" and str(exit.reason.value) == "CONFIRMATION_EXIT"
    serialized = json.loads(json.dumps(asdict(result), default=str))
    assert serialized["exits"][0]["reason"] == "CONFIRMATION_EXIT"
    assert serialized["position"]["remainder_cancel_reason"] == "CONFIRMATION_EXIT"
    assert serialized["modeled_gross_pnl"] == "-1.96"
    assert serialized["version"] == "ATRSIM_RECORDED_AGGREGATE_ENTRY_BASELINE_V3_CONFIRMATION_EXIT"


def test_confirmation_handler_is_exactly_once_and_later_print_never_reopens():
    obj = position()
    obj.on_trade(trade(119, "1.55"))
    obj.on_trade(trade(120, "1.55"))
    obj.on_trade(trade(121, "1.70"))
    exits = obj.finish()
    assert len(exits) == 1 and exits[0].reason is AggregateExitReason.CONFIRMATION_EXIT
    assert exits[0].at.event_ns == BASE+120*NS and exits[0].price == Decimal("1.55")
    assert obj.can_fill is False
    assert not obj.add_fill(EntryFact("CONTROLLED:late-fact", trade(122, "1.56").key,
                                    Decimal("1.56"), 100, SOURCE))
    assert obj.receipt().filled_qty == 196


def test_local_handler_retains_causal_cursor_and_does_not_double_advance_parent():
    obj = position()
    obj.on_trade(trade(120, "1.55", seq=1))
    obj.on_trade(trade(120, "1.55", seq=2))
    with pytest.raises(ValueError, match="strict_causal_order"):
        obj.on_trade(trade(120, "1.55", seq=2))
    assert obj.finish()[0].at.sequence == 1


@pytest.mark.parametrize("size", (99, 0))
def test_local_handler_keeps_ineligible_print_nonexecuting(size):
    obj = position()
    obj.on_trade(trade(120, "1.55", size=size))
    assert obj.exit is None and not obj.can_fill
    obj.on_trade(trade(121, "1.54"))
    assert obj.finish()[0].at.event_ns == BASE+121*NS
    assert obj.finish()[0].price == Decimal("1.54")


def test_local_handler_keeps_unknown_execution_and_open_identity():
    obj = position()
    obj.on_trade(replace(trade(120, "1.55"), execution=None))
    assert obj.can_fill is False and "UNKNOWN_FIRSTBAR_EXIT_EXECUTION_ELIGIBILITY" in obj.issues
    assert obj.finish()[0].reason is SimExitReason.OPEN
    assert obj.finish()[0].price is None


@pytest.mark.parametrize("field,value,message", (
    ("size", -1, "nonnegative_integer_size"), ("size", True, "nonnegative_integer_size"),
    ("price", Decimal("0"), "positive_finite_price")))
def test_local_handler_preserves_existing_input_validation(field, value, message):
    obj = position()
    with pytest.raises(ValueError, match=message):
        obj.on_trade(replace(trade(120, "1.55"), **{field:value}))


def test_aggregate_without_confirmation_still_returns_original_target_enum_and_price():
    obj = position(confirmation=False)
    obj.on_trade(trade(120, "1.65"))
    assert obj.finish()[0].reason is SimExitReason.TARGET_5
    assert obj.finish()[0].price == Decimal("1.64")
