"""SOURCE/UNRUN. Existing WBATTACH fixture; no replacement runner or native replay.

These controls exercise the durable reducer and retained-owner scans. Scan
setups use the existing shared caller; no historical counterfactual is claimed.
All geometry/native responses are CONTROLLED.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
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


def scan_messages(service, monkeypatch):
    """Observe actual logger calls, not caplog (OMS configures its own handler)."""
    messages = []
    actual = service.logger.critical
    def capture(message, *args, **kwargs):
        if message.startswith("[WBEXIT2-PENDING-SCAN]"):
            messages.append(message % args)
        return actual(message, *args, **kwargs)
    monkeypatch.setattr(service.logger, "critical", capture)
    return messages


@pytest.mark.asyncio
async def test_terminal_shape_is_one_report_per_original_target_and_stop(make_case):
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
    from tests.unit.test_wbexit2_route import owner_case
    case = await owner_case(make_case)
    broker = case[2]
    pair, = [r for r in broker.requests if r.metadata.get("webull_exit_only_pair") == "true"]
    target, stop = [broker.report(WebullBrokerAdapter._combo_leg_coid(pair.client_order_id, leg),
                                 event="cancelled") for leg in ("T", "S")]
    before = len(broker.requests), len(broker.cancelled), broker.positions_reads
    # Shape ONLY, CONTROLLED reports: this does not attest a fresh/account census.
    assert owner.positive_original_terminal((target, stop), pair.client_order_id,
        broker.owned, broker.symbol, case[4][0]) == (target, stop)
    for wrong in ((target,), (target, target), (target, target, stop, stop)):
        with pytest.raises(ValueError):
            owner.positive_original_terminal(wrong, pair.client_order_id, broker.owned, broker.symbol, case[4][0])
    assert before == (len(broker.requests), len(broker.cancelled), broker.positions_reads)


@pytest.mark.asyncio
async def test_RTH_HOLD_names_symbol_and_original_age_on_every_existing_scan(make_case, monkeypatch):
    from tests.unit.test_wbexit2_route import owner_case, close_case, saved
    case = await owner_case(make_case)
    service, _, broker, _, clock = case
    broker.lag = True
    await close_case(case)
    original = saved(case)
    before = len(broker.requests), len(broker.cancelled), broker.positions_reads, broker.working_reads
    messages = scan_messages(service, monkeypatch)
    for seconds in (7, 14, 21):
        clock[0] = datetime.fromisoformat(original["opened_at"]) + timedelta(seconds=seconds)
        # Supplied CONTROLLED observation clock, not a physical elapsed-time proof.
        await owner.refresh_pending(service)
        assert "symbol=ABC" in messages[-1] and f"age_seconds={seconds:.3f}" in messages[-1]
        assert "scan_exception=true" in messages[-1] and "native_protection=UNKNOWN" in messages[-1]
    assert len(messages) == 3, "no dedupe of unresolved HOLD scans"
    assert before == (len(broker.requests), len(broker.cancelled), broker.positions_reads, broker.working_reads)
    final = saved(case)
    assert final["opened_at"] == original["opened_at"] and final["first_cancel_at"] == original["first_cancel_at"]
    assert final["owner_id"] == original["owner_id"] and final["sell"] is None
    assert service._wbexit2_refresh_active is False


@pytest.mark.asyncio
async def test_failed_scan_releases_active_latch_and_reports_on_next_scan(make_case, monkeypatch):
    from tests.unit.test_wbexit2_route import owner_case, close_case, saved
    case = await owner_case(make_case)
    service, _, broker, _, _ = case
    broker.lag = True
    await close_case(case)
    original = saved(case)
    key = broker.account, broker.symbol
    cached = deepcopy(service._wbexit2_pending[key])
    messages = scan_messages(service, monkeypatch)
    before = len(broker.requests), len(broker.cancelled), broker.positions_reads
    service._wbexit2_pending[key] = {**cached, "owner_id": "CONTROLLED-not-a-UUID"}
    with pytest.raises(ValueError):
        await owner.refresh_pending(service)
    assert service._wbexit2_refresh_active is False
    service._wbexit2_pending[key] = cached  # Restore only the negative in-memory corruption input.
    async def read_failed(fn, *, commit=True):
        raise RuntimeError("CONTROLLED-scan-read-failed")
    monkeypatch.setattr(service, "_run_db", read_failed)
    with pytest.raises(RuntimeError, match="CONTROLLED-scan-read-failed"):
        await owner.refresh_pending(service)
    assert service._wbexit2_refresh_active is False and len(messages) == 2
    async def broker_stage_failed(*, account_names=None):
        raise RuntimeError("CONTROLLED-first-broker-stage-failed")
    monkeypatch.setattr(service, "sync_broker_orders", broker_stage_failed)
    # Actual Main-composed pass entry must report BEFORE its first fallible
    # broker stage, not merely after a successful native positions sync.
    with pytest.raises(RuntimeError, match="CONTROLLED-first-broker-stage-failed"):
        await service._sync_broker_state_pass()
    assert len(messages) == 3 and service._wbexit2_refresh_active is False
    assert all("symbol=ABC" in message and "age_seconds=" in message for message in messages)
    assert saved(case) == original and before == (len(broker.requests), len(broker.cancelled), broker.positions_reads)


@pytest.mark.asyncio
async def test_flag_OFF_and_actually_resolved_owner_have_no_pending_scan_line(make_case, monkeypatch):
    from tests.unit.test_wbexit2_route import owner_case, close_case, saved
    case = await owner_case(make_case)
    service, _, broker, _, _ = case
    broker.lag = True
    await close_case(case)
    messages = scan_messages(service, monkeypatch)
    actual, calls = service._run_db, []
    async def counted(fn, *, commit=True):
        calls.append(commit)
        return await actual(fn, commit=commit)
    monkeypatch.setattr(service, "_run_db", counted)
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_wbexit2_enabled", False)
    await owner.refresh_pending(service)
    assert calls == [] and messages == []
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_wbexit2_enabled", True)
    broker.lag = False
    await close_case(case)  # Real shared terminal/owned-SELL/ledger path, no phase edit.
    assert saved(case)["phase"] == "RESOLVED" and not service._wbexit2_pending
    before = len(broker.requests), len(broker.cancelled), broker.positions_reads
    await owner.refresh_pending(service)
    assert messages == [] and before == (len(broker.requests), len(broker.cancelled), broker.positions_reads)
