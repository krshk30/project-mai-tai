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


# ═══════════════════════════════════════════════════════════════════════════════════════════
# #885 RETROSPECTIVE (codex-2, 2026-09-04). Three findings against the merged ceiling, all real.
#
# ⛔ Finding 3 is why this block exists: the ceiling test above proves only that a HELD read
# LEAVES 17 intact. It never drives `_emit_v2_exit_on_loop`, never reaches 20, and never asserts
# the stand-down — so mutating the ceiling branch to `if False:` left the whole file GREEN (8
# passed). A bound nothing exercises is not a bound. These tests drive the REAL emit path with
# only the persistence stubbed.
# ═══════════════════════════════════════════════════════════════════════════════════════════

class _Payload:
    def __init__(self, status: str) -> None:
        self.status = status


class _Ev:
    def __init__(self, status: str) -> None:
        self.payload = _Payload(status)


class _Sess:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def commit(self):
        pass


class _Pos:
    quantity = 10


def _emit_svc(*, statuses, close_on_fill=False, row=object()):
    """The REAL `_emit_v2_exit_on_loop` with only persistence/broker stubbed.

    `statuses` is what `_emit_v2_managed_sell` returns for each call — e.g. ["rejected"] for a
    refused close, [] for the missing strategy/account case that returns no events at all.
    """
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.logger = logging.getLogger("test-ceiling")
    svc._v2_exit_close_failures = {}
    svc._v2_exit_stood_down = set()
    svc._v2_exit_reject_total = {}
    svc._managed_v2_symbols = {KEY}
    svc._cw_flip_pending = set()
    svc._cw_floor_armed = set()
    svc._oco_exit_fetch_deferrals = {}
    svc.closed_rows = []

    svc.session_factory = _Sess

    class _Store:
        def get_open_managed_position(self, *_a, **_k):
            return row

        def update_managed_position_from_position(self, *_a, **_k):
            pass

        def close_managed_position(self, _s, r):
            svc.closed_rows.append(r)

    svc.store = _Store()

    async def _sell(*_a, **_k):
        return [_Ev(s) for s in statuses]
    svc._emit_v2_managed_sell = _sell

    async def _reconcile(*_a, **_k):
        return False  # broker does NOT confirm flat — the storm case
    svc._v2_close_reconcile_flat = _reconcile

    svc._a2_should_defer = lambda *_a, **_k: False
    svc._a2_enabled_for = lambda *_a, **_k: False
    svc._a2_note_reject = lambda *_a, **_k: None
    svc._a2_clear = lambda *_a, **_k: None
    svc._is_exit_refused_not_sellable = lambda *_a, **_k: False

    async def _escalate(*_a, **_k):
        return None
    svc._a2_maybe_escalate = _escalate

    svc._clear_exit_reservation_release = lambda *_a, **_k: None

    async def _publish(*_a, **_k):
        return None
    svc._publish_order_event = _publish

    svc._close_on_fill = close_on_fill
    return svc


def _drive_close(svc, n=1):
    """n HARD closes through the REAL on-loop emit path."""
    for _ in range(n):
        asyncio.run(
            svc._emit_v2_exit_on_loop(
                ACCT, SYM, _Pos(), 1.0,
                kind="HARD", reference_price=1.0, reason="hard_stop", bid=1.0,
                close_on_fill=svc._close_on_fill,
            )
        )


# ------------------------------------------------------- FINDING 3: the ceiling, actually driven
def test_the_absolute_ceiling_stands_the_loop_down_at_the_bound() -> None:
    """⛔ THE BOUND ITSELF. Rejected closes must accumulate to the ceiling and STOP the loop.

    This is the test whose absence let `if False:` on the ceiling branch pass 8/8.
    """
    svc = _emit_svc(statuses=["rejected"], close_on_fill=True)
    n = OmsRiskService._V2_EXIT_MAX_REJECTS_PER_EPISODE

    _drive_close(svc, n - 1)
    assert svc._v2_exit_reject_total[KEY] == n - 1
    assert KEY not in svc._v2_exit_stood_down, "must NOT stand down one short of the bound"

    _drive_close(svc, 1)
    assert svc._v2_exit_reject_total[KEY] == n
    assert KEY in svc._v2_exit_stood_down, (
        "at the ceiling the retry loop MUST stand down regardless of the broker read — this is "
        "the CHPT 2026-09-03 defect (205 rejected closes in 8 minutes)"
    )


def test_the_ceiling_does_not_delete_the_row_or_its_protection() -> None:
    """Standing down stops the HAMMERING. It must never abandon the position."""
    svc = _emit_svc(statuses=["rejected"], close_on_fill=True)
    _drive_close(svc, OmsRiskService._V2_EXIT_MAX_REJECTS_PER_EPISODE)
    assert KEY in svc._v2_exit_stood_down
    assert svc.closed_rows == [], "the managed row must be LEFT IN PLACE for the exit poll"


# ------------------------------------------- FINDING 2: absence of a refusal is not progress
def test_an_empty_event_list_is_NOT_progress_and_must_not_clear_the_ceiling() -> None:
    """⛔ `_emit_v2_managed_sell` returns [] when the strategy/broker-account lookup misses.

    An empty list contains no rejected event, so `if not rejected` classified "we emitted NOTHING"
    as "the close placed" and wiped the ceiling. A counter that a no-op erases cannot bound a
    storm: alternate one failed lookup with one rejected close and the total never grows.
    """
    svc = _emit_svc(statuses=["rejected"], close_on_fill=True)
    _drive_close(svc, 5)
    assert svc._v2_exit_reject_total[KEY] == 5

    svc._emit_v2_managed_sell = lambda *_a, **_k: _empty()
    _drive_close(svc, 1)

    assert svc._v2_exit_reject_total.get(KEY) == 5, (
        "a call that emitted NO ORDER AT ALL must not count as real progress"
    )


async def _empty():
    return []


def test_a_placed_close_DOES_still_clear_the_ceiling() -> None:
    """The control for the test above: real progress must still reset, or the bound fails CLOSED.

    ⛔ Without this, a fix for finding 2 that simply never resets would look identical.
    """
    svc = _emit_svc(statuses=["rejected"], close_on_fill=True)
    _drive_close(svc, 5)
    assert svc._v2_exit_reject_total[KEY] == 5

    svc._emit_v2_managed_sell = lambda *_a, **_k: _accepted()
    _drive_close(svc, 1)

    assert KEY not in svc._v2_exit_reject_total, (
        "an accepted close IS progress — the ceiling must reset, otherwise a healthy position "
        "accumulates toward a stand-down it never earned"
    )


async def _accepted():
    return [_Ev("accepted")]


# --------------------------------------------- FINDING 1: the total must not outlive its episode
def test_a_closed_episode_clears_the_total_so_the_NEXT_position_starts_clean() -> None:
    """⛔ THE POISON. 17 rejects + a legitimate close ⇒ the next position stands down after 3.

    `_v2_exit_end_episode` is the single place that ends an episode; every path that closes the
    managed row must go through it.
    """
    svc = _emit_svc(statuses=["rejected"])
    svc._v2_exit_reject_total = {KEY: 17}
    svc._v2_exit_close_failures = {KEY: 3}
    svc._v2_exit_stood_down = {KEY}

    svc._v2_exit_end_episode(KEY)

    assert KEY not in svc._v2_exit_reject_total
    assert KEY not in svc._v2_exit_close_failures
    assert KEY not in svc._v2_exit_stood_down


def test_the_emitter_finding_no_open_row_ends_the_episode() -> None:
    """The row is already gone — the episode is over, so its counters must go with it."""
    svc = _emit_svc(statuses=["rejected"], row=None)
    svc._v2_exit_reject_total = {KEY: 17}
    svc._v2_exit_stood_down = {KEY}

    _drive_close(svc, 1)

    assert KEY not in svc._v2_exit_reject_total, (
        "no open managed row means no episode; carrying 17 into the next position is the defect"
    )
    assert KEY not in svc._v2_exit_stood_down


def test_a_HELD_read_still_must_not_end_the_episode() -> None:
    """⛔ THE ASYMMETRY IS DELIBERATE — do not 'tidy' it away.

    `_v2_close_reconcile_flat` lifts the stand-down on a positively-HELD read but must LEAVE the
    absolute total, because in an exit-reservation jam we truthfully hold the position for the
    jam's whole duration. Making the total symmetric with the stand-down would rebuild the exact
    CHPT hole this ceiling exists to close.
    """
    svc = _svc(_PositionRead.HELD)
    svc._v2_exit_reject_total = {KEY: 17}
    svc._v2_exit_stood_down = {KEY}
    # reach the HELD branch: the reconcile returns early below its own threshold
    svc._v2_exit_close_failures = {KEY: OmsRiskService._V2_EXIT_RECONCILE_AFTER_FAILURES}

    asyncio.run(svc._v2_close_reconcile_flat(None, ACCT, SYM, object()))

    assert KEY not in svc._v2_exit_stood_down, "a HELD read may resume the loop"
    assert svc._v2_exit_reject_total[KEY] == 17, "but it must NOT clear the absolute ceiling"


# ═══════════════════════════════════════════════════════════════════════════════════════════
# codex-2 R1 on PR #893: ONE close can return MULTIPLE broker reports, and Schwab can answer
# `accepted` THEN `rejected` for the same order. The first fix tested "any positive status",
# which sees the `accepted` and clears the ceiling on the very tick that incremented it.
# ⛔ That is the ORIGINAL defect wearing a new mask: the total rises to N and is popped straight
# back, so the bound is unreachable exactly when a real storm is running.
# ═══════════════════════════════════════════════════════════════════════════════════════════

def test_accepted_then_rejected_on_ONE_close_is_not_progress() -> None:
    """⛔ A single close answering [accepted, rejected] must COUNT, not reset."""
    svc = _emit_svc(statuses=["accepted", "rejected"], close_on_fill=True)

    _drive_close(svc, 3)

    assert svc._v2_exit_reject_total.get(KEY) == 3, (
        "a close that ended REJECTED is not progress just because the broker also emitted an "
        "`accepted` report for it — any rejection in the batch disqualifies the whole close"
    )


def test_the_ceiling_is_still_reachable_under_mixed_status_reports() -> None:
    """THE STORM SHAPE. The bound must terminate the loop even when every close reports
    `accepted` alongside its `rejected` — otherwise the ceiling is decorative in exactly the
    case it exists for."""
    svc = _emit_svc(statuses=["accepted", "rejected"], close_on_fill=True)
    n = OmsRiskService._V2_EXIT_MAX_REJECTS_PER_EPISODE

    _drive_close(svc, n - 1)
    assert KEY not in svc._v2_exit_stood_down, "must not stand down one short of the bound"

    _drive_close(svc, 1)
    assert svc._v2_exit_reject_total[KEY] == n
    assert KEY in svc._v2_exit_stood_down, (
        "mixed accepted/rejected reports must still reach the ceiling and stop the hammering"
    )


def test_rejected_then_accepted_ordering_is_also_not_progress() -> None:
    """Order of the reports must not matter — the disqualifier is the REJECTION's presence."""
    svc = _emit_svc(statuses=["rejected", "accepted"], close_on_fill=True)

    _drive_close(svc, 3)

    assert svc._v2_exit_reject_total.get(KEY) == 3


def test_a_multi_report_close_with_NO_rejection_is_still_progress() -> None:
    """The control: a genuinely clean close reporting [accepted, filled] MUST still reset.

    ⛔ Without this, "never reset" would pass every test above while failing CLOSED — the loop
    would stand down on a position that is exiting perfectly well.
    """
    svc = _emit_svc(statuses=["rejected"], close_on_fill=True)
    _drive_close(svc, 5)
    assert svc._v2_exit_reject_total[KEY] == 5

    svc._emit_v2_managed_sell = lambda *_a, **_k: _accepted_filled()
    _drive_close(svc, 1)

    assert KEY not in svc._v2_exit_reject_total, (
        "a clean multi-report close is real progress and must clear the ceiling"
    )


async def _accepted_filled():
    return [_Ev("accepted"), _Ev("filled")]
