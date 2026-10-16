from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import (
    Base,
    BotControlAttempt,
    BrokerAccount,
    BrokerOrder,
    OmsManagedPosition,
    Strategy,
)
from project_mai_tai.oms.bot_control import BotControlStore, ControlConflict
from project_mai_tai.oms.bot_control_journal import CleanupJournal


@pytest.fixture
def journal():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    strategy, account, order_id = uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            Strategy(id=strategy, code="momentum_warn_live", name="MOM", execution_mode="live")
        )
        session.add(
            BrokerAccount(id=account, name="live:orb", provider="webull", environment="live")
        )
        session.flush()
        session.add(
            BrokerOrder(
                id=order_id,
                strategy_id=strategy,
                broker_account_id=account,
                client_order_id="owned-entry",
                broker_order_id="broker-entry",
                symbol="ABC",
                side="buy",
                order_type="limit",
                time_in_force="day",
                quantity=Decimal(10),
                status="working",
            )
        )
    store = BotControlStore(sessions)
    store.seed(strategy)
    receipt = store.request(
        strategy_id=strategy,
        command_id=uuid4(),
        request_id=uuid4(),
        expected_generation=0,
        action="STOP",
        account_ids=(account,),
        actor_reference="operator",
        reason="STOP",
    )
    now = datetime.now(UTC)
    lease = store.claim_stop(receipt.command_id, "executor-a", now=now)
    yield CleanupJournal(store), sessions, lease, order_id, now
    engine.dispose()


def prepare(journal, lease, order_id, now, **overrides):
    values = dict(
        order_id=order_id,
        operation="CANCEL_ENTRY",
        sequence_no=0,
        owner_hash="a" * 64,
        owner_revision=1,
        now=now,
    )
    values.update(overrides)
    return journal.prepare(lease, **values)


def test_prepared_identity_survives_restart_without_replaying_dispatch(journal):
    log, sessions, lease, order_id, now = journal
    initial = prepare(log, lease, order_id, now)
    assert initial.status == "PREPARED"
    assert prepare(CleanupJournal(BotControlStore(sessions)), lease, order_id, now) == initial
    dispatched = log.dispatch(lease, initial.attempt_id, now=now)
    assert dispatched.status == "MAY_HAVE_SENT"
    with sessions() as session:
        assert session.get(BotControlAttempt, initial.attempt_id).dispatch_intent_at is not None
    with pytest.raises(ControlConflict, match="uncertain_or_fenced"):
        CleanupJournal(BotControlStore(sessions)).dispatch(lease, initial.attempt_id, now=now)


def test_expired_executor_cannot_dispatch_and_takeover_cannot_resend(journal):
    log, _, lease, order_id, now = journal
    attempt = prepare(log, lease, order_id, now)
    later = now + timedelta(seconds=31)
    successor = log.store.claim_stop(lease.command_id, "executor-b", now=later)
    assert successor.fence == lease.fence + 1
    with pytest.raises(ControlConflict, match="executor_fenced"):
        log.dispatch(lease, attempt.attempt_id, now=later)
    with pytest.raises(ControlConflict, match="uncertain_or_fenced"):
        log.dispatch(successor, attempt.attempt_id, now=later)


@pytest.mark.parametrize("field,value", [("strategy_id", uuid4()), ("side", "sell")])
def test_no_foreign_or_wrong_side_cancellation(journal, field, value):
    log, sessions, lease, order_id, now = journal
    with sessions.begin() as session:
        setattr(session.get(BrokerOrder, order_id), field, value)
    with pytest.raises(ControlConflict, match="not_owned"):
        prepare(log, lease, order_id, now)
    with sessions() as session:
        assert session.scalar(select(BotControlAttempt)) is None


def test_same_identity_different_owner_evidence_refuses(journal):
    log, _, lease, order_id, now = journal
    prepare(log, lease, order_id, now)
    with pytest.raises(ControlConflict, match="identity_conflict"):
        prepare(log, lease, order_id, now, owner_revision=2)


def read(log, lease, attempt, now, **overrides):
    values = dict(
        broker_order_id="broker-entry",
        client_order_id="owned-entry",
        status="cancelled",
        filled_quantity=Decimal(0),
        observed_at=now,
        now=now,
    )
    values.update(overrides)
    return log.reconcile(lease, attempt.attempt_id, **values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"broker_order_id": "foreign"},
        {"client_order_id": "foreign"},
        {"filled_quantity": Decimal(11)},
        {"filled_quantity": Decimal("NaN")},
        {"status": "not_found"},
        {"status": "filled", "filled_quantity": Decimal(2)},
        {"observed_at": datetime(2000, 1, 1, tzinfo=UTC)},
    ],
)
def test_missing_stale_foreign_or_partial_terminal_proof_never_settles(journal, overrides):
    log, sessions, lease, order_id, now = journal
    attempt = prepare(log, lease, order_id, now)
    log.dispatch(lease, attempt.attempt_id, now=now)
    with pytest.raises(ControlConflict):
        read(log, lease, attempt, now, **overrides)
    with sessions() as session:
        assert session.get(BotControlAttempt, attempt.attempt_id).status == "MAY_HAVE_SENT"


def test_authoritative_cancel_partial_fill_terminal_retains_exact_fill(journal):
    log, sessions, lease, order_id, now = journal
    attempt = prepare(log, lease, order_id, now)
    log.dispatch(lease, attempt.attempt_id, now=now)
    assert read(log, lease, attempt, now, filled_quantity=Decimal(3)).status == "TERMINAL"
    with sessions() as session:
        row = session.get(BotControlAttempt, attempt.attempt_id)
        assert row.terminal_evidence["filled_quantity"] == "3"
        # The audit does not invent broker fills or update accounting state.
        assert session.get(BrokerOrder, order_id).status == "working"


def test_live_executor_lease_is_not_stolen(journal):
    log, _, lease, _, now = journal
    assert log.store.claim_stop(lease.command_id, "executor-b", now=now) is None


def native(log, lease, order_id, now, managed_id=None):
    return log.prepare_native(
        lease,
        entry_id=order_id,
        managed_id=managed_id,
        binding_key="exact-parent-tree",
        now=now,
    )


def test_native_parent_identity_replay_and_changed_broker_id_refusal(journal):
    log, sessions, lease, order_id, now = journal
    attempt = native(log, lease, order_id, now)
    assert attempt.order_id == order_id
    assert native(log, lease, order_id, now) == attempt
    with sessions.begin() as session:
        session.get(BrokerOrder, order_id).broker_order_id = "other-root"
    with pytest.raises(ControlConflict, match="identity_changed"):
        native(log, lease, order_id, now)
    with pytest.raises(ControlConflict, match="identity_changed"):
        log.dispatch(lease, attempt.attempt_id, now=now)
        log.record_native_outcome(lease, attempt.attempt_id, outcome="released", now=now)


@pytest.mark.parametrize("outcome", ["reserved", "unanswerable", "unsupported"])
def test_native_unknown_never_terminal_or_replays_cancel(journal, outcome):
    log, sessions, lease, order_id, now = journal
    attempt = native(log, lease, order_id, now)
    log.dispatch(lease, attempt.attempt_id, now=now)
    result = log.record_native_outcome(lease, attempt.attempt_id, outcome=outcome, now=now)
    assert result.status == "UNKNOWN"
    with pytest.raises(ControlConflict, match="uncertain_or_fenced"):
        log.dispatch(lease, attempt.attempt_id, now=now)
    with sessions() as session:
        assert session.get(BrokerOrder, order_id).status == "working"
        audit = session.get(BotControlAttempt, attempt.attempt_id)
        assert audit.terminal_evidence["entry_broker_order_id"] == "broker-entry"
        assert audit.terminal_evidence["outcome"] == outcome


@pytest.mark.parametrize("outcome", ["released", "resolved_by_fill"])
def test_native_positive_existing_reader_result_does_not_invent_accounting(journal, outcome):
    log, sessions, lease, order_id, now = journal
    attempt = native(log, lease, order_id, now)
    with pytest.raises(ControlConflict, match="not_dispatched"):
        log.record_native_outcome(lease, attempt.attempt_id, outcome=outcome, now=now)
    log.dispatch(lease, attempt.attempt_id, now=now)
    with pytest.raises(ControlConflict, match="before_dispatch"):
        log.record_native_outcome(
            lease, attempt.attempt_id, outcome=outcome, now=now - timedelta(seconds=1)
        )
    assert (
        log.record_native_outcome(lease, attempt.attempt_id, outcome=outcome, now=now).status
        == "TERMINAL"
    )
    with sessions() as session:
        assert session.get(BrokerOrder, order_id).status == "working"


@pytest.mark.parametrize(
    "field,value",
    [
        ("strategy_id", uuid4()),
        ("side", "sell"),
        ("broker_order_id", None),
        ("broker_account_id", uuid4()),
    ],
)
def test_native_foreign_missing_or_wrong_side_parent_refuses(journal, field, value):
    log, sessions, lease, order_id, now = journal
    with sessions.begin() as session:
        setattr(session.get(BrokerOrder, order_id), field, value)
    with pytest.raises(ControlConflict, match="not_owned"):
        native(log, lease, order_id, now)
    with sessions() as session:
        assert session.scalar(select(BotControlAttempt)) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("strategy_code", "foreign"),
        ("broker_account_name", "foreign"),
        ("symbol", "OTHER"),
        ("entry_order_id", uuid4()),
        ("entry_client_order_id", "foreign"),
    ],
)
def test_native_managed_episode_binding_is_exact(journal, field, value):
    log, sessions, lease, order_id, now = journal
    managed_id = uuid4()
    with sessions.begin() as session:
        managed = OmsManagedPosition(
            id=managed_id,
            strategy_code="momentum_warn_live",
            broker_account_name="live:orb",
            symbol="ABC",
            entry_order_id=order_id,
            entry_client_order_id="owned-entry",
            entry_price=Decimal(3),
            original_quantity=10,
            current_quantity=10,
        )
        session.add(managed)
    initial = native(log, lease, order_id, now, managed_id)
    assert initial.status == "PREPARED"
    with sessions.begin() as session:
        setattr(session.get(OmsManagedPosition, managed_id), field, value)
    with pytest.raises(ControlConflict, match="binding_changed"):
        native(log, lease, order_id, now, managed_id)


def test_pre_dispatch_recovery_never_marks_broker_order_terminal(journal):
    log, sessions, lease, order_id, now = journal
    attempt = prepare(log, lease, order_id, now)
    later = now + timedelta(seconds=31)
    successor = log.store.claim_stop(lease.command_id, "executor-b", now=later)
    assert (
        log.prove_prepared_not_sent(successor, attempt.attempt_id, now=later).status
        == "PROVEN_NOT_SENT"
    )
    with sessions() as session:
        assert session.get(BrokerOrder, order_id).status == "working"


def test_dispatch_intent_is_not_unsent_proof_even_if_socket_not_reached(journal):
    log, _, lease, order_id, now = journal
    attempt = prepare(log, lease, order_id, now)
    log.dispatch(lease, attempt.attempt_id, now=now)
    with pytest.raises(ControlConflict, match="already_uncertain"):
        log.prove_prepared_not_sent(lease, attempt.attempt_id, now=now)


def test_partial_fill_read_cannot_move_backwards(journal):
    log, _, lease, order_id, now = journal
    attempt = prepare(log, lease, order_id, now)
    log.dispatch(lease, attempt.attempt_id, now=now)
    assert (
        read(log, lease, attempt, now, status="partially_filled", filled_quantity=Decimal(3)).status
        == "WORKING"
    )
    with pytest.raises(ControlConflict, match="would_regress"):
        read(log, lease, attempt, now, status="working", filled_quantity=Decimal(0))


def test_unresolved_exit_blocks_second_identity(journal):
    log, sessions, lease, order_id, now = journal
    with sessions.begin() as session:
        order = session.get(BrokerOrder, order_id)
        order.side, order.status = "sell", "pending"
        order.broker_order_id = None
    prepare(log, lease, order_id, now, operation="SUBMIT_EXIT")
    with pytest.raises(ControlConflict, match="already_unresolved"):
        prepare(log, lease, order_id, now, operation="SUBMIT_EXIT", sequence_no=1)


def test_native_unknown_is_not_terminal_and_wrong_parent_cannot_release(journal):
    log, sessions, lease, order_id, now = journal
    with sessions.begin() as unit:
        order = unit.get(BrokerOrder, order_id)
        order.side = "sell"
        order.payload = {"entry_broker_order_id": "original-owned-entry"}
    attempt = prepare(log, lease, order_id, now, operation="CANCEL_PROTECTION")
    log.dispatch(lease, attempt.attempt_id, now=now)
    proof = dict(
        owner_hash="a" * 64,
        entry_broker_id="original-owned-entry",
        child_ids=("owned-T", "owned-S"),
        cumulative_exited=Decimal(0),
        terminal=False,
        working=False,
        observed_at=now,
        now=now,
    )
    assert log.reconcile_tree(lease, attempt.attempt_id, **proof).status == "UNKNOWN"
    with pytest.raises(ControlConflict, match="binding_changed"):
        log.reconcile_tree(lease, attempt.attempt_id, **{**proof, "entry_broker_id": "foreign"})
    assert (
        log.reconcile_tree(lease, attempt.attempt_id, **{**proof, "terminal": True}).status
        == "TERMINAL"
    )
    with sessions() as unit:
        assert unit.get(BrokerOrder, order_id).status == "working"
