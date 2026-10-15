import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest

from project_mai_tai.market_data.stage_diagnostics import StageDiagnostics
from tests.unit.test_momwarn1_live_health import setup  # noqa: F401


def test_timing_is_bounded_and_does_not_claim_gil_or_task_cpu():
    diagnostics = StageDiagnostics()
    for _ in range(100):
        assert diagnostics.sync("snapshot_encode", lambda: 42) == 42
    result = diagnostics.snapshot()
    assert result["counts"]["snapshot_encode"][0] == 100
    assert len(result["recent"]) <= 8
    assert len(json.dumps(result, separators=(",", ":")).encode()) <= 4096
    assert "NOT_GIL_PROOF" in result["scope"]
    row = result["recent"][-1]
    assert row[2] <= row[3] <= row[4]
    assert row[5] >= 0


@pytest.mark.asyncio
async def test_thread_queue_and_real_started_done_are_separate():
    diagnostics = StageDiagnostics()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    release, occupied = threading.Event(), threading.Event()
    def blocker():
        occupied.set()
        release.wait(2)
    future = loop.run_in_executor(executor, blocker)
    while not occupied.is_set():
        await asyncio.sleep(0)
    task = asyncio.create_task(diagnostics.thread("health_config", lambda: 7))
    await asyncio.sleep(.01)
    row = diagnostics.snapshot()["recent"][-1]
    assert row[3] is None and row[4] is None
    release.set()
    assert await task == 7
    await future
    row = diagnostics.snapshot()["recent"][-1]
    assert row[3] - row[2] >= 5_000_000
    assert row[4] >= row[3] and row[5] >= 0
    executor.shutdown(wait=True)
    loop.set_default_executor(ThreadPoolExecutor())


@pytest.mark.asyncio
async def test_timeout_and_cancel_are_not_swallowed_or_relaxed():
    diagnostics = StageDiagnostics()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(diagnostics.asynchronous("health_total", asyncio.sleep(.1)), timeout=.01)
    result = diagnostics.snapshot()
    row = result["recent"][-1]
    assert row[5] is None
    assert row[7] == "CancelledError"
    assert result["counts"]["health_total"][0] == 1


def test_failed_sync_preserves_exception():
    diagnostics = StageDiagnostics()
    def fail():
        raise ValueError("original")
    with pytest.raises(ValueError, match="original"):
        diagnostics.sync("health_sources", fail)
    assert diagnostics.snapshot()["recent"][-1][7] == "ValueError"


def test_slow_count_and_thread_cpu_are_different_from_wall_wait():
    diagnostics = StageDiagnostics()
    diagnostics.sync("snapshot_sdk_fetch_parse", time.sleep, .51)
    result = diagnostics.snapshot()
    counts = result["counts"]["snapshot_sdk_fetch_parse"]
    assert counts[0:2] == [1, 1]
    assert counts[2] >= 500
    assert counts[3] < counts[2]
    assert len(result["slow"]) == 1


@pytest.mark.asyncio
async def test_actual_reader_three_hops_keep_same_health_and_redis_read(setup):  # noqa: F811
    env = setup()
    diagnostics = StageDiagnostics()
    env.reader.stage_diagnostics = diagnostics
    health = await env.reader.read_live(env.client, "controlled")
    assert health.oms_sync_seconds == 2.3
    assert health.v2_bar_lateness_seconds == 4
    env.client.xrevrange.assert_called_once_with("controlled:strategy-state-isolated", count=32)
    counts = diagnostics.snapshot()["counts"]
    assert set(counts) == {"health_config", "health_redis", "health_state_decode", "health_sources", "health_validate"}
    assert all(count[0] == 1 for count in counts.values())


@pytest.mark.asyncio
async def test_sdk_decode_is_scoped_to_snapshot_fetch_no_other_requests_change():
    from project_mai_tai.market_data.massive_provider import MassiveSnapshotProvider
    class Client:
        def _decode(self, response):
            return json.loads(response)
        def get_snapshot_all(self, *args, **kwargs):
            return self._decode("[]")
    provider = MassiveSnapshotProvider("CONTROLLED_NOT_A_KEY")
    provider._client = Client()
    diagnostics = StageDiagnostics()
    provider.stage_diagnostics = diagnostics
    assert await diagnostics.thread("snapshot_fetch", provider.fetch_all_snapshots) == []
    counts = diagnostics.snapshot()["counts"]
    assert counts["snapshot_json_decode"][0] == 1
    assert counts["snapshot_normalize"][0] == 1
    assert provider._client._decode('{"unchanged":true}') == {"unchanged": True}
    assert diagnostics.snapshot()["counts"]["snapshot_json_decode"][0] == 1
