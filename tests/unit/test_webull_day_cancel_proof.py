"""FI2 controlled broker reads; FI3 retained receipts never gain invented books."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.broker_adapters import webull_day_cancel as collector
from project_mai_tai.cancel_terminal_proof import (
    BookOrder, CancelReceipt, CancelScope, CancelTerminalEvidence, CompleteWorkingBook,
    evaluate_cancel_terminal,
)
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, TradeIntent
from project_mai_tai.oms import cancel_terminal as journal
from project_mai_tai.webull_day_cancel_proof import BoundDayCancel, DayCancelAbsence, FlatPositionRead
from tests.integration import test_cancel_terminal_runtime as runtime
from tests.unit import test_buy_submission_journal as tokens
from tests.unit import test_webull_adapter as wb

sessions = tokens.sessions
sdk = runtime.sdk
fake_sdk = wb.fake_sdk
NOW = runtime.NOW
SCOPE = CancelScope("live:orb", "ACC1", "DKI", "exact-coid", "cancel-event")
REASON = "Webull order rejected: ORDER_CAN_NOT_BE_CANCEL ORDER_CAN_NOT_BE_CANCEL (http 417)"
RECEIPT = CancelReceipt(SCOPE, NOW - 1000, "rejected", "broker_reject", REASON)
BOUND = BoundDayCancel(SCOPE, "WB-exact", NOW - 60_000, "day", "buy",
                       "ORDER_CAN_NOT_BE_CANCEL", "417", REASON)
BOOK = CompleteWorkingBook(SCOPE.account_name, SCOPE.account_id, NOW - 500, NOW - 100,
                           True, "all_working", (), "broker")
TODAY = replace(BOOK, coverage="today")
POSITIONS = FlatPositionRead(SCOPE.account_name, SCOPE.account_id, SCOPE.symbol,
                             NOW - 500, NOW - 100, True, True)
WITNESS = DayCancelAbsence(BOUND, TODAY, POSITIONS)
EVIDENCE = CancelTerminalEvidence(RECEIPT, BOOK, source="broker", day_absence=WITNESS)
RECORDED = json.loads((Path(__file__).parents[1] / "fixtures/fi23_retained_cancel_receipts_20261009.json").read_text())["cases"]


def proof(evidence=EVIDENCE, receipt=RECEIPT, now=NOW):
    return evaluate_cancel_terminal(receipt, evidence, now_ms=now)


def test_positive_exact_day_barrier_is_not_an_order_or_fill_receipt():
    assert proof().terminal
    assert proof().reason == "cancel_terminal_webull_day_absence"
    assert EVIDENCE.target_status == "" and EVIDENCE.target_filled_quantity == ""
    assert not proof(replace(EVIDENCE, day_absence=None)).terminal


@pytest.mark.parametrize("row", RECORDED, ids=lambda row: row["symbol"] + ":" + row["account"])
def test_recorded_three_diagnostics_do_not_acquire_invented_terminal_receipts(row):
    # Actual persisted receipt fields only. No fabricated coid, book or position.
    intent = SimpleNamespace(intent_type="cancel", status=row["status"], symbol=row["symbol"],
        updated_at=datetime.fromisoformat(row["updated_at"]), payload={
            "event_id": row["event_id"], "refusal_origin": row["refusal_origin"],
            "refusal_code": row["refusal_code"], "metadata": {
                "target_client_order_id": row["target_client_order_id"],
                "client_order_id": row["client_order_id"], "fanout_segment_id": row["segment"],
                "clearwait_removal_token": row["token"]}})
    account = SimpleNamespace(name=row["account"], external_account_id=None)
    assert journal.receipt_from_intent(intent, account) is None


def test_before_four_calendar_today_cannot_prove_previous_session():
    now = int(datetime(2026, 10, 9, 7, 0, tzinfo=UTC).timestamp() * 1000)
    receipt = replace(RECEIPT, observed_at_ms=now - 1000)
    bound = replace(BOUND, submitted_at_ms=now - 60_000)
    evidence = replace(EVIDENCE, receipt=receipt, day_absence=replace(WITNESS, bound=bound))
    assert not proof(evidence, receipt, now).terminal


@pytest.mark.parametrize("field", ["today", "positions"])
def test_old_but_post_cancel_snapshot_is_independently_stale(field):
    receipt = replace(RECEIPT, observed_at_ms=NOW - 20_000)
    stale = replace(getattr(WITNESS, field), started_at_ms=NOW - 15_001)
    witness = replace(WITNESS, **{field: stale})
    assert not proof(replace(EVIDENCE, receipt=receipt, day_absence=witness), receipt).terminal


@pytest.mark.parametrize("status", [None, [], {}, True])
def test_unreadable_today_status_is_unknown_not_an_exception(status):
    malformed = replace(TODAY, orders=(BookOrder("another", "OTHER", status),))
    assert not proof(replace(EVIDENCE, day_absence=replace(WITNESS, today=malformed))).terminal


@pytest.mark.parametrize("changes", [
    {"scope": replace(SCOPE, client_order_id="foreign")}, {"broker_order_id": ""},
    {"submitted_at_ms": 0}, {"submitted_at_ms": None}, {"submitted_at_ms": NOW - 86_400_000},
    {"submitted_at_ms": NOW + 1}, {"time_in_force": "gtc"}, {"side": "sell"},
    {"cancel_http_status": "200"}, {"cancel_error_code": "ORDER_NOT_FOUND"},
    {"cancel_reason": "another cancel"},
])
def test_old_gtc_null_identity_or_wrong_refusal_stays_unknown(changes):
    assert not proof(replace(EVIDENCE, day_absence=replace(WITNESS, bound=replace(BOUND, **changes)))).terminal


@pytest.mark.parametrize("field,changes", [
    ("today", {"complete": False}), ("today", {"coverage": "all_working"}),
    ("today", {"source": "cache"}), ("today", {"account_id": "other"}),
    ("today", {"started_at_ms": NOW - 15_001}), ("today", {"started_at_ms": NOW - 1001}),
    ("today", {"finished_at_ms": NOW + 1}), ("positions", {"flat": False}),
    ("positions", {"complete": False}), ("positions", {"source": "cache"}),
    ("positions", {"symbol": "OTHER"}), ("positions", {"account_name": "other"}),
    ("positions", {"started_at_ms": NOW - 15_001}), ("positions", {"finished_at_ms": NOW + 1}),
])
def test_incomplete_stale_foreign_or_nonflat_reads_never_prove(field, changes):
    witness = replace(WITNESS, **{field: replace(getattr(WITNESS, field), **changes)})
    assert not proof(replace(EVIDENCE, day_absence=witness)).terminal


@pytest.mark.parametrize("status", ["working", "pending", "partially_filled", "filled", "cancelled"])
@pytest.mark.parametrize("identity", ["client", "broker"])
def test_today_presence_including_fill_then_flat_is_not_zero_fill_terminal(status, identity):
    row = BookOrder(SCOPE.client_order_id if identity == "client" else "another", SCOPE.symbol,
                    status, "buy", BOUND.broker_order_id if identity == "broker" else "other")
    assert not proof(replace(EVIDENCE, day_absence=replace(WITNESS, today=replace(TODAY, orders=(row,))))).terminal


@pytest.mark.parametrize("account", ["live:orb", "live:schwab_1m_v2"])
@pytest.mark.parametrize("hour", [7, 14])
def test_real_working_buy_unknown_in_pm_and_rth_both_account_scopes(account, hour):
    now = int(datetime(2026, 10, 8, hour + 4, 45, tzinfo=UTC).timestamp() * 1000)
    scope = replace(SCOPE, account_name=account)
    receipt = replace(RECEIPT, scope=scope, observed_at_ms=now - 1000)
    bound = replace(BOUND, scope=scope, submitted_at_ms=now - 60_000)
    book = replace(BOOK, account_name=account, started_at_ms=now - 500, finished_at_ms=now - 100)
    witness = DayCancelAbsence(bound, replace(book, coverage="today"),
        replace(POSITIONS, account_name=account, started_at_ms=now - 500, finished_at_ms=now - 100))
    evidence = replace(EVIDENCE, receipt=receipt, book=book, day_absence=witness)
    assert proof(evidence, receipt, now).terminal
    for coid in (scope.client_order_id, "older-gtc-operator-buy"):
        working = replace(book, orders=(BookOrder(coid, scope.symbol, "working", "buy"),))
        assert not proof(replace(evidence, book=working), receipt, now).terminal


def install_sdk(monkeypatch):
    class Request:
        def __init__(self):
            self.values = {}
        def get_version(self):
            return "v2"
        def get_method(self):
            return "GET"
        def get_action_name(self):
            return self.endpoint
        def __getattr__(self, name):
            if name.startswith("set_"):
                return lambda value: self.values.update({name[4:]: value})
            raise AttributeError(name)
    for module, name, endpoint in (
        ("get_today_orders_request", "TodayOrdersListRequest", "/trade/orders/list-today"),
        ("get_account_positions_request", "AccountPositionsRequest", "/account/positions"),
    ):
        path = "webull.trade.request." + module
        obj = ModuleType(path)
        setattr(obj, name, type(name, (Request,), {"endpoint": endpoint}))
        monkeypatch.setitem(sys.modules, path, obj)


class Client(runtime.Client):
    def __init__(self, today=None, positions=None):
        super().__init__()
        self.today = today or [{"has_next": False, "orders": []}]
        self.positions = positions or [{"has_next": False, "holdings": []}]
    def get_response(self, request):
        if hasattr(request, "kind"):
            return super().get_response(request)
        endpoint = request.get_action_name()
        self.calls.append((endpoint, dict(request.values), threading.get_ident()))
        if self.on_read:
            self.on_read()
        pages = self.today if endpoint.endswith("list-today") else self.positions
        return SimpleNamespace(status_code=200, body=pages.pop(0))


def permit_time(monkeypatch, routed):
    leaf, _ = broker.broker_binding(routed, "live:orb")
    # Advance only the controlled permit clock, not receipt freshness.
    clock = [0.0]
    leaf._query_budget.clock = lambda: clock[0]
    async def cooldown(seconds):
        assert seconds == 2
        clock[0] += 2
    monkeypatch.setattr(broker.asyncio, "sleep", cooldown)


@pytest.mark.asyncio
async def test_actual_offloop_collector_and_serialized_journal_roundtrip(sdk, monkeypatch):
    install_sdk(monkeypatch)
    client = Client()
    routed = runtime.adapter(client)
    permit_time(monkeypatch, routed)
    evidence = await broker.acquire_broker_cancel_evidence(routed, RECEIPT,
        broker_order_id=BOUND.broker_order_id, bound_day_cancel=BOUND)
    assert proof(evidence).terminal
    assert [call[0] for call in client.calls] == ["detail", "open", "/trade/orders/list-today", "/account/positions"]
    assert all(call[2] != threading.get_ident() for call in client.calls)
    decoded = collector.decode_day_absence(json.loads(json.dumps(asdict(evidence.day_absence))))
    assert decoded == evidence.day_absence


@pytest.mark.parametrize("pages", [
    [{"orders": []}], [{"has_next": True, "orders": []}],
    [{"has_next": False, "hasNext": True, "orders": []}],
    [{"has_next": 0, "orders": []}], [{"account_id": "foreign", "has_next": False, "orders": []}],
    [{"has_next": False, "orders": [{"client_order_id": "x", "items": []}]}],
])
def test_today_pagination_and_unknown_rows_fail_closed(sdk, monkeypatch, pages):
    install_sdk(monkeypatch)
    leaf, _ = broker.broker_binding(runtime.adapter(Client(today=pages)), "live:orb")
    with pytest.raises(ValueError):
        collector.acquire_day_absence(leaf, BOUND)


@pytest.mark.asyncio
async def test_exact_db_cancel_audit_binding_and_private_threads(sessions, sdk, monkeypatch):
    install_sdk(monkeypatch)
    client = Client()
    routed = runtime.adapter(client)
    intent_id = runtime.seed(sessions, routed)
    with sessions() as session:
        intent = session.get(TradeIntent, intent_id)
        intent.created_at = datetime.fromtimestamp((NOW - 2000) / 1000, UTC)
        intent.updated_at = datetime.fromtimestamp(RECEIPT.observed_at_ms / 1000, UTC)
        intent.payload = {**intent.payload, "refusal_origin": "broker_reject", "refusal_code": REASON,
            journal.BINDING_KEY: {**intent.payload[journal.BINDING_KEY], "broker_order_id": BOUND.broker_order_id}}
        order = BrokerOrder(strategy_id=intent.strategy_id, broker_account_id=intent.broker_account_id,
            client_order_id=SCOPE.client_order_id, broker_order_id=BOUND.broker_order_id, symbol=SCOPE.symbol,
            side="buy", order_type="limit", time_in_force="day", quantity=Decimal(1), status="accepted",
            submitted_at=datetime.fromtimestamp(BOUND.submitted_at_ms / 1000, UTC))
        session.add(order)
        session.flush()
        session.add(BrokerOrderEvent(order_id=order.id, event_type="rejected", event_source="broker",
            event_at=datetime.fromtimestamp((NOW - 1500) / 1000, UTC), payload={
                "client_order_id": SCOPE.client_order_id, "reason": REASON,
                "metadata": {"webull_error_code": "ORDER_CAN_NOT_BE_CANCEL", "webull_http_status": "417"}}))
        session.commit()
    permit_time(monkeypatch, routed)
    sessions.threads.clear()
    await journal.acquire_cancel_terminal_evidence(sessions, routed, [intent_id])
    assert runtime.read(sessions, intent_id)[1].terminal
    # Consumer read above is synchronous by design; acquisition must use workers.
    assert any(thread != threading.get_ident() for thread in sessions.threads)
    with sessions() as session:
        order = session.scalar(select(BrokerOrder))
        assert order.status == "accepted"  # No synthetic cancellation/fill attribution.
        order.status = "filled"
        session.commit()
        assert not journal.load_cancel_terminal_evidence(session, [session.get(TradeIntent, intent_id)])
    request = journal._read_request(sessions, intent_id, routed)
    assert request.bound_day_cancel is None


@pytest.mark.asyncio
async def test_unexpected_body_warning_has_actual_order_and_both_lookup_ids(fake_sdk, caplog):
    adapter = wb._adapter(wb._FakeClient({"detail": None}))
    adapter._ordinary_order_body = lambda *args, **kwargs: (200, None)
    request = wb._order(metadata={"broker_order_id": "exact-broker-id"})
    with caplog.at_level("WARNING"):
        assert await adapter.fetch_order_update(request) is None
    assert "unexpected body=None" in caplog.text
    for value in ("account=live:orb", "symbol=AAPL", "order=orb-AAPL-open-1",
                  "coid=orb-AAPL-open-1", "lookup_coid=orb-AAPL-open-1", "broker_order_id=exact-broker-id"):
        assert value in caplog.text
