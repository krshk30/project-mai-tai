"""Run the same Core/ORM equivalence controls on isolated PostgreSQL schemas."""

import pytest

from tests.integration import test_cancel_terminal_runtime as runtime
from tests.unit import test_close_core_read_equivalence as controls

sessions = runtime.sessions


@pytest.fixture
def database(sessions):
    return controls.seed_database(sessions)


@pytest.mark.parametrize("include", [True, False])
def test_pg_same_rows_values_order_and_total(database, include):
    controls.test_core_same_rows_values_order_and_total(database, include)


@pytest.mark.parametrize("include", [True, False])
@pytest.mark.parametrize("case", ["loaded", "json_in_place", "dirty", "new", "deleted", "no_autoflush"])
def test_pg_identity_transaction_equivalence(database, include, case):
    controls.test_reservation_identity_and_transaction_equivalence(database, include, case)


def test_pg_stale_loaded_identity(database):
    controls.test_clean_stale_loaded_order_keeps_original_identity_value(database)


@pytest.mark.parametrize("case", ["absent", "existing", "loaded", "dirty", "new", "deleted", "no_autoflush"])
def test_pg_stop_delete_and_rollback(database, case):
    controls.test_stop_delete_equivalent_and_never_commits(database, case)


def test_pg_reused_stop_lookup_key_isolation(database):
    controls.test_reused_stop_absence_lookup_never_leaks_a_key(database)
