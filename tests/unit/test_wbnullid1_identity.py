
from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from tests.unit.oms_sqlite_fixtures import own_service
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from threading import get_ident
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.models import Base, BrokerOrder, SystemIncident, TradeIntent
from project_mai_tai.oms import webull_order_identity as identity
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from tests.unit.test_v2_exit_paper_isolation import _FakeRedis, _make_sf


def setup(*, broker_id=None, provider="webull", sessions=None):
    sessions = _make_sf() if sessions is None else sessions
    adapter = AsyncMock()
    service = own_service(OmsRiskService(
        Settings(_env_file=None, oms_adapter="simulated", oms_cancel_verify_interval_seconds=0,
                 strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
                 strategy_schwab_1m_v2_webull_account_name="live:orb"),
        redis_client=_FakeRedis(), session_factory=sessions, broker_adapter=adapter,
    ))
    with sessions.begin() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2")
        account = service.store.ensure_broker_account(session, "live:orb", provider=provider, environment="test")
        intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id,
                             symbol="FLYE", side="buy", intent_type="open", quantity=Decimal(2),
                             status="accepted", reason="CONTROLLED", payload={})
        session.add(intent)
        session.flush()
        order = BrokerOrder(intent_id=intent.id, strategy_id=strategy.id, broker_account_id=account.id,
                            symbol="FLYE", side="buy", quantity=Decimal(2), status="accepted",
                            client_order_id="CONTROLLED-client", broker_order_id=broker_id,
                            order_type="STOP_LIMIT", time_in_force="day",
                            payload={"fanout_slot_id": "unchanged-slot"},
                            submitted_at=datetime(2026, 10, 8, tzinfo=UTC),
                            updated_at=datetime(2026, 10, 8, tzinfo=UTC))
        session.add(order)
        session.flush()
        order_id = order.id
    report = ExecutionReport("accepted", "CONTROLLED-client", broker_order_id="CONTROLLED-venue",
                             symbol="FLYE", side="buy", quantity=Decimal(2), origin="broker")
    return service, sessions, order_id, report


def evidence(order_id, report, source="order_sync"):
    return identity.IdentityEvidence.from_report(order_id, "live:orb", "CONTROLLED-client",
                                                "FLYE", "buy", report, source)


def snapshot(sessions, order_id):
    with sessions() as session:
        row = session.get(BrokerOrder, order_id)
        return {c.name: getattr(row, c.name) for c in BrokerOrder.__table__.columns}


async def persist(service, items):
    return await service._run_db(lambda session: identity.persist_identities(
        session, items, owner_guard=service._rpg_owns_old_order,
    ))


@pytest.mark.asyncio
async def test_unchanged_status_identity_is_id_only_off_loop(monkeypatch):
    service, sessions, order_id, report = setup()
    before = snapshot(sessions, order_id)
    threads = []
    native = identity.persist_identities
    main_thread = get_ident()

    def probe(*args, **kwargs):
        threads.append(get_ident())
        return native(*args, **kwargs)

    monkeypatch.setattr(identity, "persist_identities", probe)
    service._queue_webull_order_identities([evidence(order_id, report)])
    await service._drain_webull_order_identities()
    after = snapshot(sessions, order_id)
    assert after.pop("broker_order_id") == "CONTROLLED-venue"
    before.pop("broker_order_id")
    assert after == before
    assert threads and all(t != main_thread for t in threads)
    service.broker_adapter.assert_not_called()
    assert await persist(service, [evidence(order_id, report)]) == ["already_saved"]
    await service._webull_identity_writer.close()


@pytest.mark.asyncio
async def test_real_sync_unchanged_accepted_saves_id_without_fill_or_new_read(monkeypatch, tmp_path):
    # The actual sync reader and dedicated identity writer overlap. StaticPool's
    # single in-memory connection lets one Session release the other's SAVEPOINT.
    # Keep this threaded case on private file-backed, independent connections.
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'identity.sqlite'}",
        connect_args={"check_same_thread": False}, poolclass=NullPool)
    tables = [table for table in Base.metadata.sorted_tables
        if table.name not in {"market_trade_ticks", "market_quote_ticks"}]
    Base.metadata.create_all(engine, tables=tables)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service, sessions, order_id, report = setup(sessions=sessions)
    before = snapshot(sessions, order_id)
    service.broker_adapter.fetch_order_update.return_value = report
    monkeypatch.setattr(service, "_should_refresh_working_order", lambda row: False)
    service._refresh_drift_working_cache = AsyncMock()
    try:
        result = await service.sync_broker_orders(account_names=["live:orb"])
        await service._drain_webull_order_identities()
        assert result == {"orders": 0, "terminal_orders": 0}
        service.broker_adapter.fetch_order_update.assert_awaited_once()
        after = snapshot(sessions, order_id)
        assert after.pop("broker_order_id") == "CONTROLLED-venue"
        before.pop("broker_order_id")
        assert after == before
    finally:
        writer = service.__dict__.get("_webull_identity_writer")
        if writer is not None:
            await writer.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_cancel_verifier_saves_native_id_without_mutating_cancel_status():
    service, sessions, order_id, report = setup()
    report = replace(report, event_type="cancelled", intent_type="cancel")
    service.broker_adapter.fetch_order_update.return_value = report
    request = OrderRequest("CONTROLLED-client", "live:orb", "schwab_1m_v2", "FLYE",
                           "buy", "cancel", Decimal(2), "CONTROLLED")
    assert await service._verify_cancel_landed(
        request=request, account_name="live:orb", symbol="FLYE", client_order_id=request.client_order_id,
        broker_order_id="", target_order_id=order_id,
    ) == "cancelled"
    await service._drain_webull_order_identities()
    row = snapshot(sessions, order_id)
    assert row["broker_order_id"] == "CONTROLLED-venue" and row["status"] == "accepted"
    await service._webull_identity_writer.close()


@pytest.mark.parametrize("field,value", [("client_order_id", "other"), ("symbol", "OTHER"),
    ("side", "sell"), ("origin", "unknown"), ("broker_order_id", None),
    ("broker_order_id", "CONTROLLED-client")])
def test_unbound_or_client_only_report_is_not_identity_proof(field, value):
    _, _, order_id, report = setup()
    assert evidence(order_id, replace(report, **{field: value})) is None


@pytest.mark.asyncio
async def test_conflicting_id_is_not_overwritten_and_creates_one_incident(monkeypatch):
    service, sessions, order_id, report = setup(broker_id="original")
    loud = Mock(wraps=identity.LOG.error)
    monkeypatch.setattr(identity.LOG, "error", loud)
    before = snapshot(sessions, order_id)
    assert await persist(service, [evidence(order_id, report)]) == ["conflict"]
    assert await persist(service, [evidence(order_id, report)]) == ["conflict"]
    assert snapshot(sessions, order_id) == before
    with sessions() as session:
        assert len(session.scalars(select(SystemIncident)).all()) == 1
    assert loud.call_count == 2 and "identity_conflict" in loud.call_args.args[0]


@pytest.mark.asyncio
async def test_owner_changed_before_write_remains_fenced(monkeypatch):
    service, sessions, order_id, report = setup()
    monkeypatch.setattr(service, "_rpg_owns_old_order", lambda *args: True)
    assert await persist(service, [evidence(order_id, report)]) == ["owner_refused"]
    assert snapshot(sessions, order_id)["broker_order_id"] is None


@pytest.mark.asyncio
async def test_inline_ack_conflict_diagnostic_failure_does_not_poison_fill_transaction(monkeypatch):
    service, sessions, order_id, report = setup(broker_id="original")

    def fail(session, *args):
        session.execute(SystemIncident.__table__.insert().values(id=uuid4(), severity=None))

    monkeypatch.setattr(identity, "record_conflict", fail)
    with sessions.begin() as session:
        order = session.get(BrokerOrder, order_id)
        service.store.update_order_from_report(order, report=report, metadata=order.payload)
        assert order.broker_order_id == "original"
        service.store.apply_fill_to_positions(session, strategy_id=order.strategy_id,
            broker_account_id=order.broker_account_id, symbol=order.symbol, side="buy",
            quantity=Decimal(2), price=Decimal(3), reported_at=datetime.now(UTC))
    await service._drain_webull_order_identities()
    await service._webull_identity_writer.close()
    with sessions() as session:
        order = session.get(BrokerOrder, order_id)
        assert service.store.get_virtual_position(session, strategy_id=order.strategy_id,
            broker_account_id=order.broker_account_id, symbol=order.symbol).quantity == 2


def test_non_webull_report_update_is_unchanged():
    service, sessions, order_id, report = setup(broker_id="original", provider="schwab")
    with sessions.begin() as session:
        order = session.get(BrokerOrder, order_id)
        service.store.update_order_from_report(order, report=report, metadata=order.payload)
        assert order.broker_order_id == "CONTROLLED-venue"
