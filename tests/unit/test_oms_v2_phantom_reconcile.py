"""v2 CW managed-exit phantom-reconcile (2026-07-13 AGEN churn).

When a v2 position is closed out-of-band (manual/external), the CW full-close rejects
("oversold") every tick and close_on_fill waits for a fill that never comes -> the OMS
churns forever. `_v2_close_reconcile_flat` clears the managed row ONLY after a fresh
broker read confirms the account is flat.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from project_mai_tai.oms.service import OmsRiskService

ACCT, SYM = "live:schwab_1m_v2", "AGEN"


def _svc(*, broker_flat: bool = True, read_raises: bool = False):
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                 exception=lambda *a, **k: None, debug=lambda *a, **k: None)
    svc._v2_exit_close_failures = {}
    svc._managed_v2_symbols = {(ACCT, SYM)}
    svc._cw_flip_pending = {(ACCT, SYM)}
    closed: list = []
    svc.store = SimpleNamespace(close_managed_position=lambda session, row: closed.append(row))

    async def _positions(_name):
        if read_raises:
            raise RuntimeError("broker unreachable")
        return [] if broker_flat else [SimpleNamespace(symbol=SYM, quantity=Decimal("2"))]

    svc.broker_adapter = SimpleNamespace(list_account_positions=_positions)
    return svc, closed


def test_v2_reconcile_below_threshold_does_not_read_or_clear():
    svc, closed = _svc(broker_flat=True)
    row = object()
    for _ in range(OmsRiskService._V2_EXIT_RECONCILE_AFTER_FAILURES - 1):
        assert asyncio.run(svc._v2_close_reconcile_flat(None, ACCT, SYM, row)) is False
    assert closed == []
    assert svc._v2_exit_close_failures[(ACCT, SYM)] == OmsRiskService._V2_EXIT_RECONCILE_AFTER_FAILURES - 1


def test_v2_reconcile_clears_phantom_when_broker_flat():
    svc, closed = _svc(broker_flat=True)
    row = object()
    res = False
    for _ in range(OmsRiskService._V2_EXIT_RECONCILE_AFTER_FAILURES):
        res = asyncio.run(svc._v2_close_reconcile_flat(None, ACCT, SYM, row))
    assert res is True
    assert closed == [row]
    assert (ACCT, SYM) not in svc._managed_v2_symbols
    assert (ACCT, SYM) not in svc._cw_flip_pending
    assert (ACCT, SYM) not in svc._v2_exit_close_failures


def test_v2_reconcile_keeps_row_when_broker_still_holds():
    svc, closed = _svc(broker_flat=False)
    row = object()
    res = True
    for _ in range(OmsRiskService._V2_EXIT_RECONCILE_AFTER_FAILURES + 2):
        res = asyncio.run(svc._v2_close_reconcile_flat(None, ACCT, SYM, row))
    assert res is False
    assert closed == []
    assert (ACCT, SYM) in svc._managed_v2_symbols
    assert svc._v2_exit_close_failures.get((ACCT, SYM), 0) < OmsRiskService._V2_EXIT_RECONCILE_AFTER_FAILURES


def test_v2_reconcile_read_failure_never_clears():
    svc, closed = _svc(read_raises=True)
    row = object()
    res = True
    for _ in range(OmsRiskService._V2_EXIT_RECONCILE_AFTER_FAILURES):
        res = asyncio.run(svc._v2_close_reconcile_flat(None, ACCT, SYM, row))
    assert res is False
    assert closed == []
    assert (ACCT, SYM) in svc._managed_v2_symbols
