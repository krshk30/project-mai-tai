"""Source-only synthetic controls: ACK uncertainty is not an exit trigger."""
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, Fill, OmsManagedPosition, SystemIncident, TradeIntent
from project_mai_tai.oms import service as oms, webull_attach_riskoff as wb
from tests.unit.test_wbattach1_riskoff import attach, closes, make_case as make_case, restarted


async def software_exit(case, reason="oms_v2_managed_exit:CW_HARD_STOP", *, confirmation_context=None):
    service, factory, broker, _, _ = case
    with factory() as session:
        row = session.scalar(select(OmsManagedPosition))
        position = service._hydrate_v2_position(row)
        managed_id = str(row.id)
    return await service._emit_v2_exit_on_loop(broker.account, "ABC", position, 10.,
        kind="HARD", reference_price=8., reason=reason, bid=8., close_on_fill=True,
        expected_managed_row_id=managed_id, confirmation_context=confirmation_context)


@pytest.mark.asyncio
async def test_unknown_ack_and_restart_never_cancel_or_replace_without_exit_trigger(make_case):
    case = make_case()
    case[2].mode, case[2].child_unknown = "late", True
    assert await attach(case) is False
    assert await attach(case) is False
    case = restarted(case)
    assert await attach(case) is False
    assert len(case[2].requests) == 1 and case[2].cancelled == [] and closes(case[2]) == []
    with case[1]() as session:
        state = session.get(BrokerOrder, case[3]).payload[wb.KEY]
        assert len(state["attempts"]) == 1 and state["attempts"][0]["phase"] == "unknown"
        assert state["attempts"][0]["cancel_requested"] is False
        assert not state.get("exit_reason") and not state.get("exit_phase")


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:orb", "live:webull-alt"])
@pytest.mark.parametrize("reason", ["oms_v2_managed_exit:CW_HARD_STOP",
    "oms_v2_managed_exit:CW_FLIP", "oms_v2_managed_exit:CONFIRMATION_EXIT"])
@pytest.mark.parametrize("hour,session_code", [(14, None), (21, "PM")])
async def test_unknown_ack_real_software_trigger_releases_children_then_fresh_owned_sell(
        make_case, monkeypatch, account, reason, hour, session_code):
    case = make_case(account=account)
    service, factory, broker, entry_id, clock = case
    broker.mode = "late"
    assert await attach(case) is False
    assert broker.cancelled == [] and closes(broker) == []
    clock[0] = datetime(2026, 10, 15, hour, 0, tzinfo=UTC)
    monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: session_code)
    chronology = []
    read, confirm, submit = broker.list_account_positions, broker.confirm_exit_pair_terminal, broker.submit_order

    async def positions(account):
        chronology.append("positions")
        return await read(account)

    async def terminal(**binding):
        chronology.append("terminal")
        return await confirm(**binding)

    async def dispatch(request):
        chronology.append("sell")
        assert chronology[-2] == "positions"  # Last adapter await before SELL.
        return await submit(request)

    monkeypatch.setattr(broker, "list_account_positions", positions)
    monkeypatch.setattr(broker, "confirm_exit_pair_terminal", terminal)
    monkeypatch.setattr(broker, "submit_order", dispatch)
    assert await software_exit(case, reason) == "closed"
    assert len(broker.cancelled) == len(closes(broker)) == 1
    request = closes(broker)[0]
    assert request.reason == reason and request.quantity == 10 and broker.total == 1000
    assert request.broker_account_name == account
    assert request.order_type == ("limit" if session_code else "market")
    assert request.metadata["session"] == (session_code or "NORMAL")
    if session_code:
        assert Decimal(request.metadata["limit_price"]) <= 8
        assert request.metadata["extended_hours"] == "true"
    assert chronology.index("terminal") < chronology.index("positions") < chronology.index("sell")
    with factory() as session:
        state = session.get(BrokerOrder, entry_id).payload[wb.KEY]
        assert state["attempts"][0]["phase"] == "released"
        managed = session.scalar(select(OmsManagedPosition))
        assert managed.status == "closed" and managed.current_quantity == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", ["accepted", "late"])
async def test_positive_position_cannot_waive_unknown_children_or_duplicate_cancel(make_case, ack, monkeypatch, caplog):
    case = make_case()
    monkeypatch.setattr(case[0].logger, "handlers", [*case[0].logger.handlers, caplog.handler])
    broker = case[2]
    broker.mode = ack
    await attach(case)
    broker.child_unknown = True
    assert await software_exit(case) == "refused"
    assert await software_exit(case) == "refused"
    assert broker.total == 1010 and len(broker.cancelled) == 1 and closes(broker) == []
    with case[1]() as session:
        state = session.get(BrokerOrder, case[3]).payload[wb.KEY]
        assert state["attempts"][0]["cancel_requested"] is True and not state.get("exit_phase")
        incidents = session.scalars(select(SystemIncident)).all()
        assert incidents  # Existing incident identity is retained; the new attempt is logged.
    assert "software_exit_child_proof_required" in caplog.text
    broker.child_unknown = False
    assert await software_exit(case) == "closed"
    assert len(broker.cancelled) == len(closes(broker)) == 1


@pytest.mark.asyncio
async def test_readable_live_pair_never_allows_sell_until_both_children_terminal(make_case):
    case = make_case()
    assert await attach(case) is True
    case[2].child_state = "working"
    assert await software_exit(case) == "refused"
    assert closes(case[2]) == [] and case[2].total == 1010
    case[2].child_state = None
    assert await software_exit(case) == "closed"
    assert len(case[2].cancelled) == len(closes(case[2])) == 1


@pytest.mark.asyncio
async def test_existing_software_owner_excludes_second_exit_and_protection_writer(make_case):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    async def competing():
        assert await software_exit(case) == "refused"
        assert await attach(case) is False
    case[2].close_barrier = competing
    assert await software_exit(case) == "closed"
    assert len(case[2].cancelled) == len(closes(case[2])) == 1
    assert len(case[2].requests) == 2  # Original pair and this single close.
    assert not case[0].__dict__.get("_wbattach_exit_owners")


@pytest.mark.asyncio
async def test_working_software_sell_remains_reserved_and_is_never_resubmitted(make_case):
    case = make_case()
    case[2].mode, case[2].close_pending = "late", True
    await attach(case)
    assert await software_exit(case) == "close_submitted"
    assert await software_exit(case) == "refused"
    assert len(closes(case[2])) == len(case[2].cancelled) == 1
    with case[1]() as session:
        state = session.get(BrokerOrder, case[3]).payload[wb.KEY]
        assert state["exit_phase"] == "claimed"


@pytest.mark.asyncio
@pytest.mark.parametrize("filled", [2, 10])
async def test_software_trigger_accounts_child_fill_before_owned_residual(make_case, filled):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    case[2].child_filled = Decimal(filled)
    assert await software_exit(case) == "closed"
    assert [request.quantity for request in closes(case[2])] == ([] if filled == 10 else [Decimal(8)])
    assert case[2].total == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["handoff", "prewire"])
@pytest.mark.parametrize("damage", ["zero", "unknown", "stale"])
async def test_fresh_position_failure_never_sells_even_with_terminal_children(make_case, monkeypatch, point, damage):
    case = make_case()
    broker = case[2]
    broker.mode = "late"
    await attach(case)
    read = broker.list_account_positions
    calls = 0

    async def changed(account):
        nonlocal calls
        calls += 1
        if point == "handoff" or calls >= 2:
            if damage == "zero":
                broker.total = Decimal(0)
            elif damage == "unknown":
                broker.known = False
            else:
                broker.age = 31
        return await read(account)

    monkeypatch.setattr(broker, "list_account_positions", changed)
    assert await software_exit(case) == ("closed" if damage == "zero" else "refused")
    assert closes(broker) == [] and len(broker.cancelled) == 1
    with case[1]() as session:
        state = session.get(BrokerOrder, case[3]).payload[wb.KEY]
        assert state["attempts"][0]["phase"] == "released"
        assert state.get("exit_phase") == ("resolved_by_broker_flat" if damage == "zero" else None)


@pytest.mark.asyncio
async def test_submitting_pair_dispatch_is_not_treated_as_absent(make_case):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    with case[1].begin() as session:
        entry = session.get(BrokerOrder, case[3])
        state = dict(entry.payload[wb.KEY])
        state["attempts"] = [{**state["attempts"][0], "phase": "submitting"}]
        entry.payload = {**entry.payload, wb.KEY: state}
    assert await software_exit(case) == "refused"
    assert case[2].cancelled == [] and closes(case[2]) == []


@pytest.mark.asyncio
async def test_prior_session_unknown_children_in_premarket_require_audited_recovery(make_case, monkeypatch):
    case = make_case()
    case[2].mode = "late"
    await attach(case)
    case[4][0] = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: "AM")
    assert await software_exit(case) == "refused"
    assert closes(case[2]) == [] and case[2].cancelled == []


@pytest.mark.asyncio
@pytest.mark.parametrize("hour,session_code", [(12, "AM"), (21, "PM")])
async def test_same_session_bare_fill_without_any_native_dispatch_keeps_software_exit(make_case, monkeypatch, hour, session_code):
    case = make_case()
    service, factory, broker, entry_id, clock = case
    clock[0] = datetime(2026, 10, 15, hour, 0, tzinfo=UTC)
    monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: session_code)
    with factory.begin() as session:
        entry = session.get(BrokerOrder, entry_id)
        entry.submitted_at = clock[0]
        session.get(TradeIntent, entry.intent_id).created_at = clock[0]
        session.scalar(select(Fill)).filled_at = clock[0]
        session.scalar(select(OmsManagedPosition)).entry_time = clock[0]
    submit = broker.submit_order
    async def dispatch(request):
        return [replace(report, reported_at=clock[0]) for report in await submit(request)]
    monkeypatch.setattr(broker, "submit_order", dispatch)
    assert await software_exit(case) == "closed"
    assert broker.cancelled == [] and len(closes(broker)) == 1 and broker.total == 1000
    assert closes(broker)[0].order_type == "limit"
    assert closes(broker)[0].metadata["session"] == session_code


@pytest.mark.asyncio
async def test_schwab_and_other_strategies_do_not_select_q_software_handoff(make_case, monkeypatch):
    case = make_case()
    with case[1].begin() as session:
        entry = session.get(BrokerOrder, case[3])
        row = session.scalar(select(OmsManagedPosition))
        account = session.get(wb.BrokerAccount, entry.broker_account_id)
        account.provider = "schwab"
        monkeypatch.setattr(type(case[0].settings), "provider_for_account", lambda self, name: "schwab")
        def unexpected_read(*args, **kwargs):
            pytest.fail("unrelated Schwab software exit performed a Q DB read")
        with monkeypatch.context() as scope:
            scope.setattr(session, "get", unexpected_read)
            assert not wb.atr_software_exit_required(case[0], session, row, "oms_v2_managed_exit:CW_HARD_STOP")
        account.provider = "webull"
        for code in ("momentum_warn_live", "orb_schwab"):
            row.strategy_code = code
            assert not wb.atr_software_exit_required(case[0], session, row, "oms_v2_managed_exit:CW_HARD_STOP")
