"""Full ATR STOP is an ATR-routing seam, never a C6 standalone behavior change."""

from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, Fill
from project_mai_tai.oms import bot_control_atr as atr
from project_mai_tai.oms.bot_control import ControlConflict
from project_mai_tai.oms.bot_control_legacy import LegacyStopDriver
from project_mai_tai.oms.service import OmsRiskService
from tests.unit.test_killsw1_atr_stop import Redis, book as book, drive


def test_atr_flag_selects_full_owner_but_orb_keeps_conservative_c6_driver(book):
    drivers = book.service._bot_control_runtime.executor.drivers
    assert isinstance(drivers["schwab_1m_v2"], atr.AtrStopDriver)
    assert isinstance(drivers["orb_schwab"], LegacyStopDriver)


def test_atr_flag_off_registers_existing_legacy_drivers_for_both_bots(book):
    settings = book.service.settings.model_copy(update={
        "strategy_schwab_1m_v2_country_route_enabled": False,
    })
    service = OmsRiskService(settings, redis_client=Redis(), session_factory=book.sessions,
                             broker_adapter=book.broker)
    assert all(isinstance(service._bot_control_runtime.executor.drivers[code], LegacyStopDriver)
               for code in ("schwab_1m_v2", "orb_schwab"))


@pytest.mark.asyncio
async def test_flag_off_after_registration_cannot_release_or_sell_held_atr(book):
    book.service.settings.strategy_schwab_1m_v2_country_route_enabled = False
    assert (await drive(book)).phase == "BLOCKED_UNKNOWN"
    assert not book.submits and not book.cancels and book.release_count == 0


@pytest.mark.asyncio
async def test_stop_reason_without_owned_context_never_enters_normal_sell_path(book):
    assert await book.service._emit_v2_exit_on_loop_claimed(
        book.account_name, "ABC", None, 10,
        kind="HARD", reference_price=10, reason="KILLSW1_STOP", bid=10,
        close_on_fill=True, expected_managed_row_id=str(book.row),
    ) == "refused"
    assert not book.submits and not book.cancels and book.release_count == 0


@pytest.mark.asyncio
async def test_atr_layer_does_not_bypass_core_partial_native_parent_block(book, monkeypatch):
    with book.sessions.begin() as unit:
        parent = unit.get(BrokerOrder, book.entry)
        parent.status = "working"
        native = "true" if book.provider == "schwab" else "false"
        parent.payload = {"bracket": native, "native_oco_bracket": native}
        for fill in unit.scalars(select(Fill).where(Fill.order_id == book.entry)):
            fill.quantity = Decimal(3)
    original = book.broker.fetch_order_update

    async def partial(query):
        report = await original(query)
        return replace(report, filled_quantity=Decimal(3),
            event_type="partially_filled" if report.event_type == "working" else report.event_type)

    monkeypatch.setattr(book.broker, "fetch_order_update", partial)
    if book.provider == "schwab":
        with pytest.raises(ControlConflict, match="partial_native_parent_protection_UNPROVEN"):
            await atr._cancel_entry(book.service, book.lease, book.journal, book.entry)
        assert not book.cancels
    else:
        await atr._cancel_entry(book.service, book.lease, book.journal, book.entry)
        assert len(book.cancels) == 1  # Proven bare Webull entry remainder remains cancellable.
    assert not book.submits and book.release_count == 0
