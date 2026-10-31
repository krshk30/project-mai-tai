"""Main's ELPW qty key proof; all quantities below are controlled, not native amounts."""

from copy import deepcopy

import pytest

from project_mai_tai.brokerdecides1_native import webull_book


def normalize(holding):
    return webull_book(
        account="live:webull", native_account="controlled-account",
        position_pages=[{"holdings": [holding], "has_next": False}],
        positions_request_account="controlled-account",
        order_pages=[{"orders": [], "hasNext": False, "pageSize": 0}],
        order_request_account="controlled-account",
        wire_started_ms=1000, wire_completed_ms=1100,
        scope="all_native_working_including_previous_sessions", complete=True,
    )


@pytest.mark.parametrize("field", ["quantity", "qty", "position", "shares"])
@pytest.mark.parametrize("value", ["3", "-3", "0.125"])
def test_controlled_quantity_alias_holds_gross_exposure(field, value):
    row = {"symbol": "CONTROLLED", field: value}
    before = deepcopy(row)
    book = normalize(row)
    assert book.complete and book.positions == frozenset({"CONTROLLED"})
    assert book.working == frozenset() and row == before


@pytest.mark.parametrize("field", ["quantity", "qty", "position", "shares"])
def test_controlled_quantity_zero_is_not_held(field):
    book = normalize({"symbol": "CONTROLLED", field: "0"})
    assert book.complete and book.positions == frozenset()


@pytest.mark.parametrize("row", [
    {"quantity": 0, "qty": 3, "position": 4, "shares": 5},
    {"quantity": None, "qty": 0, "position": 4, "shares": 5},
    {"quantity": "", "qty": None, "position": 0, "shares": 5},
    {"quantity": None, "qty": "", "position": None, "shares": 0},
])
def test_controlled_alias_precedence_preserves_first_zero(row):
    assert normalize({"symbol": "CONTROLLED", **row}).positions == frozenset()


@pytest.mark.parametrize("row", [
    {}, {"quantity": None}, {"quantity": "", "qty": None, "position": "", "shares": None},
])
def test_missing_all_quantity_evidence_is_unknown(row):
    with pytest.raises(ValueError, match="^native_quantity_UNKNOWN$"):
        normalize({"symbol": "CONTROLLED", **row})


@pytest.mark.parametrize("field", ["quantity", "qty", "position", "shares"])
@pytest.mark.parametrize("value", [True, False, "not-a-number", "NaN", "Infinity", "-Infinity", {}, []])
def test_strict_number_rejects_bad_alias_without_inventing_flat(field, value):
    with pytest.raises(ValueError, match="^native_quantity_UNKNOWN$"):
        normalize({"symbol": "CONTROLLED", field: value})


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_primary_is_unknown_even_when_secondary_says_zero(value):
    # A parseable but non-finite primary must not fall through to false flat.
    with pytest.raises(ValueError, match="^native_quantity_UNKNOWN$"):
        normalize({"symbol": "CONTROLLED", "quantity": value, "qty": 0})


@pytest.mark.parametrize("row", [
    {"quantity": "not-a-number", "qty": "3"},
    {"quantity": None, "qty": "", "position": "bad", "shares": "3"},
    {"quantity": "", "qty": "3", "position": "bad", "shares": "NaN"},
    {"quantity": "3", "qty": "bad", "position": "NaN", "shares": None},
])
def test_adapter_precedence_skips_empty_or_parse_invalid_neighbors(row):
    assert normalize({"symbol": "CONTROLLED", **row}).positions == frozenset({"CONTROLLED"})


@pytest.mark.parametrize("invalid", [True, False, "bad", {}, []])
def test_adapter_parse_invalid_primary_does_not_override_later_explicit_zero(invalid):
    assert normalize({"symbol": "CONTROLLED", "quantity": invalid, "qty": "0"}).positions == frozenset()


def test_main_proven_elpw_qty_key_with_controlled_amount_and_negative_shapes():
    # Main ONE read10:57:05Z: qty PRESENT, quantity/position/shares ABSENT.
    # Only the key is real evidence. "7" and "0" are controlled test values.
    row = {"symbol": "ELPW", "qty": "7"}
    before = deepcopy(row)
    held = normalize(row)
    assert held.complete and held.positions == frozenset({"ELPW"})
    assert row == before
    flat = normalize({"symbol": "ELPW", "qty": "0"})
    assert flat.complete and flat.positions == frozenset()
    with pytest.raises(ValueError, match="^native_quantity_UNKNOWN$"):
        normalize({"symbol": "ELPW"})
