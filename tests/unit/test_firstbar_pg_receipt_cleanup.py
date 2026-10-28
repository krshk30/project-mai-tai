"""Controlled finalization faults, not actual PostgreSQL lifecycle proof."""

import subprocess

import pytest

from tools import firstbar_private_pg_store as setup


@pytest.mark.parametrize("primary", [None, ValueError("CONTROLLED original failure")])
def test_receipt_failure_stops_owned_server_and_preserves_primary_error(tmp_path, monkeypatch, primary):
    data = tmp_path / "data"
    data.mkdir()
    (data / "postmaster.pid").write_text("CONTROLLED_NOT_A_PROCESS")
    calls = []

    def failed_write(*args):
        raise OSError("CONTROLLED receipt disk failure")

    def stopped(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(setup, "exclusive_json", failed_write)
    monkeypatch.setattr(setup.subprocess, "run", stopped)
    if primary is None:
        with pytest.raises(OSError, match="receipt disk failure"):
            setup.finalize_owned_receipt(tmp_path, data, {}, primary_error=primary)
    else:
        setup.finalize_owned_receipt(tmp_path, data, {}, primary_error=primary)
        assert "OSError" in primary.__notes__[0]
        assert str(primary) == "CONTROLLED original failure"
    assert calls == [[str(setup.BIN / "pg_ctl"), "-D", str(data), "-w", "-m", "fast", "stop"]]


def test_successful_finalization_does_not_stop_owned_store(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(setup, "exclusive_json", lambda *args: seen.append(args))
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected stop"))
    setup.finalize_owned_receipt(tmp_path, tmp_path / "data", {"status": "CONTROLLED"})
    assert seen == [(tmp_path / "creation-receipt.json", {"status": "CONTROLLED"})]


@pytest.mark.parametrize("primary", [None, ValueError("CONTROLLED retained setup error")])
def test_nonzero_owned_stop_is_reported_without_losing_primary_error(tmp_path, monkeypatch, primary):
    data = tmp_path / "data"
    data.mkdir()
    (data / "postmaster.pid").write_text("CONTROLLED_NOT_A_PROCESS")

    def failed_write(*args):
        raise OSError("CONTROLLED receipt finalization failed")

    monkeypatch.setattr(setup, "exclusive_json", failed_write)
    monkeypatch.setattr(setup.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 7))
    if primary is None:
        with pytest.raises(OSError) as caught:
            setup.finalize_owned_receipt(tmp_path, data, {})
        notes = caught.value.__notes__
    else:
        setup.finalize_owned_receipt(tmp_path, data, {}, primary_error=primary)
        assert str(primary) == "CONTROLLED retained setup error"
        notes = primary.__notes__
    assert any("owned cleanup stop exit=7" in note and str(tmp_path) in note for note in notes)
