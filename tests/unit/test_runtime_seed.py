from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, Strategy
from project_mai_tai.runtime_seed import seed_runtime_metadata
from project_mai_tai.settings import Settings


def build_test_session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_runtime_seed_creates_expected_strategies_and_accounts() -> None:
    session_factory = build_test_session_factory()
    summary = seed_runtime_metadata(
        Settings(oms_adapter="alpaca_paper"),
        session_factory=session_factory,
    )

    assert summary.strategies == 4
    assert summary.broker_accounts == 3

    with session_factory() as session:
        strategies = session.scalars(select(Strategy).order_by(Strategy.code)).all()
        broker_accounts = session.scalars(select(BrokerAccount).order_by(BrokerAccount.name)).all()

        assert [strategy.code for strategy in strategies] == [
            "macd_1m",
            "macd_30s",
            "runner",
            "tos",
        ]
        assert all(strategy.execution_mode == "paper" for strategy in strategies)
        assert [account.name for account in broker_accounts] == [
            "paper:macd_1m",
            "paper:macd_30s",
            "paper:tos_runner_shared",
        ]


def test_runtime_seed_updates_existing_records_when_adapter_changes() -> None:
    session_factory = build_test_session_factory()
    seed_runtime_metadata(Settings(oms_adapter="simulated"), session_factory=session_factory)
    seed_runtime_metadata(Settings(oms_adapter="alpaca_paper"), session_factory=session_factory)

    with session_factory() as session:
        strategies = session.scalars(select(Strategy)).all()
        assert len(strategies) == 4
        assert all(strategy.execution_mode == "paper" for strategy in strategies)
