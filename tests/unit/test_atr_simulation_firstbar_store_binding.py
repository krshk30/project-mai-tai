"""New source controls for actual Gibbs -> SQLite -> view; all CONTROLLED inputs.

Not the 173-case replay, actual PG/durability, mounted UI or process acceptance.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from project_mai_tai.atr_simulation.firstbar import CompletedFlipBar
from project_mai_tai.atr_simulation.firstbar_binding import (
    FIRSTBAR_ENGINE_HEAD, FIRSTBAR_FILL_POLICY, FIRSTBAR_RESULT_VERSION,
    load_firstbar_engine,
)
from project_mai_tai.atr_simulation.firstbar_display import firstbar_tables
from project_mai_tai.atr_simulation.firstbar_projection import (
    SealedFirstBarCohort, firstbar_result_row, publish_firstbar_cohort,
)
from project_mai_tai.atr_simulation.firstbar_read_model import firstbar_display_view
from project_mai_tai.atr_simulation.firstbar_replay import FirstBarReplayInput
from project_mai_tai.atr_simulation.models import SIMULATION_TABLES, metadata, rows
from project_mai_tai.atr_simulation.repository import SimulationRepository
from project_mai_tai.atr_simulation.store_schema_20261023 import simulation_metadata
from project_mai_tai.atr_simulation.types import (
    Boundary, Coverage, EpisodeInput, EventKey, EvidenceKind, FlipIdentity,
    LiveFacts, SourceRef,
)
from tests.support.atr_simulation_eligibility import POLICY, controlled_trade


NS = 1_000_000_000
START = 1792764000 * NS
SOURCE = SourceRef("CONTROLLED:firstbar-io", "a" * 64, "$", EvidenceKind.CONTROLLED)
COVERAGE = Coverage(START, START + 300 * NS, True, (), (SOURCE,))


def _trade(second, price="2.01", size=100):
    return controlled_trade(EventKey(START + second * NS, 0, f"controlled-raw-{second}", SOURCE),
                            "X", Decimal(price), size)


def _item(account="one", *, unknown_after_fill=False, false_flip=False):
    tape = (_trade(61), _trade(62), _trade(63, "2.20"))
    if unknown_after_fill:
        tape = (tape[0], replace(tape[1], execution=None), tape[2])
    identity = FlipIdentity("2026-10-23", "X", "schwab", account, "ATR", "flip", "segment")
    episode = EpisodeInput(identity, Boundary(START + 60 * NS, SOURCE, bar_start_ns=START),
        True, SOURCE, Decimal("1.98"), 196, SOURCE,
        Boundary(START + 240 * NS, SOURCE), Boundary(START + 300 * NS, SOURCE),
        tape, (), COVERAGE, COVERAGE, (), COVERAGE,
        LiveFacts(None, (), (), replace(COVERAGE, complete=None)), POLICY)
    bar = CompletedFlipBar("X", START, START + 60 * NS, Decimal("2"),
        Decimal("1.97") if false_flip else Decimal("1.99"), Decimal("1.98"),
        "short" if false_flip else "long", True, SOURCE)
    return FirstBarReplayInput(episode, bar, Boundary(START + 180 * NS, SOURCE))


def _cohort(*items, source="CONTROLLED-new-source"):
    return SealedFirstBarCohort(source, "b" * 64, tuple(items), (SOURCE,),
        {"complete": True, "gaps": [], "sources": ["CONTROLLED"]}, True)


@pytest.fixture
def repository():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    try:
        yield SimulationRepository(engine)
    finally:
        engine.dispose()


def _stored(repository, source="CONTROLLED-new-source"):
    return repository.read(source, date(2026, 10, 23), date(2026, 10, 23))


def test_actual_canonical_engine_results_copy_position_facts_without_pnl_oracle():
    engine, api = load_firstbar_engine()
    result = engine.simulate_many((_item(),), fill_policy=api.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)[0]
    row = firstbar_result_row(result)
    assert row["mode"] == "FIRSTBAR"
    assert row["model"] == FIRSTBAR_FILL_POLICY
    assert row["first_fill_ts"] == result.position.first_fill_ts
    assert row["filled_qty"] == result.position.filled_qty == 196
    assert row["gross_pnl"] == str(result.gross_pnl)
    assert row["position"]["ignored_fills"] == []
    assert row["live"]["bought"] is None
    assert row["qualification"] == "UNQUALIFIED_REPLAY"


def test_actual_filled_unknown_preserves_entry_and_unknown_economics(repository):
    result_rows = publish_firstbar_cohort(repository, _cohort(_item(unknown_after_fill=True)))
    assert all(row["outcome_kind"] == "TRADE" and row["fills"] for row in result_rows)
    assert all(row["decision"] == "UNKNOWN" and row["gross_pnl"] is None for row in result_rows)
    tables = firstbar_tables(firstbar_display_view(_stored(repository), source_id="CONTROLLED-new-source"))
    assert all(len(table["rows"]) == 1 and table["rows"][0]["status"] == "UNKNOWN" for table in tables)
    assert all(table["rows"][0]["row"]["pnl_label"] == "UNKNOWN" for table in tables)
    assert all(table["rows"][0]["row"]["exit_price"] != "UNKNOWN" for table in tables)


def test_actual_open_null_exit_keeps_accepted_trade_without_invented_exit_or_zero(repository):
    item = _item()
    item = replace(item, episode=replace(item.episode, trades=(_trade(61), _trade(62))))
    publish_firstbar_cohort(repository, _cohort(item))
    tables = firstbar_tables(firstbar_display_view(_stored(repository), source_id="CONTROLLED-new-source"))
    assert all(table["rows"][0]["status"] == "OPEN" for table in tables)
    assert all(table["rows"][0]["row"]["exit_time"] == "UNKNOWN" for table in tables)
    assert all(table["rows"][0]["row"]["pnl_label"] == "UNKNOWN" for table in tables)


def test_firstbar_policy_fits_explicit_schema_and_old_pairs_are_preserved():
    assert rows.c.model.type.length >= len(FIRSTBAR_FILL_POLICY) > 32
    assert set(metadata.tables) == SIMULATION_TABLES == set(simulation_metadata().tables)
    constraints = " ".join(str(constraint.sqltext) for constraint in rows.constraints
                           if isinstance(constraint, sa.CheckConstraint))
    assert "FIRSTBAR_X3" in constraints and FIRSTBAR_FILL_POLICY in constraints
    assert "LIVE" in constraints and "VOLBODY" in constraints and "OPTIMISTIC" in constraints


def test_one_cohort_keeps_both_accounts_two_worlds_and_idempotent_rows(repository):
    cohort = _cohort(_item("one"), _item("two"))
    first = publish_firstbar_cohort(repository, cohort)
    second = publish_firstbar_cohort(repository, cohort)
    assert first == second
    stored = _stored(repository)
    assert len(stored["rows"]) == 4
    assert {row["identity"]["account_id"] for row in stored["rows"]} == {"one", "two"}
    assert {row["mode"] for row in stored["rows"]} == {"FIRSTBAR", "FIRSTBAR_X3"}
    assert stored["run"]["coverage"]["engine_dependency_head"] == FIRSTBAR_ENGINE_HEAD
    assert stored["run"]["coverage"]["qualification"] == "UNQUALIFIED_REPLAY"


def test_canonical_false_flip_is_stored_reason_footer_without_zero_quantity_trade(repository):
    publish_firstbar_cohort(repository, _cohort(_item(false_flip=True)))
    tables = firstbar_tables(firstbar_display_view(_stored(repository), source_id="CONTROLLED-new-source"))
    assert all(table["rows"] == [] for table in tables)
    assert all(table["skip_counts"] == {"SKIPPED_FALSE_FLIP": 1} for table in tables)
    assert all(table["totals"]["known_pnl_label"] == "UNKNOWN" for table in tables)


@pytest.mark.parametrize("field,value", [("engine_dependency_head", "old-unbound"),
                                        ("engine_contract_version", "future-unbound")])
def test_stored_unknown_binding_cannot_be_upgraded_by_current_import(repository, field, value):
    publish_firstbar_cohort(repository, _cohort(_item()))
    stored = _stored(repository)
    stored["run"]["coverage"][field] = value
    view = firstbar_display_view(stored, source_id="CONTROLLED-new-source")
    assert view["source"]["engine_dependency_head"] == "UNKNOWN"
    assert view["outcomes"] == [] and view["coverage_status"] == "UNKNOWN"
    assert all(total["known_pnl_label"] == "UNKNOWN" for total in view["totals"].values())


def test_missing_database_or_migration_is_unknown_not_no_buy_or_zero():
    view = firstbar_display_view({"run": None, "rows": [], "consumers": [], "truncated": False,
                                 "read_status": "UNAVAILABLE", "read_issues": ["MIGRATION_MISSING"]},
                                source_id="requested-source")
    assert view["coverage_status"] == "UNKNOWN" and view["source"]["engine_dependency_head"] == "UNKNOWN"
    assert all(total["known_pnl_label"] == "UNKNOWN" for total in view["totals"].values())


def test_incomplete_overlapping_cohort_refuses_before_execution(repository):
    cohort = replace(_cohort(_item()), all_overlapping_accounts_included=False)
    with pytest.raises(ValueError, match="explicit_sealed_firstbar_cohort_required"):
        publish_firstbar_cohort(repository, cohort)
    assert _stored(repository)["run"] is None


def test_future_result_version_cannot_use_current_store_projection():
    engine, api = load_firstbar_engine()
    result = engine.simulate_many((_item(),), fill_policy=api.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)[0]
    with pytest.raises(ValueError, match="firstbar_projection_version_or_policy_mismatch"):
        firstbar_result_row(replace(result, version="future-version"))


def test_wrong_firstbar_model_pair_rolls_back_entire_publication(repository):
    engine, api = load_firstbar_engine()
    result = engine.simulate_many((_item(),), fill_policy=api.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)[0]
    row = firstbar_result_row(result)
    row["model"] = "OPTIMISTIC"
    with pytest.raises(sa.exc.IntegrityError):
        repository.publish(source_id="bad-pair", input_sha256="c" * 64,
            policy_version=FIRSTBAR_RESULT_VERSION, source_refs=["CONTROLLED"],
            coverage={"complete": None}, result_rows=[row])
    assert _stored(repository, "bad-pair")["run"] is None


def test_new_world_publication_leaves_controlled_live_bytes_unchanged(repository):
    sentinel = b'{"source":"CONTROLLED_LIVE","pnl":"12.30","reason":null}'
    with repository.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE controlled_live_sentinel (payload BLOB)")
        connection.exec_driver_sql("INSERT INTO controlled_live_sentinel VALUES (?)", (sentinel,))
    publish_firstbar_cohort(repository, _cohort(_item()))
    with repository.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT payload FROM controlled_live_sentinel").scalar_one() == sentinel


def test_canonical_stored_view_is_not_mutated_and_known_subtotals_not_global(repository):
    publish_firstbar_cohort(repository, _cohort(_item()))
    stored = _stored(repository)
    snapshot = deepcopy(stored)
    view = firstbar_display_view(stored, source_id="CONTROLLED-new-source")
    assert stored == snapshot
    assert all(not total["complete_requested_window"] for total in view["totals"].values())
    assert "UNQUALIFIED_REPLAY" in view["source"]["population_label"]


def test_migration_uses_frozen_sim_snapshot_not_mutable_models_import():
    root = Path(__file__).resolve().parents[2]
    source = (root / "sql/migrations/versions/20261023_atr_simulation_store.py").read_text()
    assert "store_schema_20261023" in source
    assert "atr_simulation.models" not in source
    assert 'down_revision = "20261015_bot_control"' in source
