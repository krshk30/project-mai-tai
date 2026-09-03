"""The v2 close-retry loop must TERMINATE — without ever deleting protection.

⭐ LIVE 2026-07-29: NCRA took **145 rejected sells in 55 minutes** (AMIX 25, STFS 7). One exit
decision (`ref=3.0587`) retried against a broker answering
`NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT`.

⛔ THE CAUSE WAS NOT A MISSING BOUND. `_v2_close_reconcile_flat` RESET the counter to 0 on any
not-flat read, so it sawtoothed 1,2,3 -> check -> 0 -> 1,2,3 and never accumulated. And
`_broker_symbol_is_flat` collapses HELD and UNKNOWN into a single `False`, so an INCONCLUSIVE read
reset the counter as though we had CONFIRMED we still hold the position.

⛔ THE FIX IS NOT A WEAKER FLAT TEST. Treating UNKNOWN as flat is exactly how the ERNA naked
position happened (a live armed stop deleted while 2 shares were held). Standing down stops the
HAMMERING and pages; the row and any protection stay in place, and the read-only exit poll still
resolves it — which is what actually recovered AMIX on 07-29.
"""
from __future__ import annotations

import asyncio
import logging

from project_mai_tai.oms.service import OmsRiskService, _PositionRead

ACCT, SYM = "live:orb", "NCRA"
KEY = (ACCT, SYM)


def _svc(state: _PositionRead):
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.logger = logging.getLogger("test-retry-bound")
    svc._v2_exit_close_failures = {}
    svc._v2_exit_stood_down = set()
    svc._managed_v2_symbols = set()
    svc._cw_flip_pending = set()
    svc._cw_floor_armed = set()
    svc._oco_exit_fetch_deferrals = {}

    async def _state(_a, _s):
        return state
    svc._broker_symbol_position_state = _state

    async def _is_flat(_a, _s, *, established_at=None, state=None):
        return state is _PositionRead.FLAT_CONFIRMED
    svc._broker_symbol_is_flat = _is_flat
    return svc


class _Row:
    entry_time = None


# ⛔ Hard cap so a BAD BOUND fails fast instead of HANGING. Mutating
# `_V2_EXIT_ABANDON_AFTER_FAILURES` to 99999 made the stand-down test loop ~100k times and wedge the
# suite — a mutation that hangs is worse than one that fails, because CI just times out with no
# signal. (Same lesson as the FIFO zero-take guard.) The pinned-value test is what catches that
# mutation; this cap makes sure nothing else silently spins.
_MAX_SIM_REJECTS = 64


def _reject(svc, n=1):
    """Simulate n consecutive REJECTED closes arriving at the reconcile."""
    assert n <= _MAX_SIM_REJECTS, (
        f"refusing to simulate {n} rejects (cap {_MAX_SIM_REJECTS}) — the abandon bound is "
        f"implausibly large; check _V2_EXIT_ABANDON_AFTER_FAILURES"
    )
    out = []
    for _ in range(n):
        out.append(asyncio.run(svc._v2_close_reconcile_flat(None, ACCT, SYM, _Row())))
    return out


# ------------------------------------------------------------------ THE REGRESSION
def test_an_inconclusive_read_no_longer_resets_the_counter() -> None:
    """THE SAWTOOTH. Under UNKNOWN the counter must ACCUMULATE, not reset every 3rd reject."""
    svc = _svc(_PositionRead.UNKNOWN)
    _reject(svc, 6)
    assert svc._v2_exit_close_failures[KEY] == 6, (
        f"counter reset under an inconclusive read ({svc._v2_exit_close_failures[KEY]}) — this is "
        "the 145-reject sawtooth"
    )


def test_the_loop_stands_down_at_the_bound() -> None:
    svc = _svc(_PositionRead.UNKNOWN)
    _reject(svc, OmsRiskService._V2_EXIT_ABANDON_AFTER_FAILURES - 1)
    assert KEY not in svc._v2_exit_stood_down, "stood down too early"
    _reject(svc, 1)
    assert KEY in svc._v2_exit_stood_down


def test_the_bound_value_is_pinned() -> None:
    assert OmsRiskService._V2_EXIT_ABANDON_AFTER_FAILURES == 8
    assert OmsRiskService._V2_EXIT_RECONCILE_AFTER_FAILURES == 3


# ------------------------------------------------------------------ the safe directions
def test_a_positively_HELD_read_still_resets_and_keeps_managing() -> None:
    """⛔ MUST NOT over-correct. If the broker says we DO hold it, retrying is correct behaviour and
    the loop must never stand down."""
    svc = _svc(_PositionRead.HELD)
    _reject(svc, 30)
    # Under HELD the counter deliberately SAWTOOTHS (1,2,3 -> confirm HELD -> reset). That is the
    # correct behaviour: we really do hold the position, so retrying the close is right. What must
    # never happen is reaching the abandon bound.
    assert svc._v2_exit_close_failures[KEY] < OmsRiskService._V2_EXIT_ABANDON_AFTER_FAILURES
    assert KEY not in svc._v2_exit_stood_down, "stood down on a position we KNOW we hold"


def test_standing_down_does_NOT_close_the_row_or_delete_protection() -> None:
    """⛔ THE ERNA GUARD. Standing down must never look like 'flat'."""
    svc = _svc(_PositionRead.UNKNOWN)
    res = _reject(svc, OmsRiskService._V2_EXIT_ABANDON_AFTER_FAILURES + 3)
    assert not any(res), "returned True (=row closed) on an inconclusive read"


def test_a_confirmed_flat_still_closes_the_row() -> None:
    """The pre-existing happy path must survive."""
    svc = _svc(_PositionRead.FLAT_CONFIRMED)
    svc._find_oco_entry_order = lambda *a, **k: None
    svc._fetch_oco_exit_detail = lambda *a, **k: _noop()
    svc._persist_oco_exit_fill = lambda *a, **k: False

    class _Store:
        def close_managed_position(self, *a, **k):
            pass
    svc.store = _Store()
    _reject(svc, OmsRiskService._V2_EXIT_RECONCILE_AFTER_FAILURES - 1)
    assert asyncio.run(svc._v2_close_reconcile_flat(None, ACCT, SYM, _Row())) is True


async def _noop():
    return None


def test_a_HELD_read_lifts_an_existing_stand_down() -> None:
    """New information must be able to resume the loop — a stand-down is not permanent."""
    svc = _svc(_PositionRead.UNKNOWN)
    _reject(svc, OmsRiskService._V2_EXIT_ABANDON_AFTER_FAILURES)
    assert KEY in svc._v2_exit_stood_down
    svc2 = _svc(_PositionRead.HELD)
    svc2._v2_exit_stood_down = svc._v2_exit_stood_down
    svc2._v2_exit_close_failures = svc._v2_exit_close_failures
    _reject(svc2, 1)
    assert KEY not in svc2._v2_exit_stood_down


def test_a_HELD_read_must_NOT_clear_the_absolute_reject_ceiling():
    """⛔⭐⭐ LIVE 2026-09-03, CHPT: ~200 REJECTED market closes in THREE MINUTES.

    Every close was refused "oversold" because our OWN working exit leg reserved the shares, and
    every reconcile read answered HELD -- truthfully, we did hold it. The consecutive counter is
    DELIBERATELY reset by a positively-HELD read (see the two tests above, which specify that and
    must keep passing), so it sawtoothed and could never reach `_V2_EXIT_ABANDON_AFTER_FAILURES`.

    The absolute ceiling is the second, independent bound that closes that hole. Its whole value
    rests on ONE property: a HELD read must not clear it. If it ever does, the ceiling becomes
    exactly as unreachable as the consecutive bound was, and the reject storm returns.

    ⭐ Sustained rejected-order volume is a broker API-access risk -- a harm the trading logic
    cannot see, which is why this bound is not conditional on any position read.
    """
    svc = _svc(_PositionRead.HELD)
    svc._v2_exit_reject_total = {KEY: 17}
    svc._v2_exit_close_failures = {KEY: 3}

    asyncio.run(svc._v2_close_reconcile_flat(None, ACCT, SYM, object()))

    assert svc._v2_exit_close_failures.get(KEY, 0) == 0, (
        "the CONSECUTIVE counter must still reset on a positively-HELD read -- that behaviour is "
        "specified by the tests above and is correct when a jam clears"
    )
    assert svc._v2_exit_reject_total[KEY] == 17, (
        "a HELD read must NOT clear the absolute reject ceiling: in an exit-reservation jam we "
        "genuinely hold the position for its whole duration, so a read-conditional bound can never "
        "terminate it -- that is the CHPT defect"
    )
