from datetime import date, datetime
import pytest

from project_mai_tai.atr_simulation.aggregate_entry_reader import (
    AggregateEntryUIReader, AggregateReadBinding, END, START, bound_run, validate_scope,
)
from project_mai_tai.atr_simulation.repository import digest


SOURCE = "aggregate-entry:" + "a" * 64
BINDING = AggregateReadBinding(SOURCE, "b" * 64)


@pytest.mark.parametrize("values", [
    ("foreign", START, END, 10000),
    (SOURCE, date(2026, 9, 21), END, 10000),
    (SOURCE, START, date(2026, 10, 24), 10000),
    (SOURCE, END, START, 10000),
    (SOURCE, datetime(2026, 9, 22), END, 10000),
    (SOURCE, START, END, True),
    (SOURCE, START, END, 10001),
    (SOURCE, START, END, 0),
])
def test_scope_refuses_without_storage(values):
    with pytest.raises(ValueError):
        validate_scope(*values, BINDING)


def test_full_seed_scope_allowed_but_no_default_database_or_connection():
    validate_scope(SOURCE, START, END, 10000, BINDING)
    calls = []
    reader = AggregateEntryUIReader(binding=BINDING, engine_factory=lambda url: calls.append(url))
    assert calls == []
    result = reader.read(SOURCE, START, END)
    assert result["read_status"] == "UNAVAILABLE" and result["rows"] == []
    assert calls == []


def test_stored_run_pins_all_fields_and_ignores_only_creation_time():
    run = dict(source_id=SOURCE, input_sha256="a" * 64, policy_version="CONTROLLED",
        source_refs=[{"sha256": "c" * 64}], coverage={"unknown": True}, result_sha256="d" * 64)
    binding = AggregateReadBinding(SOURCE, digest(run))
    assert bound_run(dict(run, created_at="EXTRA"), binding)
    for field in run:
        changed = dict(run)
        changed[field] = "CHANGED"
        assert not bound_run(changed, binding)
    assert not bound_run(None, binding)
    assert not bound_run({}, binding)


@pytest.mark.parametrize("source,checksum", [("", "b"*64), (SOURCE, ""), ("other:"+"a"*64, "b"*64)])
def test_invalid_binding_refuses(source, checksum):
    with pytest.raises(ValueError):
        AggregateReadBinding(source, checksum)


@pytest.mark.parametrize("page,limit,expected_count,truncated", [
    ([], 10000, 0, False),
    ([{"CONTROLLED": 1}], 10000, 1, False),
    ([{"CONTROLLED": 1}, {"CONTROLLED": 2}], 1, 1, True),
])
def test_actual_query_seam_bound_empty_page_is_available_and_transaction_rolled_back(
        page, limit, expected_count, truncated):
    from unittest.mock import MagicMock
    from project_mai_tai.atr_simulation import aggregate_entry_reader as module
    run = dict(source_id=SOURCE, input_sha256="a"*64, policy_version="CONTROLLED",
        source_refs=[], coverage={}, result_sha256="d"*64)
    binding = AggregateReadBinding(SOURCE, digest(run))
    connection = MagicMock()
    connection.dialect.name = "postgresql"
    transaction = connection.begin.return_value
    run_result, page_result, consumers_result = MagicMock(), MagicMock(), MagicMock()
    run_result.mappings.return_value.one_or_none.return_value = run
    page_result.scalars.return_value.all.return_value = page
    consumers_result.mappings.return_value.all.return_value = []
    connection.execute.side_effect = [run_result, page_result, consumers_result]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    reader = module.AggregateEntryUIReader("postgresql+psycopg://CONTROLLED/CONTROLLED",
        binding=binding, engine_factory=lambda url: engine)
    result = reader.read(SOURCE, START, END, limit=limit)
    assert result["read_status"] == "AVAILABLE"
    assert len(result["rows"]) == expected_count and result["truncated"] is truncated
    transaction.rollback.assert_called_once()
    assert [call.args[0] for call in connection.exec_driver_sql.call_args_list] == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        "SET LOCAL statement_timeout = '750ms'", "SET LOCAL lock_timeout = '250ms'"]
    assert len(connection.execute.call_args_list) == 3
    query = connection.execute.call_args_list[1].args[0].compile()
    assert SOURCE in query.params.values()
    assert limit + 1 in query.params.values()
