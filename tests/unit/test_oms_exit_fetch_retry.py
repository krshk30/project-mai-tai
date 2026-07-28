"""A TRANSIENT exit-fill fetch failure must not book the trade unpaired (live 2026-07-28).

⭐ WHY. `_fetch_oco_exit_detail` returned `None` for two completely different things:
  * the broker answered "there is no exit"           -> a real answer, close the row
  * we could not ASK (Webull 429, a rate limit)      -> temporary, and it told us nothing
Collapsing them logged `closing without a recorded exit (P&L for this trade stays unpaired)` and
lost that trade's P&L FOREVER on a temporary error -- the exact blackout the OCO exit capture was
built to close. Live: CNET at 16:11 ET.

⛔ THE BOUND MATTERS AS MUCH AS THE RETRY. An open managed row is what blocks fan-out re-entry via
`fanout_webull_collision_managed` -- the lag P0-a was enabled to remove. So a symbol whose fetch
keeps failing must NOT pin its row open forever: after `_MAX_EXIT_FETCH_DEFERRALS` we close anyway
and accept the unpaired trade. Protecting entries outranks bookkeeping.
"""
from __future__ import annotations

import asyncio
import logging

from project_mai_tai.oms.service import (
    _EXIT_FETCH_FAILED,
    OmsRiskService,
)


def _svc() -> OmsRiskService:
    """Bare instance -- these are pure decision helpers, no DB or broker involved."""
    svc = OmsRiskService.__new__(OmsRiskService)
    svc._oco_exit_fetch_deferrals = {}
    svc.logger = logging.getLogger("test-oms-exit-retry")   # __init__ normally supplies this
    return svc


def test_the_sentinel_is_not_none_and_is_truthy() -> None:
    """PINS THE TRAP. It must be distinguishable from 'no exit' -- and because it is TRUTHY, every
    caller has to test identity BEFORE any `if detail:` branch or it sails into the close path."""
    assert _EXIT_FETCH_FAILED is not None
    assert bool(_EXIT_FETCH_FAILED) is True


def test_first_failures_hold_the_row_open() -> None:
    svc = _svc()
    for i in range(1, OmsRiskService._MAX_EXIT_FETCH_DEFERRALS + 1):
        assert svc._defer_for_exit_fetch("live:orb", "CNET") is True, f"gave up on retry {i}"


def test_it_gives_up_after_the_bound_so_the_row_cannot_block_entries() -> None:
    """THE SAFETY DIRECTION. Without this, a permanently-failing symbol pins its managed row open
    and silently blocks every future fan-out entry on that name."""
    svc = _svc()
    for _ in range(OmsRiskService._MAX_EXIT_FETCH_DEFERRALS):
        svc._defer_for_exit_fetch("live:orb", "CNET")
    assert svc._defer_for_exit_fetch("live:orb", "CNET") is False
    assert ("live:orb", "CNET") not in svc._oco_exit_fetch_deferrals, "counter must reset on give-up"


def test_the_bound_value_is_pinned() -> None:
    assert OmsRiskService._MAX_EXIT_FETCH_DEFERRALS == 3


def test_symbols_are_counted_independently() -> None:
    """One flaky symbol must not spend another symbol's retry budget."""
    svc = _svc()
    for _ in range(OmsRiskService._MAX_EXIT_FETCH_DEFERRALS):
        svc._defer_for_exit_fetch("live:orb", "CNET")
    assert svc._defer_for_exit_fetch("live:orb", "BIYA") is True
    assert svc._defer_for_exit_fetch("live:schwab_1m_v2", "CNET") is True


def test_a_broker_exception_maps_to_the_sentinel_not_none() -> None:
    """End-to-end on the helper: a raising adapter must yield the retryable sentinel."""
    svc = _svc()

    class _Boom:
        async def fetch_oco_exit_fill(self, *_a, **_k):
            raise RuntimeError("HTTP Status: 429, Code: TOO_MANY_REQUESTS")

    class _S:
        oms_record_native_oco_exit_fills_enabled = True

    svc.settings = _S()
    svc.broker_adapter = _Boom()
    got = asyncio.run(svc._fetch_oco_exit_detail("live:orb", "CNET", "BASECOID"))
    assert got is _EXIT_FETCH_FAILED, "a 429 was collapsed into 'no exit' and the P&L was lost"


def test_a_genuine_no_exit_is_still_plain_none() -> None:
    """The other direction: 'the broker says there is no exit' must NOT trigger retries."""
    svc = _svc()

    class _Empty:
        async def fetch_oco_exit_fill(self, *_a, **_k):
            return None

    class _S:
        oms_record_native_oco_exit_fills_enabled = True

    svc.settings = _S()
    svc.broker_adapter = _Empty()
    assert asyncio.run(svc._fetch_oco_exit_detail("live:orb", "CNET", "BASECOID")) is None
