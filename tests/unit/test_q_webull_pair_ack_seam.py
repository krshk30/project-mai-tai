"""Q: real pair adapter -> real strict ATR attach; SDK placement boundary stubbed.

Owned DB/position/cancel fixtures are synthetic. No historical YMAT attribution.
"""
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.webull import WebullAccountConfig
from project_mai_tai.db.models import BrokerOrder, OmsManagedPosition, SystemIncident
from project_mai_tai.oms import webull_attach_riskoff as wb
from tests.unit.test_webull_attach_report_seam import _adapter, _reg_sdk
from tests.unit.test_wbattach1_riskoff import attach, closes, make_case as make_case, restarted


def install_real_pair_adapter(case, monkeypatch, *, body=None, status=200, failure=None):
    """No substitute ExecutionReport: production submit builds every pair reply."""
    service, factory, broker, entry_id, _ = case
    placed, reports = [], []

    class PairSdk:
        def __init__(self, client):
            pass

        def place_order(self, account_id, legs, client_combo_order_id=None):
            with factory() as session:
                entry = session.get(BrokerOrder, entry_id)
                assert entry.payload[wb.KEY]["attempts"][-1]["base"] == client_combo_order_id
            placed.append((account_id, legs, client_combo_order_id))
            if failure is not None:
                raise failure
            return SimpleNamespace(body=body, status_code=status)

    _reg_sdk(monkeypatch, PairSdk)
    adapter = _adapter()
    adapter.accounts_by_name = {broker.account: WebullAccountConfig(account_id="LOCAL-ACCOUNT")}

    async def submit(request):
        assert request.metadata.get("webull_exit_only_pair") == "true", "no software SELL may escape UNKNOWN"
        broker.requests.append(request)
        broker.base = request.client_order_id
        result = await adapter.submit_order(request)
        reports.extend(result)
        return result

    monkeypatch.setattr(broker, "submit_order", submit)
    return SimpleNamespace(placed=placed, reports=reports)


def capture_owner_logs(service, monkeypatch, caplog):
    monkeypatch.setattr(service.logger, "handlers", [*service.logger.handlers, caplog.handler])
    caplog.set_level(logging.INFO, logger=service.logger.name)


@pytest.mark.asyncio
async def test_real_sdk_pair_ack_is_accepted_by_strict_atr_owner_without_cancel_or_second_writer(make_case, monkeypatch):
    case = make_case()
    wire = install_real_pair_adapter(case, monkeypatch, body={"combo_order_id": "LOCAL-COMBO-777"})
    assert await attach(case) is True
    service, factory, broker, entry_id, _ = case
    assert len(wire.placed) == len(wire.reports) == len(broker.requests) == 1
    report = wire.reports[0]
    assert (report.event_type, report.origin, report.intent_type) == ("accepted", "broker", "close")
    assert report.broker_order_id == "LOCAL-COMBO-777" and report.filled_quantity == 0
    assert report.client_order_id == wire.placed[0][2] == broker.base
    with factory() as session:
        entry = session.get(BrokerOrder, entry_id)
        state = entry.payload[wb.KEY]
        assert len(state["attempts"]) == 1 and state["attempts"][0]["phase"] == "accepted"
        assert state["attempts"][0]["cancel_requested"] is False
        assert not state.get("exit_reason") and not state.get("exit_phase")
        assert entry.payload["webull_protect_base_client_order_id"] == broker.base
        assert session.scalars(select(SystemIncident)).all() == []
    assert service._webull_protect_base[(broker.account, "ABC")] == broker.base
    assert await attach(case) is True  # Same accepted journal, no second pair.
    assert len(wire.placed) == 1 and broker.cancelled == [] and closes(broker) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, {}, {"combo_order_id": None}, {"combo_order_id": ""},
    {"combo_order_id": " "}, {"combo_order_id": " LOCAL-ID "}, {"combo_order_id": 7},
    {"combo_order_id": False}, {"combo_order_id": []}, {"combo_order_id": {}}])
async def test_missing_or_invalid_sdk_combo_never_becomes_broker_accepted(make_case, monkeypatch, body):
    case = make_case()
    case[2].child_unknown = True
    wire = install_real_pair_adapter(case, monkeypatch, body=body)
    assert await attach(case) is False
    assert len(wire.placed) == len(wire.reports) == 1
    assert wire.reports[0].origin == "unknown" and wire.reports[0].broker_order_id is None
    assert wire.reports[0].intent_type == "close"
    with case[1]() as session:
        state = session.get(BrokerOrder, case[3]).payload[wb.KEY]
        assert state["attempts"][0]["phase"] == "unknown"
        assert not state.get("exit_reason") and not state.get("exit_phase")
        assert state["attempts"][0]["cancel_requested"] is False
    assert case[2].cancelled == [] and closes(case[2]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["transport", "broker_exception", "http_failure"])
async def test_failed_sdk_pair_ack_preserves_unknown_or_real_broker_refusal(make_case, monkeypatch, mode):
    case = make_case()
    case[2].child_unknown = True

    class BrokerRefusal(Exception):
        http_status = 417
        error_code = "CONTROLLED_PAIR_REFUSAL"

    failure = TimeoutError("CONTROLLED lost ACK") if mode == "transport" else (
        BrokerRefusal("CONTROLLED refusal") if mode == "broker_exception" else None)
    wire = install_real_pair_adapter(case, monkeypatch, body={"combo_order_id": "MUST-NOT-BE-ACCEPTED"},
        status=503 if mode == "http_failure" else 200, failure=failure)
    assert await attach(case) is False
    assert len(wire.placed) == len(wire.reports) == 1
    report = wire.reports[0]
    assert report.event_type == "rejected"
    assert report.origin == ("unknown" if mode == "transport" else "broker")
    assert not report.broker_order_id
    with case[1]() as session:
        state = session.get(BrokerOrder, case[3]).payload[wb.KEY]
        assert state["attempts"][0]["phase"] == "unknown"
        assert not state.get("exit_phase")
    assert len(case[2].cancelled) == (0 if mode == "transport" else 1) and closes(case[2]) == []


@pytest.mark.asyncio
async def test_known_cancel_then_unknown_read_logs_owner_and_restart_never_recancels(make_case, monkeypatch, caplog):
    case = make_case()
    service, factory, broker, entry_id, _ = case
    capture_owner_logs(service, monkeypatch, caplog)
    wire = install_real_pair_adapter(case, monkeypatch, body={"combo_order_id": "LOCAL-COMBO-777"})
    assert await attach(case) is True
    broker.child_unknown = True
    cancel = broker.cancel_exit_pair

    async def accepted_cancel(**binding):
        with factory() as session:
            state = session.get(BrokerOrder, entry_id).payload[wb.KEY]
            assert state["attempts"][0]["cancel_requested"] is True
        await cancel(**binding)
        return [broker.report(wb.WebullBrokerAdapter._combo_leg_coid(broker.base, suffix)) for suffix in ("T", "S")]

    monkeypatch.setattr(broker, "cancel_exit_pair", accepted_cancel)
    assert await wb.request_atr_exit(service, broker.account, "ABC", "owned-entry", "CONTROLLED_RISK_OFF") is False
    with factory() as session:
        state = session.get(BrokerOrder, entry_id).payload[wb.KEY]
        assert state["attempts"][0]["cancel_requested"] is True
        assert state["attempts"][0]["phase"] == "accepted" and not state.get("exit_phase")
        managed = session.scalar(select(OmsManagedPosition))
        assert managed.status == "open" and managed.current_quantity == 10
    messages = [r.getMessage() for r in caplog.records if "[WBATTACH1-CANCEL]" in r.getMessage()]
    assert any("action=request terminal=UNKNOWN" in message for message in messages)
    assert any("action=returned terminal=UNPROVEN" in message and "/cancelled/broker/confirmed" in message for message in messages)
    assert any("action=confirmation_read outcome=unanswerable" in message for message in messages)
    assert any("action=blocked terminal=UNKNOWN sell=0" in message for message in messages)
    for message in messages:
        assert f"account={broker.account}" in message and "symbol=ABC" in message
        assert "entry=owned-entry" in message and f"entry_id={entry_id}" in message
        assert f"base={broker.base}" in message and "reason=CONTROLLED_RISK_OFF" in message
        assert "attempt=1/1" in message and "actor=ATR_OWNER" in message and "call=" in message
    resumed = restarted(case)
    capture_owner_logs(resumed[0], monkeypatch, caplog)
    assert await wb.request_atr_exit(resumed[0], broker.account, "ABC", "owned-entry", "CONTROLLED_RISK_OFF") is False
    assert len(broker.cancelled) == 1 and len(wire.placed) == 1 and closes(broker) == []
    assert any("action=not_resent prior_cancel_requested=true terminal=UNKNOWN" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["cancel", "confirmation"])
async def test_cancel_or_confirmation_exception_stays_unknown_without_retry(make_case, monkeypatch, caplog, point):
    case = make_case()
    service, factory, broker, entry_id, _ = case
    capture_owner_logs(service, monkeypatch, caplog)
    install_real_pair_adapter(case, monkeypatch, body={"combo_order_id": "LOCAL-COMBO-777"})
    assert await attach(case) is True

    async def unknown(**binding):
        raise TimeoutError("CONTROLLED unreadable response")

    monkeypatch.setattr(broker, "cancel_exit_pair" if point == "cancel" else "confirm_exit_pair_terminal", unknown)
    assert await wb.request_atr_exit(service, broker.account, "ABC", "owned-entry", "CONTROLLED_RISK_OFF") is False
    with factory() as session:
        state = session.get(BrokerOrder, entry_id).payload[wb.KEY]
        assert state["attempts"][0]["cancel_requested"] is True and not state.get("exit_phase")
    action = "call_unknown" if point == "cancel" else "confirm_unknown"
    assert any(f"action={action} terminal=UNKNOWN failure=TimeoutError" in r.getMessage() for r in caplog.records)
    assert closes(broker) == []
