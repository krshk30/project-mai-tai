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
async def test_no_bid_loud_no_emit():
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf)                  # armed, but NO quote => no bid
    _force_due(svc)
    await svc._v2_overnight_flatten()
    assert _sell_intents(sf) == []                 # cannot place — no emit; retries next loop


@pytest.mark.asyncio
async def test_retries_when_close_does_not_fill():
    """THE bug fix: a close that expires unfilled leaves the position OPEN, so the next pass
    RE-EMITS (there is no per-day claim). The bug was claim-on-emit => one attempt, then silent
    give-up while the position rides overnight naked."""
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, qty=100)
    _quote(svc, bid=9.80)
    _force_due(svc)
    calls: list[str] = []

    async def _noop_emit(acct, symbol, position, entry_price, **kw):
        calls.append(kw.get("reason", ""))   # emit attempted; position stays OPEN (no fill)

    svc._emit_v2_exit_on_loop = _noop_emit
    await svc._v2_overnight_flatten()
    await svc._v2_overnight_flatten()            # still open, no working order => RE-EMIT
    assert calls == ["V2_OVERNIGHT_FLATTEN", "V2_OVERNIGHT_FLATTEN"]   # retried, not given up


@pytest.mark.asyncio
async def test_no_double_submit_when_closed():
    """Double-submit is prevented not by a claim but by the real mechanism: the first close fills
    (close_on_fill) and closes the row, so the second pass reads None and does not re-emit."""
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf, qty=100)
    _quote(svc, bid=9.80)
    _force_due(svc)
    await svc._v2_overnight_flatten()
    await svc._v2_overnight_flatten()              # position now closed => no second emit
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


# --------------------------------------------- no-bid: NAKED vs PHANTOM (2026-07-27)
# "No bid" wore one face for two opposite situations and the old code treated both as naked:
#   (a) we genuinely hold it and the AH book is empty          -> NAKED. stay loud, retry.
#   (b) the broker holds NOTHING and the row is a PHANTOM      -> nothing to flatten, ever.
# Live 07-27: two phantom QBTX rows produced 58 ERROR lines in four minutes, every 15s, and
# cleared nothing — a human had to delete the rows. The flatten cannot price a close for stock
# that does not exist, so it can never make progress; it just pages forever.
#
# NOTE the pre-existing test_no_bid_loud_no_emit asserts only "no sell intent", which is true on
# BOTH branches — it cannot tell them apart. These assert on the ROW.

def _flat_says(svc, verdict):
    """Pin `_broker_symbol_is_flat` — the positive-confirmation helper the branch turns on."""
    async def _fake(acct, symbol, *, established_at=None):
        if isinstance(verdict, Exception):
            raise verdict
        return verdict
    svc._broker_symbol_is_flat = _fake


@pytest.mark.asyncio
async def test_no_bid_but_broker_flat_reconciles_the_phantom_row() -> None:
    """THE FIX: a row the broker does not back is cleared instead of paging forever."""
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf)
    _force_due(svc)
    _flat_says(svc, True)                       # broker positively confirms flat
    await svc._v2_overnight_flatten()
    row = _row(sf)
    assert row.current_quantity == 0            # reconciled away
    assert row.status == "closed"
    assert _sell_intents(sf) == []              # and NOT by selling anything
    assert (ACCT, SYM) not in svc._managed_v2_symbols


@pytest.mark.asyncio
async def test_no_bid_while_genuinely_HELD_stays_loud_and_keeps_the_row() -> None:
    """⛔ THE CASE THAT MUST NOT REGRESS. A real position in an empty AH book is NAKED — the
    whole reason this sweep exists. It must keep the row and keep retrying, never 'reconcile'
    away protection because the book happens to be thin."""
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf)
    _force_due(svc)
    _flat_says(svc, False)                      # broker says we HOLD it
    await svc._v2_overnight_flatten()
    row = _row(sf)
    assert row.current_quantity == 100          # untouched
    assert row.status != "closed"
    assert (ACCT, SYM) in svc._managed_v2_symbols
    assert _sell_intents(sf) == []              # still cannot price a close


@pytest.mark.asyncio
async def test_an_unreadable_broker_is_not_a_flat_broker() -> None:
    """A raised/rate-limited position read must NOT clear protection. This is the 07-15 ERNA
    shape and the Webull 429 shape: 'I could not tell' is not 'you own nothing'."""
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf)
    _force_due(svc)
    _flat_says(svc, RuntimeError("positions endpoint down"))
    await svc._v2_overnight_flatten()
    row = _row(sf)
    assert row.current_quantity == 100          # kept
    assert row.status != "closed"


@pytest.mark.asyncio
async def test_a_real_bid_still_closes_normally() -> None:
    """No-regression: with a bid the sweep emits a close exactly as before and never consults
    the flat check at all."""
    sf = _make_sf()
    svc = _svc(sf)
    _arm(svc, sf)
    _force_due(svc)
    _flat_says(svc, True)                       # would reconcile — must NOT be reached
    _quote(svc, 9.80)
    await svc._v2_overnight_flatten()
    assert len(_sell_intents(sf)) == 1          # closed by SELLING, not by reconciling
