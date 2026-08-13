"""A cancel is fire-and-forget today: we treat the ATTEMPT as the OUTCOME.

⛔⭐⭐ THE LIVE COST. FRTT 2026-08-11 13:01:02 — the cancel was emitted, died on the network
(`upstream connect error ... connection termination`), and the order stayed WORKING and unowned at
the broker for **136 minutes** until the operator killed it by hand. #679 detects that shape in
~2 minutes; nothing cures it.

⛔ Neither an exception NOR an `accepted`/`PENDING_CANCEL` report tells you what happened to the
order. Both are unknowns. The only way to resolve an unknown is to read the order back — which is
what `_verify_cancel_landed` does, and what these tests pin.

⚠️ Every test here binds to the REAL functions. An earlier file in this repo re-implemented the rule
locally and stayed green through a revert — the "tests a copy, not the code" trap.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from project_mai_tai.oms import service as svc


class _Report:
    def __init__(self, event_type: str) -> None:
        self.event_type = event_type


class _Adapter:
    """Scripted broker. `reads` is consumed one per fetch; a string becomes a report, an
    Exception instance is raised, and None means 'no answer'."""

    def __init__(self, reads: list[object] | None = None, submit_raises: bool = False) -> None:
        self.reads = list(reads or [])
        self.submit_raises = submit_raises
        self.submits = 0
        self.fetches = 0

    async def submit_order(self, request):  # noqa: ANN001
        self.submits += 1
        if self.submit_raises:
            raise RuntimeError("upstream connect error ... connection termination")
        return []

    async def fetch_order_update(self, request):  # noqa: ANN001
        self.fetches += 1
        item = self.reads.pop(0) if self.reads else None
        if isinstance(item, Exception):
            raise item
        return _Report(item) if isinstance(item, str) else None


def _service(adapter: _Adapter, **overrides) -> svc.OmsRiskService:
    """Build the service without __init__ — the repo's existing test-helper pattern."""
    s = object.__new__(svc.OmsRiskService)
    s.settings = SimpleNamespace(
        oms_cancel_verify_enabled=True,
        oms_cancel_verify_attempts=overrides.get("attempts", 2),
        oms_cancel_verify_interval_seconds=0.0,   # no real sleeping in tests
        oms_cancel_verify_resubmits=overrides.get("resubmits", 1),
    )
    s.logger = logging.getLogger("test-cancel-verify")
    s.broker_adapter = adapter
    return s


async def _verify(s: svc.OmsRiskService) -> str | None:
    return await s._verify_cancel_landed(
        request=SimpleNamespace(client_order_id="coid-1"),
        symbol="FRTT",
        account_name="live:schwab_1m_v2",
        client_order_id="coid-1",
        broker_order_id="1007548980530",
    )


# --------------------------------------------------------------------------- the settled-status set
def test_the_settled_set_is_pinned() -> None:
    """Pin the VALUES. A status quietly added or dropped changes what counts as proof."""
    assert set(svc._CANCEL_TARGET_SETTLED_STATUSES) == {"cancelled", "filled", "rejected", "expired"}


def test_ACCEPTED_is_not_settled_which_is_the_whole_bug() -> None:
    """⛔ THE MUTATION THAT RE-OPENS IT. Schwab answers a just-issued DELETE with PENDING_CANCEL,
    which maps to `accepted`. If `accepted` ever enters this set, verification declares victory on
    the exact report that told us nothing — and FRTT sits working for another 136 minutes."""
    for not_proof in ("accepted", "pending", "submitted", "partially_filled", "working"):
        assert not_proof not in svc._CANCEL_TARGET_SETTLED_STATUSES


def test_settled_set_is_NOT_the_intent_set() -> None:
    """They answer different questions. `expired` settles an ORDER (it is off the book) but is not
    an outcome a cancel INTENT may claim. Collapsing them loses that distinction."""
    assert set(svc._CANCEL_TARGET_SETTLED_STATUSES) != set(svc._TERMINAL_INTENT_STATUSES)
    assert "expired" in svc._CANCEL_TARGET_SETTLED_STATUSES
    assert "expired" not in svc._TERMINAL_INTENT_STATUSES


# --------------------------------------------------------------------------------- the happy path
def test_a_settled_read_confirms_and_stops_reading() -> None:
    a = _Adapter(reads=["cancelled"])
    assert asyncio.run(_verify(_service(a))) == "cancelled"
    assert a.fetches == 1, "must stop at the first proof, not keep polling"
    assert a.submits == 0, "must not re-submit a cancel that already landed"


def test_a_FILLED_target_is_settled_and_is_NOT_reported_as_cancelled() -> None:
    """⛔ Do not paper over a lost race. If the order filled instead of cancelling, say so."""
    assert asyncio.run(_verify(_service(_Adapter(reads=["filled"])))) == "filled"


# ------------------------------------------------------------------------------ the retry that cures
def test_a_still_working_order_triggers_a_RESUBMIT() -> None:
    """THE CURE. Reads say `accepted` (still working) -> re-send the cancel."""
    a = _Adapter(reads=["accepted", "accepted", "cancelled"])
    assert asyncio.run(_verify(_service(a, attempts=2, resubmits=1))) == "cancelled"
    assert a.submits == 1, "exactly one re-submit after the first round of reads failed to settle"


def test_resubmits_zero_means_no_resubmit() -> None:
    """Pin the knob: with resubmits=0 we verify but never re-send."""
    a = _Adapter(reads=["accepted", "accepted"])
    asyncio.run(_verify(_service(a, attempts=2, resubmits=0)))
    assert a.submits == 0


# ------------------------------------------------------------------------- it must never go quiet
def test_an_unconfirmed_cancel_WARNS_loudly(caplog: pytest.LogCaptureFixture) -> None:
    """⚠️ A cancel path that can fail silently is the same defect class as the one being fixed."""
    a = _Adapter(reads=["accepted", "accepted", "accepted", "accepted"])
    with caplog.at_level(logging.WARNING):
        asyncio.run(_verify(_service(a, attempts=2, resubmits=1)))
    assert "[OMS-CANCEL-UNCONFIRMED]" in caplog.text
    assert "coid-1" in caplog.text and "1007548980530" in caplog.text, "must carry the ids to act on"


def test_a_read_that_RAISES_does_not_end_verification(caplog: pytest.LogCaptureFixture) -> None:
    """A failed read is an unknown too — never conclude 'gone' from an error."""
    a = _Adapter(reads=[RuntimeError("boom"), "cancelled"])
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(_verify(_service(a, attempts=2, resubmits=0))) == "cancelled"
    assert "[OMS-CANCEL-VERIFY-READ-FAILED]" in caplog.text


def test_an_unreadable_order_is_reported_not_assumed(caplog: pytest.LogCaptureFixture) -> None:
    """Order never readable -> UNCONFIRMED, not a cheerful default."""
    a = _Adapter(reads=[None, None, None, None])
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(_verify(_service(a, attempts=2, resubmits=1))) is None
    assert "[OMS-CANCEL-UNCONFIRMED]" in caplog.text
    assert "UNREADABLE" in caplog.text


# ------------------------------------------------------------------------------------ the wiring
def test_process_cancel_intent_actually_spawns_verification() -> None:
    """Pins the WIRING, not just the helper. A correct verifier nobody calls is worthless —
    the same trap `test_oms_cancel_intent_terminal` was written to catch."""
    import inspect

    src = inspect.getsource(svc.OmsRiskService._process_cancel_intent)
    assert "_spawn_cancel_verification(" in src
    assert "oms_cancel_verify_enabled" in src


def test_a_RAISED_submit_still_verifies_when_enabled() -> None:
    """⛔⭐ THE FRTT SHAPE. The cancel call died on the network. That is an UNKNOWN, not a failure:
    the request may well have reached the broker. With verification on we must NOT propagate the
    raise and walk away — we must read the order back."""
    import inspect

    src = inspect.getsource(svc.OmsRiskService._process_cancel_intent)
    assert "if not verify_enabled:" in src and "raise" in src, "flag OFF must still propagate"
    assert "[OMS-CANCEL-SUBMIT-RAISED]" in src


def test_flag_off_is_byte_identical() -> None:
    """The default must change nothing: no spawn, and the raise propagates as it always has."""
    from project_mai_tai.settings import Settings

    assert Settings().oms_cancel_verify_enabled is False
    assert Settings().oms_cancel_verify_resubmits == 1
    assert Settings().oms_cancel_verify_attempts == 3
