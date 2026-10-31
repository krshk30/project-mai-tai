"""SOURCE UNRUN: pre-emitter read cancellation through the REAL _run_db.

Main only after source/roster/private fence review. Existing private SQLite and
actual snapshot reader run; only DB latency/session lifecycle are observed.
No owner/native/quantity/final-proof helper is replaced with success.
"""
import asyncio
from datetime import timedelta
from threading import Event

import pytest

from project_mai_tai.oms import wbexit3, webull_attach_riskoff as wb
from tests.unit.test_wbattach1_riskoff import make_case as make_case, closes
from tests.unit.test_wbexit2_route import owner_case, close_case
from tests.unit.test_wbexit3_obligation import enable, obligations


@pytest.mark.asyncio
async def test_resume_cancel_joins_real_snapshot_session_before_releasing_quote_claim(make_case, monkeypatch):
    case = await owner_case(make_case)
    service, factory, broker, _, clock = case
    await enable(case)
    broker.positions_unknown = True
    await close_case(case, "CW_HARD_STOP")
    before = obligations(case)
    assert len(before) == 1 and before[0]["phase"] == "RETRY"
    assert before[0]["active_token"] is None and not closes(broker)
    clock[0] += timedelta(seconds=11)
    quote = {"received_at": clock[0], "bid": broker.bid}
    assert wb._ATR_CALL.get() is None and wbexit3._ATTEMPT.get() is None

    entered, release, session_closed = Event(), Event(), Event()
    actual_snapshot = service._read_v2_managed_snapshot
    counts = {"sessions": 0, "reads": 0}

    class ObservedSession:
        def __enter__(self):
            counts["sessions"] += 1
            self.actual = factory()
            return self.actual.__enter__()

        def __exit__(self, *exception):
            try:
                return self.actual.__exit__(*exception)
            finally:
                session_closed.set()  # AFTER the real session has exited.

    def held_snapshot(db, account, symbol, close_on_fill):
        counts["reads"] += 1
        assert wb._ATR_CALL.get() is None and wbexit3._ATTEMPT.get() is None
        entered.set()
        assert release.wait(5), "CONTROLLED private read latency was not released"
        return actual_snapshot(db, account, symbol, close_on_fill)

    # _run_db itself is untouched: its actual asyncio.to_thread, actual private
    # session and actual pure DB snapshot callback must physically finish.
    monkeypatch.setattr(service, "session_factory", ObservedSession)
    monkeypatch.setattr(service, "_read_v2_managed_snapshot", held_snapshot)
    key = (broker.account, broker.symbol)
    requests, reads, cancels = len(broker.requests), broker.positions_reads, len(broker.cancelled)
    task = asyncio.create_task(wbexit3.resume_on_quote(service, *key, quote))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=3)
        assert key in service._wbexit3_quote_claims
        assert counts == {"sessions": 1, "reads": 1}
        task.cancel("first-resume-cancel")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done() and not session_closed.is_set()
        assert key in service._wbexit3_quote_claims
        task.cancel("second-resume-cancel")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done() and not session_closed.is_set()
        # A second quote cannot overlap the physically owned cancelled read.
        assert await wbexit3.resume_on_quote(service, *key, quote) is False
        assert counts == {"sessions": 1, "reads": 1}
        assert key in service._wbexit3_quote_claims
        assert (len(broker.requests), broker.positions_reads, len(broker.cancelled)) == (requests, reads, cancels)
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(task, timeout=3)
        assert cancelled.value.args == ("first-resume-cancel",)
        assert session_closed.is_set() and key not in service._wbexit3_quote_claims
        assert wb._ATR_CALL.get() is None and wbexit3._ATTEMPT.get() is None
        assert obligations(case) == before  # No new duty/nonce/fill or resolution.
        assert not closes(broker)
        assert (len(broker.requests), broker.positions_reads, len(broker.cancelled)) == (requests, reads, cancels)
    finally:
        release.set()
        if not task.done():
            task.cancel("fixture-cleanup")
        await asyncio.gather(task, return_exceptions=True)
        # Also drain the underlying real session on the OLD failing image;
        # a prematurely completed asyncio task must not hide a live DB thread.
        assert await asyncio.wait_for(asyncio.to_thread(session_closed.wait, 3), timeout=4)
