"""Actual OMS BUY/SELL reports persist audit rows through the exclusive worker."""
import threading
from time import sleep

import pytest

from project_mai_tai.oms.store import OmsStore
import test_buy_submission_tokens_runtime as controls
import test_cancel_terminal_runtime as runtime

sessions = runtime.sessions
sdk = runtime.sdk


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_actual_pg_audit_flush_offloop_before_report_publication(sessions, sdk, monkeypatch, side):
    loop_thread = threading.get_ident()
    original = OmsStore.append_order_event
    writes = []
    def slow_audit(store, *args, **kwargs):
        assert threading.get_ident() != loop_thread
        sleep(0.15)
        result = original(store, *args, **kwargs)
        writes.append(True)
        return result
    monkeypatch.setattr(OmsStore, "append_order_event", slow_audit)
    await controls.test_actual_oms_report_commit_does_not_block_quote_loop(sessions, sdk, monkeypatch, side)
    assert writes == [True]
