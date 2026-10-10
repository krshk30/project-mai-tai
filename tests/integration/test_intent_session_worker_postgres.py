"""Actual BUY/SELL/cancel calls lease their database phases off the quote loop."""

import asyncio
import threading
from time import sleep

import pytest

from project_mai_tai.db.models import TradeIntent
from project_mai_tai.oms.store import OmsStore
from sqlalchemy import select
import test_buy_submission_tokens_runtime as controls
import test_cancel_terminal_runtime as runtime

sessions = runtime.sessions
sdk = runtime.sdk


@pytest.mark.asyncio
@pytest.mark.parametrize("method,side", [
    ("ensure_strategy", "buy"),
    ("ensure_broker_account", "buy"),
    ("create_trade_intent", "buy"),
    ("record_risk_check", "buy"),
    ("get_or_create_order", "buy"),
    ("find_open_native_stop_guard_order", "sell"),
    ("find_open_exit_order", "sell"),
    ("get_virtual_position_quantity", "sell"),
    ("get_account_position", "sell"),
])
async def test_actual_intent_database_phase_runs_offloop(sessions, sdk, monkeypatch, method, side):
    caller = threading.get_ident()
    original = getattr(OmsStore, method)
    calls = []

    def slow(store, *args, **kwargs):
        assert threading.get_ident() != caller
        sleep(0.08)
        result = original(store, *args, **kwargs)
        calls.append(True)
        return result

    monkeypatch.setattr(OmsStore, method, slow)
    await controls.test_actual_oms_report_commit_does_not_block_quote_loop(
        sessions, sdk, monkeypatch, side
    )
    assert calls


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["find_open_order_for_cancel", "commit"])
async def test_actual_cancel_persists_before_publication_without_loop_db(sessions, sdk, monkeypatch, phase):
    _, leaf, _, guard = await controls.setup_protocol(sessions, monkeypatch)
    service = controls.oms(sessions, leaf, monkeypatch)
    service.broker_adapter = guard
    caller = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    publications = []
    target_class = sessions.class_ if phase == "commit" else OmsStore
    original = getattr(target_class, phase)

    def slow(owner, *args, **kwargs):
        assert threading.get_ident() != caller
        entered.set()
        assert release.wait(3)
        return original(owner, *args, **kwargs)

    async def publish(event):
        assert release.is_set()
        with sessions() as independent:
            intent = independent.scalar(select(TradeIntent).where(
                TradeIntent.id == event.payload.intent_db_id))
            assert intent.status == "rejected"
        publications.append(event)

    monkeypatch.setattr(target_class, phase, slow)
    monkeypatch.setattr(service, "_publish_order_event", publish)
    task = asyncio.create_task(service.process_trade_intent(runtime.cancel_event()))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        await asyncio.sleep(0.01)
        assert not task.done() and not publications
    finally:
        release.set()
        try:
            result = await task
        finally:
            await service._drain_cancel_terminal_evidence()
    assert not service.__dict__.get("_cancel_feedback_tasks")
    assert not service.__dict__.get("_cancel_terminal_assessment_tasks")
    assert not service.__dict__.get("_cancel_terminal_tasks")
    assert result and result[0].payload.reason == "cancel_target_not_found"
    assert publications == result
