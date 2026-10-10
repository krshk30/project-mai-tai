"""Fresh registration columns avoid ORM allocation only for unchanged records."""

import pytest
from sqlalchemy import event

from project_mai_tai.db.models import Strategy
from tests.unit import test_intent_order_scalar_reads as scalar_control


database = scalar_control.database
CONFIG = dict(name="controlled", execution_mode="paper", metadata_json={})


def test_matching_registration_uses_fresh_core_columns_without_orm_or_write(database):
    store, factory, (strategy_id, _) = database
    loaded, writes = [], []
    event.listen(factory, "loaded_as_persistent", lambda _session, row: loaded.append(row))
    event.listen(factory.kw["bind"], "before_cursor_execute",
                 lambda _conn, _cursor, sql, *_args: writes.append(sql)
                 if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) else None)
    with factory() as session:
        reference = store.ensure_intent_strategy(session, "macd_30s", **CONFIG)
        assert reference.id == strategy_id and reference.code == "macd_30s"
        assert not loaded and not session.identity_map and not writes
    # An external edit must be seen rather than hidden behind a process cache.
    with factory() as session:
        row = session.get(Strategy, strategy_id)
        row.metadata_json = {"external": "change"}
        session.commit()
    with factory() as session:
        reference = store.ensure_intent_strategy(session, "macd_30s", **CONFIG)
        assert isinstance(reference, Strategy) and reference.metadata_json == {}
        session.commit()


@pytest.mark.parametrize("field,value", [
    ("name", "changed"), ("execution_mode", "live"),
    ("metadata_json", {"nested": {"key": [1, 2]}}), ("is_enabled", False),
])
def test_registration_differences_use_original_metadata_and_configuration_write(database, field, value):
    store, factory, (strategy_id, _) = database
    with factory() as session:
        row = session.get(Strategy, strategy_id)
        setattr(row, field, value)
        session.commit()
    with factory() as session:
        actual = store.ensure_intent_strategy(session, "macd_30s", **CONFIG)
        assert isinstance(actual, Strategy) and actual.id == strategy_id
        assert actual.name == CONFIG["name"] and actual.execution_mode == CONFIG["execution_mode"]
        assert actual.metadata_json == {} and actual.is_enabled
        session.commit()
    with factory() as session:
        persisted = session.get(Strategy, strategy_id)
        assert persisted.metadata_json == {} and persisted.is_enabled


@pytest.mark.parametrize("autoflush", [False, True])
def test_registration_dirty_identity_falls_back_without_replacing_object(database, autoflush):
    store, factory, (strategy_id, _) = database
    with factory() as session:
        row = session.get(Strategy, strategy_id)
        row.metadata_json = {"dirty": True}
        session.autoflush = autoflush
        assert store.ensure_intent_strategy(session, "macd_30s", **CONFIG) is row
        assert row.metadata_json == {}
        session.rollback()


def test_missing_registration_retains_original_insert_path(database):
    store, factory, _ = database
    with factory() as session:
        row = store.ensure_intent_strategy(session, "new-controlled", **CONFIG)
        assert isinstance(row, Strategy) and row.id and row.code == "new-controlled"
        session.commit()
