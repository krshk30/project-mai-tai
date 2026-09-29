"""At 16:00, the current row's broker exit legs must be confirmed gone before software sells."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.broker_adapters.protocols import ExitPairReleaseResult
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder, OmsManagedPosition, SystemIncident, TradeIntent
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from tests.unit.test_confirmation_exit_fanout import (
    SYMBOL as WEBULL_SYMBOL,
    WEBULL,
    _FanoutAdapter,
    _service as _webull_service,
)

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


def _svc(
    sf, *, transition: bool = True, stand_down: bool = True, floor: bool = False
) -> OmsRiskService:
    settings = Settings(
        oms_v2_exit_management_enabled=True,
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        oms_native_oco_stand_down_enabled=stand_down,
        oms_v2_eod_oco_transition_enabled=transition,
        oms_v2_cw_floor_exit_enabled=floor,
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


def _confirmed_release(svc) -> None:
    async def release(*_args, **_kwargs):
        return "released"

    svc._release_native_oco_for_cw_flip = release


def _open_apus_row(sf, *, symbol: str = SYM) -> None:
    with sf() as session:
        session.add(
            OmsManagedPosition(
                strategy_code="schwab_1m_v2",
                broker_account_name=ACCT,
                symbol=symbol,
                entry_price=Decimal("5.11"),
                original_quantity=2,
                current_quantity=2,
                entry_path="ATR Flip",
                entry_time=_u(2026, 9, 24, 15, 54),
                status="open",
                config_name="make_v2_variant",
            )
        )
        session.commit()


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
    _open_apus_row(sf)
    _arm_managed(svc)
    _confirmed_release(svc)
    assert svc._native_oco_stand_down_active(ACCT, SYM) is True   # OCO armed => ladder deferred
    _force_due(svc)
    await svc._v2_eod_oco_transition()
    day = svc._session_day_et()
    assert (day, ACCT, SYM) in svc._v2_eod_oco_transitioned          # day-scoped latch set
    # Even if the broker sync re-arms the (expiring) OCO, the latch keeps the ladder running.
    svc._native_oco_armed_confirmed_at[(ACCT, SYM)] = datetime.now(timezone.utc)
    assert svc._native_oco_stand_down_active(ACCT, SYM) is False     # ladder now owns the exit


@pytest.mark.asyncio
async def test_working_leg_blocks_1600_but_1955_submits_flatten():
    sf = _make_sf()
    svc = _svc(sf)
    svc.logger = __import__("logging").getLogger("card10-eod-test")
    _open_apus_row(sf)
    _arm_managed(svc)
    _force_due(svc)
    svc.settings.oms_v2_overnight_flatten_enabled = True
    svc._v2_overnight_flatten_due = lambda now=None: True
    svc._latest_quotes_by_symbol[SYM] = {
        "bid": 5.50, "ask": 5.51, "received_at": datetime.now(timezone.utc),
    }
    releases = ["reserved", "released"]

    async def release(*_args, **_kwargs):
        return releases.pop(0) if releases else "released"

    svc._release_native_oco_for_cw_flip = release
    await svc._v2_eod_oco_transition()
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    with sf() as session:
        assert session.scalars(select(TradeIntent).where(TradeIntent.side == "sell")).all() == []
    await svc._v2_overnight_flatten()
    with sf() as session:
        intents = session.scalars(select(TradeIntent).where(TradeIntent.side == "sell")).all()
        assert len(intents) == 1
        assert intents[0].reason == "V2_OVERNIGHT_FLATTEN"
        incidents = session.scalars(select(SystemIncident)).all()
        assert {i.payload["source"] for i in incidents} == {"oms_v2_exit_release_unresolved"}
    assert (svc._session_day_et(), ACCT, SYM) not in svc._v2_eod_oco_transitioned

    with sf() as session:
        row = session.scalar(select(OmsManagedPosition).where(OmsManagedPosition.symbol == SYM))
        assert row.status == "closed"


@pytest.mark.asyncio
async def test_missing_exit_pair_handle_is_unknown_not_permission_to_sell():
    sf = _make_sf()
    svc = _svc(sf)
    _open_apus_row(sf)
    _arm_managed(svc)
    _force_due(svc)
    svc._latest_quotes_by_symbol[SYM] = {
        "bid": 5.50, "ask": 5.51, "received_at": datetime.now(timezone.utc),
    }

    await svc._v2_eod_oco_transition()
    await svc._evaluate_v2_managed_exit(ACCT, SYM)

    with sf() as session:
        assert session.scalars(select(TradeIntent).where(TradeIntent.side == "sell")).all() == []
        incident = session.scalars(select(SystemIncident)).one()
        assert incident.status == "open"
        assert incident.payload["risk_state"] == "protection_unknown"


@pytest.mark.asyncio
async def test_direct_exit_emitter_cannot_bypass_unconfirmed_handover():
    sf = _make_sf()
    svc = _svc(sf)
    _open_apus_row(sf)
    _arm_managed(svc)
    _force_due(svc)

    with sf() as session:
        snapshot = svc._read_v2_managed_snapshot(session, ACCT, SYM, True)
    assert snapshot is not None
    position = svc._hydrate_v2_position(snapshot)
    result = await svc._emit_v2_exit_on_loop(
        ACCT, SYM, position, snapshot.entry_price, kind="HARD",
        reference_price=5.50, reason="oms_v2_managed_exit:CW_TARGET",
        bid=5.50, close_on_fill=True,
    )

    assert result == "eod_handover_unconfirmed"
    with sf() as session:
        assert session.scalars(select(TradeIntent).where(TradeIntent.side == "sell")).all() == []


@pytest.mark.asyncio
async def test_webull_handover_requires_strict_second_broker_read():
    adapter = _FanoutAdapter()
    svc, sf = _webull_service(fanout=True, adapter=adapter)
    svc.settings.oms_v2_eod_oco_transition_enabled = True
    svc._v2_eod_oco_transition_due = lambda now=None: True
    svc._managed_v2_symbols = {(WEBULL, WEBULL_SYMBOL)}
    svc._webull_protect_base[(WEBULL, WEBULL_SYMBOL)] = "known-protect-base"
    reads = ["unanswerable", "released"]

    async def strict_read(**_kwargs):
        return ExitPairReleaseResult(outcome=reads.pop(0))

    adapter.confirm_exit_pair_terminal = strict_read
    await svc._v2_eod_oco_transition()
    assert len(adapter.cancel_pair_calls) == 1
    assert (svc._session_day_et(), WEBULL, WEBULL_SYMBOL) not in svc._v2_eod_oco_transitioned
    with sf() as session:
        incident = session.scalars(select(SystemIncident)).one()
        assert incident.status == "open"

    svc.__dict__.get("_v2_eod_oco_last_try", {}).clear()
    await svc._v2_eod_oco_transition()
    assert len(adapter.cancel_pair_calls) == 2
    assert (svc._session_day_et(), WEBULL, WEBULL_SYMBOL) in svc._v2_eod_oco_transitioned


@pytest.mark.asyncio
async def test_handover_for_old_row_cannot_unlock_replacement_position():
    sf = _make_sf()
    svc = _svc(sf)
    _open_apus_row(sf)
    _arm_managed(svc)
    _force_due(svc)
    with sf() as session:
        old_row = svc.store.get_open_managed_position(
            session, broker_account_name=ACCT, symbol=SYM
        )
        assert old_row is not None
        old_id = str(old_row.id)

    async def release(*_args, **_kwargs):
        with sf() as session:
            row = svc.store.get_open_managed_position(
                session, broker_account_name=ACCT, symbol=SYM
            )
            assert row is not None
            # The table has a unique (account,symbol) key, so represent the replacement
            # episode by changing the row UUID during the broker await.
            row.id = uuid4()
            row.entry_price = Decimal("6.00")
            session.commit()
        return "released"

    svc._release_native_oco_for_cw_flip = release
    await svc._v2_eod_oco_transition()

    key = (svc._session_day_et(), ACCT, SYM)
    assert key not in svc._v2_eod_oco_transitioned
    assert svc._v2_eod_oco_transition_rows.get(key) is None
    with sf() as session:
        current = svc.store.get_open_managed_position(
            session, broker_account_name=ACCT, symbol=SYM
        )
        assert current is not None and str(current.id) != old_id


@pytest.mark.asyncio
async def test_recent_155950_oco_resolution_is_not_released_at_1600():
    sf = _make_sf()
    svc = _svc(sf)
    _open_apus_row(sf)
    _arm_managed(svc)
    _confirmed_release(svc)
    svc._native_oco_resolving[(ACCT, SYM)] = datetime.now(timezone.utc) - timedelta(
        seconds=10
    )
    svc._native_oco_armed_confirmed_at.pop((ACCT, SYM), None)
    _force_due(svc)

    await svc._v2_eod_oco_transition()

    assert (ACCT, SYM) in svc._native_oco_resolving
    assert (svc._session_day_et(), ACCT, SYM) not in svc._v2_eod_oco_transitioned
    assert svc._native_oco_stand_down_active(ACCT, SYM) is True

    svc._native_oco_resolving[(ACCT, SYM)] -= timedelta(seconds=91)
    await svc._v2_eod_oco_transition()
    assert (svc._session_day_et(), ACCT, SYM) in svc._v2_eod_oco_transitioned


@pytest.mark.asyncio
async def test_apus_peak_during_stand_down_arms_floor_at_handover():
    sf = _make_sf()
    svc = _svc(sf, floor=True)
    _open_apus_row(sf)
    _arm_managed(svc)
    _confirmed_release(svc)
    _force_due(svc, False)
    svc._latest_quotes_by_symbol[SYM] = {
        "bid": 5.48,
        "received_at": datetime.now(timezone.utc),
    }

    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    _force_due(svc)
    await svc._v2_eod_oco_transition()

    assert (ACCT, SYM) in svc._cw_floor_armed
    with sf() as session:
        row = svc.store.get_open_managed_position(
            session, broker_account_name=ACCT, symbol=SYM
        )
        assert row is not None and row.floor_price is not None
        assert row.floor_price >= Decimal("5.2122")
        assert session.scalars(select(BrokerOrder)).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bid", "quote_age_seconds"),
    [(5.15, 0), (5.48, 10)],
)
async def test_handover_does_not_arm_floor_without_fresh_target_cross(
    bid: float, quote_age_seconds: int
):
    sf = _make_sf()
    svc = _svc(sf, floor=True)
    _open_apus_row(sf)
    _arm_managed(svc)
    _confirmed_release(svc)
    _force_due(svc, False)
    svc._latest_quotes_by_symbol[SYM] = {
        "bid": bid,
        "received_at": datetime.now(timezone.utc) - timedelta(seconds=quote_age_seconds),
    }

    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    _force_due(svc)
    await svc._v2_eod_oco_transition()

    assert (ACCT, SYM) not in svc._cw_floor_armed
    with sf() as session:
        row = svc.store.get_open_managed_position(
            session, broker_account_name=ACCT, symbol=SYM
        )
        assert row is not None and row.floor_price is None


@pytest.mark.asyncio
async def test_resolving_grace_peak_arms_floor_when_handover_follows():
    sf = _make_sf()
    svc = _svc(sf, floor=True)
    _open_apus_row(sf)
    _arm_managed(svc)
    _confirmed_release(svc)
    _force_due(svc, False)
    # The 16:00:05 sync moves the DAY bracket into resolving before the 16:00:44 quote.
    svc._native_oco_armed_confirmed_at.pop((ACCT, SYM))
    svc._native_oco_resolving[(ACCT, SYM)] = datetime.now(timezone.utc) - timedelta(seconds=39)
    assert svc._native_oco_stand_down_active(ACCT, SYM) is True
    svc._latest_quotes_by_symbol[SYM] = {
        "bid": 5.48,
        "received_at": datetime.now(timezone.utc),
    }

    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert svc._v2_native_oco_high_bid[(ACCT, SYM)] == 5.48
    # At 16:01:35 the grace has expired, so the held share transfers to the EH ladder.
    svc._native_oco_resolving[(ACCT, SYM)] -= timedelta(seconds=91)
    _force_due(svc)
    await svc._v2_eod_oco_transition()

    assert (ACCT, SYM) in svc._cw_floor_armed
    with sf() as session:
        row = svc.store.get_open_managed_position(
            session, broker_account_name=ACCT, symbol=SYM
        )
        assert row is not None and row.floor_price is not None
        assert row.floor_price >= Decimal("5.2122")


@pytest.mark.asyncio
async def test_transition_is_idempotent_per_day():
    sf = _make_sf()
    svc = _svc(sf)
    _open_apus_row(sf)
    _arm_managed(svc)
    _confirmed_release(svc)
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
