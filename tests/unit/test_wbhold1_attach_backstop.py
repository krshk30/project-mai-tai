"""Bound native-protection controls, local broker boundary, reviewer14:5x."""

import asyncio
import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot
from project_mai_tai.db.models import BrokerOrder, Fill, Strategy, VirtualPosition
from project_mai_tai.oms import service as module
from tests.unit import test_edgeprot1_webull_edge as edge

replay = edge.replay


@pytest.fixture(autouse=True)
def controlled_clock(monkeypatch):
    monkeypatch.setattr(module, "utcnow", lambda: edge.NOW)
    monkeypatch.setattr(module, "_is_regular_market_session", lambda now=None: True)


def ledger(factory, quantity):
    if quantity is None:
        return
    with factory.begin() as session:
        entry = edge._entry(session)
        session.add(VirtualPosition(
            strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            symbol=edge.SYM, quantity=Decimal(quantity), average_price=Decimal("2.49"),
        ))


async def attach(service, requested=120):
    return await service._attach_webull_protection(
        broker_account_name=edge.ACCT, symbol=edge.SYM, quantity=requested,
        entry_price=2.49, strategy_code="schwab_1m_v2",
        entry_client_order_id="MNDR-OWNED-ENTRY",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("virtual", [0, None, 3, 120, 9999])
async def test_bare_fill_managed120_attaches_pair_despite_zero_or_stale_virtual(replay, virtual):
    service, factory, wire = replay
    ledger(factory, virtual)

    async def positions(account):
        return [BrokerPositionSnapshot(account, edge.SYM, Decimal(1120), Decimal("2.49"))]

    wire.list_account_positions = positions
    assert await attach(service)
    assert [request.quantity for request in wire.requests] == [Decimal(120)]
    assert all(leg["quantity"] == "120" for leg in wire.legs[0])
    with factory() as session:
        entry = service._find_oco_entry_order(
            session, edge.ACCT, edge.SYM, client_order_id="MNDR-OWNED-ENTRY",
        )
        assert entry.payload[service._WEBULL_PROTECT_STATE_KEY] == "resting"
        assert service._oco_exit_base_for_entry(entry, broker_account_name=edge.ACCT, symbol=edge.SYM)


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,managed,broker,expected", [
    (20, 120, 1120, 20), (120, 20, 1120, 20), (120, 120, 20, 20),
    (120, 0, 1120, 0), (120, 120, 0, 0), (120, 120, -1, 0),
])
async def test_bound_pair_keeps_request_managed_and_fresh_broker_ceilings(
    replay, requested, managed, broker, expected,
):
    service, factory, wire = replay
    ledger(factory, 9999)
    with factory.begin() as session:
        edge._row(session).current_quantity = managed

    async def positions(account):
        return [BrokerPositionSnapshot(account, edge.SYM, Decimal(broker), Decimal("2.49"))]

    wire.list_account_positions = positions
    assert await attach(service, requested) is bool(expected)
    assert [r.quantity for r in wire.requests] == ([Decimal(expected)] if expected else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exception", "empty", "none", "foreign_account", "foreign_symbol",
                                 "malformed", "nan", "missing_quantity", "duplicate"])
async def test_bound_pair_does_not_use_an_unreadable_or_foreign_broker_ceiling(replay, kind):
    service, factory, wire = replay
    ledger(factory, 0)

    async def positions(account):
        if kind == "exception":
            raise RuntimeError("CONTROLLED broker read failure")
        if kind == "empty":
            return []
        if kind == "none":
            return None
        if kind == "foreign_account":
            return [BrokerPositionSnapshot("OTHER", edge.SYM, Decimal(9999), Decimal(2))]
        if kind == "foreign_symbol":
            return [BrokerPositionSnapshot(account, "OTHER", Decimal(9999), Decimal(2))]
        if kind == "missing_quantity":
            return [SimpleNamespace(broker_account_name=account, symbol=edge.SYM)]
        if kind == "duplicate":
            return [BrokerPositionSnapshot(account, edge.SYM, Decimal(120), Decimal(2))]*2
        return [BrokerPositionSnapshot(account, edge.SYM,
            "bad" if kind == "malformed" else Decimal("NaN"), Decimal(2))]

    wire.list_account_positions = positions
    assert await attach(service) is False
    assert wire.requests == []


@pytest.mark.asyncio
async def test_foreign_zero_row_does_not_override_exact_account_broker_holding(replay):
    service, factory, wire = replay
    ledger(factory, 0)

    async def positions(account):
        return [
            BrokerPositionSnapshot("OTHER", edge.SYM, Decimal(0), Decimal(2)),
            BrokerPositionSnapshot(account, edge.SYM, Decimal(120), Decimal("2.49")),
        ]

    wire.list_account_positions = positions
    assert await attach(service)
    assert [r.quantity for r in wire.requests] == [Decimal(120)]


@pytest.mark.asyncio
async def test_broker_read_cancellation_is_not_turned_into_protection_authority(replay):
    service, factory, wire = replay
    ledger(factory, 0)

    async def positions(account):
        raise asyncio.CancelledError

    wire.list_account_positions = positions
    with pytest.raises(asyncio.CancelledError):
        await attach(service)
    assert wire.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["decrease", "zero", "closed", "entry", "strategy", "account", "symbol"])
async def test_attach_rechecks_bound_row_after_broker_await_and_warns(replay, change, caplog):
    service, factory, wire = replay
    ledger(factory, 9999)

    async def positions(account):
        with factory.begin() as session:
            row = edge._row(session)
            if change in {"decrease", "zero"}:
                row.current_quantity = 20 if change == "decrease" else 0
            elif change == "closed":
                row.status = "closed"
            elif change == "entry":
                row.entry_client_order_id = "FOREIGN-ENTRY"
            elif change == "strategy":
                row.strategy_code = "orb"
            elif change == "account":
                row.broker_account_name = "OTHER"
            else:
                row.symbol = "OTHER"
        return [BrokerPositionSnapshot(account, edge.SYM, Decimal(1120), Decimal("2.49"))]

    wire.list_account_positions = positions
    with caplog.at_level(logging.WARNING):
        assert await attach(service) is (change == "decrease")
    assert [r.quantity for r in wire.requests] == ([Decimal(20)] if change == "decrease" else [])
    if change != "decrease":
        assert "WEBULL-PROTECT-OWNERSHIP" in caplog.text


@pytest.mark.asyncio
async def test_same_entry_client_id_on_replacement_row_cannot_reuse_old_row_authority(replay, monkeypatch, caplog):
    service, factory, wire = replay
    ledger(factory, 0)
    original = service._run_db

    async def replace_between_reads(operation, **kwargs):
        result = await original(operation, **kwargs)
        if operation.__name__ == "_owned_episode":
            with factory.begin() as session:
                old = edge._row(session)
                old.symbol = "CLOSED-OLD"  # SQLite's test index is not partial.
                old.status = "closed"
                service.store.create_managed_position(
                    session, strategy_code="schwab_1m_v2", broker_account_name=edge.ACCT,
                    symbol=edge.SYM, quantity=120, entry_price=Decimal("2.49"),
                    entry_client_order_id="MNDR-OWNED-ENTRY",
                )
        return result

    monkeypatch.setattr(service, "_run_db", replace_between_reads)
    with caplog.at_level(logging.WARNING):
        assert await attach(service) is False
    assert wire.requests == []
    assert "episode_or_owner_changed" in caplog.text


@pytest.mark.asyncio
async def test_software_exit_reservation_arriving_during_read_still_defers_pair(replay, caplog):
    service, factory, wire = replay
    ledger(factory, 0)

    async def positions(account):
        with factory.begin() as session:
            entry = edge._entry(session)
            session.add(BrokerOrder(
                intent_id=entry.intent_id, strategy_id=entry.strategy_id,
                broker_account_id=entry.broker_account_id, client_order_id="CONTROLLED-PENDING-SELL",
                symbol=edge.SYM, side="sell", status="accepted", quantity=Decimal(120),
                order_type="LIMIT", time_in_force="day", submitted_at=edge.NOW, payload={},
            ))
        return [BrokerPositionSnapshot(account, edge.SYM, Decimal(1120), Decimal("2.49"))]

    wire.list_account_positions = positions
    with caplog.at_level(logging.WARNING):
        assert await attach(service) is False
    assert wire.requests == []
    assert "software_exit_reserved" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("first_read", ["settling", "unreadable"])
async def test_bound_attach_retains_retry_budget_and_one_broker_read_per_attempt(replay, monkeypatch, first_read):
    service, factory, wire = replay
    ledger(factory, 0)
    object.__setattr__(service.settings, "oms_webull_protect_attempts", 2)
    object.__setattr__(service.settings, "oms_webull_protect_interval_seconds", 1)
    calls = []

    async def positions(account):
        calls.append(account)
        if len(calls) == 1:
            if first_read == "unreadable":
                raise RuntimeError("CONTROLLED settling read")
            return []
        return [BrokerPositionSnapshot(account, edge.SYM, Decimal(120), Decimal("2.49"))]

    async def no_delay(_seconds):
        pass

    wire.list_account_positions = positions
    monkeypatch.setattr(module.asyncio, "sleep", no_delay)
    assert await attach(service)
    assert calls == [edge.ACCT, edge.ACCT]
    assert [r.quantity for r in wire.requests] == [Decimal(120)]


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_mode", ["live", "paper"])
async def test_unmarked_exact_episode_is_protection_only_without_fabricated_ledger(replay, execution_mode):
    from project_mai_tai.oms.webull_attach_riskoff import BARE_FILL_PROOF, KEY
    service, factory, wire = replay
    with factory.begin() as session:
        session.get(Strategy, edge._entry(session).strategy_id).execution_mode = execution_mode

    async def positions(account):
        return [BrokerPositionSnapshot(account, edge.SYM, Decimal(1120), Decimal("2.49"))]

    wire.list_account_positions = positions
    assert await attach(service)
    assert len(wire.requests) == 1 and wire.requests[0].quantity == 120
    assert wire.requests[0].metadata["webull_exit_only_pair"] == "true"
    with factory() as session:
        assert session.scalar(select(VirtualPosition)) is None
        assert session.scalar(select(Fill)) is None
        entry = edge._entry(session)
        assert KEY not in entry.payload and BARE_FILL_PROOF not in entry.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["stop", "target"])
async def test_unmarked_legacy_crossed_price_never_gets_riskoff_sell_authority(replay, monkeypatch, side):
    from project_mai_tai.oms.webull_attach_riskoff import BARE_FILL_PROOF, KEY
    service, factory, wire = replay
    with factory.begin() as session:
        session.get(Strategy, edge._entry(session).strategy_id).execution_mode = "live"

    async def quote(**_kwargs):
        price = 2 if side == "stop" else 3
        return {"bid_price": price, "ask_price": price, "last_price": price}

    monkeypatch.setattr(service, "_fetch_quote_for_order", quote)
    assert not await attach(service)
    assert wire.requests == []
    assert (edge.ACCT, edge.SYM) in service._webull_protect_failed
    with factory() as session:
        assert session.scalar(select(BrokerOrder).where(BrokerOrder.side == "sell")) is None
        entry = edge._entry(session)
        assert KEY not in entry.payload and BARE_FILL_PROOF not in entry.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["wbattach1", "wbattach1_bare_fill_observed"])
async def test_legacy_inflight_attach_cannot_wire_after_strict_marker_appears(replay, marker, caplog):
    service, factory, wire = replay
    reads = []

    async def positions(account):
        reads.append(account)
        with factory.begin() as session:
            entry = edge._entry(session)
            entry.payload = {**entry.payload, marker: {}}
        return [BrokerPositionSnapshot(account, edge.SYM, Decimal(1120), Decimal("2.49"))]

    wire.list_account_positions = positions
    assert not await attach(service)
    assert reads == [edge.ACCT] and wire.requests == []
    assert "strict_episode_requires_strict_owner" in caplog.text
    with factory() as session:
        assert edge._entry(session).payload[marker] == {}
        assert session.scalar(select(BrokerOrder).where(BrokerOrder.side == "sell")) is None


@pytest.mark.asyncio
async def test_legacy_attach_rechecks_entry_uuid_binding_after_positions_await(replay, caplog):
    service, factory, wire = replay
    reads = []

    async def positions(account):
        reads.append(account)
        with factory.begin() as session:
            edge._row(session).entry_order_id = None
        return [BrokerPositionSnapshot(account, edge.SYM, Decimal(1120), Decimal("2.49"))]

    wire.list_account_positions = positions
    assert not await attach(service)
    assert reads == [edge.ACCT] and wire.requests == []
    assert "episode_or_owner_changed" in caplog.text
