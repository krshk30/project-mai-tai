"""Pin: a v2 position OPENED IN PRE-MARKET stays CW-ladder-managed across the 09:30 RTH open.

Operator decision (2026-07-24, option 1 — "keep on the CW ladder, do NOT convert to OCO"): a v2
position that opens in extended hours has NO broker-native OCO — `_apply_v2_oco_bracket_entry` skips
when `not _is_regular_market_session()` (service.py, and see test_oms_v2_oco_emit.py::
test_emit_skipped_outside_regular_hours). Across the 09:30 open the software CW +2%/−5% ladder owns
the exit CONTINUOUSLY, and NO OCO is emitted at the open for the already-held position. This is the
clean mirror of the Phase A 16:00 EOD OCO→ladder transition (test_v2_eod_oco_transition.py); the
operator wants it KEPT AS-IS, made explicit and regression-proof.

The mechanism is `_native_oco_stand_down_active` FAILING OPEN: with no confirmed broker bracket it
returns False, so `_evaluate_v2_managed_exit` runs the ladder (never stood down). The ladder is
session-aware — pre-09:30 it EH-routes the exit (LIMIT + session=AM off the live bid, #390); in RTH
it uses the normal MARKET exit.

Asserts on STATE + the emitted INTENT METADATA (the live route), never on log narration. Mirrors the
harness of test_v2_managed_exit.py + test_v2_eod_oco_transition.py.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import OmsManagedPosition, TradeIntent
from project_mai_tai.oms.service import OmsRiskService, _panic_limit_price
from project_mai_tai.settings import Settings

_ET = ZoneInfo("America/New_York")
ACCT = "paper:schwab_1m_v2"
SYM = "VSME"


# NOTE: no `_market_is_fillable` monkeypatch — every emit test here calls
# `_evaluate_v2_managed_exit` DIRECTLY, which does not consult the fillable gate (that gate lives
# upstream in `_handle_quote_tick_event`). Keeping the real method un-patched lets
# `test_position_is_fillable_continuously_across_the_open` assert the genuine 7 AM–8 PM ET window.


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


def _svc(sf) -> OmsRiskService:
    """A v2 service configured exactly as it runs live for this case: the CW +2%/−5% ladder ON,
    the native-OCO stand-down ON, AND the native-OCO bracket EMIT ON — so the OCO-emit path is
    fully live and the tests prove it is NOT triggered for a held position at the open (rather than
    vacuously passing because the emit is disabled)."""
    settings = Settings(
        oms_v2_exit_management_enabled=True,
        strategy_schwab_1m_v2_confirmed_window_enabled=True,   # CW +2%/−5% ladder
        oms_native_oco_stand_down_enabled=True,
        oms_v2_emit_native_oco_bracket_enabled=True,           # OCO-emit path LIVE (entry-only)
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


def _arm_premarket_no_oco(svc, sf, *, entry=10.0, qty=100, **rowkw) -> None:
    """A managed v2 position opened in pre-market: it is in `_managed_v2_symbols` but has NO
    native-OCO confirmation (the EH entry never placed a bracket). Deliberately does NOT touch
    `_native_oco_armed_confirmed_at` — that emptiness is what makes the stand-down fail open."""
    with sf() as s:
        row = svc.store.create_managed_position(
            s, strategy_code="schwab_1m_v2", broker_account_name=ACCT,
            symbol=SYM, entry_price=Decimal(str(entry)), quantity=qty, entry_path="MACD Cross",
        )
        for k, v in rowkw.items():
            setattr(row, k, v)
        s.flush()
        s.commit()
    svc._managed_v2_symbols.add((ACCT, SYM))
    # invariant of THIS fixture: no armed OCO for the position (the whole point of the pin)
    assert (ACCT, SYM) not in svc._native_oco_armed_confirmed_at


def _quote(svc, bid: float) -> None:
    svc._latest_quotes_by_symbol[SYM] = {
        "bid": bid, "ask": bid + 0.01, "received_at": datetime.now(UTC),
    }


def _row(sf) -> OmsManagedPosition | None:
    with sf() as s:
        return s.scalar(select(OmsManagedPosition).where(OmsManagedPosition.symbol == SYM))


def _intents(sf) -> list[TradeIntent]:
    with sf() as s:
        return list(s.scalars(select(TradeIntent).where(TradeIntent.symbol == SYM)).all())


def _sell_intents(sf) -> list[TradeIntent]:
    return [i for i in _intents(sf) if i.side == "sell"]


def _meta(intent: TradeIntent) -> dict:
    return intent.payload["metadata"]


def _force_session(monkeypatch, value: str | None) -> None:
    """Pin the wall-clock the exit routing keys off: value=None => RTH; "AM" => pre-market."""
    monkeypatch.setattr(
        "project_mai_tai.oms.service._extended_hours_session", lambda now=None: value
    )


# --------------------------------------------------------------------------- (1)
# The load-bearing mechanism: with NO confirmed broker OCO, the stand-down FAILS OPEN in BOTH an
# EH and an RTH wall-clock, so the ladder is never stood down as the position crosses 09:30.

@pytest.mark.parametrize("session", [None, "AM"], ids=["rth", "premarket"])
def test_stand_down_fails_open_for_no_oco_position_across_the_open(monkeypatch, session) -> None:
    sf = _make_sf()
    svc = _svc(sf)
    _arm_premarket_no_oco(svc, sf)
    _force_session(monkeypatch, session)
    # No armed OCO confirmation => fail-open => NOT stood down, in either session.
    assert svc._native_oco_stand_down_active(ACCT, SYM) is False


# --------------------------------------------------------------------------- (2)
# Pre-market (before 09:30): the CW hard stop (−5%) fires an EH-ROUTED LIMIT exit off the live bid.
# Proves the position is managed (not naked) while it is still pre-market.

@pytest.mark.asyncio
async def test_premarket_no_oco_position_eh_ladder_emits_limit_exit(monkeypatch) -> None:
    _force_session(monkeypatch, "AM")                 # pre-market wall-clock
    sf = _make_sf()
    svc = _svc(sf)
    _arm_premarket_no_oco(svc, sf, entry=10.0, qty=100)

    _quote(svc, bid=9.40)                             # below CW stop 9.50 = 10*(1−5%)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    sells = _sell_intents(sf)
    assert len(sells) == 1
    m = _meta(sells[0])
    assert m["oms_v2_managed_exit"] == "true"
    assert m["order_type"] == "limit"                 # EH → LIMIT, not an unfillable MARKET
    assert m["session"] == "AM" and m["extended_hours"] == "true"
    assert m["limit_price"] == _panic_limit_price(9.40, 0.5)   # buffered marketable off the bid
    assert m["reference_price"] == "9.5000"           # CW −5% leg LEVEL, preserved
    assert sells[0].reason.endswith("CW_HARD_STOP")


# --------------------------------------------------------------------------- (3)
# After 09:30 (RTH): the SAME no-OCO position, same CW ladder, now uses the NORMAL MARKET exit.
# Together with (2) this is the continuity across the open: EH-limit → RTH-market, no gap.

@pytest.mark.asyncio
async def test_rth_no_oco_position_ladder_emits_market_exit(monkeypatch) -> None:
    _force_session(monkeypatch, None)                 # RTH wall-clock
    sf = _make_sf()
    svc = _svc(sf)
    _arm_premarket_no_oco(svc, sf, entry=10.0, qty=100)

    _quote(svc, bid=9.40)                             # CW hard stop
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    sells = _sell_intents(sf)
    assert len(sells) == 1
    m = _meta(sells[0])
    assert m["order_type"] == "market"                # RTH → normal MARKET exit
    assert "session" not in m and "limit_price" not in m
    assert m["reference_price"] == "9.5000"
    assert sells[0].reason.endswith("CW_HARD_STOP")


# --------------------------------------------------------------------------- (4)
# The +2% target side of the ladder is live for the no-OCO position too (managed, not naked).

@pytest.mark.asyncio
async def test_premarket_no_oco_position_ladder_takes_the_plus2_target(monkeypatch) -> None:
    _force_session(monkeypatch, "AM")
    sf = _make_sf()
    svc = _svc(sf)
    _arm_premarket_no_oco(svc, sf, entry=10.0, qty=100)

    _quote(svc, bid=10.20)                            # +2% CW target
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    sells = _sell_intents(sf)
    assert len(sells) == 1
    assert sells[0].reason.endswith("CW_TARGET")
    assert _meta(sells[0])["reference_price"] == "10.2000"   # +2% leg level


# --------------------------------------------------------------------------- (5)
# NO OCO is emitted for the HELD position at/after 09:30. The OCO-emit path
# (`_apply_v2_oco_bracket_entry`) is ENTRY-ONLY (called only on a buy-open intent, service.py ~907;
# entry coverage is test_oms_v2_oco_emit.py). Driving quotes across the open for a held position
# must never produce a buy-open / bracketed intent — only the managed SELL.

@pytest.mark.asyncio
async def test_no_oco_emitted_for_held_position_across_the_open(monkeypatch) -> None:
    _force_session(monkeypatch, None)                 # at/after the 09:30 open
    sf = _make_sf()
    svc = _svc(sf)                                    # OCO-emit flag is ON in _svc
    _arm_premarket_no_oco(svc, sf, entry=10.0, qty=100)

    _quote(svc, bid=9.40)                             # triggers the exit
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    all_intents = _intents(sf)
    # exactly one intent, and it is the managed SELL — never a buy-open re-entry / OCO bracket
    assert len(all_intents) == 1
    only = all_intents[0]
    assert only.side == "sell" and only.intent_type == "close"
    assert "bracket" not in _meta(only)
    assert "native_oco_bracket" not in _meta(only)
    assert not any(i.side == "buy" or i.intent_type == "open" for i in all_intents)


# --------------------------------------------------------------------------- (6)
# A held position that has NOT hit a threshold at the open emits NOTHING at all — no OCO, no exit,
# just persisted ladder state. Pins that the 09:30 open is not itself an OCO-emit / re-entry event.

@pytest.mark.asyncio
async def test_no_threshold_at_open_emits_nothing(monkeypatch) -> None:
    _force_session(monkeypatch, None)
    sf = _make_sf()
    svc = _svc(sf)
    _arm_premarket_no_oco(svc, sf, entry=10.0, qty=100)

    _quote(svc, bid=10.05)                            # +0.5%: no target (<+2%), no stop (>−5%)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    assert _intents(sf) == []                         # nothing emitted at the open
    assert _row(sf).status == "open"                  # still held + managed
    assert (ACCT, SYM) in svc._managed_v2_symbols


# --------------------------------------------------------------------------- (7) GUARD / MUTATION
# Fail-open is the whole safety. If a regression made the stand-down WRONGLY stand down a no-OCO
# position (return True), the ladder would go SILENT and the position would be naked across the
# open. Pin both directions on the SAME −5% quote: forced-True => silent; real predicate (fail-open,
# no OCO armed) => emits. A break of fail-open flips the second assert red.

@pytest.mark.asyncio
async def test_wrong_stand_down_silences_the_ladder_fail_open_keeps_it_alive(monkeypatch) -> None:
    _force_session(monkeypatch, None)
    sf = _make_sf()
    svc = _svc(sf)
    _arm_premarket_no_oco(svc, sf, entry=10.0, qty=100)
    _quote(svc, bid=9.40)                             # a genuine CW hard stop

    # (a) the regression shape: stand-down wrongly True => the ladder stands down => SILENCE.
    monkeypatch.setattr(svc, "_native_oco_stand_down_active", lambda acct, sym: True)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert _sell_intents(sf) == []                    # naked — this is the failure we guard against
    assert _row(sf).status == "open"

    # (b) the real predicate: no OCO armed => fail-open False => the ladder runs and exits.
    monkeypatch.undo()
    _force_session(monkeypatch, None)
    assert svc._native_oco_stand_down_active(ACCT, SYM) is False
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert len(_sell_intents(sf)) == 1                # managed after all — fail-open saved it


# --------------------------------------------------------------------------- (8) CONTINUITY
# The tick consumer only reaches `_evaluate_v2_managed_exit` while `_market_is_fillable`. The
# default fillable window is 7 AM–8 PM ET, so a pre-market-opened position is evaluated
# CONTINUOUSLY on BOTH sides of 09:30 — there is no un-managed gap at the open. (This test does not
# force the market open — it asserts the real gate at explicit ET wall-clocks on a weekday.)

def test_position_is_fillable_continuously_across_the_open() -> None:
    sf = _make_sf()
    svc = _svc(sf)
    thursday = lambda h, mi: datetime(2026, 7, 23, h, mi, tzinfo=_ET).astimezone(UTC)  # noqa: E731
    assert svc._market_is_fillable(now=thursday(9, 29)) is True   # 1 min before the open
    assert svc._market_is_fillable(now=thursday(9, 31)) is True   # 1 min after the open
    assert svc._market_is_fillable(now=thursday(8, 0)) is True    # pre-market (managed)
    assert svc._market_is_fillable(now=thursday(15, 0)) is True   # RTH (managed)
