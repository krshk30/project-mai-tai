"""Pure check of actual application default operands; no engine or SQL run."""

import pytest

from tools.l12_service_boot_replay import require_manual_stop_application_bounds


def test_exact_750_250_defaults_are_retained():
    assert require_manual_stop_application_bounds(("750ms", "250ms")) is None


@pytest.mark.parametrize("values", [
    ("0", "0"), ("750ms", "0"), ("0", "250ms"), ("1s", "250ms"),
    ("750ms", "1s"), ("750ms",), ("750ms", "250ms", "extra"),
])
def test_probe_only_or_changed_defaults_do_not_qualify_real_application_transaction(values):
    with pytest.raises(ValueError, match="MANSTOP_actual_application_transaction_defaults"):
        require_manual_stop_application_bounds(values)
