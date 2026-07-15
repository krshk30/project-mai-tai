"""Track-2 Phase-2 Slice-3 — OMS-managed v2 exit ladder (quote-driven).

Exercises `_evaluate_v2_managed_exit` end-to-end through the REAL emit path
(`_emit_v2_managed_sell` → SimulatedBrokerAdapter → `_record_order_reports`) on a
SQLite schema (all tables EXCEPT the JSONB market_*_ticks, which can't render on
SQLite). Decision B (leg-level fills): DECISION on the bid, FILL reference_price at
the leg level. Precedence hard>floor>scale, one action/quote, sole-writer row.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport
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


@pytest.fixture(autouse=True)
def _market_always_fillable(monkeypatch):
    """These tests exercise the exit ladder through `_handle_quote_tick_event` /
    `_evaluate_v2_managed_exit`, not the 7 AM–8 PM ET fillable-session gate. Hold the
    market open so they are deterministic regardless of wall-clock run time (the gate
    itself is covered in test_oms_fillable_window.py / test_oms_risk_service.py)."""
    monkeypatch.setattr(
        "project_mai_tai.oms.service.OmsRiskService._market_is_fillable",
        lambda self, now=None: True,
    )


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


def _svc(sf, *, enabled: bool = True, close_on_fill: bool = True, adapter=None) -> OmsRiskService:
    settings = Settings(
        oms_v2_exit_management_enabled=enabled,
        oms_v2_exit_close_on_fill_enabled=close_on_fill,
    )
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
    svc._managed_v2_symbols.add((ACCT, symbol))


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
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    intents = _sell_intents(sf)
    assert len(intents) == 1
    i = intents[0]
    assert i.intent_type == "close" and Decimal(str(i.quantity)) == Decimal("100")
    assert i.payload["metadata"]["oms_v2_managed_exit"] == "true"
    assert _ref(i) == Decimal("9.8500")          # leg LEVEL (stop), not the 9.80 bid
    assert _sell_order_count(sf) == 1            # reached the adapter + filled
    r = _row(sf)
    assert r.status == "closed" and r.current_quantity == 0
    assert (ACCT, SYM) not in svc._managed_v2_symbols


# --------------------------------------------------------------------------- (2)

@pytest.mark.asyncio
async def test_scale_emits_partial_at_scale_level_row_stays_open() -> None:
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=10.25)                      # +2.5% → PCT2 scale (>=2%)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    intents = _sell_intents(sf)
    assert len(intents) == 1 and intents[0].intent_type == "scale"
    assert Decimal(str(intents[0].quantity)) == Decimal("50")           # 50% of 100
    assert _ref(intents[0]) == Decimal("10.2000")                       # +2% LEVEL, not 10.25 bid
    r = _row(sf)
    assert r.status == "open" and r.current_quantity == 50
    assert "PCT2" in (r.scales_done or [])
    assert (ACCT, SYM) in svc._managed_v2_symbols       # still armed


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
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

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
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

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
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert _sell_intents(sf) == []
    assert _row(sf).status == "open"


# --------------------------------------------------------------------------- (6)

@pytest.mark.asyncio
async def test_no_double_close_on_second_quote() -> None:
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.80)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)     # closes
    await svc._evaluate_v2_managed_exit(ACCT, SYM)     # row closed + symbol dropped → no-op
    assert len(_sell_intents(sf)) == 1


# --------------------------------------------------------------------------- (7)

@pytest.mark.asyncio
async def test_stale_quote_skipped() -> None:
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.80, age_s=30.0)            # 30s old > 5s window
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
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


# ---- Extended-hours exit routing (2026-07-05 CLRO/CELZ stuck-exit fix) --------------
# In RTH the exit stays MARKET/NORMAL (byte-identical). In extended hours it routes a
# LIMIT + session=AM|PM so it can actually fill. Protective legs (hard-stop/floor) use a
# marketable buffer below the bid; scale partials price at the bid. reference_price (leg
# level) is unchanged, so the SimulatedBrokerAdapter (fills at reference_price) is
# behaviorally identical — these assert the emitted INTENT METADATA (the live route).

from project_mai_tai.oms.service import (  # noqa: E402
    _extended_hours_session,
    _format_limit_price,
    _panic_limit_price,
)


def _meta(intent: TradeIntent) -> dict:
    return intent.payload["metadata"]


def _force_session(monkeypatch, value: str | None) -> None:
    monkeypatch.setattr("project_mai_tai.oms.service._extended_hours_session", lambda now=None: value)


# --------------------------------------------------------------------------- (E1)

@pytest.mark.asyncio
async def test_rth_exit_stays_market_byte_identical(monkeypatch) -> None:
    _force_session(monkeypatch, None)                # regular trading hours
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.80)                            # hard stop
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    m = _meta(_sell_intents(sf)[0])
    assert m["order_type"] == "market"               # unchanged
    assert "session" not in m and "limit_price" not in m
    assert m["reference_price"] == "9.8500"          # leg level, unchanged


# --------------------------------------------------------------------------- (E2)

@pytest.mark.asyncio
async def test_pm_hard_stop_routes_buffered_marketable_limit(monkeypatch) -> None:
    _force_session(monkeypatch, "PM")                # after-hours
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.80)                            # hard stop, live bid 9.80
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    m = _meta(_sell_intents(sf)[0])
    assert m["order_type"] == "limit"
    assert m["session"] == "PM" and m["extended_hours"] == "true"
    assert m["price_source"] == "bid"
    assert m["limit_price"] == _panic_limit_price(9.80, 0.5) == "9.75"   # bid x (1-0.5%)
    assert m["reference_price"] == "9.8500"          # leg level PRESERVED (sim/re-score parity)


# --------------------------------------------------------------------------- (E3)

@pytest.mark.asyncio
async def test_am_floor_breach_routes_buffered_marketable_limit(monkeypatch) -> None:
    _force_session(monkeypatch, "AM")                # pre-market
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=50,
         peak_profit_pct=Decimal("3"), tier=3, floor_pct=Decimal("1.5"),
         floor_price=Decimal("10.15"), scales_done=["PCT2"])
    _quote(svc, bid=10.10)                           # floor breach, live bid 10.10
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    m = _meta(_sell_intents(sf)[0])
    assert m["order_type"] == "limit" and m["session"] == "AM"
    assert m["limit_price"] == _panic_limit_price(10.10, 0.5) == "10.05"
    assert m["reference_price"] == "10.1500"         # floor level PRESERVED


# --------------------------------------------------------------------------- (E4)

@pytest.mark.asyncio
async def test_pm_scale_routes_at_bid_zero_buffer(monkeypatch) -> None:
    _force_session(monkeypatch, "PM")
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=10.25)                           # +2.5% → PCT2 scale, live bid 10.25
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    m = _meta(_sell_intents(sf)[0])
    assert m["order_type"] == "limit" and m["session"] == "PM"
    assert m["price_source"] == "bid"
    assert m["limit_price"] == _format_limit_price(10.25) == "10.25"     # AT the bid, NOT buffered
    assert m["reference_price"] == "10.2000"         # +2% leg level PRESERVED


# --------------------------------------------------------------------------- (E5)

@pytest.mark.asyncio
async def test_eh_missing_bid_falls_back_to_market(monkeypatch) -> None:
    """Fail-safe: EH but no usable bid at emit → stays MARKET (never blocks the exit).
    The eval guards bid>0, so this exercises _emit_v2_managed_sell directly with bid=None."""
    _force_session(monkeypatch, "PM")
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, entry=10.0, qty=100)
    with sf() as s:
        row = svc.store.get_open_managed_position(s, broker_account_name=ACCT, symbol=SYM)
        events = await svc._emit_v2_managed_sell(
            s, row, intent_type="close", quantity=100,
            reference_price=9.85, reason="oms_v2_managed_exit:HARD_STOP", bid=None,
        )
        s.commit()
    assert events
    m = _meta(_sell_intents(sf)[0])
    assert m["order_type"] == "market"               # fail-safe: no bid → market, not blocked
    assert "session" not in m


# --------------------------------------------------------------------------- (E6)

def test_extended_hours_session_boundaries() -> None:
    """The session helper the exit routing keys off (America/New_York)."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    assert _extended_hours_session(datetime(2026, 7, 6, 8, 0, tzinfo=et)) == "AM"    # pre-market
    assert _extended_hours_session(datetime(2026, 7, 6, 14, 0, tzinfo=et)) is None   # RTH
    assert _extended_hours_session(datetime(2026, 7, 6, 16, 30, tzinfo=et)) == "PM"  # after-hours


# ---- #6: mark closed on FILL not submit (CLRO closed-on-submit desync fix) --------------
# Root cause: the eval used to close the managed row on the exit SUBMIT. If the exit never
# filled (CLRO: EH market order that couldn't cross), the OMS showed flat while the broker
# still held the shares -> stranded, unmonitored, reconciler mismatch. #6 fill-gates the
# close: the row transitions to closed ONLY on a confirmed fill; a working-but-unfilled exit
# leaves the row open + monitored + broker-consistent, and does not re-emit (dedup guard).

class _NoFillAdapter:
    """Submits the exit (accepted / working) but NEVER returns a fill — the CLRO case
    (a resting limit that doesn't cross, or an EH order that can't fill)."""

    def __init__(self) -> None:
        self.submitted: list = []

    async def submit_order(self, request):
        self.submitted.append(request)
        return [ExecutionReport(
            event_type="accepted", client_order_id=request.client_order_id,
            broker_order_id="wrk-1", symbol=request.symbol, side=request.side,
            intent_type=request.intent_type, quantity=request.quantity,
            reason=request.reason, metadata=dict(request.metadata),
        )]

    async def fetch_order_update(self, request):
        return None                       # still working; no fill

    async def list_account_positions(self, broker_account_name: str):
        return []


class _PartialFillAdapter:
    """Fills only part of the exit on submit (e.g. 6 of 10)."""

    def __init__(self, fill_qty: int) -> None:
        self.fill_qty = fill_qty

    async def submit_order(self, request):
        return [ExecutionReport(
            event_type="partially_filled", client_order_id=request.client_order_id,
            broker_order_id="wrk-1", broker_fill_id="f1", symbol=request.symbol, side=request.side,
            intent_type=request.intent_type, quantity=request.quantity,
            filled_quantity=Decimal(str(self.fill_qty)), fill_price=Decimal("9.85"),
            reason=request.reason, metadata=dict(request.metadata),
        )]

    async def fetch_order_update(self, request):
        return None

    async def list_account_positions(self, broker_account_name: str):
        return []


# --------------------------------------------------------------------------- (#6-1) MUST-PASS
@pytest.mark.asyncio
async def test_clro_never_fills_stays_open_monitored_no_reemit() -> None:
    """THE invariant proof (analog of the SPOF stop-decouple test): an exit that SUBMITS
    but NEVER fills must leave the position OPEN, quantity intact, still monitored, and must
    NOT re-emit on the next quote — so the OMS record stays consistent with the broker."""
    sf = _make_sf()
    adapter = _NoFillAdapter()
    svc = _svc(sf, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)

    _quote(svc, bid=9.80)                              # hard stop
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert len(adapter.submitted) == 1                 # exit submitted...
    r = _row(sf)
    assert r.status == "open"                          # ...but NOT marked closed on submit
    assert r.current_quantity == 100                   # broker still holds 100 -> record honest
    assert (ACCT, SYM) in svc._managed_v2_symbols              # still monitored / protected
    assert _sell_order_count(sf) == 1                  # one working exit order

    # next quote: the dedup guard prevents a re-emit storm while the exit is working
    _quote(svc, bid=9.75)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert len(adapter.submitted) == 1                 # NO second exit emitted
    r2 = _row(sf)
    assert r2.status == "open" and r2.current_quantity == 100
    assert (ACCT, SYM) in svc._managed_v2_symbols


# --------------------------------------------------------------------------- (#6-2)
@pytest.mark.asyncio
async def test_partial_fill_decrements_not_flat() -> None:
    """Partial fill: exit 10, fills 6 -> position is 4 (NOT 0, NOT 10). Fill-gated qty."""
    sf = _make_sf()
    svc = _svc(sf, adapter=_PartialFillAdapter(fill_qty=6))
    _arm(svc, sf, entry=10.0, qty=10)

    _quote(svc, bid=9.80)                              # hard stop -> close 10, fills 6
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    r = _row(sf)
    assert r.status == "open" and r.current_quantity == 4   # 10 - 6 confirmed fill
    assert (ACCT, SYM) in svc._managed_v2_symbols              # remaining 4 still monitored


# --------------------------------------------------------------------------- (#6-3)
@pytest.mark.asyncio
async def test_retry_after_working_exit_cancelled() -> None:
    """Once the working exit terminates without a full fill (cancelled), the guard clears
    and the next quote re-emits (retry) — the position is not left un-exited forever."""
    sf = _make_sf()
    adapter = _NoFillAdapter()
    svc = _svc(sf, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.80)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert len(adapter.submitted) == 1

    # simulate the working exit going terminal (cancelled) -> no longer an open exit order
    with sf() as s:
        for o in s.scalars(select(BrokerOrder).where(BrokerOrder.symbol == SYM, BrokerOrder.side == "sell")):
            o.status = "cancelled"
        s.commit()

    _quote(svc, bid=9.75)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert len(adapter.submitted) == 2                 # re-emitted after the guard cleared
    assert _row(sf).status == "open" and _row(sf).current_quantity == 100


# --------------------------------------------------------------------------- (#6-4)
@pytest.mark.asyncio
async def test_happy_path_immediate_fill_closes_on_fill() -> None:
    """Paper / immediate-fill: the sim adapter fills inline -> the fill handler closes the row
    in the same eval. Behaviour-identical to pre-#6 for the immediate-fill case."""
    sf = _make_sf()
    svc = _svc(sf)                                     # SimulatedBrokerAdapter fills inline
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.80)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    r = _row(sf)
    assert r.status == "closed" and r.current_quantity == 0
    assert (ACCT, SYM) not in svc._managed_v2_symbols


# --------------------------------------------------------------------------- (#6-5) rollback lever
@pytest.mark.asyncio
async def test_legacy_flag_off_closes_on_submit() -> None:
    """Rollback lever: with close-on-fill OFF, the legacy close-on-submit behaviour is
    preserved — the row closes on submit even though the exit never filled (the old bug,
    intentionally reachable so a rollback is byte-identical to prior production)."""
    sf = _make_sf()
    svc = _svc(sf, close_on_fill=False, adapter=_NoFillAdapter())
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.80)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    r = _row(sf)
    assert r.status == "closed" and r.current_quantity == 0    # legacy: closed on submit
    assert (ACCT, SYM) not in svc._managed_v2_symbols


# --------------------------------------------------------------------------- (DUAL-BROKER)
# Account-aware CW/managed-exit path: the guard set + eval are keyed by (account, symbol)
# so a future v2 Webull-mirror has BOTH legs' ladders evaluated. Flag OFF == single account
# (byte-identical to prior behaviour); flag ON tracks/exits each account independently.

WEBULL_ACCT = "live:v2_webull"


def _svc_dual(sf) -> OmsRiskService:
    """v2 svc with the Webull-mirror flag ON and BOTH broker accounts seeded."""
    settings = Settings(
        oms_v2_exit_management_enabled=True,
        oms_v2_exit_close_on_fill_enabled=True,
        strategy_schwab_1m_v2_webull_mirror_enabled=True,
        strategy_schwab_1m_v2_webull_account_name=WEBULL_ACCT,
    )
    svc = OmsRiskService(
        settings, redis_client=_FakeRedis(), session_factory=sf,
        broker_adapter=SimulatedBrokerAdapter(),
    )
    with sf() as s:
        svc.store.ensure_strategy(s, "schwab_1m_v2", name="v2")
        svc.store.ensure_broker_account(s, ACCT, provider="simulated", environment="test")
        svc.store.ensure_broker_account(s, WEBULL_ACCT, provider="simulated", environment="test")
        s.commit()
    return svc


def _arm_on(svc, sf, acct, *, symbol=SYM, entry=10.0, qty=100) -> None:
    with sf() as s:
        svc.store.create_managed_position(
            s, strategy_code="schwab_1m_v2", broker_account_name=acct,
            symbol=symbol, entry_price=Decimal(str(entry)), quantity=qty, entry_path="MACD Cross",
        )
        s.commit()
    svc._managed_v2_symbols.add((acct, symbol))


def _quote_event(symbol: str, bid: float) -> QuoteTickEvent:
    return QuoteTickEvent(
        source_service="market-data",
        payload=QuoteTickPayload(
            symbol=symbol, bid_price=Decimal(str(bid)), ask_price=Decimal(str(bid + 0.01)),
        ),
    )


@pytest.mark.asyncio
async def test_flag_off_single_account_evaluates_as_before() -> None:
    """(a) mirror flag OFF: _v2_accounts() == [schwab] and a managed symbol on the schwab
    account is exited exactly as before, driven through the real quote-tick dispatch."""
    sf = _make_sf()
    svc = _svc(sf)                                   # flag defaults OFF
    assert svc._v2_accounts() == [ACCT]
    _arm(svc, sf, entry=10.0, qty=100)
    assert svc._managed_v2_symbols == {(ACCT, SYM)}
    await svc._handle_quote_tick_event(_quote_event(SYM, 9.80))   # hard stop
    assert _sell_order_count(sf) == 1
    assert _row(sf).status == "closed"
    assert (ACCT, SYM) not in svc._managed_v2_symbols            # disarmed


@pytest.mark.asyncio
async def test_flag_on_tracks_and_exits_both_accounts_independently() -> None:
    """(b) mirror flag ON: _v2_accounts() includes the Webull account, and a managed row on
    EACH account for the SAME symbol is tracked + exited independently on one quote."""
    sf = _make_sf()
    svc = _svc_dual(sf)
    assert svc._v2_accounts() == [ACCT, WEBULL_ACCT]
    _arm_on(svc, sf, ACCT, entry=10.0, qty=100)
    _arm_on(svc, sf, WEBULL_ACCT, entry=10.0, qty=100)
    assert svc._managed_v2_symbols == {(ACCT, SYM), (WEBULL_ACCT, SYM)}

    await svc._handle_quote_tick_event(_quote_event(SYM, 9.80))   # hard stop on BOTH legs

    with sf() as s:
        rows = list(s.scalars(
            select(OmsManagedPosition).where(OmsManagedPosition.symbol == SYM)
        ).all())
    assert len(rows) == 2                                         # one row per account
    assert {r.broker_account_name for r in rows} == {ACCT, WEBULL_ACCT}
    assert all(r.status == "closed" for r in rows)               # each leg exited
    assert svc._managed_v2_symbols == set()                      # both disarmed


# --------------------------------------------------------------------------- decided_at
# The [OMS-V2-MANAGED-EXIT] line is emitted AFTER submit_order + _record_order_reports,
# so its own log timestamp trails the broker round-trip. Measured on live fills
# 2026-07-15: 30/30 markers postdated the broker fill (median +1.4s, max +4.5s), which
# is what produced the phantom "~3.9s decision lag". decided_at pins the pre-submit
# decision instant so exit latency stays measurable from the log.
#
# caplog can't be used here: OmsRiskService.__init__ -> configure_logging ->
# logging.basicConfig(force=True) drops caplog's root handler. Capture off the
# service's own logger instead.

class _SlowFillAdapter:
    """Fills, but only after a measurable broker round-trip (the real-world case)."""

    def __init__(self, delay_s: float = 0.25) -> None:
        self.delay_s = delay_s
        self.submit_started_at: datetime | None = None

    async def submit_order(self, request):
        import asyncio
        self.submit_started_at = datetime.now(UTC)
        await asyncio.sleep(self.delay_s)
        return [ExecutionReport(
            event_type="filled", client_order_id=request.client_order_id,
            broker_order_id="slow-1", broker_fill_id="slow-f1", symbol=request.symbol,
            side=request.side, intent_type=request.intent_type, quantity=request.quantity,
            filled_quantity=request.quantity, fill_price=Decimal("9.85"),
            reason=request.reason, metadata=dict(request.metadata),
        )]

    async def fetch_order_update(self, request):
        return None

    async def list_account_positions(self, broker_account_name: str):
        return []


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _exit_marker(cap: _Capture) -> logging.LogRecord:
    hits = [r for r in cap.records if "[OMS-V2-MANAGED-EXIT]" in r.getMessage()
            and "decided_at=" in r.getMessage()]
    assert len(hits) == 1, f"expected exactly 1 exit marker, got {len(hits)}"
    return hits[0]


def _decided_at(rec: logging.LogRecord) -> datetime:
    return datetime.fromisoformat(rec.getMessage().split("decided_at=")[1].split()[0])


async def _run_slow_exit(delay: float) -> tuple[_SlowFillAdapter, _Capture]:
    adapter = _SlowFillAdapter(delay_s=delay)
    sf = _make_sf()
    svc = _svc(sf, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.80)                       # below stop 9.85 = 10*(1-1.5%)
    cap = _Capture()
    svc.logger.addHandler(cap)
    try:
        await svc._evaluate_v2_managed_exit(ACCT, SYM)
    finally:
        svc.logger.removeHandler(cap)
    return adapter, cap


@pytest.mark.asyncio
async def test_decided_at_precedes_the_broker_submit() -> None:
    """decided_at must be the DECISION instant — strictly before the broker call —
    not the moment the log line is written."""
    adapter, cap = await _run_slow_exit(0.25)
    assert adapter.submit_started_at is not None, "the exit must have reached the broker"
    assert _decided_at(_exit_marker(cap)) <= adapter.submit_started_at


@pytest.mark.asyncio
async def test_decided_at_is_earlier_than_the_log_lines_own_timestamp() -> None:
    """Pins the bug this fixes: the line is written after the round-trip, so its own
    timestamp is NOT usable as the decision time — decided_at is."""
    delay = 0.25
    _, cap = await _run_slow_exit(delay)
    rec = _exit_marker(cap)
    logged_at = datetime.fromtimestamp(rec.created, UTC)
    assert (logged_at - _decided_at(rec)).total_seconds() >= delay
