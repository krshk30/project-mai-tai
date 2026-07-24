"""v2 EOD OCO transition (Phase A, docs/premarket-eod-exit-design.md; decision A = KEEP MANAGING).

At 16:00 ET the native OCO exit legs expire with the RTH close (session=NORMAL + duration=DAY), so
for every OMS-managed v2 position still open the OMS releases the native-OCO stand-down for the rest
of the day and lets the software +2%/−5% EH-limit ladder own the exit. Asserts on STATE (the day-scoped
latch + the stand-down predicate flipping to False), never on log narration. Mirrors
test_v2_overnight_flatten.py. The transition places/cancels NO broker order — the RTH OCO auto-expires.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.base import Base
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


def _svc(sf, *, transition: bool = True, stand_down: bool = True) -> OmsRiskService:
    settings = Settings(
        oms_v2_exit_management_enabled=True,
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        oms_native_oco_stand_down_enabled=stand_down,
        oms_v2_eod_oco_transition_enabled=transition,
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


def _arm_managed(svc) -> None:
    """Register an OMS-managed v2 position with a FRESH broker-armed OCO confirmation, so the
    stand-down predicate is True until the transition releases it."""
    svc._managed_v2_symbols.add((ACCT, SYM))
    svc._native_oco_armed_confirmed_at[(ACCT, SYM)] = datetime.now(timezone.utc)


def _u(y, mo, d, h, mi):  # ET wall-clock -> tz-aware UTC (what the due-check consumes)
    return datetime(y, mo, d, h, mi, tzinfo=_ET).astimezone(timezone.utc)


def _force_due(svc, due: bool = True) -> None:
    svc._v2_eod_oco_transition_due = lambda now=None: due


# --- due-gate: pins the 16:00 threshold (mutate the default minute/hour => red) ---


def test_due_check_time_and_weekday():
    svc = _svc(_make_sf())
    assert svc._v2_eod_oco_transition_due(now=_u(2026, 7, 16, 16, 0)) is True    # Thu, 4:00 PM sharp
    assert svc._v2_eod_oco_transition_due(now=_u(2026, 7, 16, 16, 1)) is True    # Thu, after
    assert svc._v2_eod_oco_transition_due(now=_u(2026, 7, 16, 19, 55)) is True   # Thu, later
    assert svc._v2_eod_oco_transition_due(now=_u(2026, 7, 16, 15, 59)) is False  # Thu, 3:59 PM
    assert svc._v2_eod_oco_transition_due(now=_u(2026, 7, 18, 17, 0)) is False   # Saturday


def test_due_check_respects_settings():
    sf = _make_sf()
    svc = _svc(sf)
    svc.settings.oms_v2_eod_oco_transition_hour_et = 15
    svc.settings.oms_v2_eod_oco_transition_minute_et = 30
    assert svc._v2_eod_oco_transition_due(now=_u(2026, 7, 16, 15, 30)) is True
    assert svc._v2_eod_oco_transition_due(now=_u(2026, 7, 16, 15, 29)) is False


# --- the transition releases the stand-down for the day ---


@pytest.mark.asyncio
async def test_transition_releases_stand_down():
    sf = _make_sf()
    svc = _svc(sf)
    _arm_managed(svc)
    assert svc._native_oco_stand_down_active(ACCT, SYM) is True   # OCO armed => ladder deferred
    _force_due(svc)
    await svc._v2_eod_oco_transition()
    day = svc._session_day_et()
    assert (day, ACCT, SYM) in svc._v2_eod_oco_transitioned          # day-scoped latch set
    # Even if the broker sync re-arms the (expiring) OCO, the latch keeps the ladder running.
    svc._native_oco_armed_confirmed_at[(ACCT, SYM)] = datetime.now(timezone.utc)
    assert svc._native_oco_stand_down_active(ACCT, SYM) is False     # ladder now owns the exit


@pytest.mark.asyncio
async def test_transition_is_idempotent_per_day():
    sf = _make_sf()
    svc = _svc(sf)
    _arm_managed(svc)
    _force_due(svc)
    await svc._v2_eod_oco_transition()
    assert len(svc._v2_eod_oco_transitioned) == 1
    # A second sweep with the OCO re-armed must NOT re-process (fire once per position per day):
    # the already-latched key is skipped, so the freshly re-armed confirmation is left untouched.
    stamp = datetime.now(timezone.utc)
    svc._native_oco_armed_confirmed_at[(ACCT, SYM)] = stamp
    await svc._v2_eod_oco_transition()
    assert len(svc._v2_eod_oco_transitioned) == 1
    assert svc._native_oco_armed_confirmed_at.get((ACCT, SYM)) == stamp  # not re-popped


@pytest.mark.asyncio
async def test_flag_off_is_byte_identical():
    sf = _make_sf()
    svc = _svc(sf, transition=False)
    _arm_managed(svc)
    _force_due(svc)                       # even forced due, flag off => nothing happens
    await svc._v2_eod_oco_transition()
    assert svc._v2_eod_oco_transitioned == set()
    assert svc._native_oco_stand_down_active(ACCT, SYM) is True   # stand-down untouched


@pytest.mark.asyncio
async def test_not_due_no_transition():
    sf = _make_sf()
    svc = _svc(sf)
    _arm_managed(svc)
    _force_due(svc, due=False)            # before 16:00
    await svc._v2_eod_oco_transition()
    assert svc._v2_eod_oco_transitioned == set()
    assert svc._native_oco_stand_down_active(ACCT, SYM) is True


def test_stand_down_short_circuit_is_day_scoped():
    """A latch entry for a DIFFERENT day must not release today's stand-down (proves the
    session_day is part of the key — mutate the key to drop the day and this turns red)."""
    sf = _make_sf()
    svc = _svc(sf)
    _arm_managed(svc)
    yesterday = (datetime.now(_ET) - timedelta(days=1)).strftime("%Y-%m-%d")
    svc._v2_eod_oco_transitioned.add((yesterday, ACCT, SYM))
    assert svc._native_oco_stand_down_active(ACCT, SYM) is True   # yesterday's latch is inert today
