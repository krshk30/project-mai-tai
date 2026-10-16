from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import OrderRequest

from project_mai_tai.db.models import (
    Base,
    BotControlAttempt,
    BotControlCommand,
    BotControlState,
    BrokerAccount,
    BrokerOrder,
    Strategy,
    TradeIntent,
)
from project_mai_tai.oms.bot_control import (
    AdmissionLatch,
    BotControlStore,
    ControlConflict,
    UnseededScope,
)
from project_mai_tai.oms.bot_control_runtime import BotControlRuntime, ControlledBrokerAdapter


@pytest.fixture
def control():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    strategy, account = uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            Strategy(id=strategy, code="momentum_warn_live", name="MOM", execution_mode="live")
        )
        session.add(
            BrokerAccount(id=account, name="live:orb", provider="webull", environment="live")
        )
    yield BotControlStore(sessions), sessions, strategy, account
    engine.dispose()


def request(store, strategy, account, **overrides):
    values = dict(
        strategy_id=strategy,
        command_id=uuid4(),
        request_id=uuid4(),
        expected_generation=0,
        action="STOP",
        account_ids=(account,),
        actor_reference="operator",
        reason="test STOP",
    )
    values.update(overrides)
    return store.request(**values)


def flat(account):
    return {
        str(account): dict(
            complete=True,
            owned_quantity=0,
            working_orders=0,
            in_flight=0,
            evidence_hash="a" * 64,
            observed_at=datetime.now(UTC).isoformat(),
        )
    }


def test_absence_is_not_run_and_seed_is_explicit(control):
    store, _, strategy, account = control
    with pytest.raises(UnseededScope):
        store.hydrate(strategy)
    with pytest.raises(UnseededScope):
        request(store, strategy, account)
    assert store.seed(strategy).admission_open
    assert store.seed(strategy).generation == 0


def test_stop_commits_before_receipt_and_restores_on_new_store(control):
    store, sessions, strategy, account = control
    store.seed(strategy)
    receipt = request(store, strategy, account)
    assert receipt.generation == 1
    assert receipt.phase == "PENDING_CENSUS"
    restarted = BotControlStore(sessions).hydrate(strategy)
    assert restarted.desired_state == "STOP"
    assert not restarted.admission_open
    assert restarted.active_command_id == receipt.command_id
    assert store.seed(strategy) == restarted  # provisioning cannot clear a STOP


def test_exact_request_replay_survives_stale_expected_generation(control):
    store, _, strategy, account = control
    store.seed(strategy)
    command_id, request_id = uuid4(), uuid4()
    first = request(store, strategy, account, command_id=command_id, request_id=request_id)
    replay = request(store, strategy, account, command_id=command_id, request_id=request_id)
    assert replay.replayed
    assert replay.command_id == first.command_id
    assert replay.generation == 1
    assert store.hydrate(strategy).generation == 1
    with pytest.raises(ControlConflict, match="request_identity_conflict"):
        request(
            store, strategy, account, command_id=command_id, request_id=request_id, reason="changed"
        )


def test_command_uuid_cannot_be_reused_with_new_request(control):
    store, _, strategy, account = control
    store.seed(strategy)
    command_id = uuid4()
    request(store, strategy, account, command_id=command_id)
    with pytest.raises(ControlConflict, match="command_identity_conflict"):
        request(store, strategy, account, command_id=command_id)


def test_stale_generation_and_on_before_settlement_refuse(control):
    store, _, strategy, account = control
    store.seed(strategy)
    with pytest.raises(ControlConflict, match="on_requires_settled_stop"):
        request(store, strategy, account, action="ON")
    request(store, strategy, account)
    with pytest.raises(ControlConflict, match="stale_generation"):
        request(store, strategy, account)
    with pytest.raises(ControlConflict, match="command_unsettled"):
        request(store, strategy, account, expected_generation=1, action="ON")
    assert not store.hydrate(strategy).admission_open


@pytest.mark.parametrize(
    "field,value",
    [
        ("complete", False),
        ("owned_quantity", None),
        ("owned_quantity", 1),
        ("owned_quantity", False),
        ("working_orders", 1),
        ("working_orders", None),
        ("in_flight", 1),
        ("in_flight", None),
        ("evidence_hash", ""),
    ],
)
def test_unknown_or_nonflat_inventory_never_settles(control, field, value):
    store, _, strategy, account = control
    store.seed(strategy)
    receipt = request(store, strategy, account)
    census = flat(account)
    census[str(account)][field] = value
    with pytest.raises(ControlConflict, match="inventory_not_proven_settled"):
        store.settle_stop(receipt.command_id, census=census)
    assert store.hydrate(strategy).active_command_id == receipt.command_id


def test_missing_extra_account_census_is_rejected(control):
    store, _, strategy, account = control
    store.seed(strategy)
    receipt = request(store, strategy, account)
    for census in ({}, flat(uuid4()), {**flat(account), **flat(uuid4())}):
        with pytest.raises(ControlConflict, match="incomplete_account_census"):
            store.settle_stop(receipt.command_id, census=census)


def test_only_explicit_on_after_settlement_reopens_and_increments_generation(control):
    store, sessions, strategy, account = control
    store.seed(strategy)
    receipt = request(store, strategy, account)
    stopped = store.settle_stop(receipt.command_id, census=flat(account))
    assert not stopped.admission_open
    assert stopped.desired_state == "STOP"
    assert stopped.active_command_id is None
    on = request(store, strategy, account, action="ON", expected_generation=1)
    assert on.generation == 2 and on.phase == "SETTLED"
    assert store.hydrate(strategy).admission_open
    with sessions() as session:
        commands = session.scalars(select(BotControlCommand)).all()
        assert len(commands) == 2
        assert all(not command.active for command in commands)


@pytest.mark.parametrize("status", ["PREPARED", "MAY_HAVE_SENT", "WORKING", "UNKNOWN"])
def test_unresolved_attempt_prevents_on_even_with_flat_snapshot(control, status):
    store, sessions, strategy, account = control
    store.seed(strategy)
    receipt = request(store, strategy, account)
    with sessions.begin() as session:
        session.add(
            BotControlAttempt(
                command_id=receipt.command_id,
                strategy_id=strategy,
                execution_mode="live",
                generation=1,
                account_id=account,
                symbol="BBCI",
                binding_kind="ENTRY",
                binding_key="owned-entry",
                sequence_no=0,
                operation="SUBMIT_EXIT",
                status=status,
                revision=0,
            )
        )
    with pytest.raises(ControlConflict, match="unresolved_attempt"):
        store.settle_stop(receipt.command_id, census=flat(account))


def test_paper_scope_and_paper_account_cannot_seed_or_enter_live_control(control):
    store, sessions, strategy, account = control
    paper_strategy, paper_account = uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(Strategy(id=paper_strategy, code="paper", name="Paper", execution_mode="paper"))
        session.add(
            BrokerAccount(id=paper_account, name="paper:orb", provider="paper", environment="paper")
        )
    with pytest.raises(ControlConflict, match="not_a_live_strategy"):
        store.seed(paper_strategy)
    store.seed(strategy)
    with pytest.raises(ControlConflict, match="account_scope_not_live"):
        request(store, strategy, paper_account)
    assert store.hydrate(strategy).generation == 0


def test_unknown_duplicate_and_empty_account_scopes_refuse(control):
    store, _, strategy, account = control
    store.seed(strategy)
    for ids in ((), (account, account)):
        with pytest.raises(ControlConflict, match="explicit_unique_account_scope_required"):
            request(store, strategy, account, account_ids=ids)
    with pytest.raises(ControlConflict, match="account_scope_not_live"):
        request(store, strategy, account, account_ids=(uuid4(),))


def order(sessions, strategy, account, **overrides):
    identity = uuid4()
    values = dict(
        id=identity,
        strategy_id=strategy,
        broker_account_id=account,
        client_order_id=str(identity),
        symbol="BBCI",
        side="buy",
        order_type="limit",
        time_in_force="day",
        quantity=Decimal(25),
        status="pending",
        payload={},
    )
    values.update(overrides)
    with sessions.begin() as session:
        session.add(BrokerOrder(**values))
    return identity


def test_permit_commits_before_stop_and_cannot_be_replayed(control):
    store, sessions, strategy, account = control
    store.seed(strategy)
    identity = order(sessions, strategy, account)
    permit = store.authorize_entry(order_id=identity, strategy_id=strategy, expected_generation=0)
    assert permit.generation == 0 and permit.revision == 1
    with sessions() as session:
        durable = json.loads(session.get(BrokerOrder, identity).payload["bot_control_permit"])
        assert durable["status"] == "MAY_HAVE_SENT" and durable["generation"] == 0
    with pytest.raises(ControlConflict, match="entry_dispatch_already_uncertain"):
        store.authorize_entry(order_id=identity, strategy_id=strategy, expected_generation=0)
    stop = request(store, strategy, account)
    with pytest.raises(ControlConflict, match="owned_order_not_terminal"):
        store.settle_stop(stop.command_id, census=flat(account))


def test_stop_before_permit_and_old_generation_after_on_cannot_send(control):
    store, sessions, strategy, account = control
    store.seed(strategy)
    stop = request(store, strategy, account)
    store.settle_stop(stop.command_id, census=flat(account))
    request(store, strategy, account, action="ON", expected_generation=1)
    identity = order(sessions, strategy, account)
    with pytest.raises(ControlConflict, match="entry_fenced"):
        store.authorize_entry(order_id=identity, strategy_id=strategy, expected_generation=0)
    permit = store.authorize_entry(
        order_id=identity, strategy_id=strategy, expected_generation=2, decided_at=datetime.now(UTC)
    )
    assert permit.generation == 2
    request(store, strategy, account, expected_generation=2)
    fresh_order = order(sessions, strategy, account)
    with pytest.raises(ControlConflict, match="entry_fenced"):
        store.authorize_entry(order_id=fresh_order, strategy_id=strategy, expected_generation=3)


@pytest.mark.parametrize(
    "overrides",
    [
        {"side": "sell"},
        {"quantity": Decimal(0)},
        {"status": "accepted"},
        {"broker_order_id": "known-broker-order"},
        {"submitted_at": datetime(2026, 10, 15, tzinfo=UTC)},
    ],
)
def test_permit_refuses_nonentry_or_already_submitted_order(control, overrides):
    store, sessions, strategy, account = control
    store.seed(strategy)
    identity = order(sessions, strategy, account, **overrides)
    with pytest.raises(ControlConflict, match="entry_order_not_unsent_owned_buy"):
        store.authorize_entry(order_id=identity, strategy_id=strategy, expected_generation=0)


def test_wrong_owner_cannot_acquire_permit(control):
    store, sessions, strategy, account = control
    other = uuid4()
    with sessions.begin() as session:
        session.add(Strategy(id=other, code="orb_schwab", name="ORB", execution_mode="live"))
    store.seed(strategy)
    identity = order(sessions, other, account)
    with pytest.raises(ControlConflict, match="entry_order_not_unsent_owned_buy"):
        store.authorize_entry(order_id=identity, strategy_id=strategy, expected_generation=0)


@pytest.mark.parametrize(
    "stamp",
    [
        None,
        "not-a-time",
        "2026-10-15T12:00:00",
        (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    ],
)
def test_unfresh_census_does_not_complete_stop(control, stamp):
    store, _, strategy, account = control
    store.seed(strategy)
    stop = request(store, strategy, account)
    census = flat(account)
    census[str(account)]["observed_at"] = stamp
    with pytest.raises(ControlConflict, match="inventory_observation"):
        store.settle_stop(stop.command_id, census=census)


def test_cached_latch_starts_closed_and_requires_exact_generation(control):
    store, _, strategy, account = control
    latch = AdmissionLatch()
    assert not latch.permits(strategy, 0)
    latch.replace((store.seed(strategy),))
    assert latch.permits(strategy, 0)
    assert not latch.permits(strategy, 1)
    request(store, strategy, account)
    latch.replace((store.hydrate(strategy),))
    assert not latch.permits(strategy, 0)
    assert not latch.permits(strategy, 1)
    latch.invalidate(strategy)
    assert not latch.permits(strategy, 0)


def test_late_hydration_cannot_replace_newer_stop_with_old_run(control):
    store, _, strategy, account = control
    latch = AdmissionLatch()
    running = store.seed(strategy)
    request(store, strategy, account)
    stopped = store.hydrate(strategy)
    latch.replace((stopped,))
    latch.replace((running,))
    assert not latch.permits(strategy, 0) and not latch.permits(strategy, 1)


def test_on_cannot_change_settled_account_scope(control):
    store, sessions, strategy, account = control
    other = uuid4()
    with sessions.begin() as session:
        session.add(
            BrokerAccount(id=other, name="live:schwab", provider="schwab", environment="live")
        )
    store.seed(strategy)
    stop = request(store, strategy, account)
    store.settle_stop(stop.command_id, census=flat(account))
    with pytest.raises(ControlConflict, match="on_requires_same_settled_scope"):
        request(store, strategy, other, action="ON", expected_generation=1)
    assert not store.hydrate(strategy).admission_open


@pytest.mark.parametrize("generation", [True, -1, 2**63 - 1, 1.0])
def test_invalid_generation_never_wraps_or_coerces(control, generation):
    store, _, strategy, account = control
    store.seed(strategy)
    with pytest.raises(ControlConflict, match="invalid_action_or_generation"):
        request(store, strategy, account, expected_generation=generation)


def test_corrupt_run_state_cannot_override_durable_stop(control):
    store, sessions, strategy, account = control
    store.seed(strategy)
    request(store, strategy, account)
    with sessions.begin() as session:
        state = session.get(BotControlState, (strategy, "live"))
        state.desired_state = "RUN"
        state.active_command_id = None
    with pytest.raises(ControlConflict, match="control_history_inconsistent"):
        store.hydrate(strategy)
    identity = order(sessions, strategy, account)
    with pytest.raises(ControlConflict, match="control_history_inconsistent"):
        store.authorize_entry(order_id=identity, strategy_id=strategy, expected_generation=1)


def test_restart_missing_active_binding_cannot_be_treated_as_settled(control):
    store, sessions, strategy, account = control
    store.seed(strategy)
    request(store, strategy, account)
    with sessions.begin() as session:
        session.get(BotControlState, (strategy, "live")).active_command_id = None
    with pytest.raises(ControlConflict, match="command_not_proven_settled"):
        store.hydrate(strategy)


def test_unknown_scope_still_commits_stop_but_cannot_claim_flat_or_on(control):
    store, _, strategy, account = control
    store.seed(strategy)
    receipt = request(store, strategy, account, account_ids=(), scope_complete=False)
    assert receipt.phase == "BLOCKED_SCOPE"
    assert not store.hydrate(strategy).admission_open
    with pytest.raises(ControlConflict, match="inventory_scope_unknown"):
        store.settle_stop(receipt.command_id, census={})
    with pytest.raises(ControlConflict):
        request(store, strategy, account, expected_generation=1, action="ON", scope_complete=False)


def test_inactive_legacy_account_does_not_prevent_stop_latch(control):
    store, sessions, strategy, account = control
    store.seed(strategy)
    with sessions.begin() as session:
        session.get(BrokerAccount, account).is_active = False
    assert request(store, strategy, account).phase == "PENDING_CENSUS"
    assert not store.hydrate(strategy).admission_open


class RecordingAdapter:
    def __init__(self):
        self.requests = []

    async def submit_order(self, value):
        self.requests.append(value)
        return []

    async def replace_bracket_order(self, value, parent):
        self.requests.append((value, parent))
        return "confirmed"


def entry_request(identity, **overrides):
    values = dict(
        client_order_id=str(identity),
        broker_account_name="live:orb",
        strategy_code="momentum_warn_live",
        symbol="BBCI",
        side="buy",
        intent_type="open",
        quantity=Decimal(25),
        reason="owned_entry",
        metadata={
            "bot_control_generation": "0",
            "bot_control_decided_at": datetime.now(UTC).isoformat(),
        },
    )
    values.update(overrides)
    return OrderRequest(**values)


@pytest.mark.asyncio
async def test_real_wire_permit_is_durable_before_fake_adapter_call(control):
    store, sessions, strategy, account = control
    store.seed(strategy)
    identity = order(sessions, strategy, account)
    runtime = BotControlRuntime(sessions)
    await runtime.refresh()
    raw = RecordingAdapter()
    adapter = ControlledBrokerAdapter(raw, runtime)
    value = entry_request(identity)
    assert await adapter.submit_order(value) == []
    assert raw.requests == [value]
    with sessions() as session:
        marker = session.get(BrokerOrder, identity).payload["bot_control_permit"]
        assert json.loads(marker)["status"] == "MAY_HAVE_SENT"
        assert marker == value.metadata["bot_control_permit"]
    with pytest.raises(ControlConflict, match="already_uncertain"):
        await adapter.submit_order(value)
    assert len(raw.requests) == 1


@pytest.mark.asyncio
async def test_durable_stop_beats_stale_running_cache_without_false_terminal_report(control):
    store, sessions, strategy, account = control
    store.seed(strategy)
    identity = order(sessions, strategy, account)
    runtime = BotControlRuntime(sessions)
    await runtime.refresh()
    assert runtime.admission_open("momentum_warn_live")
    request(store, strategy, account)
    raw = RecordingAdapter()
    adapter = ControlledBrokerAdapter(raw, runtime)
    with pytest.raises(ControlConflict, match="entry_fenced"):
        await adapter.submit_order(entry_request(identity))
    assert raw.requests == []
    with sessions() as session:
        assert session.get(BrokerOrder, identity).status == "pending"


@pytest.mark.asyncio
async def test_permit_before_stop_is_explicit_inflight_not_instant_cancellation(control):
    store, sessions, strategy, account = control
    store.seed(strategy)
    identity = order(sessions, strategy, account)
    runtime = BotControlRuntime(sessions)
    await runtime.refresh()

    class StopAtWire(RecordingAdapter):
        async def submit_order(self, value):
            stop = request(store, strategy, account)
            with pytest.raises(ControlConflict, match="owned_order_not_terminal"):
                store.settle_stop(stop.command_id, census=flat(account))
            return await super().submit_order(value)

    raw = StopAtWire()
    await ControlledBrokerAdapter(raw, runtime).submit_order(entry_request(identity))
    assert len(raw.requests) == 1
    assert store.hydrate(strategy).desired_state == "STOP"


@pytest.mark.asyncio
async def test_unknown_state_affects_entry_only_and_only_that_scope(control):
    store, sessions, strategy, account = control
    other = uuid4()
    with sessions.begin() as session:
        session.add(Strategy(id=other, code="orb_schwab", name="ORB", execution_mode="live"))
    store.seed(strategy)
    runtime = BotControlRuntime(sessions)
    await runtime.refresh()
    assert runtime.admission_open("momentum_warn_live")
    assert not runtime.admission_open("orb_schwab")
    raw = RecordingAdapter()
    adapter = ControlledBrokerAdapter(raw, runtime)
    close = entry_request(uuid4(), strategy_code="orb_schwab", side="sell", intent_type="close")
    cancel = entry_request(uuid4(), strategy_code="orb_schwab", intent_type="cancel")
    await adapter.submit_order(close)
    await adapter.submit_order(cancel)
    assert raw.requests == [close, cancel]
    with pytest.raises(ControlConflict, match="scope_not_running"):
        await adapter.submit_order(entry_request(uuid4(), strategy_code="orb_schwab"))


@pytest.mark.asyncio
async def test_runtime_replacement_uses_distinct_durable_intent_and_never_replays(control):
    store, sessions, strategy, account = control
    store.seed(strategy)
    identity = order(
        sessions, strategy, account, status="accepted", broker_order_id="native-parent"
    )
    intent_id = uuid4()
    with sessions.begin() as session:
        session.add(
            TradeIntent(
                id=intent_id,
                strategy_id=strategy,
                broker_account_id=account,
                symbol="BBCI",
                side="buy",
                intent_type="cancel",
                quantity=Decimal(0),
                reason="owned_reprice",
                status="pending",
                payload={},
            )
        )
    runtime = BotControlRuntime(sessions)
    await runtime.refresh()
    raw = RecordingAdapter()
    value = entry_request(identity)
    value.metadata["bot_control_replace_intent_id"] = str(intent_id)
    adapter = ControlledBrokerAdapter(raw, runtime)
    assert await adapter.replace_bracket_order(value, "native-parent") == "confirmed"
    with pytest.raises(ControlConflict, match="already_uncertain"):
        await adapter.replace_bracket_order(value, "native-parent")
    assert len(raw.requests) == 1
    with sessions() as session:
        assert session.get(BrokerOrder, identity).status == "accepted"
        assert "bot_control_permit" in session.get(TradeIntent, intent_id).payload


@pytest.mark.asyncio
async def test_failed_hydration_does_not_keep_previous_run_permission(control, monkeypatch):
    store, sessions, strategy, _ = control
    store.seed(strategy)
    runtime = BotControlRuntime(sessions)
    await runtime.refresh()
    assert runtime.admission_open("momentum_warn_live")

    def unavailable(_):
        raise RuntimeError("test_db_failure")

    monkeypatch.setattr(runtime.store, "hydrate", unavailable)
    await runtime.refresh()
    assert not runtime.admission_open("momentum_warn_live")
