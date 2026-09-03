"""16:01 cancel-and-reexit: cancel our OWN working SELL legs, CONFIRM zero, then PM-limit exit.

Every test here asserts a REFUSAL or a STATE, never log narration, and each one was checked to go
RED with the guard it pins removed -- a test that cannot come out false is not coverage.

⛔ The properties that are load-bearing, in the order they matter:
  1. ONCE PER POSITION PER DAY, not once per tick (the 220-in-14-minutes shape).
  2. A refused DELETE stops everything -- no retry, no PM exit, legs left alone.
  3. NO PM exit without an INDEPENDENT confirm read reporting zero working legs.
  4. If the PM exit does not go out AFTER we cancelled, protection is restored -- ONCE.
  5. The PM exit goes through `_emit_v2_exit_on_loop`, never a direct POST.
"""
from __future__ import annotations

from datetime import datetime, timezone
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


class _LegAdapter(SimulatedBrokerAdapter):
    """Adapter whose leg reads and cancels are scripted per call."""

    def __init__(self, harvests, cancel_result=None, raise_on_harvest=False):
        super().__init__()
        self._harvests = list(harvests)      # one entry per fetch call
        self._cancel_result = cancel_result
        self._raise_on_harvest = raise_on_harvest
        self.harvest_calls = 0
        self.cancel_calls: list[list[str]] = []

    async def fetch_working_exit_leg_ids(self, broker_account_name, symbols):
        self.harvest_calls += 1
        if self._raise_on_harvest:
            raise RuntimeError("broker unreadable")
        return self._harvests.pop(0) if self._harvests else {}

    async def cancel_exit_leg_ids(self, broker_account_name, order_ids):
        self.cancel_calls.append(list(order_ids))
        return self._cancel_result or {"cancelled": list(order_ids), "refused": None,
                                       "untouched": []}


def _make_sf() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", future=True,
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    tables = [t for t in Base.metadata.sorted_tables
              if t.name not in ("market_trade_ticks", "market_quote_ticks")]
    Base.metadata.create_all(engine, tables=tables)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _svc(adapter, *, enabled: bool = True) -> OmsRiskService:
    sf = _make_sf()
    svc = OmsRiskService(
        Settings(
            oms_v2_exit_management_enabled=True,
            oms_v2_eod_cancel_reexit_enabled=enabled,
        ),
        redis_client=_FakeRedis(), session_factory=sf, broker_adapter=adapter,
    )
    with sf() as s:
        svc.store.ensure_strategy(s, "schwab_1m_v2", name="v2")
        svc.store.ensure_broker_account(s, ACCT, provider="simulated", environment="test")
        s.commit()
    svc._managed_v2_symbols.add((ACCT, SYM))
    svc._v2_eod_cancel_reexit_due = lambda now=None: True
    return svc


def _track(svc, *, pm_ok: bool, restore_raises: bool = False, rth: bool = True,
           monkeypatch=None):
    """Replace the two leaf actions with recorders so orchestration is what is under test.

    ⛔ `rth` matters: a protective OCO CANNOT be re-armed outside regular hours (Schwab rejects a
    STOP leg after the close), so the restore path deliberately refuses. Tests that want to
    observe a restore must say they are in RTH.
    """
    import project_mai_tai.oms.service as _svcmod
    _svcmod._is_regular_market_session = lambda now=None: rth
    calls = {"pm": 0, "restore": 0}

    async def _pm(acct, symbol, close_on_fill):
        calls["pm"] += 1
        return pm_ok

    async def _restore(acct, symbol, **kw):
        calls["restore"] += 1
        if restore_raises:
            raise RuntimeError("restore refused")

    svc._v2_eod_place_pm_exit = _pm
    svc._emit_v2_rth_edge_bracket = _restore
    return calls


def _u(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=_ET).astimezone(timezone.utc)


# --- the due gate ------------------------------------------------------------------

def test_due_gate_pins_1601_and_refuses_weekends():
    svc = _svc(_LegAdapter([]))
    del svc._v2_eod_cancel_reexit_due  # use the real one
    assert svc._v2_eod_cancel_reexit_due(now=_u(2026, 9, 4, 16, 1)) is True
    assert svc._v2_eod_cancel_reexit_due(now=_u(2026, 9, 4, 16, 0)) is False   # 16:00 is too early
    assert svc._v2_eod_cancel_reexit_due(now=_u(2026, 9, 4, 15, 59)) is False
    assert svc._v2_eod_cancel_reexit_due(now=_u(2026, 9, 5, 17, 0)) is False   # Saturday


@pytest.mark.asyncio
async def test_flag_off_does_nothing_at_all():
    a = _LegAdapter([{SYM: ["1", "2"]}])
    svc = _svc(a, enabled=False)
    _track(svc, pm_ok=True)
    await svc._v2_eod_cancel_and_reexit()
    assert a.harvest_calls == 0 and a.cancel_calls == []


# --- 1. once per position, NOT once per tick ---------------------------------------

@pytest.mark.asyncio
async def test_claims_once_per_position_not_once_per_tick():
    a = _LegAdapter([{SYM: ["1"]}, {}, {SYM: ["1"]}, {}])
    svc = _svc(a)
    calls = _track(svc, pm_ok=True)
    for _ in range(4):                      # four 5s ticks after 16:01
        await svc._v2_eod_cancel_and_reexit()
    assert a.cancel_calls == [["1"]], "cancelled more than once — the per-tick hole is back"
    assert calls["pm"] == 1


# --- 2. a refused DELETE stops everything ------------------------------------------

@pytest.mark.asyncio
async def test_a_refused_delete_places_no_pm_exit_and_does_not_retry():
    a = _LegAdapter(
        [{SYM: ["1", "2"]}],
        cancel_result={"cancelled": [], "refused": {"order_id": "1", "status_code": 400,
                                                    "body": "cannot cancel"},
                       "untouched": ["2"]},
    )
    svc = _svc(a)
    calls = _track(svc, pm_ok=True)
    await svc._v2_eod_cancel_and_reexit()
    await svc._v2_eod_cancel_and_reexit()
    assert calls["pm"] == 0, "placed a PM exit after a REFUSED cancel"
    assert calls["restore"] == 0, "restored protection we never removed"
    assert len(a.cancel_calls) == 1, "retried a refused cancel"


# --- 3. no PM exit without a confirmed-zero re-read --------------------------------

@pytest.mark.asyncio
async def test_no_pm_exit_when_the_confirm_read_still_shows_a_working_leg():
    # cancel "succeeds", but the independent re-read still sees leg 2 working.
    a = _LegAdapter([{SYM: ["1", "2"]}, {SYM: ["2"]}])
    svc = _svc(a)
    calls = _track(svc, pm_ok=True)
    await svc._v2_eod_cancel_and_reexit()
    assert calls["pm"] == 0, "sold against shares a working leg still reserves"


@pytest.mark.asyncio
async def test_an_unreadable_confirm_blocks_the_pm_exit_and_restores():
    class _A(_LegAdapter):
        async def fetch_working_exit_leg_ids(self, acct, symbols):
            self.harvest_calls += 1
            if self.harvest_calls == 1:
                return {SYM: ["1"]}
            raise RuntimeError("unreadable on the confirm")

    a = _A([])
    svc = _svc(a)
    calls = _track(svc, pm_ok=True)
    await svc._v2_eod_cancel_and_reexit()
    assert calls["pm"] == 0, "placed a PM exit without confirming the legs are gone"
    assert calls["restore"] == 1, "cancelled legs and left the position with nothing"


# --- the common case: DAY legs already expired at the bell -------------------------

@pytest.mark.asyncio
async def test_nothing_to_cancel_still_places_the_pm_exit():
    a = _LegAdapter([{}, {}])               # no legs at 16:01 (DAIC 08-25 / CELU 08-27 shape)
    svc = _svc(a)
    calls = _track(svc, pm_ok=True)
    await svc._v2_eod_cancel_and_reexit()
    assert a.cancel_calls == [], "cancelled something when there was nothing to cancel"
    assert calls["pm"] == 1


# --- 4. restore, exactly once ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_failed_pm_exit_after_a_real_cancel_restores_exactly_once():
    a = _LegAdapter([{SYM: ["1", "2"]}, {}])
    svc = _svc(a)
    calls = _track(svc, pm_ok=False)
    await svc._v2_eod_cancel_and_reexit()
    await svc._v2_eod_cancel_and_reexit()   # a second tick must not restore again
    assert calls["restore"] == 1, "restore did not happen exactly once"


@pytest.mark.asyncio
async def test_a_failed_restore_is_marked_and_not_retried():
    a = _LegAdapter([{SYM: ["1"]}, {}])
    svc = _svc(a)
    calls = _track(svc, pm_ok=False, restore_raises=True)
    await svc._v2_eod_cancel_and_reexit()   # must not raise out of the sweep
    await svc._v2_eod_cancel_and_reexit()
    assert calls["restore"] == 1, "cycled on a failed restore"


@pytest.mark.asyncio
async def test_nothing_removed_means_nothing_restored():
    a = _LegAdapter([{}, {}])
    svc = _svc(a)
    calls = _track(svc, pm_ok=False)        # PM exit fails, but we cancelled NOTHING
    await svc._v2_eod_cancel_and_reexit()
    assert calls["restore"] == 0, "restored a bracket we never removed"


# --- 5. the PM exit uses the managed-exit path, never a direct POST ----------------

@pytest.mark.asyncio
async def test_pm_exit_goes_through_the_managed_exit_path():
    """If this ever becomes a direct broker POST, the order is invisible to
    get_open_exit_reserved_quantity and the 19:55 flatten places a SECOND sell."""
    a = _LegAdapter([])
    svc = _svc(a)
    seen: dict = {}

    class _Snap:
        dedup_active = False
        entry_price = 10.0
        current_quantity = 1

    async def _run_db(fn, commit=False):
        return _Snap()

    async def _emit(acct, symbol, position, entry_price, **kw):
        seen.update(kw)

    svc._run_db = _run_db
    svc._hydrate_v2_position = lambda snap: type(
        "P", (), {"update_price": lambda self, b: None}
    )()
    svc._emit_v2_exit_on_loop = _emit
    svc._latest_quotes_by_symbol[SYM] = {"bid": 9.5}

    await svc._v2_eod_place_pm_exit(ACCT, SYM, True)
    assert seen.get("reason") == "V2_EOD_CANCEL_REEXIT"
    assert seen.get("kind") == "EOD_CANCEL_REEXIT"

# ==================================================================================
# The six findings codex withheld the pin on at 52af4289. One test each; each was
# checked to go RED with its fix reverted.
# ==================================================================================

@pytest.mark.asyncio
async def test_F1_never_sells_when_an_oco_child_has_already_filled():
    """'No WORKING legs' has two causes: the legs lapsed (we still hold) or one FILLED (already
    sold). They read identically. Selling on the second is a naked short."""
    class _A(_LegAdapter):
        async def fetch_oco_resolved_by_fill_symbols(self, acct, symbols, **kw):
            return {SYM}

    a = _A([{}, {}])
    svc = _svc(a)
    emitted = []
    svc._emit_v2_exit_on_loop = lambda *ar, **kw: emitted.append(kw)
    async def _close_row(*ar, **kw):
        return None

    svc._close_resolved_oco_managed_row = _close_row
    placed = await svc._v2_eod_place_pm_exit(ACCT, SYM, True)
    assert emitted == [], "placed a sell after an OCO child had already FILLED"
    assert placed is True  # nothing to place AND nothing to restore


@pytest.mark.asyncio
async def test_F2_a_partial_cancel_refusal_restores_because_protection_is_now_degraded():
    """Leg 1 cancelled, leg 2 refused => the pair is HALF a pair. That is not 'nothing happened'."""
    a = _LegAdapter(
        [{SYM: ["1", "2"]}],
        cancel_result={"cancelled": ["1"], "refused": {"order_id": "2", "status_code": 400,
                                                       "body": "no"}, "untouched": []},
    )
    svc = _svc(a)
    calls = _track(svc, pm_ok=True, rth=True)
    await svc._v2_eod_cancel_and_reexit()
    assert calls["pm"] == 0, "placed a PM exit after a refused cancel"
    assert calls["restore"] == 1, "left the position with HALF a protective pair and did nothing"


@pytest.mark.asyncio
async def test_F3_a_venue_that_cannot_be_asked_is_never_read_as_confirmed_zero():
    """routing raises for an adapter without the capability; {} would be indistinguishable from
    'the broker reports no working legs' and would sell into an unasked venue."""
    class _A(_LegAdapter):
        async def fetch_working_exit_leg_ids(self, acct, symbols):
            self.harvest_calls += 1
            raise RuntimeError("venue cannot be asked")

    a = _A([])
    svc = _svc(a)
    calls = _track(svc, pm_ok=True)
    await svc._v2_eod_cancel_and_reexit()
    assert calls["pm"] == 0, "sold on a venue whose legs we never actually read"
    assert a.cancel_calls == []


@pytest.mark.asyncio
async def test_F4_no_bracket_restore_is_attempted_after_the_close():
    """Schwab rejects a STOP leg outside RTH (measured 2026-08-04), so a post-close 'restore'
    places nothing. It must not be attempted, and must never log as RESTORED."""
    a = _LegAdapter([{SYM: ["1"]}, {}])
    svc = _svc(a)
    calls = _track(svc, pm_ok=False, rth=False)
    await svc._v2_eod_cancel_and_reexit()
    assert calls["restore"] == 0, "tried to re-arm an OCO the broker would certainly reject"


def test_F5_the_window_is_closed_not_open_ended():
    svc = _svc(_LegAdapter([]))
    del svc._v2_eod_cancel_reexit_due
    assert svc._v2_eod_cancel_reexit_due(now=_u(2026, 9, 4, 16, 1)) is True
    assert svc._v2_eod_cancel_reexit_due(now=_u(2026, 9, 4, 16, 14)) is True
    assert svc._v2_eod_cancel_reexit_due(now=_u(2026, 9, 4, 16, 15)) is False  # window shut
    assert svc._v2_eod_cancel_reexit_due(now=_u(2026, 9, 4, 19, 30)) is False  # NOT still due


@pytest.mark.asyncio
async def test_F6_a_truncated_order_book_refuses_instead_of_reporting_zero():
    """maxResults truncation is a FALSE ZERO here, and false-zero means 'sell'."""
    from project_mai_tai.broker_adapters.schwab import _ORDER_PAGE_LIMIT, SchwabBrokerAdapter

    ad = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    ad.accounts_by_name = {ACCT: type("A", (), {"account_hash": "h"})()}
    ad.ACCEPTED_STATUSES = {"WORKING"}

    async def _req(method, path, **kw):
        return 200, {}, [{"orderId": str(i)} for i in range(_ORDER_PAGE_LIMIT)]

    ad._authorized_request_json = _req
    with pytest.raises(RuntimeError, match="TRUNCATED"):
        await ad.fetch_working_exit_leg_ids(ACCT, [SYM])


@pytest.mark.asyncio
async def test_F3_routing_raises_rather_than_returning_empty_for_an_unsupported_adapter():
    """The fix lives in routing: {} is indistinguishable from 'the broker reports no legs'."""
    from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter

    r = RoutingBrokerAdapter.__new__(RoutingBrokerAdapter)
    r._adapter_for_account = lambda name: object()   # an adapter WITHOUT the capability
    with pytest.raises(RuntimeError, match="cannot be asked"):
        await r.fetch_working_exit_leg_ids(ACCT, [SYM])
