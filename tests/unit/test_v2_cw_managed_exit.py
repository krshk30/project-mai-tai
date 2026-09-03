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


class _ConfirmationAdapter(SimulatedBrokerAdapter):
    def __init__(self, *, armed: bool = False, release_result: str = "released") -> None:
        super().__init__()
        self.armed = armed
        self.release_result = release_result
        self.release_calls: list[tuple[str, str]] = []

    async def fetch_armed_native_oco_symbols(
        self, broker_account_name: str, symbols: list[str]
    ) -> set[str]:
        del broker_account_name
        return set(symbols) if self.armed else set()

    async def release_native_oco_for_close(
        self, broker_account_name: str, entry_broker_order_id: str
    ) -> str:
        self.release_calls.append((broker_account_name, entry_broker_order_id))
        return self.release_result


def _make_sf() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", future=True,
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    tables = [t for t in Base.metadata.sorted_tables
              if t.name not in ("market_trade_ticks", "market_quote_ticks")]
    Base.metadata.create_all(engine, tables=tables)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _svc(
    sf,
    *,
    cw: bool = True,
    floor: bool = False,
    adapter: SimulatedBrokerAdapter | None = None,
) -> OmsRiskService:
    settings = Settings(
        oms_v2_exit_management_enabled=True,
        oms_v2_exit_close_on_fill_enabled=True,
        strategy_schwab_1m_v2_confirmed_window_enabled=cw,
        oms_v2_cw_floor_exit_enabled=floor,
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


def _arm(svc, sf, *, entry=10.0, qty=100) -> None:
    with sf() as s:
        svc.store.create_managed_position(
            s, strategy_code="schwab_1m_v2", broker_account_name=ACCT,
            symbol=SYM, entry_price=Decimal(str(entry)), quantity=qty, entry_path="ATR Flip",
        )
        s.commit()
    svc._managed_v2_symbols.add((ACCT, SYM))


def _quote(svc, bid: float, *, ask: float | None = None) -> None:
    from datetime import UTC, datetime
    svc._latest_quotes_by_symbol[SYM] = {
        "bid": bid, "ask": bid + 0.01 if ask is None else ask,
        "received_at": datetime.now(UTC),
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
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
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
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
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
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
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
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
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
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
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


@pytest.mark.asyncio
async def test_confirmation_exit_releases_native_oco_before_close() -> None:
    sf = _make_sf()
    adapter = _ConfirmationAdapter(armed=True)
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    await svc._handle_stream_message(
        {
            "data": json.dumps(
                {
                    "event_type": "v2_confirmation_exit",
                    "symbol": SYM,
                    "broker_account_name": ACCT,
                    "source_fill_id": "fill-1",
                    "broker_order_id": "entry-order-1",
                    "evaluated_at_ms": "1",
                    "atr_state": "short",
                    "should_exit": True,
                    "entry_slot": "first",
                }
            )
        }
    )
    _quote(svc, bid=9.9)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert adapter.release_calls == [(ACCT, "entry-order-1")]
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CONFIRMATION_EXIT")


@pytest.mark.asyncio
async def test_confirmation_exit_never_arms_for_reclaim() -> None:
    sf = _make_sf()
    svc = _svc(sf, cw=True, adapter=_ConfirmationAdapter())
    await svc._handle_stream_message(
        {
            "data": json.dumps(
                {
                    "event_type": "v2_confirmation_exit",
                    "symbol": SYM,
                    "broker_account_name": ACCT,
                    "evaluated_at_ms": "1",
                    "atr_state": "short",
                    "should_exit": True,
                    "entry_slot": "reclaim",
                }
            )
        }
    )
    assert (ACCT, SYM) not in svc._confirmation_exit_pending


@pytest.mark.asyncio
async def test_confirmation_exit_blocks_when_oco_release_is_unconfirmed() -> None:
    sf = _make_sf()
    adapter = _ConfirmationAdapter(armed=True, release_result="unanswerable")
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    svc._confirmation_exit_pending[(ACCT, SYM)] = {
        "evaluated_at_ms": "1",
        "broker_order_id": "entry-order-1",
    }
    _quote(svc, bid=9.9)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert _sell_intents(sf) == []
    assert (ACCT, SYM) in svc._confirmation_exit_pending


@pytest.mark.asyncio
async def test_confirmation_exit_defers_to_an_oco_leg_that_already_filled() -> None:
    sf = _make_sf()
    adapter = _ConfirmationAdapter(armed=True, release_result="resolved_by_fill")
    svc = _svc(sf, cw=True, adapter=adapter)
    _arm(svc, sf, entry=10.0, qty=100)
    svc._confirmation_exit_pending[(ACCT, SYM)] = {
        "source_fill_id": "fill-1",
        "evaluated_at_ms": "1",
        "broker_order_id": "entry-order-1",
    }
    closed: list[tuple[str, str]] = []

    async def close_resolved(acct: str, symbol: str, *, detail=None) -> None:
        closed.append((acct, symbol))

    svc._close_resolved_oco_managed_row = close_resolved  # type: ignore[method-assign]
    _quote(svc, bid=9.9)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert adapter.release_calls == [(ACCT, "entry-order-1")]
    assert closed == [(ACCT, SYM)]
    assert _sell_intents(sf) == []
    assert (ACCT, SYM) not in svc._confirmation_exit_pending


# --- CW floor exit (2026-07-14): arm at +2%, ride, close on fall-back-to-floor ---


@pytest.mark.asyncio
async def test_cw_floor_arms_at_target_then_exits_on_fallback():
    sf = _make_sf()
    svc = _svc(sf, cw=True, floor=True)
    _arm(svc, sf, entry=10.0, qty=100)                # target/floor = 10.20
    _quote(svc, bid=10.25)                            # reaches +2% -> ARM, no exit
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert _sell_intents(sf) == []
    assert (ACCT, SYM) in svc._cw_floor_armed
    assert _row(sf).status == "open"
    _quote(svc, bid=10.60)                            # rides higher -> still no exit
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert _sell_intents(sf) == []
    _quote(svc, bid=10.20)                            # falls through the persisted +4.5% ratchet
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CW_FLOOR")
    assert _ref(intents[0]) == Decimal("10.4500")     # the ratcheted floor level, not fixed +2%
    assert (ACCT, SYM) not in svc._cw_floor_armed


@pytest.mark.asyncio
async def test_cw_floor_consumes_bid_ratchet_with_both_fallback_outcomes_reachable():
    sf = _make_sf()
    svc = _svc(sf, cw=True, floor=True)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=10.25)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)  # arm fixed +2% minimum
    _quote(svc, bid=10.60)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)  # peak +6% -> durable floor +4.5%

    _quote(svc, bid=10.46)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert _sell_intents(sf) == []  # above 10.45 ratchet: keep riding

    _quote(svc, bid=10.44)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1
    assert intents[0].reason.endswith("CW_FLOOR")
    assert _ref(intents[0]) == Decimal("10.4500")


@pytest.mark.asyncio
async def test_cw_floor_ignores_wide_ask_bounce_when_bid_does_not_really_move():
    sf = _make_sf()
    svc = _svc(sf, cw=True, floor=True)
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=10.25)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    _quote(svc, bid=10.25, ask=12.00)  # spread bounce: ask jumps, executable bid does not
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    _quote(svc, bid=10.21, ask=10.22)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert _sell_intents(sf) == []
    assert _row(sf).floor_price == Decimal("10.05000000")


@pytest.mark.asyncio
async def test_cw_floor_off_is_hard_target_byte_identical():
    sf = _make_sf()
    svc = _svc(sf, cw=True, floor=False)              # floor OFF -> +2% is a HARD close
    _arm(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=10.25)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    intents = _sell_intents(sf)
    assert len(intents) == 1 and intents[0].reason.endswith("CW_TARGET")
    assert (ACCT, SYM) not in svc._cw_floor_armed
