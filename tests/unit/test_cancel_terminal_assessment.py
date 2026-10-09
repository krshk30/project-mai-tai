"""Explicit refresh signals never acquire proof on the quote/exit consumer."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
import threading
from uuid import UUID, uuid4

import pytest

from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.db.models import BrokerAccount, DashboardSnapshot, Strategy, TradeIntent
from project_mai_tai.oms import cancel_terminal_assessment as assessment
from project_mai_tai.oms.service import OmsRiskService
from tests.integration import test_cancel_terminal_runtime as runtime
from tests.unit import test_buy_submission_journal as tokens

sessions = tokens.sessions


def seed(sessions):
    now = datetime.now(UTC)
    at = int(now.timestamp() * 1000)
    raised = now - timedelta(seconds=2)
    updated = now - timedelta(seconds=1)
    token, process = str(uuid4()), str(uuid4())
    request = {"schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": "FLYE",
        "opportunity_id": str(at - 60_000), "token": token,
        "requested_at_ms": str(int(raised.timestamp() * 1000)),
        "account_names": ["schwab", "live:orb"], "active": True, "purpose": "retry_exhausted"}
    receipts = []
    with sessions() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")
        session.add(strategy)
        session.flush()
        for name, provider, external in [("schwab", "schwab", "hash"), ("live:orb", "webull", "ACC1")]:
            account = BrokerAccount(name=name, provider=provider, environment="test", external_account_id=external)
            session.add(account)
            session.flush()
            event_id = str(uuid4())
            row = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id, symbol="FLYE",
                side="buy", intent_type="cancel", quantity=1, reason="barrier", status="rejected",
                created_at=raised, updated_at=updated, payload={"source_service": "schwab-1m-v2",
                    "event_id": event_id, "metadata": {"clearwait_removal_token": token,
                        "clearwait_opportunity_id": request["opportunity_id"],
                        "fanout_segment_id": request["opportunity_id"], "clearwait_buy_only": "true",
                        "clearwait_purpose": "retry_exhausted", "reason": "retry_budget_exhausted",
                        "buy_submission_process_id": process}})
            session.add(row)
            session.flush()
            receipts.append({"intent_id": str(row.id), "event_id": event_id, "account_name": name,
                "provider": provider, "account_id": external, "status": "rejected",
                "created_at": raised.isoformat(), "updated_at": updated.isoformat(),
                "coverage_process_id": process})
        session.add(DashboardSnapshot(snapshot_type="v2_removed_wait", payload=request, created_at=raised))
        session.commit()
    # Routing factories are lazy; use the controlled leaf returned by broker_binding.
    from project_mai_tai.broker_adapters.cancel_terminal import broker_binding
    webull, _ = broker_binding(runtime.adapter(runtime.Client()), "live:orb")
    schwab = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    schwab.accounts_by_name = {"schwab": SchwabAccountConfig(account_hash="hash")}
    adapter = RoutingBrokerAdapter(default_provider="webull",
        provider_by_account={"schwab": "schwab", "live:orb": "webull"},
        factories_by_provider={"schwab": lambda: schwab, "webull": lambda: webull})
    payload = {"event_type": "v2_cancel_terminal_assessment", "schema_version": 1,
        "source_service": "schwab-1m-v2", "assessment_id": str(uuid4()), "assessment_at_ms": at,
        "trigger": "fresh_sell", "request": request, "receipts": receipts}
    return payload, adapter, at


def test_fresh_sell_names_both_exact_committed_receipts_and_original_lower_bound(sessions):
    payload, adapter, at = seed(sessions)
    found = assessment.read_assessment_receipts(sessions, adapter, payload, at)
    assert {(name, start) for name, _id, start in found} == {("schwab", at), ("live:orb", at)}


@pytest.mark.parametrize("field", ["old_clock", "future_clock", "trigger", "source", "token", "generation",
    "inactive", "missing_account", "duplicate_account", "receipt_id", "receipt_revision", "process", "provider"])
def test_changed_or_incomplete_assessment_has_no_read_authority(sessions, field):
    payload, adapter, at = seed(sessions)
    changed = deepcopy(payload)
    if field == "old_clock":
        at += 15_001
    elif field == "future_clock":
        at -= 1
    elif field in {"trigger", "source"}:
        changed["trigger" if field == "trigger" else "source_service"] = "quote_tick"
    elif field in {"token", "generation"}:
        changed["request"]["token" if field == "token" else "opportunity_id"] = "1234"
    elif field == "inactive":
        changed["request"]["active"] = False
    elif field == "missing_account":
        changed["receipts"].pop()
    elif field == "duplicate_account":
        changed["receipts"][1] = changed["receipts"][0]
    else:
        key = {"receipt_id": "intent_id", "receipt_revision": "updated_at",
               "process": "coverage_process_id", "provider": "provider"}[field]
        changed["receipts"][0][key] = str(uuid4())
    assert not assessment.read_assessment_receipts(sessions, adapter, changed, at)


@pytest.mark.parametrize("change", ["request_replaced", "latest_receipt", "receipt_working"])
def test_signal_does_not_adopt_a_new_request_or_a_new_receipt_revision(sessions, change):
    payload, adapter, at = seed(sessions)
    from sqlalchemy import select
    with sessions() as session:
        if change == "request_replaced":
            row = session.scalar(select(DashboardSnapshot))
            row.payload = {**row.payload, "token": "new-token"}
        else:
            row = session.get(TradeIntent, UUID(payload["receipts"][0]["intent_id"]))
            if change == "latest_receipt":
                row.updated_at = datetime.fromtimestamp(at / 1000, UTC)
            else:
                row.status = "pending"
        session.commit()
    assert not assessment.read_assessment_receipts(sessions, adapter, payload, at)


@pytest.mark.asyncio
async def test_source_receiver_returns_while_database_read_stalls_and_deduplicates(monkeypatch):
    service = OmsRiskService.__new__(OmsRiskService)
    service.session_factory = object()
    service.broker_adapter = object()
    service.logger = SimpleNamespace(warning=lambda *a, **k: None)
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    reads, scheduled = [], []
    account_id = uuid4()

    def stalled(*args):
        reads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return (("live:orb", account_id, 123),)

    monkeypatch.setattr("project_mai_tai.oms.service.read_assessment_receipts", stalled)
    monkeypatch.setattr(service, "_schedule_cancel_terminal_evidence",
        lambda name, id_, **kw: scheduled.append((name, id_, kw)))
    payload = {"event_type": "v2_cancel_terminal_assessment", "assessment_id": str(uuid4())}
    try:
        await asyncio.wait_for(service._handle_stream_message({"data": json.dumps(payload)}), .05)
        await asyncio.wait_for(service._handle_stream_message({"data": json.dumps(payload)}), .05)
        assert await asyncio.to_thread(entered.wait, 1)
        assert scheduled == []
    finally:
        release.set()
        await service._drain_cancel_terminal_evidence()
    assert len(reads) == 1 and reads[0] != main_thread
    assert scheduled == [("live:orb", account_id, {"minimum_started_at_ms": 123})]
