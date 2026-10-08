import copy
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

SPEC = importlib.util.spec_from_file_location("oct8_gate", Path(__file__).with_name("gate_readonly.py"))
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
NOW = datetime(2026, 10, 8, 20, 10, tzinfo=timezone.utc)


def receipt():
    return dict(errors=[], brokers={account: dict(started_at=NOW.isoformat(), identity_bound=True,
        complete=True, holdings=[], working_orders=[]) for account in gate.ACCOUNTS},
        sql=dict(observed_at=NOW.isoformat(), session_start=gate.session_start(NOW).isoformat(),
            complete=True, managed_rows=[], virtual_rows=[], working_orders=[], inflight_intents=[],
            session_order_counts=[], session_fill_counts=[]))


def test_clear_complete_empty_accounts():
    data = receipt()
    assert gate.evaluate(data, NOW) == 0
    assert data["blockers"] == data["unknown"] == []


def test_aixi_135_blocks_even_after_net_zero_bot_roundtrip():
    data = receipt()
    data["brokers"][gate.ACCOUNTS[1]]["holdings"] = [dict(account=gate.ACCOUNTS[1], symbol="AIXI", quantity="135", short="0")]
    data["sql"]["session_order_counts"] = [dict(account=gate.ACCOUNTS[1], symbol="AIXI", total=2)]
    data["sql"]["session_fill_counts"] = [dict(account=gate.ACCOUNTS[1], symbol="AIXI", total=2)]
    assert gate.evaluate(data, NOW) == 1
    assert data["operator_only_holdings"] == []


@pytest.mark.parametrize("source", ["session_order_counts", "session_fill_counts"])
def test_either_bot_record_prevents_operator_allowance(source):
    data = receipt()
    data["brokers"][gate.ACCOUNTS[0]]["holdings"] = [dict(account=gate.ACCOUNTS[0], symbol="MANUAL", quantity="1", short="0")]
    data["sql"][source] = [dict(account=gate.ACCOUNTS[0], symbol="MANUAL", total=1)]
    assert gate.evaluate(data, NOW) == 1


def test_operator_only_requires_zero_orders_and_zero_fills():
    data = receipt()
    data["brokers"][gate.ACCOUNTS[0]]["holdings"] = [dict(account=gate.ACCOUNTS[0], symbol="MANUAL", quantity="1000", short="0")]
    assert gate.evaluate(data, NOW) == 0
    assert data["operator_only_holdings"][0]["session_orders"] == 0
    assert data["operator_only_holdings"][0]["session_fills"] == 0


@pytest.mark.parametrize("label", ["managed_rows", "virtual_rows", "working_orders", "inflight_intents"])
def test_measured_work_blocks(label):
    data = receipt()
    data["sql"][label] = [dict(account=gate.ACCOUNTS[1], symbol="AIXI", quantity="135", status="accepted")]
    assert gate.evaluate(data, NOW) == 1
    assert data["blockers"][0]["kind"] == label


def test_measured_blocker_not_retried_as_unknown():
    data = receipt()
    data["errors"] = ["Schwab unreadable"]
    data["sql"]["managed_rows"] = [dict(account=gate.ACCOUNTS[1], symbol="AIXI", quantity="135")]
    assert gate.evaluate(data, NOW) == 1
    assert data["unknown"] == ["Schwab unreadable"]


@pytest.mark.parametrize("mutation", ["stale", "future", "unbound", "incomplete", "sql_missing", "source_missing", "wrong_session"])
def test_unreadable_is_rc2(mutation):
    data = receipt()
    source = data["brokers"][gate.ACCOUNTS[0]]
    if mutation == "stale":
        source["started_at"] = (NOW - timedelta(seconds=121)).isoformat()
    elif mutation == "future":
        source["started_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif mutation == "unbound":
        source["identity_bound"] = False
    elif mutation == "incomplete":
        source["complete"] = False
    elif mutation == "sql_missing":
        data.pop("sql")
    elif mutation == "source_missing":
        data["brokers"].pop(gate.ACCOUNTS[0])
    else:
        data["sql"]["session_start"] = (NOW - timedelta(days=1)).isoformat()
    assert gate.evaluate(data, NOW) == 2


@pytest.mark.parametrize("status", [None, "mystery", "aborted", "rejected"])
def test_unproven_local_status_is_unknown(status):
    data = receipt()
    data["sql"]["working_orders"] = [dict(account=gate.ACCOUNTS[0], symbol="OLOX", status=status)]
    assert gate.evaluate(data, NOW) == 2


def test_live_broker_order_blocks_with_empty_sql():
    data = receipt()
    data["brokers"][gate.ACCOUNTS[1]]["working_orders"] = [dict(account=gate.ACCOUNTS[1], id="x", status="working")]
    assert gate.evaluate(data, NOW) == 1


@pytest.mark.parametrize("value", ["NaN", "Infinity", None, True, "bad"])
def test_invalid_quantity(value):
    with pytest.raises(ValueError):
        gate.quantity(value)


def test_schwab_identity_mapping():
    assert gate.schwab_account_number([dict(hashValue="hash", accountNumber="123")], "hash") == "123"
    for data in ([], [{"hashValue": "foreign", "accountNumber": "123"}],
                 [dict(hashValue="hash", accountNumber="123")] * 2):
        with pytest.raises(ValueError):
            gate.schwab_account_number(data, "hash")


@pytest.mark.parametrize("body", [{}, {"securitiesAccount": {}},
    {"securitiesAccount": {"accountNumber": "wrong", "currentBalances": {}}},
    {"securitiesAccount": {"accountNumber": "123", "currentBalances": {}, "positions": None}}])
def test_schwab_malformed_not_empty(body):
    with pytest.raises(ValueError):
        gate.schwab_positions(body, "123")


def test_schwab_explicit_empty_envelope():
    assert gate.schwab_positions({"securitiesAccount": {"accountNumber": "123", "currentBalances": {}}}, "123") == []


def test_schwab_child_working_under_filled_parent_blocks():
    rows = [dict(orderId=1, status="FILLED", childOrderStrategies=[dict(orderId=2, status="WORKING")])]
    assert gate.working_rows(rows, gate.ACCOUNTS[0], schwab=True)[0]["id"] == "2"


def test_recorded_webull_items_order_status_is_not_parent_status():
    filled = dict(client_order_id="parent", account_id="account", items=[dict(order_status="FILLED")])
    assert gate.working_rows([filled], gate.ACCOUNTS[1], expected_id="account") == []
    live = dict(filled, items=[dict(order_status="FILLED"), dict(order_status="WORKING")])
    assert gate.working_rows([live], gate.ACCOUNTS[1], expected_id="account") == [
        dict(account=gate.ACCOUNTS[1], id="parent:item:1", status="working")]
    with pytest.raises(ValueError):
        gate.working_rows([dict(filled, items=[])], gate.ACCOUNTS[1], expected_id="account")


@pytest.mark.parametrize("rows", [[{}], [dict(client_order_id="x", status="UNKNOWN")],
    [dict(client_order_id="x", status="FILLED", account_id="wrong")],
    [dict(client_order_id="x", status="FILLED")] * 2])
def test_order_unknown_foreign_duplicate(rows):
    with pytest.raises(ValueError):
        gate.working_rows(rows, gate.ACCOUNTS[1], expected_id="account")


class Request:
    def __init__(self):
        self.values = {}

    def __getattr__(self, key):
        return lambda value: self.values.update({key: value})


class Reader:
    def __init__(self, pages):
        self.pages, self.requests = iter(pages), []

    def read(self, request):
        self.requests.append(request.values)
        return next(self.pages)


def test_webull_pages_complete_and_cursor():
    reader = Reader([dict(holdings=[dict(instrument_id="a", symbol="A", quantity="1")], has_next=True),
                     dict(holdings=[], has_next=False)])
    rows = gate.webull_pages(reader, Request, "account", "positions")
    assert len(rows) == 1
    assert reader.requests[1]["set_last_instrument_id"] == "a"
    assert reader.requests[0]["set_account_id"] == "account"


@pytest.mark.parametrize("page", [dict(orders=[]), dict(orders=[], has_next="false"),
    dict(orders=[], has_next=True), dict(orders=[], has_next=False, account_id="foreign"),
    dict(orders=[], has_next=False, hasNext=True), dict(orders=None, has_next=False)])
def test_webull_malformed_pagination_unknown(page):
    with pytest.raises(ValueError):
        gate.webull_pages(Reader([page]), Request, "account", "orders")


def test_webull_duplicate_cursor_blocks():
    page = dict(orders=[dict(client_order_id="x", status="FILLED")], has_next=True)
    with pytest.raises(ValueError):
        gate.webull_pages(Reader([page, page]), Request, "account", "orders")


def test_spaced_requests_two_seconds_no_cache():
    now, called = [10.0], []
    def sleep(seconds):
        now[0] += seconds
    def response(request):
        called.append(now[0])
        return SimpleNamespace(status_code=200, json=lambda: dict(orders=[], has_next=False))
    client = gate.SpacedClient(SimpleNamespace(get_response=response), lambda: now[0], sleep)
    client.read(Request())
    client.read(Request())
    client.read(Request())
    assert called == [10.0, 12.0, 14.0]


def test_webull_http_failure_is_unknown_not_empty():
    client = gate.SpacedClient(SimpleNamespace(get_response=lambda _: SimpleNamespace(status_code=429)))
    with pytest.raises(ValueError, match="HTTP"):
        client.read(Request())


def test_local_abort_positive_proof():
    row = dict(status="rejected", broker_order_id=None, has_fill=False, intent_bound=True,
               intent_payload=dict(refusal_origin="client_abort", refusal_code="rpg_stale_strategy_authorization"))
    assert gate.local_terminal_proof(row)
    for field, value in (("broker_order_id", "123"), ("has_fill", True), ("intent_bound", False),
                         ("intent_payload", {}), ("status", "working")):
        changed = dict(row, **{field: value})
        assert not gate.local_terminal_proof(changed)


@pytest.mark.asyncio
async def test_schwab_never_authorized_refresh_or_401_retry():
    calls = []
    async def read(method, path, access_token):
        calls.append((method, path))
        return 401, {}, {}
    async def access():
        return "TEST-TOKEN-NOT-PRINTED"
    adapter = SimpleNamespace(_adapter_refresh_enabled=False,
        accounts_by_name={gate.ACCOUNTS[0]: SimpleNamespace(account_hash="hash")},
        _get_access_token=access, _access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        _request_json=read)
    with pytest.raises(ValueError, match="HTTP 401"):
        await gate.schwab_reads(adapter)
    assert calls == [("GET", "/trader/v1/accounts/accountNumbers")]


@pytest.mark.asyncio
async def test_schwab_direct_fake_adapter_complete_known_status_filters():
    filters = []
    async def read(method, path, access_token):
        assert method == "GET"
        if path.endswith("accountNumbers"):
            body = [dict(hashValue="hash", accountNumber="123")]
        elif path.endswith("?fields=positions"):
            body = {"securitiesAccount": {"accountNumber": "123", "currentBalances": {}, "positions": []}}
        else:
            query = parse_qs(urlparse(path).query)
            filters.append(query.get("status", ["SESSION_ALL"])[0])
            assert query["fromEnteredTime"][0].endswith(".000Z")
            assert query["toEnteredTime"][0].endswith(".000Z")
            body = []
        return 200, {}, body
    async def token():
        return "NEVER-PRINT-ME"
    adapter = SimpleNamespace(_adapter_refresh_enabled=False,
        accounts_by_name={gate.ACCOUNTS[0]: SimpleNamespace(account_hash="hash")},
        _get_access_token=token, _access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        _request_json=read, ACCEPTED_STATUSES={"ACCEPTED", "WORKING"}, PARTIAL_FILL_STATUSES={"PARTIAL_FILL"})
    data = await gate.schwab_reads(adapter)
    assert filters == ["ACCEPTED", "WORKING", "SESSION_ALL"]
    assert data["complete"] is True and data["holdings"] == data["working_orders"] == []
    assert "hash" not in json.dumps(data)
    assert "123" not in json.dumps(data)
    assert "NEVER-PRINT-ME" not in json.dumps(data)


def test_live_main_requires_root_and_keeps_json_stdout(monkeypatch, capsys):
    monkeypatch.setattr(gate.os, "geteuid", lambda: 501)
    monkeypatch.setattr(gate.sys, "argv", ["gate_readonly.py"])
    assert gate.main() == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["rc"] == 2
    assert "requires root" in output.err


def test_live_main_root_mock_receipt(monkeypatch, capsys):
    import project_mai_tai.settings as settings
    monkeypatch.setattr(gate.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gate.sys, "argv", ["gate_readonly.py"])
    monkeypatch.setattr(settings, "Settings", lambda **_: SimpleNamespace(schwab_adapter_token_refresh_enabled=False))
    async def collect(_):
        data = receipt()
        gate.evaluate(data, NOW)
        return data
    monkeypatch.setattr(gate, "collect", collect)
    assert gate.main() == 0
    assert json.loads(capsys.readouterr().out)["rc"] == 0


@pytest.mark.parametrize("oversized", [False, True])
def test_sql_repeatable_read_readonly_complete_bound(monkeypatch, oversized):
    import sqlalchemy
    import project_mai_tai.db.session as sessions
    calls = []
    class Result:
        def __init__(self, scalar=None, rows=None):
            self.scalar, self.rows = scalar, rows or []
        def scalar_one(self):
            return self.scalar
        def scalars(self):
            return self.rows
        def mappings(self):
            return self.rows
    class Connection:
        def execution_options(self, **kwargs):
            calls.append(kwargs)
            return self
        def __enter__(self):
            calls.append("SET TRANSACTION READ ONLY")
            return self
        def __exit__(self, *args):
            calls.append("transaction closed")
        def execute(self, query, params=None):
            query = str(query)
            calls.append(query)
            if query == "SHOW transaction_read_only":
                return Result("on")
            if query == "SELECT now()":
                return Result(NOW)
            if query.startswith("SELECT name FROM"):
                return Result(rows=list(gate.ACCOUNTS))
            assert query.endswith("LIMIT 1025")
            assert params["start"] == gate.session_start(NOW).astimezone(timezone.utc)
            return Result(rows=[dict(account=gate.ACCOUNTS[1], symbol="AIXI", quantity=135)] * 1025
                          if oversized and "oms_managed_positions" in query else [])
    connection = Connection()
    engine = SimpleNamespace(connect=lambda: connection, dispose=lambda: calls.append("disposed"))
    def build_engine(url, **kwargs):
        assert kwargs == dict(connect_timeout_s=5, statement_timeout_ms=5000, lock_timeout_ms=500, pool_timeout_s=5)
        return engine
    monkeypatch.setattr(sessions, "build_engine", build_engine)
    monkeypatch.setattr(sqlalchemy.event, "listens_for", lambda *args: lambda f: f)
    if oversized:
        with pytest.raises(ValueError, match="complete proof bound"):
            gate.sql_snapshot(SimpleNamespace(database_url="TEST"), NOW)
    else:
        assert gate.sql_snapshot(SimpleNamespace(database_url="TEST"), NOW)["complete"] is True
    assert dict(isolation_level="REPEATABLE READ") in calls
    assert "SHOW transaction_read_only" in calls
    assert "SET TRANSACTION READ ONLY" in calls
    assert calls[-1] == "disposed"


def test_enabled_token_refresh_refused_before_reads(monkeypatch, capsys):
    import project_mai_tai.settings as settings
    monkeypatch.setattr(gate.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gate.sys, "argv", ["gate_readonly.py"])
    monkeypatch.setattr(settings, "Settings", lambda **_: SimpleNamespace(schwab_adapter_token_refresh_enabled=True))
    monkeypatch.setattr(gate, "collect", lambda _: pytest.fail("read attempted with token refresh enabled"))
    assert gate.main() == 2
    assert json.loads(capsys.readouterr().out)["rc"] == 2


@pytest.mark.asyncio
async def test_collect_exceptions_keep_stdout_receipt_and_stderr(monkeypatch, capsys):
    import project_mai_tai.broker_adapters.schwab as schwab
    import project_mai_tai.broker_adapters.webull as webull
    monkeypatch.setattr(schwab, "SchwabBrokerAdapter", lambda _: None)
    monkeypatch.setattr(webull, "WebullBrokerAdapter", lambda _: None)
    async def failed(_):
        raise RuntimeError("fake source failure")
    monkeypatch.setattr(gate, "schwab_reads", failed)
    monkeypatch.setattr(gate, "webull_reads", lambda _: (_ for _ in ()).throw(RuntimeError("fake Webull failure")))
    monkeypatch.setattr(gate, "sql_snapshot", lambda *args: (_ for _ in ()).throw(RuntimeError("fake SQL failure")))
    data = await gate.collect(None)
    assert data["rc"] == 2
    assert "fake source failure" in capsys.readouterr().err
    assert json.loads(json.dumps(data))["rc"] == 2


def test_sql_queries_have_no_ledger_health_or_token_write():
    for query in gate.QUERIES.values():
        assert query.startswith("SELECT ")
        assert "reconciliation" not in query
        assert "service_heartbeats" not in query


@pytest.mark.parametrize("changed", ["sql", "brokers"])
def test_missing_source_never_clear(changed):
    data = copy.deepcopy(receipt())
    data.pop(changed)
    assert gate.evaluate(data, NOW) == 2
