"""OWNMIX1: recorded MI parents, durable ownership, both accounts and every session."""
from __future__ import annotations

import copy
import importlib.util
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters import schwab as schwab_module
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.db.models import (
    AccountPosition, Base, BrokerOrder, Fill, OmsManagedPosition,
    TradeIntent, VirtualPosition,
)
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ownmix1"
ROWS = json.loads((FIXTURES / "mi_rows.json").read_text())
PARENTS = json.loads((FIXTURES / "mi_broker_parents.json").read_text())
ACCT = "live:schwab_1m_v2"
V2_COID = "schwab_1m_v2-MI-open-2220fd5b2d58"
ORB_COID = "orb_schwab-MI-open-c16106e4d4e5"


class _Redis:
    async def xadd(self, *_args, **_kwargs):
        return "1-0"


@pytest.fixture
def replay(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    service = OmsRiskService(
        settings=Settings(oms_adapter="simulated", oms_v2_exit_management_enabled=True,
                          strategy_schwab_1m_v2_account_name=ACCT,
                          oms_native_oco_exit_poll_enabled=True,
                          oms_native_oco_exit_poll_min_secs=0,
                          oms_record_native_oco_exit_fills_enabled=True,
                          oms_native_oco_stand_down_enabled=True),
        redis_client=_Redis(), session_factory=sessions,
    )
    with sessions() as session:
        for data in ROWS["orders"]:
            strategy = service.store.ensure_strategy(session, data["strategy"])
            account = service.store.ensure_broker_account(
                session, data["account"], provider="webull" if data["account"] == "live:orb"
                else "schwab", environment="live")
            intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id,
                                 symbol="MI", side="buy", intent_type="open",
                                 quantity=Decimal(data["quantity"]), reason="ATR Flip",
                                 status="filled", payload={})
            session.add(intent)
            session.flush()
            order = BrokerOrder(
                id=UUID(data["id"]), intent_id=intent.id, strategy_id=strategy.id,
                broker_account_id=account.id, symbol="MI", side="buy",
                client_order_id=data["client_order_id"], broker_order_id=data["broker_order_id"],
                quantity=Decimal(data["quantity"]), status="filled", order_type="STOP_LIMIT",
                time_in_force="day", payload=data["payload"],
                submitted_at=datetime.fromisoformat(data["submitted_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
            )
            session.add(order)
        session.commit()
        for data in ROWS["managed"]:
            coid = V2_COID if data["broker_account_name"] == ACCT else ROWS["orders"][2]["client_order_id"]
            order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == coid))
            service.store.create_managed_position(
                session, strategy_code=data["strategy_code"],
                broker_account_name=data["broker_account_name"], symbol="MI",
                entry_price=Decimal(data["entry_price"]), quantity=data["original_quantity"],
                entry_time=datetime.fromisoformat(data["entry_time"]),
                entry_order_id=order.id, entry_client_order_id=order.client_order_id,
            )
        session.commit()

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 5, 13, 40, tzinfo=UTC)

    monkeypatch.setattr(schwab_module, "datetime", _Clock)
    adapter = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    adapter.settings = service.settings
    adapter.accounts_by_name = {ACCT: SimpleNamespace(account_hash="REPLAY")}
    bodies = {str(item["body"]["orderId"]): copy.deepcopy(item["body"])
              for item in PARENTS["orders"]}
    calls = []

    async def request(method, path):
        calls.append((method, path))
        assert method == "GET"
        parent = path.split("?")[0].rsplit("/", 1)[1]
        return 200, {}, bodies.get(parent, list(bodies.values()))

    adapter._authorized_request_json = request
    service.broker_adapter = adapter
    return service, sessions, bodies, calls


def _entry(session, coid=V2_COID):
    return session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == coid))


def _row(service, session, account=ACCT):
    return service.store.get_open_managed_position(
        session, broker_account_name=account, symbol="MI")


def _detail(parent="1008165370999", coid=V2_COID, qty="180"):
    return {"symbol": "MI", "quantity": Decimal(qty), "price": Decimal("3.05"),
            "filled_at": datetime(2026, 10, 5, 13, 39, 37, tzinfo=UTC),
            "broker_order_id": "1008165371002" if qty == "180" else "1008165370307",
            "entry_broker_order_id": parent, "exit_base_client_order_id": coid}


@pytest.mark.asyncio
async def test_o_t1_recorded_orb_exit_cannot_resolve_v2_and_logs_once(replay, caplog):
    service, sessions, _, _ = replay
    service._managed_v2_symbols.add((ACCT, "MI"))
    foreign = _detail("1008165370304", ORB_COID, "2")
    for _ in range(3):
        assert not await service._close_resolved_oco_managed_row(ACCT, "MI", detail=foreign)
    with sessions() as session:
        assert _row(service, session).current_quantity == 180
        assert service._find_oco_entry_order(session, ACCT, "MI").client_order_id == V2_COID
        assert session.scalars(select(Fill)).all() == []
    lines = [r.message for r in caplog.records if "ENTRY-OWNERSHIP-MISMATCH" in r.message]
    assert len(lines) == 1
    assert V2_COID in lines[0] and ORB_COID in lines[0]
    assert not service._native_oco_stand_down_active(ACCT, "MI")
    assert (ACCT, "MI") in service._managed_v2_symbols


@pytest.mark.asyncio
async def test_o_t2_recorded_v2_parent_child_is_polled_recorded_and_closed(replay):
    service, sessions, _, calls = replay
    service._v2_accounts = lambda: [ACCT]
    await service._poll_native_oco_exits()
    with sessions() as session:
        assert _row(service, session) is None
        fill = session.scalar(select(Fill))
        assert fill.quantity == 180 and fill.price == Decimal("3.05")
        assert fill.broker_fill_id.startswith("1008165371002:")
        assert session.get(BrokerOrder, fill.order_id).strategy_id == _entry(session).strategy_id
    assert all("1008165370304" not in path for _, path in calls)


@pytest.mark.asyncio
async def test_o_t3_equal_quantity_older_same_strategy_episode_updated_later_is_refused(replay):
    service, sessions, _, _ = replay
    with sessions() as session:
        old = _entry(session, ORB_COID)
        old.strategy_id = _entry(session).strategy_id
        old.quantity = Decimal("180")
        old.updated_at = datetime(2026, 10, 5, 14, tzinfo=UTC)
        session.commit()
        assert service._find_oco_entry_order(session, ACCT, "MI").client_order_id == V2_COID
    assert not await service._close_resolved_oco_managed_row(
        ACCT, "MI", detail=_detail("1008165370304", ORB_COID, "180"))
    with sessions() as session:
        assert _row(service, session).current_quantity == 180


@pytest.mark.asyncio
async def test_o_t4_orb_and_v2_resolve_only_their_own_recorded_children(replay):
    service, sessions, _, _ = replay
    with sessions() as session:
        orb = _entry(session, ORB_COID)
        session.add(VirtualPosition(strategy_id=orb.strategy_id,
                                    broker_account_id=orb.broker_account_id, symbol="MI",
                                    quantity=2, average_price=Decimal("3.0498")))
        session.commit()
    await service._poll_orb_schwab_child_exits()
    with sessions() as session:
        assert _row(service, session).current_quantity == 180
        fill = session.scalar(select(Fill))
        assert fill.quantity == 2
        assert session.get(BrokerOrder, fill.order_id).strategy_id == _entry(session, ORB_COID).strategy_id
    assert await service._close_resolved_oco_managed_row(ACCT, "MI", detail=_detail())
    with sessions() as session:
        assert len(session.scalars(select(Fill)).all()) == 2


@pytest.mark.asyncio
async def test_o_t5_actual_parent_read_429_keeps_row_for_retry(replay):
    service, sessions, _, _ = replay

    async def failed(*_args, **_kwargs):
        raise RuntimeError("HTTP 429")

    service.broker_adapter._authorized_request_json = failed
    service._v2_accounts = lambda: [ACCT]
    await service._poll_native_oco_exits()
    with sessions() as session:
        assert _row(service, session).current_quantity == 180
        assert session.scalars(select(Fill)).all() == []
    assert (ACCT, "MI") in service._managed_v2_symbols


@pytest.mark.asyncio
async def test_o_t6_actual_mi_webull_handle_attaches_only_to_bound_entry(replay):
    service, sessions, _, _ = replay
    web_coid = ROWS["orders"][2]["client_order_id"]
    assert await service._persist_webull_protect_base(
        "live:orb", "MI", "RECORDED-ENTRY-protect-control", entry_client_order_id=web_coid)
    assert not await service._persist_webull_protect_base("live:orb", "MI", "EMPTY")
    assert not await service._persist_webull_protect_base(
        "live:orb", "MI", "FOREIGN", entry_client_order_id=ORB_COID)
    with sessions() as session:
        web = _entry(session, web_coid)
        assert web.payload["webull_protect_base_client_order_id"] == "RECORDED-ENTRY-protect-control"
        assert "webull_protect_base_client_order_id" not in _entry(session, ORB_COID).payload


def test_o_t7_restart_preserves_durable_entry_and_reenrolls_both_accounts(replay):
    service, sessions, _, _ = replay
    service._managed_v2_symbols.clear()
    service._v2_accounts = lambda: [ACCT, "live:orb"]
    service._rehydrate_managed_v2_symbols()
    assert service._managed_v2_symbols == {(ACCT, "MI"), ("live:orb", "MI")}
    with sessions() as session:
        assert service._find_oco_entry_order(session, ACCT, "MI").client_order_id == V2_COID


@pytest.mark.asyncio
async def test_o_t8_owned_equal_quantity_fill_still_records_before_close(replay):
    service, sessions, _, _ = replay
    with sessions() as session:
        row_id = _row(service, session).id
        assert service._persist_oco_exit_fill(session, ACCT, "MI", _entry(session), _detail())
        session.commit()
    assert await service._close_resolved_oco_managed_row(ACCT, "MI", detail=_detail())
    with sessions() as session:
        assert session.get(OmsManagedPosition, row_id).status == "closed"
        assert len(session.scalars(select(Fill)).all()) == 1


def test_o_t9_actual_mi_restore_then_clear_leaves_closed_orb_row_untouched(replay):
    service, sessions, _, _ = replay
    with sessions() as session:
        v2, orb = _entry(session), _entry(session, ORB_COID)
        # ORB zero after its recorded sale; v2 zero at the observed erroneous-clear point.
        orb_virtual = VirtualPosition(strategy_id=orb.strategy_id,
                                      broker_account_id=orb.broker_account_id, symbol="MI",
                                      quantity=0, average_price=0, realized_pnl=Decimal("-0.4996"))
        v2_virtual = VirtualPosition(strategy_id=v2.strategy_id,
                                     broker_account_id=v2.broker_account_id, symbol="MI",
                                     quantity=0, average_price=0)
        session.add_all([orb_virtual, v2_virtual])
        session.add(AccountPosition(broker_account_id=v2.broker_account_id, symbol="MI",
                                    quantity=180, average_price=Decimal("3.31"), market_value=0))
        session.flush()
        before = (orb_virtual.quantity, orb_virtual.average_price,
                  orb_virtual.realized_pnl, orb_virtual.opened_at, orb_virtual.updated_at)
        restored = service.store.restore_virtual_positions_from_managed(session)
        assert restored == [(v2.broker_account_id, "MI", Decimal("180"))]
        assert v2_virtual.quantity == 180
        backing = session.scalar(select(AccountPosition))
        backing.quantity = 0
        cleared = service.store.clear_virtual_positions_without_account_backing(session)
        assert cleared == [(v2.broker_account_id, "MI", Decimal("180"))]
        assert before == (orb_virtual.quantity, orb_virtual.average_price,
                          orb_virtual.realized_pnl, orb_virtual.opened_at, orb_virtual.updated_at)


@pytest.mark.asyncio
async def test_legacy_unbound_row_stays_open_without_child_poll_or_standdown(replay, caplog):
    service, sessions, _, calls = replay
    with sessions() as session:
        row = _row(service, session)
        row.entry_order_id = None
        row.entry_client_order_id = None
        session.commit()
    service._v2_accounts = lambda: [ACCT]
    for _ in range(3):
        await service._poll_native_oco_exits()
    assert not await service._close_resolved_oco_managed_row(ACCT, "MI", detail=_detail())
    assert calls == []
    assert not service._native_oco_stand_down_active(ACCT, "MI")
    with sessions() as session:
        assert _row(service, session).current_quantity == 180
    assert len([r for r in caplog.records if "ENTRY-OWNERSHIP-MISMATCH" in r.message]) == 1


@pytest.mark.parametrize("account", [ACCT, "live:orb"])
@pytest.mark.parametrize("session_name", ["AM", "RTH", "PM"])
def test_bound_lookup_all_accounts_sessions_and_eod_parent(replay, account, session_name):
    service, sessions, _, _ = replay
    with sessions() as session:
        row = _row(service, session, account)
        entry = service._find_oco_entry_order(session, account, "MI")
        entry.payload = {**entry.payload, "session": session_name}
        session.flush()
        assert entry.id == row.entry_order_id
        assert service._latest_filled_entry_order_id(session, account, "MI") == entry.broker_order_id


@pytest.mark.parametrize("field", ["entry_order_id", "entry_client_order_id", "strategy_code"])
def test_binding_each_identity_component_is_required(replay, field):
    service, sessions, _, _ = replay
    with sessions() as session:
        row = _row(service, session)
        setattr(row, field, uuid4() if field == "entry_order_id" else "orb_schwab")
        assert service._find_oco_entry_order(session, ACCT, "MI") is None


@pytest.mark.asyncio
async def test_foreign_live_bracket_never_stands_down_owned_ladder(replay):
    service, sessions, bodies, _ = replay
    # Sensitivity control: only the foreign parent's legs are live.
    for child in bodies["1008165370304"]["childOrderStrategies"][0]["childOrderStrategies"]:
        child["status"] = "WORKING"
    service._managed_v2_symbols = {(ACCT, "MI")}
    await service._refresh_native_oco_armed_state([ACCT])
    assert not service._native_oco_stand_down_active(ACCT, "MI")
    with sessions() as session:
        assert _row(service, session).current_quantity == 180


@pytest.mark.parametrize("field,value", [
    ("entry_broker_order_id", "1008165370304"),
    ("exit_base_client_order_id", ORB_COID),
    ("quantity", Decimal("2")),
    ("symbol", "APUS"),
])
@pytest.mark.asyncio
async def test_each_exit_proof_component_is_independently_required(replay, field, value):
    service, sessions, _, _ = replay
    detail = {**_detail(), field: value}
    assert not await service._close_resolved_oco_managed_row(ACCT, "MI", detail=detail)
    assert not service._native_oco_stand_down_active(ACCT, "MI")
    with sessions() as session:
        assert _row(service, session).current_quantity == 180
        assert session.scalars(select(Fill)).all() == []


def test_real_open_fill_writes_both_entry_ids(replay):
    service, sessions, _, _ = replay
    with sessions() as session:
        row = _row(service, session)
        session.delete(row)
        session.flush()
        entry = _entry(session)
        service._apply_managed_position_after_fill(
            session=session, strategy_code="schwab_1m_v2", broker_account_name=ACCT,
            symbol="MI", side="buy", intent_type="open", quantity=Decimal("180"),
            price=Decimal("3.31"), metadata={}, entry_client_order_id=entry.client_order_id,
            entry_order_id=entry.id,
        )
        row = _row(service, session)
        assert row.entry_order_id == entry.id and row.entry_client_order_id == entry.client_order_id


def test_o_t10_retained_41_quantity_matches_have_unmeasured_parent_ownership():
    manifest = json.loads((FIXTURES / "historical_parent_audit.json").read_text())
    assert manifest["retained_poll_count"] == 42
    assert manifest["quantity_matches"] == 41
    assert manifest["verified_correct_historical_parents"] == 0
    assert manifest["historical_equal_quantity_ownership_unmeasured"] == 41
    assert all(item["parent_ownership"] == "UNMEASURED" for item in manifest["comparisons"])


def test_additive_migration_leaves_existing_rows_unbound_and_downgrades():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[2] / "sql/migrations/versions/20261005_0022_managed_entry_binding.py"
    spec = importlib.util.spec_from_file_location("ownmix1_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "20260916_0021"
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE oms_managed_positions (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO oms_managed_positions (id) VALUES (1)"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert connection.execute(text(
            "SELECT entry_order_id, entry_client_order_id FROM oms_managed_positions"
        )).one() == (None, None)
        columns = {item["name"]: item for item in inspect(connection).get_columns("oms_managed_positions")}
        assert columns["entry_order_id"]["nullable"] and columns["entry_client_order_id"]["nullable"]
        migration.downgrade()
        assert [item["name"] for item in inspect(connection).get_columns("oms_managed_positions")] == ["id"]


@pytest.mark.asyncio
async def test_owned_parent_with_two_working_legs_stands_down(replay):
    service, _, bodies, calls = replay
    for child in bodies["1008165370999"]["childOrderStrategies"][0]["childOrderStrategies"]:
        child["status"] = "WORKING"
    service._managed_v2_symbols = {(ACCT, "MI")}
    await service._refresh_native_oco_armed_state([ACCT])
    assert service._native_oco_stand_down_active(ACCT, "MI")
    assert calls and all("1008165370999" in path for _, path in calls)


@pytest.mark.asyncio
async def test_unbound_legacy_row_cannot_retain_previous_native_standdown(replay):
    service, sessions, bodies, _ = replay
    for child in bodies["1008165370999"]["childOrderStrategies"][0]["childOrderStrategies"]:
        child["status"] = "WORKING"
    service._managed_v2_symbols = {(ACCT, "MI")}
    await service._refresh_native_oco_armed_state([ACCT])
    assert service._native_oco_stand_down_active(ACCT, "MI")
    with sessions() as session:
        _row(service, session).entry_order_id = None
        session.commit()
    await service._refresh_native_oco_armed_state([ACCT])
    assert not service._native_oco_stand_down_active(ACCT, "MI")


@pytest.mark.asyncio
async def test_poll_refuses_row_replaced_during_exact_parent_fetch(replay):
    service, sessions, _, _ = replay
    with sessions() as session:
        # SQLite cannot render this PostgreSQL open-row-only index predicate.
        session.execute(text("DROP INDEX uq_oms_managed_positions_open_symbol"))
        session.commit()
    service._v2_accounts = lambda: [ACCT]
    original = service.broker_adapter._authorized_request_json
    replacement = []

    async def replace_during_fetch(method, path):
        response = await original(method, path)
        with sessions() as session:
            row, entry = _row(service, session), _entry(session)
            service.store.close_managed_position(session, row)
            session.flush()
            new = service.store.create_managed_position(
                session, strategy_code="schwab_1m_v2", broker_account_name=ACCT,
                symbol="MI", entry_price=Decimal("3.31"), quantity=180,
                entry_time=datetime(2026, 10, 5, 13, 40, tzinfo=UTC),
                entry_order_id=entry.id, entry_client_order_id=entry.client_order_id,
            )
            replacement.append(new.id)
            session.commit()
        return response

    service.broker_adapter._authorized_request_json = replace_during_fetch
    await service._poll_native_oco_exits()
    with sessions() as session:
        assert _row(service, session).id == replacement[0]
        assert _row(service, session).current_quantity == 180
        assert session.scalars(select(Fill)).all() == []
    assert not service._native_oco_stand_down_active(ACCT, "MI")


@pytest.mark.parametrize("quantity", ["broken", "NaN", "Infinity", "0", "-180"])
@pytest.mark.asyncio
async def test_invalid_exit_quantity_fails_closed_without_holding_ladder(replay, quantity):
    service, sessions, _, _ = replay
    assert not await service._close_resolved_oco_managed_row(
        ACCT, "MI", detail={**_detail(), "quantity": quantity})
    assert not service._native_oco_stand_down_active(ACCT, "MI")
    assert not service.__dict__.get("_oco_exit_fill_pending")
    with sessions() as session:
        assert _row(service, session).current_quantity == 180
