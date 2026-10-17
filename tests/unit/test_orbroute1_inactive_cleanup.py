"""Source-authored P2 controls; no inactive admission or foreign cleanup authority."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import (
    BotControlAttempt, BrokerAccount, BrokerOrder, Strategy, TradeIntent,
)
from project_mai_tai.oms.bot_control import ControlConflict
from project_mai_tai.oms.bot_control_orb import _cancel_entry
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from tests.unit.test_killsw1_commands import control as control, order
from tests.unit.test_killsw1_legacy import legacy as legacy
from tests.unit.test_orbroute1_oms_chain import Redis


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_inactive_exact_own_working_buy_is_cancelled_once_not_sold(legacy, provider):
    with legacy.sessions.begin() as session:
        session.get(Strategy, legacy.strategy).code = "orb_schwab"
        account = session.get(BrokerAccount, legacy.accounts[0])
        account.is_active, account.provider, account.environment = False, provider, "production"
        name = account.name
        entry = session.get(BrokerOrder, legacy.entries[0])
        native = "true" if provider == "schwab" else "false"
        entry.payload = {**entry.payload, "bracket": native, "native_oco_bracket": native}
    service = OmsRiskService(
        Settings(_env_file=None, orb_country_routing_enabled=True,
                 orb_webull_account_name=name if provider == "webull" else ""),
        redis_client=Redis(), session_factory=legacy.sessions, broker_adapter=legacy.broker,
    )
    await _cancel_entry(service, legacy.lease, legacy.journal, legacy.entries[0], lambda: datetime.now(UTC))
    with legacy.sessions() as session:
        owned = session.get(BrokerOrder, legacy.entries[0])
        assert owned.status == "cancelled"
        assert session.get(BrokerAccount, owned.broker_account_id).is_active is False
        assert all(session.get(BrokerOrder, identity).status == "working" for identity in legacy.protection)
        attempt = session.scalar(select(BotControlAttempt).where(
            BotControlAttempt.command_id == legacy.lease.command_id,
            BotControlAttempt.target_order_id == owned.id,
            BotControlAttempt.operation == "CANCEL_ENTRY"))
        assert attempt.status == "TERMINAL"
    assert len(legacy.broker.sent) == 1
    assert legacy.broker.sent[0].intent_type == "cancel"
    assert legacy.broker.sent[0].client_order_id == "owned-0"
    assert len(legacy.broker.reads) >= (2 if provider == "schwab" else 1)
    # A terminal entry cannot acquire a second cancellation, even if called directly.
    with pytest.raises(ValueError, match="cancel_identity_unknown"):
        await _cancel_entry(service, legacy.lease, legacy.journal, legacy.entries[0], lambda: datetime.now(UTC))
    assert len(legacy.broker.sent) == 1
    assert all(request.side == "buy" and request.intent_type == "cancel" for request in legacy.broker.sent)


def test_inactive_orb_new_buy_still_fails_actual_c6_entry_permit(control):
    store, sessions, strategy_id, account_id = control
    with sessions.begin() as session:
        session.get(Strategy, strategy_id).code = "orb_schwab"
        account = session.get(BrokerAccount, account_id)
        account.is_active, account.environment = False, "production"
    store.seed(strategy_id)
    entry_id = order(sessions, strategy_id, account_id)
    with pytest.raises(ControlConflict, match="entry_account_not_live"):
        store.authorize_entry(order_id=entry_id, strategy_id=strategy_id, expected_generation=0)
    with sessions() as session:
        entry = session.get(BrokerOrder, entry_id)
        assert entry.status == "pending" and entry.broker_order_id is None
        assert "bot_control_permit" not in (entry.payload or {})


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "foreign_strategy", "foreign_account", "foreign_intent", "missing_intent",
    "paper_strategy", "paper_account", "unknown_account", "unsupported_provider",
    "missing_coid", "missing_root",
])
async def test_inactive_cleanup_rejects_nonowned_or_unknown_identity_before_io(legacy, mutation):
    with legacy.sessions.begin() as session:
        strategy = session.get(Strategy, legacy.strategy)
        strategy.code = "orb_schwab"
        entry = session.get(BrokerOrder, legacy.entries[0])
        account = session.get(BrokerAccount, entry.broker_account_id)
        account.is_active, account.environment = False, "production"
        foreign_id = session.scalar(select(Strategy.id).where(Strategy.code == "foreign"))
        if mutation == "foreign_strategy":
            entry.strategy_id = foreign_id
        elif mutation == "foreign_account":
            foreign = BrokerAccount(name="live:outside-scope", provider="schwab", environment="production")
            session.add(foreign)
            session.flush()
            entry.broker_account_id = foreign.id
        elif mutation == "foreign_intent":
            session.get(TradeIntent, entry.intent_id).strategy_id = foreign_id
        elif mutation == "missing_intent":
            entry.intent_id = None
        elif mutation == "paper_strategy":
            strategy.execution_mode = "paper"
        elif mutation == "paper_account":
            account.name = "paper:inactive"
        elif mutation == "unknown_account":
            account.name = "UNKNOWN:inactive"
        elif mutation == "unsupported_provider":
            account.provider = "unknown"
        elif mutation == "missing_coid":
            entry.client_order_id = ""
        elif mutation == "missing_root":
            entry.broker_order_id = None
    expected_error = ControlConflict if mutation == "paper_strategy" else ValueError
    expected_reason = "not_a_live_strategy" if mutation == "paper_strategy" else "orb_stop_entry_cancel_identity_unknown"
    with pytest.raises(expected_error, match=f"^{expected_reason}$"):
        await _cancel_entry(legacy.service, legacy.lease, legacy.journal,
                            legacy.entries[0], lambda: datetime.now(UTC))
    assert not legacy.broker.reads and not legacy.broker.sent
    with legacy.sessions() as session:
        assert session.scalar(select(BotControlAttempt.attempt_id)) is None
