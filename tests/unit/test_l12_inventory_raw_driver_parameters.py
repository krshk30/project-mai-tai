"""Actual driver conversion and controlled parent seam; no PG connection."""
import pytest
from psycopg import ProgrammingError
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

from tests.unit.test_l12_main_nonce_target import capability, owned_files
from tests.unit.test_l12_schema_rehearsal_outcomes import state, parent


def test_actual_psycopg_literal_percent_requires_parameter_free_dispatch():
    query = PostgresQuery(Transformer())
    with pytest.raises(ProgrammingError, match="allowed as placeholders"):
        query.convert(parent.INVENTORY_SQL, {})
    query.convert(parent.INVENTORY_SQL, None)
    assert query.query == parent.INVENTORY_SQL.encode("utf-8")
    assert query.params is None


def test_both_raw_inventory_reads_select_parameter_free_driver_path(monkeypatch, capability, owned_files):
    factory, kwargs, events, made, primary, journal = state(monkeypatch, capability, owned_files, None)
    try:
        parent.rehearse(factory, **kwargs)
        calls = [(sql, options) for sql, options in made[0].actual_connection.calls
            if sql == parent.INVENTORY_SQL]
        assert len(calls) == 2
        assert all(options == {"execution_options": {"no_parameters": True}}
            for sql, options in calls)
    finally:
        journal.close()
