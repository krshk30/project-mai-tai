"""Track-2 Phase-2 Slice-3 — PAPER-ISOLATION SURVIVAL RE-PROOF (the deploy gate).

Proves a v2 OMS-emitted exit SELL can NEVER reach a real Schwab order, even with a
live Schwab adapter + a real account hash in the same process. Three layers:
  1. config — provider_for_account → simulated; configured_schwab_accounts refuses v2;
  2. routing by construction — the emit's order account is ALWAYS the v2 account →
     RoutingBrokerAdapter sends it to the SimulatedBrokerAdapter;
  3. survival / fault-injection — a full open→quote→exit cycle (hard stop AND scale)
     with the Schwab adapter wired LIVE into the router asserts SchwabBrokerAdapter
     .submit_order is NEVER called for the v2 sell, while a real-schwab-account order
     DOES hit it (so the slot is proven reachable, not dead).
Slice 3 does not deploy until this is green.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter, configured_schwab_accounts
from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, OmsManagedPosition, TradeIntent
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

V2_ACCT = "paper:schwab_1m_v2"
SCHWAB_ACCT = "paper:schwab_1m"
SYM = "VSME"


class _FakeRedis:
    async def xadd(self, *a, **kw):
        return b"1-1"


def _make_sf() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", future=True,
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    tables = [t for t in Base.metadata.sorted_tables
              if t.name not in ("market_trade_ticks", "market_quote_ticks")]
    Base.metadata.create_all(engine, tables=tables)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _routing_adapter(settings) -> RoutingBrokerAdapter:
    """Router with v2→simulated AND a real schwab slot (SchwabBrokerAdapter, lazily
    built). The schwab __init__/submit_order are patched in the test so it's cheap +
    spied. This is the production routing shape with both providers in one process."""
    return RoutingBrokerAdapter(
        default_provider="simulated",
        provider_by_account={V2_ACCT: "simulated", SCHWAB_ACCT: "schwab"},
        factories_by_provider={
            "simulated": lambda: SimulatedBrokerAdapter(),
            "schwab": lambda: SchwabBrokerAdapter(settings),
        },
    )


def _svc(sf, settings, router) -> OmsRiskService:
    svc = OmsRiskService(settings, redis_client=_FakeRedis(), session_factory=sf, broker_adapter=router)
    with sf() as s:
        svc.store.ensure_strategy(s, "schwab_1m_v2", name="v2")
        svc.store.ensure_broker_account(s, V2_ACCT, provider="simulated", environment="test")
        s.commit()
    return svc


def _arm(svc, sf, *, entry=10.0, qty=100, **rowkw) -> None:
    with sf() as s:
        row = svc.store.create_managed_position(
            s, strategy_code="schwab_1m_v2", broker_account_name=V2_ACCT,
            symbol=SYM, entry_price=Decimal(str(entry)), quantity=qty, entry_path="MACD Cross",
        )
        for k, v in rowkw.items():
            setattr(row, k, v)
        s.flush()
        s.commit()
    svc._managed_v2_symbols.add((V2_ACCT, SYM))


def _quote(svc, bid: float) -> None:
    svc._latest_quotes_by_symbol[SYM] = {"bid": bid, "ask": bid + 0.01, "received_at": datetime.now(UTC)}


# --------------------------------------------------------------------------- (1)

def test_config_layer_refuses_v2_from_schwab() -> None:
    """Layer 1: even WITH a real account hash, v2 routes to simulated and is refused
    by the Schwab account map."""
    settings = Settings(oms_v2_exit_management_enabled=True, schwab_account_hash="REALHASH-2EE5A4")
    assert settings.provider_for_account(V2_ACCT) == "simulated"
    accounts = configured_schwab_accounts(settings)
    assert V2_ACCT not in accounts
    # a retired real bot IS still registered — proves the refusal is v2-scoped, not blanket
    assert "paper:macd_30s" in accounts


# --------------------------------------------------------------------------- (2)

@pytest.mark.asyncio
async def test_v2_hard_stop_exit_fills_simulated_schwab_never_called() -> None:
    sf = _make_sf()
    settings = Settings(oms_v2_exit_management_enabled=True, schwab_account_hash="REALHASH-2EE5A4")
    with patch.object(SchwabBrokerAdapter, "__init__", return_value=None), \
         patch.object(SchwabBrokerAdapter, "submit_order", new_callable=AsyncMock) as schwab_submit:
        schwab_submit.return_value = []
        router = _routing_adapter(settings)
        svc = _svc(sf, settings, router)
        _arm(svc, sf, entry=10.0, qty=100)
        _quote(svc, bid=9.80)                       # hard stop
        await svc._evaluate_v2_managed_exit(V2_ACCT, SYM)

        # the v2 sell filled on SIMULATED, the row closed
        with sf() as s:
            sell = s.scalar(select(TradeIntent).where(TradeIntent.side == "sell"))
            acct = s.get(BrokerAccount, sell.broker_account_id)
        assert acct.name == V2_ACCT                 # the INVARIANT: emit used the v2 account
        assert _make_row_status(sf) == "closed"
        # THE GATE: Schwab adapter NEVER touched for the v2 exit
        schwab_submit.assert_not_called()

        # sanity / fault-injection: the schwab slot IS live + reachable — a real
        # schwab-account order DOES hit it — so v2-not-reaching-it is meaningful.
        await router.submit_order(OrderRequest(
            client_order_id="probe-1", broker_account_name=SCHWAB_ACCT, strategy_code="schwab_1m",
            symbol="T", side="sell", intent_type="close", quantity=Decimal("1"),
            reason="probe", metadata={"reference_price": "1.0"}, order_type="market", time_in_force="day",
        ))
        schwab_submit.assert_called_once()          # slot works; v2 simply never routes here


# --------------------------------------------------------------------------- (3)

@pytest.mark.asyncio
async def test_v2_scale_exit_also_routes_simulated_never_schwab() -> None:
    sf = _make_sf()
    settings = Settings(oms_v2_exit_management_enabled=True, schwab_account_hash="REALHASH-2EE5A4")
    with patch.object(SchwabBrokerAdapter, "__init__", return_value=None), \
         patch.object(SchwabBrokerAdapter, "submit_order", new_callable=AsyncMock) as schwab_submit:
        schwab_submit.return_value = []
        router = _routing_adapter(settings)
        svc = _svc(sf, settings, router)
        _arm(svc, sf, entry=10.0, qty=100)
        _quote(svc, bid=10.25)                      # +2.5% → scale
        await svc._evaluate_v2_managed_exit(V2_ACCT, SYM)

        with sf() as s:
            sell = s.scalar(select(TradeIntent).where(TradeIntent.side == "sell"))
            acct = s.get(BrokerAccount, sell.broker_account_id)
        assert sell.intent_type == "scale" and acct.name == V2_ACCT
        schwab_submit.assert_not_called()           # scale leg never reaches schwab either


def _make_row_status(sf) -> str:
    with sf() as s:
        return s.scalar(select(OmsManagedPosition).where(OmsManagedPosition.symbol == SYM)).status
