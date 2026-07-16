"""v2 overnight flatten — close every OMS-managed v2 position at 19:55 ET before the 20:00 gate
(v2 has no native stop). Drives `_v2_overnight_flatten` through the REAL emit path on SQLite,
mirroring test_v2_cw_managed_exit.py. Asserts on STATE (emitted close intent + closed row), never
on log narration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import OmsManagedPosition, TradeIntent
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

_ET = ZoneInfo("America/New_York")
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


def _svc(sf, *, flatten: bool = True) -> OmsRiskService:
    settings = Settings(
        oms_v2_exit_management_enabled=True,
        oms_v2_exit_close_on_fill_enabled=True,
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        oms_v2_overnight_flatten_enabled=flatten,
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
    svc._managed_v2_symbols.add((ACCT, SYM))


def _quote(svc, bid: float) -> None:
    svc._latest_quotes_by_symbol[SYM] = {
        "bid": bid, "ask": bid + 0.01, "received_at": datetime.now(timezone.utc),
    }


def _sell_intents(sf) -> list[TradeIntent]:
    with sf() as s:
        return list(s.scalars(select(TradeIntent).where(
            TradeIntent.symbol == SYM, TradeIntent.side == "sell")).all())


def _row(sf) -> OmsManagedPosition | None:
    with sf() as s:
        return s.scalar(select(OmsManagedPosition).where(OmsManagedPosition.symbol == SYM))


def _u(y, mo, d, h, mi):  # ET wall-clock -> tz-aware UTC (what the due-check consumes)
    return datetime(y, mo, d, h, mi, tzinfo=_ET).astimezone(timezone.utc)


def _force_due(svc, due: bool = True) -> None:
    svc._v2_overnight_flatten_due = lambda now=None: due


def test_due_check_time_and_weekday():
    svc = _svc(_make_sf())
    assert svc._v2_overnight_flatten_due(now=_u(2026, 7, 16, 19, 55)) is True   # Thu, at T
    assert svc._v2_overnight_flatten_due(now=_u(2026, 7, 16, 20, 30)) is True   # Thu, after T
    assert svc._v2_overnight_flatten_due(now=_u(2026, 7, 16, 19, 54)) is False  # Thu, before T
    assert svc._v2_overnight_flatten_due(now=_u(2026, 7, 18, 20, 0)) is False   # Saturday


@pytest.mark.asyncio
async def test_flatten_closes_open_position_full_qty():
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.80)          # mid-range (no ladder trigger) — flatten must fire anyway
    _force_due(svc)
    await svc._v2_overnight_flatten()
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].intent_type == "close"
    assert Decimal(str(intents[0].quantity)) == Decimal("100")   # FULL qty
    assert intents[0].reason.endswith("V2_OVERNIGHT_FLATTEN")
    row = _row(sf)
    assert row.current_quantity == 0 or row.status == "closed"


@pytest.mark.asyncio
async def test_flag_off_is_byte_identical():
    sf = _make_sf()
    svc = _svc(sf, flatten=False)
    _arm(svc, sf)
    _quote(svc, bid=9.80)
    _force_due(svc)                # even forced due, flag off => nothing
    await svc._v2_overnight_flatten()
    assert _sell_intents(sf) == []


@pytest.mark.asyncio
async def test_before_time_no_flatten():
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf)
    _quote(svc, bid=9.80)
    _force_due(svc, due=False)     # not yet 19:55
    await svc._v2_overnight_flatten()
    assert _sell_intents(sf) == []


@pytest.mark.asyncio
async def test_no_bid_loud_and_not_claimed():
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf)                  # armed, but NO quote => no bid
    _force_due(svc)
    await svc._v2_overnight_flatten()
    assert _sell_intents(sf) == []                 # cannot place — no emit
    assert svc._v2_overnight_flattened == set()    # claim NOT held => retries next loop


@pytest.mark.asyncio
async def test_idempotent_one_close_per_day():
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, qty=100)
    _quote(svc, bid=9.80)
    _force_due(svc)
    await svc._v2_overnight_flatten()
    await svc._v2_overnight_flatten()              # second pass same day
    assert len(_sell_intents(sf)) == 1


@pytest.mark.asyncio
async def test_manual_holding_untouched_scoping_invariant():
    sf = _make_sf()
    svc = _svc(sf)
    _quote(svc, bid=9.80)
    _force_due(svc)
    # NO _arm => SYM is not in _managed_v2_symbols (a manual holding is invisible here)
    await svc._v2_overnight_flatten()
    assert _sell_intents(sf) == []
