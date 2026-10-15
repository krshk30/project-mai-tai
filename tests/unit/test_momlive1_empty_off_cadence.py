import asyncio
import json
import inspect
import textwrap
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from project_mai_tai.events import MarketDataSubscriptionEvent
from project_mai_tai.db.models import VirtualPosition
from project_mai_tai.market_data.gateway import MarketDataGatewayService
from project_mai_tai.momentum_live.admission import Refusal, STRATEGY_CODE
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from project_mai_tai.settings import Settings
from tests.unit.test_market_data_gateway import (
    FakeRedis, FakeReferenceCache, FakeSnapshotProvider, FakeTradeStream,
)
from tests.unit.test_momlive1_guard_owner_compat import daily_context as _daily_context
from tests.unit.test_momlive1_runtime import setup
from tests.unit.test_momlive1_admission import ledger, signal

guard_context = _daily_context


class OwnerRedis(FakeRedis):
    async def xadd(self, stream, fields, **kwargs):
        result = await super().xadd(stream, fields, **kwargs)
        if stream.endswith(":market-data-subscriptions"):
            await self.gateway.apply_subscription_event(
                MarketDataSubscriptionEvent.model_validate_json(fields["data"]), message_id=result)
        return result


class GuardRedis:
    def __init__(self, redis):
        self.redis = redis

    def xrevrange(self, name, count):
        assert count == 1
        for stream, payload, _ in reversed(self.redis.entries):
            if stream == name:
                return [("1-0", {"data": json.dumps(payload)})]
        return []

    def hgetall(self, name):
        return self.redis.hashes.get(name, {})


async def started_off():
    now, broker, service, on = setup()
    redis = OwnerRedis()
    gateway = MarketDataGatewayService(
        settings=Settings(_env_file=None, market_data_momentum_warning_enabled=False),
        redis_client=redis, snapshot_provider=FakeSnapshotProvider(),
        trade_stream=FakeTradeStream(), reference_cache=FakeReferenceCache())
    gateway._desired_symbols_by_consumer.update({name: set() for name in
        ("strategy-engine", "schwab-1m-v2", "orb", "orb-schwab", "momentum-paper")})
    redis.gateway = gateway
    service.redis = redis
    on.identity = "111:222"
    assert await on.start()
    await on.close()
    service.settings.momentum_live_enabled = False
    off = OmsLiveRuntime(service, clock=lambda: now[0], identity="123:456")
    assert await off.start() is False
    return now, broker, service, off, redis


@pytest.mark.asyncio
async def test_real_gateway_on_off_delayed_guard_keeps_fresh_empty_proof(guard_context, monkeypatch):
    from project_mai_tai.momentum_live import runtime as module

    now, broker, service, off, redis = await started_off()
    real_sleep = asyncio.sleep
    ticks = asyncio.Queue()
    sleeps = []

    async def cadence_sleep(seconds):
        sleeps.append(seconds)
        await ticks.get()

    monkeypatch.setattr(module.asyncio, "sleep", cadence_sleep)
    daily, manifest, settings, _, guard, _, proc, unit = guard_context
    settings.momentum_live_enabled = False
    settings.market_data_momentum_warning_enabled = False
    monkeypatch.setattr(daily.wall_time, "time_ns", off.now_ns)
    synchronous = GuardRedis(redis)
    manifest["gateway_identity"] = {
        "version": 1, "unit": "project-mai-tai-market-data.service",
        "main_pid": 123, "start_ticks": 456,
        "exec_main_start_timestamp": unit["ExecMainStartTimestamp"],
    }
    gateway_check = daily.verify_gateway_identity
    owner_check = daily.verify_live_owner_scope
    monkeypatch.setattr(daily, "verify_gateway_identity",
                        lambda m, s: gateway_check(m, s, proc_root=proc))
    monkeypatch.setattr(daily, "verify_live_owner_scope",
                        lambda m, s, r, g, o: owner_check(m, s, r, g, o, proc_root=proc))
    guard.Blind = ValueError
    guard._owners = lambda r, prefix: {name: set(json.loads(value)) for name, value in
        r.hgetall(prefix + ":market-data-subscription-owners").items()
        if not name.startswith("_")}
    guard.RedisSafety = lambda _: SimpleNamespace(sample=lambda: (None, {}))
    guard.LiveSignals = lambda *_: SimpleNamespace(heartbeat=lambda _: ("healthy", 0, {}))

    def dryrun_owner_gate():
        owners = {name: set(json.loads(value)) for name, value in
                  synchronous.hgetall("mai_tai:market-data-subscription-owners").items()
                  if not name.startswith("_")}
        assert owners["momentum-live"] == set()
        assert daily.prestart_checks(manifest, guard, settings, synchronous, now[0])[0] == unit

    try:
        assert off.actor is None
        assert service._momentum_live_actor is None
        dryrun_owner_gate()
        for _ in range(2):
            now[0] += timedelta(seconds=70)  # More than twice the 30.819 s guard bound.
            with pytest.raises(ValueError, match="gateway owners"):
                dryrun_owner_gate()
            prior = len(redis.entries)
            ticks.put_nowait(None)
            for _ in range(1000):
                await real_sleep(0.001)
                if len(redis.entries) > prior:
                    break
            assert len(redis.entries) > prior
            dryrun_owner_gate()
        assert sleeps and set(sleeps) == {1}
        assert off.publication_task is not None
        assert broker.sent == [] and broker.cancelled == []
        assert off.actor is None and service._momentum_live_quote_dispatch is None
    finally:
        await off.close()
    assert off.publication_task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["db", "held", "unknown"])
async def test_off_cadence_never_refreshes_failed_empty_proof(failure, monkeypatch):
    from project_mai_tai.momentum_live import runtime as module

    now, broker, service, off, redis = await started_off()
    real_sleep = asyncio.sleep
    ticks = asyncio.Queue()
    attempted = asyncio.Event()
    original = off.db.prove_empty_owned_state
    if failure in {"held", "unknown"}:
        with service.session_factory() as session:
            if failure == "held":
                strategy = service.store.ensure_strategy(session, STRATEGY_CODE, name="Momentum LIVE",
                                                         execution_mode="live", metadata_json={})
                account = service.store.ensure_broker_account(session, "live:orb", provider="webull",
                                                             environment="live")
                session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
                                            symbol="SCNI", quantity=Decimal(3), average_price=Decimal(3)))
            else:
                book = ledger()
                book.claim(signal(), now_ns=10)
                assert off.db.claim(session, episode=book.snapshot()[0], account="live:orb",
                                    provider="webull", signal={})
            session.commit()

    async def sleep(_seconds):
        await ticks.get()

    def refuse(session):
        attempted.set()
        if failure == "db":
            raise OperationalError("controlled_read", {}, RuntimeError("unreadable"))
        return original(session)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    monkeypatch.setattr(off.db, "prove_empty_owned_state", refuse)
    prior = list(redis.entries)
    try:
        now[0] += timedelta(seconds=70)
        ticks.put_nowait(None)
        for _ in range(1000):
            await real_sleep(0.001)
            if attempted.is_set():
                break
        assert attempted.is_set()
        await real_sleep(0.01)
        assert redis.entries == prior
        assert off.actor is None and broker.sent == []
    finally:
        await off.close()
    assert off.publication_task.done()


@pytest.mark.asyncio
async def test_off_start_db_failure_never_creates_proof_task(monkeypatch):
    _, broker, service, on = setup(enabled=False)
    redis = OwnerRedis()
    service.redis = redis

    def unreadable(_session):
        raise OperationalError("controlled_read", {}, RuntimeError("unreadable"))

    monkeypatch.setattr(on.db, "prove_empty_owned_state", unreadable)
    with pytest.raises(OperationalError):
        await on.start()
    assert on.actor is None and on.publication_task is None
    assert redis.entries == [] and broker.sent == []


@pytest.mark.asyncio
async def test_oms_cleanup_stops_empty_off_task_even_when_runtime_close_fails(monkeypatch):
    _, broker, service, off, _ = await started_off()
    task = off.publication_task

    async def broken_close():
        raise RuntimeError("controlled close failure")

    monkeypatch.setattr(off, "close", broken_close)
    await service._close_momentum_live_runtime(off)
    assert task.done() and off.actor is None and broker.sent == []


@pytest.mark.asyncio
async def test_no_prior_proof_off_preserves_no_task_no_owner_write():
    _, broker, service, runtime = setup(enabled=False)
    redis = OwnerRedis()
    service.redis = redis
    assert await runtime.start() is False
    assert runtime.actor is None and runtime.publication_task is None
    assert redis.entries == [] and broker.sent == []
    await runtime.close()


@pytest.mark.asyncio
async def test_invalid_prior_proof_off_cannot_start_cadence():
    _, broker, service, runtime = setup(enabled=False)
    redis = OwnerRedis()
    redis.entries.append(("mai_tai:momentum-live-owner-proof", {}, {}))
    service.redis = redis
    with pytest.raises(Refusal, match="previous_proof_UNKNOWN"):
        await runtime.start()
    assert runtime.actor is None and runtime.publication_task is None
    assert len(redis.entries) == 1 and broker.sent == []


@pytest.mark.asyncio
async def test_original_one_shot_mutation_is_caught(guard_context, monkeypatch):
    from project_mai_tai.momentum_live import runtime as module

    source = textwrap.dedent(inspect.getsource(OmsLiveRuntime._cleanup_empty_boot_owner))
    line = "    self.publication_task = asyncio.create_task(self._publish_empty_off_loop())\n"
    assert line in source
    namespace = dict(module.__dict__)
    exec(source.replace(line, ""), namespace)
    monkeypatch.setattr(OmsLiveRuntime, "_cleanup_empty_boot_owner",
                        namespace["_cleanup_empty_boot_owner"])
    with pytest.raises(AssertionError):
        await test_real_gateway_on_off_delayed_guard_keeps_fresh_empty_proof(guard_context, monkeypatch)
