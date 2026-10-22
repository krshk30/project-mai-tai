"""Input-refusal controls only; these are not the historical OLOX replay gate."""
import pytest

from tools.slotcap_olox_same_sell_replay import replay_olox_same_sell


def forbidden(*args):
    raise AssertionError("Unbound input must be rejected before any replay")


def test_adapter_refuses_any_filename_except_sanitized_without_reading_it(tmp_path):
    # Deliberately absent path; the adapter must refuse the name before opening.
    with pytest.raises(ValueError, match="SANITIZED"):
        replay_olox_same_sell(None, sanitized_path=tmp_path / "unapproved.txt",
                             load_bound_snapshot=forbidden, set_clock=forbidden)


def test_adapter_refuses_changed_sanitized_bytes_before_snapshot_loading(tmp_path):
    path = tmp_path / "SLOTCAP-OLOX-EXPORT-LOGS-SANITIZED-20261021.txt"
    path.write_bytes(b"synthetic hash mismatch control, no incident evidence")
    with pytest.raises(ValueError, match="hash mismatch"):
        replay_olox_same_sell(None, sanitized_path=path,
                             load_bound_snapshot=forbidden, set_clock=forbidden)
