"""Real Webull confirmation parser through the actual ATR/ORB exit owners."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters import webull as adapter_module
from project_mai_tai.broker_adapters.webull import WebullAccountConfig
from project_mai_tai.db.models import Fill, VirtualPosition
from project_mai_tai.oms import orb_country, service as oms
from tests.unit.test_webull_adapter import _LegClient, _adapter, fake_sdk as fake_sdk
from tests.unit.test_wbattach1_riskoff import NOW, attach, closes, make_case as make_case


def install_raw_confirmation(case, monkeypatch, *, filled=0, damage=None, execution_aliases=None):
    broker = case[2]
    broker.mode = "late"
    seen = []
    applied = False
    monkeypatch.setattr(adapter_module, "datetime", oms.datetime)

    async def confirm(**binding):
        nonlocal applied
        base = binding["base_client_order_id"]
        bodies = {}
        for suffix in ("T", "S"):
            coid = adapter_module.WebullBrokerAdapter._combo_leg_coid(base, suffix)
            item = {"order_id": "native-" + coid, "client_order_id": coid,
                "symbol": "ABC", "account_id": "ACC1", "side": "SELL",
                "order_status": "FILLED" if filled == 10 and suffix == "T" else "CANCELLED",
                "filled_qty": str(filled if suffix == "T" else 0),
                "filled_price": "11" if filled and suffix == "T" else None}
            body = {"items": [item]}
            if suffix == "T" and execution_aliases is not None:
                item.update(execution_aliases)
            if suffix == "S":
                if damage == "working":
                    item["order_status"] = "WORKING"
                elif damage == "missing-id":
                    item.pop("order_id")
                elif damage == "conflicting-id":
                    body["order_id"] = "different-native-child"
                elif damage == "wrong-coid":
                    item["client_order_id"] = "other-child"
                elif damage == "missing-quantity":
                    item.pop("filled_qty")
                elif damage == "nan-quantity":
                    item["filled_qty"] = "NaN"
                elif damage == "absent":
                    continue  # Existing reader's genuine ORDER_NOT_FOUND exception contract.
            bodies[coid] = body
        client = _LegClient(bodies)
        adapter = _adapter(client)
        adapter.accounts_by_name = {broker.account: WebullAccountConfig(account_id="ACC1")}
        result = await adapter.confirm_exit_pair_terminal(**binding)
        seen.extend(result.reports)
        if not applied:
            broker.total -= Decimal(filled)
            applied = True
        return result

    broker.confirm_exit_pair_terminal = confirm
    return seen


async def drive(case, strategy):
    if strategy == "schwab_1m_v2":
        await attach(case)
    else:
        with pytest.raises(TimeoutError):
            await orb_country.reconcile_one(case[0], case[3])
        await orb_country.reconcile_one(case[0], case[3])


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
@pytest.mark.parametrize("account", ["live:orb", "live:webull-alt"])
@pytest.mark.parametrize("filled", [0, 2])
async def test_raw_terminal_children_preserve_identity_partial_and_owned_residual(
    make_case, fake_sdk, monkeypatch, strategy, account, filled
):
    case = make_case(strategy, account)
    reports = install_raw_confirmation(case, monkeypatch, filled=filled)
    await drive(case, strategy)
    assert [request.quantity for request in closes(case[2])] == [Decimal(10 - filled)]
    assert case[2].total == 1000
    assert all(report.broker_order_id == "native-" + report.client_order_id for report in reports)
    assert reports[0].filled_quantity == filled
    if filled:
        assert reports[0].broker_fill_id == reports[0].broker_order_id + ":2"
        assert reports[0].fill_price == 11
        with case[1]() as unit:
            child_fill = unit.scalar(select(Fill).where(Fill.broker_fill_id == reports[0].broker_fill_id))
            assert child_fill.quantity == 2 and child_fill.price == 11
            assert child_fill.payload["metadata"]["webull_terminal_with_partial_fill"] == "cancelled"
    if strategy == "schwab_1m_v2":
        await attach(case)
    else:
        await orb_country.reconcile_one(case[0], case[3])
    assert [request.quantity for request in closes(case[2])] == [Decimal(10 - filled)]
    assert case[2].total == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("damage,origin,event_type,cancel_outcome,closed", [
    ("absent", "broker", "cancelled", "already_absent", True),
    ("working", "broker", "accepted", "not_confirmed", False),
    ("missing-id", "unknown", "accepted", "could_not_tell", False),
], ids=["absent", "working", "missing-id"])
async def test_real_raw_fallback_reports_use_the_defining_protocol_clock(
    make_case, fake_sdk, monkeypatch, damage, origin, event_type, cancel_outcome, closed
):
    case = make_case()
    reports = install_raw_confirmation(case, monkeypatch, damage=damage)
    await attach(case)
    assert reports and all(report.reported_at == NOW for report in reports)
    coid = adapter_module.WebullBrokerAdapter._combo_leg_coid(case[2].base, "S")
    sibling = [report for report in reports if report.client_order_id == coid]
    assert sibling
    for report in sibling:
        assert (report.origin, report.event_type) == (origin, event_type)
        assert report.symbol == "ABC" and report.side == "sell" and report.intent_type == "cancel"
        assert report.filled_quantity == 0 and report.broker_fill_id is None
        assert report.metadata["eod_exit_pair_confirmation"] == "true"
        assert report.metadata["cancel_outcome"] == cancel_outcome
        assert report.broker_order_id == ("native-" + coid if damage == "working" else None)
    assert len(closes(case[2])) == int(closed)
    assert case[2].total == (1000 if closed else 1010)
    with case[1]() as unit:
        assert unit.scalar(select(VirtualPosition.quantity)) == (0 if closed else 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
@pytest.mark.parametrize("damage", ["missing-id", "conflicting-id", "wrong-coid",
    "missing-quantity", "nan-quantity", "working"])
async def test_raw_ambiguous_child_never_releases_or_submits_residual(
    make_case, fake_sdk, monkeypatch, strategy, damage
):
    case = make_case(strategy)
    install_raw_confirmation(case, monkeypatch, damage=damage)
    await drive(case, strategy)
    assert not closes(case[2])
    assert case[2].total == 1010


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_raw_filled_target_unknown_sibling_books_once_without_second_sell(
    make_case, fake_sdk, monkeypatch, strategy
):
    case = make_case(strategy)
    install_raw_confirmation(case, monkeypatch, filled=10, damage="working")
    await drive(case, strategy)
    if strategy == "schwab_1m_v2":
        await attach(case)
    else:
        await orb_country.reconcile_one(case[0], case[3])
    assert not closes(case[2])
    with case[1]() as unit:
        assert sum(unit.scalars(select(Fill.quantity).where(Fill.side == "sell")), Decimal(0)) == 10
        assert unit.scalar(select(VirtualPosition.quantity)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_raw_partial_target_unknown_sibling_books_once_without_residual_sell(
    make_case, fake_sdk, monkeypatch, strategy
):
    case = make_case(strategy)
    install_raw_confirmation(case, monkeypatch, filled=2, damage="working")
    await drive(case, strategy)
    if strategy == "schwab_1m_v2":
        await attach(case)
    else:
        await orb_country.reconcile_one(case[0], case[3])
    assert not closes(case[2]) and case[2].total == 1008
    with case[1]() as unit:
        sold = list(unit.scalars(select(Fill).where(Fill.side == "sell")))
        assert len(sold) == 1 and sold[0].quantity == 2 and sold[0].price == 11
        assert unit.scalar(select(VirtualPosition.quantity)) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_raw_order_not_found_absence_remains_distinct_from_missing_native_id(
    make_case, fake_sdk, monkeypatch, strategy
):
    case = make_case(strategy)
    reports = install_raw_confirmation(case, monkeypatch, damage="absent")
    await drive(case, strategy)
    assert [request.quantity for request in closes(case[2])] == [Decimal(10)]
    absent = [report for report in reports if report.metadata.get("cancel_outcome") == "already_absent"]
    assert len(absent) == 1 and absent[0].broker_order_id is None
    assert absent[0].metadata["eod_exit_pair_confirmation"] == "true"


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
@pytest.mark.parametrize("aliases", [
    {"filled_qty": "0", "filledQty": "2"},
    {"filled_qty": "2", "filledQty": "0"},
    {"filled_qty": "0", "filledQty": "NaN"},
    {"filled_qty": "0", "filledQty": "not-a-number"},
    {"filled_qty": "2", "filled_price": "11", "filledPrice": "12"},
    {"filled_qty": "2", "filled_price": "11", "avgFillPrice": "NaN"},
])
async def test_conflicting_execution_aliases_never_oversell_into_manual_shares(
    make_case, fake_sdk, monkeypatch, strategy, aliases
):
    case = make_case(strategy)
    install_raw_confirmation(case, monkeypatch, filled=2, execution_aliases=aliases)
    await drive(case, strategy)
    assert not closes(case[2])
    assert case[2].total == 1008


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_equivalent_decimal_aliases_preserve_execution_and_residual_eight(
    make_case, fake_sdk, monkeypatch, strategy
):
    case = make_case(strategy)
    reports = install_raw_confirmation(case, monkeypatch, filled=2, execution_aliases={
        "filled_qty": "2", "filledQty": "2.00000000",
        "filled_price": "11", "avgFillPrice": "11.0000"})
    await drive(case, strategy)
    assert [request.quantity for request in closes(case[2])] == [Decimal(8)]
    assert reports[0].filled_quantity == 2 and reports[0].fill_price == 11
    assert case[2].total == 1000
