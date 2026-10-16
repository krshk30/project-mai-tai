"""Source-stage boundaries: no systemd/package/broker operations run in these controls."""
from pathlib import Path
import sys

import pytest

from ops.weekmaint import copy_plan

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("dsn", [
    "postgresql+psycopg://user@production:51767/project_mai_tai",
    "postgresql+psycopg://user@127.0.0.1:5432/replay_private",
    "postgresql+psycopg://user@production:51767/replay_private",
    "postgresql+psycopg://user@/replay_private?host=/var/run/postgresql&port=51767",
])
def test_copy_preview_rejects_nonprivate_target_before_connection(dsn):
    with pytest.raises(ValueError):
        copy_plan.clone_url(dsn)


@pytest.mark.parametrize("mode", ["--execute", "--boot-verify"])
def test_source_stage_live_paths_refuse_before_any_connection(monkeypatch, capsys, mode):
    def forbidden(*_args, **_kwargs):
        pytest.fail("source stage tried a live connection")
    monkeypatch.setattr(copy_plan, "create_engine", forbidden)
    monkeypatch.setattr(sys, "argv", ["weekmaint", mode])
    assert copy_plan.main() == 78
    assert "BLOCKED_NOT_INSTALLED_SOURCE_STAGE" in capsys.readouterr().out


def test_proposed_chain_depends_on_sec_success_not_just_ordering_or_second_timer():
    folder = ROOT / "ops/weekmaint"
    dropin = (folder / "project-mai-tai-momlive-sec.service.d/60-weekmaint-after-success.conf").read_text()
    assert "OnSuccess=project-mai-tai-weekmaint.service" in dropin
    service = (folder / "project-mai-tai-weekmaint.service").read_text()
    assert "After=network-online.target project-mai-tai-momlive-sec.service" in service
    assert "Restart=no" in service and "Wants=project-mai-tai-momlive-sec" not in service
    assert not list(folder.glob("*.timer"))  # The existing Saturday SEC timer is the sole chain trigger.
    assert "MAI_TAI_MAINTENANCE_ATTENDED" not in service
    retain = (ROOT / "ops/systemd/project-mai-tai-prune-retention-weekly.timer").read_text()
    assert "OnCalendar=Sun *-*-* 05:45:00 America/New_York" in retain


def test_no_snapshot_delete_or_trading_history_delete_is_authored():
    folder = ROOT / "ops/weekmaint"
    for path in folder.glob("*.py"):
        source = path.read_text()
        assert "DELETE FROM" not in source and "TRUNCATE " not in source
