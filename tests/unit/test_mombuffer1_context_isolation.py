"""Same-process/task composition controls. Author-only until main executes."""

import asyncio

import pytest

from project_mai_tai.momentum_live.entry_wire import _ENTRY_WIRE, entry_wire_check, entry_wire_guard
from tests.unit.test_mombuffer1_incap_reprice import priced as priced  # noqa: F401
from tests.unit.test_mombuffer1_review_guards import (
    test_actual_adapter_local_guard_after_wait_is_terminal_and_never_calls_transport as native_refusal_case,
)
from tests.unit.test_momlive1_execution import setup as execution_setup
from tests.unit.test_momlive1_virtual_clear import (
    test_bbci_missing_target_then_broker_zero_keeps_ownership_until_actual_exit as virtual_clear_case,
)
from tests.unit.test_momstuck2_prewire_refusal import env  # noqa: F401
from tests.unit.test_webull_adapter import fake_sdk as fake_sdk  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_mombuffer_native_refusal_then_virtual_clear_native_in_same_task(
    priced, fake_sdk, monkeypatch, provider,
):
    owner = asyncio.current_task()
    assert _ENTRY_WIRE.get() is None
    # Restore only the adapter mocks; never clear/reset the production ContextVar.
    with monkeypatch.context() as patches:
        await native_refusal_case(priced, fake_sdk, patches, provider)
    assert asyncio.current_task() is owner and _ENTRY_WIRE.get() is None
    # Run the ORIGINAL full body, including the bad-native-receipt rejection,
    # retained ownership, real-exit accounting, one fill and no-cancel assertions.
    await virtual_clear_case("native")
    assert asyncio.current_task() is owner and _ENTRY_WIRE.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["exception", "cancelled_child"])
async def test_entry_scope_unwinds_before_virtual_clear_without_manual_reset(ending):
    h = execution_setup()
    _, request, _ = await h.executor.admit(h.signal)
    assert _ENTRY_WIRE.get() is None

    async def scope():
        with entry_wire_check(request, lambda: {}) as guard:
            assert entry_wire_guard(request) is guard
            if ending == "exception":
                raise RuntimeError("SYNTHETIC scope exit")
            await asyncio.Event().wait()

    if ending == "exception":
        with pytest.raises(RuntimeError, match="SYNTHETIC scope exit"):
            await scope()
    else:
        entered = asyncio.Event()
        exited = asyncio.Event()

        async def cancellable():
            try:
                with entry_wire_check(request, lambda: {}) as guard:
                    assert entry_wire_guard(request) is guard
                    entered.set()
                    await asyncio.Event().wait()
            finally:
                # Check the child's context AFTER the production manager unwinds,
                # not just the unaffected parent task's copy of the ContextVar.
                assert _ENTRY_WIRE.get() is None
                exited.set()

        task = asyncio.create_task(cancellable())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert exited.is_set()
    assert _ENTRY_WIRE.get() is None
    await virtual_clear_case("native")
    assert _ENTRY_WIRE.get() is None
