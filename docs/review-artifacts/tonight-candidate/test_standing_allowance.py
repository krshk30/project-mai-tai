"""Gate-policy tests using the measured October 5 MI/NXL finding shape."""
import copy
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

spec = importlib.util.spec_from_file_location("flat_gate", Path(__file__).with_name("strict_flat_readonly.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
NOW = datetime(2026, 10, 5, 19, 10, tzinfo=timezone.utc)


def evidence():
    checked = (NOW - timedelta(seconds=10)).isoformat()
    observed = (NOW - timedelta(seconds=9)).isoformat()
    summary = {"checked_at": checked, "accounts_checked": 4, "total_findings": 2,
               "critical_findings": 2, "warning_findings": 0, "info_findings": 0,
               "cutover_confidence": 30}
    findings = [{"symbol": symbol, "finding_type": "position_quantity_mismatch",
                 "severity": "critical", "payload": {
                     "fingerprint": "position-quantity:live:schwab_1m_v2:" + symbol,
                     "account_name": "live:schwab_1m_v2", "net_fill_balance": net,
                     "title": "Our records claim a position missing at the broker for " + symbol,
                     "account_quantity": "0", "virtual_quantity": "0", "managed_quantity": "0"}}
                for symbol, net in (("MI", "180.00000000"), ("NXL", "2.00000000"))]
    details = {"total_findings": "2", "critical_findings": "2", "run_status": "completed",
               "cutover_confidence": "30"}
    services = [{"service_name": name, "status": "healthy", "effective_status": "healthy",
                 "observed_at_raw": observed} for name in gate.evaluate_live_deploy_preflight.__globals__["EXPECTED_SERVICE_NAMES"]]
    reconciler = next(row for row in services if row["service_name"] == "reconciler")
    reconciler.update(status="degraded", effective_status="degraded", details=details)
    overview = {"errors": [], "counts": {"pending_intents": 0, "open_virtual_positions": 0,
                "open_account_positions": 0}, "services": services, "recent_intents": [], "recent_fills": [],
                "reconciliation": {"latest_run": {"status": "completed", "summary": summary},
                    "findings": [{**{key: row[key] for key in ("symbol", "finding_type", "severity")},
                                  "title": row["payload"]["title"]} for row in findings]}}
    result = {key: [] for key in ("broker_holdings", "managed_rows", "virtual_rows", "account_rows",
                                 "working_orders", "inflight_intents")}
    result.update(net_bot_fills=[{"account": "live:schwab_1m_v2", "symbol": "MI", "net": "180"}],
                  direct_read_started_at={name: checked for name in gate.ACCOUNTS},
                  account_stamps=[{"account": name, "updated_at": checked} for name in gate.ACCOUNTS])
    run = {"status": "completed", "completed_at": checked, "summary": summary}
    result["overview_sql_run"] = copy.deepcopy(run)
    result["overview_sql_findings"] = copy.deepcopy(findings)
    heartbeat = {"status": "degraded", "observed_at": observed, "payload": {"details": details}}
    return result, overview, run, findings, heartbeat


def test_exact_measured_two_findings_allow_only_counts_degraded_and_mi_balance():
    args = evidence()
    original = copy.deepcopy(args)
    adjusted, audit = gate.standing_allowance(*args, NOW)
    assert args == original
    assert len(audit) == 3
    assert all("[STANDING-ALLOWANCE]" in row for row in audit)
    failures = gate.evaluate_live_deploy_preflight(args[1], service_target="oms", now=NOW)
    assert len(failures) == 3
    assert gate.evaluate_live_deploy_preflight(adjusted, service_target="oms", now=NOW) == []


def bind_findings(args, findings):
    """Keep secondary evidence coherent so only the targeted guard can refuse."""
    args[3][:] = copy.deepcopy(findings)
    args[0]["overview_sql_findings"] = copy.deepcopy(findings)
    args[1]["reconciliation"]["findings"] = [
        {**{key: row[key] for key in ("symbol", "finding_type", "severity")},
         "title": row["payload"]["title"]} for row in findings]
    for summary in (args[2]["summary"], args[0]["overview_sql_run"]["summary"]):
        summary.update(total_findings=len(findings), critical_findings=len(findings))
    args[4]["payload"]["details"].update(total_findings=str(len(findings)),
                                         critical_findings=str(len(findings)))


def test_duplicate_exact_mi_finding_refused_even_when_all_counts_match():
    args = evidence()
    bind_findings(args, [args[3][0], args[3][0]])
    with pytest.raises(ValueError, match="finding not an exact standing allowance"):
        gate.standing_allowance(*args, NOW)


def test_noncritical_finding_refused_even_when_cached_sql_and_overview_agree():
    args = evidence()
    findings = copy.deepcopy(args[3])
    findings[0]["severity"] = "warning"
    bind_findings(args, findings)
    with pytest.raises(ValueError, match="finding not an exact standing allowance"):
        gate.standing_allowance(*args, NOW)


def test_other_finding_type_refused_even_when_all_identity_sources_agree():
    args = evidence()
    findings = copy.deepcopy(args[3])
    findings[0]["finding_type"] = "unrecorded_exit"
    bind_findings(args, findings)
    with pytest.raises(ValueError, match="finding not an exact standing allowance"):
        gate.standing_allowance(*args, NOW)


def test_current_mi_180_requires_its_matching_finding_not_only_nxl():
    args = evidence()
    bind_findings(args, [args[3][1]])
    with pytest.raises(ValueError, match="current-session net fills outside"):
        gate.standing_allowance(*args, NOW)


def test_other_symbol_180_never_uses_mi_current_session_allowance():
    args = evidence()
    args[0]["net_bot_fills"][0]["symbol"] = "APUS"
    with pytest.raises(ValueError, match="current-session net fills outside"):
        gate.standing_allowance(*args, NOW)


def test_raw_heartbeat_status_must_agree_even_when_overview_is_degraded():
    args = evidence()
    args[4]["status"] = "healthy"
    with pytest.raises(ValueError, match="degradation not attributable"):
        gate.standing_allowance(*args, NOW)


@pytest.mark.parametrize("field,value", [
    ("fingerprint", "position-quantity:live:orb:MI"), ("account_name", "live:orb"),
    ("net_fill_balance", "179"), ("account_quantity", "1"), ("virtual_quantity", "1"),
    ("managed_quantity", "1"), ("net_fill_balance", "NaN"), ("net_fill_balance", None)])
def test_changed_finding_identity_balance_or_exposure_refuses(field, value):
    args = evidence()
    args[3][0]["payload"][field] = value
    args[0]["overview_sql_findings"][0]["payload"][field] = value
    with pytest.raises((ValueError, TypeError)):
        gate.standing_allowance(*args, NOW)


@pytest.mark.parametrize("label", ["broker_holdings", "managed_rows", "virtual_rows", "account_rows",
                                    "working_orders", "inflight_intents"])
def test_any_real_holding_or_open_row_voids_allowance(label):
    args = evidence()
    args[0][label] = [{"account": "live:orb", "symbol": "APUS", "quantity": "1"}]
    with pytest.raises(ValueError):
        gate.standing_allowance(*args, NOW)


def test_third_finding_even_with_matching_summary_blocks():
    args = evidence()
    other = copy.deepcopy(args[3][0])
    other["symbol"] = "APUS"
    args[3].append(other)
    args[2]["summary"].update(total_findings=3, critical_findings=3)
    with pytest.raises(ValueError):
        gate.standing_allowance(*args, NOW)


@pytest.mark.parametrize("source", ["broker", "run", "heartbeat", "account_stamp"])
@pytest.mark.parametrize("age", [121, -1])
def test_stale_or_future_evidence_is_unknown(source, age):
    args = evidence()
    stamp = (NOW - timedelta(seconds=age)).isoformat()
    if source == "broker":
        args[0]["direct_read_started_at"][gate.ACCOUNTS[1]] = stamp
    elif source == "run":
        args[2]["summary"]["checked_at"] = stamp
    elif source == "account_stamp":
        args[0]["account_stamps"][1]["updated_at"] = stamp
    else:
        args[4]["observed_at"] = stamp
    with pytest.raises(ValueError):
        gate.standing_allowance(*args, NOW)


@pytest.mark.parametrize("row", [
    {"account": "live:schwab_1m_v2", "symbol": "MI", "net": "181"},
    {"account": "live:schwab_1m_v2", "symbol": "NXL", "net": "2"},
    {"account": "live:orb", "symbol": "MI", "net": "180"}])
def test_current_session_balance_only_exact_schwab_mi_180(row):
    args = evidence()
    args[0]["net_bot_fills"] = [row]
    with pytest.raises(ValueError):
        gate.standing_allowance(*args, NOW)


def test_reconciler_error_or_count_mismatch_not_whitelisted():
    args = evidence()
    args[4]["payload"]["details"]["error"] = "RedisError"
    with pytest.raises(ValueError):
        gate.standing_allowance(*args, NOW)


def test_overview_cache_may_lag_one_fresh_identical_run_not_changed_or_stale():
    args = copy.deepcopy(evidence())
    args[2]["summary"] = copy.deepcopy(args[2]["summary"])
    args[2]["summary"]["checked_at"] = (NOW - timedelta(seconds=1)).isoformat()
    gate.standing_allowance(*args, NOW)
    args[1]["reconciliation"]["latest_run"]["summary"]["checked_at"] = (NOW - timedelta(seconds=121)).isoformat()
    with pytest.raises(ValueError):
        gate.standing_allowance(*args, NOW)


@pytest.mark.parametrize("field,value", [("title", "foreign cached title"),
                                         ("fingerprint", "position-quantity:live:orb:MI"),
                                         ("net_fill_balance", "179")])
def test_cached_projection_and_full_run_must_match_identity(field, value):
    args = evidence()
    if field == "title":
        args[1]["reconciliation"]["findings"][0][field] = value
    else:
        args[0]["overview_sql_findings"][0]["payload"][field] = value
    with pytest.raises(ValueError):
        gate.standing_allowance(*args, NOW)


@pytest.mark.parametrize("response", [{"positions": []},
    {"securitiesAccount": {"accountNumber": "222", "positions": [], "currentBalances": {}}},
    {"securitiesAccount": {"positions": [], "currentBalances": {}}}])
def test_empty_schwab_positions_require_configured_account_identity(response):
    with pytest.raises(ValueError):
        gate.schwab_holdings(response, "111")


def test_schwab_account_hash_mapping_and_empty_identity_are_both_proven():
    mapping = [{"accountNumber": "111", "hashValue": "configured"},
               {"accountNumber": "222", "hashValue": "foreign"}]
    number = gate.schwab_account_number(mapping, "configured")
    assert number == "111"
    assert gate.schwab_holdings({"securitiesAccount": {
        "accountNumber": number, "currentBalances": {}}}, number) == []
    with pytest.raises(ValueError):
        gate.schwab_account_number(mapping, "missing")
    with pytest.raises(ValueError):
        gate.schwab_account_number(mapping + [mapping[0]], "configured")


def webull_fixture(monkeypatch, bodies):
    class Request:
        def set_account_id(self, value):
            pass

        def set_page_size(self, value):
            assert value == 50

        def set_last_instrument_id(self, value):
            pass

    monkeypatch.setitem(sys.modules, "webull.trade.request.get_account_positions_request",
                        SimpleNamespace(AccountPositionsRequest=Request))
    calls = []
    def response(request):
        calls.append(request)
        return SimpleNamespace(status_code=200, body=bodies[len(calls) - 1])
    adapter = SimpleNamespace(accounts_by_name={gate.ACCOUNTS[1]: SimpleNamespace(account_id="configured")},
        _get_client=lambda: SimpleNamespace(get_response=response), _body=lambda row: row.body,
        _first_str=lambda row, *keys: next((row[key] for key in keys if key in row), None))
    return adapter, calls


@pytest.mark.parametrize("markers", [{}, {"has_next": False, "hasNext": True}, {"has_next": 0}])
def test_webull_ambiguous_completion_never_hides_second_page(monkeypatch, markers):
    zero_page = [{"symbol": "MI", "quantity": "0", "instrument_id": str(i)} for i in range(50)]
    adapter, calls = webull_fixture(monkeypatch, [
        {"holdings": zero_page, **markers},
        {"holdings": [{"symbol": "MI", "quantity": "180"}], "has_next": False}])
    with pytest.raises(ValueError):
        gate.webull_positions(adapter)
    assert len(calls) == 1


def test_webull_explicit_second_page_holding_is_recorded_not_flat(monkeypatch):
    adapter, calls = webull_fixture(monkeypatch, [
        {"holdings": [{"symbol": "MI", "quantity": "0", "instrument_id": "one"}], "has_next": True},
        {"holdings": [{"symbol": "MI", "quantity": "180"}], "has_next": False}])
    holdings, sizes = gate.webull_positions(adapter)
    assert holdings == [[gate.ACCOUNTS[1], "MI", "180"]]
    assert len(calls) == len(sizes) == 2


def test_summary_counts_must_match_exact_finding_rows():
    args = copy.deepcopy(evidence())
    args[2]["summary"] = copy.deepcopy(args[2]["summary"])
    args[2]["summary"]["critical_findings"] = 1
    with pytest.raises(ValueError):
        gate.standing_allowance(*args, NOW)
    args = evidence()
    args[2]["summary"]["total_findings"] = 3
    with pytest.raises(ValueError):
        gate.standing_allowance(*args, NOW)


def test_webull_empty_holdings_never_hides_alternate_position_collection(monkeypatch):
    adapter, calls = webull_fixture(monkeypatch, [{"holdings": [],
        "positions": [{"symbol": "MI", "quantity": "180"}], "has_next": False}])
    with pytest.raises(ValueError, match="holdings/positions conflict"):
        gate.webull_positions(adapter)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["pending", "fill", "stale", "unhealthy", "errors"])
def test_unrelated_general_gate_failure_survives_allowance(failure):
    args = evidence()
    overview = args[1]
    if failure == "pending":
        overview["counts"]["pending_intents"] = 1
    elif failure == "fill":
        overview["recent_fills"] = [{"filled_at": NOW.isoformat()}]
    elif failure in ("stale", "unhealthy"):
        row = next(row for row in overview["services"] if row["service_name"] == "oms-risk")
        if failure == "stale":
            row["observed_at_raw"] = (NOW - timedelta(seconds=121)).isoformat()
        else:
            row["effective_status"] = "degraded"
    else:
        overview["errors"] = ["HTTP failure"]
    adjusted, _ = gate.standing_allowance(*args, NOW)
    assert len(gate.evaluate_live_deploy_preflight(adjusted, service_target="oms", now=NOW)) == 1
