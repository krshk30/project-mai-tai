"""SOURCE/UNRUN. Existing WBATTACH fixture; no replacement runner or native replay.

These controls exercise the durable database reducer, not integrated routing or
historical ON counterfactuals. All geometry/native responses are CONTROLLED.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, OmsManagedPosition, Strategy
from project_mai_tai.oms import wbexit2 as owner
from tests.unit.test_wbattach1_riskoff import make_case as make_case


def bound(session, case):
    entry = session.get(BrokerOrder, case[3])
    row = session.scalar(select(OmsManagedPosition))
    return owner.identity(entry, row, session.get(BrokerAccount, entry.broker_account_id),
                          session.get(Strategy, entry.strategy_id))


def start(session, case):
    binding = bound(session, case)
    state = owner.begin(session, binding, quantity=Decimal(10), reason="oms_v2_managed_exit:CW_FLIP",
        now=case[4][0], entry_anchor=case[4][0], native_account_id="CONTROLLED-native-account", originals=[{"base": "CONTROLLED-original", "quantity": "10", "children": [
            {"coid": "CONTROLLED-T", "native_id": "CONTROLLED-native-T", "price": "10.20"},
            {"coid": "CONTROLLED-S", "native_id": "CONTROLLED-native-S", "price": "9.50"}]}])
    state = owner.transition(session, binding, state["generation"], operation="acquire",
        now=case[4][0], invocation="CONTROLLED-unit-invocation")
    return binding, state


def step(session, binding, state, case, operation, **evidence):
    return owner.transition(session, binding, state["generation"], operation=operation,
                            now=case[4][0], invocation=state["invocation"], **evidence)


def terminal(session, binding, state, case):
    return step(session, binding, state, case, "terminal", all_originals_terminal=True,
                fills={"CONTROLLED-T": "0", "CONTROLLED-S": "0"})


def reserve(session, binding, state, case):
    return step(session, binding, state, case, "reserve_sell", request={
        "coid": "CONTROLLED-one-sell", "quantity": "10", "request_binding": "CONTROLLED-full-request-binding"})


def test_owner_survives_joined_terminal_wait_and_fresh_session_without_new_working_pair(make_case):
    case = make_case()
    with case[1].begin() as session:
        binding, state = start(session, case)
        state = step(session, binding, state, case, "cancel", base="CONTROLLED-original")
        original_start = state["first_cancel_at"]
        state = step(session, binding, state, case, "hold", reason="terminal_unknown", original_attempt_failed=True)
    case[4][0] += timedelta(seconds=12)
    with case[1].begin() as session:
        # A fresh session/models represent a durable checkpoint boundary. No
        # process death/SDK quiescence or integrated restart is claimed here.
        actual = owner.read(session, bound(session, case))
        assert actual == state and actual["remaining"] == "10"
        actual = terminal(session, binding, actual, case)
        assert actual["originals"] == state["originals"]
        assert actual["first_cancel_at"] == original_start
        assert actual["original_attempt_outcome"] == "FAIL"
        assert actual["owner_id"] == state["owner_id"] and actual["phase"] == "TERMINAL"


def test_unknown_sell_ack_keeps_exact_reservation_and_never_grants_second_sell(make_case):
    case = make_case()
    with case[1].begin() as session:
        binding, state = start(session, case)
        state = reserve(session, binding, terminal(session, binding, state, case), case)
    with case[1].begin() as session:
        saved = owner.read(session, binding)
        held = step(session, binding, saved, case, "hold", reason="sell_ack_unknown")
        assert held["sell"] == saved["sell"] and held["phase"] == "SELL_RESERVED"
        with pytest.raises(ValueError, match="already_reserved"):
            reserve(session, binding, held, case)


def test_stale_generation_cannot_overwrite_current_owner(make_case):
    case = make_case()
    with case[1].begin() as session:
        binding, state = start(session, case)
    with case[1].begin() as session:
        current = step(session, binding, state, case, "hold", reason="stop_or_lease_unknown")
    with case[1].begin() as session:
        with pytest.raises(ValueError, match="CAS_lost"):
            terminal(session, binding, state, case)
        assert owner.read(session, binding) == current


def test_partial_OUR_sell_cannot_use_manual_account_inventory_to_resolve(make_case):
    case = make_case()
    with case[1].begin() as session:
        binding, state = start(session, case)
        state = reserve(session, binding, terminal(session, binding, state, case), case)
        state = step(session, binding, state, case, "sell_status", coid="CONTROLLED-one-sell",
                     native_proven=True, status="partially_filled", filled="3")
        assert state["remaining"] == "7"
        with pytest.raises(ValueError, match="positive_resolution"):
            step(session, binding, state, case, "resolve", managed_closed=True,
                 virtual_flat=True, attributed_execution_complete=True)
        with pytest.raises(ValueError, match="already_reserved"):
            reserve(session, binding, state, case)


def test_terminal_child_fill_highwater_cannot_regress_or_exceed_owned_quantity(make_case):
    case = make_case()
    with case[1].begin() as session:
        binding, state = start(session, case)
        state = step(session, binding, state, case, "terminal", all_originals_terminal=True,
                     fills={"CONTROLLED-T": "2", "CONTROLLED-S": "0"})
        assert state["remaining"] == "8"
        with pytest.raises(ValueError, match="regressed"):
            terminal(session, binding, state, case)
        with pytest.raises(ValueError, match="exceeds_original"):
            step(session, binding, state, case, "terminal", all_originals_terminal=True,
                 fills={"CONTROLLED-T": "7", "CONTROLLED-S": "7"})


def test_actual_entry_binding_rejects_metadata_only_or_wrong_entry(make_case):
    case = make_case()
    with case[1].begin() as session:
        entry = session.get(BrokerOrder, case[3])
        row = session.scalar(select(OmsManagedPosition))
        entry.side = "sell"
        with pytest.raises(ValueError, match="actual_entry_binding"):
            owner.identity(entry, row, session.get(BrokerAccount, entry.broker_account_id),
                           session.get(Strategy, entry.strategy_id))


def test_bare_or_malformed_tree_is_not_fabricated_as_protected(make_case):
    case = make_case()
    with case[1].begin() as session:
        binding = bound(session, case)
        with pytest.raises(ValueError, match="original_tree_unknown"):
            owner.begin(session, binding, quantity=Decimal(10), originals=None,
                        reason="oms_v2_managed_exit:CW_FLIP", now=case[4][0])


def test_unjoined_durable_invocation_cannot_be_stolen_by_a_new_task_token(make_case):
    case = make_case()
    with case[1].begin() as session:
        binding, state = start(session, case)
        with pytest.raises(ValueError, match="not_physically_joined"):
            owner.transition(session, binding, state["generation"], operation="acquire",
                             now=case[4][0], invocation="CONTROLLED-other-task")
        with pytest.raises(ValueError, match="current_durable_invocation"):
            owner.transition(session, binding, state["generation"], operation="cancel",
                now=case[4][0], invocation="CONTROLLED-other-task", base="CONTROLLED-original")


def test_cancel_reservation_can_clear_ONLY_actual_unentered_joined_command(make_case):
    case = make_case()
    with case[1].begin() as session:
        binding, state = start(session, case)
        state = step(session, binding, state, case, "cancel", base="CONTROLLED-original")
        risk_start = state["first_cancel_at"]
        with pytest.raises(ValueError, match="never_entered_not_proven"):
            step(session, binding, state, case, "unentered_cancel", bases=["CONTROLLED-original"],
                 physical_joined=False, adapter_never_entered=True)
        state = step(session, binding, state, case, "unentered_cancel", bases=["CONTROLLED-original"],
                     physical_joined=True, adapter_never_entered=True)
        assert state["cancel_bases"] == [] and state["first_cancel_at"] == risk_start
        assert state["phase"] == "HOLD" and Decimal(state["remaining"]) == 10


@pytest.mark.asyncio
async def test_cancelled_waiter_captures_joined_commit_result_before_primary_cancel(make_case):
    case = make_case()
    call = owner._RecordOnly("CONTROLLED", "ABC", {}, {})
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []
    async def physical():
        entered.set()
        await release.wait()
        return {"cancel_bases": ["PRIOR-original"], "generation": 4}
    def committed(state):
        seen.append(state)
    task = asyncio.create_task(owner.joined(case[0], call, physical(), completed=committed))
    try:
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and call.wb2_pending is not None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert seen == [{"cancel_bases": ["PRIOR-original"], "generation": 4}]
        assert call.wb2_pending is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_nonselected_preparation_keeps_original_await_without_WBEXIT2_binding(make_case):
    from project_mai_tai.broker_adapters.protocols import OrderRequest
    case = make_case()
    request = OrderRequest("CONTROLLED-PM", case[2].account, "schwab_1m_v2", "ABC", "sell", "close",
                           Decimal(10), "oms_v2_managed_exit:CONFIRMATION_EXIT", {})
    entered = []
    async def existing_preparation():
        entered.append("original")
        return "CONTROLLED-original-result"
    # No actual WBEXIT2 context exists: PM/AH/Schwab preparation must never
    # inspect a nonexistent WBEXIT2 owner or add its physical task wrapper.
    assert await owner.prepare_current(case[0], request, existing_preparation()) == "CONTROLLED-original-result"
    assert entered == ["original"]
