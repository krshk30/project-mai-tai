"""OMS restart-while-holding safety (F2) — dual-broker.

Proves the invariant: an OMS-OWNED position is never naked/dropped across an OMS restart,
AND a manual (no-provenance) broker holding is never touched.

- v2/Schwab already survived via the managed-row rehydrate (confirmed here).
- ORB/Webull was the real gap: its stop lived only in the in-memory `_armed_hard_stops`
  dict, never rebuilt on boot -> naked across a restart. F2 adds the durable
  `oms_armed_stops` mirror + boot rehydrate + protected-before-serving reconcile.

Scoping invariant (code-enforced): OMS-ownership is defined by the per-strategy
`virtual_positions` ledger. A manual holding has no such row -> invisible to the reconcile
-> never rehydrated, armed, sold, or flagged. The only alert-worthy mismatch is the INVERSE
(an OMS-owned record whose position is missing/short at the broker, or unprotected).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import AccountPosition, OmsManagedPosition, VirtualPosition
from project_mai_tai.oms.service import ArmedHardStop, OmsRiskService
from project_mai_tai.oms.store import OmsStore

ORB_ACCT = "live:orb"
ORB_CODE = "orb"
V2_ACCT = "live:schwab_1m_v2"
V2_CODE = "schwab_1m_v2"


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},  # _run_db uses a worker thread
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


async def _anoop(*a, **k):  # stub for sync_broker_positions (account_positions pre-seeded)
    return {}


def _svc(sf: sessionmaker[Session]) -> OmsRiskService:
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.logger = SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, exception=lambda *a, **k: None, debug=lambda *a, **k: None,
    )
    svc.session_factory = sf
    svc.store = OmsStore()
    svc.settings = SimpleNamespace(
        strategy_schwab_1m_v2_account_name=V2_ACCT, oms_v2_exit_management_enabled=True,
    )
    svc._armed_stop_persistence_enabled = True
    svc._armed_stop_dirty = set()
    svc._armed_hard_stops = {}
    svc._managed_v2_symbols = set()
    svc._latest_quotes_by_symbol = {}
    svc._latest_trades_by_symbol = {}
    svc._boot_protection_alerts = 0
    svc.sync_broker_positions = _anoop  # broker truth is the pre-seeded account_positions
    return svc


def _armed_row(svc, session, *, code, acct, symbol, qty, entry, stop_price, trail_pct, hwm):
    svc.store.upsert_armed_stop(
        session, strategy_code=code, broker_account_name=acct, symbol=symbol,
        quantity=Decimal(str(qty)), entry_price=Decimal(str(entry)), stop_loss_pct=8.0,
        stop_price=Decimal(str(stop_price)), quote_max_age_ms=2000, initial_panic_buffer_pct=1.5,
        trail_pct=trail_pct, high_water_mark=(Decimal(str(hwm)) if hwm is not None else None),
        close_in_flight=False,
    )
    session.commit()


def _owned(svc, session, *, code, acct, symbol, provider, virtual_qty, broker_qty):
    """Seed an OMS-owned position: a per-strategy virtual_positions row (= ownership) and,
    optionally, the broker-truth account_positions row."""
    strat = svc.store.ensure_strategy(session, code)
    acc = svc.store.ensure_broker_account(session, acct, provider=provider, environment="live")
    session.add(VirtualPosition(
        strategy_id=strat.id, broker_account_id=acc.id, symbol=symbol,
        quantity=Decimal(str(virtual_qty)), average_price=Decimal("1"),
    ))
    if broker_qty is not None:
        session.add(AccountPosition(
            broker_account_id=acc.id, symbol=symbol,
            quantity=Decimal(str(broker_qty)), average_price=Decimal("1"),
        ))
    session.commit()


# --------------------------------------------------------------------------- #
# T-ORB-REHYDRATE — ORB's own stop survives the restart (full fidelity)
# --------------------------------------------------------------------------- #
def test_torb_rehydrate_restores_the_ratcheted_orb_stop():
    sf = _session_factory()
    with sf() as s:
        _armed_row(_svc(sf), s, code=ORB_CODE, acct=ORB_ACCT, symbol="AZI",
                   qty=5, entry="1.20", stop_price="1.35", trail_pct=8.0, hwm="1.47")
    svc = _svc(sf)
    assert svc._armed_hard_stops == {}  # fresh boot: process memory empty
    asyncio.run(svc._rehydrate_armed_hard_stops())
    key = svc._hard_stop_key(ORB_CODE, ORB_ACCT, "AZI")
    assert key in svc._armed_hard_stops, "ORB armed stop was NOT rehydrated across restart"
    stop = svc._armed_hard_stops[key]
    # Full fidelity: the RATCHETED stop_price (1.35) + high-water-mark (1.47) are restored,
    # NOT a looser entry+trail% re-derive (which would be 1.20*(1-8%) = 1.104).
    assert stop.stop_price == Decimal("1.35")
    assert stop.high_water_mark == Decimal("1.47")
    assert stop.quantity == Decimal("5") and stop.trail_pct == 8.0


# --------------------------------------------------------------------------- #
# Mirror round-trip — arm -> flush -> persisted -> rehydrate -> pop -> deleted
# --------------------------------------------------------------------------- #
def test_mirror_roundtrip_arm_flush_rehydrate_and_pop_delete():
    sf = _session_factory()
    svc = _svc(sf)
    key = svc._hard_stop_key(ORB_CODE, ORB_ACCT, "IVF")
    svc._armed_hard_stops[key] = ArmedHardStop(
        strategy_code=ORB_CODE, broker_account_name=ORB_ACCT, symbol="IVF",
        quantity=Decimal("5"), entry_price=Decimal("1.85"), stop_loss_pct=8.0,
        stop_price=Decimal("1.70"), quote_max_age_ms=2000, initial_panic_buffer_pct=1.5,
        trail_pct=8.0, high_water_mark=Decimal("1.90"),
    )
    svc._armed_stop_dirty.add(key)
    asyncio.run(svc._flush_dirty_armed_stops())
    with sf() as s:
        rows = svc.store.list_armed_stops(s)
        assert len(rows) == 1 and rows[0].symbol == "IVF"
        assert Decimal(str(rows[0].stop_price)) == Decimal("1.70")

    # A fresh process rehydrates the persisted state.
    svc2 = _svc(sf)
    asyncio.run(svc2._rehydrate_armed_hard_stops())
    assert key in svc2._armed_hard_stops
    assert svc2._armed_hard_stops[key].high_water_mark == Decimal("1.90")

    # Popping (position closed) + flush deletes the mirror row.
    svc2._armed_hard_stops.pop(key)
    svc2._armed_stop_dirty.add(key)
    asyncio.run(svc2._flush_dirty_armed_stops())
    with sf() as s:
        assert svc2.store.list_armed_stops(s) == []


# --------------------------------------------------------------------------- #
# T-MANUAL-IGNORED — a no-provenance broker holding is NEVER touched (invariant)
# --------------------------------------------------------------------------- #
def test_tmanual_ignored_no_provenance_holding_untouched_across_restart():
    sf = _session_factory()
    svc = _svc(sf)
    with sf() as s:  # a MANUAL Webull holding: account_position only, NO virtual_positions row
        acc = svc.store.ensure_broker_account(s, ORB_ACCT, provider="webull", environment="live")
        s.add(AccountPosition(
            broker_account_id=acc.id, symbol="FCUV",
            quantity=Decimal("400"), average_price=Decimal("6.87"),
        ))
        s.commit()
    asyncio.run(svc._rehydrate_armed_hard_stops())
    asyncio.run(svc._reconcile_protection_before_serving())
    # The OMS did NOTHING to the manual holding: no stop armed, no alert, no mirror row.
    assert svc._armed_hard_stops == {}
    assert svc._boot_protection_alerts == 0
    with sf() as s:
        assert svc.store.list_armed_stops(s) == []


# --------------------------------------------------------------------------- #
# T-ORB-LOST-RECORD — an OMS-owned position with no rehydrated stop is alerted
# --------------------------------------------------------------------------- #
def test_torb_lost_record_owned_position_without_stop_is_alerted():
    sf = _session_factory()
    svc = _svc(sf)
    with sf() as s:  # OMS-owned (virtual qty) + broker holds it, but NO armed-stop mirror row
        _owned(svc, s, code=ORB_CODE, acct=ORB_ACCT, symbol="DSY",
               provider="webull", virtual_qty=5, broker_qty=5)
    asyncio.run(svc._rehydrate_armed_hard_stops())  # nothing to rehydrate
    asyncio.run(svc._reconcile_protection_before_serving())
    assert svc._boot_protection_alerts >= 1, "a NAKED OMS-owned position was NOT detected"
    # The manual invariant still holds — we did not fabricate a stop for it.
    assert svc._hard_stop_key(ORB_CODE, ORB_ACCT, "DSY") not in svc._armed_hard_stops


# --------------------------------------------------------------------------- #
# T-BROKER-FLAT-CLOSE — an OMS-owned position gone at the broker is reconciled/alerted
# --------------------------------------------------------------------------- #
def test_tbroker_flat_close_owned_position_missing_at_broker_is_alerted():
    sf = _session_factory()
    svc = _svc(sf)
    with sf() as s:
        _owned(svc, s, code=ORB_CODE, acct=ORB_ACCT, symbol="CANF",
               provider="webull", virtual_qty=5, broker_qty=0)  # broker FLAT
        _armed_row(svc, s, code=ORB_CODE, acct=ORB_ACCT, symbol="CANF",
                   qty=5, entry="4.00", stop_price="3.90", trail_pct=0.0, hwm=None)
    asyncio.run(svc._rehydrate_armed_hard_stops())  # stop rehydrates (protected)...
    asyncio.run(svc._reconcile_protection_before_serving())
    # ...but the position is gone at the broker -> VANISHED mismatch alerted.
    assert svc._boot_protection_alerts >= 1


# --------------------------------------------------------------------------- #
# T-V2-REHYDRATE — v2/Schwab managed row still survives a restart (regression)
# --------------------------------------------------------------------------- #
def test_tv2_rehydrate_managed_row_still_survives():
    sf = _session_factory()
    svc = _svc(sf)
    with sf() as s:
        s.add(OmsManagedPosition(
            strategy_code=V2_CODE, broker_account_name=V2_ACCT, symbol="LNAI",
            entry_price=Decimal("4.20"), original_quantity=10, current_quantity=10,
            status="open",
        ))
        s.commit()
    svc._rehydrate_managed_v2_symbols()  # the existing v2 rehydrate path
    assert (V2_ACCT, "LNAI") in svc._managed_v2_symbols, "v2 managed position did NOT survive restart"
