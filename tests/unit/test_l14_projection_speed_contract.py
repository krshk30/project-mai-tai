"""NEW controlled source-successor checks; UNRUN. No retained model or PG."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import json
from uuid import UUID

import pytest

from project_mai_tai.atr_simulation.completed_ui import completed_comparison_view, render_completed_section
from project_mai_tai.atr_simulation.engine_binding import bound_simulation_view
from project_mai_tai.atr_simulation.repository import json_value
from project_mai_tai.services import atr_simulation_legacy_routes as routes
from tests.unit.test_atr_simulation_legacy_visibility import DAY, SOURCE, Reader, live_row, request, stored_page


def large_controlled_page():
    page = stored_page()
    row = live_row()
    row["body_observations"] = [{"sequence": number, "price_bin": 10000 + number,
        "source": "CONTROLLED_NOT_NATIVE", "issues": ["UNKNOWN_CAPTURE"]} for number in range(4096)]
    page["rows"] = [row]
    return page


def test_api_skips_only_unused_html_and_preserves_entire_raw_tree(monkeypatch):
    page = large_controlled_page()
    before = deepcopy(page)
    def unused(*args, **kwargs):
        raise AssertionError("API must not render unused HTML")
    monkeypatch.setattr(routes, "render_completed_section", unused)
    monkeypatch.setattr(routes, "render_simulation_section", unused)
    response = request(Reader(page), "/atr-simulation/api?source_id=" + SOURCE + "&day=" + DAY.isoformat())
    assert response.status_code == 200
    view = response.json()
    assert view["rows"] == before["rows"]
    assert len(view["rows"][0]["body_observations"]) == 4096
    assert page == before and view["live_totals_included"] is False


def test_api_still_checks_duplicate_source_identity_leg_and_fails_unknown():
    page = large_controlled_page()
    page["rows"].append(deepcopy(page["rows"][0]))
    reader = Reader(page)
    response = request(reader, "/atr-simulation/api?source_id=" + SOURCE + "&day=" + DAY.isoformat())
    assert response.json()["read_status"] == "UNAVAILABLE"
    assert response.json()["rows"] == [] and len(reader.calls) == 1


@pytest.mark.parametrize("damage", ["nonfinite_metadata", "other_world", "other_source", "missing_account"])
def test_fast_api_keeps_all_original_data_boundaries(damage):
    page = large_controlled_page()
    if damage == "nonfinite_metadata":
        page["rows"][0]["body_observations"][-1]["bad"] = float("nan")
    elif damage == "other_world":
        page["rows"][0].update(mode="FIRSTBAR", model="MODELED_STOP_AT_OBSERVED_PRINT_VOLUME")
    elif damage == "other_source":
        page["run"]["source_id"] = "CONTROLLED_FOREIGN"
    else:
        del page["rows"][0]["identity"]["account_id"]
    response = request(Reader(page), "/atr-simulation/api?source_id=" + SOURCE + "&day=" + DAY.isoformat())
    assert response.json()["read_status"] == "UNAVAILABLE" and response.json()["rows"] == []


def test_borrowed_completion_matches_detached_bytes_and_never_mutates_owned_view():
    page = large_controlled_page()
    view = bound_simulation_view(page)
    view["read_status"] = "AVAILABLE"
    before = deepcopy(view)
    detached = completed_comparison_view(view)
    borrowed = completed_comparison_view(view, copy_receipts=False)
    assert borrowed == detached
    assert render_completed_section(borrowed) == render_completed_section(detached)
    assert borrowed["rows"][0]["legs"][0]["receipt"] is view["rows"][0]
    assert detached["rows"][0]["legs"][0]["receipt"] is not view["rows"][0]
    assert view == before


def test_default_completed_public_api_still_owns_detached_receipt():
    view = bound_simulation_view(large_controlled_page())
    view["read_status"] = "AVAILABLE"
    completed = completed_comparison_view(view)
    view["rows"][0]["body_observations"][0]["source"] = "CONTROLLED_MUTATION"
    assert completed["rows"][0]["legs"][0]["receipt"]["body_observations"][0]["source"] == "CONTROLLED_NOT_NATIVE"


@pytest.mark.parametrize("value", [None, True, False, 123, 1.25, "CONTROLLED_RAW"])
def test_exact_json_primitive_has_identical_value(value):
    assert json_value(value) is value


def test_nonprimitive_canonical_conversions_and_hash_encoding_stay_exact():
    class Policy(str, Enum):
        VALUE = "CONTROLLED_POLICY"
    @dataclass
    class Fact:
        amount: Decimal
    value = {"enum": Policy.VALUE, "decimal": Decimal("1.2300"),
        "day": date(2026, 10, 23), "stamp": datetime(2026, 10, 23, tzinfo=timezone.utc),
        "uuid": UUID(int=1), "fact": Fact(Decimal("2.500")), "tuple": (1, "two")}
    expected = {"enum": "CONTROLLED_POLICY", "decimal": "1.2300", "day": "2026-10-23",
        "stamp": "2026-10-23T00:00:00+00:00", "uuid": "00000000-0000-0000-0000-000000000001",
        "fact": {"amount": "2.500"}, "tuple": [1, "two"]}
    assert json_value(value) == expected
    assert json.dumps(json_value(value), sort_keys=True, separators=(",", ":")) == json.dumps(
        expected, sort_keys=True, separators=(",", ":"))
