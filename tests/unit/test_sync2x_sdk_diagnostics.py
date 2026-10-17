"""Source-authored SDK-label controls. No wire/latency qualification. UNRUN."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from project_mai_tai import sync_sdk_diagnostics as diagnostics
from project_mai_tai.broker_adapters.webull import _EndpointCountingClient
from project_mai_tai.oms.service import OmsRiskService
from tests.unit.test_webull_adapter import _FakeClient, _adapter, fake_sdk as fake_sdk


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ok", "exception", "cancelled"])
async def test_actual_sync_finally_labels_sdk_not_wire_and_preserves_outcome(caplog, outcome):
    service = object.__new__(OmsRiskService)
    service.settings = SimpleNamespace(oms_native_oco_confirmation_max_age_seconds=30)
    service.logger = logging.getLogger("sync2x-controlled")
    original = RuntimeError("controlled original failure") if outcome == "exception" else asyncio.CancelledError()
    delegate_calls = []
    class Delegate:
        def get_response(self, request):
            delegate_calls.append(request)
            return "controlled reply"
    client = _EndpointCountingClient(Delegate(), lambda *_args, **_kwargs: None)
    async def actual_pass(*, account_names):
        diagnostics.log_logical_accounts(service.logger, ["live:atr", "live:orb"])
        assert await asyncio.to_thread(client.get_response, "controlled request") == "controlled reply"
        if outcome != "ok":
            raise original
        return {"accounts": 2}
    service._sync_broker_state_pass = actual_pass
    with caplog.at_level(logging.INFO):
        if outcome == "ok":
            assert await service.sync_broker_state() == {"accounts": 2}
        else:
            with pytest.raises(type(original)) as caught:
                await service.sync_broker_state()
            assert caught.value is original
    assert delegate_calls == ["controlled request"] and diagnostics.CURRENT.get() is None
    start = [record.message for record in caplog.records if "logical_accounts=" in record.message]
    end = [record.message for record in caplog.records if "phase=end" in record.message]
    assert len(start) == len(end) == 1
    assert 'logical_accounts=["live:atr","live:orb"] physical_http_reads=PENDING' in start[0]
    assert "physical_http_reads=UNKNOWN sdk_invocations=1 sdk_scope=webull_delegate_until_end" in end[0]


@pytest.mark.asyncio
async def test_real_positions_cache_hit_does_not_add_sdk_invocation(fake_sdk, monkeypatch):
    from project_mai_tai.broker_adapters import webull as module

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 100.0))
    client = _FakeClient({"positions": {"holdings": [], "has_next": False}})
    leaf = _adapter(client)
    frame = diagnostics.SdkFrame()
    token = diagnostics.CURRENT.set(frame)
    try:
        assert await leaf.list_account_positions("live:orb") == []
        assert await leaf.list_account_positions("live:orb") == []
    finally:
        count = frame.finish()
        diagnostics.CURRENT.reset(token)
    assert count == 1 and client.calls == {"positions": 1}


def test_sdk_exception_and_diagnostic_failure_do_not_rewrite_original(monkeypatch):
    original = RuntimeError("controlled SDK failure")
    class Delegate:
        def get_response(self, _request):
            raise original
    frame = diagnostics.SdkFrame()
    token = diagnostics.CURRENT.set(frame)
    try:
        monkeypatch.setattr(frame, "note", lambda: (_ for _ in ()).throw(ValueError("diagnostic only")))
        client = _EndpointCountingClient(Delegate(), lambda *_args, **_kwargs: None)
        with pytest.raises(RuntimeError) as caught:
            client.get_response(object())
        assert caught.value is original
        assert frame.finish() == "UNKNOWN"
    finally:
        diagnostics.CURRENT.reset(token)


@pytest.mark.asyncio
async def test_detached_task_inherited_closed_frame_cannot_contaminate_next_pass():
    ready = asyncio.Event()
    old = diagnostics.SdkFrame()
    token = diagnostics.CURRENT.set(old)
    async def late_sdk_call():
        await ready.wait()
        diagnostics.note_sdk_invocation()
    task = asyncio.create_task(late_sdk_call())
    assert old.finish() == 0
    diagnostics.CURRENT.reset(token)
    current = diagnostics.SdkFrame()
    token = diagnostics.CURRENT.set(current)
    try:
        ready.set()
        await task
        assert current.finish() == old.invocations == 0
    finally:
        diagnostics.CURRENT.reset(token)
