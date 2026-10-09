"""Exit order projection equivalence on real PostgreSQL."""
import pytest

import test_cancel_terminal_runtime as runtime
from tests.unit import test_exit_order_projection as control
from tests.unit.test_drift_cache_projection_equivalence import make_lane

sessions = runtime.sessions


@pytest.fixture
def lane(sessions):
    return make_lane(sessions)


@pytest.mark.parametrize("value", ["true", " TRUE ", "\tTrUe\n", True, False, None, 1, "false", ["true"]])
@pytest.mark.parametrize("native,include_native", [(True, True), (False, False), (False, True)])
def test_pg_exit_discriminator_and_selection(lane, value, native, include_native):
    control.test_exit_read_matches_discriminator_and_latest_order(lane, value, native, include_native)


def test_pg_exit_in_transaction_identity(lane):
    control.test_exit_read_preserves_in_transaction_payload_and_identity(lane)


def test_pg_no_guard_no_hydration(lane):
    control.test_native_exit_without_a_guard_hydrates_no_orders(lane)
