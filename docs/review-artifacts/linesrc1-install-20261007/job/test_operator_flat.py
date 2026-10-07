"""[codex] Controlled zero-session-count cases; no production classification claim."""
from copy import deepcopy
from datetime import timedelta
from datetime import datetime
import asyncio
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import operator_flat_policy as o
import release_policy as p
import runner
import strict_flat_readonly as flat
from test_runner import NOW


def evidence(accounts=o.ACCOUNTS):
    stamp=NOW.isoformat()
    result=dict(broker_holdings=[],account_rows=[],managed_rows=[],virtual_rows=[],working_orders=[],inflight_intents=[],
        as_of_et=stamp,sql_snapshot_at_utc=stamp,proof_completed_at_utc=stamp,fill_session_start_et=o.session_start(NOW).isoformat(),
        direct_read_started_at={name:stamp for name in o.ACCOUNTS},schwab_identity_bound=True,webull_identity_bound=True,
        account_stamps=[dict(account=name,updated_at=stamp) for name in o.ACCOUNTS],
        session_order_census_complete=True,session_fill_census_complete=True,session_order_counts=[],fill_balances=[],net_bot_fills=[])
    findings=[]
    for account in accounts:
        result["broker_holdings"].append([account,"IPDN","1000","0"] if account==o.ACCOUNTS[0] else [account,"IPDN","1000"])
        result["account_rows"].append(dict(account=account,symbol="IPDN",quantity="1000",updated_at=stamp,source_updated_at=stamp))
        payload=dict(account_name=account,fingerprint="position-quantity:"+account+":IPDN",direction="broker_only_manual",
            ownership="manual_not_ours",title="position present at broker with no matching fill balance of ours for IPDN - not ours, taking no action",
            account_quantity="1000",quantity_delta="1000",fill_delta="1000",virtual_quantity="0",managed_quantity="0",our_quantity="0",net_fill_balance="0")
        findings.append(dict(symbol="IPDN",finding_type="position_quantity_mismatch",severity="info",payload=payload))
    summary=dict(checked_at=stamp,total_findings=len(findings),critical_findings=0,warning_findings=0,info_findings=len(findings),cutover_confidence="full")
    run=dict(status="completed",completed_at=stamp,summary=summary)
    details=dict(total_findings=str(len(findings)),critical_findings="0",run_status="completed",cutover_confidence="full")
    heartbeat=dict(status="healthy",observed_at=stamp,payload=dict(details=details))
    overview=dict(counts=dict(open_account_positions=len(accounts)),reconciliation=dict(latest_run=deepcopy(run),
        findings=[dict(symbol=f["symbol"],finding_type=f["finding_type"],severity=f["severity"],title=f["payload"]["title"]) for f in findings]),
        services=[dict(service_name="reconciler",effective_status="healthy",status="healthy",observed_at_raw=stamp,details=details)])
    result["overview_sql_run"]=deepcopy(run); result["overview_sql_findings"]=deepcopy(findings)
    return result,findings,overview,run,heartbeat


@pytest.mark.parametrize("accounts",[(o.ACCOUNTS[0],),(o.ACCOUNTS[1],),o.ACCOUNTS])
def test_exact_zero_orders_AND_fills_accounts_independent_matching_only(accounts):
    result,findings,overview,run,heartbeat=evidence(accounts)
    proofs=o.prove(result,findings,NOW)
    assert len(proofs)==len(accounts) and all(row["session_bot_orders"]==row["session_bot_fills"]==0 for row in proofs)
    adjusted,audit=flat.standing_allowance(result,overview,run,findings,heartbeat,NOW)
    assert adjusted["counts"]["open_account_positions"]==0
    assert overview["counts"]["open_account_positions"]==len(accounts)
    assert result["broker_holdings"] and len(result["operator_only_holdings"])==len(accounts)
    assert sum("[OPERATOR-ONLY]" in line for line in audit)==len(accounts)


@pytest.mark.parametrize("account",o.ACCOUNTS)
@pytest.mark.parametrize("kind",["rejected_order","filled_order","aborted_order","netzero_fills","zeroqty_fill","SELL_fill"])
def test_any_session_bot_record_blocks_even_terminal_or_net_zero(account,kind):
    result,findings,*_=evidence((account,))
    if "order" in kind:
        result["session_order_counts"]=[dict(account=account,symbol="IPDN",total=1,status=kind)]
    else:
        result["fill_balances"]=[dict(account=account,symbol="IPDN",total=2 if kind=="netzero_fills" else 1,
                                     net=0,known=2,buy_quantity=127,sell_quantity=127)]
    with pytest.raises(ValueError,match="session bot order/fill"): o.prove(result,findings,NOW)


@pytest.mark.parametrize("damage",["stale_direct","stale_db","stale_row","stale_source","wrong_session","unknown_orders","unknown_fills",
    "foreign_account","missing_account","different_quantity","duplicate_holding","duplicate_count","unownedSELL","unrelated_finding"])
def test_unknown_stale_identity_unownedSELL_and_other_findings_not_waived(damage):
    result,findings,overview,run,heartbeat=evidence((o.ACCOUNTS[0],))
    old=(NOW-timedelta(seconds=121)).isoformat()
    if damage=="stale_direct": result["direct_read_started_at"][o.ACCOUNTS[0]]=old
    elif damage=="stale_db": result["sql_snapshot_at_utc"]=old
    elif damage=="stale_row": result["account_rows"][0]["updated_at"]=old
    elif damage=="stale_source": result["account_rows"][0]["source_updated_at"]=old
    elif damage=="wrong_session": result["fill_session_start_et"]=old
    elif damage=="unknown_orders": result["session_order_census_complete"]=False
    elif damage=="unknown_fills": result["session_fill_census_complete"]=False
    elif damage=="foreign_account": result["broker_holdings"][0][0]="foreign"
    elif damage=="missing_account": result["account_stamps"].pop()
    elif damage=="different_quantity": result["account_rows"][0]["quantity"]=999
    elif damage=="duplicate_holding": result["broker_holdings"]*=2
    elif damage=="duplicate_count": result["session_order_counts"]=[dict(account=o.ACCOUNTS[1],symbol="MI",total=1)]*2
    elif damage=="unownedSELL": findings[0]["finding_type"]="unowned_sell"; findings[0]["severity"]="critical"
    elif damage=="unrelated_finding": findings.append(dict(symbol="BAD",finding_type="unowned_sell",severity="critical",payload=dict(account_name=o.ACCOUNTS[0])))
    else: findings=[]
    with pytest.raises((ValueError,KeyError,TypeError)):
        flat.standing_allowance(result,overview,run,findings,heartbeat,NOW)


@pytest.mark.parametrize("key",["managed_rows","virtual_rows","working_orders","inflight_intents"])
def test_every_open_guard_blocks_even_zero_quantity_or_pending_alias(key):
    result,findings,*_=evidence()
    result[key]=[dict(quantity=0,status="submitting",side="SELL")]
    with pytest.raises(ValueError): o.prove(result,findings,NOW)


def test_runner_flat_retains_raw_operator_holding_but_requires_bot_zero_and_complete_fields(tmp_path):
    fx=runner.Real(tmp_path,{},tmp_path)
    result,findings,*_=evidence()
    result.update(rc=0,blockers=[],bot_broker_holdings=[],bot_account_rows=[],operator_only_holdings=o.prove(result,findings,NOW))
    fx.reader=Mock(return_value=json.dumps(result).encode())
    assert fx.flat()["broker_holdings"]
    result["bot_broker_holdings"]=result["broker_holdings"]
    fx.reader=Mock(return_value=json.dumps(result).encode())
    with pytest.raises(p.Stop): fx.flat()


@pytest.mark.parametrize("accounts",[(o.ACCOUNTS[0],),(o.ACCOUNTS[1],),o.ACCOUNTS])
def test_operator_absent_reconciliation_finding_still_admitted_not_dependent_on_poller(accounts):
    result,findings,overview,run,heartbeat=evidence(accounts)
    findings=[]; result["overview_sql_findings"]=[]; overview["reconciliation"]["findings"]=[]
    for state in (run,result["overview_sql_run"],overview["reconciliation"]["latest_run"]):
        state["summary"].update(total_findings=0,info_findings=0)
    heartbeat["payload"]["details"]["total_findings"]="0"
    proofs=o.prove(result,findings,NOW)
    assert all(not row["matching_finding_present"] for row in proofs)
    adjusted,_=flat.standing_allowance(result,overview,run,findings,heartbeat,NOW)
    assert adjusted["counts"]["open_account_positions"]==0
    assert adjusted["reconciliation"]["latest_run"]["summary"]["total_findings"]==0


@pytest.mark.parametrize("kind",["order","netzero_fills","managed","virtual","working","intent"])
def test_complete_helper_measured_work_is_rc1_typed_wait_not_unknown_retry(kind):
    result,findings,overview,run,heartbeat=evidence((o.ACCOUNTS[0],))
    key=dict(account=o.ACCOUNTS[0],symbol="IPDN")
    if kind=="order": result["session_order_counts"]=[dict(**key,total=1,status="rejected")]
    elif kind=="netzero_fills": result["fill_balances"]=[dict(**key,total=2,known=2,net=0,buy_quantity=127,sell_quantity=127)]
    else:
        labels=dict(managed="managed_rows",virtual="virtual_rows",working="working_orders",intent="inflight_intents")
        result[labels[kind]]=[dict(**key,quantity=0,status="pending",side="buy")]
    reply=flat.known_work_wait(result,overview,run,findings,heartbeat,NOW,None)
    assert reply["rc"]==1 and reply["waiting_kind"]=="FRESH_KNOWN_BOT_WORK"
    assert reply["projection_is_wait_only_never_GO"] and reply["remaining_general_failures"]==[]
    assert result["broker_holdings"] and "operator_only_holdings" not in result


def test_normal_owned_bot_holding_waits_without_operator_exemption():
    result,_,overview,run,heartbeat=evidence((o.ACCOUNTS[0],))
    findings=[]; result["overview_sql_findings"]=[]; overview["reconciliation"]["findings"]=[]
    for state in (run,result["overview_sql_run"],overview["reconciliation"]["latest_run"]):
        state["summary"].update(total_findings=0,info_findings=0)
    heartbeat["payload"]["details"]["total_findings"]="0"
    result["fill_balances"]=[dict(account=o.ACCOUNTS[0],symbol="IPDN",total=1,known=1,net=1000,buy_quantity=1000,sell_quantity=0)]
    result["net_bot_fills"]=deepcopy(result["fill_balances"])
    value=flat.known_work_wait(result,overview,run,findings,heartbeat,NOW,None)
    assert value["rc"]==1 and value["blockers"]==["bot_positions"]


def test_measured_wait_stale_and_unrelated_findings_cannot_become_PENDING():
    result,findings,overview,run,heartbeat=evidence()
    result["working_orders"]=[dict(account=o.ACCOUNTS[0],symbol="TEST",status="accepted")]
    findings.append(dict(symbol="BAD",finding_type="unowned_sell",severity="critical",payload=dict(account_name=o.ACCOUNTS[0])))
    with pytest.raises(ValueError): flat.known_work_wait(result,overview,run,findings,heartbeat,NOW,None)
    findings.pop(); result["sql_snapshot_at_utc"]=(NOW-timedelta(seconds=121)).isoformat()
    with pytest.raises(ValueError,match="stale"): flat.known_work_wait(result,overview,run,findings,heartbeat,NOW,None)


@pytest.mark.parametrize("kind", ["operator","operator_no_finding","session_order","netzero_fills","managed",
    "working","pending_sell","stale","unownedSELL","truncated"])
def test_collect_full_read_boundary_return_codes_never_retry_measured_work(monkeypatch,capsys,kind):
    # Local broker/SQL/health boundaries only; the collector and policies are real.
    result,findings,overview,run,heartbeat=evidence((o.ACCOUNTS[0],))
    run["id"]="CONTROLLED_RUN"
    stamp=NOW.isoformat()
    overview["services"][0].update(instance_name="CONTROLLED",raw_status="healthy")
    if kind=="operator_no_finding":
        findings=[]; overview["reconciliation"]["findings"]=[]
        for state in (run,result["overview_sql_run"],overview["reconciliation"]["latest_run"]):
            state["summary"].update(total_findings=0,info_findings=0)
        heartbeat["payload"]["details"]["total_findings"]="0"
    if kind=="session_order": result["session_order_counts"]=[dict(account=o.ACCOUNTS[0],symbol="IPDN",total=1)]
    if kind=="netzero_fills": result["fill_balances"]=[dict(account=o.ACCOUNTS[0],symbol="IPDN",total=2,known=2,net=0,buy_quantity=127,sell_quantity=127)]
    if kind=="managed": result["managed_rows"]=[dict(account=o.ACCOUNTS[0],symbol="IPDN",quantity=0)]
    if kind=="working": result["working_orders"]=[dict(id="controlled",account=o.ACCOUNTS[0],symbol="IPDN",status="accepted")]
    if kind=="pending_sell": result["inflight_intents"]=[dict(id="controlled",account=o.ACCOUNTS[0],symbol="IPDN",status="submitting",side="SELL")]
    if kind=="stale": result["account_rows"][0]["source_updated_at"]=(NOW-timedelta(seconds=121)).isoformat()
    if kind=="unownedSELL": findings[0].update(finding_type="unowned_sell",severity="critical")
    if kind=="truncated": result["session_order_counts"]=[dict(account=o.ACCOUNTS[0],symbol="X",total=1)]*65
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(flat,"datetime",Clock)
    config=SimpleNamespace(schwab_adapter_token_refresh_enabled=False,database_url="CONTROLLED")
    monkeypatch.setattr(flat,"Settings",lambda **kwargs: config)
    requests=[]
    class Schwab:
        accounts_by_name={o.ACCOUNTS[0]:SimpleNamespace(account_hash="HASH")}
        async def _authorized_request_json(self,method,path):
            requests.append((method,path))
            if path.endswith("accountNumbers"): return 200,{},[dict(hashValue="HASH",accountNumber="123")]
            return 200,{},dict(securitiesAccount=dict(accountNumber="123",currentBalances={},positions=[
                dict(instrument=dict(symbol="IPDN"),longQuantity=1000,shortQuantity=0)]))
    monkeypatch.setattr(flat,"SchwabBrokerAdapter",lambda config:Schwab())
    monkeypatch.setattr(flat,"WebullBrokerAdapter",lambda config:object())
    monkeypatch.setattr(flat,"webull_positions",lambda adapter:([],[2]))
    monkeypatch.setattr(flat,"urlopen",lambda *args,**kwargs:BytesIO(json.dumps(overview).encode()))
    queries=[]
    class Reply:
        def __init__(self,value): self.value=value
        def scalar(self): return self.value
        def scalars(self): return self
        def all(self): return self.value
        def mappings(self): return self.value
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execution_options(self,**kwargs):
            assert kwargs==dict(isolation_level="REPEATABLE READ"); return self
        def execute(self,statement,params=None):
            sql=str(statement); queries.append((sql,params))
            if sql=="SELECT now()": return Reply(NOW)
            if sql.startswith("SELECT name FROM broker_accounts"): return Reply(list(o.ACCOUNTS))
            if "FROM reconciliation_runs" in sql: return Reply([run])
            if "FROM reconciliation_findings" in sql: return Reply(findings)
            label=next(key for key,needle in dict(managed_rows="FROM oms_managed_positions",virtual_rows="FROM virtual_positions",
                account_rows="FROM account_positions",account_stamps="LEFT JOIN account_positions",fill_balances="FROM fills",
                session_order_counts="count(*) total FROM broker_orders",working_orders="SELECT b.id,a.name",inflight_intents="SELECT t.id,a.name").items() if needle in sql)
            return Reply(result[label])
    engine=Mock(connect=Mock(return_value=Connection()))
    build=Mock(return_value=engine); monkeypatch.setattr(flat,"build_engine",build)
    monkeypatch.setattr(flat.event,"listens_for",lambda *args,**kwargs:lambda function:function)
    session=Mock(); session.__enter__=Mock(return_value=session); session.__exit__=Mock(return_value=None)
    monkeypatch.setattr(flat,"Session",lambda **kwargs:session)
    health=Mock(return_value=[]); monkeypatch.setattr(flat,"evaluate_live_deploy_preflight",health)
    if kind in {"stale","unownedSELL","truncated"}:
        with pytest.raises(ValueError): asyncio.run(flat.collect("oms"))
    else:
        rc=asyncio.run(flat.collect("oms"))
        raw=capsys.readouterr().out
        value=json.loads(raw[raw.index("{\n"):])
        assert rc==value["rc"]==(0 if kind.startswith("operator") else 1)
        if rc==0: assert value["broker_holdings"] and value["bot_broker_holdings"]==[]
        else:
            assert value["waiting_kind"]=="FRESH_KNOWN_BOT_WORK" and value["projection_is_wait_only_never_GO"]
            assert "operator_only_holdings" not in value
        assert health.call_count>=2
    assert all(method=="GET" for method,path in requests)
    assert engine.dispose.called and build.call_args.kwargs==dict(connect_timeout_s=5,statement_timeout_ms=5000)
    sql=next(sql for sql,params in queries if "count(*) total FROM broker_orders" in sql)
    assert "b.submitted_at IS NULL" in sql and "i.created_at>=:start" in sql and "b.updated_at>=:start" in sql
    assert "status" not in sql and "net" not in sql and "LIMIT 65" in sql
