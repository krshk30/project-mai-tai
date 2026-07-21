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


@pytest.mark.asyncio
async def test_refresh_failure_leaves_entries_to_age_out_rather_than_extending_them() -> None:
    """A DB stall in the refresh must not renew confirmations — it must let them expire."""
    service = _service()
    stamped = utcnow() - timedelta(seconds=20)
    service._native_oco_armed_confirmed_at[(ACCT, SYMBOL)] = stamped

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("DB stalled")

    service._run_db = _boom  # type: ignore[assignment]
    await service._refresh_native_oco_armed_state(None)

    # Unchanged, NOT refreshed: it keeps aging toward the fail-open cutoff.
    assert service._native_oco_armed_confirmed_at[(ACCT, SYMBOL)] == stamped


@pytest.mark.asyncio
async def test_flag_off_clears_all_stand_down_state() -> None:
    """Flag off ⇒ the ladder behaves exactly as it does today, with no residue."""
    service = _service(oms_native_oco_stand_down_enabled=False)
    service._native_oco_armed_confirmed_at[(ACCT, SYMBOL)] = utcnow()
    await service._refresh_native_oco_armed_state(None)
    assert service._native_oco_armed_confirmed_at == {}
    assert service._native_oco_stand_down_active(ACCT, SYMBOL) is False
