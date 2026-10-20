"""Actual composed emitter/preparation/validator path, with controlled broker I/O."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, OmsManagedPosition, Strategy, TradeIntent
from project_mai_tai.oms import service as oms, webull_attach_riskoff as wb
from tests.unit.test_wbattach1_riskoff import attach, closes, crossed, make_case as make_case


@pytest.fixture
def manual_continuation_without_auto_recovery(monkeypatch):
    """Counterfactual: inspect one abort, then explicitly invoke the next owner.

    Actual automatic risk-off recovery is covered in the AIXI metadata module.
    Keep this test's original first-call no-wire/abort/next-call assertions.
    """
    scheduled = []

    def record_only(service, decision, account, symbol, **kwargs):
        scheduled.append((account, symbol, kwargs))

    monkeypatch.setattr(oms.OmsRiskService, "_spawn_confirmation_webull_recovery", record_only)
    return scheduled


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [False, True])
@pytest.mark.parametrize("fault", [None, "stale-wire", "lower-wire", "session-ended"])
async def test_actual_composed_first_wire_and_proven_unsent_next_call(
        make_case, monkeypatch, control, fault, manual_continuation_without_auto_recovery):
    case = make_case(control=control)
    service, factory, broker, entry_id, clock = case
    if control:
        with factory() as unit:
            strategy = unit.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
            service._bot_control_runtime.store.seed(strategy.id)
        await service._bot_control_runtime.refresh()
    crossed(case, "stop")
    actual_validator = wb.validate_atr_before_wire
    actual_submit = broker.submit_order
    checked, wires = [], []

    async def validator(actual_service, request, managed_id):
        assert actual_service is service
        with factory() as unit:
            row = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
            assert row is not None and row.status == "pending" and row.submitted_at is None
            assert row.broker_order_id is None and row.payload == request.metadata
            assert unit.get(TradeIntent, row.intent_id).payload["metadata"] == request.metadata
            assert row.payload["oms_managed_row_id"] == managed_id
        checked.append(request.client_order_id)
        if len(checked) == 1:
            if fault == "stale-wire":
                broker.age = 60
            elif fault == "lower-wire":
                broker.total = Decimal(9)
            elif fault == "session-ended":
                clock[0] = clock[0].replace(hour=20)
        await actual_validator(actual_service, request, managed_id)

    async def sender(request):
        if request.reason == wb.REASON:
            assert checked[-1] == request.client_order_id
            if control:
                owner = oms._CONTROL_MANAGED_EMIT.get()
                key = (request.broker_account_name, request.symbol, request.metadata["oms_managed_row_id"])
                assert owner is not None and service._control_managed_exit_claims[key] is owner
                assert service._control_managed_exit_symbol_owners[key[:2]] is owner
            with factory() as unit:
                row = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
                assert row.status == "pending" and row.submitted_at is None and row.broker_order_id is None
            wires.append(request.client_order_id)
        return await actual_submit(request)

    monkeypatch.setattr(wb, "validate_atr_before_wire", validator)
    broker.submit_order = sender
    await attach(case)
    assert len(checked) == 1
    assert len(manual_continuation_without_auto_recovery) == (0 if fault is None else 1)
    with factory() as unit:
        prepared = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == checked[0]))
        if fault is None:
            assert wires == checked and prepared.status == "filled"
        else:
            assert not wires and not closes(broker)
            assert prepared.status == "aborted" and unit.get(TradeIntent, prepared.intent_id).status == "aborted"
            audit = unit.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == prepared.id))
            assert audit.event_source == "client" and audit.payload["proven_not_sent"] is True
            entry = unit.get(BrokerOrder, entry_id)
            journal = entry.payload[wb.KEY]
            assert not journal.get("exit_phase")
            assert journal["unsent_handoffs"][-1]["abort_event_id"] == str(audit.id)
            assert unit.scalar(select(OmsManagedPosition.current_quantity)) == 10
    if fault is not None:
        broker.age, broker.total = 0, Decimal(1010)
        clock[0] = clock[0].replace(hour=14)
        await attach(case)  # A NEW real owner invocation and a NEW committed pending identity.
        assert len(checked) == 2 and checked[0] != checked[1]
        assert wires == [checked[1]] and len(closes(broker)) == 1
    assert not service.__dict__.get("_control_managed_exit_claims")
    assert not service.__dict__.get("_control_managed_exit_symbol_owners")
