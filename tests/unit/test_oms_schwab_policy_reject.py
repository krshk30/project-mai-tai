"""A Schwab policy reject learned by polling must block later opens (#992)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import (
    Base,
    BrokerAccount,
    BrokerOrder,
    SchwabIneligibleToday,
    SystemIncident,
    TradeIntent,
)
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.settings import Settings


PMAX_COID = "schwab_1m_v2-PMAX-open-86e63b23ebc4"
POLICY_REASON = "Opening transactions for this security must be placed with a broker. Contact us"
PMAX_METADATA = {
    "path": "ATR Flip",
    "atr_variant": "CW-v2-resting",
    "order_type": "STOP_LIMIT",
    "fanout_slot": "resting",
    "fanout_attempt_id": PMAX_COID,
    "source": "schwab_1m_v2",
    "broker_order_id": "1008051314821",
}


class _Redis:
    async def xadd(self, *_args, **_kwargs):
        return "1-0"


class _Broker:
    def __init__(self) -> None:
        self.submit_calls = 0

    async def fetch_order_update(self, request):
        assert request.client_order_id == PMAX_COID
        return ExecutionReport(
            event_type="rejected",
            client_order_id=PMAX_COID,
            broker_order_id="1008051314821",
            symbol="PMAX",
            side="buy",
            intent_type="open",
            quantity=Decimal("2"),
            reason=POLICY_REASON,
            metadata=PMAX_METADATA,
            reported_at=datetime.now(UTC),
        )

    async def submit_order(self, _request):
        self.submit_calls += 1
        raise AssertionError("cached policy reject must not reach the broker")


@pytest.mark.asyncio
async def test_polled_pmax_policy_reject_is_logged_cached_paged_and_blocks_retry(capsys) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    broker = _Broker()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            strategy_schwab_1m_v2_broker_provider="schwab",
            strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        ),
        redis_client=_Redis(),
        session_factory=sessions,
        broker_adapter=broker,
    )
    service._market_is_fillable = lambda now=None: True

    with sessions() as session:
        store = OmsStore()
        strategy = store.ensure_strategy(
            session, "schwab_1m_v2", name="Schwab 1m v2", execution_mode="live", metadata_json={}
        )
        account = store.ensure_broker_account(
            session, "live:schwab_1m_v2", provider="schwab", environment="development"
        )
        intent = TradeIntent(
            strategy_id=strategy.id,
            broker_account_id=account.id,
            symbol="PMAX",
            side="buy",
            intent_type="open",
            quantity=Decimal("2"),
            reason="CW_V2_RESTING",
            status="submitted",
            payload={"metadata": PMAX_METADATA},
        )
        session.add(intent)
        session.flush()
        stale = datetime.now(UTC) - timedelta(seconds=1)
        session.add(
            BrokerOrder(
                intent_id=intent.id,
                strategy_id=strategy.id,
                broker_account_id=account.id,
                client_order_id=PMAX_COID,
                broker_order_id="1008051314821",
                symbol="PMAX",
                side="buy",
                order_type="stop_limit",
                time_in_force="day",
                quantity=Decimal("2"),
                status="accepted",
                payload=PMAX_METADATA,
                submitted_at=stale,
                updated_at=stale,
            )
        )
        session.commit()

    await service.sync_broker_orders(account_names=["live:schwab_1m_v2"])

    with sessions() as session:
        cache_after_sync = session.scalars(select(SchwabIneligibleToday)).all()
        account_after_sync = session.scalar(select(BrokerAccount).where(BrokerAccount.name == "live:schwab_1m_v2"))
    assert len(cache_after_sync) == 1
    assert cache_after_sync[0].symbol == "PMAX"
    assert account_after_sync is not None
    assert account_after_sync.provider == "schwab"
    assert cache_after_sync[0].broker_account_id == account_after_sync.id
    assert cache_after_sync[0].session_date == service._current_session_day()

    retry = await service.process_trade_intent(
        TradeIntentEvent(
            source_service="schwab-1m-v2",
            payload=TradeIntentPayload(
                strategy_code="schwab_1m_v2",
                broker_account_name="live:schwab_1m_v2",
                symbol="PMAX",
                side="buy",
                quantity=Decimal("2"),
                intent_type="open",
                reason="CW_V2_RESTING",
                metadata=PMAX_METADATA,
            ),
        )
    )

    assert retry[0].payload.reason == "schwab_ineligible_cached"
    assert broker.submit_calls == 0
    output = capsys.readouterr().out
    assert output.count(f"[OMS-BROKER-REJECT] sym=PMAX acct=live:schwab_1m_v2 coid={PMAX_COID}") == 1
    assert "[OMS-INTENT-DROPPED] live:schwab_1m_v2 PMAX reason=schwab_ineligible_cached" in output
    with sessions() as session:
        cache = session.scalars(select(SchwabIneligibleToday)).all()
        incidents = session.scalars(select(SystemIncident)).all()
    assert len(cache) == 1 and cache[0].symbol == "PMAX"
    assert len(incidents) == 1
    assert incidents[0].payload["symbol"] == "PMAX"

    with sessions() as session:
        repeat = ExecutionReport(
            event_type="rejected",
            client_order_id="schwab_1m_v2-PMAX-open-575a0567382f",
            symbol="PMAX",
            side="buy",
            intent_type="open",
            quantity=Decimal("2"),
            reason=POLICY_REASON,
            reported_at=cache_after_sync[0].first_seen_at,
        )
        service._record_broker_rejection(
            session,
            report=repeat,
            broker_account_id=account_after_sync.id,
            broker_account_name="live:schwab_1m_v2",
            symbol="PMAX",
            client_order_id="schwab_1m_v2-PMAX-open-575a0567382f",
            intent_type="open",
        )
        unrelated = ExecutionReport(
            event_type="rejected",
            client_order_id="schwab_1m_v2-WETO-open-f546cb82efbf",
            symbol="WETO",
            side="buy",
            intent_type="open",
            quantity=Decimal("1"),
            reason="ORDER_RISK_RULE_PRICE_AGGRESSIVE",
            reported_at=datetime.now(UTC),
        )
        service._record_broker_rejection(
            session,
            report=unrelated,
            broker_account_id=account_after_sync.id,
            broker_account_name="live:schwab_1m_v2",
            symbol="WETO",
            client_order_id=unrelated.client_order_id,
            intent_type="open",
        )
        session.commit()
    with sessions() as session:
        assert len(session.scalars(select(SchwabIneligibleToday)).all()) == 1
        assert len(session.scalars(select(SystemIncident)).all()) == 1
    assert "[OMS-BROKER-REJECT] sym=WETO" in capsys.readouterr().out
