"""OMS extended-hours REACTIVE-entry marketable-limit enhancement (P-B1) — flag-gated, default-off,
byte-identical when off.

CONTEXT: the bot already routes a v2 EH open to a session=AM/PM LIMIT at the live ask (dc11d5a), so
the reactive entry is fillable pre-market today. This flag (`oms_v2_eh_entry_enabled`) layers the
design's thin-EH slippage protection on top: re-price the entry as a marketable limit off the OMS's
OWN fresh Polygon ask (`_latest_quotes_by_symbol`), buffered above the ask and BOUNDED by a max-cross
cap vs the strategy signal price — past the cap or with no fresh ask, ABANDON (no blind order).

RTH and flag-off are byte-identical: the OMS never touches the intent (the bot's plain limit-at-ask
stands). The resting entry (P-B2) and non-v2 strategies are excluded.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import project_mai_tai.oms.service as oms_service
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings


# --------------------------------------------------------------------------- helpers

class _FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    async def xadd(self, stream, fields, **kwargs):
        del kwargs
        self.entries.append((stream, json.loads(fields["data"])))
        return "1-0"

    async def get(self, key):
        return None

    async def set(self, key, value, ex=None):
        del ex
        return True

    async def xread(self, offsets, block=0, count=0):
        del offsets, block, count
        return []

    async def aclose(self):
        return None


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _oms(**settings_kw) -> OmsRiskService:
    return OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated", **settings_kw),
        redis_client=_FakeRedis(),
        session_factory=_session_factory(),
    )


def _v2_open(metadata: dict, *, symbol: str = "FOO", qty: str = "10") -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="schwab_1m_v2",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name="paper:schwab_1m_v2",
            symbol=symbol,
            side="buy",
            quantity=Decimal(qty),
            intent_type="open",
            reason="schwab_1m_v2 ATR Flip CW-v2",
            metadata=metadata,
        ),
    )


def _routed_meta(**extra) -> dict:
    """The metadata the OMS receives for a v2 EH reactive open: the strategy's signal fields PLUS the
    bot's `_apply_extended_hours_routing` stamp (order_type=limit + session=AM + limit_price=ask)."""
    md = {
        "path": "ATR Flip",
        "atr_variant": "CW-v2",
        "entry_price": "2.00",       # the strategy signal / break price = the cap anchor
        "order_type": "limit",       # bot already routed to a limit
        "session": "AM",
        "extended_hours": "true",
        "limit_price": "1.92",       # bot's plain limit-at-ask
        "reference_price": "1.92",
        "price_source": "ask",
    }
    md.update(extra)
    return md


def _set_quote(service: OmsRiskService, symbol: str, *, ask: float, bid: float, age_ms: int = 0) -> None:
    service._latest_quotes_by_symbol[symbol.upper()] = {
        "ask": ask,
        "bid": bid,
        "received_at": datetime.now(UTC) - timedelta(milliseconds=age_ms),
    }


def _stored_order(service: OmsRiskService) -> BrokerOrder | None:
    with service.session_factory() as session:
        return session.scalar(select(BrokerOrder))


@pytest.fixture
def eh(monkeypatch):
    """Force extended hours (pre-market AM) for the module-level session helpers the OMS reads."""
    monkeypatch.setattr(oms_service, "_is_regular_market_session", lambda now=None: False)
    monkeypatch.setattr(oms_service, "_extended_hours_session", lambda now=None: "AM")


@pytest.fixture
def rth(monkeypatch):
    monkeypatch.setattr(oms_service, "_is_regular_market_session", lambda now=None: True)
    monkeypatch.setattr(oms_service, "_extended_hours_session", lambda now=None: None)


# --------------------------------------------------------------------------- flag / default

def test_flag_defaults_off():
    s = Settings()
    assert s.oms_v2_eh_entry_enabled is False
    assert s.oms_v2_eh_entry_limit_buffer_pct == 0.3
    assert s.oms_v2_eh_entry_max_cross_pct == 1.0
    assert s.oms_v2_eh_entry_quote_max_age_ms == 2000


# --------------------------------------------------------------------------- re-pricing (flag ON, EH)

@pytest.mark.asyncio
async def test_eh_prices_marketable_buffered_limit(eh):
    service = _oms(oms_v2_eh_entry_enabled=True)
    # signal 2.00, cap = 2.00*1.01 = 2.02; ask 1.92 -> limit = 1.92*1.003 = 1.92576 -> 1.92 (round-down).
    _set_quote(service, "FOO", ask=1.92, bid=1.90)
    events = await service.process_trade_intent(_v2_open(_routed_meta()))
    assert [e.payload.status for e in events] == ["accepted", "filled"]
    order = _stored_order(service)
    assert order is not None
    assert order.payload["order_type"] == "limit"
    assert order.payload["session"] == "AM"
    assert order.payload["extended_hours"] == "true"
    assert order.payload["oms_v2_eh_entry"] == "true"
    assert order.payload["oms_v2_eh_entry_ask"] == "1.9200"
    assert order.payload["oms_v2_eh_entry_cap"] == "2.0200"
    assert order.payload["limit_price"] == "1.92"   # ask*(1+0.3%) rounded down to tick
    assert order.payload["reference_price"] == "1.92"


@pytest.mark.asyncio
async def test_eh_limit_floored_to_max_cross_cap(eh):
    service = _oms(oms_v2_eh_entry_enabled=True, oms_v2_eh_entry_limit_buffer_pct=2.0)
    # signal 2.00 -> cap 2.02. ask 2.015 (<= cap). ask*(1+2%) = 2.0553 would exceed cap -> floored to 2.02.
    _set_quote(service, "FOO", ask=2.015, bid=2.00)
    events = await service.process_trade_intent(_v2_open(_routed_meta()))
    assert [e.payload.status for e in events] == ["accepted", "filled"]
    order = _stored_order(service)
    assert order is not None
    assert order.payload["limit_price"] == "2.02"   # min(2.0553, 2.02) floored to tick
    assert order.payload["oms_v2_eh_entry"] == "true"


@pytest.mark.asyncio
async def test_eh_abandons_ask_past_cross_cap(eh):
    service = _oms(oms_v2_eh_entry_enabled=True)
    # signal 2.00 -> cap 2.02; ask 2.05 > cap -> the market ran away -> abandon (prefer no fill).
    _set_quote(service, "FOO", ask=2.05, bid=2.03)
    events = await service.process_trade_intent(_v2_open(_routed_meta()))
    assert events[-1].payload.status == "rejected"
    assert events[-1].payload.reason == "ASK_PAST_CROSS_CAP"
    assert _stored_order(service) is None   # never submitted


@pytest.mark.asyncio
async def test_eh_abandons_no_fresh_quote(eh):
    service = _oms(oms_v2_eh_entry_enabled=True)
    # no quote in the book at all -> never submit a blind limit.
    events = await service.process_trade_intent(_v2_open(_routed_meta()))
    assert events[-1].payload.status == "rejected"
    assert events[-1].payload.reason == "NO_FRESH_QUOTE"
    assert _stored_order(service) is None


@pytest.mark.asyncio
async def test_eh_abandons_stale_quote(eh):
    service = _oms(oms_v2_eh_entry_enabled=True)
    _set_quote(service, "FOO", ask=1.92, bid=1.90, age_ms=10_000)  # 10s old > 2000ms
    events = await service.process_trade_intent(_v2_open(_routed_meta()))
    assert events[-1].payload.status == "rejected"
    assert events[-1].payload.reason == "NO_FRESH_QUOTE"
    assert _stored_order(service) is None


@pytest.mark.asyncio
async def test_eh_abandons_missing_signal(eh):
    service = _oms(oms_v2_eh_entry_enabled=True)
    _set_quote(service, "FOO", ask=1.92, bid=1.90)
    md = _routed_meta()
    del md["entry_price"]   # fail-closed: no cap anchor
    events = await service.process_trade_intent(_v2_open(md))
    assert events[-1].payload.status == "rejected"
    assert events[-1].payload.reason == "MISSING_SIGNAL"
    assert _stored_order(service) is None


# --------------------------------------------------------------------------- byte-identical / exclusions

@pytest.mark.asyncio
async def test_flag_off_eh_byte_identical(eh):
    """Flag OFF -> the OMS does not touch the intent; the bot's plain limit-at-ask (1.92) fills as today."""
    service = _oms()  # flag default off
    _set_quote(service, "FOO", ask=1.80, bid=1.78)  # a different fresh ask that WOULD have re-priced
    events = await service.process_trade_intent(_v2_open(_routed_meta()))
    assert [e.payload.status for e in events] == ["accepted", "filled"]
    order = _stored_order(service)
    assert order is not None
    assert order.payload["limit_price"] == "1.92"   # untouched (bot's price)
    assert "oms_v2_eh_entry" not in order.payload


@pytest.mark.asyncio
async def test_rth_flag_on_byte_identical(rth):
    """Flag ON but regular session -> the OMS never routes (byte-identical MARKET/limit path). A RTH v2
    open is a plain market order (the bot adds no routing in RTH); the OMS leaves it untouched."""
    service = _oms(oms_v2_eh_entry_enabled=True)
    _set_quote(service, "FOO", ask=1.92, bid=1.90)
    events = await service.process_trade_intent(
        _v2_open({"path": "ATR Flip", "atr_variant": "CW-v2", "entry_price": "2.00",
                  "reference_price": "2.00"})  # plain RTH market open
    )
    assert [e.payload.status for e in events] == ["accepted", "filled"]
    order = _stored_order(service)
    assert order is not None
    assert "oms_v2_eh_entry" not in order.payload
    assert order.payload.get("order_type", "market") == "market"


@pytest.mark.asyncio
async def test_resting_entry_excluded(eh):
    """The RESTING entry (P-B2) is excluded — it carries resting_entry=true and is drained on a path
    that never reaches this builder; even if it did, applies() skips it."""
    service = _oms(oms_v2_eh_entry_enabled=True)
    _set_quote(service, "FOO", ask=1.80, bid=1.78)
    md = _routed_meta(resting_entry="true", order_type="STOP_LIMIT",
                      stop_price="1.90", limit_price="1.91")
    await service.process_trade_intent(_v2_open(md))
    order = _stored_order(service)
    assert order is not None
    assert "oms_v2_eh_entry" not in order.payload


@pytest.mark.asyncio
async def test_non_v2_untouched(eh):
    """Flag ON but a non-v2 strategy is never re-priced (ORB/others untouched)."""
    service = _oms(oms_v2_eh_entry_enabled=True)
    _set_quote(service, "BAR", ask=1.92, bid=1.90)
    event = TradeIntentEvent(
        source_service="orb",
        payload=TradeIntentPayload(
            strategy_code="orb",
            broker_account_name="paper:orb",
            symbol="BAR",
            side="buy",
            quantity=Decimal("5"),
            intent_type="open",
            reason="ORB_OPEN",
            metadata={"order_type": "limit", "session": "AM", "extended_hours": "true",
                      "limit_price": "1.92", "reference_price": "1.92", "entry_price": "1.90"},
        ),
    )
    await service.process_trade_intent(event)
    order = _stored_order(service)
    assert order is not None
    assert "oms_v2_eh_entry" not in order.payload
    assert order.payload["limit_price"] == "1.92"   # untouched
