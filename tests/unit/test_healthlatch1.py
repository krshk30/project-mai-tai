from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/healthlatch1"
spec = importlib.util.spec_from_file_location("healthlatch1", ROOT / "ops/health/fleet_health_check.py")
fhc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fhc)


def _state_and_logs(tmp_path):
    state = tmp_path / "state.json"
    state.write_bytes((FIXTURES / "socket_evidence_offsets.json").read_bytes())
    paper = tmp_path / "momentum-paper.log"
    paper.write_bytes((FIXTURES / "momentum-paper.log").read_bytes())
    market = tmp_path / "market-data.log"
    market.write_text("healthy\n")
    return state, market, paper


# 23 function+runtime verdicts: strategy-bar-freshness retired (HEALTHNOISE1).
@pytest.mark.parametrize("runtime_only,expected", [(True, 19), (False, 23)])
def test_today_exact_stale_state_and_paper_log_summary_green(
    tmp_path, monkeypatch, capsys, runtime_only, expected,
):
    state, market, paper = _state_and_logs(tmp_path)
    assert paper.stat().st_size == 321
    real_reader = fhc._read_appended_socket_evidence
    read_paths = []

    def reader(path, prior):
        read_paths.append(path)
        return real_reader(path, prior)

    monkeypatch.setattr(fhc, "_SOCKET_EVIDENCE_STATE_PATH", state)
    monkeypatch.setattr(fhc, "_MARKET_DATA_LOG_PATH", market)
    monkeypatch.setattr(fhc, "_MOMENTUM_LOG_PATH", paper, raising=False)
    monkeypatch.setattr(fhc, "_read_appended_socket_evidence", reader)
    states = {unit: fhc.ServiceRuntime(0, "active", "running") for unit in fhc.RUNTIME_SERVICES}
    runtime_rows = fhc.classify_service_runtime_rows(
        states, {unit: 0 for unit in states}, elapsed_s=300,
        now=datetime(2026, 10, 5, 10, 33, tzinfo=UTC), maintenance={},
    )
    runtime = (
        fhc.CheckSpec(lambda: runtime_rows, fhc.FLEET_RUNTIME),
        fhc.CheckSpec(fhc.check_massive_socket_policy_violations, fhc.FLEET_RUNTIME),
    )
    functions = tuple(
        fhc.CheckSpec(
            lambda name=original.check.__name__: (
                "GREEN",
                "v2-bar-continuity" if name == "check_bar_continuity"
                else name.removeprefix("check_").replace("_", "-"),
                "fixture",
            ),
            original.alert_class,
        ) for original in fhc.FUNCTION_CHECKS
    )
    monkeypatch.setattr(fhc, "RUNTIME_CHECKS", runtime)
    monkeypatch.setattr(fhc, "CHECKS", runtime + functions)
    assert fhc.main(runtime_only=runtime_only) == 0
    output = capsys.readouterr().out
    assert output.count("VERDICT:") == expected
    assert f"SUMMARY: GREEN fleet-function-health checks={expected}" in output
    assert "feed-policy-violation" not in output
    assert output.count("v2-bar-continuity") == (0 if runtime_only else 1)
    assert read_paths == [market]
    saved = json.loads(state.read_text())
    assert set(saved) == {"logs"}
    assert set(saved["logs"]) == {str(market)}


def test_old_paper_cooloff_logs_are_never_read(tmp_path, monkeypatch):
    state, market, paper = _state_and_logs(tmp_path)
    paper.write_text(
        "[MOMENTUM-PAPER-FEED-POLICY] decision=cooloff reason=feed_policy_violation\n"
        "[MOMENTUM-PAPER-FEED-POLICY] decision=recovered\n"
    )
    before = paper.read_bytes()
    real_reader = fhc._read_appended_socket_evidence

    def reader(path, prior):
        assert path == market
        return real_reader(path, prior)

    monkeypatch.setattr(fhc, "_read_appended_socket_evidence", reader)
    rows = fhc.check_massive_socket_policy_violations(
        state_path=state, market_data_log=market, momentum_log=paper,
    )
    assert len(rows) == 1 and rows[0][0] == "GREEN"
    assert paper.read_bytes() == before


def test_gateway_rotation_keeps_new_policy_close_red(tmp_path):
    state, market, paper = _state_and_logs(tmp_path)
    clean = fhc.check_massive_socket_policy_violations(
        state_path=state, market_data_log=market, momentum_log=paper,
    )
    assert clean[0][0] == "GREEN"
    market.rename(tmp_path / "market-data.log-old")
    market.write_text("received 1008 (policy violation); then sent 1008 (policy violation)\n")
    rotated = fhc.check_massive_socket_policy_violations(
        state_path=state, market_data_log=market, momentum_log=paper,
    )
    assert rotated[0][0] == "RED"
    assert "new_matches=2" in rotated[0][2]
    clean = fhc.check_massive_socket_policy_violations(
        state_path=state, market_data_log=market, momentum_log=paper,
    )
    assert clean[0][0] == "GREEN" and "new_matches=0" in clean[0][2]


def test_stale_state_cleanup_preserves_gateway_cursor_without_replaying_old_close(tmp_path):
    state, market, paper = _state_and_logs(tmp_path)
    market.write_text("received 1008 (policy violation)\n")
    stat = market.stat()
    old = json.loads(state.read_text())
    old["logs"][str(market)] = {
        "device": stat.st_dev, "inode": stat.st_ino, "offset": stat.st_size,
    }
    state.write_text(json.dumps(old))
    rows = fhc.check_massive_socket_policy_violations(
        state_path=state, market_data_log=market, momentum_log=paper,
    )
    assert rows[0][0] == "GREEN" and "bytes_scanned=0" in rows[0][2]
    saved = json.loads(state.read_text())
    assert saved == {"logs": {str(market): old["logs"][str(market)]}}


def test_missing_socket_state_keeps_existing_first_read_behavior(tmp_path):
    state, market, paper = _state_and_logs(tmp_path)
    state.unlink()
    rows = fhc.check_massive_socket_policy_violations(
        state_path=state, market_data_log=market, momentum_log=paper,
    )
    assert rows[0][0] == "GREEN" and state.exists()


def test_n2_shared_cursor_characterizes_bar_gap_consuming_pager_evidence(tmp_path):
    """Assessment only: this existing latent loss is NOT repaired by HEALTHLATCH1."""
    state, market, paper = _state_and_logs(tmp_path)
    market.write_text("received 1008 (policy violation); then sent 1008 (policy violation)\n")
    bar_gap = fhc.check_massive_socket_policy_violations(
        state_path=state, market_data_log=market, momentum_log=paper,
    )
    pager = fhc.check_massive_socket_policy_violations(
        state_path=state, market_data_log=market, momentum_log=paper,
    )
    assert bar_gap[0][0] == "RED"
    assert pager[0][0] == "GREEN" and "new_matches=0" in pager[0][2]
