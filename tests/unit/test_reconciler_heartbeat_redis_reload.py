from __future__ import annotations

import logging

import pytest
from redis.exceptions import (
    AuthenticationError,
    BusyLoadingError,
    ConnectionError as RedisConnectionError,
    ResponseError,
    TimeoutError as RedisTimeoutError,
)

from project_mai_tai.reconciliation import service as reconciliation_service
from project_mai_tai.reconciliation.service import ReconciliationService
from project_mai_tai.settings import Settings


class ScriptedRedis:
    def __init__(self, outcomes: list[BaseException | str]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def xadd(self, *args: object, **kwargs: object) -> str:
        del args, kwargs
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _service(redis_client: ScriptedRedis) -> ReconciliationService:
    return ReconciliationService(
        settings=Settings(redis_stream_prefix="test"),
        redis_client=redis_client,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_busy_loading_retries_then_marks_recovery(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis_client = ScriptedRedis(
        [
            BusyLoadingError("Redis is loading the dataset in memory"),
            BusyLoadingError("Redis is loading the dataset in memory"),
            "1-0",
        ]
    )
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(reconciliation_service.asyncio, "sleep", record_sleep)
    caplog.set_level(logging.INFO, logger="reconciler")

    await _service(redis_client)._publish_heartbeat("healthy", {})

    assert redis_client.calls == 3
    assert sleeps == [0.5, 1.0]
    assert "[RECONCILER-HEARTBEAT-RETRY] attempt=1/5" in caplog.text
    assert "[RECONCILER-HEARTBEAT-RECOVERED] attempt=3/5" in caplog.text
    assert "transient_failures=2 outcome=published" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transient_error",
    [RedisConnectionError("connection closed"), RedisTimeoutError("timed out")],
    ids=["connection-drop", "timeout"],
)
async def test_redis_restart_transport_siblings_retry_then_succeed(
    transient_error: BaseException,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = ScriptedRedis([transient_error, "1-0"])
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(reconciliation_service.asyncio, "sleep", record_sleep)

    await _service(redis_client)._publish_heartbeat("healthy", {})

    assert redis_client.calls == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "non_transient_error",
    [ResponseError("WRONGTYPE"), AuthenticationError("bad credentials")],
    ids=["response-error", "authentication-error"],
)
async def test_non_transient_redis_error_propagates_without_retry(
    non_transient_error: BaseException,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis_client = ScriptedRedis([non_transient_error])
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(reconciliation_service.asyncio, "sleep", record_sleep)
    caplog.set_level(logging.INFO, logger="reconciler")

    with pytest.raises(type(non_transient_error), match=str(non_transient_error)):
        await _service(redis_client)._publish_heartbeat("healthy", {})

    assert redis_client.calls == 1
    assert sleeps == []
    assert "[RECONCILER-HEARTBEAT-FAILED] attempt=1/5" in caplog.text
    assert (
        f"outcome=non_transient_propagated error={type(non_transient_error).__name__}"
        in caplog.text
    )
    assert "[RECONCILER-HEARTBEAT-RETRY]" not in caplog.text
    assert "[RECONCILER-HEARTBEAT-RECOVERED]" not in caplog.text


@pytest.mark.asyncio
async def test_transient_reload_exhaustion_propagates_after_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis_client = ScriptedRedis([BusyLoadingError("loading") for _ in range(5)])
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(reconciliation_service.asyncio, "sleep", record_sleep)
    caplog.set_level(logging.INFO, logger="reconciler")

    with pytest.raises(BusyLoadingError, match="loading"):
        await _service(redis_client)._publish_heartbeat("healthy", {})

    assert redis_client.calls == 5
    assert sleeps == [0.5, 1.0, 2.0, 4.0]
    assert "[RECONCILER-HEARTBEAT-FAILED] attempt=5/5" in caplog.text
    assert "transient_failures=5 outcome=transient_exhausted" in caplog.text
    assert "[RECONCILER-HEARTBEAT-RECOVERED]" not in caplog.text


@pytest.mark.asyncio
async def test_clean_publish_stays_quiet_without_retry_markers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis_client = ScriptedRedis(["1-0"])
    caplog.set_level(logging.INFO, logger="reconciler")

    await _service(redis_client)._publish_heartbeat("healthy", {})

    assert redis_client.calls == 1
    assert "[RECONCILER-HEARTBEAT-RETRY]" not in caplog.text
    assert "[RECONCILER-HEARTBEAT-RECOVERED]" not in caplog.text
    assert "[RECONCILER-HEARTBEAT-FAILED]" not in caplog.text
