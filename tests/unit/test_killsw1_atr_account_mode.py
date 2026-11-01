"""ATR-layer owner mode guards; actual production deployment rows stay unmodified."""

from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from decimal import Decimal

import pytest

from project_mai_tai.db.models import BrokerAccount, OmsManagedPosition, Strategy
from project_mai_tai.oms import bot_control_atr as atr
from project_mai_tai.oms.bot_control import ControlConflict
from tests.unit.test_killsw1_atr_stop import book as book, drive, fresh_receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("deployment", ["production", "paper"])
async def test_live_alias_actual_live_strategy_reaches_owned_atr_sender_not_deployment_guard(book, deployment):
    with book.sessions.begin() as unit:
        unit.get(BrokerAccount, book.account).environment = deployment
    # Retained live alias plus fake environment=paper is explicitly LIVE per02:0x.
    with book.sessions() as unit:
        _, _, _, remaining = atr._managed(book.service, unit, book.lease, book.row)
        assert remaining == Decimal(10)
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"  # Actual later receipt still required.
    assert book.release_count == 1 and not book.submits
    fresh_receipt(book)
    assert (await drive(book)).phase == "EXIT_WORKING"
    assert len(book.submits) == 1 and book.submits[0].quantity == Decimal(10)


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["paper:atr-controlled", "unknown:atr-controlled"])
async def test_production_paper_or_unknown_alias_never_releases_or_sells(book, alias):
    with book.sessions.begin() as unit:
        account = unit.get(BrokerAccount, book.account)
        account.name, account.environment = alias, "production"
        unit.get(OmsManagedPosition, book.row).broker_account_name = alias
    with book.sessions() as unit:
        with pytest.raises(ControlConflict, match="managed_binding_UNKNOWN"):
            atr._managed(book.service, unit, book.lease, book.row)
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert book.release_count == 0 and not book.submits and not book.cancels


@pytest.mark.asyncio
async def test_actual_paper_strategy_cannot_borrow_live_alias_for_atr_stop(book):
    with book.sessions.begin() as unit:
        unit.get(BrokerAccount, book.account).environment = "production"
        unit.get(Strategy, book.strategy).execution_mode = "paper"
    with book.sessions() as unit:
        with pytest.raises(ControlConflict, match="managed_binding_UNKNOWN"):
            atr._managed(book.service, unit, book.lease, book.row)
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert book.release_count == 0 and not book.submits and not book.cancels


def test_inactive_owned_production_account_not_discarded_by_mode_helper(book):
    with book.sessions.begin() as unit:
        account = unit.get(BrokerAccount, book.account)
        account.environment, account.is_active = "production", False
    with book.sessions() as unit:
        _, _, account, remaining = atr._managed(book.service, unit, book.lease, book.row)
        assert account.id == book.account and remaining == Decimal(10)
