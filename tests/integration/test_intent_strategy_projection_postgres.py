"""Registration fast-path and write fallback equivalence on actual PostgreSQL."""

import pytest

import test_cancel_terminal_runtime as runtime
from tests.unit import test_intent_strategy_projection as control
from tests.unit.test_intent_order_scalar_reads import seed_database

sessions = runtime.sessions


@pytest.fixture
def database(sessions):
    return seed_database(sessions)


def test_pg_matching_registration_is_fresh_without_hydration(database):
    control.test_matching_registration_uses_fresh_core_columns_without_orm_or_write(database)


@pytest.mark.parametrize("field,value", [
    ("name", "changed"), ("execution_mode", "live"),
    ("metadata_json", {"nested": {"key": [1, 2]}}), ("is_enabled", False),
])
def test_pg_configuration_changes_still_write(database, field, value):
    control.test_registration_differences_use_original_metadata_and_configuration_write(database, field, value)


@pytest.mark.parametrize("autoflush", [False, True])
def test_pg_dirty_identity_is_not_replaced(database, autoflush):
    control.test_registration_dirty_identity_falls_back_without_replacing_object(database, autoflush)


def test_pg_missing_registration_still_inserts(database):
    control.test_missing_registration_retains_original_insert_path(database)
