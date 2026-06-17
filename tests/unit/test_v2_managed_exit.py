"""Track-2 Phase-2 Slice-3 — OMS-managed v2 exit ladder (quote-driven).

Exercises `_evaluate_v2_managed_exit` end-to-end through the REAL emit path
(`_emit_v2_managed_sell` → SimulatedBrokerAdapter → `_record_order_reports`) on a
SQLite schema (all tables EXCEPT the JSONB market_*_ticks, which can't render on
SQLite). Decision B (leg-level fills): DECISION on the bid, FILL reference_price at
the leg level. Precedence hard>floor>scale, one action/quote, sole-writer row.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder, OmsManagedPosition, TradeIntent
from project_mai_tai.events import (
    QuoteTickEvent,
    QuoteTickPayload,
    TradeTickEvent,
    TradeTickPayload,
)
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


def _svc(sf, *, enabled: bool = True, adapter=None) -> OmsRiskService:
    settings = Settings(oms_v2_exit_management_enabled=enabled)
    svc = OmsRiskService(
        settings, redis_client=_FakeRedis(), session_factory=sf,
        broker_adapter=adapter or SimulatedBrokerAdapter(),
    )
    with sf() as s:
        svc.store.ensure_strategy(s, "schwab_1m_v2", name="v2")
        svc.store.ensure_broker_account(s, ACCT, provider="simulated", environment="test")
        s.commit()
    return svc


def _arm(svc, sf, *, symbol=SYM, entry=10.0, qty=100, **rowkw) -> None:
    with sf() as s:
        row = svc.store.create_managed_position(
            s, strategy_code="schwab_1m_v2", broker_account_name=ACCT,
            symbol=symbol, entry_price=Decimal(str(entry)), quantity=qty, entry_path="MACD Cross",
        )
        for k, v in rowkw.items():
            setattr(row, k, v)
        s.flush()
        s.commit()
    svc._managed_v2_symbols.add(symbol)


def _quote(svc, bid: float, *, symbol=SYM, age_s: float = 0.0) -> None:
    svc._latest_quotes_by_symbol[symbol] = {
        "bid": bid, "ask": bid + 0.01,
        "received_at": datetime.now(UTC).replace(microsecond=0) if age_s == 0
        else datetime.now(UTC),
    }
    if age_s:
        from datetime import timedelta
        svc._latest_quotes_by_symbol[symbol]["received_at"] = datetime.now(UTC) - timedelta(seconds=age_s)


def _row(sf, symbol=SYM) -> OmsManagedPosition | None:
    with sf() as s:
        return s.scalar(select(OmsManagedPosition).where(OmsManagedPosition.symbol == symbol))


def _sell_intents(sf, symbol=SYM) -> list[TradeIntent]:
    """The v2 managed-exit SELL intents (intent_type/quantity/reason + payload.metadata)."""
    with sf() as s:
        return list(s.scalars(select(TradeIntent).where(
            TradeIntent.symbol == symbol, TradeIntent.side == "sell")).all())


def _ref(intent: TradeIntent) -> Decimal:
    return Decimal(intent.payload["metadata"]["reference_price"])


def _sell_order_count(sf, symbol=SYM) -> int:
    with sf() as s:
        return len(list(s.scalars(select(BrokerOrder).where(
            BrokerOrder.symbol == symbol, BrokerOrder.side == "sell")).all()))


# --------------------------------------------------------------------------- (1)

@pytest.mark.asyncio
async def test_hard_stop_emits_full_close_at_stop_level() -> None:
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.80)                       # below stop 9.85 = 10*(1-1.5%)
    await svc._evaluate_v2_managed_exit(SYM)

    intents = _sell_intents(sf)
    assert len(intents) == 1
    i = intents[0]
    assert i.intent_type == "close" and Decimal(str(i.quantity)) == Decimal("100")
    assert i.payload["metadata"]["oms_v2_managed_exit"] == "true"
    assert _ref(i) == Decimal("9.8500")          # leg LEVEL (stop), not the 9.80 bid
    assert _sell_order_count(sf) == 1            # reached the adapter + filled
    r = _row(sf)
    assert r.status == "closed" and r.current_quantity == 0
    assert SYM not in svc._managed_v2_symbols


# --------------------------------------------------------------------------- (2)

@pytest.mark.asyncio
async def test_scale_emits_partial_at_scale_level_row_stays_open() -> None:
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=10.25)                      # +2.5% → PCT2 scale (>=2%)
    await svc._evaluate_v2_managed_exit(SYM)

    intents = _sell_intents(sf)
    assert len(intents) == 1 and intents[0].intent_type == "scale"
    assert Decimal(str(intents[0].quantity)) == Decimal("50")           # 50% of 100
    assert _ref(intents[0]) == Decimal("10.2000")                       # +2% LEVEL, not 10.25 bid
    r = _row(sf)
    assert r.status == "open" and r.current_quantity == 50
    assert "PCT2" in (r.scales_done or [])
    assert SYM in svc._managed_v2_symbols       # still armed


# --------------------------------------------------------------------------- (3)

@pytest.mark.asyncio
async def test_floor_breach_emits_full_close_at_floor_level() -> None:
    sf = _make_sf()
    svc = _svc(sf)
    # pre-armed floor state (already scaled PCT2; peak 3% → floor locked 1.5% → 10.15)
    _arm(svc, sf, entry=10.0, qty=50,
         peak_profit_pct=Decimal("3"), tier=3, floor_pct=Decimal("1.5"),
         floor_price=Decimal("10.15"), scales_done=["PCT2"])
    _quote(svc, bid=10.10)                      # below floor_price 10.15 → breach
    await svc._evaluate_v2_managed_exit(SYM)

    intents = _sell_intents(sf)
    assert len(intents) == 1 and intents[0].intent_type == "close"
    assert Decimal(str(intents[0].quantity)) == Decimal("50")
    assert _ref(intents[0]) == Decimal("10.1500")                       # floor LEVEL
    assert _row(sf).status == "closed"


# --------------------------------------------------------------------------- (4)

@pytest.mark.asyncio
async def test_no_exit_persists_ladder_state_no_order() -> None:
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=10.15)                      # +1.5%: no stop, no floor-breach, no scale (<2%)
    await svc._evaluate_v2_managed_exit(SYM)

    assert _sell_intents(sf) == []               # nothing emitted
    r = _row(sf)
    assert r.status == "open"
    # co-located quote->Position state-update persisted: tier 2 (peak>=1), floor BE (1% band)
    assert r.tier == 2
    assert r.floor_pct is not None and Decimal(str(r.floor_pct)) == Decimal("0")  # 1% band → BE


# --------------------------------------------------------------------------- (5)

@pytest.mark.asyncio
async def test_dormant_when_flag_off() -> None:
    sf = _make_sf()
    svc = _svc(sf, enabled=False)
    _arm(svc, sf, entry=10.0, qty=100)
    svc._managed_v2_symbols.clear()             # OFF: slice-1 never arms it
    _quote(svc, bid=9.50)                        # would be a hard stop if evaluated
    await svc._evaluate_v2_managed_exit(SYM)
    assert _sell_intents(sf) == []
    assert _row(sf).status == "open"


# --------------------------------------------------------------------------- (6)

@pytest.mark.asyncio
async def test_no_double_close_on_second_quote() -> None:
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.80)
    await svc._evaluate_v2_managed_exit(SYM)     # closes
    await svc._evaluate_v2_managed_exit(SYM)     # row closed + symbol dropped → no-op
    assert len(_sell_intents(sf)) == 1


# --------------------------------------------------------------------------- (7)

@pytest.mark.asyncio
async def test_stale_quote_skipped() -> None:
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.80, age_s=30.0)            # 30s old > 5s window
    await svc._evaluate_v2_managed_exit(SYM)
    assert _sell_intents(sf) == []                # never acted on a stale quote
    assert _row(sf).status == "open"


# ---- Track-2 intrabar fix: event-time staleness + last-quote-wins coalescing -------

def _qp(bid: float, symbol: str = SYM) -> dict:
    return QuoteTickEvent(
        source_service="md",
        payload=QuoteTickPayload(symbol=symbol, bid_price=Decimal(str(bid)), ask_price=Decimal(str(bid + 0.01))),
    ).model_dump(mode="json")


def _tp(price: float, symbol: str = SYM) -> dict:
    return TradeTickEvent(
        source_service="md",
        payload=TradeTickPayload(symbol=symbol, price=Decimal(str(price)), size=100),
    ).model_dump(mode="json")


# --------------------------------------------------------------------------- (8)

@pytest.mark.asyncio
async def test_handle_quote_uses_event_time_for_staleness() -> None:
    """The 2026-06-17 LNAI bug: received_at was processing-time, so a 70s-backlogged quote
    sailed through the 5s guard and the scale filled into a vanished spike. Now received_at
    is the producer's event time, so a stale event is rejected and a fresh one acts."""
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)

    stale = QuoteTickEvent(
        source_service="md",
        produced_at=datetime.now(UTC) - timedelta(seconds=70),
        payload=QuoteTickPayload(symbol=SYM, bid_price=Decimal("9.80"), ask_price=Decimal("9.81")),
    )
    await svc._handle_quote_tick_event(stale)
    assert svc._latest_quotes_by_symbol[SYM]["received_at"] == stale.produced_at  # event time, not now()
    assert _sell_intents(sf) == []                # 70s-old event rejected by the guard
    assert _row(sf).status == "open"

    fresh = QuoteTickEvent(
        source_service="md",
        payload=QuoteTickPayload(symbol=SYM, bid_price=Decimal("9.80"), ask_price=Decimal("9.81")),
    )
    await svc._handle_quote_tick_event(fresh)
    assert len(_sell_intents(sf)) == 1            # fresh event acts → hard stop closes


# --------------------------------------------------------------------------- (9)

def test_coalesce_last_quote_wins() -> None:
    """A burst of quotes for one symbol collapses to the FRESHEST — so the ladder decides
    on the current price, never a stale intermediate spike that already reversed."""
    events = OmsRiskService._coalesce_ticks([_qp(10.10), _qp(10.25), _qp(10.05)])
    quotes = [e for e in events if isinstance(e, QuoteTickEvent)]
    assert len(quotes) == 1
    assert float(quotes[0].payload.bid_price) == 10.05            # last (freshest) wins


def test_coalesce_keeps_one_freshest_quote_per_symbol() -> None:
    events = OmsRiskService._coalesce_ticks([_qp(10.1, "AAA"), _qp(10.2, "BBB"), _qp(10.9, "AAA")])
    by_symbol = {e.payload.symbol: float(e.payload.bid_price) for e in events if isinstance(e, QuoteTickEvent)}
    assert by_symbol == {"AAA": 10.9, "BBB": 10.2}


def test_coalesce_preserves_all_trades_in_order() -> None:
    """Trades are NOT coalesced (armed-hard-stop fidelity) — every one survives, in order."""
    events = OmsRiskService._coalesce_ticks([_tp(10.1), _tp(10.2), _tp(10.0)])
    prices = [float(e.payload.price) for e in events if isinstance(e, TradeTickEvent)]
    assert prices == [10.1, 10.2, 10.0]


def test_coalesce_ignores_unknown_and_symbolless() -> None:
    events = OmsRiskService._coalesce_ticks(
        [{"event_type": "heartbeat"}, {"event_type": "quote_tick", "payload": {}}, _qp(10.25)]
    )
    assert len(events) == 1 and float(events[0].payload.bid_price) == 10.25
