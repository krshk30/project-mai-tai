"""Controlled October6 authorization policy, never a positive live receipt."""
import copy
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("ipdn_legacy_controls", ROOT / "test_standing_allowance.py")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
gate = base.gate
NOW = datetime(2026, 10, 6, 20, 10, tzinfo=timezone.utc)
base.NOW = NOW


def bind(args, findings):
    base.bind_findings(args, findings)
    critical = sum(row["severity"] == "critical" for row in findings)
    warning = sum(row["severity"] == "warning" for row in findings)
    info = sum(row["severity"] == "info" for row in findings)
    confidence = max(0, 100 - critical * 35 - warning * 10)
    for summary in (args[2]["summary"], args[0]["overview_sql_run"]["summary"]):
        summary.update(critical_findings=critical, warning_findings=warning,
                       info_findings=info, cutover_confidence=confidence)
    args[4]["payload"]["details"].update(critical_findings=str(critical),
                                        cutover_confidence=str(confidence))
    status = "degraded" if critical or warning else "healthy"
    args[4]["status"] = status
    service = next(row for row in args[1]["services"] if row["service_name"] == "reconciler")
    service.update(status=status, effective_status=status)


def controlled_rule_snapshot():
    """Operator input1000; finding fields modeled on the existing source schema."""
    args = base.evidence()
    result = args[0]
    checked = (NOW - timedelta(seconds=10)).isoformat()
    result.update(schwab_identity_bound=True, as_of_et=checked,
                  sql_snapshot_at_utc=checked, proof_completed_at_utc=NOW.isoformat(),
                  fill_session_start_et="2026-10-06T04:00:00-04:00",
                  broker_holdings=[[gate.ACCOUNTS[0], "IPDN", "1000", "0"]],
                  account_rows=[{"account": gate.ACCOUNTS[0], "symbol": "IPDN", "quantity": "1000",
                                 "updated_at": checked, "source_updated_at": checked}],
                  fill_balances=[{"account": gate.ACCOUNTS[0], "symbol": "MI", "net": "180",
                                  "total": 1, "known": 1, "buy_quantity": "180", "sell_quantity": "0"},
                                 {"account": gate.ACCOUNTS[0], "symbol": "IPDN", "net": "0",
                                  "total": 2, "known": 2, "buy_quantity": "127", "sell_quantity": "127"}])
    finding = {"symbol": "IPDN", "finding_type": "position_quantity_mismatch", "severity": "info",
               "payload": {"fingerprint": "position-quantity:live:schwab_1m_v2:IPDN",
                           "account_name": gate.ACCOUNTS[0], "account_quantity": "1000",
                           "virtual_quantity": "0", "managed_quantity": "0", "our_quantity": "0",
                           "quantity_delta": "1000", "net_fill_balance": "0", "fill_delta": "1000",
                           "direction": "broker_only_manual", "ownership": "manual_not_ours",
                           "title": "position present at broker with no matching fill balance of ours for IPDN - not ours, taking no action"}}
    bind(args, [*args[3], finding])
    args[1]["counts"]["open_account_positions"] = 1
    return args


def admit(args, now=NOW):
    return gate.standing_allowance(*args, now)


def test_controlled_operator_rule1000_preserves_roundtrip_not_operator_only():
    args = controlled_rule_snapshot()
    original = copy.deepcopy(args)
    adjusted, audit = admit(args)
    assert args == original
    assert gate.evaluate_live_deploy_preflight(adjusted, service_target="oms", now=NOW) == []
    residual = gate.ipdn_residual(args[0], args[3], NOW)
    assert residual["ownership_basis"] == "dated_operator_decision"
    assert residual["not_operator_only_from_net_zero"] is True
    assert residual["current_bot_buy_quantity"] == residual["current_bot_sell_quantity"] == "127"
    assert residual["current_bot_fill_count"] == "2"
    line = next(line for line in audit if ":IPDN " in line)
    assert "broker_flat=false" in line and "NOT_operator_only_from_net_zero" in line
    assert "source_updated_at=" in line and "date_et=2026-10-06" in line


def test_controlled_ipdn_only_does_not_require_retired_mi_nxl_findings():
    args = controlled_rule_snapshot()
    bind(args, [args[3][-1]])
    args[0]["net_bot_fills"] = []
    args[0]["fill_balances"] = args[0]["fill_balances"][1:]
    adjusted, _ = admit(args)
    assert gate.evaluate_live_deploy_preflight(adjusted, service_target="oms", now=NOW) == []


@pytest.mark.parametrize("side,value", [(0, "live:orb"), (1, "APUS"), (2, "999"),
                                     (2, "1001"), (2, "132"), (2, "-1000"),
                                     (2, "NaN"), (3, "1")])
def test_ipdn_direct_account_symbol_quantity_and_short_are_exact(side, value):
    args = controlled_rule_snapshot()
    args[0]["broker_holdings"][0][side] = value
    with pytest.raises((ValueError, TypeError)):
        admit(args)


@pytest.mark.parametrize("field,value", [("account", "live:orb"), ("symbol", "APUS"),
                                      ("quantity", "999"), ("quantity", "132"),
                                      ("quantity", None)])
def test_ipdn_book_account_symbol_quantity_are_exact(field, value):
    args = controlled_rule_snapshot()
    args[0]["account_rows"][0][field] = value
    with pytest.raises((ValueError, TypeError)):
        admit(args)


@pytest.mark.parametrize("key", ["managed_rows", "virtual_rows", "working_orders", "inflight_intents"])
def test_ipdn_never_admits_bot_books_or_olox_working_inflight(key):
    args = controlled_rule_snapshot()
    args[0][key] = [{"account": "live:orb", "symbol": "OLOX", "quantity": "1"}]
    with pytest.raises(ValueError):
        admit(args)


@pytest.mark.parametrize("day", [-1, 1])
def test_ipdn_rule_expires_outside_authorized_et_date(day):
    with pytest.raises(ValueError, match="date outside"):
        admit(controlled_rule_snapshot(), NOW + timedelta(days=day))


@pytest.mark.parametrize("field", ["updated_at", "source_updated_at"])
@pytest.mark.parametrize("age", [121, -1, None])
def test_ipdn_book_source_and_local_timestamp_must_both_be_fresh(field, age):
    args = controlled_rule_snapshot()
    args[0]["account_rows"][0][field] = None if age is None else (NOW - timedelta(seconds=age)).isoformat()
    with pytest.raises(ValueError):
        admit(args)


@pytest.mark.parametrize("field", ["as_of_et", "sql_snapshot_at_utc", "proof_completed_at_utc"])
def test_ipdn_read_failure_or_stale_snapshot_is_unknown(field):
    args = controlled_rule_snapshot()
    args[0][field] = (NOW - timedelta(seconds=121)).isoformat()
    with pytest.raises(ValueError):
        admit(args)
    args[0][field] = None
    with pytest.raises(ValueError):
        admit(args)


def test_ipdn_identity_failure_never_reuses_cached_book1000():
    args = controlled_rule_snapshot()
    args[0]["schwab_identity_bound"] = False
    with pytest.raises(ValueError, match="identity unproven"):
        admit(args)


@pytest.mark.parametrize("field,value", [("net", "1"), ("net", "-1"), ("known", 1),
                                      ("known", 3), ("net", None), ("buy_quantity", "NaN"),
                                      ("buy_quantity", "128"), ("sell_quantity", "128")])
def test_ipdn_nonzero_bot_fill_unknown_sell_or_inconsistent_totals_block(field, value):
    args = controlled_rule_snapshot()
    args[0]["fill_balances"][1][field] = value
    with pytest.raises((ValueError, TypeError)):
        admit(args)


@pytest.mark.parametrize("net", ["1", "-1", "1000"])
def test_ipdn_coherent_nonzero_bot_fill_still_blocks(net):
    args = controlled_rule_snapshot()
    row = args[0]["fill_balances"][1]
    row.update(net=net, buy_quantity=str(max(int(net), 0)), sell_quantity=str(max(-int(net), 0)))
    args[0]["net_bot_fills"].append({"account": gate.ACCOUNTS[0], "symbol": "IPDN", "net": net})
    with pytest.raises(ValueError, match="current bot fill balance nonzero"):
        gate.ipdn_residual(args[0], args[3], NOW)
    with pytest.raises(ValueError, match="current bot fill balance nonzero"):
        admit(args)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "session", "reported_nonzero"])
def test_ipdn_complete_session_census_required(fault):
    args = controlled_rule_snapshot()
    if fault == "missing":
        args[0].pop("fill_balances")
    elif fault == "duplicate":
        args[0]["fill_balances"].append(copy.deepcopy(args[0]["fill_balances"][1]))
    elif fault == "session":
        args[0]["fill_session_start_et"] = "2026-10-05T04:00:00-04:00"
    else:
        args[0]["net_bot_fills"].append({"account": gate.ACCOUNTS[0], "symbol": "IPDN", "net": "1"})
    with pytest.raises(ValueError):
        admit(args)


@pytest.mark.parametrize("field,value", [("account_name", "live:orb"), ("net_fill_balance", "1000"),
                                      ("account_quantity", "999"), ("managed_quantity", "1"),
                                      ("virtual_quantity", "1"), ("our_quantity", "1"),
                                      ("quantity_delta", "999"), ("fill_delta", "999"),
                                      ("direction", "negative_net_fill_balance"),
                                      ("ownership", "ours_or_conflicting"), ("title", "other")])
def test_ipdn_finding_source_shape_cannot_be_broadened(field, value):
    args = controlled_rule_snapshot()
    finding = copy.deepcopy(args[3][-1])
    finding["payload"][field] = value
    bind(args, [*args[3][:-1], finding])
    with pytest.raises(ValueError):
        admit(args)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "critical", "foreign_info", "olox_warning", "unowned_sell"])
def test_ipdn_never_waives_other_findings_or_unowned_sell(fault):
    args = controlled_rule_snapshot()
    findings = copy.deepcopy(args[3])
    if fault == "missing":
        findings.pop()
    elif fault == "duplicate":
        findings.append(copy.deepcopy(findings[-1]))
    elif fault == "critical":
        findings[-1]["severity"] = "critical"
    else:
        other = copy.deepcopy(findings[-1])
        other.update(symbol="OLOX", severity="warning" if fault == "olox_warning" else "info")
        if fault == "unowned_sell":
            other.update(symbol="IPDN", finding_type="unowned_sell", severity="critical")
        findings.append(other)
    bind(args, findings)
    with pytest.raises(ValueError):
        admit(args)


def test_ipdn_single_unowned_sell_finding_never_uses_residual_allowance():
    args = controlled_rule_snapshot()
    finding = copy.deepcopy(args[3][-1])
    finding["finding_type"] = "unowned_sell"
    bind(args, [*args[3][:-1], finding])
    with pytest.raises(ValueError, match="operator-residual schema"):
        admit(args)


@pytest.mark.parametrize("key", ["broker_holdings", "account_rows"])
def test_any_second_position_voids_dated_residual(key):
    args = controlled_rule_snapshot()
    args[0][key].append(copy.deepcopy(args[0][key][0]))
    with pytest.raises(ValueError):
        admit(args)


@pytest.mark.parametrize("field", ["direction", "ownership", "our_quantity", "quantity_delta", "fill_delta"])
def test_ipdn_cached_sql_source_identity_must_match(field):
    args = controlled_rule_snapshot()
    args[0]["overview_sql_findings"][-1]["payload"][field] = "foreign"
    with pytest.raises(ValueError, match="identities differ"):
        admit(args)


def test_ipdn_does_not_mask_other_overview_positions_or_recent_fill_settlement():
    args = controlled_rule_snapshot()
    args[1]["counts"]["open_account_positions"] = 2
    with pytest.raises(ValueError, match="position count"):
        admit(args)
    args[1]["counts"]["open_account_positions"] = 1
    args[1]["recent_fills"] = [{"filled_at": NOW.isoformat()}]
    adjusted, _ = admit(args)
    assert any("fills were recorded" in line for line in
               gate.evaluate_live_deploy_preflight(adjusted, service_target="oms", now=NOW))


def test_recorded_125925_ipdn132_66_and_olox_census_remains_blocked():
    # Exact blocker subset of raw SHA256 8cd374a1...; not a positive1000 snapshot.
    args = controlled_rule_snapshot()
    result = args[0]
    result["broker_holdings"] = [[gate.ACCOUNTS[0], "IPDN", "132.0", "0.0"]]
    result["managed_rows"] = [{"account": gate.ACCOUNTS[1], "symbol": "IPDN", "quantity": 66, "status": "open"},
                              {"account": gate.ACCOUNTS[0], "symbol": "IPDN", "quantity": 132, "status": "open"}]
    result["virtual_rows"] = [{"account": gate.ACCOUNTS[1], "symbol": "IPDN", "quantity": "66.00000000"},
                              {"account": gate.ACCOUNTS[0], "symbol": "IPDN", "quantity": "132.00000000"}]
    result["account_rows"] = [{"account": gate.ACCOUNTS[1], "symbol": "IPDN", "quantity": "66.00000000",
                               "updated_at": "2026-10-06T16:59:17.046799+00:00",
                               "source_updated_at": "2026-10-06T16:59:16.653368+00:00"},
                              {"account": gate.ACCOUNTS[0], "symbol": "IPDN", "quantity": "132.00000000",
                               "updated_at": "2026-10-06T16:59:17.127722+00:00",
                               "source_updated_at": "2026-10-06T16:59:17.019561+00:00"}]
    result["fill_balances"] = [{"account": gate.ACCOUNTS[0], "symbol": "IPDN", "net": "132.00000000",
                                "total": 5, "known": 5, "buy_quantity": "390.00000000", "sell_quantity": "258.00000000"},
                               {"account": gate.ACCOUNTS[1], "symbol": "IPDN", "net": "66.00000000",
                                "total": 1, "known": 1, "buy_quantity": "66.00000000", "sell_quantity": "0"}]
    result["net_bot_fills"] = copy.deepcopy(result["fill_balances"])
    result["working_orders"] = [{"account": gate.ACCOUNTS[1], "symbol": "OLOX", "status": "accepted"}]
    result["inflight_intents"] = [{"account": gate.ACCOUNTS[1], "symbol": "OLOX", "status": "submitted"}]
    bind(args, args[3][:-1])  # The capture contains no IPDN residual finding.
    with pytest.raises(ValueError, match="managed_rows"):
        admit(args)
    with pytest.raises(ValueError, match="direct holding not exact"):
        gate.ipdn_residual(result, args[3], NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["transport", "http", "identity"])
async def test_collector_direct_read_failure_has_no_cached_fallback(monkeypatch, fault):
    class Adapter:
        accounts_by_name = {gate.ACCOUNTS[0]: SimpleNamespace(account_hash="configured")}

        async def _authorized_request_json(self, method, path):
            assert method == "GET"
            if fault == "transport":
                raise RuntimeError("controlled unreadable broker")
            if fault == "http":
                return 503, {}, {}
            return 200, {}, [{"hashValue": "foreign", "accountNumber": "111"}]

    monkeypatch.setattr(gate, "Settings", lambda **kwargs: SimpleNamespace(schwab_adapter_token_refresh_enabled=False))
    monkeypatch.setattr(gate, "SchwabBrokerAdapter", lambda config: Adapter())
    with pytest.raises((ValueError, RuntimeError)):
        await gate.collect("oms")


@pytest.mark.asyncio
async def test_controlled_collector_preserves_zero_net_activity_and_reports_residual_not_flat(monkeypatch, capsys):
    args = controlled_rule_snapshot()
    result, overview, run, findings, _ = args
    next(row for row in overview["services"] if row["service_name"] == "reconciler")["instance_name"] = "controlled"
    sql, hooks = [], []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    class Reply:
        def __init__(self, rows):
            self.rows = rows

        def mappings(self):
            return self.rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

        def scalar(self):
            return self.rows[0]

    class Connection:
        def __enter__(self):
            for hook in hooks:
                hook(self)
            return self

        def __exit__(self, *args):
            pass

        def execution_options(self, **kwargs):
            assert kwargs == {"isolation_level": "REPEATABLE READ"}
            return self

        def exec_driver_sql(self, query):
            sql.append(query)

        def execute(self, statement, params=None):
            query = str(statement)
            sql.append(query)
            if query == "SELECT now()":
                return Reply([NOW])
            if query.startswith("SELECT name FROM broker_accounts"):
                return Reply(list(gate.ACCOUNTS))
            if "FROM reconciliation_runs" in query:
                return Reply([{**copy.deepcopy(run), "id": "controlled-run"}])
            if "FROM reconciliation_findings" in query:
                assert params == {"id": "controlled-run"}
                return Reply(copy.deepcopy(findings))
            if "FROM fills f" in query:
                return Reply(copy.deepcopy(result["fill_balances"]))
            if "max(p.updated_at)" in query:
                return Reply(copy.deepcopy(result["account_stamps"]))
            if "FROM account_positions p" in query:
                return Reply(copy.deepcopy(result["account_rows"]))
            assert any(table in query for table in ("oms_managed_positions", "virtual_positions", "broker_orders", "trade_intents"))
            return Reply([])

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            pass

    class Adapter:
        accounts_by_name = {gate.ACCOUNTS[0]: SimpleNamespace(account_hash="configured")}

        async def _authorized_request_json(self, method, path):
            assert method == "GET"
            if path.endswith("accountNumbers"):
                return 200, {}, [{"hashValue": "configured", "accountNumber": "111"}]
            return 200, {}, {"securitiesAccount": {"accountNumber": "111", "currentBalances": {},
                            "positions": [{"instrument": {"symbol": "IPDN"},
                                           "longQuantity": 1000, "shortQuantity": 0}]}}

    class OverviewReply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == 2_000_001
            return json.dumps(overview).encode()

    def register(engine, name):
        assert name == "begin"
        def decorator(function):
            hooks.append(function)
            return function
        return decorator

    monkeypatch.setattr(gate, "datetime", Clock)
    monkeypatch.setattr(gate, "Settings", lambda **kwargs: SimpleNamespace(schwab_adapter_token_refresh_enabled=False, database_url="controlled"))
    monkeypatch.setattr(gate, "SchwabBrokerAdapter", lambda config: Adapter())
    monkeypatch.setattr(gate, "WebullBrokerAdapter", lambda config: object())
    monkeypatch.setattr(gate, "webull_positions", lambda adapter: ([], [32]))
    monkeypatch.setattr(gate, "build_engine", lambda *args, **kwargs: Engine())
    monkeypatch.setattr(gate.event, "listens_for", register)
    monkeypatch.setattr(gate, "urlopen", lambda *args, **kwargs: OverviewReply())
    assert await gate.collect("oms") == 0
    stdout = capsys.readouterr().out
    packet = json.loads(stdout[stdout.index('{\n'):])
    assert packet["blockers"] == []
    assert packet["dated_operator_residual"]["current_bot_fill_count"] == "2"
    assert packet["dated_operator_residual"]["not_operator_only_from_net_zero"] is True
    assert len(packet["fill_balances"]) == 2  # MI plus real-policy roundtrip activity.
    assert packet["broker_holdings"] == [[gate.ACCOUNTS[0], "IPDN", "1000", "0"]]
    assert "broker_flat=false" in stdout and " symbol_broker_flat=fresh" in stdout
    assert "SET TRANSACTION READ ONLY" in sql
    assert all(query.startswith(("SELECT", "SET TRANSACTION READ ONLY")) for query in sql)
