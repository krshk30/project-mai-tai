"""Confirmed-window (variant CW) OMS managed exit — PRs #2/#3, end-to-end.

Drives `_evaluate_v2_managed_exit` with the CW flag on through the REAL emit path
(SimulatedBrokerAdapter) on the SQLite schema, mirroring test_v2_managed_exit.py. Proves
the CW exit REPLACES the scale/floor ladder: full close at +2% (CW_TARGET) or -5%
(CW_HARD_STOP) or a bar-close flip (CW_FLIP, armed via the `v2_cw_flip` dispatcher event),
and NO exit between the two bounds when no flip is pending. Also proves the dispatcher
arms the in-memory pending set only when CW is enabled.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import OmsManagedPosition, TradeIntent
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

ACCT = "paper:schwab_1m_v2"
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


def _svc(sf, *, cw: bool = True) -> OmsRiskService:
    settings = Settings(
        oms_v2_exit_management_enabled=True,
        oms_v2_exit_close_on_fill_enabled=True,
        strategy_schwab_1m_v2_confirmed_window_enabled=cw,
    )
    svc = OmsRiskService(
        settings, redis_client=_FakeRedis(), session_factory=sf,
        broker_adapter=SimulatedBrokerAdapter(),
    )
    with sf() as s:
        svc.store.ensure_strategy(s, "schwab_1m_v2", name="v2")
        svc.store.ensure_broker_account(s, ACCT, provider="simulated", environment="test")
        s.commit()
    return svc


def _arm(svc, sf, *, entry=10.0, qty=100) -> None:
    with sf() as s:
        svc.store.create_managed_position(
            s, strategy_code="schwab_1m_v2", broker_account_name=ACCT,
            symbol=SYM, entry_price=Decimal(str(entry)), quantity=qty, entry_path="ATR Flip",
        )
        s.commit()
    svc._managed_v2_symbols.add(SYM)


def _quote(svc, bid: float) -> None:
    from datetime import UTC, datetime
    svc._latest_quotes_by_symbol[SYM] = {
        "bid": bid, "ask": bid + 0.01, "received_at": datetime.now(UTC),
    }


def _row(sf) -> OmsManagedPosition | None:
    with sf() as s:
        return s.scalar(select(OmsManagedPosition).where(OmsManagedPosition.symbol == SYM))


def _sell_intents(sf) -> list[TradeIntent]:
    with sf() as s:
        return list(s.scalars(select(TradeIntent).where(
            TradeIntent.symbol == SYM, TradeIntent.side == "sell")).all())


def _ref(i: TradeIntent) -> Decimal:
    return Decimal(i.payload["metadata"]["reference_price"])


@pytest.mark.asyncio
async def test_cw_target_full_close_at_plus_2pct():
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=10.25)                       # >= +2% target (10.20)
    await svc._evaluate_v2_managed_exit(SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1
    i = intents[0]
    assert i.intent_type == "close" and Decimal(str(i.quantity)) == Decimal("100")
    assert i.reason.endswith("CW_TARGET")
    assert _ref(i) == Decimal("10.2000")          # target LEVEL, not the 10.25 bid
    assert _row(sf).current_quantity == 0 or _row(sf).status == "closed"


@pytest.mark.asyncio
async def test_cw_hard_stop_full_close_at_minus_5pct():
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.40)                          # <= -5% stop (9.50)
    await svc._evaluate_v2_managed_exit(SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CW_HARD_STOP")
    assert _ref(intents[0]) == Decimal("9.5000")   # stop LEVEL


@pytest.mark.asyncio
async def test_cw_no_exit_between_bounds_without_flip():
    # -5% < bid < +2% and no flip pending -> the CW exit does NOTHING (proves the
    # scale/floor ladder is NOT running under CW).
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.90)                          # -1%
    await svc._evaluate_v2_managed_exit(SYM)
    assert _sell_intents(sf) == []
    assert _row(sf).status == "open"


@pytest.mark.asyncio
async def test_cw_flip_full_close_at_bid():
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    _arm(svc, sf, entry=10.0, qty=100)
    # Arm the flip via the dispatcher event, then a quote inside the bounds closes it.
    await svc._handle_stream_message(
        {"data": json.dumps({"event_type": "v2_cw_flip", "symbol": SYM,
                             "broker_account_name": ACCT})}
    )
    assert (ACCT, SYM) in svc._cw_flip_pending
    _quote(svc, bid=9.90)                          # inside bounds, but flip pending
    await svc._evaluate_v2_managed_exit(SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CW_FLIP")
    assert _ref(intents[0]) == Decimal("9.9000")   # fills at the bid
    assert (ACCT, SYM) not in svc._cw_flip_pending  # consumed


@pytest.mark.asyncio
async def test_cw_target_takes_precedence_over_pending_flip():
    sf = _make_sf()
    svc = _svc(sf, cw=True)
    _arm(svc, sf, entry=10.0, qty=100)
    svc._cw_flip_pending.add((ACCT, SYM))
    _quote(svc, bid=10.25)                          # +2% AND flip pending -> target wins
    await svc._evaluate_v2_managed_exit(SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CW_TARGET")
    assert (ACCT, SYM) not in svc._cw_flip_pending


@pytest.mark.asyncio
async def test_dispatcher_arms_pending_only_when_cw_enabled():
    sf = _make_sf()
    on = _svc(sf, cw=True)
    await on._handle_stream_message(
        {"data": json.dumps({"event_type": "v2_cw_flip", "symbol": SYM,
                             "broker_account_name": ACCT})}
    )
    assert (ACCT, SYM) in on._cw_flip_pending

    off = _svc(_make_sf(), cw=False)
    await off._handle_stream_message(
        {"data": json.dumps({"event_type": "v2_cw_flip", "symbol": SYM,
                             "broker_account_name": ACCT})}
    )
    assert (ACCT, SYM) not in off._cw_flip_pending
