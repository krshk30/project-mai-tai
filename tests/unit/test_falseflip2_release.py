import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot,
    Fill, OmsManagedPosition, Strategy, SystemIncident, TradeIntent, VirtualPosition,
)
from project_mai_tai.falseflip1 import Classification
from project_mai_tai.falseflip1_runtime import BUDGET_TYPE, FalseBudget, FalseFlipStore
from project_mai_tai.positions_read_receipt import record_receipts
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, Quote
from project_mai_tai.v2_falseflip_release import (
    FalseFlipReleaseProof, persist_owner_transition, read_release_proofs, unanswered_incidents,
)
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipRecord, FlipPositionBook, FlipPositionClose
from project_mai_tai.v2_removed_wait import RemovedWait
from tests.unit.test_falseflip1_runtime import (
    CASES, FALSE_SLOTS, REAL_CASES, ack, database, runtime,
)
from tests.unit.test_retryleft1 import strategy_for_record

TCRT = json.loads((Path(__file__).parents[1] / "fixtures/falseflip2_tcrt_20261013.json").read_text())
AT = datetime(2026, 10, 13, 12, 50, 18, tzinfo=UTC)
ACCOUNTS = ("live:schwab_1m_v2", "live:orb")
OPPORTUNITY = 1791894062903
SEGMENT = 1791893820000


def captured_database():
    """Exact recorded rows/events; Fill projection from filled orders, not new fills.

    Historical positions-read receipts were not retained. The receipts below are
    explicitly controlled 08:50:18 evidence, so timing is conditional, not observed.
    """
    engine, factory = database()
    with factory() as session:
        strategy = Strategy(id=UUID(TCRT["orders"][0]["strategy_id"]), code="schwab_1m_v2", name="v2")
        session.add(strategy)
        for name in ACCOUNTS:
            order = next(o for o in TCRT["orders"] if o["account"] == name)
            session.add(BrokerAccount(id=UUID(order["broker_account_id"]), name=name,
                provider="webull" if name == "live:orb" else "schwab", environment="live"))
        session.flush()
        for data in TCRT["orders"]:
            order = BrokerOrder(id=UUID(data["id"]), strategy_id=strategy.id,
                broker_account_id=UUID(data["broker_account_id"]), client_order_id=data["client_order_id"],
                broker_order_id=data["broker_order_id"], symbol="TCRT", side=data["side"],
                status=data["status"], order_type=data["order_type"], time_in_force=data["time_in_force"],
                quantity=Decimal(str(data["quantity"])), payload=data["payload"],
                submitted_at=datetime.fromisoformat(data["submitted_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]))
            session.add(order)
            event = next(e for e in TCRT["events"] if e["order_id"] == data["id"] and e["event_type"] == "filled")
            session.add(Fill(order_id=order.id, strategy_id=strategy.id,
                broker_account_id=order.broker_account_id, symbol="TCRT", side=order.side,
                quantity=order.quantity, price=Decimal("1.9194") if order.side == "buy" else Decimal("1.9006"),
                filled_at=datetime.fromisoformat(event["event_at"])))
        for data in TCRT["managed"]:
            session.add(OmsManagedPosition(id=UUID(data["id"]), strategy_code="schwab_1m_v2",
                broker_account_name=data["broker_account_name"], symbol="TCRT",
                entry_order_id=UUID(data["entry_order_id"]), entry_client_order_id=data["entry_client_order_id"],
                entry_classification=data["entry_classification"], entry_price=Decimal(str(data["entry_price"])),
                original_quantity=data["original_quantity"], current_quantity=0, status="closed",
                entry_time=datetime.fromisoformat(data["entry_time"]),
                updated_at=datetime.fromisoformat(data["updated_at"])))
        record_receipts(session, [(a, AT, 0) for a in ACCOUNTS], recorded_at=AT)
        session.commit()
    return engine, factory


def tcrt_runtime():
    proofs = tuple(Classification.from_payload(r["entry_classification"]) for r in TCRT["managed"])
    owner = next(r["payload"] for r in TCRT["ownership"] if r["snapshot_type"] == "v2_flip_entry_ownership"
                 and r["payload"]["reason"] == "webull_first_rest_filled")
    record = FlipEntryOwnershipRecord("TCRT", OPPORTUNITY, owner["phase"], int(owner["flip_bar_ts"]),
        int(owner["provisional_started_ms"]), tuple(owner["fill_accounts"]), owner["position_ids"],
        owner["position_entry_ms"], SEGMENT, owner["retry_closes_at_place"])
    strategy, state, _, clock, writes = strategy_for_record(record, AT, pm=True, quantities=(311, 155))
    for name, value in {
        "strategy_schwab_1m_v2_false_flip_enabled": True,
        "strategy_schwab_1m_v2_false_flip2_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct": .5,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_band_pct": .5,
        "strategy_schwab_1m_v2_resting_buy_round_up_enabled": True,
    }.items():
        setattr(strategy.settings, name, value)
    strategy._resting_in_window = lambda now=None: True
    strategy._resting_session_is_eh = lambda now=None: True
    strategy._eh_resting_enabled = True
    strategy._resting_trigger_offset_pct = .5
    strategy._resting_entry_band_pct = .5
    strategy.configure_falseflip({}, readable=True)
    state.cw_armed = False  # Recorded pre-BUY owner has cw_arm_bar_ts=0.
    state.resting_active = state.webull_resting_active = False
    state.atr_state, state.atr_state_age = "short", 32
    state.atr_trail = float(proofs[0].bar.trail)
    bar = next(b for b in TCRT["bars"] if b["bar_time"].startswith("2026-10-13T12:49:"))
    state.bars.append(OHLCVBar(int(datetime.fromisoformat(bar["bar_time"]).timestamp()*1000),
        *(float(bar[k]) for k in ("open_price", "high_price", "low_price", "close_price")), bar["volume"]))
    return strategy, state, clock, proofs, writes


def apply_book(strategy, state, clock, classifications, *, complete=True, all_filled=True):
    opportunity = classifications[0].identity.opportunity_id
    strategy.apply_flip_position_book(FlipPositionBook(clock[0], True, {},
        closes_by_symbol={state.symbol: tuple(FlipPositionClose(p.identity.account,
            p.identity.managed_row_id, "CONFIRMATION_EXIT") for p in classifications)},
        entry_classifications={state.symbol: tuple(p.as_payload() for p in classifications)},
        filled_opportunities={state.symbol: tuple((p.identity.account, opportunity) for p in classifications)},
        closed_entry_rows=frozenset((p.identity.account, p.identity.managed_row_id) for p in classifications),
        falseflip_release_proofs={(state.symbol, opportunity): FalseFlipReleaseProof(
            state.symbol, opportunity, clock[0], complete, "controlled", all_filled=all_filled)}))


def settle(strategy, state, clock, classifications, **kwargs):
    for _ in range(3):
        apply_book(strategy, state, clock, classifications, **kwargs)
        ack(strategy)


def release(strategy, state):
    record, = strategy.pending_falseflip2_releases()
    assert strategy.falseflip2_release_valid(record)
    assert strategy.acknowledge_falseflip2_release(record)
    assert state.flip_owner_phase == "idle"
    return record


def test_recorded_tcrt_with_controlled_fresh_receipts_rearms_before_buy_without_cancel():
    strategy, state, clock, proofs, writes = tcrt_runtime()
    settle(strategy, state, clock, proofs)
    assert not strategy.pending_falseflip_cancel_publications()
    release(strategy, state)
    assert not writes and not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
    strategy._falseflip_restoring_track(state)
    plan, = strategy.pending_falseflip2_rearms()
    assert state.atr_state == "short" and not state.resting_active
    # Production commits this exact record off-loop before this memory acknowledgement.
    assert strategy.acknowledge_falseflip2_rearm(plan)
    ack(strategy)
    assert state.resting_trigger == pytest.approx(1.93)
    assert state.resting_wire_cap == pytest.approx(1.94)
    assert state.retry_one_segment_id == SEGMENT and state.retry_one_closes_in_segment == 0
    clock[0] += 17000
    # A controlled crossing at the recorded case's first real-cross time; not a new recorded fill.
    quote = Quote("TCRT", 1.92, 1.93, 1.93, clock[0], clock[0])
    draft = strategy.on_quote("TCRT", quote)
    assert draft is not None and draft.intent_type == "open"
    mirror, = strategy.drain_webull_fanout_intents()
    assert draft.metadata["cw_entry_slot"] == mirror.metadata["cw_entry_slot"] == "first"
    assert strategy.on_quote("TCRT", quote) is None
    assert not strategy.drain_webull_fanout_intents()


@pytest.mark.parametrize("all_filled", [True, False])
def test_settlement_marker_distinguishes_filled_closed_from_cancelled_sibling(all_filled):
    strategy, state, clock, proofs, _ = tcrt_runtime()
    settle(strategy, state, clock, proofs, all_filled=all_filled)
    budget = strategy._falseflip_budget(state)
    assert (OPPORTUNITY in budget.filled_closed) is all_filled
    assert (OPPORTUNITY in budget.cancelled) is not all_filled
    release(strategy, state)


@pytest.mark.parametrize("problem", ["long", "new_sell", "thin", "too_young", "gap", "stale_bar", "hold"])
def test_stale_rearm_list_never_bypasses_native_short_and_admission_guards(problem):
    strategy, state, clock, proofs, _ = tcrt_runtime()
    settle(strategy, state, clock, proofs)
    release(strategy, state)
    strategy._falseflip_rearm.add(state.symbol)  # stale FF1 list must have no effect
    if problem == "long":
        state.atr_state, state.atr_short_flip_bar_ts = "long", 0
    elif problem == "new_sell":
        state.atr_short_flip_bar_ts = state.retry_one_segment_id = clock[0]
        state.atr_state_age = 1
    elif problem == "thin":
        strategy._liquidity_floor_ok = lambda _: False
    elif problem == "too_young":
        state.atr_state_age = 1
    elif problem == "gap":
        strategy.gap_hold_active = lambda _: True
    elif problem == "stale_bar":
        clock[0] += 120000
    else:
        strategy._entries_held = True
    strategy._falseflip_restoring_track(state)
    assert not strategy.pending_falseflip2_rearms()
    assert not state.resting_active and not strategy.drain_pending_intents()
    if problem == "long":
        assert not strategy._strict_first_rest_admitted(state, slot="first")


@pytest.mark.parametrize("slot", FALSE_SLOTS)
@pytest.mark.parametrize("pm", [False, True])
def test_recorded_class_scalar_replay_requires_controlled_receipts_and_native_normal_arm(slot, pm):
    rows = [r for r in CASES if r["slot"] == slot]
    strategy, state, clock, proofs, _ = runtime(rows, pm=pm, falseflip2=True)
    state.atr_state_age = 10
    settle(strategy, state, clock, proofs)
    release(strategy, state)
    strategy._falseflip_restoring_track(state)
    plan, = strategy.pending_falseflip2_rearms()
    assert strategy.acknowledge_falseflip2_rearm(plan)
    ack(strategy)
    assert state.resting_active
    assert not strategy.pending_falseflip_cancel_publications()
    if not pm:
        assert len(strategy.drain_pending_intents()) == len(strategy.drain_webull_direct_intents()) == 1


@pytest.mark.parametrize("row", list({r["fill_id"]:r for r in [*REAL_CASES, *[
    case for case in CASES if case["symbol"] in {"IPDN", "OLOX"} and case["classification"] == "REAL_FLIP"
]]}.values()), ids=lambda r: r["fill_id"])
def test_real_fill_is_never_compensated_or_rearmed(row):
    strategy, state, clock, proofs, _ = runtime([row], falseflip2=True)
    apply_book(strategy, state, clock, proofs)
    assert not strategy.pending_falseflip2_releases()
    assert strategy._falseflip_effective_closes(state, 1) == 1


def test_database_actual_tcrt_closed_fills_need_both_real_read_receipts():
    engine, factory = captured_database()
    with factory() as session:
        rows = session.scalars(select(OmsManagedPosition)).all()
        proof = read_release_proofs(session, rows, ACCOUNTS, now=AT)[("TCRT", OPPORTUNITY)]
        assert proof.complete and proof.all_filled
        assert len(proof.row_bindings) == 2
        record_receipts(session, [(ACCOUNTS[1], AT - timedelta(seconds=16), 0)], recorded_at=AT)
        session.commit()
        bad = read_release_proofs(session, rows, ACCOUNTS, now=AT)[("TCRT", OPPORTUNITY)]
        assert not bad.complete and bad.reason == "broker_position_read_stale_or_unknown"
    engine.dispose()


@pytest.mark.parametrize("problem", ["order", "intent", "position", "partial", "missing_fill", "wrong_coid", "pre_close_read"])
def test_database_live_or_uncertain_evidence_stays_blocked(problem):
    engine, factory = captured_database()
    with factory() as session:
        rows = session.scalars(select(OmsManagedPosition)).all()
        order = session.get(BrokerOrder, rows[0].entry_order_id)
        if problem == "order":
            order.status = "accepted"
        elif problem == "intent":
            session.add(TradeIntent(strategy_id=order.strategy_id, broker_account_id=order.broker_account_id,
                symbol="TCRT", side="buy", intent_type="open", quantity=1, reason="controlled", status="submitted"))
        elif problem == "position":
            session.add(VirtualPosition(strategy_id=order.strategy_id, broker_account_id=order.broker_account_id,
                symbol="TCRT", quantity=1, average_price=Decimal("1.9")))
        elif problem == "partial":
            order.quantity += 1
            order.status = "partially_filled"
        elif problem == "missing_fill":
            session.delete(session.scalar(select(Fill).where(Fill.order_id == order.id)))
        elif problem == "wrong_coid":
            order.client_order_id = "unrelated"
        else:
            record_receipts(session, [(a, AT - timedelta(seconds=12), 0) for a in ACCOUNTS], recorded_at=AT)
        session.commit()
        proof = read_release_proofs(session, rows, ACCOUNTS, now=AT)[("TCRT", OPPORTUNITY)]
        assert not proof.complete
    engine.dispose()


def test_manual_or_orb_shares_never_become_v2_owner_but_exit_must_be_own_exact_quantity():
    engine, factory = captured_database()
    with factory() as session:
        row = session.scalar(select(OmsManagedPosition))
        order = session.get(BrokerOrder, row.entry_order_id)
        other = Strategy(code="orb_schwab", name="ORB")
        session.add(other)
        session.flush()
        session.add_all([AccountPosition(broker_account_id=order.broker_account_id, symbol="TCRT",
            quantity=1000, average_price=Decimal("1.9")),
            VirtualPosition(strategy_id=other.id, broker_account_id=order.broker_account_id,
                symbol="TCRT", quantity=1000, average_price=Decimal("1.9"))])
        session.commit()
        rows = session.scalars(select(OmsManagedPosition)).all()
        assert read_release_proofs(session, rows, ACCOUNTS, now=AT)[("TCRT", OPPORTUNITY)].complete
        own_exit = session.scalar(select(Fill).where(Fill.side == "sell", Fill.broker_account_id == order.broker_account_id))
        own_exit.quantity -= 1
        session.commit()
        assert not read_release_proofs(session, rows, ACCOUNTS, now=AT)[("TCRT", OPPORTUNITY)].complete
    engine.dispose()


def test_atomic_offloop_owner_then_dispatch_snapshots_are_idempotent_and_preserve_retry_budget():
    engine, factory = captured_database()
    strategy, state, clock, proofs, _ = tcrt_runtime()
    settle(strategy, state, clock, proofs)
    old = strategy.pending_falseflip2_releases()[0]
    request = RemovedWait("TCRT", OPPORTUNITY, "controlled-request", clock[0]-1000, ACCOUNTS, "false_flip_restore")
    strategy._removed_wait_requests["TCRT"] = request
    with factory() as session:
        session.add_all([DashboardSnapshot(snapshot_type=BUDGET_TYPE, payload=strategy._falseflip_budget(state).payload(), created_at=AT),
            DashboardSnapshot(snapshot_type="v2_fanout_segment_identity", created_at=AT,
                payload={"symbol":"TCRT", "fanout_segment_id":str(OPPORTUNITY), "active":True}),
            DashboardSnapshot(snapshot_type="v2_flip_entry_ownership", created_at=AT,
                payload=old.as_payload(active=True, reason="controlled")),
            DashboardSnapshot(snapshot_type="v2_removed_wait", created_at=AT, payload=request.payload(active=True))])
        session.commit()
    with pytest.raises(ValueError):
        persist_owner_transition(factory, old, ACCOUNTS, lambda: False, request=request, now=AT)
    persist_owner_transition(factory, old, ACCOUNTS, lambda: True, request=request, now=AT + timedelta(milliseconds=1))
    persist_owner_transition(factory, old, ACCOUNTS, lambda: True, request=request, now=AT + timedelta(milliseconds=2))
    release(strategy, state)
    strategy._falseflip_restoring_track(state)
    plan, = strategy.pending_falseflip2_rearms()
    persist_owner_transition(factory, plan[0], ACCOUNTS, lambda: True, previous=plan[2], now=AT+timedelta(milliseconds=3))
    persist_owner_transition(factory, plan[0], ACCOUNTS, lambda: True, previous=plan[2], now=AT+timedelta(milliseconds=4))
    with factory() as session:
        dispatch = session.scalars(select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == "v2_wait_dispatch")).all()
        assert len(dispatch) == 1 and dispatch[0].payload["kind"] == "begin"
        budget = session.scalar(select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == BUDGET_TYPE))
        assert FalseBudget.read(budget.payload).refunded_counts == ()
    engine.dispose()


def test_budget_old_payload_backward_compatible_and_second_false_owes_exactly_one_skip():
    budget = FalseBudget("X", 1).closed(2, 1).fully_filled_closed(2).closed(3, 2).fully_filled_closed(3)
    assert budget.skip_due
    assert FalseBudget.read(budget.payload()) == budget
    assert not budget.skip().skip_due
    legacy = FalseBudget("X", 1).payload()
    legacy.pop("filled_closed")
    assert FalseBudget.read(legacy).filled_closed == ()


def test_partial_sibling_needs_native_terminal_receipt_and_fresh_flat_reads():
    engine, factory = captured_database()
    with factory() as session:
        rows = session.scalars(select(OmsManagedPosition)).all()
        order = session.get(BrokerOrder, rows[0].entry_order_id)
        order.quantity += 1
        order.status = "cancelled"
        session.commit()
        proof = read_release_proofs(session, rows, ACCOUNTS, now=AT)[("TCRT", OPPORTUNITY)]
        assert not proof.complete
        session.add(BrokerOrderEvent(order_id=order.id, event_type="cancelled", event_source="broker",
            event_at=AT, payload={"client_order_id": order.client_order_id}))
        session.commit()
        proof = read_release_proofs(session, rows, ACCOUNTS, now=AT)[("TCRT", OPPORTUNITY)]
        assert proof.complete and not proof.all_filled
    engine.dispose()


@pytest.mark.parametrize("problem", ["not_confirmed", "could_not_tell", "read_precedes_cancel", "malformed_binding"])
def test_bad_terminal_receipt_or_binding_is_symbol_unknown_not_permission(problem):
    engine, factory = captured_database()
    with factory() as session:
        rows = session.scalars(select(OmsManagedPosition)).all()
        order = session.get(BrokerOrder, rows[0].entry_order_id)
        if problem == "malformed_binding":
            rows[0].entry_classification = {**rows[0].entry_classification, "entry_order_id": "not-a-uuid"}
        else:
            order.quantity += 1
            order.status = "cancelled"
            session.add(BrokerOrderEvent(order_id=order.id, event_type="cancelled", event_source="broker",
                event_at=AT+timedelta(seconds=1) if problem == "read_precedes_cancel" else AT,
                payload={"client_order_id": order.client_order_id,
                         "metadata":{"cancel_outcome": problem if problem != "read_precedes_cancel" else "already_absent"}}))
        session.commit()
        assert not read_release_proofs(session, rows, ACCOUNTS, now=AT)[("TCRT", OPPORTUNITY)].complete
    engine.dispose()


@pytest.mark.parametrize("status", ["accepted", "unknown", "partially_filled", "cancelled"])
def test_unfilled_sibling_never_disappears_from_proof_without_real_cancel(status):
    engine, factory = captured_database()
    with factory() as session:
        rows = session.scalars(select(OmsManagedPosition)).all()
        entry = session.get(BrokerOrder, rows[0].entry_order_id)
        sibling = BrokerOrder(strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            symbol="TCRT", side="buy", quantity=1, order_type="stop_limit", time_in_force="day",
            status=status, client_order_id="controlled-sibling", submitted_at=AT,
            payload={"fanout_segment_id": str(OPPORTUNITY)})
        session.add(sibling)
        session.commit()
        assert not read_release_proofs(session, rows, ACCOUNTS, now=AT)[("TCRT", OPPORTUNITY)].complete
        if status == "cancelled":
            session.add(BrokerOrderEvent(order_id=sibling.id, event_type="cancelled", event_source="broker",
                event_at=AT, payload={"client_order_id": sibling.client_order_id}))
            session.commit()
            proof = read_release_proofs(session, rows, ACCOUNTS, now=AT)[("TCRT", OPPORTUNITY)]
            assert proof.complete and not proof.all_filled
    engine.dispose()


def test_unanswered_false_restore_incident_is_bounded_deduplicated_and_never_releases():
    engine, factory = captured_database()
    request = RemovedWait("TCRT", OPPORTUNITY, "controlled-unanswered", int(AT.timestamp()*1000)-31000,
        ACCOUNTS, "false_flip_restore")
    unanswered_incidents(factory, (request,), now=AT)
    unanswered_incidents(factory, (request,), now=AT)
    with factory() as session:
        incidents = session.scalars(select(SystemIncident)).all()
        assert len(incidents) == 1 and incidents[0].severity == "critical"
        assert incidents[0].payload["action"] == "keep_blocked"
        assert session.scalars(select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == "v2_removed_wait")).all() == []
    engine.dispose()


@pytest.mark.asyncio
async def test_actual_position_poll_transition_commits_on_worker_before_intrabar_rest(monkeypatch):
    import threading
    import project_mai_tai.v2_falseflip_release as release_module

    engine, factory = captured_database()
    strategy, state, clock, proofs, _ = tcrt_runtime()
    old = strategy._flip_owner_record(state)
    with factory() as session:
        session.add_all([DashboardSnapshot(snapshot_type="v2_fanout_segment_identity", created_at=AT,
            payload={"symbol":"TCRT", "fanout_segment_id":str(OPPORTUNITY), "active":True}),
            DashboardSnapshot(snapshot_type="v2_flip_entry_ownership", created_at=AT,
                payload=old.as_payload(active=True, reason="controlled"))])
        session.commit()
    apply_book(strategy, state, clock, proofs)
    bot = SchwabV2BotService.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot.session_factory = strategy, strategy.settings, factory
    store = FalseFlipStore(factory)
    bot._falseflip_store = SimpleNamespace(commit=lambda before, after: store.commit(before, after, now=AT))
    writes = []
    original = release_module.persist_owner_transition

    def writer(*args, **kwargs):
        writes.append(threading.get_ident())
        return original(*args, **kwargs, now=AT+timedelta(milliseconds=len(writes)))

    monkeypatch.setattr(release_module, "persist_owner_transition", writer)
    await bot._falseflip2_poll(strategy._falseflip_book)
    assert writes == [writes[0], writes[0]] and writes[0] != threading.get_ident()
    assert state.resting_active and state.resting_trigger == pytest.approx(1.93)
    assert not strategy._falseflip_budget(state).awaiting_rearm
    assert not strategy._removed_wait_requests
    with factory() as session:
        owner = session.scalar(select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == "v2_flip_entry_ownership")
            .order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()).limit(1))
        assert owner.payload["active"] and owner.payload["opportunity_id"] == str(state.fanout_segment_id)
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_symbol", ["TCRT", "UNRELATED"])
async def test_same_symbol_change_during_write_never_grants_ack_and_other_symbol_is_not_a_global_fence(monkeypatch, changed_symbol):
    import project_mai_tai.v2_falseflip_release as release_module

    strategy, state, clock, proofs, _ = tcrt_runtime()
    settle(strategy, state, clock, proofs)
    bot = SchwabV2BotService.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot.session_factory = strategy, strategy.settings, object()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    checked = []
    import threading

    go = threading.Event()

    def writer(_factory, _record, _accounts, valid, **_kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert go.wait(2)
        checked.append(valid())
        if not valid():
            raise ValueError("superseded")

    monkeypatch.setattr(release_module, "persist_owner_transition", writer)
    task = asyncio.create_task(bot._falseflip2_poll(strategy._falseflip_book))
    await entered.wait()
    bot._clearwait_emit_generation_by_symbol = {changed_symbol: 1}
    bot._clearwait_emit_generation = 999
    go.set()
    await task
    if changed_symbol == "TCRT":
        assert checked == [False]
        assert state.flip_owner_phase == "provisional"
    else:
        assert checked == [True, True]
        assert state.resting_active
    assert strategy._removed_wait_restore_readable
    assert not strategy.pending_falseflip2_rearms()


def test_second_false_skip_durable_once_then_recross_can_rearm_same_sell_segment():
    strategy, state, clock, proofs, _ = tcrt_runtime()
    prior = FalseBudget("TCRT", SEGMENT).closed(SEGMENT+1, 1).fully_filled_closed(SEGMENT+1)
    strategy.configure_falseflip({("TCRT", SEGMENT): prior}, readable=True)
    state.retry_one_closes_in_segment = 2
    state.flip_owner_retry_closes_at_place = 1
    state.flip_owner_phase = "consumed"
    settle(strategy, state, clock, proofs)
    release(strategy, state)
    trigger = strategy._resting_trigger_for_line(state.atr_trail)
    strategy._falseflip_restoring_track(state)
    assert not strategy.pending_falseflip2_rearms()
    assert strategy.on_quote("TCRT", Quote("TCRT", trigger-.01, trigger, trigger, clock[0], clock[0])) is None
    ack(strategy)
    assert strategy._falseflip_budget(state).skipped
    strategy._falseflip_restoring_track(state)
    assert not strategy.pending_falseflip2_rearms()
    strategy.on_quote("TCRT", Quote("TCRT", trigger-.02, trigger-.01, trigger-.01, clock[0], clock[0]))
    strategy._falseflip_restoring_track(state)
    plan, = strategy.pending_falseflip2_rearms()
    assert strategy.acknowledge_falseflip2_rearm(plan)
    ack(strategy)
    draft = strategy.on_quote("TCRT", Quote("TCRT", trigger-.01, trigger, trigger, clock[0], clock[0]))
    assert draft is not None
    assert len(strategy.drain_webull_fanout_intents()) == 1
    assert state.retry_one_segment_id == SEGMENT


def test_boot_after_release_uses_settled_episode_but_never_rearms_on_long_or_unreadable_proof():
    strategy, state, clock, proofs, _ = tcrt_runtime()
    settle(strategy, state, clock, proofs)
    release(strategy, state)
    budgets = dict(strategy._falseflip_budgets)
    strategy.configure_falseflip(budgets, readable=True)
    apply_book(strategy, state, clock, proofs)
    assert not strategy.pending_falseflip2_releases()  # no opportunity=0 owner write
    strategy._falseflip_restoring_track(state)
    assert len(strategy.pending_falseflip2_rearms()) == 1
    state.atr_state, state.atr_short_flip_bar_ts = "long", 0
    strategy._falseflip_restoring_track(state)
    assert not strategy.pending_falseflip2_rearms()


@pytest.mark.parametrize("phase", ["unknown", "consumed", "provisional"])
def test_fully_closed_false_owner_retires_without_buy_flip_unknown_or_cancel_receipt(phase):
    strategy, state, clock, proofs, _ = tcrt_runtime()
    state.flip_owner_phase = phase
    settle(strategy, state, clock, proofs)
    release(strategy, state)
    assert not strategy._removed_wait_requests
    assert state.flip_owner_opportunity_id == 0 and state.flip_owner_phase == "idle"


def test_real_episode_after_false_compensation_still_consumes_one_trade_forever():
    strategy, state, clock, proofs, _ = tcrt_runtime()
    state.flip_owner_phase = "consumed"
    state.retry_one_closes_in_segment = 1
    settle(strategy, state, clock, proofs)
    release(strategy, state)
    assert strategy._falseflip_effective_closes(state, 1) == 0
    strategy._falseflip_restoring_track(state)
    plan, = strategy.pending_falseflip2_rearms()
    assert strategy.acknowledge_falseflip2_rearm(plan)
    ack(strategy)
    state.retry_one_closes_in_segment = 2  # native ledger consumes the subsequent REAL close
    state.flip_owner_phase = "consumed"
    assert strategy._falseflip_effective_closes(state, 2) == 1
    assert not strategy._strict_first_rest_admitted(state, slot="first")


def test_native_veea_false_episode_after_both_closed_rows_with_controlled_reads_rearms_rth():
    packet = json.loads((Path(__file__).parents[1] / "fixtures/falseflip2_veea_20261009.json").read_text())
    proofs = tuple(Classification.from_payload(row["entry_classification"]) for row in packet["managed"])
    closed_at = max(datetime.fromisoformat(row["updated_at"]) for row in packet["managed"])
    assert closed_at == datetime(2026, 10, 9, 18, 18, 26, 458763, tzinfo=UTC)
    at = closed_at + timedelta(seconds=1)
    # Missing historical SELL identity is explicitly controlled, never reconstructed as fact.
    segment = proofs[0].bar.bar_ms - 600000
    record = FlipEntryOwnershipRecord("VEEA", proofs[0].identity.opportunity_id, "unknown", 0, 0,
        ACCOUNTS, {p.identity.account:p.identity.managed_row_id for p in proofs},
        {p.identity.account:p.identity.fill_ms for p in proofs}, segment, 0)
    strategy, state, _, clock, _ = strategy_for_record(record, at, pm=False, quantities=(108, 54))
    strategy.settings.strategy_schwab_1m_v2_false_flip_enabled = True
    strategy.settings.strategy_schwab_1m_v2_false_flip2_enabled = True
    strategy.configure_falseflip({}, readable=True)
    state.resting_active = state.webull_resting_active = False
    state.atr_state, state.atr_trail, state.atr_state_age = "short", float(proofs[0].bar.trail), 10
    state.bars.append(OHLCVBar(clock[0]//60000*60000-60000, 5.52, 5.53, 5.51, 5.52, 10000))
    strategy._resting_in_window = lambda now=None: True
    strategy._resting_session_is_eh = lambda now=None: False
    strategy._liquidity_floor_ok = lambda _: True  # volume tape for that poll was not retained
    strategy._resting_stop_ask_allows = lambda *_args, **_kwargs: True
    settle(strategy, state, clock, proofs)
    release(strategy, state)
    strategy._falseflip_restoring_track(state)
    plan, = strategy.pending_falseflip2_rearms()
    assert strategy.acknowledge_falseflip2_rearm(plan)
    assert len(strategy.drain_pending_intents()) == len(strategy.drain_webull_direct_intents()) == 1


def test_fresh_sell_drops_stale_false_rearm_and_starts_new_native_budget():
    strategy, state, clock, proofs, _ = tcrt_runtime()
    settle(strategy, state, clock, proofs)
    release(strategy, state)
    clock[0] = (clock[0] // 60000 + 2) * 60000
    state.bars.append(replace(state.bars[-1], timestamp_ms=clock[0]-60000))
    state.atr_short_flip_bar_ts = state.bars[-1].timestamp_ms
    strategy._retry_one_start_segment_on_sell(state, live=True)
    assert state.retry_one_segment_id != SEGMENT
    assert strategy._falseflip_budget(state).episodes == ()
    state.atr_state_age = 1
    strategy._falseflip_restoring_track(state)
    assert not strategy.pending_falseflip2_rearms() and not state.resting_active


def test_superseded_false_cancel_ack_never_poison_global_restore_or_false_budget_readability():
    strategy, state, clock, proofs, _ = tcrt_runtime()
    request = RemovedWait("TCRT", OPPORTUNITY, "controlled-superseded", clock[0], ACCOUNTS, "false_flip_restore")
    strategy._falseflip_cancel_publications["TCRT"] = request
    state.flip_owner_opportunity_id = state.fanout_segment_id = OPPORTUNITY + 1
    assert not strategy.falseflip2_cancel_valid(request)
    strategy.acknowledge_falseflip_cancel_publication(request)
    assert strategy._falseflip_readable and strategy._removed_wait_restore_readable
    assert not strategy._removed_wait_requests


def test_g3_and_core_atr_setters_unchanged_from_frozen_base():
    import ast
    import hashlib

    # C1 LATEST C11821 2026-10-27 18:21: reviewed L12 strict-first branch is
    # expected. Pin its exact 20dc AST (CI40579469080); retry-one stays frozen.
    original = {
        "_strict_first_rest_admitted": "11063d9514a8cf920c060eb0c3501091e9c6127ed20677f693d72c06bffff922",
        # SLOTCAP-20261021: BUY flip clears slotclear_deferred_sell; ATR math unchanged
        # (claude-1 review 21:10:48 ET).
        # C11835 / operator 10-22 ~11:58 HALTGAP, reviewed L11 KEEPWATCH:
        # only attested jointly-empty gaps span; otherwise the clamp is unchanged.
        "_update_atr_state": "c88c21fa6c418ab0878884331da19f659410ddae1b7f2540cfc7e3d419259e17",
        "_retry_one_start_segment_on_sell": "88c1377d5552662ba2bd7597603dca7e403ba03bde67690bc0325d0f2006afe2",
    }
    after = ast.parse(inspect.getsource(__import__("project_mai_tai.strategy_core.schwab_1m_v2", fromlist=["_"])))
    for name, digest in original.items():
        current = [n for n in ast.walk(after) if isinstance(n, ast.FunctionDef) and n.name == name]
        assert hashlib.sha256(ast.dump(current[0]).encode()).hexdigest() == digest
