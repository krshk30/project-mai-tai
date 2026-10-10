"""Real PG identity/autoflush/rollback controls for the Core order-ID lookup."""

import pytest

from tests.unit import test_order_identity_core_lookup as control
from tests.integration.test_intent_order_scalar_reads_postgres import database as controlled_database
from tests.integration.test_cancel_terminal_runtime import sessions as controlled_sessions

database, sessions = controlled_database, controlled_sessions


@pytest.mark.parametrize("kind", ["missing", "cached", "unloaded", "expired"])
@pytest.mark.parametrize("mode", ["autoflush", "disabled", "context"])
def test_pg_clean_order_id_is_core_with_exact_identity(database, kind, mode):
    control.test_clean_order_id_is_core_and_hydration_retains_exact_identity(database, kind, mode)


@pytest.mark.parametrize("mode", ["autoflush", "disabled", "context"])
@pytest.mark.parametrize("kind", ["dirty", "pending", "rename"])
def test_pg_nonclean_or_noautoflush_preserves_original_behavior(database, mode, kind):
    control.test_nonclean_or_noautoflush_lookup_preserves_original_behavior(database, mode, kind)


@pytest.mark.parametrize("mode", ["autoflush", "disabled", "context"])
def test_pg_deleted_lookup_preserves_original_identity(database, mode):
    control.test_deleted_order_lookup_retains_original_flush_and_identity(database, mode)


@pytest.mark.parametrize("existing", [False, True])
def test_pg_clean_creation_or_update_rollback(database, existing):
    control.test_clean_order_creation_or_update_rolls_back_without_extra_rows(database, existing)
