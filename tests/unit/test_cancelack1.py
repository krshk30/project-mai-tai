"""Synthetic obligation controls, NOT SAIQ/POM native or historical replay."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, DashboardSnapshot, Strategy, TradeIntent, VirtualPosition
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.v2_removed_wait import DISPATCH_SNAPSHOT_TYPE, SNAPSHOT_TYPE
from tests.unit.test_v2_fresh_sell_owner_release import (
    MIRROR, PRIMARY, commit_and_later_bar, instant, refresh, sell, setup,
)


def synthetic_case(monkeypatch, *, pm=False, required=(MIRROR,), acknowledged=None, purpose="retry_exhausted"):
    bot, state, clock, old, writes, engine = setup(monkeypatch, pm=pm, purpose=purpose)
    # The existing PM fixture shifts bar/request time but not the opportunity ID.
    # This NEW synthetic scenario shifts the entire identity/time envelope too.
    opportunity = old.opportunity_id - (6 * 60 * 60_000 if pm else 0)
    request = replace(old, opportunity_id=opportunity)
    bot.strategy._removed_wait_requests[request.symbol] = request
    state.flip_owner_opportunity_id = opportunity
    if state.fanout_segment_id == old.opportunity_id:
        state.fanout_segment_id = opportunity
    if bot.strategy._restored_fanout_segment_ids.get(request.symbol) == old.opportunity_id:
        bot.strategy._restored_fanout_segment_ids[request.symbol] = opportunity
    state.flip_owner_fill_accounts = set(required)
    state.flip_owner_position_ids = {name: f"SYNTHETIC-closed-{name}" for name in required}
    state.flip_owner_position_entry_ms = {name: opportunity + 1 for name in required}
    acknowledged = set(required if acknowledged is None else acknowledged)
    with bot.session_factory() as session:
        accounts = {row.name: row for row in session.scalars(select(BrokerAccount))}
        own = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        for row in session.scalars(select(DashboardSnapshot)):
            if row.snapshot_type == SNAPSHOT_TYPE:
                row.payload = request.payload(active=True)
            elif row.snapshot_type == "v2_flip_entry_ownership":
                row.payload = {**row.payload, "opportunity_id": str(opportunity),
                    "fill_accounts": list(required), "position_ids": dict(state.flip_owner_position_ids),
                    "position_entry_ms": {name: str(opportunity + 1) for name in required}}
        for row in session.scalars(select(TradeIntent)):
            name = next(name for name, account in accounts.items() if account.id == row.broker_account_id)
            if name not in acknowledged:
                session.delete(row)
                continue
            md = {**row.payload["metadata"], "clearwait_opportunity_id": str(opportunity),
                  "fanout_segment_id": str(opportunity), "fanout_slot_id": fanout_slot_id(
                      strategy_code="schwab_1m_v2", symbol=request.symbol, segment_id=opportunity, slot="resting")}
            row.payload = {**row.payload, "metadata": md}
        begin = {"schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": request.symbol,
                 "opportunity_id": str(opportunity), "account_names": list(request.account_names),
                 "kind": "begin", "attempt_token": "", "account_name": ""}
        session.add(DashboardSnapshot(snapshot_type=DISPATCH_SNAPSHOT_TYPE, payload=begin,
                                      created_at=instant(opportunity)))
        for index, name in enumerate(required):
            token = f"SYNTHETIC-dispatch-{index}"
            at = opportunity + 10 + index
            session.add(DashboardSnapshot(snapshot_type=DISPATCH_SNAPSHOT_TYPE,
                payload={**begin, "kind": "attempt", "attempt_token": token, "account_name": name},
                created_at=instant(at)))
            md = {"fanout_segment_id": str(opportunity), "clearwait_dispatch_token": token}
            parent = TradeIntent(id=uuid4(), strategy_id=own.id, broker_account_id=accounts[name].id,
                symbol=request.symbol, side="buy", intent_type="open", quantity=1,
                reason="SYNTHETIC prior BUY obligation", status="cancelled", payload={"metadata": md},
                created_at=instant(at + 1), updated_at=instant(request.requested_at_ms + 1))
            session.add(parent)
            session.flush()
            session.add(BrokerOrder(strategy_id=own.id, broker_account_id=accounts[name].id,
                intent_id=parent.id, symbol=request.symbol, side="buy", quantity=1, status="cancelled",
                client_order_id=f"SYNTHETIC-buy-{index}", order_type="limit", time_in_force="day",
                payload=md, submitted_at=instant(at + 1), updated_at=instant(request.requested_at_ms + 1)))
        session.commit()
    refresh(bot, clock)
    return bot, state, clock, request, writes, engine


@pytest.mark.asyncio
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("required", [(MIRROR,), (PRIMARY,), (PRIMARY, MIRROR)],
                         ids=["webull-only", "schwab-only", "legacy-two"])
@pytest.mark.parametrize("purpose", ["retry_exhausted", "false_flip_restore"])
@pytest.mark.parametrize("no_target", [False, True])
async def test_positive_scope_releases_only_after_fresh_sell_and_both_flat(
    monkeypatch, pm, required, purpose, no_target,
):
    bot, state, clock, request, writes, engine = synthetic_case(
        monkeypatch, pm=pm, required=required, purpose=purpose)
    try:
        if not no_target:
            with bot.session_factory() as session:
                for row in session.scalars(select(TradeIntent).where(TradeIntent.intent_type == "cancel")):
                    row.status = "cancelled"
                    row.payload = {"metadata": row.payload["metadata"]}
                session.commit()
        await bot._fresh_sell_owner_release_poll()
        assert bot.strategy._fresh_sell_release_evidence[request.symbol].clear
        assert state.flip_owner_phase == "bound" and bot.strategy._removed_wait_gate_closed(request.symbol)
        assert not bot.strategy.drain_pending_intents() and not bot.strategy.drain_webull_direct_intents()
        sell(bot, state)
        assert state.flip_owner_phase == "bound"  # Off-loop commit is still mandatory.
        await commit_and_later_bar(bot, state, clock)
        assert state.flip_owner_phase == "idle" and not bot.strategy._removed_wait_requests
        assert bot._removed_wait_store.restore() == {} and not writes
        assert state.fanout_last_retired_opportunity_id == request.opportunity_id
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("fault", [
    "missing_journal", "partial_attempt", "missing_buy", "orphan_order", "wrong_opportunity",
    "duplicate_begin", "late_attempt", "owner_other_leg", "legacy_missing_other_ack",
    "pending_cancel", "unknown_cancel", "unknown_terminal_cancel", "wrong_token", "wrong_reason", "not_buy_only",
    "no_target_wrong_origin", "no_target_wrong_code", "settling", "other_position",
    "other_orb_buy", "other_unknown_intent", "other_receipt_stale", "other_receipt_missing",
    "missing_owner", "owner_position_other_leg", "missing_attempt", "duplicate_attempt",
    "journal_universe", "late_buy", "late_other_attempt", "no_ack",
    "requested_terminal_cancel", "unrecognized_terminal_cancel",
])
async def test_unknown_or_missing_obligation_and_existing_safety_gates_hold(monkeypatch, pm, fault):
    required = (PRIMARY, MIRROR) if fault == "legacy_missing_other_ack" else (MIRROR,)
    bot, state, clock, request, writes, engine = synthetic_case(
        monkeypatch, pm=pm, required=required, acknowledged=(MIRROR,))
    try:
        with bot.session_factory() as session:
            journal = list(session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == DISPATCH_SNAPSHOT_TYPE)))
            owner = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_flip_entry_ownership"))
            cancel = session.scalar(select(TradeIntent).where(TradeIntent.intent_type == "cancel"))
            parent = session.scalar(select(TradeIntent).where(TradeIntent.intent_type == "open"))
            primary = session.scalar(select(BrokerAccount).where(BrokerAccount.name == PRIMARY))
            own = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
            foreign = session.scalar(select(Strategy).where(Strategy.code == "orb_schwab"))
            if fault == "missing_journal":
                for row in journal:
                    session.delete(row)
            elif fault == "missing_owner":
                session.delete(owner)
            elif fault == "owner_position_other_leg":
                owner.payload = {**owner.payload, "position_ids": {PRIMARY: "SYNTHETIC-other-leg"}}
            elif fault == "missing_attempt":
                session.delete(next(row for row in journal if row.payload["kind"] == "attempt"))
            elif fault == "duplicate_attempt":
                attempt = next(row for row in journal if row.payload["kind"] == "attempt")
                session.add(DashboardSnapshot(snapshot_type=DISPATCH_SNAPSHOT_TYPE,
                    payload=dict(attempt.payload), created_at=attempt.created_at))
            elif fault == "journal_universe":
                journal[0].payload = {**journal[0].payload, "account_names": [MIRROR]}
            elif fault == "late_buy":
                parent.created_at = instant(request.requested_at_ms + 1)
            elif fault == "late_other_attempt":
                session.add(DashboardSnapshot(snapshot_type=DISPATCH_SNAPSHOT_TYPE,
                    payload={**next(row.payload for row in journal if row.payload["kind"] == "begin"),
                             "kind": "attempt", "attempt_token": "SYNTHETIC-late-other-leg",
                             "account_name": PRIMARY}, created_at=instant(request.requested_at_ms + 1)))
            elif fault == "no_ack":
                session.delete(cancel)
            elif fault == "partial_attempt":
                session.add(DashboardSnapshot(snapshot_type=DISPATCH_SNAPSHOT_TYPE,
                    payload={**next(row.payload for row in journal if row.payload["kind"] == "begin"),
                             "kind": "attempt", "attempt_token": "SYNTHETIC-unmatched",
                             "account_name": PRIMARY}, created_at=instant(request.opportunity_id + 20)))
            elif fault == "missing_buy":
                for row in session.scalars(select(BrokerOrder).where(BrokerOrder.intent_id == parent.id)):
                    session.delete(row)
                session.delete(parent)
            elif fault == "orphan_order":
                row = session.scalar(select(BrokerOrder))
                row.intent_id = None
            elif fault == "wrong_opportunity":
                parent.payload = {"metadata": {**parent.payload["metadata"], "fanout_segment_id": "0"}}
            elif fault == "duplicate_begin":
                session.add(DashboardSnapshot(snapshot_type=DISPATCH_SNAPSHOT_TYPE,
                    payload=next(row.payload for row in journal if row.payload["kind"] == "begin"),
                    created_at=instant(request.opportunity_id)))
            elif fault == "late_attempt":
                next(row for row in journal if row.payload["kind"] == "attempt").created_at = instant(
                    request.requested_at_ms + 1)
            elif fault == "owner_other_leg":
                owner.payload = {**owner.payload, "fill_accounts": [PRIMARY]}
            elif fault == "pending_cancel":
                cancel.status = "pending"
            elif fault == "unknown_cancel":
                cancel.status = "rejected"
                cancel.payload = {**cancel.payload, "refusal_origin": "could_not_tell"}
            elif fault in {"unknown_terminal_cancel", "requested_terminal_cancel", "unrecognized_terminal_cancel"}:
                outcome = {"unknown_terminal_cancel": "could_not_tell",
                           "requested_terminal_cancel": "requested",
                           "unrecognized_terminal_cancel": "UNKNOWN-future-outcome"}[fault]
                cancel.status = "cancelled"
                cancel.payload = {**cancel.payload, "metadata": {**cancel.payload["metadata"],
                                                                 "cancel_outcome": outcome}}
            elif fault in {"wrong_token", "wrong_reason", "not_buy_only"}:
                key, value = {"wrong_token": ("clearwait_removal_token", "wrong"),
                    "wrong_reason": ("reason", "scanner_removal"), "not_buy_only": ("clearwait_buy_only", "false")}[fault]
                cancel.payload = {**cancel.payload, "metadata": {**cancel.payload["metadata"], key: value}}
            elif fault == "no_target_wrong_origin":
                cancel.payload = {**cancel.payload, "refusal_origin": "client_abort"}
            elif fault == "no_target_wrong_code":
                cancel.payload = {**cancel.payload, "refusal_code": "other"}
            elif fault == "settling":
                cancel.updated_at = instant(clock[0] - 14_999)
            elif fault == "other_position":
                session.add(VirtualPosition(strategy_id=own.id, broker_account_id=primary.id,
                    symbol=request.symbol, quantity=1, average_price=1))
            elif fault == "other_orb_buy":
                session.add(BrokerOrder(strategy_id=foreign.id, broker_account_id=primary.id,
                    symbol=request.symbol, side="buy", quantity=1, status="accepted",
                    client_order_id="SYNTHETIC-ORB-working", order_type="limit", time_in_force="day"))
            elif fault == "other_unknown_intent":
                session.add(TradeIntent(strategy_id=foreign.id, broker_account_id=primary.id,
                    symbol=request.symbol, side="buy", intent_type="open", quantity=1,
                    reason="SYNTHETIC shared owner", status="unknown", payload={}))
            session.commit()
        if fault == "other_receipt_stale":
            refresh(bot, clock, age=15_001)
        elif fault == "other_receipt_missing":
            monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.load_receipts",
                lambda session: {MIRROR: {"writer": "oms.sync_broker_positions", "read_at": instant(clock[0])}})
        await bot._fresh_sell_owner_release_poll()
        proof = bot.strategy._fresh_sell_release_evidence[request.symbol]
        assert not proof.clear
        sell(bot, state)
        assert state.flip_owner_phase == "bound" and bot.strategy._removed_wait_requests[request.symbol] == request
        assert writes == [] and not bot.strategy._strict_first_rest_admitted(state, slot="first")
        assert not bot.strategy.drain_pending_intents() and not bot.strategy.drain_webull_direct_intents()
    finally:
        engine.dispose()
