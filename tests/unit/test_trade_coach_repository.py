from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.ai_trade_coach.models import TradeCoachConfig
from project_mai_tai.ai_trade_coach.repository import TradeCoachRepository
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import AiTradeReview
from project_mai_tai.db.models import BrokerAccount
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.db.models import Fill
from project_mai_tai.db.models import Strategy
from project_mai_tai.strategy_core.time_utils import EASTERN_TZ
from project_mai_tai.trade_episodes import cycle_key
def _session_factory() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
def _seed_cycle(
    *,
    session,
    strategy: Strategy,
    broker_account: BrokerAccount,
    symbol: str,
    entry_time: datetime,
    exit_time: datetime,
    entry_price: str,
    exit_price: str,
    prefix: str,
) -> str:
    entry_order = BrokerOrder(
        strategy_id=strategy.id,
        broker_account_id=broker_account.id,
        client_order_id=f"{prefix}-entry",
        broker_order_id=f"{prefix}-entry",
        symbol=symbol,
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=Decimal("100"),
        status="filled",
        payload={},
        submitted_at=entry_time,
        updated_at=entry_time,
    )
    exit_order = BrokerOrder(
        strategy_id=strategy.id,
        broker_account_id=broker_account.id,
        client_order_id=f"{prefix}-exit",
        broker_order_id=f"{prefix}-exit",
        symbol=symbol,
        side="sell",
        order_type="market",
        time_in_force="day",
        quantity=Decimal("100"),
        status="filled",
        payload={},
        submitted_at=exit_time,
        updated_at=exit_time,
    )
    session.add_all([entry_order, exit_order])
    session.flush()

    session.add_all(
        [
            Fill(
                order_id=entry_order.id,
                strategy_id=strategy.id,
                broker_account_id=broker_account.id,
                broker_fill_id=f"{prefix}-fill-entry",
                symbol=symbol,
                side="buy",
                quantity=Decimal("100"),
                price=Decimal(entry_price),
                filled_at=entry_time,
                payload={},
            ),
            Fill(
                order_id=exit_order.id,
                strategy_id=strategy.id,
                broker_account_id=broker_account.id,
                broker_fill_id=f"{prefix}-fill-exit",
                symbol=symbol,
                side="sell",
                quantity=Decimal("100"),
                price=Decimal(exit_price),
                filled_at=exit_time,
                payload={},
            ),
        ]
    )
    return cycle_key(
        strategy_code=strategy.code,
        broker_account_name=broker_account.name,
        symbol=symbol,
        entry_time=TradeCoachRepository._datetime_str(entry_time),
        exit_time=TradeCoachRepository._datetime_str(exit_time),
    )


def test_list_reviewable_cycles_sorts_globally_and_skips_reviewed() -> None:
    session_factory = _session_factory()
    config = TradeCoachConfig(max_similar_trades=3)
    repository = TradeCoachRepository(session_factory=session_factory, config=config)

    session_start = datetime(2026, 4, 24, 4, 0, tzinfo=EASTERN_TZ)
    session_end = datetime(2026, 4, 25, 4, 0, tzinfo=EASTERN_TZ)

    with session_factory() as session:
        macd = Strategy(code="macd_30s", name="Schwab 30 Sec Bot", execution_mode="paper")
        webull = Strategy(code="webull_30s", name="Webull 30 Sec Bot", execution_mode="live")
        paper = BrokerAccount(
            name="paper:macd_30s",
            provider="alpaca",
            environment="paper",
            external_account_id="paper-macd-30s",
        )
        live = BrokerAccount(
            name="live:webull_30s",
            provider="webull",
            environment="live",
            external_account_id="live-webull-30s",
        )
        session.add_all([macd, webull, paper, live])
        session.flush()

        reviewed_cycle_key = _seed_cycle(
            session=session,
            strategy=macd,
            broker_account=paper,
            symbol="OLD1",
            entry_time=datetime(2026, 4, 24, 9, 30, tzinfo=EASTERN_TZ),
            exit_time=datetime(2026, 4, 24, 9, 35, tzinfo=EASTERN_TZ),
            entry_price="1.00",
            exit_price="1.10",
            prefix="old1",
        )
        _seed_cycle(
            session=session,
            strategy=webull,
            broker_account=live,
            symbol="MID2",
            entry_time=datetime(2026, 4, 24, 9, 40, tzinfo=EASTERN_TZ),
            exit_time=datetime(2026, 4, 24, 9, 46, tzinfo=EASTERN_TZ),
            entry_price="2.00",
            exit_price="2.30",
            prefix="mid2",
        )
        _seed_cycle(
            session=session,
            strategy=macd,
            broker_account=paper,
            symbol="NEW3",
            entry_time=datetime(2026, 4, 24, 9, 50, tzinfo=EASTERN_TZ),
            exit_time=datetime(2026, 4, 24, 9, 57, tzinfo=EASTERN_TZ),
            entry_price="3.00",
            exit_price="3.25",
            prefix="new3",
        )
        session.add(
            AiTradeReview(
                strategy_code="macd_30s",
                broker_account_name="paper:macd_30s",
                symbol="OLD1",
                review_type=config.review_type,
                cycle_key=reviewed_cycle_key,
                provider="openai",
                model="gpt-4.1-mini",
                verdict="good",
                action="enter",
                confidence=Decimal("0.9"),
                summary="Already reviewed.",
                payload={"concise_summary": "Already reviewed."},
            )
        )
        session.commit()

    cycles = repository.list_reviewable_cycles(
        strategy_accounts=[
            ("macd_30s", "paper:macd_30s"),
            ("webull_30s", "live:webull_30s"),
        ],
        session_start=session_start,
        session_end=session_end,
        review_limit=10,
    )

    assert [cycle.symbol for cycle in cycles] == ["NEW3", "MID2"]
    assert all(cycle.symbol != "OLD1" for cycle in cycles)
