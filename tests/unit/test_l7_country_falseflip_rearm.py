"""COUNTRY x FALSEFLIP2 integration controls, authored without execution.

WORX/SAIQ release clocks come from the retained 2026-10-20 producer packet.
Database identities/executions are explicitly controlled, remapped TCRT fixture
rows, NOT purported WORX/SAIQ vendor receipts. Both position-read receipts are
controlled. GLND/WETO labels exercise the previously successful rearm shape.
"""

from dataclasses import replace
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
import threading

import pytest
from sqlalchemy import BigInteger, cast, select

from project_mai_tai.atr_country_route import AtrCountryRouteBook, CountryRouteRefusal
from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, Fill, OmsManagedPosition
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.falseflip1_runtime import BUDGET_TYPE, FalseBudget, FalseFlipStore
from project_mai_tai.momentum_live_sec import CountryRoute
from project_mai_tai.positions_read_receipt import record_receipts
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.services import schwab_1m_v2_bot as service_module
from project_mai_tai.strategy_core.schwab_1m_v2 import Quote, SchwabV2Strategy
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipStore
from project_mai_tai.v2_removed_wait import RemovedWaitStore
from tests.unit.test_falseflip2_release import (
    ACCOUNTS, AT, OPPORTUNITY, SEGMENT, apply_book, captured_database,
    release, settle, tcrt_runtime,
)


RELEASE_CLOCKS = {
    "WORX": datetime(2026, 10, 20, 12, 22, 7, 429000, tzinfo=UTC),
    "SAIQ": datetime(2026, 10, 20, 12, 3, 24, tzinfo=UTC),
    "GLND": AT,
    "WETO": AT,
}


def country_case(symbol="WORX", broker="schwab", pm=True):
    engine, factory = captured_database()
    strategy, state, clock, proofs, _ = tcrt_runtime()
    # Move retained scalar relationships by whole minutes (bar alignment stays
    # valid). The new observation time is controlled, after all remapped fills.
    observed = RELEASE_CLOCKS[symbol]
    if not pm:
        observed = observed.replace(hour=14)
    shift = timedelta(minutes=int((observed-AT).total_seconds() // 60))
    delta = int(shift.total_seconds()*1000)
    old_symbol = state.symbol
    strategy._symbol_states.pop(old_symbol)
    state.symbol = symbol
    strategy._symbol_states[symbol] = state
    state.retry_one_segment_id = state.atr_short_flip_bar_ts = SEGMENT + delta
    state.flip_owner_retry_segment_id = SEGMENT + delta
    state.flip_owner_position_entry_ms = {k: v+delta for k, v in state.flip_owner_position_entry_ms.items()}
    state.flip_owner_provisional_started_ms += delta
    state.bars = type(state.bars)(
        (replace(bar, timestamp_ms=bar.timestamp_ms+delta) for bar in state.bars),
        maxlen=state.bars.maxlen,
    )
    clock[0] = int(observed.timestamp()*1000)
    state.bars[-1] = replace(state.bars[-1], timestamp_ms=clock[0]//60000*60000-60000)
    proofs = tuple(replace(p,
        identity=replace(p.identity, symbol=symbol, managed_symbol=symbol, fill_symbol=symbol,
            fill_ms=p.identity.fill_ms+delta,
            slot_id=fanout_slot_id(strategy_code="schwab_1m_v2", symbol=symbol,
                                  segment_id=OPPORTUNITY, slot="resting")),
        bar=replace(p.bar, symbol=symbol, bar_ms=p.bar.bar_ms+delta,
                    observed_at_ms=p.bar.observed_at_ms+delta)) for p in proofs)
    with factory() as session:
        for row in session.scalars(select(BrokerOrder)).all():
            row.symbol = symbol
            row.submitted_at += shift
            row.updated_at += shift
        for fill in session.scalars(select(Fill)).all():
            fill.symbol = symbol
            fill.filled_at += shift
        for row in session.scalars(select(OmsManagedPosition)).all():
            row.symbol = symbol
            row.entry_time += shift
            row.updated_at += shift
            row.entry_classification = next(p.as_payload() for p in proofs
                if p.identity.managed_row_id == str(row.id))
        record_receipts(session, [(a, observed, 0) for a in ACCOUNTS], recorded_at=observed)
        old = strategy._flip_owner_record(state)
        session.add_all([
            DashboardSnapshot(snapshot_type="v2_fanout_segment_identity", created_at=observed,
                payload={"symbol":symbol, "fanout_segment_id":str(OPPORTUNITY), "active":True}),
            DashboardSnapshot(snapshot_type="v2_flip_entry_ownership", created_at=observed,
                payload=old.as_payload(active=True, reason="controlled")),
        ])
        session.commit()
    strategy.settings.strategy_schwab_1m_v2_country_route_enabled = True
    strategy._resting_session_is_eh = lambda now=None: SchwabV2Strategy._resting_session_is_eh(
        strategy, now or datetime.fromtimestamp(clock[0]/1000, UTC))
    strategy._resting_in_window = lambda now=None: SchwabV2Strategy._resting_in_window(
        strategy, now or datetime.fromtimestamp(clock[0]/1000, UTC))
    strategy._eh_resting_enabled = pm
    strategy._resting_stop_ask_allows = lambda *_args, **_kwargs: True
    strategy._liquidity_floor_ok = lambda _: True
    route_calls, identity_calls = [], []

    def route_for(name, now):
        route_calls.append((name, now, state.fanout_segment_id, state.flip_owner_phase))
        return CountryRoute(broker, "sec_us" if broker == "schwab" else "sec_non_us",
                            "READY", broker == "schwab", "0000000001", now)

    strategy.configure_country_routing(AtrCountryRouteBook(strategy.settings, route_for))
    strategy.configure_fanout_identity_persistence(lambda *args: identity_calls.append(args))
    store = FalseFlipStore(factory)
    bot = SchwabV2BotService.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot.session_factory = strategy, strategy.settings, factory
    bot._falseflip_store = SimpleNamespace(commit=lambda before, after:
        store.commit(before, after, now=datetime.fromtimestamp(clock[0]/1000, UTC)))
    return SimpleNamespace(engine=engine, factory=factory, strategy=strategy, state=state,
        clock=clock, proofs=proofs, bot=bot, routes=route_calls, identities=identity_calls,
        writer_sequence=0)


def book(case):
    apply_book(case.strategy, case.state, case.clock, case.proofs)
    return case.strategy._falseflip_book


def install_writer(monkeypatch, case, *, fault=None, after_commit=None):
    import project_mai_tai.v2_falseflip_release as module

    original = module.persist_owner_transition
    calls = []

    def writer(*args, **kwargs):
        record = args[1]
        previous = kwargs.get("previous")
        if previous is not None:
            assert not case.state.fanout_segment_id and not case.state.flip_owner_opportunity_id
            assert case.state.flip_owner_phase == "idle"
            assert not case.routes and not case.identities
            with case.factory() as session:
                latest = session.scalar(select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == "v2_fanout_segment_identity"
                ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()).limit(1))
                assert (latest.payload["active"] is False
                        and latest.payload["fanout_segment_id"] == str(previous[1])) or (
                    latest.payload["active"] is True
                    and latest.payload["fanout_segment_id"] == str(record.opportunity_id))
            if fault is not None:
                fault(case)
        calls.append((record.opportunity_id, previous))
        case.writer_sequence += 1
        now = datetime.fromtimestamp(case.clock[0]/1000, UTC) + timedelta(milliseconds=case.writer_sequence)
        result = original(*args, **kwargs, now=now)
        if previous is not None and after_commit is not None:
            after_commit(case)
        return result

    monkeypatch.setattr(module, "persist_owner_transition", writer)
    return calls


def latest_snapshot(session, kind):
    return session.scalar(select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == kind)
        .order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()).limit(1))


def latest_budget(session, symbol, segment):
    # Budget revisions share the controlled clock; random UUID order is not CAS order.
    row = session.scalar(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == BUDGET_TYPE,
        DashboardSnapshot.payload["symbol"].as_string() == symbol,
        cast(DashboardSnapshot.payload["segment"].as_string(), BigInteger) == segment,
    ).order_by(DashboardSnapshot.payload["revision"].as_integer().desc()).limit(1))
    assert row is not None
    return FalseBudget.read(row.payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["WORX", "SAIQ", "GLND", "WETO"])
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("pm", [True, False], ids=["PM", "RTH"])
async def test_country_release_atomic_rearm_then_one_routed_rest(monkeypatch, symbol, broker, pm):
    case = country_case(symbol, broker, pm)
    try:
        calls = install_writer(monkeypatch, case)
        await case.bot._falseflip2_poll(book(case))
        state = case.state
        assert len(calls) == 2 and calls[0] == (OPPORTUNITY, None)
        candidate = calls[1][0]
        assert candidate != OPPORTUNITY and calls[1][1][1] == OPPORTUNITY
        assert state.fanout_segment_id == state.flip_owner_opportunity_id == candidate
        assert state.resting_active and state.flip_owner_phase == "resting"
        assert len(case.routes) == 1 and case.routes[0][2:] == (candidate, "resting")
        assert not case.identities  # No separate country-triggered identity mint.
        assert not case.strategy.pending_falseflip2_rearms()
        assert not case.strategy.pending_falseflip_cancel_publications()
        assert state.atr_country_route["atr_country_broker"] == broker
        assert state.resting_is_broker_order is not pm
        primary = case.strategy.drain_pending_intents()
        webull = case.strategy.drain_webull_direct_intents()
        if pm:
            assert primary == webull == []
        else:
            selected, other = (primary, webull) if broker == "schwab" else (webull, primary)
            assert len(selected) == 1 and not other
            assert selected[0].metadata["fanout_segment_id"] == str(candidate)
            assert selected[0].metadata["atr_country_broker"] == broker
        with case.factory() as session:
            active = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_fanout_segment_identity")).all()
            assert [r.payload["fanout_segment_id"] for r in active if r.payload["active"]] == [
                str(OPPORTUNITY), str(candidate)]
            begins = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_wait_dispatch")).all()
            assert len(begins) == 1 and begins[0].payload["kind"] == "begin"
            assert begins[0].payload["opportunity_id"] == str(candidate)
        await case.bot._falseflip2_poll(case.strategy._falseflip_book)
        assert len(calls) == 2 and len(case.routes) == 1
        assert not case.strategy.drain_pending_intents() and not case.strategy.drain_webull_direct_intents()
    finally:
        case.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_second_false_flip_country_keeps_skip_then_atomic_recross(monkeypatch, broker):
    case = country_case(broker=broker)
    try:
        segment = case.state.retry_one_segment_id
        prior = FalseBudget(case.state.symbol, segment).closed(segment+1, 1).fully_filled_closed(segment+1)
        case.strategy.configure_falseflip({(case.state.symbol, segment):prior}, readable=True)
        case.state.retry_one_closes_in_segment = 2
        case.state.flip_owner_retry_closes_at_place = 1
        case.state.flip_owner_phase = "consumed"
        with case.factory() as session:
            session.add(DashboardSnapshot(snapshot_type=BUDGET_TYPE,
                payload=prior.payload(), created_at=datetime.fromtimestamp(case.clock[0]/1000, UTC)))
            session.commit()
        calls = install_writer(monkeypatch, case)
        await case.bot._falseflip2_poll(book(case))
        assert len(calls) == 1  # Release only; the owed skip blocks the rearm.
        assert not case.state.resting_active and not case.routes and not case.identities
        trigger = case.strategy._resting_trigger_for_line(case.state.atr_trail)
        quote = lambda ask: Quote(case.state.symbol, ask-.01, ask, ask, case.clock[0], case.clock[0])
        assert case.strategy.on_quote(case.state.symbol, quote(trigger)) is None
        await case.bot._falseflip_poll()  # The real native budget CAS consumes exactly one skip.
        assert case.strategy._falseflip_budget(case.state).skipped
        case.strategy._falseflip_restoring_track(case.state)
        assert not case.strategy.pending_falseflip2_rearms()  # Still above the skipped edge.
        assert case.strategy.on_quote(case.state.symbol, quote(trigger-.01)) is None
        await case.bot._falseflip2_poll(case.strategy._falseflip_book)
        assert len(calls) == 2 and case.state.resting_active
        assert len(case.routes) == 1 and not case.identities
        assert case.state.retry_one_segment_id == segment
        with case.factory() as session:
            saved = latest_budget(session, case.state.symbol, segment)
            assert saved.skipped and len(saved.episodes) == 2
    finally:
        case.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_same_process_budget_reload_after_release_replans_before_country_and_commits_once(monkeypatch, broker):
    case = country_case(broker=broker)
    try:
        def stop_before_rearm(_case):
            raise OSError("controlled shutdown before rearm commit")

        calls = install_writer(monkeypatch, case, fault=stop_before_rearm)
        await case.bot._falseflip2_poll(book(case))
        assert len(calls) == 1 and case.state.flip_owner_phase == "idle"
        assert not case.state.fanout_segment_id and not case.routes
        # Same-process budget reload, NOT a fresh-process restore proof. Pending
        # plans are not durable authority; reconstruct the old episode's proof.
        with case.factory() as session:
            saved = latest_budget(session, case.state.symbol, case.state.retry_one_segment_id)
            assert latest_snapshot(session, "v2_fanout_segment_identity").payload["active"] is False
        case.strategy.configure_falseflip({(saved.symbol,saved.segment):saved}, readable=True)
        monkeypatch.undo()
        calls = install_writer(monkeypatch, case)
        await case.bot._falseflip2_poll(book(case))
        assert len(calls) == 1 and calls[0][1] is not None
        assert case.state.resting_active and len(case.routes) == 1 and not case.identities
        with case.factory() as session:
            begins = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_wait_dispatch")).all()
            assert len(begins) == 1
    finally:
        case.engine.dispose()


@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("pm", [True, False])
def test_fresh_normal_country_segment_has_no_false_rearm(broker, pm):
    case = country_case(broker=broker, pm=pm)
    try:
        case.strategy.configure_falseflip({}, readable=True)
        case.state.flip_owner_phase = "idle"
        case.state.fanout_segment_id = case.state.flip_owner_opportunity_id = 0
        case.state.flip_owner_fill_accounts.clear()
        case.state.flip_owner_position_ids.clear()
        case.state.flip_owner_position_entry_ms.clear()
        case.state.flip_owner_first_rest_placed = False
        case.state.flip_owner_evidence_readable = True
        case.state.flip_owner_evidence_at_ms = case.clock[0]
        case.strategy._cw_v2_resting_track(case.state, None)
        assert case.state.resting_active and len(case.routes) == 1
        assert len(case.identities) == 1
        assert case.routes[0][2] == case.state.fanout_segment_id
        assert not case.strategy.pending_falseflip2_rearms()
        assert case.state.atr_country_route["atr_country_broker"] == broker
    finally:
        case.engine.dispose()


@pytest.mark.asyncio
async def test_committed_rearm_changed_ack_fence_retries_same_candidate_without_duplicate_dispatch(monkeypatch):
    case = country_case()
    try:
        def revoke_ack(c):
            c.bot._clearwait_emit_generation_by_symbol = {c.state.symbol: 1}

        calls = install_writer(monkeypatch, case, after_commit=revoke_ack)
        await case.bot._falseflip2_poll(book(case))
        assert len(calls) == 2 and not case.routes and not case.identities
        assert case.state.flip_owner_phase == "idle" and case.state.fanout_segment_id == 0
        plan, = case.strategy.pending_falseflip2_rearms()
        candidate = plan[0].opportunity_id
        monkeypatch.undo()
        retries = install_writer(monkeypatch, case)
        await case.bot._falseflip2_poll(case.strategy._falseflip_book)
        assert retries == [(candidate, plan[2])]
        assert case.state.resting_active and case.state.fanout_segment_id == candidate
        assert len(case.routes) == 1 and not case.identities
        with case.factory() as session:
            begins = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_wait_dispatch")).all()
            assert len(begins) == 1
    finally:
        case.engine.dispose()


def test_rest_return_diagnostics_are_bounded_by_reason_episode_and_bar(caplog):
    case = country_case()
    try:
        caplog.set_level("INFO")
        case.state.restart_rest_evidence_pending = True
        for _ in range(100):
            case.strategy._queue_resting_place(case.state, case.state.atr_trail)
        assert caplog.text.count("reason=restart_evidence_pending") == 1
        assert not case.routes and not case.identities
        case.state.bars[-1] = replace(case.state.bars[-1], timestamp_ms=case.state.bars[-1].timestamp_ms+60000)
        case.strategy._queue_resting_place(case.state, case.state.atr_trail)
        assert caplog.text.count("reason=restart_evidence_pending") == 2
    finally:
        case.engine.dispose()


@pytest.mark.parametrize("broker", ["schwab", "webull"])
def test_second_false_old_skip_cannot_block_a_new_real_short_segment(broker):
    case = country_case(broker=broker)
    try:
        segment = case.state.retry_one_segment_id
        prior = FalseBudget(case.state.symbol, segment).closed(segment+1, 1).fully_filled_closed(segment+1)
        case.strategy.configure_falseflip({(case.state.symbol,segment):prior}, readable=True)
        case.state.retry_one_closes_in_segment = 2
        case.state.flip_owner_retry_closes_at_place = 1
        case.state.flip_owner_phase = "consumed"
        settle(case.strategy, case.state, case.clock, case.proofs)
        release(case.strategy, case.state)
        assert case.strategy._falseflip_budget(case.state).skip_due
        assert not case.state.resting_active and not case.routes
        case.state.retry_one_segment_id = case.state.atr_short_flip_bar_ts = case.clock[0]//60000*60000
        case.state.retry_one_closes_in_segment = 0
        case.strategy._falseflip_restoring_track(case.state)
        assert case.state.symbol not in case.strategy._falseflip2_rearm
        assert not case.strategy.pending_falseflip2_rearms()
        case.strategy._cw_v2_resting_track(case.state, None)
        assert case.state.resting_active and len(case.routes) == 1
        assert len(case.identities) == 1
        assert case.strategy._falseflip_budgets[(case.state.symbol,segment)].skip_due
    finally:
        case.engine.dispose()


def test_rearm_retains_strict_first_slot_and_existing_removal_owner_barriers():
    case = country_case()
    try:
        settle(case.strategy, case.state, case.clock, case.proofs)
        release(case.strategy, case.state)
        case.strategy._queue_resting_place(case.state, case.state.atr_trail, slot="reclaim")
        assert not case.strategy.pending_falseflip2_rearms()
        case.strategy._removed_scanner_symbols.add(case.state.symbol)
        case.strategy._queue_resting_place(case.state, case.state.atr_trail)
        assert not case.strategy.pending_falseflip2_rearms()
        assert not case.state.fanout_segment_id and not case.state.flip_owner_opportunity_id
        assert not case.routes and not case.identities
        assert not case.state.resting_active
    finally:
        case.engine.dispose()


@pytest.mark.parametrize("caller", ["selector", "sizing"])
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("phase", ["idle", "resting", "unknown"])
def test_pending_idle_rearm_any_country_caller_cannot_mint(caller, broker, phase, caplog):
    case = country_case(broker=broker)
    try:
        settle(case.strategy, case.state, case.clock, case.proofs)
        release(case.strategy, case.state)
        case.state.flip_owner_phase = phase
        if phase != "idle":
            case.state.fanout_segment_id = case.state.flip_owner_opportunity_id = 999
        caplog.set_level("INFO")
        for _ in range(20):
            if caller == "selector":
                with pytest.raises(CountryRouteRefusal, match="country_waiting_atomic_rearm"):
                    case.strategy._country_entry_selection(case.state)
            else:
                assert case.strategy._sized_open(case.state.symbol, leg=broker,
                    price=Decimal("2"), basis="stop_limit_wire_limit") is None
        expected = 0 if phase == "idle" else 999
        assert case.state.fanout_segment_id == case.state.flip_owner_opportunity_id == expected
        assert case.state.flip_owner_phase == phase and not case.routes and not case.identities
        assert caplog.text.count("reason=country_waiting_atomic_rearm") <= 2
        assert not case.strategy.pending_falseflip2_rearms()
    finally:
        case.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [
    "generation", "open_emit", "owner", "commit", "durable_owner", "late_fill", "stale_receipt",
    "partial_pair", "conflicting_pair",
])
async def test_rearm_native_writer_refuses_changed_proof_owner_or_fence(monkeypatch, fault):
    case = country_case()
    try:
        def disturb(c):
            if fault == "generation":
                c.bot._clearwait_emit_generation_by_symbol = {c.state.symbol: 1}
            elif fault == "open_emit":
                c.bot._clearwait_open_emits = {c.state.symbol: 1}
            elif fault == "owner":
                c.state.flip_owner_opportunity_id = 999
            elif fault == "durable_owner":
                with c.factory() as session:
                    session.add(DashboardSnapshot(snapshot_type="v2_fanout_segment_identity",
                        created_at=datetime.fromtimestamp(c.clock[0]/1000, UTC)+timedelta(seconds=1),
                        payload={"symbol":c.state.symbol,"fanout_segment_id":"999","active":True}))
                    session.commit()
            elif fault == "late_fill":
                with c.factory() as session:
                    fill = session.scalars(select(Fill).where(Fill.side == "buy")).first()
                    fill.quantity += 1
                    session.commit()
            elif fault == "stale_receipt":
                at = datetime.fromtimestamp(c.clock[0]/1000, UTC)
                with c.factory() as session:
                    record_receipts(session, [(a, at-timedelta(seconds=16), 0) for a in ACCOUNTS], recorded_at=at)
                    session.commit()
            elif fault in {"partial_pair", "conflicting_pair"}:
                plan, = c.strategy.pending_falseflip2_rearms()
                at = datetime.fromtimestamp(c.clock[0]/1000, UTC)+timedelta(seconds=1)
                with c.factory() as session:
                    session.add(DashboardSnapshot(snapshot_type="v2_fanout_segment_identity", created_at=at,
                        payload={"symbol":c.state.symbol, "fanout_segment_id":str(plan[0].opportunity_id),
                                 "active":True, "reason":"first_rest_working"}))
                    if fault == "conflicting_pair":
                        session.add(DashboardSnapshot(snapshot_type="v2_flip_entry_ownership", created_at=at,
                            payload=replace(plan[0], opportunity_id=999).as_payload(
                                active=True, reason="first_rest_working")))
                    session.commit()
            else:
                raise OSError("controlled unavailable writer")

        calls = install_writer(monkeypatch, case, fault=disturb)
        await case.bot._falseflip2_poll(book(case))
        assert not case.state.resting_active and not case.routes and not case.identities
        assert len(case.strategy.pending_falseflip2_rearms()) == 1
        assert case.state.fanout_segment_id == 0
        assert case.state.flip_owner_opportunity_id == (999 if fault == "owner" else 0)
        assert not case.strategy.drain_pending_intents() and not case.strategy.drain_webull_direct_intents()
        with case.factory() as session:
            assert not session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_wait_dispatch")).all()
        assert len(calls) == (1 if fault == "commit" else 2)
    finally:
        case.engine.dispose()


@pytest.mark.parametrize("caller", ["selector", "sizing"])
@pytest.mark.parametrize("binding", ["owner_zero", "fanout_zero", "owner_mismatch", "fanout_mismatch"])
def test_committed_prebound_country_guard_checks_both_ids_independently(caller, binding):
    case = country_case()
    try:
        settle(case.strategy, case.state, case.clock, case.proofs)
        release(case.strategy, case.state)
        candidate = case.clock[0]
        case.state.fanout_segment_id = case.state.flip_owner_opportunity_id = candidate
        case.state.flip_owner_phase = "resting"
        case.state.flip_owner_retry_segment_id = case.state.retry_one_segment_id
        case.state.flip_owner_retry_closes_at_place = case.state.retry_one_closes_in_segment
        case.strategy._falseflip2_prebound[case.state.symbol] = case.strategy._flip_owner_record(case.state)
        if binding.startswith("owner"):
            case.state.flip_owner_opportunity_id = 0 if binding.endswith("zero") else candidate+1
        else:
            case.state.fanout_segment_id = 0 if binding.endswith("zero") else candidate+1
        before = (case.state.fanout_segment_id,case.state.flip_owner_opportunity_id)
        if caller == "selector":
            with pytest.raises(CountryRouteRefusal, match="country_waiting_atomic_rearm"):
                case.strategy._country_entry_selection(case.state)
        else:
            assert case.strategy._sized_open(case.state.symbol, leg="schwab",
                price=Decimal("2"), basis="stop_limit_wire_limit") is None
        assert (case.state.fanout_segment_id,case.state.flip_owner_opportunity_id) == before
        assert not case.routes and not case.identities
    finally:
        case.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [
    "before_invocation", "during_precommit", "after_commit_before_ack", "commit_then_raise",
])
async def test_line_refresh_eventually_arms_current_price_with_exact_candidate_and_one_dispatch(monkeypatch, boundary):
    case = country_case()
    try:
        def move_line(c):
            c.state.atr_trail += .01

        def committed_move(c):
            move_line(c)
            if boundary == "commit_then_raise":
                raise OSError("controlled lost commit acknowledgement")

        if boundary == "before_invocation":
            tracking = case.strategy._falseflip_restoring_track
            monkeypatch.setattr(case.strategy, "_falseflip_restoring_track", lambda _:None)
            calls = install_writer(monkeypatch, case)
            await case.bot._falseflip2_poll(book(case))
            assert len(calls) == 1 and case.state.flip_owner_phase == "idle"
            monkeypatch.setattr(case.strategy, "_falseflip_restoring_track", tracking)
            tracking(case.state)
            plan, = case.strategy.pending_falseflip2_rearms()
            move_line(case)
        else:
            install_writer(monkeypatch, case,
                fault=move_line if boundary == "during_precommit" else None,
                after_commit=committed_move if boundary != "during_precommit" else None)
            await case.bot._falseflip2_poll(book(case))
            plan, = case.strategy.pending_falseflip2_rearms()
        assert not case.state.resting_active and not case.routes and not case.identities
        assert case.state.atr_trail != plan[1]
        before = case.strategy._falseflip_budget(case.state)
        monkeypatch.undo()
        retried = install_writer(monkeypatch, case)
        await case.bot._falseflip2_poll(case.strategy._falseflip_book)
        assert retried == [(plan[0].opportunity_id,plan[2])]
        assert case.state.resting_active and case.state.resting_level == case.state.atr_trail
        assert case.state.fanout_segment_id == case.state.flip_owner_opportunity_id == plan[0].opportunity_id
        after = case.strategy._falseflip_budget(case.state)
        assert after.episodes == before.episodes and after.refunded_counts == before.refunded_counts
        assert case.state.retry_one_closes_in_segment == plan[0].retry_closes_at_place
        assert len(case.routes) == 1 and not case.identities
        with case.factory() as session:
            begins = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_wait_dispatch")).all()
            assert len(begins) == 1 and begins[0].payload["opportunity_id"] == str(plan[0].opportunity_id)
    finally:
        case.engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_waiter_never_refreshes_or_reinvokes_until_actual_worker_finally(monkeypatch):
    case = country_case()
    entered, go = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    owner = task = None
    try:
        def paused(_case):
            loop.call_soon_threadsafe(entered.set)
            assert go.wait(3)

        calls = install_writer(monkeypatch, case, fault=paused)
        task = asyncio.create_task(case.bot._falseflip2_poll(book(case)))
        await asyncio.wait_for(entered.wait(), 3)
        plan, = case.strategy.pending_falseflip2_rearms()
        owner = case.strategy._falseflip2_rearm_workers[case.state.symbol]
        case.state.atr_trail += .01
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owner["waiter_finished"] is True and not owner["completed"].is_set()
        assert not owner["task"].done() and not owner["task"].cancelled()
        case.strategy._falseflip_restoring_track(case.state)
        await case.bot._falseflip2_poll(case.strategy._falseflip_book)
        assert case.strategy.pending_falseflip2_rearms() == (plan,)
        assert case.strategy._falseflip2_rearm_workers[case.state.symbol] is owner
        assert len(calls) == 1 and not case.routes and not case.state.resting_active
        go.set()
        assert await asyncio.to_thread(owner["completed"].wait, 3)
        await asyncio.gather(owner["task"], return_exceptions=True)
        monkeypatch.undo()
        retried = install_writer(monkeypatch, case)
        await case.bot._falseflip2_poll(case.strategy._falseflip_book)
        assert retried == [(plan[0].opportunity_id,plan[2])]
        assert case.state.resting_active and case.state.resting_level == case.state.atr_trail
        assert case.state.fanout_segment_id == plan[0].opportunity_id
        with case.factory() as session:
            begins = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_wait_dispatch")).all()
            assert len(begins) == 1
    finally:
        go.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if owner is not None:
            assert await asyncio.to_thread(owner["completed"].wait, 3)
            await asyncio.gather(owner["task"], return_exceptions=True)
        case.engine.dispose()


def test_unknown_worker_completion_cannot_refresh_existing_candidate():
    case = country_case()
    try:
        settle(case.strategy, case.state, case.clock, case.proofs)
        release(case.strategy, case.state)
        case.strategy._falseflip_restoring_track(case.state)
        plan, = case.strategy.pending_falseflip2_rearms()
        case.strategy._falseflip2_rearm_workers = {case.state.symbol:{
            "plan":plan, "completed":threading.Event(), "waiter_finished":True}}
        case.state.atr_trail += .01
        case.strategy._falseflip_restoring_track(case.state)
        assert case.strategy.pending_falseflip2_rearms() == (plan,)
        assert not case.state.resting_active and not case.routes and not case.identities
    finally:
        case.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("pm", [True, False], ids=["PM", "RTH"])
async def test_new_strategy_and_service_restore_native_stores_after_release_then_rearm(monkeypatch, broker, pm):
    case = country_case(broker=broker, pm=pm)
    try:
        # First process commits only the old release. No candidate has been
        # committed or routed when this controlled process boundary occurs.
        monkeypatch.setattr(case.strategy, "_falseflip_restoring_track", lambda _:None)
        calls = install_writer(monkeypatch, case)
        await case.bot._falseflip2_poll(book(case))
        assert len(calls) == 1 and case.state.flip_owner_phase == "idle"
        old_strategy, old_state, old_bot = case.strategy, case.state, case.bot
        at = datetime.fromtimestamp(case.clock[0]/1000, UTC)
        fanout_store = FanoutSegmentIdentityStore(case.factory)
        owner_store = FlipEntryOwnershipStore(case.factory)
        owner_store.record_retry_budget(old_state.symbol, old_state.retry_one_segment_id,
                                       old_state.retry_one_closes_in_segment, now=at)
        active = fanout_store.restore_active(now=at)
        owners = owner_store.restore_active(now=at)
        retries = owner_store.restore_retry_budgets(now=at)
        budgets = FalseFlipStore(case.factory).restore(now=at)
        assert not active and not owners
        monkeypatch.undo()
        fresh = SchwabV2Strategy(old_strategy.settings.model_copy(deep=True))
        fresh._now_ms = lambda:case.clock[0]

        def identity(*args):
            case.identities.append(args)
            fanout_store.record(*args, now=at)

        fresh.configure_fanout_identity_persistence(identity, restored=active)
        fresh.configure_flip_entry_ownership(
            lambda record, active, reason:owner_store.record(record,active=active,reason=reason,now=at),
            active_segments=active, restored=owners, restored_retry_budgets=retries,
            retry_budget_persist=lambda symbol, segment, count:
                owner_store.record_retry_budget(symbol,segment,count,now=at))
        removed = RemovedWaitStore(case.factory)
        fresh.configure_removed_wait(removed.record_rules, restored=removed.restore(), readable=True)
        fresh.configure_falseflip(budgets, readable=True)
        state = fresh.watchlist_state(old_state.symbol)
        assert state.retry_one_segment_id == old_state.retry_one_segment_id
        assert state.retry_one_closes_in_segment == old_state.retry_one_closes_in_segment
        state.atr_short_flip_bar_ts = state.retry_one_segment_id
        state.atr_state, state.atr_trail, state.atr_state_age = "short",old_state.atr_trail,old_state.atr_state_age
        state.bars = type(old_state.bars)(old_state.bars, maxlen=old_state.bars.maxlen)
        fresh._resting_in_window = lambda now=None:SchwabV2Strategy._resting_in_window(fresh,now or at)
        fresh._resting_session_is_eh = lambda now=None:SchwabV2Strategy._resting_session_is_eh(fresh,now or at)
        fresh._eh_resting_enabled = pm
        fresh._resting_stop_ask_allows = lambda *_args,**_kwargs:True
        fresh._liquidity_floor_ok = lambda _:True

        def route_for(symbol, now):
            case.routes.append((symbol,now,state.fanout_segment_id,state.flip_owner_phase))
            return CountryRoute(broker,"sec_us" if broker == "schwab" else "sec_non_us",
                                "READY",broker == "schwab","0000000001",now)

        fresh.configure_country_routing(AtrCountryRouteBook(fresh.settings,route_for))
        bot = SchwabV2BotService.__new__(SchwabV2BotService)
        bot.strategy,bot.settings,bot.session_factory = fresh,fresh.settings,case.factory
        bot._falseflip_store = SimpleNamespace(commit=lambda before,after:
            FalseFlipStore(case.factory).commit(before,after,now=at))
        case.strategy,case.state,case.bot = fresh,state,bot
        assert fresh is not old_strategy and state is not old_state and bot is not old_bot
        assert not fresh.pending_falseflip2_rearms() and not fresh._falseflip2_rearm
        calls = install_writer(monkeypatch,case)
        await bot._falseflip2_poll(book(case))
        assert len(calls) == 1 and calls[0][1][1] == OPPORTUNITY
        assert state.resting_active and len(case.routes) == 1 and not case.identities
        assert state.atr_country_route["atr_country_broker"] == broker
        with case.factory() as session:
            begins = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_wait_dispatch")).all()
            assert len(begins) == 1
    finally:
        case.engine.dispose()


@pytest.mark.asyncio
async def test_cancel_before_worker_entry_keeps_shielded_task_then_retries_same_candidate(monkeypatch):
    case = country_case()
    queued, enter = asyncio.Event(), asyncio.Event()
    original_to_thread = asyncio.to_thread
    owner = task = None
    queued_count = []
    try:
        async def queued_worker(function, *args, **kwargs):
            if function.__name__ == "write_rearm":
                queued_count.append(function)
                queued.set()
                await enter.wait()  # Explicitly BEFORE the actual worker entry.
            return await original_to_thread(function, *args, **kwargs)

        calls = install_writer(monkeypatch,case)
        monkeypatch.setattr(service_module.asyncio,"to_thread",queued_worker)
        task = asyncio.create_task(case.bot._falseflip2_poll(book(case)))
        await asyncio.wait_for(queued.wait(),3)
        plan, = case.strategy.pending_falseflip2_rearms()
        owner = case.strategy._falseflip2_rearm_workers[case.state.symbol]
        retained_task = owner["task"]
        assert len(calls) == 1 and not owner["completed"].is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owner["task"] is retained_task and not retained_task.done() and not retained_task.cancelled()
        case.state.atr_trail += .01
        await case.bot._falseflip2_poll(case.strategy._falseflip_book)
        assert len(queued_count) == 1 and len(calls) == 1
        assert case.strategy.pending_falseflip2_rearms() == (plan,)
        assert not case.state.resting_active and not case.routes
        enter.set()
        await asyncio.gather(retained_task,return_exceptions=True)
        assert owner["completed"].is_set()
        monkeypatch.undo()
        retried = install_writer(monkeypatch,case)
        await case.bot._falseflip2_poll(case.strategy._falseflip_book)
        assert retried == [(plan[0].opportunity_id,plan[2])]
        assert case.state.resting_active and case.state.resting_level == case.state.atr_trail
        assert case.state.fanout_segment_id == plan[0].opportunity_id
        with case.factory() as session:
            begins = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_wait_dispatch")).all()
            assert len(begins) == 1
    finally:
        enter.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        if owner is not None:
            await asyncio.gather(owner["task"],return_exceptions=True)
        case.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unproven_no_entry", "accepted_delayed", "accepted_committed"])
async def test_untyped_submission_failure_never_implies_no_wire_or_rollback(monkeypatch,outcome):
    case = country_case()
    original_to_thread = asyncio.to_thread
    release_worker = threading.Event()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    detached = []
    try:
        async def uncertain_submit(function,*args,**kwargs):
            if function.__name__ != "write_rearm":
                return await original_to_thread(function,*args,**kwargs)
            if outcome == "accepted_delayed":
                def submitted():
                    loop.call_soon_threadsafe(entered.set)
                    assert release_worker.wait(3)
                    return function(*args,**kwargs)

                detached.append(asyncio.create_task(original_to_thread(submitted)))
                await entered.wait()
            elif outcome == "accepted_committed":
                await original_to_thread(function,*args,**kwargs)
            # Identical error shape in all cases: it cannot classify whether a
            # pool accepted the job. Only native completion permits a retry.
            raise RuntimeError("controlled submission outcome UNKNOWN")

        calls = install_writer(monkeypatch,case)
        monkeypatch.setattr(service_module.asyncio,"to_thread",uncertain_submit)
        await case.bot._falseflip2_poll(book(case))
        plan, = case.strategy.pending_falseflip2_rearms()
        case.state.atr_trail += .01
        assert not case.state.resting_active and not case.routes and not case.identities
        if outcome == "accepted_committed":
            assert len(calls) == 2
        else:
            owner = case.strategy._falseflip2_rearm_workers[case.state.symbol]
            assert owner["task"].done() and not owner["completed"].is_set()
            await case.bot._falseflip2_poll(case.strategy._falseflip_book)
            assert case.strategy.pending_falseflip2_rearms() == (plan,)
            assert len(calls) == 1 and not case.state.resting_active
            if outcome == "unproven_no_entry":
                # The test knows it never called the function, but production
                # has no typed rejection receipt. Keep UNKNOWN; do not clear it.
                assert case.strategy.falseflip2_rearm_worker_pending(case.state.symbol)
                return
            release_worker.set()
            await asyncio.gather(*detached,return_exceptions=True)
            assert owner["completed"].is_set()
        monkeypatch.undo()
        retried = install_writer(monkeypatch,case)
        await case.bot._falseflip2_poll(case.strategy._falseflip_book)
        assert retried == [(plan[0].opportunity_id,plan[2])]
        assert case.state.resting_active and case.state.resting_level == case.state.atr_trail
        assert case.state.fanout_segment_id == plan[0].opportunity_id
        with case.factory() as session:
            begins = session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_wait_dispatch")).all()
            assert len(begins) == 1
    finally:
        release_worker.set()
        if detached:
            await asyncio.gather(*detached,return_exceptions=True)
        case.engine.dispose()


@pytest.mark.parametrize("problem", ["unreadable", "stale", "restart", "slot", "changed_line", "stranded"])
def test_pending_rearm_guards_and_stranded_identity_are_not_bypassed(problem):
    case = country_case()
    try:
        settle(case.strategy, case.state, case.clock, case.proofs)
        release(case.strategy, case.state)
        if problem == "unreadable":
            apply_book(case.strategy, case.state, case.clock, case.proofs, complete=False)
        elif problem == "stale":
            case.clock[0] += 16000
        elif problem == "restart":
            case.state.restart_rest_evidence_pending = True
        elif problem == "slot":
            case.state.cw_resting_taken = True
        elif problem == "stranded":
            case.state.fanout_segment_id = case.state.flip_owner_opportunity_id = 999
        else:
            case.strategy._falseflip_restoring_track(case.state)
            plan, = case.strategy.pending_falseflip2_rearms()
            case.state.atr_trail += .01
            assert not case.strategy.falseflip2_rearm_valid(plan)
            assert not case.strategy.acknowledge_falseflip2_rearm(plan)
            return
        case.strategy._falseflip_restoring_track(case.state)
        assert not case.strategy.pending_falseflip2_rearms()
        assert not case.state.resting_active and not case.routes and not case.identities
        assert case.state.fanout_segment_id == (999 if problem == "stranded" else 0)
    finally:
        case.engine.dispose()
