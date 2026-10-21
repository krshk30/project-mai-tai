"""Source-authored durable-history controls; not original-case/native receipts."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from project_mai_tai.cancelack1 import ObligationScope, load_flat_obligations, load_proven_accounts, working_buy_present
from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill,
    OmsManagedPosition, Strategy, TradeIntent,
)
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Config, SchwabV2Strategy
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipStore
from project_mai_tai.v2_removed_wait import DISPATCH_SNAPSHOT_TYPE, RemovedWaitStore
from tests.unit.test_cancelack1 import synthetic_case
from tests.unit.test_cancelack1_four_sites import confirmation_case, terminal_buys
from tests.unit.test_v2_fresh_sell_owner_release import (
    MIRROR, PRIMARY, commit_and_later_bar, instant, refresh, sell,
)


def old_abort(bot, request, *, writer="rpg", fault="none"):
    """Explicit synthetic inputs matching the persisted writer, not a bare status."""
    with bot.session_factory() as session:
        strategy = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == PRIMARY))
        parent_id, event_id = uuid4(), str(uuid4())
        at = instant(request.opportunity_id - 60_000)
        if writer == "momentum":
            strategy = Strategy(code="momentum_warn_live", name="synthetic prewire writer")
            session.add(strategy)
            session.flush()
            receipt = {"reason": "refused_before_wire", "refusal": "synthetic", "context": {}}
            payload = {"momlive_parent": True, "momlive_episode": {
                "episode_id": str(parent_id), "state": "terminal_no_fill", "filled": "0", "exited": "0"},
                "momstuck2_refusal": receipt}
            md = {"episode_id": str(parent_id), "momlive_entry_never_dispatched": True,
                  "momstuck2_refusal": receipt}
        else:
            payload = {"event_id": event_id, "refusal_origin": "client_abort",
                       "refusal_code": "rpg_old_buy_still_owned"}
            md = {"refusal_origin": "client_abort", "refusal_code": "rpg_old_buy_still_owned",
                  "rpg_local_abort_no_wire": "true", "rpg_abort_event_id": event_id}
        parent = TradeIntent(id=parent_id, strategy_id=strategy.id, broker_account_id=account.id,
            symbol=request.symbol, side="buy", intent_type="open", quantity=1, reason="synthetic old abort",
            status="cancelled" if writer == "momentum" else "aborted", payload=payload,
            created_at=at, updated_at=at)
        session.add(parent)
        session.flush()
        order = BrokerOrder(strategy_id=strategy.id, broker_account_id=account.id, intent_id=parent.id,
            symbol=request.symbol, side="buy", quantity=1, status="aborted", payload=md,
            client_order_id=f"SYNTHETIC-old-abort-{parent.id}", order_type="limit", time_in_force="day",
            submitted_at=None, updated_at=at)
        session.add(order)
        session.flush()
        if writer == "rpg":
            session.add(BrokerOrderEvent(order_id=order.id, event_type="aborted", event_source="client",
                event_at=at, payload={"reason": md["refusal_code"], "metadata": dict(md)}))
        if fault == "bare_status":
            order.payload = {}
        elif fault == "native_id":
            order.broker_order_id = "SYNTHETIC-possible-wire"
        elif fault == "permit":
            order.payload = {**order.payload, "bot_control_permit": "SYNTHETIC-may-have-sent"}
        elif fault == "unknown_event":
            session.add(BrokerOrderEvent(order_id=order.id, event_type="rejected", event_source="unknown",
                event_at=instant(request.opportunity_id - 59_000), payload={}))
        elif fault == "working_replacement":
            session.add(BrokerOrder(strategy_id=strategy.id, broker_account_id=account.id,
                intent_id=parent.id, symbol=request.symbol, side="buy", quantity=1, status="accepted",
                client_order_id=f"SYNTHETIC-working-replacement-{parent.id}", order_type="limit",
                time_in_force="day", submitted_at=at, updated_at=at))
        flag_modified(order, "updated_at")
        session.commit()
        return order.id


def later_closed_episode(bot, request, clock, name, *, fault="none"):
    at = clock[0] + 60_000
    segment, token = at, f"SYNTHETIC-later-{uuid4()}"
    with bot.session_factory() as session:
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == name))
        strategy = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        if fault == "same_token":
            token = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == DISPATCH_SNAPSHOT_TYPE,
                DashboardSnapshot.payload["kind"].as_string() == "attempt")).payload["attempt_token"]
        begin = {"schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": request.symbol,
            "opportunity_id": str(segment), "account_names": list(request.account_names),
            "kind": "begin", "attempt_token": "", "account_name": ""}
        for offset, payload in ((0, begin), (1, {**begin, "kind": "attempt", "attempt_token": token,
                                                "account_name": name})):
            session.add(DashboardSnapshot(snapshot_type=DISPATCH_SNAPSHOT_TYPE, payload=payload,
                                          created_at=instant(at + offset)))
        session.add(DashboardSnapshot(snapshot_type="v2_flip_entry_ownership",
            payload={"schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": request.symbol,
                "opportunity_id": str(segment), "fill_accounts": [name], "position_ids": {},
                "active": False, "phase": "idle"}, created_at=instant(at + 30_000)))
        md = {"fanout_segment_id": str(segment), "clearwait_dispatch_token": token}
        parent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id, symbol=request.symbol,
            side="buy", intent_type="open", quantity=1, reason="synthetic later closed episode",
            status="filled", payload={"metadata": md}, created_at=instant(at + 2), updated_at=instant(at + 30_000))
        session.add(parent)
        session.flush()
        order = BrokerOrder(strategy_id=strategy.id, broker_account_id=account.id, intent_id=parent.id,
            symbol=request.symbol, side="buy", quantity=1, status="filled", payload=md,
            client_order_id=f"SYNTHETIC-later-{parent.id}", broker_order_id=f"SYNTHETIC-native-{parent.id}",
            order_type="limit", time_in_force="day", submitted_at=instant(at + 2), updated_at=instant(at + 30_000))
        session.add(order)
        session.flush()
        session.add(BrokerOrderEvent(order_id=order.id, event_source="broker", event_type="filled",
            event_at=instant(at + 3), payload={}))
        session.add(Fill(order_id=order.id, strategy_id=strategy.id, broker_account_id=account.id,
            symbol=request.symbol, side="buy", quantity=1, price=1,
            broker_fill_id=f"SYNTHETIC-fill-{parent.id}", filled_at=instant(at + 3)))
        session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=name,
            symbol=request.symbol, entry_order_id=order.id, entry_client_order_id=order.client_order_id,
            entry_price=1, original_quantity=1, current_quantity=0, status="closed"))
        if fault == "missing_segment":
            parent.payload = {"metadata": {"clearwait_dispatch_token": token}}
        elif fault == "same_episode":
            parent.payload = {"metadata": {**md, "fanout_segment_id": str(request.opportunity_id)}}
        elif fault == "wrong_order_token":
            order.payload = {**md, "clearwait_dispatch_token": "SYNTHETIC-wrong"}
        elif fault == "orphan_order":
            order.intent_id = None
        elif fault == "possibly_sent":
            order.status = "unknown"
        elif fault == "still_managed":
            row = next(row for row in session.new if isinstance(row, OmsManagedPosition))
            row.status, row.current_quantity = "open", 1
        flag_modified(order, "updated_at")
        flag_modified(parent, "updated_at")
        session.commit()
    clock[0] = at + 120_000
    refresh(bot, clock)


@pytest.mark.parametrize("required", [(PRIMARY,), (MIRROR,)])
@pytest.mark.parametrize("fault", ["none", "missing_segment", "same_episode", "same_token",
                                   "wrong_order_token", "orphan_order", "possibly_sent", "still_managed"])
def test_real_query_later_closed_episode_does_not_poison_old_confirmation(monkeypatch, required, fault):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, required=required)
    try:
        entry = confirmation_case(bot, request, clock, required, "none")
        later_closed_episode(bot, request, clock, required[0], fault=fault)
        clock[0] += 24 * 60 * 60_000
        refresh(bot, clock)
        assert (entry.order_id in bot._closed_confirmation_entry_ids([entry])) is (fault == "none")
        with bot.session_factory() as session:
            accounts = {a.id: a.name for a in session.scalars(select(BrokerAccount))}
            strategy_id = session.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
            scope = ObligationScope(request.symbol, request.opportunity_id, clock[0], request.account_names)
            if fault == "none":
                assert load_proven_accounts(session, scope, accounts, strategy_id) == frozenset(required)
            else:
                with pytest.raises(ValueError):
                    load_proven_accounts(session, scope, accounts, strategy_id)
        assert state.flip_owner_phase == "bound" and writes == []
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("required", [(PRIMARY,), (MIRROR,), (PRIMARY, MIRROR)])
@pytest.mark.parametrize("writer", ["rpg", "momentum"])
@pytest.mark.parametrize("fault", ["none", "bare_status", "native_id", "permit", "unknown_event", "working_replacement"])
async def test_old_proven_abort_not_working_but_unknown_and_replacement_still_hold(
    monkeypatch, pm, required, writer, fault,
):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, pm=pm, required=required)
    try:
        old_abort(bot, request, writer=writer, fault=fault)
        with bot.session_factory() as session:
            assert working_buy_present(session, request.symbol, now=instant(clock[0])) is (fault != "none")
        await bot._fresh_sell_owner_release_poll()
        assert bot.strategy._fresh_sell_release_evidence[request.symbol].clear is (fault == "none")
        sell(bot, state)
        assert state.flip_owner_phase == "bound" and writes == []
        if fault == "none":
            await commit_and_later_bar(bot, state, clock)
            assert state.flip_owner_phase == "idle" and bot._removed_wait_store.restore() == {}
        else:
            assert bot.strategy._removed_wait_requests[request.symbol] == request
        assert not bot.strategy.drain_pending_intents() and not bot.strategy.drain_webull_direct_intents()
    finally:
        engine.dispose()


@pytest.mark.parametrize("required", [(PRIMARY,), (MIRROR,), (PRIMARY, MIRROR)])
@pytest.mark.parametrize("working", [False, True])
def test_complete_two_vp_retirement_still_checks_global_working_buy(monkeypatch, required, working):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, required=required)
    try:
        entry = confirmation_case(bot, request, clock, required, "none")
        old_abort(bot, request, fault="working_replacement" if working else "none")
        clock[0] += 24 * 60 * 60_000
        refresh(bot, clock)
        assert (entry.order_id in bot._closed_confirmation_entry_ids([entry])) is not working
        assert state.flip_owner_phase == "bound" and writes == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("fault", ["none", "unlinked", "unknown_job", "working_successor",
                                   "mismatched_account", "unknown_event", "already_absent",
                                   "accepted_cancel_request", "tied_working_event"])
def test_resolved_replacement_needs_old_and_successor_proof_not_status(monkeypatch, fault):
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch)
    try:
        old_id = old_abort(bot, request)
        new_id = old_abort(bot, request)
        with bot.session_factory() as session:
            old, successor = session.get(BrokerOrder, old_id), session.get(BrokerOrder, new_id)
            parent = session.get(TradeIntent, old.intent_id)
            flag_modified(parent, "updated_at")
            parent.status = "completed"
            flag_modified(old, "updated_at")
            old.status, old.broker_order_id = "replaced", "SYNTHETIC-old-native"
            old.submitted_at = instant(request.opportunity_id - 61_000)
            outcome = {"already_absent": "already_absent",
                       "accepted_cancel_request": "confirmed_after_accepted_request"}.get(fault, "confirmed")
            session.add(BrokerOrderEvent(order_id=old.id, event_type="cancelled", event_source="broker",
                event_at=instant(request.opportunity_id - 59_000), payload={"cancel_outcome": outcome}))
            def envelope(order):
                return {"client_order_id": order.client_order_id, "symbol": request.symbol, "side": "buy",
                        "broker_account_name": PRIMARY, "strategy_code": "schwab_1m_v2", "quantity": "1"}
            payload = {"phase": "expired", "old": envelope(old), "replacement": envelope(successor)}
            if fault == "unknown_job":
                payload["phase"] = "submit_unknown"
            elif fault == "mismatched_account":
                payload["replacement"]["broker_account_name"] = MIRROR
            elif fault == "working_successor":
                flag_modified(successor, "updated_at")
                successor.status = "accepted"
            elif fault == "unknown_event":
                session.add(BrokerOrderEvent(order_id=successor.id, event_type="rejected", event_source="unknown",
                    event_at=instant(request.opportunity_id - 59_000), payload={}))
            elif fault == "tied_working_event":
                session.add(BrokerOrderEvent(order_id=old.id, event_type="accepted", event_source="broker",
                    event_at=instant(request.opportunity_id - 59_000), payload={}))
            if fault != "unlinked":
                session.add(DashboardSnapshot(snapshot_type="atr_reprice_handoff", payload=payload,
                                              created_at=instant(request.opportunity_id - 58_000)))
            session.commit()
        with bot.session_factory() as session:
            assert working_buy_present(session, request.symbol, now=instant(clock[0])) is (fault != "none")
        assert state.flip_owner_phase == "bound" and writes == []
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [(PRIMARY,), (MIRROR,)])
@pytest.mark.parametrize("roll", [False, True], ids=["mid-session", "across-04ET"])
@pytest.mark.parametrize("working", [False, True])
async def test_dispose_reopen_db_history_not_ram_proves_restart_barrier(monkeypatch, tmp_path, required, roll, working):
    import tests.unit.test_v2_fresh_sell_owner_release as original

    database = f"sqlite:///{tmp_path / 'restart.sqlite'}"
    monkeypatch.setattr(original, "create_engine", lambda _url, **kwargs: create_engine(database, **kwargs))
    bot, state, clock, request, writes, engine = synthetic_case(monkeypatch, required=required)
    settings = bot.settings
    terminal_buys(bot, request)
    old_abort(bot, request, fault="working_replacement" if working else "none")
    FanoutSegmentIdentityStore(bot.session_factory).record(request.symbol, request.opportunity_id,
        True, "synthetic persisted restart identity", now=instant(request.opportunity_id))
    with bot.session_factory() as session:
        owner = session.scalar(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "v2_flip_entry_ownership"))
        owner.created_at = instant(request.opportunity_id)
        session.commit()
    # Explicitly cross the next 04:00 ET; this October fixture is EDT (UTC-4).
    if roll:
        next_day = instant(clock[0]).replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
        clock[0] = int(next_day.timestamp() * 1000) + 1
    refresh(bot, clock)
    engine.dispose()
    del bot, state, engine
    reopened = create_engine(database)
    try:
        factory = sessionmaker(reopened, expire_on_commit=False)
        restored = RemovedWaitStore(factory).restore()
        assert restored[request.symbol] == request
        fresh = SchwabV2BotService.__new__(SchwabV2BotService)
        fresh.settings, fresh.session_factory = settings, factory
        fresh.strategy = SchwabV2Strategy(settings, config=SchwabV2Config(macd_warmup_settling_bars=0))
        fresh.strategy._now_ms = lambda: clock[0]
        fresh.strategy._boot_ms = clock[0] - 60_000
        segments = FanoutSegmentIdentityStore(factory)
        owners = FlipEntryOwnershipStore(factory)
        active = segments.restore_active(now=instant(clock[0]))
        restored_owners = owners.restore_active(now=instant(clock[0]))
        assert bool(active) is not roll and bool(restored_owners) is not roll
        fresh.strategy.configure_fanout_identity_persistence(segments.record, restored=active)
        fresh.strategy.configure_flip_entry_ownership(
            lambda record, active, reason: owners.record(record, active=active, reason=reason, now=instant(clock[0])),
            active_segments=active, restored=restored_owners)
        fresh._removed_wait_store = RemovedWaitStore(factory)
        fresh.strategy.configure_removed_wait(fresh._removed_wait_store.record, restored=restored, readable=True)
        fresh.strategy.drain_pending_intents()
        fresh.strategy.drain_webull_direct_intents()
        assert fresh.strategy._cancelack_obligations == {}
        assert fresh.strategy._fresh_sell_release_evidence == {}
        evidence, = fresh._fetch_fresh_sell_release_evidence((restored[request.symbol],))
        assert evidence.clear is not working, evidence.reason
        with factory() as session:
            accounts = {a.id: a.name for a in session.scalars(select(BrokerAccount))}
            strategy_id = session.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
            scope = ObligationScope(request.symbol, request.opportunity_id, request.requested_at_ms, request.account_names)
            if working:
                with pytest.raises(ValueError, match="position_or_buy_held"):
                    load_flat_obligations(session, scope, accounts, strategy_id, instant(clock[0]), 15_000)
            else:
                proof = load_flat_obligations(session, scope, accounts, strategy_id, instant(clock[0]), 15_000)
                assert proof.required_accounts == frozenset(required)
        fresh_state = fresh.strategy.watchlist_state(request.symbol)
        refresh(fresh, clock)
        if roll:
            # Session-scoped owner/segment stores correctly do not resurrect yesterday.
            assert fresh_state.flip_owner_phase == "idle"
            await fresh._removed_wait_poll()
        else:
            assert fresh_state.flip_owner_phase == "bound"
            fresh_state.retry_one_watch_start_ms = fresh.strategy._boot_ms
            fresh_state.cw_resting_taken = fresh_state.cw_reclaim_taken = True
            bar_at = clock[0] - 60_000
            fresh_state.bars.append(OHLCVBar(bar_at, 2, 2.1, 1.9, 2, 100_000))
            fresh_state.atr_short_flip_bar_ts = bar_at
            await fresh._fresh_sell_owner_release_poll()
            sell(fresh, fresh_state)
            assert fresh_state.flip_owner_phase == "bound"
            if not working:
                await commit_and_later_bar(fresh, fresh_state, clock)
                assert fresh_state.flip_owner_phase == "idle"
        assert (request.symbol in fresh.strategy._removed_wait_requests) is working
        assert (request.symbol in fresh._removed_wait_store.restore()) is working
        assert not fresh.strategy.drain_pending_intents() and not fresh.strategy.drain_webull_direct_intents()
        assert writes == []
    finally:
        reopened.dispose()
