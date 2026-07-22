"""The OCO stand-down: the software exit ladder defers to an armed broker-native bracket.

★ These tests exist to pin the FAIL-OPEN DIRECTION (operator-confirmed 2026-07-21), which is
the whole safety argument for this switch:

  wrong True  -> ladder stands down AND no bracket is working -> the position has NO exit
                 (the ERNA shape; unrecoverable in the moment)
  wrong False -> ladder runs alongside the bracket -> at worst an oversell
                 (the NXTC class; loud, logged, reconcilable)

So every ambiguous input must resolve to False. A test suite that only proved "stand-down
works when armed" would pass on an implementation that also stood down on a dead sync loop —
which is the failure this design exists to prevent.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from project_mai_tai.oms.service import OmsRiskService, utcnow
from project_mai_tai.settings import Settings


def _service(**overrides: object) -> OmsRiskService:
    """Build only the state the stand-down predicate touches.

    Deliberately does NOT stand up the full service (DB, adapter, streams): the predicate
    under test is pure in-memory by design — that is the point of it, since a DB round-trip
    on the per-quote-tick path is the #391-family freeze driver.
    """
    kwargs: dict[str, object] = {
        "oms_adapter": "simulated",
        "oms_native_oco_stand_down_enabled": True,
    }
    kwargs.update(overrides)
    settings = Settings(**kwargs)  # type: ignore[arg-type]
    service = OmsRiskService.__new__(OmsRiskService)
    service.settings = settings
    service.logger = logging.getLogger("test-oco-stand-down")
    service._native_oco_armed_confirmed_at = {}
    service._native_oco_resolving = {}
    service._managed_v2_symbols = set()
    return service


ACCT = "live:schwab_1m_v2"
SYMBOL = "KIDZ"


def test_no_confirmation_runs_the_ladder() -> None:
    """The default state is NOT stand-down. A restart begins here: empty set, ladder live."""
    service = _service()
    assert service._native_oco_stand_down_active(ACCT, SYMBOL) is False


def test_fresh_confirmation_stands_down() -> None:
    service = _service()
    service._native_oco_armed_confirmed_at[(ACCT, SYMBOL)] = utcnow()
    assert service._native_oco_stand_down_active(ACCT, SYMBOL) is True


def test_stale_confirmation_resumes_the_ladder() -> None:
    """★ THE FAIL-OPEN CASE. A stalled sync loop must NOT hold the ladder down.

    Pins the VALUE of the dwell (30s default), not just the behaviour: a silent bump of
    this constant would let a dead sync suppress the exit ladder for longer, and that must
    show up as a red test rather than as a live position with no exit.
    """
    service = _service()
    assert service.settings.oms_native_oco_confirmation_max_age_seconds == 30
    service._native_oco_armed_confirmed_at[(ACCT, SYMBOL)] = utcnow() - timedelta(seconds=31)
    assert service._native_oco_stand_down_active(ACCT, SYMBOL) is False
    # ...and the expired entry is dropped, so it cannot linger and flap back to True.
    assert (ACCT, SYMBOL) not in service._native_oco_armed_confirmed_at


def test_confirmation_just_inside_the_dwell_still_stands_down() -> None:
    """Boundary pinned from the other side, so the dwell can't silently drift to ~0."""
    service = _service()
    service._native_oco_armed_confirmed_at[(ACCT, SYMBOL)] = utcnow() - timedelta(seconds=29)
    assert service._native_oco_stand_down_active(ACCT, SYMBOL) is True


def test_stand_down_is_scoped_to_the_exact_position() -> None:
    """An armed bracket on one symbol must never silence another symbol's ladder."""
    service = _service()
    service._native_oco_armed_confirmed_at[(ACCT, SYMBOL)] = utcnow()
    assert service._native_oco_stand_down_active(ACCT, "AGEN") is False
    assert service._native_oco_stand_down_active("live:orb", SYMBOL) is False


class _StubAdapter:
    """Minimal broker-adapter stub exposing the OCO capabilities."""

    def __init__(
        self,
        armed: set[str] | None = None,
        boom: bool = False,
        resolved: set[str] | None = None,
        resolved_boom: bool = False,
    ) -> None:
        self._armed = armed or set()
        self._boom = boom
        self._resolved = resolved or set()
        self._resolved_boom = resolved_boom
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.resolved_calls: list[tuple[str, tuple[str, ...]]] = []

    async def fetch_armed_native_oco_symbols(self, account: str, symbols: list[str]) -> set[str]:
        self.calls.append((account, tuple(symbols)))
        if self._boom:
            raise RuntimeError("broker unreachable")
        return {s for s in symbols if s in self._armed}

    async def fetch_oco_resolved_by_fill_symbols(
        self, account: str, symbols: list[str]
    ) -> set[str]:
        self.resolved_calls.append((account, tuple(symbols)))
        if self._resolved_boom:
            raise RuntimeError("broker unreachable")
        return {s for s in symbols if s in self._resolved}


@pytest.mark.asyncio
async def test_refresh_confirms_only_broker_armed_symbols() -> None:
    """The BROKER is the source of truth — a symbol the broker reports armed gets stamped,
    one it does not report does not. (OCO legs live at the broker, never in broker_orders.)"""
    service = _service()
    service._managed_v2_symbols = {(ACCT, SYMBOL), (ACCT, "AGEN")}
    service.broker_adapter = _StubAdapter(armed={SYMBOL})  # KIDZ armed, AGEN not
    await service._refresh_native_oco_armed_state(None)
    assert (ACCT, SYMBOL) in service._native_oco_armed_confirmed_at
    assert (ACCT, "AGEN") not in service._native_oco_armed_confirmed_at


@pytest.mark.asyncio
async def test_refresh_drops_a_symbol_the_broker_no_longer_reports_armed() -> None:
    """Bracket resolved (filled/cancelled) -> broker stops reporting it -> ladder resumes."""
    service = _service()
    service._managed_v2_symbols = {(ACCT, SYMBOL)}
    service._native_oco_armed_confirmed_at[(ACCT, SYMBOL)] = utcnow() - timedelta(seconds=5)
    service.broker_adapter = _StubAdapter(armed=set())   # broker reports nothing armed
    await service._refresh_native_oco_armed_state(None)
    assert (ACCT, SYMBOL) not in service._native_oco_armed_confirmed_at


@pytest.mark.asyncio
async def test_broker_fetch_failure_leaves_entries_to_age_out_rather_than_extending_them() -> None:
    """★ FAIL-OPEN: an unreachable broker must NOT renew confirmations — it must let them expire."""
    service = _service()
    service._managed_v2_symbols = {(ACCT, SYMBOL)}
    stamped = utcnow() - timedelta(seconds=20)
    service._native_oco_armed_confirmed_at[(ACCT, SYMBOL)] = stamped
    service.broker_adapter = _StubAdapter(boom=True)
    await service._refresh_native_oco_armed_state(None)
    # Unchanged, NOT refreshed: it keeps aging toward the fail-open cutoff.
    assert service._native_oco_armed_confirmed_at[(ACCT, SYMBOL)] == stamped


@pytest.mark.asyncio
async def test_no_adapter_capability_clears_and_runs_the_ladder() -> None:
    """An adapter without the capability (Webull/Alpaca/sim) -> nothing armed -> ladder runs."""
    service = _service()
    service._managed_v2_symbols = {(ACCT, SYMBOL)}
    service._native_oco_armed_confirmed_at[(ACCT, SYMBOL)] = utcnow()
    service.broker_adapter = object()   # no fetch_armed_native_oco_symbols
    await service._refresh_native_oco_armed_state(None)
    assert service._native_oco_armed_confirmed_at == {}


@pytest.mark.asyncio
async def test_flag_off_clears_all_stand_down_state() -> None:
    """Flag off ⇒ the ladder behaves exactly as it does today, with no residue."""
    service = _service(oms_native_oco_stand_down_enabled=False)
    service._native_oco_armed_confirmed_at[(ACCT, SYMBOL)] = utcnow()
    service.broker_adapter = _StubAdapter(armed={SYMBOL})
    await service._refresh_native_oco_armed_state(None)
    assert service._native_oco_armed_confirmed_at == {}
    assert service._native_oco_stand_down_active(ACCT, SYMBOL) is False


@pytest.mark.asyncio
async def test_resolution_grace_keeps_ladder_deferred_until_position_reconciles() -> None:
    """When the OCO clears (a leg filled -> position closing) the ladder must stay deferred until
    the position reconciles to flat -- else it fires a redundant close on a stale position (the
    'rejected sell on every OCO resolution' noise). Common case: cleared early by reconcile."""
    service = _service()
    service._managed_v2_symbols = {(ACCT, SYMBOL)}
    service._native_oco_armed_confirmed_at[(ACCT, SYMBOL)] = utcnow()
    # sync 1: broker reports armed -> stays armed
    service.broker_adapter = _StubAdapter(armed={SYMBOL})
    await service._refresh_native_oco_armed_state(None)
    assert service._native_oco_stand_down_active(ACCT, SYMBOL) is True

    # sync 2: OCO resolved (leg filled) -> broker reports nothing armed, position not yet flat
    service.broker_adapter = _StubAdapter(armed=set())
    await service._refresh_native_oco_armed_state(None)
    # armed cleared, but RESOLVING grace holds the ladder down (no redundant close)
    assert (ACCT, SYMBOL) not in service._native_oco_armed_confirmed_at
    assert (ACCT, SYMBOL) in service._native_oco_resolving
    assert service._native_oco_stand_down_active(ACCT, SYMBOL) is True

    # sync 3: position reconciled flat (left _managed_v2_symbols) -> resolving cleared, ladder free
    service._managed_v2_symbols = set()
    await service._refresh_native_oco_armed_state(None)
    assert (ACCT, SYMBOL) not in service._native_oco_resolving
    assert service._native_oco_stand_down_active(ACCT, SYMBOL) is False


def test_resolution_grace_backstop_resumes_ladder_if_position_stays_held() -> None:
    """The rare manual-OCO-cancel case: position genuinely still held after the grace -> the
    ladder MUST resume (do not defer a real held position forever)."""
    service = _service()
    service._managed_v2_symbols = {(ACCT, SYMBOL)}
    grace = service.settings.oms_native_oco_resolve_grace_seconds
    service._native_oco_resolving[(ACCT, SYMBOL)] = utcnow() - timedelta(seconds=grace + 1)
    assert service._native_oco_stand_down_active(ACCT, SYMBOL) is False
    assert (ACCT, SYMBOL) not in service._native_oco_resolving   # expired entry dropped


@pytest.mark.asyncio
async def test_rearm_removes_a_resolving_entry() -> None:
    """A symbol that re-arms (new bracket) must leave the resolving set."""
    service = _service()
    service._native_oco_resolving[(ACCT, SYMBOL)] = utcnow()
    service._managed_v2_symbols = {(ACCT, SYMBOL)}
    service.broker_adapter = _StubAdapter(armed={SYMBOL})   # broker re-reports it armed
    await service._refresh_native_oco_armed_state(None)
    assert (ACCT, SYMBOL) not in service._native_oco_resolving
    assert service._native_oco_stand_down_active(ACCT, SYMBOL) is True




# ---------------------------------------------------------------------------
# 2026-07-22: proactive close of a resolved-BY-FILL OCO. The broker-created OCO fill closes the
# position but never decrements the managed row, so without this the row's only close-path is the
# reject-driven _v2_close_reconcile_flat -- the exit ladder resumes and churns ~3 rejected closes
# first (observed live on BOTH KSCP and LABT; the 90s grace is dwarfed by Schwab's ~6min
# fill->positions propagation). The close is keyed on the broker's OWN execution record (a
# recently-FILLED child SELL leg, fetch_oco_resolved_by_fill_symbols) -- authoritative, unlike a
# FLAT_INFERRED positions read (07-15 ERNA). A bracket that resolved by EXPIRY/CANCEL (still held,
# e.g. SMCX at the close) has no filled leg, is skipped, and the ladder manages it.
# ---------------------------------------------------------------------------

class _RowStore:
    """Minimal managed-position store: one open row that close() flips shut."""

    def __init__(self) -> None:
        self.open = True
        self.closed_calls = 0

    def get_open_managed_position(self, _session: object, *, broker_account_name: str, symbol: str):
        if not self.open:
            return None
        return type("_Row", (), {"entry_time": utcnow()})()

    def close_managed_position(self, _session: object, _row: object) -> None:
        self.open = False
        self.closed_calls += 1


def _reconcile_service(
    *, resolved: set[str] | None = None, flag: bool = True, resolved_boom: bool = False,
    has_capability: bool = True,
) -> OmsRiskService:
    """A service wired for the resolved-by-fill close path: a fake store + inline _run_db, and a
    _StubAdapter whose fetch_oco_resolved_by_fill_symbols reports `resolved`. Nothing here touches
    a real DB; the adapter's own fill-status walk is proven in test_schwab_native_bracket."""
    service = _service(oms_native_oco_resolve_flat_reconcile_enabled=flag)
    service._cw_flip_pending = set()
    service._cw_floor_armed = set()
    service._v2_exit_close_failures = {}
    service.store = _RowStore()  # type: ignore[attr-defined]

    async def _run_db(fn, commit=False):  # type: ignore[no-untyped-def]
        return fn(object())

    service._run_db = _run_db  # type: ignore[assignment]
    adapter = _StubAdapter(armed=set(), resolved=resolved, resolved_boom=resolved_boom)
    if not has_capability:
        # An adapter without the fill-status capability (Webull/Alpaca/sim): shadow the method so
        # getattr(adapter, "fetch_oco_resolved_by_fill_symbols", None) resolves to None.
        adapter.fetch_oco_resolved_by_fill_symbols = None  # type: ignore[assignment]
    service.broker_adapter = adapter
    return service


@pytest.mark.asyncio
async def test_resolved_by_fill_closes_phantom_row_with_no_ladder_rejects() -> None:
    """THE FIX. The broker's execution record shows the OCO resolved by a FILL -> the row is closed
    on the sync, and the symbol leaves BOTH the managed set and the resolving set -> the ladder
    never resumes, so it never fires the ~3 rejected closes."""
    service = _reconcile_service(resolved={SYMBOL})
    service._native_oco_resolving[(ACCT, SYMBOL)] = utcnow()
    service._managed_v2_symbols = {(ACCT, SYMBOL)}

    await service._refresh_native_oco_armed_state(None)

    assert service.store.closed_calls == 1                         # row closed directly
    assert (ACCT, SYMBOL) not in service._native_oco_resolving     # resolving cleared
    assert (ACCT, SYMBOL) not in service._managed_v2_symbols       # left the managed set
    assert service._native_oco_stand_down_active(ACCT, SYMBOL) is False
    assert service.broker_adapter.resolved_calls == [(ACCT, (SYMBOL,))]


@pytest.mark.asyncio
async def test_resolved_by_expiry_keeps_the_still_held_position() -> None:
    """THE SAFETY CASE (SMCX at the close). The OCO cleared with NO filled leg -> the broker
    reports nothing resolved-by-fill -> the row is NOT closed. It stays managed + resolving, so the
    software ladder correctly takes over a genuinely-held position. This is the strand the
    fill-status keying exists to prevent (vs a FLAT_INFERRED positions read)."""
    service = _reconcile_service(resolved=set())   # no filled leg -> resolved-by-fill is empty
    service._native_oco_resolving[(ACCT, SYMBOL)] = utcnow()
    service._managed_v2_symbols = {(ACCT, SYMBOL)}

    await service._refresh_native_oco_armed_state(None)

    assert service.store.closed_calls == 0                         # NOT closed -> ladder manages it
    assert (ACCT, SYMBOL) in service._native_oco_resolving
    assert (ACCT, SYMBOL) in service._managed_v2_symbols
    assert service.broker_adapter.resolved_calls == [(ACCT, (SYMBOL,))]  # the record WAS consulted


@pytest.mark.asyncio
async def test_resolved_by_fill_is_flag_gated_and_ships_inert() -> None:
    """Flag OFF => byte-identical to today: no fill-status fetch, no proactive close. The symbol
    stays in resolving/managed, so the reject self-heal path is unchanged. Pins the VALUE of the
    gate so a silent default flip would turn this red."""
    service = _reconcile_service(resolved={SYMBOL}, flag=False)
    assert service.settings.oms_native_oco_resolve_flat_reconcile_enabled is False
    service._native_oco_resolving[(ACCT, SYMBOL)] = utcnow()
    service._managed_v2_symbols = {(ACCT, SYMBOL)}

    await service._refresh_native_oco_armed_state(None)

    assert service.store.closed_calls == 0
    assert service.broker_adapter.resolved_calls == []             # the fetch was never made
    assert (ACCT, SYMBOL) in service._native_oco_resolving
    assert (ACCT, SYMBOL) in service._managed_v2_symbols


@pytest.mark.asyncio
async def test_resolved_by_fill_fetch_error_is_fail_open() -> None:
    """FAIL-OPEN. A raising fill-status fetch must NOT break the sync and must leave the row for
    the grace backstop + reject self-heal -- never close it on a guess."""
    service = _reconcile_service(resolved={SYMBOL}, resolved_boom=True)
    service._native_oco_resolving[(ACCT, SYMBOL)] = utcnow()
    service._managed_v2_symbols = {(ACCT, SYMBOL)}

    await service._refresh_native_oco_armed_state(None)   # must not raise

    assert service.store.closed_calls == 0
    assert (ACCT, SYMBOL) in service._native_oco_resolving
    assert (ACCT, SYMBOL) in service._managed_v2_symbols


@pytest.mark.asyncio
async def test_resolved_by_fill_no_capability_keeps_row() -> None:
    """An adapter without the fill-status capability (Webull/Alpaca/sim) -> nothing closed, the
    grace backstop + reject self-heal still apply."""
    service = _reconcile_service(resolved={SYMBOL}, has_capability=False)
    service._native_oco_resolving[(ACCT, SYMBOL)] = utcnow()
    service._managed_v2_symbols = {(ACCT, SYMBOL)}

    await service._refresh_native_oco_armed_state(None)

    assert service.store.closed_calls == 0
    assert (ACCT, SYMBOL) in service._native_oco_resolving
