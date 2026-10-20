"""Stored-precision controls; remapped rows/replies are not live SAIQ receipts."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import (
    AccountPosition, BrokerOrder, Fill, OmsManagedPosition, TradeIntent, VirtualPosition,
)
from project_mai_tai.oms.price_binding import stored_price
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.target_cache import ManagedTargetCache
from project_mai_tai.oms import webull_attach_riskoff as wb
from tests.unit.test_falseflip1_runtime import database
from tests.unit.test_wbattach1_riskoff import make_case, closes


RAW = Decimal("5.0898305085")
STORED = Decimal("5.08983051")
COID = "schwab_1m_v2-SAIQ-open-a619444b6e7d"


@pytest.mark.parametrize("raw,expected", [
    (RAW, STORED), (float(RAW), STORED), ("1.86", Decimal("1.86000000")),
    ("5.000000005", Decimal("5.00000001")), ("5.000000004", Decimal("5.00000000")),
    ("0.000000005", Decimal("0.00000001")),
])
def test_stored_precision_uses_explicit_half_up_not_epsilon(raw, expected):
    assert stored_price(raw) == expected


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "0", "-1", "bad",
                                  "0.000000004", "10000000000", "9999999999.999999995"])
def test_non_finite_zero_negative_or_out_of_column_prices_fail_closed(bad):
    with pytest.raises((ValueError, InvalidOperation)):
        stored_price(bad)


def saiq_case(make_case, raw=RAW, stored=STORED, *, at=None):
    service, factory, broker, entry_id, clock = make_case()
    clock[0] = at or datetime(2026, 10, 20, 14, 38, 43, tzinfo=UTC)
    with factory.begin() as session:
        entry = session.get(BrokerOrder, entry_id)
        entry.symbol, entry.client_order_id, entry.quantity = "SAIQ", COID, Decimal(59)
        entry.submitted_at = clock[0]
        parent = session.get(TradeIntent, entry.intent_id)
        parent.symbol, parent.quantity, parent.created_at = "SAIQ", Decimal(59), clock[0]
        for fill in session.scalars(select(Fill)):
            fill.symbol, fill.quantity, fill.price, fill.filled_at = "SAIQ", Decimal(59), stored, clock[0]
        row = session.scalar(select(OmsManagedPosition))
        row.symbol, row.entry_client_order_id, row.entry_price = "SAIQ", COID, stored
        row.original_quantity = row.current_quantity = 59
        row.entry_time = clock[0]
        entry.payload = {**entry.payload, wb.BARE_FILL_PROOF: {
            **entry.payload[wb.BARE_FILL_PROOF], "entry_coid": COID}}
        vp = session.scalar(select(VirtualPosition))
        vp.symbol, vp.quantity, vp.average_price = "SAIQ", Decimal(59), stored
        position = session.scalar(select(AccountPosition))
        position.symbol, position.quantity, position.average_price = "SAIQ", Decimal(1059), stored
        position.source_updated_at = clock[0]
    broker.total = Decimal(1059)
    original_positions, original_submit = broker.list_account_positions, broker.submit_order

    async def positions(account):
        return [replace(p, symbol="SAIQ", average_price=stored) for p in await original_positions(account)]

    async def submit(request):
        return [replace(report, symbol="SAIQ", reported_at=clock[0],
                        fill_price=Decimal("4.50") if report.filled_quantity else report.fill_price)
                for report in await original_submit(request)]

    broker.list_account_positions, broker.submit_order = positions, submit
    return SimpleNamespace(service=service, factory=factory, broker=broker, entry_id=entry_id,
                           clock=clock, raw=raw)


async def attach(case):
    return await case.service._attach_webull_protection(
        broker_account_name=case.broker.account, symbol="SAIQ", quantity=59,
        entry_price=float(case.raw), strategy_code="schwab_1m_v2", entry_client_order_id=COID)


@pytest.mark.asyncio
async def test_controlled_saiq_59_fractional_average_proceeds_to_one_attach(make_case):
    case = saiq_case(make_case)
    assert float(RAW) != float(STORED)  # Pin the old refusal, not just the helper.
    assert await attach(case) is True
    pairs = [r for r in case.broker.requests if r.metadata.get("webull_exit_only_pair") == "true"]
    assert len(pairs) == 1 and pairs[0].symbol == "SAIQ" and pairs[0].quantity == 59
    assert not closes(case.broker)
    assert await attach(case) is True
    assert len(case.broker.requests) == 1  # Existing accepted owner, never a second writer.


@pytest.mark.asyncio
async def test_real_stored_price_change_still_blocks_before_any_attach(make_case):
    case = saiq_case(make_case, raw=Decimal("5.09"), stored=Decimal("5.10"))
    assert await attach(case) is False
    assert not case.broker.requests


@pytest.mark.asyncio
async def test_whole_cent_ymat_class_price_unchanged(make_case):
    case = saiq_case(make_case, raw=Decimal("1.86"), stored=Decimal("1.86000000"))
    assert await attach(case) is True
    assert len(case.broker.requests) == 1 and not closes(case.broker)


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [12, 21])
async def test_pm_ah_attach_remains_off_without_touching_software_ladder(make_case, hour):
    case = saiq_case(make_case, at=datetime(2026, 10, 20, hour, 38, 43, tzinfo=UTC))
    assert await attach(case) is False
    assert not case.broker.requests and not case.broker.cancelled


@pytest.mark.asyncio
async def test_fractional_price_does_not_block_independent_riskoff_owner(make_case):
    case = saiq_case(make_case)
    await wb.request_atr_exit(case.service, case.broker.account, "SAIQ", COID, "hard_stop_15pct")
    assert len(closes(case.broker)) == 1 and closes(case.broker)[0].quantity == 59
    with case.factory() as session:
        row = session.scalar(select(OmsManagedPosition))
        assert row.current_quantity == 0 and row.status == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("session", ["AM", "NORMAL", "PM"])
@pytest.mark.parametrize("account", ["paper:schwab_1m_v2", "paper:webull_v2"])
async def test_actual_cw_hard_stop_not_blocked_by_fractional_stored_price(monkeypatch, session, account):
    from tests.unit.test_v2_cw_managed_exit import _make_sf, _svc, _arm, _quote, SYM
    from project_mai_tai.oms import service as module

    factory = _make_sf()
    # Configured fixture roles use simulated I/O, not native account qualification.
    service = _svc(factory, cw=True, dual=True)
    monkeypatch.setattr(module, "_is_regular_market_session", lambda now=None: session == "NORMAL")
    monkeypatch.setattr(module, "_extended_hours_session", lambda now=None: None if session == "NORMAL" else session)
    monkeypatch.setattr(service, "_market_is_fillable", lambda: True)
    _arm(service, factory, account=account, entry=float(RAW), qty=59)
    _quote(service, bid=4.40)
    await service._evaluate_v2_managed_exit(account, SYM)
    with factory() as unit:
        intents = unit.scalars(select(TradeIntent).where(TradeIntent.side == "sell")).all()
        assert len(intents) == 1 and intents[0].quantity == 59
        assert intents[0].reason.endswith("CW_HARD_STOP")


@pytest.mark.parametrize("account", ["live:schwab_1m_v2", "live:orb"])
def test_committed_cache_and_fresh_database_row_share_stored_precision(account):
    engine, factory = database()
    cache = ManagedTargetCache(factory, (account,))
    row_id, entry_id = uuid4(), uuid4()
    try:
        with factory() as session:
            row = OmsManagedPosition(id=row_id, strategy_code="schwab_1m_v2", broker_account_name=account,
                symbol="SAIQ", entry_order_id=entry_id, entry_price=RAW, original_quantity=59,
                current_quantity=59, status="open")
            session.add(row)
            session.flush()
            assert cache.get((account, "SAIQ")) is None
            session.commit()
        bound = cache.get((account, "SAIQ"))
        assert bound is not None and bound.entry_price == STORED
        context = {"cw_target_bound_row_id": str(row_id), "cw_target_entry_order_id": str(entry_id),
                   "cw_target_entry_price": str(RAW), "cw_target_quantity": "59"}
        with factory() as session:
            loaded = session.get(OmsManagedPosition, row_id)
            assert loaded.entry_price == STORED
            assert OmsRiskService._cw_target_binding_matches(loaded, context)
            assert not OmsRiskService._cw_target_binding_matches(loaded, {
                **context, "cw_target_entry_price": "5.10"})
            assert not OmsRiskService._cw_target_binding_matches(loaded, {
                **context, "cw_target_entry_order_id": str(uuid4())})
    finally:
        engine.dispose()


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal(0), Decimal(-1)])
def test_invalid_cache_price_is_unknown_without_aborting_fill_transaction(bad):
    cache = ManagedTargetCache.__new__(ManagedTargetCache)
    cache._accounts = {"live:orb"}
    row = SimpleNamespace(strategy_code="schwab_1m_v2", broker_account_name="live:orb", status="open",
                          entry_order_id=uuid4(), id=uuid4(), current_quantity=59, entry_price=bad)
    assert cache._value(row) is None
