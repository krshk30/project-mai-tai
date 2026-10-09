"""Actual failed token -> independent fleet verdict -> controlled notification wrapper."""

import contextlib
import importlib.util
import io
import logging
from pathlib import Path

import pytest
from sqlalchemy import select

from project_mai_tai.oms import buy_submission_journal as journal
from test_buy_submission_journal import adapter, request
import test_buy_submission_journal as runtime
from test_fleet_health_cron import _call_log, _run_wrapper

sessions = runtime.sessions


def checker():
    path = Path(__file__).resolve().parents[2] / "ops/health/fleet_health_check.py"
    spec = importlib.util.spec_from_file_location("buy_token_fleet_health", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_real_failed_commit_blocks_wire_and_pages_runtime_with_delivery_retry(
    sessions, tmp_path, monkeypatch, caplog, capsys,
):
    leaf, guard = adapter(sessions)
    calls = []
    async def wire(req):
        calls.append(req)
        return []
    leaf.submit_order = wire
    prepare = guard._prepare
    def fail(*args):
        raise RuntimeError("controlled durable commit failure")
    monkeypatch.setattr(guard, "_prepare", fail)
    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError, match="commit failure"):
            await guard.submit_order(request())
    assert calls == []
    expected = "[OMS-SUBMIT-TOKEN] sym=DKI acct=schwab result=commit_failed buy_blocked=1"
    assert any(r.levelno == logging.ERROR and r.getMessage() == expected for r in caplog.records)
    log = tmp_path / "oms.log"
    log.write_text(caplog.text)
    health = checker()
    monkeypatch.setattr(health, "_BUY_TOKEN_LOG_PATH", log)
    monkeypatch.setattr(health, "_BUY_TOKEN_STATE_PATH", tmp_path / "offsets.json")
    monkeypatch.setenv("FLEET_HEALTH_OUT", str(tmp_path / "state"))
    registered = [s for s in health.RUNTIME_CHECKS if s.check is health.check_buy_submission_commit_failures]
    assert len(registered) == 1 and registered[0].alert_class == health.FLEET_RUNTIME
    monkeypatch.setattr(health, "RUNTIME_CHECKS", tuple(registered))
    assert health.main(runtime_only=True) == 2
    output = capsys.readouterr().out
    assert "class=FLEET_RUNTIME" in output and "buy_blocked_scopes=schwab:DKI" in output
    # Run the actual shell pager in PM/runtime mode. Its controlled curl fails;
    # delivery is not marked accepted and no HTTP leaves the test process.
    assert _run_wrapper(tmp_path, output, mode="runtime", curl_exit=22).returncode == 0
    assert (tmp_path / "state/paged.active").read_text() == ""
    assert health.main(runtime_only=True) == 2  # Cursor advanced, incident retained.
    retry = capsys.readouterr().out
    assert "bytes_scanned=0" in retry
    assert _run_wrapper(tmp_path, retry, mode="runtime").returncode == 0
    assert _call_log(tmp_path).count("CALL") == 2
    assert "service-runtime:oms:submit-token-commit" in (tmp_path / "state/paged.active").read_text()
    assert calls == []  # Pager failure and acknowledgement cannot resume a BUY.
    assert _run_wrapper(tmp_path, retry, mode="runtime").returncode == 0
    assert _call_log(tmp_path).count("CALL") == 2
    monkeypatch.setattr(guard, "_prepare", prepare)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await guard.submit_order(request())
    with log.open("a") as stream:
        stream.write(caplog.text)
    assert health.main(runtime_only=True) == 0
    recovered = capsys.readouterr().out
    assert _run_wrapper(tmp_path, recovered, mode="runtime", check_exit=0).returncode == 0
    with sessions() as session:
        assert session.scalar(select(journal.BuySubmissionToken)).state == "reported_ambiguous"


@pytest.mark.parametrize("case", ["missing_log", "bad_state", "partial_marker", "large_backlog"])
def test_unreadable_or_unscanned_operational_evidence_never_reports_green(tmp_path, case):
    health = checker()
    log, state = tmp_path / "oms.log", tmp_path / "state.json"
    if case != "missing_log":
        log.write_text("[OMS-SUBMIT-TOKEN] sym=DKI acct=schwab result=commit_failed buy_blocked=1"
                       if case == "partial_marker" else "healthy\n")
    if case == "bad_state":
        state.write_text("not JSON")
    if case == "large_backlog":
        log.write_text("healthy\n" * 150_000)
    rows = health.check_buy_submission_commit_failures(state_path=state, oms_log=log)
    assert rows[0][0] == "RED"


def test_log_rotation_cannot_erase_an_active_commit_failure(tmp_path):
    health = checker()
    log, state = tmp_path / "oms.log", tmp_path / "state.json"
    log.write_text("[OMS-SUBMIT-TOKEN] sym=DKI acct=schwab result=commit_failed buy_blocked=1\n")
    assert health.check_buy_submission_commit_failures(state_path=state, oms_log=log)[0][0] == "RED"
    log.rename(tmp_path / "oms.old")
    log.write_text("healthy\n")
    assert health.check_buy_submission_commit_failures(state_path=state, oms_log=log)[0][0] == "RED"


def test_recovery_before_first_page_retains_actual_wrapper_delivery_retry(tmp_path, monkeypatch):
    health = checker()
    log, state = tmp_path / "oms.log", tmp_path / "offsets.json"
    monkeypatch.setenv("FLEET_HEALTH_OUT", str(tmp_path / "state"))
    log.write_text("[OMS-SUBMIT-TOKEN] sym=DKI acct=schwab result=commit_failed buy_blocked=1\n"
                   "[OMS-SUBMIT-TOKEN] sym=DKI acct=schwab result=committed buy_blocked=0\n")
    monkeypatch.setattr(health, "_BUY_TOKEN_LOG_PATH", log)
    monkeypatch.setattr(health, "_BUY_TOKEN_STATE_PATH", state)
    spec = next(s for s in health.RUNTIME_CHECKS if s.check is health.check_buy_submission_commit_failures)
    monkeypatch.setattr(health, "RUNTIME_CHECKS", (spec,))
    def run():
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = health.main(runtime_only=True)
        return code, output.getvalue()
    code, output = run()
    assert code == 2 and "buy_blocked_scopes=-" in output
    assert _run_wrapper(tmp_path, output, mode="runtime", curl_exit=22).returncode == 0
    code, retry = run()
    assert code == 2 and "notification_pending=1" in retry
    assert _run_wrapper(tmp_path, retry, mode="runtime").returncode == 0
    assert _call_log(tmp_path).count("CALL") == 2
    code, recovered = run()
    assert code == 0 and "notification_pending=0" in recovered
    assert _run_wrapper(tmp_path, recovered, mode="runtime", check_exit=code).returncode == 0
