"""v2 sim-fill fix: emitter sets reference_price + OMS persists reject reasons.

The headline test drives a REAL signal through SchwabV2Strategy and feeds the
strategy's OWN emitted metadata (verbatim — nothing hand-injected) through the
SimulatedBrokerAdapter, asserting it fills. This is the test P1 lacked: it
hand-set reference_price, so it couldn't catch that v2's emitter never produced
it. If the emitter drops/mis-formats reference_price, this test rejects + fails.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy


def _drive_real_v2_signal() -> tuple[object, ChartBar]:
    """Engineer a deterministic real signal: 135 flat bars (macd==signal==0, no
    cross) then a fresh green volume-spike bar that crosses MACD above signal and
    passes every gate. Returns the strategy's REAL TradeIntentDraft."""
    strat = SchwabV2Strategy(Settings())
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    n_flat = 135
    for i in range(n_flat):
        ts = now_ms - (n_flat - i + 1) * 60_000  # ascending, all older than final
        assert strat.on_bar("TEST", ChartBar("TEST", 10.0, 10.0, 10.0, 10.0, 1000, ts)) is None
    final = ChartBar("TEST", 10.0, 11.0, 10.0, 11.0, 100_000, now_ms)  # fresh green spike
    draft = strat.on_bar("TEST", final)
    return draft, final


@pytest.mark.asyncio
async def test_real_v2_emit_metadata_fills_on_simulated_adapter() -> None:
    draft, final = _drive_real_v2_signal()
    assert draft is not None, "engineered bars must fire a real v2 signal"

    # reference_price present AND parses as a positive Decimal — presence alone
    # is not enough (a formatting regression could pass presence yet reject).
    assert "reference_price" in draft.metadata
    ref = Decimal(draft.metadata["reference_price"])
    assert ref > 0
    assert ref == Decimal(str(final.close))  # signal bar close, by design

    # Build the OrderRequest exactly as the emitter + OMS do: metadata VERBATIM.
    payload = TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name="paper:schwab_1m_v2",
        symbol=draft.symbol, side=draft.side, quantity=draft.quantity,
        intent_type=draft.intent_type, reason=draft.reason, metadata=dict(draft.metadata),
    )
    request = OrderRequest(
        client_order_id="schwab_1m_v2-TEST-open-1",
        broker_account_name=payload.broker_account_name, strategy_code=payload.strategy_code,
        symbol=payload.symbol, side=payload.side, intent_type=payload.intent_type,
        quantity=payload.quantity, reason=payload.reason, metadata=dict(payload.metadata),
    )
    adapter = SimulatedBrokerAdapter()
    reports = await adapter.submit_order(request)
    event_types = {r.event_type for r in reports}
    assert "filled" in event_types
    assert "rejected" not in event_types  # the regression this guards
    filled = next(r for r in reports if r.event_type == "filled")
    assert filled.fill_price == ref  # fills at the signal bar close
    positions = await adapter.list_account_positions(payload.broker_account_name)
    assert len(positions) == 1 and positions[0].symbol == "TEST"


# --------------------------- reject_reason persistence ----------------------

def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", future=True,
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_get_or_create_order_persists_reject_reason_on_open_path() -> None:
    store = OmsStore()
    with _session_factory()() as session:
        strategy = store.ensure_strategy(session, "schwab_1m_v2", name="v2")
        account = store.ensure_broker_account(
            session, "paper:schwab_1m_v2", provider="simulated", environment="test"
        )
        intent = store.create_trade_intent(
            session, strategy=strategy, broker_account=account,
            event=TradeIntentEvent(source_service="t", payload=TradeIntentPayload(
                strategy_code="schwab_1m_v2", broker_account_name="paper:schwab_1m_v2",
                symbol="TEST", side="buy", quantity=Decimal("100"),
                intent_type="open", reason="VWAP Breakout", metadata={})),
        )
        order = store.get_or_create_order(
            session, intent=intent, strategy_id=strategy.id, broker_account_id=account.id,
            client_order_id="o1", symbol="TEST", side="buy", quantity=Decimal("100"),
            metadata={"path": "VWAP Breakout"}, status="rejected",
            reject_reason="missing reference_price",
        )
        assert order.payload.get("reject_reason") == "missing reference_price"


def test_update_order_from_report_persists_reject_reason_only_on_reject() -> None:
    store = OmsStore()
    rejected = BrokerOrder(payload={}, status="pending")
    store.update_order_from_report(
        rejected,
        report=ExecutionReport(event_type="rejected", client_order_id="o1",
                               reason="missing reference_price"),
        metadata={"path": "X"},
    )
    assert rejected.payload["reject_reason"] == "missing reference_price"

    filled = BrokerOrder(payload={}, status="pending")
    store.update_order_from_report(
        filled,
        report=ExecutionReport(event_type="filled", client_order_id="o2", reason=""),
        metadata={"path": "X"},
    )
    assert "reject_reason" not in filled.payload
