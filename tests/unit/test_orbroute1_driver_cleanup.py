"""Actual STOP driver: entry cleanup precedes held-target SELL validation."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import (
    BotControlAttempt, BrokerAccount, BrokerOrder, Strategy, SystemIncident, VirtualPosition,
)
from project_mai_tai.oms import orb_schwab_eod as owner
from project_mai_tai.oms.bot_control import ControlConflict
from project_mai_tai.oms.bot_control_orb import drive_orb_stop
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from tests.unit.test_killsw1_legacy import legacy as legacy
from tests.unit.test_orbroute1_oms_chain import Redis


def prepare(legacy, provider):
    with legacy.sessions.begin() as session:
        session.get(Strategy, legacy.strategy).code = "orb_schwab"
        for identity in legacy.accounts:
            account = session.get(BrokerAccount, identity)
            account.provider, account.environment = provider, "production"
        name = session.get(BrokerAccount, legacy.accounts[0]).name
        for identity in legacy.entries:
            entry = session.get(BrokerOrder, identity)
            native = "true" if provider == "schwab" else "false"
            entry.payload = {**entry.payload, "bracket": native, "native_oco_bracket": native}
        # Keep a single owned working entry; foreign/protection orders remain untouched.
        session.get(BrokerOrder, legacy.entries[1]).status = "cancelled"
    return OmsRiskService(
        Settings(_env_file=None, orb_country_routing_enabled=True,
                 orb_webull_account_name=name if provider == "webull" else ""),
        redis_client=Redis(), session_factory=legacy.sessions, broker_adapter=legacy.broker,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["schwab_1m_v2"], indirect=True)
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("held_case", ["same_inactive_account", "other_account_missing_parent"])
async def test_driver_cleans_owned_buy_before_ineligible_held_target(legacy, provider, held_case):
    service = prepare(legacy, provider)
    normal_state = {"phase": "normal_owner_sentinel"}
    with legacy.sessions.begin() as session:
        session.get(BrokerAccount, legacy.accounts[0]).is_active = False
        entry = session.get(BrokerOrder, legacy.entries[0])
        entry.payload = {**entry.payload, owner._STATE: normal_state}
        held_account = legacy.accounts[0] if held_case == "same_inactive_account" else legacy.accounts[1]
        symbol = "ABC" if held_case == "same_inactive_account" else "NO_PARENT"
        session.add(VirtualPosition(strategy_id=legacy.strategy, broker_account_id=held_account,
            symbol=symbol, quantity=Decimal(2), average_price=Decimal(6)))
    def clock():
        return datetime.now(UTC)
    assert (await drive_orb_stop(service, legacy.lease, legacy.journal, clock=clock)).phase == "CANCELLING_ENTRIES"
    assert len(legacy.broker.sent) == 1
    assert legacy.broker.sent[0].client_order_id == "owned-0"
    assert legacy.broker.sent[0].intent_type == "cancel" and legacy.broker.sent[0].side == "buy"
    assert (await drive_orb_stop(service, legacy.lease, legacy.journal, clock=clock)).phase == "BLOCKED_UNKNOWN"
    assert (await drive_orb_stop(service, legacy.lease, legacy.journal, clock=clock)).phase == "BLOCKED_UNKNOWN"
    assert len(legacy.broker.sent) == 1  # No repeat cancel, protection release or SELL.
    with legacy.sessions() as session:
        entry = session.get(BrokerOrder, legacy.entries[0])
        assert entry.status == "cancelled" and entry.payload[owner._STATE] == normal_state
        assert all(session.get(BrokerOrder, identity).status == "working" for identity in legacy.protection)
        foreign = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == "foreign-entry"))
        assert foreign.status == "working"
        position = session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == symbol))
        assert position.quantity == 2
        incidents = session.scalars(select(SystemIncident)).all()
        assert len(incidents) == 1 and incidents[0].severity == "critical"
        assert incidents[0].payload["account_id"] == str(held_account)
        assert incidents[0].payload["symbol"] == symbol
        assert incidents[0].payload["reason"] == "orb_stop_parent_unknown"
        attempts = session.scalars(select(BotControlAttempt)).all()
        assert len(attempts) == 1 and attempts[0].operation == "CANCEL_ENTRY"
        assert attempts[0].status == "TERMINAL"
    assert legacy.store.hydrate(legacy.strategy).desired_state == "STOP"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["schwab_1m_v2"], indirect=True)
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("mutation", ["foreign_strategy", "paper_strategy", "paper_account", "unknown_account"])
async def test_driver_never_borrows_foreign_paper_or_unknown_cleanup(legacy, provider, mutation):
    service = prepare(legacy, provider)
    with legacy.sessions.begin() as session:
        entry = session.get(BrokerOrder, legacy.entries[0])
        account = session.get(BrokerAccount, legacy.accounts[0])
        account.is_active = False
        if mutation == "foreign_strategy":
            entry.strategy_id = session.scalar(select(Strategy.id).where(Strategy.code == "foreign"))
        elif mutation == "paper_strategy":
            session.get(Strategy, legacy.strategy).execution_mode = "paper"
        elif mutation == "paper_account":
            account.name = "paper:inactive"
        elif mutation == "unknown_account":
            account.name = "UNKNOWN:inactive"
        session.add(VirtualPosition(strategy_id=legacy.strategy, broker_account_id=account.id,
            symbol="ABC", quantity=Decimal(2), average_price=Decimal(6)))
    if mutation == "paper_strategy":
        with pytest.raises(ControlConflict, match="^not_a_live_strategy$"):
            await drive_orb_stop(service, legacy.lease, legacy.journal)
    else:
        assert (await drive_orb_stop(service, legacy.lease, legacy.journal)).phase == "BLOCKED_UNKNOWN"
    assert not legacy.broker.sent and not legacy.broker.reads
    with legacy.sessions() as session:
        assert session.scalar(select(BotControlAttempt.attempt_id)) is None
        assert all(session.get(BrokerOrder, identity).status == "working" for identity in legacy.protection)
