"""Read projection and ORM identity controls on isolated real PostgreSQL."""

from decimal import Decimal

import pytest

import test_cancel_terminal_runtime as runtime
from tests.unit import test_intent_order_scalar_reads as control

sessions = runtime.sessions


@pytest.fixture
def database(sessions):
    return control.seed_database(sessions)


@pytest.mark.parametrize("quantity", [None, Decimal(0), Decimal(-1), Decimal("1.25")])
def test_pg_virtual_quantity_without_hydration(database, quantity):
    control.test_virtual_quantity_matches_without_hydrating_position(database, quantity)


@pytest.mark.parametrize("autoflush", [False, True])
def test_pg_virtual_quantity_transaction_semantics(database, autoflush):
    control.test_virtual_quantity_retains_dirty_identity_and_autoflush_semantics(database, autoflush)


def test_pg_order_create_and_repeated_report_identity(database):
    control.test_order_create_and_repeated_report_preserve_exact_identity_and_payload(database)


@pytest.mark.parametrize("quantity", [None, Decimal(0), Decimal(-1), Decimal("1.25")])
def test_pg_account_quantity_without_hydration(database, quantity):
    control.test_account_quantity_matches_without_hydrating_position(database, quantity)


@pytest.mark.parametrize("autoflush", [False, True])
def test_pg_account_quantity_transaction_semantics(database, autoflush):
    control.test_account_quantity_retains_dirty_identity_and_autoflush_semantics(database, autoflush)


@pytest.mark.parametrize("virtual", [False, True])
def test_pg_clean_quantity_without_orm_setup(database, virtual):
    control.test_clean_quantity_read_uses_core_result_without_orm_setup(database, virtual)


@pytest.mark.parametrize("virtual", [False, True])
def test_pg_pending_quantity_autoflush_and_rollback(database, virtual):
    control.test_pending_quantity_keeps_autoflush_and_transaction_values(database, virtual)
