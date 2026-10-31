"""Private durable overlay controls. Not native/production concurrency proof."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import insert

from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai.db.models import (
    BotControlAttempt, BotControlCommand, BotControlState, BrokerAccount,
    BrokerOrder, Strategy, TradeIntent,
)
from test_brokerdecides1_local_census import ACCOUNTS, census as census


def control(session, account, strategy, *, status="UNKNOWN", operation="CANCEL_ENTRY"):
    old = datetime.now(UTC) - timedelta(days=2)
    session.add(BotControlState(strategy_id=strategy.id, execution_mode="live", generation=1,
        revision=0, desired_state="STOP", updated_at=old))
    session.flush()
    command = BotControlCommand(strategy_id=strategy.id, execution_mode="live", generation=1,
        expected_generation=0, revision=0, action="STOP", request_id=uuid4(),
        request_hash="a" * 64, account_scope={}, account_scope_hash="b" * 64,
        actor_reference="private-fixture", reason="synthetic unresolved native effect",
        phase="UNKNOWN", active=True, inventory_census={}, accepted_at=old, updated_at=old)
    session.add(command)
    session.flush()
    row = BotControlAttempt(command_id=command.command_id, strategy_id=strategy.id,
        execution_mode="live", generation=1, account_id=account.id, symbol="WORX",
        binding_kind="symbol", binding_key="WORX", sequence_no=0, revision=0,
        operation=operation, status=status, prepared_at=old)
    session.add(row)
    session.flush()
    return row


@pytest.mark.parametrize("strategy_code", ["schwab_1m_v2", "momentum_warn_live", "orb_fixture"])
@pytest.mark.parametrize("account_index", [0, 1])
@pytest.mark.parametrize("status,operation", [
    ("PREPARED", "CANCEL_ENTRY"), ("MAY_HAVE_SENT", "SUBMIT_EXIT"),
    ("WORKING", "ADOPT_EXISTING_EXIT"), ("UNKNOWN", "CANCEL_PROTECTION"),
])
def test_old_unresolved_control_holds_across_accounts_and_strategies(
        census, strategy_code, account_index, status, operation):
    session, accounts, strategy = census
    if strategy_code != strategy.code:
        strategy = Strategy(code=strategy_code, name="private fixture")
        session.add(strategy)
        session.flush()
    row = control(session, accounts[account_index], strategy, status=status, operation=operation)
    inflight, fills, terminals = local_evidence(session, ACCOUNTS)
    assert inflight == frozenset({"WORX"}) and not fills and not terminals
    assert row.status == status  # Observation never mutates control ownership/finality.


@pytest.mark.parametrize("status", ["TERMINAL", "PROVEN_NOT_SENT"])
def test_existing_control_finality_is_not_automatically_reopened(census, status):
    session, accounts, strategy = census
    row = control(session, accounts[0], strategy, status=status)
    assert local_evidence(session, ACCOUNTS) == (frozenset(), frozenset(), frozenset())
    assert row.status == status
    # This is only a local uncertainty census, not native-flat or STOP permission.


def nonentry(session, account, strategy, kind, status):
    parent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id, symbol="WORX",
        side="buy" if kind == "cancel" else "sell", intent_type="cancel" if kind == "cancel" else "close",
        quantity=Decimal("1"), reason="private fixture", status=status, payload={})
    session.add(parent)
    session.flush()
    if kind == "order":
        child = BrokerOrder(intent_id=parent.id, strategy_id=strategy.id, broker_account_id=account.id,
            client_order_id="private-sell", symbol="WORX", side="sell", quantity=Decimal("1"),
            order_type="limit", time_in_force="day", status=status, payload={})
        # Isolate the order path: a closed parent must not hide a pending child.
        parent.status = "cancelled"
        session.add(child)
    session.flush()


@pytest.mark.parametrize("kind", ["intent", "order", "cancel"])
@pytest.mark.parametrize("account_index", [0, 1])
def test_pending_sell_or_cancel_cannot_hide_behind_a_native_read(census, kind, account_index):
    session, accounts, strategy = census
    nonentry(session, accounts[account_index], strategy, kind, "pending")
    assert local_evidence(session, ACCOUNTS)[0] == frozenset({"WORX"})


@pytest.mark.parametrize("kind", ["intent", "order", "cancel"])
@pytest.mark.parametrize("account_index", [0, 1])
def test_closed_nonentry_is_not_promoted_to_unresolved_history(census, kind, account_index):
    session, accounts, strategy = census
    nonentry(session, accounts[account_index], strategy, kind, "cancelled")
    assert local_evidence(session, ACCOUNTS) == (frozenset(), frozenset(), frozenset())
    # Actual native working/position guards remain independent of this local label.


def test_unresolved_control_overflow_is_unknown_not_partial_success(census):
    session, accounts, strategy = census
    first = control(session, accounts[0], strategy)
    session.execute(insert(BotControlAttempt), [dict(attempt_id=uuid4(), command_id=first.command_id,
        strategy_id=strategy.id, execution_mode="live", generation=1, account_id=accounts[0].id,
        symbol="WORX", binding_kind="symbol", binding_key="WORX", sequence_no=index, revision=0,
        operation="CANCEL_ENTRY", status="UNKNOWN") for index in range(1, 4097)])
    session.flush()
    with pytest.raises(ValueError, match="control_census_overflow"):
        local_evidence(session, ACCOUNTS)


def test_account_outside_configured_scope_cannot_alias_either_live_book(census):
    session, accounts, strategy = census
    foreign = BrokerAccount(name="paper:outside", provider="webull", environment="paper")
    session.add(foreign)
    session.flush()
    control(session, foreign, strategy)
    assert local_evidence(session, ACCOUNTS) == (frozenset(), frozenset(), frozenset())
