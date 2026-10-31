"""Owned projection contracts; no retained PG, provider or timing proof."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import sys
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import JSONResponse
import pytest

from project_mai_tai.atr_simulation import engine_binding, read_model
from project_mai_tai.atr_simulation.ui_schema import (
    _DetachedSimulationSnapshot, detached_simulation_snapshot,
)
from project_mai_tai.services import atr_firstbar_routes as firstbar
from project_mai_tai.services import atr_simulation_legacy_routes as legacy
from tests.unit.test_atr_simulation_legacy_visibility import DAY, SOURCE, live_row, stored_page


def page():
    value = stored_page()
    row = live_row()
    row["body_observations"] = [
        {"at": index, "prices": ["1.00", "1.10"], "volume": 100 + index}
        for index in range(200)]
    row["extra"] = {"decimal": Decimal("1.2300"), "day": DAY,
        "stamp": datetime(2026, 10, 23, 20, tzinfo=timezone.utc),
        "uuid": UUID("12345678-1234-5678-1234-567812345678")}
    value["rows"] = [row]
    return value


def test_owned_projection_keeps_full_raw_api_bytes_and_detaches_every_nested_row():
    source = page()
    before = deepcopy(source)
    expected = engine_binding.bound_simulation_view(source)
    expected.update(read_status="AVAILABLE", read_issues=[], workers_truncated=False)
    view, html = legacy._project(source, SOURCE, DAY, DAY, render_html=False)
    assert html is None and view == expected
    assert view.body == JSONResponse(expected).body
    assert len(view["rows"][0]["body_observations"]) == 200
    assert view["rows"][0]["extra"]["stamp"] == "2026-10-23T20:00:00+00:00"
    view["rows"][0]["body_observations"][0]["prices"].append("foreign")
    assert source == before


def test_public_projection_default_still_returns_an_independent_normalized_tree():
    source = page()
    before = deepcopy(source)
    view = read_model.simulation_view(source)
    view["rows"][0]["body_observations"].clear()
    assert source == before


def test_owned_snapshot_cannot_be_applied_to_another_stored_tree():
    owned = detached_simulation_snapshot(page())
    with pytest.raises(ValueError, match="owned_validated"):
        read_model.simulation_view(deepcopy(owned.snapshot), _detached_snapshot=owned)
    with pytest.raises(ValueError, match="owned_validated"):
        read_model.simulation_view(owned.snapshot, _detached_snapshot=True)


@pytest.mark.parametrize("kind", ["identity", "nonfinite", "missing"])
def test_direct_snapshot_constructor_cannot_bypass_validation(kind):
    source = page()
    if kind == "identity":
        source["rows"][0]["identity"]["symbol"] = Decimal("123")
    elif kind == "nonfinite":
        source["rows"][0]["extra"]["invalid"] = float("inf")
    else:
        del source["rows"][0]["identity"]
    with pytest.raises((ValueError, KeyError)):
        _DetachedSimulationSnapshot(source)


def test_direct_constructor_detaches_and_foreign_tree_still_refuses():
    source = page()
    owned = _DetachedSimulationSnapshot(source)
    source["rows"][0]["body_observations"].clear()
    assert len(owned.snapshot["rows"][0]["body_observations"]) == 200
    with pytest.raises(ValueError, match="owned_validated"):
        read_model.simulation_view(source, _detached_snapshot=owned)


def test_nonstring_and_literal_metadata_keys_preserve_public_api_bytes():
    source = page()
    source["rows"][0]["extra"]["keys"] = {
        None: "none fact", "null": "literal null fact", "None": "literal None fact",
        True: "true fact", "true": "literal true fact", "True": "literal True fact",
        2: "integer fact", "2.0": "literal float fact", 3.5: "float fact",
    }
    expected = engine_binding.bound_simulation_view(source)
    expected.update(read_status="AVAILABLE", read_issues=[], workers_truncated=False)
    view, _ = legacy._project(source, SOURCE, DAY, DAY, render_html=False)
    assert view == expected and view.body == JSONResponse(expected).body
    assert view["rows"][0]["extra"]["keys"] == {
        "None": "literal None fact", "null": "literal null fact",
        "True": "literal True fact", "true": "literal true fact",
        "2": "integer fact", "2.0": "literal float fact", "3.5": "float fact",
    }


def test_row_type_checks_happen_before_decimal_identity_can_become_a_string():
    source = page()
    source["rows"][0]["identity"]["symbol"] = Decimal("123")
    view, _ = legacy._project_or_unknown(source, SOURCE, DAY, DAY)
    assert view["read_status"] == "UNAVAILABLE"
    assert view["read_issues"] == ["LEGACY_SIMULATION_MALFORMED_VIEW"]
    assert view["rows"] == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), Decimal("NaN"), object()])
def test_nested_invalid_metadata_stays_inside_the_unknown_boundary(bad):
    source = page()
    source["rows"][0]["body_observations"][0]["bad"] = bad
    view, html = legacy._project_or_unknown(source, SOURCE, DAY, DAY)
    assert view["read_status"] == "UNAVAILABLE" and view["rows"] == []
    assert "UNKNOWN" in html


@pytest.mark.parametrize("kind", ["cycle", "deep"])
def test_recursive_metadata_refuses_without_erasing_the_unknown_result(kind):
    source = page()
    if kind == "cycle":
        value = []
        value.append(value)
    else:
        value = []
        # Pytest may raise the interpreter limit; exceed the actual active bound.
        for _ in range(sys.getrecursionlimit() + 100):
            value = [value]
    source["rows"][0]["extra"] = value
    view, _ = legacy._project_or_unknown(source, SOURCE, DAY, DAY)
    assert view["read_status"] == "UNAVAILABLE" and view["rows"] == []


def test_consumer_datetime_is_retained_for_real_age_calculation():
    source = page()
    stamp = datetime(2026, 10, 23, 20, tzinfo=timezone.utc)
    source["consumers"] = [dict(consumer_id="CONTROLLED", updated_at=stamp,
        status="CONTROLLED", issues=[])]
    detached = detached_simulation_snapshot(source)
    assert detached.snapshot["consumers"][0]["updated_at"] == stamp
    result = read_model.simulation_view(detached.snapshot, now=stamp, _detached_snapshot=detached)
    assert result["workers"][0]["age_seconds"] == 0
    assert result["workers"][0]["online"] is True


def test_unselected_loaders_do_not_submit_work_or_touch_a_configured_reader(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("unselected empty projection must not use executor")
    class Reader:
        def read(self, *args, **kwargs):
            raise AssertionError("unselected source must not read storage")
    monkeypatch.setattr(asyncio, "to_thread", forbidden)
    old = asyncio.run(legacy.load_legacy_display(Reader(), day=DAY))
    new = asyncio.run(firstbar.load_firstbar_display(Reader()))
    assert old["view"]["read_status"] == "UNAVAILABLE"
    assert new["read_status"] == "UNAVAILABLE"
    assert old["source_id"] is new["source_id"] is None


@pytest.mark.parametrize("loader,kwargs", [
    (legacy.load_legacy_display, {"day": DAY, "days": 8}),
    (firstbar.load_firstbar_display, {"days": 0}),
])
def test_unselected_fast_path_does_not_skip_scope_validation(loader, kwargs):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(loader(None, **kwargs))
    assert caught.value.status_code == 400


def test_default_shared_page_retains_live_bytes_and_forms_with_no_executor(monkeypatch):
    from tests.unit.test_legacy_sim_shared_mount import mounted, request
    from project_mai_tai.services import control_plane as cp

    async def forbidden(*args, **kwargs):
        raise AssertionError("no source selected: no empty SIM executor work")
    captured = []
    render = cp._render_bot_detail_page
    def observe(data, strategy, **kwargs):
        captured.append((deepcopy(data), strategy, kwargs["firstbar_simulation_html"]))
        return render(data, strategy, **kwargs)
    monkeypatch.setattr(cp, "_render_bot_detail_page", observe)
    with mounted() as (app, first, old, data, original):
        monkeypatch.setattr(asyncio, "to_thread", forbidden)
        response = request(app, "/bot/1m-schwab-v2")
        assert response.status_code == 200 and first.calls == old.calls == []
        snapshot, strategy, panel = captured[0]
        assert response.text.replace(panel, "", 1) == render(snapshot, strategy)
        assert 'name="legacy_source_id"' in panel and 'data-firstbar-mount="true"' in panel
        assert "UNKNOWN" in panel and data == original
