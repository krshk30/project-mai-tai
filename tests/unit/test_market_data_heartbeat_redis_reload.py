from __future__ import annotations

import asyncio
import logging

import pytest
from redis.exceptions import (
    AuthenticationError,
    BusyLoadingError,
    ResponseError,
    TimeoutError as RedisTimeoutError,
)

from project_mai_tai.market_data import gateway
from project_mai_tai.market_data.gateway import MarketDataGatewayService
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

    async def xrevrange(self, *args: object, **kwargs: object) -> list[object]:
        del args, kwargs
        return []

    async def aclose(self) -> None:
        return None


class EmptySnapshots:
    def fetch_all_snapshots(self) -> list[object]:
        return []


class IdleTradeStream:
    async def start(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def stop(self) -> None:
        return None

    async def sync_subscriptions(self, symbols: object) -> None:
        del symbols


class LoadedReferenceCache:
    def load_from_cache(self) -> bool:
        return True

    def ticker_count(self) -> int:
        return 0


def _service(redis_client: ScriptedRedis) -> MarketDataGatewayService:
    return MarketDataGatewayService(
        settings=Settings(
            redis_stream_prefix="test",
            market_data_reference_refresh_interval_seconds=0,
        ),
        redis_client=redis_client,  # type: ignore[arg-type]
        snapshot_provider=EmptySnapshots(),  # type: ignore[arg-type]
        trade_stream=IdleTradeStream(),  # type: ignore[arg-type]
        reference_cache=LoadedReferenceCache(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_busy_loading_retries_then_publishes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis_client = ScriptedRedis(
        [BusyLoadingError("loading"), BusyLoadingError("loading"), "1-0"]
    )
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(gateway.asyncio, "sleep", record_sleep)
    caplog.set_level(logging.INFO, logger="market-data-gateway")

    await _service(redis_client)._publish_heartbeat("healthy", {})

    assert redis_client.calls == 3
    assert sleeps == [0.5, 1.0]
    assert "[MARKET-DATA-HEARTBEAT-RETRY] attempt=1/5" in caplog.text
    assert "[MARKET-DATA-HEARTBEAT-RECOVERED] attempt=3/5" in caplog.text


@pytest.mark.asyncio
async def test_redis_timeout_retries_then_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = ScriptedRedis([RedisTimeoutError("timed out"), "1-0"])
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(gateway.asyncio, "sleep", record_sleep)

    await _service(redis_client)._publish_heartbeat("healthy", {})

    assert redis_client.calls == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_busy_loading_retry_is_bounded_and_exhaustion_propagates(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis_client = ScriptedRedis([BusyLoadingError("loading") for _ in range(5)])
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(gateway.asyncio, "sleep", record_sleep)
    caplog.set_level(logging.INFO, logger="market-data-gateway")

    with pytest.raises(BusyLoadingError, match="loading"):
        await _service(redis_client)._publish_heartbeat("healthy", {})

    assert redis_client.calls == 5
    assert sleeps == [0.5, 1.0, 2.0, 4.0]
    assert "attempt=5/5 transient_failures=5 outcome=transient_exhausted" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [ResponseError("WRONGTYPE"), AuthenticationError("bad credentials")],
    ids=["response-error", "authentication-error"],
)
async def test_non_transient_error_propagates_without_retry(
    failure: BaseException,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis_client = ScriptedRedis([failure])
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(gateway.asyncio, "sleep", record_sleep)
    caplog.set_level(logging.INFO, logger="market-data-gateway")

    with pytest.raises(type(failure), match=str(failure)):
        await _service(redis_client)._publish_heartbeat("healthy", {})

    assert redis_client.calls == 1
    assert sleeps == []
    assert "outcome=non_transient_propagated" in caplog.text
    assert "[MARKET-DATA-HEARTBEAT-RETRY]" not in caplog.text


@pytest.mark.asyncio
async def test_run_exits_when_heartbeat_task_dies(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _service(ScriptedRedis(["1-0", "2-0"]))

    async def heartbeat_dies(_stop_event: asyncio.Event) -> None:
        raise RuntimeError("synthetic heartbeat death")

    monkeypatch.setattr(service, "_heartbeat_loop", heartbeat_dies)
    monkeypatch.setattr(gateway, "_install_signal_handlers", lambda _event: None)
    caplog.set_level(logging.INFO, logger="market-data-gateway")

    with pytest.raises(RuntimeError, match="synthetic heartbeat death"):
        await asyncio.wait_for(service.run(), timeout=1.0)

    assert "[MARKET-DATA-HEARTBEAT-TASK-DIED]" in caplog.text
    assert "outcome=process_exit reason=exception error=RuntimeError" in caplog.text
