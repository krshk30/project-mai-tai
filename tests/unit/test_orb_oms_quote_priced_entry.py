"""ORB OMS-quote-priced entry (Piece 1) — flag-gated, default-off, byte-identical when off.

Covers BOTH halves of the contract:
- ORB emission (orb_app._build_open_intent): flag OFF ships the signal-time break-level
  limit (characterization of today); flag ON OMITS limit_price/reference_price (fail-closed)
  and hands the OMS the bound + price_source + gap_cap.
- OMS re-pricing (process_trade_intent): flag ON prices the entry from the live quote book
  (limit = min(ask+1tick, break*(1+gap_cap))), abandons on MISSING_BOUND / NO_FRESH_QUOTE /
  ASK_PAST_GAP_CAP; flag OFF and non-ORB are untouched (byte-identical).

See docs/orb-oms-quote-priced-entry-design.md.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.services.orb_app import OrbService
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


def _rh_svc(**kw) -> OrbService:
    """ORB service in running-high mode (the live path)."""
    return OrbService(settings=Settings(orb_running_high_enabled=True, **kw), redis_client=MagicMock())


def _oms(**settings_kw) -> OmsRiskService:
    return OmsRiskService(
        settings=Settings(redis_stream_prefix="test", oms_adapter="simulated", **settings_kw),
        redis_client=_FakeRedis(),
        session_factory=_session_factory(),
    )


def _orb_open(metadata: dict, *, symbol: str = "FOO", qty: str = "5") -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="orb",
        payload=TradeIntentPayload(
            strategy_code="orb",
            broker_account_name="paper:orb",
            symbol=symbol,
            side="buy",
            quantity=Decimal(qty),
            intent_type="open",
            reason="ORB_OPEN",
            metadata=metadata,
        ),
    )


def _set_quote(service: OmsRiskService, symbol: str, *, ask: float, bid: float, age_ms: int = 0) -> None:
    service._latest_quotes_by_symbol[symbol.upper()] = {
        "ask": ask,
        "bid": bid,
        "received_at": datetime.now(UTC) - timedelta(milliseconds=age_ms),
    }


def _stored_order(service: OmsRiskService) -> BrokerOrder | None:
    with service.session_factory() as session:
        return session.scalar(select(BrokerOrder))


# --------------------------------------------------------------------------- ORB emission

def test_emit_flag_off_ships_break_level_limit():
    """Characterization of today: flag OFF -> limit_price == break level (byte-identical)."""
    md = _rh_svc()._build_open_intent("FOO", 10.50).payload.metadata
    assert md["order_type"] == "limit"
    assert md["limit_price"] == "10.5000"
    assert md["reference_price"] == "10.5000"
    assert md["orb_intended_break_level"] == "10.5000"
    assert "price_source" not in md


def test_emit_flag_on_omits_price_failclosed():
    """Flag ON -> NO limit_price/reference_price (a stale price is structurally unshippable);
    the OMS gets the bound + price_source + gap_cap instead."""
    md = _rh_svc(orb_oms_quote_priced_entry_enabled=True)._build_open_intent("FOO", 10.50).payload.metadata
    assert md["order_type"] == "limit"
    assert "limit_price" not in md
    assert "reference_price" not in md
    assert md["price_source"] == "ask"
    assert md["orb_intended_break_level"] == "10.5000"
    assert md["orb_gap_cap_pct"] == "1.5"


# --------------------------------------------------------------------------- OMS re-pricing

@pytest.mark.asyncio
async def test_oms_prices_at_ask_plus_tick_bounded():
    service = _oms(orb_oms_quote_priced_entry_enabled=True)
    _set_quote(service, "FOO", ask=1.92, bid=1.90)  # bound = 1.90*1.05 = 1.995 -> ask in range
    events = await service.process_trade_intent(
        _orb_open({
            "order_type": "limit",
            "price_source": "ask",
            "orb_intended_break_level": "1.90",
            "orb_gap_cap_pct": "5.0",
        })
    )
    assert [e.payload.status for e in events] == ["accepted", "filled"]
    order = _stored_order(service)
    assert order is not None
    assert order.payload["limit_price"] == "1.93"           # ask 1.92 + 1 tick, under bound
    assert order.payload["reference_price"] == "1.93"
    assert order.payload["oms_quote_priced"] == "true"
    assert order.payload["oms_quote_ask"] == "1.9200"


@pytest.mark.asyncio
async def test_oms_limit_capped_at_gap_cap_bound():
    """ask is within the bound but ask+1tick would exceed it -> limit floored to the bound."""
    service = _oms(orb_oms_quote_priced_entry_enabled=True)
    _set_quote(service, "FOO", ask=1.995, bid=1.99)  # bound = 2.00*(1+0) = 2.00; ask <= bound
    events = await service.process_trade_intent(
        _orb_open({
            "order_type": "limit",
            "price_source": "ask",
            "orb_intended_break_level": "2.00",
            "orb_gap_cap_pct": "0.0",
        })
    )
    assert [e.payload.status for e in events] == ["accepted", "filled"]
    order = _stored_order(service)
    assert order is not None
    assert order.payload["limit_price"] == "2.00"   # min(1.995+0.01, 2.00) floored to tick
    assert order.payload["oms_quote_priced"] == "true"


@pytest.mark.asyncio
async def test_oms_abandons_ask_past_gap_cap():
    service = _oms(orb_oms_quote_priced_entry_enabled=True)
    _set_quote(service, "FOO", ask=2.00, bid=1.98)  # bound = 1.90*1.015 = 1.9285 < 2.00
    events = await service.process_trade_intent(
        _orb_open({
            "order_type": "limit",
            "price_source": "ask",
            "orb_intended_break_level": "1.90",
            "orb_gap_cap_pct": "1.5",
        })
    )
    assert events[-1].payload.status == "rejected"
    assert events[-1].payload.reason == "ASK_PAST_GAP_CAP"
    assert _stored_order(service) is None  # never submitted


@pytest.mark.asyncio
async def test_oms_abandons_no_fresh_quote():
    service = _oms(orb_oms_quote_priced_entry_enabled=True)
    # no quote in the book at all
    events = await service.process_trade_intent(
        _orb_open({
            "order_type": "limit",
            "price_source": "ask",
            "orb_intended_break_level": "1.90",
            "orb_gap_cap_pct": "1.5",
        })
    )
    assert events[-1].payload.status == "rejected"
    assert events[-1].payload.reason == "NO_FRESH_QUOTE"
    assert _stored_order(service) is None


@pytest.mark.asyncio
async def test_oms_abandons_stale_quote():
    service = _oms(orb_oms_quote_priced_entry_enabled=True)
    _set_quote(service, "FOO", ask=1.92, bid=1.90, age_ms=10_000)  # 10s old > 2000ms
    events = await service.process_trade_intent(
        _orb_open({
            "order_type": "limit",
            "price_source": "ask",
            "orb_intended_break_level": "1.90",
            "orb_gap_cap_pct": "5.0",
        })
    )
    assert events[-1].payload.status == "rejected"
    assert events[-1].payload.reason == "NO_FRESH_QUOTE"


@pytest.mark.asyncio
async def test_oms_abandons_missing_bound():
    service = _oms(orb_oms_quote_priced_entry_enabled=True)
    _set_quote(service, "FOO", ask=1.92, bid=1.90)
    events = await service.process_trade_intent(
        _orb_open({
            "order_type": "limit",
            "price_source": "ask",
            # orb_intended_break_level intentionally absent -> fail-closed
            "orb_gap_cap_pct": "5.0",
        })
    )
    assert events[-1].payload.status == "rejected"
    assert events[-1].payload.reason == "MISSING_BOUND"


@pytest.mark.asyncio
async def test_oms_flag_off_passthrough_byte_identical():
    """Flag OFF -> OMS does not touch the limit; ORB's own price fills as today."""
    service = _oms()  # flag default off
    events = await service.process_trade_intent(
        _orb_open({
            "order_type": "limit",
            "price_source": "ask",
            "limit_price": "1.90",
            "reference_price": "1.90",
            "orb_intended_break_level": "1.90",
        })
    )
    assert [e.payload.status for e in events] == ["accepted", "filled"]
    order = _stored_order(service)
    assert order is not None
    assert order.payload["limit_price"] == "1.90"
    assert "oms_quote_priced" not in order.payload


@pytest.mark.asyncio
async def test_oms_non_orb_untouched_when_flag_on():
    """Flag ON but a non-ORB strategy is never re-priced (v2/others untouched)."""
    service = _oms(orb_oms_quote_priced_entry_enabled=True)
    _set_quote(service, "UGRO", ask=2.60, bid=2.55)
    event = TradeIntentEvent(
        source_service="strategy-engine",
        payload=TradeIntentPayload(
            strategy_code="macd_30s",
            broker_account_name="paper:macd_30s",
            symbol="UGRO",
            side="buy",
            quantity=Decimal("10"),
            intent_type="open",
            reason="ENTRY_P1_MACD_CROSS",
            metadata={"path": "P1_MACD_CROSS", "reference_price": "2.55", "price_source": "ask"},
        ),
    )
    events = await service.process_trade_intent(event)
    assert [e.payload.status for e in events] == ["accepted", "filled"]
    order = _stored_order(service)
    assert order is not None
    assert order.payload["reference_price"] == "2.55"   # untouched
    assert "oms_quote_priced" not in order.payload
