from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from project_mai_tai.db.models import Base, BrokerAccount, DashboardSnapshot, Strategy, VirtualPosition
from project_mai_tai.oms.bot_control import ControlConflict
from project_mai_tai.oms.bot_control_evidence import owned_exit_ceiling, positions_after_protection
from project_mai_tai.positions_read_receipt import SNAPSHOT_TYPE


@pytest.fixture
def evidence():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.mark.parametrize("age,release_age,allowed", [
    (0, 1, True), (30, 31, True), (31, 32, False),
    (1, 1, False), (2, 1, False), (-1, 1, False),
])
def test_real_wire_receipt_must_follow_release_and_be_at_most_thirty_seconds_old(evidence, age, release_age, allowed):
    now = datetime(2026, 10, 15, 14, tzinfo=UTC)
    terminal = now - timedelta(seconds=release_age)
    read_at = now - timedelta(seconds=age)
    evidence.add(DashboardSnapshot(snapshot_type=SNAPSHOT_TYPE, created_at=now, payload={
        "broker_account_name": "live:orb", "writer": "oms.sync_broker_positions",
        "read_at": read_at.isoformat(), "position_count": 1, "recorded_at": now.isoformat(),
    }))
    evidence.flush()
    if allowed:
        assert positions_after_protection(evidence, account_name="live:orb", terminal_at=terminal, now=now) == read_at
    else:
        with pytest.raises(ControlConflict):
            positions_after_protection(evidence, account_name="live:orb", terminal_at=terminal, now=now)


@pytest.mark.parametrize("change", ["missing", "naive", "future", "other_writer", "other_account", "bool_count"])
def test_unknown_receipts_never_promote_cache_or_broker_trade_date_to_freshness(evidence, change):
    now = datetime(2026, 10, 15, 14, tzinfo=UTC)
    payload = {"broker_account_name": "live:orb", "writer": "oms.sync_broker_positions",
               "read_at": now.isoformat(), "position_count": 1}
    if change == "naive":
        payload["read_at"] = now.replace(tzinfo=None).isoformat()
    elif change == "future":
        payload["read_at"] = (now + timedelta(seconds=1)).isoformat()
    elif change == "other_writer":
        payload["writer"] = "cache_return_time"
    elif change == "other_account":
        payload["broker_account_name"] = "live:other"
    elif change == "bool_count":
        payload["position_count"] = False
    if change != "missing":
        evidence.add(DashboardSnapshot(snapshot_type=SNAPSHOT_TYPE, created_at=now, payload=payload))
        evidence.flush()
    with pytest.raises(ControlConflict):
        positions_after_protection(evidence, account_name="live:orb", terminal_at=now - timedelta(seconds=1), now=now)


@pytest.mark.parametrize("own,broker,expected", [(33, 133, 33), (20, 133, 20), (33, 10, 10), (0, 133, 0)])
def test_quantity_ceiling_never_borrows_other_strategy_inventory(evidence, own, broker, expected):
    strategy, foreign, account = uuid4(), uuid4(), uuid4()
    evidence.add_all([
        Strategy(id=strategy, code="momentum_warn_live", name="MOM", execution_mode="live"),
        Strategy(id=foreign, code="manual", name="manual", execution_mode="live"),
        BrokerAccount(id=account, name="live:orb", environment="live", provider="webull"),
    ])
    evidence.flush()
    evidence.add_all([
        VirtualPosition(strategy_id=strategy, broker_account_id=account, symbol="SCNI", quantity=Decimal(own)),
        VirtualPosition(strategy_id=foreign, broker_account_id=account, symbol="SCNI", quantity=Decimal(100)),
    ])
    evidence.flush()
    assert owned_exit_ceiling(evidence, strategy_id=strategy, account_id=account, symbol="SCNI",
        remaining=Decimal(33), broker_total=Decimal(broker)) == Decimal(expected)
