"""J's disjoint C5 account-scope controls; I owns service implementation."""

from tests.unit.oms_sqlite_fixtures import new_unit_service
from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from tests.unit.oms_sqlite_fixtures import private_session_factory as build_test_session_factory
from tests.unit.oms_sqlite_fixtures import own_service
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, Strategy, VirtualPosition
from project_mai_tai.oms import service as module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

from tests.unit.test_oms_risk_service import FakeRedis

A, B = UUID(int=1), UUID(int=2)
NAMES = {A: "live:scope-a", B: "live:scope-b"}


def subject(monkeypatch):
    result = new_unit_service()
    result.logger = Mock()
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    return result, clock


def observe(result, rows, scope, names=NAMES):
    result._log_momentum_owned_backing_mismatch(rows, names, observed_account_ids=scope)


@pytest.mark.asyncio
async def test_actual_alternating_account_sync_does_not_rearm_other_account(monkeypatch):
    factory = build_test_session_factory()
    with factory.begin() as session:
        strategy = Strategy(code="momentum_warn_live", name="Controlled MOM", execution_mode="live")
        session.add(strategy)
        for identity, name in NAMES.items():
            session.add(BrokerAccount(id=identity, name=name, provider="webull", environment="production"))
        session.flush()
        session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=A, symbol="BBCI",
            quantity=D(25), average_price=D("3.68"), realized_pnl=0,
            updated_at=datetime.now(UTC)-timedelta(minutes=5)))
    service = own_service(OmsRiskService(Settings(_env_file=None, oms_adapter="simulated",
        oms_virtual_position_clear_min_age_seconds=0), FakeRedis(), session_factory=factory))
    reads = []

    async def positions(account):
        reads.append(account)
        return []

    service.broker_adapter = SimpleNamespace(list_account_positions=positions)
    service.logger = Mock()
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    await service.sync_broker_positions(account_names=[NAMES[A]])
    for second in (101, 110, 130, 159.999):
        clock[0] = second
        await service.sync_broker_positions(account_names=[NAMES[B]])
        await service.sync_broker_positions(account_names=[NAMES[A]])
    with factory() as session:
        position = session.scalar(select(VirtualPosition))
        assert (position.quantity, position.average_price, position.realized_pnl) == (25, D("3.68"), 0)
    assert reads == [NAMES[A]] + [name for _ in range(4) for name in (NAMES[B], NAMES[A])]
    assert service.logger.critical.call_count == 1, "clean B must not rearm unchanged A"
    clock[0] = 160.0
    await service.sync_broker_positions(account_names=[NAMES[A]])
    assert service.logger.critical.call_count == 2


def test_grouping_and_unobserved_accounts_do_not_rearm_mismatch(monkeypatch):
    service, clock = subject(monkeypatch)
    rows = [(A, "BBCI", D(25))]
    observe(service, rows, {A})
    observe(service, [], {B})
    observe(service, [], set())  # No successful reads is UNKNOWN, not a clean pass.
    observe(service, rows, {A, B})
    observe(service, rows, {A})
    assert service.logger.critical.call_count == 1
    clock[0] = 160
    observe(service, rows, {A})
    assert service.logger.critical.call_count == 2


def test_explicit_same_account_clear_rearms_only_that_account(monkeypatch):
    service, _ = subject(monkeypatch)
    rows = [(A, "BBCI", D(25))]
    observe(service, rows, {A})
    observe(service, [], {B})
    observe(service, rows, {A})
    assert service.logger.critical.call_count == 1
    observe(service, [], {A})
    observe(service, rows, {A})
    assert service.logger.critical.call_count == 2


def test_reorder_decimal_scale_and_group_scope_are_not_changes(monkeypatch):
    service, _ = subject(monkeypatch)
    observe(service, [(A, "BBCI", D("25.00")), (A, "MLEC", D(35))], {A})
    observe(service, [(A, "MLEC", D("35.0")), (A, "BBCI", D(25))], {A, B})
    assert service.logger.critical.call_count == 1


def test_per_account_receipt_state_is_bounded_without_symbol_histories(monkeypatch):
    service, clock = subject(monkeypatch)
    for index in range(1, 150):
        account = UUID(int=index)
        clock[0] += 1
        observe(service, [(account, "BBCI", D(25))], {account}, {account: f"live:scope-{index}"})
    state = service._momentum_backing_mismatch_log
    maps = [value for value in state.values() if isinstance(value, dict)]
    assert len(maps) == 1, "one bounded per-account receipt map required"
    assert len(maps[0]) == 128
    assert UUID(int=1) not in maps[0] and str(UUID(int=1)) not in maps[0]
    assert len(repr(state)) < 60_000, "receipt state must not accumulate symbol histories"
