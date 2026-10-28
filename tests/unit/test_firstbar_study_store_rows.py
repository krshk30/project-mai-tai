"""First controlled serialization checks; no historical replay or DB proof."""

from copy import deepcopy

from project_mai_tai.atr_simulation.firstbar_binding import load_firstbar_engine
from tests.unit.test_atr_simulation_firstbar_store_binding import _item
from tools.atr_firstbar_study_replay import canonical_study_rows


def test_actual_typed_unknown_results_survive_as_canonical_seed_rows_without_economics():
    engine, api = load_firstbar_engine()
    results = engine.simulate_many((_item(unknown_after_fill=True),),
        fill_policy=api.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)
    before = deepcopy(results)
    rows = canonical_study_rows(results)
    assert results == before
    assert len(rows) == 2 and {row["mode"] for row in rows} == {"FIRSTBAR", "FIRSTBAR_X3"}
    assert all(row["population_label"] == "replay, in-sample; underlying capture completeness UNKNOWN" for row in rows)
    assert all(row["qualification"] == "UNQUALIFIED_REPLAY" for row in rows)
    assert all(row["fills"] and row["gross_pnl"] is None for row in rows)
    assert all(row["live_totals_included"] is False for row in rows)
